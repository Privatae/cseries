"""MCP proxy Unix socket — in-pod CEIGAS forwarder.

The CEIGAS MCP server (spawned by `claude_agent_sdk.query()` as a stdio
subprocess) lives in a separate Python process from the kernel. It needs
to issue authorize / emit_signal / memory_search against the
entity's brain, but the subprocess can't import the kernel's modules
directly. So MCP opens a Unix socket to this proxy inside the same
container; the proxy invokes the in-pod kernel function for each
request and returns the response over the socket.

2026-09-27: that MCP server (server/ceigas/ceigas_mcp.py) is removed. Its
only launcher was the claude-code app's in-container session route, which
is retired, so nothing outside this proxy's own tests connects to the
socket now.

Wire format (newline-delimited JSON):
  MCP → proxy:   {"type": "<msg_type>", "id": "<uuid>", "payload": {...}}
  proxy → MCP:   {"id": "<uuid>", "status": "ok"|"error",
                   "result": {...} | null, "error": "..."}

msg_type values handled (all served by in-pod calls):
  ceigas_authorize        → the operator kill switch (ceigas_kill_switch), 2026-10-17
  ceigas_signal           → accepted, not recorded (2026-10-17)
  ceigas_memory_search    → brain_service.search_memory
  (ceigas_codebase_search removed Ship 3 — was a cross-tenant leak surface)

Ship 6 (2026-05-24): migrated from the old relay client's
register_stream_consumer() pattern (that client has since been removed)
that forwarded every request to the platform. That design (a) was broken because
the relay client was passed as None, fail-closing every MCP write, and (b) was architecturally
wrong — the pod holds the kill-switch state that no remote bridge can
answer for. The
bridge is reserved for ops that genuinely need platform-side work
(code.read, code.grep, k8s.rollout_status, etc.).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

logger = logging.getLogger("privatae.pc.mcp_proxy")

SOCK_PATH = Path(os.environ.get("CEIGAS_BRIDGE_SOCK", "/tmp/ceigas-bridge.sock"))


class MCPProxy:
    """Unix-socket server that handles MCP tool requests by calling
    in-pod kernel functions directly.

    Replaces the legacy BridgeProxy over the old relay client — there's no bridge
    round-trip. The kernel already has every resource the proxy needs:
        pool                — for memory queries
        brain_service       — brain_service.search_memory for entity memory
    """

    def __init__(self, pool, brain_service_module):
        self._pool = pool
        self._brain_service = brain_service_module
        self._server: asyncio.AbstractServer | None = None

    async def start(self):
        # Remove stale socket (tmpfs, single-process owner).
        try:
            SOCK_PATH.unlink()
        except FileNotFoundError:
            pass
        self._server = await asyncio.start_unix_server(
            self._handle, path=str(SOCK_PATH),
        )
        # 0660 = owner+group read/write; container runs as uid 1000 and
        # the MCP subprocess inherits that uid.
        os.chmod(SOCK_PATH, 0o660)
        logger.info("MCP proxy listening on %s (local-binding mode)", SOCK_PATH)

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        try:
            SOCK_PATH.unlink()
        except FileNotFoundError:
            pass

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        request_id = ""
        try:
            line = await reader.readline()
            if not line:
                return
            try:
                req = json.loads(line.decode())
            except json.JSONDecodeError as e:
                return await self._write(writer, {"status": "error", "error": f"bad_json: {e}"})

            msg_type = req.get("type", "")
            request_id = req.get("id", "")
            payload = req.get("payload", {}) or {}

            if not msg_type or not request_id:
                return await self._write(writer, {
                    "id": request_id, "status": "error",
                    "error": "missing type or id",
                })

            if msg_type == "ceigas_authorize":
                result = await self._handle_authorize(payload)
            elif msg_type == "ceigas_signal":
                result = await self._handle_signal(payload)
            elif msg_type == "ceigas_memory_search":
                result = await self._handle_memory_search(payload)
            elif msg_type == "ceigas_code_search":
                result = await self._handle_code_search(payload)
            else:
                result = {"status": "error",
                          "error": f"unknown msg_type: {msg_type!r}"}

            await self._write(writer, {"id": request_id, **result})

        except Exception as e:
            logger.error("proxy handler error (id=%s): %s", request_id, e)
            try:
                await self._write(writer, {
                    "id": request_id, "status": "error", "error": str(e),
                })
            except Exception:
                pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    @staticmethod
    async def _write(writer: asyncio.StreamWriter, payload: dict) -> None:
        writer.write((json.dumps(payload) + "\n").encode())
        await writer.drain()

    def _pinned_entity_id(self) -> int:
        """The pod's OWN entity id — the only identity this proxy may act as.

        The proxy serves an in-pod loopback socket; every request is the
        local entity acting on its own brain. The wire-supplied entity_id
        is therefore NOT a trust input (a compromised MCP subprocess could
        forge another entity's id to get a CEIGAS decision / write an audit
        row under a different identity). We pin to brain_service._entity_id
        — the same canonical pod-identity source memory_handlers / decisions
        use (brain_service._entity_id, falling back to ENTITY_ID).
        """
        try:
            eid = getattr(self._brain_service, "_entity_id", None)
        except Exception:
            eid = None
        if not eid:
            try:
                eid = int(os.environ.get("ENTITY_ID", "0"))
            except (TypeError, ValueError):
                eid = 0
        return int(eid or 0)

    # ── Handlers ────────────────────────────────────────────────

    async def _handle_authorize(self, p: dict) -> dict:
        """ceigas_authorize → the operator kill switch, answered in the pod.

        Returns {allowed, result, reason, conductance} under `result`: refused
        while the switch is set (unless the crossing is a named stop or touches
        core.governance), allowed otherwise. Grants are the capability gate's;
        this door answers only for the switch.

        Payload key for the entity id: `entity_id` (what the removed
        ceigas_mcp.py sent). The legacy `source_entity_id` was the BRIDGE
        protocol wrapper; this proxy unwraps directly so the key matches
        the MCP-side caller.

        The wire-supplied entity_id is IGNORED for authorization — this is
        a loopback proxy and the only identity that may cross is the pod's
        own. We pin to brain_service._entity_id; if the wire carried a
        DIFFERENT non-zero id, that's a forgery attempt and we refuse
        (don't silently re-scope the decision to ourselves either).
        """
        try:
            pinned = self._pinned_entity_id()
            if not pinned:
                return {"status": "error", "error": "pod host id unknown"}
            wire_id = p.get("entity_id") or p.get("source_entity_id")
            if wire_id is not None:
                try:
                    if int(wire_id) and int(wire_id) != pinned:
                        logger.warning(
                            "ceigas_authorize: wire entity_id=%s != pod %d — refusing",
                            wire_id, pinned,
                        )
                        return {
                            "status": "error",
                            "error": "entity_id mismatch — proxy acts only as the pod's own host",
                        }
                except (TypeError, ValueError):
                    return {"status": "error", "error": "entity_id not an int"}
            from ceigas_kill_switch import KILL_SWITCH_REASON, kill_switch_refuses
            if kill_switch_refuses(p.get("source_domain") or "core.entity",
                                   p.get("target_domain") or "",
                                   p.get("operation") or "read"):
                crossing = {"allowed": False, "result": "denied",
                            "reason": KILL_SWITCH_REASON, "conductance": 0.0}
            else:
                crossing = {"allowed": True, "result": "allowed",
                            "reason": "authorized", "conductance": 1.0}
            return {"status": "ok", "result": crossing}
        except Exception as e:
            logger.warning("ceigas_authorize local call failed: %s", e)
            return {"status": "error", "error": f"local authorize failed: {e}"}

    async def _handle_signal(self, p: dict) -> dict:
        """ceigas_signal: accepted and not recorded. The audit table it wrote
        (entity_behavioral_log) went on 2026-10-17; a tool call the Host makes
        is on tool_call_log, written by the runtime that made it."""
        return {"status": "ok", "result": {"recorded": False}}

    async def _handle_memory_search(self, p: dict) -> dict:
        """ceigas_memory_search → brain_service.search_memory locally.

        Audience filtering stays at the brain_service layer; we forward
        the query string and limit. Returns `{"results": [...]}` on
        success."""
        try:
            query = (p.get("query") or "").strip()
            if not query:
                return {"status": "error", "error": "query required"}
            limit = int(p.get("limit") or 10)
            results = await self._brain_service.search_memory(
                query=query, limit=limit,
            )
            return {"status": "ok", "result": {"results": results}}
        except Exception as e:
            logger.warning("ceigas_memory_search local query failed: %s", e)
            return {"status": "error", "error": f"memory search failed: {e}"}

    async def _handle_code_search(self, p: dict) -> dict:
        """ceigas_code_search → in-pod hybrid code search over the entity's named
        projects (pgvector + tsvector in the pod's Postgres; embed + rerank on the
        configured lane). Runs in the
        main process, so the searcher's bridge client is live. Returns
        {"result": {"results": [...]}}."""
        try:
            from codesearch import store, searcher
            project = (p.get("project") or "").strip()
            query = (p.get("query") or "").strip()
            if not project or not query:
                return {"status": "error", "error": "project + query required"}
            results = await searcher.search(
                store.connect(), project=project, query=query, limit=int(p.get("limit") or 10))
            return {"status": "ok", "result": {"results": results}}
        except Exception as e:
            logger.warning("ceigas_code_search local query failed: %s", e)
            return {"status": "error", "error": f"code search failed: {e}"}


_proxy: MCPProxy | None = None


async def start_proxy(pool, brain_service_module) -> MCPProxy:
    """Construct + start the in-pod MCP proxy. Called once during
    server lifespan startup, after brain_pool is ready."""
    global _proxy
    _proxy = MCPProxy(pool, brain_service_module)
    await _proxy.start()
    return _proxy


async def stop_proxy() -> None:
    global _proxy
    if _proxy is not None:
        await _proxy.stop()
        _proxy = None
