"""Data loading and schema adaptation for the LoCoMo-10 benchmark.

The official dataset path is deliberately fail-closed: Hippo pins both the
upstream Git commit and the exact locomo10.json SHA-256. CI smoke data is loaded
through a separate explicit helper and is never used as an implicit fallback.
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

from benchmarks.schemas import BenchmarkDataset, CorpusItem, EvaluationQuery

logger = logging.getLogger(__name__)

# Official category IDs from snap-research/locomo/task_eval/evaluation.py:
#   1 multi-hop, 2 temporal, 3 open-domain, 4 single-hop, 5 adversarial.
LOCOMO_CATEGORY_MAP: dict[int, str] = {
    1: "multi_hop",
    2: "temporal",
    3: "open_domain",
    4: "single_hop",
    5: "adversarial",
}

LOCOMO_CATEGORY_NAME_MAP: dict[str, str] = {
    "1": "multi_hop",
    "2": "temporal",
    "3": "open_domain",
    "4": "single_hop",
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

DEFAULT_CACHE_DIR = Path.home() / ".hippo" / "benchmarks" / "data"
BUILTIN_FIXTURE_PATH = Path(__file__).parent.parent / "data" / "locomo10_fixture.json"

# Reproducible upstream pin. The SHA-256 is the byte hash of locomo10.json at
# this commit and is independently published by multiple LoCoMo users/audits.
OFFICIAL_DATASET_REVISION = "3eb6f2c585f5e1699204e3c3bdf7adc5c28cb376"
OFFICIAL_DATASET_SHA256 = "79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4"
OFFICIAL_DATA_URL = (
    "https://raw.githubusercontent.com/snap-research/locomo/"
    f"{OFFICIAL_DATASET_REVISION}/data/locomo10.json"
)
OFFICIAL_SAMPLE_COUNT = 10
OFFICIAL_QUESTION_COUNT = 1986


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
    def from_dict(cls, data: Mapping[str, Any]) -> "LoCoMoTurn":
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
    def from_dict(
        cls,
        session_id: str,
        date_time: str | None,
        raw_turns: Sequence[Mapping[str, Any]],
    ) -> "LoCoMoSession":
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
    def from_dict(
        cls,
        qa_id: str,
        sample_id: str,
        data: Mapping[str, Any],
    ) -> "LoCoMoQAItem":
        raw_cat = data.get("category", 4)
        cat_int = int(raw_cat) if str(raw_cat).isdigit() else 4
        cat_name = LOCOMO_CATEGORY_MAP.get(
            cat_int,
            LOCOMO_CATEGORY_NAME_MAP.get(str(raw_cat).lower(), "single_hop"),
        )
        raw_ev = data.get("evidence", [])
        evidence_list = (
            [str(e) for e in raw_ev] if isinstance(raw_ev, list) else [str(raw_ev)]
        )

        return cls(
            qa_id=qa_id,
            question=str(data.get("question", "")),
            answer=(
                str(data["answer"])
                if "answer" in data and data["answer"] is not None
                else None
            ),
            adversarial_answer=(
                str(data["adversarial_answer"])
                if "adversarial_answer" in data
                and data["adversarial_answer"] is not None
                else None
            ),
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
    def from_dict(cls, data: Mapping[str, Any]) -> "LoCoMoSample":
        sample_id = str(data.get("sample_id", ""))
        conv = data.get("conversation", {})
        speaker_a = conv.get("speaker_a") if isinstance(conv, Mapping) else None
        speaker_b = conv.get("speaker_b") if isinstance(conv, Mapping) else None

        sessions: list[LoCoMoSession] = []
        if isinstance(conv, Mapping):
            session_indices: list[int] = []
            for key in conv:
                match = re.match(r"^session_(\d+)$", key)
                if match:
                    session_indices.append(int(match.group(1)))
            session_indices.sort()

            for idx in session_indices:
                sess_key = f"session_{idx}"
                date_key = f"session_{idx}_date_time"
                sess_turns = conv.get(sess_key, [])
                if isinstance(sess_turns, list):
                    sessions.append(
                        LoCoMoSession.from_dict(
                            session_id=sess_key,
                            date_time=conv.get(date_key),
                            raw_turns=sess_turns,
                        )
                    )

        raw_qa = data.get("qa", [])
        qa_items: list[LoCoMoQAItem] = []
        if isinstance(raw_qa, list):
            for idx, q_data in enumerate(raw_qa):
                if not isinstance(q_data, Mapping):
                    continue
                qa_items.append(
                    LoCoMoQAItem.from_dict(
                        qa_id=f"{sample_id}_qa_{idx + 1}",
                        sample_id=sample_id,
                        data=q_data,
                    )
                )

        metadata = {
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
            metadata=metadata,
        )


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download_file(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    try:
        urllib.request.urlretrieve(url, temporary)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def _official_dataset_path(cache_dir: Path | None = None) -> Path:
    cache = cache_dir or DEFAULT_CACHE_DIR
    cache.mkdir(parents=True, exist_ok=True)
    destination = cache / "locomo10.json"

    if destination.exists():
        actual = _sha256_path(destination)
        if actual == OFFICIAL_DATASET_SHA256:
            return destination
        logger.warning(
            "Cached LoCoMo dataset hash %s does not match the pinned official hash; "
            "re-downloading from the pinned revision.",
            actual,
        )

    _download_file(OFFICIAL_DATA_URL, destination)
    actual = _sha256_path(destination)
    if actual != OFFICIAL_DATASET_SHA256:
        destination.unlink(missing_ok=True)
        raise ValueError(
            "Downloaded LoCoMo dataset hash mismatch: "
            f"expected {OFFICIAL_DATASET_SHA256}, got {actual}"
        )
    return destination


def load_locomo_samples(
    source_path_or_url: str | Path | None = None,
    cache_dir: Path | None = None,
    expected_hash: str | None = None,
) -> list[LoCoMoSample]:
    """Load LoCoMo samples.

    With no explicit source this loads the pinned official dataset only. It
    never falls back to the built-in CI fixture.
    """
    is_official = source_path_or_url is None

    if is_official:
        target_file = _official_dataset_path(cache_dir)
        expected_hash = OFFICIAL_DATASET_SHA256
    else:
        source = str(source_path_or_url)
        if source.startswith(("http://", "https://")):
            cache = cache_dir or DEFAULT_CACHE_DIR
            cache.mkdir(parents=True, exist_ok=True)
            target_file = cache / "locomo10.json"
            if not target_file.exists():
                _download_file(source, target_file)
        else:
            target_file = Path(source_path_or_url).expanduser()
            if not target_file.is_file():
                raise FileNotFoundError(
                    f"Specified LoCoMo source does not exist: {source_path_or_url}"
                )

    if expected_hash:
        actual_hash = _sha256_path(target_file)
        if actual_hash != expected_hash:
            raise ValueError(
                f"Dataset hash mismatch for {target_file}! "
                f"Expected {expected_hash}, got {actual_hash}"
            )

    parsed = json.loads(target_file.read_text(encoding="utf-8"))
    if not isinstance(parsed, list):
        raise TypeError(
            "Invalid LoCoMo JSON format; expected list of conversation samples, "
            f"got {type(parsed)}"
        )

    samples = [
        LoCoMoSample.from_dict(sample)
        for sample in parsed
        if isinstance(sample, Mapping)
    ]

    if is_official:
        question_count = sum(len(sample.qa_items) for sample in samples)
        if len(samples) != OFFICIAL_SAMPLE_COUNT or question_count != OFFICIAL_QUESTION_COUNT:
            raise ValueError(
                "Pinned LoCoMo dataset shape mismatch: "
                f"expected {OFFICIAL_SAMPLE_COUNT} samples / "
                f"{OFFICIAL_QUESTION_COUNT} questions, got "
                f"{len(samples)} / {question_count}"
            )

    logger.info("Loaded %s LoCoMo samples from %s", len(samples), target_file)
    return samples


def load_locomo_fixture_samples() -> list[LoCoMoSample]:
    """Load the deterministic zero-network CI fixture explicitly."""
    return load_locomo_samples(BUILTIN_FIXTURE_PATH)


def convert_to_locomo_benchmark(
    samples: Sequence[LoCoMoSample],
    name: str = "locomo10",
    version: str = "1.0.0",
) -> BenchmarkDataset:
    """Convert LoCoMo samples into Hippo's standardized benchmark schema."""
    corpus_items: list[CorpusItem] = []
    queries: list[EvaluationQuery] = []
    qrels: dict[str, dict[str, int]] = {}
    forbidden: dict[str, list[str]] = {}

    for sample in samples:
        sample_id = sample.sample_id
        project_id = f"eval_locomo_{sample_id}"
        user_id = f"eval_user_{sample_id}"

        for session in sample.sessions:
            for turn in session.turns:
                corpus_id = f"{sample_id}_{turn.dia_id}"
                if session.date_time:
                    text = f"[{session.date_time}] {turn.speaker}: {turn.text}"
                else:
                    text = f"{turn.speaker}: {turn.text}"

                corpus_items.append(
                    CorpusItem(
                        id=corpus_id,
                        text=text,
                        scope="project",
                        project_id=project_id,
                        user_id=user_id,
                        status="active",
                        category=session.session_id,
                        metadata={
                            "benchmark": "locomo",
                            "sample_id": sample_id,
                            "session_id": session.session_id,
                            "session_date_time": session.date_time,
                            "dia_id": turn.dia_id,
                            "speaker": turn.speaker,
                            "img_url": turn.img_url,
                            "blip_caption": turn.blip_caption,
                        },
                    )
                )

        for qa in sample.qa_items:
            is_adversarial = qa.category == 5
            evidence_ids = [f"{sample_id}_{dia_id}" for dia_id in qa.evidence]

            if is_adversarial:
                qrels[qa.qa_id] = {}
                reference_answer = "Unknown / Not mentioned"
            else:
                qrels[qa.qa_id] = {corpus_id: 1 for corpus_id in evidence_ids}
                reference_answer = qa.answer or ""

            queries.append(
                EvaluationQuery(
                    query_id=qa.qa_id,
                    query=qa.question,
                    scope="project",
                    project_id=project_id,
                    user_id=user_id,
                    expected_empty=is_adversarial,
                    category=qa.category_name,
                    reference_answer=reference_answer,
                    evidence_ids=evidence_ids,
                    metadata={
                        "benchmark": "locomo",
                        "sample_id": sample_id,
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
        description=(
            f"LoCoMo-10 Benchmark ({len(queries)} queries across "
            f"{len(samples)} long-term dialogues, {len(corpus_items)} turns)"
        ),
        corpus=corpus_items,
        queries=queries,
        qrels=qrels,
        forbidden=forbidden,
    )
