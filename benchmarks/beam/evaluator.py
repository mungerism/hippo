"""Scale evaluation metrics, latency quantile tracking, and performance profiling for BEAM.

Measures:
- Ingest duration and throughput (items/sec).
- Query latency distribution (P50, P95, P99).
- Vector index size footprint.
- Estimated tokens and resource cost.
- Retrieval metrics breakdown across query categories.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import logging
import math
import sys
import time
from typing import Any, Mapping, Optional, Sequence

from benchmarks.adapter import BenchmarkAdapter
from benchmarks.metrics import (
    aggregate_by_category,
    aggregate_metrics,
    evaluate_single_query,
)
from benchmarks.schemas import (
    BenchmarkDataset,
    EvaluationQuery,
    QueryEvaluationResult,
)

logger = logging.getLogger(__name__)

# Price per million tokens for standard vector embeddings (e.g. OpenAI text-embedding-3-small or Gemini embed)
DEFAULT_EMBEDDING_COST_PER_1M_TOKENS = 0.020


def estimate_tokens_from_text(text: str) -> int:
    """Rough heuristic token estimator: ~4 characters per token for English/code."""
    if not text:
        return 0
    return max(1, math.ceil(len(text) / 4.0))


def _get_peak_rss_bytes() -> Optional[int]:
    """Return process peak resident memory in bytes when the platform exposes it."""
    try:
        import resource

        peak = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        # macOS reports bytes; Linux and the common BSD CI environments report KiB.
        return peak if sys.platform == "darwin" else peak * 1024
    except (ImportError, OSError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class BeamPerformanceProfile:
    """System resource, timing, and capacity profile under scale evaluation."""

    ingest_duration_seconds: float
    ingest_throughput_items_per_sec: float
    query_latency_p50_ms: float
    query_latency_p95_ms: float
    query_latency_p99_ms: float
    index_size_bytes: Optional[int]
    peak_rss_bytes: Optional[int]
    total_memories: int
    total_queries: int
    estimated_corpus_tokens: int
    estimated_cost_usd: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BeamEvaluationResult:
    """Full evaluation and performance profiling result for BEAM benchmark."""

    retrieval_metrics: dict[str, float]
    category_metrics: dict[str, dict[str, float]]
    performance: BeamPerformanceProfile
    query_results: list[QueryEvaluationResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "retrieval_metrics": self.retrieval_metrics,
            "category_metrics": self.category_metrics,
            "performance": self.performance.to_dict(),
            "query_results": [q.to_dict() for q in self.query_results],
        }

    def to_markdown(self) -> str:
        """Render comprehensive Markdown summary with scale and latency tables."""
        lines = [
            "# 📈 BEAM Scale & Performance Evaluation Report",
            "",
            f"- **Corpus Memories**: `{self.performance.total_memories:,}`",
            f"- **Evaluation Queries**: `{self.performance.total_queries}`",
            f"- **Estimated Corpus Tokens**: `~{self.performance.estimated_corpus_tokens:,}`",
            f"- **Estimated Cost**: `${self.performance.estimated_cost_usd:.4f}` USD",
            "",
            "## 1. 规模性能表现 (Performance & Scale Profile)",
            "",
            "| 性能度量指标 (Metric) | 测量值 (Value) | 行业参考基准 (Reference) |",
            "| :--- | :---: | :---: |",
            f"| **写入耗时 (Ingest Duration)** | `{self.performance.ingest_duration_seconds:.2f} s` | 批量异步入库 |",
            f"| **写入吞吐 (Ingest Throughput)** | `{self.performance.ingest_throughput_items_per_sec:.1f} items/s` | `> 50 items/s` |",
            f"| **查询延迟 P50 (Query Latency P50)** | `{self.performance.query_latency_p50_ms:.2f} ms` | `< 15.0 ms` |",
            f"| **查询延迟 P95 (Query Latency P95)** | `{self.performance.query_latency_p95_ms:.2f} ms` | `< 50.0 ms` |",
            f"| **查询延迟 P99 (Query Latency P99)** | `{self.performance.query_latency_p99_ms:.2f} ms` | `< 100.0 ms` |",
        ]

        if self.performance.index_size_bytes is not None:
            size_kb = self.performance.index_size_bytes / 1024.0
            size_mb = size_kb / 1024.0
            size_str = f"{size_mb:.2f} MB" if size_mb >= 1.0 else f"{size_kb:.1f} KB"
            lines.append(f"| **索引体积 (Index Size)** | `{size_str}` | 单二进制紧凑存储 |")

        if self.performance.peak_rss_bytes is not None:
            rss_mb = self.performance.peak_rss_bytes / (1024.0 * 1024.0)
            lines.append(
                f"| **进程峰值 RSS (Peak RSS)** | `{rss_mb:.1f} MB` | 运行资源诊断 |"
            )

        lines.extend([
            "",
            "## 2. 核心召回质量指标 (@3 主口径 / @10 扩展口径)",
            "",
            "| 指标 (Metric) | 得分 (Score) | 目标规范 (Goal) |",
            "| :--- | :---: | :---: |",
            f"| **Recall@3** (Hippo 主口径) | `{self.retrieval_metrics.get('recall@3', 0.0):.4f}` | `≥ 0.8000` |",
            f"| **Recall@10** (BEAM 比较口径) | `{self.retrieval_metrics.get('recall@10', 0.0):.4f}` | `≥ 0.9000` |",
            f"| **Precision@3** | `{self.retrieval_metrics.get('precision@3', 0.0):.4f}` | 高纯度无噪点 |",
            f"| **MRR** | `{self.retrieval_metrics.get('mrr', 0.0):.4f}` | 首位精准排序 |",
            f"| **nDCG@10** | `{self.retrieval_metrics.get('ndcg@10', 0.0):.4f}` | 全局分级增益 |",
            f"| **Forbidden Leakage@3** | `{int(self.retrieval_metrics.get('forbidden_leakage@3', 0))}` | **必须恒等于 0** |",
            f"| **Empty Accuracy@3** | `{self.retrieval_metrics.get('empty_accuracy@3', 0.0):.4f}` | 硬负样本置空 |",
            "",
            "## 3. 能力分桶明细 (Capability Breakdown)",
            "",
            "| 任务类别 (Category) | 题数 | Recall@3 | Recall@10 | nDCG@10 | MRR |",
            "| :--- | :---: | :---: | :---: | :---: | :---: |",
        ])

        for cat, cat_m in sorted(self.category_metrics.items()):
            r3 = cat_m.get("recall@3", 0.0)
            r10 = cat_m.get("recall@10", 0.0)
            ndcg10 = cat_m.get("ndcg@10", 0.0)
            mrr = cat_m.get("mrr", 0.0)
            # Count queries in category
            cat_count = sum(1 for q in self.query_results if q.category == cat)
            lines.append(
                f"| `{cat}` | {cat_count} | {r3:.4f} | {r10:.4f} | {ndcg10:.4f} | {mrr:.4f} |"
            )

        return "\n".join(lines)


class BeamEvaluator:
    """Evaluates long-horizon agent memory datasets with latency and capacity profiling."""

    def __init__(
        self,
        cost_per_million_tokens: float = DEFAULT_EMBEDDING_COST_PER_1M_TOKENS,
    ):
        self.cost_per_million_tokens = cost_per_million_tokens

    def evaluate(
        self,
        adapter: BenchmarkAdapter,
        dataset: BenchmarkDataset,
        limit: int = 3,
        k_values: Sequence[int] = (1, 3, 5, 10),
        ingest_duration_seconds: float = 0.0,
    ) -> BeamEvaluationResult:
        """Run scale evaluation over BEAM dataset, tracking query latency and memory footprint."""
        query_results: list[QueryEvaluationResult] = []
        latencies_ms: list[float] = []
        # Retrieval depth must cover every reported @K metric. The production
        # injection budget (normally 3) is a separate contract and must not
        # silently truncate Recall@10 / nDCG@10.
        search_limit = max([int(limit)] + [int(k) for k in k_values])

        for q in dataset.queries:
            t0 = time.perf_counter()
            retrieved_ids, trace = adapter.search(
                q, limit=search_limit, capture_trace=True
            )
            latency_ms = (time.perf_counter() - t0) * 1000.0
            latencies_ms.append(latency_ms)

            q_qrels = dataset.qrels.get(q.query_id, {})
            q_forbidden = dataset.forbidden.get(q.query_id, [])

            single_metrics = evaluate_single_query(
                query=q,
                retrieved_ids=retrieved_ids,
                qrels=q_qrels,
                forbidden_ids=q_forbidden,
                k_values=k_values,
            )
            single_metrics["latency_ms"] = round(latency_ms, 2)

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

        # Aggregate metrics
        aggregate = aggregate_metrics(query_results)
        category_breakdown = aggregate_by_category(query_results)

        # Compute latency percentiles
        sorted_latencies = sorted(latencies_ms) if latencies_ms else [0.0]
        n_lat = len(sorted_latencies)

        def _percentile(p: float) -> float:
            idx = int(math.ceil(p * n_lat)) - 1
            return round(sorted_latencies[max(0, min(n_lat - 1, idx))], 2)

        p50 = _percentile(0.50)
        p95 = _percentile(0.95)
        p99 = _percentile(0.99)

        # Throughput
        num_corpus = len(dataset.corpus)
        effective_ingest_time = max(0.001, ingest_duration_seconds)
        throughput = round(num_corpus / effective_ingest_time, 2)

        # Index size
        index_size = adapter.get_index_size_bytes()

        # Token & cost estimations
        total_tokens = sum(estimate_tokens_from_text(c.text) for c in dataset.corpus)
        estimated_cost = round((total_tokens / 1_000_000.0) * self.cost_per_million_tokens, 6)

        perf = BeamPerformanceProfile(
            ingest_duration_seconds=round(ingest_duration_seconds, 3),
            ingest_throughput_items_per_sec=throughput,
            query_latency_p50_ms=p50,
            query_latency_p95_ms=p95,
            query_latency_p99_ms=p99,
            index_size_bytes=index_size,
            peak_rss_bytes=_get_peak_rss_bytes(),
            total_memories=num_corpus,
            total_queries=len(dataset.queries),
            estimated_corpus_tokens=total_tokens,
            estimated_cost_usd=estimated_cost,
        )

        return BeamEvaluationResult(
            retrieval_metrics=aggregate,
            category_metrics=category_breakdown,
            performance=perf,
            query_results=query_results,
        )
