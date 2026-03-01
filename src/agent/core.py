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
from .prompts import build_system_prompt
from .tools import TOOLS

logger = logging.getLogger(__name__)

# Short-term in-memory conversation history (per session, not persisted)
# Each item: {"role": "user"|"assistant", "content": str|list}
MAX_HISTORY = 20  # messages kept in context window


class AgentCore:
    def __init__(
        self,
        memory: MemoryStore,
        todoist: TodoistClient,
        calendar: CalendarClient,
        # Injected so the agent can schedule proactive messages
        schedule_reminder_fn: Callable[[str, datetime], Awaitable[None]] | None = None,
    ) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        self._memory = memory
        self._todoist = todoist
        self._calendar = calendar
        self._schedule_reminder = schedule_reminder_fn
        self._scheduler = None  # injected from main.py after creation
        self._history: list[dict] = []
        self._tz = ZoneInfo(settings.agent_timezone)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def handle_message(self, user_text: str) -> str:
        """Process one user message and return the assistant's reply."""
        self._history.append({"role": "user", "content": user_text})
        self._trim_history()

        response_text = await self._run_agent_loop()

        self._history.append({"role": "assistant", "content": response_text})

        # Asynchronously save a summary to memory (don't block the response)
        asyncio.create_task(self._save_conversation_summary(user_text, response_text))

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
                model=settings.claude_model,
                max_tokens=4096,
                system=build_system_prompt(self._calendar.calendar_names()),
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

        # --- Memory ---
        if name == "search_memory":
            query = inputs["query"]
            collection = inputs.get("collection", "both")
            results = []
            if collection in ("conversations", "both"):
                convs = self._memory.search_conversations(query)
                if convs:
                    results.append("Past conversations:\n" + "\n---\n".join(convs))
            if collection in ("notes", "both"):
                notes = self._memory.search_notes(query)
                if notes:
                    results.append("Saved notes:\n" + "\n---\n".join(notes))
            return "\n\n".join(results) if results else "No relevant memories found."

        if name == "save_note":
            doc_id = self._memory.save_note(inputs["note"], inputs.get("tags"))
            return f"Note saved (id={doc_id})"

        if name == "list_recent_notes":
            notes = self._memory.list_recent_notes(inputs.get("limit", 10))
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
            return self._scheduler.cancel_job(inputs["job_id"])

        return f"Unknown tool: {name}"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _trim_history(self) -> None:
        """Keep the in-context history from growing unbounded."""
        if len(self._history) > MAX_HISTORY:
            # Always keep the last MAX_HISTORY messages
            self._history = self._history[-MAX_HISTORY:]

    async def _save_conversation_summary(self, user_text: str, assistant_text: str) -> None:
        """Summarise and persist a conversation turn to memory."""
        try:
            # Ask Claude to write a one-sentence summary
            summary_response = await self._client.messages.create(
                model="claude-haiku-4-5-20251001",  # Fast + cheap for this
                max_tokens=200,
                messages=[{
                    "role": "user",
                    "content": (
                        f"Summarise this exchange in one sentence for future recall:\n\n"
                        f"User: {user_text[:500]}\n\nAssistant: {assistant_text[:500]}"
                    ),
                }],
            )
            summary = summary_response.content[0].text
            self._memory.save_conversation_summary(summary)
        except Exception:
            logger.debug("Failed to save conversation summary", exc_info=True)
