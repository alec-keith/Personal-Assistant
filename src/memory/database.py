"""
PostgreSQL connection pool and schema initialization.

Railway auto-injects DATABASE_URL when you add a Postgres plugin.
Locally: set DATABASE_URL in .env, or leave blank to disable memory.
"""

import logging

import asyncpg

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE IF NOT EXISTS conversations (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    content     TEXT        NOT NULL,
    metadata    JSONB       NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS notes (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    content     TEXT        NOT NULL,
    tags        TEXT[]      NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS recurring_jobs (
    id           TEXT        PRIMARY KEY,
    message      TEXT        NOT NULL,
    description  TEXT        NOT NULL,
    trigger_type TEXT        NOT NULL,
    trigger_args JSONB       NOT NULL,
    end_date     TEXT
);
"""


class Database:
    def __init__(self, url: str) -> None:
        self._url = url
        self._pool: asyncpg.Pool | None = None

    async def initialize(self) -> None:
        self._pool = await asyncpg.create_pool(self._url, min_size=1, max_size=5)
        async with self._pool.acquire() as conn:
            await conn.execute(SCHEMA)
        logger.info("PostgreSQL database initialized")

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("Database not initialized — call initialize() first")
        return self._pool

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
