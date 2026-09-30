"""BEAM dataset loading and schema adaptation.

Offline CI uses the bundled Hippo fixture. Named official runs load the pinned
Mohammadta/BEAM Hugging Face 100K split used by Mem0's public benchmark runner.
The historical beam-128k CLI alias is retained for compatibility, but the
upstream dataset currently names this scale bucket 100K.
"""

from __future__ import annotations

import ast
from dataclasses import asdict, dataclass, field
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional
import urllib.request

from benchmarks.schemas import BenchmarkDataset, CorpusItem, EvaluationQuery

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = Path.home() / ".hippo" / "benchmarks" / "data"
BUILTIN_BEAM_FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "beam_128k_fixture.json"
)

OFFICIAL_BEAM_DATASET_ID = "Mohammadta/BEAM"
OFFICIAL_BEAM_DATASET_REVISION = "8b4ddc477010c07a852752fe2f27a2722755ff2b"
OFFICIAL_BEAM_SPLIT = "100K"
OFFICIAL_BEAM_PROTOCOL_REVISION = "4b61c5d31b9c668a12b4f5e78064248a02c82d2b"


@dataclass(frozen=True, slots=True)
class BeamMemory:
    """A single memory entry within a BEAM conversation."""

    id: str
    text: str
    timestamp: Optional[str] = None
    category: Optional[str] = None
    status: str = "active"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "BeamMemory":
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
    def from_dict(cls, data: Mapping[str, Any]) -> "BeamQuery":
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
    def from_dict(cls, data: Mapping[str, Any]) -> "BeamDataset":
        return cls(
            benchmark=str(data.get("benchmark", "BEAM")),
            profile=str(data.get("profile", "128k")),
            version=str(data.get("version", "1.0.0")),
            description=str(data.get("description", "")),
            memories=[BeamMemory.from_dict(m) for m in data.get("memories", [])],
            queries=[BeamQuery.from_dict(q) for q in data.get("queries", [])],
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
        raise RuntimeError(f"Failed to download BEAM JSON from {url}: {exc}") from exc


def load_beam_dataset(
    source_path_or_url: str | Path | None = None,
    expected_hash: Optional[str] = None,
    cache_dir: Optional[Path | str] = None,
) -> BeamDataset:
    """Load Hippo's normalized BEAM JSON format.

    With no source this intentionally loads the bundled smoke fixture. Official
    named runs call load_official_beam_dataset and never fall back to this fixture.
    """
    if source_path_or_url is None:
        target_file = BUILTIN_BEAM_FIXTURE_PATH
    else:
        source = str(source_path_or_url)
        p = Path(source_path_or_url).expanduser()
        if p.exists() and p.is_file():
            target_file = p
        elif source.startswith(("http://", "https://")):
            cdir = Path(cache_dir or DEFAULT_CACHE_DIR).resolve()
            target_file = cdir / "beam_normalized.json"
            if not target_file.exists():
                _download_json(target_file, source)
        else:
            raise FileNotFoundError(f"Specified BEAM source does not exist: {source_path_or_url}")

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
        "Loaded BEAM dataset (%s): %d memories, %d queries from %s",
        dataset.profile,
        len(dataset.memories),
        len(dataset.queries),
        target_file,
    )
    return dataset


def _iter_turns(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, list):
        for item in value:
            yield from _iter_turns(item)
    elif isinstance(value, Mapping):
        if "content" in value and ("role" in value or "id" in value or "index" in value):
            yield value
        elif "turns" in value:
            yield from _iter_turns(value.get("turns"))
        else:
            for nested in value.values():
                if isinstance(nested, (list, Mapping)):
                    yield from _iter_turns(nested)


def _parse_probing_questions(raw: Any) -> Mapping[str, Any]:
    if isinstance(raw, Mapping):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return parsed if isinstance(parsed, Mapping) else {}


def _rubric_text(question: Mapping[str, Any]) -> Optional[str]:
    rubric = question.get("rubric")
    if isinstance(rubric, Mapping):
        rubric = rubric.get("nuggets", [])
    if not isinstance(rubric, list):
        return str(rubric) if rubric else None
    values: list[str] = []
    for item in rubric:
        if isinstance(item, Mapping):
            value = item.get("description") or item.get("text")
            if value:
                values.append(str(value))
        elif item:
            values.append(str(item))
    return " | ".join(values) if values else None


def _convert_official_rows(rows: Iterable[Mapping[str, Any]]) -> BeamDataset:
    memories: list[BeamMemory] = []
    queries: list[BeamQuery] = []

    for conv_idx, row in enumerate(rows):
        conversation_id = str(row.get("conversation_id", f"conv_{conv_idx}"))
        seed = row.get("conversation_seed") if isinstance(row.get("conversation_seed"), Mapping) else {}
        default_category = str(seed.get("category", "general"))
        source_lookup: dict[str, str] = {}

        for turn_idx, turn in enumerate(_iter_turns(row.get("chat", []))):
            content = str(turn.get("content", "")).strip()
            if not content:
                continue
            raw_id = turn.get("index")
            if raw_id is None:
                raw_id = turn.get("id", turn_idx)
            memory_id = f"beam_{conversation_id}_{raw_id}"
            for candidate in (turn.get("id"), turn.get("index"), raw_id):
                if candidate is not None:
                    source_lookup[str(candidate)] = memory_id
            role = str(turn.get("role", "unknown"))
            memories.append(
                BeamMemory(
                    id=memory_id,
                    text=f"{role}: {content}",
                    timestamp=turn.get("time_anchor"),
                    category=str(turn.get("question_type") or default_category),
                    metadata={
                        "conversation_id": conversation_id,
                        "source_turn_id": raw_id,
                        "official_split": OFFICIAL_BEAM_SPLIT,
                    },
                )
            )

        probing = _parse_probing_questions(row.get("probing_questions"))
        q_counter = 0
        for question_type, group in probing.items():
            items = group if isinstance(group, list) else [group]
            for question in items:
                if isinstance(question, str):
                    question = {"question_text": question}
                if not isinstance(question, Mapping):
                    continue
                question_text = str(
                    question.get("question_text") or question.get("question") or ""
                ).strip()
                if not question_text:
                    continue
                source_ids = question.get("source_chat_ids") or []
                if not isinstance(source_ids, list):
                    source_ids = [source_ids]
                evidence_ids = [
                    source_lookup[str(source_id)]
                    for source_id in source_ids
                    if str(source_id) in source_lookup
                ]
                query_id = f"beam_{conversation_id}_q{q_counter}_{question_type}"
                q_counter += 1
                queries.append(
                    BeamQuery(
                        query_id=query_id,
                        query=question_text,
                        category=str(question_type),
                        expected_empty=bool(
                            question.get(
                                "expected_empty",
                                str(question_type).lower() == "abstention" and not evidence_ids,
                            )
                        ),
                        evidence_ids=evidence_ids,
                        reference_answer=_rubric_text(question),
                        metadata={
                            "conversation_id": conversation_id,
                            "difficulty": question.get("difficulty"),
                            "source_chat_ids": [str(v) for v in source_ids],
                            "official_split": OFFICIAL_BEAM_SPLIT,
                        },
                    )
                )

    if not memories or not queries:
        raise ValueError(
            "Pinned official BEAM split produced no memories or queries; upstream schema may have changed."
        )

    return BeamDataset(
        benchmark="BEAM",
        profile="100k",
        version=f"hf:{OFFICIAL_BEAM_DATASET_ID}@{OFFICIAL_BEAM_DATASET_REVISION}",
        description=(
            "Pinned official BEAM 100K split normalized for Hippo retrieval/scale profiling. "
            "This is a retrieval proxy over BEAM source turns; the official BEAM answer/judge "
            "score remains a separate end-to-end metric."
        ),
        memories=memories,
        queries=queries,
    )


def load_official_beam_dataset(
    cache_dir: Optional[Path | str] = None,
) -> BeamDataset:
    """Load the pinned official BEAM 100K split from Hugging Face."""
    try:
        from datasets import load_dataset as hf_load_dataset
    except ImportError as exc:
        raise RuntimeError(
            "Official BEAM requires the optional Hugging Face datasets package. "
            "Run with: uv run --with datasets python -m benchmarks.runner "
            "--dataset beam-100k --adapter engine"
        ) from exc

    try:
        dataset = hf_load_dataset(
            OFFICIAL_BEAM_DATASET_ID,
            split=OFFICIAL_BEAM_SPLIT,
            revision=OFFICIAL_BEAM_DATASET_REVISION,
            cache_dir=str(Path(cache_dir or DEFAULT_CACHE_DIR).expanduser()),
        )
    except Exception as exc:
        raise RuntimeError(
            "Failed to load pinned official BEAM dataset "
            f"{OFFICIAL_BEAM_DATASET_ID}@{OFFICIAL_BEAM_DATASET_REVISION} "
            f"split={OFFICIAL_BEAM_SPLIT}: {exc}"
        ) from exc

    return _convert_official_rows(dataset)


def convert_to_beam_benchmark(
    beam_data: BeamDataset,
    dataset_name: Optional[str] = None,
) -> BenchmarkDataset:
    """Convert BEAM data into Hippo's standard benchmark representation."""
    corpus: list[CorpusItem] = []
    queries: list[EvaluationQuery] = []
    qrels: dict[str, dict[str, int]] = {}
    forbidden: dict[str, list[str]] = {}

    name = dataset_name or f"beam-{beam_data.profile}"
    superseded_ids = [m.id for m in beam_data.memories if m.status == "superseded"]

    for memory in beam_data.memories:
        metadata = dict(memory.metadata)
        metadata["benchmark"] = "BEAM"
        metadata["profile"] = beam_data.profile
        if memory.timestamp:
            metadata["timestamp"] = memory.timestamp
        corpus.append(
            CorpusItem(
                id=memory.id,
                text=memory.text,
                scope="project",
                project_id=f"eval_beam_{beam_data.profile}",
                user_id="eval_user_beam",
                status=memory.status,
                category=memory.category,
                metadata=metadata,
            )
        )

    for query in beam_data.queries:
        queries.append(
            EvaluationQuery(
                query_id=query.query_id,
                query=query.query,
                scope="project",
                project_id=f"eval_beam_{beam_data.profile}",
                user_id="eval_user_beam",
                expected_empty=query.expected_empty,
                category=query.category,
                reference_answer=query.reference_answer,
                evidence_ids=query.evidence_ids,
                metadata={
                    "benchmark": "BEAM",
                    "profile": beam_data.profile,
                    **query.metadata,
                },
            )
        )
        qrels[query.query_id] = (
            {evidence_id: 1 for evidence_id in query.evidence_ids}
            if not query.expected_empty
            else {}
        )
        if superseded_ids:
            forbidden[query.query_id] = list(superseded_ids)

    return BenchmarkDataset(
        name=name,
        version=beam_data.version,
        description=beam_data.description or f"BEAM {beam_data.profile} scale benchmark",
        corpus=corpus,
        queries=queries,
        qrels=qrels,
        forbidden=forbidden,
    )
