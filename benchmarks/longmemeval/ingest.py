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

        # If adapter is a live HippoEngineAdapter, leverage the real infer=True
        # distillation path while preserving logical session IDs for evaluation.
        if isinstance(adapter, HippoEngineAdapter):
            ingested_sessions = 0
            extracted_memories = 0
            failures = []

            for item in corpus:
                turns = item.metadata.get("turns")
                session_id = item.metadata.get("session_id") or item.id
                meta = dict(item.metadata)
                meta["benchmark_session_id"] = session_id
                meta["benchmark_id"] = item.id
                meta["source"] = "session_distillation"

                # search() uses this logical corpus map when normalizing traces.
                adapter._corpus_by_logical[item.id] = item

                try:
                    if turns and isinstance(turns, list):
                        messages = [
                            {
                                "role": str(turn.get("role", "user")),
                                "content": str(turn.get("content", "")),
                            }
                            for turn in turns
                            if isinstance(turn, Mapping)
                        ]
                        res = adapter.engine.add(
                            messages=messages,
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
                                    adapter._backend_to_logical[str(mem["id"])] = item.id
                    else:
                        adapter.ingest_corpus([item])
                    ingested_sessions += 1
                except Exception as exc:
                    failures.append(
                        {
                            "session_id": str(session_id),
                            "logical_id": item.id,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    logger.exception(
                        "LongMemEval mem0-session ingestion failed for session %s",
                        session_id,
                    )

            if failures:
                sample = "; ".join(
                    f"{failure['session_id']}: {failure['error']}"
                    for failure in failures[:3]
                )
                raise RuntimeError(
                    f"LongMemEval mem0-session ingestion failed for "
                    f"{len(failures)}/{len(corpus)} sessions. "
                    f"First failures: {sample}"
                )

            return {
                "profile": self.profile_name,
                "sessions_count": ingested_sessions,
                "extracted_memories": extracted_memories,
                "failed_sessions": 0,
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
