#!/usr/bin/env python3
"""
Interactive setup wizard — run this once to configure your .env file.
Validates each connection as you go.

Usage:
    python scripts/setup_wizard.py
"""
import sys, os, asyncio, getpass
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
ENV_PATH = ROOT / ".env"

BOLD = "\033[1m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
RESET = "\033[0m"


def p(msg): print(msg)
def ok(msg): print(f"{GREEN}✓ {msg}{RESET}")
def warn(msg): print(f"{YELLOW}⚠ {msg}{RESET}")
def err(msg): print(f"{RED}✗ {msg}{RESET}")
def header(msg): print(f"\n{BOLD}{msg}{RESET}")


def prompt(label: str, secret: bool = False, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        if secret:
            val = getpass.getpass(f"  {label}{suffix}: ")
        else:
            val = input(f"  {label}{suffix}: ").strip()
        return val or default
    except (KeyboardInterrupt, EOFError):
        print("\nAborted.")
        sys.exit(1)


def load_env() -> dict[str, str]:
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env


def save_env(env: dict[str, str]) -> None:
    lines = []
    # Preserve comments from .env.example
    example = ROOT / ".env.example"
    if example.exists():
        for line in example.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or not stripped:
                lines.append(line)
            elif "=" in stripped:
                key = stripped.split("=")[0].strip()
                val = env.get(key, "")
                lines.append(f"{key}={val}")
    else:
        for k, v in env.items():
            lines.append(f"{k}={v}")
    ENV_PATH.write_text("\n".join(lines) + "\n")
    ok(f"Saved to {ENV_PATH}")


# --------------------------------------------------------------------------
# Validators
# --------------------------------------------------------------------------

async def check_anthropic(key: str) -> bool:
    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=key)
        r = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            messages=[{"role": "user", "content": "hi"}],
        )
        ok("Anthropic API key valid")
        return True
    except Exception as e:
        err(f"Anthropic check failed: {e}")
        return False


async def check_discord(token: str) -> bool:
    try:
        import httpx
        async with httpx.AsyncClient() as c:
            r = await c.get(
                "https://discord.com/api/v10/users/@me",
                headers={"Authorization": f"Bot {token}"},
            )
        if r.status_code == 200:
            data = r.json()
            ok(f"Discord bot valid: {data.get('username')}#{data.get('discriminator')}")
            return True
        else:
            err(f"Discord token invalid (HTTP {r.status_code})")
            return False
    except Exception as e:
        err(f"Discord check failed: {e}")
        return False


async def check_todoist(token: str) -> bool:
    try:
        import httpx
        async with httpx.AsyncClient() as c:
            r = await c.get(
                "https://api.todoist.com/rest/v2/projects",
                headers={"Authorization": f"Bearer {token}"},
            )
        if r.status_code == 200:
            projects = r.json()
            ok(f"Todoist valid — {len(projects)} project(s) found")
            return True
        else:
            err(f"Todoist token invalid (HTTP {r.status_code})")
            return False
    except Exception as e:
        err(f"Todoist check failed: {e}")
        return False


async def check_icloud(username: str, password: str) -> bool:
    try:
        import caldav
        client = caldav.DAVClient(
            url="https://caldav.icloud.com",
            username=username,
            password=password,
        )
        principal = client.principal()
        cals = principal.calendars()
        ok(f"iCloud CalDAV valid — {len(cals)} calendar(s): {', '.join(c.name for c in cals[:3])}")
        return True
    except Exception as e:
        err(f"iCloud CalDAV check failed: {e}")
        warn("Make sure you're using an App-Specific Password, not your Apple ID password.")
        warn("Generate one at: appleid.apple.com → Security → App-Specific Passwords")
        return False


# --------------------------------------------------------------------------
# Main wizard
# --------------------------------------------------------------------------

async def main() -> None:
    p(f"\n{BOLD}╔══════════════════════════════════════╗")
    p(f"║   Atlas Personal Assistant Setup     ║")
    p(f"╚══════════════════════════════════════╝{RESET}")

    env = load_env()

    # 1. Anthropic
    header("1/4  Claude API (Anthropic)")
    p("  Get your key at: https://console.anthropic.com")
    key = prompt("ANTHROPIC_API_KEY", secret=True, default=env.get("ANTHROPIC_API_KEY", ""))
    if key:
        env["ANTHROPIC_API_KEY"] = key
        await check_anthropic(key)

    # 2. Discord
    header("2/4  Discord")
    p("  Steps:")
    p("    a) Go to https://discord.com/developers/applications")
    p("    b) New Application → Bot → Reset Token → copy token")
    p("    c) Enable: Message Content Intent, Server Members Intent")
    p("    d) To get your User ID: Discord Settings → Advanced → Developer Mode")
    p("       then right-click your own name → Copy User ID")
    token = prompt("DISCORD_BOT_TOKEN", secret=True, default=env.get("DISCORD_BOT_TOKEN", ""))
    user_id = prompt("DISCORD_USER_ID (your personal ID)", default=env.get("DISCORD_USER_ID", ""))
    if token:
        env["DISCORD_BOT_TOKEN"] = token
        await check_discord(token)
    if user_id:
        env["DISCORD_USER_ID"] = user_id

    p("\n  Bot invite URL (open this in your browser to add the bot to your server):")
    if token:
        try:
            import httpx, asyncio as _a
            async with httpx.AsyncClient() as c:
                r = await c.get(
                    "https://discord.com/api/v10/users/@me",
                    headers={"Authorization": f"Bot {token}"},
                )
            app_id = r.json().get("id", "YOUR_APP_ID")
        except Exception:
            app_id = "YOUR_APP_ID"
        print(f"  https://discord.com/oauth2/authorize?client_id={app_id}&scope=bot&permissions=274878024704")
    else:
        print("  (fill in DISCORD_BOT_TOKEN first, then re-run to get the URL)")

    # 3. Todoist
    header("3/4  Todoist")
    p("  Get your API token at: https://todoist.com/app/settings/integrations/developer")
    todoist_token = prompt("TODOIST_API_TOKEN", secret=True, default=env.get("TODOIST_API_TOKEN", ""))
    if todoist_token:
        env["TODOIST_API_TOKEN"] = todoist_token
        await check_todoist(todoist_token)

    # 4. iCloud CalDAV (Fantastical)
    header("4/4  Fantastical via iCloud CalDAV")
    p("  Fantastical reads from iCloud Calendar. We write there and it appears instantly.")
    p("  You need an App-Specific Password (NOT your Apple ID password):")
    p("    1) Go to: https://appleid.apple.com")
    p("    2) Security → App-Specific Passwords → Generate")
    p("    3) Name it 'Atlas' and copy the xxxx-xxxx-xxxx-xxxx password")
    icloud_user = prompt("ICLOUD_USERNAME (your Apple ID email)", default=env.get("ICLOUD_USERNAME", ""))
    icloud_pass = prompt("ICLOUD_APP_PASSWORD (xxxx-xxxx-xxxx-xxxx)", secret=True, default=env.get("ICLOUD_APP_PASSWORD", ""))
    if icloud_user and icloud_pass:
        env["ICLOUD_USERNAME"] = icloud_user
        env["ICLOUD_APP_PASSWORD"] = icloud_pass
        await check_icloud(icloud_user, icloud_pass)

    # Agent config
    header("Agent settings")
    tz = prompt("Your timezone (e.g. America/New_York, America/Los_Angeles)", default=env.get("AGENT_TIMEZONE", "America/New_York"))
    env["AGENT_TIMEZONE"] = tz
    name = prompt("Agent name", default=env.get("AGENT_NAME", "Atlas"))
    env["AGENT_NAME"] = name

    # Save
    header("Saving configuration")
    save_env(env)

    p(f"\n{BOLD}All done!{RESET}")
    p("Run the assistant with:")
    p(f"  source .venv/bin/activate && python main.py")
    p("")
    p("Or install as a background service (auto-starts on login):")
    p(f"  bash scripts/install_service.sh")


if __name__ == "__main__":
    os.chdir(ROOT)
    asyncio.run(main())
