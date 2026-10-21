#!/usr/bin/env python3
"""A standalone Host's Check for updates asks npm, and Settings says what it got.

GET /api/update/check (server/routes/update.py) used to ask only the platform
bridge, so on a Host with no platform it failed and Settings said "You're up
to date". Pinned here:

  1. with a NoPlatform bridge the check asks npm's latest for cseries and
     answers {available, latest, current, channel: "installer", message}:
     newer, equal and older versions compare by number, not as text;
  2. a registry that fails, or a version that cannot be compared (a "dev"
     build), comes back as `error` with available false;
     A registry that has not answered by the check's own deadline is an
     error too, given then;
  3. the registry is read at NPM_LATEST_URL over https with a short timeout,
     and its "version" field is the answer; a redirect is followed only to
     https on the registry's own host, never to plain http or elsewhere;
  4. a bridge that is not connected on a Host that is not a network member is
     standalone too; a member's or a connected bridge's check still goes to
     the bridge, and never to npm;
  5. the Settings app shows each answer (scripts/test_update_check_standalone.js).

Container-free: APP_ROOT=<repo> python3 scripts/test_update_check_standalone.py
The HTTP call and the bridge are stand-ins; nothing leaves the machine.
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1]).resolve()
sys.path[:0] = [str(ROOT), str(ROOT / "brain")]

FAILS: list[str] = []


def check(cond, msg, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + msg + (f"\n        {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(msg)


class ConnectedBridge:
    connected = True

    def __init__(self):
        self.ops = []

    async def request(self, op, params=None, *, raise_on_infra=False):
        self.ops.append(op)
        return {"tag": "0.1.9", "available": True}


class DownBridge:
    connected = False


async def main():
    # server.main serves the dashboard from /app and will not import outside
    # the image; the route asks it for one thing, the bridge client.
    import types
    server_main = types.ModuleType("server.main")
    server_main.get_bridge_client = lambda: None
    sys.modules["server.main"] = server_main
    from fastapi import HTTPException
    from server.bridge import membership
    from server.bridge.no_platform import NoPlatform
    from server.routes import update

    real_bridge = server_main.get_bridge_client
    real_npm, real_tag = update._npm_latest, update._read_local_tag
    real_cache = update.VERSION_CACHE_PATH
    real_status = membership.Membership.status
    npm_calls = []

    def npm(answer):
        def _latest():
            npm_calls.append(1)
            if isinstance(answer, Exception):
                raise answer
            return answer
        return _latest

    try:
        server_main.get_bridge_client = lambda: NoPlatform()
        update._read_local_tag = lambda: "0.1.4"

        # 1
        for latest, want in (("0.1.5", True), ("0.1.4", False), ("0.1.10", True), ("0.1.3", False)):
            update._npm_latest = npm(latest)
            out = await update.check_updates()
            check(out.get("available") is want and out.get("latest") == latest
                  and out.get("current") == "0.1.4" and out.get("channel") == "installer"
                  and out.get("message") == "Run npx cseries update on this machine"
                  and "error" not in out,
                  f"standalone: npm says {latest}, running 0.1.4 -> available={want}", str(out))

        # 2
        update._npm_latest = npm(OSError("timed out"))
        out = await update.check_updates()
        check(out.get("available") is False and "timed out" in (out.get("error") or ""),
              "an unreachable registry comes back as an error", str(out))
        update._npm_latest = npm("0.1.5")
        update._read_local_tag = lambda: "dev"
        out = await update.check_updates()
        check(out.get("available") is False and "dev" in (out.get("error") or ""),
              "a dev build's version is reported as not comparable", str(out))
        update._read_local_tag = lambda: "0.1.4"

        # 2b — a registry that keeps the check waiting past the deadline is
        # an error, answered at the deadline, not when the registry finishes.
        import time
        real_deadline = getattr(update, "NPM_DEADLINE_S", None)

        def slow():
            time.sleep(0.6)
            return "0.1.5"

        update._npm_latest = slow
        update.NPM_DEADLINE_S = 0.1
        t0 = time.monotonic()
        out = await update.check_updates()
        took = time.monotonic() - t0
        if real_deadline is None:
            del update.NPM_DEADLINE_S
        else:
            update.NPM_DEADLINE_S = real_deadline
        check(out.get("available") is False and "in time" in (out.get("error") or "") and took < 0.5,
              "a registry that does not answer by the deadline is an error, answered then",
              f"{took:.2f}s {out}")
        check(real_deadline is not None and real_deadline <= 30,
              "the whole check has a deadline of its own", repr(real_deadline))

        # 3 — the HTTP and HTTPS handlers are stand-ins at the class, so no
        # request can leave the machine whichever opener the route builds.
        import http.client
        import io
        import urllib.request
        import urllib.response
        latest_url = "https://registry.npmjs.org/cseries/latest"

        def answer(url, code, body=b"", location=None):
            hdrs = http.client.HTTPMessage()
            if location:
                hdrs["Location"] = location
            r = urllib.response.addinfourl(io.BytesIO(body), hdrs, url, code)
            r.msg = http.client.responses.get(code, "")
            return r

        def version_body(v):
            return json.dumps({"name": "cseries", "version": v}).encode()

        seen = []
        first = {}

        def https_open(self, req):
            seen.append(("https", req.full_url, req.timeout))
            if req.full_url == latest_url and first.get("location"):
                return answer(req.full_url, 302, location=first["location"])
            if req.full_url == latest_url:
                return answer(req.full_url, 200, version_body("0.1.4"))
            return answer(req.full_url, 200, version_body("9.9.9"))

        def http_open(self, req):
            seen.append(("http", req.full_url, req.timeout))
            return answer(req.full_url, 200, version_body("9.9.9"))

        real_https = urllib.request.HTTPSHandler.https_open
        real_http = urllib.request.HTTPHandler.http_open
        urllib.request.HTTPSHandler.https_open = https_open
        urllib.request.HTTPHandler.http_open = http_open

        def run():
            seen.clear()
            try:
                return real_npm(), None
            except Exception as e:  # noqa: BLE001
                return None, e

        try:
            got, err = run()
            check(got == "0.1.4", "npm's version field is the answer", repr((got, err)))
            check(seen and seen[0][:2] == ("https", latest_url),
                  "the registry's latest for cseries is read over https", str(seen))
            check(seen and seen[0][2] is not None and seen[0][2] <= 10,
                  "with a short timeout", str(seen))

            first["location"] = "http://registry.npmjs.org/cseries/latest"
            got, err = run()
            check(err is not None and not any(s[0] == "http" for s in seen),
                  "a redirect to plain http is refused, and http is never asked", repr((got, err, seen)))

            first["location"] = "https://registry.example.net/cseries/latest"
            got, err = run()
            check(err is not None and len(seen) == 1,
                  "a redirect to another host is refused, and that host is never asked", repr((got, err, seen)))

            first["location"] = "https://registry.npmjs.org/cseries/latest?again=1"
            got, err = run()
            check(got == "9.9.9" and err is None and len(seen) == 2,
                  "a redirect on the registry itself, over https, is followed", repr((got, err, seen)))
        finally:
            urllib.request.HTTPSHandler.https_open = real_https
            urllib.request.HTTPHandler.http_open = real_http

        # 4
        update._npm_latest = npm("0.1.5")
        server_main.get_bridge_client = lambda: DownBridge()
        membership.Membership.status = lambda self: {"member": False}
        out = await update.check_updates()
        check(out.get("channel") == "installer",
              "a bridge that is down on a non-member Host is standalone", str(out))
        membership.Membership.status = lambda self: {"member": True}
        n = len(npm_calls)
        update.VERSION_CACHE_PATH = Path(tempfile.mkdtemp()) / "absent" / "version-cache.json"
        try:
            out = await update.check_updates()
            code = 200
        except HTTPException as e:
            code = e.status_code
        check(code == 500 and len(npm_calls) == n,
              "a member Host whose bridge is down keeps the platform path", f"{code} {out}")

        bridge = ConnectedBridge()
        server_main.get_bridge_client = lambda: bridge
        cache_dir = Path(tempfile.mkdtemp())
        update.VERSION_CACHE_PATH = cache_dir / "version-cache.json"
        real_dir = update.UPDATE_DIR
        update.UPDATE_DIR = cache_dir
        try:
            out = await update.check_updates()
        finally:
            update.UPDATE_DIR = real_dir
        check(bridge.ops == ["pc.version_query"] and out.get("tag") == "0.1.9"
              and len(npm_calls) == n and out.get("channel") != "installer",
              "a connected bridge is asked as before, and npm is not", str(out))
    finally:
        server_main.get_bridge_client = real_bridge
        update._npm_latest, update._read_local_tag = real_npm, real_tag
        update.VERSION_CACHE_PATH = real_cache
        membership.Membership.status = real_status

    # 5
    node = shutil.which("node")
    if not node:
        check(False, "node is needed for the Settings half of this gate")
        return
    r = subprocess.run([node, str(ROOT / "scripts/test_update_check_standalone.js")],
                       capture_output=True, text=True, timeout=60)
    print(r.stdout.rstrip())
    check(r.returncode == 0, "Settings shows each answer as specified", r.stderr[-800:])


asyncio.run(main())
if FAILS:
    print(f"\nFAIL: {len(FAILS)} check(s)")
    sys.exit(1)
print("\nall checks passed")
