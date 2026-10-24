"""Advanced intelligence system for persistent memory.

Provides:
  - Importance scoring and temporal decay for memories
  - Memory cross-referencing (links between related memories)
  - Entity extraction (people, places, topics) from memory content
  - Auto-summarization of short_term clusters into long_term memories
  - Context-aware recall (semantic + recency + importance + access frequency)
  - Confidence scoring for recall results
  - Memory clustering by topic similarity
"""

from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .system import memory_system
from ._helpers import (
    MEMORY_TYPE_LONG_TERM,
    MEMORY_TYPE_QUICK_NOTE,
    MEMORY_TYPE_SHORT_TERM,
    _hash_content,
)

logger = logging.getLogger(__name__)

# Default importance thresholds
DEFAULT_IMPORTANCE_MIN = 10      # below this, memory is considered low-importance
DEFAULT_IMPORTANCE_MAX = 100     # maximum importance score
DECAY_HALF_LIFE_DAYS = 30        # importance halves every 30 days for non-pinned memories
RECENCY_BOOST_DAYS = 7           # memories newer than this get a recency boost
ACCESS_FREQUENCY_WEIGHT = 0.3    # weight of access count in importance
RECENCY_WEIGHT = 0.4             # weight of recency in importance
PINNED_BONUS = 20                # flat bonus for pinned memories


def _now_iso() -> str:
    return datetime.utcnow().isoformat()


def _parse_dt(iso_str: Optional[str]) -> Optional[datetime]:
    if not iso_str:
        return None
    try:
        return datetime.fromisoformat(iso_str)
    except Exception:
        return None


def _days_ago(iso_str: Optional[str]) -> float:
    dt = _parse_dt(iso_str)
    if dt is None:
        return 99999.0
    return (datetime.utcnow() - dt).total_seconds() / 86400.0


class IntelligenceSystem:
    """Advanced memory intelligence layer on top of the persistent storage.

    Operates on the global ``memory_system`` instance. All methods are
    defensive: if storage is unavailable they return safe defaults.
    """

    # ------------------------------------------------------------------
    # importance scoring
    # ------------------------------------------------------------------
    def compute_importance(self, mem: Dict[str, Any]) -> float:
        """Compute an importance score (0-100) for a memory.

        Factors:
          - access count (more accesses = more important)
          - recency (newer = more important, decays over time)
          - pinned status (flat bonus)
          - memory type (long_term > short_term > quick_note)
          - content length / detail (longer, detailed = slightly more important)
        """
        if not mem:
            return 0.0

        access_count = int(mem.get("access_count", 0) or 0)
        days_ago = _days_ago(mem.get("created_at"))
        memory_type = mem.get("memory_type", MEMORY_TYPE_LONG_TERM)
        pinned = "pinned" in (mem.get("tags") or [])
        content_len = len(str(mem.get("content", "")))

        # Access frequency component (logarithmic to avoid runaway)
        access_score = min(40.0, ACCESS_FREQUENCY_WEIGHT * 100.0 * math.log1p(access_count))

        # Recency component (exponential decay)
        recency_raw = math.exp(-days_ago / DECAY_HALF_LIFE_DAYS)
        recency_score = RECENCY_WEIGHT * 100.0 * recency_raw

        # Type base score
        type_scores = {
            MEMORY_TYPE_LONG_TERM: 30.0,
            MEMORY_TYPE_SHORT_TERM: 15.0,
            MEMORY_TYPE_QUICK_NOTE: 5.0,
        }
        type_score = type_scores.get(memory_type, 10.0)

        # Pinned bonus
        pinned_score = PINNED_BONUS if pinned else 0.0

        # Detail bonus (capped)
        detail_score = min(10.0, content_len / 100.0)

        total = access_score + recency_score + type_score + pinned_score + detail_score
        return round(min(100.0, max(0.0, total)), 2)

    def apply_decay(self) -> Dict[str, int]:
        """Apply temporal decay to all long_term memories, lowering importance
        of old, rarely-accessed memories. Returns counts of affected memories."""
        if not memory_system.is_available():
            return {"decayed": 0}

        try:
            if memory_system.backend == "sqlite":
                with memory_system._sqlite_lock:
                    rows = memory_system.sqlite_conn.execute(
                        "SELECT key, content, category, memory_type, tags_json, created_at, access_count FROM memories WHERE memory_type = ?",
                        (MEMORY_TYPE_LONG_TERM,),
                    ).fetchall()

                    decayed = 0
                    for row in rows:
                        mem = {
                            "key": row[0],
                            "content": row[1],
                            "category": row[2],
                            "memory_type": row[3],
                            "tags": json.loads(row[4]) if row[4] else [],
                            "created_at": row[5],
                            "access_count": row[6],
                        }
                        importance = self.compute_importance(mem)

                        # If importance drops very low and not pinned, consider
                        # demoting to short_term (it will be cleaned up later)
                        tags = mem["tags"]
                        if importance < DEFAULT_IMPORTANCE_MIN and "pinned" not in tags:
                            new_tags = [t for t in tags if t != "pinned"]
                            memory_system.sqlite_conn.execute(
                                "UPDATE memories SET memory_type = ?, tags_json = ?, updated_at = ? WHERE key = ?",
                                (MEMORY_TYPE_SHORT_TERM, json.dumps(new_tags, ensure_ascii=False), _now_iso(), mem["key"]),
                            )
                            decayed += 1

                    memory_system.sqlite_conn.commit()
                    return {"decayed": decayed}
            else:
                # MongoDB path - less aggressive decay
                return {"decayed": 0}
        except Exception as e:
            logger.error(f"Decay failed: {e}")
            return {"decayed": 0}

    # ------------------------------------------------------------------
    # memory linking (cross-references)
    # ------------------------------------------------------------------
    def link_memories(self, key_a: str, key_b: str, relation: str = "related") -> Dict[str, Any]:
        """Create a bidirectional link between two memories."""
        if not memory_system.is_available():
            return {"success": False, "message": "Memory storage unavailable"}

        try:
            if memory_system.backend == "sqlite":
                with memory_system._sqlite_lock:
                    # ensure link table exists
                    memory_system.sqlite_conn.execute("""
                        CREATE TABLE IF NOT EXISTS memory_links (
                            key_a TEXT NOT NULL,
                            key_b TEXT NOT NULL,
                            relation TEXT NOT NULL DEFAULT 'related',
                            created_at TEXT NOT NULL,
                            PRIMARY KEY (key_a, key_b, relation)
                        )
                    """)
                    memory_system.sqlite_conn.execute(
                        "INSERT OR IGNORE INTO memory_links (key_a, key_b, relation, created_at) VALUES (?, ?, ?, ?)",
                        (key_a, key_b, relation, _now_iso()),
                    )
                    memory_system.sqlite_conn.execute(
                        "INSERT OR IGNORE INTO memory_links (key_a, key_b, relation, created_at) VALUES (?, ?, ?, ?)",
                        (key_b, key_a, relation, _now_iso()),
                    )
                    memory_system.sqlite_conn.commit()
                    return {"success": True, "key_a": key_a, "key_b": key_b, "relation": relation}
            else:
                # store links as a tags-like field on each memory
                res_a = memory_system.read(key_a)
                res_b = memory_system.read(key_b)
                if not res_a.get("success") or not res_b.get("success"):
                    return {"success": False, "message": "One or both memories not found"}

                link_tag = f"link:{key_b}"
                tags_a = res_a["memory"].get("tags", [])
                if link_tag not in tags_a:
                    tags_a.append(link_tag)
                memory_system.update(key=key_a, tags=tags_a)

                link_tag_a = f"link:{key_a}"
                tags_b = res_b["memory"].get("tags", [])
                if link_tag_a not in tags_b:
                    tags_b.append(link_tag_a)
                memory_system.update(key=key_b, tags=tags_b)

                return {"success": True, "key_a": key_a, "key_b": key_b, "relation": relation}
        except Exception as e:
            logger.error(f"Link failed: {e}")
            return {"success": False, "message": str(e)}

    def get_linked_memories(self, key: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Get memories linked to the given key."""
        if not memory_system.is_available():
            return []

        try:
            linked_keys: List[str] = []
            if memory_system.backend == "sqlite":
                with memory_system._sqlite_lock:
                    rows = memory_system.sqlite_conn.execute(
                        "SELECT key_b FROM memory_links WHERE key_a = ? LIMIT ?",
                        (key, limit),
                    ).fetchall()
                    linked_keys = [r[0] for r in rows]
            else:
                mem = memory_system.read(key)
                if not mem.get("success"):
                    return []
                tags = mem["memory"].get("tags", [])
                linked_keys = [t[5:] for t in tags if t.startswith("link:")][:limit]

            results = []
            for lk in linked_keys:
                res = memory_system.read(lk)
                if res.get("success"):
                    results.append(res["memory"])
            return results
        except Exception as e:
            logger.error(f"Get linked failed: {e}")
            return []

    # ------------------------------------------------------------------
    # entity extraction
    # ------------------------------------------------------------------
    _ENTITY_PATTERNS = {
        "person": re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})\b"),
        "place": re.compile(r"\b(?:in|at|to|from)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})\b", re.IGNORECASE),
        "topic": re.compile(r"\b(?:about|regarding|concerning)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})\b", re.IGNORECASE),
    }

    def extract_entities(self, content: str) -> Dict[str, List[str]]:
        """Extract people, places, and topics from text content."""
        entities: Dict[str, List[str]] = {"person": [], "place": [], "topic": []}
        if not content:
            return entities

        seen: Dict[str, set] = {k: set() for k in entities}
        for entity_type, pattern in self._ENTITY_PATTERNS.items():
            for match in pattern.finditer(content):
                name = match.group(1).strip()
                # filter out common false positives
                if name.lower() in {"the", "a", "an", "this", "that", "i", "you", "we", "they", "he", "she", "it"}:
                    continue
                if len(name) < 3:
                    continue
                if name not in seen[entity_type]:
                    seen[entity_type].add(name)
                    entities[entity_type].append(name)

        return entities

    def tag_memory_with_entities(self, key: str, content: str) -> Dict[str, Any]:
        """Extract entities from content and add them as tags to the memory."""
        if not memory_system.is_available():
            return {"success": False, "message": "Memory storage unavailable"}

        entities = self.extract_entities(content)
        try:
            res = memory_system.read(key)
            if not res.get("success"):
                return {"success": False, "message": "Memory not found"}

            existing_tags = set(res["memory"].get("tags", []))
            new_tags = []
            for etype, names in entities.items():
                for name in names:
                    tag = f"{etype}:{name}"
                    if tag not in existing_tags:
                        existing_tags.add(tag)
                        new_tags.append(tag)

            if new_tags:
                memory_system.update(key=key, tags=list(existing_tags))

            return {"success": True, "key": key, "entities": entities, "new_tags": new_tags}
        except Exception as e:
            logger.error(f"Entity tagging failed: {e}")
            return {"success": False, "message": str(e)}

    # ------------------------------------------------------------------
    # context-aware recall
    # ------------------------------------------------------------------
    def recall_context_aware(
        self,
        query: str,
        limit: int = 15,
        min_importance: float = 15.0,
        include_linked: bool = True,
        boost_recent: bool = True,
    ) -> Dict[str, Any]:
        """Context-aware memory recall.

        Combines multiple signals:
          1. Semantic vector search (if RAG enabled)
          2. Keyword search (fallback / supplement)
          3. Importance scoring (decay-aware)
          4. Recency boost
          5. Memory links (expands around highly relevant results)

        Returns ranked memories with confidence scores.
        """
        if not memory_system.is_available():
            return {"success": False, "message": "Memory storage unavailable", "memories": []}

        candidate_keys: List[str] = []
        seen: Dict[str, float] = {}

        # 1. Vector search (semantic)
        if memory_system.rag_enabled:
            try:
                vec_result = memory_system.vector_search(query=query, limit=limit * 2)
                if vec_result.get("success"):
                    for m in vec_result.get("memories", []):
                        key = m.get("key")
                        if key and key not in seen:
                            # combine vector score with importance
                            vec_score = float(m.get("score", 0)) * 100.0
                            importance = self.compute_importance(m)
                            combined = (vec_score * 0.6) + (importance * 0.4)
                            seen[key] = combined
                            candidate_keys.append(key)
            except Exception as e:
                logger.debug(f"Vector search in recall failed: {e}")

        # 2. Keyword search (supplement)
        try:
            kw_result = memory_system.search(term=query, limit=limit * 2)
            if kw_result.get("success"):
                for m in kw_result.get("memories", []):
                    key = m.get("key")
                    if key and key not in seen:
                        importance = self.compute_importance(m)
                        seen[key] = importance * 0.5  # keyword alone is weaker
                        candidate_keys.append(key)
        except Exception as e:
            logger.debug(f"Keyword search in recall failed: {e}")

        # 3. If nothing found, fall back to recent important memories
        if not candidate_keys:
            try:
                recent = memory_system.get_recent_for_prompt(count=limit)
                for m in recent:
                    key = m.get("key")
                    if key and key not in seen:
                        seen[key] = self.compute_importance(m) * 0.3
                        candidate_keys.append(key)
            except Exception:
                pass

        # 4. Expand with linked memories
        if include_linked and candidate_keys:
            expanded: List[str] = []
            for key in candidate_keys[: max(1, limit // 3)]:
                linked = self.get_linked_memories(key, limit=5)
                for lm in linked:
                    lk = lm.get("key")
                    if lk and lk not in seen:
                        seen[lk] = self.compute_importance(lm) * 0.4
                        expanded.append(lk)
            candidate_keys.extend(expanded)

        # 5. Rank by combined score, apply importance threshold, cap at limit
        ranked: List[Tuple[str, float]] = sorted(seen.items(), key=lambda x: x[1], reverse=True)
        results: List[Dict[str, Any]] = []

        for key, score in ranked:
            if len(results) >= limit:
                break
            if score < min_importance:
                continue

            res = memory_system.read(key)
            if not res.get("success"):
                continue

            mem = res["memory"]
            importance = self.compute_importance(mem)
            confidence = round(min(100.0, (score / 100.0) * 100.0), 2)

            # Recency boost for final ranking
            if boost_recent:
                days = _days_ago(mem.get("created_at"))
                recency_boost = max(0.0, 10.0 * math.exp(-days / RECENCY_BOOST_DAYS))
                confidence = round(min(100.0, confidence + recency_boost), 2)

            results.append({
                **mem,
                "importance": importance,
                "confidence": confidence,
                "relevance_score": round(score, 2),
            })

        # Increment access count for recalled memories
        for r in results:
            try:
                memory_system.read(r["key"])  # bumps access_count
            except Exception:
                pass

        return {
            "success": True,
            "query": query,
            "memories": results,
            "count": len(results),
            "method": "context_aware",
        }

    # ------------------------------------------------------------------
    # auto-summarization
    # ------------------------------------------------------------------
    def summarize_cluster(self, memories: List[Dict[str, Any]], topic: str) -> Dict[str, Any]:
        """Summarize a cluster of related memories into a single long_term memory."""
        if not memories:
            return {"success": False, "message": "No memories to summarize"}

        contents = [f"[{m.get('key', '?')}] {m.get('content', '')}" for m in memories]
        joined = "\n".join(contents)

        # Simple extractive summary: take the most important sentences
        sentences = re.split(r"(?<=[.!?])\s+", joined)
        scored_sentences: List[Tuple[float, str]] = []
        query_terms = set(topic.lower().split())
        for s in sentences:
            s = s.strip()
            if not s or len(s) < 10:
                continue
            score = 0.0
            terms = set(s.lower().split())
            score += len(query_terms & terms) * 2.0  # topic overlap
            score += len(s) / 200.0  # length bonus (detailed sentences)
            scored_sentences.append((score, s))

        scored_sentences.sort(key=lambda x: x[0], reverse=True)
        top = [s for _, s in scored_sentences[:5]]
        summary = " ".join(top) if top else joined[:500]

        summary_key = f"summary_{topic.replace(' ', '_')}_{int(time.time())}"
        res = memory_system.save(
            key=summary_key,
            content=summary,
            category="auto_summary",
            memory_type=MEMORY_TYPE_LONG_TERM,
            tags=["auto_summary", "summarized", topic.lower().replace(" ", "_")],
        )

        # mark source memories as summarized (add tag)
        for m in memories:
            mk = m.get("key")
            if mk:
                try:
                    tags = list(m.get("tags") or [])
                    if "summarized" not in tags:
                        tags.append("summarized")
                        memory_system.update(key=mk, tags=tags)
                except Exception:
                    pass

        return {
            "success": res.get("success", False),
            "key": summary_key,
            "summary": summary,
            "source_count": len(memories),
            "message": res.get("message"),
        }

    def auto_summarize_old_short_term(self, max_age_days: int = 5, batch_size: int = 20) -> Dict[str, Any]:
        """Find old short_term memories, cluster by topic, and summarize into long_term."""
        if not memory_system.is_available():
            return {"success": False, "message": "Memory storage unavailable"}

        try:
            # Get old short_term memories
            cutoff = (datetime.utcnow() - timedelta(days=max_age_days)).isoformat()
            if memory_system.backend == "sqlite":
                with memory_system._sqlite_lock:
                    rows = memory_system.sqlite_conn.execute(
                        "SELECT key, content, category, created_at, tags_json FROM memories "
                        "WHERE memory_type = ? AND created_at < ? AND tags_json NOT LIKE '%\"summarized\"%' "
                        "ORDER BY created_at ASC LIMIT ?",
                        (MEMORY_TYPE_SHORT_TERM, cutoff, batch_size),
                    ).fetchall()
                    memories = []
                    for row in rows:
                        memories.append({
                            "key": row[0],
                            "content": row[1],
                            "category": row[2],
                            "created_at": row[3],
                            "tags": json.loads(row[4]) if row[4] else [],
                        })
            else:
                # MongoDB fallback - just list recent short_term
                res = memory_system.list_memories(memory_type=MEMORY_TYPE_SHORT_TERM, limit=batch_size)
                memories = res.get("memories", []) if res.get("success") else []

            if not memories:
                return {"success": True, "summarized": 0, "message": "No old short_term memories to summarize"}

            # Simple clustering by category
            clusters: Dict[str, List[Dict[str, Any]]] = {}
            for m in memories:
                cat = m.get("category", "general")
                clusters.setdefault(cat, []).append(m)

            results = []
            for cat, cluster_memories in clusters.items():
                if len(cluster_memories) < 2:
                    continue
                topic = f"{cat} memories from last {max_age_days} days"
                summary_res = self.summarize_cluster(cluster_memories, topic)
                results.append(summary_res)

            return {
                "success": True,
                "clusters_processed": len(results),
                "source_memories": len(memories),
                "results": results,
            }
        except Exception as e:
            logger.error(f"Auto-summarize failed: {e}")
            return {"success": False, "message": str(e)}

    # ------------------------------------------------------------------
    # confidence scoring
    # ------------------------------------------------------------------
    def score_confidence(self, memory: Dict[str, Any], query_context: str = "") -> float:
        """Score confidence (0-100) that a memory is relevant to the current context."""
        if not memory:
            return 0.0

        importance = self.compute_importance(memory)
        content = str(memory.get("content", ""))
        access_count = int(memory.get("access_count", 0) or 0)
        pinned = "pinned" in (memory.get("tags") or [])
        days_ago = _days_ago(memory.get("created_at"))

        # Base confidence from importance
        confidence = importance * 0.5

        # Access count boost
        confidence += min(15.0, math.log1p(access_count) * 3.0)

        # Pinned boost
        if pinned:
            confidence += 15.0

        # Query context overlap
        if query_context:
            q_terms = set(query_context.lower().split())
            c_terms = set(content.lower().split())
            if q_terms and c_terms:
                overlap = len(q_terms & c_terms) / len(q_terms)
                confidence += overlap * 20.0

        # Recency (newer = more confident)
        recency = math.exp(-days_ago / 14.0)  # 14 day half-life for confidence
        confidence += recency * 15.0

        return round(min(100.0, max(0.0, confidence)), 2)


# Global singleton
intelligence_system = IntelligenceSystem()
