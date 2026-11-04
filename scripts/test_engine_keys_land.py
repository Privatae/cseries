#!/usr/bin/env python3
"""A coding engine's key reaches the engine at once, and Kimi lists the models
of the endpoint it runs on.

  c. server/routes/vault.py store_secret: a harness-<id> key written in Keys
     writes the engine's credential file in the same request
     (harness_keys.materialize_from_keys), not at the next 300 s pass, and
     GET /api/inference/routing's engine_keys names the engine, which the
     Orchestrator compares when its window is focused again (so the engine's
     row reads connected without reopening the window)
  d. server/routes/inference.py: the Kimi engine's model list is read from
     the endpoint its spec in Keys names (a provider spec's base_url and key,
     or the base a bare key authenticates on), not from Moonshot's registry
     base; the coding matrix reads it before the registry provider's list.
     Never from the engine's file on disk (a run can rewrite it), and only an
     https address on the internet is asked, at the address that was checked

Container-free: the vault and the file paths stubbed; a stand-in
OpenAI-compatible /models on 127.0.0.1 (real HTTP).
APP_ROOT=<repo> python3 scripts/test_engine_keys_land.py
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import tempfile
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain"), os.path.join(_ROOT, "scripts")]
from gate_loopback import ClientClosesFirst  # noqa: E402

from server.network import harness_keys, harness_state  # noqa: E402
from server.routes import inference as inf  # noqa: E402
from server.routes import vault as vault_routes  # noqa: E402
from server.vault import api as vault_api, providers as vp  # noqa: E402

FAILS: list[str] = []
SEEN: list[tuple[str, str]] = []


def check(name, ok, detail=""):
    print(("OK   " if ok else "FAIL ") + name + ("" if ok else f"  -- {detail}"))
    if not ok:
        FAILS.append(name)


class H(ClientClosesFirst, BaseHTTPRequestHandler):
    # ClientClosesFirst: the CI runner's firewall refuses a loopback SYN that
    # lands on a TIME_WAIT the server left (scripts/gate_loopback.py).
    def log_message(self, *a):
        pass

    def do_GET(self):
        SEEN.append((self.path, self.headers.get("Authorization") or ""))
        if self.path == "/coding/v1/models":
            raw = json.dumps({"data": [{"id": "kimi-invented-coder"}, {"id": "kimi-invented-k3"}]}).encode()
            self.send_response(200)
        else:
            raw = b"{}"
            self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


SRV = ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=SRV.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{SRV.server_address[1]}/coding/v1"


class _M:
    def __init__(self):
        self.undo = []

    def set(self, obj, name, value, item=False):
        if item:
            self.undo.append((obj, name, obj.get(name), True))
            obj[name] = value
        else:
            self.undo.append((obj, name, getattr(obj, name), False))
            setattr(obj, name, value)

    def restore(self):
        for obj, name, old, item in reversed(self.undo):
            if item:
                obj[name] = old
            else:
                setattr(obj, name, old)


def fake_request():
    return types.SimpleNamespace(headers={}, client=types.SimpleNamespace(host="127.0.0.1"),
                                 scope={"client": ("127.0.0.1", 1)})


def test_c_a_harness_key_writes_the_engine_file_at_once():
    tmp = tempfile.mkdtemp(prefix="cseries-test-engine-")
    path = os.path.join(tmp, "provider.json")
    m = _M()
    stored: dict = {}
    provisioned: list = []

    async def _store(pool, *, entity_id, name, cred_type, plaintext, actor, if_absent=False):
        stored[name] = plaintext
        return 7

    async def _open(pool, *, entity_id, name, actor, **kw):
        return stored.get(name)

    async def _no(*a, **k):
        return False

    async def _none(*a, **k):
        return None

    async def _prov(p):
        provisioned.append(p)
        return True
    m.set(harness_keys._PRIMARY_CREDS, "kimi", path, item=True)
    m.set(harness_keys, "_resolve_pool_entity", lambda: (object(), 1))
    m.set(harness_keys, "_provision_harness", _prov)
    m.set(vault_api, "store_secret", _store)
    m.set(vault_api, "open_secret", _open)
    m.set(vault_routes.vault, "store_secret", _store)
    m.set(harness_state, "is_disconnected", _no)
    m.set(harness_state, "record_credential", _none)
    m.set(vault_routes, "_pool", lambda: object())
    m.set(vault_routes, "_entity_id", lambda: 1)
    m.set(vault_routes, "_actor", lambda req, a: "test")
    try:
        spec = json.dumps({"base_url": BASE, "api_key": "k-invented", "type": "openai"})
        req = vault_routes.StoreReq(name="harness-kimi", cred_type="api_key",
                                    value_b64=base64.b64encode(spec.encode()).decode())
        out = asyncio.run(vault_routes.store_secret(req, fake_request()))
        on_disk = open(path, encoding="utf-8").read() if os.path.exists(path) else None
        check("c1. adding harness-kimi in Keys writes the engine's file in the same request",
              on_disk == spec and out.get("engine") == "kimi"
              and out.get("engine_file") == "materialized" and provisioned == ["kimi"],
              repr((out, on_disk, provisioned)))
        check("c2. GET /routing's engine_keys names the engine now",
              "kimi" in inf._engine_keys_present(), inf._engine_keys_present())
        # Another key name writes no engine file.
        out2 = asyncio.run(vault_routes.store_secret(
            vault_routes.StoreReq(name="github-token", value_b64=base64.b64encode(b"x").decode()),
            fake_request()))
        check("c3. a key that is no engine's writes no engine file",
              "engine" not in out2 and provisioned == ["kimi"], repr(out2))

        # d. the engine's own endpoint: the spec in Keys, only https on the
        # internet, the checked address, the spec's key.
        from server import backup_stores, brain_service
        m.set(brain_service, "get_pool", lambda: object())
        m.set(brain_service, "_entity_id", 1)
        gets: list = []

        def _pinned(base, addr, key, timeout=15.0):
            gets.append((base, addr, key))
            return {"data": [{"id": "kimi-invented-coder"}, {"id": "kimi-invented-k3"}]}
        resolved = {"kimi.invented.example": ["198.51.100.7"], "lan.invented.example": ["192.168.1.20"]}
        m.set(inf, "_pinned_get_json", _pinned)
        m.set(backup_stores, "_resolve", lambda host, port: list(resolved.get(host, [])))
        # The address the stand-in resolver gives is documentation space; the
        # rule under test is "public", so the test treats it as public.
        m.set(backup_stores, "_public_ip", lambda ip: not (ip.is_private or ip.is_loopback)
              or str(ip) == "198.51.100.7")

        def spec_models(spec):
            stored["harness-kimi"] = spec.encode() if isinstance(spec, str) else spec
            inf._ENGINE_MODELS_CACHE.clear()
            gets.clear()
            return asyncio.run(inf._engine_spec_models("kimi"))
        got = spec_models(json.dumps({"base_url": "https://kimi.invented.example/coding/v1",
                                      "api_key": "k-invented", "type": "openai"}))
        check("d1. a provider spec in Keys: its models come from ITS base_url, at the checked "
              "address, with ITS key",
              got and [r["id"] for r in got[0]] == ["kimi-invented-coder", "kimi-invented-k3"]
              and got[1] is None
              and gets == [("https://kimi.invented.example/coding/v1", "198.51.100.7", "k-invented")],
              repr((got, gets)))
        # An engine run rewrote its credential file to point somewhere else:
        # the Host still reads the spec the person put in Keys.
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"base_url": "http://127.0.0.1:9/v1", "api_key": "x"}))
        got = spec_models(json.dumps({"base_url": "https://kimi.invented.example/coding/v1",
                                      "api_key": "k-invented"}))
        check("d2. the engine's file on disk (which a run can rewrite) is never where the Host reads "
              "the endpoint from",
              [g[0] for g in gets] == ["https://kimi.invented.example/coding/v1"], repr(gets))
        for label, base in (("plain http", BASE), ("an address on the person's network",
                                                    "https://lan.invented.example/v1"),
                            ("this machine", "https://127.0.0.1:8443/v1"),
                            ("a name for this machine", "https://localhost/v1")):
            got = spec_models(json.dumps({"base_url": base, "api_key": "k-invented"}))
            check(f"d3. {label}: the Host does not ask it, and the row says why",
                  got is not None and got[0] == [] and "does not read its model list" in (got[1] or "")
                  and not gets and not SEEN, repr((got, gets)))
        m.set(vp, "resolve_base_url_sync",
              lambda p, k: "https://kimi.invented.example/coding/v1" if (p, k) == ("kimi", "sk-invented-bare") else None)
        got = spec_models("sk-invented-bare")
        check("d4. a bare key lists the models of the base that key authenticates on",
              got and len(got[0]) == 2 and gets and gets[0][2] == "sk-invented-bare", repr((got, gets)))
        stored.pop("harness-kimi", None)
        inf._ENGINE_MODELS_CACHE.clear()
        check("d5. no harness-kimi in Keys: no list of its own (the registry provider's stands)",
              asyncio.run(inf._engine_spec_models("kimi")) is None)
        check("d6. engines with no provider spec are untouched",
              asyncio.run(inf._engine_spec_models("codex")) is None)
    finally:
        m.restore()
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_d_the_matrix_reads_the_engine_list_first():
    src = open(os.path.join(_ROOT, "server/routes/inference.py"), encoding="utf-8").read()
    body = src[src.index("async def _models_for("):src.index("def _catalog_loading(")]
    i_own = body.find("await _engine_spec_models(entry_id)")
    i_reg = body.find("await _direct_models(provider)")
    check("d7. the coding matrix lists the engine's own endpoint before the registry provider's",
          0 <= i_own < i_reg, repr((i_own, i_reg)))


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                check(name, False, repr(e))
    SRV.shutdown()
    print(f"\nFAILURES: {len(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
