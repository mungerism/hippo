import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from google import genai
from google.auth.credentials import AnonymousCredentials
from mem0.configs.embeddings.base import BaseEmbedderConfig

from hippo_memory.config import HippoConfig, resolve_collection_name
from hippo_memory.embeddings.vertex_genai import VertexAIGenAIEmbedding


class TestVertexAIProvider(unittest.TestCase):
    def _config(self, env: dict[str, str]) -> HippoConfig:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with patch.dict(os.environ, env, clear=True), patch(
            "hippo_memory.config.ensure_qdrant_server"
        ):
            return HippoConfig(storage_dir=Path(tmp.name))

    def test_vertex_config_defaults(self):
        env = {
            "HIPPO_PROVIDER": "vertexai",
            "GOOGLE_CLOUD_PROJECT": "hippo-test",
        }
        with patch.dict(os.environ, env, clear=True), patch(
            "hippo_memory.config.ensure_qdrant_server"
        ):
            config = HippoConfig(storage_dir=Path(tempfile.mkdtemp()))
            mem0_config = config.get_mem0_config()

        self.assertEqual(config.provider, "vertexai")
        self.assertEqual(mem0_config["llm"]["provider"], "gemini")
        self.assertTrue(mem0_config["llm"]["config"]["vertexai"])
        self.assertEqual(mem0_config["llm"]["config"]["project"], "hippo-test")
        self.assertEqual(mem0_config["llm"]["config"]["location"], "global")
        self.assertEqual(mem0_config["embedder"]["provider"], "vertexai")
        self.assertEqual(
            mem0_config["embedder"]["config"]["model"], "gemini-embedding-2"
        )
        self.assertEqual(mem0_config["embedder"]["config"]["embedding_dims"], 768)
        self.assertEqual(
            mem0_config["vector_store"]["config"]["collection_name"],
            "hippo_memories_vertexai_gemini_embedding_2_768",
        )

    def test_vertex_config_custom_values(self):
        env = {
            "HIPPO_PROVIDER": "vertexai",
            "GOOGLE_CLOUD_PROJECT": "custom-project",
            "GOOGLE_CLOUD_LOCATION": "us",
            "VERTEX_LLM_MODEL": "gemini-custom",
            "VERTEX_EMBEDDING_MODEL": "gemini-embedding-2",
            "VERTEX_EMBEDDING_DIMS": "256",
        }
        with patch.dict(os.environ, env, clear=True), patch(
            "hippo_memory.config.ensure_qdrant_server"
        ):
            config = HippoConfig(storage_dir=Path(tempfile.mkdtemp()))
            mem0_config = config.get_mem0_config()

        self.assertEqual(mem0_config["llm"]["config"]["model"], "gemini-custom")
        self.assertEqual(mem0_config["llm"]["config"]["location"], "us")
        self.assertEqual(
            mem0_config["embedder"]["config"]["model"],
            "gemini-embedding-2",
        )
        self.assertEqual(mem0_config["embedder"]["config"]["embedding_dims"], 256)
        self.assertEqual(
            mem0_config["vector_store"]["config"]["collection_name"],
            "hippo_memories_vertexai_gemini_embedding_2_256",
        )

    def test_vertex_requires_project(self):
        env = {"HIPPO_PROVIDER": "vertexai"}
        with patch.dict(os.environ, env, clear=True), patch(
            "hippo_memory.config.ensure_qdrant_server"
        ):
            config = HippoConfig(storage_dir=Path(tempfile.mkdtemp()))
            with self.assertRaisesRegex(ValueError, "GOOGLE_CLOUD_PROJECT"):
                config.get_mem0_config()

    def test_auto_does_not_select_vertex(self):
        env = {
            "HIPPO_PROVIDER": "auto",
            "GOOGLE_CLOUD_PROJECT": "adc-project",
        }
        with patch.dict(os.environ, env, clear=True), patch(
            "hippo_memory.config.ensure_qdrant_server"
        ):
            config = HippoConfig(storage_dir=Path(tempfile.mkdtemp()))
        self.assertNotEqual(config.provider, "vertexai")
        self.assertEqual(config.provider, "gemini")

    def test_collection_override(self):
        env = {"HIPPO_COLLECTION_NAME": "hippo_custom"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(
                resolve_collection_name("vertexai", "gemini-embedding-2", 768),
                "hippo_custom",
            )

    def test_vertex_registration_replaces_mem0_legacy_embedder(self):
        from mem0.utils.factory import EmbedderFactory

        original = EmbedderFactory.provider_to_class["vertexai"]
        self.addCleanup(
            EmbedderFactory.provider_to_class.__setitem__, "vertexai", original
        )
        env = {
            "HIPPO_PROVIDER": "vertexai",
            "GOOGLE_CLOUD_PROJECT": "hippo-test",
        }
        with patch.dict(os.environ, env, clear=True), patch(
            "hippo_memory.config.ensure_qdrant_server"
        ):
            config = HippoConfig(storage_dir=Path(tempfile.mkdtemp()))
            config.get_mem0_config()

        self.assertEqual(
            EmbedderFactory.provider_to_class["vertexai"],
            "hippo_memory.embeddings.vertex_genai.VertexAIGenAIEmbedding",
        )

    def test_embedding_2_formats_retrieval_instruction_in_prompt(self):
        fake_client = MagicMock()
        fake_client.models.embed_content.return_value = SimpleNamespace(
            embeddings=[SimpleNamespace(values=[0.1, 0.2])]
        )
        env = {
            "GOOGLE_CLOUD_PROJECT": "hippo-test",
            "GOOGLE_CLOUD_LOCATION": "global",
        }
        config = BaseEmbedderConfig(
            model="gemini-embedding-2",
            embedding_dims=768,
        )

        with patch.dict(os.environ, env, clear=True), patch(
            "hippo_memory.embeddings.vertex_genai.genai.Client",
            return_value=fake_client,
        ) as client_cls:
            embedder = VertexAIGenAIEmbedding(config)
            values = embedder.embed("memory query", memory_action="search")

        self.assertEqual(values, [0.1, 0.2])
        client_cls.assert_called_once_with(
            vertexai=True,
            project="hippo-test",
            location="global",
        )
        call = fake_client.models.embed_content.call_args
        self.assertEqual(call.kwargs["model"], "gemini-embedding-2")
        self.assertEqual(
            call.kwargs["contents"],
            "task: search result | query: memory query",
        )
        self.assertEqual(call.kwargs["config"].output_dimensionality, 768)
        self.assertIsNone(call.kwargs["config"].task_type)

    def test_embedding_2_formats_documents_for_add_and_update(self):
        fake_client = MagicMock()
        fake_client.models.embed_content.return_value = SimpleNamespace(
            embeddings=[SimpleNamespace(values=[0.1])]
        )
        env = {"GOOGLE_CLOUD_PROJECT": "hippo-test"}
        config = BaseEmbedderConfig(
            model="gemini-embedding-2",
            embedding_dims=768,
        )

        with patch.dict(os.environ, env, clear=True), patch(
            "hippo_memory.embeddings.vertex_genai.genai.Client",
            return_value=fake_client,
        ):
            embedder = VertexAIGenAIEmbedding(config)
            embedder.embed("fact", memory_action="add")
            add_call = fake_client.models.embed_content.call_args
            self.assertEqual(add_call.kwargs["contents"], "title: none | text: fact")

            embedder.embed("updated fact", memory_action="update")
            update_call = fake_client.models.embed_content.call_args
            self.assertEqual(
                update_call.kwargs["contents"],
                "title: none | text: updated fact",
            )

    def test_unsupported_embedding_model_raises_error(self):
        env = {"GOOGLE_CLOUD_PROJECT": "hippo-test"}
        config = BaseEmbedderConfig(
            model="gemini-embedding-001",
            embedding_dims=768,
        )
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(
                ValueError, "Vertex AI provider currently only supports gemini-embedding-2"
            ):
                VertexAIGenAIEmbedding(config)

    def test_embedding_2_preview_rejected(self):
        env = {"GOOGLE_CLOUD_PROJECT": "hippo-test"}
        config = BaseEmbedderConfig(
            model="gemini-embedding-2-preview",
            embedding_dims=768,
        )
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(
                ValueError, "Vertex AI provider currently only supports gemini-embedding-2"
            ):
                VertexAIGenAIEmbedding(config)

    def test_embedding_2_batch_embeds_each_text_separately(self):
        client = genai.Client(
            vertexai=True,
            project="hippo-test",
            location="global",
            credentials=AnonymousCredentials(),
        )
        responses = [
            SimpleNamespace(embeddings=[SimpleNamespace(values=[0.1])]),
            SimpleNamespace(embeddings=[SimpleNamespace(values=[0.2])]),
        ]
        env = {"GOOGLE_CLOUD_PROJECT": "hippo-test"}
        config = BaseEmbedderConfig(
            model="gemini-embedding-2",
            embedding_dims=256,
        )

        with (
            patch.dict(os.environ, env, clear=True),
            patch(
                "hippo_memory.embeddings.vertex_genai.genai.Client",
                return_value=client,
            ),
            patch.object(client.models, "_embed_content", side_effect=responses) as request,
        ):
            result = VertexAIGenAIEmbedding(config).embed_batch(
                ["a", "b"], memory_action="add"
            )

        self.assertEqual(result, [[0.1], [0.2]])
        self.assertEqual(request.call_count, 2)
        self.assertEqual(
            [
                call.kwargs["content"].parts[0].text
                for call in request.call_args_list
            ],
            ["title: none | text: a", "title: none | text: b"],
        )
        for call in request.call_args_list:
            self.assertEqual(len(call.kwargs["content"].parts), 1)
            self.assertEqual(call.kwargs["config"].output_dimensionality, 256)

    def test_doctor_vertex_adc_success_and_failure(self):
        from hippo_memory import doctor

        env = {
            "HIPPO_PROVIDER": "vertexai",
            "GOOGLE_CLOUD_PROJECT": "hippo-test",
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(doctor._active_provider(), "vertexai")
            with patch(
                "google.auth.default",
                return_value=(object(), "detected-project"),
            ):
                ok, detail = doctor._provider_credentials_status("vertexai")
                self.assertTrue(ok)
                self.assertIn("ADC 可用", detail)

            with patch("google.auth.default", side_effect=RuntimeError("no adc")):
                ok, detail = doctor._provider_credentials_status("vertexai")
                self.assertFalse(ok)
                self.assertIn("ADC 不可用", detail)

    def test_doctor_invalid_vertex_embedding_dims_does_not_crash(self):
        from hippo_memory import doctor

        env = {
            "HIPPO_PROVIDER": "vertexai",
            "GOOGLE_CLOUD_PROJECT": "hippo-test",
            "VERTEX_EMBEDDING_DIMS": "abc",
        }
        with patch.dict(os.environ, env, clear=True), patch(
            "hippo_memory.doctor.is_listening", return_value=False
        ):
            checks = doctor.collect_checks()

        collection_checks = [c for c in checks if c["name"] == "Qdrant collection"]
        self.assertEqual(len(collection_checks), 1)
        self.assertFalse(collection_checks[0]["ok"])
        self.assertIn("must be an integer", collection_checks[0]["detail"])

    def test_sanitize_google_application_credentials_removes_missing_file(self):
        from hippo_memory.config import sanitize_google_application_credentials

        fake_path = "/tmp/hippo-test-nonexistent-key-999.json"
        with patch.dict(os.environ, {"GOOGLE_APPLICATION_CREDENTIALS": fake_path}):
            removed = sanitize_google_application_credentials()
            self.assertEqual(removed, fake_path)
            self.assertNotIn("GOOGLE_APPLICATION_CREDENTIALS", os.environ)

    def test_sanitize_google_application_credentials_preserves_existing_file(self):
        from hippo_memory.config import sanitize_google_application_credentials

        with tempfile.NamedTemporaryFile() as tmp:
            with patch.dict(os.environ, {"GOOGLE_APPLICATION_CREDENTIALS": tmp.name}):
                removed = sanitize_google_application_credentials()
                self.assertIsNone(removed)
                self.assertEqual(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"), tmp.name)

    def test_doctor_cleans_up_missing_credentials(self):
        from hippo_memory import doctor

        fake_path = "/tmp/hippo-test-nonexistent-key-888.json"
        env = {
            "HIPPO_PROVIDER": "vertexai",
            "GOOGLE_CLOUD_PROJECT": "hippo-test",
            "GOOGLE_APPLICATION_CREDENTIALS": fake_path,
        }
        with patch.dict(os.environ, env):
            with patch(
                "google.auth.default",
                return_value=(object(), "detected-project"),
            ):
                ok, _ = doctor._provider_credentials_status("vertexai")
                self.assertTrue(ok)
                self.assertNotIn("GOOGLE_APPLICATION_CREDENTIALS", os.environ)


if __name__ == "__main__":
    unittest.main()
