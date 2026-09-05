import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from mem0 import Memory
from hippo_memory.config import HippoConfig
from hippo_memory.router import ScopeRouter


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
        content: str,
        scope: str = "project",
        project_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        image_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Add a memory (text or multimodal).

        Args:
            content: The text content, fact, or interaction to remember.
            scope: 'project' (repo-specific) or 'global' (personal preference).
            project_id: Explicit project name (defaults to auto-detected git repo).
            metadata: Custom metadata dictionary.
            image_path: Optional path to a local image/screenshot for visual memory.

        Returns:
            Dict containing the added memory results.
        """
        params = self.router.build_add_params(
            scope=scope,
            user_id=self.config.user_id,
            project_id=project_id,
            extra_metadata=metadata,
        )

        full_content = content
        if image_path:
            img_p = Path(image_path).expanduser()
            if img_p.exists():
                full_content += f"\n[Referenced Image: {img_p.name}]"
                params["metadata"]["image_path"] = str(img_p)

        messages = [{"role": "user", "content": full_content}]
        try:
            result = self.memory.add(messages, **params)
        except Exception as e:
            err_str = str(e)
            # If primary flagship model hits temporary 503 capacity issues, fallback gracefully
            if ("503" in err_str or "UNAVAILABLE" in err_str) and hasattr(self.memory, "llm"):
                orig_model = getattr(self.memory.llm.config, "model", "")
                fallback_model = "gemini-3.5-flash-lite"
                if orig_model != fallback_model:
                    try:
                        self.memory.llm.config.model = fallback_model
                        result = self.memory.add(messages, **params)
                    finally:
                        self.memory.llm.config.model = orig_model
                else:
                    raise
            else:
                raise
        return result

    def search(
        self,
        query: str,
        scope: str = "all",
        project_id: Optional[str] = None,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """Search relevant memories using multi-signal hybrid retrieval.

        Args:
            query: The search term or natural language question.
            scope: 'all' (both global and project), 'project', or 'global'.
            project_id: Explicit project name (defaults to auto-detected git repo).
            limit: Maximum number of memories to return.

        Returns:
            List of matching memory objects.
        """
        filters = self.router.build_search_filters(
            scope=scope,
            user_id=self.config.user_id,
            project_id=project_id,
        )

        results = self.memory.search(
            query=query,
            filters=filters,
            top_k=limit,
        )
        return results if isinstance(results, list) else results.get("results", [])

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

    def list_memories(
        self,
        scope: str = "all",
        project_id: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """List stored memories under the specified scope.

        Args:
            scope: 'all', 'project', or 'global'.
            project_id: Explicit project name.
            limit: Maximum items to return.
        """
        filters = self.router.build_search_filters(
            scope=scope,
            user_id=self.config.user_id,
            project_id=project_id,
        )

        results = self.memory.get_all(filters=filters, top_k=limit)
        return results if isinstance(results, list) else results.get("results", [])

    def delete(self, memory_id: str) -> bool:
        """Delete a specific memory by its ID."""
        try:
            self.memory.delete(memory_id)
            return True
        except Exception:
            return False
