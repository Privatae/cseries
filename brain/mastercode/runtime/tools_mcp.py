"""The mcp tools: their handlers and the helpers only they use.

Moved out of runtime/tools.py, which re-exports every name defined here. A tool's
category in the tool registry (public_tool_registry `_meta.category`) names the module
its handler lives in."""

import logging

from mastercode.runtime.tool_call_ledger import refused as _refused, refused_if_caller as _refused_if_caller

log = logging.getLogger("cseries.mastercode.entity_runtime")


_MCP_OPS = ("servers", "tools", "connect", "disconnect", "watch", "unwatch")


async def _exec_mcp_connections(args: dict, entity_id: int, pool) -> dict:
    """`mcp_connections` — the Host's remote MCP servers (server/mcp_connections.py).
    servers and tools are reads; connect drafts a connection and its egress
    allow rule, contacting nothing, for the person to approve in the Network
    app; disconnect revokes both. Runs after the capability gate (META_TOOL_CAPS
    4|32, servers/tools at 1) and the turn type (a typed turn names only keys it
    holds, and needs `mcp:<name>` for a server's tools).

    watch is started by a Host turn's operate branch (session.py, which holds
    the turn for it and knows the thread its answer goes to); it never runs
    here. unwatch stops one (runtime/mcp_watch.py)."""
    a = args or {}
    op = str(a.get("op") or "servers").strip().lower()
    if op not in _MCP_OPS:
        return _refused(f"op must be one of {', '.join(_MCP_OPS)}")
    if pool is None:
        return {"status": "error", "result": "mcp_connections: the Host's database is not ready"}
    if op == "watch":
        return _refused("A watch is started from your own turn: operate(intent, "
                        "tool='mcp_connections', args={op: 'watch', tool, args, while_text}). "
                        "An agent cannot start one.")
    if op == "unwatch":
        from mastercode.runtime import mcp_watch
        return await mcp_watch.unwatch(pool, entity_id, a.get("task_id"))
    from server import mcp_client, mcp_connections as mc
    try:
        if op == "servers":
            return {"status": "ok", "result": await mc.servers(pool, entity_id)}
        if op == "tools":
            return {"status": "ok", "result": await mc.tools(pool, entity_id, a.get("name"))}
        if op == "connect":
            return {"status": "ok", "result": await mc.connect(
                pool, entity_id, name=a.get("name"), url=a.get("url"), key=a.get("key"),
                header=a.get("header"))}
        return {"status": "ok", "result": await mc.disconnect(pool, entity_id, name=a.get("name"))}
    except mc.McpConnectionError as e:
        return _refused(str(e))
    except ValueError as e:              # nac_propose's validation of the drafted rule
        return _refused(str(e))
    except mcp_client.McpError as e:
        # The server's own refusal is the call's to act on; a gate, network
        # or timeout failure is the lane's.
        if e.caller_refusal:
            return _refused(f"mcp_connections {op}: {e}")
        return {"status": "error", "result": f"mcp_connections {op}: {e}"}
    except Exception as e:  # noqa: BLE001
        log.exception("mcp_connections %s failed (entity=%s)", op, entity_id)
        return _refused_if_caller(e, f"mcp_connections {op} failed: {getattr(e, 'detail', None) or e}")
