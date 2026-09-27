"""Ingestion strategies for LongMemEval-S benchmark profiles.

Implements:
- DirectFactsIngestStrategy: Fast, deterministic ingestion of gold evidence facts
  using infer=False, isolating retrieval and relevance gate performance.
- Mem0SessionIngestStrategy: Multi-session conversational ingestion using infer=True,
  measuring end-to-end memory distillation and ingestion loss.
"""

from __future__ import annotations

import abc
import logging
from typing import Any, Dict, Mapping, Sequence

from benchmarks.adapter import BenchmarkAdapter, HippoEngineAdapter
from benchmarks.schemas import CorpusItem

logger = logging.getLogger(__name__)


class LongMemEvalIngestStrategy(abc.ABC):
    """Abstract base strategy for ingesting LongMemEval benchmark data."""

    @property
    @abc.abstractmethod
    def profile_name(self) -> str:
        """Name of the ingest profile ('direct-facts' or 'mem0-session')."""
        pass

    @abc.abstractmethod
    def ingest(
        self,
        adapter: BenchmarkAdapter,
        corpus: Sequence[CorpusItem],
    ) -> Dict[str, Any]:
        """Ingest the corpus items using this strategy.

        Returns:
            Dict containing ingestion statistics (items_ingested, duration_sec, etc.).
        """
        pass


class DirectFactsIngestStrategy(LongMemEvalIngestStrategy):
    """Ingests gold evidence facts directly as atomic memory records (infer=False)."""

    @property
    def profile_name(self) -> str:
        return "direct-facts"

    def ingest(
        self,
        adapter: BenchmarkAdapter,
        corpus: Sequence[CorpusItem],
    ) -> Dict[str, Any]:
        logger.info(f"Ingesting {len(corpus)} gold evidence facts with direct-facts profile...")
        adapter.ingest_corpus(corpus)
        return {
            "profile": self.profile_name,
            "facts_count": len(corpus),
            "status": "success",
        }


class Mem0SessionIngestStrategy(LongMemEvalIngestStrategy):
    """Ingests multi-session chat histories with conversational inference (infer=True)."""

    @property
    def profile_name(self) -> str:
        return "mem0-session"

    def ingest(
        self,
        adapter: BenchmarkAdapter,
        corpus: Sequence[CorpusItem],
    ) -> Dict[str, Any]:
        logger.info(f"Ingesting {len(corpus)} sessions with mem0-session profile...")

        # If adapter is a live HippoEngineAdapter, we can leverage session distillation
        if isinstance(adapter, HippoEngineAdapter):
            ingested_sessions = 0
            extracted_memories = 0
            for item in corpus:
                turns = item.metadata.get("turns")
                session_id = item.metadata.get("session_id") or item.id
                meta = dict(item.metadata)
                meta["benchmark_session_id"] = session_id
                meta["source"] = "session_distillation"

                if turns and isinstance(turns, list):
                    # Ingest multi-turn session with inference
                    try:
                        res = adapter.engine.add(
                            messages=turns,
                            user_id=item.user_id or adapter.config.user_id,
                            agent_id=item.project_id if item.scope == "project" else "global",
                            metadata=meta,
                            infer=True,
                        )
                        results = res.get("results", []) if isinstance(res, Mapping) else res
                        if isinstance(results, list):
                            extracted_memories += len(results)
                            for mem in results:
                                if isinstance(mem, Mapping) and mem.get("id"):
                                    # Map backend memory id back to this session logical id
                                    adapter._backend_to_logical[str(mem["id"])] = item.id
                    except Exception as e:
                        logger.warning(f"Error during mem0-session distillation for session {session_id}: {e}")
                else:
                    # Fallback to direct text ingest if no turn breakdown
                    adapter.ingest_corpus([item])
                ingested_sessions += 1

            return {
                "profile": self.profile_name,
                "sessions_count": ingested_sessions,
                "extracted_memories": extracted_memories,
                "status": "success",
            }
        else:
            # Replay or offline adapter
            adapter.ingest_corpus(corpus)
            return {
                "profile": self.profile_name,
                "sessions_count": len(corpus),
                "status": "success",
            }


def resolve_ingest_strategy(profile: str) -> LongMemEvalIngestStrategy:
    """Resolve ingest strategy by name ('direct-facts' or 'mem0-session')."""
    norm = profile.lower().strip().replace("_", "-")
    if norm in ("direct-facts", "direct", "facts"):
        return DirectFactsIngestStrategy()
    elif norm in ("mem0-session", "session", "sessions"):
        return Mem0SessionIngestStrategy()
    else:
        raise ValueError(
            f"Unknown LongMemEval ingest profile: {profile!r}. Expected 'direct-facts' or 'mem0-session'."
        )
