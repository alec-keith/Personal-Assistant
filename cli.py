#!/usr/bin/env python3
"""
Atlas CLI — interact with your assistant from the terminal without Discord.

Usage:
    python cli.py                    # interactive chat session
    python cli.py "what's my day?"   # single message
    python cli.py --tasks today      # list today's tasks
    python cli.py --events 7         # list next 7 days of events
"""
import sys, asyncio, argparse
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

BOLD  = "\033[1m"
CYAN  = "\033[96m"
GRAY  = "\033[90m"
RESET = "\033[0m"


async def build_agent():
    from config import settings
    from src.memory.store import MemoryStore
    from src.integrations.todoist import TodoistClient
    from src.integrations.calendar import CalendarClient
    from src.agent.core import AgentCore

    memory   = MemoryStore()
    todoist  = TodoistClient()
    calendar = CalendarClient()
    agent    = AgentCore(memory=memory, todoist=todoist, calendar=calendar)
    return agent, todoist, calendar


async def chat(initial_message: str | None = None) -> None:
    agent, _, _ = await build_agent()
    from config import settings

    print(f"\n{BOLD}Atlas CLI{RESET} {GRAY}(type 'exit' to quit){RESET}\n")

    if initial_message:
        # Single-shot mode
        print(f"{CYAN}You:{RESET} {initial_message}")
        response = await agent.handle_message(initial_message)
        print(f"\n{BOLD}Atlas:{RESET} {response}\n")
        return

    # Interactive loop
    while True:
        try:
            user_input = input(f"{CYAN}You:{RESET} ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nBye!")
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit", "q"):
            print("Bye!")
            break

        response = await agent.handle_message(user_input)
        print(f"\n{BOLD}Atlas:{RESET} {response}\n")


async def show_tasks(filter_str: str) -> None:
    _, todoist, _ = await build_agent()
    tasks = await todoist.list_tasks(filter_str=filter_str)
    summary = await todoist.format_tasks_summary(tasks)
    print(f"\n{BOLD}Tasks ({filter_str}):{RESET}\n{summary}\n")


async def show_events(days: int) -> None:
    _, _, calendar = await build_agent()
    events = await calendar.list_events(days_ahead=days)
    summary = calendar.format_events_summary(events)
    print(f"\n{BOLD}Events (next {days} days):{RESET}\n{summary}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Atlas personal assistant CLI")
    parser.add_argument("message", nargs="?", help="Single message to send")
    parser.add_argument("--tasks", metavar="FILTER", help="List tasks with filter (e.g. today, overdue, p1)")
    parser.add_argument("--events", metavar="DAYS", type=int, help="List calendar events for next N days")
    args = parser.parse_args()

    if args.tasks:
        asyncio.run(show_tasks(args.tasks))
    elif args.events:
        asyncio.run(show_events(args.events))
    else:
        asyncio.run(chat(args.message))


if __name__ == "__main__":
    main()
