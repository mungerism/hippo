"""Deterministic, credential-free adapters and contract harnesses for Mem0 (#81).

Provides:
- DeterministicEmbedder: Pure local 768-dim hash-based embedder with pause/resume support.
- DeterministicLlm: Rule-based structured extractor supporting empty, transient, and custom facts.
- Factory registration helper for Mem0 and isolated HippoEngine builder.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Union

from mem0 import Memory
from mem0.embeddings.base import EmbeddingBase
from mem0.llms.base import LLMBase
from mem0.utils.factory import EmbedderFactory, LlmFactory
from qdrant_client import QdrantClient

from hippo_memory.engine import HippoEngine


class DeterministicEmbedder(EmbeddingBase):
    """Pure offline deterministic embedding generator with token-aware cosine similarity."""

    def __init__(self, config: Optional[Any] = None):
        self.config = config
        if hasattr(config, "embedding_dims") and getattr(config, "embedding_dims") is not None:
            self.dims: int = int(getattr(config, "embedding_dims"))
        elif isinstance(config, dict):
            self.dims: int = int(config.get("dims", config.get("embedding_dims", 768)))
        else:
            self.dims = 768
        self.call_count: int = 0
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

        if tokens:
            for token in tokens:
                digest = hashlib.sha256(token.encode("utf-8")).digest()
                for i in range(self.dims):
                    byte_val = digest[i % len(digest)]
                    vec[i] += ((byte_val / 255.0) * 2.0 - 1.0)
        else:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            for i in range(self.dims):
                byte_val = digest[i % len(digest)]
                vec[i] += ((byte_val / 255.0) * 2.0 - 1.0)

        # L2-normalize to unit vector for cosine distance
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [round(x / norm, 6) for x in vec]

    def embed(self, text: str, memory_action: Optional[str] = None, **kwargs: Any) -> List[float]:
        return self._hash_to_vector(text)

    def embed_batch(self, texts: List[str], memory_action: Optional[str] = None, **kwargs: Any) -> List[List[float]]:
        return [self._hash_to_vector(t) for t in texts]


class DeterministicLlm(LLMBase):
    """Offline rule-based LLM simulator returning deterministic Mem0 fact extraction payloads."""

    def __init__(self, config: Optional[Any] = None):
        self.config = config
        self.call_count: int = 0
        self.custom_responses: List[Union[str, Dict[str, Any]]] = []

    def queue_response(self, response: Union[str, Dict[str, Any]]) -> None:
        """Queue a specific response for the next extraction call."""
        self.custom_responses.append(response)

    def generate_response(
        self,
        messages: List[Dict[str, Any]],
        response_format: Optional[Dict[str, Any]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        self.call_count += 1

        if self.custom_responses:
            next_resp = self.custom_responses.pop(0)
            if isinstance(next_resp, dict):
                return json.dumps(next_resp)
            return str(next_resp)

        # Inspect last user message
        last_user_content = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                last_user_content = str(msg.get("content", ""))
                break

        target_content = last_user_content
        if "## New Messages" in last_user_content:
            part = last_user_content.split("## New Messages", 1)[1]
            if "\n##" in part:
                target_content = part.split("\n##", 1)[0]
            else:
                target_content = part

        if "GENUINE_EMPTY" in target_content:
            return json.dumps({"memory": []})

        if "POST_TRANSIENT" in target_content:
            return json.dumps({"memory": [{"text": "ok", "event": "ADD"}]})

        # By default extract clean facts from target message lines
        lines = []
        for raw_line in target_content.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("IGNORE:"):
                continue
            # Strip role prefix like "user: "
            if line.lower().startswith("user:"):
                line = line[5:].strip()
            elif line.lower().startswith("assistant:"):
                line = line[10:].strip()
            if line:
                lines.append(line)

        if not lines:
            return json.dumps({"memory": []})

        memories = [
            {"text": f"Fact: {line}", "event": "ADD"}
            for line in lines
        ]
        return json.dumps({"memory": memories})


from mem0.configs.llms.base import BaseLlmConfig

# Register adapters in Mem0 factories under standard provider key
EmbedderFactory.provider_to_class["openai"] = (
    "tests.test_support.deterministic_adapters.DeterministicEmbedder"
)
LlmFactory.provider_to_class["openai"] = (
    "tests.test_support.deterministic_adapters.DeterministicLlm",
    BaseLlmConfig,
)


def create_isolated_mem0(
    collection_name: str = "test_contract_col",
    dims: int = 768,
    storage_dir: Optional[str] = None,
) -> Memory:
    """Create a real Mem0 Memory instance using deterministic adapters and isolated storage."""
    qdrant_client = QdrantClient(":memory:")
    history_db = ":memory:" if storage_dir is None else str(Path(storage_dir) / "history.db")

    config = {
        "vector_store": {
            "provider": "qdrant",
            "config": {
                "client": qdrant_client,
                "collection_name": collection_name,
                "embedding_model_dims": dims,
            },
        },
        "llm": {"provider": "openai", "config": {}},
        "embedder": {"provider": "openai", "config": {"embedding_dims": dims}},
        "history_db_path": history_db,
    }
    return Memory.from_config(config)


import os

os.environ["MEM0_TELEMETRY"] = "false"
from hippo_memory.config import HippoConfig


import dataclasses


def create_contract_engine(
    collection_name: str = "test_contract_engine",
    storage_dir: Optional[str] = None,
) -> HippoEngine:
    """Create a HippoEngine bound to a real isolated Mem0 instance with deterministic adapters."""
    mem = create_isolated_mem0(collection_name=collection_name, storage_dir=storage_dir)
    config = HippoConfig(storage_dir=storage_dir) if storage_dir else HippoConfig()
    if hasattr(config, "gate_config") and config.gate_config is not None:
        config.gate_config = dataclasses.replace(config.gate_config, enabled=False)
    engine = HippoEngine(config=config)
    engine._memory = mem
    engine._hook_memory_persistence(mem)
    return engine
