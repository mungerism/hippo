"""Benchmark and feedback loop for Worker idle polling overhead (Issue #120).

Measures wall-clock time, CPU process time, and profiler hot spots during idle
worker poll cycles across varying historical terminal job counts (0 pending).

Usage:
  uv run python -m benchmarks.worker_idle_overhead --scale 0,500,1800 --iterations 3 --check --max-cpu-ms 25
"""

import argparse
import cProfile
import json
from pathlib import Path
import pstats
import sys
import tempfile
import time
from typing import Dict, List, Tuple

from hippo_memory.hooks.models import CapturedPayload, JobState
from hippo_memory.hooks.spool import SpoolStorage


def seed_terminal_jobs(storage: SpoolStorage, count: int) -> None:
    """Fast-seed N realistic terminal jobs (completed, skipped, dead) with zero pending."""
    states = [
        JobState.COMPLETED.value,
        JobState.COMPLETED.value,
        JobState.SKIPPED.value,
        JobState.DEAD.value,
    ]
    base_time = 1730000000.0

    for i in range(count):
        job_id = f"job-hist-{i:06d}"
        job_dir = storage.jobs_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        state = states[i % len(states)]
        payload = {
            "job_id": job_id,
            "schema_version": storage.version,
            "host": "codex" if i % 2 == 0 else "antigravity",
            "event": "Stop",
            "session_id": f"sess-{i // 5:04d}",
            "project_dir": "/tmp/bench_project",
            "created_at": base_time + i,
            "state": state,
            "routing_identity": {
                "namespace": "bench_project",
                "user_id": "bench_user",
                "context_key": "/tmp/bench_project",
                "mode": "legacy",
                "revision": "legacy",
                "segment": "main",
            },
        }
        state_data = {
            "state": state,
            "attempt": 1 if state != JobState.DEAD.value else 3,
            "updated_at": base_time + i + 1,
            "not_before": 0.0,
            "claimed_at": None,
        }

        with open(job_dir / "payload.json", "w", encoding="utf-8") as f:
            json.dump(payload, f)
        with open(job_dir / "state.json", "w", encoding="utf-8") as f:
            json.dump(state_data, f)


def execute_idle_poll_cycle(storage: SpoolStorage, router=None, short_circuit: bool = True) -> int:
    """Execute exactly one complete Worker daemon polling iteration when idle."""
    if short_circuit and not storage.has_active_jobs():
        return 0
    storage.recover_expired_leases()
    storage.coalesce_pending_jobs(router)
    now = time.time()
    pending = storage.list_jobs(state=JobState.PENDING)
    ready = [j for j in pending if j.not_before <= now]
    return len(ready)


def benchmark_scale(
    scale: int,
    iterations: int,
    enable_profile: bool = False,
    short_circuit: bool = True,
) -> Tuple[float, float, List[str]]:
    """Benchmark an isolated spool queue with `scale` terminal jobs."""
    profile_lines: List[str] = []

    with tempfile.TemporaryDirectory(prefix=f"hippo-idle-scale-{scale}-") as tmpdir:
        storage = SpoolStorage(base_dir=Path(tmpdir), version=2)
        seed_terminal_jobs(storage, scale)

        # Warm-up run
        execute_idle_poll_cycle(storage, short_circuit=short_circuit)

        # Timed iterations
        wall_times = []
        cpu_times = []

        profiler = cProfile.Profile() if enable_profile else None
        if profiler:
            profiler.enable()

        for _ in range(iterations):
            t0_wall = time.perf_counter()
            t0_cpu = time.process_time()

            pending_count = execute_idle_poll_cycle(storage, short_circuit=short_circuit)
            assert pending_count == 0, f"Expected 0 pending jobs, got {pending_count}"

            t1_wall = time.perf_counter()
            t1_cpu = time.process_time()

            wall_times.append((t1_wall - t0_wall) * 1000.0)
            cpu_times.append((t1_cpu - t0_cpu) * 1000.0)

        if profiler:
            profiler.disable()
            import io
            s = io.StringIO()
            ps = pstats.Stats(profiler, stream=s).sort_stats("cumulative")
            ps.print_stats(15)
            profile_lines = s.getvalue().splitlines()

        avg_wall = sum(wall_times) / len(wall_times)
        avg_cpu = sum(cpu_times) / len(cpu_times)
        return avg_wall, avg_cpu, profile_lines


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scale", default="0,500,1800", help="Comma-separated job counts to test")
    parser.add_argument("--iterations", type=int, default=3, help="Number of poll cycles per scale")
    parser.add_argument("--check", action="store_true", help="Fail if largest scale exceeds budget")
    parser.add_argument("--max-cpu-ms", type=float, default=25.0, help="Max allowed CPU ms per cycle for largest scale")
    parser.add_argument("--max-cycle-ms", type=float, default=35.0, help="Max allowed wall-clock ms per cycle for largest scale")
    parser.add_argument("--profile", action="store_true", help="Print cProfile top functions for largest scale")
    parser.add_argument("--no-short-circuit", action="store_true", help="Disable has_active_jobs() short-circuit to measure pure unshorted active query overhead")
    args = parser.parse_args()

    scales = [int(s.strip()) for s in args.scale.split(",") if s.strip()]
    if not scales:
        parser.error("No scales specified")

    print(f"=== Hippo Worker Idle Overhead Benchmark (Issue #120) ===")
    print(f"Iterations per scale: {args.iterations}")
    print()

    results: Dict[int, Dict[str, float]] = {}
    last_profile: List[str] = []

    for s in scales:
        is_largest = (s == max(scales))
        wall_ms, cpu_ms, prof = benchmark_scale(
            scale=s,
            iterations=args.iterations,
            enable_profile=args.profile or (is_largest and args.profile),
            short_circuit=not args.no_short_circuit,
        )
        results[s] = {"wall_ms": wall_ms, "cpu_ms": cpu_ms}
        if is_largest and prof:
            last_profile = prof

    # Summary table
    print(f"| {'Scale (Terminal Jobs)':<22} | {'Wall Time / Cycle (ms)':<22} | {'CPU Time / Cycle (ms)':<22} |")
    print(f"|:{'':-<22}:|:{'':-<22}:|:{'':-<22}:|")
    for s in scales:
        print(f"| {s:<22} | {results[s]['wall_ms']:<22.2f} | {results[s]['cpu_ms']:<22.2f} |")
    print()

    if last_profile:
        print("Top 15 Hot Spots for largest scale:")
        for line in last_profile[:20]:
            print("  " + line)
        print()

    largest = max(scales)
    largest_cpu = results[largest]["cpu_ms"]
    largest_wall = results[largest]["wall_ms"]

    if args.check:
        passed = (largest_cpu <= args.max_cpu_ms) and (largest_wall <= args.max_cycle_ms)
        if passed:
            print(f"✅ [PASS] Largest scale ({largest} jobs) within budget: CPU={largest_cpu:.2f}ms <= {args.max_cpu_ms:.1f}ms, Wall={largest_wall:.2f}ms <= {args.max_cycle_ms:.1f}ms")
            sys.exit(0)
        else:
            print(f"❌ [FAIL] Largest scale ({largest} jobs) EXCEEDED budget:")
            if largest_cpu > args.max_cpu_ms:
                print(f"   CPU time: {largest_cpu:.2f}ms > limit {args.max_cpu_ms:.1f}ms (Budget exceeded by {largest_cpu - args.max_cpu_ms:.2f}ms)")
            if largest_wall > args.max_cycle_ms:
                print(f"   Wall time: {largest_wall:.2f}ms > limit {args.max_cycle_ms:.1f}ms (Budget exceeded by {largest_wall - args.max_cycle_ms:.2f}ms)")
            sys.exit(1)


if __name__ == "__main__":
    main()
