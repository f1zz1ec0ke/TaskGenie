import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    base_url: str = os.environ.get("TASKGENIE_BASE_URL", "http://100.84.115.121:1234/v1")
    api_key: str = os.environ.get("TASKGENIE_API_KEY", "lm-studio")
    model: str = os.environ.get("TASKGENIE_MODEL", "")
    temperature: float = float(os.environ.get("TASKGENIE_TEMPERATURE", "0.4"))
    max_depth: int = int(os.environ.get("TASKGENIE_MAX_DEPTH", "4"))
    max_turns_per_agent: int = int(os.environ.get("TASKGENIE_MAX_TURNS", "25"))
    scratch_dir: str = os.environ.get("TASKGENIE_SCRATCH", ".scratch")


CONFIG = Config()
