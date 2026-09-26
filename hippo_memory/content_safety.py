"""Hippo Content Safety - Shared structural hazard classifiers for retrieval and persistence gates.

These classifiers intentionally detect *structural* hazards only. They must remain
conservative because the same helpers are used by both retrieval and persistence.
Descriptive engineering facts that merely discuss logs, control tags, or prompt
injection must not be classified as attacks.
"""

import re

# A raw-log candidate must START like an operational log/trace.  Do not use a
# MULTILINE search here: durable prose may legitimately quote a log line later
# in the text (for example, a troubleshooting rule followed by an INFO sample).
RAW_LOG_PATTERN = re.compile(
    r"^(?:(?:\[\s*)?(?:\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?"
    r"(?:Z|[+-]\d{2}:?\d{2})?\s*\]?\s*)?"
    r"\[?\s*(?:DEBUG|TRACE|INFO|WARN(?:ING)?|ERROR|FATAL)\b\s*\]?"
    r"|Traceback\s*\(most\s+recent\s+call\s+last\):"
    r"|\s*at\s+[a-zA-Z0-9_$.]+\([a-zA-Z0-9_$.]+:\d+\)"
    r"|\s*panic:\s+runtime\s+error:)",
    re.IGNORECASE,
)

# Context boundary escape tags or administrative control tags.  The helper
# below requires the tag to be the first structural token; mentioning a literal
# tag inside a normal declarative sentence is therefore allowed.
CONTEXT_CONTROL_TAG_PATTERN = re.compile(
    r"</?(?:[A-Za-z][\w:-]*context[\w:-]*|admin|system|developer)\b[^>]*>",
    re.IGNORECASE,
)

# Imperative instruction overrides and prompt-injection directives.  Keep the
# dangerous imperative anchored at the beginning (apart from the historical
# "act/respond as" clause) so descriptive security facts remain retrievable.
INSTRUCTION_OVERRIDE_PATTERN = re.compile(
    r"^\s*(?:please\s+)?(?:ignore|disregard|override|bypass)\b.{0,80}"
    r"\b(?:instruction|instructions|command|commands|prompt|prompts|policy|policies|rule|rules|safety|gate|gates)\b"
    r"|^\s*(?:\[?\s*important\s*\]?\s*)?(?:system|developer|admin)\s+"
    r"(?:instruction|instructions|prompt|message|override)\s*:"
    r"|^\s*(?:important\s+)?system\s+override\s*:"
    r"|^\s*(?:please\s+)?(?:reveal|show|print|expose|dump)\b.{0,80}"
    r"\b(?:system|developer|instruction|instructions|prompt|prompts|secret|secrets|credential|credentials)\b"
    r"|^\s*(?:you\s+are\s+now\s+in|enter|switch\s+to)\s+"
    r"(?:developer|dan|jailbreak|unrestricted|god|admin)\s+mode\b"
    r"|(?:^|[.!?]\s+)(?:now\s+)?(?:act|behave|respond)\s+as\b",
    re.IGNORECASE | re.DOTALL,
)

# Low-information transient acknowledgements (single to repeated conversational flotsam)
TRANSIENT_ONLY_PATTERN = re.compile(
    r"^(?:(?:好的|收到|明白(?:了)?|我知道了|知道了|没问题|可以|行|谢谢|多谢)[，,！!。.;；\s]*){1,3}$"
    r"|^(?:(?:thanks(?:\s+(?:a lot|so much))?|thank you|ok(?:ay)?|got it|understood|"
    r"sounds good|looks good|great)[,!.;\s]*){1,3}$",
    re.IGNORECASE,
)


def is_raw_log(text: str) -> bool:
    """Return whether the candidate itself starts as a raw operational log."""
    if not text:
        return False
    return bool(RAW_LOG_PATTERN.match(text.strip()))


def is_context_control_tag(text: str) -> bool:
    """Return whether text starts with a context/control breakout tag.

    A normal sentence that quotes a tag later in the text is descriptive data,
    not a structural breakout attempt.
    """
    if not text:
        return False
    return bool(CONTEXT_CONTROL_TAG_PATTERN.match(text.strip()))


def is_instruction_override(text: str) -> bool:
    """Return whether text contains an imperative instruction override."""
    if not text:
        return False
    return bool(INSTRUCTION_OVERRIDE_PATTERN.search(text.strip()))


def is_instruction_like(text: str) -> bool:
    """Return whether text has structural control-tag or override form."""
    if not text:
        return False
    s = text.strip()
    return is_context_control_tag(s) or is_instruction_override(s)


def is_transient_only(text: str) -> bool:
    """Return whether the entire utterance is only a low-information acknowledgement."""
    if not text:
        return False
    return bool(TRANSIENT_ONLY_PATTERN.fullmatch(text.strip()))


def is_transient_or_injection(text: str) -> bool:
    """Detect structural candidate hazards without benchmark-specific phrases."""
    return is_raw_log(text) or is_instruction_like(text) or is_transient_only(text)
