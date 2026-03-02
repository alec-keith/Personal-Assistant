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
import sys
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

sys.path.insert(0, str(Path(__file__).parent))

from config import settings
from src.memory.database import Database
from src.memory.store import MemoryStore
from src.integrations.todoist import TodoistClient
from src.integrations.calendar import CalendarClient
from src.integrations.clickup import ClickUpClient
from src.agent.core import AgentCore
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
        db = Database(settings.database_url)
        await db.initialize()
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

    # -- FastAPI app (needed even if BB disabled, for Railway health checks) --
    app = build_app()

    # -- Agent --
    agent = AgentCore(
        memory=memory,
        todoist=todoist,
        calendar=calendar,
        clickup=clickup,
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

    scheduler = ProactiveScheduler(send_fn=send_fn, todoist=todoist, calendar=calendar, db=db)
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
