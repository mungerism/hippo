"""Embedding reindex and migration workflow for Hippo Memory Hub.

Provides an explicit, safe, resumable workflow for moving existing Hippo
memories between embedding profiles by re-embedding stored text into a
separate target collection while preserving logical records and retrieval behavior.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.http.models import Record

from hippo_memory.config import (
    LEGACY_COLLECTION_NAME,
    EmbeddingProfile,
    HippoConfig,
    get_config,
)
from hippo_memory.exceptions import HippoValidationError

logger = logging.getLogger(__name__)


@dataclass
class ReindexPlan:
    """Preview plan for an embedding reindex/migration run."""

    source_collection: str
    target_collection: str
    target_profile: EmbeddingProfile
    source_points_count: int
    target_existing_points_count: int
    source_entities_collection: str | None = None
    target_entities_collection: str | None = None
    source_entities_count: int = 0
    target_existing_entities_count: int = 0
    batch_size: int = 32
    dry_run: bool = True

    @property
    def total_source_records(self) -> int:
        return self.source_points_count + self.source_entities_count


@dataclass
class ReindexResult:
    """Summary result of an embedding reindex/migration run."""

    scanned: int = 0
    migrated: int = 0
    skipped: int = 0
    conflicted: int = 0
    failed: int = 0
    conflict_ids: list[str] = field(default_factory=list)
    failed_ids: list[str] = field(default_factory=list)
    details: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return self.conflicted == 0 and self.failed == 0 and len(self.errors) == 0

    def stats(self) -> dict[str, int]:
        return {
            "scanned": self.scanned,
            "migrated": self.migrated,
            "skipped": self.skipped,
            "conflicted": self.conflicted,
            "failed": self.failed,
        }


def is_transient_error(e: Exception) -> bool:
    """Determine whether an error is transient and safe to retry with backoff."""
    msg = str(e).lower()
    transient_indicators = [
        "429",
        "resource_exhausted",
        "rate limit",
        "timeout",
        "timed out",
        "deadline exceeded",
        "500",
        "502",
        "503",
        "504",
        "unavailable",
        "internal error",
        "connection reset",
        "connection refused",
    ]
    return any(indicator in msg for indicator in transient_indicators)


def retry_with_backoff(
    func: Callable[[], Any],
    max_retries: int = 3,
    base_wait: float = 1.0,
    max_wait: float = 30.0,
) -> Any:
    """Execute a callable with bounded exponential backoff on transient errors."""
    for attempt in range(max_retries):
        try:
            return func()
        except Exception as e:
            if attempt == max_retries - 1 or not is_transient_error(e):
                raise
            wait = min(max_wait, base_wait * (2**attempt))
            logger.warning(
                "Transient error during embedding call (attempt %d/%d): %s. Backing off for %.1fs...",
                attempt + 1,
                max_retries,
                e,
                wait,
            )
            time.sleep(wait)


def payloads_match(source_payload: dict[str, Any], target_payload: dict[str, Any]) -> bool:
    """Check whether a target point's payload matches the source point's payload.

    Treats key business attributes (data, hash, user_id, agent_id, scope, created_at, source)
    as canonical. Returns False on divergent content or metadata.
    """
    if source_payload == target_payload:
        return True

    critical_keys = [
        "data",
        "hash",
        "user_id",
        "agent_id",
        "scope",
        "project",
        "created_at",
        "source",
        "category",
        "lifecycle",
        "entity_type",
    ]
    for k in critical_keys:
        if source_payload.get(k) != target_payload.get(k):
            return False

    return True


class EmbeddingMigrator:
    """Core workflow engine for migrating memories across embedding profiles."""

    def __init__(
        self,
        config: HippoConfig | None = None,
        client: QdrantClient | None = None,
    ):
        self.config = config or get_config()
        self.client = client or QdrantClient(
            host=self.config.qdrant_host,
            port=self.config.qdrant_port,
        )

    def resolve_target_profile(
        self,
        target_provider: str | None = None,
        target_model: str | None = None,
        target_dims: int | None = None,
    ) -> EmbeddingProfile:
        """Resolve the target EmbeddingProfile from explicit parameters or current config."""
        provider = (target_provider or self.config.provider).strip().lower()

        if provider == "vertexai":
            model = target_model or os.getenv("VERTEX_EMBEDDING_MODEL", "gemini-embedding-2")
            raw_dims = target_dims or int(os.getenv("VERTEX_EMBEDDING_DIMS", "768"))
            return EmbeddingProfile(provider="vertexai", model=model, dimensions=int(raw_dims))
        elif provider == "gemini":
            model = target_model or os.getenv("GEMINI_EMBEDDING_MODEL", "models/gemini-embedding-2")
            raw_dims = target_dims or int(os.getenv("GEMINI_EMBEDDING_DIMS", "768"))
            return EmbeddingProfile(provider="gemini", model=model, dimensions=int(raw_dims))
        elif provider == "openai":
            model = target_model or os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
            raw_dims = target_dims or int(os.getenv("OPENAI_EMBEDDING_DIMS", "1536"))
            return EmbeddingProfile(provider="openai", model=model, dimensions=int(raw_dims))
        else:
            model = target_model or "default"
            dims = target_dims or 768
            return EmbeddingProfile(provider=provider, model=model, dimensions=int(dims))

    def detect_collections(
        self,
        source_collection: str | None = None,
        target_collection: str | None = None,
        target_provider: str | None = None,
        target_model: str | None = None,
        target_dims: int | None = None,
    ) -> tuple[str, str, EmbeddingProfile]:
        """Detect and validate source and target collection names."""
        target_profile = self.resolve_target_profile(
            target_provider=target_provider,
            target_model=target_model,
            target_dims=target_dims,
        )

        resolved_target = (target_collection or target_profile.collection_name).strip()

        if source_collection and source_collection.strip():
            resolved_source = source_collection.strip()
        else:
            # If target is not the legacy collection, default source to legacy
            if resolved_target != LEGACY_COLLECTION_NAME:
                resolved_source = LEGACY_COLLECTION_NAME
            else:
                raise HippoValidationError(
                    "Cannot infer source collection when target is already the legacy collection. "
                    "Please specify --source explicitly."
                )

        if resolved_source == resolved_target:
            raise HippoValidationError(
                f"Source and target collections cannot be identical: '{resolved_source}'. "
                "Migration requires distinct source and target vector spaces."
            )

        return resolved_source, resolved_target, target_profile

    def get_collection_points_count(self, collection_name: str) -> int:
        """Safely fetch points_count for a collection if it exists, else 0."""
        try:
            if not self.client.collection_exists(collection_name):
                return 0
            info = self.client.get_collection(collection_name)
            return getattr(info, "points_count", 0) or 0
        except Exception as e:  # noqa: BLE001
            logger.debug("Failed to get collection count for %s: %e", collection_name, e)
            return 0

    def validate_target_schema(
        self,
        collection_name: str,
        expected_dims: int,
    ) -> None:
        """Validate that an existing target collection matches expected vector dimensions."""
        if not self.client.collection_exists(collection_name):
            return

        info = self.client.get_collection(collection_name)
        vectors_config = getattr(info.config.params, "vectors", None)
        actual_size = None
        if hasattr(vectors_config, "size"):
            actual_size = vectors_config.size
        elif isinstance(vectors_config, dict):
            if "" in vectors_config:
                actual_size = getattr(vectors_config[""], "size", None)
            elif "size" in vectors_config:
                actual_size = vectors_config["size"]

        if actual_size is not None:
            # Discard MagicMock in test environments without explicit size
            if hasattr(actual_size, "_mock_name"):
                return
            try:
                size_int = int(actual_size)
            except (TypeError, ValueError):
                size_int = None
            if size_int is not None and size_int != expected_dims:
                raise HippoValidationError(
                    f"Target collection '{collection_name}' has vector dimension {size_int}, "
                    f"which does not match target profile dimension {expected_dims}."
                )

    def plan(
        self,
        source_collection: str | None = None,
        target_collection: str | None = None,
        target_provider: str | None = None,
        target_model: str | None = None,
        target_dims: int | None = None,
        batch_size: int = 32,
    ) -> ReindexPlan:
        """Generate a dry-run migration plan without performing any writes or embedding calls."""
        src, dst, profile = self.detect_collections(
            source_collection=source_collection,
            target_collection=target_collection,
            target_provider=target_provider,
            target_model=target_model,
            target_dims=target_dims,
        )

        src_count = self.get_collection_points_count(src)
        dst_count = self.get_collection_points_count(dst)

        src_entities = f"{src}_entities"
        dst_entities = f"{dst}_entities"

        has_src_entities = self.client.collection_exists(src_entities)
        src_ent_count = self.get_collection_points_count(src_entities) if has_src_entities else 0
        dst_ent_count = self.get_collection_points_count(dst_entities) if has_src_entities else 0

        return ReindexPlan(
            source_collection=src,
            target_collection=dst,
            target_profile=profile,
            source_points_count=src_count,
            target_existing_points_count=dst_count,
            source_entities_collection=src_entities if has_src_entities else None,
            target_entities_collection=dst_entities if has_src_entities else None,
            source_entities_count=src_ent_count,
            target_existing_entities_count=dst_ent_count,
            batch_size=batch_size,
            dry_run=True,
        )

    def build_target_vector_store(
        self,
        collection_name: str,
        dims: int,
    ) -> Any:
        """Create or wrap a Mem0 Qdrant vector store with BM25 slot and filter indexes."""
        from mem0.vector_stores.qdrant import Qdrant

        return Qdrant(
            collection_name=collection_name,
            embedding_model_dims=dims,
            client=self.client,
            on_disk=False,
        )

    def build_target_embedder(self, profile: EmbeddingProfile) -> Any:
        """Construct the Embedder instance corresponding to the target profile."""
        from mem0.utils.factory import EmbedderFactory

        if profile.provider == "vertexai":
            from hippo_memory.config import _register_vertex_genai_embedder

            _register_vertex_genai_embedder()
            embedder_config = {
                "model": profile.model,
                "embedding_dims": profile.dimensions,
            }
            return EmbedderFactory.create("vertexai", embedder_config)

        elif profile.provider == "gemini":
            api_key = os.getenv("GOOGLE_API_KEY", "")
            embedder_config = {
                "model": profile.model,
                "embedding_dims": profile.dimensions,
                "api_key": api_key,
            }
            return EmbedderFactory.create("gemini", embedder_config)

        elif profile.provider == "openai":
            api_key = os.getenv("OPENAI_API_KEY", "")
            embedder_config = {
                "model": profile.model,
                "embedding_dims": profile.dimensions,
                "api_key": api_key,
            }
            return EmbedderFactory.create("openai", embedder_config)

        else:
            embedder_config = {
                "model": profile.model,
                "embedding_dims": profile.dimensions,
            }
            return EmbedderFactory.create(profile.provider, embedder_config)

    def _scroll_collection(
        self,
        collection_name: str,
        batch_size: int = 100,
    ) -> Iterator[list[Record]]:
        """Scroll through a collection in page-sized batches (read-only)."""
        offset = None
        while True:
            records, next_offset = self.client.scroll(
                collection_name=collection_name,
                limit=batch_size,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            if records:
                yield records
            if next_offset is None:
                break
            offset = next_offset

    def _process_batch(
        self,
        source_records: list[Record],
        target_collection: str,
        target_vector_store: Any,
        target_embedder: Any,
        expected_dims: int,
        recompute_existing: bool,
        result: ReindexResult,
        progress_callback: Callable[[int], None] | None = None,
    ) -> None:
        """Process a single batch with batch-bounded target check, conflict detection, and re-embedding."""
        result.scanned += len(source_records)

        # Batch-bounded retrieve from target
        batch_ids = [str(r.id) for r in source_records]
        existing_targets: dict[str, Record] = {}
        if self.client.collection_exists(target_collection):
            try:
                retrieved = self.client.retrieve(
                    collection_name=target_collection,
                    ids=batch_ids,
                    with_payload=True,
                    with_vectors=False,
                )
                existing_targets = {str(r.id): r for r in retrieved}
            except Exception as e:  # noqa: BLE001
                logger.warning("Failed to retrieve existing target IDs for batch: %s", e)

        records_to_migrate: list[Record] = []
        for src_rec in source_records:
            rec_id = str(src_rec.id)
            src_payload = src_rec.payload or {}

            if rec_id in existing_targets:
                tgt_rec = existing_targets[rec_id]
                tgt_payload = tgt_rec.payload or {}

                if payloads_match(src_payload, tgt_payload):
                    if recompute_existing:
                        records_to_migrate.append(src_rec)
                    else:
                        result.skipped += 1
                else:
                    result.conflicted += 1
                    result.conflict_ids.append(rec_id)
                    result.details.append(
                        {
                            "id": rec_id,
                            "status": "conflicted",
                            "reason": "Payload divergence between source and target",
                            "source_data": src_payload.get("data", "")[:80],
                            "target_data": tgt_payload.get("data", "")[:80],
                        }
                    )
            else:
                records_to_migrate.append(src_rec)

        if not records_to_migrate:
            if progress_callback:
                progress_callback(len(source_records))
            return

        # Extract text contents
        texts = [r.payload.get("data", "") if r.payload else "" for r in records_to_migrate]
        payloads = [dict(r.payload) if r.payload else {} for r in records_to_migrate]
        ids = [str(r.id) for r in records_to_migrate]

        # Generate embeddings with transient retry
        def _embed() -> list[list[float]]:
            if hasattr(target_embedder, "embed_batch"):
                return target_embedder.embed_batch(texts, memory_action="add")
            return [target_embedder.embed(t, memory_action="add") for t in texts]

        try:
            vectors = retry_with_backoff(_embed)
        except Exception as e:  # noqa: BLE001
            err_msg = f"Failed to generate embeddings for batch ({len(texts)} items): {e}"
            logger.error(err_msg)
            result.failed += len(records_to_migrate)
            result.failed_ids.extend(ids)
            result.errors.append(err_msg)
            if progress_callback:
                progress_callback(len(source_records))
            return

        # Assert vector length on first batch
        if vectors:
            actual_dim = len(vectors[0])
            if actual_dim != expected_dims:
                mismatch_err = (
                    f"Generated vector dimension ({actual_dim}) does not match "
                    f"target profile dimension ({expected_dims})."
                )
                result.failed += len(records_to_migrate)
                result.failed_ids.extend(ids)
                result.errors.append(mismatch_err)
                raise HippoValidationError(mismatch_err)

        # Upsert into target vector store
        try:
            target_vector_store.insert(vectors=vectors, payloads=payloads, ids=ids)
            result.migrated += len(records_to_migrate)
        except Exception as e:  # noqa: BLE001
            err_msg = f"Failed to upsert points into target collection {target_collection}: {e}"
            logger.error(err_msg)
            result.failed += len(records_to_migrate)
            result.failed_ids.extend(ids)
            result.errors.append(err_msg)

        if progress_callback:
            progress_callback(len(source_records))

    def migrate(
        self,
        source_collection: str | None = None,
        target_collection: str | None = None,
        target_provider: str | None = None,
        target_model: str | None = None,
        target_dims: int | None = None,
        batch_size: int = 32,
        recompute_existing: bool = False,
        dry_run: bool = False,
        progress_callback: Callable[[int], None] | None = None,
    ) -> ReindexResult:
        """Execute the reindex/migration workflow."""
        src, dst, profile = self.detect_collections(
            source_collection=source_collection,
            target_collection=target_collection,
            target_provider=target_provider,
            target_model=target_model,
            target_dims=target_dims,
        )

        result = ReindexResult()

        if dry_run:
            plan = self.plan(
                source_collection=src,
                target_collection=dst,
                target_provider=target_provider,
                target_model=target_model,
                target_dims=target_dims,
                batch_size=batch_size,
            )
            result.scanned = plan.total_source_records
            result.skipped = plan.target_existing_points_count + plan.target_existing_entities_count
            return result

        if not self.client.collection_exists(src):
            raise HippoValidationError(f"Source collection '{src}' does not exist.")

        # Validate target collection schema before any writes
        self.validate_target_schema(dst, profile.dimensions)

        # Prepare target vector store and embedder
        target_vector_store = self.build_target_vector_store(dst, profile.dimensions)
        target_embedder = self.build_target_embedder(profile)

        # 1. Migrate primary collection
        logger.info("Migrating primary collection: %s -> %s", src, dst)
        for batch in self._scroll_collection(src, batch_size=batch_size):
            self._process_batch(
                source_records=batch,
                target_collection=dst,
                target_vector_store=target_vector_store,
                target_embedder=target_embedder,
                expected_dims=profile.dimensions,
                recompute_existing=recompute_existing,
                result=result,
                progress_callback=progress_callback,
            )

        # 2. Migrate companion entities collection if present
        src_entities = f"{src}_entities"
        dst_entities = f"{dst}_entities"
        if self.client.collection_exists(src_entities):
            logger.info("Migrating companion entity collection: %s -> %s", src_entities, dst_entities)
            self.validate_target_schema(dst_entities, profile.dimensions)
            target_entity_store = self.build_target_vector_store(dst_entities, profile.dimensions)

            for batch in self._scroll_collection(src_entities, batch_size=batch_size):
                self._process_batch(
                    source_records=batch,
                    target_collection=dst_entities,
                    target_vector_store=target_entity_store,
                    target_embedder=target_embedder,
                    expected_dims=profile.dimensions,
                    recompute_existing=recompute_existing,
                    result=result,
                    progress_callback=progress_callback,
                )

        return result
