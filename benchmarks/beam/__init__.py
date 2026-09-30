"""BEAM (Benchmark for Evaluating Agent Memory) integration package for Hippo."""

from benchmarks.beam.evaluator import (
    DEFAULT_EMBEDDING_COST_PER_1M_TOKENS,
    BeamEvaluationResult,
    BeamEvaluator,
    BeamPerformanceProfile,
    estimate_tokens_from_text,
)
from benchmarks.beam.loader import (
    BUILTIN_BEAM_FIXTURE_PATH,
    DEFAULT_CACHE_DIR,
    OFFICIAL_BEAM_128K_URL,
    BeamDataset,
    BeamMemory,
    BeamQuery,
    convert_to_beam_benchmark,
    load_beam_dataset,
)

__all__ = [
    "BUILTIN_BEAM_FIXTURE_PATH",
    "BeamDataset",
    "BeamEvaluationResult",
    "BeamEvaluator",
    "BeamMemory",
    "BeamPerformanceProfile",
    "BeamQuery",
    "DEFAULT_CACHE_DIR",
    "DEFAULT_EMBEDDING_COST_PER_1M_TOKENS",
    "OFFICIAL_BEAM_128K_URL",
    "convert_to_beam_benchmark",
    "estimate_tokens_from_text",
    "load_beam_dataset",
]
