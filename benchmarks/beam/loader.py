"""Data loading and schema adaptation for BEAM (Benchmark for Evaluating Agent Memory).

Supports the 128K profile, local caching, SHA256 integrity checks,
and adaptation into standard Hippo BenchmarkDataset objects.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence
import urllib.request

from benchmarks.schemas import (
    BenchmarkDataset,
    CorpusItem,
    EvaluationQuery,
)

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = Path.home() / ".hippo" / "benchmarks" / "data"
BUILTIN_BEAM_FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "beam_128k_fixture.json"
)

# Official upstream repository URL for BEAM-128K
OFFICIAL_BEAM_128K_URL = (
    "https://raw.githubusercontent.com/mem0ai/BEAM/main/data/beam_128k.json"
)


@dataclass(frozen=True, slots=True)
class BeamMemory:
    """A single memory entry within the BEAM haystack context."""

    id: str
    text: str
    timestamp: Optional[str] = None
    category: Optional[str] = None
    status: str = "active"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> BeamMemory:
        return cls(
            id=str(data["id"]),
            text=str(data["text"]),
            timestamp=data.get("timestamp"),
            category=data.get("category"),
            status=str(data.get("status", "active")),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class BeamQuery:
    """An evaluation query within BEAM."""

    query_id: str
    query: str
    category: str = "general"
    expected_empty: bool = False
    evidence_ids: list[str] = field(default_factory=list)
    reference_answer: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> BeamQuery:
        return cls(
            query_id=str(data["query_id"]),
            query=str(data["query"]),
            category=str(data.get("category", "general")),
            expected_empty=bool(data.get("expected_empty", False)),
            evidence_ids=[str(i) for i in data.get("evidence_ids", [])],
            reference_answer=data.get("reference_answer"),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class BeamDataset:
    """A loaded BEAM dataset profile."""

    benchmark: str
    profile: str
    version: str
    description: str
    memories: list[BeamMemory]
    queries: list[BeamQuery]

    def to_dict(self) -> dict[str, Any]:
        return {
            "benchmark": self.benchmark,
            "profile": self.profile,
            "version": self.version,
            "description": self.description,
            "memories": [m.to_dict() for m in self.memories],
            "queries": [q.to_dict() for q in self.queries],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> BeamDataset:
        return cls(
            benchmark=str(data.get("benchmark", "BEAM")),
            profile=str(data.get("profile", "128k")),
            version=str(data.get("version", "1.0.0")),
            description=str(data.get("description", "")),
            memories=[BeamMemory.from_dict(m) for m in data.get("memories", [])],
            queries=[BeamQuery.from_dict(q) for q in data.get("queries", [])],
        )


def _download_official_beam(dest: Path, url: str = OFFICIAL_BEAM_128K_URL) -> None:
    """Download BEAM dataset from upstream repository."""
    logger.info(f"Downloading BEAM dataset from {url} to {dest}...")
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp_file = dest.with_suffix(".tmp")
    try:
        urllib.request.urlretrieve(url, temp_file)
        temp_file.replace(dest)
        logger.info(f"Successfully downloaded BEAM dataset to {dest}")
    except Exception as exc:
        if temp_file.exists():
            temp_file.unlink()
        raise RuntimeError(f"Failed to download BEAM dataset from {url}: {exc}") from exc


def load_beam_dataset(
    source_path_or_url: str | Path | None = None,
    expected_hash: Optional[str] = None,
    cache_dir: Optional[Path | str] = None,
) -> BeamDataset:
    """Load and parse BEAM dataset from local path, cache, or official remote URL.

    Falls back to built-in smoke fixture when no source is specified and local cache is absent.
    """
    target_file: Path
    if source_path_or_url:
        p = Path(source_path_or_url).expanduser()
        if p.exists() and p.is_file():
            target_file = p
        elif str(source_path_or_url).startswith(("http://", "https://")):
            cdir = Path(cache_dir or DEFAULT_CACHE_DIR).resolve()
            cdir.mkdir(parents=True, exist_ok=True)
            dest = cdir / "beam_128k.json"
            if not dest.exists():
                _download_official_beam(dest, str(source_path_or_url))
            target_file = dest
        else:
            raise FileNotFoundError(f"Specified BEAM source does not exist: {source_path_or_url}")
    else:
        # Check cache or fallback to fixture
        cdir = Path(cache_dir or DEFAULT_CACHE_DIR).resolve()
        cached = cdir / "beam_128k.json"
        if cached.exists():
            target_file = cached
        elif BUILTIN_BEAM_FIXTURE_PATH.is_file():
            logger.info(f"Using built-in BEAM fixture from {BUILTIN_BEAM_FIXTURE_PATH}")
            target_file = BUILTIN_BEAM_FIXTURE_PATH
        else:
            raise FileNotFoundError("BEAM dataset not found and built-in fixture missing.")

    raw_data = target_file.read_bytes()
    if expected_hash:
        file_hash = hashlib.sha256(raw_data).hexdigest()
        if file_hash != expected_hash:
            raise ValueError(
                f"Dataset hash mismatch for {target_file}! Expected {expected_hash}, got {file_hash}"
            )

    parsed = json.loads(raw_data.decode("utf-8"))
    if not isinstance(parsed, Mapping):
        raise TypeError(f"Invalid BEAM JSON format; expected dictionary root, got {type(parsed)}")

    dataset = BeamDataset.from_dict(parsed)
    logger.info(
        f"Loaded BEAM dataset ({dataset.profile}): {len(dataset.memories)} memories, "
        f"{len(dataset.queries)} queries from {target_file}"
    )
    return dataset


def convert_to_beam_benchmark(
    beam_data: BeamDataset,
    dataset_name: Optional[str] = None,
) -> BenchmarkDataset:
    """Convert BEAM dataset into standard Hippo BenchmarkDataset representation."""
    corpus: list[CorpusItem] = []
    queries: list[EvaluationQuery] = []
    qrels: dict[str, dict[str, int]] = {}
    forbidden: dict[str, list[str]] = {}

    name = dataset_name or f"beam-{beam_data.profile}"

    # Collect superseded IDs for forbidden leakage tracking
    superseded_ids = [m.id for m in beam_data.memories if m.status == "superseded"]

    # Ingest memories into CorpusItem
    for m in beam_data.memories:
        item_meta = dict(m.metadata)
        item_meta["benchmark"] = "BEAM"
        item_meta["profile"] = beam_data.profile
        if m.timestamp:
            item_meta["timestamp"] = m.timestamp

        corpus.append(
            CorpusItem(
                id=m.id,
                text=m.text,
                scope="project",
                project_id=f"eval_beam_{beam_data.profile}",
                user_id="eval_user_beam",
                status=m.status,
                category=m.category,
                metadata=item_meta,
            )
        )

    # Ingest queries into EvaluationQuery and Qrels
    for q in beam_data.queries:
        queries.append(
            EvaluationQuery(
                query_id=q.query_id,
                query=q.query,
                scope="project",
                project_id=f"eval_beam_{beam_data.profile}",
                user_id="eval_user_beam",
                expected_empty=q.expected_empty,
                category=q.category,
                reference_answer=q.reference_answer,
                evidence_ids=q.evidence_ids,
                metadata={
                    "benchmark": "BEAM",
                    "profile": beam_data.profile,
                    **q.metadata,
                },
            )
        )

        if not q.expected_empty and q.evidence_ids:
            qrels[q.query_id] = {eid: 1 for eid in q.evidence_ids}
        else:
            qrels[q.query_id] = {}

        if superseded_ids:
            forbidden[q.query_id] = list(superseded_ids)

    return BenchmarkDataset(
        name=name,
        version=beam_data.version,
        description=beam_data.description or f"BEAM {beam_data.profile} scale benchmark",
        corpus=corpus,
        queries=queries,
        qrels=qrels,
        forbidden=forbidden,
    )
