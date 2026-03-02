"""
Proactive scheduler — lets the assistant reach out to you unprompted.

Three modes:
1. One-shot reminders: scheduled at a specific datetime ("remind me at 3pm")
2. Built-in periodic jobs: morning briefing, evening wrap-up, overdue nudge
3. Dynamic recurring jobs: Roman can add/cancel these at runtime and they persist
   across restarts via the PostgreSQL recurring_jobs table (or JSON fallback)
"""

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Callable, Awaitable
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from config import settings
from src.integrations.todoist import TodoistClient
from src.integrations.calendar import CalendarClient

logger = logging.getLogger(__name__)

SendFn = Callable[[str], Awaitable[None]]

# JSON fallback used when no database is available
JOBS_FILE = Path("data/recurring_jobs.json")


class ProactiveScheduler:
    def __init__(
        self,
        send_fn: SendFn,
        todoist: TodoistClient,
        calendar: CalendarClient,
        db=None,  # src.memory.database.Database | None
    ) -> None:
        self._send = send_fn
        self._todoist = todoist
        self._calendar = calendar
        self._db = db
        self._scheduler = AsyncIOScheduler(timezone=settings.agent_timezone)
        self._setup_builtin_jobs()

    async def initialize(self) -> None:
        """Load persistent jobs. Call this after the event loop is running."""
        await self._load_persistent_jobs()

    def start(self) -> None:
        self._scheduler.start()
        logger.info("Proactive scheduler started")

    def shutdown(self) -> None:
        self._scheduler.shutdown(wait=False)

    # ------------------------------------------------------------------
    # Public: one-shot reminder (called by agent via tool)
    # ------------------------------------------------------------------

    async def schedule_reminder(self, message: str, when: datetime) -> None:
        job_id = f"reminder_{when.isoformat()}"
        job_def = {
            "id": job_id,
            "message": message,
            "description": f"One-shot reminder at {when.isoformat()}",
            "trigger_type": "date",
            "trigger_args": {"run_date": when.isoformat()},
            "end_date": None,
        }
        await self._persist_job(job_def)
        self._scheduler.add_job(
            self._send_and_cleanup_reminder,
            trigger=DateTrigger(run_date=when, timezone=settings.agent_timezone),
            id=job_id,
            replace_existing=True,
            kwargs={"message": message, "job_id": job_id},
        )
        logger.info("Scheduled reminder at %s: %s", when, message[:60])

    # ------------------------------------------------------------------
    # Public: dynamic recurring jobs (called by agent via tool)
    # ------------------------------------------------------------------

    async def add_recurring_job(
        self,
        job_id: str,
        message: str,
        description: str,
        interval_minutes: int | None = None,
        cron: str | None = None,
        end_date: str | None = None,
    ) -> str:
        if not interval_minutes and not cron:
            return "Error: provide either interval_minutes or a cron expression."

        full_id = f"custom_{job_id}"

        if interval_minutes:
            trigger = IntervalTrigger(
                minutes=interval_minutes,
                timezone=settings.agent_timezone,
                end_date=end_date,
            )
            job_def = {
                "id": full_id,
                "message": message,
                "description": description,
                "trigger_type": "interval",
                "trigger_args": {"minutes": interval_minutes},
                "end_date": end_date,
            }
        else:
            try:
                trigger = CronTrigger.from_crontab(
                    cron,
                    timezone=settings.agent_timezone,
                    end_date=end_date,
                )
            except Exception as e:
                return f"Invalid cron expression '{cron}': {e}"
            job_def = {
                "id": full_id,
                "message": message,
                "description": description,
                "trigger_type": "cron",
                "trigger_args": {"crontab": cron},
                "end_date": end_date,
            }

        self._scheduler.add_job(
            self._dynamic_checkin,
            trigger=trigger,
            id=full_id,
            replace_existing=True,
            kwargs={"message": message},
        )

        await self._persist_job(job_def)

        next_run = self._scheduler.get_job(full_id)
        next_str = ""
        if next_run and next_run.next_run_time:
            next_str = f" — next run: {next_run.next_run_time.strftime('%-I:%M %p')}"
        logger.info("Added dynamic job '%s': %s", full_id, description)
        return f"Scheduled: {description}{next_str}"

    async def cancel_job(self, job_id: str) -> str:
        """Remove a dynamic recurring job by ID."""
        full_id = f"custom_{job_id}" if not job_id.startswith("custom_") else job_id

        removed_scheduler = False
        try:
            self._scheduler.remove_job(full_id)
            removed_scheduler = True
        except Exception:
            pass

        removed_db = await self._delete_job(full_id)

        if removed_scheduler or removed_db:
            return f"Cancelled job: {full_id}"
        return f"No job found with id '{job_id}'."

    def list_jobs(self) -> str:
        """List all active scheduled jobs."""
        lines = []
        for job in self._scheduler.get_jobs():
            next_run = ""
            if job.next_run_time:
                next_run = f" (next: {job.next_run_time.strftime('%a %-I:%M %p')})"

            if job.id.startswith("custom_"):
                desc = job.kwargs.get("message", job.id)[:60]
                lines.append(f"[custom] {job.id}: {desc}{next_run}")
            elif job.id.startswith("reminder_"):
                msg = job.kwargs.get("message", "")[:60]
                lines.append(f"[one-shot] {msg}{next_run}")
            else:
                lines.append(f"[built-in] {job.id}{next_run}")

        if not lines:
            return "No scheduled jobs."
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Persistence — PostgreSQL preferred, JSON file fallback
    # ------------------------------------------------------------------

    async def _persist_job(self, job_def: dict) -> None:
        if self._db is not None:
            try:
                async with self._db.pool.acquire() as conn:
                    await conn.execute(
                        """
                        INSERT INTO recurring_jobs
                            (id, message, description, trigger_type, trigger_args, end_date)
                        VALUES ($1, $2, $3, $4, $5::jsonb, $6)
                        ON CONFLICT (id) DO UPDATE SET
                            message=EXCLUDED.message,
                            description=EXCLUDED.description,
                            trigger_type=EXCLUDED.trigger_type,
                            trigger_args=EXCLUDED.trigger_args,
                            end_date=EXCLUDED.end_date
                        """,
                        job_def["id"],
                        job_def["message"],
                        job_def["description"],
                        job_def["trigger_type"],
                        json.dumps(job_def["trigger_args"]),
                        job_def.get("end_date"),
                    )
                return
            except Exception:
                logger.exception("Failed to persist job to DB, falling back to JSON")

        # JSON fallback
        jobs = self._read_jobs_file()
        jobs = [j for j in jobs if j["id"] != job_def["id"]]
        jobs.append(job_def)
        JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
        JOBS_FILE.write_text(json.dumps(jobs, indent=2))

    async def _delete_job(self, full_id: str) -> bool:
        if self._db is not None:
            try:
                async with self._db.pool.acquire() as conn:
                    result = await conn.execute(
                        "DELETE FROM recurring_jobs WHERE id = $1", full_id
                    )
                return result.split()[-1] != "0"
            except Exception:
                logger.exception("Failed to delete job from DB")

        # JSON fallback
        jobs = self._read_jobs_file()
        new_jobs = [j for j in jobs if j["id"] != full_id]
        removed = len(new_jobs) < len(jobs)
        if JOBS_FILE.exists():
            JOBS_FILE.write_text(json.dumps(new_jobs, indent=2))
        return removed

    async def _load_persistent_jobs(self) -> None:
        jobs: list[dict] = []

        if self._db is not None:
            try:
                async with self._db.pool.acquire() as conn:
                    rows = await conn.fetch("SELECT * FROM recurring_jobs")
                jobs = [
                    {
                        "id": r["id"],
                        "message": r["message"],
                        "description": r["description"],
                        "trigger_type": r["trigger_type"],
                        "trigger_args": dict(r["trigger_args"]),
                        "end_date": r["end_date"],
                    }
                    for r in rows
                ]
            except Exception:
                logger.exception("Failed to load jobs from DB, falling back to JSON")

        if not jobs:
            jobs = self._read_jobs_file()

        for job_def in jobs:
            try:
                end_date = job_def.get("end_date")
                trigger_type = job_def["trigger_type"]

                if trigger_type == "date":
                    run_date = datetime.fromisoformat(job_def["trigger_args"]["run_date"])
                    tz = ZoneInfo(settings.agent_timezone)
                    if run_date.tzinfo is None:
                        run_date = run_date.replace(tzinfo=tz)
                    now = datetime.now(tz)
                    age_seconds = (now - run_date).total_seconds()
                    if age_seconds > 3600:
                        # Over an hour late — stale, just delete
                        await self._delete_job(job_def["id"])
                        continue
                    elif age_seconds > 0:
                        # Missed but recent — fire immediately
                        asyncio.create_task(self._send_and_cleanup_reminder(
                            job_def["message"], job_def["id"]
                        ))
                        continue
                    else:
                        trigger = DateTrigger(run_date=run_date, timezone=settings.agent_timezone)
                        self._scheduler.add_job(
                            self._send_and_cleanup_reminder,
                            trigger=trigger,
                            id=job_def["id"],
                            replace_existing=True,
                            kwargs={"message": job_def["message"], "job_id": job_def["id"]},
                        )
                    continue

                if trigger_type == "interval":
                    trigger = IntervalTrigger(
                        **{k: v for k, v in job_def["trigger_args"].items()},
                        timezone=settings.agent_timezone,
                        end_date=end_date,
                    )
                else:
                    trigger = CronTrigger.from_crontab(
                        job_def["trigger_args"]["crontab"],
                        timezone=settings.agent_timezone,
                        end_date=end_date,
                    )
                self._scheduler.add_job(
                    self._dynamic_checkin,
                    trigger=trigger,
                    id=job_def["id"],
                    replace_existing=True,
                    kwargs={"message": job_def["message"]},
                )
            except Exception:
                logger.exception("Failed to load persistent job %s", job_def.get("id"))

        if jobs:
            logger.info("Loaded %d persistent recurring jobs", len(jobs))

    def _read_jobs_file(self) -> list[dict]:
        if not JOBS_FILE.exists():
            return []
        try:
            return json.loads(JOBS_FILE.read_text())
        except Exception:
            return []

    # ------------------------------------------------------------------
    # Internal job handlers
    # ------------------------------------------------------------------

    async def _dynamic_checkin(self, message: str) -> None:
        await self._send(message)

    async def _send_reminder(self, message: str) -> None:
        await self._send(f"⏰ {message}")

    async def _send_and_cleanup_reminder(self, message: str, job_id: str) -> None:
        await self._send(f"⏰ {message}")
        await self._delete_job(job_id)

    # ------------------------------------------------------------------
    # Built-in periodic jobs
    # ------------------------------------------------------------------

    def _setup_builtin_jobs(self) -> None:
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

        # Overdue task nudge: every day at 10 AM
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
        try:
            tasks = await self._todoist.get_today_tasks()
            events = await self._calendar.get_today_events()
            task_summary = await self._todoist.format_tasks_summary(tasks)
            event_summary = self._calendar.format_events_summary(events)

            lines = ["Good morning — here's your day:", ""]
            if events:
                lines += ["Calendar:", event_summary, ""]
            if tasks:
                lines += ["Today's tasks:", task_summary]
            else:
                lines.append("Nothing due today.")

            await self._send("\n".join(lines))
        except Exception:
            logger.exception("Morning briefing failed")

    async def _evening_wrapup(self) -> None:
        try:
            incomplete = await self._todoist.get_today_tasks()
            if not incomplete:
                await self._send("Evening check-in: all clear. Good work today.")
                return
            summary = await self._todoist.format_tasks_summary(incomplete)
            await self._send(
                f"Evening check-in — {len(incomplete)} open task(s) still on the board:\n\n"
                f"{summary}\n\nWant to reschedule, push anything, or call it done?"
            )
        except Exception:
            logger.exception("Evening wrap-up failed")

    async def _overdue_nudge(self) -> None:
        try:
            overdue = await self._todoist.get_overdue_tasks()
            if not overdue:
                return
            summary = await self._todoist.format_tasks_summary(overdue)
            await self._send(
                f"{len(overdue)} overdue task(s):\n\n{summary}\n\n"
                "Want to tackle them, reschedule, or drop any?"
            )
        except Exception:
            logger.exception("Overdue nudge failed")
