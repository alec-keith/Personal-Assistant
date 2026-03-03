"""
PostgreSQL-backed memory store with hybrid search (semantic + full-text).

Tables (legacy):
  - conversations: rolling summaries of past exchanges
  - notes: explicit notes the user asks Roman to remember
  - user_profile: a single living document always injected into the system prompt

Tables (Roman-Elite v1.1):
  - memory: unified store with discriminator (long_term, working, episodic_log, pattern_store)
  - onboarding_state: single-row interview progress tracker

Semantic search uses pgvector + Voyage AI embeddings (voyage-3-lite, 1024 dims).
Falls back to full-text search if VOYAGE_API_KEY is not configured.

If no database is configured (DATABASE_URL empty), all operations are
silent no-ops so the bot still runs without memory.
"""

import json
import logging
import uuid
from datetime import date, datetime, timedelta, timezone
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
                try:
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
                except Exception:
                    # pgvector column may not exist yet — fall back to text-only
                    await conn.execute(
                        "INSERT INTO conversations (id, content, metadata) VALUES ($1, $2, $3::jsonb)",
                        doc_id,
                        summary,
                        str(metadata or {}),
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
                try:
                    await conn.execute(
                        """
                        INSERT INTO notes (id, content, tags, embedding)
                        VALUES ($1, $2, $3, $4::vector)
                        """,
                        doc_id, note, tags or [], _vec_to_pg(embedding),
                    )
                except Exception:
                    # pgvector column may not exist yet — fall back to text-only
                    await conn.execute(
                        "INSERT INTO notes (id, content, tags) VALUES ($1, $2, $3)",
                        doc_id, note, tags or [],
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

    async def get_and_clear_location_reminders(self, location: str) -> list[dict]:
        """
        Fetch notes tagged location:{location} that are due today or earlier (or have no due date).
        Deletes only the ones that fired. Returns list of {"content": str, "due_date": str | None}.
        """
        if not self._available:
            return []
        today = date.today().isoformat()
        tag = f"location:{location}"
        async with self._db.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, content, tags FROM notes WHERE $1 = ANY(tags)",
                tag,
            )
            if not rows:
                return []

            due_now = []
            for r in rows:
                tags = r["tags"] or []
                due_tag = next((t for t in tags if t.startswith("due:")), None)
                if due_tag is None or due_tag[4:] <= today:
                    due_now.append({
                        "id": str(r["id"]),
                        "content": r["content"],
                        "due_date": due_tag[4:] if due_tag else None,
                    })

            if not due_now:
                return []

            ids = [item["id"] for item in due_now]
            await conn.execute(
                "DELETE FROM notes WHERE id = ANY($1::uuid[])",
                ids,
            )
        return [{"content": item["content"], "due_date": item["due_date"]} for item in due_now]

    # ------------------------------------------------------------------
    # Roman-Elite v1.1: Unified memory table
    # ------------------------------------------------------------------

    # TTL defaults per store (days). None = no expiry.
    _STORE_TTL: dict[str, int | None] = {
        "long_term": None,
        "working": 30,
        "episodic_log": 365,
        "pattern_store": None,
    }

    async def save_memory(
        self,
        store: str,
        content: str,
        metadata: dict | None = None,
        tags: list[str] | None = None,
    ) -> str:
        """Write an entry to the unified memory table with auto-TTL."""
        if not self._available:
            return ""
        doc_id = str(uuid.uuid4())
        ttl = self._STORE_TTL.get(store)
        expires_at = (datetime.now(timezone.utc) + timedelta(days=ttl)) if ttl else None
        meta_json = json.dumps(metadata or {})
        embedding = await _embed(content)
        async with self._db.pool.acquire() as conn:
            if embedding is not None:
                try:
                    await conn.execute(
                        """
                        INSERT INTO memory (id, store, content, metadata, tags, embedding, expires_at)
                        VALUES ($1, $2, $3, $4::jsonb, $5, $6::vector, $7)
                        """,
                        doc_id, store, content, meta_json, tags or [], _vec_to_pg(embedding), expires_at,
                    )
                except Exception:
                    await conn.execute(
                        """
                        INSERT INTO memory (id, store, content, metadata, tags, expires_at)
                        VALUES ($1, $2, $3, $4::jsonb, $5, $6)
                        """,
                        doc_id, store, content, meta_json, tags or [], expires_at,
                    )
            else:
                await conn.execute(
                    """
                    INSERT INTO memory (id, store, content, metadata, tags, expires_at)
                    VALUES ($1, $2, $3, $4::jsonb, $5, $6)
                    """,
                    doc_id, store, content, meta_json, tags or [], expires_at,
                )
        logger.info("Saved to %s: %s…", store, content[:60])
        return doc_id

    async def search_memory(
        self,
        query: str,
        stores: list[str] | None = None,
        n: int = 5,
    ) -> list[dict]:
        """Semantic/full-text search across specified stores, excludes expired."""
        if not self._available:
            return []
        stores = stores or ["long_term", "working", "episodic_log", "pattern_store"]
        embedding = await _embed(query)
        async with self._db.pool.acquire() as conn:
            if embedding is not None:
                rows = await conn.fetch(
                    """
                    SELECT id, store, content, metadata, tags, created_at FROM memory
                    WHERE store = ANY($1)
                      AND (expires_at IS NULL OR expires_at > NOW())
                      AND embedding IS NOT NULL
                    ORDER BY embedding <=> $2::vector
                    LIMIT $3
                    """,
                    stores, _vec_to_pg(embedding), n,
                )
                if rows:
                    return [
                        {
                            "id": str(r["id"]),
                            "store": r["store"],
                            "content": r["content"],
                            "metadata": dict(r["metadata"]) if r["metadata"] else {},
                            "tags": r["tags"] or [],
                            "created_at": r["created_at"].isoformat(),
                        }
                        for r in rows
                    ]

            # Full-text fallback
            rows = await conn.fetch(
                """
                SELECT id, store, content, metadata, tags, created_at FROM memory
                WHERE store = ANY($1)
                  AND (expires_at IS NULL OR expires_at > NOW())
                  AND to_tsvector('english', content) @@ plainto_tsquery('english', $2)
                ORDER BY ts_rank(to_tsvector('english', content), plainto_tsquery('english', $2)) DESC
                LIMIT $3
                """,
                stores, query, n,
            )
            if not rows:
                rows = await conn.fetch(
                    """
                    SELECT id, store, content, metadata, tags, created_at FROM memory
                    WHERE store = ANY($1)
                      AND (expires_at IS NULL OR expires_at > NOW())
                    ORDER BY created_at DESC
                    LIMIT $2
                    """,
                    stores, n,
                )
        return [
            {
                "id": str(r["id"]),
                "store": r["store"],
                "content": r["content"],
                "metadata": dict(r["metadata"]) if r["metadata"] else {},
                "tags": r["tags"] or [],
                "created_at": r["created_at"].isoformat(),
            }
            for r in rows
        ]

    async def get_memory_by_key(self, store: str, key: str) -> str | None:
        """Look up a working memory entry by its metadata key."""
        if not self._available:
            return None
        async with self._db.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT content FROM memory
                WHERE store = $1
                  AND metadata->>'key' = $2
                  AND (expires_at IS NULL OR expires_at > NOW())
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                store, key,
            )
        return row["content"] if row else None

    async def delete_expired_memory(self) -> int:
        """Remove expired entries. Returns count deleted."""
        if not self._available:
            return 0
        async with self._db.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM memory WHERE expires_at IS NOT NULL AND expires_at < NOW()"
            )
        count = int(result.split()[-1])
        if count:
            logger.info("TTL cleanup: removed %d expired memory entries", count)
        return count

    # ------------------------------------------------------------------
    # Roman-Elite v1.1: Pattern store
    # ------------------------------------------------------------------

    async def record_pattern(self, key: str, description: str) -> None:
        """Increment evidence_count for an existing pattern, or insert a new one."""
        if not self._available:
            return
        today = date.today().isoformat()
        async with self._db.pool.acquire() as conn:
            # Try to increment existing pattern
            result = await conn.execute(
                """
                UPDATE memory SET
                    metadata = jsonb_set(
                        jsonb_set(metadata, '{occurrences}',
                            (COALESCE((metadata->>'occurrences')::int, 0) + 1)::text::jsonb),
                        '{last_observed}', $2::jsonb),
                    updated_at = NOW()
                WHERE store = 'pattern_store'
                  AND metadata->>'pattern_key' = $1
                """,
                key, json.dumps(today),
            )
            if result.split()[-1] != "0":
                return
            # Insert new pattern
            await self.save_memory(
                store="pattern_store",
                content=description,
                metadata={
                    "pattern_key": key,
                    "occurrences": 1,
                    "first_seen": today,
                    "last_observed": today,
                    "confirmed_by_user": False,
                },
                tags=["pattern"],
            )

    async def get_patterns(self, confirmed_only: bool = False) -> list[dict]:
        """Return all patterns, optionally filtered to confirmed-only."""
        if not self._available:
            return []
        async with self._db.pool.acquire() as conn:
            if confirmed_only:
                rows = await conn.fetch(
                    """
                    SELECT content, metadata FROM memory
                    WHERE store = 'pattern_store'
                      AND (metadata->>'confirmed_by_user')::boolean = true
                    ORDER BY updated_at DESC
                    """
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT content, metadata FROM memory
                    WHERE store = 'pattern_store'
                    ORDER BY updated_at DESC
                    """
                )
        return [
            {"content": r["content"], "metadata": dict(r["metadata"]) if r["metadata"] else {}}
            for r in rows
        ]

    async def confirm_pattern(self, key: str) -> None:
        """Mark a pattern as user-confirmed."""
        if not self._available:
            return
        async with self._db.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE memory SET
                    metadata = jsonb_set(metadata, '{confirmed_by_user}', 'true'::jsonb),
                    updated_at = NOW()
                WHERE store = 'pattern_store'
                  AND metadata->>'pattern_key' = $1
                """,
                key,
            )

    # ------------------------------------------------------------------
    # Roman-Elite v1.1: Onboarding state
    # ------------------------------------------------------------------

    async def get_onboarding_state(self) -> dict:
        """Load onboarding state from DB."""
        if not self._available:
            return {"status": "not_started", "current_wave_id": None, "question_index": 0,
                    "completed_waves": [], "answers": {}}
        async with self._db.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM onboarding_state WHERE id = 1")
        if not row:
            return {"status": "not_started", "current_wave_id": None, "question_index": 0,
                    "completed_waves": [], "answers": {}}
        return {
            "status": row["status"],
            "current_wave_id": row["current_wave_id"],
            "question_index": row["question_index"],
            "completed_waves": list(row["completed_waves"] or []),
            "answers": dict(row["answers"]) if row["answers"] else {},
        }

    async def update_onboarding_state(self, **kwargs) -> None:
        """Update specific fields on the onboarding state row."""
        if not self._available:
            return
        set_clauses = ["updated_at = NOW()"]
        values = []
        idx = 1
        for key, val in kwargs.items():
            if key == "status":
                set_clauses.append(f"status = ${idx}")
                values.append(val)
            elif key == "current_wave_id":
                set_clauses.append(f"current_wave_id = ${idx}")
                values.append(val)
            elif key == "question_index":
                set_clauses.append(f"question_index = ${idx}")
                values.append(val)
            elif key == "completed_waves":
                set_clauses.append(f"completed_waves = ${idx}")
                values.append(val)
            elif key == "answers":
                set_clauses.append(f"answers = ${idx}::jsonb")
                values.append(json.dumps(val))
            else:
                continue
            idx += 1

        if len(set_clauses) <= 1:
            return

        sql = f"UPDATE onboarding_state SET {', '.join(set_clauses)} WHERE id = 1"
        async with self._db.pool.acquire() as conn:
            await conn.execute(sql, *values)
