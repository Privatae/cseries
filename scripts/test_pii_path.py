#!/usr/bin/env python3
"""
PII sterilize path gate (design 2026-08-03 v0.1 §1/§3 — smoke).

Exercises the platform-side v1 skeleton end-to-end against a STDLIB
http.server fixture implementing the v1 contract (in-memory token map,
stable tokens per (scope,type,value), isolated across scopes, 401 without
the key, a 502 no-output mode). The fixture is a test double for the
contract — its regexes are NOT production detection (detection is never
reimplemented platform-side; the fixture just has to answer the contract).

  1. mode OFF           → write_turn_row lands RAW text directly (zero
                          behavior change), queue untouched, sibling
                          metadata complete
  2. mode ON turn-row   → nothing lands until drain; drained row carries
                          only tokenized text; raw text never reaches the
                          DB writer; SIBLING metadata/tags scrubbed to
                          structural keys (free-text stripped, not tokenized)
  3. service down (502) → item stays queued, nothing written; past the
                          bound the oldest items drop and the counter moves
  4. token semantics    → same value+scope = same token; other scope =
                          different token (stability + isolation)
  5. no key             → server 401 surfaced as a typed failure
  6. rehydrate          → round-trip restores; unknown tokens come back
                          unmodified and listed (never repaired); restored
                          count carried; /health unauthenticated; a 200
                          without a text body fails closed on both endpoints;
                          drain_once never raises (worker stranding guard)
  6b. mesh routing      → MESH_OVERLAY_CIDR (heartbeat-shaped) endpoints go
                          THROUGH the hub peerfwd CONNECT proxy (the pod
                          can't forward-dial the mesh, measured 2026-08-04);
                          refusal/unreachability are typed failures and
                          queued items stay queued
  7. wiring             → the two write sites route via pii_guard.submit
                          (MastercodeBook.record_interaction, the third, left
                          with the simulated psyche 2026-10-17),
                          sibling scrub wired, never-die worker loop,
                          smoke gate + Dockerfile COPY ship

Exit 0 = all pass; exit 1 + [pii-path] FAIL lines otherwise.
"""

import asyncio
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

FAILS = []


def check(name: str, ok: bool, detail: str = ""):
    tag = "ok  " if ok else "FAIL"
    print(f"[pii-path] {tag} {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


# ── Contract fixture ─────────────────────────────────────────────────────
# Token shape [PII:TYPE:N]; map per scope; (scope,type,value) → one stable
# token. `state["down"]` flips every endpoint to 502 (no output exists).
FIXTURE_KEY = "test-key"

_FIXTURE_STATE = {"down": False}
_TOKEN_RE = re.compile(r"\[PII:([A-Z]+):(\d+)\]")
# Fixture-only stand-ins so the contract has something to tokenize.
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_PHONE_RE = re.compile(r"\b\d{3}[-.]\d{3}[-.]\d{4}\b")


class _Vault:
    """scope → {(type, value): token} + reverse. The token SEQUENCE is
    global (monotonic across scopes) so the same value in two scopes gets
    visibly different tokens; the MAPS stay per-scope, so a token minted in
    one scope resolves in no other (isolation)."""
    def __init__(self):
        self.by_scope = {}
        self.n = 0

    def tokenize(self, scope, kind, value):
        vault = self.by_scope.setdefault(scope, {"fwd": {}, "rev": {}})
        key = (kind, value)
        if key not in vault["fwd"]:
            self.n += 1
            tok = f"[PII:{kind}:{self.n}]"
            vault["fwd"][key] = tok
            vault["rev"][tok] = value
        return vault["fwd"][key]

    def resolve(self, scope, token):
        vault = self.by_scope.get(scope)
        if not vault:
            return None
        return vault["rev"].get(token)

    def size(self, scope):
        return len(self.by_scope.get(scope, {}).get("fwd", {}))


_VAULT = _Vault()


class _ProxyFixture:
    """hub/peerfwd stand-in: just enough CONNECT to prove routing. Records
    the CONNECT line, answers 200 (or 403 when refusing), then splices the
    tunnel to the real contract fixture on loopback. The upstream dial is
    loopback — what matters is the client came THROUGH the proxy."""
    def __init__(self, upstream_port):
        self.upstream_port = upstream_port
        self.connects = []
        self.refuse = False
        self.server = None

    async def start(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def _handle(self, reader, writer):
        try:
            line = await asyncio.wait_for(reader.readline(), 5)
            self.connects.append(line.decode(errors="replace").strip())
            while True:  # drain CONNECT headers
                h = await asyncio.wait_for(reader.readline(), 5)
                if h in (b"\r\n", b"\n", b""):
                    break
            if self.refuse:
                writer.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
                await writer.drain()
                return
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            # One request per tunnel (Connection: close): read head + body,
            # relay verbatim to the fixture, stream the answer back.
            req = b""
            while b"\r\n\r\n" not in req:
                chunk = await asyncio.wait_for(reader.read(65536), 5)
                if not chunk:
                    break
                req += chunk
            head, _, rest = req.partition(b"\r\n\r\n")
            m = re.search(rb"Content-Length:\s*(\d+)", head, re.I)
            need = int(m.group(1)) if m else 0
            while len(rest) < need:
                rest += await asyncio.wait_for(reader.read(65536), 5)
            up_reader, up_writer = await asyncio.open_connection(
                "127.0.0.1", self.upstream_port)
            up_writer.write(head + b"\r\n\r\n" + rest)
            await up_writer.drain()
            while True:
                chunk = await asyncio.wait_for(up_reader.read(65536), 5)
                if not chunk:
                    break
                writer.write(chunk)
                await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass


class _Handler(BaseHTTPRequestHandler):
    def _authed(self):
        return self.headers.get("Authorization") == f"Bearer {FIXTURE_KEY}"

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # quiet
        pass

    def _guard(self):
        if not self._authed():
            self._send(401, {"error": "unauthorized"})
            return False
        if _FIXTURE_STATE["down"]:
            self._send(502, {"error": "no output"})
            return False
        return True

    def do_GET(self):
        if self.path == "/health":
            # Contract: /health is UNAUTHENTICATED (the availability gate
            # must answer before any key exists). Still 502s when down.
            if _FIXTURE_STATE["down"]:
                self._send(502, {"error": "no output"})
                return
            self._send(200, {"ok": True})
            return
        m = re.fullmatch(r"/scope/(.+)", self.path)
        if m:
            if not self._guard():
                return
            scope = m.group(1)
            self._send(200, {"scope": scope, "tokens": _VAULT.size(scope)})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._guard():
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        except Exception:
            self._send(400, {"error": "bad json"})
            return
        if _FIXTURE_STATE.get("bare_200") and self.path in ("/sterilize", "/rehydrate"):
            # Contract violation drill: 200 with NO text body. The client
            # must treat this as a failure, never a partial success.
            self._send(200, {"ok": True})
            return
        text = body.get("text") or ""
        scope = body.get("scope") or ""
        if self.path == "/sterilize":
            findings = []
            for kind, rx in (("EMAIL", _EMAIL_RE), ("PHONE", _PHONE_RE)):
                def _sub(m, kind=kind):
                    tok = _VAULT.tokenize(scope, kind, m.group(0))
                    findings.append({"token": tok, "type": kind})
                    return tok
                text = rx.sub(_sub, text)
            self._send(200, {"text": text, "findings": findings,
                             "stats": {"rules": len(findings)}})
            return
        if self.path == "/rehydrate":
            unknown = []
            restored = 0

            def _back(m):
                nonlocal restored
                tok = m.group(0)
                val = _VAULT.resolve(scope, tok)
                if val is None:
                    unknown.append(tok)
                    return tok  # surfaced as-is, never repaired
                restored += 1
                return val
            self._send(200, {"text": _TOKEN_RE.sub(_back, text),
                             "unknown_tokens": unknown,
                             "restored": restored})
            return
        self._send(404, {"error": "not found"})


def main() -> int:
    # /app in the image; APP_ROOT=<repo> to run against the source tree.
    app_root = os.environ.get("APP_ROOT", "/app")
    sys.path.insert(0, app_root)
    sys.path.insert(0, os.path.join(app_root, "brain"))

    # Gate-controlled environment: mode on, worker manual, fixture endpoint.
    os.environ["PII_QUEUE_WORKER"] = "off"

    import importlib
    try:
        from mastercode import pii_client, pii_queue
        me = importlib.import_module("mastercode.memory_engine")
    except ModuleNotFoundError:
        if app_root == "/app":
            raise  # the image must import the real package — no fallback
        # Source-tree run without image deps: stub the two non-stdlib
        # imports memory_engine needs at module load (unused by this path).
        import types
        for dep, attrs in (("aiohttp", {"ClientSession": object, "ClientTimeout": object}),
                           ("asyncpg", {"Pool": object})):
            if dep not in sys.modules:
                stub = types.ModuleType(dep)
                for k, v in attrs.items():
                    setattr(stub, k, v)
                sys.modules[dep] = stub
        me = importlib.import_module("mastercode.memory_engine")
        from mastercode import pii_client, pii_queue

    # The turn-row landing path embeds before insert — stub the embed lane
    # off (unembedded insert branch); the PII path is what this gate proves.
    async def _no_embed(_text):
        return None
    me._get_memory_embedding = _no_embed

    class _StubPool:
        def __init__(self):
            self.inserts = []

        async def fetchval(self, sql, *args):
            self.inserts.append((sql, args))
            return 42

    loop = asyncio.new_event_loop()

    # ── 1. mode OFF → raw lands directly, zero behavior change ──────────
    os.environ.pop("PII_MODE", None)
    pool = _StubPool()
    row_id = loop.run_until_complete(
        me.write_turn_row(pool, 7, "sess-off", "user", "mail me at alex@example.com"))
    check("mode OFF: turn row lands directly with the RAW text (zero change)",
          row_id == 42 and len(pool.inserts) == 1
          and pool.inserts[0][1][1] == "mail me at alex@example.com")
    check("mode OFF: queue untouched",
          pii_queue.stats()["pending"] == 0)
    pool_off2 = _StubPool()
    loop.run_until_complete(me.write_turn_row(
        pool_off2, 7, "sess-off2", "user", "x",
        tags={"app_id": "circles-replay", "variant_prompt_preview": "raw preview"}))
    check("mode OFF: sibling metadata lands COMPLETE (zero change, no scrub)",
          "variant_prompt_preview" in (pool_off2.inserts[0][1][3]
                                      if pool_off2.inserts else ""))

    # Fixture up, mode ON.
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    os.environ["PII_MODE"] = "on"
    os.environ["PII_SERVICE_URL"] = f"http://127.0.0.1:{port}"
    os.environ["PII_API_KEY"] = FIXTURE_KEY

    # ── 2. mode ON turn-row → tokenized lands, raw never touches the DB ──
    pool2 = _StubPool()
    queued = loop.run_until_complete(
        me.write_turn_row(pool2, 7, "sess-on", "user", "mail me at alex@example.com"))
    check("mode ON: write defers (returns None), nothing lands before drain",
          queued is None and not pool2.inserts and pii_queue.stats()["pending"] == 1)
    drained = loop.run_until_complete(pii_queue.drain_once())
    landed = pool2.inserts[0][1][1] if pool2.inserts else ""
    check("mode ON: drain lands exactly one tokenized row",
          drained == 1 and len(pool2.inserts) == 1 and "[PII:EMAIL:1]" in landed)
    check("mode ON: raw message text never reaches the DB writer (content field)",
          all("alex@example.com" not in str(args) for _, args in pool2.inserts))
    check("stats() carries the four contract counters",
          set(pii_queue.stats()) == {"pending", "sterilized", "failed", "dropped"}
          and pii_queue.stats()["sterilized"] == 1)

    # ── 2b. sibling fields: the queue tokenizes
    # the content field ONLY — metadata/tags must be scrubbed to structural
    # keys before submit, or filenames/previews land raw in the row.
    pool_md = _StubPool()
    loop.run_until_complete(me.write_turn_row(
        pool_md, 7, "sess-md", "user", "see attached",
        tags={"app_id": "circles-replay",
              "variant_prompt_preview": "mail alex@example.com",
              "attachments": [{"name": "my-secret-diagnosis.pdf", "kind": "file",
                               "media_type": "application/pdf", "size": 1234}]}))
    loop.run_until_complete(pii_queue.drain_once())
    md_json = pool_md.inserts[0][1][3] if pool_md.inserts else "{}"
    md = json.loads(md_json)
    check("mode ON: sibling free-text STRIPPED (prompt preview, filename)",
          "variant_prompt_preview" not in md
          and "alex@example.com" not in md_json
          and "my-secret-diagnosis.pdf" not in md_json)
    check("mode ON: sibling structural keys KEPT (app_id, attachment shape)",
          md.get("app_id") == "circles-replay"
          and md.get("attachments") == [{"kind": "file",
                                         "media_type": "application/pdf",
                                         "size": 1234}])

    # ── 3. service down → queued, nothing written; bound drops oldest ────
    _FIXTURE_STATE["down"] = True
    pool3 = _StubPool()
    loop.run_until_complete(
        me.write_turn_row(pool3, 7, "sess-down", "user", "call 555-0100"))
    drained_down = loop.run_until_complete(pii_queue.drain_once())
    check("service down (502): item stays queued, nothing written",
          drained_down == 0 and not pool3.inserts
          and pii_queue.stats()["pending"] >= 1)
    check("service down: failed-attempt counter moved",
          pii_queue.stats()["failed"] >= 1)

    dropped_before = pii_queue.stats()["dropped"]
    pii_queue._queue.clear()
    old_bound = pii_queue._BOUND
    pii_queue._BOUND = 3
    for i in range(5):
        loop.run_until_complete(pii_queue.submit(
            f"text {i}", "thread:bound", _noop_writer, pool=None, entity_id=7))
    pii_queue._BOUND = old_bound
    check("past the bound: oldest dropped, drop counter exact",
          pii_queue.stats()["dropped"] - dropped_before == 2
          and pii_queue.stats()["pending"] == 3)
    pii_queue._queue.clear()
    pii_queue._counts["failed"] = 0
    _FIXTURE_STATE["down"] = False

    # ── 4. token stability + scope isolation (real client, real HTTP) ────
    r1 = loop.run_until_complete(pii_client.sterilize("mail a@b.com", "thread:x"))
    r2 = loop.run_until_complete(pii_client.sterilize("mail a@b.com", "thread:x"))
    r3 = loop.run_until_complete(pii_client.sterilize("mail a@b.com", "thread:y"))
    check("same value + same scope → same token (stability)",
          r1.ok and r2.ok and r1.text == r2.text and "[PII:EMAIL:" in r1.text)
    check("same value, other scope → different token (isolation)",
          r3.ok and r3.text != r1.text)

    # ── 5. no key → 401 surfaced as a typed failure ──────────────────────
    del os.environ["PII_API_KEY"]
    r401 = loop.run_until_complete(pii_client.sterilize("mail a@b.com", "thread:x"))
    check("no key → 401 surfaced as failure (never partial output)",
          not r401.ok and r401.error == "unauthorized" and r401.text is None)
    os.environ["PII_API_KEY"] = FIXTURE_KEY

    # ── 6. rehydrate: round-trip + unknown tokens unmodified ─────────────
    rh = loop.run_until_complete(pii_client.rehydrate(r1.text, "thread:x"))
    check("rehydrate round-trips a known token",
          rh.ok and rh.text == "mail a@b.com" and not rh.unknown_tokens)
    rh_unknown = loop.run_until_complete(
        pii_client.rehydrate("hello [PII:EMAIL:99] there", "thread:x"))
    check("unknown token surfaces UNMODIFIED and listed (never repaired)",
          rh_unknown.ok and "[PII:EMAIL:99]" in rh_unknown.text
          and rh_unknown.unknown_tokens == ["[PII:EMAIL:99]"])
    check("rehydrate carries the contract's restored count",
          rh.restored == 1 and rh_unknown.restored == 0)

    import urllib.request
    code = urllib.request.urlopen(
        f"http://127.0.0.1:{port}/health", timeout=5).status
    check("contract: /health answers UNAUTHENTICATED", code == 200)

    # Contract-violation drill: a 200 with NO text body is a failure on both
    # body-carrying endpoints — never a partial success.
    _FIXTURE_STATE["bare_200"] = True
    bare_s = loop.run_until_complete(pii_client.sterilize("mail a@b.com", "thread:x"))
    bare_r = loop.run_until_complete(pii_client.rehydrate("hello", "thread:x"))
    _FIXTURE_STATE["bare_200"] = False
    check("200 without a text body fails closed on sterilize AND rehydrate",
          not bare_s.ok and bare_s.error == "bad_payload"
          and not bare_r.ok and bare_r.error == "bad_payload")

    # Worker stranding guard: drain_once never raises
    # even when the client does — a raised drain strands the queue silently.
    pii_queue._queue.clear()
    orig_sterilize = pii_client.sterilize

    async def _boom(*a, **k):
        raise RuntimeError("boom")
    pii_client.sterilize = _boom
    try:
        loop.run_until_complete(pii_queue.submit(
            "text", "thread:z", _noop_writer, pool=None, entity_id=7))
        drain_rc = loop.run_until_complete(pii_queue.drain_once())
    finally:
        pii_client.sterilize = orig_sterilize
    check("drain_once never raises (client raise → item stays queued, counted)",
          drain_rc == 0 and pii_queue.stats()["pending"] == 1
          and pii_queue.stats()["failed"] >= 1)
    pii_queue._queue.clear()

    # ── 6b. mesh endpoints ride the hub peerfwd CONNECT proxy ────────────
    # The pod has no route to the mesh overlay (MESH_OVERLAY_CIDR): a
    # heartbeat-shaped endpoint must go THROUGH the proxy, a loopback
    # override must dial direct.
    os.environ["MESH_OVERLAY_CIDR"] = "2001:db8::/64"
    proxy = _ProxyFixture(port)
    proxy_port = loop.run_until_complete(proxy.start())
    os.environ["PII_SERVICE_URL"] = "http://[2001:db8::1]:18081"  # heartbeat-shaped
    os.environ["HUB_PEER_FORWARD_ADDR"] = f"127.0.0.1:{proxy_port}"
    via_proxy = loop.run_until_complete(
        pii_client.sterilize("route alex@example.com home", "thread:mesh"))
    check("mesh endpoint goes THROUGH the CONNECT proxy",
          via_proxy.ok and any(c.startswith("CONNECT [2001:db8::1]:18081")
                               for c in proxy.connects))
    check("proxied call returns the sterilized body",
          "[PII:EMAIL:" in (via_proxy.text or ""))
    check("routing rule: mesh→proxy env, non-mesh→direct, default [::1]:3129",
          pii_client._proxy_for("http://[2001:db8::1]:18081") == f"127.0.0.1:{proxy_port}"
          and pii_client._proxy_for(f"http://127.0.0.1:{port}") is None)

    proxy.refuse = True
    refused = loop.run_until_complete(pii_client.sterilize("x", "thread:mesh"))
    pend = pii_queue.stats()["pending"]
    loop.run_until_complete(pii_queue.submit(
        "call 555-0100", "thread:mesh", _noop_writer, pool=None, entity_id=7))
    rc = loop.run_until_complete(pii_queue.drain_once())
    check("proxy CONNECT refusal → typed failure, item stays queued",
          not refused.ok and refused.error == "connect_refused"
          and rc == 0 and pii_queue.stats()["pending"] == pend + 1)
    proxy.refuse = False
    check("proxy recovery: the queued item drains once the proxy answers",
          loop.run_until_complete(pii_queue.drain_once()) == 1)

    os.environ["HUB_PEER_FORWARD_ADDR"] = "127.0.0.1:1"  # nothing listens there
    unreach = loop.run_until_complete(pii_client.sterilize("x", "thread:mesh"))
    check("proxy unreachable → typed failure, no partial output",
          not unreach.ok and unreach.error and unreach.text is None)
    os.environ.pop("HUB_PEER_FORWARD_ADDR", None)
    check("mesh resolution defaults to [::1]:3129 without the env",
          pii_client._proxy_for("http://[2001:db8::1]:18081") == "[::1]:3129")
    os.environ["PII_SERVICE_URL"] = f"http://127.0.0.1:{port}"  # direct again
    os.environ.pop("MESH_OVERLAY_CIDR", None)
    proxy.server.close()

    # ── 7. wiring (source pins) ──────────────────────────────────────────
    me_src = open(os.path.join(app_root, "brain/mastercode/memory_engine.py")).read()
    check("write_turn_row routes via pii_guard.submit at thread scope",
          "pii_guard.submit(" in me_src and 'f"thread:{session_id}"' in me_src
          and "_write_turn_row_landed" in me_src)
    sess_src = open(os.path.join(app_root, "brain/mastercode/runtime/session.py")).read()
    check("_save_session_to_db routes transcripts via pii_guard.submit",
          "pii_guard.submit(" in sess_src and 'f"thread:{sess.session_id}"' in sess_src)
    q_src = open(os.path.join(app_root, "brain/mastercode/pii_queue.py")).read()
    check("write_turn_row scrubs sibling metadata/tags on the queue path",
          "pii_guard.scrub_metadata(metadata_extra)" in me_src
          and "pii_guard.scrub_metadata(tags)" in me_src
          and "_STRUCTURAL_META_KEYS" in q_src)
    check("drain worker cannot die with items queued (never-die loop)",
          "except asyncio.CancelledError" in q_src)
    smoke_path = os.path.join(app_root, "scripts/smoke_test.sh")
    docker_path = os.path.join(app_root, "Dockerfile.linux")
    if os.path.exists(smoke_path) and os.path.exists(docker_path):
        smoke_src = open(smoke_path).read()
        check("smoke_test.sh runs the pii-path gate",
              "test_pii_path.py" in smoke_src)
        docker_src = open(docker_path).read()
        check("Dockerfile.linux COPYs the gate into the image",
              "scripts/test_pii_path.py /app/scripts/test_pii_path.py" in docker_src)
    else:
        # In-image: neither file is shipped; the proof of the COPY is this
        # gate's own presence at /app/scripts/.
        check("gate script present in the image (COPY proved)",
              os.path.exists("/app/scripts/test_pii_path.py"))

    server.shutdown()
    print(f"[pii-path] {'PASS' if not FAILS else 'FAIL'} "
          f"({len(FAILS)} failing)" + (f": {FAILS}" if FAILS else ""))
    return 1 if FAILS else 0


async def _noop_writer(_text):
    return None


if __name__ == "__main__":
    sys.exit(main())
