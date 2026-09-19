"""Tests for embedding reindex and migration workflow (Issue #28)."""

import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

from qdrant_client.http.models import Record

from hippo_memory.config import (
    EmbeddingProfile,
    HippoConfig,
    LEGACY_COLLECTION_NAME,
    resolve_collection_name,
)
from hippo_memory.exceptions import HippoValidationError
from hippo_memory.reindex import (
    EmbeddingMigrator,
    ReindexPlan,
    ReindexResult,
    is_transient_error,
    payloads_match,
    retry_with_backoff,
)


class TestEmbeddingProfileAndNaming(unittest.TestCase):
    """Verify embedding profile isolation and deterministic collection naming."""

    def test_profile_isolation_across_providers(self):
        gemini_col = resolve_collection_name("gemini", "gemini-embedding-2", 768)
        vertex_col = resolve_collection_name("vertexai", "gemini-embedding-2", 768)
        openai_col = resolve_collection_name("openai", "text-embedding-3-small", 1536)

        self.assertEqual(gemini_col, "hippo_memories_gemini_gemini_embedding_2_768")
        self.assertEqual(vertex_col, "hippo_memories_vertexai_gemini_embedding_2_768")
        self.assertEqual(openai_col, "hippo_memories_openai_text_embedding_3_small_1536")

        # Profiles must never share collections
        self.assertNotEqual(gemini_col, vertex_col)
        self.assertNotEqual(gemini_col, openai_col)
        self.assertNotEqual(vertex_col, openai_col)

    def test_profile_entities_collection_naming(self):
        profile = EmbeddingProfile(provider="vertexai", model="gemini-embedding-2", dimensions=768)
        self.assertEqual(profile.collection_name, "hippo_memories_vertexai_gemini_embedding_2_768")
        self.assertEqual(
            profile.entity_collection_name,
            "hippo_memories_vertexai_gemini_embedding_2_768_entities",
        )

    def test_legacy_collection_is_not_active_default(self):
        # Unless explicitly requested, legacy hippo_memories is not generated
        col = resolve_collection_name("vertexai")
        self.assertNotEqual(col, LEGACY_COLLECTION_NAME)

    def test_operator_override_takes_precedence(self):
        with patch.dict(os.environ, {"HIPPO_COLLECTION_NAME": "custom_override_col"}):
            self.assertEqual(resolve_collection_name("gemini"), "custom_override_col")
            self.assertEqual(resolve_collection_name("vertexai"), "custom_override_col")


class TestPayloadMatchingAndHelpers(unittest.TestCase):
    """Test payload comparison and transient error retry logic."""

    def test_payloads_match_exact_and_canonical(self):
        p1 = {"data": "test memory", "hash": "h1", "user_id": "u1", "agent_id": "a1"}
        p2 = {"data": "test memory", "hash": "h1", "user_id": "u1", "agent_id": "a1"}
        self.assertTrue(payloads_match(p1, p2))

        # Different text
        p3 = {"data": "different memory", "hash": "h1", "user_id": "u1", "agent_id": "a1"}
        self.assertFalse(payloads_match(p1, p3))

        # Different user
        p4 = {"data": "test memory", "hash": "h1", "user_id": "u2", "agent_id": "a1"}
        self.assertFalse(payloads_match(p1, p4))

    def test_is_transient_error(self):
        self.assertTrue(is_transient_error(Exception("429 Resource Exhausted")))
        self.assertTrue(is_transient_error(Exception("Rate limit exceeded")))
        self.assertTrue(is_transient_error(Exception("503 Service Unavailable")))
        self.assertTrue(is_transient_error(Exception("ReadTimeout during request")))
        self.assertFalse(is_transient_error(ValueError("Invalid dimensions")))
        self.assertFalse(is_transient_error(Exception("401 Unauthorized")))

    def test_retry_with_backoff_success_after_transient(self):
        attempts = 0

        def _flaky():
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise Exception("429 rate limit")
            return "success"

        with patch("time.sleep", return_value=None):
            res = retry_with_backoff(_flaky, max_retries=3, base_wait=0.01)
        self.assertEqual(res, "success")
        self.assertEqual(attempts, 3)

    def test_retry_with_backoff_fails_fast_on_non_transient(self):
        attempts = 0

        def _bad():
            nonlocal attempts
            attempts += 1
            raise ValueError("Bad arguments")

        with self.assertRaises(ValueError):
            retry_with_backoff(_bad, max_retries=3)
        self.assertEqual(attempts, 1)


class TestEmbeddingMigratorWorkflow(unittest.TestCase):
    """Comprehensive test suite covering the full Reindex/Migration workflow."""

    def setUp(self):
        self.mock_client = MagicMock()
        self.config = HippoConfig(user_id="test-user")
        self.migrator = EmbeddingMigrator(config=self.config, client=self.mock_client)

    def test_refuse_source_equals_target(self):
        with self.assertRaisesRegex(HippoValidationError, "cannot be identical"):
            self.migrator.detect_collections(
                source_collection="same_col",
                target_collection="same_col",
                target_provider="vertexai",
            )

    def test_dry_run_zero_writes_and_no_embed_calls(self):
        self.mock_client.collection_exists.side_effect = lambda c: c == "hippo_memories"
        mock_info = SimpleNamespace(points_count=42)
        self.mock_client.get_collection.return_value = mock_info

        plan = self.migrator.plan(
            source_collection="hippo_memories",
            target_provider="vertexai",
            target_model="gemini-embedding-2",
            target_dims=768,
        )

        self.assertEqual(plan.source_collection, "hippo_memories")
        self.assertEqual(plan.target_collection, "hippo_memories_vertexai_gemini_embedding_2_768")
        self.assertEqual(plan.source_points_count, 42)
        self.assertTrue(plan.dry_run)

        # Run migrate with dry_run=True
        result = self.migrator.migrate(
            source_collection="hippo_memories",
            target_provider="vertexai",
            dry_run=True,
        )
        self.assertEqual(result.scanned, 42)
        # Assert absolutely no upsert or embed calls were performed
        self.mock_client.upsert.assert_not_called()
        self.mock_client.create_collection.assert_not_called()

    def test_schema_mismatch_gate_blocks_before_writes(self):
        self.mock_client.collection_exists.return_value = True
        # Mock target collection exists with dimension 1536
        mock_vectors = SimpleNamespace(size=1536)
        mock_params = SimpleNamespace(vectors=mock_vectors)
        mock_config = SimpleNamespace(params=mock_params)
        mock_info = SimpleNamespace(config=mock_config, points_count=10)
        self.mock_client.get_collection.return_value = mock_info

        with self.assertRaisesRegex(HippoValidationError, "vector dimension 1536.*does not match"):
            self.migrator.validate_target_schema(
                collection_name="mismatched_col",
                expected_dims=768,
            )

    def test_successful_full_migration_primary_and_entities(self):
        # 1. Source has primary collection and entity companion collection
        def _col_exists(c):
            return c in ("hippo_memories", "hippo_memories_entities")

        self.mock_client.collection_exists.side_effect = _col_exists

        # Primary source records
        primary_records = [
            Record(
                id="mem-1",
                payload={"data": "memory one", "hash": "h1", "user_id": "u1"},
            ),
            Record(
                id="mem-2",
                payload={"data": "memory two", "hash": "h2", "user_id": "u1"},
            ),
        ]
        # Entity source records
        entity_records = [
            Record(
                id="ent-1",
                payload={"data": "entity Python", "entity_type": "tech", "linked_memory_ids": ["mem-1"]},
            ),
        ]

        def _scroll(collection_name, limit, offset=None, with_payload=True, with_vectors=False):
            if offset is not None:
                return [], None
            if collection_name == "hippo_memories":
                return primary_records, None
            elif collection_name == "hippo_memories_entities":
                return entity_records, None
            return [], None

        self.mock_client.scroll.side_effect = _scroll
        # Target collections do not yet have these IDs
        self.mock_client.retrieve.return_value = []

        mock_target_store = MagicMock()
        mock_embedder = MagicMock()
        mock_embedder.embed_batch.return_value = [[0.1] * 768, [0.2] * 768]

        with patch.object(self.migrator, "build_target_vector_store", return_value=mock_target_store), \
             patch.object(self.migrator, "build_target_embedder", return_value=mock_embedder):

            result = self.migrator.migrate(
                source_collection="hippo_memories",
                target_provider="vertexai",
                target_dims=768,
                batch_size=10,
            )

        self.assertTrue(result.success)
        self.assertEqual(result.scanned, 3)  # 2 primary + 1 entity
        self.assertEqual(result.migrated, 3)
        self.assertEqual(result.skipped, 0)
        self.assertEqual(result.conflicted, 0)
        self.assertEqual(result.failed, 0)

        # Verify insert called on target store with preserved IDs and payloads
        self.assertEqual(mock_target_store.insert.call_count, 2)
        first_call = mock_target_store.insert.call_args_list[0]
        self.assertEqual(first_call.kwargs["ids"], ["mem-1", "mem-2"])
        self.assertEqual(first_call.kwargs["payloads"][0]["data"], "memory one")

    def test_resume_skips_matching_records(self):
        # Target already contains mem-1 with identical payload
        self.mock_client.collection_exists.side_effect = lambda c: c in ("hippo_memories", "target_col")

        source_records = [
            Record(id="mem-1", payload={"data": "memory one", "hash": "h1"}),
            Record(id="mem-2", payload={"data": "memory two", "hash": "h2"}),
        ]
        self.mock_client.scroll.side_effect = [
            (source_records, None),
        ]
        # Target retrieve returns mem-1
        self.mock_client.retrieve.return_value = [
            Record(id="mem-1", payload={"data": "memory one", "hash": "h1"}),
        ]

        mock_target_store = MagicMock()
        mock_embedder = MagicMock()
        mock_embedder.embed_batch.return_value = [[0.2] * 768]

        with patch.object(self.migrator, "build_target_vector_store", return_value=mock_target_store), \
             patch.object(self.migrator, "build_target_embedder", return_value=mock_embedder):

            result = self.migrator.migrate(
                source_collection="hippo_memories",
                target_collection="target_col",
                target_provider="vertexai",
                target_dims=768,
                recompute_existing=False,
            )

        self.assertTrue(result.success)
        self.assertEqual(result.scanned, 2)
        self.assertEqual(result.skipped, 1)  # mem-1 skipped
        self.assertEqual(result.migrated, 1)  # mem-2 migrated
        # Only mem-2 was embedded and inserted
        mock_embedder.embed_batch.assert_called_once_with(["memory two"], memory_action="add")
        mock_target_store.insert.assert_called_once()
        self.assertEqual(mock_target_store.insert.call_args.kwargs["ids"], ["mem-2"])

    def test_recompute_existing_forces_reembedding(self):
        self.mock_client.collection_exists.side_effect = lambda c: c in ("hippo_memories", "target_col")

        source_records = [
            Record(id="mem-1", payload={"data": "memory one", "hash": "h1"}),
        ]
        self.mock_client.scroll.return_value = (source_records, None)
        self.mock_client.retrieve.return_value = [
            Record(id="mem-1", payload={"data": "memory one", "hash": "h1"}),
        ]

        mock_target_store = MagicMock()
        mock_embedder = MagicMock()
        mock_embedder.embed_batch.return_value = [[0.5] * 768]

        with patch.object(self.migrator, "build_target_vector_store", return_value=mock_target_store), \
             patch.object(self.migrator, "build_target_embedder", return_value=mock_embedder):

            result = self.migrator.migrate(
                source_collection="hippo_memories",
                target_collection="target_col",
                target_provider="vertexai",
                target_dims=768,
                recompute_existing=True,  # Force recompute
            )

        self.assertTrue(result.success)
        self.assertEqual(result.skipped, 0)
        self.assertEqual(result.migrated, 1)
        mock_target_store.insert.assert_called_once()
        self.assertEqual(mock_target_store.insert.call_args.kwargs["ids"], ["mem-1"])

    def test_payload_conflict_gate_blocks_divergent_records(self):
        self.mock_client.collection_exists.side_effect = lambda c: c in ("hippo_memories", "target_col")

        # mem-1 has divergent payload (different text and hash)
        source_records = [
            Record(id="mem-1", payload={"data": "source fact", "hash": "h_src"}),
        ]
        self.mock_client.scroll.return_value = (source_records, None)
        self.mock_client.retrieve.return_value = [
            Record(id="mem-1", payload={"data": "target conflicting fact", "hash": "h_tgt"}),
        ]

        mock_target_store = MagicMock()
        mock_embedder = MagicMock()

        with patch.object(self.migrator, "build_target_vector_store", return_value=mock_target_store), \
             patch.object(self.migrator, "build_target_embedder", return_value=mock_embedder):

            result = self.migrator.migrate(
                source_collection="hippo_memories",
                target_collection="target_col",
                target_provider="vertexai",
                target_dims=768,
            )

        self.assertFalse(result.success)
        self.assertEqual(result.scanned, 1)
        self.assertEqual(result.conflicted, 1)
        self.assertEqual(result.migrated, 0)
        self.assertIn("mem-1", result.conflict_ids)
        # Never overwrite conflicting target record
        mock_target_store.insert.assert_not_called()

    def test_generated_vector_dimension_mismatch_fails_fast(self):
        self.mock_client.collection_exists.return_value = True
        source_records = [
            Record(id="mem-1", payload={"data": "test text"}),
        ]
        self.mock_client.scroll.return_value = (source_records, None)
        self.mock_client.retrieve.return_value = []

        mock_target_store = MagicMock()
        mock_embedder = MagicMock()
        # Returns 256 dimensions when 768 was expected
        mock_embedder.embed_batch.return_value = [[0.1] * 256]

        with patch.object(self.migrator, "build_target_vector_store", return_value=mock_target_store), \
             patch.object(self.migrator, "build_target_embedder", return_value=mock_embedder):

            with self.assertRaises(HippoValidationError):
                self.migrator.migrate(
                    source_collection="hippo_memories",
                    target_collection="target_col",
                    target_provider="vertexai",
                    target_dims=768,
                )

    def test_source_collection_untouched(self):
        # Verify source collection is never mutated (no delete/clear/upsert on source)
        self.mock_client.collection_exists.return_value = True
        source_records = [
            Record(id="mem-1", payload={"data": "sample"}),
        ]
        self.mock_client.scroll.return_value = (source_records, None)
        self.mock_client.retrieve.return_value = []

        mock_target_store = MagicMock()
        mock_embedder = MagicMock()
        mock_embedder.embed_batch.return_value = [[0.1] * 768]

        with patch.object(self.migrator, "build_target_vector_store", return_value=mock_target_store), \
             patch.object(self.migrator, "build_target_embedder", return_value=mock_embedder):

            self.migrator.migrate(
                source_collection="hippo_memories",
                target_collection="target_col",
                target_provider="vertexai",
                target_dims=768,
            )

        # Client upsert or delete must never be called on 'hippo_memories'
        for call_item in self.mock_client.method_calls:
            name, args, kwargs = call_item
            if name in ("upsert", "delete", "clear_payload", "overwrite_payload"):
                collection = kwargs.get("collection_name") or (args[0] if args else None)
                self.assertNotEqual(collection, "hippo_memories")


if __name__ == "__main__":
    unittest.main()
