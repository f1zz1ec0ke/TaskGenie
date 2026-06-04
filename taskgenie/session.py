"""Persistence: snapshot a TaskGenie run to disk and resume it later.

Conservative checkpointing: only saves at clean quiescent points (after the
CEO's run loop returns). State is JSON for readability; the per-agent scratch
directories live alongside.

Two on-disk locations per session:
  - `.taskgenie/sessions/<id>/`  — internal: state.json, events.jsonl, scratch
  - `./TaskGenie/<project>/`     — USER-FACING: the workspace deliverables
                                    (game.py, README.md, etc.) plus a copy of
                                    the CEO's report. This is what the user
                                    opens after the run.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
import secrets
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from taskgenie.config import CONFIG
from taskgenie.tools.registry import REGISTRY

logger = logging.getLogger(__name__)

STATE_FILE = "state.json"
EVENTS_FILE = "events.jsonl"
PROJECTS_ROOT_ENV = "TASKGENIE_PROJECTS_DIR"
DEFAULT_PROJECTS_ROOT = "TaskGenie"


def make_session_id(task: str) -> str:
    date = _dt.date.today().isoformat()
    words = re.findall(r"\w+", task.lower())[:6]
    slug = "-".join(words)[:40] or "session"
    suffix = secrets.token_hex(2)
    return f"{date}-{slug}-{suffix}"


def _project_slug(task: str) -> str:
    """A short, friendly directory name derived from the task."""
    words = re.findall(r"\w+", task.lower())[:4]
    slug = "-".join(words)[:30]
    return slug or "project"


def _projects_root() -> Path:
    return Path(os.environ.get(PROJECTS_ROOT_ENV, DEFAULT_PROJECTS_ROOT)).resolve()


def _unique_project_dir(name: str) -> Path:
    """Pick a project dir under the projects root, suffixing on collision."""
    root = _projects_root()
    base = root / name
    if not base.exists():
        return base
    for i in range(2, 1000):
        candidate = root / f"{name}-{i}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Too many existing {name}* projects in {root}")


@dataclass
class HistoryEntry:
    kind: str  # "task" | "blocker" | "unblock" | "done" | "abandoned"
    ts: str = field(default_factory=lambda: _dt.datetime.now().isoformat(timespec="seconds"))
    text: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class SessionData:
    session_id: str
    task: str
    started_at: str
    last_saved_at: str
    status: str  # "running" | "blocked" | "asking" | "done" | "abandoned" | "error"
    ceo_id: str | None
    project_dir: str = ""  # absolute path to ./TaskGenie/<project>/
    agents: dict[str, dict[str, Any]] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)
    tool_signature: dict[str, dict[str, str]] = field(default_factory=dict)
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0

    def add_history(self, entry: HistoryEntry) -> None:
        self.history.append(asdict(entry))


class Session:
    """One TaskGenie session on disk. Owns the state.json + scratch dirs + event log."""

    def __init__(self, data: SessionData) -> None:
        self.data = data

    @property
    def session_id(self) -> str:
        return self.data.session_id

    @property
    def root_dir(self) -> Path:
        return CONFIG.sessions_dir / self.session_id

    @property
    def state_path(self) -> Path:
        return self.root_dir / STATE_FILE

    @property
    def events_path(self) -> Path:
        return self.root_dir / EVENTS_FILE

    @property
    def scratch_root(self) -> Path:
        return self.root_dir / "scratch"

    @property
    def project_dir(self) -> Path:
        """The user-facing project directory under ./TaskGenie/<project>/.
        Workers write deliverable artifacts here; the CEO's report is
        copied alongside as README.md so the user can `cd` here and see
        everything together."""
        if self.data.project_dir:
            return Path(self.data.project_dir)
        # Backward-compat for sessions saved before project_dir existed:
        # fall back to the old session/workspace/ location.
        return self.root_dir / "workspace"

    # Alias retained so older callers (and tools that referenced
    # session.workspace_dir) still resolve to the right path.
    @property
    def workspace_dir(self) -> Path:
        return self.project_dir

    def add_tokens(self, prompt_tokens: int, completion_tokens: int) -> None:
        """Track cumulative token usage across the session."""
        self.data.total_prompt_tokens += prompt_tokens
        self.data.total_completion_tokens += completion_tokens

    @property
    def total_tokens(self) -> int:
        return self.data.total_prompt_tokens + self.data.total_completion_tokens

    def estimated_cost(self, cost_per_million: float) -> float:
        """Estimate cost based on total tokens and configured price."""
        if cost_per_million <= 0:
            return 0.0
        return (self.total_tokens / 1_000_000) * cost_per_million

    @classmethod
    def create(cls, task: str, project: str | None = None) -> "Session":
        session_id = make_session_id(task)
        now = _dt.datetime.now().isoformat(timespec="seconds")
        slug = project or _project_slug(task)
        project_path = _unique_project_dir(slug)
        data = SessionData(
            session_id=session_id,
            task=task,
            started_at=now,
            last_saved_at=now,
            status="running",
            ceo_id=None,
            project_dir=str(project_path),
        )
        data.add_history(HistoryEntry(kind="task", text=task))
        session = cls(data)
        session.root_dir.mkdir(parents=True, exist_ok=True)
        session.scratch_root.mkdir(parents=True, exist_ok=True)
        project_path.mkdir(parents=True, exist_ok=True)
        return session

    @classmethod
    def load(cls, session_id: str) -> "Session":
        path = CONFIG.sessions_dir / session_id / STATE_FILE
        if not path.exists():
            raise FileNotFoundError(f"no session: {session_id}")
        try:
            raw = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            raise ValueError(
                f"Session {session_id} has a corrupt state.json: {e}"
            ) from e
        if not isinstance(raw, dict):
            raise ValueError(
                f"Session {session_id} state.json is not a JSON object"
            )
        # Filter to fields the dataclass actually knows about so a state.json
        # written by a newer version (with extra fields) still loads on an
        # older binary. Missing fields fall back to dataclass defaults.
        known_fields = {f.name for f in fields(SessionData)}
        unknown = sorted(set(raw) - known_fields)
        filtered = {k: v for k, v in raw.items() if k in known_fields}
        try:
            data = SessionData(**filtered)
        except TypeError as e:
            raise ValueError(
                f"Session {session_id} state.json is missing required fields: {e}"
            ) from e
        session = cls(data)
        if unknown:
            session._unknown_fields_on_load = unknown
        logger.info(f"Session {session.session_id} loaded ({len(data.agents)} agents)")
        return session

    @classmethod
    def load_latest(cls) -> "Session | None":
        sessions = cls.list_all()
        if not sessions:
            return None
        return cls.load(sessions[0]["session_id"])

    @classmethod
    def list_all(cls) -> list[dict[str, Any]]:
        root = CONFIG.sessions_dir
        if not root.exists():
            return []
        rows: list[dict[str, Any]] = []
        for sub in root.iterdir():
            state = sub / STATE_FILE
            if not state.exists():
                continue
            try:
                d = json.loads(state.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            agents_dict = d.get("agents", {})
            total_prompt_tokens = 0
            for agent_data in agents_dict.values():
                if isinstance(agent_data, dict):
                    total_prompt_tokens += agent_data.get("prompt_tokens", 0)
            rows.append(
                {
                    "session_id": d.get("session_id", sub.name),
                    "task": d.get("task", ""),
                    "started_at": d.get("started_at", ""),
                    "last_saved_at": d.get("last_saved_at", ""),
                    "status": d.get("status", "?"),
                    "agent_count": len(agents_dict),
                    "prompt_tokens": total_prompt_tokens,
                }
            )
        rows.sort(key=lambda r: r["last_saved_at"], reverse=True)
        return rows

    def save(self) -> None:
        logger.debug(f"Saving session {self.session_id}")
        self.data.last_saved_at = _dt.datetime.now().isoformat(timespec="seconds")
        self.data.tool_signature = {
            "management": REGISTRY.signature("management"),
            "work": REGISTRY.signature("work"),
            # ceo-only tools (currently just ask_user) live in their own
            # bucket -- without persisting them, schema changes to
            # ceo-only tools would silently slip past the resume diff.
            "ceo": REGISTRY.signature("ceo"),
        }
        self.root_dir.mkdir(parents=True, exist_ok=True)
        # Atomic write. `default=str` mirrors event_logger/artifacts and
        # keeps an accidentally-leaked non-JSON-native value (a Path, a
        # set, a stray datetime) from raising mid-write and orphaning the
        # temp file. The try/finally below cleans up any temp that didn't
        # make it through `os.replace`.
        #
        # IMPORTANT: capture tmp_path the moment NamedTemporaryFile opens
        # -- not after json.dump succeeds. The previous version captured
        # it AFTER the write/flush/fsync, so the cleanup branch was
        # skipped on exactly the json.dump failure it was supposed to
        # handle. tempfile.NamedTemporaryFile(delete=False) creates the
        # file on disk before yielding, so .name is valid immediately.
        tmp_path: str | None = None
        consumed = False
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                dir=self.root_dir,
                prefix=".state-",
                suffix=".json.tmp",
                delete=False,
                encoding="utf-8",
            ) as tmp:
                tmp_path = tmp.name  # CAPTURE FIRST so finally can clean up on any subsequent failure
                json.dump(asdict(self.data), tmp, indent=2, default=str)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_path, self.state_path)
            consumed = True
            # fsync the directory on POSIX so the rename itself is
            # durable across power loss. Windows doesn't expose this;
            # skip there.
            if os.name != "nt":
                try:
                    dir_fd = os.open(self.root_dir, os.O_RDONLY)
                    try:
                        os.fsync(dir_fd)
                    finally:
                        os.close(dir_fd)
                except OSError:
                    pass
        finally:
            if tmp_path and not consumed:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def snapshot_agents(self, agents: dict[str, dict[str, Any]]) -> None:
        self.data.agents = agents

    def set_ceo(self, ceo_id: str) -> None:
        self.data.ceo_id = ceo_id

    def mark_blocked(self, blocker_payload: dict[str, Any]) -> None:
        self.data.status = "blocked"
        self.data.add_history(HistoryEntry(kind="blocker", payload=blocker_payload))

    def mark_unblock(self, user_response: str) -> None:
        self.data.status = "running"
        self.data.add_history(HistoryEntry(kind="unblock", text=user_response))

    def mark_question(self, question_payload: dict[str, Any]) -> None:
        self.data.status = "asking"
        self.data.add_history(HistoryEntry(kind="question", payload=question_payload))

    def mark_answer(self, user_answer: str) -> None:
        self.data.status = "running"
        self.data.add_history(HistoryEntry(kind="answer", text=user_answer))

    def mark_done(self, report: str) -> None:
        self.data.status = "done"
        self.data.add_history(HistoryEntry(kind="done", text=report))

    def mark_followup(self, user_text: str) -> None:
        """A user follow-up after the CEO already completed once. Session
        moves back to 'running' so resume picks up correctly."""
        self.data.status = "running"
        self.data.add_history(HistoryEntry(kind="followup", text=user_text))

    def mark_reset(self, fired_count: int) -> None:
        """The user issued /reset; the team was disbanded but the
        workspace and CEO persist. Recorded for forensic history."""
        self.data.add_history(
            HistoryEntry(kind="reset", payload={"fired_count": fired_count})
        )

    def mark_abandoned(self) -> None:
        self.data.status = "abandoned"
        self.data.add_history(HistoryEntry(kind="abandoned"))

    def mark_error(self, error: str) -> None:
        self.data.status = "error"
        self.data.add_history(HistoryEntry(kind="error", text=error))

    def diff_tools_against(
        self,
        current_mgmt: dict[str, str],
        current_work: dict[str, str],
        current_ceo: dict[str, str],
    ) -> str | None:
        """Compare a current tool signature against the one stored at save time.
        Returns a short human-readable summary, or None if nothing changed.

        A bucket key that was NEVER recorded in tool_signature (None, not {})
        is treated as "no signature available; skip" -- otherwise pre-round-2
        sessions (which had no `ceo` bucket) would spuriously report every
        current CEO tool as new on resume.
        """
        saved_mgmt = self.data.tool_signature.get("management")
        saved_work = self.data.tool_signature.get("work")
        saved_ceo = self.data.tool_signature.get("ceo")

        def diff(
            saved: dict[str, str] | None, current: dict[str, str]
        ) -> list[str]:
            if saved is None:
                return []  # bucket wasn't recorded; can't diff meaningfully
            lines: list[str] = []
            saved_keys = set(saved)
            current_keys = set(current)
            for name in sorted(current_keys - saved_keys):
                lines.append(f"  + {name} (new since last run)")
            for name in sorted(saved_keys - current_keys):
                lines.append(f"  - {name} (removed since last run)")
            for name in sorted(saved_keys & current_keys):
                if saved[name] != current[name]:
                    lines.append(f"  ~ {name} (updated since last run)")
            return lines

        mgmt_diff = diff(saved_mgmt, current_mgmt)
        work_diff = diff(saved_work, current_work)
        ceo_diff = diff(saved_ceo, current_ceo)
        if not mgmt_diff and not work_diff and not ceo_diff:
            return None

        out = ["Tool changes since this session was paused:"]
        if mgmt_diff:
            out.append("Management tools:")
            out.extend(mgmt_diff)
        if work_diff:
            out.append("Worker tools:")
            out.extend(work_diff)
        if ceo_diff:
            out.append("CEO-only tools:")
            out.extend(ceo_diff)
        return "\n".join(out)
