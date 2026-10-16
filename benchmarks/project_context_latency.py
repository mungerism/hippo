"""Local routing and Hook wall-time probe; no memory backend or worker starts.

Run with the deployed interpreter to measure the deployed entry point:
  /path/to/python -m benchmarks.project_context_latency --hippo /path/to/hippo
"""

import argparse
import fcntl
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import tempfile
import time


def summarize(samples):
    ordered = sorted(samples)
    return {
        "samples": len(ordered), "median_ms": statistics.median(ordered),
        "p95_ms": ordered[math.ceil(len(ordered) * 0.95) - 1],
        "max_ms": ordered[-1],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hippo", required=True, help="Deployed executable, not a shell command")
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--host", choices=["codex", "antigravity", "pi", "zcode"], default="codex")
    parser.add_argument("--check", action="store_true", help="Exit 1 when a measured latency target fails")
    args = parser.parse_args()
    if args.samples < 1:
        parser.error("--samples must be positive")
    command = str(Path(args.hippo).expanduser().resolve(strict=True))
    with tempfile.TemporaryDirectory(prefix="hippo-context-latency-") as temporary:
        base = Path(temporary).resolve()
        repo = base / "repository"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        other_repo = base / "other-repository"
        other_repo.mkdir()
        subprocess.run(["git", "init", "-q", str(other_repo)], check=True)
        home = base / "hippo"
        home.mkdir()
        (home / "projects.json").write_text(json.dumps({
            "version": 1, "mode": "canonical", "bindings": [],
        }))
        os.environ["HIPPO_HOME"] = str(home)
        # Imports are excluded from warm resolver timings, included in CLI timings.
        from hippo_memory.project_context import ProjectContextResolver
        from hippo_memory.hooks.spool import SpoolStorage

        route_samples = []
        multi_root_samples = []
        for _ in range(args.samples):
            start = time.perf_counter()
            ProjectContextResolver().automatic(candidates=[("probe", str(repo), False)], roots=None)
            route_samples.append((time.perf_counter() - start) * 1000)
            start = time.perf_counter()
            ProjectContextResolver().automatic(
                candidates=[("probe", str(repo), False)],
                roots=[repo.as_uri(), other_repo.as_uri()],
            )
            multi_root_samples.append((time.perf_counter() - start) * 1000)

        storage = SpoolStorage(version=2)
        storage.base_dir.mkdir(parents=True, exist_ok=True)
        hook_samples = []
        # An isolated held lock prevents capture's automatic drain fallback.
        with (storage.base_dir / "worker.lock").open("a") as worker_lock:
            fcntl.flock(worker_lock, fcntl.LOCK_EX)
            for index in range(args.samples):
                event = {
                    "session_id": f"latency-{index}", "event": "Stop", "cwd": str(repo),
                    "last_assistant_message": "Synthetic latency probe completed.",
                }
                if args.host == "antigravity":
                    event = {"conversationId": f"latency-{index}", "workspacePaths": [str(repo)]}
                payload = json.dumps(event)
                start = time.perf_counter()
                result = subprocess.run(
                    [command, "hook", "capture", "--host", args.host, "--queue-version", "2"],
                    input=payload, capture_output=True, text=True,
                    cwd=repo, env=dict(os.environ), timeout=15,
                )
                hook_samples.append((time.perf_counter() - start) * 1000)
                if result.returncode != 0 or "capture error" in result.stderr:
                    raise RuntimeError(f"Hook capture failed; timing is not a successful sample: {result.stderr}")
                json.loads(result.stdout)
        # One distinct job per launch proves the benchmark timed actual persistence.
        if len(list(storage.jobs_dir.iterdir())) != args.samples:
            raise RuntimeError("Hook did not persist every measured job")
        report = {
            "host": args.host,
            "resolver_warm": summarize(route_samples),
            "resolver_warm_two_roots": summarize(multi_root_samples),
            "hook_process_wall": summarize(hook_samples),
            "resolver_p95_target_ms": 50, "hook_max_target_ms": 50,
            "memory_backend_used": False, "worker_started": False,
        }
        report["resolver_target_passed"] = max(
            report["resolver_warm"]["p95_ms"], report["resolver_warm_two_roots"]["p95_ms"],
        ) <= 50
        report["hook_target_passed"] = report["hook_process_wall"]["max_ms"] <= 50
        print(json.dumps(report, indent=2))
        if args.check and not (report["resolver_target_passed"] and report["hook_target_passed"]):
            raise SystemExit(1)


if __name__ == "__main__":
    main()
