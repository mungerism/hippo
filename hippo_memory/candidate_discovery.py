"""Pure-read candidate discovery for Cold Path memory consolidation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from typing import Any, Mapping

from hippo_memory.engine import HippoEngine
from hippo_memory.lifecycle import add_lifecycle_exclusion, is_active_memory


MemoryIdentity = tuple[str, str]


def resolve_scope_identity(
    engine: HippoEngine,
    scope: str,
    project_id: str | None = None,
    user_id: str | None = None,
) -> MemoryIdentity:
    """Resolve the storage identity ``(user_id, agent_id)`` for a scope."""
    uid = user_id or engine.config.user_id
    if scope == "global":
        return uid, "global"
    if scope == "project":
        return uid, engine.router.resolve_project(project_id)
    raise ValueError("scope must be 'project' or 'global'")


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
        filters = add_lifecycle_exclusion(
            {
                "user_id": identity[0],
                "agent_id": identity[1],
                "NOT": [
                    {
                        "expiration_date": {
                            "lt": datetime.now(timezone.utc).date().isoformat()
                        }
                    },
                ],
            }
        )
        scan_probe = self.engine.memory.get_all(
            filters=filters,
            top_k=self.scan_limit + 1,
            show_expired=True,
        )
        probe_result = (
            scan_probe.get("results", [])
            if isinstance(scan_probe, Mapping)
            else scan_probe
        )
        if len(list(probe_result or [])) > self.scan_limit:
            raise CandidateScanLimitExceeded(
                f"memory scan exceeds scan_limit={self.scan_limit} for identity={identity!r}"
            )
        response = self.engine.memory.get_all(
            filters=filters,
            top_k=self.scan_limit + 1,
            show_expired=False,
        )
        raw_result = (
            response.get("results", []) if isinstance(response, Mapping) else response
        )
        raw_memories = list(raw_result or [])
        if len(raw_memories) > self.scan_limit:
            raise CandidateScanLimitExceeded(
                f"active memory scan exceeds scan_limit={self.scan_limit} for identity={identity!r}"
            )
        active = [
            memory
            for memory in raw_memories
            if self._matches_identity(memory, identity) and is_active_memory(memory)
        ]
        normalized_since = self._normalize_datetime(since) if since is not None else None
        seeds = [
            memory
            for memory in active
            if normalized_since is None
            or self._latest_timestamp(memory) >= normalized_since
        ]

        edges_by_pair: dict[tuple[str, str], CandidateEdge] = {}
        for seed in seeds:
            seed_id = str(seed.get("id", ""))
            seed_text = str(seed.get("memory", ""))
            if not seed_id or not seed_text:
                continue
            neighbors = self.engine.search_semantic_neighbors(
                query=seed_text,
                filters=filters,
                top_k=self.top_k + 1,
            )
            accepted_for_seed = 0
            for neighbor in neighbors:
                if not isinstance(neighbor, Mapping):
                    continue
                neighbor_id = str(neighbor.get("id", ""))
                score = neighbor.get("semantic_score")
                if (
                    not neighbor_id
                    or neighbor_id == seed_id
                    or not isinstance(score, (int, float))
                    or isinstance(score, bool)
                    or not math.isfinite(float(score))
                    or float(score) < self.semantic_threshold
                    or not self._matches_identity(neighbor, identity)
                    or not is_active_memory(neighbor)
                ):
                    continue
                edge = CandidateEdge(
                    seed_id=seed_id,
                    neighbor_id=neighbor_id,
                    semantic_score=float(score),
                    identity=identity,
                )
                accepted_for_seed += 1
                pair_key = tuple(sorted((seed_id, neighbor_id)))
                previous = edges_by_pair.get(pair_key)
                if previous is None or self._edge_preference(edge) < self._edge_preference(
                    previous
                ):
                    edges_by_pair[pair_key] = edge
                if accepted_for_seed >= self.top_k:
                    break

        edges = [edges_by_pair[key] for key in sorted(edges_by_pair)]
        return CandidateSet(scanned=len(active), seeds=len(seeds), candidate_pairs=edges)

    def _resolve_identity(
        self,
        scope: str,
        project_id: str | None,
        user_id: str | None,
    ) -> MemoryIdentity:
        return resolve_scope_identity(
            self.engine, scope, project_id=project_id, user_id=user_id
        )

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

    @staticmethod
    def _edge_preference(edge: CandidateEdge) -> tuple[float, str, str]:
        return (-edge.semantic_score, edge.seed_id, edge.neighbor_id)
