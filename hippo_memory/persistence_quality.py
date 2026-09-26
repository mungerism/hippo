"""Hippo Persistence Quality - Quality gate and anti-pollution filters for session distillation (Warm Path).

Guards the long-term memory store against conversational flotsam (acknowledgements, transient noise),
raw operational log traces, and prompt injection attacks during background session distillation,
while preserving legitimate durable engineering rules, architectural decisions, and configurations.
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
    # Acknowledgement phrases
    "好的",
    "收到",
    "明白了",
    "明白",
    "我知道了",
    "知道了",
    "稍等",
    "正在处理",
    "正在运行",
    "正在执行",
    "正在查看",
    "正在运行测试",
    "继续",
    "没问题",
    "可以",
    "行",
    "谢谢",
    "多谢",
    "这就去办",
    "这就开始",
    "马上处理",
    "这就处理",
    "ok",
    "okay",
    "sure",
    "got it",
    "done",
    "working on it",
    "understood",
    "looks good",
    "sounds good",
    "thanks",
    "thank you",
    "great",
    "confirmed",
    "let's discuss in the standup meeting",
    "let's discuss in the standup",
    "i will start working on it",
    # Ephemeral probing phrases
    "运行测试",
    "跑测试",
    "跑下测试",
    "查看代码",
    "检查文件",
    "查看状态",
    "running tests",
    "checking files",
    "analyzing codebase",
    "fetching docs",
}

_TRANSIENT_WRAPPERS = "*_`\"'“”‘’「」『』()（）[]【】#~～ \t\r\n"

# Pattern detecting typical Lorem Ipsum placeholder text
LOREM_IPSUM_PATTERN = re.compile(
    r"\blorem\s+ipsum\s+dolor\s+sit\s+amet\b",
    re.IGNORECASE,
)

# Extended full-match pattern for acknowledgements
EXTENDED_ACKNOWLEDGEMENT_PATTERN = re.compile(
    r"^(?:(?:好的|收到|明白(?:了)?|我知道了|知道了|没问题|可以|行|谢谢|多谢|稍等|正在处理|继续|这就去办|马上处理|这就开始|这就处理)[，,！!。.;；\s]*){1,4}$"
    r"|^(?:(?:thanks(?:\s+(?:a\s+lot|so\s+much))?|thank\s+you|ok(?:ay)?|got\s+it|understood|"
    r"sounds\s+good|looks\s+good|great|confirmed|done|sure|working\s+on\s+it|"
    r"i\s+will\s+start\s+working\s+on\s+it|let's\s+discuss\s+in\s+the\s+standup(?:\s+meeting)?)[,!.;\s]*){1,4}$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PersistenceAuditResult:
    """Outcome of persistence quality gate audit."""

    accepted: bool
    reason: str


def clean_transient_text(text: str) -> str:
    """Normalize and clean transient interaction text.

    Strips Markdown wrapping characters (*, _, `), quotes, brackets, and trailing
    punctuation/emojis/spaces to isolate the core candidate phrase.
    """
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
    """Return whether text is purely an acknowledgement or conversational confirmation."""
    if not text:
        return True
    s = text.strip()
    # Guard against negative/decision emojis being wrongly classified as empty or transient
    if any(e in s for e in ["👎", "❌", "⚠️", "🔥", "🛑", "❗", "❓"]):
        return False
    if is_transient_only(s) or EXTENDED_ACKNOWLEDGEMENT_PATTERN.fullmatch(s):
        return True
    cleaned = clean_transient_text(s).lower()
    return len(s) < 60 and bool(cleaned) and (cleaned in TRANSIENT_PHRASES)


def is_placeholder_noise(text: str) -> bool:
    """Return whether text looks like placeholder filler (e.g. Lorem Ipsum)."""
    if not text:
        return False
    return bool(LOREM_IPSUM_PATTERN.search(text))


def should_skip_session(
    turns: Sequence[Mapping[str, Any]],
    *,
    last_user_goal: str = "",
    last_assistant_final: str = "",
    touched_files: Sequence[str] | None = None,
) -> tuple[bool, str]:
    """Conservative session-level pre-check for Warm Path distillation.

    Determines whether a session should be skipped before invoking LLM extraction.
    Adheres strictly to conservative fail-open:
    - If touched_files is non-empty: NEVER skip.
    - If last_user_goal is substantive (not transient ping-pong): NEVER skip.
    - If last_assistant_final is substantive: NEVER skip.
    - Only skip when the entire session is high-confidence flotsam (all acknowledgements,
      pure operational logs without stable facts, or placeholder noise).

    Returns:
        (should_skip, reason_code)
    """
    # 1. Preservation rule: touched files indicate substantive project mutations
    if touched_files:
        return False, "has_touched_files"

    user_goal = last_user_goal.strip() if last_user_goal else ""
    assistant_final = last_assistant_final.strip() if last_assistant_final else ""

    # 2. Preservation rule: substantive user goal/decision must be preserved for Mem0 analysis
    if user_goal and not is_acknowledgement_text(user_goal):
        return False, "substantive_user_goal"

    # 3. Check assistant final reply
    if assistant_final:
        if is_acknowledgement_text(assistant_final):
            # User goal was transient/empty, and assistant reply is pure confirmation with no touched files
            return True, "Delta skip: transient acknowledgment without file mutations"
        else:
            # Substantive assistant reply must be preserved
            return False, "substantive_assistant_reply"

    if not turns:
        if not assistant_final or is_acknowledgement_text(assistant_final):
            return True, "Delta skip: pre_empty_turns"
        return False, "has_assistant_reply"

    # 4. Analyze turns content for pure flotsam or operational logs
    all_turns_transient = True
    all_turns_raw_log = True
    has_substantive_content = False

    for turn in turns:
        content = str(turn.get("content", "")).strip()
        if not content:
            continue

        # Check placeholder noise
        if is_placeholder_noise(content):
            continue

        # Check acknowledgement
        if is_acknowledgement_text(content):
            all_turns_raw_log = False
            continue

        all_turns_transient = False

        # Check raw operational log
        if is_raw_log(content):
            # If the log snippet contains explicit durable configuration keywords,
            # it might contain architectural facts -> do NOT classify as pure flotsam
            lower = content.lower()
            if any(k in lower for k in ["port", "host", "listen", "config", "data_dir", "database", "endpoint", "version"]):
                has_substantive_content = True
                all_turns_raw_log = False
            continue

        all_turns_raw_log = False
        has_substantive_content = True
        break

    if has_substantive_content:
        return False, "has_substantive_content"

    if all_turns_transient:
        return True, "Delta skip: pre_transient_only"

    if all_turns_raw_log:
        return True, "Delta skip: pre_raw_log_only"

    return False, "conservative_proceed"


def audit_distilled_memory(
    text: str,
    metadata: Mapping[str, Any] | None = None,
) -> PersistenceAuditResult:
    """Post-distillation / pre-mutation persistence audit for a single extracted memory item.

    Audits extracted text before inserting or updating the vector store.
    Strictly performs ACCEPT or DROP without rewriting text.

    Args:
        text: The extracted memory text to audit.
        metadata: Optional metadata dictionary associated with the write.

    Returns:
        PersistenceAuditResult with accepted=True/False and a structured reason code.
    """
    if not text or not text.strip():
        return PersistenceAuditResult(accepted=False, reason="post_empty")

    s = text.strip()

    # 1. Block control tags and context breakout attempts
    if is_context_control_tag(s):
        return PersistenceAuditResult(accepted=False, reason="post_control_tag")

    # 2. Block imperative instruction overrides / prompt injection directives
    # INSTRUCTION_OVERRIDE_PATTERN is anchored to imperative directives;
    # descriptive facts (e.g. "系统会拦截 prompt injection") are preserved.
    if is_instruction_override(s):
        return PersistenceAuditResult(accepted=False, reason="post_instruction_override")

    # 3. Block transient acknowledgements that survived distillation
    if is_acknowledgement_text(s):
        return PersistenceAuditResult(accepted=False, reason="post_transient_only")

    # 4. Block raw operational logs that survived distillation
    if is_raw_log(s):
        return PersistenceAuditResult(accepted=False, reason="post_raw_log")

    # 5. Block placeholder text (Lorem ipsum)
    if is_placeholder_noise(s):
        return PersistenceAuditResult(accepted=False, reason="post_placeholder_noise")

    return PersistenceAuditResult(accepted=True, reason="post_accepted")
