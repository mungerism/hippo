"""LMEB dataset loading and schema adaptation.

Offline CI uses the bundled Hippo fixture. Named official runs load a pinned
Dialogue/MemBench retrieval split from the KaLM-Embedding/LMEB Hugging Face
dataset and never fall back to fixture data.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Mapping, Optional
import urllib.request

from benchmarks.schemas import BenchmarkDataset, CorpusItem, EvaluationQuery

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = Path.home() / ".hippo" / "benchmarks" / "data"
BUILTIN_LMEB_FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "lmeb_dialogue_fixture.json"
)

OFFICIAL_LMEB_DATASET_ID = "KaLM-Embedding/LMEB"
# The official dataset tree shows the Dialogue/MemBench upload under this
# verified revision. A revision pin is preferable to mutable main.
OFFICIAL_LMEB_DATASET_REVISION = "9671811"
OFFICIAL_LMEB_FAMILY = "MemBench"
OFFICIAL_LMEB_SPLIT = "single_hop"
OFFICIAL_LMEB_QRELS_PATH = "Dialogue/MemBench/single_hop/qrels.tsv"


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
    def from_dict(cls, data: Mapping[str, Any]) -> "LmebCorpusItem":
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
    def from_dict(cls, data: Mapping[str, Any]) -> "LmebQueryItem":
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
    def from_dict(cls, data: Mapping[str, Any]) -> "LmebDataset":
        return cls(
            benchmark=str(data.get("benchmark", "LMEB")),
            subset=str(data.get("subset", "dialogue-memory")),
            version=str(data.get("version", "1.0.0")),
            description=str(data.get("description", "")),
            corpus=[LmebCorpusItem.from_dict(c) for c in data.get("corpus", [])],
            queries=[LmebQueryItem.from_dict(q) for q in data.get("queries", [])],
        )


def _download_json(dest: Path, url: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp_file = dest.with_suffix(dest.suffix + ".tmp")
    try:
        urllib.request.urlretrieve(url, temp_file)
        temp_file.replace(dest)
    except Exception as exc:
        if temp_file.exists():
            temp_file.unlink()
        raise RuntimeError(f"Failed to download LMEB JSON from {url}: {exc}") from exc


def load_lmeb_dataset(
    source_path_or_url: str | Path | None = None,
    expected_hash: Optional[str] = None,
    cache_dir: Optional[Path | str] = None,
) -> LmebDataset:
    """Load Hippo's normalized LMEB JSON format.

    With no source this intentionally loads the bundled smoke fixture. Official
    named runs call load_official_lmeb_dataset and never fall back to the fixture.
    """
    if source_path_or_url is None:
        target_file = BUILTIN_LMEB_FIXTURE_PATH
    else:
        source = str(source_path_or_url)
        p = Path(source_path_or_url).expanduser()
        if p.exists() and p.is_file():
            target_file = p
        elif source.startswith(("http://", "https://")):
            cdir = Path(cache_dir or DEFAULT_CACHE_DIR).resolve()
            target_file = cdir / "lmeb_normalized.json"
            if not target_file.exists():
                _download_json(target_file, source)
        else:
            raise FileNotFoundError(f"Specified LMEB source does not exist: {source_path_or_url}")

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
        "Loaded LMEB dataset (%s): %d corpus items, %d queries from %s",
        dataset.subset,
        len(dataset.corpus),
        len(dataset.queries),
        target_file,
    )
    return dataset


def _first_value(row: Mapping[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        value = row.get(name)
        if value is not None and value != "":
            return value
    return None


def _row_id(row: Mapping[str, Any], *, kind: str) -> str:
    value = _first_value(row, ("_id", "id", "query_id", "docid", "document_id"))
    if value is None:
        raise ValueError(f"Official LMEB {kind} row has no supported id field: {sorted(row.keys())}")
    return str(value)


def _row_text(row: Mapping[str, Any], *, kind: str) -> str:
    value = _first_value(row, ("text", "query", "content", "question"))
    if value is None:
        raise ValueError(f"Official LMEB {kind} row has no supported text field: {sorted(row.keys())}")
    title = row.get("title")
    text = str(value)
    return f"{title}\n{text}" if title else text


def _load_qrels(path: Path) -> dict[str, list[str]]:
    qrels: dict[str, list[str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.strip().split()
        if len(parts) < 3:
            continue
        query_id, corpus_id, raw_score = parts[0], parts[1], parts[2]
        try:
            score = float(raw_score)
        except ValueError:
            continue
        if score <= 0:
            continue
        qrels.setdefault(str(query_id), []).append(str(corpus_id))
    if not qrels:
        raise ValueError(f"Pinned LMEB qrels file contained no positive judgments: {path}")
    return qrels


def load_official_lmeb_dataset(
    cache_dir: Optional[Path | str] = None,
) -> LmebDataset:
    """Load pinned official LMEB Dialogue/MemBench single-hop retrieval data."""
    try:
        from datasets import load_dataset as hf_load_dataset
        from huggingface_hub import hf_hub_download
    except ImportError as exc:
        raise RuntimeError(
            "Official LMEB requires optional Hugging Face benchmark dependencies. "
            "Run with: uv run --with datasets --with huggingface_hub "
            "python -m benchmarks.runner --dataset lmeb-dialogue --adapter engine"
        ) from exc

    cache = str(Path(cache_dir or DEFAULT_CACHE_DIR).expanduser())
    try:
        corpus_rows = hf_load_dataset(
            OFFICIAL_LMEB_DATASET_ID,
            f"{OFFICIAL_LMEB_FAMILY}_corpus",
            split=OFFICIAL_LMEB_SPLIT,
            revision=OFFICIAL_LMEB_DATASET_REVISION,
            cache_dir=cache,
        )
        query_rows = hf_load_dataset(
            OFFICIAL_LMEB_DATASET_ID,
            f"{OFFICIAL_LMEB_FAMILY}_queries",
            split=OFFICIAL_LMEB_SPLIT,
            revision=OFFICIAL_LMEB_DATASET_REVISION,
            cache_dir=cache,
        )
        qrels_path = Path(
            hf_hub_download(
                repo_id=OFFICIAL_LMEB_DATASET_ID,
                filename=OFFICIAL_LMEB_QRELS_PATH,
                repo_type="dataset",
                revision=OFFICIAL_LMEB_DATASET_REVISION,
                cache_dir=cache,
            )
        )
    except Exception as exc:
        raise RuntimeError(
            "Failed to load pinned official LMEB dataset "
            f"{OFFICIAL_LMEB_DATASET_ID}@{OFFICIAL_LMEB_DATASET_REVISION}: {exc}"
        ) from exc

    qrels = _load_qrels(qrels_path)
    corpus: list[LmebCorpusItem] = []
    corpus_ids: set[str] = set()
    for row in corpus_rows:
        corpus_id = _row_id(row, kind="corpus")
        corpus_ids.add(corpus_id)
        metadata = {
            key: value
            for key, value in dict(row).items()
            if key not in {"_id", "id", "text", "title", "content"}
        }
        corpus.append(
            LmebCorpusItem(
                id=corpus_id,
                text=_row_text(row, kind="corpus"),
                category=OFFICIAL_LMEB_SPLIT,
                metadata=metadata,
            )
        )

    queries: list[LmebQueryItem] = []
    for row in query_rows:
        query_id = _row_id(row, kind="query")
        relevant = [doc_id for doc_id in qrels.get(query_id, []) if doc_id in corpus_ids]
        metadata = {
            key: value
            for key, value in dict(row).items()
            if key not in {"_id", "id", "query_id", "text", "query", "content", "question"}
        }
        queries.append(
            LmebQueryItem(
                query_id=query_id,
                query=_row_text(row, kind="query"),
                category=OFFICIAL_LMEB_SPLIT,
                relevant_ids=relevant,
                metadata=metadata,
            )
        )

    if not corpus or not queries:
        raise ValueError(
            "Pinned official LMEB split produced no corpus or queries; upstream schema may have changed."
        )

    return LmebDataset(
        benchmark="LMEB",
        subset=f"dialogue-{OFFICIAL_LMEB_FAMILY.lower()}-{OFFICIAL_LMEB_SPLIT}",
        version=f"hf:{OFFICIAL_LMEB_DATASET_ID}@{OFFICIAL_LMEB_DATASET_REVISION}",
        description=(
            "Pinned official LMEB Dialogue/MemBench single-hop retrieval subset for "
            "embedding component comparison."
        ),
        corpus=corpus,
        queries=queries,
    )


def convert_to_lmeb_benchmark(
    lmeb_data: LmebDataset,
    dataset_name: Optional[str] = None,
) -> BenchmarkDataset:
    """Convert LMEB data into Hippo's standard benchmark representation."""
    corpus: list[CorpusItem] = []
    queries: list[EvaluationQuery] = []
    qrels: dict[str, dict[str, int]] = {}

    name = dataset_name or f"lmeb-{lmeb_data.subset}"

    for item in lmeb_data.corpus:
        metadata = dict(item.metadata)
        metadata["benchmark"] = "LMEB"
        metadata["subset"] = lmeb_data.subset
        corpus.append(
            CorpusItem(
                id=item.id,
                text=item.text,
                scope="project",
                project_id=f"eval_lmeb_{lmeb_data.subset}",
                user_id="eval_user_lmeb",
                status="active",
                category=item.category,
                metadata=metadata,
            )
        )

    for query in lmeb_data.queries:
        queries.append(
            EvaluationQuery(
                query_id=query.query_id,
                query=query.query,
                scope="project",
                project_id=f"eval_lmeb_{lmeb_data.subset}",
                user_id="eval_user_lmeb",
                expected_empty=len(query.relevant_ids) == 0,
                category=query.category,
                evidence_ids=query.relevant_ids,
                metadata={
                    "benchmark": "LMEB",
                    "subset": lmeb_data.subset,
                    **query.metadata,
                },
            )
        )
        qrels[query.query_id] = {rid: 1 for rid in query.relevant_ids}

    return BenchmarkDataset(
        name=name,
        version=lmeb_data.version,
        description=lmeb_data.description or f"LMEB {lmeb_data.subset} embedding benchmark",
        corpus=corpus,
        queries=queries,
        qrels=qrels,
        forbidden={},
    )
