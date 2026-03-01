"""
Proactive scheduler — lets the assistant reach out to you unprompted.

Two modes:
1. One-shot reminders: scheduled at a specific datetime (e.g. "remind me at 3pm")
2. Periodic jobs: run on a cron-style schedule (daily briefing, overdue task check, etc.)
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Callable, Awaitable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from config import settings
from src.integrations.todoist import TodoistClient
from src.integrations.calendar import CalendarClient

logger = logging.getLogger(__name__)

# Type alias for the "send a message to the user" function
SendFn = Callable[[str], Awaitable[None]]


class ProactiveScheduler:
    def __init__(
        self,
        send_fn: SendFn,
        todoist: TodoistClient,
        calendar: CalendarClient,
    ) -> None:
        self._send = send_fn
        self._todoist = todoist
        self._calendar = calendar
        self._scheduler = AsyncIOScheduler(timezone=settings.agent_timezone)
        self._setup_periodic_jobs()

    def start(self) -> None:
        self._scheduler.start()
        logger.info("Proactive scheduler started")

    def shutdown(self) -> None:
        self._scheduler.shutdown(wait=False)

    # ------------------------------------------------------------------
    # Public: one-shot reminders (called by the agent via tool)
    # ------------------------------------------------------------------

    async def schedule_reminder(self, message: str, when: datetime) -> None:
        """Schedule a one-shot reminder at a specific datetime."""
        job_id = f"reminder_{when.isoformat()}"
        self._scheduler.add_job(
            self._send_reminder,
            trigger=DateTrigger(run_date=when, timezone=settings.agent_timezone),
            id=job_id,
            replace_existing=True,
            kwargs={"message": message},
        )
        logger.info("Scheduled reminder at %s: %s", when, message[:60])

    async def _send_reminder(self, message: str) -> None:
        await self._send(f"⏰ **Reminder:** {message}")

    # ------------------------------------------------------------------
    # Periodic jobs — edit these to customise your briefings
    # ------------------------------------------------------------------

    def _setup_periodic_jobs(self) -> None:
        # Morning briefing: 8:30 AM every weekday
        self._scheduler.add_job(
            self._morning_briefing,
            trigger=CronTrigger(
                day_of_week="mon-fri",
                hour=8,
                minute=30,
                timezone=settings.agent_timezone,
            ),
            id="morning_briefing",
            replace_existing=True,
        )

        # Evening wrap-up: 6:00 PM every weekday
        self._scheduler.add_job(
            self._evening_wrapup,
            trigger=CronTrigger(
                day_of_week="mon-fri",
                hour=18,
                minute=0,
                timezone=settings.agent_timezone,
            ),
            id="evening_wrapup",
            replace_existing=True,
        )

        # Overdue task nudge: every day at 10 AM (including weekends)
        self._scheduler.add_job(
            self._overdue_nudge,
            trigger=CronTrigger(
                hour=10,
                minute=0,
                timezone=settings.agent_timezone,
            ),
            id="overdue_nudge",
            replace_existing=True,
        )

    async def _morning_briefing(self) -> None:
        """Good morning message with today's agenda."""
        try:
            tasks = await self._todoist.get_today_tasks()
            events = await self._calendar.get_today_events()

            task_summary = await self._todoist.format_tasks_summary(tasks)
            event_summary = self._calendar.format_events_summary(events)

            lines = ["☀️ **Good morning! Here's your day:**", ""]
            if events:
                lines += ["**Calendar**", event_summary, ""]
            if tasks:
                lines += ["**Today's tasks**", task_summary]
            else:
                lines.append("No tasks due today — enjoy the breathing room!")

            await self._send("\n".join(lines))
        except Exception:
            logger.exception("Morning briefing failed")

    async def _evening_wrapup(self) -> None:
        """End-of-day nudge: what's still open?"""
        try:
            tasks = await self._todoist.get_today_tasks()
            incomplete = [t for t in tasks if not t.get("is_completed")]

            if not incomplete:
                await self._send("🌇 **Evening check-in:** All done for today — great work!")
                return

            summary = await self._todoist.format_tasks_summary(incomplete)
            await self._send(
                f"🌇 **Evening check-in:** You still have {len(incomplete)} open task(s):\n\n"
                f"{summary}\n\n"
                "Want to reschedule, delegate, or just leave them for tomorrow?"
            )
        except Exception:
            logger.exception("Evening wrap-up failed")

    async def _overdue_nudge(self) -> None:
        """Nudge if there are overdue tasks (skip if none)."""
        try:
            overdue = await self._todoist.get_overdue_tasks()
            if not overdue:
                return
            summary = await self._todoist.format_tasks_summary(overdue)
            await self._send(
                f"📌 **Heads up:** You have {len(overdue)} overdue task(s):\n\n{summary}\n\n"
                "Want to tackle them, reschedule, or drop any?"
            )
        except Exception:
            logger.exception("Overdue nudge failed")
