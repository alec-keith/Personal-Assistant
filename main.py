"""
Entry point for the Atlas personal assistant.

Run with:
    python main.py

Or as a background service:
    nohup python main.py >> logs/atlas.log 2>&1 &
"""

import asyncio
import logging
import sys
from pathlib import Path

# Ensure src/ is on the path
sys.path.insert(0, str(Path(__file__).parent))

from config import settings
from src.memory.store import MemoryStore
from src.integrations.todoist import TodoistClient
from src.integrations.calendar import CalendarClient
from src.agent.core import AgentCore
from src.scheduler.proactive import ProactiveScheduler
from src.integrations.messaging.discord_gateway import DiscordGateway

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/atlas.log"),
    ],
)
logger = logging.getLogger(__name__)


async def main() -> None:
    logger.info("Starting %s...", settings.agent_name)

    # -- Shared resources --
    memory = MemoryStore()
    todoist = TodoistClient()
    calendar = CalendarClient()

    # -- Scheduler (needs send_fn, wired up below) --
    # We use a placeholder then patch it after the gateway is created
    send_fn_holder: list = []

    async def send_fn(text: str) -> None:
        if send_fn_holder:
            await send_fn_holder[0](text)

    scheduler = ProactiveScheduler(
        send_fn=send_fn,
        todoist=todoist,
        calendar=calendar,
    )

    # -- Agent --
    agent = AgentCore(
        memory=memory,
        todoist=todoist,
        calendar=calendar,
        schedule_reminder_fn=scheduler.schedule_reminder,
    )

    # -- Discord gateway --
    discord = DiscordGateway(on_message=agent.handle_message)

    # Wire up the send function
    send_fn_holder.append(discord.send_message)

    # -- Start scheduler --
    scheduler.start()

    logger.info("%s is online.", settings.agent_name)

    # -- Run Discord bot (blocks until stopped) --
    try:
        await discord.start()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        scheduler.shutdown()


if __name__ == "__main__":
    Path("logs").mkdir(exist_ok=True)
    asyncio.run(main())
