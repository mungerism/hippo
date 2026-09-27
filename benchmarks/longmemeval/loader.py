"""Data loading and schema adaptation for LongMemEval-S benchmark.

The official cleaned LongMemEval-S release uses three parallel arrays for each
question's history: haystack_session_ids, haystack_dates, and haystack_sessions.
This module keeps that upstream representation faithful while converting it to
Hippo's benchmark schemas.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import logging
from pathlib import Path
import tempfile
from typing import Any, Dict, List, Mapping, Optional, Sequence
import urllib.request

from benchmarks.schemas import BenchmarkDataset, CorpusItem, EvaluationQuery

logger = logging.getLogger(__name__)

LONGMEMEVAL_CATEGORIES = {
    "information_extraction",
    "multi_session",
    "temporal_reasoning",
    "knowledge_update",
    "abstention",
}

OFFICIAL_QUESTION_TYPE_TO_CATEGORY = {
    "single-session-user": "information_extraction",
    "single-session-assistant": "information_extraction",
    "single-session-preference": "information_extraction",
    "multi-session": "multi_session",
    "temporal-reasoning": "temporal_reasoning",
    "knowledge-update": "knowledge_update",
}

DEFAULT_CACHE_DIR = Path.home() / ".hippo" / "benchmarks" / "data"
OFFICIAL_DATASET_REVISION = "98d7416c24c778c2fee6e6f3006e7a073259d48f"
OFFICIAL_DATASET_FILENAME = "longmemeval_s_cleaned.json"
OFFICIAL_DATASET_SHA256 = "d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442"
OFFICIAL_DATA_URL = (
    "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/"
    f"{OFFICIAL_DATASET_REVISION}/{OFFICIAL_DATASET_FILENAME}"
)
BUILTIN_FIXTURE_PATH = Path(__file__).parent.parent / "data" / "longmemeval_s_fixture.json"


@dataclass(frozen=True, slots=True)
class SessionTurn:
    role: str
    content: str
    has_answer: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SessionTurn":
        return cls(
            role=str(data.get("role", "user")),
            content=str(data.get("content", "")),
            has_answer=bool(data.get("has_answer", False)),
        )


@dataclass(frozen=True, slots=True)
class HaystackSession:
    session_id: str
    date: Optional[str] = None
    turns: List[SessionTurn] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "date": self.date,
            "turns": [turn.to_dict() for turn in self.turns],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "HaystackSession":
        raw_turns = data.get("turns") or data.get("messages") or []
        return cls(
            session_id=str(data.get("session_id", "")),
            date=str(data.get("date") or data.get("session_date") or "") or None,
            turns=[SessionTurn.from_dict(turn) for turn in raw_turns],
        )


@dataclass(frozen=True, slots=True)
class LongMemEvalItem:
    question_id: str
    question: str
    answer: str
    question_type: str
    haystack_sessions: List[HaystackSession]
    answer_session_ids: List[str] = field(default_factory=list)
    evidence: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "question_id": self.question_id,
            "question": self.question,
            "answer": self.answer,
            "question_type": self.question_type,
            "answer_session_ids": self.answer_session_ids,
            "evidence": self.evidence,
            "metadata": self.metadata,
            "haystack_sessions": [session.to_dict() for session in self.haystack_sessions],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "LongMemEvalItem":
        question_id = str(data["question_id"])
        official_type = str(data.get("question_type") or "").strip().lower()

        if question_id.endswith("_abs"):
            category = "abstention"
        elif official_type in OFFICIAL_QUESTION_TYPE_TO_CATEGORY:
            category = OFFICIAL_QUESTION_TYPE_TO_CATEGORY[official_type]
        else:
            # Keep compatibility with Hippo's explicit-category light fixture format,
            # but reject unknown types instead of silently mis-bucketing real data.
            normalized = official_type.replace("-", "_").replace(" ", "_")
            if normalized not in LONGMEMEVAL_CATEGORIES:
                raise ValueError(
                    f"Unsupported LongMemEval question_type {official_type!r} "
                    f"for question {question_id!r}"
                )
            category = normalized

        raw_sessions = data.get("haystack_sessions") or []
        sessions: List[HaystackSession]

        if raw_sessions and isinstance(raw_sessions[0], list):
            # Official schema: session ids, dates, and contents are parallel arrays.
            session_ids = [str(value) for value in data.get("haystack_session_ids", [])]
            dates = list(data.get("haystack_dates", []))
            if len(session_ids) != len(raw_sessions) or len(dates) != len(raw_sessions):
                raise ValueError(
                    f"Malformed LongMemEval item {question_id!r}: "
                    "haystack_session_ids, haystack_dates, and haystack_sessions "
                    "must have identical lengths"
                )
            sessions = [
                HaystackSession(
                    session_id=session_ids[index],
                    date=str(dates[index]) if dates[index] is not None else None,
                    turns=[SessionTurn.from_dict(turn) for turn in raw_session],
                )
                for index, raw_session in enumerate(raw_sessions)
            ]
        else:
            # Compatibility path for explicit object-based local fixtures.
            sessions = [
                HaystackSession.from_dict(session)
                for session in raw_sessions
                if isinstance(session, Mapping)
            ]

        answer_session_ids = [
            str(session_id) for session_id in data.get("answer_session_ids", [])
        ]

        evidence_pieces: List[str] = []
        for session in sessions:
            if answer_session_ids and session.session_id not in answer_session_ids:
                continue
            for turn in session.turns:
                if turn.has_answer and turn.content.strip():
                    evidence_pieces.append(turn.content.strip())

        explicit_evidence = data.get("evidence")
        evidence = (
            "\n".join(evidence_pieces)
            if evidence_pieces
            else str(explicit_evidence).strip()
            if explicit_evidence
            else None
        )

        metadata = dict(data.get("metadata") or {})
        metadata.update(
            {
                "official_question_type": official_type,
                "question_date": data.get("question_date"),
            }
        )

        return cls(
            question_id=question_id,
            question=str(data["question"]),
            answer=str(data.get("answer", "")),
            question_type=category,
            haystack_sessions=sessions,
            answer_session_ids=answer_session_ids,
            evidence=evidence,
            metadata=metadata,
        )


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_and_verify(path: Path, expected_hash: Optional[str]) -> str:
    payload = path.read_bytes()
    if expected_hash:
        actual_hash = _sha256_bytes(payload)
        if actual_hash != expected_hash:
            raise ValueError(
                f"Dataset hash mismatch for {path}: "
                f"expected {expected_hash}, got {actual_hash}"
            )
    return payload.decode("utf-8")


def _download_official_dataset(cache: Path) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    destination = cache / OFFICIAL_DATASET_FILENAME

    if destination.exists():
        try:
            _read_and_verify(destination, OFFICIAL_DATASET_SHA256)
            return destination
        except ValueError:
            logger.warning("Cached LongMemEval-S hash mismatch; downloading pinned copy again")

    logger.info(
        "Downloading pinned LongMemEval-S cleaned dataset revision %s to %s",
        OFFICIAL_DATASET_REVISION,
        destination,
    )
    with tempfile.NamedTemporaryFile(
        prefix="longmemeval_s_", suffix=".json", dir=cache, delete=False
    ) as tmp:
        temp_path = Path(tmp.name)

    try:
        urllib.request.urlretrieve(OFFICIAL_DATA_URL, temp_path)
        _read_and_verify(temp_path, OFFICIAL_DATASET_SHA256)
        temp_path.replace(destination)
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)

    return destination


def load_longmemeval_items(
    source_path_or_url: Optional[str | Path] = None,
    cache_dir: Optional[Path] = None,
    expected_hash: Optional[str] = None,
) -> List[LongMemEvalItem]:
    """Load LongMemEval-S.

    With no source, download/use the pinned official cleaned 500-question release
    and always verify its SHA-256. Local paths and custom URLs are supported for
    fixtures or experiments; pass expected_hash when those should also be pinned.
    """

    cache = cache_dir or DEFAULT_CACHE_DIR
    cache.mkdir(parents=True, exist_ok=True)

    if source_path_or_url is None:
        target_file = _download_official_dataset(cache)
        effective_hash = OFFICIAL_DATASET_SHA256
    else:
        source = str(source_path_or_url)
        path = Path(source).expanduser()
        if path.exists() and path.is_file():
            target_file = path
        elif source.startswith(("http://", "https://")):
            destination = cache / OFFICIAL_DATASET_FILENAME
            urllib.request.urlretrieve(source, destination)
            target_file = destination
        else:
            raise FileNotFoundError(f"Specified LongMemEval source does not exist: {source}")
        effective_hash = expected_hash

    raw_data = _read_and_verify(target_file, effective_hash)
    parsed = json.loads(raw_data)
    if isinstance(parsed, dict) and "questions" in parsed:
        parsed = parsed["questions"]
    elif isinstance(parsed, dict) and "data" in parsed:
        parsed = parsed["data"]

    if not isinstance(parsed, list):
        raise ValueError(
            f"Invalid LongMemEval JSON format; expected a list, got {type(parsed).__name__}"
        )

    items = [LongMemEvalItem.from_dict(item) for item in parsed]
    logger.info("Loaded %d LongMemEval items from %s", len(items), target_file)
    return items


def load_longmemeval_fixture_items() -> List[LongMemEvalItem]:
    """Load the committed zero-network fixture used only by unit tests/CI."""
    return load_longmemeval_items(BUILTIN_FIXTURE_PATH)


def _evidence_by_session(item: LongMemEvalItem) -> Dict[str, str]:
    evidence: Dict[str, str] = {}
    for session in item.haystack_sessions:
        if item.answer_session_ids and session.session_id not in item.answer_session_ids:
            continue
        pieces = [
            turn.content.strip()
            for turn in session.turns
            if turn.has_answer and turn.content.strip()
        ]
        if pieces:
            evidence[session.session_id] = "\n".join(pieces)

    # Compatibility for tiny fixtures that provide only a pre-distilled evidence field.
    if not evidence and item.evidence and item.answer_session_ids:
        if len(item.answer_session_ids) == 1:
            evidence[item.answer_session_ids[0]] = item.evidence.strip()

    return evidence


def convert_to_direct_facts_benchmark(
    items: Sequence[LongMemEvalItem],
    name: str = "longmemeval-s-direct-facts",
    version: str = "cleaned-2025-09",
) -> BenchmarkDataset:
    """Convert gold evidence turns into atomic retrieval facts.

    Multi-session questions retain one fact per evidence session instead of
    collapsing all evidence into one synthetic memory, preserving the benchmark's
    multi-evidence retrieval difficulty.
    """

    corpus_items: List[CorpusItem] = []
    queries: List[EvaluationQuery] = []
    qrels: Dict[str, Dict[str, int]] = {}
    forbidden: Dict[str, List[str]] = {}

    for item in items:
        qid = item.question_id
        is_abstention = item.question_type == "abstention"
        evidence_ids: List[str] = []

        if not is_abstention:
            session_evidence = _evidence_by_session(item)
            if not session_evidence:
                raise ValueError(
                    f"LongMemEval question {qid!r} has no gold evidence turns; "
                    "refusing to substitute the gold answer as retrieval evidence"
                )

            for session_id, evidence_text in session_evidence.items():
                cid = f"fact_{qid}_{session_id}"
                evidence_ids.append(cid)
                corpus_items.append(
                    CorpusItem(
                        id=cid,
                        text=evidence_text,
                        scope="project",
                        project_id=f"eval_proj_{qid}",
                        user_id=f"eval_user_{qid}",
                        status="active",
                        category=item.question_type,
                        metadata={
                            "benchmark": "longmemeval",
                            "question_id": qid,
                            "question_type": item.question_type,
                            "official_question_type": item.metadata.get(
                                "official_question_type"
                            ),
                            "answer_session_id": session_id,
                        },
                    )
                )

        qrels[qid] = {cid: 1 for cid in evidence_ids}
        queries.append(
            EvaluationQuery(
                query_id=qid,
                query=item.question,
                scope="project",
                project_id=f"eval_proj_{qid}",
                user_id=f"eval_user_{qid}",
                expected_empty=is_abstention,
                category=item.question_type,
                query_time=item.metadata.get("question_date"),
                reference_answer=item.answer,
                evidence_ids=evidence_ids,
                metadata={
                    "benchmark": "longmemeval",
                    "question_type": item.question_type,
                    "official_question_type": item.metadata.get(
                        "official_question_type"
                    ),
                    "answer_session_ids": item.answer_session_ids,
                },
            )
        )

    return BenchmarkDataset(
        name=name,
        version=version,
        description=(
            f"LongMemEval-S direct-facts profile "
            f"({len(queries)} queries, {len(corpus_items)} evidence facts)"
        ),
        corpus=corpus_items,
        queries=queries,
        qrels=qrels,
        forbidden=forbidden,
    )


def convert_to_sessions_benchmark(
    items: Sequence[LongMemEvalItem],
    name: str = "longmemeval-s-mem0-sessions",
    version: str = "cleaned-2025-09",
) -> BenchmarkDataset:
    """Convert complete timestamped sessions for end-to-end ingestion."""

    corpus_items: List[CorpusItem] = []
    queries: List[EvaluationQuery] = []
    qrels: Dict[str, Dict[str, int]] = {}
    forbidden: Dict[str, List[str]] = {}

    for item in items:
        qid = item.question_id
        is_abstention = item.question_type == "abstention"
        relevant_session_cids: List[str] = []

        for session in item.haystack_sessions:
            cid = f"sess_{qid}_{session.session_id}"
            transcript_lines: List[str] = []
            if session.date:
                transcript_lines.append(f"Date: {session.date}")
            for turn in session.turns:
                transcript_lines.append(f"{turn.role.capitalize()}: {turn.content}")
            session_text = "\n".join(transcript_lines)

            is_gold_session = session.session_id in item.answer_session_ids
            corpus_items.append(
                CorpusItem(
                    id=cid,
                    text=session_text,
                    scope="project",
                    project_id=f"eval_proj_{qid}",
                    user_id=f"eval_user_{qid}",
                    status="active",
                    category=item.question_type,
                    metadata={
                        "benchmark": "longmemeval",
                        "question_id": qid,
                        "session_id": session.session_id,
                        "session_date": session.date,
                        "is_gold": is_gold_session,
                        "turns": [turn.to_dict() for turn in session.turns],
                    },
                )
            )
            if is_gold_session and not is_abstention:
                relevant_session_cids.append(cid)

        qrels[qid] = {cid: 1 for cid in relevant_session_cids}
        queries.append(
            EvaluationQuery(
                query_id=qid,
                query=item.question,
                scope="project",
                project_id=f"eval_proj_{qid}",
                user_id=f"eval_user_{qid}",
                expected_empty=is_abstention,
                category=item.question_type,
                query_time=item.metadata.get("question_date"),
                reference_answer=item.answer,
                evidence_ids=relevant_session_cids,
                metadata={
                    "benchmark": "longmemeval",
                    "question_type": item.question_type,
                    "official_question_type": item.metadata.get(
                        "official_question_type"
                    ),
                    "answer_session_ids": item.answer_session_ids,
                },
            )
        )

    return BenchmarkDataset(
        name=name,
        version=version,
        description=(
            f"LongMemEval-S mem0-session profile "
            f"({len(queries)} queries, {len(corpus_items)} sessions)"
        ),
        corpus=corpus_items,
        queries=queries,
        qrels=qrels,
        forbidden=forbidden,
    )
