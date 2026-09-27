"""Three-tier evaluator and loss quantification for LongMemEval-S.

Provides:
- BaseReader & BaseJudge abstractions with RuleBased and LLM implementations.
- Three evaluation tiers:
    1. Retrieval Tier (pure recall/MRR/nDCG/empty-accuracy, zero LLM calls).
    2. Oracle Reader Tier (performance ceiling given gold evidence).
    3. End-to-End Tier (full retrieve -> answer -> judge pipeline).
- Loss quantification attributing performance drops into:
    Ingest Loss + Retrieval Loss + Reader Loss = Total Loss.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
import logging
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from benchmarks.adapter import BenchmarkAdapter
from benchmarks.metrics import (
    aggregate_by_category,
    aggregate_metrics,
    evaluate_single_query,
)
from benchmarks.schemas import (
    BenchmarkDataset,
    QueryEvaluationResult,
)

logger = logging.getLogger(__name__)

# Standard Reader prompt template
LONGMEMEVAL_READER_PROMPT = """\
You are an intelligent, helpful AI assistant with access to the user's long-term memory.
Based ONLY on the provided retrieved memories, answer the user's question clearly and accurately.

Retrieved Context:
{context}

Question:
{question}

Instructions:
1. If the provided context explicitly contains the information needed to answer the question, state the answer directly.
2. If the context does NOT contain enough information, or if the question asks about something not mentioned, state clearly: "I don't know based on the provided context" or "The information is unknown / not mentioned". Do NOT guess or hallucinate.
3. Be concise and precise.
"""

# Common phrases indicating correct abstention in answers
ABSTENTION_PHRASES = [
    "don't know",
    "do not know",
    "unknown",
    "not mentioned",
    "not provided",
    "no information",
    "cannot find",
    "unspecified",
    "not stated",
    "未知",
    "不知道",
    "未提及",
    "未说明",
]


class BaseReader(abc.ABC):
    """Abstract interface for LongMemEval question answering."""

    @abc.abstractmethod
    def answer(self, question: str, context: str) -> str:
        """Generate answer for question given retrieved context."""
        pass


class RuleBasedMockReader(BaseReader):
    """Deterministic, offline reader for unit tests and local smoke verification."""

    def __init__(self, answer_map: Optional[Mapping[str, str]] = None):
        self.answer_map = dict(answer_map or {})

    def answer(self, question: str, context: str) -> str:
        q_norm = question.strip().lower()
        if q_norm in self.answer_map:
            return self.answer_map[q_norm]

        if not context or not context.strip():
            return "I don't know based on the provided context."

        # Extract first non-trivial sentence from context
        lines = [line.strip() for line in context.splitlines() if line.strip()]
        for line in lines:
            if not line.startswith(("Date:", "User:", "Assistant:")):
                return line

        return lines[0] if lines else "I don't know based on the provided context."


class BaseJudge(abc.ABC):
    """Abstract interface for evaluating candidate answers against reference answers."""

    @abc.abstractmethod
    def judge(
        self,
        question: str,
        reference_answer: str,
        candidate_answer: str,
        is_abstention: bool = False,
    ) -> Tuple[float, str]:
        """Judge candidate answer correctness.

        Returns:
            Tuple of (score in [0.0, 1.0], explanation_string).
        """
        pass


class RuleBasedJudge(BaseJudge):
    """Deterministic offline judge matching key entities and abstention signals."""

    def judge(
        self,
        question: str,
        reference_answer: str,
        candidate_answer: str,
        is_abstention: bool = False,
    ) -> Tuple[float, str]:
        cand_clean = candidate_answer.strip().lower()
        ref_clean = reference_answer.strip().lower()

        if is_abstention:
            # For abstention queries, correct behavior is stating unknown
            is_abstained = any(p in cand_clean for p in ABSTENTION_PHRASES)
            if is_abstained:
                return 1.0, "Correctly abstained / expressed uncertainty."
            else:
                return 0.0, "Failed to abstain; generated ungrounded answer."

        # Normal queries: check token / phrase overlap with reference answer
        ref_words = set(re.findall(r"\b[a-zA-Z0-9_\u4e00-\u9fa5]+\b", ref_clean))
        ref_content_words = {w for w in ref_words if len(w) > 2 and w not in {"the", "and", "for", "with"}}

        if not ref_content_words:
            matched = ref_clean in cand_clean
            return (1.0, "Matched reference") if matched else (0.0, "Missing reference")

        overlap = sum(1 for w in ref_content_words if w in cand_clean)
        match_ratio = overlap / float(len(ref_content_words))

        if match_ratio >= 0.5 or ref_clean in cand_clean:
            return 1.0, f"Matched {overlap}/{len(ref_content_words)} key concepts from reference."
        else:
            return 0.0, f"Insufficient match: {overlap}/{len(ref_content_words)} concepts matched."


@dataclass
class TierEvaluationResult:
    """Evaluation summary for a specific tier (retrieval, oracle, or end-to-end)."""

    tier_name: str
    overall_metrics: Dict[str, float]
    category_metrics: Dict[str, Dict[str, float]]
    query_details: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tier_name": self.tier_name,
            "overall_metrics": self.overall_metrics,
            "category_metrics": self.category_metrics,
            "query_details": self.query_details,
        }


@dataclass
class LossQuantification:
    """Quantitative loss attribution across ingestion, retrieval, and generation."""

    direct_facts_recall_at_3: float
    sessions_recall_at_3: Optional[float]
    oracle_reader_accuracy: Optional[float]
    end_to_end_accuracy: Optional[float]

    # Attribution formulas
    ingest_loss: Optional[float] = None
    retrieval_loss: Optional[float] = None
    reader_loss: Optional[float] = None
    total_loss: Optional[float] = None

    def compute(self) -> None:
        if self.sessions_recall_at_3 is not None:
            self.ingest_loss = max(
                0.0, round(self.direct_facts_recall_at_3 - self.sessions_recall_at_3, 4)
            )

        if self.oracle_reader_accuracy is not None:
            self.reader_loss = round(1.0 - self.oracle_reader_accuracy, 4)

        if (
            self.oracle_reader_accuracy is not None
            and self.end_to_end_accuracy is not None
        ):
            self.retrieval_loss = max(
                0.0, round(self.oracle_reader_accuracy - self.end_to_end_accuracy, 4)
            )
            self.total_loss = round(1.0 - self.end_to_end_accuracy, 4)

    def to_dict(self) -> Dict[str, Any]:
        self.compute()
        return {
            "direct_facts_recall@3": self.direct_facts_recall_at_3,
            "sessions_recall@3": self.sessions_recall_at_3,
            "oracle_reader_accuracy": self.oracle_reader_accuracy,
            "end_to_end_accuracy": self.end_to_end_accuracy,
            "ingest_loss": self.ingest_loss,
            "retrieval_loss": self.retrieval_loss,
            "reader_loss": self.reader_loss,
            "total_loss": self.total_loss,
        }


class LongMemEvalEvaluator:
    """Coordinates multi-tier evaluations on LongMemEval-S datasets."""

    def __init__(
        self,
        reader: Optional[BaseReader] = None,
        judge: Optional[BaseJudge] = None,
    ):
        self.reader = reader or RuleBasedMockReader()
        self.judge = judge or RuleBasedJudge()

    def evaluate_retrieval_tier(
        self,
        adapter: BenchmarkAdapter,
        dataset: BenchmarkDataset,
        limit: int = 3,
        k_values: Sequence[int] = (1, 3, 5, 10),
    ) -> TierEvaluationResult:
        """Tier 1: Run retrieval-only benchmark measuring Recall/nDCG/EmptyAccuracy."""
        query_results: List[QueryEvaluationResult] = []
        details: List[Dict[str, Any]] = []

        for q in dataset.queries:
            retrieved_ids, trace = adapter.search(q, limit=limit, capture_trace=True)
            q_qrels = dataset.qrels.get(q.query_id, {})
            q_forbidden = dataset.forbidden.get(q.query_id, [])

            single_metrics = evaluate_single_query(
                query=q,
                retrieved_ids=retrieved_ids,
                qrels=q_qrels,
                forbidden_ids=q_forbidden,
                k_values=k_values,
            )

            relevant_ids = [cid for cid, grade in q_qrels.items() if grade > 0]
            res = QueryEvaluationResult(
                query_id=q.query_id,
                query=q.query,
                category=q.category,
                retrieved_ids=retrieved_ids,
                relevant_ids=relevant_ids,
                forbidden_ids=q_forbidden,
                metrics=single_metrics,
                expected_empty=q.expected_empty,
                scope=q.scope,
                project_id=q.project_id,
                user_id=q.user_id,
                trace=trace,
            )
            query_results.append(res)
            details.append(
                {
                    "query_id": q.query_id,
                    "query": q.query,
                    "category": q.category,
                    "retrieved_ids": retrieved_ids,
                    "qrels": q_qrels,
                    "metrics": single_metrics,
                }
            )

        overall = aggregate_metrics(query_results)
        by_cat = aggregate_by_category(query_results)

        return TierEvaluationResult(
            tier_name="retrieval",
            overall_metrics=overall,
            category_metrics=by_cat,
            query_details=details,
        )

    def evaluate_oracle_reader_tier(
        self,
        dataset: BenchmarkDataset,
        corpus_lookup: Mapping[str, str],
    ) -> TierEvaluationResult:
        """Tier 2: Measure upper-bound accuracy when reader is given gold evidence."""
        scores: List[float] = []
        scores_by_cat: Dict[str, List[float]] = {}
        details: List[Dict[str, Any]] = []

        for q in dataset.queries:
            # Gather gold evidence text
            ev_texts = [
                corpus_lookup[eid]
                for eid in q.evidence_ids
                if eid in corpus_lookup
            ]
            context = "\n".join(ev_texts)

            answer = self.reader.answer(q.query, context)
            score, explanation = self.judge.judge(
                question=q.query,
                reference_answer=q.reference_answer or "",
                candidate_answer=answer,
                is_abstention=q.expected_empty,
            )

            scores.append(score)
            scores_by_cat.setdefault(q.category, []).append(score)

            details.append(
                {
                    "query_id": q.query_id,
                    "query": q.query,
                    "category": q.category,
                    "gold_context": context,
                    "generated_answer": answer,
                    "reference_answer": q.reference_answer,
                    "score": score,
                    "explanation": explanation,
                }
            )

        accuracy = round(sum(scores) / float(len(scores)), 4) if scores else 0.0
        cat_metrics = {
            cat: {"accuracy": round(sum(s) / float(len(s)), 4)}
            for cat, s in scores_by_cat.items()
        }

        return TierEvaluationResult(
            tier_name="oracle-reader",
            overall_metrics={"accuracy": accuracy},
            category_metrics=cat_metrics,
            query_details=details,
        )

    def evaluate_end_to_end_tier(
        self,
        adapter: BenchmarkAdapter,
        dataset: BenchmarkDataset,
        corpus_lookup: Mapping[str, str],
        limit: int = 3,
    ) -> TierEvaluationResult:
        """Tier 3: Run full pipeline: retrieve Top-K -> answer -> judge."""
        scores: List[float] = []
        scores_by_cat: Dict[str, List[float]] = {}
        details: List[Dict[str, Any]] = []

        for q in dataset.queries:
            retrieved_ids, trace = adapter.search(q, limit=limit, capture_trace=True)
            retrieved_texts = [
                corpus_lookup[rid]
                for rid in retrieved_ids
                if rid in corpus_lookup
            ]
            context = "\n\n".join(retrieved_texts)

            answer = self.reader.answer(q.query, context)
            score, explanation = self.judge.judge(
                question=q.query,
                reference_answer=q.reference_answer or "",
                candidate_answer=answer,
                is_abstention=q.expected_empty,
            )

            scores.append(score)
            scores_by_cat.setdefault(q.category, []).append(score)

            details.append(
                {
                    "query_id": q.query_id,
                    "query": q.query,
                    "category": q.category,
                    "retrieved_ids": retrieved_ids,
                    "retrieved_context": context,
                    "generated_answer": answer,
                    "reference_answer": q.reference_answer,
                    "score": score,
                    "explanation": explanation,
                }
            )

        accuracy = round(sum(scores) / float(len(scores)), 4) if scores else 0.0
        cat_metrics = {
            cat: {"accuracy": round(sum(s) / float(len(s)), 4)}
            for cat, s in scores_by_cat.items()
        }

        return TierEvaluationResult(
            tier_name="end-to-end",
            overall_metrics={"accuracy": accuracy},
            category_metrics=cat_metrics,
            query_details=details,
        )
