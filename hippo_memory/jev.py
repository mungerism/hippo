"""Opt-in Jev Choice adapter for cold-path *observation*, never mutation.

The provider verdict is deliberately not promoted to a consolidation relation
until a later, separately reviewed calibration/activation policy exists.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from hippo_memory.decision import (
    RELATION_DISTINCT,
    VALID_RELATIONS,
    ClassificationResult,
)

JEV_MODEL = "jev-1.13.0"
JEV_RUBRIC_VERSION = "memory-relation-v1"
JEV_URL = "https://api.typesafe.ai/v1/systemone"
_MAX_REQUEST_BYTES = 32_768
_MAX_RESPONSE_BYTES = 65_536
_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504, 529}
_RUBRIC = {
    "type": "choice",
    "instructions": (
        "Classify the semantic relationship between two memory facts. "
        "Ignore timestamps, provenance and which memory should win. "
        f"Rubric version: {JEV_RUBRIC_VERSION}."
    ),
    "criteria": {
        "EQUIVALENT": "Same assertion, allowing paraphrase; neither changes the other's meaning.",
        "CONFLICT": "Assertions cannot both hold for the same subject and context.",
        "DISTINCT": "Different, compatible, or insufficiently comparable assertions.",
    },
}


class JevFailure(Exception):
    """Sanitized failure code; never contains request/response text or credentials."""


@dataclass(frozen=True, slots=True)
class JevChoice:
    choice: str
    probabilities: Mapping[str, float]
    provider_confidence: float


def _probability(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise JevFailure("invalid_probability")
    number = float(value)
    if not math.isfinite(number) or not 0 <= number <= 1:
        raise JevFailure("invalid_probability")
    return number


def _parse_choice(data: Any) -> JevChoice:
    if not isinstance(data, dict) or data.get("model") != JEV_MODEL:
        raise JevFailure("invalid_response_model")
    answers = data.get("answers")
    answer = answers.get("relation") if isinstance(answers, dict) else None
    if not isinstance(answer, dict) or answer.get("type") != "choice":
        raise JevFailure("invalid_answer")
    choice = answer.get("choice")
    probabilities = answer.get("probabilities")
    if choice not in VALID_RELATIONS or not isinstance(probabilities, dict):
        raise JevFailure("invalid_choice")
    if set(probabilities) != set(VALID_RELATIONS):
        raise JevFailure("invalid_probability_keys")
    parsed = {key: _probability(value) for key, value in probabilities.items()}
    if not math.isclose(sum(parsed.values()), 1.0, rel_tol=0, abs_tol=1e-5):
        raise JevFailure("invalid_probability_sum")
    if parsed[choice] != max(parsed.values()):
        raise JevFailure("choice_probability_mismatch")
    confidence = _probability(answer.get("confidence"))
    return JevChoice(choice, parsed, confidence)


class JevClient:
    """Explicitly constructed, injectable HTTP client with bounded retries.

    No environment lookup or network/client initialization occurs on import or
    on Hippo's default path. The caller owns an injected client; otherwise this
    instance owns and closes its own httpx client.
    """

    def __init__(
        self,
        api_key: str,
        *,
        client: httpx.Client | None = None,
        connect_timeout: float = 2.0,
        deadline_seconds: float = 8.0,
        max_retries: int = 1,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("api_key is required for explicit Jev use")
        if not 0 < connect_timeout <= deadline_seconds or not math.isfinite(
            deadline_seconds
        ):
            raise ValueError("invalid Jev timeouts")
        if (
            isinstance(max_retries, bool)
            or not isinstance(max_retries, int)
            or not 0 <= max_retries <= 2
        ):
            raise ValueError("max_retries must be 0..2")
        self._api_key = api_key
        self._client = client if client is not None else httpx.Client()
        self._owns_client = client is None
        self.connect_timeout = connect_timeout
        self.deadline_seconds = deadline_seconds
        self.max_retries = max_retries

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def classify_pair(self, memory_a: str, memory_b: str) -> JevChoice:
        payload = {
            "model": JEV_MODEL,
            "state": {"memory_a": memory_a, "memory_b": memory_b},
            "questions": {"relation": _RUBRIC},
        }
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(encoded) > _MAX_REQUEST_BYTES:
            raise JevFailure("request_too_large")
        end = time.monotonic() + self.deadline_seconds
        for attempt in range(self.max_retries + 1):
            remaining = end - time.monotonic()
            if remaining <= 0:
                raise JevFailure("deadline_exceeded")
            timeout = httpx.Timeout(
                remaining, connect=min(self.connect_timeout, remaining)
            )
            try:
                with self._client.stream(
                    "POST",
                    JEV_URL,
                    content=encoded,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    timeout=timeout,
                ) as response:
                    if response.status_code != 200:
                        if (
                            response.status_code in _RETRYABLE_STATUS
                            and attempt < self.max_retries
                        ):
                            self._backoff(response, attempt, end)
                            continue
                        raise JevFailure(f"http_{response.status_code}")
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > _MAX_RESPONSE_BYTES:
                            raise JevFailure("response_too_large")
                        if time.monotonic() >= end:
                            raise JevFailure("deadline_exceeded")
            except (httpx.TimeoutException, httpx.TransportError):
                if attempt < self.max_retries and time.monotonic() < end:
                    self._backoff(None, attempt, end)
                    continue
                raise JevFailure("transport_failure") from None
            try:
                return _parse_choice(json.loads(body))
            except (UnicodeError, ValueError, TypeError):
                raise JevFailure("invalid_json") from None
        raise JevFailure("retry_exhausted")

    @staticmethod
    def _backoff(response: httpx.Response | None, attempt: int, end: float) -> None:
        retry_after = (
            response.headers.get("retry-after") if response is not None else None
        )
        try:
            delay = (
                float(retry_after) if retry_after is not None else 0.1 * (2**attempt)
            )
        except ValueError:
            delay = 0.1 * (2**attempt)
        delay = min(max(delay, 0.0), 1.0, max(0.0, end - time.monotonic()))
        if delay:
            time.sleep(delay)


class JevRelationshipClassifier:
    """#61 observation-only implementation of RelationshipClassificationBackend."""

    def __init__(self, client: JevClient) -> None:
        self.client = client

    def classify(
        self, memory_a: Mapping[str, Any], memory_b: Mapping[str, Any]
    ) -> ClassificationResult:
        # Canonical order makes the request independent of candidate discovery order.
        first, second = sorted(
            (
                (str(memory_a.get("id", "")), str(memory_a.get("memory", ""))),
                (str(memory_b.get("id", "")), str(memory_b.get("memory", ""))),
            )
        )
        try:
            verdict = self.client.classify_pair(first[1], second[1])
        except JevFailure as exc:
            return ClassificationResult(
                RELATION_DISTINCT,
                "jev_abstained",
                None,
                {
                    "provider": "jev",
                    "model": JEV_MODEL,
                    "rubric_version": JEV_RUBRIC_VERSION,
                    "failure_code": str(exc),
                },
            )
        except Exception:  # noqa: BLE001 - third-party transport must fail closed
            return ClassificationResult(
                RELATION_DISTINCT,
                "jev_abstained",
                None,
                {
                    "provider": "jev",
                    "model": JEV_MODEL,
                    "rubric_version": JEV_RUBRIC_VERSION,
                    "failure_code": "unexpected_client_failure",
                },
            )
        return ClassificationResult(
            RELATION_DISTINCT,
            "jev_uncalibrated",
            None,
            {
                "provider": "jev",
                "model": JEV_MODEL,
                "rubric_version": JEV_RUBRIC_VERSION,
                "choice": verdict.choice,
                "probabilities": dict(verdict.probabilities),
                "selected_class_probability": verdict.probabilities[verdict.choice],
                "provider_confidence": verdict.provider_confidence,
            },
        )
