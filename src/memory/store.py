"""
PostgreSQL-backed memory store with hybrid search (semantic + full-text).

Three tables:
  - conversations: rolling summaries of past exchanges
  - notes: explicit notes the user asks Roman to remember
  - user_profile: a single living document always injected into the system prompt

Semantic search uses pgvector + Voyage AI embeddings (voyage-3-lite, 1024 dims).
Falls back to full-text search if VOYAGE_API_KEY is not configured.

If no database is configured (DATABASE_URL empty), all operations are
silent no-ops so the bot still runs without memory.
"""

import logging
import uuid
from typing import Any

from .database import Database

logger = logging.getLogger(__name__)

# Lazily imported so missing voyageai package doesn't crash the whole bot
_voyage_client = None


def _get_voyage_client() -> Any | None:
    global _voyage_client
    if _voyage_client is not None:
        return _voyage_client
    try:
        from config import settings
        if not settings.voyage_api_key:
            return None
        import voyageai
        _voyage_client = voyageai.AsyncClient(api_key=settings.voyage_api_key)
        logger.info("Voyage AI embeddings enabled (voyage-3-lite)")
        return _voyage_client
    except Exception:
        logger.debug("Voyage AI not available — using full-text search only")
        return None


async def _embed(text: str) -> list[float] | None:
    """Generate a 1024-dim embedding via Voyage AI, or None if unavailable."""
    client = _get_voyage_client()
    if client is None:
        return None
    try:
        result = await client.embed([text], model="voyage-3-lite", input_type="document")
        return result.embeddings[0]
    except Exception:
        logger.debug("Embedding generation failed", exc_info=True)
        return None


def _vec_to_pg(vec: list[float]) -> str:
    """Format a float list as a pgvector literal: '[0.1,0.2,...]'"""
    return "[" + ",".join(f"{v:.8f}" for v in vec) + "]"


class MemoryStore:
    def __init__(self, db: Database | None = None) -> None:
        self._db = db

    @property
    def _available(self) -> bool:
        return self._db is not None

    # ------------------------------------------------------------------
    # User Profile
    # ------------------------------------------------------------------

    async def get_profile(self) -> str:
        """Return the full user profile document (empty string if none saved yet)."""
        if not self._available:
            return ""
        async with self._db.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT content FROM user_profile WHERE id = 1")
        return row["content"] if row else ""

    async def update_profile(self, content: str) -> None:
        """Replace the entire user profile document."""
        if not self._available:
            return
        async with self._db.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO user_profile (id, content, updated_at)
                VALUES (1, $1, NOW())
                ON CONFLICT (id) DO UPDATE SET content = $1, updated_at = NOW()
                """,
                content,
            )
        logger.info("User profile updated (%d chars)", len(content))

    # ------------------------------------------------------------------
    # Conversations
    # ------------------------------------------------------------------

    async def save_conversation_summary(self, summary: str, metadata: dict | None = None) -> str:
        if not self._available:
            return ""
        doc_id = str(uuid.uuid4())
        embedding = await _embed(summary)
        async with self._db.pool.acquire() as conn:
            if embedding is not None:
                await conn.execute(
                    """
                    INSERT INTO conversations (id, content, metadata, embedding)
                    VALUES ($1, $2, $3::jsonb, $4::vector)
                    """,
                    doc_id,
                    summary,
                    str(metadata or {}),
                    _vec_to_pg(embedding),
                )
            else:
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
        embedding = await _embed(query)
        async with self._db.pool.acquire() as conn:
            if embedding is not None:
                # Semantic search: cosine distance (lower = more similar)
                rows = await conn.fetch(
                    """
                    SELECT content FROM conversations
                    WHERE embedding IS NOT NULL
                    ORDER BY embedding <=> $1::vector
                    LIMIT $2
                    """,
                    _vec_to_pg(embedding),
                    n,
                )
                if rows:
                    return [r["content"] for r in rows]

            # Fall back to full-text search
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
        embedding = await _embed(note)
        async with self._db.pool.acquire() as conn:
            if embedding is not None:
                await conn.execute(
                    """
                    INSERT INTO notes (id, content, tags, embedding)
                    VALUES ($1, $2, $3, $4::vector)
                    """,
                    doc_id, note, tags or [], _vec_to_pg(embedding),
                )
            else:
                await conn.execute(
                    "INSERT INTO notes (id, content, tags) VALUES ($1, $2, $3)",
                    doc_id, note, tags or [],
                )
        logger.info("Saved note: %s…", note[:60])
        return doc_id

    async def search_notes(self, query: str, n: int = 5) -> list[str]:
        if not self._available:
            return []
        embedding = await _embed(query)
        async with self._db.pool.acquire() as conn:
            if embedding is not None:
                rows = await conn.fetch(
                    """
                    SELECT content FROM notes
                    WHERE embedding IS NOT NULL
                    ORDER BY embedding <=> $1::vector
                    LIMIT $2
                    """,
                    _vec_to_pg(embedding),
                    n,
                )
                if rows:
                    return [r["content"] for r in rows]

            # Fall back to full-text search
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

    async def get_and_clear_location_reminders(self, location: str) -> list[str]:
        """
        Fetch all notes tagged with location:{location}, delete them, return their content.
        Called when the user's iOS Shortcut signals arrival at a named location.
        """
        if not self._available:
            return []
        tag = f"location:{location}"
        async with self._db.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, content FROM notes WHERE $1 = ANY(tags)",
                tag,
            )
            if not rows:
                return []
            ids = [str(r["id"]) for r in rows]
            await conn.execute(
                "DELETE FROM notes WHERE id = ANY($1::uuid[])",
                ids,
            )
        return [r["content"] for r in rows]
