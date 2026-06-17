"""Final-answer artifact writer.

On task completion, save the CEO's report to three on-disk forms inside the
session directory:
- final.md   -- the report verbatim (already markdown)
- final.html -- markdown rendered to a self-contained HTML page
- final.json -- structured record (task, report, stats, agent tree, history)

Plus an optional --output PATH copy in a user-chosen location, with the
format inferred from the file extension.
"""

from __future__ import annotations

import datetime as _dt
import html as _htmllib
import json
import shutil
from pathlib import Path
from typing import Any

from taskgenie.agent import Agent
from taskgenie.session import Session

_HTML_CSS = """\
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       max-width: 760px; margin: 2.5em auto; padding: 0 1em;
       line-height: 1.55; color: #1d1d1f; }
h1, h2, h3 { line-height: 1.25; margin-top: 1.6em; }
h1 { border-bottom: 1px solid #ddd; padding-bottom: 0.3em; }
code { background: #f4f4f5; padding: 0.15em 0.4em; border-radius: 3px;
       font-size: 0.92em; }
pre  { background: #f4f4f5; padding: 1em; border-radius: 6px;
       overflow-x: auto; font-size: 0.9em; }
pre code { background: none; padding: 0; }
blockquote { border-left: 3px solid #ddd; margin: 1em 0;
             padding-left: 1em; color: #555; }
table { border-collapse: collapse; }
th, td { border: 1px solid #ddd; padding: 0.4em 0.7em; }
hr  { border: none; border-top: 1px solid #ddd; margin: 2em 0; }
.taskgenie-meta { color: #888; font-size: 0.85em; margin-bottom: 2em;
                  border-bottom: 1px dashed #ddd; padding-bottom: 1em; }
"""


def _render_markdown(text: str) -> str:
    """Markdown -> HTML body. markdown-it-py is a transitive dep via rich."""
    try:
        from markdown_it import MarkdownIt

        md = MarkdownIt("commonmark", {"breaks": True, "html": False})
        return md.render(text)
    except Exception:
        # Fall back to escaped <pre> so we never lose the report.
        return f"<pre>{_htmllib.escape(text)}</pre>"


def _wrap_html(title: str, body_html: str, meta: dict[str, str]) -> str:
    meta_html = " &middot; ".join(
        f"<span><b>{_htmllib.escape(k)}:</b> {_htmllib.escape(v)}</span>"
        for k, v in meta.items()
    )
    return (
        "<!doctype html>\n"
        "<html lang='en'><head><meta charset='utf-8'>"
        f"<title>{_htmllib.escape(title)}</title>"
        f"<style>{_HTML_CSS}</style></head><body>"
        f"<div class='taskgenie-meta'>{meta_html}</div>"
        f"{body_html}"
        "</body></html>"
    )


def _safe_jsonable(obj: Any) -> Any:
    """Recursively coerce obj into a JSON-serialisable form.

    The strict `json` module rejects:
      - dict keys that aren't str/int/float/bool/None (e.g. tuple)
      - values like bytes, datetime, set, dataclass instances

    Real runs have surfaced "keys must be str, ... not tuple" failures
    when something deep in the stats/agent tree has a non-string key.
    Rather than crash the whole final.json write and lose the bundle,
    sanitise: stringify problematic keys, recurse through containers,
    fall back to `repr()` for anything else exotic. The lossy
    conversion is preferable to losing the artifact.
    """
    # Primitives JSON natively supports.
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            # Stringify EVERY non-str key explicitly so we don't rely on
            # json's silent coercion. Without this, `{True: 'a', 'true': 'b'}`
            # and `{1: 'a', '1': 'b'}` collide on the JSON side (both keys
            # serialize to the same string) and one value is silently
            # dropped. Better to make the coercion visible.
            if isinstance(k, str):
                safe_k = k
            elif k is None:
                safe_k = "null"
            elif isinstance(k, bool):
                safe_k = "true" if k else "false"
            elif isinstance(k, (int, float)):
                safe_k = str(k)
            else:
                safe_k = repr(k)
            out[safe_k] = _safe_jsonable(v)
        return out
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [_safe_jsonable(x) for x in obj]
    # Last resort: stringify so json.dumps doesn't blow up.
    try:
        return str(obj)
    except Exception:
        return repr(obj)


def _agent_tree(ceo: Agent) -> list[dict[str, Any]]:
    """Snapshot every agent: id, name, role, status, parent, follow_ups, report."""
    nodes: list[dict[str, Any]] = []
    for a in Agent.REGISTRY.values():
        # Pull the agent's final report from its last "complete" message if any.
        report = ""
        for m in reversed(a.messages):
            if m.get("role") == "assistant" and isinstance(m.get("tool_calls"), list):
                for tc in m["tool_calls"]:
                    fn = tc.get("function", {})
                    if fn.get("name") == "complete":
                        try:
                            report = json.loads(fn.get("arguments", "{}")).get(
                                "report", ""
                            )
                        except json.JSONDecodeError:
                            pass
                        break
                if report:
                    break
        nodes.append(
            {
                "id": a.id,
                "name": a.name,
                "role_type": a.role_type,
                "status": a.status,
                "depth": a.depth,
                "parent_id": a.parent.id if a.parent else None,
                "child_ids": [c.id for c in a.children],
                "follow_ups": list(a.follow_ups),
                "blocker_reason": a.blocker_reason,
                "blocker_needs": a.blocker_needs,
                "report": report,
            }
        )
    return nodes


def write_final(
    session: Session,
    ceo: Agent,
    report: str,
    stats: dict[str, Any] | None,
    extra_output: Path | None = None,
) -> dict[str, Path]:
    """Write final.{md,html,json} into the session dir.

    If extra_output is given, also writes a copy there with format inferred
    from its extension (.md, .html, or .json; anything else gets .md
    appended).

    Returns {format: path} for every artifact written, so the caller can
    print the paths.
    """
    session_dir = session.root_dir
    session_dir.mkdir(parents=True, exist_ok=True)

    md_path = session_dir / "final.md"
    html_path = session_dir / "final.html"
    json_path = session_dir / "final.json"

    md_path.write_text(report or "(no answer)\n", encoding="utf-8")

    meta = {
        "task": session.data.task[:120],
        "session": session.session_id,
        "completed": _dt.datetime.now().isoformat(timespec="seconds"),
    }
    html_body = _render_markdown(report or "_(no answer)_")
    html_doc = _wrap_html(
        title=f"TaskGenie — {session.data.task[:60]}",
        body_html=html_body,
        meta=meta,
    )
    html_path.write_text(html_doc, encoding="utf-8")

    bundle: dict[str, Any] = {
        "task": session.data.task,
        "session_id": session.session_id,
        "started_at": session.data.started_at,
        "completed_at": meta["completed"],
        "report": report,
        "stats": stats or {},
        "ceo_follow_ups": list(ceo.follow_ups),
        "agents": _agent_tree(ceo),
        "history": list(session.data.history),
    }
    json_path.write_text(
        json.dumps(_safe_jsonable(bundle), indent=2, default=str),
        encoding="utf-8",
    )

    paths: dict[str, Path] = {"md": md_path, "html": html_path, "json": json_path}

    if extra_output is not None:
        dest = Path(extra_output).expanduser()
        suffix = dest.suffix.lower()
        if suffix == ".html":
            dest.write_text(html_doc, encoding="utf-8")
        elif suffix == ".json":
            dest.write_text(json_path.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            if not suffix:
                dest = dest.with_suffix(".md")
            shutil.copyfile(md_path, dest)
        paths["extra"] = dest

    return paths
