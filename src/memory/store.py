"""
PostgreSQL-backed memory store with full-text search.

Two tables:
  - conversations: rolling summaries of past exchanges
  - notes: explicit notes the user asks Roman to remember

If no database is configured (DATABASE_URL empty), all operations are
silent no-ops so the bot still runs without memory.
"""

import logging
import uuid

from .database import Database

logger = logging.getLogger(__name__)


class MemoryStore:
    def __init__(self, db: Database | None = None) -> None:
        self._db = db

    @property
    def _available(self) -> bool:
        return self._db is not None

    # ------------------------------------------------------------------
    # Conversations
    # ------------------------------------------------------------------

    async def save_conversation_summary(self, summary: str, metadata: dict | None = None) -> str:
        if not self._available:
            return ""
        doc_id = str(uuid.uuid4())
        async with self._db.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO conversations (id, content, metadata) VALUES ($1, $2, $3::jsonb)",
                doc_id,
                summary,
                str(metadata or {}),
            )
        return doc_id

    async def search_conversations(self, query: str, n: int = 5) -> list[str]:
        if not self._available:
            return []
        async with self._db.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT content FROM conversations
                WHERE to_tsvector('english', content) @@ plainto_tsquery('english', $1)
                ORDER BY ts_rank(to_tsvector('english', content), plainto_tsquery('english', $1)) DESC
                LIMIT $2
                """,
                query, n,
            )
            if not rows:
                rows = await conn.fetch(
                    "SELECT content FROM conversations ORDER BY created_at DESC LIMIT $1", n
                )
        return [r["content"] for r in rows]

    # ------------------------------------------------------------------
    # Notes / explicit memories
    # ------------------------------------------------------------------

    async def save_note(self, note: str, tags: list[str] | None = None) -> str:
        if not self._available:
            return ""
        doc_id = str(uuid.uuid4())
        async with self._db.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO notes (id, content, tags) VALUES ($1, $2, $3)",
                doc_id, note, tags or [],
            )
        logger.info("Saved note: %s…", note[:60])
        return doc_id

    async def search_notes(self, query: str, n: int = 5) -> list[str]:
        if not self._available:
            return []
        async with self._db.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT content FROM notes
                WHERE to_tsvector('english', content) @@ plainto_tsquery('english', $1)
                ORDER BY ts_rank(to_tsvector('english', content), plainto_tsquery('english', $1)) DESC
                LIMIT $2
                """,
                query, n,
            )
            if not rows:
                rows = await conn.fetch(
                    "SELECT content FROM notes ORDER BY created_at DESC LIMIT $1", n
                )
        return [r["content"] for r in rows]

    async def list_recent_notes(self, limit: int = 10) -> list[dict]:
        if not self._available:
            return []
        async with self._db.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, content, tags, created_at FROM notes ORDER BY created_at DESC LIMIT $1",
                limit,
            )
        return [
            {
                "id": str(r["id"]),
                "content": r["content"],
                "metadata": {
                    "timestamp": r["created_at"].isoformat(),
                    "tags": ",".join(r["tags"] or []),
                },
            }
            for r in rows
        ]

    async def delete_note(self, doc_id: str) -> None:
        if not self._available:
            return
        async with self._db.pool.acquire() as conn:
            await conn.execute("DELETE FROM notes WHERE id = $1::uuid", doc_id)
