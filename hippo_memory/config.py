import os
import re
from pathlib import Path
from typing import Any, Dict, Optional
from dotenv import load_dotenv

# Base paths
HIPPO_HOME = Path(os.getenv("HIPPO_HOME", "~/.hippo")).expanduser()
DEFAULT_STORAGE_DIR = HIPPO_HOME / "storage"
DEFAULT_SPOOL_DIR = HIPPO_HOME / "spool"
DEFAULT_ENV_FILE = HIPPO_HOME / ".env"

# Auto-load .env from ~/.hippo/.env and project root .env
if DEFAULT_ENV_FILE.exists():
    load_dotenv(DEFAULT_ENV_FILE, override=True)
load_dotenv(override=True)

# Normalize Google Gemini API key names
if not os.getenv("GOOGLE_API_KEY"):
    for alt_key in ["GEMINI_API_KEY", "GOOGLE_GENERATIVE_AI_API_KEY"]:
        val = os.getenv(alt_key)
        if val:
            os.environ["GOOGLE_API_KEY"] = val
            break


def ensure_qdrant_server():
    """Ensure Qdrant server is running on 127.0.0.1:6333."""
    import socket
    import subprocess
    import time

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    result = sock.connect_ex(("127.0.0.1", 6333))
    sock.close()

    if result != 0:
        qdrant_bin = HIPPO_HOME / "bin" / "qdrant"
        qdrant_cfg = HIPPO_HOME / "config" / "qdrant.yaml"
        if qdrant_bin.exists() and qdrant_cfg.exists():
            log_path = HIPPO_HOME / "qdrant.log"
            with open(log_path, "a") as f:
                subprocess.Popen(
                    [str(qdrant_bin), "--config-path", str(qdrant_cfg)],
                    stdout=f,
                    stderr=f,
                    # qdrant 会在 cwd 下创建 ./snapshots/tmp 等相对路径，固定到可写的 HIPPO_HOME
                    cwd=str(HIPPO_HOME),
                    start_new_session=True,
                )
            time.sleep(1.0)


DEFAULT_CUSTOM_INSTRUCTIONS = (
    "所有提取的记忆事实必须使用简体中文输出。"
    "保留关键技术专有名词（如编程语言、框架、工具名、API、配置项等）的原名，生成清晰、独立、精炼的中文事实陈述句。"
)


def resolve_collection_name(
    provider: str,
    embedding_model: Optional[str] = None,
    dims: Optional[int] = None,
) -> str:
    """Resolve the Qdrant collection without mixing incompatible vector spaces.

    Existing Gemini/OpenAI users retain the historical ``hippo_memories``
    collection for backward compatibility. Vertex AI defaults to an isolated
    collection because its embeddings must not be mixed with vectors created by
    the Gemini Developer API or another embedding model.
    """
    override = os.getenv("HIPPO_COLLECTION_NAME", "").strip()
    if override:
        return override

    if provider != "vertexai":
        return "hippo_memories"

    model = embedding_model or os.getenv("VERTEX_EMBEDDING_MODEL", "gemini-embedding-2")
    dimensions = dims or int(os.getenv("VERTEX_EMBEDDING_DIMS", "768"))
    model_slug = re.sub(r"[^a-zA-Z0-9]+", "_", model).strip("_").lower()
    return f"hippo_memories_vertexai_{model_slug}_{dimensions}"


def _register_vertex_genai_embedder() -> None:
    """Replace Mem0's legacy Vertex embedder with Hippo's google-genai adapter.

    Mem0 currently maps the ``vertexai`` provider to
    ``vertexai.language_models.TextEmbeddingModel``. That implementation does
    not support ``gemini-embedding-2``'s current embedContent API, while Mem0's
    config schema only accepts known provider names. Reusing the existing
    ``vertexai`` provider key keeps Hippo compatible with Mem0's public config
    shape and limits the compatibility shim to a single factory mapping.
    """
    from mem0.utils.factory import EmbedderFactory

    EmbedderFactory.provider_to_class[
        "vertexai"
    ] = "hippo_memory.embeddings.vertex_genai.VertexAIGenAIEmbedding"


class HippoConfig:
    """Hippo unified memory configuration."""

    def __init__(
        self,
        user_id: Optional[str] = None,
        storage_dir: Optional[Path] = None,
        provider: Optional[str] = None,
    ):
        self.user_id = user_id or os.getenv("HIPPO_USER_ID", "munger")
        self.storage_dir = Path(
            storage_dir or os.getenv("HIPPO_STORAGE_DIR", str(DEFAULT_STORAGE_DIR))
        ).expanduser()
        self.storage_dir.mkdir(parents=True, exist_ok=True)

        self.qdrant_host = os.getenv("QDRANT_HOST", "127.0.0.1")
        self.qdrant_port = int(os.getenv("QDRANT_PORT", "6333"))
        self.qdrant_url = f"http://{self.qdrant_host}:{self.qdrant_port}"
        self.history_db_path = str(self.storage_dir / "history.db")

        # Ensure Qdrant server is running
        ensure_qdrant_server()

        # Provider determination. Vertex AI is intentionally opt-in because ADC
        # may exist on a developer machine for unrelated Google Cloud workloads.
        configured_provider = provider or os.getenv("HIPPO_PROVIDER", "auto").lower()
        if configured_provider == "auto":
            if os.getenv("GOOGLE_API_KEY"):
                self.provider = "gemini"
            elif os.getenv("OPENAI_API_KEY"):
                self.provider = "openai"
            else:
                self.provider = "gemini"  # default fallback
        else:
            self.provider = configured_provider

        # Relevance gate & retrieval thresholds
        self.semantic_threshold = float(os.getenv("HIPPO_SEMANTIC_THRESHOLD", "0.1"))
        self.final_threshold = float(os.getenv("HIPPO_FINAL_THRESHOLD", "0.32"))
        self.dense_only_threshold = float(os.getenv("HIPPO_DENSE_ONLY_THRESHOLD", "0.62"))
        self.relative_threshold_ratio = float(os.getenv("HIPPO_RELATIVE_RATIO", "0.50"))
        self.max_injected = int(os.getenv("HIPPO_MAX_INJECTED", "3"))
        self.gate_enabled = os.getenv("HIPPO_GATE_ENABLED", "1").lower() in ("1", "true", "yes")

        from hippo_memory.gate import SearchGateConfig

        self.gate_config = SearchGateConfig(
            final_threshold=self.final_threshold,
            dense_only_threshold=self.dense_only_threshold,
            relative_threshold_ratio=self.relative_threshold_ratio,
            enabled=self.gate_enabled,
        )

    def get_gate_config(self):
        """Get the pre-constructed SearchGateConfig object."""
        return self.gate_config

    def get_mem0_config(self) -> Dict[str, Any]:
        """Generate Mem0 configuration dictionary based on active provider."""
        if self.provider == "gemini":
            api_key = os.getenv("GOOGLE_API_KEY", "")
            llm_model = os.getenv("GEMINI_LLM_MODEL", "gemini-3.5-flash-lite")
            embed_model = os.getenv("GEMINI_EMBEDDING_MODEL", "models/gemini-embedding-2")
            dims = 768

            return {
                "vector_store": {
                    "provider": "qdrant",
                    "config": {
                        "collection_name": resolve_collection_name("gemini", embed_model, dims),
                        "embedding_model_dims": dims,
                        "host": self.qdrant_host,
                        "port": self.qdrant_port,
                    },
                },
                "llm": {
                    "provider": "gemini",
                    "config": {
                        "model": llm_model,
                        "api_key": api_key,
                        "temperature": 0.1,
                    },
                },
                "embedder": {
                    "provider": "gemini",
                    "config": {
                        "model": embed_model,
                        "embedding_dims": dims,
                        "api_key": api_key,
                    },
                },
                "history_db_path": self.history_db_path,
                "version": "v1.1",
                "custom_instructions": DEFAULT_CUSTOM_INSTRUCTIONS,
            }

        elif self.provider == "vertexai":
            project = os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
            if not project:
                raise ValueError(
                    "GOOGLE_CLOUD_PROJECT is required when HIPPO_PROVIDER=vertexai"
                )

            location = os.getenv("GOOGLE_CLOUD_LOCATION", "global").strip() or "global"
            llm_model = os.getenv(
                "VERTEX_LLM_MODEL",
                os.getenv("GEMINI_LLM_MODEL", "gemini-3.5-flash-lite"),
            )
            embed_model = os.getenv("VERTEX_EMBEDDING_MODEL", "gemini-embedding-2")
            dims = int(os.getenv("VERTEX_EMBEDDING_DIMS", "768"))

            _register_vertex_genai_embedder()

            return {
                "vector_store": {
                    "provider": "qdrant",
                    "config": {
                        "collection_name": resolve_collection_name(
                            "vertexai", embed_model, dims
                        ),
                        "embedding_model_dims": dims,
                        "host": self.qdrant_host,
                        "port": self.qdrant_port,
                    },
                },
                "llm": {
                    "provider": "gemini",
                    "config": {
                        "model": llm_model,
                        "vertexai": True,
                        "project": project,
                        "location": location,
                        "temperature": 0.1,
                    },
                },
                "embedder": {
                    "provider": "vertexai",
                    "config": {
                        "model": embed_model,
                        "embedding_dims": dims,
                    },
                },
                "history_db_path": self.history_db_path,
                "version": "v1.1",
                "custom_instructions": DEFAULT_CUSTOM_INSTRUCTIONS,
            }

        elif self.provider == "openai":
            api_key = os.getenv("OPENAI_API_KEY", "")
            llm_model = os.getenv("OPENAI_LLM_MODEL", "gpt-4o-mini")
            embed_model = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
            dims = 1536

            return {
                "vector_store": {
                    "provider": "qdrant",
                    "config": {
                        "collection_name": resolve_collection_name("openai", embed_model, dims),
                        "embedding_model_dims": dims,
                        "host": self.qdrant_host,
                        "port": self.qdrant_port,
                    },
                },
                "llm": {
                    "provider": "openai",
                    "config": {
                        "model": llm_model,
                        "api_key": api_key,
                        "temperature": 0.1,
                    },
                },
                "embedder": {
                    "provider": "openai",
                    "config": {
                        "model": embed_model,
                        "embedding_dims": dims,
                        "api_key": api_key,
                    },
                },
                "history_db_path": self.history_db_path,
                "version": "v1.1",
                "custom_instructions": DEFAULT_CUSTOM_INSTRUCTIONS,
            }

        else:
            raise ValueError(f"Unsupported provider: {self.provider}")


_default_config: Optional[HippoConfig] = None


def get_config() -> HippoConfig:
    """Get or create singleton HippoConfig instance."""
    global _default_config
    if _default_config is None:
        _default_config = HippoConfig()
    return _default_config
