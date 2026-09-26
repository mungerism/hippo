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
    r"^(?:DEBUG|TRACE|INFO|WARN(?:ING)?|ERROR|FATAL)\b"
    r"(?:\s+\d{4}-\d{2}-\d{2}|\s+\[|\s+[A-Za-z0-9_.-]+)",
    re.IGNORECASE,
)
CONTEXT_CONTROL_TAG_PATTERN = re.compile(
    r"</?(?:[A-Za-z][\w:-]*context[\w:-]*|admin|system|developer)\b[^>]*>",
    re.IGNORECASE,
)
INSTRUCTION_OVERRIDE_PATTERN = re.compile(
    # Imperative override directives. Anchor the action near the beginning so
    # descriptive facts such as "the sandbox blocks attempts to bypass safety"
    # remain valid memories.
    r"^\s*(?:please\s+)?(?:ignore|disregard|override|bypass)\b.{0,80}"
    r"\b(?:instruction|instructions|command|commands|prompt|prompts|policy|policies|rule|rules|safety)\b"
    r"|^\s*(?:system|developer)\s+(?:instruction|instructions|prompt|message)\s*:"
    r"|^\s*(?:please\s+)?(?:reveal|show|print|expose|dump)\b.{0,80}"
    r"\b(?:system|developer|instruction|instructions|prompt|prompts|secret|secrets|credential|credentials)\b"
    r"|(?:^|[.!?]\s+)(?:now\s+)?(?:act|behave|respond)\s+as\b",
    re.IGNORECASE | re.DOTALL,
)
TRANSIENT_ONLY_PATTERN = re.compile(
    r"^(?:(?:好的|收到|明白(?:了)?|我知道了|知道了|没问题|可以|行|谢谢|多谢)[，,！!。.;；\s]*){1,3}$"
    r"|^(?:(?:thanks(?:\s+(?:a lot|so much))?|thank you|ok(?:ay)?|got it|understood|"
    r"sounds good|looks good|great)[,!.;\s]*){1,3}$",
    re.IGNORECASE,
)


def is_raw_log(text: str) -> bool:
    """Return whether text looks like a raw operational log envelope."""
    return bool(RAW_LOG_PATTERN.search(text.strip()))


def is_instruction_like(text: str) -> bool:
    """Return whether text contains control-tag or instruction-override structure."""
    s = text.strip()
    return bool(
        CONTEXT_CONTROL_TAG_PATTERN.search(s)
        or INSTRUCTION_OVERRIDE_PATTERN.search(s)
    )


def is_transient_only(text: str) -> bool:
    """Return whether the entire utterance is only a low-information acknowledgement."""
    return bool(TRANSIENT_ONLY_PATTERN.fullmatch(text.strip()))


def is_transient_or_injection(text: str) -> bool:
    """Detect structural candidate hazards without benchmark-specific phrases."""
    return is_raw_log(text) or is_instruction_like(text) or is_transient_only(text)


def extract_explicit_identity_subjects(query: str) -> set[str]:
    """Extract only high-confidence identity/project subjects from query grammar.

    This is intentionally narrower than generic proper-noun detection. We only
    accept explicit possessives, labelled identities (user/project/repo), or an
    auxiliary-verb subject in a small set of factual constructions. This avoids
    treating sentence starters such as "Current" as identities.
    """
    subjects: set[str] = set()

    for match in re.finditer(r"\b([A-Z][A-Za-z0-9_.-]{1,})['’]s\b", query):
        subjects.add(match.group(1).lower())

    for match in re.finditer(
        r"\b(?:user|project|repo|repository)\s+([A-Za-z0-9_.-]{2,})\b",
        query,
        re.IGNORECASE,
    ):
        subjects.add(match.group(1).lower())

    for match in re.finditer(
        r"\b(?:does|did|is|are|was|were|has|have|can|could|should|would|will)\s+"
        r"([A-Z][A-Za-z0-9_.-]{1,})\s+"
        r"(?:use|prefer|run|require|work|listen|store|deploy)\b",
        query,
        re.IGNORECASE,
    ):
        # Require capitalization in the original capture for this unlabeled form.
        raw_subject = match.group(1)
        if raw_subject[:1].isupper():
            subjects.add(raw_subject.lower())

    return subjects


def _candidate_identity_tokens(item: Mapping[str, Any], text: str) -> set[str]:
    """Collect identity tokens explicitly present in candidate text/metadata."""
    tokens = set(re.findall(r"\b[A-Za-z0-9_.-]+\b", text.lower()))
    metadata = item.get("metadata")
    metadata_map = metadata if isinstance(metadata, Mapping) else {}

    for key in ("user_id", "project_id", "agent_id"):
        for source in (item, metadata_map):
            value = source.get(key) if isinstance(source, Mapping) else None
            if isinstance(value, str) and value.strip():
                tokens.add(value.strip().lower())

    return tokens


def extract_query_content_and_entities(query: str) -> tuple[set[str], set[str], set[str]]:
    """Extract lexical content and CJK characters from a query.

    The middle return value is intentionally an empty set. Production retrieval
    must not infer "primary entities" from capitalization or word position:
    sentence starters and ordinary nouns are too easy to misclassify, which can
    turn a relevance heuristic into a destructive hard filter. Entity-aware
    relaxation remains available through Mem0's explicit entity_boost signal.

    Returns:
        (content_tokens, empty_entity_set, cjk_chars)
    """
    raw_tokens = re.findall(r"\b[A-Za-z0-9_\.\-]+\b", query)
    content_tokens = {
        token.lower()
        for token in raw_tokens
        if len(token) > 1 and token.lower() not in GATE_STOP_WORDS
    }
    cjk_chars = set(re.findall(r"[\u4e00-\u9fff]", query))
    return content_tokens, set(), cjk_chars


def _is_valid_numeric(val: Any) -> bool:
    """Check whether a value is a valid, finite float or int (not bool)."""
    return isinstance(val, (int, float)) and not isinstance(val, bool) and math.isfinite(val)


@dataclass(frozen=True, slots=True)
class SearchGateConfig:
    """Configuration for search relevance gating."""

    final_threshold: float = 0.32
    dense_only_threshold: float = 0.62
    relative_threshold_ratio: float = 0.50
    lexical_min_coverage: float = 0.35
    lexical_bm25_threshold: float = 0.15
    lexical_semantic_threshold: float = 0.48
    enabled: bool = True

    def __post_init__(self):
        for name in (
            "final_threshold",
            "dense_only_threshold",
            "relative_threshold_ratio",
            "lexical_min_coverage",
            "lexical_bm25_threshold",
            "lexical_semantic_threshold",
        ):
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
    # Query-side rejection is deliberately narrow. Users may legitimately ask
    # about prompt injection or safety policies; instruction-like wording alone
    # must not suppress a substantive retrieval request.
    query_unwanted = (
        is_raw_log(query) or is_transient_only(query) or is_instruction_like(query)
        if query
        else False
    )
    q_content, _q_entities, q_cjk = (
        extract_query_content_and_entities(query) if query else (set(), set(), set())
    )
    q_identity_subjects = extract_explicit_identity_subjects(query) if query else set()

    if query_unwanted:
        # Fast fail-closed: transient acknowledgement or injection query yields zero long-term memory recall
        for i, item in enumerate(results):
            cid = str(item.get("id", f"unknown_{i}")) if isinstance(item, Mapping) else f"unknown_{i}"
            score = float(item["score"]) if isinstance(item, Mapping) and _is_valid_numeric(item.get("score")) else None
            decisions.append(GateDecision(memory_id=cid, accepted=False, reason="query_transient_log_or_directive", final_score=score))
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

        if query and q_identity_subjects:
            candidate_identity_tokens = _candidate_identity_tokens(item, cand_text)
            if not (q_identity_subjects & candidate_identity_tokens):
                decisions.append(
                    GateDecision(
                        memory_id=cid,
                        accepted=False,
                        reason="explicit_identity_subject_mismatch",
                        final_score=final_score,
                        details=dict(details),
                    )
                )
                continue

        if query:
            cand_tokens = set(re.findall(r"\b[A-Za-z0-9_\.\-]+\b", cand_text.lower()))
            cand_cjk = set(re.findall(r"[\u4e00-\u9fff]", cand_text))

            content_overlap = q_content & cand_tokens
            cjk_overlap = q_cjk & cand_cjk

            token_cov = len(content_overlap) / len(q_content) if q_content else 0.0
            cjk_cov = len(cjk_overlap) / len(q_cjk) if q_cjk else 0.0
            effective_coverage = max(token_cov, cjk_cov)

            # Signal 1: Strong Entity Support (explicit KG entity boost).
            # We deliberately avoid heuristic "entity mismatch" rejection based on
            # capitalization or token position; those heuristics caused valid
            # production queries to be dropped.
            has_strong_entity = (entity_boost > 0.0)

            # Signal 3: Meaningful Lexical Support
            has_meaningful_lexical = (
                (
                    effective_coverage >= cfg.lexical_min_coverage
                    or (len(q_content) <= 2 and len(content_overlap) >= 1)
                    or (len(q_cjk) <= 4 and len(cjk_overlap) >= 2)
                )
                and bm25_score >= cfg.lexical_bm25_threshold
            )

            if has_strong_entity:
                absolute_pass = (final_score >= cfg.final_threshold)
                fail_reason = "final_threshold_failed"
            elif has_meaningful_lexical:
                absolute_pass = (
                    final_score >= cfg.final_threshold
                    and semantic_score >= cfg.lexical_semantic_threshold
                )
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
