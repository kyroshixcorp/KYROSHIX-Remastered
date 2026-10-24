"""Cognitive intelligence layer: reasoning, decision-making, and auto-maintenance.

Provides:
  - Reasoning chains (think step by step before acting)
  - Decision confidence scoring
  - Memory auto-maintenance (decay, cleanup, summarization scheduling)
  - Context compression for long sessions
  - Self-correction (detect and fix repeated failures)
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from .system import memory_system
from .intelligence import intelligence_system

logger = logging.getLogger(__name__)


class CognitiveSystem:
    """Advanced reasoning and decision-making for the bot.

    Operates on top of the memory + intelligence systems to provide
    step-by-step reasoning, confidence-based decisions, and self-correction.
    """

    def __init__(self):
        self._failure_history: Dict[str, List[float]] = {}  # tool_name -> timestamps
        self._max_failures_per_window = 5
        self._failure_window_seconds = 300
        self._last_maintenance = 0.0
        self._maintenance_interval_seconds = 600  # 10 minutes

    # ------------------------------------------------------------------
    # reasoning chain
    # ------------------------------------------------------------------
    def reason_step_by_step(self, query: str, context: str = "") -> Dict[str, Any]:
        """Perform step-by-step reasoning on a query before acting.

        Returns a structured reasoning trace that the main model can use
        to make better decisions. This is a lightweight local reasoning
        (no extra API call) that structures the thinking.
        """
        steps: List[Dict[str, Any]] = []
        step_no = 0

        # Step 1: Understand the query
        step_no += 1
        steps.append({
            "step": step_no,
            "phase": "understand",
            "content": f"User query: {query[:200]}",
            "confidence": 0.9,
        })

        # Step 2: Analyze context
        step_no += 1
        ctx_summary = context[:300] if context else "No additional context"
        steps.append({
            "step": step_no,
            "phase": "analyze_context",
            "content": f"Context: {ctx_summary}",
            "confidence": 0.8,
        })

        # Step 3: Check memory relevance
        step_no += 1
        try:
            recall = intelligence_system.recall_context_aware(
                query=query, limit=5, min_importance=10.0
            )
            mem_count = recall.get("count", 0)
            mem_method = recall.get("method", "none")
            steps.append({
                "step": step_no,
                "phase": "memory_recall",
                "content": f"Found {mem_count} relevant memories via {mem_method}",
                "confidence": 0.85 if mem_count > 0 else 0.4,
            })
        except Exception as e:
            steps.append({
                "step": step_no,
                "phase": "memory_recall",
                "content": f"Memory recall failed: {e}",
                "confidence": 0.1,
            })

        # Step 4: Formulate plan
        step_no += 1
        plan = self._formulate_plan(query, steps)
        steps.append({
            "step": step_no,
            "phase": "plan",
            "content": plan,
            "confidence": 0.8,
        })

        # Step 5: Confidence check
        step_no += 1
        avg_conf = sum(s.get("confidence", 0) for s in steps) / len(steps)
        steps.append({
            "step": step_no,
            "phase": "confidence",
            "content": f"Overall reasoning confidence: {avg_conf:.2f}",
            "confidence": avg_conf,
        })

        return {
            "success": True,
            "query": query,
            "steps": steps,
            "overall_confidence": round(avg_conf, 2),
            "recommended_action": plan,
        }

    def _formulate_plan(self, query: str, steps: List[Dict[str, Any]]) -> str:
        """Formulate an action plan based on reasoning steps."""
        q_lower = query.lower()
        plan_parts = []

        if any(w in q_lower for w in ["remember", "save", "memória", "lembre"]):
            plan_parts.append("Save key information to long-term memory")
        if any(w in q_lower for w in ["what do you know", "o que você sabe", "recall", "lembrar"]):
            plan_parts.append("Retrieve relevant memories before answering")
        if any(w in q_lower for w in ["walk", "andar", "wander", "explore", "explorar"]):
            plan_parts.append("Consider using wanderer or playMotion for movement")
        if any(w in q_lower for w in ["dance", "dançar", "sit", "sentar", "jump", "pular"]):
            plan_parts.append("Use playMotion for physical actions")
        if any(w in q_lower for w in ["search", "buscar", "web", "internet"]):
            plan_parts.append("Use web search tools if available")

        if not plan_parts:
            plan_parts.append("Answer directly based on context and memories")

        return "; ".join(plan_parts)

    # ------------------------------------------------------------------
    # decision confidence
    # ------------------------------------------------------------------
    def score_decision_confidence(
        self,
        action: str,
        available_tools: List[str],
        context: str = "",
        recent_failures: int = 0,
    ) -> float:
        """Score confidence (0-100) that a decision/action is appropriate."""
        confidence = 50.0  # base

        # Tool availability boost
        if action in available_tools:
            confidence += 25.0
        else:
            confidence -= 20.0

        # Recent failures penalty
        confidence -= recent_failures * 10.0

        # Context richness
        if context and len(context) > 50:
            confidence += 10.0

        # Memory-backed actions get boost
        memory_actions = {"memory", "recall_intelligent", "search", "search_fts"}
        if action in memory_actions:
            confidence += 10.0

        return round(min(100.0, max(0.0, confidence)), 2)

    # ------------------------------------------------------------------
    # self-correction / failure tracking
    # ------------------------------------------------------------------
    def record_failure(self, tool_name: str) -> None:
        """Record a tool failure for self-correction tracking."""
        now = time.time()
        if tool_name not in self._failure_history:
            self._failure_history[tool_name] = []
        self._failure_history[tool_name].append(now)
        # Prune old entries
        cutoff = now - self._failure_window_seconds
        self._failure_history[tool_name] = [
            t for t in self._failure_history[tool_name] if t > cutoff
        ]

    def get_recent_failures(self, tool_name: str) -> int:
        """Get number of recent failures for a tool."""
        now = time.time()
        cutoff = now - self._failure_window_seconds
        if tool_name not in self._failure_history:
            return 0
        return sum(1 for t in self._failure_history[tool_name] if t > cutoff)

    def is_tool_blocked(self, tool_name: str) -> bool:
        """Return True if a tool has failed too many times recently."""
        return self.get_recent_failures(tool_name) >= self._max_failures_per_window

    # ------------------------------------------------------------------
    # auto-maintenance
    # ------------------------------------------------------------------
    def should_run_maintenance(self) -> bool:
        """Return True if it's time to run memory maintenance."""
        return (time.time() - self._last_maintenance) > self._maintenance_interval_seconds

    def run_maintenance(self) -> Dict[str, Any]:
        """Run full memory maintenance: decay, cleanup, summarization."""
        self._last_maintenance = time.time()
        results: Dict[str, Any] = {
            "timestamp": datetime.now().isoformat(),
            "actions": [],
        }

        if not memory_system.is_available():
            results["actions"].append({
                "action": "maintenance",
                "status": "skipped",
                "reason": "memory unavailable",
            })
            return results

        # 1. Apply temporal decay
        try:
            decay_res = intelligence_system.apply_decay()
            results["actions"].append({
                "action": "decay",
                "status": "ok",
                "decayed": decay_res.get("decayed", 0),
            })
        except Exception as e:
            results["actions"].append({
                "action": "decay",
                "status": "error",
                "message": str(e),
            })

        # 2. Cleanup expired memories
        try:
            cleanup_res = memory_system.cleanup_expired()
            results["actions"].append({
                "action": "cleanup",
                "status": "ok",
                "deleted": cleanup_res,
            })
        except Exception as e:
            results["actions"].append({
                "action": "cleanup",
                "status": "error",
                "message": str(e),
            })

        # 3. Auto-summarize old short_term memories
        try:
            summary_res = intelligence_system.auto_summarize_old_short_term(
                max_age_days=5, batch_size=20
            )
            results["actions"].append({
                "action": "summarize",
                "status": "ok" if summary_res.get("success") else "error",
                "clusters": summary_res.get("clusters_processed", 0),
                "source_memories": summary_res.get("source_memories", 0),
            })
        except Exception as e:
            results["actions"].append({
                "action": "summarize",
                "status": "error",
                "message": str(e),
            })

        # 4. Vacuum SQLite
        try:
            memory_system.vacuum_sqlite()
            results["actions"].append({
                "action": "vacuum",
                "status": "ok",
            })
        except Exception as e:
            results["actions"].append({
                "action": "vacuum",
                "status": "error",
                "message": str(e),
            })

        logger.info(f"Memory maintenance completed: {len(results['actions'])} actions")
        return results

    # ------------------------------------------------------------------
    # context compression
    # ------------------------------------------------------------------
    def compress_context(self, memories: List[Dict[str, Any]], max_chars: int = 3000) -> str:
        """Compress a list of memories into a concise context block."""
        if not memories:
            return ""

        # Sort by importance/confidence if available
        sorted_mems = sorted(
            memories,
            key=lambda m: float(m.get("importance", 0) or 0) + float(m.get("confidence", 0) or 0),
            reverse=True,
        )

        lines: List[str] = []
        total = 0
        for m in sorted_mems:
            content = m.get("content", "")
            key = m.get("key", "?")
            cat = m.get("category", "general")
            imp = m.get("importance", 0)
            line = f"[{key}] ({cat}, imp:{imp:.0f}): {content}"
            if total + len(line) > max_chars:
                break
            lines.append(line)
            total += len(line)

        return "\n".join(lines)


# Global singleton
cognitive_system = CognitiveSystem()
