#!/usr/bin/env python3
"""Gate: an installed app's own MCP server becomes a connection of its Host
(manifest.McpSpec, migrations/v221,
server/mcp_connections.py sync_app / _use_app).

A fake app MCP server on 127.0.0.1 stands in for the app's runtime pod: modern
era (2026-07-28), it refuses any request whose bearer is not the app's own
token, lists a tool that returns an image and one that takes an image, and
records what it was sent. The pod's wake (harness_backend.ensure_awake_app) is
stubbed to name it; everything else is the kernel's own code on a real kernel
schema, the app registered through the real registry.

  A  the manifest: `mcp: {path}` only on a process app with a runtime.image;
     the path is checked; validate_publish reports the same.
  M  migrations/v221 on the full current schema, run twice over: an app row
     with an app:// URL is admitted, a remote row still needs https, an app
     row may not carry https or another app's URL.
  S  sync_app at install: one active connection named after the app, its
     terms signed with the Host's key and naming the app, and no egress rule;
     a second sync keeps it and marks its tool list for a fresh fetch; a
     remote server of the same name is left alone; an app that stops
     declaring mcp loses it.
  W  the wire: the tool list and every call go straight to the app's pod
     with the app's token, and NOTHING reaches the egress gate or the proxy
     variables (both traps); a pod that moved costs one re-wake.
  K  the Host's rules hold: calls refused until skills/mcp-<app> is written;
     arguments checked against the schema; the result fenced as data.
  I  images: one the app returns is kept in the Host's gallery and the result
     names it; one of the Host's images passed by name reaches the app as a
     data: URL; a name that is not one of its images is refused.
  U  uninstalled (or its manifest changed): the connection is refused, and
     drop_app revokes it.

  python3 scripts/test_mcp_app_connections_pg.py
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import contextlib
import dataclasses
import json
import os
import pathlib
import struct
import subprocess
import sys
import tempfile
import zlib

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parents[1])
sys.path[:0] = [str(ROOT), str(ROOT / "brain"), str(ROOT / "scripts")]

from aiohttp import web  # noqa: E402

EID = 1
APP = "wallpaper"
MIGRATION = ROOT / "migrations" / "v221_mcp_app_connections.sql"
FAILED = 0


def check(label, cond, detail=""):
    global FAILED
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f"\n      {str(detail)[:600]}"))
    if not cond:
        FAILED += 1


def _png(w=2, h=2) -> bytes:
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * w for _ in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


PNG = _png()

# ── the traps: nothing may reach them ──────────────────────────────────────
TRAP_HITS: list[str] = []


async def _trap(reader, writer):
    TRAP_HITS.append((await reader.read(200)).decode("latin-1", "replace")[:120])
    writer.close()


# ── the fake app pod ───────────────────────────────────────────────────────
APP_REQS: list[dict] = []
TOKEN = {"want": ""}

TOOLS = [
    {"name": "render", "description": "Render the scene and return a preview.",
     "inputSchema": {"type": "object", "properties": {"size": {"type": "integer"}},
                     "required": ["size"]}},
    {"name": "edit", "description": "Open an image to edit.",
     "inputSchema": {"type": "object", "properties": {"image": {"type": "string"}},
                     "required": ["image"]}},
    {"name": "big", "description": "Return one large image.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "many", "description": "Return ten images.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "put", "description": "Put a file into the app.",
     "inputSchema": {"type": "object", "properties": {"data": {"type": "string"}},
                     "required": ["data"]}},
]
# The app's own files, served at GET /files/<path> as the creative bridge
# serves its window's (another app's file, handed over by its app:// name).
WAV = b"RIFF" + os.urandom(3 * 1024 * 1024)
APP_FILES = {"bounces/mix.wav": (WAV, "audio/wav")}
FILE_REQS: list[dict] = []


async def _app_file(request: web.Request):
    rel = request.match_info["rel"]
    FILE_REQS.append({"auth": request.headers.get("Authorization", ""), "rel": rel})
    if rel not in APP_FILES:
        return web.json_response({"error": "no such file"}, status=404)
    body, ctype = APP_FILES[rel]
    return web.Response(body=body, content_type=ctype)
# A PNG whose body is noise: the gallery checks the signature, and 6 MB of it
# is a response the 4 MiB remote-server cap would refuse.
BIG = PNG[:8] + os.urandom(6 * 1024 * 1024)


async def _app_mcp(request: web.Request):
    body = await request.json()
    APP_REQS.append({"auth": request.headers.get("Authorization", ""), "body": body,
                     "path": request.path})
    if request.headers.get("Authorization") != f"Bearer {TOKEN['want']}":
        return web.json_response({"error": "unauthorized"}, status=401)
    rid, method = body.get("id"), body.get("method")
    params = body.get("params") or {}
    if method == "tools/list":
        return web.json_response({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if name == "render":
            content = [{"type": "image", "mimeType": "image/png",
                        "data": base64.b64encode(PNG).decode()},
                       {"type": "text", "text": f"rendered at {args.get('size')}"}]
        elif name == "edit":
            img = str(args.get("image") or "")
            if img.startswith("data:image/png;base64,"):
                n = len(base64.b64decode(img.split(",", 1)[1]))
                content = [{"type": "text", "text": f"opened a png of {n} bytes"}]
            else:
                content = [{"type": "text", "text": f"opened {img[:80]}"}]
        elif name == "put":
            data = str(args.get("data") or "")
            if data.startswith("data:") and ";base64," in data:
                head, b64 = data.split(",", 1)
                body = base64.b64decode(b64)
                content = [{"type": "text", "text": f"got {head[5:].split(';')[0]} of {len(body)} bytes, "
                                                    f"sha256 {hashlib.sha256(body).hexdigest()}"}]
            else:
                content = [{"type": "text", "text": f"got text {data[:80]}"}]
        elif name == "big":
            content = [{"type": "image", "mimeType": "image/png", "data": base64.b64encode(BIG).decode()}]
        elif name == "many":
            content = [{"type": "image", "mimeType": "image/png", "data": base64.b64encode(PNG).decode()}
                       for _ in range(10)]
        else:
            return web.json_response({"jsonrpc": "2.0", "id": rid,
                                      "error": {"code": -32601, "message": "no such tool"}})
        return web.json_response({"jsonrpc": "2.0", "id": rid, "result": {"content": content}})
    return web.json_response({"jsonrpc": "2.0", "id": rid,
                              "error": {"code": -32601, "message": "method not found"}})


def _skill(server, tools):
    allowed = ", ".join(["operate"] + [f"mcp:{server}/{t}" for t in tools])
    return f"""---
name: mcp-{server}
description: Use the {server} app's tools and check what they return.
allowed-tools: {allowed}
sources: mcp:{server}
mcp-server: {server}
---

## When to use

The person asks for something the {server} app does.

## Steps

1. Call `operate` with the app's tool and its arguments.

## Checks

The result names what was asked.

## When it fails

Say what the app answered.
"""


def _app_yaml(path: str | None = "/mcp", kind="process", image="creative", extra="") -> str:
    mcp = f"mcp:\n  path: {path}\n{extra}" if path else ""
    img = f"  image: {image}\n" if image else ""
    cmd = '  command: ["python3", "server.py"]\n  ready_path: /health\n' if kind == "process" else ""
    return (f"id: {APP}\nname: Wallpaper\nentrypoint: index.html\nprovenance: custom\n"
            f"version: 0.1.0\npermissions: []\nnetwork: []\n"
            f"resources:\n  baseline_memory_mi: 512\n"
            f"runtime:\n  kind: {kind}\n{img}{cmd}{mcp}")


def manifest_checks(tmp: pathlib.Path):
    import yaml

    from server.applications import manifest as M
    print("A. the manifest")
    d = tmp / "m1"
    d.mkdir()
    (d / "index.html").write_text("<html></html>")
    (d / "app.yaml").write_text(_app_yaml())
    m = M.load(d / "app.yaml")
    check("A1 a process app with an image declares mcp: {path: /mcp}", m.mcp.path == "/mcp", m.mcp)
    (d / "app.yaml").write_text(_app_yaml(path=None))
    check("A1 ...absent means none", M.load(d / "app.yaml").mcp.path == "")
    for yaml_text, why in ((_app_yaml(kind="static", image=None), "needs runtime.kind: process"),
                           (_app_yaml(path="mcp"), "absolute path"),
                           (_app_yaml(path="/a/../b"), "absolute path"),
                           (_app_yaml(extra="  port: 9\n"), "not a key of mcp")):
        (d / "app.yaml").write_text(yaml_text)
        try:
            M.load(d / "app.yaml")
            check(f"A2 refused: {why}", False, "loaded")
        except M.ManifestError as e:
            check(f"A2 refused: {why}", why in str(e), str(e))
    _, errs = M.mcp_spec(yaml.safe_load(_app_yaml(path="mcp")))
    check("A3 mcp_spec reports the same violation without raising",
          bool(errs) and "absolute path" in errs[0], errs)
    check("A3 ...and validate_publish reports it",
          "mcp_spec(m)" in (ROOT / "server/applications/build_contract.py").read_text())


async def service_checks(dsn: str, app_port: int, tmp: pathlib.Path):
    import asyncpg

    from mastercode.runtime import direct_ops, host_skills
    from server import mcp_connections as MC
    from server.applications import harness_backend, installer, registry
    from server.applications import manifest as M
    from server.network import nac_stepup
    from server.network.nac_egress import _verify_rule

    pool = await asyncpg.create_pool(dsn, min_size=2, max_size=6)
    try:
        for dom in ("core.entity", "external.operator"):
            await pool.execute("INSERT INTO domain_registry (id, name, domain_group) VALUES ($1, $1, 'core') "
                               "ON CONFLICT DO NOTHING", dom)
        await pool.execute(
            "INSERT INTO entity_domain_access (entity_id, domain_id, access_level) VALUES "
            "($1, 'core.entity', 'read_write'), ($1, 'external.operator', 'read') "
            "ON CONFLICT (entity_id, domain_id) DO UPDATE SET access_level = CASE "
            "WHEN entity_domain_access.access_level IN ('none', 'read') THEN EXCLUDED.access_level "
            "ELSE entity_domain_access.access_level END", EID)
        await nac_stepup.ensure_stepup_secret(pool)
        await nac_stepup.mark_enrolled(pool)

        # The app, installed: its install dir and its registry row, the two
        # traces of the real installer that the connection reads.
        root = tmp / "apps"
        (root / APP).mkdir(parents=True)
        (root / APP / "index.html").write_text("<html></html>")
        (root / APP / "app.yaml").write_text(_app_yaml())
        installer.INSTALL_ROOT = root
        manifest = M.load(root / APP / "app.yaml")
        await registry.register(pool, manifest)
        TOKEN["want"] = await harness_backend.app_source_token(pool, APP)

        # The pod's wake, stubbed. With dead_first, the first answer is a pod
        # that moved (nothing listens there).
        wakes = {"n": 0, "dead_first": False, "invalidated": 0}

        async def _wake(app_id):
            wakes["n"] += 1
            if wakes["dead_first"]:
                wakes["dead_first"] = False
                return "127.0.0.1:9"
            return f"127.0.0.1:{app_port}" if app_id == APP else None

        def _invalidate(app_id):
            wakes["invalidated"] += 1
        # The address is confirmed with the operator before every contact:
        # ensure_awake_app_fresh ages the cached stamp, so the real
        # ensure_awake_app re-stamps and takes the pod's current address.
        real_ensure, real_wake = harness_backend.ensure_awake_app, harness_backend._wake_app
        st = harness_backend._st(APP, harness_backend._APP_RT_MARK)
        st.update({"url": "http://10.0.0.9:8851", "stamped_at": __import__("time").monotonic()})
        operator_says = {"n": 0}

        async def _operator_wake(app_id):
            operator_says["n"] += 1
            return {"ok": True, "url": "http://10.0.0.7:8851"}
        harness_backend._wake_app = _operator_wake
        got_host = await harness_backend.ensure_awake_app_fresh(APP)
        check("W0 a fresh address is the operator's answer now, never the cached one",
              operator_says["n"] == 1 and got_host == "10.0.0.7:8851", (operator_says, got_host))
        harness_backend._wake_app = real_wake
        harness_backend._state.pop(APP, None)
        assert real_ensure is harness_backend.ensure_awake_app

        harness_backend.ensure_awake_app = _wake
        harness_backend.invalidate_app_url = _invalidate

        print("S. the connection the install makes")
        rules_before = await pool.fetchval("SELECT count(*) FROM nac_egress")
        got = await MC.sync_app(pool, EID, manifest)
        row = await MC.live_row(pool, EID, APP)
        pub = await nac_stepup.entity_verify_pubkey(pool, EID)
        check("S1 sync_app makes one active connection named after the app, at app://<id>/mcp",
              bool(got) and got["status"] == "active" and row is not None and row["status"] == "active"
              and row["url"] == f"app://{APP}/mcp" and row["app_id"] == APP, (got, row))
        check("S1 ...its terms name the app and are signed with the Host's key",
              row["signed_terms"] == MC.terms(row) and row["signed_terms"].get("app") == APP
              and _verify_rule(pub, row["signed_terms"], row["signature"]), row)
        check("S1 ...and no egress rule is drafted (the traffic stays in the namespace)",
              row["rule_id"] is None
              and await pool.fetchval("SELECT count(*) FROM nac_egress") == rules_before)
        check("S1 verify() admits it", await MC.verify(pool, EID, row) is None)
        check("S1 nothing was contacted", not APP_REQS and wakes["n"] == 0, APP_REQS)

        print("W. the wire")
        listed = await MC.tools(pool, EID, APP)
        check("W1 the tool list is fetched from the app's pod (op='tools')",
              [t["name"] for t in listed["tools"]][:2] == [f"mcp:{APP}/render", f"mcp:{APP}/edit"], listed)
        check("W1 ...with the app's own token as the bearer, at the declared path",
              bool(APP_REQS) and APP_REQS[-1]["auth"] == f"Bearer {TOKEN['want']}"
              and APP_REQS[-1]["path"] == "/mcp", APP_REQS[-1:])
        check("W1 ...and nothing reached the egress gate or the proxy variables", not TRAP_HITS, TRAP_HITS)

        print("K. the Host's rules")
        out = await MC.call(pool, EID, f"mcp:{APP}/render", {"size": 512})
        check("K1 a call is refused until skills/mcp-<app> is written",
              out.get("status") != "ok" and f"skills/mcp-{APP}" in out.get("result", ""), out)
        w = await host_skills.write(pool, EID, f"mcp-{APP}", _skill(APP, ["render", "edit", "big", "many", "put"]),
                                    author="host")
        check("K2 the Host writes the skill to the standard (the app is an active connection)",
              w.get("status") == "ok", w)
        out = await MC.call(pool, EID, f"mcp:{APP}/render", {"size": "big"})
        check("K3 arguments are checked against the app's schema", out.get("status") != "ok"
              and "size" in json.dumps(out), out)

        print("I. images")
        n0 = len(APP_REQS)
        out = await direct_ops.run_direct_op(f"mcp:{APP}/render", {"size": 512}, entity_id=EID, pool=pool)
        res = out.get("result", "")
        check("I1 a call through operate's path runs, and its result is fenced as data",
              out.get("status") == "ok" and f"from MCP server {APP}" in res
              and "rendered at 512" in res, out)
        img = await pool.fetchrow("SELECT id, content_type, bytes, model FROM generated_images "
                                  "WHERE entity_id = $1 ORDER BY created_at DESC LIMIT 1", EID)
        check("I1 the image the app returned is kept in the Host's gallery",
              img is not None and bytes(img["bytes"]) == PNG and img["content_type"] == "image/png"
              and img["model"] == f"app:{APP}", dict(img or {}))
        check("I1 ...and the result names it, never the raw base64",
              img is not None and f"/api/images/{img['id']}" in res
              and base64.b64encode(PNG).decode()[:40] not in res, res)
        out = await MC.call(pool, EID, f"mcp:{APP}/edit", {"image": f"/api/images/{img['id']}"})
        sent = APP_REQS[-1]["body"]["params"]["arguments"]["image"]
        check("I2 one of the Host's images, passed by name, reaches the app as a data: URL",
              out.get("status") == "ok" and f"opened a png of {len(PNG)} bytes" in out["result"]
              and sent.startswith("data:image/png;base64,"), (out, sent[:60]))
        n1 = len(APP_REQS)
        out = await MC.call(pool, EID, f"mcp:{APP}/edit", {"image": "/api/images/gimg_" + "A" * 22})
        check("I3 a name that is not one of its images is refused, and nothing is sent",
              out.get("status") != "ok" and "not one of your images" in out.get("result", "")
              and len(APP_REQS) == n1, out)
        check("I4 every app call reached the pod and nothing reached the traps",
              not TRAP_HITS and n0 < len(APP_REQS), TRAP_HITS)
        before = await pool.fetchval("SELECT count(*) FROM generated_images WHERE entity_id = $1", EID)
        out = await MC.call(pool, EID, f"mcp:{APP}/big", {})
        kept = await pool.fetchrow("SELECT bytes FROM generated_images WHERE entity_id = $1 "
                                   "ORDER BY created_at DESC LIMIT 1", EID)
        check("I5 a 6 MB image comes back whole from the app's own server (the 4 MiB cap is a "
              "remote server's)", out.get("status") == "ok" and kept is not None
              and bytes(kept["bytes"]) == BIG, str(out)[:300])
        out = await MC.call(pool, EID, f"mcp:{APP}/many", {})
        after = await pool.fetchval("SELECT count(*) FROM generated_images WHERE entity_id = $1", EID)
        check("I6 every image the app returns is kept: ten of ten",
              out.get("status") == "ok" and after - before == 11 and out["result"].count("[image kept as") == 10,
              (after - before, str(out)[:300]))

        print("F. a file of an app, handed to an app by its app:// name")
        n1 = len(APP_REQS)
        out = await MC.call(pool, EID, f"mcp:{APP}/put", {"data": f"app://{APP}/files/bounces/mix.wav"})
        check("F1 the file reaches the app whole, as a data: URL of its own type",
              out.get("status") == "ok"
              and f"got audio/wav of {len(WAV)} bytes, sha256 {hashlib.sha256(WAV).hexdigest()}" in out["result"],
              str(out)[:300])
        check("F1 ...fetched from the app that has it, with that app's own token",
              FILE_REQS and FILE_REQS[-1] == {"auth": f"Bearer {TOKEN['want']}", "rel": "bounces/mix.wav"},
              FILE_REQS[-1:])
        n1 = len(APP_REQS)
        out = await MC.call(pool, EID, f"mcp:{APP}/put", {"data": "app://not-installed/files/mix.wav"})
        check("F2 an app the Host has not installed is refused, and nothing is sent",
              out.get("status") != "ok" and "not a file of an app you have installed" in out.get("result", "")
              and len(APP_REQS) == n1, out)
        out = await MC.call(pool, EID, f"mcp:{APP}/put", {"data": f"app://{APP}/files/no/such.wav"})
        check("F3 a file the app does not have is refused, and nothing is sent",
              out.get("status") != "ok" and "has no file no/such.wav" in out.get("result", "")
              and len(APP_REQS) == n1, out)
        real_max = MC.MAX_IMAGES_TO_APP_BYTES
        MC.MAX_IMAGES_TO_APP_BYTES = 1024 * 1024
        try:
            out = await MC.call(pool, EID, f"mcp:{APP}/put", {"data": f"app://{APP}/files/bounces/mix.wav"})
        finally:
            MC.MAX_IMAGES_TO_APP_BYTES = real_max
        check("F4 the files and images of one call share the call's total, and past it nothing is sent",
              out.get("status") != "ok" and "over 1 MB" in out.get("result", "") and len(APP_REQS) == n1, out)
        n2 = len(FILE_REQS)
        out = await MC.call(pool, EID, f"mcp:{APP}/put", {"data": f"app://{APP}/files/bounces/../../health"})
        check("F6 a name with .. is refused before any address of the app is asked for",
              out.get("status") != "ok" and "is not a file name" in out.get("result", "")
              and len(FILE_REQS) == n2 and len(APP_REQS) == n1, out)
        out = await MC.call(pool, EID, f"mcp:{APP}/put", {"data": "see app://x/files/a.wav later"})
        check("F5 only a whole argument that is an app:// file name is taken; text that mentions one is not",
              out.get("status") == "ok" and "got text see app://x/files/a.wav" in out["result"], out)
        # The app holding the file answers nothing (a pod that moved): the
        # call is refused naming the handover, never an exception out of the
        # lane, and the next try wakes the app again.
        wakes["dead_first"] = True
        inv0, n1 = wakes["invalidated"], len(APP_REQS)
        try:
            out = await MC.call(pool, EID, f"mcp:{APP}/put", {"data": f"app://{APP}/files/bounces/mix.wav"})
        except Exception as e:  # noqa: BLE001 - what the check is about
            out = {"status": "raised", "result": f"{type(e).__name__}: {e}"}
        wakes["dead_first"] = False
        check("F7 a handover the holding app does not answer is refused as one, and nothing is sent",
              out.get("status") not in ("ok", "raised") and f"{APP} did not hand over bounces/mix.wav"
              in out.get("result", "") and len(APP_REQS) == n1 and wakes["invalidated"] == inv0 + 1,
              (out, wakes))

        print("W2. a pod that moved")
        wakes["dead_first"] = True
        inv0 = wakes["invalidated"]
        out = await MC.call(pool, EID, f"mcp:{APP}/render", {"size": 64})
        check("W2 one failed connect, one re-wake, and the call lands",
              out.get("status") == "ok" and wakes["invalidated"] == inv0 + 1, (out, wakes))

        print("S2. sync again, a name collision, mcp removed")
        got2 = await MC.sync_app(pool, EID, manifest)
        row2 = await MC.live_row(pool, EID, APP)
        check("S2 a second sync (an upgrade) keeps the connection and fetches the list again on next use",
              got2["status"] == "active" and row2["id"] == row["id"] and row2["tools_fetched_at"] is None,
              (got2, row2))
        await pool.execute(
            "INSERT INTO mcp_connections (entity_id, name, url, status) VALUES ($1, 'remote-one', "
            "'https://remote.example/mcp', 'draft')", EID)
        got3 = await MC.sync_app(pool, EID, dataclasses.replace(manifest, id="remote-one"))
        check("S3 a remote server already holding the app's name is left alone, and the result says so",
              got3["status"] == "skipped" and "remote" in got3["why"]
              and (await MC.live_row(pool, EID, "remote-one"))["url"].startswith("https://"), got3)

        print("U. uninstalled, or the manifest changed")
        (root / APP / "app.yaml").write_text(_app_yaml(path="/other"))
        why = await MC.verify(pool, EID, await MC.live_row(pool, EID, APP))
        check("U1 a manifest that no longer declares this path: the connection is refused",
              bool(why) and "no longer serves MCP" in why, why)
        (root / APP / "app.yaml").write_text(_app_yaml())
        await pool.execute("UPDATE installed_applications SET status = 'disabled' WHERE id = $1", APP)
        why = await MC.verify(pool, EID, await MC.live_row(pool, EID, APP))
        check("U2 an app that is not installed and active: refused", bool(why) and "not installed" in why, why)
        # Its row is still active (a disabled app keeps it; uninstall revokes
        # it only as a best effort): its files are refused as its tools are.
        n2 = len(FILE_REQS)
        try:
            got = await MC._own_images_inline(pool, EID, {"data": f"app://{APP}/files/bounces/mix.wav"})
            why = f"handed over: {str(got)[:80]}"
        except MC.McpConnectionError as e:
            why = str(e)
        check("U2 ...and so are its files: nothing is asked of it",
              "not installed" in why and len(FILE_REQS) == n2, (why, len(FILE_REQS) - n2))
        await pool.execute("UPDATE installed_applications SET status = 'active' WHERE id = $1", APP)
        got4 = await MC.sync_app(pool, EID, dataclasses.replace(manifest, mcp=M.McpSpec()))
        check("U3 an upgrade that drops mcp revokes the connection",
              got4 == {"name": APP, "status": "revoked"} and await MC.live_row(pool, EID, APP) is None, got4)
        await MC.sync_app(pool, EID, manifest)
        n = await MC.drop_app(pool, EID, APP)
        out = await MC.call(pool, EID, f"mcp:{APP}/render", {"size": 1})
        check("U4 drop_app (uninstall) revokes it, and a call is refused",
              n == 1 and out.get("status") != "ok" and "No MCP server" in out.get("result", ""), (n, out))
        check("U5 through all of it, nothing reached the egress gate or the proxy variables",
              not TRAP_HITS, TRAP_HITS)
    finally:
        await pool.close()


async def main(dsn: str) -> int:
    trap = await asyncio.start_server(_trap, "127.0.0.1", 0)
    trap_port = trap.sockets[0].getsockname()[1]
    os.environ["NAC_EGRESS_PORT"] = str(trap_port)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ[k] = f"http://127.0.0.1:{trap_port}"
    os.environ.pop("NO_PROXY", None)
    os.environ.pop("no_proxy", None)
    # The bridge reads a request of up to 192 MB (creative_mcp.MAX_BODY); aiohttp's
    # own default is 1 MiB.
    app = web.Application(client_max_size=192 * 1024 * 1024)
    app.router.add_post("/mcp", _app_mcp)
    app.router.add_get("/files/{rel:.+}", _app_file)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    app_port = site._server.sockets[0].getsockname()[1]
    try:
        with tempfile.TemporaryDirectory(prefix="mcp-app-") as td:
            tmp = pathlib.Path(td)
            manifest_checks(tmp)

            print("M. migrations/v221 on the full current schema, run twice over")
            from kernel_schema import _psql
            r1 = _psql(dsn, "-v", "ON_ERROR_STOP=1", "-f", str(MIGRATION))
            r2 = _psql(dsn, "-v", "ON_ERROR_STOP=1", "-f", str(MIGRATION))
            check("M1 v221 runs again on the full schema, and again", r1.returncode == 0 and r2.returncode == 0,
                  (r1.stderr[-400:], r2.stderr[-400:]))
            ok = _psql(dsn, "-c", "INSERT INTO mcp_connections (entity_id, name, url, app_id) "
                                         "VALUES (1, 'a', 'app://a/mcp', 'a')")
            check("M2 an app row at app://<its id>/... is admitted", ok.returncode == 0, ok.stderr[-200:])
            for sql, why in (("(1, 'b', 'http://b.example/m', NULL)", "a remote row still needs https"),
                             ("(1, 'c', 'https://c.example/m', 'c')", "an app row may not be https"),
                             ("(1, 'd', 'app://other/mcp', 'd')", "an app row names its own app"),
                             ("(1, 'e', 'app://e/mcp', NULL)", "app:// needs an app_id")):
                bad = _psql(dsn, "-c", "INSERT INTO mcp_connections (entity_id, name, url, app_id) "
                                              f"VALUES {sql}")
                check(f"M3 refused: {why}", "violates" in bad.stderr, bad.stderr[-200:])
            # Only the row M2 admitted (every M3 row was refused).
            _psql(dsn, "-c", "DELETE FROM mcp_connections WHERE entity_id = 1 AND name = 'a'")
            await service_checks(dsn, app_port, tmp)
    finally:
        await runner.cleanup()
        trap.close()
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    import signal

    from kernel_schema import kernel_schema_db, throwaway_server
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    SERVER = os.environ.get("TEST_PG_DSN") or os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
    if not SERVER and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        print("SKIP-LOUD: no test server (TEST_PG_DSN/PEER_TEST_DSN/TEST_DSN) and docker unavailable — "
              "this gate needs a real database for the connection service.")
        sys.exit(2)
    with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as _srv, \
            kernel_schema_db(_srv, entity_id=EID) as _db:
        rc = asyncio.run(main(_db))
    print("RESULT: ALL PASS" if FAILED == 0 else f"RESULT: {FAILED} FAILED")
    sys.exit(rc)
