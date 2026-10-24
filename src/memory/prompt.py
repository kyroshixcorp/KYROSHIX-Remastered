"""Format memories for system prompt injection with intelligent selection."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .system import memory_system
from .intelligence import intelligence_system

logger = logging.getLogger(__name__)


def format_memories_for_prompt(memories: List[Dict[str, Any]], max_length: int = 200) -> str:
    """Format memories for system prompt injection."""
    if not memories:
        return ""

    lines = []
    for i, mem in enumerate(memories, 1):
        content = mem.get("content", "")
        if len(content) > max_length:
            content = content[:max_length] + "..."

        importance = mem.get("importance")
        confidence = mem.get("confidence")
        meta = ""
        if importance is not None:
            meta += f" [imp:{importance}]"
        if confidence is not None:
            meta += f" [conf:{confidence}]"

        line = f"{i}. [{mem.get('key', 'unknown')}] ({mem.get('category', 'general')}){meta}: {content}"
        lines.append(line)

    return "\n".join(lines)


def get_memory_content_for_prompt(count: int = 10, query: Optional[str] = None) -> str:
    """Get intelligently selected memory content for system prompt.

    When a query is provided, uses context-aware recall (semantic + importance
    + recency). Otherwise falls back to recent pinned memories.
    """
    if not memory_system.is_available():
        return ""

    try:
        if query:
            recall = intelligence_system.recall_context_aware(
                query=query, limit=count, min_importance=15.0
            )
            memories = recall.get("memories", [])
        else:
            memories = memory_system.get_recent_for_prompt(count=count)
            for m in memories:
                m["importance"] = intelligence_system.compute_importance(m)
                m["confidence"] = intelligence_system.score_confidence(m)

        if not memories:
            return ""

        formatted = format_memories_for_prompt(memories)
        if formatted:
            stats = memory_system.stats().get("stats", {})
            total = stats.get("total", 0)
            long_term = stats.get("long_term", 0)
            short_term = stats.get("short_term", 0)
            return (
                f"\n=== INTELLIGENT MEMORIES ({len(memories)} selected, "
                f"{total} total: {long_term} long_term, {short_term} short_term) ===\n"
                f"{formatted}"
            )
        return ""
    except Exception as e:
        logger.error(f"get_memory_content_for_prompt failed: {e}")
        return ""
