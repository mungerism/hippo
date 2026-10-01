"""Instance-scoped Mem0 persistence adaptation layer (Issue #80, Decision 6).

Removes global class-level monkey-patching of Memory.entity_store and binds
all persistence safeguards, write-lock coordination, observed-version revalidation,
and quality auditing to an individual HippoEngine instance.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set, Tuple

from hippo_memory.persistence_quality import audit_distilled_memory
from hippo_memory.apply import record_version

if TYPE_CHECKING:
    from hippo_memory.engine import HippoEngine

logger = logging.getLogger("hippo_memory.engine")


def _extract_insert_args(
    args: tuple[Any, ...], kwargs: dict[str, Any]
) -> tuple[Optional[list], Optional[list], Optional[list]]:
    """Extract (vectors, payloads, ids) matching Mem0's official insert signature:
    insert(vectors, payloads=None, ids=None)
    """
    vecs = kwargs.get("vectors") if "vectors" in kwargs else (args[0] if len(args) > 0 else None)
    pays = kwargs.get("payloads") if "payloads" in kwargs else (args[1] if len(args) > 1 else None)
    i_ids = kwargs.get("ids") if "ids" in kwargs else (args[2] if len(args) > 2 else None)
    return vecs, pays, i_ids


def _rebuild_insert_call(
    orig_args: tuple[Any, ...],
    orig_kwargs: dict[str, Any],
    vecs: Any,
    pays: Any,
    ids: Any,
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    """Rebuild (args, kwargs) preserving the caller's convention (positional vs keyword)."""
    new_kwargs = dict(orig_kwargs)
    new_args = list(orig_args)

    if "vectors" in new_kwargs:
        new_kwargs["vectors"] = vecs
    elif len(new_args) > 0:
        new_args[0] = vecs

    if "payloads" in new_kwargs:
        new_kwargs["payloads"] = pays
    elif len(new_args) > 1:
        new_args[1] = pays

    if "ids" in new_kwargs:
        new_kwargs["ids"] = ids
    elif len(new_args) > 2:
        new_args[2] = ids

    return tuple(new_args), new_kwargs


def _extract_update_args(
    args: tuple[Any, ...], kwargs: dict[str, Any]
) -> tuple[Optional[str], Optional[dict[str, Any]]]:
    """Extract (vector_id, payload) matching Mem0's official update signature:
    update(vector_id, vector=None, payload=None)
    """
    v_id = kwargs.get("vector_id") if "vector_id" in kwargs else (args[0] if len(args) > 0 else None)
    if "payload" in kwargs:
        payload = kwargs.get("payload")
    elif len(args) > 2:
        payload = args[2]
    elif len(args) == 2 and isinstance(args[1], Mapping):
        payload = args[1]
    else:
        payload = None
    return (str(v_id) if v_id is not None else None), payload


def _rebuild_update_call(
    orig_args: tuple[Any, ...],
    orig_kwargs: dict[str, Any],
    payload: Any,
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    """Rebuild (args, kwargs) preserving the caller's convention (positional vs keyword)."""
    new_kwargs = dict(orig_kwargs)
    new_args = list(orig_args)

    if "payload" in new_kwargs:
        new_kwargs["payload"] = payload
    elif len(new_args) > 2:
        new_args[2] = payload
    elif len(new_args) == 2 and isinstance(new_args[1], Mapping):
        new_args[1] = payload
    else:
        new_kwargs["payload"] = payload

    return tuple(new_args), new_kwargs


class Mem0PersistenceAdapter:
    """Instance-scoped persistence adapter for a Mem0 Memory instance."""

    def __init__(self, engine: Any, mem: Any) -> None:
        self.engine = engine
        self.mem = mem
        self.wrapped_ids: Set[int] = set()
        self._attached: bool = False

    def _get_effective_holder(self) -> Optional[Any]:
        """Holder ownership guard: only return holder if it belongs to this engine."""
        from hippo_memory.engine import _current_write_lock_holder
        holder = _current_write_lock_holder.get()
        if holder is not None and getattr(holder, "engine", None) is self.engine:
            return holder
        return None

    def attach(self) -> None:
        """Attach instance-scoped dynamic subclass and persistence safeguards to mem."""
        if self.mem is None:
            return

        # Fail-closed if already bound to another engine's adapter (use __dict__ to avoid Mock auto-generation)
        existing_adapter = getattr(self.mem, "__dict__", {}).get("_hippo_adapter")
        if existing_adapter is not None:
            if existing_adapter.engine is not self.engine:
                raise RuntimeError(
                    f"Memory instance {self.mem} is already attached to another HippoEngine adapter!"
                )
            return

        self.mem._hippo_adapter = self

        mem_type = type(self.mem)
        # Check if already a dynamic subclass for this instance (skip subclassing MagicMock)
        if not getattr(mem_type, "_is_hippo_instance_subclass", False) and mem_type.__name__ != "MagicMock":
            orig_entity_store_prop = getattr(mem_type, "entity_store", None)

            # Create an instance-specific subclass
            class HippoInstanceMemory(mem_type):
                _is_hippo_instance_subclass = True

                @property
                def entity_store(self):
                    adapter = getattr(self, "__dict__", {}).get("_hippo_adapter")
                    if adapter is not None:
                        return adapter.get_entity_store(self)
                    if orig_entity_store_prop is not None:
                        return orig_entity_store_prop.fget(self)
                    return getattr(self, "_entity_store", None)

            HippoInstanceMemory.__name__ = f"HippoInstanceMemory_{id(self.mem)}"
            HippoInstanceMemory.__qualname__ = f"HippoInstanceMemory_{id(self.mem)}"
            try:
                self.mem.__class__ = HippoInstanceMemory
            except TypeError:
                # Some types (e.g. SimpleNamespace or C extensions) do not support __class__ assignment
                pass

        # Hook primary vector store
        self._hook_vector_store()
        # Hook history database
        self._hook_history_db()

        # If entity_store is explicitly present in __dict__ (e.g. mocks or direct assignment), hook it
        if "entity_store" in getattr(self.mem, "__dict__", {}) and self.mem.__dict__["entity_store"] is not None:
            self.hook_entity_store(self.mem.__dict__["entity_store"])
        else:
            es_inst = getattr(self.mem, "_entity_store", None)
            if es_inst is not None and type(self.mem).__name__ != "MagicMock":
                self.hook_entity_store(es_inst)

        # Hook mem-level entity helper methods
        self._hook_mem_helpers()

        self._attached = True

    def get_entity_store(self, instance: Any) -> Any:
        """Lazy entity_store accessor called from dynamic subclass property."""
        if "entity_store" in getattr(instance, "__dict__", {}) and instance.__dict__["entity_store"] is not None:
            res_es = instance.__dict__["entity_store"]
            self.hook_entity_store(res_es)
            return res_es

        # Find the original property from base class
        orig_prop = None
        for base in type(instance).__mro__[1:]:
            if hasattr(base, "entity_store"):
                candidate = getattr(base, "entity_store")
                if isinstance(candidate, property):
                    orig_prop = candidate
                    break

        if orig_prop is not None:
            res_es = orig_prop.fget(instance)
        else:
            res_es = getattr(instance, "_entity_store", None)

        if res_es is not None:
            self.hook_entity_store(res_es)
        return res_es

    def _hook_vector_store(self) -> None:
        from hippo_memory.engine import _dedup_before_insert, _is_stale_mutation

        vector_store = getattr(self.mem, "vector_store", None)
        if vector_store is None or id(vector_store) in self.wrapped_ids:
            return
        self.wrapped_ids.add(id(vector_store))

        orig_insert = getattr(vector_store, "insert", None)
        if orig_insert is not None:
            def _locked_insert(*args: Any, **kwargs: Any) -> Any:
                holder = self._get_effective_holder()
                v_new: Optional[list[Any]] = None
                id_new: Optional[list[str]] = None
                p_new: Optional[list[dict[str, Any]]] = None
                i_ids: Optional[list[str]] = None
                pays: Optional[list[dict[str, Any]]] = None
                vecs: Optional[list[Any]] = None
                if holder is not None:
                    holder.vector_store = vector_store
                    holder.ensure_locked()
                    vecs, pays, i_ids = _extract_insert_args(args, kwargs)
                    if holder.dedup_enabled:
                        if holder.check_observed_context_stale(vector_store):
                            logger.warning(
                                "Warm Path insert aborted for user=%s agent=%s: "
                                "observed retrieval context was mutated concurrently during LLM extraction",
                                holder.user_id,
                                holder.agent_id,
                            )
                            if i_ids:
                                for s_id in i_ids:
                                    holder.skipped_ids.add(str(s_id))
                            holder.context_aborted = True
                            holder.primary_completed = True
                            holder.release_if_locked()
                            return []

                        if pays:
                            v_new, id_new, p_new = _dedup_before_insert(
                                vector_store, holder.user_id, holder.agent_id, vecs, i_ids, pays, holder=holder
                            )
                            if not p_new:
                                holder.primary_completed = True
                                holder.release_if_locked()
                                return []
                            args, kwargs = _rebuild_insert_call(args, kwargs, v_new, p_new, id_new)

                    cand_pays = p_new if p_new is not None else pays
                    cand_ids = id_new if id_new is not None else i_ids
                    cand_vecs = v_new if v_new is not None else vecs
                    if holder.persistence_quality_enabled and cand_pays:
                        v_accepted = []
                        id_accepted = []
                        p_accepted = []
                        for idx, p in enumerate(cand_pays):
                            cur_id = cand_ids[idx] if cand_ids is not None and idx < len(cand_ids) else None
                            cur_vec = cand_vecs[idx] if cand_vecs is not None and idx < len(cand_vecs) else None
                            cur_text = ""
                            if isinstance(p, Mapping):
                                cur_text = p.get("data") or p.get("text") or ""
                            meta = p.get("metadata") if isinstance(p, Mapping) else None

                            audit_res = audit_distilled_memory(
                                str(cur_text),
                                metadata=meta if isinstance(meta, Mapping) else p,
                            )
                            if audit_res.accepted:
                                p_accepted.append(p)
                                if cand_vecs is not None:
                                    v_accepted.append(cur_vec)
                                if cand_ids is not None:
                                    id_accepted.append(cur_id)
                            else:
                                logger.info(
                                    "Warm Path persistence quality gate dropped insert candidate "
                                    "(reason=%s, id=%s, text_len=%d)",
                                    audit_res.reason,
                                    cur_id,
                                    len(cur_text),
                                )
                                if cur_id:
                                    holder.skipped_ids.add(str(cur_id))

                        v_new = v_accepted if cand_vecs is not None else None
                        id_new = id_accepted if cand_ids is not None else None
                        p_new = p_accepted
                        if not p_new:
                            holder.primary_completed = True
                            holder.release_if_locked()
                            return []
                        args, kwargs = _rebuild_insert_call(args, kwargs, v_new, p_new, id_new)
                res = orig_insert(*args, **kwargs)
                if holder is not None:
                    actual_ids = id_new if id_new is not None else i_ids
                    actual_pays = p_new if p_new is not None else pays
                    if actual_ids and actual_pays:
                        for mid, p in zip(actual_ids, actual_pays):
                            p_dict = p if isinstance(p, Mapping) else getattr(p, "payload", {})
                            holder.committed_primary_versions[str(mid)] = record_version(p_dict)
                    holder.primary_completed = True
                    if actual_ids:
                        holder.primary_committed = True
                    if not getattr(self.mem, "db", None):
                        holder.release_if_locked()
                return res

            vector_store.insert = _locked_insert

        orig_update = getattr(vector_store, "update", None)
        if orig_update is not None:
            def _locked_update(*args: Any, **kwargs: Any) -> Any:
                holder = self._get_effective_holder()
                if holder is not None:
                    holder.vector_store = vector_store
                    holder.ensure_locked()
                    if holder.dedup_enabled:
                        v_id, _ = _extract_update_args(args, kwargs)
                        if (
                            holder.check_observed_context_stale(vector_store)
                            or (v_id and _is_stale_mutation(vector_store, str(v_id), holder))
                        ):
                            if v_id:
                                holder.skipped_ids.add(str(v_id))
                            holder.context_aborted = True
                            holder.primary_completed = True
                            holder.entity_linking_aborted = True
                            holder.release_if_locked()
                            return None

                    if holder.persistence_quality_enabled:
                        v_id, p = _extract_update_args(args, kwargs)
                        cur_text = ""
                        if isinstance(p, Mapping):
                            cur_text = p.get("data") or p.get("text") or ""
                        meta = p.get("metadata") if isinstance(p, Mapping) else None
                        audit_res = audit_distilled_memory(
                            str(cur_text),
                            metadata=meta if isinstance(meta, Mapping) else p,
                        )
                        if not audit_res.accepted:
                            logger.info(
                                "Warm Path persistence quality gate dropped update candidate "
                                "(reason=%s, id=%s, text_len=%d)",
                                audit_res.reason,
                                v_id,
                                len(cur_text),
                            )
                            if v_id:
                                holder.skipped_ids.add(str(v_id))
                            holder.primary_completed = True
                            holder.entity_linking_aborted = True
                            holder.release_if_locked()
                            return None
                res = orig_update(*args, **kwargs)
                if holder is not None:
                    v_id, p = _extract_update_args(args, kwargs)
                    if v_id and p is not None:
                        p_dict = p if isinstance(p, Mapping) else getattr(p, "payload", {})
                        holder.committed_primary_versions[str(v_id)] = record_version(p_dict)
                    holder.primary_completed = True
                    holder.primary_committed = True
                    if not getattr(self.mem, "db", None):
                        holder.release_if_locked()
                return res

            vector_store.update = _locked_update

        orig_delete = getattr(vector_store, "delete", None)
        if orig_delete is not None:
            def _locked_delete(*args: Any, **kwargs: Any) -> Any:
                holder = self._get_effective_holder()
                if holder is not None:
                    holder.vector_store = vector_store
                    holder.ensure_locked()
                    if holder.dedup_enabled:
                        v_id = kwargs.get("vector_id") if "vector_id" in kwargs else (args[0] if len(args) > 0 else None)
                        if (
                            holder.check_observed_context_stale(vector_store)
                            or (v_id and _is_stale_mutation(vector_store, str(v_id), holder))
                        ):
                            if v_id:
                                holder.skipped_ids.add(str(v_id))
                            holder.context_aborted = True
                            holder.primary_completed = True
                            holder.entity_linking_aborted = True
                            holder.release_if_locked()
                            return None
                res = orig_delete(*args, **kwargs)
                if holder is not None:
                    holder.primary_completed = True
                    holder.primary_committed = True
                    if not getattr(self.mem, "db", None):
                        holder.release_if_locked()
                return res

            vector_store.delete = _locked_delete

        orig_search = getattr(vector_store, "search", None)
        if orig_search is not None and getattr(orig_search, "_hippo_wrapped", False) is not True:
            def _tracked_search(*args: Any, **kwargs: Any) -> Any:
                res = orig_search(*args, **kwargs)
                holder = self._get_effective_holder()
                if holder is not None and res:
                    items = res if isinstance(res, list) else getattr(res, "results", [])
                    for item in items:
                        mid = getattr(item, "id", None) or (item.get("id") if isinstance(item, dict) else None)
                        payload = getattr(item, "payload", None)
                        if not isinstance(payload, Mapping):
                            payload = item if isinstance(item, Mapping) else {}
                        if mid:
                            holder.observed_versions[str(mid)] = record_version(payload)
                return res

            _tracked_search._hippo_wrapped = True
            vector_store.search = _tracked_search

    def _hook_history_db(self) -> None:
        db = getattr(self.mem, "db", None)
        if db is None or id(db) in self.wrapped_ids:
            return
        self.wrapped_ids.add(id(db))

        orig_batch_add = getattr(db, "batch_add_history", None)
        if orig_batch_add is not None:
            def _locked_batch_add(*args: Any, **kwargs: Any) -> Any:
                holder = self._get_effective_holder()
                if holder is not None:
                    holder.ensure_locked()
                    if holder.skipped_ids:
                        records = kwargs.get("records") if "records" in kwargs else (args[0] if len(args) > 0 else None)
                        if records and isinstance(records, list):
                            filtered_records = [
                                rec for rec in records
                                if not (isinstance(rec, dict) and str(rec.get("memory_id")) in holder.skipped_ids)
                            ]
                            if not filtered_records:
                                holder.primary_completed = True
                                holder.release_if_locked()
                                return None
                            if "records" in kwargs:
                                kwargs["records"] = filtered_records
                            elif len(args) > 0:
                                args = (filtered_records,) + args[1:]
                res = orig_batch_add(*args, **kwargs)
                if holder is not None:
                    holder.primary_completed = True
                    holder.primary_committed = True
                    holder.release_if_locked()
                return res

            db.batch_add_history = _locked_batch_add

        orig_add_history = getattr(db, "add_history", None)
        if orig_add_history is not None:
            def _locked_add_history(*args: Any, **kwargs: Any) -> Any:
                holder = self._get_effective_holder()
                if holder is not None:
                    holder.ensure_locked()
                    m_id = kwargs.get("memory_id") if "memory_id" in kwargs else (args[0] if len(args) > 0 else None)
                    if m_id and str(m_id) in holder.skipped_ids:
                        holder.primary_completed = True
                        holder.release_if_locked()
                        return None
                res = orig_add_history(*args, **kwargs)
                if holder is not None:
                    holder.primary_completed = True
                    holder.primary_committed = True
                    holder.release_if_locked()
                return res

            db.add_history = _locked_add_history

    def hook_entity_store(self, es: Any) -> None:
        """Hook lazy entity store methods with write lock re-acquisition and link filtering."""
        if es is None or id(es) in self.wrapped_ids:
            return
        self.wrapped_ids.add(id(es))
        vector_store = getattr(self.mem, "vector_store", None)

        orig_es_search_batch = getattr(es, "search_batch", None)
        if orig_es_search_batch is not None:
            def _locked_es_search_batch(*args: Any, **kwargs: Any) -> Any:
                holder = self._get_effective_holder()
                if holder is not None and not holder.ensure_entity_locked(vector_store):
                    return []
                return orig_es_search_batch(*args, **kwargs)

            es.search_batch = _locked_es_search_batch

        orig_es_search = getattr(es, "search", None)
        if orig_es_search is not None:
            def _locked_es_search(*args: Any, **kwargs: Any) -> Any:
                holder = self._get_effective_holder()
                if holder is not None and not holder.ensure_entity_locked(vector_store):
                    return []
                return orig_es_search(*args, **kwargs)

            es.search = _locked_es_search

        orig_es_update = getattr(es, "update", None)
        if orig_es_update is not None:
            def _locked_es_update(*args: Any, **kwargs: Any) -> Any:
                holder = self._get_effective_holder()
                if holder is not None:
                    if not holder.ensure_entity_locked(vector_store):
                        return None
                    if holder.error is not None:
                        return None
                    pruned_ids = holder.all_entity_skipped_ids
                    if pruned_ids:
                        v_id, payload = _extract_update_args(args, kwargs)
                        if isinstance(payload, dict) and "linked_memory_ids" in payload:
                            existing_links: set[str] = set()
                            if v_id and hasattr(es, "get"):
                                try:
                                    old_rec = es.get(vector_id=v_id)
                                    if old_rec:
                                        old_payload = getattr(old_rec, "payload", None)
                                        if type(old_rec).__name__ != "MagicMock" or type(old_payload).__name__ != "MagicMock":
                                            if not isinstance(old_payload, Mapping):
                                                old_payload = old_rec if isinstance(old_rec, Mapping) else {}
                                            if type(old_payload).__name__ != "MagicMock":
                                                existing_links = {
                                                    str(x) for x in old_payload.get("linked_memory_ids", [])
                                                    if str(x) not in holder.skipped_ids
                                                }
                                except Exception:
                                    existing_links = set()

                            filtered_links = []
                            for mid in payload["linked_memory_ids"]:
                                s_mid = str(mid)
                                if s_mid in holder.skipped_ids:
                                    continue
                                if s_mid in holder.entity_pruned_memory_ids:
                                    if s_mid in existing_links:
                                        filtered_links.append(mid)
                                    continue
                                filtered_links.append(mid)

                            payload["linked_memory_ids"] = filtered_links
                            if existing_links and set(str(x) for x in filtered_links) == existing_links:
                                return None
                            args, kwargs = _rebuild_update_call(args, kwargs, payload)
                return orig_es_update(*args, **kwargs)

            es.update = _locked_es_update

        orig_es_insert = getattr(es, "insert", None)
        if orig_es_insert is not None:
            def _locked_es_insert(*args: Any, **kwargs: Any) -> Any:
                holder = self._get_effective_holder()
                if holder is not None:
                    if not holder.ensure_entity_locked(vector_store):
                        return None
                    if holder.error is not None:
                        return None
                    pruned_ids = holder.all_entity_skipped_ids
                    if pruned_ids:
                        vecs, pays, ids_list = _extract_insert_args(args, kwargs)
                        if isinstance(pays, list):
                            filtered_pays = []
                            filtered_vecs = []
                            filtered_ids = []
                            for i, p in enumerate(pays):
                                if isinstance(p, dict) and "linked_memory_ids" in p:
                                    p["linked_memory_ids"] = [
                                        mid for mid in p["linked_memory_ids"]
                                        if str(mid) not in pruned_ids
                                    ]
                                    if not p["linked_memory_ids"]:
                                        continue
                                filtered_pays.append(p)
                                if vecs is not None and i < len(vecs):
                                    filtered_vecs.append(vecs[i])
                                if ids_list is not None and i < len(ids_list):
                                    filtered_ids.append(ids_list[i])

                            if not filtered_pays:
                                return None

                            args, kwargs = _rebuild_insert_call(
                                args,
                                kwargs,
                                filtered_vecs if vecs is not None else None,
                                filtered_pays,
                                filtered_ids if ids_list is not None else None,
                            )
                return orig_es_insert(*args, **kwargs)

            es.insert = _locked_es_insert

        orig_es_delete = getattr(es, "delete", None)
        if orig_es_delete is not None:
            def _locked_es_delete(*args: Any, **kwargs: Any) -> Any:
                holder = self._get_effective_holder()
                if holder is not None:
                    if not holder.ensure_entity_locked(vector_store):
                        return None
                    if holder.error is not None:
                        return None
                    pruned_ids = holder.all_entity_skipped_ids
                    if pruned_ids:
                        v_id = kwargs.get("vector_id") if "vector_id" in kwargs else (args[0] if len(args) > 0 else None)
                        if v_id and str(v_id) in pruned_ids:
                            return None
                return orig_es_delete(*args, **kwargs)

            es.delete = _locked_es_delete

    def _hook_mem_helpers(self) -> None:
        vector_store = getattr(self.mem, "vector_store", None)

        orig_existing_by_text = getattr(self.mem, "_existing_entities_by_text", None)
        if orig_existing_by_text is not None and getattr(orig_existing_by_text, "_hippo_wrapped", False) is not True:
            def _locked_existing_by_text(*args: Any, **kwargs: Any) -> Any:
                holder = self._get_effective_holder()
                if holder is not None and not holder.ensure_entity_locked(vector_store):
                    return {}
                return orig_existing_by_text(*args, **kwargs)

            _locked_existing_by_text._hippo_wrapped = True
            self.mem._existing_entities_by_text = _locked_existing_by_text

        orig_remove_es = getattr(self.mem, "_remove_memory_from_entity_store", None)
        if orig_remove_es is not None and getattr(orig_remove_es, "_hippo_wrapped", False) is not True:
            def _locked_remove_from_entity_store(memory_id: Any, *args: Any, **kwargs: Any) -> Any:
                holder = self._get_effective_holder()
                if holder is not None:
                    if not holder.ensure_entity_locked(vector_store):
                        return None
                    if holder.error is not None:
                        return None
                    if str(memory_id) in holder.all_entity_skipped_ids:
                        return None
                return orig_remove_es(memory_id, *args, **kwargs)

            _locked_remove_from_entity_store._hippo_wrapped = True
            self.mem._remove_memory_from_entity_store = _locked_remove_from_entity_store

        orig_link_es = getattr(self.mem, "_link_entities_for_memory", None)
        if orig_link_es is not None and getattr(orig_link_es, "_hippo_wrapped", False) is not True:
            def _locked_link_entities_for_memory(memory_id: Any, *args: Any, **kwargs: Any) -> Any:
                holder = self._get_effective_holder()
                if holder is not None:
                    if not holder.ensure_entity_locked(vector_store):
                        return None
                    if holder.error is not None:
                        return None
                    if str(memory_id) in holder.all_entity_skipped_ids:
                        return None
                return orig_link_es(memory_id, *args, **kwargs)

            _locked_link_entities_for_memory._hippo_wrapped = True
            self.mem._link_entities_for_memory = _locked_link_entities_for_memory
