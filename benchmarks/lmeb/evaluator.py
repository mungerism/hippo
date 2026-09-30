"""LMEB component evaluation and side-by-side embedding profile comparator.

Computes:
- nDCG@10 (MTEB/KaLM alignment)
- Recall@10
- MRR
- Recall@3 (Hippo reference)

Provides a side-by-side Markdown comparison table and enforces explicit disclaimer:
"Component scores evaluate embedding representation and candidate recall only;
they do not equal Hippo end-to-end production memory quality with lifecycle gates."
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import logging
from typing import Any, Mapping, Optional, Sequence

from benchmarks.adapter import BenchmarkAdapter
from benchmarks.metrics import aggregate_metrics, evaluate_single_query
from benchmarks.schemas import BenchmarkDataset, QueryEvaluationResult

logger = logging.getLogger(__name__)

DISCLAIMER_TEXT = (
    "⚠️ **免责声明 (Component Disclaimer)**: "
    "LMEB 是用于评估 Embedding 模型在长上下文对话记忆中表征与候选召回能力的组件级基准（衡量 Top-10 覆盖率与相关性排序）。"
    "组件得分**不等于** Hippo 生产端到端的综合记忆质量与安全表现（如身份正交隔离、状态失效防污染与硬负样本置空）。"
    "不得以单一组件高分替代 Hippo Gold v1 安全门禁。"
)


@dataclass(frozen=True, slots=True)
class LmebProfileMetrics:
    """Evaluation metrics for a specific embedding profile on LMEB."""

    profile_name: str
    provider: str
    model: str
    dims: Optional[int]
    ndcg_10: float
    recall_10: float
    recall_3: float
    mrr: float
    hit_rate_10: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "LmebProfileMetrics":
        return cls(
            profile_name=str(data["profile_name"]),
            provider=str(data.get("provider", "unknown")),
            model=str(data.get("model", "unknown")),
            dims=(int(data["dims"]) if data.get("dims") not in (None, "") else None),
            ndcg_10=float(data.get("ndcg_10", 0.0)),
            recall_10=float(data.get("recall_10", 0.0)),
            recall_3=float(data.get("recall_3", 0.0)),
            mrr=float(data.get("mrr", 0.0)),
            hit_rate_10=float(data.get("hit_rate_10", 0.0)),
        )


@dataclass
class LmebComparisonReport:
    """Side-by-side embedding comparison report across multiple profiles."""

    dataset_name: str
    profiles: list[LmebProfileMetrics]
    disclaimer: str = DISCLAIMER_TEXT

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset_name": self.dataset_name,
            "disclaimer": self.disclaimer,
            "profiles": [p.to_dict() for p in self.profiles],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "LmebComparisonReport":
        return cls(
            dataset_name=str(data["dataset_name"]),
            profiles=[
                LmebProfileMetrics.from_dict(profile)
                for profile in data.get("profiles", [])
                if isinstance(profile, Mapping)
            ],
            disclaimer=str(data.get("disclaimer") or DISCLAIMER_TEXT),
        )

    def to_markdown(self) -> str:
        """Render side-by-side comparison table in Markdown."""
        lines = [
            "# 🔬 LMEB 对话记忆组件评测与 Profile 对比报告",
            "",
            "> 针对不同 Embedding Provider、模型选型与维度的长上下文记忆召回表现进行同口径客观度量。",
            "",
            f"{self.disclaimer}",
            "",
            "## 1. Profile 并列对比矩阵 (Side-by-Side Comparison)",
            "",
            "| Profile 名称 | Provider / Model | 维度 (Dims) | nDCG@10 (主口径) | Recall@10 | MRR | Recall@3 (参考) |",
            "| :--- | :--- | :---: | :---: | :---: | :---: | :---: |",
        ]

        # Sort profiles by nDCG@10 descending
        sorted_profiles = sorted(self.profiles, key=lambda p: p.ndcg_10, reverse=True)
        for p in sorted_profiles:
            dim_str = str(p.dims) if p.dims is not None else "-"
            lines.append(
                f"| **{p.profile_name}** | `{p.provider}` / `{p.model}` | {dim_str} | "
                f"**{p.ndcg_10:.4f}** | {p.recall_10:.4f} | {p.mrr:.4f} | {p.recall_3:.4f} |"
            )

        lines.extend([
            "",
            "## 2. 选型建议与分析规则 (Selection Guidelines)",
            "",
            "1. **nDCG@10 优先**：若两个模型 Recall@10 相近，优先选用 nDCG@10 更高（即正例排在前位）的模型，以减轻下游 Reader 的上下文噪声；",
            "2. **维度与吞吐平衡**：低维度模型（如 MRL 裁剪到 512 或 768 维）若 nDCG 下降小于 0.015，推荐用于大规模场景以节约 50% 向量存储与索引开销；",
            "3. **安全复验要求**：选定新 Embedding 模型后，必须在 `hippo_gold_v1` 上完整重放四大安全硬门禁，确认 zero leakage 后方可更新生产配置。",
        ])

        return "\n".join(lines)


class LmebEvaluator:
    """Evaluates one or multiple embedding adapters on LMEB benchmark."""

    def evaluate_profile(
        self,
        adapter: BenchmarkAdapter,
        dataset: BenchmarkDataset,
        profile_name: str = "default",
        k_values: Sequence[int] = (1, 3, 5, 10),
    ) -> LmebProfileMetrics:
        """Run LMEB evaluation on a single adapter."""
        profile_meta = adapter.get_embedding_profile()
        query_results: list[QueryEvaluationResult] = []

        for q in dataset.queries:
            retrieved_ids, trace = adapter.search(q, limit=10, capture_trace=True)
            q_qrels = dataset.qrels.get(q.query_id, {})

            single_metrics = evaluate_single_query(
                query=q,
                retrieved_ids=retrieved_ids,
                qrels=q_qrels,
                forbidden_ids=[],
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
                    forbidden_ids=[],
                    metrics=single_metrics,
                    expected_empty=q.expected_empty,
                    scope=q.scope,
                    project_id=q.project_id,
                    user_id=q.user_id,
                    trace=trace,
                )
            )

        aggregate = aggregate_metrics(query_results)

        return LmebProfileMetrics(
            profile_name=profile_name,
            provider=str(profile_meta.get("provider", "unknown")),
            model=str(profile_meta.get("model", "unknown")),
            dims=profile_meta.get("dims"),
            ndcg_10=aggregate.get("ndcg@10", 0.0),
            recall_10=aggregate.get("recall@10", 0.0),
            recall_3=aggregate.get("recall@3", 0.0),
            mrr=aggregate.get("mrr", 0.0),
            hit_rate_10=aggregate.get("hit_rate@10", 0.0),
        )

    def compare_profiles(
        self,
        adapters_by_name: Mapping[str, BenchmarkAdapter],
        dataset: BenchmarkDataset,
    ) -> LmebComparisonReport:
        """Run side-by-side evaluation across multiple adapters/profiles."""
        profiles: list[LmebProfileMetrics] = []
        for name, adapter in adapters_by_name.items():
            metrics = self.evaluate_profile(adapter, dataset, profile_name=name)
            profiles.append(metrics)

        return LmebComparisonReport(
            dataset_name=dataset.name,
            profiles=profiles,
        )


def merge_lmeb_reports(
    reports: Sequence[LmebComparisonReport],
) -> LmebComparisonReport:
    """Merge independently executed LMEB profile reports into one comparison.

    Embedding providers often require different credentials and environment
    configuration, so profile runs are intentionally isolated. This merger is
    the supported way to create a true side-by-side report without conflating
    ingestion profiles with embedding profiles.
    """
    if len(reports) < 2:
        raise ValueError("LMEB comparison requires at least two profile reports")

    dataset_names = {report.dataset_name for report in reports}
    if len(dataset_names) != 1:
        raise ValueError(
            "LMEB profile reports must use the same dataset: "
            + ", ".join(sorted(dataset_names))
        )

    profiles_by_name: dict[str, LmebProfileMetrics] = {}
    for report in reports:
        for profile in report.profiles:
            existing = profiles_by_name.get(profile.profile_name)
            if existing is not None and existing != profile:
                raise ValueError(
                    f"Conflicting LMEB profile results for {profile.profile_name!r}"
                )
            profiles_by_name[profile.profile_name] = profile

    if len(profiles_by_name) < 2:
        raise ValueError("LMEB comparison requires at least two distinct profiles")

    return LmebComparisonReport(
        dataset_name=next(iter(dataset_names)),
        profiles=list(profiles_by_name.values()),
    )
