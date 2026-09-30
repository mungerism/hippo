"""Benchmark runner, report generation, and baseline comparison.

Orchestrates evaluation runs across datasets and adapters, produces deterministic
JSON and Markdown reports, and computes diffs against approved baselines.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.metadata
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
import uuid

from benchmarks.adapter import (
    BenchmarkAdapter,
    HippoEngineAdapter,
    ReplayFixtureAdapter,
)
from benchmarks.gold.specification import (
    GoldScenario,
    SecurityGateThresholds,
    audit_security_gates,
)
from benchmarks.beam import (
    BUILTIN_BEAM_FIXTURE_PATH,
    DEFAULT_EMBEDDING_COST_PER_1M_TOKENS,
    OFFICIAL_BEAM_DATASET_ID,
    OFFICIAL_BEAM_DATASET_REVISION,
    OFFICIAL_BEAM_PROTOCOL_REVISION,
    OFFICIAL_BEAM_SPLIT,
    BeamEvaluationResult,
    BeamEvaluator,
    convert_to_beam_benchmark,
    estimate_tokens_from_text,
    load_beam_dataset,
    load_official_beam_dataset,
)
from benchmarks.lmeb import (
    BUILTIN_LMEB_FIXTURE_PATH,
    OFFICIAL_LMEB_DATASET_ID,
    OFFICIAL_LMEB_DATASET_REVISION,
    OFFICIAL_LMEB_FAMILY,
    OFFICIAL_LMEB_SPLIT,
    LmebComparisonReport,
    LmebEvaluator,
    convert_to_lmeb_benchmark,
    load_lmeb_dataset,
    load_official_lmeb_dataset,
    merge_lmeb_reports,
)
from benchmarks.locomo import (
    GeminiLoCoMoReader,
    LoCoMoEvaluationResult,
    LoCoMoEvaluator,
    OFFICIAL_DATASET_REVISION as LOCOMO_OFFICIAL_DATASET_REVISION,
    OFFICIAL_DATASET_SHA256 as LOCOMO_OFFICIAL_DATASET_SHA256,
    OFFICIAL_DATA_URL as LOCOMO_OFFICIAL_DATA_URL,
    RuleBasedMockLoCoMoReader,
    convert_to_locomo_benchmark,
    ensure_official_scorer_available,
    load_locomo_fixture_samples,
    load_locomo_samples,
)
from benchmarks.longmemeval import (
    BUILTIN_FIXTURE_PATH,
    OFFICIAL_DATASET_REVISION,
    OFFICIAL_DATASET_SHA256,
    OFFICIAL_DATA_URL,
    GeminiJudge,
    GeminiReader,
    LongMemEvalEvaluator,
    LossQuantification,
    RuleBasedJudge,
    RuleBasedMockReader,
    convert_to_direct_facts_benchmark,
    convert_to_sessions_benchmark,
    export_official_hypotheses,
    load_longmemeval_fixture_items,
    load_longmemeval_items,
    resolve_ingest_strategy,
)
from benchmarks.metrics import (
    aggregate_by_category,
    aggregate_metrics,
    evaluate_single_query,
)
from benchmarks.schemas import (
    BenchmarkDataset,
    BenchmarkReport,
    CorpusItem,
    EvaluationQuery,
    QueryEvaluationResult,
    RunManifest,
)
from hippo_memory.gate import SearchGateConfig

logger = logging.getLogger(__name__)


def get_git_sha() -> str:
    """Retrieve current Git commit SHA with dirty flag if uncommitted changes exist."""
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True
        ).strip()
        status = subprocess.check_output(
            ["git", "status", "--porcelain"], stderr=subprocess.DEVNULL, text=True
        ).strip()
        return f"{sha}-dirty" if status else sha
    except Exception:
        return "unknown"


def get_mem0_version() -> str:
    """Detect installed mem0 / mem0ai version."""
    for pkg in ("mem0ai", "mem0"):
        try:
            return importlib.metadata.version(pkg)
        except Exception:
            continue
    return "unknown"


def get_hippo_version() -> str:
    """Detect hippo package version."""
    try:
        return importlib.metadata.version("hippo")
    except Exception:
        return "0.1.0"


def create_smoke_fixture_dataset() -> BenchmarkDataset:
    """Create a minimal built-in deterministic fixture dataset for smoke tests."""
    corpus = [
        CorpusItem(
            id="mem_1",
            text="Python 3.12 is the primary development environment for Hippo.",
            scope="project",
            project_id="hippo",
            user_id="alice",
            status="active",
            category="fact",
        ),
        CorpusItem(
            id="mem_2",
            text="Alice prefers dark theme in VS Code and all IDE editors.",
            scope="global",
            project_id=None,
            user_id="alice",
            status="active",
            category="preference",
        ),
        CorpusItem(
            id="mem_3_old",
            text="Hippo uses Qdrant legacy collection on port 6334.",
            scope="project",
            project_id="hippo",
            user_id="alice",
            status="superseded",
            category="fact",
        ),
        CorpusItem(
            id="mem_4_other",
            text="Bob's private secret token for repository zebra.",
            scope="project",
            project_id="zebra",
            user_id="bob",
            status="active",
            category="confidential",
        ),
        CorpusItem(
            id="mem_5_gate_weak",
            text="Some completely irrelevant text about culinary recipes and baking sourdough.",
            scope="project",
            project_id="hippo",
            user_id="alice",
            status="active",
            category="noise",
        ),
    ]

    queries = [
        EvaluationQuery(
            query_id="q1_python",
            query="Which Python version does Hippo develop with?",
            scope="project",
            project_id="hippo",
            user_id="alice",
            expected_empty=False,
            category="fact",
        ),
        EvaluationQuery(
            query_id="q2_theme",
            query="What IDE color theme does user Alice prefer?",
            scope="all",
            project_id="hippo",
            user_id="alice",
            expected_empty=False,
            category="preference",
        ),
        EvaluationQuery(
            query_id="q3_adversarial",
            query="What is the legacy superseded collection port for Hippo?",
            scope="project",
            project_id="hippo",
            user_id="alice",
            expected_empty=True,  # mem_3_old is superseded, must NOT be returned!
            category="superseded",
        ),
        EvaluationQuery(
            query_id="q4_negative",
            query="What are the best ingredients for baking sourdough bread?",
            scope="project",
            project_id="hippo",
            user_id="alice",
            expected_empty=True,  # mem_5_gate_weak should be blocked by relevance gate!
            category="noise",
        ),
    ]

    qrels = {
        "q1_python": {"mem_1": 2},
        "q2_theme": {"mem_2": 2},
        "q3_adversarial": {},  # No valid active memory
        "q4_negative": {},
    }

    forbidden = {
        "q1_python": ["mem_4_other"],
        "q2_theme": ["mem_4_other"],
        "q3_adversarial": ["mem_3_old", "mem_4_other"],
        "q4_negative": ["mem_4_other"],
    }

    return BenchmarkDataset(
        name="hippo_smoke_fixture",
        version="1.0.0",
        description="Built-in deterministic smoke dataset for gate, lifecycle, and metrics verification",
        corpus=corpus,
        queries=queries,
        qrels=qrels,
        forbidden=forbidden,
    )


class BenchmarkRunner:
    """Executes benchmark datasets against an adapter and produces full reports."""

    def __init__(
        self,
        adapter: BenchmarkAdapter,
        k_values: Sequence[int] = (1, 3, 5, 10),
        max_injected: int = 3,
        seed: Optional[int] = 42,
        ingest_profile: str = "direct-facts",
    ):
        self.adapter = adapter
        self.k_values = [int(k) for k in k_values]
        self.max_injected = max_injected
        self.seed = seed
        self.ingest_profile = ingest_profile
        self.evaluation_depth = max([self.max_injected] + self.k_values)
        self.last_ingest_duration_seconds: float = 0.0

    def run(
        self,
        dataset: BenchmarkDataset,
        *,
        capture_trace: bool = True,
        extra_manifest: Optional[Dict[str, Any]] = None,
    ) -> BenchmarkReport:
        """Run the complete evaluation and produce a BenchmarkReport."""
        start_time = time.perf_counter()
        timestamp = datetime.now(timezone.utc).isoformat()

        # Ingest corpus into isolated adapter using configured profile
        t_ingest_start = time.perf_counter()
        strategy = resolve_ingest_strategy(self.ingest_profile)
        strategy.ingest(self.adapter, dataset.corpus)
        self.last_ingest_duration_seconds = round(time.perf_counter() - t_ingest_start, 4)

        query_results: List[QueryEvaluationResult] = []
        latencies_ms: List[float] = []
        cand_recalls_20: List[float] = []
        total_injected_tokens = 0

        for q in dataset.queries:
            q_start = time.perf_counter()
            retrieved_ids, trace = self.adapter.search(
                query=q,
                limit=self.evaluation_depth,
                capture_trace=capture_trace,
            )
            q_latency = round((time.perf_counter() - q_start) * 1000.0, 3)
            latencies_ms.append(q_latency)

            qrels_for_q = dataset.qrels.get(q.query_id, {})
            forbidden_for_q = dataset.forbidden.get(q.query_id, [])

            metrics_dict = evaluate_single_query(
                query=q,
                retrieved_ids=retrieved_ids,
                qrels=qrels_for_q,
                forbidden_ids=forbidden_for_q,
                k_values=self.k_values,
            )

            # Calculate Candidate Recall@20 from Stage 1 trace if available
            relevant_ids = [cid for cid, grade in qrels_for_q.items() if grade > 0]
            if trace is not None and trace.candidate_stage and relevant_ids:
                top20_cand_ids = {c.id for c in trace.candidate_stage[:20]}
                cand_hits = sum(1 for rid in relevant_ids if rid in top20_cand_ids)
                cand_rec = cand_hits / float(len(relevant_ids))
                cand_recalls_20.append(cand_rec)
                metrics_dict["candidate_recall@20"] = round(cand_rec, 4)

            # Estimate injected tokens (roughly 1 token per 4 chars)
            injected_ids = retrieved_ids[: self.max_injected]
            if injected_ids:
                retrieved_chars = sum(
                    len(c.text) for c in dataset.corpus if c.id in injected_ids
                )
                total_injected_tokens += max(1, retrieved_chars // 4)

            query_results.append(
                QueryEvaluationResult(
                    query_id=q.query_id,
                    query=q.query,
                    category=q.category,
                    retrieved_ids=retrieved_ids,
                    relevant_ids=relevant_ids,
                    forbidden_ids=forbidden_for_q,
                    metrics=metrics_dict,
                    expected_empty=q.expected_empty,
                    scope=q.scope,
                    project_id=q.project_id,
                    user_id=q.user_id,
                    trace=trace,
                )
            )

        duration = round(time.perf_counter() - start_time, 4)

        # Aggregate retrieval metrics only over the retrieval-scored contract.
        # Persistence-quality diagnostics intentionally remain in category_metrics
        # but do not penalize direct-facts retrieval, which bypasses ingest policy.
        primary_results = [
            result
            for result in query_results
            if result.category != GoldScenario.PERSISTENCE_QUALITY.value
        ]
        agg_metrics = aggregate_metrics(primary_results)
        agg_metrics["primary_query_count"] = float(len(primary_results))
        agg_metrics["persistence_quality_query_count"] = float(
            len(query_results) - len(primary_results)
        )
        cat_metrics = aggregate_by_category(query_results)

        # Append auxiliary latency and candidate recall metrics
        if cand_recalls_20:
            agg_metrics["candidate_recall@20"] = round(sum(cand_recalls_20) / len(cand_recalls_20), 4)

        if latencies_ms:
            sorted_lat = sorted(latencies_ms)
            p50_idx = int(len(sorted_lat) * 0.50)
            p95_idx = min(len(sorted_lat) - 1, int(len(sorted_lat) * 0.95))
            agg_metrics["latency_p50_ms"] = round(sorted_lat[p50_idx], 2)
            agg_metrics["latency_p95_ms"] = round(sorted_lat[p95_idx], 2)
            agg_metrics["latency_avg_ms"] = round(sum(sorted_lat) / len(sorted_lat), 2)

        if query_results:
            agg_metrics["avg_injected_tokens"] = round(total_injected_tokens / float(len(query_results)), 2)

        if hasattr(self.adapter, "get_index_size_bytes"):
            measured_index_size = self.adapter.get_index_size_bytes()
            if measured_index_size is not None:
                agg_metrics["index_size_bytes"] = float(measured_index_size)

        agg_metrics["ingest_duration_seconds"] = self.last_ingest_duration_seconds

        # Build manifest
        gate_cfg = getattr(self.adapter, "gate_config", SearchGateConfig())
        gate_thresholds = {
            "final_threshold": getattr(gate_cfg, "final_threshold", 0.32),
            "dense_only_threshold": getattr(gate_cfg, "dense_only_threshold", 0.70),
            "relative_threshold_ratio": getattr(gate_cfg, "relative_threshold_ratio", 0.50),
            "lexical_min_coverage": getattr(gate_cfg, "lexical_min_coverage", 0.35),
            "lexical_bm25_threshold": getattr(gate_cfg, "lexical_bm25_threshold", 0.15),
            "lexical_semantic_threshold": getattr(
                gate_cfg, "lexical_semantic_threshold", 0.48
            ),
            "enabled": getattr(gate_cfg, "enabled", True),
        }

        run_id = f"eval_{dataset.name}_{int(time.time())}"
        embedding_profile = (
            self.adapter.get_embedding_profile()
            if hasattr(self.adapter, "get_embedding_profile")
            else {
                "provider": "unknown",
                "model": "unknown",
                "dims": 0,
                "collection": getattr(self.adapter, "collection_name", "replay_memory"),
            }
        )

        index_size_bytes = None
        if hasattr(self.adapter, "get_index_size_bytes"):
            index_size_bytes = self.adapter.get_index_size_bytes()

        manifest = RunManifest(
            run_id=run_id,
            timestamp=timestamp,
            git_sha=get_git_sha(),
            dataset_name=dataset.name,
            dataset_hash=dataset.compute_hash(),
            mem0_version=get_mem0_version(),
            hippo_version=get_hippo_version(),
            embedding_profile=embedding_profile,
            gate_thresholds=gate_thresholds,
            max_injected=self.max_injected,
            k_values=self.k_values,
            seed=self.seed,
            duration_seconds=duration,
            adapter=type(self.adapter).__name__,
            ingest_profile=self.ingest_profile,
            index_size_bytes=index_size_bytes,
            benchmark_config={
                "evaluation_depth": self.evaluation_depth,
                **(extra_manifest or {}),
            },
            host_info={"platform": sys.platform, "python_version": sys.version.split()[0]},
        )

        return BenchmarkReport(
            manifest=manifest,
            aggregate_metrics=agg_metrics,
            category_metrics=cat_metrics,
            query_results=query_results,
        )


def generate_markdown_report(report: BenchmarkReport) -> str:
    """Format a BenchmarkReport into a clean, comprehensive Markdown summary."""
    m = report.manifest
    agg = report.aggregate_metrics

    lines: List[str] = [
        f"# Hippo 召回评测报告: `{m.dataset_name}`",
        "",
        f"- **运行 ID**: `{m.run_id}`",
        f"- **时间戳**: `{m.timestamp}`",
        f"- **Git SHA**: `{m.git_sha}`",
        f"- **数据集 Hash**: `{m.dataset_hash[:16]}...`",
        f"- **Mem0 版本**: `{m.mem0_version}` | **Hippo 版本**: `{m.hippo_version}`",
        f"- **Adapter / Ingest**: `{m.adapter}` / `{m.ingest_profile}`",
        f"- **评测耗时**: `{m.duration_seconds}s` (共 {len(report.query_results)} 道题目)",
        f"- **主检索口径 / Persistence 诊断**: `{int(agg.get('primary_query_count', len(report.query_results)))} / {int(agg.get('persistence_quality_query_count', 0))}`",
        "",
        "## 0. 安全硬门禁审查 (Security Hard Gates)",
        "",
    ]

    is_strict_gold = "gold" in m.dataset_name.lower()
    gate_thresh = (
        None
        if is_strict_gold
        else SecurityGateThresholds(
            min_total_queries=0,
            min_hard_negative_ratio=0.0,
            max_hard_negative_fpr=1.0,
        )
    )
    gate_audit = audit_security_gates(report, thresholds=gate_thresh)
    audit_icon = "✅ 全部通过 (PASSED)" if gate_audit["passed"] else "❌ 门禁违规 (FAILED)"
    lines.extend([
        f"- **门禁判定**: **{audit_icon}**",
        f"- **检索硬负样本数与占比**: `{gate_audit['negative_queries_count']}/{gate_audit['retrieval_queries_count']}` (`{gate_audit['negative_ratio']:.2%}`, 最低要求 >= 25%) ",
        "",
        "| 安全硬门禁不变量 | 测量值 | 门禁阈值 | 判定 |",
        "| :--- | :---: | :---: | :---: |",
        f"| **Cross-User Leakage** (跨用户泄漏) | **{gate_audit['cross_user_leakage']}** | 0 | {'✅ 合规' if gate_audit['cross_user_leakage'] == 0 else '❌ 违规'} |",
        f"| **Cross-Project Leakage** (跨项目泄漏) | **{gate_audit['cross_project_leakage']}** | 0 | {'✅ 合规' if gate_audit['cross_project_leakage'] == 0 else '❌ 违规'} |",
        f"| **Superseded Leakage** (过期事实泄漏) | **{gate_audit['superseded_leakage']}** | 0 | {'✅ 合规' if gate_audit['superseded_leakage'] == 0 else '❌ 违规'} |",
        (
            f"| **Hard-Negative FPR** (硬负样本假阳率) | **{gate_audit['hard_negative_fpr']:.2%}** | <= 2.00% | {'✅ 合规' if gate_audit['hard_negative_fpr'] <= 0.02 else '❌ 违规'} |"
            if is_strict_gold
            else f"| **Hard-Negative FPR** (硬负样本假阳率) | **{gate_audit['hard_negative_fpr']:.2%}** | N/A (外部基准) | ⚪ 参考指标 |"
        ),
        "",
        "## 1. 核心召回与安全指标汇总 (Primary Metrics)",
        "",
        "> 注：`@3` 为 Hippo 生产默认主报告口径（对齐 `max_injected=3`）。",
        "",
        "| 指标 | @1 | @3 (Hippo 主口径) | @5 | @10 |",
        "| :--- | :---: | :---: | :---: | :---: |",
    ])

    for metric_name, label in [
        ("recall", "Recall (召回率)"),
        ("precision", "Precision (精准度)"),
        ("hit_rate", "Hit Rate (命中率)"),
        ("ndcg", "nDCG (排序得分)"),
        ("forbidden_leakage", "Forbidden Leakage (安全泄漏数)"),
        ("empty_accuracy", "Empty Accuracy (无答案置空率)"),
    ]:
        v1 = agg.get(f"{metric_name}@1", 0.0)
        v3 = agg.get(f"{metric_name}@3", 0.0)
        v5 = agg.get(f"{metric_name}@5", 0.0)
        v10 = agg.get(f"{metric_name}@10", 0.0)
        lines.append(f"| **{label}** | {v1:.4f} | **{v3:.4f}** | {v5:.4f} | {v10:.4f} |")

    cand_rec = agg.get("candidate_recall@20")
    if cand_rec is not None:
        lines.append(f"| **Candidate Recall@20** (粗筛上限) | - | - | - | **{cand_rec:.4f}** |")

    mrr_val = agg.get("mrr", 0.0)
    lines.append(f"| **MRR (平均倒数排名)** | - | **{mrr_val:.4f}** | - | - |")

    # Add performance latency metrics if present
    p50 = agg.get("latency_p50_ms")
    p95 = agg.get("latency_p95_ms")
    avg_tokens = agg.get("avg_injected_tokens")
    if p50 is not None and p95 is not None:
        lines.extend([
            "",
            "> **性能与吞吐概览**：",
            f"> - P50 延迟: `{p50} ms` | P95 延迟: `{p95} ms` | 平均注入: `{avg_tokens or 0} tokens`",
            (
                f"> - 索引体积: `{int(agg['index_size_bytes'])} bytes`"
                if agg.get("index_size_bytes") is not None
                else "> - 索引体积: `unavailable`（当前向量后端未暴露可复现的磁盘字节数）"
            ),
        ])

    lines.extend([
        "",
        "## 2. 分类能力评估 (Category Breakdown @3)",
        "",
        "| 分类 (Category) | 题数 | Recall@3 | Precision@3 | Hit@3 | nDCG@3 | Leakage@3 | Empty Acc@3 |",
        "| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |",
    ])

    # Count by category
    cat_counts: Dict[str, int] = {}
    for qr in report.query_results:
        cat_counts[qr.category] = cat_counts.get(qr.category, 0) + 1

    for cat, cat_m in sorted(report.category_metrics.items()):
        cnt = cat_counts.get(cat, 0)
        r3 = cat_m.get("recall@3", 0.0)
        p3 = cat_m.get("precision@3", 0.0)
        h3 = cat_m.get("hit_rate@3", 0.0)
        n3 = cat_m.get("ndcg@3", 0.0)
        l3 = cat_m.get("forbidden_leakage@3", 0.0)
        e3 = cat_m.get("empty_accuracy@3")
        e3_display = f"{e3:.4f}" if e3 is not None else "-"
        lines.append(
            f"| `{cat}` | {cnt} | {r3:.4f} | {p3:.4f} | {h3:.4f} | {n3:.4f} | {l3:.2f} | {e3_display} |"
        )

    lines.extend([
        "",
        "## 3. 逐题搜索阶段与诊断 (Pipeline Stages Summary)",
        "",
        "| Query ID | 类别 | 候选阶段 | Lifecycle通过/拒绝 | Gate通过/拒绝 | 最终Top-N | R@3 | Leakage@3 |",
        "| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: |",
    ])

    for qr in report.query_results:
        t = qr.trace
        if t is not None:
            cand_cnt = len(t.candidate_stage)
            life_p = len(t.lifecycle_scope_stage.passed_ids)
            life_r = len(t.lifecycle_scope_stage.rejected)
            gate_p = len(t.gate_stage.passed_ids)
            gate_r = len(t.gate_stage.rejected)
            final_cnt = len(t.final_stage_ids)
            life_str = f"{life_p}/{life_r}"
            gate_str = f"{gate_p}/{gate_r}"
        else:
            cand_cnt = "-"
            life_str = "-"
            gate_str = "-"
            final_cnt = len(qr.retrieved_ids)

        r3 = qr.metrics.get("recall@3", 0.0)
        l3 = int(qr.metrics.get("forbidden_leakage@3", 0.0))
        leak_display = f"**{l3}**" if l3 > 0 else "0"
        lines.append(
            f"| `{qr.query_id}` | `{qr.category}` | {cand_cnt} | {life_str} | {gate_str} | {final_cnt} | {r3:.2f} | {leak_display} |"
        )

    lines.append("")
    return "\n".join(lines)


def _embedding_signature(profile: Mapping[str, Any]) -> Dict[str, Any]:
    """Return only embedding fields that must match across comparable runs."""
    return {
        "provider": profile.get("provider"),
        "model": profile.get("model"),
        "dims": profile.get("dims"),
    }


def _validate_baseline_compatibility(
    current: BenchmarkReport,
    baseline: BenchmarkReport,
) -> None:
    """Fail fast when two reports do not describe the same evaluation contract."""
    c = current.manifest
    b = baseline.manifest
    mismatches: List[str] = []

    checks = [
        ("dataset_name", c.dataset_name, b.dataset_name),
        ("dataset_hash", c.dataset_hash, b.dataset_hash),
        ("schema_version", c.schema_version, b.schema_version),
        ("max_injected", c.max_injected, b.max_injected),
        ("k_values", list(c.k_values), list(b.k_values)),
        ("gate_thresholds", dict(c.gate_thresholds), dict(b.gate_thresholds)),
        ("adapter", c.adapter, b.adapter),
        ("ingest_profile", c.ingest_profile, b.ingest_profile),
        (
            "embedding_profile",
            _embedding_signature(c.embedding_profile),
            _embedding_signature(b.embedding_profile),
        ),
    ]
    for name, current_value, baseline_value in checks:
        if current_value != baseline_value:
            mismatches.append(
                f"{name}: current={current_value!r}, baseline={baseline_value!r}"
            )

    if mismatches:
        raise ValueError(
            "Incompatible baseline report; refusing to compare runs with different "
            "evaluation contracts: " + "; ".join(mismatches)
        )


def compare_reports(
    current: BenchmarkReport,
    baseline: BenchmarkReport,
    tolerance: float = 0.02,
    latency_p95_tolerance_ratio: float = 0.10,
) -> Dict[str, Any]:
    """Compare current evaluation report with a compatible baseline report.

    Detects:
        - Core metric regressions (absolute drop > tolerance; default 2 percentage points)
        - Forbidden leakage regressions (any increase > 0)
        - P95 latency warnings (relative increase > 10%; escalation is handled across runs)
        - Per-query regressions and improvements
    """
    _validate_baseline_compatibility(current, baseline)
    diff_metrics: Dict[str, Dict[str, Any]] = {}
    has_regression = False
    has_security_violation = False
    latency_warning = False
    latency_p95_change_ratio: Optional[float] = None

    all_keys = set(current.aggregate_metrics.keys()) | set(baseline.aggregate_metrics.keys())

    for k in sorted(all_keys):
        c_val = current.aggregate_metrics.get(k, 0.0)
        b_val = baseline.aggregate_metrics.get(k, 0.0)
        delta = round(c_val - b_val, 4)

        if "forbidden_leakage" in k:
            regressed = (c_val > 0.0) or (delta > 0.0)
            if regressed:
                has_security_violation = True
                has_regression = True
        elif k == "latency_p95_ms":
            # A single noisy shared-runner latency breach only warns. The caller
            # may persist consecutive breaches and escalate the second one.
            regressed = False
            if b_val > 0:
                latency_p95_change_ratio = (c_val - b_val) / b_val
                latency_warning = latency_p95_change_ratio > latency_p95_tolerance_ratio
        elif k in {"recall@3", "ndcg@3", "mrr"}:
            regressed = delta < -tolerance
            if regressed:
                has_regression = True
        else:
            # Other retrieval/diagnostic/performance metrics are reported but are
            # not merge-blocking under the Issue #57 regression policy.
            regressed = False

        diff_metrics[k] = {
            "baseline": b_val,
            "current": c_val,
            "delta": delta,
            "regressed": regressed,
        }

    # Compare query by query
    b_queries = {q.query_id: q for q in baseline.query_results}
    regressed_queries: List[Dict[str, Any]] = []
    improved_queries: List[Dict[str, Any]] = []

    for cq in current.query_results:
        bq = b_queries.get(cq.query_id)
        if not bq:
            continue

        c_r3 = cq.metrics.get("recall@3", 0.0)
        b_r3 = bq.metrics.get("recall@3", 0.0)
        c_leak = cq.metrics.get("forbidden_leakage@3", 0.0)
        b_leak = bq.metrics.get("forbidden_leakage@3", 0.0)

        if (c_r3 < b_r3 - tolerance) or (c_leak > b_leak):
            regressed_queries.append(
                {
                    "query_id": cq.query_id,
                    "category": cq.category,
                    "baseline_recall@3": b_r3,
                    "current_recall@3": c_r3,
                    "baseline_leakage@3": b_leak,
                    "current_leakage@3": c_leak,
                }
            )
        elif (c_r3 > b_r3 + tolerance) or (c_leak < b_leak):
            improved_queries.append(
                {
                    "query_id": cq.query_id,
                    "category": cq.category,
                    "baseline_recall@3": b_r3,
                    "current_recall@3": c_r3,
                }
            )

    return {
        "dataset_name": current.manifest.dataset_name,
        "baseline_sha": baseline.manifest.git_sha,
        "current_sha": current.manifest.git_sha,
        "has_regression": has_regression,
        "has_security_violation": has_security_violation,
        "latency_warning": latency_warning,
        "latency_p95_change_ratio": latency_p95_change_ratio,
        "metrics_diff": diff_metrics,
        "regressed_queries": regressed_queries,
        "improved_queries": improved_queries,
    }


def update_latency_breach_state(diff_data: Dict[str, Any], state_path: Path) -> int:
    """Persist consecutive P95 latency breaches and return the updated count.

    The first >10% breach is warning-only. A second consecutive breach is
    escalated by the caller. A healthy run resets the counter to zero.
    """
    state_path.parent.mkdir(parents=True, exist_ok=True)
    previous_count = 0
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            previous_count = int(state.get("consecutive_p95_breaches", 0))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            previous_count = 0

    count = previous_count + 1 if diff_data.get("latency_warning") else 0
    state_path.write_text(
        json.dumps({"consecutive_p95_breaches": count}, indent=2) + "\n",
        encoding="utf-8",
    )
    return count


def generate_diff_markdown(diff_data: Dict[str, Any]) -> str:
    """Format comparison result into Markdown."""
    lines = [
        "# Hippo 评测 Baseline 对比报告",
        "",
        f"- **数据集**: `{diff_data['dataset_name']}`",
        f"- **Baseline Git SHA**: `{diff_data['baseline_sha']}`",
        f"- **Current Git SHA**: `{diff_data['current_sha']}`",
        f"- **回归判定 (Regression Status)**: {'❌ 存在退化或泄漏' if diff_data['has_regression'] else '✅ 全部通过 (No Regressions)'}",
        f"- **P95 延迟**: {'⚠️ 较 baseline 上升超过 10%' if diff_data.get('latency_warning') else '✅ 未触发 10% 告警'}",
        f"- **连续 P95 告警次数**: {diff_data.get('latency_consecutive_breaches', 0)}",
        "",
        "## 1. 核心指标对比 (@3 主口径)",
        "",
        "| 指标 | Baseline | Current | 差异 (Delta) | 状态 |",
        "| :--- | :---: | :---: | :---: | :---: |",
    ]

    for mk, mdata in diff_data["metrics_diff"].items():
        if "@3" in mk or mk == "mrr":
            status_icon = "❌ 退化" if mdata["regressed"] else "✅ 正常"
            delta_str = f"+{mdata['delta']:.4f}" if mdata["delta"] > 0 else f"{mdata['delta']:.4f}"
            lines.append(
                f"| `{mk}` | {mdata['baseline']:.4f} | {mdata['current']:.4f} | {delta_str} | {status_icon} |"
            )

    if diff_data["regressed_queries"]:
        lines.extend([
            "",
            "## 2. 退化题目清单 (Regressed Queries)",
            "",
            "| Query ID | 类别 | Baseline Recall@3 | Current Recall@3 | Baseline Leakage | Current Leakage |",
            "| :--- | :--- | :---: | :---: | :---: | :---: |",
        ])
        for rq in diff_data["regressed_queries"]:
            lines.append(
                f"| `{rq['query_id']}` | `{rq['category']}` | {rq['baseline_recall@3']:.4f} | {rq['current_recall@3']:.4f} | {rq['baseline_leakage@3']:.0f} | {rq['current_leakage@3']:.0f} |"
            )

    return "\n".join(lines)


def format_tier_markdown(
    tier_results: Mapping[str, Any],
    loss_quant: Optional[LossQuantification],
    retrieval_r3: float,
    locomo_result: Optional[LoCoMoEvaluationResult] = None,
) -> str:
    """Format multi-tier evaluation results and loss attribution table as Markdown."""
    lines = [
        "",
        "## 4. 多层评测与误差归因 (Multi-Tier Evaluation & Loss Attribution)",
        "",
        "### 4.1 多层指标汇总 (Tier Summary)",
        "",
        "| 评测层次 (Tier) | 核心指标 | 得分 | 说明 |",
        "| :--- | :---: | :---: | :--- |",
        f"| **Tier 1: Retrieval** | Recall@3 | **{retrieval_r3:.4f}** | 纯检索与相关性门禁能力 (零 LLM 开销) |",
    ]

    if "oracle-reader" in tier_results:
        ora_acc = tier_results["oracle-reader"].overall_metrics.get("accuracy", 0.0)
        lines.append(
            f"| **Tier 2: Oracle Reader** | Accuracy | **{ora_acc:.4f}** | 给定真实黄金证据下的阅读理解上限 |"
        )

    if "end-to-end" in tier_results:
        e2e_acc = tier_results["end-to-end"].overall_metrics.get("accuracy", 0.0)
        lines.append(
            f"| **Tier 3: End-to-End** | Accuracy | **{e2e_acc:.4f}** | 完整检索 + 问答生成 + 答案评判闭环 |"
        )

    if locomo_result and locomo_result.qa_metrics:
        avg_f1 = locomo_result.qa_metrics.get("avg_f1")
        avg_em = locomo_result.qa_metrics.get("avg_em")
        adversarial_accuracy = locomo_result.qa_metrics.get("adversarial_accuracy")
        lines.extend([
            "",
            "### 4.2 LoCoMo 对话理解与 QA",
            "",
        ])
        if avg_f1 is not None:
            lines.append(f"- **Categories 1-4 官方口径 QA F1**: `{avg_f1:.4f}`")
        if avg_em is not None:
            lines.append(f"- **Categories 1-4 补充 Exact Match**: `{avg_em:.4f}`")
        if adversarial_accuracy is not None:
            lines.append(
                f"- **Category 5 Adversarial Accuracy**: "
                f"`{adversarial_accuracy:.4f}`（不混入 QA F1）"
            )
        lines.extend([
            "",
            "| 分类 (Category) | QA F1 | Adversarial Accuracy |",
            "| :--- | :---: | :---: |",
        ])
        for cat, cat_m in sorted(locomo_result.category_metrics.items()):
            f1_val = cat_m.get("qa_f1")
            adv_val = cat_m.get("adversarial_accuracy")
            f1_str = f"**{f1_val:.4f}**" if f1_val is not None else "-"
            adv_str = f"**{adv_val:.4f}**" if adv_val is not None else "-"
            lines.append(f"| `{cat}` | {f1_str} | {adv_str} |")

    if loss_quant:
        loss_quant.compute()
        lines.extend([
            "",
            "### 4.3 分层误差诊断 (Loss Diagnostics)",
            "",
            "> 注意：这些字段来自不同阶段/指标尺度，是诊断性差值，**不能相加得到 Total Loss**。",
            "",
            "| 误差层级 (Loss Component) | 差值 (Delta) | 诊断说明 |",
            "| :--- | :---: | :--- |",
            (
                f"| **Ingest Loss** (提炼摄入损耗) | {loss_quant.ingest_loss:.4f} | 多会话提取对话转记忆时的信息损失 |"
                if loss_quant.ingest_loss is not None
                else "| **Ingest Loss** (提炼摄入损耗) | - | 仅在跨 profile 对比或包含会话评测时提供 |"
            ),
            (
                f"| **Retrieval→QA Gap** | {loss_quant.retrieval_loss:.4f} | Oracle QA 与检索上下文 QA 的准确率差值；可能包含阶段交互 |"
                if loss_quant.retrieval_loss is not None
                else "| **Retrieval Loss** (检索门禁损耗) | - | -"
            ),
            (
                f"| **Reader Loss** (生成推理损耗) | {loss_quant.reader_loss:.4f} | 证据充分输入下大模型未能正确回答的损耗 |"
                if loss_quant.reader_loss is not None
                else "| **Reader Loss** (生成推理损耗) | - | -"
            ),
            (
                f"| **Total Loss** (端到端总损耗) | {loss_quant.total_loss:.4f} | 1 - End-to-End Accuracy；不等于上方差值之和 |"
                if loss_quant.total_loss is not None
                else "| **Total Loss** (端到端总损耗) | - | -"
            ),
        ])

    lines.append("")
    return "\n".join(lines)


def save_report(
    report: BenchmarkReport,
    output_dir: Path,
    base_name: Optional[str] = None,
    tier_results: Optional[Mapping[str, Any]] = None,
    loss_quant: Optional[LossQuantification] = None,
    locomo_result: Optional[LoCoMoEvaluationResult] = None,
    beam_result: Optional[BeamEvaluationResult] = None,
    lmeb_report: Optional[LmebComparisonReport] = None,
) -> Tuple[Path, Path]:
    """Save BenchmarkReport as JSON and Markdown files with identical numbers."""
    output_dir.mkdir(parents=True, exist_ok=True)
    prefix = base_name or f"{report.manifest.dataset_name}_{int(time.time())}"
    json_path = output_dir / f"{prefix}.json"
    md_path = output_dir / f"{prefix}.md"

    report_dict = report.to_dict()
    if tier_results:
        report_dict["tier_results"] = {
            k: v.to_dict() if hasattr(v, "to_dict") else v
            for k, v in tier_results.items()
        }
    if loss_quant:
        report_dict["loss_quantification"] = loss_quant.to_dict()
    if locomo_result:
        report_dict["locomo_evaluation"] = locomo_result.to_dict()
    if beam_result:
        report_dict["beam_evaluation"] = beam_result.to_dict()
    if lmeb_report:
        report_dict["lmeb_evaluation"] = lmeb_report.to_dict()

    json_path.write_text(json.dumps(report_dict, indent=2, ensure_ascii=False), encoding="utf-8")

    md_content = generate_markdown_report(report)
    if tier_results or loss_quant or locomo_result:
        md_content += format_tier_markdown(
            tier_results or {},
            loss_quant,
            report.aggregate_metrics.get("recall@3", 0.0),
            locomo_result=locomo_result,
        )
    if beam_result:
        md_content += "\n\n---\n\n" + beam_result.to_markdown()
    if lmeb_report:
        md_content += "\n\n---\n\n" + lmeb_report.to_markdown()

    md_path.write_text(md_content, encoding="utf-8")

    return json_path, md_path


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Command-line entrypoint for benchmarks.runner."""
    parser = argparse.ArgumentParser(
        description="Hippo Memory Retrieval Evaluation Runner"
    )
    parser.add_argument(
        "--dataset",
        type=str,
        default=None,
        help=(
            "Benchmark JSON path or alias: 'longmemeval-s' downloads the pinned "
            "official cleaned release; 'longmemeval-fixture' uses the zero-network CI fixture."
        ),
    )
    parser.add_argument(
        "--profile",
        type=str,
        choices=["direct-facts", "mem0-session"],
        default="direct-facts",
        help="Ingestion profile (default: direct-facts).",
    )
    parser.add_argument(
        "--tier",
        type=str,
        choices=["retrieval", "oracle-reader", "end-to-end", "all"],
        default="retrieval",
        help="Evaluation tier to run (default: retrieval).",
    )
    parser.add_argument(
        "--adapter",
        type=str,
        choices=["replay", "engine"],
        default="replay",
        help="Adapter: replay is deterministic CI smoke; engine runs isolated real Hippo.",
    )
    parser.add_argument(
        "--qa-backend",
        type=str,
        choices=["mock", "gemini"],
        default="mock",
        help="Reader/judge backend for QA tiers. mock is CI-only; official runs must use a real backend.",
    )
    parser.add_argument(
        "--reader-model",
        type=str,
        default=os.getenv("LONGMEMEVAL_READER_MODEL", "gemini-3.5-flash-lite"),
        help="Reader model for --qa-backend gemini.",
    )
    parser.add_argument(
        "--judge-model",
        type=str,
        default=os.getenv("LONGMEMEVAL_JUDGE_MODEL", "gemini-3.5-flash-lite"),
        help="Judge model for --qa-backend gemini.",
    )
    parser.add_argument(
        "--qa-temperature",
        type=float,
        default=0.0,
        help="Temperature for reader/judge generation (default 0).",
    )
    parser.add_argument(
        "--reader-token-budget",
        type=int,
        default=256,
        help="Maximum reader output tokens.",
    )
    parser.add_argument(
        "--judge-token-budget",
        type=int,
        default=10,
        help="Maximum judge output tokens.",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="benchmarks/reports",
        help="Directory to save evaluation reports.",
    )
    parser.add_argument(
        "--baseline",
        type=str,
        default=None,
        help="Path to an approved baseline JSON report to compare against.",
    )
    parser.add_argument(
        "--latency-state",
        type=str,
        default=None,
        help=(
            "Optional JSON state file used to persist consecutive P95 latency "
            "breaches across scheduled runs."
        ),
    )
    parser.add_argument(
        "--max-injected",
        type=int,
        default=3,
        help="Production memory injection budget for @3 and QA context (default 3).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for deterministic runs.",
    )
    parser.add_argument(
        "--collection",
        type=str,
        default=None,
        help="Explicit isolated Qdrant collection name (engine adapter only).",
    )
    parser.add_argument(
        "--report-name",
        type=str,
        default=None,
        help="Explicit base filename for generated reports (without extension).",
    )
    parser.add_argument(
        "--dataset-cache-dir",
        type=str,
        default=None,
        help="Optional cache directory for pinned public benchmark downloads.",
    )
    parser.add_argument(
        "--lmeb-profile-name",
        type=str,
        default=None,
        help=(
            "Human-readable embedding profile label for an LMEB run. "
            "This is independent of --profile, which controls ingestion strategy."
        ),
    )
    parser.add_argument(
        "--lmeb-compare-report",
        action="append",
        default=[],
        help=(
            "Path to a prior LMEB JSON report. Repeat at least twice to merge "
            "independent embedding runs into a side-by-side comparison report."
        ),
    )
    parser.add_argument(
        "--beam-compare-report",
        action="append",
        default=[],
        help=(
            "Path to a prior BEAM JSON report. Repeat at least twice with "
            "different scale splits to generate a quality/latency/cost curve."
        ),
    )
    parser.add_argument(
        "--max-benchmark-items",
        type=int,
        default=250000,
        help="Fail closed before ingest if a BEAM/LMEB corpus exceeds this item count.",
    )
    parser.add_argument(
        "--max-estimated-embedding-cost-usd",
        type=float,
        default=5.0,
        help="Fail closed before ingest when estimated BEAM/LMEB embedding cost exceeds this budget.",
    )
    parser.add_argument(
        "--embedding-cost-per-million-tokens",
        type=float,
        default=DEFAULT_EMBEDDING_COST_PER_1M_TOKENS,
        help="Cost model used for benchmark preflight and BEAM reporting.",
    )

    args = parser.parse_args(argv)

    if args.beam_compare_report:
        if len(args.beam_compare_report) < 2:
            print(
                "Error: --beam-compare-report must be supplied at least twice.",
                file=sys.stderr,
            )
            return 1
        points: list[dict[str, Any]] = []
        try:
            for raw_path in args.beam_compare_report:
                report_path = Path(raw_path)
                payload = json.loads(report_path.read_text(encoding="utf-8"))
                section = payload.get("beam_evaluation")
                if not isinstance(section, Mapping):
                    raise ValueError(
                        f"{report_path} does not contain a beam_evaluation object"
                    )
                performance = section.get("performance")
                metrics = section.get("retrieval_metrics")
                if not isinstance(performance, Mapping) or not isinstance(metrics, Mapping):
                    raise ValueError(
                        f"{report_path} has an invalid beam_evaluation object"
                    )
                manifest = payload.get("manifest")
                dataset_name = (
                    str(manifest.get("dataset_name"))
                    if isinstance(manifest, Mapping) and manifest.get("dataset_name")
                    else report_path.stem
                )
                points.append(
                    {
                        "dataset_name": dataset_name,
                        "total_memories": int(performance.get("total_memories", 0)),
                        "estimated_corpus_tokens": int(
                            performance.get("estimated_corpus_tokens", 0)
                        ),
                        "recall@3": float(metrics.get("recall@3", 0.0)),
                        "recall@10": float(metrics.get("recall@10", 0.0)),
                        "ndcg@10": float(metrics.get("ndcg@10", 0.0)),
                        "p95_ms": float(
                            performance.get("query_latency_p95_ms", 0.0)
                        ),
                        "estimated_cost_usd": float(
                            performance.get("estimated_cost_usd", 0.0)
                        ),
                        "index_size_bytes": performance.get("index_size_bytes"),
                    }
                )
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            print(f"Error merging BEAM reports: {exc}", file=sys.stderr)
            return 1

        points.sort(key=lambda point: point["total_memories"])
        if len({point["total_memories"] for point in points}) < 2:
            print(
                "Error: BEAM scale curve requires reports with at least two "
                "different corpus sizes.",
                file=sys.stderr,
            )
            return 1

        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        base_name = args.report_name or "beam_scale_curve"
        json_path = output_dir / f"{base_name}.json"
        md_path = output_dir / f"{base_name}.md"
        json_path.write_text(
            json.dumps(
                {"beam_scale_curve": {"points": points}},
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        lines = [
            "# BEAM Scale Curve",
            "",
            "| Dataset | Memories | Tokens | Recall@3 | Recall@10 | nDCG@10 | P95 ms | Est. Cost USD | Index Bytes |",
            "| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for point in points:
            index_size = (
                str(point["index_size_bytes"])
                if point["index_size_bytes"] is not None
                else "-"
            )
            lines.append(
                f"| {point['dataset_name']} | {point['total_memories']} | "
                f"{point['estimated_corpus_tokens']} | {point['recall@3']:.4f} | "
                f"{point['recall@10']:.4f} | {point['ndcg@10']:.4f} | "
                f"{point['p95_ms']:.2f} | {point['estimated_cost_usd']:.6f} | "
                f"{index_size} |"
            )
        md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("BEAM scale comparison succeeded!")
        print(f"JSON report: {json_path}")
        print(f"Markdown summary: {md_path}")
        return 0

    if args.lmeb_compare_report:
        if len(args.lmeb_compare_report) < 2:
            print(
                "Error: --lmeb-compare-report must be supplied at least twice.",
                file=sys.stderr,
            )
            return 1
        reports: list[LmebComparisonReport] = []
        try:
            for raw_path in args.lmeb_compare_report:
                report_path = Path(raw_path)
                payload = json.loads(report_path.read_text(encoding="utf-8"))
                section = payload.get("lmeb_evaluation")
                if not isinstance(section, Mapping):
                    raise ValueError(
                        f"{report_path} does not contain an lmeb_evaluation object"
                    )
                reports.append(LmebComparisonReport.from_dict(section))
            merged = merge_lmeb_reports(reports)
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            print(f"Error merging LMEB reports: {exc}", file=sys.stderr)
            return 1

        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        base_name = args.report_name or "lmeb_profile_comparison"
        json_path = output_dir / f"{base_name}.json"
        md_path = output_dir / f"{base_name}.md"
        json_path.write_text(
            json.dumps(
                {"lmeb_evaluation": merged.to_dict()},
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        md_path.write_text(merged.to_markdown(), encoding="utf-8")
        print("LMEB comparison succeeded!")
        print(f"JSON report: {json_path}")
        print(f"Markdown summary: {md_path}")
        return 0

    is_longmemeval = False
    is_locomo = False
    is_beam = False
    is_lmeb = False
    official_named_dataset = False
    longmemeval_items = None
    locomo_samples = None
    beam_dataset = None
    lmeb_dataset = None
    benchmark_source: Dict[str, Any] = {}

    try:
        if args.dataset:
            ds_raw = args.dataset.strip()
            ds_lower = ds_raw.lower()

            if ds_lower in ("locomo", "locomo10", "locomo-10"):
                is_locomo = True
                official_named_dataset = True
                locomo_samples = load_locomo_samples()
                benchmark_source = {
                    "dataset_source": LOCOMO_OFFICIAL_DATA_URL,
                    "dataset_revision": LOCOMO_OFFICIAL_DATASET_REVISION,
                    "dataset_source_sha256": LOCOMO_OFFICIAL_DATASET_SHA256,
                    "dataset_release": "locomo10",
                }
            elif ds_lower in ("locomo-fixture", "locomo_fixture", "locomo-smoke"):
                is_locomo = True
                locomo_samples = load_locomo_fixture_samples()
                benchmark_source = {
                    "dataset_source": "benchmarks/data/locomo10_fixture.json",
                    "dataset_release": "ci-fixture",
                }
            elif ds_lower in ("longmemeval", "longmemeval-s", "longmemeval_s"):
                is_longmemeval = True
                official_named_dataset = True
                longmemeval_items = load_longmemeval_items()
                benchmark_source = {
                    "dataset_source": OFFICIAL_DATA_URL,
                    "dataset_revision": OFFICIAL_DATASET_REVISION,
                    "dataset_source_sha256": OFFICIAL_DATASET_SHA256,
                    "dataset_release": "longmemeval-cleaned",
                }
            elif ds_lower in (
                "longmemeval-fixture",
                "longmemeval_fixture",
                "longmemeval-smoke",
            ):
                is_longmemeval = True
                longmemeval_items = load_longmemeval_fixture_items()
                benchmark_source = {
                    "dataset_source": str(BUILTIN_FIXTURE_PATH),
                    "dataset_release": "ci-fixture",
                }
            elif ds_lower in (
                "beam",
                "beam-100k",
                "beam_100k",
                "beam-128k",
                "beam_128k",
                "beam-128",
                "beam-500k",
                "beam_500k",
                "beam-1m",
                "beam_1m",
            ):
                is_beam = True
                official_named_dataset = True
                if args.adapter != "engine":
                    print(
                        "Error: the named official BEAM benchmark must use --adapter engine. "
                        "Use --dataset beam-fixture for replay/CI smoke tests.",
                        file=sys.stderr,
                    )
                    return 1
                if "500k" in ds_lower:
                    beam_split = "500K"
                elif "1m" in ds_lower:
                    beam_split = "1M"
                else:
                    beam_split = OFFICIAL_BEAM_SPLIT
                beam_dataset = load_official_beam_dataset(
                    cache_dir=args.dataset_cache_dir,
                    split=beam_split,
                )
                benchmark_source = {
                    "dataset_source": f"hf://datasets/{OFFICIAL_BEAM_DATASET_ID}",
                    "dataset_revision": OFFICIAL_BEAM_DATASET_REVISION,
                    "dataset_release": f"beam-{beam_split.lower()}",
                    "upstream_protocol_revision": OFFICIAL_BEAM_PROTOCOL_REVISION,
                    "legacy_alias_used": "128" in ds_lower,
                }
            elif ds_lower in ("beam-fixture", "beam_fixture", "beam-smoke"):
                is_beam = True
                beam_dataset = load_beam_dataset(BUILTIN_BEAM_FIXTURE_PATH)
                benchmark_source = {
                    "dataset_source": str(BUILTIN_BEAM_FIXTURE_PATH),
                    "dataset_release": "ci-fixture",
                }
            elif ds_lower in ("lmeb", "lmeb-dialogue", "lmeb_dialogue", "lmeb-memory"):
                is_lmeb = True
                official_named_dataset = True
                if args.adapter != "engine":
                    print(
                        "Error: the named official LMEB benchmark must use --adapter engine. "
                        "Use --dataset lmeb-fixture for replay/CI smoke tests.",
                        file=sys.stderr,
                    )
                    return 1
                lmeb_dataset = load_official_lmeb_dataset(
                    cache_dir=args.dataset_cache_dir
                )
                benchmark_source = {
                    "dataset_source": f"hf://datasets/{OFFICIAL_LMEB_DATASET_ID}",
                    "dataset_revision": OFFICIAL_LMEB_DATASET_REVISION,
                    "dataset_release": (
                        f"lmeb-dialogue-{OFFICIAL_LMEB_FAMILY.lower()}-"
                        f"{OFFICIAL_LMEB_SPLIT}"
                    ),
                }
            elif ds_lower in ("lmeb-fixture", "lmeb_fixture", "lmeb-smoke"):
                is_lmeb = True
                lmeb_dataset = load_lmeb_dataset(BUILTIN_LMEB_FIXTURE_PATH)
                benchmark_source = {
                    "dataset_source": str(BUILTIN_LMEB_FIXTURE_PATH),
                    "dataset_release": "ci-fixture",
                }
            else:
                dataset_path = Path(ds_raw)
                if not dataset_path.is_file():
                    print(
                        f"Error: dataset file not found: {dataset_path}",
                        file=sys.stderr,
                    )
                    return 1

                content = dataset_path.read_text(encoding="utf-8")
                parsed = json.loads(content)
                if (
                    isinstance(parsed, list)
                    and parsed
                    and isinstance(parsed[0], Mapping)
                    and "sample_id" in parsed[0]
                    and "conversation" in parsed[0]
                ):
                    is_locomo = True
                    locomo_samples = load_locomo_samples(dataset_path)
                    benchmark_source = {
                        "dataset_source": str(dataset_path.resolve()),
                        "dataset_release": "local-locomo",
                    }
                elif (
                    isinstance(parsed, list)
                    and parsed
                    and isinstance(parsed[0], Mapping)
                    and "question_id" in parsed[0]
                    and "haystack_sessions" in parsed[0]
                ) or (
                    isinstance(parsed, dict)
                    and ("questions" in parsed or "data" in parsed)
                ):
                    is_longmemeval = True
                    longmemeval_items = load_longmemeval_items(dataset_path)
                    benchmark_source = {
                        "dataset_source": str(dataset_path.resolve()),
                        "dataset_release": "local-longmemeval",
                    }
                elif isinstance(parsed, Mapping) and (
                    parsed.get("benchmark") == "BEAM"
                    or ("profile" in parsed and "memories" in parsed)
                ):
                    is_beam = True
                    beam_dataset = load_beam_dataset(dataset_path)
                    benchmark_source = {
                        "dataset_source": str(dataset_path.resolve()),
                        "dataset_release": f"local-beam-{beam_dataset.profile}",
                    }
                elif isinstance(parsed, Mapping) and (
                    parsed.get("benchmark") == "LMEB"
                    or ("subset" in parsed and "corpus" in parsed)
                ):
                    is_lmeb = True
                    lmeb_dataset = load_lmeb_dataset(dataset_path)
                    benchmark_source = {
                        "dataset_source": str(dataset_path.resolve()),
                        "dataset_release": f"local-lmeb-{lmeb_dataset.subset}",
                    }
                else:
                    dataset = BenchmarkDataset.from_dict(parsed)
                    benchmark_source = {
                        "dataset_source": str(dataset_path.resolve()),
                        "dataset_release": "local-benchmark",
                    }
        else:
            dataset = create_smoke_fixture_dataset()
            benchmark_source = {"dataset_release": "hippo-smoke-fixture"}

        if is_longmemeval and longmemeval_items is not None:
            if args.profile == "mem0-session":
                dataset = convert_to_sessions_benchmark(longmemeval_items)
            else:
                dataset = convert_to_direct_facts_benchmark(longmemeval_items)
        elif is_locomo and locomo_samples is not None:
            dataset = convert_to_locomo_benchmark(locomo_samples)
        elif is_beam and beam_dataset is not None:
            dataset = convert_to_beam_benchmark(beam_dataset)
        elif is_lmeb and lmeb_dataset is not None:
            dataset = convert_to_lmeb_benchmark(lmeb_dataset)
    except Exception as exc:
        print(
            f"Error loading benchmark dataset: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1

    if is_beam or is_lmeb:
        if args.max_benchmark_items <= 0:
            print("Error: --max-benchmark-items must be positive.", file=sys.stderr)
            return 1
        if args.max_estimated_embedding_cost_usd < 0:
            print(
                "Error: --max-estimated-embedding-cost-usd cannot be negative.",
                file=sys.stderr,
            )
            return 1
        if args.embedding_cost_per_million_tokens < 0:
            print(
                "Error: --embedding-cost-per-million-tokens cannot be negative.",
                file=sys.stderr,
            )
            return 1

        corpus_items = len(dataset.corpus)
        estimated_tokens = sum(
            estimate_tokens_from_text(item.text) for item in dataset.corpus
        )
        estimated_embedding_cost = (
            estimated_tokens / 1_000_000.0
        ) * args.embedding_cost_per_million_tokens
        benchmark_source["resource_budget"] = {
            "max_corpus_items": args.max_benchmark_items,
            "max_estimated_embedding_cost_usd": args.max_estimated_embedding_cost_usd,
            "embedding_cost_per_million_tokens": args.embedding_cost_per_million_tokens,
            "estimated_corpus_tokens": estimated_tokens,
            "estimated_embedding_cost_usd": round(estimated_embedding_cost, 6),
        }

        if corpus_items > args.max_benchmark_items:
            print(
                "Error: benchmark corpus exceeds configured resource budget: "
                f"{corpus_items} items > {args.max_benchmark_items}.",
                file=sys.stderr,
            )
            return 2
        if estimated_embedding_cost > args.max_estimated_embedding_cost_usd:
            print(
                "Error: estimated embedding cost exceeds configured budget: "
                f"${estimated_embedding_cost:.4f} > "
                f"${args.max_estimated_embedding_cost_usd:.4f}.",
                file=sys.stderr,
            )
            return 2

    if official_named_dataset and args.adapter != "engine":
        if is_locomo:
            dataset_label = "LoCoMo-10"
            fixture_name = "locomo-fixture"
        elif is_beam:
            dataset_label = "BEAM"
            fixture_name = "beam-fixture"
        elif is_lmeb:
            dataset_label = "LMEB"
            fixture_name = "lmeb-fixture"
        else:
            dataset_label = "LongMemEval-S"
            fixture_name = "longmemeval-fixture"
        print(
            f"Error: the named official {dataset_label} benchmark must use "
            f"--adapter engine. Use --dataset {fixture_name} for replay/CI smoke tests.",
            file=sys.stderr,
        )
        return 1

    qa_requested = args.tier in ("oracle-reader", "end-to-end", "all")
    if (is_beam or is_lmeb) and qa_requested:
        ds_name = "BEAM" if is_beam else "LMEB"
        print(
            f"Error: {ds_name} is a retrieval/scale benchmark and only supports --tier retrieval.",
            file=sys.stderr,
        )
        return 1

    if is_locomo and args.tier == "oracle-reader":
        print(
            "Error: LoCoMo exposes retrieval and end-to-end QA tiers; "
            "use --tier end-to-end or --tier all.",
            file=sys.stderr,
        )
        return 1

    if official_named_dataset and qa_requested and args.qa_backend == "mock":
        dataset_label = "LoCoMo-10" if is_locomo else "LongMemEval-S"
        print(
            f"Error: mock readers are CI smoke helpers and cannot score official "
            f"{dataset_label}. Select --qa-backend gemini and pin the reader model.",
            file=sys.stderr,
        )
        return 1

    if args.adapter == "replay":
        adapter: BenchmarkAdapter = ReplayFixtureAdapter()
    elif args.adapter == "engine":
        coll = args.collection or (
            f"eval_locomo_{int(time.time())}_{uuid.uuid4().hex[:6]}"
            if is_locomo
            else (
                f"eval_beam_{int(time.time())}_{uuid.uuid4().hex[:6]}"
                if is_beam
                else (
                    f"eval_lmeb_{int(time.time())}_{uuid.uuid4().hex[:6]}"
                    if is_lmeb
                    else (
                        f"eval_longmemeval_{int(time.time())}_{uuid.uuid4().hex[:6]}"
                        if is_longmemeval
                        else None
                    )
                )
            )
        )
        adapter = HippoEngineAdapter(collection_name=coll)
    else:
        raise ValueError(f"Unknown adapter {args.adapter}")

    evaluator: Optional[LongMemEvalEvaluator] = None
    locomo_evaluator: Optional[LoCoMoEvaluator] = None

    if qa_requested and is_locomo:
        if official_named_dataset:
            try:
                ensure_official_scorer_available()
            except RuntimeError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 1
        if args.qa_backend == "gemini":
            locomo_evaluator = LoCoMoEvaluator(
                reader=GeminiLoCoMoReader(
                    args.reader_model,
                    temperature=args.qa_temperature,
                    max_output_tokens=args.reader_token_budget,
                ),
                strict_official_scorer=official_named_dataset,
            )
        else:
            locomo_evaluator = LoCoMoEvaluator(
                reader=RuleBasedMockLoCoMoReader(),
                strict_official_scorer=False,
            )
    elif qa_requested:
        if args.qa_backend == "gemini":
            evaluator = LongMemEvalEvaluator(
                reader=GeminiReader(
                    args.reader_model,
                    temperature=args.qa_temperature,
                    max_output_tokens=args.reader_token_budget,
                ),
                judge=GeminiJudge(
                    args.judge_model,
                    temperature=args.qa_temperature,
                    max_output_tokens=args.judge_token_budget,
                ),
            )
        else:
            evaluator = LongMemEvalEvaluator(
                reader=RuleBasedMockReader(),
                judge=RuleBasedJudge(),
            )

    qa_manifest: Optional[Dict[str, Any]] = None
    if locomo_evaluator is not None:
        qa_manifest = locomo_evaluator.manifest_config()
    elif evaluator is not None:
        qa_manifest = evaluator.manifest_config()

    manifest_extra: Dict[str, Any] = {
        **benchmark_source,
        "tier": args.tier,
        "qa_backend": args.qa_backend if qa_requested else None,
        "qa": qa_manifest,
    }

    try:
        runner = BenchmarkRunner(
            adapter=adapter,
            max_injected=args.max_injected,
            seed=args.seed,
            ingest_profile=args.profile,
        )
        report = runner.run(
            dataset,
            capture_trace=True,
            extra_manifest=manifest_extra,
        )

        tier_results: Dict[str, Any] = {}
        loss_quant: Optional[LossQuantification] = None
        locomo_result: Optional[LoCoMoEvaluationResult] = None
        beam_result: Optional[BeamEvaluationResult] = None
        lmeb_report: Optional[LmebComparisonReport] = None

        if is_beam:
            beam_evaluator = BeamEvaluator(
                cost_per_million_tokens=args.embedding_cost_per_million_tokens,
            )
            beam_result = beam_evaluator.evaluate(
                adapter=adapter,
                dataset=dataset,
                limit=max(runner.k_values),
                k_values=runner.k_values,
                ingest_duration_seconds=getattr(runner, "last_ingest_duration_seconds", 0.0),
            )
        elif is_lmeb:
            lmeb_evaluator = LmebEvaluator()
            profile_meta = adapter.get_embedding_profile()
            profile_name = args.lmeb_profile_name or (
                f"{profile_meta.get('provider', 'unknown')}:"
                f"{profile_meta.get('model', 'unknown')}:"
                f"{profile_meta.get('dims', 0)}"
            )
            profile_metrics = lmeb_evaluator.evaluate_profile(
                adapter,
                dataset,
                profile_name=profile_name,
            )
            lmeb_report = LmebComparisonReport(
                dataset_name=dataset.name,
                profiles=[profile_metrics],
            )
        elif is_locomo and qa_requested:
            assert locomo_evaluator is not None
            locomo_result = locomo_evaluator.evaluate(
                adapter=adapter,
                dataset=dataset,
                limit=args.max_injected,
                k_values=runner.k_values,
                evaluate_qa=True,
            )
        elif qa_requested:
            assert evaluator is not None
            corpus_lookup = {item.id: item.text for item in dataset.corpus}

            if args.tier in ("oracle-reader", "all"):
                tier_results["oracle-reader"] = evaluator.evaluate_oracle_reader_tier(
                    dataset=dataset,
                    corpus_lookup=corpus_lookup,
                )

            if args.tier in ("end-to-end", "all"):
                tier_results["end-to-end"] = evaluator.evaluate_end_to_end_tier(
                    adapter=adapter,
                    dataset=dataset,
                    corpus_lookup=corpus_lookup,
                    limit=args.max_injected,
                )

            if args.tier == "all":
                oracle_acc = tier_results["oracle-reader"].overall_metrics.get(
                    "accuracy", 0.0
                )
                e2e_acc = tier_results["end-to-end"].overall_metrics.get(
                    "accuracy", 0.0
                )
                recall_at_3 = report.aggregate_metrics.get("recall@3", 0.0)
                loss_quant = LossQuantification(
                    direct_facts_recall_at_3=(
                        recall_at_3 if args.profile == "direct-facts" else None
                    ),
                    sessions_recall_at_3=(
                        recall_at_3 if args.profile == "mem0-session" else None
                    ),
                    oracle_reader_accuracy=oracle_acc,
                    end_to_end_accuracy=e2e_acc,
                )
                loss_quant.compute()

        out_dir = Path(args.output_dir)
        report_base = args.report_name or f"report_{dataset.name}"
        json_path, md_path = save_report(
            report=report,
            output_dir=out_dir,
            base_name=report_base,
            tier_results=tier_results if tier_results else None,
            loss_quant=loss_quant,
            locomo_result=locomo_result,
            beam_result=beam_result,
            lmeb_report=lmeb_report,
        )

        hypothesis_paths: List[Path] = []
        for tier_name in ("oracle-reader", "end-to-end"):
            if tier_name in tier_results:
                hypothesis_paths.append(
                    export_official_hypotheses(
                        tier_results[tier_name],
                        out_dir / f"{report_base}_{tier_name}_official.jsonl",
                    )
                )

        print("Evaluation succeeded!")
        print(f"JSON report: {json_path}")
        print(f"Markdown summary: {md_path}")
        for path in hypothesis_paths:
            print(f"Official-evaluator hypotheses: {path}")
        print("-" * 50)
        print(f"Recall@3: {report.aggregate_metrics.get('recall@3', 0.0):.4f}")
        print(f"Recall@5: {report.aggregate_metrics.get('recall@5', 0.0):.4f}")
        print(f"Recall@10: {report.aggregate_metrics.get('recall@10', 0.0):.4f}")
        print(f"nDCG@10: {report.aggregate_metrics.get('ndcg@10', 0.0):.4f}")
        print(f"Precision@3: {report.aggregate_metrics.get('precision@3', 0.0):.4f}")
        print(
            f"Candidate Recall@20: "
            f"{report.aggregate_metrics.get('candidate_recall@20', 0.0):.4f}"
        )
        print(
            f"Forbidden Leakage@3: "
            f"{report.aggregate_metrics.get('forbidden_leakage@3', 0.0):.0f}"
        )
        print(
            f"Empty Accuracy@3: "
            f"{report.aggregate_metrics.get('empty_accuracy@3', 0.0):.4f}"
        )
        p50 = report.aggregate_metrics.get("latency_p50_ms")
        p95 = report.aggregate_metrics.get("latency_p95_ms")
        if p50 is not None and p95 is not None:
            print(f"Latency P50: {p50:.2f}ms | P95: {p95:.2f}ms")
        print("-" * 50)

        if tier_results:
            print("Multi-Tier Summary:")
            print(
                "  Tier 1 (Retrieval Recall@3): "
                f"{report.aggregate_metrics.get('recall@3', 0.0):.4f}"
            )
            if "oracle-reader" in tier_results:
                print(
                    "  Tier 2 (Oracle Reader Acc): "
                    f"{tier_results['oracle-reader'].overall_metrics.get('accuracy', 0.0):.4f}"
                )
            if "end-to-end" in tier_results:
                print(
                    "  Tier 3 (End-to-End Acc):    "
                    f"{tier_results['end-to-end'].overall_metrics.get('accuracy', 0.0):.4f}"
                )
            if loss_quant:
                print("Loss Diagnostics (non-additive):")
                if loss_quant.ingest_loss is not None:
                    print(f"  Ingest Loss:       {loss_quant.ingest_loss:.4f}")
                if loss_quant.retrieval_loss is not None:
                    print(f"  Retrieval→QA Gap:  {loss_quant.retrieval_loss:.4f}")
                if loss_quant.reader_loss is not None:
                    print(f"  Reader Loss:       {loss_quant.reader_loss:.4f}")
                if loss_quant.total_loss is not None:
                    print(f"  Total Loss:        {loss_quant.total_loss:.4f}")
            print("-" * 50)

        if locomo_result and locomo_result.qa_metrics:
            print("LoCoMo QA Evaluation:")
            if "avg_f1" in locomo_result.qa_metrics:
                print(
                    "  Categories 1-4 QA F1: "
                    f"{locomo_result.qa_metrics['avg_f1']:.4f}"
                )
            if "avg_em" in locomo_result.qa_metrics:
                print(
                    "  Categories 1-4 EM: "
                    f"{locomo_result.qa_metrics['avg_em']:.4f}"
                )
            if "adversarial_accuracy" in locomo_result.qa_metrics:
                print(
                    "  Category 5 Adversarial Accuracy: "
                    f"{locomo_result.qa_metrics['adversarial_accuracy']:.4f}"
                )
            print("-" * 50)

        if beam_result:
            print("BEAM Scale & Performance Evaluation:")
            perf = beam_result.performance
            print(
                f"  Ingest: {perf.ingest_duration_seconds:.2f}s "
                f"({perf.ingest_throughput_items_per_sec:.1f} items/s)"
            )
            print(
                f"  Latency: P50={perf.query_latency_p50_ms:.2f}ms | "
                f"P95={perf.query_latency_p95_ms:.2f}ms | "
                f"P99={perf.query_latency_p99_ms:.2f}ms"
            )
            if perf.index_size_bytes is not None:
                print(f"  Index Size: {perf.index_size_bytes:,} bytes")
            print(
                f"  Corpus Tokens: ~{perf.estimated_corpus_tokens:,} | "
                f"Cost: ${perf.estimated_cost_usd:.4f} USD"
            )
            print(
                f"  Recall@3: {beam_result.retrieval_metrics.get('recall@3', 0.0):.4f} | "
                f"Recall@10: {beam_result.retrieval_metrics.get('recall@10', 0.0):.4f} | "
                f"nDCG@10: {beam_result.retrieval_metrics.get('ndcg@10', 0.0):.4f}"
            )
            print("-" * 50)

        if lmeb_report:
            print("LMEB Component Evaluation:")
            print(f"  {lmeb_report.disclaimer}")
            for p in lmeb_report.profiles:
                print(
                    f"  Profile [{p.profile_name}]: "
                    f"nDCG@10={p.ndcg_10:.4f} | Recall@10={p.recall_10:.4f} | "
                    f"MRR={p.mrr:.4f} | Recall@3={p.recall_3:.4f}"
                )
            print("-" * 50)

        is_strict_gold = "gold" in report.manifest.dataset_name.lower()
        gate_thresh = (
            None
            if is_strict_gold
            else SecurityGateThresholds(
                min_total_queries=0,
                min_hard_negative_ratio=0.0,
                max_hard_negative_fpr=1.0,
            )
        )
        gate_audit = audit_security_gates(report, thresholds=gate_thresh)
        print(
            f"Security Hard Gates: "
            f"{'PASSED ✅' if gate_audit['passed'] else 'FAILED ❌'}"
        )
        if not gate_audit["passed"]:
            for violation in gate_audit["violations"]:
                print(
                    f"  [SECURITY VIOLATION] {violation}",
                    file=sys.stderr,
                )
            return 2

        if args.baseline:
            baseline_path = Path(args.baseline)
            if not baseline_path.is_file():
                print(
                    f"Error: Baseline file not found: {baseline_path}",
                    file=sys.stderr,
                )
                return 1

            baseline_report = BenchmarkReport.from_json(
                baseline_path.read_text(encoding="utf-8")
            )
            try:
                diff = compare_reports(report, baseline_report)
            except ValueError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 1

            if args.latency_state:
                breach_count = update_latency_breach_state(
                    diff, Path(args.latency_state)
                )
                diff["latency_consecutive_breaches"] = breach_count
                if diff["latency_warning"]:
                    print(
                        "WARNING: P95 latency increased by more than 10% "
                        f"(consecutive breaches: {breach_count}).",
                        file=sys.stderr,
                    )
                if breach_count >= 2:
                    diff["has_regression"] = True
                    diff["latency_escalated"] = True
                    print(
                        "ERROR: P95 latency regression confirmed on consecutive runs.",
                        file=sys.stderr,
                    )

            diff_md = generate_diff_markdown(diff)
            diff_path = out_dir / f"diff_{dataset.name}.md"
            diff_path.write_text(diff_md, encoding="utf-8")
            print(f"Diff report: {diff_path}")
            if diff["has_regression"]:
                print("WARNING: Regression detected against baseline!")
                return 2

        return 0
    finally:
        adapter.cleanup()


if __name__ == "__main__":
    sys.exit(main())
