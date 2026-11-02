"""Opt-in native Pi CLI acceptance with deterministic providers and isolated state.

Run: uv run python -m tests.pi_native_acceptance /absolute/path/to/pi
No personal Pi settings, production spool, external model calls or credentials.
"""

import fcntl
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from hippo_memory.hooks import JobState, SpoolStorage, SpoolWorker
from hippo_memory.project_context import ProjectContextResolver
from tests.test_support import create_contract_engine


PROVIDER = '''import { createAssistantMessageEventStream } from "@earendil-works/pi-ai";
export default function (pi) {
  pi.registerProvider("hippo-fixture", {
    baseUrl: "http://127.0.0.1:1", api: "hippo-fixture-api", apiKey: "fixture-not-a-secret",
    models: [{ id: "deterministic", name: "Offline fixture", reasoning: false,
      input: ["text"], contextWindow: 10000, maxTokens: 100,
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } }],
    streamSimple(model) {
      const stream = createAssistantMessageEventStream();
      const message = { role: "assistant", content: [{ type: "text", text: "已确认：项目统一使用 uv 管理依赖。" }],
        api: model.api, provider: model.provider, model: model.id, stopReason: "stop", timestamp: Date.now(),
        usage: { input: 1, output: 1, cacheRead: 0, cacheWrite: 0, totalTokens: 2,
          cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } } };
      queueMicrotask(() => { stream.push({ type: "start", partial: message });
        stream.push({ type: "done", reason: "stop", message }); stream.end(); });
      return stream;
    }
  });
}
'''


def main(pi_bin: str) -> None:
    source = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="hippo-pi-native-") as directory:
        base = Path(directory).resolve()
        repo = base / "project"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        home = base / "hippo"
        home.mkdir()
        bindings = home / "projects.json"
        bindings.write_text(json.dumps({"version": 1, "mode": "canonical", "bindings": []}))
        provider = base / "provider.ts"
        provider.write_text(PROVIDER)
        binary = base / "hippo-bin"
        binary.write_text(f"#!/bin/sh\nexport PYTHONPATH={shlex.quote(str(source))}\nexec {shlex.quote(sys.executable)} -m hippo_memory.cli \"$@\"\n")
        binary.chmod(0o755)
        environment = {k: v for k, v in os.environ.items() if k in (
            "PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "NODE_OPTIONS",
        )}
        environment.update(
            HIPPO_HOME=str(home), HIPPO_BIN=str(binary), HIPPO_QUEUE_VERSION="2",
            HIPPO_DISABLE_RECOVERY_WAKEUP="1", PI_CODING_AGENT_DIR=str(base / "pi-agent"), PI_OFFLINE="1",
            MEM0_TELEMETRY="false", POSTHOG_DISABLED="1",
        )
        storage = SpoolStorage(home / "spool", version=2)
        # Keep detached captures from auto-starting an external model Worker.
        with storage.lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = subprocess.run([
                pi_bin, "--offline", "--print", "--no-extensions", "--no-mcp", "--no-tools",
                "--no-skills", "--no-context-files", "--no-prompt-templates", "--no-themes",
                "--session-dir", str(base / "sessions"),
                "-e", str(provider), "-e", str(source / "integrations/pi/hippo-memory.ts"),
                "--provider", "hippo-fixture", "--model", "deterministic",
                "项目长期规范：统一使用 uv 管理 Python 依赖。",
            ], cwd=repo, env=environment, capture_output=True, text=True, timeout=30)
            if result.returncode:
                raise AssertionError(f"Native Pi failed: {result.stderr[-3000:]} {result.stdout[-1000:]}")
            for _ in range(100):
                jobs = storage.list_jobs()
                if len(jobs) >= 2:
                    break
                time.sleep(.02)
        assert len(jobs) == 2, f"Expected both lifecycle captures, got {len(jobs)}; {result.stderr[-2000:]}"
        assert {job.event for job in jobs} == {"Stop", "SessionEnd"}
        assert all(job.turns_captured and len(job.turns) == 2 for job in jobs)
        assert all(job.project_dir == str(repo) and job.transcript_leaf_id for job in jobs)
        engine = create_contract_engine()
        engine.memory.llm.queue_response({"memory": [{"text": "项目统一使用 uv 管理 Python 依赖。"}]})
        with patch("hippo_memory.project_context.bindings_path", return_value=bindings):
            namespace = ProjectContextResolver().path_project(str(repo), "fixture").namespace
            SpoolWorker(storage, engine).drain(wait_for_retries=False)
            completed = storage.list_jobs(JobState.COMPLETED)
            assert len(completed) == 1
            assert completed[0].routing_identity["namespace"] == namespace
            assert engine.search("uv", scope="project", project_id=str(repo))
            assert engine.search("uv", scope="project", project_id="other-project") == []
        assert len(list(storage.receipts_dir.glob("*.json"))) == 1
        assert storage.enqueue(storage.load_payload(jobs[0].job_id))[0] is False
        print("Native Pi acceptance passed: both events, two turns, canonical project, deterministic Mem0 receipt, scope isolation and deduplication.")


if __name__ == "__main__":
    main(sys.argv[1])
