"""A stdio MCP server over VoxInk's local control API (`PROJECT_SPEC.md` §14.7).

Runs as `python -m voxink_mcp`. It speaks the Model Context Protocol over
stdin/stdout without a framework dependency: the protocol is JSON-RPC 2.0 with
a handful of methods, and keeping it hand-written keeps the offline promise
free of another package. Every tool goes through the shared `ControlClient`,
so what an agent can do here is exactly what `voxink` can do, under the same
grant.
"""

from .server import serve, TOOLS

__all__ = ["serve", "TOOLS"]
