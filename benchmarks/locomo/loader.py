"""Data loading and schema adaptation for LoCoMo-10 benchmark.

Provides models, loaders, and converters to transform Snap Research's LoCoMo-10
into Hippo standardized BenchmarkDataset schemas supporting:
- 5 official categories: single-hop, multi-hop, temporal, open-domain, adversarial.
- Evidence mapping from dialog turns to CorpusItems and graded Qrels.
- Deterministic hashing, offline caching, and built-in smoke fixtures.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from benchmarks.schemas import (
    BenchmarkDataset,
    CorpusItem,
    EvaluationQuery,
)

logger = logging.getLogger(__name__)

# Official LoCoMo category mappings
LOCOMO_CATEGORY_MAP: dict[int, str] = {
    1: "single_hop",
    2: "multi_hop",
    3: "temporal",
    4: "open_domain",
    5: "adversarial",
}

LOCOMO_CATEGORY_NAME_MAP: dict[str, str] = {
    "1": "single_hop",
    "2": "multi_hop",
    "3": "temporal",
    "4": "open_domain",
    "5": "adversarial",
    "single_hop": "single_hop",
    "single-hop": "single_hop",
    "multi_hop": "multi_hop",
    "multi-hop": "multi_hop",
    "temporal": "temporal",
    "open_domain": "open_domain",
    "open-domain": "open_domain",
    "adversarial": "adversarial",
}

# Cache directory for downloaded LoCoMo files
DEFAULT_CACHE_DIR = Path.home() / ".hippo" / "benchmarks" / "data"

# Official remote URL for locomo10.json
OFFICIAL_DATA_URLS = [
    "https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json",
]


@dataclass(frozen=True, slots=True)
class LoCoMoTurn:
    """A single dialogue turn within a conversational session."""

    speaker: str
    dia_id: str
    text: str
    img_url: list[str] | None = None
    blip_caption: str | None = None
    query: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> LoCoMoTurn:
        raw_img = data.get("img_url")
        img_list = [str(u) for u in raw_img] if isinstance(raw_img, list) else None
        return cls(
            speaker=str(data.get("speaker", "")),
            dia_id=str(data.get("dia_id", "")),
            text=str(data.get("text", "")),
            img_url=img_list,
            blip_caption=data.get("blip_caption"),
            query=data.get("query"),
        )


@dataclass(frozen=True, slots=True)
class LoCoMoSession:
    """A multi-turn session within a conversation with timestamp metadata."""

    session_id: str
    date_time: str | None = None
    turns: list[LoCoMoTurn] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "date_time": self.date_time,
            "turns": [t.to_dict() for t in self.turns],
        }

    @classmethod
    def from_dict(cls, session_id: str, date_time: str | None, raw_turns: Sequence[Mapping[str, Any]]) -> LoCoMoSession:
        return cls(
            session_id=session_id,
            date_time=date_time,
            turns=[LoCoMoTurn.from_dict(t) for t in raw_turns],
        )


@dataclass(frozen=True, slots=True)
class LoCoMoQAItem:
    """An annotated question-answering evaluation pair in LoCoMo-10."""

    qa_id: str
    question: str
    answer: str | None
    adversarial_answer: str | None
    evidence: list[str]
    category: int
    category_name: str
    sample_id: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, qa_id: str, sample_id: str, data: Mapping[str, Any]) -> LoCoMoQAItem:
        raw_cat = data.get("category", 1)
        cat_int = int(raw_cat) if str(raw_cat).isdigit() else 1
        cat_name = LOCOMO_CATEGORY_MAP.get(cat_int, LOCOMO_CATEGORY_NAME_MAP.get(str(raw_cat).lower(), "single_hop"))
        raw_ev = data.get("evidence", [])
        evidence_list = [str(e) for e in raw_ev] if isinstance(raw_ev, list) else [str(raw_ev)]

        return cls(
            qa_id=qa_id,
            question=str(data.get("question", "")),
            answer=str(data["answer"]) if "answer" in data and data["answer"] is not None else None,
            adversarial_answer=str(data["adversarial_answer"]) if "adversarial_answer" in data and data["adversarial_answer"] is not None else None,
            evidence=evidence_list,
            category=cat_int,
            category_name=cat_name,
            sample_id=sample_id,
        )


@dataclass(frozen=True, slots=True)
class LoCoMoSample:
    """A full conversation sample containing sessions and QA pairs."""

    sample_id: str
    speaker_a: str | None = None
    speaker_b: str | None = None
    sessions: list[LoCoMoSession] = field(default_factory=list)
    qa_items: list[LoCoMoQAItem] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "speaker_a": self.speaker_a,
            "speaker_b": self.speaker_b,
            "sessions": [s.to_dict() for s in self.sessions],
            "qa_items": [q.to_dict() for q in self.qa_items],
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> LoCoMoSample:
        sample_id = str(data.get("sample_id", ""))
        conv = data.get("conversation", {})
        speaker_a = conv.get("speaker_a")
        speaker_b = conv.get("speaker_b")

        # Parse sessions dynamically from conversation dict
        sessions: list[LoCoMoSession] = []
        if isinstance(conv, Mapping):
            # Sort session keys like session_1, session_2, ...
            session_indices = []
            for k in conv:
                m = re.match(r"^session_(\d+)$", k)
                if m:
                    session_indices.append(int(m.group(1)))
            session_indices.sort()

            for idx in session_indices:
                sess_key = f"session_{idx}"
                date_key = f"session_{idx}_date_time"
                sess_turns = conv.get(sess_key, [])
                date_val = conv.get(date_key)
                if isinstance(sess_turns, list):
                    sessions.append(
                        LoCoMoSession.from_dict(
                            session_id=sess_key,
                            date_time=date_val,
                            raw_turns=sess_turns,
                        )
                    )

        # Parse QA items
        raw_qa = data.get("qa", [])
        qa_items: list[LoCoMoQAItem] = []
        for idx, q_data in enumerate(raw_qa):
            qa_id = f"{sample_id}_qa_{idx+1}"
            qa_items.append(LoCoMoQAItem.from_dict(qa_id=qa_id, sample_id=sample_id, data=q_data))

        meta = {
            "event_summary": data.get("event_summary"),
            "observation": data.get("observation"),
            "session_summary": data.get("session_summary"),
        }

        return cls(
            sample_id=sample_id,
            speaker_a=speaker_a,
            speaker_b=speaker_b,
            sessions=sessions,
            qa_items=qa_items,
            metadata=meta,
        )


def load_locomo_samples(
    source_path_or_url: str | Path | None = None,
    cache_dir: Path | None = None,
    expected_hash: str | None = None,
) -> list[LoCoMoSample]:
    """Load and parse LoCoMo-10 samples from file, cache, or official URL."""
    cache = cache_dir or DEFAULT_CACHE_DIR
    cache.mkdir(parents=True, exist_ok=True)

    target_file: Path | None = None

    if source_path_or_url:
        p = Path(source_path_or_url).expanduser()
        if p.exists() and p.is_file():
            target_file = p
        elif str(source_path_or_url).startswith("http://") or str(source_path_or_url).startswith("https://"):
            dest = cache / "locomo10.json"
            if not dest.exists():
                logger.info(f"Downloading LoCoMo-10 from {source_path_or_url} to {dest}...")
                urllib.request.urlretrieve(str(source_path_or_url), dest)
            target_file = dest
        else:
            raise FileNotFoundError(f"Specified LoCoMo source does not exist: {source_path_or_url}")
    else:
        # Check cache or fallback to built-in light fixture
        dest = cache / "locomo10.json"
        if dest.exists():
            target_file = dest
        else:
            fixture_path = Path(__file__).parent.parent / "data" / "locomo10_fixture.json"
            if fixture_path.exists():
                logger.info(f"Using built-in LoCoMo fixture from {fixture_path}")
                target_file = fixture_path
            else:
                raise FileNotFoundError(
                    f"No LoCoMo dataset found at {dest} and fixture missing at {fixture_path}."
                )

    raw_data = target_file.read_text(encoding="utf-8")

    if expected_hash:
        file_hash = hashlib.sha256(raw_data.encode("utf-8")).hexdigest()
        if file_hash != expected_hash:
            raise ValueError(
                f"Dataset hash mismatch for {target_file}! Expected {expected_hash}, got {file_hash}"
            )

    parsed = json.loads(raw_data)
    if not isinstance(parsed, list):
        raise TypeError(f"Invalid LoCoMo JSON format; expected list of conversation samples, got {type(parsed)}")

    samples = [LoCoMoSample.from_dict(s) for s in parsed]
    logger.info(f"Successfully loaded {len(samples)} LoCoMo samples from {target_file}")
    return samples


def convert_to_locomo_benchmark(
    samples: Sequence[LoCoMoSample],
    name: str = "locomo10",
    version: str = "1.0.0",
) -> BenchmarkDataset:
    """Convert LoCoMo samples into a standardized BenchmarkDataset.

    - Every turn in each conversation session becomes a CorpusItem with ID '{sample_id}_{dia_id}'.
    - Every QA pair becomes an EvaluationQuery with category mapped to official names.
    - Category 5 (Adversarial / No-Answer) is treated as expected_empty=True with empty Qrels.
    - Categories 1~4 are mapped to positive Qrels with evidence turns graded 1.
    """
    corpus_items: list[CorpusItem] = []
    queries: list[EvaluationQuery] = []
    qrels: dict[str, dict[str, int]] = {}
    forbidden: dict[str, list[str]] = {}

    for sample in samples:
        s_id = sample.sample_id
        proj_id = f"eval_locomo_{s_id}"
        user_id = f"eval_user_{s_id}"

        # 1. Ingest turns into corpus
        for session in sample.sessions:
            for turn in session.turns:
                cid = f"{s_id}_{turn.dia_id}"
                # Format text with timestamp context if available
                if session.date_time:
                    turn_text = f"[{session.date_time}] {turn.speaker}: {turn.text}"
                else:
                    turn_text = f"{turn.speaker}: {turn.text}"

                corpus_items.append(
                    CorpusItem(
                        id=cid,
                        text=turn_text,
                        scope="project",
                        project_id=proj_id,
                        user_id=user_id,
                        status="active",
                        category=session.session_id,
                        metadata={
                            "benchmark": "locomo",
                            "sample_id": s_id,
                            "session_id": session.session_id,
                            "session_date_time": session.date_time,
                            "dia_id": turn.dia_id,
                            "speaker": turn.speaker,
                            "img_url": turn.img_url,
                            "blip_caption": turn.blip_caption,
                        },
                    )
                )

        # 2. Build queries and qrels
        for qa in sample.qa_items:
            qid = qa.qa_id
            is_adversarial = qa.category == 5
            evidence_cids = [f"{s_id}_{dia_id}" for dia_id in qa.evidence]

            if is_adversarial:
                # Adversarial question has no valid memory answer in the dialogue
                qrels[qid] = {}
                ref_ans = "Unknown / Not mentioned"
            else:
                qrels[qid] = {cid: 1 for cid in evidence_cids}
                ref_ans = qa.answer or ""

            queries.append(
                EvaluationQuery(
                    query_id=qid,
                    query=qa.question,
                    scope="project",
                    project_id=proj_id,
                    user_id=user_id,
                    expected_empty=is_adversarial,
                    category=qa.category_name,
                    reference_answer=ref_ans,
                    evidence_ids=evidence_cids,
                    metadata={
                        "benchmark": "locomo",
                        "sample_id": s_id,
                        "category_id": qa.category,
                        "category_name": qa.category_name,
                        "evidence_dia_ids": qa.evidence,
                        "adversarial_answer": qa.adversarial_answer,
                    },
                )
            )

    return BenchmarkDataset(
        name=name,
        version=version,
        description=f"LoCoMo-10 Benchmark ({len(queries)} queries across {len(samples)} long-term dialogues, {len(corpus_items)} turns)",
        corpus=corpus_items,
        queries=queries,
        qrels=qrels,
        forbidden=forbidden,
    )
