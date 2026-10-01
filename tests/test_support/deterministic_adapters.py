"""Deterministic, credential-free Mem0 adapters and contract harnesses (#81)."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from mem0 import Memory
from mem0.configs.llms.base import BaseLlmConfig
from mem0.embeddings.base import EmbeddingBase
from mem0.llms.base import LLMBase
from mem0.utils.factory import EmbedderFactory, LlmFactory
from qdrant_client import QdrantClient

from hippo_memory.config import HippoConfig
from hippo_memory.engine import HippoEngine

_FACTORY_LOCK = threading.RLock()
_MISSING = object()


class DeterministicEmbedder(EmbeddingBase):
    """Pure offline deterministic embedding generator."""

    def __init__(self, config: Optional[Any] = None):
        self.config = config
        if hasattr(config, "embedding_dims") and getattr(config, "embedding_dims") is not None:
            self.dims = int(getattr(config, "embedding_dims"))
        elif isinstance(config, dict):
            self.dims = int(config.get("dims", config.get("embedding_dims", 768)))
        else:
            self.dims = 768
        self.call_count = 0
        self.pause_event: Optional[threading.Event] = None
        self.pause_on_text: Optional[str] = None

    def _hash_to_vector(self, text: str) -> List[float]:
        if self.pause_event is not None:
            if self.pause_on_text is None or self.pause_on_text in text:
                self.pause_event.wait()

        self.call_count += 1
        import math
        import re

        tokens = re.findall(r"\w+", text.lower())
        vec = [0.0] * self.dims
        source = tokens or [text]
        for token in source:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            for index in range(self.dims):
                byte_val = digest[index % len(digest)]
                vec[index] += ((byte_val / 255.0) * 2.0 - 1.0)

        norm = math.sqrt(sum(value * value for value in vec)) or 1.0
        return [round(value / norm, 6) for value in vec]

    def embed(self, text: str, memory_action: Optional[str] = None, **kwargs: Any) -> List[float]:
        return self._hash_to_vector(text)

    def embed_batch(
        self,
        texts: List[str],
        memory_action: Optional[str] = None,
        **kwargs: Any,
    ) -> List[List[float]]:
        return [self._hash_to_vector(text) for text in texts]


class DeterministicLlm(LLMBase):
    """Offline rule-based LLM simulator for Mem0 extraction."""

    def __init__(self, config: Optional[Any] = None):
        self.config = config
        self.call_count = 0
        self.custom_responses: List[Union[str, Dict[str, Any]]] = []

    def queue_response(self, response: Union[str, Dict[str, Any]]) -> None:
        self.custom_responses.append(response)

    def generate_response(
        self,
        messages: List[Dict[str, Any]],
        response_format: Optional[Dict[str, Any]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        self.call_count += 1

        if self.custom_responses:
            response = self.custom_responses.pop(0)
            return json.dumps(response) if isinstance(response, dict) else str(response)

        last_user_content = ""
        for message in reversed(messages):
            if message.get("role") == "user":
                last_user_content = str(message.get("content", ""))
                break

        target_content = last_user_content
        if "## New Messages" in last_user_content:
            target_content = last_user_content.split("## New Messages", 1)[1]
            if "\n##" in target_content:
                target_content = target_content.split("\n##", 1)[0]

        if "GENUINE_EMPTY" in target_content:
            return json.dumps({"memory": []})
        if "POST_TRANSIENT" in target_content:
            return json.dumps({"memory": [{"text": "ok", "event": "ADD"}]})

        lines = []
        for raw_line in target_content.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("IGNORE:"):
                continue
            if line.lower().startswith("user:"):
                line = line[5:].strip()
            elif line.lower().startswith("assistant:"):
                line = line[10:].strip()
            if line:
                lines.append(line)

        return json.dumps(
            {"memory": [{"text": f"Fact: {line}", "event": "ADD"} for line in lines]}
        )


@contextmanager
def _deterministic_factories():
    """Temporarily bind Mem0's validated openai provider key to local adapters."""
    with _FACTORY_LOCK:
        previous_embedder = EmbedderFactory.provider_to_class.get("openai", _MISSING)
        previous_llm = LlmFactory.provider_to_class.get("openai", _MISSING)
        EmbedderFactory.provider_to_class["openai"] = (
            "tests.test_support.deterministic_adapters.DeterministicEmbedder"
        )
        LlmFactory.provider_to_class["openai"] = (
            "tests.test_support.deterministic_adapters.DeterministicLlm",
            BaseLlmConfig,
        )
        try:
            yield
        finally:
            if previous_embedder is _MISSING:
                EmbedderFactory.provider_to_class.pop("openai", None)
            else:
                EmbedderFactory.provider_to_class["openai"] = previous_embedder
            if previous_llm is _MISSING:
                LlmFactory.provider_to_class.pop("openai", None)
            else:
                LlmFactory.provider_to_class["openai"] = previous_llm


def create_isolated_mem0(
    collection_name: str = "test_contract_col",
    dims: int = 768,
    storage_dir: Optional[str] = None,
    qdrant_client: Optional[QdrantClient] = None,
) -> Memory:
    """Create real Mem0 orchestration with deterministic providers and isolated storage."""
    client = qdrant_client or QdrantClient(":memory:")
    history_db = ":memory:" if storage_dir is None else str(Path(storage_dir) / "history.db")
    config = {
        "vector_store": {
            "provider": "qdrant",
            "config": {
                "client": client,
                "collection_name": collection_name,
                "embedding_model_dims": dims,
            },
        },
        "llm": {"provider": "openai", "config": {}},
        "embedder": {"provider": "openai", "config": {"embedding_dims": dims}},
        "history_db_path": history_db,
    }
    with _deterministic_factories():
        return Memory.from_config(config)


def create_contract_engine(
    collection_name: str = "test_contract_engine",
    storage_dir: Optional[str] = None,
    qdrant_client: Optional[QdrantClient] = None,
) -> HippoEngine:
    """Bind HippoEngine to deterministic Mem0 with in-memory or explicit Qdrant."""
    mem = create_isolated_mem0(
        collection_name=collection_name,
        storage_dir=storage_dir,
        qdrant_client=qdrant_client,
    )
    config = HippoConfig(storage_dir=storage_dir) if storage_dir else HippoConfig()
    if config.gate_config is not None:
        config.gate_config = dataclasses.replace(config.gate_config, enabled=False)
    engine = HippoEngine(config=config)
    engine._memory = mem
    engine._hook_memory_persistence(mem)
    return engine
