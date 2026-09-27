"""Three-tier evaluator for LongMemEval-S.

Tier 1 measures retrieval only. Tier 2 gives the reader gold context. Tier 3
uses Hippo's retrieved context. Offline rule-based reader/judge implementations
exist only for deterministic CI smoke tests; real benchmark QA runs can use the
Gemini implementations and record their full configuration in the manifest.

Loss fields are diagnostic deltas. They are deliberately not presented as an
additive decomposition because retrieval recall and QA accuracy live on
different scales and interactions prevent a mathematically valid identity.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from benchmarks.adapter import BenchmarkAdapter
from benchmarks.metrics import aggregate_by_category, aggregate_metrics, evaluate_single_query
from benchmarks.schemas import BenchmarkDataset, QueryEvaluationResult

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


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def official_judge_prompt(
    *,
    question_type: Optional[str],
    question: str,
    reference_answer: str,
    candidate_answer: str,
    is_abstention: bool,
) -> str:
    """Mirror the semantics of LongMemEval's official evaluate_qa.py prompts."""

    if is_abstention:
        return (
            "I will give you an unanswerable question, an explanation, and a response "
            "from a model. Please answer yes if the model correctly identifies the "
            "question as unanswerable. The model could say that the information is "
            "incomplete, or some other information is given but the asked information "
            "is not.\n\n"
            f"Question: {question}\n\n"
            f"Explanation: {reference_answer}\n\n"
            f"Model Response: {candidate_answer}\n\n"
            "Does the model correctly identify the question as unanswerable? "
            "Answer yes or no only."
        )

    task = (question_type or "").strip().lower()
    if task == "single-session-preference":
        return (
            "I will give you a question, a rubric for desired personalized response, "
            "and a response from a model. Please answer yes if the response satisfies "
            "the desired response. Otherwise, answer no. The model does not need to "
            "reflect all the points in the rubric. The response is correct as long as "
            "it recalls and utilizes the user's personal information correctly.\n\n"
            f"Question: {question}\n\nRubric: {reference_answer}\n\n"
            f"Model Response: {candidate_answer}\n\n"
            "Is the model response correct? Answer yes or no only."
        )

    if task == "temporal-reasoning":
        extra = (
            " In addition, do not penalize off-by-one errors for the number of days. "
            "If the question asks for the number of days/weeks/months, etc., and the "
            "model makes off-by-one errors, the response is still correct."
        )
    elif task == "knowledge-update":
        return (
            "I will give you a question, a correct answer, and a response from a model. "
            "Please answer yes if the response contains the correct answer. Otherwise, "
            "answer no. If the response contains some previous information along with "
            "an updated answer, the response should be considered correct as long as "
            "the updated answer is the required answer.\n\n"
            f"Question: {question}\n\nCorrect Answer: {reference_answer}\n\n"
            f"Model Response: {candidate_answer}\n\n"
            "Is the model response correct? Answer yes or no only."
        )
    else:
        extra = ""

    return (
        "I will give you a question, a correct answer, and a response from a model. "
        "Please answer yes if the response contains the correct answer. Otherwise, "
        "answer no. If the response is equivalent to the correct answer or contains "
        "all the intermediate steps to get the correct answer, you should also answer "
        "yes. If the response only contains a subset of the information required by "
        "the answer, answer no."
        f"{extra}\n\nQuestion: {question}\n\n"
        f"Correct Answer: {reference_answer}\n\n"
        f"Model Response: {candidate_answer}\n\n"
        "Is the model response correct? Answer yes or no only."
    )


class BaseReader(abc.ABC):
    @abc.abstractmethod
    def answer(self, question: str, context: str) -> str:
        pass

    def manifest_config(self) -> Dict[str, Any]:
        return {"backend": type(self).__name__}


class RuleBasedMockReader(BaseReader):
    """Deterministic reader for zero-network tests only."""

    def __init__(self, answer_map: Optional[Mapping[str, str]] = None):
        self.answer_map = dict(answer_map or {})

    def answer(self, question: str, context: str) -> str:
        q_norm = question.strip().lower()
        if q_norm in self.answer_map:
            return self.answer_map[q_norm]
        if not context or not context.strip():
            return "I don't know based on the provided context."

        lines = [line.strip() for line in context.splitlines() if line.strip()]
        for line in lines:
            if not line.startswith(("Date:", "User:", "Assistant:")):
                return line
        return lines[0] if lines else "I don't know based on the provided context."

    def manifest_config(self) -> Dict[str, Any]:
        return {
            "backend": "mock",
            "model": "rule-based-fixture",
            "prompt_sha256": _sha256_text(LONGMEMEVAL_READER_PROMPT),
            "temperature": 0.0,
            "max_output_tokens": 0,
        }


class GeminiReader(BaseReader):
    """Real reader backed by google-genai."""

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
                    "GOOGLE_API_KEY or GEMINI_API_KEY is required for --qa-backend gemini"
                )
            self._client = genai.Client(api_key=api_key)
        return self._client

    def answer(self, question: str, context: str) -> str:
        from google.genai import types

        prompt = LONGMEMEVAL_READER_PROMPT.format(context=context, question=question)
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
            raise RuntimeError("Gemini reader returned an empty response")
        return str(text).strip()

    def manifest_config(self) -> Dict[str, Any]:
        return {
            "backend": "gemini",
            "model": self.model,
            "prompt_sha256": _sha256_text(LONGMEMEVAL_READER_PROMPT),
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
        }


class BaseJudge(abc.ABC):
    @abc.abstractmethod
    def judge(
        self,
        question: str,
        reference_answer: str,
        candidate_answer: str,
        is_abstention: bool = False,
        question_type: Optional[str] = None,
    ) -> Tuple[float, str]:
        pass

    def manifest_config(self) -> Dict[str, Any]:
        return {"backend": type(self).__name__}


class RuleBasedJudge(BaseJudge):
    """Deterministic smoke-test judge; not an official LongMemEval metric."""

    def judge(
        self,
        question: str,
        reference_answer: str,
        candidate_answer: str,
        is_abstention: bool = False,
        question_type: Optional[str] = None,
    ) -> Tuple[float, str]:
        cand_clean = candidate_answer.strip().lower()
        ref_clean = reference_answer.strip().lower()

        if is_abstention:
            is_abstained = any(phrase in cand_clean for phrase in ABSTENTION_PHRASES)
            if is_abstained:
                return 1.0, "Correctly abstained / expressed uncertainty."
            return 0.0, "Failed to abstain; generated ungrounded answer."

        ref_words = set(re.findall(r"\b[a-zA-Z0-9_\u4e00-\u9fa5]+\b", ref_clean))
        ref_content_words = {
            word
            for word in ref_words
            if len(word) > 2 and word not in {"the", "and", "for", "with"}
        }

        if not ref_content_words:
            matched = ref_clean in cand_clean
            return (1.0, "Matched reference") if matched else (0.0, "Missing reference")

        overlap = sum(1 for word in ref_content_words if word in cand_clean)
        match_ratio = overlap / float(len(ref_content_words))
        if match_ratio >= 0.5 or ref_clean in cand_clean:
            return 1.0, (
                f"Matched {overlap}/{len(ref_content_words)} key concepts from reference."
            )
        return 0.0, (
            f"Insufficient match: {overlap}/{len(ref_content_words)} concepts matched."
        )

    def manifest_config(self) -> Dict[str, Any]:
        return {
            "backend": "mock",
            "model": "rule-based-fixture",
            "prompt_sha256": "n/a",
            "temperature": 0.0,
            "max_output_tokens": 0,
        }


class GeminiJudge(BaseJudge):
    """LLM judge using prompts aligned with LongMemEval's official evaluator."""

    def __init__(
        self,
        model: str,
        *,
        temperature: float = 0.0,
        max_output_tokens: int = 10,
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
                    "GOOGLE_API_KEY or GEMINI_API_KEY is required for --qa-backend gemini"
                )
            self._client = genai.Client(api_key=api_key)
        return self._client

    def judge(
        self,
        question: str,
        reference_answer: str,
        candidate_answer: str,
        is_abstention: bool = False,
        question_type: Optional[str] = None,
    ) -> Tuple[float, str]:
        from google.genai import types

        prompt = official_judge_prompt(
            question_type=question_type,
            question=question,
            reference_answer=reference_answer,
            candidate_answer=candidate_answer,
            is_abstention=is_abstention,
        )
        response = self._get_client().models.generate_content(
            model=self.model,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=self.temperature,
                max_output_tokens=self.max_output_tokens,
            ),
        )
        text = str(getattr(response, "text", "") or "").strip().lower()
        if not text:
            raise RuntimeError("Gemini judge returned an empty response")
        accepted = text.startswith("yes") or " yes" in text
        return (1.0 if accepted else 0.0), text

    def manifest_config(self) -> Dict[str, Any]:
        # Prompts vary by official task type; hash the source template function's
        # stable marker rather than pretending one prompt string is universal.
        marker = "LongMemEval official evaluate_qa.py semantic prompt family v1"
        return {
            "backend": "gemini",
            "model": self.model,
            "prompt_sha256": _sha256_text(marker),
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
            "prompt_family": marker,
        }


@dataclass
class TierEvaluationResult:
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
    """Diagnostic loss deltas; components are intentionally non-additive."""

    direct_facts_recall_at_3: Optional[float] = None
    sessions_recall_at_3: Optional[float] = None
    oracle_reader_accuracy: Optional[float] = None
    end_to_end_accuracy: Optional[float] = None

    ingest_loss: Optional[float] = None
    retrieval_loss: Optional[float] = None
    reader_loss: Optional[float] = None
    total_loss: Optional[float] = None

    def compute(self) -> None:
        if (
            self.direct_facts_recall_at_3 is not None
            and self.sessions_recall_at_3 is not None
        ):
            self.ingest_loss = max(
                0.0,
                round(
                    self.direct_facts_recall_at_3 - self.sessions_recall_at_3,
                    4,
                ),
            )
        else:
            self.ingest_loss = None

        self.reader_loss = (
            round(1.0 - self.oracle_reader_accuracy, 4)
            if self.oracle_reader_accuracy is not None
            else None
        )

        if (
            self.oracle_reader_accuracy is not None
            and self.end_to_end_accuracy is not None
        ):
            self.retrieval_loss = max(
                0.0,
                round(self.oracle_reader_accuracy - self.end_to_end_accuracy, 4),
            )
        else:
            self.retrieval_loss = None

        self.total_loss = (
            round(1.0 - self.end_to_end_accuracy, 4)
            if self.end_to_end_accuracy is not None
            else None
        )

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
            "components_are_additive": False,
            "note": (
                "Loss components are diagnostic deltas across different stages/scales; "
                "do not sum them to reconstruct total_loss."
            ),
        }


class LongMemEvalEvaluator:
    def __init__(
        self,
        reader: Optional[BaseReader] = None,
        judge: Optional[BaseJudge] = None,
    ):
        self.reader = reader or RuleBasedMockReader()
        self.judge = judge or RuleBasedJudge()

    def manifest_config(self) -> Dict[str, Any]:
        return {
            "reader": self.reader.manifest_config(),
            "judge": self.judge.manifest_config(),
        }

    def evaluate_retrieval_tier(
        self,
        adapter: BenchmarkAdapter,
        dataset: BenchmarkDataset,
        limit: int = 10,
        k_values: Sequence[int] = (1, 3, 5, 10),
    ) -> TierEvaluationResult:
        evaluation_depth = max([int(k) for k in k_values] + [int(limit)])
        query_results: List[QueryEvaluationResult] = []
        details: List[Dict[str, Any]] = []

        for query in dataset.queries:
            retrieved_ids, trace = adapter.search(
                query,
                limit=evaluation_depth,
                capture_trace=True,
            )
            qrels = dataset.qrels.get(query.query_id, {})
            forbidden = dataset.forbidden.get(query.query_id, [])
            metrics = evaluate_single_query(
                query=query,
                retrieved_ids=retrieved_ids,
                qrels=qrels,
                forbidden_ids=forbidden,
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
                    forbidden_ids=forbidden,
                    metrics=metrics,
                    expected_empty=query.expected_empty,
                    scope=query.scope,
                    project_id=query.project_id,
                    user_id=query.user_id,
                    trace=trace,
                )
            )
            details.append(
                {
                    "query_id": query.query_id,
                    "query": query.query,
                    "category": query.category,
                    "retrieved_ids": retrieved_ids,
                    "qrels": qrels,
                    "metrics": metrics,
                }
            )

        return TierEvaluationResult(
            tier_name="retrieval",
            overall_metrics=aggregate_metrics(query_results),
            category_metrics=aggregate_by_category(query_results),
            query_details=details,
        )

    def evaluate_oracle_reader_tier(
        self,
        dataset: BenchmarkDataset,
        corpus_lookup: Mapping[str, str],
    ) -> TierEvaluationResult:
        scores: List[float] = []
        scores_by_cat: Dict[str, List[float]] = {}
        details: List[Dict[str, Any]] = []

        for query in dataset.queries:
            context = "\n".join(
                corpus_lookup[evidence_id]
                for evidence_id in query.evidence_ids
                if evidence_id in corpus_lookup
            )
            answer = self.reader.answer(query.query, context)
            score, explanation = self.judge.judge(
                question=query.query,
                reference_answer=query.reference_answer or "",
                candidate_answer=answer,
                is_abstention=query.expected_empty,
                question_type=query.metadata.get("official_question_type"),
            )
            scores.append(score)
            scores_by_cat.setdefault(query.category, []).append(score)
            details.append(
                {
                    "query_id": query.query_id,
                    "query": query.query,
                    "category": query.category,
                    "gold_context": context,
                    "generated_answer": answer,
                    "reference_answer": query.reference_answer,
                    "score": score,
                    "explanation": explanation,
                }
            )

        accuracy = round(sum(scores) / float(len(scores)), 4) if scores else 0.0
        return TierEvaluationResult(
            tier_name="oracle-reader",
            overall_metrics={"accuracy": accuracy},
            category_metrics={
                category: {"accuracy": round(sum(values) / float(len(values)), 4)}
                for category, values in scores_by_cat.items()
            },
            query_details=details,
        )

    def evaluate_end_to_end_tier(
        self,
        adapter: BenchmarkAdapter,
        dataset: BenchmarkDataset,
        corpus_lookup: Mapping[str, str],
        limit: int = 3,
    ) -> TierEvaluationResult:
        scores: List[float] = []
        scores_by_cat: Dict[str, List[float]] = {}
        details: List[Dict[str, Any]] = []

        for query in dataset.queries:
            retrieved_ids, _trace = adapter.search(
                query,
                limit=limit,
                capture_trace=True,
            )
            context = "\n\n".join(
                corpus_lookup[retrieved_id]
                for retrieved_id in retrieved_ids
                if retrieved_id in corpus_lookup
            )
            answer = self.reader.answer(query.query, context)
            score, explanation = self.judge.judge(
                question=query.query,
                reference_answer=query.reference_answer or "",
                candidate_answer=answer,
                is_abstention=query.expected_empty,
                question_type=query.metadata.get("official_question_type"),
            )
            scores.append(score)
            scores_by_cat.setdefault(query.category, []).append(score)
            details.append(
                {
                    "query_id": query.query_id,
                    "query": query.query,
                    "category": query.category,
                    "retrieved_ids": retrieved_ids,
                    "retrieved_context": context,
                    "generated_answer": answer,
                    "reference_answer": query.reference_answer,
                    "score": score,
                    "explanation": explanation,
                }
            )

        accuracy = round(sum(scores) / float(len(scores)), 4) if scores else 0.0
        return TierEvaluationResult(
            tier_name="end-to-end",
            overall_metrics={"accuracy": accuracy},
            category_metrics={
                category: {"accuracy": round(sum(values) / float(len(values)), 4)}
                for category, values in scores_by_cat.items()
            },
            query_details=details,
        )


def export_official_hypotheses(
    result: TierEvaluationResult,
    output_path: Path,
) -> Path:
    """Export answers in the JSONL format consumed by official evaluate_qa.py."""
    if result.tier_name not in {"oracle-reader", "end-to-end"}:
        raise ValueError("Only QA tiers can be exported as LongMemEval hypotheses")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(
            {
                "question_id": detail["query_id"],
                "hypothesis": detail["generated_answer"],
            },
            ensure_ascii=False,
        )
        for detail in result.query_details
    ]
    output_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return output_path
