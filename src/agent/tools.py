"""
Tool definitions for Claude's tool-use API.
Each tool maps 1-to-1 with a handler in core.py.
"""

TOOLS: list[dict] = [
    # ------------------------------------------------------------------ Memory
    {
        "name": "search_memory",
        "description": (
            "Search the user's past conversations and saved notes for relevant context. "
            "Call this at the start of most interactions to personalise your response."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for"},
                "collection": {
                    "type": "string",
                    "enum": ["conversations", "notes", "both"],
                    "description": "Which memory collection to search",
                    "default": "both",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "save_note",
        "description": "Explicitly save something the user wants remembered long-term.",
        "input_schema": {
            "type": "object",
            "properties": {
                "note": {"type": "string", "description": "The information to remember"},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional tags for organisation",
                },
            },
            "required": ["note"],
        },
    },
    {
        "name": "list_recent_notes",
        "description": "List the most recently saved notes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Number of notes to return",
                    "default": 10,
                },
            },
        },
    },
    # ------------------------------------------------------------------ Todoist
    {
        "name": "list_tasks",
        "description": "List tasks from Todoist. Use filter_str for smart queries.",
        "input_schema": {
            "type": "object",
            "properties": {
                "filter_str": {
                    "type": "string",
                    "description": (
                        "Todoist filter syntax. Examples: 'today', 'overdue', "
                        "'p1', 'today & p1', '@focus', 'no due date'. "
                        "Leave empty to list all active tasks."
                    ),
                },
                "project_id": {"type": "string", "description": "Filter by project ID"},
            },
        },
    },
    {
        "name": "add_task",
        "description": "Add a new task to Todoist.",
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The task title"},
                "due_string": {
                    "type": "string",
                    "description": "Natural language due date e.g. 'tomorrow at 3pm', 'every Monday'",
                },
                "priority": {
                    "type": "integer",
                    "enum": [1, 2, 3, 4],
                    "description": "1=normal, 2=medium, 3=high, 4=urgent (p1)",
                    "default": 1,
                },
                "labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Labels to apply",
                },
                "description": {"type": "string", "description": "Optional task notes"},
                "project_id": {"type": "string"},
            },
            "required": ["content"],
        },
    },
    {
        "name": "complete_task",
        "description": "Mark a Todoist task as complete.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The task ID"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "update_task",
        "description": "Update a Todoist task (change due date, priority, content, etc.).",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The task ID"},
                "content": {"type": "string", "description": "New task title"},
                "due_string": {"type": "string", "description": "New due date"},
                "priority": {"type": "integer", "enum": [1, 2, 3, 4]},
                "description": {"type": "string"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "list_projects",
        "description": "List all Todoist projects.",
        "input_schema": {"type": "object", "properties": {}},
    },
    # ------------------------------------------------------------------ Calendar
    {
        "name": "list_events",
        "description": "List upcoming calendar events (synced with Fantastical).",
        "input_schema": {
            "type": "object",
            "properties": {
                "days_ahead": {
                    "type": "integer",
                    "description": "How many days to look ahead",
                    "default": 7,
                },
            },
        },
    },
    {
        "name": "get_today_events",
        "description": "Get all calendar events for today.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "add_event",
        "description": "Add a new event to the calendar (appears in Fantastical immediately).",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "start_iso": {
                    "type": "string",
                    "description": "ISO 8601 datetime e.g. '2026-03-10T14:00:00'",
                },
                "end_iso": {
                    "type": "string",
                    "description": "ISO 8601 datetime. Defaults to 1 hour after start.",
                },
                "description": {"type": "string"},
                "location": {"type": "string"},
            },
            "required": ["title", "start_iso"],
        },
    },
    # ------------------------------------------------------------------ Scheduling
    {
        "name": "schedule_reminder",
        "description": (
            "Schedule a proactive reminder to send to the user at a specific time. "
            "Use this when the user says 'remind me at 3pm' or when you want to "
            "follow up on something later."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "The message to send"},
                "when_iso": {
                    "type": "string",
                    "description": "ISO 8601 datetime when to send the reminder",
                },
            },
            "required": ["message", "when_iso"],
        },
    },
]
