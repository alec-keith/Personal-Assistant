"""
ClickUp integration via REST API v2.

Supports: list spaces/lists, list tasks, create task, update task, delete task.

ClickUp hierarchy: Workspace (Team) → Space → Folder → List → Task
Auth: personal API token in Authorization header (no "Bearer" prefix).
"""

import logging
from typing import Any

import httpx

from config import settings

logger = logging.getLogger(__name__)

BASE_URL = "https://api.clickup.com/api/v2"

PRIORITY_MAP = {1: "urgent", 2: "high", 3: "normal", 4: "low"}
PRIORITY_REVERSE = {"urgent": 1, "high": 2, "normal": 3, "low": 4}


class ClickUpClient:
    def __init__(self) -> None:
        self._headers = {
            "Authorization": settings.clickup_api_token,
            "Content-Type": "application/json",
        }
        self._team_id: str | None = None  # cached on first use

    async def _get(self, path: str, params: dict | None = None) -> Any:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(f"{BASE_URL}{path}", headers=self._headers, params=params)
            r.raise_for_status()
            return r.json()

    async def _post(self, path: str, body: dict) -> Any:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(f"{BASE_URL}{path}", headers=self._headers, json=body)
            r.raise_for_status()
            return r.json()

    async def _put(self, path: str, body: dict) -> Any:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.put(f"{BASE_URL}{path}", headers=self._headers, json=body)
            r.raise_for_status()
            return r.json()

    async def _delete(self, path: str) -> None:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.delete(f"{BASE_URL}{path}", headers=self._headers)
            r.raise_for_status()

    # ------------------------------------------------------------------
    # Workspace
    # ------------------------------------------------------------------

    async def get_team_id(self) -> str:
        """Return the first workspace/team ID (cached)."""
        if self._team_id:
            return self._team_id
        data = await self._get("/team")
        teams = data.get("teams", [])
        if not teams:
            raise RuntimeError("No ClickUp workspaces found.")
        self._team_id = teams[0]["id"]
        return self._team_id

    # ------------------------------------------------------------------
    # Spaces & Lists
    # ------------------------------------------------------------------

    async def get_spaces(self) -> list[dict]:
        team_id = await self.get_team_id()
        data = await self._get(f"/team/{team_id}/space", {"archived": "false"})
        return data.get("spaces", [])

    async def get_lists_in_space(self, space_id: str) -> list[dict]:
        """Get all lists in a space (both foldered and folderless)."""
        lists: list[dict] = []

        # Folderless lists
        data = await self._get(f"/space/{space_id}/list", {"archived": "false"})
        lists.extend(data.get("lists", []))

        # Folders → lists
        folders_data = await self._get(f"/space/{space_id}/folder", {"archived": "false"})
        for folder in folders_data.get("folders", []):
            folder_lists = await self._get(f"/folder/{folder['id']}/list", {"archived": "false"})
            for lst in folder_lists.get("lists", []):
                lst["folder_name"] = folder["name"]
                lists.append(lst)

        return lists

    async def get_all_lists(self) -> list[dict]:
        """Return all lists across all spaces with space/folder context."""
        spaces = await self.get_spaces()
        all_lists: list[dict] = []
        for space in spaces:
            lists = await self.get_lists_in_space(space["id"])
            for lst in lists:
                lst["space_name"] = space["name"]
            all_lists.extend(lists)
        return all_lists

    async def format_lists_summary(self) -> str:
        lists = await self.get_all_lists()
        if not lists:
            return "No ClickUp lists found."
        lines = []
        current_space = None
        for lst in lists:
            space = lst.get("space_name", "?")
            if space != current_space:
                lines.append(f"\n{space}:")
                current_space = space
            folder = lst.get("folder_name")
            prefix = f"  {folder}/" if folder else "  "
            task_count = lst.get("task_count", "?")
            lines.append(f"{prefix}{lst['name']} [id:{lst['id']}] ({task_count} tasks)")
        return "\n".join(lines).strip()

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------

    async def list_tasks(
        self,
        list_id: str | None = None,
        space_name: str | None = None,
        statuses: list[str] | None = None,
        assignee_ids: list[int] | None = None,
        overdue_only: bool = False,
        due_date_lt: int | None = None,  # unix ms
        subtasks: bool = False,
        page: int = 0,
    ) -> list[dict]:
        """
        List tasks. If list_id provided, fetch from that list.
        Otherwise fetch from the whole workspace (team-level endpoint).
        """
        params: dict[str, Any] = {
            "subtasks": str(subtasks).lower(),
            "page": page,
            "order_by": "due_date",
        }
        if statuses:
            params["statuses[]"] = statuses
        if overdue_only:
            import time
            params["due_date_lt"] = int(time.time() * 1000)
        elif due_date_lt:
            params["due_date_lt"] = due_date_lt

        if list_id:
            data = await self._get(f"/list/{list_id}/task", params)
            tasks = data.get("tasks", [])
        else:
            team_id = await self.get_team_id()
            # Filter by space if given
            if space_name:
                spaces = await self.get_spaces()
                match = next((s for s in spaces if s["name"].lower() == space_name.lower()), None)
                if match:
                    params["space_ids[]"] = [match["id"]]
            data = await self._get(f"/team/{team_id}/task", params)
            tasks = data.get("tasks", [])

        return tasks

    async def create_task(
        self,
        list_id: str,
        name: str,
        description: str | None = None,
        status: str | None = None,
        priority: int | None = None,  # 1=urgent 2=high 3=normal 4=low
        due_date: int | None = None,  # unix ms
        due_date_str: str | None = None,  # natural — parsed here
        assignees: list[int] | None = None,
    ) -> dict:
        body: dict[str, Any] = {"name": name}
        if description:
            body["description"] = description
        if status:
            body["status"] = status
        if priority:
            body["priority"] = priority
        if assignees:
            body["assignees"] = assignees

        # Handle due date
        if due_date:
            body["due_date"] = due_date
        elif due_date_str:
            ts = _parse_due_date(due_date_str)
            if ts:
                body["due_date"] = ts
                body["due_date_time"] = ":" in due_date_str

        task = await self._post(f"/list/{list_id}/task", body)
        logger.info("Created ClickUp task: %s (id=%s)", name, task.get("id"))
        return task

    async def update_task(
        self,
        task_id: str,
        name: str | None = None,
        description: str | None = None,
        status: str | None = None,
        priority: int | None = None,
        due_date: int | None = None,
        due_date_str: str | None = None,
    ) -> dict:
        body: dict[str, Any] = {}
        if name:
            body["name"] = name
        if description is not None:
            body["description"] = description
        if status:
            body["status"] = status
        if priority:
            body["priority"] = priority
        if due_date:
            body["due_date"] = due_date
        elif due_date_str:
            ts = _parse_due_date(due_date_str)
            if ts:
                body["due_date"] = ts
                body["due_date_time"] = ":" in due_date_str

        task = await self._put(f"/task/{task_id}", body)
        logger.info("Updated ClickUp task id=%s", task_id)
        return task

    async def delete_task(self, task_id: str) -> None:
        await self._delete(f"/task/{task_id}")
        logger.info("Deleted ClickUp task id=%s", task_id)

    # ------------------------------------------------------------------
    # Formatting
    # ------------------------------------------------------------------

    def format_tasks_summary(self, tasks: list[dict]) -> str:
        if not tasks:
            return "No tasks found."
        lines = []
        for t in tasks:
            name = t.get("name", "(no name)")
            task_id = t.get("id", "?")
            status = (t.get("status") or {}).get("status", "?")
            priority_num = (t.get("priority") or {}).get("id")
            priority_label = PRIORITY_MAP.get(int(priority_num), "") if priority_num else ""
            due_ms = t.get("due_date")
            if due_ms:
                from datetime import datetime, timezone
                due_str = datetime.fromtimestamp(int(due_ms) / 1000, tz=timezone.utc).strftime("%b %-d")
            else:
                due_str = "no due date"
            list_name = (t.get("list") or {}).get("name", "")
            parts = [name]
            parts.append(f"status:{status}")
            if priority_label:
                parts.append(f"priority:{priority_label}")
            parts.append(f"due:{due_str}")
            if list_name:
                parts.append(f"list:{list_name}")
            parts.append(f"[id:{task_id}]")
            lines.append(" | ".join(parts))
        return "\n".join(lines)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _parse_due_date(due_str: str) -> int | None:
    """
    Parse a natural language due date to unix milliseconds.
    Handles: 'today', 'tomorrow', 'YYYY-MM-DD', 'YYYY-MM-DD HH:MM'.
    Returns None if unparseable.
    """
    from datetime import datetime, date, timezone, timedelta
    s = due_str.strip().lower()
    today = date.today()

    if s == "today":
        dt = datetime.combine(today, datetime.min.time()).replace(tzinfo=timezone.utc)
    elif s == "tomorrow":
        dt = datetime.combine(today + timedelta(days=1), datetime.min.time()).replace(tzinfo=timezone.utc)
    else:
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(due_str.strip(), fmt).replace(tzinfo=timezone.utc)
                break
            except ValueError:
                continue
        else:
            return None

    return int(dt.timestamp() * 1000)
