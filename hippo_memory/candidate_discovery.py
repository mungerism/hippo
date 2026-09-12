"""Pure-read candidate discovery for Cold Path memory consolidation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from typing import Any, Mapping, Sequence

from hippo_memory.engine import HippoEngine


MemoryIdentity = tuple[str, str]


class CandidateScanLimitExceeded(RuntimeError):
    """Raised when discovery cannot prove that the seed scan is complete."""


@dataclass(frozen=True, slots=True)
class CandidateEdge:
    """A direct semantic-neighbor relationship requiring classification."""

    seed_id: str
    neighbor_id: str
    semantic_score: float
    identity: MemoryIdentity


@dataclass(frozen=True, slots=True)
class CandidateSet:
    """Summary and direct edges produced by one discovery run."""

    scanned: int
    seeds: int
    candidate_pairs: list[CandidateEdge]


class CandidateDiscovery:
    """Discover direct ANN candidate edges without mutating memories."""

    def __init__(
        self,
        engine: HippoEngine,
        *,
        top_k: int = 10,
        semantic_threshold: float = 0.85,
        scan_limit: int = 10_000,
    ) -> None:
        if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k <= 0:
            raise ValueError("top_k must be a positive integer")
        if not isinstance(scan_limit, int) or isinstance(scan_limit, bool) or scan_limit <= 0:
            raise ValueError("scan_limit must be a positive integer")
        if (
            not isinstance(semantic_threshold, (int, float))
            or isinstance(semantic_threshold, bool)
            or not math.isfinite(semantic_threshold)
            or not 0.0 <= semantic_threshold <= 1.0
        ):
            raise ValueError("semantic_threshold must be a finite number in [0.0, 1.0]")
        self.engine = engine
        self.top_k = top_k
        self.semantic_threshold = semantic_threshold
        self.scan_limit = scan_limit

    def discover(
        self,
        *,
        scope: str = "project",
        project_id: str | None = None,
        since: datetime | None = None,
        user_id: str | None = None,
    ) -> CandidateSet:
        identity = self._resolve_identity(scope, project_id, user_id)
        filters = {
            "user_id": identity[0],
            "agent_id": identity[1],
            "NOT": [{"status": "superseded"}],
        }
        response = self.engine.memory.get_all(filters=filters, top_k=self.scan_limit + 1)
        raw_result = response.get("results", []) if isinstance(response, Mapping) else response
        raw_memories = list(raw_result or [])
        if len(raw_memories) > self.scan_limit:
            raise CandidateScanLimitExceeded(
                f"active memory scan exceeds scan_limit={self.scan_limit} for identity={identity!r}"
            )
        active = [
            memory
            for memory in raw_memories
            if self._matches_identity(memory, identity) and self._is_active(memory)
        ]
        normalized_since = self._normalize_datetime(since) if since is not None else None
        seeds = [
            memory
            for memory in active
            if normalized_since is None
            or self._latest_timestamp(memory) >= normalized_since
        ]

        edges: list[CandidateEdge] = []
        seen_pairs: set[frozenset[str]] = set()
        for seed in seeds:
            seed_id = str(seed.get("id", ""))
            seed_text = str(seed.get("memory", ""))
            if not seed_id or not seed_text:
                continue
            vector = self.engine.memory.embedding_model.embed(seed_text, "search")
            neighbors = self.engine.memory.vector_store.search(
                query=seed_text,
                vectors=vector,
                top_k=self.top_k + 1,
                filters=filters,
            )
            accepted_for_seed = 0
            for neighbor in neighbors:
                neighbor_id = str(getattr(neighbor, "id", ""))
                score = getattr(neighbor, "score", None)
                payload = getattr(neighbor, "payload", {}) or {}
                pair_key = frozenset((seed_id, neighbor_id))
                if (
                    not neighbor_id
                    or neighbor_id == seed_id
                    or pair_key in seen_pairs
                    or not isinstance(score, (int, float))
                    or isinstance(score, bool)
                    or not math.isfinite(float(score))
                    or float(score) < self.semantic_threshold
                    or not self._matches_identity(payload, identity)
                    or not self._is_active(payload)
                ):
                    continue
                seen_pairs.add(pair_key)
                edges.append(
                    CandidateEdge(
                        seed_id=seed_id,
                        neighbor_id=neighbor_id,
                        semantic_score=float(score),
                        identity=identity,
                    )
                )
                accepted_for_seed += 1
                if accepted_for_seed >= self.top_k:
                    break

        return CandidateSet(scanned=len(active), seeds=len(seeds), candidate_pairs=edges)

    def _resolve_identity(
        self,
        scope: str,
        project_id: str | None,
        user_id: str | None,
    ) -> MemoryIdentity:
        uid = user_id or self.engine.config.user_id
        if scope == "global":
            return uid, "global"
        if scope == "project":
            return uid, self.engine.router.resolve_project(project_id)
        raise ValueError("scope must be 'project' or 'global'")

    @staticmethod
    def _metadata(item: Mapping[str, Any]) -> Mapping[str, Any]:
        metadata = item.get("metadata", {})
        return metadata if isinstance(metadata, Mapping) else {}

    @classmethod
    def _matches_identity(cls, item: Mapping[str, Any], identity: MemoryIdentity) -> bool:
        metadata = cls._metadata(item)
        return (
            item.get("user_id", metadata.get("user_id")) == identity[0]
            and item.get("agent_id", metadata.get("agent_id")) == identity[1]
        )

    @classmethod
    def _is_active(cls, item: Mapping[str, Any]) -> bool:
        return cls._metadata(item).get("status", item.get("status", "active")) != "superseded"

    @staticmethod
    def _normalize_datetime(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @classmethod
    def _latest_timestamp(cls, item: Mapping[str, Any]) -> datetime:
        timestamps: list[datetime] = []
        for field in ("created_at", "updated_at"):
            raw = item.get(field, cls._metadata(item).get(field))
            if isinstance(raw, datetime):
                timestamps.append(cls._normalize_datetime(raw))
            elif isinstance(raw, str) and raw.strip():
                try:
                    timestamps.append(
                        cls._normalize_datetime(
                            datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
                        )
                    )
                except ValueError:
                    continue
        return max(timestamps, default=datetime.min.replace(tzinfo=timezone.utc))
