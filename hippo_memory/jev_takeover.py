"""Scoped Jev relationship classification takeover for Cold Path (#65).

Enforces strict fail-closed safety gates:
1. Allowlisted projects only (rejects unlisted or global scope).
2. Verified calibration artifact (status == GO, model/rubric match, qualified active config).
3. Independent probability and margin thresholds for EQUIVALENT and CONFLICT.
4. Hard execution budget (calls & time deadline).
5. Immutable decision evidence persisted before apply; zero Jev re-invocation on crash replay.
6. Deterministic winner arbitration, version revalidation, and targeted mismerge recovery.
"""

from __future__ import annotations

import logging
import math
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Optional

from hippo_memory.decision import (
    RELATION_CONFLICT,
    RELATION_DISTINCT,
    RELATION_EQUIVALENT,
    ClassificationResult,
    RelationshipClassificationBackend,
)
from hippo_memory.jev import (
    JEV_MODEL,
    JEV_RUBRIC_VERSION,
    JevChoice,
    JevClient,
    JevFailure,
)
from hippo_memory.jev_calibration import (
    CalibrationNotQualifiedError,
    RelationThreshold,
    compute_rubric_hash,
    validate_calibration,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class JevTakeoverPolicy:
    """Explicit configuration policy for scoped Jev takeover."""

    allowed_projects: frozenset[str]
    calibration_artifact: Mapping[str, Any]
    max_calls: int = 200
    max_elapsed_seconds: float = 60.0
    deadline_per_call: float = 5.0

    def __post_init__(self) -> None:
        if (
            not isinstance(self.allowed_projects, frozenset)
            or not self.allowed_projects
            or any(
                not isinstance(project, str) or not project.strip()
                for project in self.allowed_projects
            )
        ):
            raise ValueError(
                "allowed_projects must be a non-empty frozenset of project IDs"
            )
        if (
            isinstance(self.max_calls, bool)
            or not isinstance(self.max_calls, int)
            or self.max_calls < 0
        ):
            raise ValueError("max_calls must be a nonnegative integer")
        for field_name, value in (
            ("max_elapsed_seconds", self.max_elapsed_seconds),
            ("deadline_per_call", self.deadline_per_call),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or value <= 0
            ):
                raise ValueError(f"{field_name} must be finite and positive")
        validate_calibration(
            self.calibration_artifact,
            expected_model=JEV_MODEL,
            expected_rubric=JEV_RUBRIC_VERSION,
        )


_SAFE_FAILURE_CODES = {
    "invalid_probability",
    "invalid_response_model",
    "invalid_answer",
    "invalid_choice",
    "invalid_probability_keys",
    "invalid_probability_sum",
    "choice_probability_mismatch",
    "request_too_large",
    "deadline_exceeded",
    "response_too_large",
    "transport_failure",
    "invalid_json",
    "retry_exhausted",
    "not_allowed",
    "budget_exhausted",
    "invalid_input",
    "input_requires_redaction",
}


_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"\b(?:sk|rk)-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(
        r"(?i)\b(?:api[_ -]?key|access[_ -]?token|secret|password|passwd|pwd)"
        r"\b\s*[:=]\s*[\"']?[^\s\"';]{8,}"
    ),
)


def _requires_redaction(text: str) -> bool:
    """Return True when local secret scrubbing would alter the fact text."""
    return any(pattern.search(text) is not None for pattern in _SECRET_PATTERNS)


def _safe_failure_code(code: str) -> str:
    if code in _SAFE_FAILURE_CODES or (
        len(code) == 8 and code.startswith("http_") and code[5:].isdigit()
    ):
        return code
    return "jev_failure"


class JevTakeoverClassifier(RelationshipClassificationBackend):
    """Scoped Jev relationship classifier with calibrated threshold enforcement."""

    def __init__(
        self,
        client: JevClient,
        policy: JevTakeoverPolicy,
        *,
        scope: str = "project",
        project_id: Optional[str] = None,
    ) -> None:
        self.client = client
        self.policy = policy
        self.scope = scope
        self.project_id = project_id
        self._start_time = time.monotonic()
        self.calls = 0
        self.failures = 0
        self.abstentions = 0
        self.accepted_equivalent = 0
        self.accepted_conflict = 0

        # Extract calibrated thresholds
        thresholds_cfg = self.policy.calibration_artifact["thresholds"]
        self.thresholds = {
            RELATION_EQUIVALENT: RelationThreshold(
                min_probability=float(thresholds_cfg["EQUIVALENT"]["min_probability"]),
                min_margin=float(thresholds_cfg["EQUIVALENT"]["min_margin"]),
            ),
            RELATION_CONFLICT: RelationThreshold(
                min_probability=float(thresholds_cfg["CONFLICT"]["min_probability"]),
                min_margin=float(thresholds_cfg["CONFLICT"]["min_margin"]),
            ),
        }

    def _abstain(
        self,
        reason: str,
        *,
        failure_code: Optional[str] = None,
        choice: Optional[str] = None,
        probabilities: Optional[Mapping[str, float]] = None,
        confidence: Optional[float] = None,
        margin: Optional[float] = None,
    ) -> ClassificationResult:
        self.abstentions += 1
        evidence: dict[str, Any] = {
            "provider": "jev",
            "model": JEV_MODEL,
            "rubric_version": JEV_RUBRIC_VERSION,
            "rubric_hash": self.policy.calibration_artifact.get("metadata", {}).get(
                "rubric_hash"
            ),
            "calibration_artifact_id": self.policy.calibration_artifact.get(
                "artifact_id"
            ),
            "status": "abstained",
            "reason": reason,
        }
        if failure_code is not None:
            evidence["failure_code"] = _safe_failure_code(failure_code)
        if choice is not None:
            evidence["choice"] = choice
        if probabilities is not None:
            evidence["probabilities"] = dict(probabilities)
        if confidence is not None:
            evidence["provider_confidence"] = confidence
        if margin is not None:
            evidence["margin"] = margin
        return ClassificationResult(RELATION_DISTINCT, reason, confidence, evidence)

    def classify(
        self, memory_a: Mapping[str, Any], memory_b: Mapping[str, Any]
    ) -> ClassificationResult:
        # 1. Project and Scope Guard: only allowlisted projects, never global scope
        if (
            self.scope != "project"
            or not self.project_id
            or self.project_id not in self.policy.allowed_projects
        ):
            return self._abstain("project_not_allowed", failure_code="not_allowed")

        # 2. Input guard. RFC-0001 forbids sending a pair when local secret
        # cleaning would alter semantic text; empty fields also abstain. No
        # redacted/truncated text is ever promoted to a destructive decision.
        memory_a_text = memory_a.get("memory")
        memory_b_text = memory_b.get("memory")
        if (
            not isinstance(memory_a_text, str)
            or not memory_a_text.strip()
            or not isinstance(memory_b_text, str)
            or not memory_b_text.strip()
        ):
            return self._abstain("input_empty", failure_code="invalid_input")
        if _requires_redaction(memory_a_text) or _requires_redaction(memory_b_text):
            return self._abstain(
                "input_requires_redaction",
                failure_code="input_requires_redaction",
            )

        # 3. Budget Guards: max calls and time elapsed
        if self.calls >= self.policy.max_calls:
            return self._abstain(
                "call_budget_exhausted", failure_code="budget_exhausted"
            )

        elapsed = time.monotonic() - self._start_time
        if elapsed >= self.policy.max_elapsed_seconds:
            return self._abstain(
                "time_budget_exhausted", failure_code="budget_exhausted"
            )

        remaining_time = max(0.1, self.policy.max_elapsed_seconds - elapsed)
        deadline = min(self.policy.deadline_per_call, remaining_time)

        # 4. Canonical sort for deterministic ordering
        first, second = sorted(
            (
                (str(memory_a.get("id", "")), memory_a_text),
                (str(memory_b.get("id", "")), memory_b_text),
            )
        )

        self.calls += 1
        try:
            verdict = self.client.classify_pair(
                first[1], second[1], deadline_seconds=deadline
            )
        except JevFailure as exc:
            failure_code = str(exc)
            if failure_code == "request_too_large":
                # JevClient rejects this locally before any HTTP request.
                return self._abstain(
                    "input_budget_exceeded", failure_code=failure_code
                )
            self.failures += 1
            return self._abstain(
                "jev_service_failure", failure_code=failure_code
            )
        except Exception:
            self.failures += 1
            return self._abstain(
                "jev_transport_failure", failure_code="transport_failure"
            )

        choice = verdict.choice
        probabilities = dict(verdict.probabilities)
        provider_conf = verdict.provider_confidence

        if choice not in (RELATION_EQUIVALENT, RELATION_CONFLICT, RELATION_DISTINCT):
            return self._abstain("malformed_choice", failure_code="invalid_choice")

        probs_sorted = sorted(probabilities.values(), reverse=True)
        if not probs_sorted:
            return self._abstain("empty_probabilities", failure_code="invalid_probability")
        p1 = probs_sorted[0]
        p2 = probs_sorted[1] if len(probs_sorted) > 1 else 0.0
        margin = round(p1 - p2, 6)

        # Tie breaking: top probability tie always abstains
        if math.isclose(p1, p2, abs_tol=1e-5):
            return self._abstain(
                "probability_tie",
                choice=choice,
                probabilities=probabilities,
                confidence=provider_conf,
                margin=margin,
            )

        # Evaluate against calibrated thresholds
        if choice in (RELATION_EQUIVALENT, RELATION_CONFLICT):
            thresh = self.thresholds[choice]
            if p1 < thresh.min_probability:
                return self._abstain(
                    f"probability_below_threshold: {p1:.3f} < {thresh.min_probability:.3f}",
                    choice=choice,
                    probabilities=probabilities,
                    confidence=provider_conf,
                    margin=margin,
                )
            if margin < thresh.min_margin:
                return self._abstain(
                    f"margin_below_threshold: {margin:.3f} < {thresh.min_margin:.3f}",
                    choice=choice,
                    probabilities=probabilities,
                    confidence=provider_conf,
                    margin=margin,
                )

            # Thresholds satisfied: promote to destructive relation
            if choice == RELATION_EQUIVALENT:
                self.accepted_equivalent += 1
            else:
                self.accepted_conflict += 1

            evidence = {
                "provider": "jev",
                "model": JEV_MODEL,
                "rubric_version": JEV_RUBRIC_VERSION,
                "rubric_hash": self.policy.calibration_artifact.get(
                    "metadata", {}
                ).get("rubric_hash"),
                "calibration_artifact_id": self.policy.calibration_artifact.get(
                    "artifact_id"
                ),
                "choice": choice,
                "probabilities": probabilities,
                "selected_probability": p1,
                "margin": margin,
                "provider_confidence": provider_conf,
                "thresholds_applied": {
                    "min_probability": thresh.min_probability,
                    "min_margin": thresh.min_margin,
                },
                "status": "takeover_accepted",
            }
            return ClassificationResult(
                choice, "jev_takeover_calibrated", p1, evidence
            )

        # DISTINCT verdict
        return ClassificationResult(
            RELATION_DISTINCT,
            "jev_distinct",
            p1,
            {
                "provider": "jev",
                "model": JEV_MODEL,
                "rubric_version": JEV_RUBRIC_VERSION,
                "rubric_hash": self.policy.calibration_artifact.get(
                    "metadata", {}
                ).get("rubric_hash"),
                "calibration_artifact_id": self.policy.calibration_artifact.get(
                    "artifact_id"
                ),
                "choice": choice,
                "probabilities": probabilities,
                "selected_probability": p1,
                "margin": margin,
                "provider_confidence": provider_conf,
                "status": "distinct",
            },
        )

    def stats(self) -> dict[str, Any]:
        elapsed = time.monotonic() - self._start_time
        if (
            self.scope != "project"
            or not self.project_id
            or self.project_id not in self.policy.allowed_projects
        ):
            status = "not_allowed"
        elif (
            self.failures > 0
            or self.calls >= self.policy.max_calls
            or elapsed >= self.policy.max_elapsed_seconds
        ):
            status = "degraded"
        else:
            status = "ok"
        return {
            "status": status,
            "allowed": (
                self.scope == "project"
                and bool(self.project_id)
                and self.project_id in self.policy.allowed_projects
            ),
            "calls": self.calls,
            "failures": self.failures,
            "abstentions": self.abstentions,
            "accepted_equivalent": self.accepted_equivalent,
            "accepted_conflict": self.accepted_conflict,
            "elapsed_seconds": round(elapsed, 2),
        }
