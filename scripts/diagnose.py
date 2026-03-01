#!/usr/bin/env python3
"""
Health check — tests every service connection and reports status.

Usage:
    python scripts/diagnose.py
"""
import sys, asyncio
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
RESET = "\033[0m"
BOLD = "\033[1m"

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    icon = f"{GREEN}✓{RESET}" if ok else f"{RED}✗{RESET}"
    print(f"  {icon}  {name}" + (f"  — {detail}" if detail else ""))


async def check_env() -> None:
    print(f"\n{BOLD}Environment{RESET}")
    try:
        from config import settings
        record(".env loaded", True, f"agent_name={settings.agent_name}")
        record("timezone set", bool(settings.agent_timezone), settings.agent_timezone)
    except Exception as e:
        record(".env / settings", False, str(e))


async def check_anthropic() -> None:
    print(f"\n{BOLD}Anthropic (Claude){RESET}")
    try:
        from config import settings
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        r = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=5,
            messages=[{"role": "user", "content": "ping"}],
        )
        record("API key", True, f"model={settings.claude_model}")
    except Exception as e:
        record("API key", False, str(e)[:80])


async def check_discord() -> None:
    print(f"\n{BOLD}Discord{RESET}")
    try:
        from config import settings
        import httpx
        async with httpx.AsyncClient() as c:
            r = await c.get(
                "https://discord.com/api/v10/users/@me",
                headers={"Authorization": f"Bot {settings.discord_bot_token}"},
            )
        if r.status_code == 200:
            d = r.json()
            record("bot token", True, f"{d.get('username')}#{d.get('discriminator')}")
        else:
            record("bot token", False, f"HTTP {r.status_code}")
        record("user ID configured", bool(settings.discord_user_id), str(settings.discord_user_id))
    except Exception as e:
        record("discord", False, str(e)[:80])


async def check_todoist() -> None:
    print(f"\n{BOLD}Todoist{RESET}")
    try:
        from src.integrations.todoist import TodoistClient
        client = TodoistClient()
        projects = await client.list_projects()
        tasks = await client.get_today_tasks()
        record("API token", True, f"{len(projects)} projects, {len(tasks)} tasks due today")
    except Exception as e:
        record("Todoist", False, str(e)[:80])


async def check_calendar() -> None:
    print(f"\n{BOLD}Calendar (iCloud / Fantastical){RESET}")
    try:
        from config import settings
        if not settings.use_icloud:
            record("iCloud CalDAV", False, "ICLOUD_USERNAME / ICLOUD_APP_PASSWORD not set")
            return
        from src.integrations.calendar import CalendarClient
        client = CalendarClient()
        if client._calendar is None:
            record("CalDAV connection", False, "could not connect — check credentials")
            return
        cals = await client.list_available_calendars()
        events = await client.get_today_events()
        record("CalDAV connection", True, f"calendar='{client._calendar.name}', {len(events)} events today")
    except Exception as e:
        record("Calendar", False, str(e)[:80])


async def check_memory() -> None:
    print(f"\n{BOLD}Memory (ChromaDB){RESET}")
    try:
        from src.memory.store import MemoryStore
        store = MemoryStore()
        store.save_note("health check test", tags=["_diag"])
        results_found = store.search_notes("health check")
        record("ChromaDB", True, f"persist_dir exists, search returned {len(results_found)} result(s)")
    except Exception as e:
        record("ChromaDB", False, str(e)[:80])


async def main() -> None:
    print(f"\n{BOLD}╔══════════════════════════════╗")
    print(f"║   Atlas Diagnostics          ║")
    print(f"╚══════════════════════════════╝{RESET}")

    await check_env()
    await check_anthropic()
    await check_discord()
    await check_todoist()
    await check_calendar()
    await check_memory()

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    color = GREEN if passed == total else (YELLOW if passed >= total // 2 else RED)

    print(f"\n{BOLD}Result: {color}{passed}/{total} checks passed{RESET}")
    if passed < total:
        print("Run `python scripts/setup_wizard.py` to fix missing config.\n")
    else:
        print(f"{GREEN}All systems go! Run: python main.py{RESET}\n")


if __name__ == "__main__":
    import os
    os.chdir(ROOT)
    asyncio.run(main())
