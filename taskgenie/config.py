from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Load .env from cwd at import time so all downstream reads see it.
load_dotenv(Path.cwd() / ".env", override=False)


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    return int(raw) if raw else default


def _env_float(key: str, default: float) -> float:
    raw = os.environ.get(key)
    return float(raw) if raw else default


def _env_bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes")


def _resolve_api_key() -> str:
    """Return the best available API key.

    Priority: TASKGENIE_API_KEY (if set to a real value) -> OPENAI_API_KEY
    -> fallback default "lm-studio".
    """
    tg = os.environ.get("TASKGENIE_API_KEY", "").strip()
    if tg and tg != "lm-studio":
        return tg
    openai = os.environ.get("OPENAI_API_KEY", "").strip()
    if openai:
        return openai
    # Nothing useful found; keep the original TASKGENIE value or default.
    return tg if tg else "lm-studio"


_DEFAULT_BASE_URL = "http://localhost:1234/v1"


def _resolve_base_url() -> str:
    """Return the best available base URL.

    Priority: TASKGENIE_BASE_URL (if not the default localhost) ->
    OPENAI_BASE_URL -> fallback default.
    """
    tg = os.environ.get("TASKGENIE_BASE_URL", "").strip()
    if tg and tg != _DEFAULT_BASE_URL:
        return tg
    openai = os.environ.get("OPENAI_BASE_URL", "").strip()
    if openai:
        return openai
    return tg if tg else _DEFAULT_BASE_URL


@dataclass(frozen=True)
class Config:
    base_url: str = _resolve_base_url()
    api_key: str = _resolve_api_key()
    model: str = _env("TASKGENIE_MODEL", "")
    ceo_model: str = _env("TASKGENIE_CEO_MODEL", "")
    manager_model: str = _env("TASKGENIE_MANAGER_MODEL", "")
    worker_model: str = _env("TASKGENIE_WORKER_MODEL", "")
    temperature: float = _env_float("TASKGENIE_TEMPERATURE", 0.1)
    max_depth: int = _env_int("TASKGENIE_MAX_DEPTH", 8)
    max_turns_per_agent: int = _env_int("TASKGENIE_MAX_TURNS", 1000)
    max_concurrent_llm: int = _env_int("TASKGENIE_MAX_CONCURRENT_LLM", 2)
    exec_timeout_seconds: int = _env_int("TASKGENIE_EXEC_TIMEOUT", 60)
    mcp_call_timeout_seconds: int = _env_int("TASKGENIE_MCP_CALL_TIMEOUT", 60)
    # Context-window management knobs (see taskgenie/context.py + archivist.py).
    # Default ctx_window matches Claude 3.5+ / many local models; smaller
    # models should override via TASKGENIE_CTX_WINDOW.
    ctx_window_tokens: int = _env_int("TASKGENIE_CTX_WINDOW", 128000)
    compact_at_pct: float = _env_float("TASKGENIE_COMPACT_AT_PCT", 0.8)
    compact_keep_recent: int = _env_int("TASKGENIE_COMPACT_KEEP_RECENT", 3)
    dump_tool_output_above_bytes: int = _env_int("TASKGENIE_DUMP_OVER_BYTES", 131072)
    data_dir: str = _env("TASKGENIE_DATA_DIR", ".taskgenie")
    mcp_config_path: str = _env("TASKGENIE_MCP_CONFIG", "./mcp.json")
    # Token budget tracking.
    max_session_tokens: int = _env_int("TASKGENIE_MAX_SESSION_TOKENS", 0)
    token_cost_per_million: float = _env_float("TASKGENIE_TOKEN_COST_PER_MILLION", 0.0)
    # Logging.
    log_level: str = _env("TASKGENIE_LOG_LEVEL", "WARNING")
    # Headless (no interactive UI).
    headless: bool = _env_bool("TASKGENIE_HEADLESS", False)

    def __post_init__(self) -> None:
        errors: list[str] = []
        if not (0.0 <= self.compact_at_pct <= 1.0):
            errors.append(
                f"compact_at_pct must be between 0.0 and 1.0, got {self.compact_at_pct}"
            )
        if self.max_depth < 1:
            errors.append(f"max_depth must be >= 1, got {self.max_depth}")
        if self.max_turns_per_agent < 1:
            errors.append(
                f"max_turns_per_agent must be >= 1, got {self.max_turns_per_agent}"
            )
        if self.max_concurrent_llm < 1:
            errors.append(
                f"max_concurrent_llm must be >= 1, got {self.max_concurrent_llm}"
            )
        if self.exec_timeout_seconds < 1:
            errors.append(
                f"exec_timeout_seconds must be >= 1, got {self.exec_timeout_seconds}"
            )
        if self.ctx_window_tokens < 1000:
            errors.append(
                f"ctx_window_tokens must be >= 1000, got {self.ctx_window_tokens}"
            )
        if self.dump_tool_output_above_bytes < 0:
            errors.append(
                f"dump_tool_output_above_bytes must be >= 0, got {self.dump_tool_output_above_bytes}"
            )
        if not (0.0 <= self.temperature <= 2.0):
            errors.append(
                f"temperature must be between 0.0 and 2.0, got {self.temperature}"
            )
        if self.compact_keep_recent < 1:
            errors.append(
                f"compact_keep_recent must be >= 1, got {self.compact_keep_recent}"
            )
        if errors:
            raise ValueError(
                "Invalid Config:\n" + "\n".join(f"  - {e}" for e in errors)
            )

    @property
    def sessions_dir(self) -> Path:
        return Path(self.data_dir) / "sessions"

    @property
    def scratch_dir(self) -> Path:
        # Kept for backward-compat with the older work tools that took a raw path.
        return Path(self.data_dir) / "scratch"


CONFIG = Config()

import logging


def setup_logging(level: str = "WARNING") -> None:
    """Configure logging for TaskGenie. Called once at startup."""
    numeric = getattr(logging, level.upper(), logging.WARNING)
    logging.basicConfig(
        level=numeric,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Quiet down noisy libraries
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
