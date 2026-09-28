"""LoCoMo-10 retrieval and QA evaluation.

The scorer mirrors the official snap-research/locomo task evaluator:
- category 1: multi-hop comma-separated sub-answer F1
- category 2: temporal single-answer F1
- category 3: open-domain single-answer F1 after the official semicolon trim
- category 4: single-hop single-answer F1
- category 5: adversarial abstention accuracy

Official LoCoMo uses NLTK's PorterStemmer. Hippo keeps NLTK optional so the
zero-network fixture remains lightweight, but official named QA runs fail
closed unless NLTK is available (for example: uv run --with nltk ...).
"""

from __future__ import annotations

import hashlib
import os
import re
import string
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from benchmarks.adapter import BenchmarkAdapter
from benchmarks.metrics import aggregate_by_category, aggregate_metrics, evaluate_single_query
from benchmarks.schemas import BenchmarkDataset, QueryEvaluationResult

try:
    from nltk.stem import PorterStemmer
except ImportError:  # pragma: no cover - availability depends on eval environment
    PorterStemmer = None  # type: ignore[assignment]

_PORTER = PorterStemmer() if PorterStemmer is not None else None

LOCOMO_READER_PROMPT = """\
You are answering questions about a long-running conversation using only the
retrieved dialogue memories below.

Retrieved Dialogue Memories:
{context}

Question:
{question}

Instructions:
1. Answer concisely using only information supported by the retrieved memories.
2. If the memories do not support the premise or answer, say "Not mentioned in
   the dialogue." Do not guess or fill gaps from general knowledge.
3. Preserve dates, names, quantities, and relationships exactly when possible.
"""

OFFICIAL_ADVERSARIAL_PHRASES = (
    "no information available",
    "not mentioned",
)

ABSTENTION_PHRASES = (
    *OFFICIAL_ADVERSARIAL_PHRASES,
    "don't know",
    "do not know",
    "unknown",
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
)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_answer(text: str) -> str:
    """Match LoCoMo's official text normalization."""

    def remove_articles(value: str) -> str:
        return re.sub(r"\b(a|an|the|and)\b", " ", value)

    def remove_punc(value: str) -> str:
        exclude = set(string.punctuation)
        return "".join(ch for ch in value if ch not in exclude)

    normalized = text.replace(",", "")
    normalized = remove_punc(normalized.lower())
    normalized = remove_articles(normalized)
    return " ".join(normalized.split())


def _stemmed_tokens(text: str, *, require_porter: bool = False) -> list[str]:
    tokens = normalize_answer(text).split()
    if _PORTER is None:
        if require_porter:
            raise RuntimeError(
                "Official LoCoMo QA scoring requires NLTK PorterStemmer. "
                "Run with 'uv run --with nltk ...' or install nltk in the "
                "evaluation environment."
            )
        return tokens
    return [_PORTER.stem(token) for token in tokens]


def ensure_official_scorer_available() -> None:
    """Fail closed when exact upstream Porter stemming is unavailable."""
    if _PORTER is None:
        raise RuntimeError(
            "Official LoCoMo QA scoring requires NLTK PorterStemmer. "
            "Run with 'uv run --with nltk ...' or install nltk in the "
            "evaluation environment."
        )


def compute_exact_match(prediction: str, ground_truth: str) -> float:
    """Match the official scorer's set-based normalized exact-match helper."""
    pred = set(normalize_answer(prediction).split())
    gold = set(normalize_answer(ground_truth).split())
    return 1.0 if pred == gold else 0.0


def compute_qa_f1(
    prediction: str,
    ground_truth: str,
    *,
    require_porter: bool = False,
) -> float:
    """Compute the official single-answer Porter-stemmed token F1."""
    pred_tokens = _stemmed_tokens(prediction, require_porter=require_porter)
    gold_tokens = _stemmed_tokens(ground_truth, require_porter=require_porter)

    if not pred_tokens or not gold_tokens:
        return 1.0 if pred_tokens == gold_tokens else 0.0

    common = Counter(pred_tokens) & Counter(gold_tokens)
    num_same = sum(common.values())
    if num_same == 0:
        return 0.0

    precision = num_same / float(len(pred_tokens))
    recall = num_same / float(len(gold_tokens))
    return (2 * precision * recall) / (precision + recall)


def compute_locomo_official_f1(
    prediction: str,
    ground_truth: str,
    category: int,
    *,
    require_porter: bool = False,
) -> float:
    """Mirror LoCoMo's official category-specific F1 semantics for 1-4."""
    if category == 3:
        ground_truth = ground_truth.split(";")[0].strip()

    if category in (2, 3, 4):
        return compute_qa_f1(
            prediction,
            ground_truth,
            require_porter=require_porter,
        )

    if category == 1:
        predictions = [part.strip() for part in prediction.split(",")]
        ground_truths = [part.strip() for part in ground_truth.split(",")]
        if not ground_truths:
            return 0.0

        per_gold: list[float] = []
        for gold in ground_truths:
            best = max(
                (
                    compute_qa_f1(
                        pred,
                        gold,
                        require_porter=require_porter,
                    )
                    for pred in predictions
                ),
                default=0.0,
            )
            per_gold.append(best)
        return sum(per_gold) / float(len(per_gold))

    raise ValueError(f"LoCoMo F1 is defined for categories 1-4, got {category}")


def judge_adversarial_answer(
    prediction: str,
    adversarial_answer: str | None = None,
    *,
    strict_official: bool = False,
) -> tuple[float, str]:
    """Score category-5 refusal separately from ordinary QA F1."""
    pred_clean = prediction.strip().lower()
    phrases = OFFICIAL_ADVERSARIAL_PHRASES if strict_official else ABSTENTION_PHRASES
    is_abstained = any(phrase in pred_clean for phrase in phrases)

    trapped = False
    if adversarial_answer:
        trap = normalize_answer(adversarial_answer)
        trapped = bool(trap and trap in normalize_answer(pred_clean))

    if trapped:
        return 0.0, f"Fell into adversarial trap: '{adversarial_answer}'."
    if is_abstained:
        return 1.0, "Correctly refused the unsupported premise."
    return 0.0, "Generated an unsupported answer instead of abstaining."


class RuleBasedMockLoCoMoReader:
    """Deterministic zero-network reader for CI smoke tests only."""

    def __init__(self, answer_map: Mapping[str, str] | None = None):
        self.answer_map = dict(answer_map or {})

    def answer(self, question: str, context: str) -> str:
        q_norm = question.strip().lower()
        if q_norm in self.answer_map:
            return self.answer_map[q_norm]

        if not context or not context.strip():
            return "Not mentioned in the dialogue."

        lines = [line.strip() for line in context.splitlines() if line.strip()]
        for line in lines:
            clean_line = re.sub(r"^\[.*?\]\s*", "", line)
            clean_line = re.sub(r"^[A-Za-z0-9_]+:\s*", "", clean_line)
            if clean_line:
                return clean_line
        return "Not mentioned in the dialogue."

    def manifest_config(self) -> dict[str, Any]:
        return {
            "backend": "mock",
            "model": "rule-based-locomo-fixture",
            "prompt_sha256": _sha256_text(LOCOMO_READER_PROMPT),
            "temperature": 0.0,
            "max_output_tokens": 0,
        }


class GeminiLoCoMoReader:
    """Fixed-prompt Gemini reader for real LoCoMo QA runs."""

    def __init__(
        self,
        model: str,
        *,
        temperature: float = 0.0,
        max_output_tokens: int = 256,
        client: Any = None,
    ):
        self.model = model
        self.temperature = float(temperature)
        self.max_output_tokens = int(max_output_tokens)
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            from google import genai

            api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
            if not api_key:
                raise RuntimeError(
                    "GOOGLE_API_KEY or GEMINI_API_KEY is required for "
                    "--qa-backend gemini"
                )
            self._client = genai.Client(api_key=api_key)
        return self._client

    def answer(self, question: str, context: str) -> str:
        from google.genai import types

        prompt = LOCOMO_READER_PROMPT.format(context=context, question=question)
        response = self._get_client().models.generate_content(
            model=self.model,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=self.temperature,
                max_output_tokens=self.max_output_tokens,
            ),
        )
        text = getattr(response, "text", None)
        if not text:
            raise RuntimeError("Gemini LoCoMo reader returned an empty response")
        return str(text).strip()

    def manifest_config(self) -> dict[str, Any]:
        return {
            "backend": "gemini",
            "model": self.model,
            "prompt_sha256": _sha256_text(LOCOMO_READER_PROMPT),
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
        }


@dataclass
class LoCoMoEvaluationResult:
    """Full LoCoMo retrieval + QA result including per-question output."""

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
    """Coordinates retrieval and fixed-reader QA evaluation."""

    def __init__(
        self,
        reader: Any | None = None,
        *,
        strict_official_scorer: bool = False,
    ):
        self.reader = reader or RuleBasedMockLoCoMoReader()
        self.strict_official_scorer = strict_official_scorer

    def manifest_config(self) -> dict[str, Any]:
        reader_config = (
            self.reader.manifest_config()
            if hasattr(self.reader, "manifest_config")
            else {"backend": type(self.reader).__name__}
        )
        return {
            "reader": reader_config,
            "scorer": {
                "backend": "snap-research/locomo task_eval/evaluation.py semantics",
                "porter_stemmer": "nltk.stem.PorterStemmer",
                "strict_official": self.strict_official_scorer,
                "adversarial_reported_separately": True,
            },
        }

    def evaluate(
        self,
        adapter: BenchmarkAdapter,
        dataset: BenchmarkDataset,
        limit: int = 3,
        k_values: Sequence[int] = (1, 3, 5, 10),
        evaluate_qa: bool = True,
    ) -> LoCoMoEvaluationResult:
        """Evaluate retrieval to max(k) while restricting QA context to limit."""
        corpus_lookup = {item.id: item.text for item in dataset.corpus}
        query_results: list[QueryEvaluationResult] = []
        details: list[dict[str, Any]] = []

        regular_f1_scores: list[float] = []
        regular_em_scores: list[float] = []
        adversarial_scores: list[float] = []
        f1_by_cat: dict[str, list[float]] = {}
        adversarial_by_cat: dict[str, list[float]] = {}

        search_depth = max([int(limit), *[int(k) for k in k_values]])

        for query in dataset.queries:
            retrieved_ids, trace = adapter.search(
                query,
                limit=search_depth,
                capture_trace=True,
            )
            qrels = dataset.qrels.get(query.query_id, {})
            forbidden_ids = dataset.forbidden.get(query.query_id, [])
            metrics = evaluate_single_query(
                query=query,
                retrieved_ids=retrieved_ids,
                qrels=qrels,
                forbidden_ids=forbidden_ids,
                k_values=k_values,
            )

            relevant_ids = [cid for cid, grade in qrels.items() if grade > 0]
            query_results.append(
                QueryEvaluationResult(
                    query_id=query.query_id,
                    query=query.query,
                    category=query.category,
                    retrieved_ids=retrieved_ids,
                    relevant_ids=relevant_ids,
                    forbidden_ids=forbidden_ids,
                    metrics=metrics,
                    expected_empty=query.expected_empty,
                    scope=query.scope,
                    project_id=query.project_id,
                    user_id=query.user_id,
                    trace=trace,
                )
            )

            generated_answer = ""
            explanation = ""
            f1 = 0.0
            em = 0.0
            adversarial_accuracy: float | None = None
            qa_context_ids = retrieved_ids[:limit]

            if evaluate_qa:
                context = "\n".join(
                    corpus_lookup[cid]
                    for cid in qa_context_ids
                    if cid in corpus_lookup
                )
                generated_answer = self.reader.answer(
                    question=query.query,
                    context=context,
                )

                category_id = int(query.metadata.get("category_id", 0) or 0)
                is_adversarial = category_id == 5 or query.expected_empty

                if is_adversarial:
                    adversarial_accuracy, explanation = judge_adversarial_answer(
                        prediction=generated_answer,
                        adversarial_answer=query.metadata.get("adversarial_answer"),
                        strict_official=self.strict_official_scorer,
                    )
                    adversarial_scores.append(adversarial_accuracy)
                    adversarial_by_cat.setdefault(query.category, []).append(
                        adversarial_accuracy
                    )
                else:
                    reference = query.reference_answer or ""
                    f1 = compute_locomo_official_f1(
                        generated_answer,
                        reference,
                        category_id,
                        require_porter=self.strict_official_scorer,
                    )
                    em = compute_exact_match(generated_answer, reference)
                    regular_f1_scores.append(f1)
                    regular_em_scores.append(em)
                    f1_by_cat.setdefault(query.category, []).append(f1)
                    explanation = f"Official-style F1: {f1:.4f}; EM: {em:.1f}"

            details.append(
                {
                    "query_id": query.query_id,
                    "query": query.query,
                    "category": query.category,
                    "category_id": query.metadata.get("category_id"),
                    "retrieved_ids": retrieved_ids,
                    "qa_context_ids": qa_context_ids,
                    "generated_answer": generated_answer,
                    "reference_answer": query.reference_answer,
                    "f1": f1 if adversarial_accuracy is None else None,
                    "em": em if adversarial_accuracy is None else None,
                    "adversarial_accuracy": adversarial_accuracy,
                    "explanation": explanation,
                    "metrics": metrics,
                }
            )

        overall_retrieval = aggregate_metrics(query_results)
        category_retrieval = aggregate_by_category(query_results)

        qa_summary: dict[str, float] = {}
        if evaluate_qa and regular_f1_scores:
            qa_summary["avg_f1"] = round(
                sum(regular_f1_scores) / len(regular_f1_scores),
                4,
            )
            qa_summary["avg_em"] = round(
                sum(regular_em_scores) / len(regular_em_scores),
                4,
            )
        if evaluate_qa and adversarial_scores:
            qa_summary["adversarial_accuracy"] = round(
                sum(adversarial_scores) / len(adversarial_scores),
                4,
            )

        for category, category_metrics in category_retrieval.items():
            if f1_by_cat.get(category):
                category_metrics["qa_f1"] = round(
                    sum(f1_by_cat[category]) / len(f1_by_cat[category]),
                    4,
                )
            if adversarial_by_cat.get(category):
                category_metrics["adversarial_accuracy"] = round(
                    sum(adversarial_by_cat[category])
                    / len(adversarial_by_cat[category]),
                    4,
                )

        return LoCoMoEvaluationResult(
            retrieval_metrics=overall_retrieval,
            qa_metrics=qa_summary,
            category_metrics=category_retrieval,
            query_details=details,
        )
