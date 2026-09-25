"""Relevance gate for Hippo memory retrieval.

Provides deterministic, signal-aware anti-pollution filtering over Mem0
hybrid search results, preventing low-confidence tails from polluting
agent contexts while preserving direct semantic answers.
"""

from dataclasses import dataclass
import logging
import math
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)

GATE_SENTENCE_STARTERS = {
    "what", "how", "where", "which", "when", "who", "why", "is", "are", "do",
    "does", "did", "can", "could", "would", "should", "will", "the", "a", "an",
    "in", "on", "at", "for", "to", "list", "show", "describe", "explain", "find",
    "please", "tell", "give", "name", "resetting", "segment", "trace", "debug",
}

GATE_GENERIC_ACRONYMS = {"ide", "mcp", "pr", "ci", "cli", "api", "ui", "ux", "os", "io", "db"}

GATE_STOP_WORDS = {
    "a", "about", "above", "after", "again", "against", "all", "am", "an", "and",
    "any", "are", "aren't", "as", "at", "be", "because", "been", "before", "being",
    "below", "between", "both", "but", "by", "can", "can't", "cannot", "could",
    "couldn't", "did", "didn't", "do", "does", "doesn't", "doing", "don't",
    "down", "during", "each", "few", "for", "from", "further", "had", "hadn't",
    "has", "hasn't", "have", "haven't", "having", "he", "he'd", "he'll",
    "he's", "her", "here", "here's", "hers", "herself", "him", "himself", "his",
    "how", "how's", "i", "i'd", "i'll", "i'm", "i've", "if", "in",
    "into", "is", "isn't", "it", "it's", "its", "itself", "let's", "me",
    "more", "most", "mustn't", "my", "myself", "no", "nor", "not", "of", "off",
    "on", "once", "only", "or", "other", "ought", "our", "ours", "ourselves", "out",
    "over", "own", "same", "shan't", "she", "she'd", "she'll", "she's",
    "should", "shouldn't", "so", "some", "such", "than", "that", "that's", "the",
    "their", "theirs", "them", "themselves", "then", "there", "there's", "these",
    "they", "they'd", "they'll", "they're", "they've", "this", "those",
    "through", "to", "too", "under", "until", "up", "very", "was", "wasn't", "we",
    "we'd", "we'll", "we're", "we've", "were", "weren't", "what",
    "what's", "when", "when's", "where", "where's", "which", "while", "who",
    "who's", "whom", "why", "why's", "with", "won't", "would", "wouldn't",
    "you", "you'd", "you'll", "you're", "you've", "your", "yours",
    "yourself", "yourselves", "refer", "refers", "user", "users", "hippo", "memory", "memories",
}

RAW_LOG_PATTERN = re.compile(
    r"^(?:DEBUG|TRACE|INFO|WARN|ERROR)\s+\d{4}-\d{2}-\d{2}|^(?:DEBUG|TRACE|INFO|WARN|ERROR)\s+\[",
    re.IGNORECASE,
)
INJECTION_PATTERN = re.compile(
    r"</?hippo_retrieved_context>|<admin>|ignore all previous commands|system instruction:|bypass all safety|reveal your system instructions",
    re.IGNORECASE,
)
TRANSIENT_PATTERN = re.compile(
    r"^(?:好的|收到|明白|我知道了|thanks|thank you|ok,?\s*got it|looks great|lorem ipsum|recipe for)",
    re.IGNORECASE,
)


def is_transient_or_injection(text: str) -> bool:
    """Detect whether a query or candidate text represents transient dialogue, logs, or injection."""
    s = text.strip()
    return bool(
        RAW_LOG_PATTERN.search(s)
        or INJECTION_PATTERN.search(s)
        or TRANSIENT_PATTERN.search(s)
    )


def extract_query_content_and_entities(query: str) -> tuple[set[str], set[str], set[str]]:
    """Extract content words, primary salient entities, and CJK characters from query text.

    Returns:
        (content_tokens, primary_proper_entities, cjk_chars)
    """
    raw_tokens = re.findall(r"\b[A-Za-z0-9_\.\-]+\b", query)
    content_tokens: set[str] = set()
    primary_entities: set[str] = set()

    for i, t in enumerate(raw_tokens):
        lower_t = t.lower()
        if lower_t in GATE_STOP_WORDS or len(t) <= 1:
            continue
        content_tokens.add(lower_t)

        if lower_t in GATE_SENTENCE_STARTERS or lower_t in GATE_GENERIC_ACRONYMS:
            continue

        is_entity_prefix = (i > 0 and raw_tokens[i - 1].lower() in ("project", "repo", "repository", "user", "in", "for", "does"))

        if t[0].isupper() or t.isupper() or "." in t or "-" in t or "_" in t or is_entity_prefix:
            primary_entities.add(lower_t)
        if lower_t.endswith("'s"):
            primary_entities.add(lower_t[:-2])

    cjk_chars = set(re.findall(r"[\u4e00-\u9fff]", query))
    return content_tokens, primary_entities, cjk_chars


def _is_valid_numeric(val: Any) -> bool:
    """Check whether a value is a valid, finite float or int (not bool)."""
    return isinstance(val, (int, float)) and not isinstance(val, bool) and math.isfinite(val)


@dataclass(frozen=True, slots=True)
class SearchGateConfig:
    """Configuration for search relevance gating."""

    final_threshold: float = 0.32
    dense_only_threshold: float = 0.62
    relative_threshold_ratio: float = 0.50
    enabled: bool = True

    def __post_init__(self):
        for name in ("final_threshold", "dense_only_threshold", "relative_threshold_ratio"):
            val = getattr(self, name)
            if not _is_valid_numeric(val):
                raise ValueError(f"{name} must be a finite float, got {val!r}")
            if not (0.0 <= val <= 1.0):
                raise ValueError(f"{name} must be in range [0.0, 1.0], got {val}")


@dataclass(frozen=True, slots=True)
class GateDecision:
    """Record of a gate filtering decision for a single retrieval candidate."""

    memory_id: str
    accepted: bool
    reason: str
    final_score: Optional[float] = None
    details: Optional[Dict[str, Any]] = None


def filter_search_results_with_details(
    results: Sequence[Mapping[str, Any]],
    *,
    config: Optional[SearchGateConfig] = None,
    limit: int = 5,
    query: Optional[str] = None,
) -> tuple[List[Dict[str, Any]], List[GateDecision]]:
    """Filter candidate memories through the relevance gate and return detailed decisions.

    Provides deterministic, signal-aware anti-pollution filtering over Mem0
    hybrid search results, preventing low-confidence tails and weak lexical collisions
    from polluting agent contexts while preserving direct semantic answers.

    Args:
        results: Sequence of candidate dictionaries returned by Mem0.
        config: Optional SearchGateConfig instance.
        limit: Maximum number of accepted memories to return.
        query: Optional natural language search query string for lexical/entity alignment.

    Returns:
        Tuple of (accepted_candidates_list, list_of_gate_decisions).
    """
    if limit <= 0 or not results:
        return [], []

    cfg = config if config is not None else SearchGateConfig()
    decisions: List[GateDecision] = []

    if not cfg.enabled:
        accepted = [dict(item) for item in results[:limit]]
        for i, item in enumerate(results):
            cid = str(item.get("id", f"unknown_{i}")) if isinstance(item, Mapping) else f"unknown_{i}"
            score = float(item["score"]) if isinstance(item, Mapping) and _is_valid_numeric(item.get("score")) else None
            if i < limit:
                decisions.append(GateDecision(memory_id=cid, accepted=True, reason="gate_disabled", final_score=score))
            else:
                decisions.append(GateDecision(memory_id=cid, accepted=False, reason="truncated_by_limit", final_score=score))
        return accepted, decisions

    # Pre-parse query context if provided
    query_unwanted = is_transient_or_injection(query) if query else False
    q_content, q_primary_entities, q_cjk = (
        extract_query_content_and_entities(query) if query else (set(), set(), set())
    )

    if query_unwanted:
        # Fast fail-closed: transient acknowledgement or injection query yields zero long-term memory recall
        for i, item in enumerate(results):
            cid = str(item.get("id", f"unknown_{i}")) if isinstance(item, Mapping) else f"unknown_{i}"
            score = float(item["score"]) if isinstance(item, Mapping) and _is_valid_numeric(item.get("score")) else None
            decisions.append(GateDecision(memory_id=cid, accepted=False, reason="query_transient_or_injection", final_score=score))
        return [], decisions

    # Step 1: Structure validation and signal-aware absolute pass gate
    passed_absolute: List[tuple[Mapping[str, Any], float]] = []

    for i, item in enumerate(results):
        if not isinstance(item, Mapping):
            decisions.append(GateDecision(memory_id=f"unknown_{i}", accepted=False, reason="not_a_mapping"))
            continue

        cid = str(item.get("id", f"unknown_{i}"))
        score = item.get("score")
        details = item.get("score_details")

        if not isinstance(details, Mapping) or not _is_valid_numeric(score):
            logger.debug("Rejecting candidate %s: missing or invalid score/score_details", cid)
            decisions.append(GateDecision(memory_id=cid, accepted=False, reason="missing_or_invalid_score_details"))
            continue

        required_fields = ("final_score", "semantic_score", "bm25_score", "entity_boost")
        if any(field not in details for field in required_fields):
            logger.debug("Rejecting candidate %s: missing required fields in score_details", cid)
            decisions.append(GateDecision(memory_id=cid, accepted=False, reason="missing_required_score_fields"))
            continue

        raw_final_score = details["final_score"]
        raw_semantic_score = details["semantic_score"]
        raw_bm25_score = details["bm25_score"]
        raw_entity_boost = details["entity_boost"]

        if (
            not _is_valid_numeric(raw_final_score)
            or not _is_valid_numeric(raw_semantic_score)
            or not _is_valid_numeric(raw_bm25_score)
            or not _is_valid_numeric(raw_entity_boost)
        ):
            logger.debug("Rejecting candidate %s: non-numeric score fields in details", cid)
            decisions.append(GateDecision(memory_id=cid, accepted=False, reason="non_numeric_score_fields"))
            continue

        if abs(float(score) - float(raw_final_score)) > 1e-4:
            logger.debug(
                "Rejecting candidate %s: top-level score (%s) != final_score (%s)",
                cid,
                score,
                raw_final_score,
            )
            decisions.append(GateDecision(memory_id=cid, accepted=False, reason="score_consistency_mismatch"))
            continue

        final_score = float(raw_final_score)
        semantic_score = float(raw_semantic_score)
        bm25_score = float(raw_bm25_score)
        entity_boost = float(raw_entity_boost)

        cand_text = str(item.get("memory") or item.get("text") or "")
        if query and is_transient_or_injection(cand_text):
            decisions.append(GateDecision(memory_id=cid, accepted=False, reason="candidate_transient_or_log_pollution", final_score=final_score, details=dict(details)))
            continue

        if query:
            cand_tokens = set(re.findall(r"\b[A-Za-z0-9_\.\-]+\b", cand_text.lower()))
            cand_cjk = set(re.findall(r"[\u4e00-\u9fff]", cand_text))

            content_overlap = q_content & cand_tokens
            primary_overlap = q_primary_entities & cand_tokens
            cjk_overlap = q_cjk & cand_cjk

            token_cov = len(content_overlap) / len(q_content) if q_content else 0.0
            cjk_cov = len(cjk_overlap) / len(q_cjk) if q_cjk else 0.0
            effective_coverage = max(token_cov, cjk_cov)

            # Signal 1: Primary Proper Entity Mismatch (e.g. query asked for Bob/Carol/Redis, candidate completely misses it)
            if len(q_primary_entities) > 0 and len(primary_overlap) == 0:
                decisions.append(GateDecision(memory_id=cid, accepted=False, reason="primary_entity_mismatch", final_score=final_score, details=dict(details)))
                continue

            # Signal 2: Strong Entity Support (explicit KG entity boost)
            has_strong_entity = (entity_boost > 0.0)

            # Signal 3: Meaningful Lexical Support
            has_meaningful_lexical = (
                (
                    effective_coverage >= 0.35
                    or (len(q_content) <= 2 and len(content_overlap) >= 1)
                    or (len(q_cjk) <= 4 and len(cjk_overlap) >= 2)
                )
                and bm25_score >= 0.15
            )

            if has_strong_entity:
                absolute_pass = (final_score >= cfg.final_threshold)
                fail_reason = "final_threshold_failed"
            elif has_meaningful_lexical:
                absolute_pass = (final_score >= cfg.final_threshold) and (semantic_score >= 0.48)
                fail_reason = "lexical_semantic_threshold_failed"
            elif bm25_score == 0.0:
                # Signal 3: Pure Dense Candidate (cross-lingual, synonyms, or conceptual answer)
                absolute_pass = (semantic_score >= cfg.dense_only_threshold) and (final_score >= cfg.final_threshold)
                fail_reason = "dense_threshold_failed"
            else:
                # Signal 4: Weak Lexical Collision (spurious BM25 without sufficient content overlap or entity support)
                absolute_pass = False
                fail_reason = "weak_lexical_collision_failed"

            if not absolute_pass:
                decisions.append(GateDecision(memory_id=cid, accepted=False, reason=fail_reason, final_score=final_score, details=dict(details)))
                continue
        else:
            # Backward-compatible score-only path (when no query string is available)
            has_support = (bm25_score > 0.0) or (entity_boost > 0.0)
            if has_support:
                absolute_pass = final_score >= cfg.final_threshold
                if not absolute_pass:
                    decisions.append(
                        GateDecision(
                            memory_id=cid,
                            accepted=False,
                            reason="final_threshold_failed",
                            final_score=final_score,
                            details=dict(details),
                        )
                    )
                    continue
            else:
                if semantic_score < cfg.dense_only_threshold:
                    decisions.append(
                        GateDecision(
                            memory_id=cid,
                            accepted=False,
                            reason="dense_threshold_failed",
                            final_score=final_score,
                            details=dict(details),
                        )
                    )
                    continue
                elif final_score < cfg.final_threshold:
                    decisions.append(
                        GateDecision(
                            memory_id=cid,
                            accepted=False,
                            reason="final_threshold_failed",
                            final_score=final_score,
                            details=dict(details),
                        )
                    )
                    continue

        passed_absolute.append((item, final_score))

    if not passed_absolute:
        return [], decisions

    # Step 2: Relative pass gate
    best_final_score = max(sc for _, sc in passed_absolute)
    relative_floor = (
        cfg.relative_threshold_ratio * best_final_score
        if best_final_score > 0.0
        else 0.0
    )

    accepted: List[Dict[str, Any]] = []
    for item, final_score in passed_absolute:
        cid = str(item.get("id"))
        dt = item.get("score_details")
        dt_dict = dict(dt) if isinstance(dt, Mapping) else {}
        if final_score < relative_floor:
            decisions.append(
                GateDecision(
                    memory_id=cid,
                    accepted=False,
                    reason="relative_floor_failed",
                    final_score=final_score,
                    details=dt_dict,
                )
            )
        else:
            if len(accepted) < limit:
                accepted.append(dict(item))
                decisions.append(
                    GateDecision(
                        memory_id=cid,
                        accepted=True,
                        reason="passed",
                        final_score=final_score,
                        details=dt_dict,
                    )
                )
            else:
                decisions.append(
                    GateDecision(
                        memory_id=cid,
                        accepted=False,
                        reason="truncated_by_limit",
                        final_score=final_score,
                        details=dt_dict,
                    )
                )

    return accepted, decisions


def filter_search_results(
    results: Sequence[Mapping[str, Any]],
    *,
    config: Optional[SearchGateConfig] = None,
    limit: int = 5,
    query: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Filter raw hybrid retrieval candidates through signal-aware safety gate (zero-overhead production path).

    Args:
        results: Sequence of candidate dictionaries returned by Mem0.
        config: Optional SearchGateConfig instance (defaults to standard thresholds).
        limit: Maximum number of accepted memories to return.
        query: Optional natural language search query string for lexical/entity alignment.

    Returns:
        New list of accepted candidate dictionaries, preserving original ranking.
    """
    accepted, _ = filter_search_results_with_details(
        results,
        config=config,
        limit=limit,
        query=query,
    )
    return accepted
