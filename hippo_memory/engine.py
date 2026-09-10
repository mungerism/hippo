import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from mem0 import Memory
from hippo_memory.config import HippoConfig
from hippo_memory.exceptions import HippoValidationError
from hippo_memory.router import ScopeRouter

logger = logging.getLogger(__name__)


def build_conversation(
    text: Optional[str],
    content: Optional[str],
    messages: Optional[List[Dict[str, str]]],
) -> List[Dict[str, str]]:
    """Build the Mem0 `add()` conversation from text/content and messages.

    - 仅 messages：原样使用。
    - 仅 text/content：作为单条 user 消息。
    - 两者都传：messages 保留为上下文，显式 text 作为 assistant 角色的补充
      事实追加到末尾（对齐 Mem0 官方插件以 assistant 角色传递摘要的模式），
      绝不静默丢弃任何一方。
    """
    raw_text = text if text is not None else (content or "")
    if messages:
        conversation = list(messages)
        if raw_text.strip():
            conversation = conversation + [{"role": "assistant", "content": raw_text}]
        return conversation
    if raw_text.strip():
        return [{"role": "user", "content": raw_text}]
    raise ValueError("text/content 与 messages 至少需要提供一个")


class HippoEngine:
    """Core memory engine wrapping Mem0 with multi-scope and multi-provider support."""

    def __init__(self, config: Optional[HippoConfig] = None):
        self.config = config or HippoConfig()
        self.router = ScopeRouter(default_user_id=self.config.user_id)
        self._memory: Optional[Memory] = None

    @property
    def memory(self) -> Memory:
        """Lazy initialization of Mem0 Memory instance."""
        if self._memory is None:
            mem0_config = self.config.get_mem0_config()
            try:
                self._memory = Memory.from_config(mem0_config)
            except Exception as e:
                # Provide user-friendly diagnostic messages
                if "API key" in str(e) or "api_key" in str(e).lower():
                    raise RuntimeError(
                        f"Failed to initialize Mem0 ({self.config.provider}): API key not found. "
                        "Please set GOOGLE_API_KEY / OPENAI_API_KEY in ~/.hippo/.env or environment."
                    ) from e
                raise
        return self._memory

    def add(
        self,
        content: Optional[str] = None,
        scope: str = "project",
        project_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        image_path: Optional[str] = None,
        text: Optional[str] = None,
        messages: Optional[List[Dict[str, str]]] = None,
        user_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        run_id: Optional[str] = None,
        prompt: Optional[str] = None,
        infer: bool = True,
        expiration_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Add a memory (text or multimodal) matching Mem0 specifications.

        Args:
            content: The text content, fact, or interaction to remember (alias for text).
            scope: 'project' (repo-specific) or 'global' (personal preference).
            project_id: Explicit project name (defaults to auto-detected git repo).
            metadata: Custom metadata dictionary.
            image_path: Optional path to a local image/screenshot for visual memory.
            text: Plain sentence summarizing what to store (official Mem0 argument).
            messages: Optional structured conversation history with role/content.
                When both text and messages are provided, messages serve as context
                and text is appended as an explicit assistant-role fact (never dropped).
            user_id: Optional user identifier override.
            agent_id: Optional agent or project identifier override.
            run_id: Optional run identifier.
            prompt: Optional custom extraction prompt for Mem0 LLM distillation.
            infer: Whether to distill memories using LLM (default True).
            expiration_date: Optional expiration date string (e.g. YYYY-MM-DD).

        Returns:
            Dict containing the added memory results.
        """
        raw_text = text if text is not None else (content or "")
        uid = user_id or self.config.user_id

        # Scope and agent_id resolution
        effective_scope = scope
        effective_project = project_id
        if agent_id:
            if agent_id == "global":
                effective_scope = "global"
            else:
                effective_scope = "project"
                effective_project = agent_id

        params = self.router.build_add_params(
            scope=effective_scope,
            user_id=uid,
            project_id=effective_project,
            extra_metadata=metadata,
        )
        if run_id:
            params["run_id"] = run_id
        if prompt is not None:
            params["prompt"] = prompt
        if not infer:
            params["infer"] = infer
        if expiration_date is not None:
            params["expiration_date"] = expiration_date

        full_content = raw_text
        if image_path:
            img_p = Path(image_path).expanduser()
            if img_p.exists():
                full_content += f"\n[Referenced Image: {img_p.name}]"
                params["metadata"]["image_path"] = str(img_p)

        conversation = build_conversation(text=full_content, content=None, messages=messages)
        try:
            result = self.memory.add(conversation, **params)
        except Exception as e:
            err_str = str(e)
            # If primary flagship model hits temporary 503 capacity issues or 429 quota exhaustion, fallback gracefully
            should_fallback = any(
                code in err_str for code in ["503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED"]
            )
            if should_fallback and hasattr(self.memory, "llm"):
                orig_model = getattr(self.memory.llm.config, "model", "")
                fallback_model = "gemini-3.5-flash-lite"
                if orig_model != fallback_model:
                    try:
                        self.memory.llm.config.model = fallback_model
                        result = self.memory.add(conversation, **params)
                    finally:
                        self.memory.llm.config.model = orig_model
                else:
                    raise
            else:
                raise
        return result

    def add_explicit(
        self,
        text: str,
        scope: Literal["project", "global"] = "project",
        project_id: Optional[str] = None,
        user_id: Optional[str] = None,
        category: Literal["preference", "decision", "pitfall", "general"] = "general",
    ) -> Dict[str, Any]:
        """Direct write seam for agent-explicit facts (Hot Path).

        Bypasses redundant secondary LLM extraction by setting infer=False.
        Attaches standard provenance (source='agent_explicit') and freshness timestamps.

        Args:
            text: Plain-text concise fact or decision (max 2000 chars).
            scope: 'project' (current git repo) or 'global' (user-level habit/preference).
            project_id: Optional explicit project name.
            user_id: Optional user identifier override.
            category: Knowledge category ('preference', 'decision', 'pitfall', 'general').

        Returns:
            Structured dictionary confirming addition with stable shape.
        """
        clean_text = (text or "").strip()
        if not clean_text:
            raise HippoValidationError("text cannot be empty")
        if len(clean_text) > 2000:
            raise HippoValidationError(
                f"text length ({len(clean_text)}) exceeds max allowed 2000 characters"
            )

        valid_scopes = {"project", "global"}
        if scope not in valid_scopes:
            raise HippoValidationError(f"Invalid scope '{scope}'. Must be one of {sorted(valid_scopes)}")

        valid_categories = {"preference", "decision", "pitfall", "general"}
        if category not in valid_categories:
            raise HippoValidationError(
                f"Invalid category '{category}'. Must be one of {sorted(valid_categories)}"
            )

        now = datetime.now(timezone.utc).isoformat()
        metadata = {
            "source": "agent_explicit",
            "category": category,
            "created_at": now,
            "updated_at": now,
            "last_confirmed_at": now,
        }

        raw_res = self.add(
            text=clean_text,
            scope=scope,
            project_id=project_id,
            user_id=user_id,
            metadata=metadata,
            infer=False,
        )

        memory_id = None
        if isinstance(raw_res, dict):
            results = raw_res.get("results")
            if isinstance(results, list) and results:
                memory_id = results[0].get("id")

        if not memory_id:
            results_count = 0
            if isinstance(raw_res, dict):
                results_val = raw_res.get("results")
                if isinstance(results_val, list):
                    results_count = len(results_val)
            logger.error(
                "Failed to persist explicit memory: backend returned no valid memory ID. "
                "Response type: %s, results_count: %d",
                type(raw_res).__name__,
                results_count,
            )
            raise RuntimeError("Failed to persist explicit memory: backend returned no valid memory ID")

        return {
            "status": "success",
            "id": memory_id,
            "text": clean_text,
            "scope": scope,
            "category": category,
        }

    def search(
        self,
        query: str,
        scope: str = "all",
        project_id: Optional[str] = None,
        limit: int = 5,
        filters: Optional[Dict[str, Any]] = None,
        user_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        threshold: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Search relevant memories using multi-signal hybrid retrieval and relevance gating.

        Args:
            query: The search term or natural language question.
            scope: 'all' (both global and project), 'project', or 'global'.
            project_id: Explicit project name (defaults to auto-detected git repo).
            limit: Maximum number of memories to return.
            filters: Structured filters dictionary (official Mem0 argument).
            user_id: Optional user identifier.
            agent_id: Optional agent or project identifier.
            threshold: Optional semantic relevance threshold passed to Mem0 (defaults to config value).

        Returns:
            List of matching memory objects passing the relevance gate.
        """
        if limit <= 0:
            return []

        uid = user_id or self.config.user_id
        if filters:
            computed_filters = filters.copy()
            if "user_id" not in computed_filters and not any(k in computed_filters for k in ("AND", "OR", "NOT")):
                computed_filters["user_id"] = uid
        elif agent_id:
            computed_filters = {"user_id": uid, "agent_id": agent_id}
        else:
            computed_filters = self.router.build_search_filters(
                scope=scope,
                user_id=uid,
                project_id=project_id,
            )

        # Determine effective semantic threshold (preserve explicit 0.0)
        effective_threshold = (
            getattr(self.config, "semantic_threshold", 0.1)
            if threshold is None
            else threshold
        )

        candidate_pool_size = max(limit * 4, 20)

        results = self.memory.search(
            query=query,
            filters=computed_filters,
            top_k=candidate_pool_size,
            threshold=effective_threshold,
            explain=True,
        )
        raw_list = results if isinstance(results, list) else results.get("results", [])

        from hippo_memory.gate import filter_search_results

        gate_cfg = (
            self.config.get_gate_config()
            if hasattr(self.config, "get_gate_config")
            else None
        )
        try:
            return filter_search_results(raw_list, config=gate_cfg, limit=limit)
        except Exception as e:
            logger.error("Error applying relevance gate to search results: %s", e)
            return []

    def get(self, memory_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve a single memory by its ID."""
        try:
            return self.memory.get(memory_id)
        except Exception:
            return None

    def update(
        self,
        memory_id: str,
        text: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Update an existing memory's text or metadata."""
        kwargs: Dict[str, Any] = {}
        if text is not None:
            kwargs["text"] = text
        if metadata is not None:
            kwargs["metadata"] = metadata
        return self.memory.update(memory_id, **kwargs)

    def get_memories(
        self,
        filters: Optional[Dict[str, Any]] = None,
        limit: int = 20,
        scope: str = "all",
        project_id: Optional[str] = None,
        user_id: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """List stored memories under the specified filters or scope."""
        uid = user_id or self.config.user_id
        if filters:
            computed_filters = filters.copy()
            if "user_id" not in computed_filters and not any(k in computed_filters for k in ("AND", "OR", "NOT")):
                computed_filters["user_id"] = uid
        elif agent_id:
            computed_filters = {"user_id": uid, "agent_id": agent_id}
        else:
            computed_filters = self.router.build_search_filters(
                scope=scope,
                user_id=uid,
                project_id=project_id,
            )

        results = self.memory.get_all(filters=computed_filters, top_k=limit)
        return results if isinstance(results, list) else results.get("results", [])

    def list_memories(
        self,
        scope: str = "all",
        project_id: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """List stored memories under the specified scope (convenience alias)."""
        return self.get_memories(scope=scope, project_id=project_id, limit=limit)

    def get_user_profile(self, user_id: Optional[str] = None) -> Dict[str, Any]:
        """Retrieve all persistent global preferences and profile facts for the user.

        Returns:
            Dict containing the list of global facts and formatted markdown summary.
        """
        uid = user_id or self.config.user_id
        filters = {"user_id": uid, "agent_id": "global"}

        try:
            memories = self.memory.get_all(filters=filters, top_k=50)
            items = memories if isinstance(memories, list) else memories.get("results", [])
        except Exception:
            items = []

        facts = [item["memory"] for item in items if "memory" in item]
        formatted = "\n".join([f"- {fact}" for fact in facts]) if facts else "暂无全局用户偏好记录"

        return {
            "user_id": uid,
            "count": len(facts),
            "facts": facts,
            "markdown": formatted,
        }

    def delete(self, memory_id: str) -> bool:
        """Delete a specific memory by its ID."""
        try:
            self.memory.delete(memory_id)
            return True
        except Exception:
            return False

    def delete_all(
        self,
        user_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        run_id: Optional[str] = None,
        scope: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> bool:
        """Bulk delete memories within a given scope."""
        uid = user_id or self.config.user_id
        aid = agent_id
        if scope == "global":
            aid = "global"
        elif scope == "project" and not aid:
            aid = self.router.resolve_project(project_id)

        kwargs: Dict[str, Any] = {"user_id": uid}
        if aid:
            kwargs["agent_id"] = aid
        if run_id:
            kwargs["run_id"] = run_id

        try:
            self.memory.delete_all(**kwargs)
            return True
        except Exception:
            return False

    def list_entities(self) -> Dict[str, Any]:
        """List users and projects/agents stored in memories."""
        items = self.get_memories(scope="all", limit=500)
        agents = set()
        users = set()
        for item in items:
            if "agent_id" in item:
                agents.add(item["agent_id"])
            if "user_id" in item:
                users.add(item["user_id"])
        return {
            "users": sorted(list(users)),
            "agents": sorted(list(agents)),
            "total_memories_sampled": len(items),
        }
