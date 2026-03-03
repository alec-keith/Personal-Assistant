"""
Roman-Elite v1.1: Tool Execution Governance Layer.

Enforces the tool_executor_policy_v1.json contract:
  - Only Orchestrator can trigger execution (specialists return JSON only)
  - All writes gated by confirmation rules
  - Idempotency: reject duplicate keys within 10 minutes
  - Rate limiting: calendar 10/min, email 5/min, tasks 20/min
  - Audit logging for every execution
  - Fail safely, revert when possible
"""

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from config import settings

logger = logging.getLogger(__name__)

# Tools classified as writes (require idempotency + rate limits + gates)
WRITE_TOOLS = {
    # Calendar writes
    "add_event": "calendar",
    # Task writes
    "add_task": "tasks",
    "complete_task": "tasks",
    "update_task": "tasks",
    "clickup_create_task": "tasks",
    "clickup_update_task": "tasks",
    "clickup_delete_task": "tasks",
    # Email writes
    "send_email": "email",
    "archive_email": "email",
    "delete_email": "email",
    # Memory writes (not rate-limited but logged)
    "save_note": "memory",
    "update_profile": "memory",
    "write_long_term_memory": "memory",
    "write_working_memory": "memory",
    "log_episode": "memory",
    # Scheduler writes
    "schedule_reminder": "scheduler",
    "schedule_recurring": "scheduler",
    "cancel_job": "scheduler",
}

# Read-only tools (no gates, no rate limits)
READ_TOOLS = {
    "search_memory", "list_recent_notes", "get_profile", "list_tasks",
    "list_projects", "list_calendars", "list_events", "get_today_events",
    "list_emails", "search_emails", "read_email", "list_jobs",
    "get_location_reminders", "get_weather", "get_news", "get_stock_quotes",
    "web_search", "fetch_page", "clickup_list_tasks", "clickup_get_lists",
    "route_to_specialists", "start_onboarding", "onboarding_save_answer",
    "onboarding_advance",
}

RATE_LIMITS = {
    "calendar": 10,   # writes per minute
    "email": 5,
    "tasks": 20,
}

LEGAL_FINANCIAL_KEYWORDS = [
    "contract", "agreement", "invoice", "payment", "legal", "settlement",
    "liability", "terms and conditions", "nda", "non-disclosure",
    "salary", "compensation", "binding",
]


@dataclass
class ToolResult:
    success: bool
    output: str
    confirmation_needed: str | None = None
    tool_name: str = ""
    logged: bool = False


@dataclass
class _RateWindow:
    """Sliding window rate limiter."""
    timestamps: list[float] = field(default_factory=list)

    def check(self, limit: int, window_seconds: int = 60) -> bool:
        now = time.time()
        self.timestamps = [t for t in self.timestamps if now - t < window_seconds]
        if len(self.timestamps) >= limit:
            return False
        self.timestamps.append(now)
        return True


class ToolExecutor:
    def __init__(self) -> None:
        self._idempotency_keys: dict[str, float] = {}  # key → timestamp
        self._rate_windows: dict[str, _RateWindow] = defaultdict(_RateWindow)
        self._tz = ZoneInfo(settings.agent_timezone)
        self._pending_confirmations: dict[str, dict] = {}  # tool_id → {tool_name, inputs}

    def check(self, tool_name: str, inputs: dict, tool_id: str = "") -> ToolResult | None:
        """
        Pre-execution check. Returns a ToolResult with confirmation_needed if the action
        requires user approval, or None if the action can proceed.

        Call this BEFORE executing any tool. If it returns a ToolResult, return that
        to the model instead of executing the tool.
        """
        # Read-only tools always pass
        if tool_name in READ_TOOLS:
            return None

        category = WRITE_TOOLS.get(tool_name)
        if not category:
            return None  # Unknown tool, let it through

        # --- Idempotency check ---
        idem_key = self._build_idempotency_key(tool_name, inputs)
        now = time.time()
        # Clean expired keys
        self._idempotency_keys = {
            k: t for k, t in self._idempotency_keys.items() if now - t < 600
        }
        if idem_key in self._idempotency_keys:
            return ToolResult(
                success=False,
                output=f"Duplicate action rejected (idempotency). Same {tool_name} call was made within the last 10 minutes.",
                tool_name=tool_name,
            )

        # --- Rate limit check ---
        limit = RATE_LIMITS.get(category)
        if limit and not self._rate_windows[category].check(limit):
            return ToolResult(
                success=False,
                output=f"Rate limit exceeded for {category} writes ({limit}/minute). Wait before retrying.",
                tool_name=tool_name,
            )

        # --- Confirmation gates ---
        confirmation = self._check_confirmation_gate(tool_name, inputs, tool_id)
        if confirmation:
            return ToolResult(
                success=False,
                output=confirmation,
                confirmation_needed=confirmation,
                tool_name=tool_name,
            )

        return None  # All checks passed

    def record_execution(self, tool_name: str, inputs: dict, success: bool) -> None:
        """Record a successful execution for idempotency tracking and audit logging."""
        idem_key = self._build_idempotency_key(tool_name, inputs)
        self._idempotency_keys[idem_key] = time.time()

        # Audit log
        logger.info(
            "AUDIT | tool=%s | success=%s | payload=%s",
            tool_name,
            success,
            self._summarize_payload(tool_name, inputs),
        )

    def _build_idempotency_key(self, tool_name: str, inputs: dict) -> str:
        """Build a stable key from tool name + sorted inputs."""
        # Only include key fields, not descriptions or verbose text
        key_fields = {}
        for k, v in sorted(inputs.items()):
            if isinstance(v, str) and len(v) > 200:
                key_fields[k] = v[:100]  # Truncate long text
            else:
                key_fields[k] = v
        return f"{tool_name}:{str(key_fields)}"

    def _summarize_payload(self, tool_name: str, inputs: dict) -> str:
        """Create a safe summary for audit logs (no secrets)."""
        summary = {}
        for k, v in inputs.items():
            if any(secret in k.lower() for secret in ["password", "token", "secret", "key"]):
                summary[k] = "***"
            elif isinstance(v, str) and len(v) > 100:
                summary[k] = v[:80] + "…"
            else:
                summary[k] = v
        return str(summary)[:300]

    def _check_confirmation_gate(self, tool_name: str, inputs: dict, tool_id: str) -> str | None:
        """
        Check if a specific tool call triggers a confirmation gate.
        Returns a confirmation prompt string, or None if auto-approved.
        """
        # Check if this is a re-confirmation (user already approved)
        if tool_id and tool_id in self._pending_confirmations:
            del self._pending_confirmations[tool_id]
            return None

        # --- Calendar gates ---
        if tool_name == "add_event":
            start_iso = inputs.get("start_iso", "")
            end_iso = inputs.get("end_iso", "")
            if start_iso and end_iso:
                try:
                    start = datetime.fromisoformat(start_iso)
                    end = datetime.fromisoformat(end_iso)
                    duration_min = (end - start).total_seconds() / 60
                    if duration_min > 120:
                        self._pending_confirmations[tool_id] = {"tool_name": tool_name, "inputs": inputs}
                        return (
                            f"CONFIRMATION NEEDED: This event is {int(duration_min)} minutes "
                            f"({duration_min/60:.1f} hours). Events over 2 hours require confirmation. Proceed?"
                        )
                except (ValueError, TypeError):
                    pass

            # Outside work hours check (before 8am or after 8pm)
            if start_iso:
                try:
                    start = datetime.fromisoformat(start_iso)
                    if hasattr(start, 'hour') and (start.hour < 8 or start.hour >= 20):
                        self._pending_confirmations[tool_id] = {"tool_name": tool_name, "inputs": inputs}
                        return (
                            f"CONFIRMATION NEEDED: This event is scheduled at {start.strftime('%-I:%M %p')}, "
                            f"outside normal hours (8 AM – 8 PM). Proceed?"
                        )
                except (ValueError, TypeError):
                    pass

        # --- Email gates ---
        if tool_name == "send_email":
            body = inputs.get("body", "").lower()
            subject = inputs.get("subject", "").lower()
            combined = body + " " + subject

            # Legal/financial content check
            if any(kw in combined for kw in LEGAL_FINANCIAL_KEYWORDS):
                self._pending_confirmations[tool_id] = {"tool_name": tool_name, "inputs": inputs}
                return (
                    "CONFIRMATION NEEDED: This email appears to contain legal or financial content. "
                    "Please review the draft before sending. Proceed?"
                )

            # Default: draft-first for all emails
            self._pending_confirmations[tool_id] = {"tool_name": tool_name, "inputs": inputs}
            to = inputs.get("to", "")
            subj = inputs.get("subject", "")
            body_preview = inputs.get("body", "")[:200]
            return (
                f"CONFIRMATION NEEDED (draft-first policy): Ready to send email.\n"
                f"To: {to}\nSubject: {subj}\nBody preview: {body_preview}\n\n"
                f"Send this email?"
            )

        # --- Task gates ---
        if tool_name == "clickup_delete_task":
            self._pending_confirmations[tool_id] = {"tool_name": tool_name, "inputs": inputs}
            return (
                f"CONFIRMATION NEEDED: Permanently delete ClickUp task {inputs.get('task_id', '?')}? "
                "This cannot be undone."
            )

        if tool_name == "delete_email":
            self._pending_confirmations[tool_id] = {"tool_name": tool_name, "inputs": inputs}
            return (
                f"CONFIRMATION NEEDED: Move email {inputs.get('message_id', '?')} to trash?"
            )

        return None  # Auto-approved

    def approve_pending(self, tool_id: str) -> None:
        """Mark a pending confirmation as approved by the user."""
        if tool_id in self._pending_confirmations:
            del self._pending_confirmations[tool_id]
