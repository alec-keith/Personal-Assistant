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
        "name": "list_calendars",
        "description": "List all available calendars (e.g. Home, Work, Apple Calendar). Call this if you're unsure which calendar to use.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_events",
        "description": (
            "List upcoming calendar events across all calendars (synced with Fantastical). "
            "Each event shows which calendar it belongs to."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days_ahead": {
                    "type": "integer",
                    "description": "How many days to look ahead",
                    "default": 7,
                },
                "calendar_name": {
                    "type": "string",
                    "description": "Limit results to a specific calendar (e.g. 'Work', 'Home'). Omit to search all.",
                },
            },
        },
    },
    {
        "name": "get_today_events",
        "description": "Get all calendar events for today across all calendars.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "add_event",
        "description": (
            "Add a new event to a calendar (appears in Fantastical immediately). "
            "Includes full alert stack: 2 weeks, 1 week, 3 days, 1 day, 1 hour, and 15 minutes before. "
            "Route work/professional events to 'Work', personal/social events to 'Home'."
        ),
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
                "calendar_name": {
                    "type": "string",
                    "description": "Calendar to add the event to. Use 'Work' for professional events, 'Home' for personal ones. Defaults to Home.",
                },
            },
            "required": ["title", "start_iso"],
        },
    },
    # ------------------------------------------------------------------ Scheduling
    {
        "name": "schedule_reminder",
        "description": (
            "Schedule a one-shot reminder to send to the user at a specific time. "
            "Use this when the user says 'remind me at 3pm' or when you want to follow up once."
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
    {
        "name": "schedule_recurring",
        "description": (
            "Create a new recurring scheduled message that persists across restarts. "
            "Use this when the user wants Roman to check in regularly, send reminders on a schedule, "
            "or repeat any message on a pattern. "
            "Examples: 'check in with me every 3 hours' → interval_minutes=180; "
            "'remind me every weekday at 9am' → cron='0 9 * * mon-fri'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "Short unique identifier, e.g. 'checkin_3h' or 'daily_focus'",
                },
                "message": {
                    "type": "string",
                    "description": "The message to send each time this job fires",
                },
                "description": {
                    "type": "string",
                    "description": "Human-readable description of what this job does",
                },
                "interval_minutes": {
                    "type": "integer",
                    "description": "Run every N minutes. Use this for 'every X hours/minutes'.",
                },
                "cron": {
                    "type": "string",
                    "description": "Standard 5-field cron expression (minute hour day month weekday). Use for specific times/days.",
                },
                "end_date": {
                    "type": "string",
                    "description": (
                        "ISO date string to stop the job after, e.g. '2026-03-07'. "
                        "Use when the user says 'just this week', 'until Friday', 'for the next month', etc. "
                        "Omit entirely for indefinite recurring jobs."
                    ),
                },
            },
            "required": ["job_id", "message", "description"],
        },
    },
    {
        "name": "list_jobs",
        "description": "List all active scheduled jobs — both built-in and ones you've added.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "cancel_job",
        "description": "Cancel and permanently remove a recurring scheduled job by its ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "The job ID to cancel (from list_jobs)",
                },
            },
            "required": ["job_id"],
        },
    },
]
