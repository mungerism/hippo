"""Data loading and schema adaptation for LongMemEval-S benchmark.

Provides loaders, models, and converters to transform LongMemEval-S
(ICLR 2025) into Hippo standardized BenchmarkDataset schemas supporting both
'direct-facts' and 'mem0-session' ingest profiles.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence
import urllib.request

from benchmarks.schemas import (
    BenchmarkDataset,
    CorpusItem,
    EvaluationQuery,
)

logger = logging.getLogger(__name__)

# Standard categories defined in LongMemEval
LONGMEMEVAL_CATEGORIES = {
    "information_extraction",
    "multi_session",
    "temporal_reasoning",
    "knowledge_update",
    "abstention",
}

# Default cache path for downloaded benchmark files
DEFAULT_CACHE_DIR = Path.home() / ".hippo" / "benchmarks" / "data"

# Primary and mirror URLs for official LongMemEval-S data
OFFICIAL_DATA_URLS = [
    "https://raw.githubusercontent.com/xiaowu0162/LongMemEval/main/data/longmemeval_s.json",
    "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s.json",
]


@dataclass(frozen=True, slots=True)
class SessionTurn:
    """A single turn inside a conversational session."""

    role: str  # 'user' or 'assistant'
    content: str
    has_answer: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SessionTurn:
        return cls(
            role=str(data.get("role", "user")),
            content=str(data.get("content", "")),
            has_answer=bool(data.get("has_answer", False)),
        )


@dataclass(frozen=True, slots=True)
class HaystackSession:
    """A session inside the multi-session conversational history."""

    session_id: str
    date: Optional[str] = None
    turns: List[SessionTurn] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "date": self.date,
            "turns": [t.to_dict() for t in self.turns],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> HaystackSession:
        raw_turns = data.get("turns") or data.get("messages") or []
        return cls(
            session_id=str(data.get("session_id", "")),
            date=data.get("date") or data.get("session_date"),
            turns=[SessionTurn.from_dict(t) for t in raw_turns],
        )


@dataclass(frozen=True, slots=True)
class LongMemEvalItem:
    """A single test question with full conversational history in LongMemEval-S."""

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
            "haystack_sessions": [s.to_dict() for s in self.haystack_sessions],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> LongMemEvalItem:
        q_type = str(data.get("question_type") or data.get("category") or "information_extraction")
        # Normalize category names to standard 5 categories
        norm_type = q_type.lower().replace("-", "_").replace(" ", "_")
        if norm_type not in LONGMEMEVAL_CATEGORIES:
            for cat in LONGMEMEVAL_CATEGORIES:
                if cat in norm_type or norm_type in cat:
                    norm_type = cat
                    break

        sessions = [
            HaystackSession.from_dict(s)
            for s in data.get("haystack_sessions", [])
        ]
        ans_sessions = [str(sid) for sid in data.get("answer_session_ids", [])]

        # Extract gold evidence if present or reconstruct from turns with has_answer=True
        evidence = data.get("evidence")
        if not evidence and sessions:
            evidence_pieces = []
            for s in sessions:
                if not ans_sessions or s.session_id in ans_sessions:
                    for turn in s.turns:
                        if turn.has_answer:
                            evidence_pieces.append(turn.content.strip())
            if evidence_pieces:
                evidence = "\n".join(evidence_pieces)

        return cls(
            question_id=str(data["question_id"]),
            question=str(data["question"]),
            answer=str(data.get("answer", "")),
            question_type=norm_type,
            haystack_sessions=sessions,
            answer_session_ids=ans_sessions,
            evidence=evidence,
            metadata=dict(data.get("metadata") or {}),
        )


def load_longmemeval_items(
    source_path_or_url: Optional[str | Path] = None,
    cache_dir: Optional[Path] = None,
    expected_hash: Optional[str] = None,
) -> List[LongMemEvalItem]:
    """Load and parse LongMemEval-S items from file, cache, or official remote URL."""
    cache = cache_dir or DEFAULT_CACHE_DIR
    cache.mkdir(parents=True, exist_ok=True)

    target_file: Optional[Path] = None

    if source_path_or_url:
        p = Path(source_path_or_url).expanduser()
        if p.exists() and p.is_file():
            target_file = p
        elif str(source_path_or_url).startswith("http://") or str(source_path_or_url).startswith("https://"):
            # Download URL to cache
            dest = cache / "longmemeval_s.json"
            if not dest.exists():
                logger.info(f"Downloading LongMemEval-S from {source_path_or_url} to {dest}...")
                urllib.request.urlretrieve(str(source_path_or_url), dest)
            target_file = dest
        else:
            raise FileNotFoundError(f"Specified LongMemEval source does not exist: {source_path_or_url}")
    else:
        # Check default cache location or try built-in fixture
        dest = cache / "longmemeval_s.json"
        if dest.exists():
            target_file = dest
        else:
            # Fall back to built-in light fixture
            fixture_path = Path(__file__).parent.parent / "data" / "longmemeval_s_fixture.json"
            if fixture_path.exists():
                logger.info(f"Using built-in LongMemEval fixture from {fixture_path}")
                target_file = fixture_path
            else:
                raise FileNotFoundError(
                    f"No LongMemEval dataset found at {dest} and fixture missing at {fixture_path}."
                )

    raw_data = target_file.read_text(encoding="utf-8")

    if expected_hash:
        file_hash = hashlib.sha256(raw_data.encode("utf-8")).hexdigest()
        if file_hash != expected_hash:
            raise ValueError(
                f"Dataset hash mismatch for {target_file}! Expected {expected_hash}, got {file_hash}"
            )

    parsed = json.loads(raw_data)
    if isinstance(parsed, dict) and "questions" in parsed:
        parsed = parsed["questions"]
    elif isinstance(parsed, dict) and "data" in parsed:
        parsed = parsed["data"]

    if not isinstance(parsed, list):
        raise ValueError(f"Invalid LongMemEval JSON format; expected list of objects, got {type(parsed)}")

    items = [LongMemEvalItem.from_dict(item) for item in parsed]
    logger.info(f"Successfully loaded {len(items)} LongMemEval items from {target_file}")
    return items


def convert_to_direct_facts_benchmark(
    items: Sequence[LongMemEvalItem],
    name: str = "longmemeval-s-direct-facts",
    version: str = "1.0.0",
) -> BenchmarkDataset:
    """Convert LongMemEval items into a BenchmarkDataset with 'direct-facts' profile.

    In this profile:
    - Gold evidence facts are stored as already-distilled CorpusItem facts.
    - Abstention queries have no relevant evidence (expected_empty=True).
    - Tests the pure retrieval, scoring, and gate layer without distillation distortion.
    """
    corpus_items: List[CorpusItem] = []
    queries: List[EvaluationQuery] = []
    qrels: Dict[str, Dict[str, int]] = {}
    forbidden: Dict[str, List[str]] = {}

    for item in items:
        qid = item.question_id
        is_abstention = item.question_type == "abstention"
        ev_text = item.evidence or item.answer if not is_abstention else None

        evidence_ids: List[str] = []
        if ev_text and not is_abstention:
            # Create a dedicated corpus item for the evidence fact
            cid = f"fact_{qid}"
            evidence_ids.append(cid)
            corpus_items.append(
                CorpusItem(
                    id=cid,
                    text=ev_text.strip(),
                    scope="project",
                    project_id=f"eval_proj_{qid}",
                    user_id=f"eval_user_{qid}",
                    status="active",
                    category=item.question_type,
                    metadata={
                        "benchmark": "longmemeval",
                        "question_id": qid,
                        "question_type": item.question_type,
                        "answer_session_ids": item.answer_session_ids,
                    },
                )
            )
            qrels[qid] = {cid: 1}
        else:
            qrels[qid] = {}

        queries.append(
            EvaluationQuery(
                query_id=qid,
                query=item.question,
                scope="project",
                project_id=f"eval_proj_{qid}",
                user_id=f"eval_user_{qid}",
                expected_empty=is_abstention,
                category=item.question_type,
                reference_answer=item.answer,
                evidence_ids=evidence_ids,
                metadata={
                    "benchmark": "longmemeval",
                    "question_type": item.question_type,
                    "answer_session_ids": item.answer_session_ids,
                },
            )
        )

    return BenchmarkDataset(
        name=name,
        version=version,
        description=f"LongMemEval-S direct-facts profile ({len(queries)} queries, {len(corpus_items)} evidence facts)",
        corpus=corpus_items,
        queries=queries,
        qrels=qrels,
        forbidden=forbidden,
    )


def convert_to_sessions_benchmark(
    items: Sequence[LongMemEvalItem],
    name: str = "longmemeval-s-mem0-sessions",
    version: str = "1.0.0",
) -> BenchmarkDataset:
    """Convert LongMemEval items into a BenchmarkDataset with 'mem0-session' profile.

    In this profile:
    - Entire sessions from haystack_sessions are converted into CorpusItem units.
    - Each session contains timestamps and full conversation turns.
    - Allows evaluating full multi-session ingestion, distillation, and temporal reasoning.
    """
    corpus_items: List[CorpusItem] = []
    queries: List[EvaluationQuery] = []
    qrels: Dict[str, Dict[str, int]] = {}
    forbidden: Dict[str, List[str]] = {}

    for item in items:
        qid = item.question_id
        is_abstention = item.question_type == "abstention"

        relevant_session_cids: List[str] = []

        for sess in item.haystack_sessions:
            cid = f"sess_{qid}_{sess.session_id}"
            # Render session turns into readable transcript text
            transcript_lines = []
            if sess.date:
                transcript_lines.append(f"Date: {sess.date}")
            for t in sess.turns:
                transcript_lines.append(f"{t.role.capitalize()}: {t.content}")
            sess_text = "\n".join(transcript_lines)

            is_gold_session = sess.session_id in item.answer_session_ids

            corpus_items.append(
                CorpusItem(
                    id=cid,
                    text=sess_text,
                    scope="project",
                    project_id=f"eval_proj_{qid}",
                    user_id=f"eval_user_{qid}",
                    status="active",
                    category=item.question_type,
                    metadata={
                        "benchmark": "longmemeval",
                        "question_id": qid,
                        "session_id": sess.session_id,
                        "session_date": sess.date,
                        "is_gold": is_gold_session,
                        "turns": [t.to_dict() for t in sess.turns],
                    },
                )
            )

            if is_gold_session and not is_abstention:
                relevant_session_cids.append(cid)

        if relevant_session_cids:
            qrels[qid] = {cid: 1 for cid in relevant_session_cids}
        else:
            qrels[qid] = {}

        queries.append(
            EvaluationQuery(
                query_id=qid,
                query=item.question,
                scope="project",
                project_id=f"eval_proj_{qid}",
                user_id=f"eval_user_{qid}",
                expected_empty=is_abstention,
                category=item.question_type,
                reference_answer=item.answer,
                evidence_ids=relevant_session_cids,
                metadata={
                    "benchmark": "longmemeval",
                    "question_type": item.question_type,
                    "answer_session_ids": item.answer_session_ids,
                },
            )
        )

    return BenchmarkDataset(
        name=name,
        version=version,
        description=f"LongMemEval-S mem0-session profile ({len(queries)} queries, {len(corpus_items)} sessions)",
        corpus=corpus_items,
        queries=queries,
        qrels=qrels,
        forbidden=forbidden,
    )
