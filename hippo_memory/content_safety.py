"""Hippo Content Safety - Shared structural hazard classifiers for retrieval and persistence gates.

Extracts text-structure safety classifiers (raw operational logs, XML/context control tags,
imperative instruction overrides, and transient acknowledgements) so that both the search gate
and persistence quality gate share identical, audited detection logic without benchmark-specific hacks.
"""

import re

# Raw operational logs (e.g. DEBUG 2026-09-25 10:14:38 [urllib3] ...,
# 2026-03-29 10:00:00.123 [INFO] ..., Traceback (most recent call last): ..., TRACE [qdrant::index] ...)
RAW_LOG_PATTERN = re.compile(
    r"^(?:(?:\[\s*)?(?:\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?\s*\]?\s*)?"
    r"\[?\s*(?:DEBUG|TRACE|INFO|WARN(?:ING)?|ERROR|FATAL)\b\s*\]?"
    r"|Traceback\s*\(most\s+recent\s+call\s+last\):"
    r"|^\s*at\s+[a-zA-Z0-9_$.]+\([a-zA-Z0-9_$.]+:\d+\)"
    r"|^\s*panic:\s+runtime\s+error:)",
    re.IGNORECASE | re.MULTILINE,
)

# Context boundary escape tags or administrative control tags
CONTEXT_CONTROL_TAG_PATTERN = re.compile(
    r"</?(?:[A-Za-z][\w:-]*context[\w:-]*|admin|system|developer)\b[^>]*>",
    re.IGNORECASE,
)

# Imperative instruction overrides and prompt injection directives.
# Anchored near the beginning so descriptive facts (e.g. "the sandbox blocks attempts to bypass safety")
# remain valid memories.
INSTRUCTION_OVERRIDE_PATTERN = re.compile(
    r"(?:^|[.!?:]\s*)(?:please\s+)?(?:ignore|disregard|override|bypass)\b.{0,80}"
    r"\b(?:instruction|instructions|command|commands|prompt|prompts|policy|policies|rule|rules|safety|gate|gates)\b"
    r"|^\s*(?:\[?\s*important\s*\]?\s*)?(?:system|developer|admin)\s+(?:instruction|instructions|prompt|message|override)\s*:"
    r"|^\s*(?:please\s+)?(?:reveal|show|print|expose|dump)\b.{0,80}"
    r"\b(?:system|developer|instruction|instructions|prompt|prompts|secret|secrets|credential|credentials)\b"
    r"|^\s*(?:you\s+are\s+now\s+in|enter|switch\s+to)\s+(?:developer|dan|jailbreak|unrestricted|god|admin)\s+mode\b"
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
    """Return whether text looks like a raw operational log envelope."""
    if not text:
        return False
    return bool(RAW_LOG_PATTERN.search(text.strip()))


def is_context_control_tag(text: str) -> bool:
    """Return whether text contains XML/context control or breakout tags."""
    if not text:
        return False
    return bool(CONTEXT_CONTROL_TAG_PATTERN.search(text.strip()))


def is_instruction_override(text: str) -> bool:
    """Return whether text contains imperative instruction overrides or injection directives."""
    if not text:
        return False
    return bool(INSTRUCTION_OVERRIDE_PATTERN.search(text.strip()))


def is_instruction_like(text: str) -> bool:
    """Return whether text contains control-tag or instruction-override structure."""
    if not text:
        return False
    s = text.strip()
    return bool(
        CONTEXT_CONTROL_TAG_PATTERN.search(s)
        or INSTRUCTION_OVERRIDE_PATTERN.search(s)
    )


def is_transient_only(text: str) -> bool:
    """Return whether the entire utterance is only a low-information acknowledgement."""
    if not text:
        return False
    return bool(TRANSIENT_ONLY_PATTERN.fullmatch(text.strip()))


def is_transient_or_injection(text: str) -> bool:
    """Detect structural candidate hazards without benchmark-specific phrases."""
    return is_raw_log(text) or is_instruction_like(text) or is_transient_only(text)
