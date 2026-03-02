"""
Todoist integration using the official REST API v1.

Supports: list projects, list tasks, add task, complete task,
          update task, get task by id.

Note: Todoist migrated from /rest/v2 to /api/v1. The new API returns
paginated results as {"results": [...], "next_cursor": ...}.
"""

import logging
from datetime import date
from typing import Any

import httpx

from config import settings

logger = logging.getLogger(__name__)

BASE_URL = "https://api.todoist.com/api/v1"


def _apply_filter(tasks: list[dict], filter_str: str) -> list[dict]:
    """
    Client-side approximation of Todoist filter syntax.
    Handles: today, overdue, p1-p4, 'no due date', and | (OR) combinations.
    """
    today = date.today().isoformat()
    parts = [p.strip().lower() for p in filter_str.split("|")]

    matched = []
    for task in tasks:
        due = (task.get("due") or {})
        due_date = (due.get("date") or "")[:10]  # "YYYY-MM-DD"
        priority = task.get("priority", 1)
        priority_map = {"p1": 4, "p2": 3, "p3": 2, "p4": 1}

        include = False
        for part in parts:
            part = part.strip()
            if "today" in part and due_date == today:
                include = True
            elif "overdue" in part and due_date and due_date < today:
                include = True
            elif part in priority_map and priority == priority_map[part]:
                include = True
            elif "no due date" in part and not due_date:
                include = True
        if include:
            matched.append(task)
    return matched


class TodoistClient:
    def __init__(self) -> None:
        self._headers = {
            "Authorization": f"Bearer {settings.todoist_api_token}",
            "Content-Type": "application/json",
        }

    async def _get_all(self, url: str, params: dict | None = None) -> list[dict]:
        """Fetch all pages from a paginated v1 endpoint."""
        results = []
        cursor = None
        async with httpx.AsyncClient() as client:
            while True:
                p = dict(params or {})
                if cursor:
                    p["cursor"] = cursor
                r = await client.get(url, headers=self._headers, params=p)
                r.raise_for_status()
                data = r.json()
                # v1 returns {"results": [...], "next_cursor": ...}
                # Some endpoints return a list directly (projects)
                if isinstance(data, list):
                    return data
                results.extend(data.get("results", []))
                cursor = data.get("next_cursor")
                if not cursor:
                    break
        return results

    # ------------------------------------------------------------------
    # Projects
    # ------------------------------------------------------------------

    async def list_projects(self) -> list[dict]:
        return await self._get_all(f"{BASE_URL}/projects")

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------

    async def list_tasks(
        self,
        project_id: str | None = None,
        filter_str: str | None = None,
        label: str | None = None,
    ) -> list[dict]:
        """
        List tasks. filter_str is applied client-side (today, overdue, p1-p4).
        """
        params: dict[str, Any] = {}
        if project_id:
            params["project_id"] = project_id
        if label:
            params["label"] = label

        tasks = await self._get_all(f"{BASE_URL}/tasks", params=params)

        if filter_str:
            tasks = _apply_filter(tasks, filter_str)
        return tasks

    async def get_task(self, task_id: str) -> dict:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{BASE_URL}/tasks/{task_id}", headers=self._headers)
            r.raise_for_status()
            return r.json()

    async def add_task(
        self,
        content: str,
        project_id: str | None = None,
        due_string: str | None = None,
        due_date: str | None = None,
        priority: int = 1,
        labels: list[str] | None = None,
        description: str | None = None,
    ) -> dict:
        """
        Add a new task.
        priority: 1 (normal) – 4 (urgent, shown as p1 in app).
        due_string: natural language e.g. "tomorrow at 3pm", "every Monday".
        """
        body: dict[str, Any] = {"content": content, "priority": priority}
        if project_id:
            body["project_id"] = project_id
        if due_string:
            body["due_string"] = due_string
        elif due_date:
            body["due_date"] = due_date
        if labels:
            body["labels"] = labels
        if description:
            body["description"] = description

        async with httpx.AsyncClient() as client:
            r = await client.post(f"{BASE_URL}/tasks", headers=self._headers, json=body)
            r.raise_for_status()
            task = r.json()
            logger.info("Created task: %s (id=%s)", content, task["id"])
            return task

    async def update_task(self, task_id: str, **kwargs: Any) -> dict:
        """Update any task fields."""
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{BASE_URL}/tasks/{task_id}",
                headers=self._headers,
                json=kwargs,
            )
            r.raise_for_status()
            return r.json()

    async def complete_task(self, task_id: str) -> None:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{BASE_URL}/tasks/{task_id}/close", headers=self._headers
            )
            r.raise_for_status()
            logger.info("Completed task id=%s", task_id)

    async def delete_task(self, task_id: str) -> None:
        async with httpx.AsyncClient() as client:
            r = await client.delete(
                f"{BASE_URL}/tasks/{task_id}", headers=self._headers
            )
            r.raise_for_status()

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    async def get_today_tasks(self) -> list[dict]:
        return await self.list_tasks(filter_str="today | overdue")

    async def get_overdue_tasks(self) -> list[dict]:
        return await self.list_tasks(filter_str="overdue")

    async def format_tasks_summary(self, tasks: list[dict]) -> str:
        """Return a readable summary string for a list of tasks."""
        if not tasks:
            return "No tasks found."
        lines = []
        for t in tasks:
            due = t.get("due", {}) or {}
            due_str = due.get("string") or due.get("date") or "no due date"
            priority_map = {1: "", 2: "🔵", 3: "🟠", 4: "🔴"}
            priority_icon = priority_map.get(t.get("priority", 1), "")
            lines.append(f"- {priority_icon} {t['content']} ({due_str}) [id:{t['id']}]")
        return "\n".join(lines)
