"""Tool package: register builtins on import; expose the registry.

MCP tools are added at runtime by taskgenie.mcp_client.
"""

from __future__ import annotations

# Importing these modules registers their tools with REGISTRY as a side effect.
from taskgenie.tools import management as _management  # noqa: F401
from taskgenie.tools import work as _work  # noqa: F401
from taskgenie.tools.registry import REGISTRY

__all__ = ["REGISTRY"]
