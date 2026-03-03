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
import re
from datetime import datetime, date, timedelta
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
from .router import build_orchestrator_routing_context, route_to_specialists_handler
from .tool_executor import ToolExecutor
from .onboarding import OnboardingManager

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Location reminder helpers (module-level so they're compiled once)
# ------------------------------------------------------------------

# "when I get home remind me to X"  /  "when I get home tomorrow remind me to X"
_LOC_RE = re.compile(
    r"when\s+i\s+(?:get|arrive at|am at|reach|get back to|get to)?\s*"
    r"(home|back home|the office|work|the gym|gym)[,.]?\s+"
    r"(?:(?:on\s+)?(?:tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday|next\s+\w+)\s+)?"
    r"remind\s+me\s+(?:to\s+)?(.+)",
    re.IGNORECASE,
)

# "remind me tomorrow when I get home to X"  /  "remind me when I get home to X"
_LOC_RE_2 = re.compile(
    r"remind\s+me\s+"
    r"(?:(?:on\s+)?(?:tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday|next\s+\w+)\s+)?"
    r"when\s+i\s+(?:get|arrive at|am at|reach|get back to|get to)?\s*"
    r"(home|back home|the office|work|the gym|gym)\s+(?:to\s+)?(.+)",
    re.IGNORECASE,
)

_DAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _parse_due_date(text: str, tz: ZoneInfo) -> str | None:
    """
    Scan text for date keywords and return YYYY-MM-DD, or None if no date found.
    Handles: tomorrow, weekday names (next occurrence), 'next <weekday>'.
    """
    today = datetime.now(tz).date()
    t = text.lower()

    if "tomorrow" in t:
        return (today + timedelta(days=1)).isoformat()

    for i, day in enumerate(_DAY_NAMES):
        if day in t:
            days_ahead = (i - today.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7  # same weekday → next week
            return (today + timedelta(days=days_ahead)).isoformat()

    return None


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
        onboarding: OnboardingManager | None = None,
        tool_executor: ToolExecutor | None = None,
        # Injected so the agent can schedule proactive messages
        schedule_reminder_fn: Callable[[str, datetime], Awaitable[None]] | None = None,
    ) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        self._memory = memory
        self._todoist = todoist
        self._calendar = calendar
        self._clickup = clickup
        self._email = email
        self._onboarding = onboarding
        self._tool_executor = tool_executor or ToolExecutor()
        self._schedule_reminder = schedule_reminder_fn
        self._scheduler = None  # injected from main.py after creation
        self._history: list[dict] = []
        self._tz = ZoneInfo(settings.agent_timezone)
        self._user_profile: str = ""  # cached from DB; refreshed on each message + after update
        self._current_model: str = settings.claude_model_simple  # set per-message by router
        # Orchestrator routing context (built once, cached)
        self._routing_context: str | None = None
        if settings.enable_specialist_routing:
            self._routing_context = build_orchestrator_routing_context()

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
        logger.info("[ORCHESTRATOR] Message received: %r (model=%s, routing=%s)",
                     user_text[:80], self._current_model,
                     "enabled" if self._routing_context else "disabled")

        # Location reminder fast-path: intercept before hitting Claude.
        # Handles both "when I get home remind me to X" and "remind me tomorrow when I get home to X".
        _loc_match = _LOC_RE.search(user_text) or _LOC_RE_2.search(user_text)
        if _loc_match and not images:
            raw_loc = _loc_match.group(1).lower().strip()
            location = "home" if "home" in raw_loc else raw_loc.replace("the ", "")
            reminder_text = _loc_match.group(2).strip().rstrip(".")
            due_date = _parse_due_date(user_text, self._tz)
            tags = [f"location:{location}"]
            if due_date:
                tags.append(f"due:{due_date}")
            logger.info("Location reminder fast-path: loc=%s due=%s task=%r", location, due_date, reminder_text)
            try:
                await self._memory.save_note(reminder_text, tags=tags)
                logger.info("Location reminder saved successfully")
            except Exception:
                logger.exception("Location reminder save_note failed")
            if due_date:
                d = date.fromisoformat(due_date)
                date_label = d.strftime("%A, %B %-d")
                return f"Got it — I'll remind you to {reminder_text} when you get {location} on {date_label}."
            return f"Got it — I'll remind you to {reminder_text} when you get {location}."

        # Images always need the complex model; voice always uses the fast model;
        # otherwise route by content heuristic.
        if images:
            self._current_model = settings.claude_model_complex
        elif for_voice:
            self._current_model = settings.claude_model_simple
        else:
            self._current_model = self._select_model(user_text)

        # Orchestrator routing rules are too complex for Haiku — bump to Sonnet minimum
        if self._routing_context and self._current_model == settings.claude_model_simple:
            self._current_model = settings.claude_model_complex
            logger.info("[ORCHESTRATOR] Bumped model to %s (routing requires Sonnet minimum)",
                         self._current_model)

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

        # Build onboarding context if active
        onboarding_context = None
        if self._onboarding and settings.enable_onboarding:
            try:
                ob_state = await self._onboarding.get_state()
                if ob_state["status"] == "in_progress":
                    onboarding_context = self._onboarding.build_prompt_section(ob_state)
            except Exception:
                logger.debug("Failed to get onboarding state", exc_info=True)

        # Voice: try Groq first (faster inference), fall back to Claude on any error
        if for_voice and settings.groq_api_key:
            try:
                response_text = await self._run_groq_voice_loop()
            except Exception:
                logger.warning("Groq voice loop failed, falling back to Claude", exc_info=True)
                response_text = await self._run_agent_loop(onboarding_context=onboarding_context)
        else:
            response_text = await self._run_agent_loop(onboarding_context=onboarding_context)

        # Strip internal task IDs before returning to user — model doesn't always follow the prompt
        response_text = re.sub(r"\s*\[id:[^\]]+\]", "", response_text)

        self._history.append({"role": "assistant", "content": response_text})

        # For the memory summary, describe images as text so it's searchable
        summary_input = (
            f"[Shared {len(images)} image(s)] {user_text}" if images else user_text
        )
        asyncio.create_task(self._save_conversation_summary(summary_input, response_text))

        return response_text

    # ------------------------------------------------------------------
    # Groq voice loop (fast inference, OpenAI-compatible format)
    # ------------------------------------------------------------------

    def _tools_to_openai_format(self) -> list[dict]:
        """Convert Claude tool schema to OpenAI/Groq function-calling format."""
        return [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
                },
            }
            for t in TOOLS
        ]

    def _history_for_groq(self) -> list[dict]:
        """
        Convert self._history (Claude format) to OpenAI-compatible messages for Groq.
        Handles plain strings, tool_use assistant turns, and tool_result user turns.
        Image content is dropped (voice doesn't need it).
        """
        result: list[dict] = []
        for msg in self._history:
            role = msg["role"]
            content = msg["content"]

            if isinstance(content, str):
                result.append({"role": role, "content": content})
                continue

            if not isinstance(content, list):
                continue

            if role == "user":
                tool_results = []
                text_parts = []
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == "tool_result":
                        tool_results.append({
                            "role": "tool",
                            "tool_call_id": item["tool_use_id"],
                            "content": str(item.get("content", "")),
                        })
                    elif item.get("type") == "text":
                        text_parts.append(item["text"])
                if tool_results:
                    result.extend(tool_results)
                elif text_parts:
                    result.append({"role": "user", "content": " ".join(text_parts)})

            elif role == "assistant":
                text_parts = []
                tool_calls = []
                for item in content:
                    item_type = getattr(item, "type", None) or (
                        item.get("type") if isinstance(item, dict) else None
                    )
                    if item_type == "text":
                        t = getattr(item, "text", None) or (
                            item.get("text") if isinstance(item, dict) else ""
                        )
                        if t:
                            text_parts.append(t)
                    elif item_type == "tool_use":
                        tid = getattr(item, "id", None) or (item.get("id") if isinstance(item, dict) else "")
                        tname = getattr(item, "name", None) or (item.get("name") if isinstance(item, dict) else "")
                        tinput = getattr(item, "input", None) or (item.get("input") if isinstance(item, dict) else {})
                        tool_calls.append({
                            "id": tid,
                            "type": "function",
                            "function": {"name": tname, "arguments": json.dumps(tinput)},
                        })
                msg_dict: dict = {"role": "assistant", "content": " ".join(text_parts)}
                if tool_calls:
                    msg_dict["tool_calls"] = tool_calls
                result.append(msg_dict)

        return result

    async def _run_groq_voice_loop(self) -> str:
        """
        Run voice inference via Groq (llama-3.3-70b-versatile).
        Uses OpenAI-compatible format with full tool support.
        Raises on failure so caller can fall back to Claude.
        """
        from groq import AsyncGroq

        client = AsyncGroq(api_key=settings.groq_api_key)
        system_prompt = build_system_prompt(
            self._calendar.calendar_names(),
            self._user_profile,
            routing_context=self._routing_context if settings.enable_specialist_routing else None,
        )
        messages: list[dict] = [{"role": "system", "content": system_prompt}]
        messages.extend(self._history_for_groq())
        tools = self._tools_to_openai_format()

        while True:
            response = await client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=messages,
                tools=tools,
                tool_choice="auto",
                max_tokens=1024,
            )
            choice = response.choices[0]

            if choice.finish_reason == "stop":
                return choice.message.content or "(no response)"

            if choice.finish_reason == "tool_calls":
                tool_calls = choice.message.tool_calls or []
                messages.append({
                    "role": "assistant",
                    "content": choice.message.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                        }
                        for tc in tool_calls
                    ],
                })
                for tc in tool_calls:
                    try:
                        inputs = json.loads(tc.function.arguments)
                    except json.JSONDecodeError:
                        inputs = {}
                    result = await self._call_tool(tc.function.name, inputs, tc.id)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result if isinstance(result, str) else json.dumps(result),
                    })
                continue

            return choice.message.content or "(no response)"

    # ------------------------------------------------------------------
    # Agentic loop
    # ------------------------------------------------------------------

    async def _run_agent_loop(self, onboarding_context: str | None = None) -> str:
        """
        Run the Claude API with tool use until the model stops calling tools.
        Returns the final text response.
        """
        messages = list(self._history)
        routing_ctx = self._routing_context if settings.enable_specialist_routing else None
        turn = 0

        logger.info("[AGENT_LOOP] Starting — model=%s routing=%s onboarding=%s",
                     self._current_model,
                     "injected" if routing_ctx else "off",
                     "active" if onboarding_context else "off")

        while True:
            turn += 1
            response = await self._client.messages.create(
                model=self._current_model,
                max_tokens=4096,
                system=build_system_prompt(
                    self._calendar.calendar_names(),
                    self._user_profile,
                    routing_context=routing_ctx,
                    onboarding_context=onboarding_context,
                ),
                tools=TOOLS,
                messages=messages,
            )

            # Collect any text content from this turn
            text_parts = [
                block.text
                for block in response.content
                if block.type == "text"
            ]

            tool_names = [b.name for b in response.content if b.type == "tool_use"]
            logger.info("[AGENT_LOOP] Turn %d — stop=%s tools=%s model=%s",
                         turn, response.stop_reason, tool_names or "none", response.model)

            if response.stop_reason == "end_turn":
                logger.info("[AGENT_LOOP] Complete — %d turns, response=%d chars",
                             turn, sum(len(t) for t in text_parts))
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

        # --- Tool Executor governance (confirmation gates, idempotency, rate limits) ---
        if self._tool_executor and settings.enable_tool_executor:
            gate_result = self._tool_executor.check(name, inputs, tool_id)
            if gate_result is not None:
                return gate_result.output

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
            self._tool_executor.record_execution(name, inputs, True)
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
            self._tool_executor.record_execution(name, inputs, True)
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
            result = await self._email.send_email(
                to=inputs["to"],
                subject=inputs["subject"],
                body=inputs["body"],
                account_id=inputs.get("account_id"),
            )
            self._tool_executor.record_execution(name, inputs, True)
            return result

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
            lines = "\n".join(f"- {r['content']}" for r in reminders)
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

        # --- Orchestrator Routing ---
        if name == "route_to_specialists":
            if not settings.enable_specialist_routing:
                return "Specialist routing is disabled."
            # Gather tool_pull data before calling specialists
            tool_data = await self._gather_tool_pull_data(inputs.get("tool_pulls", []))
            result = await route_to_specialists_handler(
                client=self._client,
                intent=inputs["intent"],
                level=inputs["level"],
                specialists=inputs["specialists"],
                tool_pulls=inputs.get("tool_pulls", []),
                context_summary=inputs["context_summary"],
                tool_data=tool_data,
            )
            return result

        # --- Onboarding ---
        if name == "start_onboarding":
            if not self._onboarding:
                return "Onboarding not available."
            state = await self._onboarding.start()
            return f"Onboarding started. Status: {state['status']}, Wave: {state.get('current_wave_id', 'none')}"

        if name == "onboarding_save_answer":
            if not self._onboarding:
                return "Onboarding not available."
            state = await self._onboarding.record_answer(
                question_id=inputs["question_id"],
                answer=inputs["answer_summary"],
                is_followup=inputs.get("is_followup", False),
            )
            # Check if wave is complete
            if self._onboarding.should_advance_wave(state):
                state = await self._onboarding.advance_to_next_wave()
                if state["status"] == "completed":
                    return "Answer saved. All onboarding waves complete! Onboarding finished."
                return f"Answer saved. Wave complete — moving to: {state.get('current_wave_id', 'done')}"
            answered, total = self._onboarding.get_total_progress(state)
            return f"Answer saved. Progress: {answered}/{total} questions."

        if name == "onboarding_advance":
            if not self._onboarding:
                return "Onboarding not available."
            if inputs.get("skip_wave"):
                state = await self._onboarding.advance_to_next_wave()
            else:
                state = await self._onboarding.skip_question()
                if self._onboarding.should_advance_wave(state):
                    state = await self._onboarding.advance_to_next_wave()
            if state["status"] == "completed":
                return "Onboarding complete!"
            return f"Advanced. Now on wave: {state.get('current_wave_id', 'done')}, question {state.get('question_index', 0) + 1}"

        # --- Elite Memory Stores ---
        if name == "write_long_term_memory":
            await self._memory.save_memory(
                store="long_term",
                content=inputs["content"],
                metadata={"category": inputs.get("category", "general")},
                tags=inputs.get("tags", []),
            )
            self._tool_executor.record_execution(name, inputs, True)
            return "Saved to long-term memory."

        if name == "write_working_memory":
            await self._memory.save_memory(
                store="working",
                content=inputs["content"],
                metadata={"key": inputs["key"], **(inputs.get("metadata") or {})},
                tags=[inputs["key"]],
            )
            self._tool_executor.record_execution(name, inputs, True)
            return f"Working memory updated: {inputs['key']}"

        if name == "log_episode":
            await self._memory.save_memory(
                store="episodic_log",
                content=f"[{inputs['episode_type']}:{inputs['date']}] {inputs['content']}",
                metadata={"date": inputs["date"], "type": inputs["episode_type"]},
                tags=[inputs["episode_type"], inputs["date"]],
            )
            self._tool_executor.record_execution(name, inputs, True)
            return f"Episode logged: {inputs['episode_type']} for {inputs['date']}"

        if name == "search_elite_memory":
            stores = inputs.get("stores") or ["long_term", "working", "episodic_log", "pattern_store"]
            results = await self._memory.search_memory(
                query=inputs["query"],
                stores=stores,
                n=inputs.get("n", 5),
            )
            if not results:
                return "No results found in elite memory stores."
            lines = []
            for r in results:
                store = r.get("store", "?")
                content = r.get("content", "")
                lines.append(f"[{store}] {content}")
            return "\n---\n".join(lines)

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

    async def _gather_tool_pull_data(self, tool_pulls: list[str]) -> dict[str, str]:
        """
        Gather data for the specialist context based on tool_pull keys.
        Executes data fetches in parallel and returns a dict of key → result.
        """
        if not tool_pulls:
            return {}

        async def pull(key: str) -> tuple[str, str]:
            try:
                if key == "todoist.read_open":
                    tasks = await self._todoist.list_tasks()
                    return key, await self._todoist.format_tasks_summary(tasks)
                elif key == "calendar.read_today":
                    events = await self._calendar.get_today_events()
                    return key, self._calendar.format_events_summary(events)
                elif key == "calendar.read_7d":
                    events = await self._calendar.list_events(days_ahead=7)
                    return key, self._calendar.format_events_summary(events)
                elif key == "email.read_headers_unread":
                    if self._email and self._email.available:
                        return key, await self._email.list_emails(unread_only=True, count=15)
                    return key, "Email not configured."
                else:
                    return key, f"Unknown tool_pull: {key}"
            except Exception as e:
                logger.warning("Tool pull %s failed: %s", key, e)
                return key, f"{key}: unavailable"

        results = await asyncio.gather(*[pull(k) for k in tool_pulls], return_exceptions=True)
        data = {}
        for result in results:
            if isinstance(result, Exception):
                continue
            key, value = result
            data[key] = value
        return data

    def _select_model(self, user_text: str) -> str:
        """
        Route to Opus for complex reasoning; Sonnet for simple tasks.

        Simple  → Sonnet: action commands, calendar/task/reminder ops, short factual questions
        Complex → Opus:   analysis, strategy, decisions, brainstorming, long brain dumps
        """
        text = user_text.lower().strip()

        # Location reminders → always Sonnet (Haiku misses the save_note tool call)
        LOCATION_REMINDER_PATTERNS = (
            "when i get home", "when i arrive", "when i'm home", "when i am home",
            "when i reach home", "when i get to the office", "when i get to work",
            "when i arrive at", "when i get back",
        )
        if any(p in text for p in LOCATION_REMINDER_PATTERNS):
            logger.debug("Model router → Sonnet (location reminder)")
            return settings.claude_model_complex

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
