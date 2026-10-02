"""Offline Jev-vs-baseline report; does not import or initialize model clients.

Usage:
  uv run python benchmarks/jev_relationships.py \
    --dataset benchmarks/jev_pairs_v1.json \
    --recordings benchmarks/jev_recordings_synthetic_v1.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow direct invocation from the repository root without installing a new tool.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hippo_memory.jev_evaluation import evaluate


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline pair-classification report")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--recordings", type=Path, required=True)
    args = parser.parse_args()
    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    recordings = json.loads(args.recordings.read_text(encoding="utf-8"))
    report = evaluate(dataset, recordings)
    json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
