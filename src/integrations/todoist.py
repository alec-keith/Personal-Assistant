"""
Todoist integration using the official REST API v2.

Supports: list projects, list tasks, add task, complete task,
          update task, add comment, get task by id.
"""

import logging
from datetime import date
from typing import Any

import httpx

from config import settings

logger = logging.getLogger(__name__)

BASE_URL = "https://api.todoist.com/rest/v2"


class TodoistClient:
    def __init__(self) -> None:
        self._headers = {
            "Authorization": f"Bearer {settings.todoist_api_token}",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # Projects
    # ------------------------------------------------------------------

    async def list_projects(self) -> list[dict]:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{BASE_URL}/projects", headers=self._headers)
            r.raise_for_status()
            return r.json()

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
        List tasks. filter_str uses Todoist filter syntax
        e.g. "today", "overdue", "p1", "@label".
        """
        params: dict[str, Any] = {}
        if project_id:
            params["project_id"] = project_id
        if filter_str:
            params["filter"] = filter_str
        if label:
            params["label"] = label

        async with httpx.AsyncClient() as client:
            r = await client.get(f"{BASE_URL}/tasks", headers=self._headers, params=params)
            r.raise_for_status()
            return r.json()

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
        due_date: ISO date string e.g. "2026-03-15".
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
        """Update any task fields. Pass keyword args matching the API."""
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
            r = await client.delete(f"{BASE_URL}/tasks/{task_id}", headers=self._headers)
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
            lines.append(f"- [{t['id']}] {priority_icon} {t['content']} ({due_str})")
        return "\n".join(lines)
