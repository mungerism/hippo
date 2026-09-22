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

from benchmarks.adapter import (
    BenchmarkAdapter,
    HippoEngineAdapter,
    ReplayFixtureAdapter,
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
from hippo_memory.config import HippoConfig
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
    ):
        self.adapter = adapter
        self.k_values = [int(k) for k in k_values]
        self.max_injected = max_injected
        self.seed = seed

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

        # Ingest corpus into isolated adapter
        self.adapter.ingest_corpus(dataset.corpus)

        query_results: List[QueryEvaluationResult] = []

        for q in dataset.queries:
            retrieved_ids, trace = self.adapter.search(
                query=q,
                limit=self.max_injected,
                capture_trace=capture_trace,
            )

            qrels_for_q = dataset.qrels.get(q.query_id, {})
            forbidden_for_q = dataset.forbidden.get(q.query_id, [])

            metrics_dict = evaluate_single_query(
                query=q,
                retrieved_ids=retrieved_ids,
                qrels=qrels_for_q,
                forbidden_ids=forbidden_for_q,
                k_values=self.k_values,
            )

            relevant_ids = [cid for cid, grade in qrels_for_q.items() if grade > 0]

            query_results.append(
                QueryEvaluationResult(
                    query_id=q.query_id,
                    query=q.query,
                    category=q.category,
                    retrieved_ids=retrieved_ids,
                    relevant_ids=relevant_ids,
                    forbidden_ids=forbidden_for_q,
                    metrics=metrics_dict,
                    trace=trace,
                )
            )

        duration = round(time.perf_counter() - start_time, 4)

        # Aggregate metrics
        agg_metrics = aggregate_metrics(query_results)
        cat_metrics = aggregate_by_category(query_results)

        # Build manifest
        gate_cfg = getattr(self.adapter, "gate_config", SearchGateConfig())
        gate_thresholds = {
            "final_threshold": getattr(gate_cfg, "final_threshold", 0.32),
            "dense_only_threshold": getattr(gate_cfg, "dense_only_threshold", 0.62),
            "relative_threshold_ratio": getattr(gate_cfg, "relative_threshold_ratio", 0.50),
            "enabled": getattr(gate_cfg, "enabled", True),
        }

        run_id = f"eval_{dataset.name}_{int(time.time())}"
        manifest = RunManifest(
            run_id=run_id,
            timestamp=timestamp,
            git_sha=get_git_sha(),
            dataset_name=dataset.name,
            dataset_hash=dataset.compute_hash(),
            mem0_version=get_mem0_version(),
            hippo_version=get_hippo_version(),
            embedding_profile={
                "provider": "ollama",
                "model": "bge-m3",
                "dims": 1024,
                "collection": getattr(self.adapter, "collection_name", "replay_memory"),
            },
            gate_thresholds=gate_thresholds,
            max_injected=self.max_injected,
            k_values=self.k_values,
            seed=self.seed,
            duration_seconds=duration,
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
        f"- **评测耗时**: `{m.duration_seconds}s` (共 {len(report.query_results)} 道题目)",
        "",
        "## 1. 核心召回与安全指标汇总 (Primary Metrics)",
        "",
        "> 注：`@3` 为 Hippo 生产默认主报告口径（对齐 `max_injected=3`）。",
        "",
        "| 指标 | @1 | @3 (Hippo 主口径) | @5 | @10 |",
        "| :--- | :---: | :---: | :---: | :---: |",
    ]

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

    mrr_val = agg.get("mrr", 0.0)
    lines.extend([
        f"| **MRR (平均倒数排名)** | - | **{mrr_val:.4f}** | - | - |",
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
        e3 = cat_m.get("empty_accuracy@3", 0.0)
        lines.append(
            f"| `{cat}` | {cnt} | {r3:.4f} | {p3:.4f} | {h3:.4f} | {n3:.4f} | {l3:.2f} | {e3:.4f} |"
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


def compare_reports(
    current: BenchmarkReport,
    baseline: BenchmarkReport,
    tolerance: float = 0.001,
) -> Dict[str, Any]:
    """Compare current evaluation report with a baseline report.

    Detects:
        - Metric regressions (drop > tolerance)
        - Forbidden leakage regressions (any increase > 0)
        - Per-query regressions and improvements
    """
    diff_metrics: Dict[str, Dict[str, Any]] = {}
    has_regression = False
    has_security_violation = False

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
        else:
            regressed = delta < -tolerance
            if regressed:
                has_regression = True

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
        "metrics_diff": diff_metrics,
        "regressed_queries": regressed_queries,
        "improved_queries": improved_queries,
    }


def generate_diff_markdown(diff_data: Dict[str, Any]) -> str:
    """Format comparison result into Markdown."""
    lines = [
        f"# Hippo 评测 Baseline 对比报告",
        "",
        f"- **数据集**: `{diff_data['dataset_name']}`",
        f"- **Baseline Git SHA**: `{diff_data['baseline_sha']}`",
        f"- **Current Git SHA**: `{diff_data['current_sha']}`",
        f"- **回归判定 (Regression Status)**: {'❌ 存在退化或泄漏' if diff_data['has_regression'] else '✅ 全部通过 (No Regressions)'}",
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


def save_report(
    report: BenchmarkReport,
    output_dir: Path,
    base_name: Optional[str] = None,
) -> Tuple[Path, Path]:
    """Save BenchmarkReport as JSON and Markdown files with identical numbers."""
    output_dir.mkdir(parents=True, exist_ok=True)
    prefix = base_name or f"{report.manifest.dataset_name}_{int(time.time())}"
    json_path = output_dir / f"{prefix}.json"
    md_path = output_dir / f"{prefix}.md"

    json_path.write_text(report.to_json(indent=2), encoding="utf-8")
    md_path.write_text(generate_markdown_report(report), encoding="utf-8")

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
        help="Path to benchmark dataset JSON file (defaults to built-in smoke fixture).",
    )
    parser.add_argument(
        "--adapter",
        type=str,
        choices=["replay", "engine"],
        default="replay",
        help="Evaluation adapter to use (replay: offline mock; engine: isolated live Qdrant).",
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
        "--max-injected",
        type=int,
        default=3,
        help="Maximum memories to retrieve per query (default 3).",
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

    args = parser.parse_args(argv)

    # 1. Load dataset
    if args.dataset:
        dataset_path = Path(args.dataset)
        if not dataset_path.is_file():
            print(f"Error: dataset file not found: {dataset_path}", file=sys.stderr)
            return 1
        dataset = BenchmarkDataset.from_json(dataset_path.read_text(encoding="utf-8"))
    else:
        dataset = create_smoke_fixture_dataset()

    # 2. Instantiate adapter
    if args.adapter == "replay":
        adapter: BenchmarkAdapter = ReplayFixtureAdapter()
    elif args.adapter == "engine":
        adapter = HippoEngineAdapter(collection_name=args.collection)
    else:
        raise ValueError(f"Unknown adapter {args.adapter}")

    # 3. Run benchmark
    try:
        runner = BenchmarkRunner(
            adapter=adapter,
            max_injected=args.max_injected,
            seed=args.seed,
        )
        report = runner.run(dataset, capture_trace=True)

        # 4. Save report
        out_dir = Path(args.output_dir)
        json_path, md_path = save_report(report, out_dir, base_name=f"report_{dataset.name}")
        print(f"Evaluation succeeded!")
        print(f"JSON report: {json_path}")
        print(f"Markdown summary: {md_path}")
        print("-" * 50)
        print(f"Recall@3: {report.aggregate_metrics.get('recall@3', 0.0):.4f}")
        print(f"Precision@3: {report.aggregate_metrics.get('precision@3', 0.0):.4f}")
        print(f"Forbidden Leakage@3: {report.aggregate_metrics.get('forbidden_leakage@3', 0.0):.0f}")
        print(f"Empty Accuracy@3: {report.aggregate_metrics.get('empty_accuracy@3', 0.0):.4f}")
        print("-" * 50)

        # 5. Compare against baseline if specified
        if args.baseline:
            baseline_path = Path(args.baseline)
            if not baseline_path.is_file():
                print(f"Warning: Baseline file not found: {baseline_path}", file=sys.stderr)
            else:
                baseline_report = BenchmarkReport.from_json(baseline_path.read_text(encoding="utf-8"))
                diff = compare_reports(report, baseline_report)
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
