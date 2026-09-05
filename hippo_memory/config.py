import os
from pathlib import Path
from typing import Any, Dict, Optional
from dotenv import load_dotenv

# Base paths
HIPPO_HOME = Path(os.getenv("HIPPO_HOME", "~/.hippo")).expanduser()
DEFAULT_STORAGE_DIR = HIPPO_HOME / "storage"
DEFAULT_ENV_FILE = HIPPO_HOME / ".env"

# Auto-load .env from ~/.hippo/.env and project root .env
if DEFAULT_ENV_FILE.exists():
    load_dotenv(DEFAULT_ENV_FILE)
load_dotenv()

# Normalize Google Gemini API key names
if not os.getenv("GOOGLE_API_KEY"):
    for alt_key in ["GEMINI_API_KEY", "GOOGLE_GENERATIVE_AI_API_KEY"]:
        val = os.getenv(alt_key)
        if val:
            os.environ["GOOGLE_API_KEY"] = val
            break


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

        self.qdrant_path = str(self.storage_dir / "qdrant")
        self.history_db_path = str(self.storage_dir / "history.db")

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
            llm_model = os.getenv("GEMINI_LLM_MODEL", "gemini-2.5-flash")
            embed_model = os.getenv("GEMINI_EMBEDDING_MODEL", "models/text-embedding-004")
            dims = 768

            return {
                "vector_store": {
                    "provider": "qdrant",
                    "config": {
                        "collection_name": "hippo_memories",
                        "embedding_model_dims": dims,
                        "path": self.qdrant_path,
                        "on_disk": True,
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
                        "path": self.qdrant_path,
                        "on_disk": True,
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
