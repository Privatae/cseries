#!/usr/bin/env python3
"""Gate for the harness→host fetch channel.

The defect (factory-build-45): a harness pod has no public DNS —
its resolver serves cluster names only — so kimi's own FetchURL failed at
getaddrinfo (EAI_AGAIN raw.githubusercontent.com) before it could use the
proxy, and the harness built the bot UI framing blind. The channel that fixes
it is STRUCTURAL: the harness gets an MCP server (`host`) that forwards to its
own entity's kernel /mcp, authenticated with the per-pod control capability
the dispatcher already holds, and the kernel grants exactly one tool
(read_url, scope `fetch`) through the host's governed reader.

What is EXECUTED here, not grepped:
  1. the shim: spawned as a real subprocess against a local HTTP server that
     plays the kernel — the capability header arrives, the response comes
     back on stdout, a missing env refuses to start
  2. the kernel: _auth_or_401 with a CURRENT capability → scopes ["fetch"];
     a stale one → 401; tools/list under that scope is read_url alone; a
     tools/call reaches url_reader.read (mocked) and the envelope carries
     its result; scopes without `fetch` cannot call it
  3. the wiring: codex argv gains the `-c mcp_servers.host.*` overrides only
     when the env is present; kimi's env builder writes $HOME/.kimi-code/
     mcp.json naming the shim (and nothing else) only when present;
     claude-code-harness's _sdk_options carries mcp_servers only when present
     (or, when the SDK is not importable here, the source wiring is pinned)

Run: APP_ROOT=<repo> python3 scripts/test_harness_host_fetch.py
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from gate_loopback import ClientClosesFirst
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path[:0] = [str(_ROOT), str(_ROOT / "brain"), str(_ROOT / "system-apps"),
                str(_ROOT / "cli")]

FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"[host-fetch] ok   {label}")
    else:
        FAILED.append(label)
        print(f"[host-fetch] FAIL {label}" + (f" — {detail}" if detail else ""))


CAP = "cap-for-this-pod-generation-0123456789"


# ── 1. the shim, as a process ──────────────────────────────────────────────

class _FakeKernel(ClientClosesFirst, BaseHTTPRequestHandler):
    # ClientClosesFirst, on a threading server: a server that closes first
    # leaves a TIME_WAIT on its port per request, which CI's firewall refuses
    # a later request onto (scripts/gate_loopback.py).
    seen: list[dict] = []

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        _FakeKernel.seen.append({"path": self.path,
                                 "cap": self.headers.get("X-CSeries-Harness-Cap"),
                                 "body": body})
        if self.headers.get("X-CSeries-Harness-Cap") != CAP:
            out = {"jsonrpc": "2.0", "id": None,
                   "error": {"code": -32600, "message": "harness capability is not current"}}
            code = 401
        else:
            out = {"jsonrpc": "2.0", "id": body.get("id"),
                   "result": {"tools": [{"name": "read_url"}]}}
            code = 200
        data = json.dumps(out).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):  # silence
        pass


def _shim_tests() -> None:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeKernel)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    shim = _ROOT / "system-apps" / "_harness_runtime" / "host_mcp_shim.py"
    url = f"http://127.0.0.1:{port}/mcp"
    # The shim reads PRIVATAE_HARNESS_CAP first; an ambient one would win.
    env = {**{k: v for k, v in os.environ.items() if k != "PRIVATAE_HARNESS_CAP"},
           "CSERIES_ENTITY_MCP_URL": url, "CSERIES_HARNESS_CAP": CAP,
           # A squid in the ambient env must not capture the in-cluster call.
           "HTTPS_PROXY": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9"}
    req = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/list"}) + "\n"
    p = subprocess.run([sys.executable, str(shim)], input=req, capture_output=True,
                       text=True, env=env, timeout=30)
    lines = [l for l in p.stdout.splitlines() if l.strip()]
    resp = json.loads(lines[0]) if lines else {}
    check("shim: forwards the JSON-RPC line to the kernel URL with the capability header",
          _FakeKernel.seen and _FakeKernel.seen[-1]["cap"] == CAP
          and _FakeKernel.seen[-1]["path"] == "/mcp"
          and _FakeKernel.seen[-1]["body"].get("method") == "tools/list",
          repr(_FakeKernel.seen[-1:]))
    check("shim: the kernel's response comes back on stdout, id preserved",
          resp.get("id") == 7 and resp.get("result", {}).get("tools") == [{"name": "read_url"}],
          p.stdout[:300] + p.stderr[:300])
    check("shim: an ambient HTTPS_PROXY is bypassed for the in-cluster URL",
          p.returncode == 0 and "transport" not in p.stdout, p.stdout[:300])

    bad = {**env, "CSERIES_HARNESS_CAP": "stale"}
    p2 = subprocess.run([sys.executable, str(shim)], input=req, capture_output=True,
                        text=True, env=bad, timeout=30)
    r2 = json.loads(p2.stdout.splitlines()[0]) if p2.stdout.strip() else {}
    check("shim: a 401 from the kernel is relayed as a JSON-RPC error, not a crash",
          p2.returncode == 0 and "error" in r2
          and "not current" in r2["error"]["message"], p2.stdout[:300])

    p3 = subprocess.run([sys.executable, str(shim)], input=req, capture_output=True,
                        text=True, env={k: v for k, v in env.items()
                                        if k != "CSERIES_HARNESS_CAP"}, timeout=30)
    check("shim: refuses to start without the capability (loud, exit 2)",
          p3.returncode == 2 and "CSERIES_HARNESS_CAP" in p3.stderr, p3.stderr[:200])

    # A bastion coder run passes only PRIVATAE_HARNESS_CAP; pool pods only the
    # older name. The new name wins when both are set; a blank one falls back.
    base = {k: v for k, v in env.items() if k not in ("CSERIES_HARNESS_CAP", "PRIVATAE_HARNESS_CAP")}
    for label, extra, want in (
            ("PRIVATAE_HARNESS_CAP alone", {"PRIVATAE_HARNESS_CAP": CAP}, CAP),
            ("both set: PRIVATAE_ wins", {"PRIVATAE_HARNESS_CAP": CAP, "CSERIES_HARNESS_CAP": "old"}, CAP),
            ("blank PRIVATAE_ falls back", {"PRIVATAE_HARNESS_CAP": "  ", "CSERIES_HARNESS_CAP": CAP}, CAP)):
        n = len(_FakeKernel.seen)
        p4 = subprocess.run([sys.executable, str(shim)], input=req, capture_output=True,
                            text=True, env={**base, **extra}, timeout=30)
        check(f"shim: {label} -> that capability is presented",
              p4.returncode == 0 and len(_FakeKernel.seen) > n
              and _FakeKernel.seen[-1]["cap"] == want,
              f"rc={p4.returncode} {p4.stderr[:200]} seen={_FakeKernel.seen[n:]}")
    srv.shutdown()


# ── 2. the kernel ──────────────────────────────────────────────────────────

async def _kernel_tests() -> None:
    from server.routes import mcp as M
    from server.routes import review as R

    R._control_cap.clear()
    R._remember_control_cap("kimi", "http://harness-kimi.host-ns.svc:8847", CAP)
    check("review.control_cap_is_current: the capability of a woken pod verifies",
          R.control_cap_is_current(CAP) is True)
    check("review.control_cap_is_current: a stale/other value does not",
          R.control_cap_is_current("stale") is False
          and R.control_cap_is_current("") is False
          and R.control_cap_is_current(None) is False)

    auth = await M._auth_or_401(None, harness_cap=CAP)
    check("kernel auth: a current capability grants scopes ['fetch'] only, not internal",
          auth.get("scopes") == ["fetch"] and not auth.get("_internal")
          and auth.get("_harness") is True, repr(auth))
    try:
        await M._auth_or_401(None, harness_cap="stale")
        check("kernel auth: a non-current capability is refused (401)", False, "no raise")
    except Exception as e:  # noqa: BLE001
        check("kernel auth: a non-current capability is refused (401)",
              getattr(e, "status_code", None) == 401, repr(e))

    R._forget_control_cap("kimi")
    check("kernel auth: release forgets the capability, so it stops verifying",
          R.control_cap_is_current(CAP) is False)
    R._remember_control_cap("kimi", "http://harness-kimi.host-ns.svc:8847", CAP)

    names = [t["name"] for t in M._tools_for_scopes(["fetch"])]
    check("kernel catalog: the fetch scope advertises read_url and nothing else",
          names == ["read_url"], repr(names))
    check("kernel catalog: read_url is in TOOLS and TOOL_HANDLERS under scope fetch",
          any(t["name"] == "read_url" for t in M.TOOLS)
          and M.TOOL_HANDLERS.get("read_url", (None, None))[1] == "fetch")

    from mastercode.runtime import url_reader
    calls: list = []

    async def _fake_read(url, fmt="markdown", max_chars=None, offset=None):
        calls.append((url, fmt, max_chars))
        return {"status": "ok", "result": {"url": url, "content": "hello", "chars": 5}}
    orig = url_reader.read
    url_reader.read = _fake_read
    try:
        auth = await M._auth_or_401(None, harness_cap=CAP)
        resp = await M._handle_one(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "read_url",
                        "arguments": {"url": "https://example.org/x", "max_chars": 9}}},
            None, is_internal=False, auth=auth)
        text = (resp.get("result") or {}).get("content", [{}])[0].get("text", "")
        check("kernel call: read_url reaches url_reader.read with the harness's args",
              calls == [("https://example.org/x", "markdown", 9)], repr(calls))
        check("kernel call: the reader's envelope rides back in the tool result",
              "hello" in text and resp["result"].get("isError") is False, repr(resp)[:300])
        denied = await M._handle_one(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "read_url", "arguments": {"url": "https://example.org"}}},
            None, is_internal=True,
            auth={"id": 0, "entity_id": 9003, "scopes": ["shape", "recall", "signal"],
                  "_internal": True})
        check("kernel call: a caller without the fetch scope cannot call read_url",
              "error" in denied and len(calls) == 1, repr(denied)[:200])
    finally:
        url_reader.read = orig
        R._control_cap.clear()


# ── 3. the wiring ──────────────────────────────────────────────────────────

def _wiring_tests() -> None:
    from _harness_runtime import host_mcp, specs

    for k in ("CSERIES_ENTITY_MCP_URL", "CSERIES_HARNESS_CAP", "PRIVATAE_HARNESS_CAP"):
        os.environ.pop(k, None)
    check("host_mcp: no env → no server, no overrides, no file",
          host_mcp.server_config() is None and host_mcp.mcp_servers() == {}
          and host_mcp.codex_config_overrides() == []
          and host_mcp.write_kimi_mcp_json(Path(tempfile.mkdtemp())) is None)
    argv_off = specs.CODEX.build_argv(specs.CODEX, "fix", "/w/x", None, None)
    check("codex argv: unchanged when the channel is absent",
          not any(a.startswith("mcp_servers.") for a in argv_off), repr(argv_off))

    os.environ["CSERIES_ENTITY_MCP_URL"] = "http://host.host-ns.svc.cluster.local:8800/mcp"
    os.environ["PRIVATAE_HARNESS_CAP"] = CAP
    check("host_mcp: PRIVATAE_HARNESS_CAP alone enables the channel (bastion coder run)",
          host_mcp.enabled())
    os.environ["PRIVATAE_HARNESS_CAP"] = "  "
    check("host_mcp: a blank PRIVATAE_HARNESS_CAP alone does not enable it", not host_mcp.enabled())
    os.environ["CSERIES_HARNESS_CAP"] = CAP
    check("host_mcp: a blank PRIVATAE_HARNESS_CAP falls back to CSERIES_HARNESS_CAP", host_mcp.enabled())
    os.environ.pop("PRIVATAE_HARNESS_CAP")
    cfg = host_mcp.server_config()
    check("host_mcp: with env → a stdio server that runs the shim, carrying NO secret",
          cfg and cfg["args"] == [str(host_mcp.shim_path())] and "env" not in cfg
          and CAP not in json.dumps(cfg), repr(cfg))
    check("host_mcp: the shim it names exists and is the stdlib shim",
          host_mcp.shim_path().is_file()
          and "X-CSeries-Harness-Cap" in host_mcp.shim_path().read_text())

    argv_on = specs.CODEX.build_argv(specs.CODEX, "fix", "/w/x", None, None)
    ov = [a for a in argv_on if a.startswith("mcp_servers.host.")]
    check("codex argv: `-c mcp_servers.host.command/args` dotted overrides, prompt still last",
          len(ov) == 2 and argv_on[-1] == "fix"
          and argv_on[argv_on.index(ov[0]) - 1] == "-c"
          and json.loads(ov[1].split("=", 1)[1]) == [str(host_mcp.shim_path())],
          repr(argv_on))

    tmp = Path(tempfile.mkdtemp(prefix="kimi-home-"))
    os.environ["KIMI_HOME"] = str(tmp)
    env = specs.KIMI.build_env(specs.KIMI, {"base_url": "https://x", "api_key": "k",
                                            "type": "kimi"})
    mj = tmp / ".kimi-code" / "mcp.json"
    doc = json.loads(mj.read_text()) if mj.is_file() else {}
    check("kimi env: writes $HOME/.kimi-code/mcp.json naming the host shim (HOME = brand home)",
          env.get("HOME") == str(tmp) and doc.get("mcpServers", {}).get("host") == cfg,
          repr(doc))
    check("kimi env: the file carries no secret, and the workspace project tree is untouched",
          CAP not in mj.read_text() and "k" not in json.dumps(doc.get("mcpServers", {}).get("host", {}).get("env", {}))
          and not (tmp / ".mcp.json").exists())
    # Merge, not clobber: a pre-existing server survives.
    mj.write_text(json.dumps({"mcpServers": {"other": {"command": "x", "args": []}}}))
    specs.KIMI.build_env(specs.KIMI, {"base_url": "https://x", "api_key": "k", "type": "kimi"})
    doc2 = json.loads(mj.read_text())
    check("kimi env: merges into an existing mcp.json rather than replacing it",
          set(doc2["mcpServers"]) == {"other", "host"}, repr(doc2))

    # claude-code-harness: execute _sdk_options when the SDK imports, else pin the source.
    src = (_ROOT / "system-apps" / "claude-code-harness" / "bin" / "main.py").read_text()
    try:
        import importlib.util
        os.environ.setdefault("PORT", "8846")
        spec = importlib.util.spec_from_file_location(
            "cch_main", _ROOT / "system-apps" / "claude-code-harness" / "bin" / "main.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        if getattr(mod, "_SDK_AVAILABLE", False):
            opts = mod._sdk_options("/w/x", None, None)
            got = getattr(opts, "mcp_servers", None)
            check("claude-code-harness: _sdk_options carries the host server when the env is present",
                  isinstance(got, dict) and got.get("host") == cfg, repr(got))
            os.environ.pop("CSERIES_HARNESS_CAP")
            opts2 = mod._sdk_options("/w/x", None, None)
            check("claude-code-harness: no server when the env is absent",
                  not getattr(opts2, "mcp_servers", None))
            os.environ["CSERIES_HARNESS_CAP"] = CAP
        else:
            raise ImportError("claude_agent_sdk not importable here")
    except Exception as e:  # noqa: BLE001 — pin the wiring lexically instead
        print(f"[host-fetch] note: claude-code-harness executed check skipped ({e}); pinning source")
        check("claude-code-harness: _sdk_options wires host_mcp.mcp_servers() into mcp_servers (source)",
              "host_mcp.mcp_servers()" in src
              and 'options_kwargs["mcp_servers"] = _servers' in src
              and "from _harness_runtime import host_mcp" in src)
    for k in ("CSERIES_ENTITY_MCP_URL", "CSERIES_HARNESS_CAP", "PRIVATAE_HARNESS_CAP", "KIMI_HOME"):
        os.environ.pop(k, None)


def _middleware_tests() -> None:
    """The REAL LocalhostOnlyMiddleware, driven in-process from addresses
    outside the container. The kernel tests above start on the far side of
    it; this pins that the middleware gives the harness cap no way in."""
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient
    from server.middleware.localhost_only import LocalhostOnlyMiddleware

    async def _mcp(request):
        return JSONResponse({"reached": True})

    app = Starlette(routes=[Route("/mcp", _mcp, methods=["POST"]),
                            Route("/api/brain/x", _mcp, methods=["POST"])])
    app.add_middleware(LocalhostOnlyMiddleware)

    def _post(path, headers=None, ip="fd00:10:244::1:2"):
        # TestClient lets us set the peer address the middleware reads.
        c = TestClient(app, client=(ip, 43210))
        return c.post(path, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                      headers=headers or {})

    # A standalone Host has no harness pods: the cap header is no exemption,
    # and /mcp from any address but loopback needs the owner's session.
    r = _post("/mcp", {"X-CSeries-Harness-Cap": "cap-from-wake"})
    check("middleware: /mcp from a non-loopback address WITH the harness cap header is 403",
          r.status_code == 403, f"{r.status_code} {r.text[:120]}")
    r = _post("/mcp", {"X-CSeries-Harness-Cap": "cap-from-wake"}, ip="10.244.1.7")
    check("middleware: same over IPv4", r.status_code == 403, f"{r.status_code}")
    r = _post("/mcp")
    check("middleware: /mcp from a non-loopback address WITHOUT the header is 403",
          r.status_code == 403, f"{r.status_code}")
    r = _post("/api/brain/x", {"X-CSeries-Harness-Cap": "cap-from-wake"})
    check("middleware: the cap header opens no other route either",
          r.status_code == 403, f"{r.status_code}")

def main() -> int:
    _shim_tests()
    asyncio.run(_kernel_tests())
    _wiring_tests()
    _middleware_tests()
    if FAILED:
        print(f"[host-fetch] {len(FAILED)} FAILED: {FAILED}")
        return 1
    print("[host-fetch] all passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
