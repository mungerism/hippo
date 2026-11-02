"""Real Mem0 -> Hippo -> Worker regression for swallowed extraction failures."""

import tempfile
import json
from pathlib import Path

from hippo_memory.hooks import CapturedPayload, JobState, SpoolStorage, SpoolWorker
from tests.test_support import IsolatedTestCase, create_contract_engine


class TestDistillationResponse(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.engine = create_contract_engine(storage_dir=tmp.name)
        self.storage = SpoolStorage(Path(tmp.name) / "spool", version=2)
        self.worker = SpoolWorker(self.storage, self.engine)

    def job(self, job_id="parse-failure"):
        payload = CapturedPayload(
            job_id=job_id, host="pi", event="Stop", session_id=job_id, schema_version=2,
            project_id="response-contract", project_dir="/unused",
            turns=[{"role": "user", "content": "项目统一使用 uv 管理 Python 依赖。"}],
            last_user_goal="项目统一使用 uv 管理 Python 依赖。",
        )
        self.storage.enqueue(payload)
        return payload

    def test_malformed_json_retries_without_success_receipt(self):
        self.engine.memory.llm.queue_response('{"memory": [{"text": "truncated')
        payload = self.job()
        self.assertFalse(self.worker.process_one_job(payload))
        state = self.storage.load_state(payload.job_id)
        self.assertEqual(state["state"], "pending")
        self.assertEqual(state["attempt"], 1)
        self.assertGreater(state["not_before"], 0)
        self.assertEqual(list(self.storage.receipts_dir.glob("*.json")), [])
        self.assertFalse(self.storage.is_semantic_cursor_processed(payload.semantic_cursor))

    def test_invalid_protocol_is_bounded_and_does_not_leak_response(self):
        for index, response in enumerate([
            "", "   ", "null", "[]", "{}", '{"memory": null}',
            '{"memory": {}}', '{"memory": ["private-fact"]}',
            '{"memory": [{"text": null}]}', '{"memory": [{"text": ""}]}',
        ]):
            with self.subTest(response=response):
                payload = self.job(f"invalid-{index}")
                for attempt in range(1, 4):
                    self.engine.memory.llm.queue_response(response)
                    payload = self.storage.load_payload(payload.job_id)
                    payload.merge_state(self.storage.load_state(payload.job_id))
                    self.assertFalse(self.worker.process_one_job(payload))
                    state = self.storage.load_state(payload.job_id)
                    self.assertEqual(state["state"], "dead" if attempt == 3 else "pending")
                    self.assertEqual(state["attempt"], attempt)
                    self.assertTrue(state["error"].startswith("DISTILLATION_RESPONSE_INVALID_"))
                    self.assertNotIn("private-fact", state["error"])
                    self.assertFalse(self.storage.is_semantic_cursor_processed(payload.semantic_cursor))
                    if attempt < 3:
                        self.storage.update_state(payload.job_id, state=JobState.PENDING, not_before=0)
                self.assertEqual(list(self.storage.receipts_dir.glob("*.json")), [])

    def test_retry_can_succeed_and_duplicate_cursor_is_skipped(self):
        payload = self.job("recover")
        self.engine.memory.llm.queue_response("invalid")
        self.assertFalse(self.worker.process_one_job(payload))
        self.storage.update_state(payload.job_id, state=JobState.PENDING, not_before=0)
        payload = self.storage.load_payload(payload.job_id)
        payload.merge_state(self.storage.load_state(payload.job_id))
        self.engine.memory.llm.queue_response({"memory": [{"text": "项目使用 uv 管理依赖"}]})
        self.assertTrue(self.worker.process_one_job(payload))
        self.assertEqual(self.storage.load_state(payload.job_id)["state"], "completed")
        self.assertTrue(self.storage.is_semantic_cursor_processed(payload.semantic_cursor))
        duplicate = self.job("duplicate")
        duplicate.session_id = payload.session_id
        self.storage.save_payload(duplicate)
        calls = self.engine.memory.llm.call_count
        self.assertTrue(self.worker.process_one_job(duplicate))
        self.assertEqual(self.storage.load_state(duplicate.job_id)["state"], "skipped")
        self.assertEqual(self.engine.memory.llm.call_count, calls)

    def test_valid_empty_filtered_and_native_json_normalization_complete(self):
        for index, response in enumerate([
            '{"memory": []}',
            '{"memory": [{"text": "ok"}]}',
            '```json\n{"memory": []}\n```',
            'Here is the result: {"memory": []}',
        ]):
            with self.subTest(response=response):
                payload = self.job(f"valid-{index}")
                self.engine.memory.llm.queue_response(response)
                self.assertTrue(self.worker.process_one_job(payload))
                self.assertEqual(self.storage.load_state(payload.job_id)["state"], "completed")
                self.assertTrue(self.storage.is_semantic_cursor_processed(payload.semantic_cursor))
                receipt = json.loads((self.storage.receipts_dir / f"{payload.semantic_cursor}.json").read_text())
                self.assertEqual(receipt["receipt"]["results"], [])

    def test_other_engine_and_non_distillation_calls_are_unchanged(self):
        from tests.test_support import create_isolated_mem0

        # The adapter must not patch the global Mem0/LLM classes.
        standalone = create_isolated_mem0(collection_name="unadapted")
        standalone.llm.queue_response("invalid")
        self.assertEqual(standalone.add("fact", user_id="standalone")["results"], [])
        self.engine.memory.llm.queue_response("invalid")
        self.assertEqual(self.engine.add("fact", project_id="ordinary")["results"], [])
        # Explicit writes bypass extraction even with a malformed response queued.
        self.engine.memory.llm.queue_response("invalid")
        calls = self.engine.memory.llm.call_count
        self.assertEqual(self.engine.add_explicit("项目使用 uv", project_id="hot")["status"], "success")
        self.assertEqual(self.engine.memory.llm.call_count, calls)

