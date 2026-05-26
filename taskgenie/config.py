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


@dataclass(frozen=True)
class Config:
    base_url: str = _env("TASKGENIE_BASE_URL", "http://100.84.115.121:1234/v1")
    api_key: str = _env("TASKGENIE_API_KEY", "lm-studio")
    model: str = _env("TASKGENIE_MODEL", "")
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
    compact_at_pct: float = _env_float("TASKGENIE_COMPACT_AT_PCT", 0.6)
    compact_keep_recent: int = _env_int("TASKGENIE_COMPACT_KEEP_RECENT", 3)
    dump_tool_output_above_bytes: int = _env_int("TASKGENIE_DUMP_OVER_BYTES", 16384)
    data_dir: str = _env("TASKGENIE_DATA_DIR", ".taskgenie")
    mcp_config_path: str = _env("TASKGENIE_MCP_CONFIG", "./mcp.json")

    @property
    def sessions_dir(self) -> Path:
        return Path(self.data_dir) / "sessions"

    @property
    def scratch_dir(self) -> Path:
        # Kept for backward-compat with the older work tools that took a raw path.
        return Path(self.data_dir) / "scratch"


CONFIG = Config()
