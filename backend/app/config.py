"""Single source of runtime configuration.

Every model name and endpoint lives here (overridable via environment / `.env`),
so swapping the chat model or provider never needs a code change. Any
OpenAI-compatible endpoint works: GroqCloud (default), oMLX, etc.
"""

from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- LLM: GroqCloud (OpenAI-compatible) ---
    llm_provider: str = "groq"  # label only (logs, baselines, eval runs)
    llm_base_url: str = "https://api.groq.com/openai/v1"
    llm_api_key: str = Field(default="", repr=False)
    chat_model: str = "qwen/qwen3.8-27b"
    llm_temperature: float = 0.0
    llm_max_tokens: int = 900  # Groq free tier: max_tokens counts against ~1000 output tok/min
    llm_timeout_s: float = 60.0
    llm_max_retries: int = 4  # on 429 / 5xx, honouring Retry-After
    # Provider-specific request fields merged into every chat call, as JSON in .env,
    # Qwen3 on Groq: {"reasoning_effort": "none"} = thinking off. gpt-oss: "low"|"medium"|"high".
    llm_extra_body: dict[str, Any] = Field(default_factory=lambda: {"reasoning_effort": "none"})

    # Optional on-disk cache of LLM responses (evals set this so reruns cost no quota).
    llm_cache_dir: Path | None = None
    llm_cache_only: bool = False  # evals: score only what's cached, never call the API
    llm_disabled: bool = False  # evals: no LLM at all (saves free-tier quota during testing)
    llm_max_requests: int | None = None  # hard cap on real API calls per process (evals)
    llm_mask_numbers: bool = True  # mask account/card/phone-like digit runs before any LLM call
    # API default for use_cloud_llm on uploads and chat (per-request override).
    cloud_llm_default: bool = True

    # --- OCR (scanned pages; runs natively on macOS) ---
    ocr_engine: str = "vision"  # vision | tesseract | rapidocr (chosen by the Phase 1b bake-off)
    ocr_secondary: str = "tesseract"  # tie-breaker, only when two primary readings disagree

    # --- Embeddings (Phase 3). Groq has no embeddings API. Default: local fastembed (ONNX, CPU),
    # so statement text never leaves the machine. "openai" = any OpenAI-compatible /embeddings.
    # The chunks table is vector(384): a model with another dimension needs a schema change.
    embed_provider: str = "local"  # local | openai
    embed_model: str = "BAAI/bge-small-en-v1.5"
    embed_dim: int = 384
    embed_base_url: str = ""
    embed_api_key: str = Field(default="", repr=False)
    embed_cache_dir: Path | None = None  # default: <data_dir>/cache/embed

    @field_validator("embed_provider", "embed_model", mode="before")
    @classmethod
    def _blank_means_default(cls, v: Any, info) -> Any:
        # an empty `EMBED_MODEL=` line in .env must not wipe the default
        return cls.model_fields[info.field_name].default if v in ("", None) else v

    # --- Postgres + pgvector ---
    database_url: str = "postgresql://bank:bank@localhost:5433/bank_agents"

    # --- API ---
    max_upload_mb: int = 25
    # Optional LangSmith tracing: LANGSMITH_TRACING=true + LANGSMITH_API_KEY in .env (read by
    # LangGraph directly). Off unless configured.

    # --- Paths ---
    data_dir: Path = REPO_ROOT / "data"


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    if s.embed_cache_dir is None:
        s.embed_cache_dir = s.data_dir / "cache" / "embed"
    return s
