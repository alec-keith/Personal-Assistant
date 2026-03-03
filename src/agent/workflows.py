"""
Roman-Elite v1.1: Elite scheduled workflows.

These replace the static string builders in the scheduler for high-value
recurring outputs (morning briefing, weekly audit, inbox zero).

Each workflow:
1. Gathers data from integrations (calendar, todoist, email, weather, memory)
2. Fans out to specialists (parallel for daily briefing, sequential for others)
3. Narrative synthesizes the final output
"""

import asyncio
import logging
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

import anthropic

from config import settings
from src.integrations.todoist import TodoistClient
from src.integrations.calendar import CalendarClient
from src.memory.store import MemoryStore
from .specialists import call_specialists_parallel, call_specialist, format_specialist_outputs

logger = logging.getLogger(__name__)


async def _gather_daily_data(
    todoist: TodoistClient,
    calendar: CalendarClient,
    memory: MemoryStore,
    email_manager=None,
) -> dict[str, str]:
    """Gather all data needed for a daily briefing, in parallel."""
    tz = ZoneInfo(settings.agent_timezone)
    yesterday = (datetime.now(tz) - timedelta(days=1)).date()

    tasks = []
    task_names = []

    # Calendar events today
    async def get_cal():
        events = await calendar.get_today_events()
        return calendar.format_events_summary(events)
    tasks.append(get_cal())
    task_names.append("calendar_today")

    # Todoist tasks
    async def get_tasks():
        t = await todoist.get_today_tasks()
        return await todoist.format_tasks_summary(t)
    tasks.append(get_tasks())
    task_names.append("todoist_today")

    # Overdue tasks
    async def get_overdue():
        t = await todoist.get_overdue_tasks()
        if not t:
            return "No overdue tasks."
        return await todoist.format_tasks_summary(t)
    tasks.append(get_overdue())
    task_names.append("todoist_overdue")

    # Unread emails
    async def get_emails():
        if not email_manager or not email_manager.available:
            return "Email not configured."
        return await email_manager.list_emails(unread_only=True, count=15)
    tasks.append(get_emails())
    task_names.append("email_unread")

    # Weather
    async def get_weather():
        try:
            from src.integrations.weather import get_weather
            return await get_weather(settings.user_location, days=1)
        except Exception:
            return "Weather unavailable."
    tasks.append(get_weather())
    task_names.append("weather")

    # Working memory (week priorities)
    async def get_priorities():
        result = await memory.get_memory_by_key("working", "week_priorities")
        return result or "No week priorities set."
    tasks.append(get_priorities())
    task_names.append("week_priorities")

    # Yesterday's episodic log
    async def get_yesterday():
        results = await memory.search_memory(
            f"daily plan {yesterday.isoformat()}",
            stores=["episodic_log"],
            n=3,
        )
        if results:
            return "\n".join(r["content"] for r in results)
        return "No episodic log from yesterday."
    tasks.append(get_yesterday())
    task_names.append("yesterday_log")

    results = await asyncio.gather(*tasks, return_exceptions=True)
    data = {}
    for name, result in zip(task_names, results):
        if isinstance(result, Exception):
            logger.warning("Data gather for %s failed: %s", name, result)
            data[name] = f"{name}: unavailable"
        else:
            data[name] = result

    return data


async def run_daily_briefing(
    client: anthropic.AsyncAnthropic,
    todoist: TodoistClient,
    calendar: CalendarClient,
    memory: MemoryStore,
    email_manager=None,
) -> str:
    """
    Full daily briefing workflow — fans out to ALL specialists in parallel.

    1. Gather data (calendar, tasks, email, weather, working memory, yesterday's log)
    2. Fan out to all analysis specialists in parallel
    3. Narrative synthesizes into Roman's voice
    4. Log today's plan to episodic memory
    """
    tz = ZoneInfo(settings.agent_timezone)
    day_name = datetime.now(tz).strftime("%A")

    # Step 1: Gather data
    data = await _gather_daily_data(todoist, calendar, memory, email_manager)

    context = f"Today is {day_name}. Generate the daily briefing.\n\n"
    for key, val in data.items():
        context += f"--- {key} ---\n{val}\n\n"

    # Step 2: Fan out to all analysis specialists
    all_specialists = [
        "Roman-Exec", "Roman-Strategy", "Roman-Systems",
        "Roman-Health", "Roman-Relationships", "Roman-Finance", "Roman-Critic",
    ]
    outputs = await call_specialists_parallel(client, all_specialists, context)

    # Step 3: Narrative synthesis
    formatted = format_specialist_outputs(outputs)
    narrative_context = (
        f"Here are all specialist analyses for {day_name}'s daily briefing:\n\n"
        f"{formatted}\n\n"
        f"Raw data:\n{context}\n\n"
        f"Synthesize into a single cohesive morning briefing in Roman's voice.\n"
        f"Include: top 3 outcomes, time-block plan with buffers, cuts list, "
        f"one health action, one relationship action.\n"
        f"Plain text for iMessage. No markdown headers. Keep it tight but complete."
    )
    narrative = await call_specialist(
        client, "Roman-Narrative", narrative_context,
        model="claude-sonnet-4-6",
    )
    briefing_text = narrative.get("narrative", formatted)

    # Step 4: Log to episodic memory
    today = date.today().isoformat()
    try:
        await memory.save_memory(
            store="episodic_log",
            content=f"[daily_plan:{today}] {briefing_text[:2000]}",
            metadata={"date": today, "type": "daily_plan"},
            tags=["daily_plan", today],
        )
    except Exception:
        logger.warning("Failed to log daily plan to episodic memory")

    return briefing_text


async def run_weekly_audit(
    client: anthropic.AsyncAnthropic,
    todoist: TodoistClient,
    calendar: CalendarClient,
    memory: MemoryStore,
    email_manager=None,
) -> str:
    """
    Weekly audit workflow — sequential specialist passes.
    Exec → Strategy → Health → Relationships → Critic → Narrative
    """
    tz = ZoneInfo(settings.agent_timezone)

    # Gather week data
    async def get_week_events():
        events = await calendar.list_events(days_ahead=7)
        return calendar.format_events_summary(events)

    async def get_all_tasks():
        t = await todoist.list_tasks()
        return await todoist.format_tasks_summary(t)

    async def get_week_episodes():
        results = await memory.search_memory("weekly review wins misses lessons", stores=["episodic_log"], n=10)
        if results:
            return "\n".join(r["content"] for r in results)
        return "No episodic data this week."

    async def get_patterns():
        patterns = await memory.get_patterns()
        if patterns:
            return "\n".join(f"- {p['content']} (seen {p['metadata'].get('occurrences', '?')}x)" for p in patterns)
        return "No patterns recorded yet."

    events, tasks, episodes, patterns = await asyncio.gather(
        get_week_events(), get_all_tasks(), get_week_episodes(), get_patterns(),
        return_exceptions=True,
    )

    context = (
        f"Weekly audit for the week of {datetime.now(tz).strftime('%B %-d, %Y')}.\n\n"
        f"--- Calendar (next 7 days) ---\n{events if not isinstance(events, Exception) else 'unavailable'}\n\n"
        f"--- All Open Tasks ---\n{tasks if not isinstance(tasks, Exception) else 'unavailable'}\n\n"
        f"--- This Week's Episodes ---\n{episodes if not isinstance(episodes, Exception) else 'unavailable'}\n\n"
        f"--- Behavioral Patterns ---\n{patterns if not isinstance(patterns, Exception) else 'unavailable'}\n\n"
    )

    # Sequential specialist passes
    specialist_order = [
        "Roman-Exec", "Roman-Strategy", "Roman-Health",
        "Roman-Relationships", "Roman-Critic",
    ]
    outputs = []
    accumulated = context
    for spec_name in specialist_order:
        result = await call_specialist(client, spec_name, accumulated)
        outputs.append(result)
        accumulated = context + "\n\n--- Prior Specialist Outputs ---\n" + format_specialist_outputs(outputs)

    # Narrative synthesis
    formatted = format_specialist_outputs(outputs)
    narrative_context = (
        f"Here are the specialist analyses for the weekly audit:\n\n{formatted}\n\n"
        f"Synthesize into a weekly audit report in Roman's voice.\n"
        f"Include: what moved vs stalled, time allocation ROI, systems to build, "
        f"health load check, relationship deposit plan, next week's top outcomes.\n"
        f"Plain text for iMessage. Be direct."
    )
    narrative = await call_specialist(client, "Roman-Narrative", narrative_context, model="claude-sonnet-4-6")

    return narrative.get("narrative", formatted)


async def run_inbox_zero(
    client: anthropic.AsyncAnthropic,
    email_manager,
    todoist: TodoistClient,
) -> str:
    """
    Inbox zero workflow — Exec → Systems → Critic.
    Triages all unread emails into 6 bins: respond_now, draft, taskify, delegate, archive, follow_up.
    """
    if not email_manager or not email_manager.available:
        return "Email not configured — can't run inbox zero."

    emails = await email_manager.list_emails(unread_only=True, count=30)
    tasks = await todoist.list_tasks()
    task_summary = await todoist.format_tasks_summary(tasks)

    context = (
        f"Run inbox zero triage on these unread emails:\n\n{emails}\n\n"
        f"Current open tasks for context:\n{task_summary}\n\n"
        f"Triage into 6 bins: respond_now, draft, taskify, delegate, archive, follow_up.\n"
        f"For respond_now/draft: suggest the reply content.\n"
        f"For taskify: suggest the Todoist task with project and priority.\n"
        f"Anything >2 minutes becomes a task or draft, not an immediate response."
    )

    specialist_order = ["Roman-Exec", "Roman-Systems", "Roman-Critic"]
    outputs = []
    accumulated = context
    for spec_name in specialist_order:
        result = await call_specialist(client, spec_name, accumulated)
        outputs.append(result)
        accumulated = context + "\n\n--- Prior Outputs ---\n" + format_specialist_outputs(outputs)

    formatted = format_specialist_outputs(outputs)
    narrative_context = (
        f"Here's the inbox zero triage analysis:\n\n{formatted}\n\n"
        f"Synthesize into a clear action list in Roman's voice. "
        f"Group by bin. Be specific about what to do with each email."
    )
    narrative = await call_specialist(client, "Roman-Narrative", narrative_context, model="claude-sonnet-4-6")

    return narrative.get("narrative", formatted)
