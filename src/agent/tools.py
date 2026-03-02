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
    {
        "name": "get_profile",
        "description": (
            "Read the full user profile document — a living record of who the user is: "
            "their background, business, relationships, goals, preferences, and context. "
            "This is always pre-loaded into your context, so only call this if you think "
            "the profile may have been updated mid-conversation and you need the latest version."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "update_profile",
        "description": (
            "Update the user profile document with new information you've learned. "
            "ALWAYS call this after brain dumps, major revelations, or any time the user "
            "shares substantive personal/business details you should remember forever. "
            "This is a full replacement — write the complete updated profile each time. "
            "Organise by section: Personal, Business, Goals, Relationships, Preferences, etc. "
            "Keep it dense with facts. No filler."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The complete updated profile document (replaces existing)",
                },
            },
            "required": ["content"],
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
    # ------------------------------------------------------------------ Weather / News / Stocks
    {
        "name": "get_weather",
        "description": (
            "Get current weather conditions and a multi-day forecast for any location. "
            "Use this for any weather-related question."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "City name, e.g. 'New York', 'Miami', 'London'",
                },
                "days": {
                    "type": "integer",
                    "description": "Number of forecast days (1-7, default 3)",
                    "default": 3,
                },
            },
            "required": ["location"],
        },
    },
    {
        "name": "get_news",
        "description": (
            "Fetch the latest news headlines. "
            "topic options: 'top' (top US headlines), 'ai' (AI/ML news), "
            "'business' (business headlines), 'investing' (markets/stocks news), "
            "'technology' (tech headlines), or any custom search term."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "Topic or search query. Options: top, ai, business, investing, technology, or custom.",
                    "default": "top",
                },
                "count": {
                    "type": "integer",
                    "description": "Number of articles to return (default 8)",
                    "default": 8,
                },
            },
        },
    },
    {
        "name": "get_stock_quotes",
        "description": "Get current stock price and day change for one or more ticker symbols.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbols": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of ticker symbols, e.g. ['AAPL', 'TSLA', 'SPY']",
                },
            },
            "required": ["symbols"],
        },
    },
    # ------------------------------------------------------------------ Web
    {
        "name": "web_search",
        "description": (
            "Search the web for current information, news, prices, facts, or anything "
            "that might be outdated in your training data. Use this whenever the user "
            "asks about recent events, live data, or anything you're not confident about."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query"},
                "max_results": {
                    "type": "integer",
                    "description": "Number of results to return (default 5)",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "fetch_page",
        "description": (
            "Fetch and read the full text content of a specific webpage. "
            "Use this when you need to read an article, documentation, or any URL in detail."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The URL to fetch"},
            },
            "required": ["url"],
        },
    },
    # ------------------------------------------------------------------ ClickUp
    {
        "name": "clickup_list_tasks",
        "description": (
            "List tasks from ClickUp. Can filter by list, space, status, or show only overdue tasks. "
            "Use this when the user asks about ClickUp tasks, work items, or project status."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "list_id": {
                    "type": "string",
                    "description": "ClickUp list ID to fetch tasks from. Use clickup_get_lists to find IDs.",
                },
                "space_name": {
                    "type": "string",
                    "description": "Filter by space name (e.g. 'Work', 'Personal'). Used when list_id not provided.",
                },
                "statuses": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Filter by status names, e.g. ['Open', 'In Progress']",
                },
                "overdue_only": {
                    "type": "boolean",
                    "description": "Return only overdue tasks",
                    "default": False,
                },
            },
        },
    },
    {
        "name": "clickup_get_lists",
        "description": (
            "Show all ClickUp spaces and lists with their IDs. "
            "Call this first if you need a list_id for other ClickUp tools."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "clickup_create_task",
        "description": "Create a new task in a specific ClickUp list.",
        "input_schema": {
            "type": "object",
            "properties": {
                "list_id": {
                    "type": "string",
                    "description": "The ClickUp list ID to create the task in",
                },
                "name": {"type": "string", "description": "Task name"},
                "description": {"type": "string", "description": "Task description / notes"},
                "status": {"type": "string", "description": "Initial status (e.g. 'Open', 'In Progress')"},
                "priority": {
                    "type": "integer",
                    "enum": [1, 2, 3, 4],
                    "description": "1=urgent, 2=high, 3=normal, 4=low",
                },
                "due_date_str": {
                    "type": "string",
                    "description": "Due date as 'today', 'tomorrow', 'YYYY-MM-DD', or 'YYYY-MM-DD HH:MM'",
                },
            },
            "required": ["list_id", "name"],
        },
    },
    {
        "name": "clickup_update_task",
        "description": "Update a ClickUp task — change its name, status, priority, due date, or description.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The ClickUp task ID"},
                "name": {"type": "string", "description": "New task name"},
                "description": {"type": "string", "description": "New description"},
                "status": {"type": "string", "description": "New status (e.g. 'Complete', 'In Progress')"},
                "priority": {
                    "type": "integer",
                    "enum": [1, 2, 3, 4],
                    "description": "1=urgent, 2=high, 3=normal, 4=low",
                },
                "due_date_str": {
                    "type": "string",
                    "description": "New due date as 'today', 'tomorrow', 'YYYY-MM-DD', or 'YYYY-MM-DD HH:MM'",
                },
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "clickup_delete_task",
        "description": "Permanently delete a ClickUp task. Always confirm with the user before calling this.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The ClickUp task ID to delete"},
            },
            "required": ["task_id"],
        },
    },
    # ------------------------------------------------------------------ Email
    {
        "name": "list_emails",
        "description": (
            "List recent emails from one or all configured email accounts. "
            "Can filter to unread only, a specific folder, or a specific account."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "account_id": {
                    "type": "string",
                    "description": "Account ID to list from. Omit to check all accounts.",
                },
                "folder": {
                    "type": "string",
                    "description": "Folder to list (default: INBOX)",
                    "default": "INBOX",
                },
                "unread_only": {
                    "type": "boolean",
                    "description": "Only return unread emails",
                    "default": False,
                },
                "count": {
                    "type": "integer",
                    "description": "Number of emails to return (default 10)",
                    "default": 10,
                },
            },
        },
    },
    {
        "name": "search_emails",
        "description": "Search emails by keyword, sender, subject, or any query across all accounts.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query (e.g. 'invoice', 'from:boss@company.com', 'subject:meeting')",
                },
                "account_id": {
                    "type": "string",
                    "description": "Limit search to a specific account. Omit to search all.",
                },
                "count": {
                    "type": "integer",
                    "description": "Max results (default 10)",
                    "default": 10,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "read_email",
        "description": "Read the full content of a specific email by its ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "The email message ID"},
                "account_id": {"type": "string", "description": "The account the email belongs to"},
            },
            "required": ["message_id", "account_id"],
        },
    },
    {
        "name": "send_email",
        "description": "Send an email from one of the configured accounts.",
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email address"},
                "subject": {"type": "string", "description": "Email subject"},
                "body": {"type": "string", "description": "Email body (plain text)"},
                "account_id": {
                    "type": "string",
                    "description": "Which account to send from. Omit to use the default.",
                },
            },
            "required": ["to", "subject", "body"],
        },
    },
    {
        "name": "archive_email",
        "description": "Archive an email (removes from inbox, keeps in archive). Gmail only.",
        "input_schema": {
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "The email message ID"},
                "account_id": {"type": "string", "description": "The Gmail account ID"},
            },
            "required": ["message_id", "account_id"],
        },
    },
    {
        "name": "delete_email",
        "description": "Move an email to trash. Always confirm with the user first.",
        "input_schema": {
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "The email message ID"},
                "account_id": {"type": "string", "description": "The account the email belongs to"},
            },
            "required": ["message_id", "account_id"],
        },
    },
    # ------------------------------------------------------------------ Scheduling
    {
        "name": "schedule_reminder",
        "description": (
            "Schedule a one-shot message to send to the user at a future time. "
            "ALWAYS use this tool when the user says anything like: 'remind me', 'message me in X minutes', "
            "'send me a message at 3pm', 'check in with me later', 'ping me in an hour', or any request "
            "to be contacted at a specific future time. Never just acknowledge these requests in text — "
            "always call this tool to actually schedule the message."
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
