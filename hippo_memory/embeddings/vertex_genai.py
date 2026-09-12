"""Vertex AI embeddings backed by the Google Gen AI SDK.

Mem0's current ``vertexai`` embedder still uses ``vertexai.language_models``.
That path does not support ``gemini-embedding-2`` correctly, so Hippo replaces
only the Mem0 factory implementation for the Vertex provider while preserving
Mem0's public configuration shape.
"""

from __future__ import annotations

import os
from typing import Literal, Optional

from google import genai
from google.genai import types
from mem0.configs.embeddings.base import BaseEmbedderConfig
from mem0.embeddings.base import EmbeddingBase


class VertexAIGenAIEmbedding(EmbeddingBase):
    """Vertex AI embedding adapter using ``google-genai`` and ADC."""

    def __init__(self, config: Optional[BaseEmbedderConfig] = None):
        super().__init__(config)

        self.config.model = self.config.model or "gemini-embedding-2"
        self.config.embedding_dims = self.config.embedding_dims or 768

        model_id = (self.config.model or "").split("/")[-1]
        if model_id != "gemini-embedding-2":
            raise ValueError(
                f"Vertex AI provider currently only supports gemini-embedding-2 (got '{self.config.model}')"
            )

        project = os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
        if not project:
            raise ValueError(
                "GOOGLE_CLOUD_PROJECT is required for the Vertex AI embedding provider."
            )

        location = os.getenv("GOOGLE_CLOUD_LOCATION", "global").strip() or "global"
        self.client = genai.Client(
            vertexai=True,
            project=project,
            location=location,
        )

    def _prepare_text(
        self,
        text: str,
        memory_action: Optional[Literal["add", "search", "update"]],
    ) -> str:
        """Format task instructions for Gemini Embedding 2 retrieval use cases.

        Google does not support ``task_type`` for Gemini Embedding 2. For this
        model the retrieval intent is encoded in the prompt itself.
        """
        if memory_action == "search":
            return f"task: search result | query: {text}"
        if memory_action in ("add", "update"):
            return f"title: none | text: {text}"
        return f"task: sentence similarity | query: {text}"

    def _embed_config(self) -> types.EmbedContentConfig:
        return types.EmbedContentConfig(
            output_dimensionality=self.config.embedding_dims
        )

    def embed(
        self,
        text: str,
        memory_action: Optional[Literal["add", "search", "update"]] = None,
    ) -> list[float]:
        response = self.client.models.embed_content(
            model=self.config.model,
            contents=self._prepare_text(text, memory_action),
            config=self._embed_config(),
        )
        return response.embeddings[0].values

    def embed_batch(
        self,
        texts: list[str],
        memory_action: Optional[Literal["add", "search", "update"]] = "add",
    ) -> list[list[float]]:
        if not texts:
            return []

        # Vertex AI's embedContent endpoint accepts only one Content per
        # request. Passing list[str] would be normalized into one multi-part
        # Content and produce a single combined embedding, so Gemini Embedding
        # 2 must embed each text sequentially to preserve Mem0's contract.
        return [self.embed(text, memory_action) for text in texts]
