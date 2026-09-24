"""Benchmark adapters isolating memory retrieval implementations.

Provides:
- BenchmarkAdapter: Abstract base adapter for retrieval evaluation.
- ReplayFixtureAdapter: Offline, deterministic replay adapter with full gate
  and lifecycle trace simulation, requiring zero network or external vector store.
- HippoEngineAdapter: Live engine adapter connecting to an explicitly isolated
  temporary collection, enforcing physical isolation from production memories.
"""

from __future__ import annotations

import abc
import copy
from dataclasses import asdict
import logging
from pathlib import Path
import re
import tempfile
from typing import Any, Dict, List, Mapping, Optional, Sequence
import uuid

from benchmarks.schemas import (
    CandidateTraceItem,
    CorpusItem,
    EvaluationQuery,
    EvaluationTrace,
    GateTrace,
    LifecycleScopeTrace,
)
from hippo_memory.gate import (
    GateDecision,
    SearchGateConfig,
    filter_search_results_with_details,
)
from hippo_memory.lifecycle import (
    STATUS_ACTIVE,
    STATUS_SUPERSEDED,
    is_active_memory,
    status_of,
)

logger = logging.getLogger(__name__)


class BenchmarkAdapter(abc.ABC):
    """Abstract interface for memory retrieval benchmark adapters."""

    @abc.abstractmethod
    def ingest_corpus(self, corpus: Sequence[CorpusItem]) -> None:
        """Ingest evaluation corpus items into the benchmark backend."""
        pass

    @abc.abstractmethod
    def search(
        self,
        query: EvaluationQuery,
        limit: int = 3,
        capture_trace: bool = True,
    ) -> tuple[List[str], Optional[EvaluationTrace]]:
        """Search relevant memories for the evaluation query.

        Args:
            query: The evaluation query object.
            limit: Top-N retrieved items to return.
            capture_trace: Whether to capture the four-stage evaluation trace.

        Returns:
            Tuple of (retrieved_memory_ids, optional_evaluation_trace).
        """
        pass

    @abc.abstractmethod
    def cleanup(self) -> None:
        """Tear down temporary storage, collections, and resources."""
        pass

    @abc.abstractmethod
    def get_embedding_profile(self) -> Dict[str, Any]:
        """Return the effective embedding profile used by this adapter."""
        pass


class ReplayFixtureAdapter(BenchmarkAdapter):
    """Deterministic replay adapter executing gate and lifecycle logic offline.

    Allows reproducible testing of metrics, gate thresholds, and trace diagnostics
    without network connectivity or a running Qdrant daemon.
    """

    def __init__(
        self,
        candidates_by_query: Optional[Mapping[str, Sequence[Mapping[str, Any]]]] = None,
        gate_config: Optional[SearchGateConfig] = None,
        embedding_profile: Optional[Dict[str, Any]] = None,
    ):
        self._corpus_by_id: Dict[str, CorpusItem] = {}
        self._candidates_by_query: Dict[str, List[Dict[str, Any]]] = (
            {k: [dict(item) for item in v] for k, v in candidates_by_query.items()}
            if candidates_by_query is not None
            else {}
        )
        self.gate_config = gate_config or SearchGateConfig()
        self._embedding_profile = embedding_profile or {
            "provider": "replay",
            "model": "fixture",
            "dims": 0,
            "collection": "replay_memory",
        }

    def get_embedding_profile(self) -> Dict[str, Any]:
        """Return embedding profile for replay."""
        return dict(self._embedding_profile)

    def ingest_corpus(self, corpus: Sequence[CorpusItem]) -> None:
        """Store corpus items in memory for fixture replay."""
        for item in corpus:
            self._corpus_by_id[item.id] = item

    def set_query_candidates(
        self, query_id: str, candidates: Sequence[Mapping[str, Any]]
    ) -> None:
        """Manually inject pre-recorded candidates for a specific query."""
        self._candidates_by_query[query_id] = [dict(c) for c in candidates]

    def _generate_synthetic_candidates(
        self, query: EvaluationQuery
    ) -> List[Dict[str, Any]]:
        """Generate deterministic scored candidates from ingested corpus based on token overlap."""
        tokens = set(re.findall(r"\w+", query.query.lower()))
        candidates: List[Dict[str, Any]] = []

        for cid, item in self._corpus_by_id.items():
            item_tokens = set(re.findall(r"\w+", item.text.lower()))
            overlap = len(tokens & item_tokens)
            base_score = 0.2
            if tokens:
                base_score += 0.7 * (overlap / float(len(tokens)))

            semantic_score = round(min(0.99, max(0.05, base_score)), 4)
            bm25_score = round(1.0 if overlap > 0 else 0.0, 4)
            entity_boost = 0.0
            final_score = semantic_score

            candidates.append(
                {
                    "id": cid,
                    "memory": item.text,
                    "score": final_score,
                    "score_details": {
                        "final_score": final_score,
                        "semantic_score": semantic_score,
                        "bm25_score": bm25_score,
                        "entity_boost": entity_boost,
                    },
                    "metadata": {
                        "status": item.status,
                        "scope": item.scope,
                        "project": item.project_id,
                    },
                    "user_id": item.user_id,
                    "agent_id": item.project_id if item.scope == "project" else "global",
                }
            )

        # Sort descending by final_score, breaking ties deterministically by ID
        candidates.sort(key=lambda c: (c["score"], c["id"]), reverse=True)
        return candidates

    def search(
        self,
        query: EvaluationQuery,
        limit: int = 3,
        capture_trace: bool = True,
    ) -> tuple[List[str], Optional[EvaluationTrace]]:
        """Execute search through replay, executing active-only and gate logic."""
        if query.query_id in self._candidates_by_query:
            raw_candidates = self._candidates_by_query[query.query_id]
        else:
            raw_candidates = self._generate_synthetic_candidates(query)

        # Stage 1: Candidate retrieval stage
        candidate_stage: List[CandidateTraceItem] = []
        for item in raw_candidates:
            cid = str(item.get("id"))
            meta = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
            score = float(item.get("score", 0.0))
            details = dict(item.get("score_details") or {})
            c_scope = meta.get("scope") or item.get("scope")
            c_proj = meta.get("project") or item.get("project_id") or item.get("agent_id")
            candidate_stage.append(
                CandidateTraceItem(
                    id=cid,
                    text=str(item.get("memory") or item.get("text") or ""),
                    score=score,
                    score_details=details,
                    status=status_of(item),
                    scope=c_scope,
                    project_id=c_proj,
                    user_id=item.get("user_id"),
                )
            )

        # Stage 2: Scope and lifecycle filtering
        passed_lifecycle: List[Dict[str, Any]] = []
        rejected_lifecycle: List[Dict[str, str]] = []

        uid = query.user_id
        q_scope = query.scope
        q_proj = query.project_id

        for item in raw_candidates:
            cid = str(item.get("id"))
            if not is_active_memory(item):
                rejected_lifecycle.append({"id": cid, "reason": "superseded"})
                continue

            item_uid = item.get("user_id")
            if uid is not None and item_uid is not None and item_uid != uid:
                rejected_lifecycle.append({"id": cid, "reason": "cross_user"})
                continue

            meta = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
            item_proj = meta.get("project") or item.get("project_id") or item.get("agent_id")
            item_sc = meta.get("scope") or item.get("scope")

            if q_scope == "global" and item_sc == "project" and item_proj != "global":
                rejected_lifecycle.append({"id": cid, "reason": "cross_project"})
                continue
            if q_scope == "project" and item_sc == "global":
                rejected_lifecycle.append({"id": cid, "reason": "scope_mismatch"})
                continue
            if q_scope == "project" and q_proj and item_proj and item_proj != q_proj:
                rejected_lifecycle.append({"id": cid, "reason": "cross_project"})
                continue

            passed_lifecycle.append(dict(item))

        lifecycle_trace = LifecycleScopeTrace(
            passed_ids=[str(x.get("id")) for x in passed_lifecycle],
            rejected=rejected_lifecycle,
        )

        # Stage 3: Relevance gate filtering
        accepted, decisions = filter_search_results_with_details(
            passed_lifecycle, config=self.gate_config, limit=limit
        )

        gate_trace = GateTrace(
            passed_ids=[d.memory_id for d in decisions if d.accepted],
            rejected=[
                {
                    "id": d.memory_id,
                    "reason": d.reason,
                    "final_score": d.final_score,
                    "details": d.details or {},
                }
                for d in decisions
                if not d.accepted
            ],
            gate_config=asdict(self.gate_config),
        )

        # Stage 4: Final Top-N IDs
        final_stage_ids = [str(item.get("id")) for item in accepted]

        trace = None
        if capture_trace:
            trace = EvaluationTrace(
                query_id=query.query_id,
                query=query.query,
                candidate_stage=candidate_stage,
                lifecycle_scope_stage=lifecycle_trace,
                gate_stage=gate_trace,
                final_stage_ids=final_stage_ids,
            )

        return final_stage_ids, trace

    def cleanup(self) -> None:
        """Clear in-memory state."""
        self._corpus_by_id.clear()
        self._candidates_by_query.clear()


class HippoEngineAdapter(BenchmarkAdapter):
    """Live HippoEngine adapter strictly isolated from production storage.

    Enforces:
        1. Isolated temporary collection name with 'eval_' prefix.
        2. Forbidden access to production default collection.
        3. Automatic cleanup of temporary collections on teardown.
    """

    def __init__(
        self,
        collection_name: Optional[str] = None,
        config: Optional[Any] = None,
    ):
        from hippo_memory.config import HippoConfig
        from hippo_memory.engine import HippoEngine

        base_cfg = config or HippoConfig.from_env()
        cfg = copy.copy(base_cfg)

        # Generate or validate isolated collection name.
        if collection_name is None:
            isolated_coll = f"eval_bench_{uuid.uuid4().hex[:12]}"
        else:
            isolated_coll = collection_name

        # Resolve the collection that the supplied configuration would really use.
        # HippoConfig does not expose a mutable collection_name field; get_mem0_config()
        # is the source of truth.
        prod_coll = getattr(base_cfg, "collection_name", None)
        if not isinstance(prod_coll, str) or not prod_coll:
            try:
                prod_mem0_cfg = base_cfg.get_mem0_config()
                prod_coll = (
                    prod_mem0_cfg.get("vector_store", {})
                    .get("config", {})
                    .get("collection_name")
                )
            except Exception:
                prod_coll = None

        if isolated_coll == prod_coll or not isolated_coll.startswith("eval_"):
            raise ValueError(
                f"Security violation: evaluation collection {isolated_coll!r} must start with 'eval_' "
                f"and cannot match production collection {prod_coll!r}."
            )

        # Isolate every local persistence surface as well as the vector collection.
        self._temp_dir = tempfile.TemporaryDirectory(prefix="hippo_eval_")
        cfg.storage_dir = Path(self._temp_dir.name)
        cfg.history_db_path = str(cfg.storage_dir / "history.db")

        original_get_mem0_config = cfg.get_mem0_config

        def isolated_get_mem0_config() -> Dict[str, Any]:
            mem0_cfg = copy.deepcopy(original_get_mem0_config())
            vector_cfg = mem0_cfg.setdefault("vector_store", {}).setdefault("config", {})
            vector_cfg["collection_name"] = isolated_coll
            mem0_cfg["history_db_path"] = cfg.history_db_path
            return mem0_cfg

        cfg.get_mem0_config = isolated_get_mem0_config

        self.collection_name = isolated_coll
        self.config = cfg
        self.gate_config = (
            cfg.get_gate_config() if hasattr(cfg, "get_gate_config") else SearchGateConfig()
        )
        self.engine = HippoEngine(config=cfg)

    def get_embedding_profile(self) -> Dict[str, Any]:
        """Extract the effective embedding profile from the actual Mem0 config."""
        mem0_cfg = self.config.get_mem0_config()
        emb_cfg = mem0_cfg.get("embedder", {})
        emb_params = emb_cfg.get("config", {})
        vector_params = mem0_cfg.get("vector_store", {}).get("config", {})
        return {
            "provider": emb_cfg.get("provider", "unknown"),
            "model": emb_params.get("model", "unknown"),
            "dims": int(
                emb_params.get(
                    "embedding_dims",
                    vector_params.get("embedding_model_dims", 0),
                )
                or 0
            ),
            "collection": vector_params.get("collection_name", self.collection_name),
        }

    def ingest_corpus(self, corpus: Sequence[CorpusItem]) -> None:
        """Ingest corpus deterministically into the isolated benchmark collection."""
        for item in corpus:
            meta = dict(item.metadata)
            meta["status"] = item.status
            meta["scope"] = item.scope
            meta["benchmark_id"] = item.id
            if item.category is not None:
                meta["category"] = item.category
            if item.project_id:
                meta["project"] = item.project_id

            # infer=False preserves a one-corpus-item -> one-memory relationship and
            # avoids LLM extraction rewriting/splitting benchmark facts.
            self.engine.add(
                text=item.text,
                user_id=item.user_id or self.config.user_id,
                agent_id=item.project_id if item.scope == "project" else "global",
                metadata=meta,
                infer=False,
            )

    def search(
        self,
        query: EvaluationQuery,
        limit: int = 3,
        capture_trace: bool = True,
    ) -> tuple[List[str], Optional[EvaluationTrace]]:
        """Search using the engine while capturing evaluation trace."""
        accepted, trace = self.engine.search_with_trace(
            query=query.query,
            scope=query.scope,
            project_id=query.project_id,
            user_id=query.user_id,
            limit=limit,
            query_id=query.query_id,
        )
        def logical_id(item: Mapping[str, Any]) -> str:
            meta = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
            return str(meta.get("benchmark_id") or item.get("benchmark_id") or item.get("id"))

        retrieved_ids = [logical_id(item) for item in accepted]
        return retrieved_ids, trace if capture_trace else None

    def cleanup(self) -> None:
        """Delete the isolated collection without lazily opening production-adjacent resources."""
        try:
            mem = getattr(self.engine, "_memory", None)
            vector_store = getattr(mem, "vector_store", None) if mem is not None else None
            client = getattr(vector_store, "client", None)
            if client is not None:
                client.delete_collection(collection_name=self.collection_name)
                logger.info("Deleted isolated evaluation collection: %s", self.collection_name)
        except Exception as e:
            logger.warning("Failed to delete isolated collection %s: %s", self.collection_name, e)
        finally:
            self._temp_dir.cleanup()
