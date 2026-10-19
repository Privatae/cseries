#!/usr/bin/env python3
"""Host MCP stdio shim — the harness pod's line to its own host.

WHY (2026-09-03): a harness pod has no public
DNS (its resolver serves cluster names only) and one route out, the node
squid. A vendor CLI's own fetch tool that resolves the hostname itself before
using the proxy — kimi's FetchURL — dies at getaddrinfo (EAI_AGAIN) and the
harness carries on blind; that run built the bot UI framing "defensively"
because it could not read one vite.config.ts. The fix is structural: give the
harness a tool that works, backed by the host's own governed reader.

WHAT: a stdio MCP server the vendor CLI/SDK spawns. Every JSON-RPC line on
stdin is POSTed verbatim to the entity kernel's /mcp (CSERIES_ENTITY_MCP_URL,
injected by the operator: the entity Service in this namespace on :8800), with
the pod's control capability (PRIVATAE_HARNESS_CAP, or CSERIES_HARNESS_CAP on pool pods; the same F-013 secret the
dispatcher presents to THIS pod) as `X-CSeries-Harness-Cap`. The kernel verifies
it against the capabilities it currently holds for woken pods and grants the
`fetch` scope only — read_url and nothing else. The response comes back on
stdout. No secret is written anywhere: both values are already in this pod's
process environment, and this shim inherits them.

Sibling of system-apps/claude-code/bin/synaptive_mcp_shim.py (the in-pod
loopback shim, INTERNAL_API_SECRET auth); this one is generic over the
harnesses (claude-code SDK mcp_servers, codex `-c mcp_servers.*`, kimi
$KIMI_CODE_HOME/mcp.json — see host_mcp.py) and stdlib-only, because harness
images ship no extra Python packages.

Run by hand inside a harness pod:
  echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | python3 host_mcp_shim.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

ENV_URL = "CSERIES_ENTITY_MCP_URL"
ENV_CAP = "PRIVATAE_HARNESS_CAP"
# Pool pods still carry the capability under the older name.
ENV_CAP_OLD = "CSERIES_HARNESS_CAP"
CAP_HEADER = "X-CSeries-Harness-Cap"
# The kernel's read_url reads a page through curl_cffi + squid; a slow origin
# plus extraction can take a while, and a cut here would surface to the model
# as a failed tool rather than a slow one.
TIMEOUT_S = 180


def _send(url: str, cap: str, req_obj: dict) -> dict | None:
    """POST one JSON-RPC request; the parsed response, or None for a
    notification the kernel answered 204."""
    data = json.dumps(req_obj).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"content-type": "application/json", CAP_HEADER: cap},
    )
    try:
        # The URL is in-cluster (NO_PROXY covers .svc); an explicit empty
        # proxy map keeps a stray HTTPS_PROXY from ever routing it to squid,
        # which would 403 the platform-internal destination.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=TIMEOUT_S) as resp:
            if resp.status == 204:
                return None
            body = resp.read()
    except urllib.error.HTTPError as e:
        body = e.read() or b""
        try:
            err = json.loads(body.decode("utf-8"))
            if isinstance(err, dict) and "error" in err and "jsonrpc" in err:
                return err  # the kernel's own JSON-RPC error envelope
            detail = err.get("detail") or err.get("error") or f"HTTP {e.code}"
        except Exception:
            detail = f"HTTP {e.code}"
        return {"jsonrpc": "2.0", "id": req_obj.get("id"),
                "error": {"code": -32000, "message": f"host shim: {detail}"}}
    except Exception as e:  # noqa: BLE001 — every failure must reach the model as JSON
        return {"jsonrpc": "2.0", "id": req_obj.get("id"),
                "error": {"code": -32000, "message": f"host shim transport: {e}"}}
    try:
        return json.loads(body.decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        return {"jsonrpc": "2.0", "id": req_obj.get("id"),
                "error": {"code": -32700, "message": f"host shim parse: {e}"}}


def _emit(obj) -> None:
    if obj is None:
        return
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main() -> int:
    url = os.environ.get(ENV_URL, "").strip()
    # Stripped before the fallback, as in cseries_sidecar._env: a blank new value
    # does not hide the capability under the older name.
    cap = ((os.environ.get(ENV_CAP) or "").strip()
           or (os.environ.get(ENV_CAP_OLD) or "").strip())
    if not url or not cap:
        # Loud, not silent: a harness that starts this shim without its two
        # inputs has an operator/image mismatch, and the model should see the
        # tool absent rather than a server that answers nothing.
        sys.stderr.write(
            f"host_mcp_shim: {ENV_URL} and {ENV_CAP} (or {ENV_CAP_OLD}) must both be set — "
            "refusing to start\n")
        return 2
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
        except json.JSONDecodeError as e:
            _emit({"jsonrpc": "2.0", "id": None,
                   "error": {"code": -32700, "message": f"parse error: {e}"}})
            continue
        _emit(_send(url, cap, req))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
