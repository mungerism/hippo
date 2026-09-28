"""Evaluation logic and QA Token F1 scorer for LoCoMo-10 benchmark.

Implements:
- Standard SQuAD-style token-level F1 and Exact Match (EM) computation aligned with LoCoMo official scorer.
- Dedicated Adversarial / No-Answer verification preventing deceptive hallucination.
- 5-category breakdown reporting: single_hop, multi_hop, temporal, open_domain, adversarial.
- Multi-tier evaluations: Retrieval-only (Top-3 / Top-10) and End-to-End QA generation.
"""

from __future__ import annotations

import logging
import re
import string
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

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

# Common phrases indicating correct refusal / abstention for Adversarial questions
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
    "did not happen",
    "didn't happen",
    "never occurred",
    "not occurred",
    "false premise",
    "未知",
    "不知道",
    "未提及",
    "未说明",
    "未发生",
]


def normalize_answer(s: str) -> str:
    """Normalize text by lowercasing, removing punctuation, articles, and extra whitespace."""

    def remove_articles(text: str) -> str:
        return re.sub(r"\b(a|an|the)\b", " ", text)

    def white_space_fix(text: str) -> str:
        return " ".join(text.split())

    def remove_punc(text: str) -> str:
        exclude = set(string.punctuation)
        return "".join(ch for ch in text if ch not in exclude)

    def lower(text: str) -> str:
        return text.lower()

    return white_space_fix(remove_articles(remove_punc(lower(s))))


def compute_exact_match(prediction: str, ground_truth: str) -> float:
    """Compute exact match between normalized prediction and ground truth."""
    return 1.0 if normalize_answer(prediction) == normalize_answer(ground_truth) else 0.0


def compute_qa_f1(prediction: str, ground_truth: str) -> float:
    """Compute standard token-level F1 score between prediction and ground truth."""
    pred_tokens = normalize_answer(prediction).split()
    gold_tokens = normalize_answer(ground_truth).split()

    if not pred_tokens or not gold_tokens:
        return 1.0 if pred_tokens == gold_tokens else 0.0

    common = Counter(pred_tokens) & Counter(gold_tokens)
    num_same = sum(common.values())

    if num_same == 0:
        return 0.0

    precision = 1.0 * num_same / len(pred_tokens)
    recall = 1.0 * num_same / len(gold_tokens)
    f1 = (2 * precision * recall) / (precision + recall)
    return round(f1, 4)


def judge_adversarial_answer(
    prediction: str,
    adversarial_answer: str | None = None,
) -> tuple[float, str]:
    """Judge model answer on Adversarial / No-Answer questions (Category 5).

    - If the prediction recognizes the false premise or states unknown -> Score 1.0.
    - If the prediction falls into the trap (e.g. outputs adversarial_answer) -> Score 0.0.
    """
    pred_clean = prediction.strip().lower()

    # 1. Check if model correctly abstains
    is_abstained = any(p in pred_clean for p in ABSTENTION_PHRASES)

    # 2. Check if model outputs the deceptive adversarial trap answer
    trapped = False
    if adversarial_answer:
        adv_clean = normalize_answer(adversarial_answer)
        if adv_clean and adv_clean in normalize_answer(pred_clean):
            trapped = True

    if is_abstained and not trapped:
        return 1.0, "Correctly refused to hallucinate / identified ungrounded premise."
    elif trapped:
        return 0.0, f"Fell into adversarial trap: generated deceptive answer '{adversarial_answer}'."
    else:
        # Default for ungrounded generation on adversarial query
        return 0.0, "Generated ungrounded hallucination instead of abstaining."


class RuleBasedMockLoCoMoReader:
    """Deterministic offline reader for unit tests and local smoke verification."""

    def __init__(self, answer_map: Mapping[str, str] | None = None):
        self.answer_map = dict(answer_map or {})

    def answer(self, question: str, context: str, is_adversarial: bool = False) -> str:
        q_norm = question.strip().lower()
        if q_norm in self.answer_map:
            return self.answer_map[q_norm]

        if is_adversarial or not context or not context.strip():
            return "I don't know based on the provided context, this was not mentioned."

        # Extract first non-trivial sentence from context
        lines = [line.strip() for line in context.splitlines() if line.strip()]
        for line in lines:
            # Strip timestamp and speaker prefix if present
            clean_line = re.sub(r"^\[.*?\]\s*", "", line)
            clean_line = re.sub(r"^[A-Za-z0-9_]+:\s*", "", clean_line)
            if clean_line:
                return clean_line

        return "I don't know based on the provided context."


@dataclass
class LoCoMoEvaluationResult:
    """Full evaluation result for LoCoMo-10 benchmark."""

    retrieval_metrics: dict[str, float]
    qa_metrics: dict[str, float]
    category_metrics: dict[str, dict[str, float]]
    query_details: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "retrieval_metrics": self.retrieval_metrics,
            "qa_metrics": self.qa_metrics,
            "category_metrics": self.category_metrics,
            "query_details": self.query_details,
        }


class LoCoMoEvaluator:
    """Coordinates retrieval and QA evaluations for LoCoMo-10."""

    def __init__(self, reader: Any | None = None):
        self.reader = reader or RuleBasedMockLoCoMoReader()

    def evaluate(
        self,
        adapter: BenchmarkAdapter,
        dataset: BenchmarkDataset,
        limit: int = 3,
        k_values: Sequence[int] = (1, 3, 5, 10),
        evaluate_qa: bool = True,
    ) -> LoCoMoEvaluationResult:
        """Execute evaluation over LoCoMo dataset."""
        corpus_lookup = {c.id: c.text for c in dataset.corpus}
        query_results: list[QueryEvaluationResult] = []
        details: list[dict[str, Any]] = []

        qa_f1_scores: list[float] = []
        qa_em_scores: list[float] = []
        f1_by_cat: dict[str, list[float]] = {}

        for q in dataset.queries:
            # 1. Search retrieval
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
            query_results.append(
                QueryEvaluationResult(
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
            )

            # 2. QA Evaluation if enabled
            f1 = 0.0
            em = 0.0
            generated_answer = ""
            explanation = ""

            if evaluate_qa:
                retrieved_texts = [
                    corpus_lookup[rid] for rid in retrieved_ids if rid in corpus_lookup
                ]
                context = "\n".join(retrieved_texts)

                is_adv = q.expected_empty or (q.category == "adversarial")
                generated_answer = self.reader.answer(
                    question=q.query,
                    context=context,
                    is_adversarial=is_adv,
                )

                if is_adv:
                    adv_ans = q.metadata.get("adversarial_answer")
                    score, explanation = judge_adversarial_answer(
                        prediction=generated_answer,
                        adversarial_answer=adv_ans,
                    )
                    f1 = score
                    em = score
                else:
                    ref = q.reference_answer or ""
                    f1 = compute_qa_f1(generated_answer, ref)
                    em = compute_exact_match(generated_answer, ref)
                    explanation = f"Token F1: {f1:.4f}, EM: {em:.1f}"

                qa_f1_scores.append(f1)
                qa_em_scores.append(em)
                f1_by_cat.setdefault(q.category, []).append(f1)

            details.append(
                {
                    "query_id": q.query_id,
                    "query": q.query,
                    "category": q.category,
                    "retrieved_ids": retrieved_ids,
                    "generated_answer": generated_answer,
                    "reference_answer": q.reference_answer,
                    "f1": f1,
                    "em": em,
                    "explanation": explanation,
                    "metrics": single_metrics,
                }
            )

        overall_retrieval = aggregate_metrics(query_results)
        cat_retrieval = aggregate_by_category(query_results)

        # Merge QA metrics into category summary
        qa_summary: dict[str, float] = {}
        if evaluate_qa and qa_f1_scores:
            qa_summary["avg_f1"] = round(sum(qa_f1_scores) / float(len(qa_f1_scores)), 4)
            qa_summary["avg_em"] = round(sum(qa_em_scores) / float(len(qa_em_scores)), 4)

        for cat, cat_dict in cat_retrieval.items():
            if f1_by_cat.get(cat):
                cat_dict["qa_f1"] = round(sum(f1_by_cat[cat]) / float(len(f1_by_cat[cat])), 4)

        return LoCoMoEvaluationResult(
            retrieval_metrics=overall_retrieval,
            qa_metrics=qa_summary,
            category_metrics=cat_retrieval,
            query_details=details,
        )
