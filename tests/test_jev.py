"""Synthetic, account-free tests for the opt-in Jev observation adapter."""

import json
import unittest

import httpx

from hippo_memory.decision import RELATION_DISTINCT, ConsolidationDecider
from hippo_memory.jev import (
    JEV_MODEL,
    JEV_RUBRIC_VERSION,
    JevClient,
    JevFailure,
    JevRelationshipClassifier,
)


def response(
    *, choice="EQUIVALENT", probabilities=None, confidence=0.71, model=JEV_MODEL
):
    return {
        "model": model,
        "answers": {
            "relation": {
                "type": "choice",
                "choice": choice,
                "probabilities": probabilities
                or {"EQUIVALENT": 0.8, "CONFLICT": 0.1, "DISTINCT": 0.1},
                "confidence": confidence,
            }
        },
    }


class JevClientTests(unittest.TestCase):
    def test_success_is_observation_only_and_separates_scores(self):
        requests = []

        def handler(request):
            requests.append(request)
            payload = json.loads(request.content)
            self.assertEqual(payload["model"], JEV_MODEL)
            self.assertIn(
                JEV_RUBRIC_VERSION, payload["questions"]["relation"]["instructions"]
            )
            self.assertEqual(
                set(payload["questions"]["relation"]["criteria"]),
                {"EQUIVALENT", "CONFLICT", "DISTINCT"},
            )
            return httpx.Response(200, json=response())

        client = JevClient(
            "secret", client=httpx.Client(transport=httpx.MockTransport(handler))
        )
        result = JevRelationshipClassifier(client).classify(
            {"id": "b", "memory": "I like green tea"},
            {"id": "a", "memory": "I enjoy green tea"},
        )
        self.assertEqual(result.relation, RELATION_DISTINCT)
        self.assertEqual(result.reason, "jev_uncalibrated")
        self.assertIsNone(result.confidence)
        self.assertEqual(result.evidence["choice"], "EQUIVALENT")
        self.assertEqual(result.evidence["selected_class_probability"], 0.8)
        self.assertEqual(result.evidence["provider_confidence"], 0.71)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].headers["authorization"], "Bearer secret")

        decision = ConsolidationDecider(
            classifier=JevRelationshipClassifier(client)
        ).decide(
            {
                "id": "a",
                "memory": "I enjoy green tea",
                "user_id": "u1",
                "agent_id": "hippo",
            },
            {
                "id": "b",
                "memory": "I like green tea",
                "user_id": "u1",
                "agent_id": "hippo",
            },
        )
        self.assertEqual(decision.relation, RELATION_DISTINCT)
        self.assertIsNone(decision.winner_id)
        self.assertIsNone(decision.loser_id)
        self.assertEqual(
            decision.evidence["classification"]["details"]["choice"], "EQUIVALENT"
        )

    def test_bad_responses_abstain_without_leaking_body(self):
        bad = [
            {},
            response(model="jev-latest"),
            response(confidence=float("nan")),
            response(
                probabilities={"EQUIVALENT": 0.9, "CONFLICT": 0.9, "DISTINCT": 0.1}
            ),
            response(choice="DISTINCT"),
            response(probabilities={"EQUIVALENT": True, "CONFLICT": 0, "DISTINCT": 0}),
        ]
        for item in bad:
            with self.subTest(item=item):
                transport = httpx.MockTransport(
                    lambda _, item=item: httpx.Response(200, json=item)
                )
                client = JevClient("secret", client=httpx.Client(transport=transport))
                result = JevRelationshipClassifier(client).classify(
                    {"id": "a", "memory": "private A"},
                    {"id": "b", "memory": "private B"},
                )
                self.assertEqual(result.reason, "jev_abstained")
                self.assertNotIn("private", repr(result.evidence))
                self.assertNotIn("secret", repr(result.evidence))

    def test_auth_and_request_errors_do_not_retry(self):
        for status in (401, 422):
            calls = []

            def handler(_, calls=calls, status=status):
                calls.append(1)
                return httpx.Response(status, text="sensitive server response")

            client = JevClient(
                "secret", client=httpx.Client(transport=httpx.MockTransport(handler))
            )
            result = JevRelationshipClassifier(client).classify(
                {"id": "a", "memory": "A"}, {"id": "b", "memory": "B"}
            )
            self.assertEqual(len(calls), 1)
            self.assertEqual(result.evidence["failure_code"], f"http_{status}")
            self.assertNotIn("sensitive", repr(result))

    def test_retryable_status_is_bounded(self):
        calls = []

        def handler(_):
            calls.append(1)
            if len(calls) == 1:
                return httpx.Response(429, headers={"retry-after": "0"})
            return httpx.Response(200, json=response())

        client = JevClient(
            "secret", client=httpx.Client(transport=httpx.MockTransport(handler))
        )
        self.assertEqual(client.classify_pair("A", "B").choice, "EQUIVALENT")
        self.assertEqual(len(calls), 2)

    def test_timeout_and_size_limits(self):
        def timeout(_):
            raise httpx.ReadTimeout("private response")

        client = JevClient(
            "secret",
            client=httpx.Client(transport=httpx.MockTransport(timeout)),
            max_retries=0,
        )
        result = JevRelationshipClassifier(client).classify(
            {"id": "a", "memory": "A"}, {"id": "b", "memory": "B"}
        )
        self.assertEqual(result.evidence["failure_code"], "transport_failure")
        with self.assertRaisesRegex(JevFailure, "request_too_large"):
            client.classify_pair("x" * 40000, "B")
        oversize = JevClient(
            "secret",
            client=httpx.Client(
                transport=httpx.MockTransport(
                    lambda _: httpx.Response(200, content=b"x" * 70000)
                )
            ),
        )
        with self.assertRaisesRegex(JevFailure, "response_too_large"):
            oversize.classify_pair("A", "B")


if __name__ == "__main__":
    unittest.main()
