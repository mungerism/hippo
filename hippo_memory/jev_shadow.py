"""Run-local, opt-in Jev observation with no governance authority.

Only an explicitly allowlisted project identity may send a pair. The shadow
receives the same frozen fact text as the primary classifier and emits only
salted pair fingerprints and model statistics, never facts or record IDs.
"""

from __future__ import annotations

import hashlib
import math
import secrets
import time
from dataclasses import dataclass
from typing import Protocol

from hippo_memory.decision import (
    VALID_RELATIONS,
    ConsolidationDecision,
    resolve_identity,
)
from hippo_memory.jev import JEV_MODEL, JEV_RUBRIC_VERSION, JevChoice, JevFailure
from hippo_memory.lifecycle import is_active_memory

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
}


def _safe_failure_code(error: JevFailure) -> str:
    code = str(error)
    if code in _SAFE_FAILURE_CODES or (
        len(code) == 8 and code.startswith("http_") and code[5:].isdigit()
    ):
        return code
    return "shadow_backend_failure"


def _validate_choice(choice: JevChoice) -> None:
    if not isinstance(choice, JevChoice) or choice.choice not in VALID_RELATIONS:
        raise JevFailure("invalid_shadow_choice")
    probabilities = choice.probabilities
    if set(probabilities) != set(VALID_RELATIONS):
        raise JevFailure("invalid_shadow_choice")
    values = list(probabilities.values())
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 <= value <= 1
        for value in values
    ) or not math.isclose(sum(values), 1.0, rel_tol=0, abs_tol=1e-5):
        raise JevFailure("invalid_shadow_choice")
    if probabilities[choice.choice] != max(values):
        raise JevFailure("invalid_shadow_choice")
    confidence = choice.provider_confidence
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(confidence)
        or not 0 <= confidence <= 1
    ):
        raise JevFailure("invalid_shadow_choice")


class JevChoiceBackend(Protocol):
    def classify_pair(self, memory_a: str, memory_b: str) -> JevChoice: ...


@dataclass(frozen=True, slots=True)
class JevShadowPolicy:
    allowed_projects: frozenset[str]
    max_calls: int = 200
    max_elapsed_seconds: float = 60.0

    def __post_init__(self) -> None:
        if not isinstance(self.allowed_projects, frozenset) or any(
            not isinstance(project, str) or not project
            for project in self.allowed_projects
        ):
            raise ValueError("allowed_projects must be a frozenset of project IDs")
        if (
            isinstance(self.max_calls, bool)
            or not isinstance(self.max_calls, int)
            or self.max_calls < 0
        ):
            raise ValueError("max_calls must be a nonnegative integer")
        if (
            isinstance(self.max_elapsed_seconds, bool)
            or not isinstance(self.max_elapsed_seconds, (int, float))
            or not math.isfinite(self.max_elapsed_seconds)
            or self.max_elapsed_seconds <= 0
        ):
            raise ValueError("max_elapsed_seconds must be finite and positive")


class JevShadowRun:
    """A per-consolidation-run observer; no mutable state leaks between runs."""

    def __init__(
        self,
        backend: JevChoiceBackend,
        policy: JevShadowPolicy,
        *,
        scope: str,
        identity: tuple[str, str],
    ) -> None:
        self.backend = backend
        self.policy = policy
        self.identity = identity
        self.enabled = scope == "project" and identity[1] in policy.allowed_projects
        self.started = time.monotonic()
        self.salt = secrets.token_bytes(16)
        self.attempted = 0
        self.failed = 0
        self.skipped_budget = 0
        self.budget_exhausted = False
        self.skipped_guard = 0
        self.observations: list[dict] = []

    def _fingerprint(self, pair: tuple[tuple[str, str], tuple[str, str]]) -> str:
        encoded = repr(pair).encode("utf-8")
        return hashlib.sha256(self.salt + encoded).hexdigest()[:24]

    def observe(
        self,
        first: dict,
        second: dict,
        primary: ConsolidationDecision,
    ) -> None:
        try:
            self._observe(first, second, primary)
        except Exception:  # noqa: BLE001 - shadow preflight must not interrupt apply
            self.failed += 1
            self.observations.append(
                {"status": "degraded", "failure_code": "shadow_internal_failure"}
            )

    def _observe(
        self,
        first: dict,
        second: dict,
        primary: ConsolidationDecision,
    ) -> None:
        if not self.enabled:
            return
        if (
            resolve_identity(first) != self.identity
            or resolve_identity(second) != self.identity
            or not is_active_memory(first)
            or not is_active_memory(second)
        ):
            self.skipped_guard += 1
            return
        pair = tuple(
            sorted(
                (
                    (str(first.get("id", "")), str(first.get("memory", ""))),
                    (str(second.get("id", "")), str(second.get("memory", ""))),
                )
            )
        )
        if not pair[0][0] or not pair[1][0] or not pair[0][1] or not pair[1][1]:
            self.skipped_guard += 1
            return
        if (
            self.attempted >= self.policy.max_calls
            or time.monotonic() - self.started >= self.policy.max_elapsed_seconds
        ):
            self.skipped_budget += 1
            self.budget_exhausted = True
            return
        self.attempted += 1
        fingerprint = self._fingerprint(pair)
        started = time.monotonic()
        observation = {
            "pair_fingerprint": fingerprint,
            "primary_relation": primary.relation,
            "model": JEV_MODEL,
            "rubric_version": JEV_RUBRIC_VERSION,
        }
        try:
            choice = self.backend.classify_pair(pair[0][1], pair[1][1])
            _validate_choice(choice)
            observation.update(
                {
                    "jev_relation": choice.choice,
                    "probabilities": dict(choice.probabilities),
                    "provider_confidence": choice.provider_confidence,
                    "disagrees": choice.choice != primary.relation,
                    "status": "observed",
                }
            )
        except JevFailure as exc:
            self.failed += 1
            observation.update(
                {"status": "degraded", "failure_code": _safe_failure_code(exc)}
            )
        except Exception:  # noqa: BLE001 - observer failures cannot interrupt governance
            self.failed += 1
            observation.update(
                {"status": "degraded", "failure_code": "shadow_backend_failure"}
            )
        observation["latency_ms"] = round((time.monotonic() - started) * 1000, 3)
        self.observations.append(observation)
        if time.monotonic() - self.started >= self.policy.max_elapsed_seconds:
            self.budget_exhausted = True

    def report(self) -> dict:
        degraded = self.failed > 0 or self.budget_exhausted
        return {
            "status": (
                "not_allowed" if not self.enabled else "degraded" if degraded else "ok"
            ),
            "attempted": self.attempted,
            "observed": sum(
                item.get("status") == "observed" for item in self.observations
            ),
            "failed": self.failed,
            "skipped_budget": self.skipped_budget,
            "budget_exhausted": self.budget_exhausted,
            "skipped_guard": self.skipped_guard,
            "disagreements": sum(
                item.get("disagrees", False) for item in self.observations
            ),
            "observations": list(self.observations),
        }
