"""
Entry point for the Atlas personal assistant.

Runs two gateways simultaneously:
  - BlueBubbles (iMessage, primary) — via FastAPI webhook server
  - Discord (fallback) — via Discord.py WebSocket bot

The proactive scheduler tries BlueBubbles first; falls back to Discord.

Run locally:
    python main.py

Deploy to Railway:
    Push to GitHub → connect repo in Railway dashboard
"""

import asyncio
import logging
import random
import re
import sys
from datetime import datetime, date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

sys.path.insert(0, str(Path(__file__).parent))

from config import settings
from src.memory.database import Database
from src.memory.store import MemoryStore
from src.integrations.todoist import TodoistClient
from src.integrations.calendar import CalendarClient
from src.integrations.clickup import ClickUpClient
from src.integrations.email import EmailManager
from src.agent.core import AgentCore
from src.agent.onboarding import OnboardingManager
from src.agent.tool_executor import ToolExecutor
from src.scheduler.proactive import ProactiveScheduler
from src.integrations.messaging.discord_gateway import DiscordGateway
from src.integrations.messaging.bluebubbles_gateway import BlueBubblesGateway

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/atlas.log"),
    ],
)
logger = logging.getLogger(__name__)


def build_app() -> FastAPI:
    """Build the FastAPI app (shared between BlueBubbles webhooks and health check)."""
    app = FastAPI(title="Atlas", docs_url=None, redoc_url=None)

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse({"status": "ok", "agent": settings.agent_name})

    return app


async def main() -> None:
    logger.info("Starting %s...", settings.agent_name)

    # -- Database (PostgreSQL) --
    db: Database | None = None
    if settings.database_url:
        # asyncpg requires postgresql:// not postgres:// (Railway sometimes gives the latter)
        db_url = settings.database_url.replace("postgres://", "postgresql://", 1)
        try:
            db = Database(db_url)
            await db.initialize()
        except Exception:
            logger.exception(
                "Database initialization failed — running without memory. "
                "Check DATABASE_URL and that pgvector is enabled on the Postgres instance."
            )
            db = None
    else:
        logger.warning(
            "DATABASE_URL not set — memory disabled. "
            "Add a Postgres plugin in Railway and it will be injected automatically."
        )

    # -- Shared resources --
    memory = MemoryStore(db=db)
    todoist = TodoistClient()
    calendar = CalendarClient()
    clickup = ClickUpClient() if settings.clickup_api_token else None
    email = EmailManager.from_settings() if (settings.gmail_accounts or settings.yahoo_accounts) else None

    # -- Elite components --
    onboarding = OnboardingManager(memory) if settings.enable_onboarding else None
    tool_executor = ToolExecutor() if settings.enable_tool_executor else None

    # -- FastAPI app (needed even if BB disabled, for Railway health checks) --
    app = build_app()

    # -- Agent --
    agent = AgentCore(
        memory=memory,
        todoist=todoist,
        calendar=calendar,
        clickup=clickup,
        email=email,
        onboarding=onboarding,
        tool_executor=tool_executor,
        schedule_reminder_fn=None,  # patched below
    )

    # -- Build active gateways --
    gateways: list[DiscordGateway | BlueBubblesGateway] = []
    bb_gateway: BlueBubblesGateway | None = None
    discord_gateway: DiscordGateway | None = None

    if settings.use_bluebubbles:
        bb_gateway = BlueBubblesGateway(on_message=agent.handle_message, app=app)
        gateways.append(bb_gateway)
        logger.info("BlueBubbles (iMessage) gateway enabled")
    else:
        logger.info("BlueBubbles not configured — skipping iMessage gateway")

    if settings.use_discord:
        discord_gateway = DiscordGateway(on_message=agent.handle_message)
        gateways.append(discord_gateway)
        logger.info("Discord gateway enabled")
    else:
        logger.info("Discord not configured — skipping Discord gateway")

    if not gateways:
        logger.warning(
            "No messaging gateway configured! Set DISCORD_BOT_TOKEN or "
            "BLUEBUBBLES_SERVER_URL in .env. Running in headless mode."
        )

    # -- Scheduler with fallback-aware send_fn --
    async def send_fn(text: str) -> None:
        """Try BlueBubbles first, fall back to Discord."""
        # Prefer iMessage when Mac is up
        if bb_gateway is not None and await bb_gateway.is_reachable():
            try:
                await bb_gateway.send_message(text)
                return
            except Exception:
                logger.warning("BlueBubbles send failed, falling back to Discord")

        if discord_gateway is not None:
            try:
                await discord_gateway.send_message(text)
                return
            except Exception:
                logger.error("Discord fallback also failed")

        logger.error("No gateway available to send proactive message")

    async def clean_send_fn(text: str) -> None:
        """Strip internal task IDs before delivering any proactive message."""
        await send_fn(re.sub(r"\s*\[id:[^\]]+\]", "", text))

    # -- Location-arrived webhook (silent, no Discord message needed) --
    @app.post("/location-arrived")
    async def location_arrived(request: Request) -> JSONResponse:
        if not settings.location_webhook_secret:
            raise HTTPException(status_code=404)
        body = await request.json()
        if body.get("secret") != settings.location_webhook_secret:
            raise HTTPException(status_code=403, detail="Invalid secret")
        location = str(body.get("location", "home")).lower().strip()
        reminders = await memory.get_and_clear_location_reminders(location)
        if reminders:
            lines = "\n".join(f"• {r['content']}" for r in reminders)
            loc_phrase = {
                "home": "home",
                "office": "at the office",
                "work": "at work",
                "gym": "at the gym",
            }.get(location, f"at {location}")
            intros = [
                f"You're {loc_phrase} — here's what you had on the list:",
                f"Welcome {loc_phrase}. Don't let these slip:",
                f"Now that you're {loc_phrase}, a few things to take care of:",
                f"Glad you made it {loc_phrase}. Heads up on these:",
                f"You're {loc_phrase} — don't forget:",
            ]
            await clean_send_fn(f"{random.choice(intros)}\n{lines}")

            # Add each reminder as a Todoist task (with due date if one was specified)
            for r in reminders:
                try:
                    await todoist.add_task(
                        r["content"],
                        due_date=r["due_date"] or date.today().isoformat(),
                        labels=["location"],
                    )
                except Exception:
                    logger.warning("Failed to create Todoist task for location reminder: %r", r["content"])

            # Save to memory so Roman has context, then schedule a follow-up check
            tz = ZoneInfo(settings.agent_timezone)
            task_summary = ", ".join(r["content"] for r in reminders)
            try:
                await memory.save_note(
                    f"Reminded user on arrival {loc_phrase}: {task_summary}",
                    tags=["location:followup"],
                )
            except Exception:
                logger.warning("Failed to save location followup note")

            followup_checks = [
                f"Earlier when you got {location} you had these on the list:\n{lines}\n\nDid any of those get done? Tell me which ones and I'll mark them off.",
                f"Checking in — you had a few things to handle when you arrived {loc_phrase}:\n{lines}\n\nAnything you knocked out? I'll complete them in Todoist.",
                f"Quick follow-up on the things from when you got {location}:\n{lines}\n\nWhere do things stand? Just say which ones are done.",
                f"You had some things lined up for when you got {location}:\n{lines}\n\nHow's that looking? Tell me what you got to and I'll close them out.",
            ]
            await scheduler.schedule_reminder(
                random.choice(followup_checks),
                datetime.now(tz) + timedelta(hours=3),
            )

        return JSONResponse({"ok": True, "reminders_fired": len(reminders)})

    scheduler = ProactiveScheduler(
        send_fn=clean_send_fn, todoist=todoist, calendar=calendar,
        db=db, memory=memory, email_manager=email,
    )
    agent._scheduler = scheduler  # give agent full scheduler access
    await scheduler.initialize()  # load persistent jobs from DB
    scheduler.start()

    logger.info("%s is online.", settings.agent_name)

    # -- Run everything concurrently --
    tasks = []

    # FastAPI / uvicorn server (always runs — needed for Railway health checks)
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=settings.port,
        log_level="warning",
        access_log=False,
        timeout_graceful_shutdown=3,
    )
    server = uvicorn.Server(config)
    tasks.append(asyncio.create_task(server.serve()))

    # Discord bot (if configured)
    if discord_gateway is not None:
        tasks.append(asyncio.create_task(discord_gateway.start()))

    try:
        await asyncio.gather(*tasks)
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Shutting down...")
    finally:
        scheduler.shutdown()
        for task in tasks:
            task.cancel()
        if db:
            await db.close()


if __name__ == "__main__":
    Path("logs").mkdir(exist_ok=True)
    asyncio.run(main())
