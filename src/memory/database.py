"""
PostgreSQL connection pool and schema initialization.

Railway auto-injects DATABASE_URL when you add a Postgres plugin.
Locally: set DATABASE_URL in .env, or leave blank to disable memory.
"""

import logging

import asyncpg

logger = logging.getLogger(__name__)

# Base schema — no vector types, works on any Postgres instance
BASE_SCHEMA = """
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

CREATE TABLE IF NOT EXISTS user_profile (
    id          INTEGER     PRIMARY KEY DEFAULT 1,
    content     TEXT        NOT NULL DEFAULT '',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO user_profile (id, content) VALUES (1, '') ON CONFLICT DO NOTHING;

-- Roman-Elite v1.1: Unified memory table (long_term, working, episodic_log, pattern_store)
CREATE TABLE IF NOT EXISTS memory (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    store       TEXT        NOT NULL,
    content     TEXT        NOT NULL,
    metadata    JSONB       NOT NULL DEFAULT '{}',
    tags        TEXT[]      NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS memory_store_idx ON memory (store);
CREATE INDEX IF NOT EXISTS memory_expires_idx ON memory (expires_at) WHERE expires_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS memory_tags_idx ON memory USING GIN (tags);
CREATE INDEX IF NOT EXISTS memory_store_created_idx ON memory (store, created_at DESC);

-- Roman-Elite v1.1: Onboarding state (single row)
CREATE TABLE IF NOT EXISTS onboarding_state (
    id              INTEGER     PRIMARY KEY DEFAULT 1,
    status          TEXT        NOT NULL DEFAULT 'not_started',
    current_wave_id TEXT,
    question_index  INTEGER     NOT NULL DEFAULT 0,
    completed_waves TEXT[]      NOT NULL DEFAULT '{}',
    answers         JSONB       NOT NULL DEFAULT '{}',
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO onboarding_state (id) VALUES (1) ON CONFLICT DO NOTHING;
"""

# pgvector enhancements — attempted separately; fails gracefully if extension unavailable
VECTOR_SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;

ALTER TABLE conversations ADD COLUMN IF NOT EXISTS embedding vector(1024);
ALTER TABLE notes ADD COLUMN IF NOT EXISTS embedding vector(1024);

CREATE INDEX IF NOT EXISTS conversations_embedding_idx
    ON conversations USING hnsw (embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS notes_embedding_idx
    ON notes USING hnsw (embedding vector_cosine_ops);

ALTER TABLE memory ADD COLUMN IF NOT EXISTS embedding vector(1024);

CREATE INDEX IF NOT EXISTS memory_embedding_idx
    ON memory USING hnsw (embedding vector_cosine_ops);
"""


class Database:
    def __init__(self, url: str) -> None:
        self._url = url
        self._pool: asyncpg.Pool | None = None

    async def initialize(self) -> None:
        self._pool = await asyncpg.create_pool(self._url, min_size=1, max_size=5)
        async with self._pool.acquire() as conn:
            await conn.execute(BASE_SCHEMA)
        # pgvector is optional — falls back to full-text search if unavailable
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(VECTOR_SCHEMA)
            logger.info("PostgreSQL database initialized (pgvector enabled)")
        except Exception:
            logger.warning(
                "pgvector extension not available — memory will use full-text search. "
                "Enable pgvector on your Postgres instance for semantic search."
            )
            logger.info("PostgreSQL database initialized (text-search mode)")

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("Database not initialized — call initialize() first")
        return self._pool

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
