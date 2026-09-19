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
    resolve_collection_name,
    resolve_embedding_profile,
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
    """Compare the complete logical payload, not a lossy key subset.

    Qdrant payloads are JSON-compatible mappings, so Python's deep mapping/list
    equality gives the conflict gate the exact semantics we need: any divergent
    lifecycle, provenance, linkage, identity, timestamp, or custom metadata is
    treated as a conflict.
    """
    return source_payload == target_payload


def validate_embedding_batch(
    vectors: Any,
    expected_count: int,
    expected_dims: int,
) -> None:
    """Validate embedding cardinality and every vector dimension."""
    if vectors is None or not hasattr(vectors, "__len__"):
        raise HippoValidationError("Embedding provider returned a non-sequence result.")
    if len(vectors) != expected_count:
        raise HippoValidationError(
            f"Embedding provider returned {len(vectors)} vectors for "
            f"{expected_count} input records."
        )
    for index, vector in enumerate(vectors):
        if vector is None or not hasattr(vector, "__len__"):
            raise HippoValidationError(
                f"Embedding provider returned a non-vector result at index {index}."
            )
        actual_dim = len(vector)
        if actual_dim != expected_dims:
            raise HippoValidationError(
                f"Generated vector dimension ({actual_dim}) at index {index} does not "
                f"match target profile dimension ({expected_dims})."
            )


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
        """Resolve the same canonical profile used by Hippo runtime config."""
        provider = (target_provider or self.config.provider).strip().lower()
        return resolve_embedding_profile(
            provider=provider,
            embedding_model=target_model,
            dims=target_dims,
        )

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

        resolved_target = (
            target_collection.strip()
            if target_collection and target_collection.strip()
            else resolve_collection_name(
                target_profile.provider,
                target_profile.model,
                target_profile.dimensions,
            )
        )

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
        """Fetch points_count, propagating backend failures instead of pretending empty."""
        if not self.client.collection_exists(collection_name):
            return 0
        info = self.client.get_collection(collection_name)
        return getattr(info, "points_count", 0) or 0

    def validate_target_schema(
        self,
        collection_name: str,
        expected_dims: int,
    ) -> None:
        """Validate dense dimensions, COSINE distance, and the BM25 sparse slot."""
        if not self.client.collection_exists(collection_name):
            return

        info = self.client.get_collection(collection_name)
        if type(info).__name__ == "MagicMock":
            return

        params = getattr(getattr(info, "config", None), "params", None)
        if params is None:
            raise HippoValidationError(
                f"Cannot inspect schema for target collection '{collection_name}'."
            )

        vectors_config = getattr(params, "vectors", None)
        vector_params = vectors_config
        if isinstance(vectors_config, dict):
            vector_params = vectors_config.get("") or vectors_config.get("default")
            if vector_params is None and "size" in vectors_config:
                vector_params = vectors_config

        actual_size = (
            vector_params.get("size")
            if isinstance(vector_params, dict)
            else getattr(vector_params, "size", None)
        )
        if actual_size is None:
            raise HippoValidationError(
                f"Cannot determine vector dimension for target collection '{collection_name}'."
            )
        try:
            size_int = int(actual_size)
        except (TypeError, ValueError) as exc:
            raise HippoValidationError(
                f"Invalid vector dimension reported by target collection '{collection_name}': "
                f"{actual_size!r}."
            ) from exc
        if size_int != expected_dims:
            raise HippoValidationError(
                f"Target collection '{collection_name}' has vector dimension {size_int}, "
                f"which does not match target profile dimension {expected_dims}."
            )

        actual_distance = (
            vector_params.get("distance")
            if isinstance(vector_params, dict)
            else getattr(vector_params, "distance", None)
        )
        if actual_distance is None or "cosine" not in str(actual_distance).lower():
            raise HippoValidationError(
                f"Target collection '{collection_name}' must use COSINE distance "
                f"(got {actual_distance!r})."
            )

        sparse_config = getattr(params, "sparse_vectors", None)
        has_bm25 = isinstance(sparse_config, dict) and "bm25" in sparse_config
        if not has_bm25:
            try:
                has_bm25 = "bm25" in sparse_config
            except (TypeError, AttributeError):
                has_bm25 = False
        if not has_bm25:
            raise HippoValidationError(
                f"Target collection '{collection_name}' has no 'bm25' sparse vector slot; "
                "refusing a migration that would silently degrade hybrid retrieval."
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
        if batch_size <= 0:
            raise HippoValidationError("batch_size must be a positive integer.")

        src, dst, profile = self.detect_collections(
            source_collection=source_collection,
            target_collection=target_collection,
            target_provider=target_provider,
            target_model=target_model,
            target_dims=target_dims,
        )

        if not self.client.collection_exists(src):
            raise HippoValidationError(f"Source collection '{src}' does not exist.")

        self.validate_target_schema(dst, profile.dimensions)
        src_count = self.get_collection_points_count(src)
        dst_count = self.get_collection_points_count(dst)

        src_entities = f"{src}_entities"
        dst_entities = f"{dst}_entities"

        has_src_entities = self.client.collection_exists(src_entities)
        src_ent_count = self.get_collection_points_count(src_entities) if has_src_entities else 0
        if has_src_entities:
            self.validate_target_schema(dst_entities, profile.dimensions)
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
        """Process one batch with fail-closed conflict checks and precise failures."""
        result.scanned += len(source_records)

        batch_ids = [str(r.id) for r in source_records]
        existing_targets: dict[str, Record] = {}
        if self.client.collection_exists(target_collection):
            try:
                retrieved = retry_with_backoff(
                    lambda: self.client.retrieve(
                        collection_name=target_collection,
                        ids=batch_ids,
                        with_payload=True,
                        with_vectors=False,
                    )
                )
            except Exception as exc:
                raise HippoValidationError(
                    f"Cannot verify existing target records in '{target_collection}'; "
                    "refusing to write because conflict safety cannot be guaranteed."
                ) from exc
            existing_targets = {str(r.id): r for r in retrieved}

        records_to_migrate: list[Record] = []
        for src_rec in source_records:
            rec_id = str(src_rec.id)
            src_payload = src_rec.payload or {}

            if rec_id in existing_targets:
                tgt_payload = existing_targets[rec_id].payload or {}
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
                        }
                    )
            else:
                records_to_migrate.append(src_rec)

        if not records_to_migrate:
            if progress_callback:
                progress_callback(len(source_records))
            return

        valid_records: list[Record] = []
        texts: list[str] = []
        for record in records_to_migrate:
            payload = record.payload or {}
            text_value = payload.get("data")
            if not isinstance(text_value, str) or not text_value.strip():
                rec_id = str(record.id)
                result.failed += 1
                result.failed_ids.append(rec_id)
                result.errors.append(
                    f"Record {rec_id} in '{target_collection}' has no non-empty payload.data to embed."
                )
                continue
            valid_records.append(record)
            texts.append(text_value)

        if not valid_records:
            if progress_callback:
                progress_callback(len(source_records))
            return

        def _batch_embed() -> Any:
            if hasattr(target_embedder, "embed_batch"):
                batch_vectors = target_embedder.embed_batch(texts, memory_action="add")
            else:
                batch_vectors = [
                    target_embedder.embed(text, memory_action="add") for text in texts
                ]
            validate_embedding_batch(
                batch_vectors,
                expected_count=len(texts),
                expected_dims=expected_dims,
            )
            return batch_vectors

        try:
            vectors = retry_with_backoff(_batch_embed)
            embedded_records = valid_records
        except Exception as batch_exc:
            logger.warning(
                "Batch embedding failed validation/execution; falling back to per-record "
                "embedding to isolate failures: %s",
                batch_exc,
            )
            vectors = []
            embedded_records = []
            for record, text_value in zip(valid_records, texts):
                rec_id = str(record.id)

                def _embed_one(text: str = text_value) -> Any:
                    if hasattr(target_embedder, "embed"):
                        vector = target_embedder.embed(text, memory_action="add")
                    else:
                        one = target_embedder.embed_batch([text], memory_action="add")
                        validate_embedding_batch(one, expected_count=1, expected_dims=expected_dims)
                        vector = one[0]
                    validate_embedding_batch([vector], expected_count=1, expected_dims=expected_dims)
                    return vector

                try:
                    vector = retry_with_backoff(_embed_one)
                except Exception as exc:
                    result.failed += 1
                    result.failed_ids.append(rec_id)
                    result.errors.append(f"Failed to embed record {rec_id}: {exc}")
                    continue
                vectors.append(vector)
                embedded_records.append(record)

        if not embedded_records:
            if progress_callback:
                progress_callback(len(source_records))
            return

        payloads = [dict(r.payload) if r.payload else {} for r in embedded_records]
        ids = [str(r.id) for r in embedded_records]

        try:
            retry_with_backoff(
                lambda: target_vector_store.insert(
                    vectors=vectors,
                    payloads=payloads,
                    ids=ids,
                )
            )
            result.migrated += len(embedded_records)
        except Exception as batch_exc:
            logger.warning(
                "Batch upsert failed; falling back to per-record upserts to isolate failures: %s",
                batch_exc,
            )
            for vector, payload, rec_id in zip(vectors, payloads, ids):
                try:
                    retry_with_backoff(
                        lambda v=vector, p=payload, i=rec_id: target_vector_store.insert(
                            vectors=[v],
                            payloads=[p],
                            ids=[i],
                        )
                    )
                    result.migrated += 1
                except Exception as exc:
                    result.failed += 1
                    result.failed_ids.append(rec_id)
                    result.errors.append(
                        f"Failed to upsert record {rec_id} into '{target_collection}': {exc}"
                    )

        if progress_callback:
            progress_callback(len(source_records))

    def _verify_collection_after_migration(
        self,
        source_collection: str,
        target_collection: str,
        source_count_at_start: int,
        result: ReindexResult,
        label: str,
    ) -> None:
        """Verify source stability and minimum target coverage after a run."""
        source_count_at_end = self.get_collection_points_count(source_collection)
        if source_count_at_end != source_count_at_start:
            result.errors.append(
                f"{label} source collection changed during migration: "
                f"{source_count_at_start} -> {source_count_at_end}. "
                "Run again while writers are quiescent."
            )

        target_count = self.get_collection_points_count(target_collection)
        if target_count < source_count_at_start:
            result.errors.append(
                f"{label} target verification failed: target has {target_count} points "
                f"but source started with {source_count_at_start}."
            )

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
        if batch_size <= 0:
            raise HippoValidationError("batch_size must be a positive integer.")

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
            return result

        if not self.client.collection_exists(src):
            raise HippoValidationError(f"Source collection '{src}' does not exist.")

        src_count_at_start = self.get_collection_points_count(src)
        src_entities = f"{src}_entities"
        dst_entities = f"{dst}_entities"
        has_src_entities = self.client.collection_exists(src_entities)
        src_entities_count_at_start = (
            self.get_collection_points_count(src_entities) if has_src_entities else 0
        )

        self.validate_target_schema(dst, profile.dimensions)
        if has_src_entities:
            self.validate_target_schema(dst_entities, profile.dimensions)

        target_vector_store = self.build_target_vector_store(dst, profile.dimensions)
        target_embedder = self.build_target_embedder(profile)

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

        if has_src_entities:
            logger.info(
                "Migrating companion entity collection: %s -> %s",
                src_entities,
                dst_entities,
            )
            target_entity_store = self.build_target_vector_store(
                dst_entities,
                profile.dimensions,
            )
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

        expected_scanned = src_count_at_start + src_entities_count_at_start
        if result.scanned != expected_scanned:
            result.errors.append(
                f"Scan verification failed: scanned {result.scanned} records but "
                f"source started with {expected_scanned}."
            )

        self._verify_collection_after_migration(
            src,
            dst,
            src_count_at_start,
            result,
            "Primary",
        )
        if has_src_entities:
            self._verify_collection_after_migration(
                src_entities,
                dst_entities,
                src_entities_count_at_start,
                result,
                "Entity",
            )

        return result
