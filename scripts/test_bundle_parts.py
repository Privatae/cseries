#!/usr/bin/env python3
"""Gate: an app bundle of any size goes up and comes down (the kernel
half).

The platform refused bundles over 50 MB, and one bridge frame could not carry
more than ~47 MiB of zip as base64 anyway; a store bundle over ~7.5 MiB could
not come DOWN either, because one answer has to fit this kernel's 10 MiB
bridge reader line. Now (with the matching bridge and controller changes):
  - request_full sends a webapp/appstore publish whose bundle is over
    BUNDLE_IN_BAND_MAX as bundle.upload_part parts, then the publish with
    upload_id + parts + sha256 instead of the bytes; a bundle that fits is
    sent exactly as before; a refused part stops the publish with a sentence;
  - _fetch_bundle_from_store reads a store bundle in slices until it has
    total_size bytes and checks its sha256; a bridge without slices (no
    total_size) is answered as before.

The real BridgeClient.request_full with its socket send scripted; no bridge.
Run: APP_ROOT=<repo> python3 scripts/test_bundle_parts.py
"""
import asyncio
import base64
import hashlib
import os
import sys
import types
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT / "brain"))
sys.path.insert(0, str(ROOT))
MAIN = types.ModuleType("server.main")
sys.modules["server.main"] = MAIN
FAILS: list[str] = []


def check(label, cond, detail=""):
    print(f"[bundle-parts] {'ok  ' if cond else 'FAIL'} {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


from server.bridge import bridge_client as BC  # noqa: E402

if not hasattr(BC, "stage_bundle_params"):
    check("bridge_client sends big bundles in parts (stage_bundle_params)", False)
    print(f"[bundle-parts] {len(FAILS)} FAILED")
    sys.exit(1)

from server.routes import applications as APPS  # noqa: E402


def client(answer):
    """A BridgeClient whose wire is `answer(op, params) -> envelope`."""
    c = BC.BridgeClient.__new__(BC.BridgeClient)
    c._connected = True
    c._pending = {}
    c._op_circuit_open_until = {}
    c._op_probing = set()
    c.sent = []
    n = {"id": 0}

    def _alloc():
        n["id"] += 1
        return n["id"]

    async def _send(msg):
        c.sent.append(msg)
        env = answer(msg["op"], msg["params"])
        fut = c._pending.get(msg["id"])
        asyncio.get_running_loop().call_soon(lambda: fut.done() or fut.set_result(env))

    c._alloc_id = _alloc
    c._send = _send
    return c


async def main():
    ok = lambda op, p: {"ok": True, "data": {"op": op}}  # noqa: E731

    # small: unchanged
    small = base64.b64encode(b"z" * 1024).decode()
    c = client(ok)
    await c.request_full("webapp.publish_bundle", {"site": "s", "zip_b64": small})
    check("a bundle that fits one frame is sent in-band, exactly as before",
          [m["op"] for m in c.sent] == ["webapp.publish_bundle"]
          and c.sent[0]["params"].get("zip_b64") == small)

    # big: staged
    raw = os.urandom(BC.BUNDLE_IN_BAND_MAX + BC.BUNDLE_PART_BYTES + 123)
    c = client(ok)
    env = await c.request_full("appstore.publish_bundle",
                               {"app_id": "a", "version": "1.0.0",
                                "zip_b64": base64.b64encode(raw).decode()})
    ops = [m["op"] for m in c.sent]
    parts = [m["params"] for m in c.sent if m["op"] == "bundle.upload_part"]
    pub = c.sent[-1]["params"]
    whole = b"".join(base64.b64decode(p["data_b64"]) for p in sorted(parts, key=lambda p: p["seq"]))
    check("a bundle over one frame goes up in parts, then the publish names them",
          ops[-1] == "appstore.publish_bundle" and len(parts) == 4 and env.get("ok")
          and "zip_b64" not in pub and pub.get("parts") == 4
          and pub.get("sha256") == hashlib.sha256(raw).hexdigest()
          and all(p["upload_id"] == pub["upload_id"] for p in parts), repr(ops))
    check("...the parts are the bundle, byte for byte, none over a part's size",
          whole == raw and all(len(base64.b64decode(p["data_b64"])) <= BC.BUNDLE_PART_BYTES
                               for p in parts))
    check("...and the publish keeps its other fields", pub.get("app_id") == "a"
          and pub.get("version") == "1.0.0")

    # a refused part stops the publish
    def refuse_second(op, p):
        if op == "bundle.upload_part" and p["seq"] == 1:
            return {"ok": False, "error": "disk full"}
        return {"ok": True, "data": {}}
    c = client(refuse_second)
    env = await c.request_full("webapp.publish_bundle",
                               {"zip_b64": base64.b64encode(raw).decode()})
    check("a refused part stops the publish and says which part and why",
          env.get("ok") is False and "part 2" in env.get("error", "")
          and "disk full" in env.get("error", "")
          and "webapp.publish_bundle" not in [m["op"] for m in c.sent], repr(env))
    check("publishes wait as long ops", {"webapp.publish_bundle", "appstore.publish_bundle",
                                         "bundle.upload_part"} <= BC.LONG_OPS)

    # store fetch in slices
    bundle = os.urandom(APPS._STORE_SLICE_BYTES * 2 + 777)
    sha = hashlib.sha256(bundle).hexdigest()

    def store(op, p):
        assert op == "appstore.fetch_bundle"
        off, ln = p["offset"], p["length"]
        return {"ok": True, "data": {"app_id": "big", "version": "2.0.0",
                                     "bundle_b64": base64.b64encode(bundle[off:off + ln]).decode(),
                                     "offset": off, "total_size": len(bundle), "sha256": sha}}
    c = client(store)
    MAIN.get_bridge_client = lambda: c
    got = await APPS._fetch_bundle_from_store("big", None)
    fetches = [m["params"] for m in c.sent]
    check("a store bundle over one answer comes down in slices, whole and checked",
          got == bundle and len(fetches) == 3
          and all(f["length"] <= APPS._STORE_SLICE_BYTES for f in fetches)
          and fetches[1].get("version") == "2.0.0", repr([f.get("offset") for f in fetches]))

    def old_bridge(op, p):
        return {"ok": True, "data": {"app_id": "small", "version": "1",
                                     "bundle_b64": base64.b64encode(b"whole").decode()}}
    c = client(old_bridge)
    MAIN.get_bridge_client = lambda: c
    check("a bridge without slices is answered as before (the whole bundle)",
          await APPS._fetch_bundle_from_store("small", None) == b"whole")

    def bad_sha(op, p):
        env = store(op, p)
        env["data"]["sha256"] = "0" * 64
        return env
    c = client(bad_sha)
    MAIN.get_bridge_client = lambda: c
    try:
        await APPS._fetch_bundle_from_store("big", None)
        check("a bundle whose sha256 does not match is refused", False)
    except APPS._StoreError as e:
        check("a bundle whose sha256 does not match is refused", "whole" in str(e), str(e))

    check("the kernel's own early guards match the platform's bound, not a flat 50 MB",
          APPS._SITE_MAX_ZIP == 200 * 1024 * 1024
          and APPS._BUNDLE_ZIP_MAX == 200 * 1024 * 1024 + 50_000 * 1024,
          f"{APPS._SITE_MAX_ZIP} {APPS._BUNDLE_ZIP_MAX}")


asyncio.new_event_loop().run_until_complete(main())
if FAILS:
    print(f"[bundle-parts] {len(FAILS)} FAILED")
    sys.exit(1)
print("[bundle-parts] all checks passed")
