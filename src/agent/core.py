"""
Agent core — the brain of the assistant.

Orchestrates:
  - Claude API with tool use
  - Memory (search + save)
  - Todoist
  - Calendar
  - Proactive scheduling
"""

import asyncio
import base64
import json
import logging
from datetime import datetime
from typing import Callable, Awaitable
from zoneinfo import ZoneInfo

import anthropic

from config import settings
from src.memory.store import MemoryStore
from src.integrations.todoist import TodoistClient
from src.integrations.calendar import CalendarClient
from src.integrations.clickup import ClickUpClient
from src.integrations.email import EmailManager
from .prompts import build_system_prompt
from .tools import TOOLS

logger = logging.getLogger(__name__)

# Short-term in-memory conversation history (per session, not persisted)
# Each item: {"role": "user"|"assistant", "content": str|list}
MAX_HISTORY = 20  # messages kept in context window (older turns are recalled via memory search)


class AgentCore:
    def __init__(
        self,
        memory: MemoryStore,
        todoist: TodoistClient,
        calendar: CalendarClient,
        clickup: ClickUpClient | None = None,
        email: EmailManager | None = None,
        # Injected so the agent can schedule proactive messages
        schedule_reminder_fn: Callable[[str, datetime], Awaitable[None]] | None = None,
    ) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        self._memory = memory
        self._todoist = todoist
        self._calendar = calendar
        self._clickup = clickup
        self._email = email
        self._schedule_reminder = schedule_reminder_fn
        self._scheduler = None  # injected from main.py after creation
        self._history: list[dict] = []
        self._tz = ZoneInfo(settings.agent_timezone)
        self._user_profile: str = ""  # cached from DB; refreshed on each message + after update
        self._current_model: str = settings.claude_model_simple  # set per-message by router

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def handle_message(
        self,
        user_text: str,
        images: list[tuple[bytes, str]] | None = None,
        for_voice: bool = False,
    ) -> str:
        """
        Process one user message and return the assistant's reply.

        images: optional list of (raw_bytes, media_type) — e.g. (b"...", "image/png").
                Passed as base64 blocks in the Claude multimodal content array.
        for_voice: when True, forces the fast model — voice replies must be quick.
        """
        self._user_profile = await self._memory.get_profile()

        # Images always need the complex model; voice always uses the fast model;
        # otherwise route by content heuristic.
        if images:
            self._current_model = settings.claude_model_complex
        elif for_voice:
            self._current_model = settings.claude_model_simple
        else:
            self._current_model = self._select_model(user_text)

        # Build message content — multimodal if images present
        if images:
            content: str | list = []
            for img_bytes, media_type in images:
                content.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": base64.standard_b64encode(img_bytes).decode("utf-8"),
                    },
                })
            content.append({
                "type": "text",
                "text": user_text.strip() or "What's in this image?",
            })
        else:
            content = user_text

        self._history.append({"role": "user", "content": content})
        self._trim_history()

        response_text = await self._run_agent_loop()

        self._history.append({"role": "assistant", "content": response_text})

        # For the memory summary, describe images as text so it's searchable
        summary_input = (
            f"[Shared {len(images)} image(s)] {user_text}" if images else user_text
        )
        asyncio.create_task(self._save_conversation_summary(summary_input, response_text))

        return response_text

    # ------------------------------------------------------------------
    # Agentic loop
    # ------------------------------------------------------------------

    async def _run_agent_loop(self) -> str:
        """
        Run the Claude API with tool use until the model stops calling tools.
        Returns the final text response.
        """
        messages = list(self._history)

        while True:
            response = await self._client.messages.create(
                model=self._current_model,
                max_tokens=4096,
                system=build_system_prompt(self._calendar.calendar_names(), self._user_profile),
                tools=TOOLS,
                messages=messages,
            )

            # Collect any text content from this turn
            text_parts = [
                block.text
                for block in response.content
                if block.type == "text"
            ]

            if response.stop_reason == "end_turn":
                return "\n".join(text_parts) if text_parts else "(no response)"

            if response.stop_reason == "tool_use":
                # Execute all tool calls in this response
                tool_results = await self._execute_tool_calls(response.content)

                # Append assistant turn + tool results to messages
                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": tool_results})
                continue

            # Unexpected stop reason
            logger.warning("Unexpected stop reason: %s", response.stop_reason)
            return "\n".join(text_parts) if text_parts else "(no response)"

    # ------------------------------------------------------------------
    # Tool dispatch
    # ------------------------------------------------------------------

    async def _execute_tool_calls(self, content_blocks: list) -> list[dict]:
        """Run all tool_use blocks concurrently and return tool_result blocks."""
        tool_tasks = []
        for block in content_blocks:
            if block.type == "tool_use":
                tool_tasks.append(self._call_tool(block.name, block.input, block.id))

        results = await asyncio.gather(*tool_tasks, return_exceptions=True)

        tool_results = []
        for block, result in zip(
            [b for b in content_blocks if b.type == "tool_use"], results
        ):
            if isinstance(result, Exception):
                logger.exception("Tool %s failed", block.name)
                output = f"Error: {result}"
            else:
                output = result

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": output if isinstance(output, str) else json.dumps(output),
            })

        return tool_results

    async def _call_tool(self, name: str, inputs: dict, tool_id: str) -> str:
        logger.info("Calling tool: %s(%s)", name, json.dumps(inputs)[:120])

        # --- Profile ---
        if name == "get_profile":
            profile = await self._memory.get_profile()
            return profile if profile else "No profile saved yet."

        if name == "update_profile":
            await self._memory.update_profile(inputs["content"])
            self._user_profile = inputs["content"]  # update cache immediately
            return "Profile updated."

        # --- Memory ---
        if name == "search_memory":
            query = inputs["query"]
            collection = inputs.get("collection", "both")
            results = []
            if collection in ("conversations", "both"):
                convs = await self._memory.search_conversations(query)
                if convs:
                    results.append("Past conversations:\n" + "\n---\n".join(convs))
            if collection in ("notes", "both"):
                notes = await self._memory.search_notes(query)
                if notes:
                    results.append("Saved notes:\n" + "\n---\n".join(notes))
            return "\n\n".join(results) if results else "No relevant memories found."

        if name == "save_note":
            doc_id = await self._memory.save_note(inputs["note"], inputs.get("tags"))
            return f"Note saved (id={doc_id})"

        if name == "list_recent_notes":
            notes = await self._memory.list_recent_notes(inputs.get("limit", 10))
            if not notes:
                return "No notes saved yet."
            lines = [f"- [{n['metadata'].get('timestamp', '?')[:10]}] {n['content']}" for n in notes]
            return "\n".join(lines)

        # --- Todoist ---
        if name == "list_tasks":
            tasks = await self._todoist.list_tasks(
                filter_str=inputs.get("filter_str"),
                project_id=inputs.get("project_id"),
            )
            return await self._todoist.format_tasks_summary(tasks)

        if name == "add_task":
            task = await self._todoist.add_task(
                content=inputs["content"],
                due_string=inputs.get("due_string"),
                priority=inputs.get("priority", 1),
                labels=inputs.get("labels"),
                description=inputs.get("description"),
                project_id=inputs.get("project_id"),
            )
            return f"Task added: '{task['content']}' (id={task['id']})"

        if name == "complete_task":
            await self._todoist.complete_task(inputs["task_id"])
            return f"Task {inputs['task_id']} marked complete."

        if name == "update_task":
            task_id = inputs.pop("task_id")
            await self._todoist.update_task(task_id, **inputs)
            return f"Task {task_id} updated."

        if name == "list_projects":
            projects = await self._todoist.list_projects()
            lines = [f"- [{p['id']}] {p['name']}" for p in projects]
            return "\n".join(lines) if lines else "No projects found."

        # --- Calendar ---
        if name == "list_calendars":
            names = self._calendar.calendar_names()
            if not names:
                return "No calendars available."
            return "Available calendars: " + ", ".join(names)

        if name == "list_events":
            events = await self._calendar.list_events(
                days_ahead=inputs.get("days_ahead", 7),
                calendar_name=inputs.get("calendar_name"),
            )
            return self._calendar.format_events_summary(events)

        if name == "get_today_events":
            events = await self._calendar.get_today_events()
            return self._calendar.format_events_summary(events)

        if name == "add_event":
            tz = self._tz
            start = datetime.fromisoformat(inputs["start_iso"]).replace(tzinfo=tz)
            end = None
            if inputs.get("end_iso"):
                end = datetime.fromisoformat(inputs["end_iso"]).replace(tzinfo=tz)
            result = await self._calendar.add_event(
                title=inputs["title"],
                start=start,
                end=end,
                description=inputs.get("description", ""),
                location=inputs.get("location", ""),
                calendar_name=inputs.get("calendar_name"),
            )
            cal = result.get("calendar", "")
            cal_tag = f" [{cal}]" if cal else ""
            return f"Event added: '{result['title']}' at {result['start']}{cal_tag}"

        # --- ClickUp ---
        if name == "clickup_list_tasks":
            if not self._clickup:
                return "ClickUp not configured — set CLICKUP_API_TOKEN."
            tasks = await self._clickup.list_tasks(
                list_id=inputs.get("list_id"),
                space_name=inputs.get("space_name"),
                statuses=inputs.get("statuses"),
                overdue_only=inputs.get("overdue_only", False),
            )
            return self._clickup.format_tasks_summary(tasks)

        if name == "clickup_get_lists":
            if not self._clickup:
                return "ClickUp not configured — set CLICKUP_API_TOKEN."
            return await self._clickup.format_lists_summary()

        if name == "clickup_create_task":
            if not self._clickup:
                return "ClickUp not configured — set CLICKUP_API_TOKEN."
            task = await self._clickup.create_task(
                list_id=inputs["list_id"],
                name=inputs["name"],
                description=inputs.get("description"),
                status=inputs.get("status"),
                priority=inputs.get("priority"),
                due_date_str=inputs.get("due_date_str"),
            )
            return f"Task created: '{task['name']}' (id={task['id']})"

        if name == "clickup_update_task":
            if not self._clickup:
                return "ClickUp not configured — set CLICKUP_API_TOKEN."
            task = await self._clickup.update_task(
                task_id=inputs["task_id"],
                name=inputs.get("name"),
                description=inputs.get("description"),
                status=inputs.get("status"),
                priority=inputs.get("priority"),
                due_date_str=inputs.get("due_date_str"),
            )
            return f"Task {inputs['task_id']} updated."

        if name == "clickup_delete_task":
            if not self._clickup:
                return "ClickUp not configured — set CLICKUP_API_TOKEN."
            await self._clickup.delete_task(inputs["task_id"])
            return f"Task {inputs['task_id']} deleted."

        # --- Email ---
        if name == "list_emails":
            if not self._email or not self._email.available:
                return "Email not configured. Run scripts/setup_gmail.py and/or set YAHOO_ACCOUNTS."
            return await self._email.list_emails(
                account_id=inputs.get("account_id"),
                folder=inputs.get("folder", "INBOX"),
                unread_only=inputs.get("unread_only", False),
                count=inputs.get("count", 10),
            )

        if name == "search_emails":
            if not self._email or not self._email.available:
                return "Email not configured."
            return await self._email.search_emails(
                query=inputs["query"],
                account_id=inputs.get("account_id"),
                count=inputs.get("count", 10),
            )

        if name == "read_email":
            if not self._email or not self._email.available:
                return "Email not configured."
            return await self._email.get_email(
                message_id=inputs["message_id"],
                account_id=inputs["account_id"],
            )

        if name == "send_email":
            if not self._email or not self._email.available:
                return "Email not configured."
            return await self._email.send_email(
                to=inputs["to"],
                subject=inputs["subject"],
                body=inputs["body"],
                account_id=inputs.get("account_id"),
            )

        if name == "archive_email":
            if not self._email or not self._email.available:
                return "Email not configured."
            return await self._email.archive_email(
                message_id=inputs["message_id"],
                account_id=inputs["account_id"],
            )

        if name == "delete_email":
            if not self._email or not self._email.available:
                return "Email not configured."
            return await self._email.delete_email(
                message_id=inputs["message_id"],
                account_id=inputs["account_id"],
            )

        # --- Scheduling ---
        if name == "schedule_reminder":
            if self._scheduler is None:
                return "Scheduler not available."
            when = datetime.fromisoformat(inputs["when_iso"]).replace(tzinfo=self._tz)
            await self._scheduler.schedule_reminder(inputs["message"], when)
            return f"Reminder scheduled for {when.strftime('%a %b %-d at %-I:%M %p')}"

        if name == "schedule_recurring":
            if self._scheduler is None:
                return "Scheduler not available."
            return await self._scheduler.add_recurring_job(
                job_id=inputs["job_id"],
                message=inputs["message"],
                description=inputs["description"],
                interval_minutes=inputs.get("interval_minutes"),
                cron=inputs.get("cron"),
                end_date=inputs.get("end_date"),
            )

        if name == "list_jobs":
            if self._scheduler is None:
                return "Scheduler not available."
            return self._scheduler.list_jobs()

        if name == "cancel_job":
            if self._scheduler is None:
                return "Scheduler not available."
            return await self._scheduler.cancel_job(inputs["job_id"])

        # --- Location reminders ---
        if name == "get_location_reminders":
            location = inputs["location"].lower().strip()
            reminders = await self._memory.get_and_clear_location_reminders(location)
            if not reminders:
                return f"No pending reminders for {location}."
            lines = "\n".join(f"- {r}" for r in reminders)
            return f"Pending reminders for {location} ({len(reminders)}):\n{lines}"

        # --- Weather / News / Stocks ---
        if name == "get_weather":
            from src.integrations.weather import get_weather
            return await get_weather(inputs["location"], inputs.get("days", 3))

        if name == "get_news":
            from src.integrations.news import get_news
            return await get_news(
                settings.newsapi_key,
                topic=inputs.get("topic", "top"),
                count=inputs.get("count", 8),
            )

        if name == "get_stock_quotes":
            from src.integrations.stocks import get_stock_quotes
            return await get_stock_quotes(inputs["symbols"])

        # --- Web ---
        if name == "web_search":
            return await self._web_search(inputs["query"], inputs.get("max_results", 5))

        if name == "fetch_page":
            return await self._fetch_page(inputs["url"])

        return f"Unknown tool: {name}"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _web_search(self, query: str, max_results: int = 5) -> str:
        try:
            from config import settings as _s
            if not _s.tavily_api_key:
                return "Web search not available — TAVILY_API_KEY not configured."
            from tavily import AsyncTavilyClient
            client = AsyncTavilyClient(api_key=_s.tavily_api_key)
            response = await client.search(query, max_results=max_results)
            results = response.get("results", [])
            if not results:
                return "No results found."
            lines = []
            for r in results:
                lines.append(f"**{r.get('title', 'No title')}**")
                lines.append(r.get("url", ""))
                content = r.get("content", "").strip()
                if content:
                    lines.append(content[:400])
                lines.append("")
            return "\n".join(lines).strip()
        except Exception as e:
            logger.exception("web_search failed")
            return f"Search error: {e}"

    async def _fetch_page(self, url: str) -> str:
        try:
            from config import settings as _s
            if not _s.tavily_api_key:
                return "Web fetch not available — TAVILY_API_KEY not configured."
            from tavily import AsyncTavilyClient
            client = AsyncTavilyClient(api_key=_s.tavily_api_key)
            response = await client.extract(urls=[url])
            results = response.get("results", [])
            if not results:
                return f"Could not extract content from {url}"
            content = results[0].get("raw_content", "").strip()
            return content[:6000] if len(content) > 6000 else content
        except Exception as e:
            logger.exception("fetch_page failed")
            return f"Fetch error: {e}"

    def _select_model(self, user_text: str) -> str:
        """
        Route to Opus for complex reasoning; Sonnet for simple tasks.

        Simple  → Sonnet: action commands, calendar/task/reminder ops, short factual questions
        Complex → Opus:   analysis, strategy, decisions, brainstorming, long brain dumps
        """
        text = user_text.lower().strip()

        # Explicit action commands → always Sonnet
        SIMPLE_STARTS = (
            "add ", "create ", "schedule ", "remind ", "set a ", "list ",
            "show ", "complete ", "mark ", "delete ", "cancel ", "move ",
            "update ", "check my", "what's on", "what do i have",
            "any tasks", "am i free", "add to ", "what are my",
        )
        SIMPLE_CONTAINS = (
            "add task", "add to todoist", "add to calendar", "add event",
            "remind me", "set a reminder", "send me a message",
            "what's on my calendar", "schedule a", "message me",
        )
        if len(text) < 250 and (
            any(text.startswith(p) for p in SIMPLE_STARTS)
            or any(p in text for p in SIMPLE_CONTAINS)
        ):
            logger.debug("Model router → Sonnet (simple action)")
            return settings.claude_model_simple

        # Deep reasoning signals → Opus
        COMPLEX_SIGNALS = (
            "help me think", "help me figure", "should i ", "what do you think",
            "give me advice", "advise me", "analyze", "analysis", "strategy",
            "strategize", "pros and cons", "tradeoff", "trade-off",
            "struggling", "stuck on", "stuck with", "decision", "decide",
            "how should i", "figure out", "make sense of", "work through",
            "talk through", "think through", "break down", "deep dive",
            "what would you do", "brainstorm", "brain dump",
        )
        if any(p in text for p in COMPLEX_SIGNALS):
            logger.debug("Model router → Opus (complex reasoning signal)")
            return settings.claude_model_complex

        # Long messages are usually brain dumps or nuanced requests → Opus
        if len(user_text) > 300:
            logger.debug("Model router → Opus (long message)")
            return settings.claude_model_complex

        # Default: Sonnet handles it
        logger.debug("Model router → Sonnet (default)")
        return settings.claude_model_simple

    def _trim_history(self) -> None:
        """Keep the in-context history from growing unbounded."""
        if len(self._history) > MAX_HISTORY:
            # Always keep the last MAX_HISTORY messages
            self._history = self._history[-MAX_HISTORY:]

    async def _save_conversation_summary(self, user_text: str, assistant_text: str) -> None:
        """Summarise and persist a conversation turn to memory."""
        try:
            summary_response = await self._client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=300,
                messages=[{
                    "role": "user",
                    "content": (
                        f"Write a 2-3 sentence memory note for this exchange. "
                        f"Preserve specific details: exact words used, names, numbers, tasks, dates, decisions. "
                        f"Start with the verbatim user message in quotes.\n\n"
                        f"User: {user_text[:600]}\n\nAssistant: {assistant_text[:600]}"
                    ),
                }],
            )
            summary = summary_response.content[0].text
            await self._memory.save_conversation_summary(summary)
        except Exception:
            logger.debug("Failed to save conversation summary", exc_info=True)
