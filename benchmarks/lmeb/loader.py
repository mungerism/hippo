"""Data loader and schema converter for LMEB (Long-context Memory Embedding Benchmark).

Focuses on the dialogue-memory subset for comparing representation capability
and candidate recall across different embedding providers, models, and dimensions.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Mapping, Optional
import urllib.request

from benchmarks.schemas import (
    BenchmarkDataset,
    CorpusItem,
    EvaluationQuery,
)

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = Path.home() / ".hippo" / "benchmarks" / "data"
BUILTIN_LMEB_FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "lmeb_dialogue_fixture.json"
)

# Official repository URL for LMEB dialogue-memory subset
OFFICIAL_LMEB_DIALOGUE_URL = (
    "https://raw.githubusercontent.com/KaLM-Embedding/LMEB/main/data/dialogue_memory.json"
)


@dataclass(frozen=True, slots=True)
class LmebCorpusItem:
    """A corpus item in LMEB."""

    id: str
    text: str
    category: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> LmebCorpusItem:
        return cls(
            id=str(data["id"]),
            text=str(data["text"]),
            category=data.get("category"),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class LmebQueryItem:
    """A test query item in LMEB."""

    query_id: str
    query: str
    category: str = "general"
    relevant_ids: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> LmebQueryItem:
        return cls(
            query_id=str(data["query_id"]),
            query=str(data["query"]),
            category=str(data.get("category", "general")),
            relevant_ids=[str(i) for i in data.get("relevant_ids", [])],
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class LmebDataset:
    """A loaded LMEB dataset subset."""

    benchmark: str
    subset: str
    version: str
    description: str
    corpus: list[LmebCorpusItem]
    queries: list[LmebQueryItem]

    def to_dict(self) -> dict[str, Any]:
        return {
            "benchmark": self.benchmark,
            "subset": self.subset,
            "version": self.version,
            "description": self.description,
            "corpus": [c.to_dict() for c in self.corpus],
            "queries": [q.to_dict() for q in self.queries],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> LmebDataset:
        return cls(
            benchmark=str(data.get("benchmark", "LMEB")),
            subset=str(data.get("subset", "dialogue-memory")),
            version=str(data.get("version", "1.0.0")),
            description=str(data.get("description", "")),
            corpus=[LmebCorpusItem.from_dict(c) for c in data.get("corpus", [])],
            queries=[LmebQueryItem.from_dict(q) for q in data.get("queries", [])],
        )


def _download_official_lmeb(dest: Path, url: str = OFFICIAL_LMEB_DIALOGUE_URL) -> None:
    """Download LMEB dataset from upstream repository."""
    logger.info(f"Downloading LMEB dataset from {url} to {dest}...")
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp_file = dest.with_suffix(".tmp")
    try:
        urllib.request.urlretrieve(url, temp_file)
        temp_file.replace(dest)
        logger.info(f"Successfully downloaded LMEB dataset to {dest}")
    except Exception as exc:
        if temp_file.exists():
            temp_file.unlink()
        raise RuntimeError(f"Failed to download LMEB dataset from {url}: {exc}") from exc


def load_lmeb_dataset(
    source_path_or_url: str | Path | None = None,
    expected_hash: Optional[str] = None,
    cache_dir: Optional[Path | str] = None,
) -> LmebDataset:
    """Load and parse LMEB dataset from file, cache, or official URL."""
    target_file: Path
    if source_path_or_url:
        p = Path(source_path_or_url).expanduser()
        if p.exists() and p.is_file():
            target_file = p
        elif str(source_path_or_url).startswith(("http://", "https://")):
            cdir = Path(cache_dir or DEFAULT_CACHE_DIR).resolve()
            cdir.mkdir(parents=True, exist_ok=True)
            dest = cdir / "lmeb_dialogue.json"
            if not dest.exists():
                _download_official_lmeb(dest, str(source_path_or_url))
            target_file = dest
        else:
            raise FileNotFoundError(f"Specified LMEB source does not exist: {source_path_or_url}")
    else:
        # Check cache or fallback to fixture
        cdir = Path(cache_dir or DEFAULT_CACHE_DIR).resolve()
        cached = cdir / "lmeb_dialogue.json"
        if cached.exists():
            target_file = cached
        elif BUILTIN_LMEB_FIXTURE_PATH.is_file():
            logger.info(f"Using built-in LMEB fixture from {BUILTIN_LMEB_FIXTURE_PATH}")
            target_file = BUILTIN_LMEB_FIXTURE_PATH
        else:
            raise FileNotFoundError("LMEB dataset not found and built-in fixture missing.")

    raw_data = target_file.read_bytes()
    if expected_hash:
        file_hash = hashlib.sha256(raw_data).hexdigest()
        if file_hash != expected_hash:
            raise ValueError(
                f"Dataset hash mismatch for {target_file}! Expected {expected_hash}, got {file_hash}"
            )

    parsed = json.loads(raw_data.decode("utf-8"))
    if not isinstance(parsed, Mapping):
        raise TypeError(f"Invalid LMEB JSON format; expected dictionary root, got {type(parsed)}")

    dataset = LmebDataset.from_dict(parsed)
    logger.info(
        f"Loaded LMEB dataset ({dataset.subset}): {len(dataset.corpus)} corpus items, "
        f"{len(dataset.queries)} queries from {target_file}"
    )
    return dataset


def convert_to_lmeb_benchmark(
    lmeb_data: LmebDataset,
    dataset_name: Optional[str] = None,
) -> BenchmarkDataset:
    """Convert LMEB dataset into standard Hippo BenchmarkDataset representation."""
    corpus: list[CorpusItem] = []
    queries: list[EvaluationQuery] = []
    qrels: dict[str, dict[str, int]] = {}

    name = dataset_name or f"lmeb-{lmeb_data.subset}"

    for c in lmeb_data.corpus:
        item_meta = dict(c.metadata)
        item_meta["benchmark"] = "LMEB"
        item_meta["subset"] = lmeb_data.subset

        corpus.append(
            CorpusItem(
                id=c.id,
                text=c.text,
                scope="project",
                project_id=f"eval_lmeb_{lmeb_data.subset}",
                user_id="eval_user_lmeb",
                status="active",
                category=c.category,
                metadata=item_meta,
            )
        )

    for q in lmeb_data.queries:
        queries.append(
            EvaluationQuery(
                query_id=q.query_id,
                query=q.query,
                scope="project",
                project_id=f"eval_lmeb_{lmeb_data.subset}",
                user_id="eval_user_lmeb",
                expected_empty=len(q.relevant_ids) == 0,
                category=q.category,
                evidence_ids=q.relevant_ids,
                metadata={
                    "benchmark": "LMEB",
                    "subset": lmeb_data.subset,
                    **q.metadata,
                },
            )
        )

        if q.relevant_ids:
            qrels[q.query_id] = {rid: 1 for rid in q.relevant_ids}
        else:
            qrels[q.query_id] = {}

    return BenchmarkDataset(
        name=name,
        version=lmeb_data.version,
        description=lmeb_data.description or f"LMEB {lmeb_data.subset} embedding benchmark",
        corpus=corpus,
        queries=queries,
        qrels=qrels,
        forbidden={},
    )
