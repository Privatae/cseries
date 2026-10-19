"""runtime/mcp_names.py — how a remote MCP server's names are spelled on a Host.

One place for the four
shapes, so the capability gate, the turn-type wall, the read classifier, tool
search and the connection service (server/mcp_connections.py) read them the
same way. Pure functions, no imports beyond `re`.

  mcp:<server>/<tool>   a server's tool, run only through `operate`
  mcp:<server>          the key a turn type holds to use that server
  skills/mcp-<server>   the Host's skill for it (`mcp-server: <server>`)
  <server>              lowercase letters, digits and single hyphens, at most
                        40 characters: a skill name and a turn-type key can
                        both carry it
"""
from __future__ import annotations

import re
from typing import Any, Optional

SERVER_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
SERVER_MAX = 40
TOOL_PREFIX = "mcp:"
TYPE_KEY_PREFIX = "mcp:"
SKILL_PREFIX = "mcp-"
# The spec recommends 1-128 letters, digits, `_`, `-` and `.` for a tool name;
# anything else is left out of a server's list (the name goes into a header,
# a search entry and a call).
TOOL_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def is_server_name(name: Any) -> bool:
    return isinstance(name, str) and len(name) <= SERVER_MAX and bool(SERVER_RE.match(name))


def is_mcp_tool_name(name: Any) -> bool:
    """Anything spelled as a server tool — well-formed or not."""
    return isinstance(name, str) and name.startswith(TOOL_PREFIX)


def parse_tool_name(name: Any) -> Optional[tuple[str, str]]:
    """`mcp:<server>/<tool>` -> (server, tool), or None."""
    if not is_mcp_tool_name(name):
        return None
    server, sep, tool = name[len(TOOL_PREFIX):].partition("/")
    if not sep or not is_server_name(server) or not TOOL_RE.match(tool):
        return None
    return server, tool


def tool_name(server: str, tool: str) -> str:
    return f"{TOOL_PREFIX}{server}/{tool}"


def type_key(server: str) -> str:
    return f"{TYPE_KEY_PREFIX}{server}"


def skill_name(server: str) -> str:
    return f"{SKILL_PREFIX}{server}"


__all__ = ["SERVER_RE", "SERVER_MAX", "TOOL_PREFIX", "TYPE_KEY_PREFIX", "SKILL_PREFIX", "TOOL_RE",
           "is_server_name", "is_mcp_tool_name", "parse_tool_name", "tool_name", "type_key",
           "skill_name"]
