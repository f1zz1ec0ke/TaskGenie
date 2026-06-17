from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Awaitable, Callable, Literal

ToolKind = Literal["management", "work", "both", "ceo"]

# A dispatcher receives (agent, args) and returns a result (str or jsonable).
# Workers' tools just need `args`, but for symmetry every dispatcher gets the
# calling agent so MCP wrappers can scope or log per agent.
Dispatcher = Callable[[Any, dict[str, Any]], Awaitable[Any]]


def _schema_hash(schema: dict[str, Any]) -> str:
    """A stable 12-char hash of a tool's schema, used for resume diffs."""
    body = copy.deepcopy(schema)
    # Strip our private fields so a version bump doesn't tautologically change the hash.
    body.get("function", {}).pop("version", None)
    body.get("function", {}).pop("source", None)
    body.get("function", {}).pop("schema_hash", None)
    serial = json.dumps(body, sort_keys=True)
    return hashlib.sha256(serial.encode()).hexdigest()[:12]


def _annotate(schema: dict[str, Any], version: str, source: str) -> dict[str, Any]:
    out = copy.deepcopy(schema)
    fn = out.setdefault("function", {})
    fn["version"] = version
    fn["source"] = source
    fn["schema_hash"] = _schema_hash(out)
    return out


class ToolRegistry:
    """Central registry of all tools available to agents.

    Built-in tools register at import time. MCP tools register at runtime
    after the MCP client connects to its servers. Agents pull their tool list
    via `management_tools()` / `work_tools()`.
    """

    def __init__(self) -> None:
        self._tools: dict[str, dict[str, Any]] = {}
        self._kinds: dict[str, set[str]] = {}
        self._dispatchers: dict[str, Dispatcher] = {}
        # Tools whose name an agent's run loop intercepts directly (complete,
        # report_blocker, hire, assign, ...). The registry still tracks them
        # for schema/version/diff purposes, but no dispatcher is needed.
        self._intercepted: set[str] = set()

    def register(
        self,
        schema: dict[str, Any],
        kind: ToolKind,
        version: str = "1.0",
        source: str = "builtin",
        dispatcher: Dispatcher | None = None,
        intercepted: bool = False,
    ) -> None:
        annotated = _annotate(schema, version=version, source=source)
        name = annotated["function"]["name"]
        if name in self._tools:
            raise ValueError(f"tool {name!r} already registered")
        self._tools[name] = annotated
        kinds: set[str]
        if kind == "both":
            kinds = {"management", "work"}
        elif kind == "ceo":
            kinds = {"ceo"}
        else:
            kinds = {kind}
        self._kinds[name] = kinds
        if dispatcher is not None:
            self._dispatchers[name] = dispatcher
        if intercepted:
            self._intercepted.add(name)

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)
        self._kinds.pop(name, None)
        self._dispatchers.pop(name, None)
        self._intercepted.discard(name)

    def management_tools(self) -> list[dict[str, Any]]:
        return [t for n, t in self._tools.items() if "management" in self._kinds[n]]

    def work_tools(self) -> list[dict[str, Any]]:
        return [t for n, t in self._tools.items() if "work" in self._kinds[n]]

    def ceo_only_tools(self) -> list[dict[str, Any]]:
        return [t for n, t in self._tools.items() if "ceo" in self._kinds[n]]

    def get(self, name: str) -> dict[str, Any] | None:
        return self._tools.get(name)

    def dispatcher(self, name: str) -> Dispatcher | None:
        return self._dispatchers.get(name)

    def is_intercepted(self, name: str) -> bool:
        return name in self._intercepted

    def signature(self, kind: Literal["management", "work", "ceo"]) -> dict[str, str]:
        """A {name: schema_hash} mapping used for resume-time diffs."""
        if kind == "management":
            tools = self.management_tools()
        elif kind == "work":
            tools = self.work_tools()
        else:
            tools = self.ceo_only_tools()
        return {t["function"]["name"]: t["function"]["schema_hash"] for t in tools}

    def describe_tool(self, name: str) -> str:
        t = self._tools.get(name)
        if not t:
            return f"{name} (unknown)"
        fn = t["function"]
        return f"{fn['name']} v{fn.get('version', '?')} [{fn.get('source', '?')}]"


REGISTRY = ToolRegistry()
