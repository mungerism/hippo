import os
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

        # Provider determination
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
                        "collection_name": "hippo_memories",
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
                        "collection_name": "hippo_memories",
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

