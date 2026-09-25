"""Data schemas and transfer objects for Hippo retrieval evaluations.

Defines corpus items, evaluation queries, graded relevance judgements (qrels),
evaluation traces across pipeline stages, run manifests, and benchmark reports.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
from typing import Any, Dict, List, Mapping, Optional, Sequence


@dataclass(frozen=True, slots=True)
class CorpusItem:
    """A memory item in the evaluation corpus."""

    id: str
    text: str
    scope: str = "project"  # 'project' or 'global'
    project_id: Optional[str] = None
    user_id: Optional[str] = None
    status: str = "active"  # 'active' or 'superseded'
    category: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CorpusItem:
        return cls(
            id=str(data["id"]),
            text=str(data["text"]),
            scope=str(data.get("scope", "project")),
            project_id=data.get("project_id"),
            user_id=data.get("user_id"),
            status=str(data.get("status", "active")),
            category=data.get("category"),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class EvaluationQuery:
    """A test query in the evaluation dataset."""

    query_id: str
    query: str
    scope: str = "all"  # 'all', 'project', or 'global'
    project_id: Optional[str] = None
    user_id: Optional[str] = None
    expected_empty: bool = False
    category: str = "general"
    query_time: Optional[str] = None
    reference_answer: Optional[str] = None
    evidence_ids: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> EvaluationQuery:
        return cls(
            query_id=str(data["query_id"]),
            query=str(data["query"]),
            scope=str(data.get("scope", "all")),
            project_id=data.get("project_id"),
            user_id=data.get("user_id"),
            expected_empty=bool(data.get("expected_empty", False)),
            category=str(data.get("category", "general")),
            query_time=data.get("query_time"),
            reference_answer=data.get("reference_answer"),
            evidence_ids=[str(i) for i in data.get("evidence_ids", [])],
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class BenchmarkDataset:
    """A standardized benchmark dataset containing corpus, queries, and labels."""

    name: str
    version: str
    corpus: List[CorpusItem]
    queries: List[EvaluationQuery]
    # query_id -> {corpus_id: grade} (grade >= 1 is relevant, 0 is irrelevant)
    qrels: Dict[str, Dict[str, int]]
    # query_id -> list of forbidden memory IDs (must NOT be recalled)
    forbidden: Dict[str, List[str]] = field(default_factory=dict)
    description: str = ""

    def compute_hash(self) -> str:
        """Calculate a deterministic SHA-256 hash representing dataset content."""
        hasher = hashlib.sha256()
        # Hash the complete corpus/query contract, including category and metadata.
        # These fields can affect replay scoring and temporal evaluation semantics.
        sorted_corpus = sorted(self.corpus, key=lambda x: x.id)
        for c in sorted_corpus:
            payload = json.dumps(c.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            hasher.update(f"c:{payload}".encode("utf-8"))

        sorted_queries = sorted(self.queries, key=lambda x: x.query_id)
        for q in sorted_queries:
            payload = json.dumps(q.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            hasher.update(f"q:{payload}".encode("utf-8"))

        # Sort qrels
        for q_id in sorted(self.qrels.keys()):
            for c_id in sorted(self.qrels[q_id].keys()):
                grade = self.qrels[q_id][c_id]
                hasher.update(f"r:{q_id}:{c_id}:{grade}".encode("utf-8"))

        # Sort forbidden
        for q_id in sorted(self.forbidden.keys()):
            for c_id in sorted(self.forbidden[q_id]):
                hasher.update(f"f:{q_id}:{c_id}".encode("utf-8"))

        return hasher.hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "corpus": [c.to_dict() for c in self.corpus],
            "queries": [q.to_dict() for q in self.queries],
            "qrels": self.qrels,
            "forbidden": self.forbidden,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> BenchmarkDataset:
        return cls(
            name=str(data["name"]),
            version=str(data.get("version", "1.0.0")),
            description=str(data.get("description", "")),
            corpus=[CorpusItem.from_dict(c) for c in data.get("corpus", [])],
            queries=[EvaluationQuery.from_dict(q) for q in data.get("queries", [])],
            qrels={
                str(qid): {str(cid): int(grade) for cid, grade in grades.items()}
                for qid, grades in data.get("qrels", {}).items()
            },
            forbidden={
                str(qid): [str(cid) for cid in cids]
                for qid, cids in data.get("forbidden", {}).items()
            },
        )

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def from_json(cls, json_str: str) -> BenchmarkDataset:
        return cls.from_dict(json.loads(json_str))


# ----------------------------------------------------------------------
# Evaluation Trace Schemas
# ----------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class CandidateTraceItem:
    """A raw candidate retrieved by the underlying index."""

    id: str
    text: str = ""
    score: float = 0.0
    score_details: Dict[str, Any] = field(default_factory=dict)
    status: str = "active"
    scope: Optional[str] = None
    project_id: Optional[str] = None
    user_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CandidateTraceItem:
        return cls(
            id=str(data["id"]),
            text=str(data.get("text", "")),
            score=float(data.get("score", 0.0)),
            score_details=dict(data.get("score_details") or {}),
            status=str(data.get("status", "active")),
            scope=data.get("scope"),
            project_id=data.get("project_id"),
            user_id=data.get("user_id"),
        )


@dataclass(frozen=True, slots=True)
class LifecycleScopeTrace:
    """Record of candidates evaluated during scope and lifecycle filtering."""

    passed_ids: List[str]
    # list of {"id": id, "reason": reason}
    rejected: List[Dict[str, str]]

    def to_dict(self) -> Dict[str, Any]:
        return {"passed_ids": list(self.passed_ids), "rejected": [dict(r) for r in self.rejected]}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> LifecycleScopeTrace:
        return cls(
            passed_ids=[str(i) for i in data.get("passed_ids", [])],
            rejected=[dict(r) for r in data.get("rejected", [])],
        )


@dataclass(frozen=True, slots=True)
class GateTrace:
    """Record of candidates evaluated by relevance gate."""

    passed_ids: List[str]
    # list of {"id": id, "reason": reason, "final_score": float, "details": dict}
    rejected: List[Dict[str, Any]]
    gate_config: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed_ids": list(self.passed_ids),
            "rejected": [dict(r) for r in self.rejected],
            "gate_config": dict(self.gate_config),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> GateTrace:
        return cls(
            passed_ids=[str(i) for i in data.get("passed_ids", [])],
            rejected=[dict(r) for r in data.get("rejected", [])],
            gate_config=dict(data.get("gate_config") or {}),
        )


@dataclass(frozen=True, slots=True)
class EvaluationTrace:
    """Complete four-stage trace for one evaluation query."""

    query_id: str
    query: str
    candidate_stage: List[CandidateTraceItem]
    lifecycle_scope_stage: LifecycleScopeTrace
    gate_stage: GateTrace
    final_stage_ids: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "query_id": self.query_id,
            "query": self.query,
            "candidate_stage": [c.to_dict() for c in self.candidate_stage],
            "lifecycle_scope_stage": self.lifecycle_scope_stage.to_dict(),
            "gate_stage": self.gate_stage.to_dict(),
            "final_stage_ids": list(self.final_stage_ids),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> EvaluationTrace:
        return cls(
            query_id=str(data["query_id"]),
            query=str(data["query"]),
            candidate_stage=[
                CandidateTraceItem.from_dict(c) for c in data.get("candidate_stage", [])
            ],
            lifecycle_scope_stage=LifecycleScopeTrace.from_dict(
                data.get("lifecycle_scope_stage", {})
            ),
            gate_stage=GateTrace.from_dict(data.get("gate_stage", {})),
            final_stage_ids=[str(i) for i in data.get("final_stage_ids", [])],
        )


# ----------------------------------------------------------------------
# Run Manifest and Benchmark Report Schemas
# ----------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class QueryEvaluationResult:
    """Evaluation output for a single query."""

    query_id: str
    query: str
    category: str
    retrieved_ids: List[str]
    relevant_ids: List[str]
    forbidden_ids: List[str]
    metrics: Dict[str, float]
    expected_empty: bool = False
    scope: str = "all"
    project_id: Optional[str] = None
    user_id: Optional[str] = None
    trace: Optional[EvaluationTrace] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "query_id": self.query_id,
            "query": self.query,
            "category": self.category,
            "retrieved_ids": list(self.retrieved_ids),
            "relevant_ids": list(self.relevant_ids),
            "forbidden_ids": list(self.forbidden_ids),
            "metrics": dict(self.metrics),
            "expected_empty": self.expected_empty,
            "scope": self.scope,
            "project_id": self.project_id,
            "user_id": self.user_id,
            "trace": self.trace.to_dict() if self.trace is not None else None,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> QueryEvaluationResult:
        trace_data = data.get("trace")
        return cls(
            query_id=str(data["query_id"]),
            query=str(data["query"]),
            category=str(data.get("category", "general")),
            retrieved_ids=[str(i) for i in data.get("retrieved_ids", [])],
            relevant_ids=[str(i) for i in data.get("relevant_ids", [])],
            forbidden_ids=[str(i) for i in data.get("forbidden_ids", [])],
            metrics={str(k): float(v) for k, v in data.get("metrics", {}).items()},
            expected_empty=bool(data.get("expected_empty", False)),
            scope=str(data.get("scope", "all")),
            project_id=data.get("project_id"),
            user_id=data.get("user_id"),
            trace=EvaluationTrace.from_dict(trace_data) if trace_data else None,
        )


@dataclass(frozen=True, slots=True)
class RunManifest:
    """Metadata detailing the evaluation environment, configuration, and inputs."""

    run_id: str
    timestamp: str
    git_sha: str
    dataset_name: str
    dataset_hash: str
    mem0_version: str
    hippo_version: str
    embedding_profile: Dict[str, Any]
    gate_thresholds: Dict[str, Any]
    max_injected: int
    k_values: List[int]
    seed: Optional[int]
    duration_seconds: float
    adapter: str = "unknown"
    ingest_profile: str = "direct-facts"
    index_size_bytes: Optional[int] = None
    schema_version: str = "1.1.0"
    host_info: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RunManifest:
        return cls(
            run_id=str(data["run_id"]),
            timestamp=str(data["timestamp"]),
            git_sha=str(data["git_sha"]),
            dataset_name=str(data["dataset_name"]),
            dataset_hash=str(data["dataset_hash"]),
            mem0_version=str(data["mem0_version"]),
            hippo_version=str(data["hippo_version"]),
            embedding_profile=dict(data.get("embedding_profile") or {}),
            gate_thresholds=dict(data.get("gate_thresholds") or {}),
            max_injected=int(data.get("max_injected", 3)),
            k_values=[int(k) for k in data.get("k_values", [1, 3, 5, 10])],
            seed=data.get("seed"),
            duration_seconds=float(data.get("duration_seconds", 0.0)),
            adapter=str(data.get("adapter", "unknown")),
            ingest_profile=str(data.get("ingest_profile", "direct-facts")),
            index_size_bytes=(
                int(data["index_size_bytes"]) if data.get("index_size_bytes") is not None else None
            ),
            schema_version=str(data.get("schema_version", "1.1.0")),
            host_info=dict(data.get("host_info") or {}),
        )


@dataclass(frozen=True, slots=True)
class BenchmarkReport:
    """Complete evaluation report containing run manifest, aggregates, and query details."""

    manifest: RunManifest
    aggregate_metrics: Dict[str, float]
    category_metrics: Dict[str, Dict[str, float]]
    query_results: List[QueryEvaluationResult]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "manifest": self.manifest.to_dict(),
            "aggregate_metrics": dict(self.aggregate_metrics),
            "category_metrics": {k: dict(v) for k, v in self.category_metrics.items()},
            "query_results": [q.to_dict() for q in self.query_results],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> BenchmarkReport:
        return cls(
            manifest=RunManifest.from_dict(data["manifest"]),
            aggregate_metrics={str(k): float(v) for k, v in data.get("aggregate_metrics", {}).items()},
            category_metrics={
                str(cat): {str(mk): float(mv) for mk, mv in mdict.items()}
                for cat, mdict in data.get("category_metrics", {}).items()
            },
            query_results=[
                QueryEvaluationResult.from_dict(q) for q in data.get("query_results", [])
            ],
        )

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def from_json(cls, json_str: str) -> BenchmarkReport:
        return cls.from_dict(json.loads(json_str))
