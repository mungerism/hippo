"""Persistence quality gates for Warm Path session distillation.

The pre-check is deliberately conservative: it skips only sessions whose entire
visible content is high-confidence noise. Semantic extraction remains the LLM's
job. The post-audit is a final structural ACCEPT/DROP guard and never rewrites
text after embeddings have been produced.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from hippo_memory.content_safety import (
    is_context_control_tag,
    is_instruction_override,
    is_raw_log,
    is_transient_only,
)

# Pure transient phrases (normalized, case-insensitive)
TRANSIENT_PHRASES: set[str] = {
    "好的", "收到", "明白了", "明白", "我知道了", "知道了", "稍等",
    "正在处理", "正在运行", "正在执行", "正在查看", "正在运行测试", "继续",
    "没问题", "可以", "行", "谢谢", "多谢", "这就去办", "这就开始",
    "马上处理", "这就处理",
    "ok", "okay", "sure", "got it", "done", "working on it", "understood",
    "looks good", "sounds good", "thanks", "thank you", "great", "confirmed",
    "let's discuss in the standup meeting", "let's discuss in the standup",
    "i will start working on it",
    "运行测试", "跑测试", "跑下测试", "查看代码", "检查文件", "查看状态",
    "running tests", "checking files", "analyzing codebase", "fetching docs",
}

_TRANSIENT_WRAPPERS = "*_`\"'“”‘’「」『』()（）[]【】#~～ \t\r\n"

LOREM_IPSUM_PATTERN = re.compile(
    r"^\s*lorem\s+ipsum\s+dolor\s+sit\s+amet\b",
    re.IGNORECASE,
)

EXTENDED_ACKNOWLEDGEMENT_PATTERN = re.compile(
    r"^(?:(?:好的|收到|明白(?:了)?|我知道了|知道了|没问题|可以|行|谢谢|多谢|稍等|正在处理|继续|这就去办|马上处理|这就开始|这就处理)[，,！!。.;；\s]*){1,4}$"
    r"|^(?:(?:thanks(?:\s+(?:a\s+lot|so\s+much))?|thank\s+you|ok(?:ay)?|got\s+it|understood|"
    r"sounds\s+good|looks\s+good|great|confirmed|done|sure|working\s+on\s+it|"
    r"i\s+will\s+start\s+working\s+on\s+it|let's\s+discuss\s+in\s+the\s+standup(?:\s+meeting)?)[,!.;\s]*){1,4}$",
    re.IGNORECASE,
)

# These are only "do not pre-skip" hints. They do not parse or rewrite a fact.
# A false positive merely sends the session to the LLM, which is the safe side.
_DURABLE_LOG_HINTS = (
    "port", "listen", "listening", "host", "hostname", "config", "configured",
    "data_dir", "database", "endpoint", "version", "protocol", "bind", "path",
    "端口", "监听", "配置", "数据库", "路径", "版本", "协议",
)


@dataclass(frozen=True)
class PersistenceAuditResult:
    """Outcome of persistence quality gate audit."""

    accepted: bool
    reason: str


def clean_transient_text(text: str) -> str:
    """Normalize transient interaction text for exact phrase checks."""
    if not text:
        return ""
    s = text.strip()
    prev = None
    while prev != s:
        prev = s
        s = s.strip(_TRANSIENT_WRAPPERS)
        while s and not s[-1].isalnum():
            s = s[:-1]
    return s.strip()


def is_acknowledgement_text(text: str) -> bool:
    """Return whether text is purely an acknowledgement/status confirmation."""
    if not text:
        return True
    s = text.strip()
    # Negative/decision emoji can carry semantic feedback and must fail open.
    if any(e in s for e in ["👎", "❌", "⚠️", "🔥", "🛑", "❗", "❓"]):
        return False
    if is_transient_only(s) or EXTENDED_ACKNOWLEDGEMENT_PATTERN.fullmatch(s):
        return True

    cleaned = clean_transient_text(s).lower()
    if len(s) < 60 and bool(cleaned) and cleaned in TRANSIENT_PHRASES:
        return True

    # Status replies are often composed from multiple transient atoms, e.g.
    # "好的，正在运行测试。" or "收到，这就去办".  Accept the composition
    # only when every non-empty segment is independently transient.
    parts = [
        clean_transient_text(part).lower()
        for part in re.split(r"[，,。.!！；;]+", s)
        if clean_transient_text(part)
    ]
    return bool(parts) and all(part in TRANSIENT_PHRASES for part in parts)


def is_placeholder_noise(text: str) -> bool:
    """Return whether text starts as conventional placeholder filler."""
    if not text:
        return False
    return bool(LOREM_IPSUM_PATTERN.search(text))


def _raw_log_may_contain_durable_fact(text: str) -> bool:
    """Conservatively detect whether a raw log may encode stable configuration.

    This intentionally does not extract the fact. It only prevents an early
    pre-check skip so the distillation LLM can decide semantically.
    """
    lower = text.lower()
    return any(hint in lower for hint in _DURABLE_LOG_HINTS)


def _session_contents(
    turns: Sequence[Mapping[str, Any]],
    last_user_goal: str,
    last_assistant_final: str,
) -> list[str]:
    """Return unique visible session contents, including adapter fallbacks."""
    contents: list[str] = []
    seen: set[str] = set()

    for turn in turns:
        content = str(turn.get("content", "")).strip()
        if content and content not in seen:
            seen.add(content)
            contents.append(content)

    # Some host payloads may omit one of these snippets from turns. Include
    # them as additional evidence, but never give them priority over turns.
    for fallback in (last_user_goal, last_assistant_final):
        content = (fallback or "").strip()
        if content and content not in seen:
            seen.add(content)
            contents.append(content)

    return contents


def should_skip_session(
    turns: Sequence[Mapping[str, Any]],
    *,
    last_user_goal: str = "",
    last_assistant_final: str = "",
    touched_files: Sequence[str] | None = None,
) -> tuple[bool, str]:
    """Conservative session-level pre-check for Warm Path distillation.

    Skip only when *all* visible session content is high-confidence noise.
    Any substantive, mixed, ambiguous, or potentially durable content proceeds
    to the LLM. In particular, a trailing "好的/收到" must never erase an
    earlier durable decision.
    """
    if touched_files:
        return False, "has_touched_files"

    contents = _session_contents(turns, last_user_goal, last_assistant_final)
    if not contents:
        return True, "Delta skip: pre_empty_turns"

    saw_raw_log = False
    saw_transient = False
    saw_placeholder = False

    for content in contents:
        if is_acknowledgement_text(content):
            saw_transient = True
            continue

        if is_placeholder_noise(content):
            saw_placeholder = True
            continue

        if is_raw_log(content):
            saw_raw_log = True
            if _raw_log_may_contain_durable_fact(content):
                return False, "raw_log_may_contain_durable_fact"
            continue

        # Anything else is semantic content. Fail open and let the LLM decide.
        return False, "has_substantive_content"

    # Every visible item was classified as high-confidence noise.
    if saw_raw_log:
        return True, "Delta skip: pre_raw_log_only"
    if saw_transient or saw_placeholder:
        return True, "Delta skip: pre_transient_only"
    return False, "conservative_proceed"


def audit_distilled_memory(
    text: str,
    metadata: Mapping[str, Any] | None = None,
) -> PersistenceAuditResult:
    """Audit one extracted memory immediately before persistence.

    The audit only ACCEPTs or DROPs. It never rewrites extracted text because
    the vector may already have been generated from that exact text.
    """
    if not text or not text.strip():
        return PersistenceAuditResult(accepted=False, reason="post_empty")

    s = text.strip()

    if is_context_control_tag(s):
        return PersistenceAuditResult(accepted=False, reason="post_control_tag")

    if is_instruction_override(s):
        return PersistenceAuditResult(accepted=False, reason="post_instruction_override")

    if is_acknowledgement_text(s):
        return PersistenceAuditResult(accepted=False, reason="post_transient_only")

    if is_raw_log(s):
        return PersistenceAuditResult(accepted=False, reason="post_raw_log")

    if is_placeholder_noise(s):
        return PersistenceAuditResult(accepted=False, reason="post_placeholder_noise")

    return PersistenceAuditResult(accepted=True, reason="post_accepted")
