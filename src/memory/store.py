"""
ChromaDB-backed vector memory store.

Two collections:
  - "conversations": rolling window of message summaries
  - "notes": explicit notes/brain-dumps the user asks to remember
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Literal

import chromadb
from chromadb.utils import embedding_functions

from config import settings

logger = logging.getLogger(__name__)

CollectionName = Literal["conversations", "notes"]


class MemoryStore:
    def __init__(self) -> None:
        self._client = chromadb.PersistentClient(path=settings.chroma_persist_dir)
        ef = embedding_functions.DefaultEmbeddingFunction()

        self._conversations = self._client.get_or_create_collection(
            "conversations", embedding_function=ef
        )
        self._notes = self._client.get_or_create_collection(
            "notes", embedding_function=ef
        )
        logger.info("Memory store ready at %s", settings.chroma_persist_dir)

    # ------------------------------------------------------------------
    # Conversations
    # ------------------------------------------------------------------

    def save_conversation_summary(self, summary: str, metadata: dict | None = None) -> str:
        """Persist a summary of a conversation turn."""
        doc_id = str(uuid.uuid4())
        self._conversations.add(
            ids=[doc_id],
            documents=[summary],
            metadatas=[{
                "timestamp": datetime.now(timezone.utc).isoformat(),
                **(metadata or {}),
            }],
        )
        return doc_id

    def search_conversations(self, query: str, n: int = 5) -> list[str]:
        """Retrieve the most relevant past conversation summaries."""
        if self._conversations.count() == 0:
            return []
        results = self._conversations.query(
            query_texts=[query],
            n_results=min(n, self._conversations.count()),
        )
        return results["documents"][0] if results["documents"] else []

    # ------------------------------------------------------------------
    # Notes / explicit memories
    # ------------------------------------------------------------------

    def save_note(self, note: str, tags: list[str] | None = None) -> str:
        """Explicitly store something the user wants remembered."""
        doc_id = str(uuid.uuid4())
        self._notes.add(
            ids=[doc_id],
            documents=[note],
            metadatas=[{
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "tags": ",".join(tags or []),
            }],
        )
        logger.info("Saved note: %s…", note[:60])
        return doc_id

    def search_notes(self, query: str, n: int = 5) -> list[str]:
        """Retrieve notes relevant to a query."""
        if self._notes.count() == 0:
            return []
        results = self._notes.query(
            query_texts=[query],
            n_results=min(n, self._notes.count()),
        )
        return results["documents"][0] if results["documents"] else []

    def list_recent_notes(self, limit: int = 10) -> list[dict]:
        """Return the most recent notes (for display purposes)."""
        result = self._notes.get(
            limit=limit,
            include=["documents", "metadatas"],
        )
        items = []
        for doc, meta in zip(result["documents"], result["metadatas"]):
            items.append({"content": doc, "metadata": meta})
        return sorted(items, key=lambda x: x["metadata"].get("timestamp", ""), reverse=True)

    def delete_note(self, doc_id: str) -> None:
        self._notes.delete(ids=[doc_id])
