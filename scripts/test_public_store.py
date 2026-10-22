#!/usr/bin/env python3
"""The free public App Store reader (server/applications/public_store.py).

  1. The catalog's signature is the release scheme under kind cseries-store:
     the Host pins exactly verify.js's two keys, and a good catalog verifies
     while a tampered one, one signed for another kind, one signed by a key
     that is not pinned and an unsigned one are refused.
  2. The catalog's shape is checked: a bad app id, a repeated id, a sha256
     that is not 64 hex, a bundle URL that is not http(s), an image not
     pinned by digest and a version that is not X.Y.Z each refuse it.
  3. Against a local HTTP server, through a redirect as a GitHub release's
     latest/download answers: the list, a bundle in slices, the icon, a free
     entitlement and purchase; a bundle whose bytes do not match the signed
     sha256 is refused and not kept; a catalog older than one already read is
     refused; a catalog that cannot be read again is served as read earlier.
  4. The router: a standalone Host (NoPlatform) and a member door that is not
     reached or does not offer the op are answered by the public store; a door
     that answers is used; the door's list gains the public apps it lacks; a
     door that does not carry an app gives way to the public store for it; a
     door that exists but is not answering is never routed around for an app
     outside the public catalog (the entitlement stays unverifiable).
  5. End to end, in process: the install route takes {app_id} on a
     standalone Host, the bundle comes from the public store and installs
     through installer.install_bundle, signature and provenance checked
     against the trust store as for any other source.
  6. The first start: on a standalone Host the boot pass installs the app the
     catalog marks default, once (an uninstall holds), and leaves the others;
     server/main.py starts that pass.
  7. The first start when the store does not answer: a default app whose
     download failed is not recorded as offered, and the boot pass waits and
     tries again until it installs; the store's copy of an app never replaces
     one this Host built itself under the same id, while an app from the store
     still upgrades.
  8. The start-time upgrade leaves a runtime app on its installed version when
     the new one reserves more memory (an over-plan refusal would uninstall
     it), and still upgrades one that reserves the same.

Container-free: python3 scripts/test_public_store.py (needs cryptography, yaml).
"""
import asyncio
import base64
import hashlib
import http.server
import json
import os
import re
import sys
import tempfile
import threading
import types
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT / "brain"))
sys.path.insert(0, str(ROOT))

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

FAILS: list[str] = []


def check(label, cond, detail=""):
    print(f"[public-store] {'ok  ' if cond else 'FAIL'} {label}"
          + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


# server.main is the kernel's whole web app; a stand-in carries the two names
# the store and the installer reach for.
MAIN = types.ModuleType("server.main")
MAIN.get_bridge_client = lambda: None
SHELL: list = []


async def _broadcast(msg):
    SHELL.append(msg)
MAIN.broadcast = _broadcast
sys.modules["server.main"] = MAIN

try:
    from server.applications import public_store as PS  # noqa: E402
except ImportError as e:
    check("server/applications/public_store.py exists", False, str(e))
    sys.exit(1)
from server.applications import bundles  # noqa: E402
from server.bridge.no_platform import NoPlatform  # noqa: E402

WORK = Path(tempfile.mkdtemp(prefix="pubstore-"))


def key_b64(k):
    raw = k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def sign(k, kind, body):
    v = json.loads(body)["version"]
    return base64.b64encode(k.sign(PS.statement(kind, v, hashlib.sha256(body).hexdigest()))).decode()


def refused(fn):
    try:
        fn()
    except PS.StoreError as e:
        return str(e)
    return None


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── 1. the signature ─────────────────────────────────────────────────────────

js = (ROOT / "packages/cseries/src/verify.js").read_text(encoding="utf-8")
js_keys = re.findall(r"'([A-Za-z0-9+/=]{44})'", js[js.index("BASTION_RELEASE_KEYS = ["):js.index("];")])
check("the Host pins exactly verify.js's release keys", tuple(js_keys) == PS.RELEASE_KEYS, js_keys)
check("the statement is the release scheme's, under kind cseries-store",
      PS.statement(PS.KIND, "1.2.3", "ab" * 32) ==
      b"privatae-release-signature-v1\ncseries-store\n1.2.3\n" + b"ab" * 32 + b"\n")
check("the signature sits at <catalog>.<16 hex of its sha256>.sig",
      PS.signature_url("https://example.test/r/catalog.json", b"{}") ==
      "https://example.test/r/catalog.json." + hashlib.sha256(b"{}").hexdigest()[:16] + ".sig")

rel = Ed25519PrivateKey.generate()          # the test release key
other = Ed25519PrivateKey.generate()
body = json.dumps({"version": "0.4.0", "apps": []}).encode()
good = sign(rel, PS.KIND, body)
check("a catalog signed for cseries-store verifies",
      PS.verify_signed(body, good, keys=[key_b64(rel)]) == json.loads(body))
cases = {
    "a tampered catalog": (body.replace(b"0.4.0", b"0.4.1"), good, [key_b64(rel)]),
    "a catalog signed for the cloud box's kind": (body, sign(rel, "cseries-cloud", body), [key_b64(rel)]),
    "a catalog signed by a key that is not pinned": (body, sign(other, PS.KIND, body), [key_b64(rel)]),
    "an unsigned catalog": (body, "", [key_b64(rel)]),
    "a catalog with no version": (b'{"apps": []}', good, [key_b64(rel)]),
}
for what, (b, s, ks) in cases.items():
    check(f"{what} is refused", refused(lambda: PS.verify_signed(b, s, keys=ks)) is not None)
check("the test key is not one of the pinned keys",
      refused(lambda: PS.verify_signed(body, good)) is not None)

# ── 2. the shape ─────────────────────────────────────────────────────────────

D = "sha256:" + "c" * 64


def entry(**kw):
    e = {"app_id": "gadget-notes", "name": "Gadget Notes", "summary": "notes", "publisher": "C-Series",
         "version": "1.2.3", "provenance": "standard", "is_default": False, "price_micros": 0,
         "bundle": {"url": "gadget-notes-1.2.3.cseries", "sha256": "d" * 64, "size": 10},
         "images": {"python": "ghcr.io/example/runtime-python" + "@" + D},
         "license": "AGPL-3.0-only", "source": "https://example.test/src/gadget-notes"}
    e.update(kw)
    return e


check("a well-formed catalog validates",
      set(PS.validate_catalog({"version": "0.4.0", "apps": [entry()]})["apps"]) == {"gadget-notes"})
bad = [
    ("an app id that is not a manifest id", {"version": "0.4.0", "apps": [entry(app_id="../x")]}),
    ("an app listed twice", {"version": "0.4.0", "apps": [entry(), entry()]}),
    ("a sha256 that is not 64 hex", {"version": "0.4.0",
                                     "apps": [entry(bundle={"url": "a", "sha256": "d" * 10, "size": 1})]}),
    ("a bundle URL that is not http(s)", {"version": "0.4.0",
                                          "apps": [entry(bundle={"url": "file:///etc/passwd",
                                                                 "sha256": "d" * 64, "size": 1})]}),
    ("an image not pinned by digest", {"version": "0.4.0",
                                       "apps": [entry(images={"python": "ghcr.io/example/x:latest"})]}),
    ("an image family the runtime does not know", {"version": "0.4.0",
                                                   "apps": [entry(images={"ubuntu": "a" + "@" + D})]}),
    ("a bundle bigger than the cap", {"version": "0.4.0",
                                      "apps": [entry(bundle={"url": "a", "sha256": "d" * 64,
                                                             "size": PS.MAX_BUNDLE_BYTES + 1})]}),
    ("a catalog version that is not X.Y.Z", {"version": "4", "apps": []}),
]
for what, doc in bad:
    check(f"a catalog with {what} is refused", refused(lambda: PS.validate_catalog(doc)) is not None)

# ── 3. against a local HTTP server ───────────────────────────────────────────

SRV = WORK / "srv"
(SRV / "rel").mkdir(parents=True)


class _Handler(http.server.SimpleHTTPRequestHandler):
    """Serves SRV; /latest/<x> answers 302 to /rel/<x>, as a GitHub
    release's latest/download does."""

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(SRV), **kw)

    def do_GET(self):
        if self.path.startswith("/latest/"):
            self.send_response(302)
            self.send_header("Location", "/rel/" + self.path[len("/latest/"):])
            self.end_headers()
            return
        return super().do_GET()

    def log_message(self, *a):
        pass


httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{httpd.server_address[1]}"
CATALOG_URL = f"{BASE}/latest/catalog.json"

apps_key = Ed25519PrivateKey.generate()     # stands in for the C-Series apps key
apps_pem = apps_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption())
apps_pub = apps_key.public_key().public_bytes(serialization.Encoding.Raw,
                                              serialization.PublicFormat.Raw)
ICON = b"\x89PNG\r\n\x1a\n" + b"invented-icon-bytes" * 4


def make_bundle(app_id="gadget-notes", version="1.2.3", extra=b""):
    src = WORK / f"src-{app_id}-{version}"
    src.mkdir(exist_ok=True)
    (src / "app.yaml").write_text(
        f"id: {app_id}\nname: Gadget Notes\ndescription: A small notes page.\nicon: icon.png\n"
        f'version: "{version}"\nprovenance: standard\nentrypoint: index.html\npermissions: []\n')
    (src / "index.html").write_bytes(b"<!doctype html><title>notes</title>" + extra)
    (src / "icon.png").write_bytes(ICON)
    return bundles.sign(bundles.pack(src), apps_pem)


BIG = os.urandom(300_000)                    # makes the bundle span several slices
bundle = make_bundle(extra=BIG)
bsha = hashlib.sha256(bundle).hexdigest()
(SRV / "rel" / "gadget-notes-1.2.3.cseries").write_bytes(bundle)


def publish(version, apps, key=rel, kind=PS.KIND):
    doc = {"version": version, "generated_at": "2026-01-02T03:04:05Z", "apps": apps}
    b = json.dumps(doc).encode()
    for p in (SRV / "rel").glob("catalog.json*"):
        p.unlink()
    (SRV / "rel" / "catalog.json").write_bytes(b)
    (SRV / "rel" / f"catalog.json.{hashlib.sha256(b).hexdigest()[:16]}.sig").write_text(sign(key, kind, b))


good_entry = entry(bundle={"url": "gadget-notes-1.2.3.cseries", "sha256": bsha, "size": len(bundle)})
publish("0.4.0", [good_entry])

keys_file = WORK / "keys.txt"
keys_file.write_text(key_b64(rel) + "\n")
os.environ["CSERIES_STORE_URL"] = CATALOG_URL
os.environ["CSERIES_STORE_KEYS_FILE"] = str(keys_file)
os.environ["CSERIES_STORE_CACHE"] = str(WORK / "cache")


async def part3():
    st = PS.PublicStore(CATALOG_URL, cache_dir=WORK / "cache")
    env = await st.answer("appstore.list", {})
    rows = (env.get("data") or {}).get("apps") or []
    check("the list answers through the redirect, in the platform's row shape",
          env.get("ok") and len(rows) == 1 and rows[0]["app_id"] == "gadget-notes"
          and rows[0]["latest_version"] == "1.2.3" and rows[0]["price_micros"] == 0
          and rows[0]["size_bytes"] == len(bundle) and rows[0]["is_default"] is False
          and rows[0]["provenance"] == "standard", env)
    check("the licence id rides as license_id, never as the manifest-terms field the store page reads",
          rows and "license" not in rows[0] and rows[0].get("license_id") == "AGPL-3.0-only"
          and rows[0].get("source_url") == "https://example.test/src/gadget-notes", rows)
    got, off = bytearray(), 0
    n = 0
    d = {}
    while True:
        e = await st.answer("appstore.fetch_bundle", {"app_id": "gadget-notes", "offset": off,
                                                      "length": 100_000})
        d = e.get("data") or {}
        chunk = base64.b64decode(d.get("bundle_b64") or "")
        got += chunk
        n += 1
        if not chunk or len(got) >= d.get("total_size", 0):
            break
        off = len(got)
    check("a bundle comes down in slices, whole, with the signed sha256 beside it",
          bytes(got) == bundle and n == 4 and d.get("sha256") == bsha
          and d.get("version") == "1.2.3", (n, len(got), len(bundle)))
    check("the bundle is kept, under its sha256",
          (WORK / "cache" / f"{bsha}.cseries").read_bytes() == bundle)
    e = await st.answer("appstore.fetch_bundle", {"app_id": "gadget-notes", "version": "9.9.9"})
    check("a version the catalog does not carry is a bundle fetch 404",
          not e.get("ok") and str(e.get("error", "")).startswith("bundle fetch 404"), e)
    e = await st.answer("appstore.fetch_bundle", {"app_id": "not-listed"})
    check("an app the catalog does not carry is a bundle fetch 404",
          not e.get("ok") and str(e.get("error", "")).startswith("bundle fetch 404"), e)
    e = await st.answer("appstore.icon", {"app_id": "gadget-notes"})
    check("the icon is the bundle's own, from its manifest's icon file",
          e.get("ok") and base64.b64decode(e["data"]["icon_b64"]) == ICON, e)
    e = await st.answer("appstore.entitlement.check", {"app_id": "gadget-notes"})
    check("every public app is free: entitled, price 0",
          e.get("ok") and e["data"]["entitled"] is True and e["data"]["price_micros"] == 0, e)
    e = await st.answer("appstore.purchase", {"app_id": "gadget-notes"})
    check("a purchase is the free path", e.get("ok") and e["data"]["is_free"] is True, e)

    # A bundle whose bytes are not the signed ones.
    other_bundle = make_bundle("other-notes", "2.0.0")
    (SRV / "rel" / "other-notes-2.0.0.cseries").write_bytes(other_bundle + b"x")
    publish("0.4.1", [good_entry, entry(app_id="other-notes", version="2.0.0",
                                        bundle={"url": "other-notes-2.0.0.cseries",
                                                "sha256": hashlib.sha256(other_bundle).hexdigest(),
                                                "size": len(other_bundle) + 1})])
    st2 = PS.PublicStore(CATALOG_URL, cache_dir=WORK / "cache")
    e = await st2.answer("appstore.fetch_bundle", {"app_id": "other-notes"})
    check("a bundle that does not match the signed sha256 is refused",
          not e.get("ok") and "signed catalog says" in str(e.get("error")), e)
    check("and nothing of it is kept",
          not (WORK / "cache" / f"{hashlib.sha256(other_bundle).hexdigest()}.cseries").exists()
          and not list((WORK / "cache").glob(".dl-*")))

    # A catalog older than one already read from this URL.
    publish("0.3.9", [good_entry])
    st3 = PS.PublicStore(CATALOG_URL, cache_dir=WORK / "cache")
    e = await st3.answer("appstore.list", {})
    check("a signed catalog older than one already read is refused",
          not e.get("ok") and "older than" in str(e.get("error")), e)

    # One that cannot be read again is served as read earlier.
    clock = [1000.0]
    st4 = PS.PublicStore(CATALOG_URL, cache_dir=WORK / "cache", now=lambda: clock[0])
    publish("0.4.1", [good_entry])
    first = await st4.answer("appstore.list", {})
    publish("0.4.2", [good_entry], key=other)        # not trusted: the read fails
    clock[0] += PS.CATALOG_TTL_S + 1
    again = await st4.answer("appstore.list", {})
    check("a catalog that cannot be read again is served as read earlier",
          first.get("ok") and again.get("ok") and again["data"] == first["data"], again)
    clock[0] += PS.STALE_MAX_S + 1
    late = await st4.answer("appstore.list", {})
    check("but not forever", not late.get("ok") and late.get("error_kind") == "infra", late)
    publish("0.4.1", [good_entry])

    # The wrong kind, served for real, is refused at the read.
    publish("0.4.3", [good_entry], kind="cseries-cloud")
    st5 = PS.PublicStore(CATALOG_URL, cache_dir=WORK / "cache5")
    e = await st5.answer("appstore.list", {})
    check("a served catalog signed for another kind is refused",
          not e.get("ok") and "does not verify" in str(e.get("error")), e)
    publish("0.4.1", [good_entry])

run(part3())

# ── 3b. how old the catalog is, and its rollback record ──────────────────────
#
# A catalog's signature says who made it, not when, so whoever serves the URL
# could keep serving the newest one a Host has read. The signed issued_at lets
# the Host say so: past 45 days the App Store shows a notice, and still lists
# and installs (a quiet stretch between releases must not empty every store).
# A Host that cannot keep its rollback record says that too, rather than only
# logging it.

DAY = 86400
ISSUED = 1_790_000_000                      # 2026-09-21T14:13:20Z


def publish_issued(version, issued=None):
    doc = {"version": version, "generated_at": "2026-01-02T03:04:05Z", "apps": [good_entry]}
    if issued is not None:
        doc["issued_at"] = issued
    b = json.dumps(doc).encode()
    for p in (SRV / "rel").glob("catalog.json*"):
        p.unlink()
    (SRV / "rel" / "catalog.json").write_bytes(b)
    (SRV / "rel" / f"catalog.json.{hashlib.sha256(b).hexdigest()[:16]}.sig").write_text(sign(rel, PS.KIND, b))


def kinds(env):
    return [n.get("kind") for n in ((env.get("data") or {}).get("notices") or [])]


async def part3b():
    check("issued_at is read from the signed catalog as Unix seconds",
          PS.validate_catalog({"version": "0.4.0", "apps": [], "issued_at": "2026-09-21T14:13:20Z"})
          ["issued_at"] == ISSUED)
    for bad_t in ("2026-09-21", "2026-09-21T14:13:20+02:00", "2026-13-41T99:99:99Z", 1790000000):
        check(f"an issued_at of {bad_t!r} refuses the catalog",
              refused(lambda: PS.validate_catalog({"version": "0.4.0", "apps": [], "issued_at": bad_t}))
              is not None)
    check("a catalog from before issued_at existed still validates, undated",
          PS.validate_catalog({"version": "0.4.0", "apps": []})["issued_at"] is None)

    stamp = "2026-09-21T14:13:20Z"
    publish_issued("0.5.0", stamp)
    fresh = PS.PublicStore(CATALOG_URL, cache_dir=WORK / "cache3b", wall=lambda: ISSUED + 10 * DAY)
    e = await fresh.answer("appstore.list", {})
    check("a catalog issued 10 days ago lists with no notice",
          e.get("ok") and e["data"]["apps"] and kinds(e) == [], e)
    old = PS.PublicStore(CATALOG_URL, cache_dir=WORK / "cache3b", wall=lambda: ISSUED + 46 * DAY)
    e = await old.answer("appstore.list", {})
    note = ((e.get("data") or {}).get("notices") or [{}])[0]
    check("one issued 46 days ago still lists, with a notice saying how old it is and what that can mean",
          e.get("ok") and [a["app_id"] for a in e["data"]["apps"]] == ["gadget-notes"] and
          kinds(e) == ["catalog_old"] and "2026-09-21" in note.get("text", "") and
          "46 days" in note.get("text", "") and "no release" in note.get("text", ""), e)
    e = await old.answer("appstore.fetch_bundle", {"app_id": "gadget-notes", "length": 10})
    check("and its apps still install (the bundle comes down)", e.get("ok") and e["data"]["sha256"] == bsha, e)
    publish_issued("0.5.1")
    undated = PS.PublicStore(CATALOG_URL, cache_dir=WORK / "cache3b", wall=lambda: ISSUED)
    e = await undated.answer("appstore.list", {})
    check("a catalog that does not say when it was issued lists with a notice saying so",
          e.get("ok") and kinds(e) == ["catalog_undated"], e)

    # The rollback record cannot be kept: the cache "folder" is a file.
    blocker = WORK / "not-a-folder"
    blocker.write_text("x")
    publish_issued("0.5.2", stamp)
    stuck = PS.PublicStore(CATALOG_URL, cache_dir=blocker, wall=lambda: ISSUED + DAY)
    e = await stuck.answer("appstore.list", {})
    note = ((e.get("data") or {}).get("notices") or [{}])[-1]
    check("a Host that cannot keep its rollback record lists, and says it cannot",
          e.get("ok") and kinds(e) == ["rollback_unrecorded"] and str(blocker) in note.get("text", ""), e)
    ok_again = PS.PublicStore(CATALOG_URL, cache_dir=WORK / "cache3b", wall=lambda: ISSUED + DAY)
    e = await ok_again.answer("appstore.list", {})
    check("a writable record gives no such notice", e.get("ok") and kinds(e) == [], e)

    # A member Host's list carries the public catalog's notices with the door's apps.
    member = PS.StoreRouter(Door({"appstore.list": {"ok": True, "data": {"apps": [
        {"app_id": "member-app", "latest_version": "1.0.0"}]}}}), store=old)
    e = await member.request_full("appstore.list", {})
    check("the door's list carries the public catalog's notices too",
          e.get("ok") and kinds(e) == ["catalog_old"] and
          [a["app_id"] for a in e["data"]["apps"]] == ["member-app", "gadget-notes"], e)
    publish("0.5.3", [good_entry])

# ── 4. the router ────────────────────────────────────────────────────────────


class Door:
    """A member door: connected, answering from `answers` (op -> envelope)."""

    network_member = True

    def __init__(self, answers, connected=True):
        self.answers = answers
        self._connected = connected
        self.sent = []

    async def request_full(self, op, params=None):
        self.sent.append(op)
        a = self.answers.get(op)
        if a is None:
            return {"ok": False, "error": f"not offered by the network: {op}", "error_kind": "infra"}
        return a(params) if callable(a) else a


async def part4():
    PS._stores.clear()
    r = PS.store_client(NoPlatform())
    check("a standalone Host's App Store client is connected (the public store)", r._connected)
    e = await r.request_full("appstore.list", {})
    check("a standalone Host's list is the public catalog",
          e.get("ok") and [a["app_id"] for a in e["data"]["apps"]] == ["gadget-notes"], e)
    e = await r.request_full("appstore.entitlement.check", {"app_id": "gadget-notes"})
    check("a standalone Host is entitled to a public app", e.get("ok") and e["data"]["entitled"], e)
    e = await r.request_full("appstore.entitlement.check", {"app_id": "own-thing"})
    check("on a standalone Host an app outside the catalog is free, as an unlisted app is",
          e.get("ok") and e["data"]["entitled"] is False and e["data"]["price_micros"] == 0, e)
    e = await r.request_full("inbox.send", {})
    check("an op that is not an App Store op goes to the bridge client unchanged",
          not e.get("ok") and "no platform" in str(e.get("error")), e)

    infra = Door({"appstore.list": {"ok": False, "error": "timeout", "error_kind": "infra"},
                  "appstore.fetch_bundle": {"ok": False, "error": "timeout", "error_kind": "infra"}})
    r = PS.store_client(infra)
    e = await r.request_full("appstore.list", {})
    check("a door that times out gives way to the public store",
          e.get("ok") and infra.sent == ["appstore.list"]
          and [a["app_id"] for a in e["data"]["apps"]] == ["gadget-notes"], e)
    e = await r.request_full("appstore.fetch_bundle", {"app_id": "gadget-notes", "length": 10})
    check("and so does its bundle fetch", e.get("ok") and e["data"]["sha256"] == bsha, e)
    e = await r.request_full("appstore.icon", {"app_id": "gadget-notes"})
    check("an op the network does not offer gives way to the public store", e.get("ok"), e)
    e = await r.request_full("appstore.entitlement.check", {"app_id": "paid-thing"})
    check("a door that exists and is not answering is not routed around for an app outside the "
          "public catalog", not e.get("ok") and e.get("error_kind") == "infra", e)
    gone = PS.store_client(Door({}, connected=False))
    e = await gone.request_full("appstore.entitlement.check", {"app_id": "paid-thing"})
    check("nor is a member door that is not connected",
          not e.get("ok") and e.get("error_kind") == "infra", e)

    member_row = {"app_id": "member-app", "latest_version": "1.0.0", "price_micros": 5_000_000}
    answers = {
        "appstore.list": {"ok": True, "data": {"apps": [member_row]}},
        "appstore.entitlement.check": {"ok": True, "data": {"entitled": False,
                                                            "price_micros": 5_000_000}},
        "appstore.fetch_bundle": lambda p: ({"ok": False, "error": "bundle fetch 404"}
                                            if p.get("app_id") != "member-app" else
                                            {"ok": True, "data": {"bundle_b64": "AA=="}}),
    }
    door = Door(answers)
    r = PS.store_client(door)
    e = await r.request_full("appstore.list", {})
    ids = [a["app_id"] for a in (e.get("data") or {}).get("apps", [])]
    check("a door that answers is used, and its list gains the public apps it lacks",
          e.get("ok") and ids == ["member-app", "gadget-notes"], ids)
    e = await r.request_full("appstore.entitlement.check", {"app_id": "member-app"})
    check("the door's own entitlement answer stands", e == answers["appstore.entitlement.check"], e)
    e = await r.request_full("appstore.fetch_bundle", {"app_id": "member-app"})
    check("the door's bundle is used when it has it",
          (e.get("data") or {}).get("bundle_b64") == "AA==", e)
    e = await r.request_full("appstore.fetch_bundle", {"app_id": "gadget-notes", "length": 10})
    check("a door that does not carry a public app gives way to the public store for it",
          e.get("ok") and e["data"]["sha256"] == bsha, e)
    e = await r.request_full("appstore.fetch_bundle", {"app_id": "nowhere"})
    check("an app neither carries is the door's own refusal",
          not e.get("ok") and e.get("error") == "bundle fetch 404", e)

    os.environ["CSERIES_STORE_URL"] = "off"
    PS._stores.clear()
    r = PS.store_client(NoPlatform())
    check("with the public store off, a standalone Host has no App Store", not r._connected)
    os.environ["CSERIES_STORE_URL"] = CATALOG_URL
    PS._stores.clear()

run(part3b())
run(part4())

# ── 5. end to end: the install route on a standalone Host ────────────────────


class _Conn:
    def __init__(self):
        self.executed = []

    async def execute(self, *a):
        self.executed.append(a)

    async def fetch(self, *a):
        return []

    async def fetchrow(self, *a):
        return None

    async def fetchval(self, *a):
        return None


class _Pool:
    def __init__(self):
        self.conn = _Conn()

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self):
                return pool.conn

            async def __aexit__(self, *a):
                return False
        return _Ctx()


class _Request:
    def __init__(self, body):
        self.headers = {"content-type": "application/json"}
        self._body = body

    async def json(self):
        return self._body


async def part5():
    from server import brain_service
    from server.applications import installer, registry, trust_store
    from server.routes import applications as APPS

    MAIN.get_bridge_client = lambda: NoPlatform()
    PS._stores.clear()
    pool = _Pool()
    install_root = WORK / "apps"
    install_root.mkdir()
    saved = (brain_service.get_pool, installer.INSTALL_ROOT, trust_store.list_active_keys,
             registry.register, installer._check_entitlement)
    entitlement_asked = []
    real_check = installer._check_entitlement

    async def watched_check(app_id):
        entitlement_asked.append(app_id)
        return await real_check(app_id)

    async def keys(_pool):
        return [trust_store.SigningKey(
            fingerprint=hashlib.sha256(apps_pub).hexdigest(), public_key=apps_pub,
            publisher="C-Series", provenance_allowed=["native", "standard"],
            registered_at=datetime.now(timezone.utc), revoked_at=None)]

    async def register(_pool, manifest, authored_here=None):
        return registry.InstalledApp(
            id=manifest.id, name=manifest.name, version=manifest.version,
            provenance=manifest.provenance, entrypoint=manifest.entrypoint,
            permissions=list(manifest.permissions), source_dir=str(manifest.source_dir),
            source_signature=None, installed_at=datetime.now(timezone.utc), upgraded_at=None,
            status="active", app_license={}, authored_here=authored_here)

    def patch():
        brain_service.get_pool = lambda: pool
        installer.INSTALL_ROOT = install_root
        trust_store.list_active_keys = keys
        registry.register = register
        installer._check_entitlement = watched_check

    def restore():
        (brain_service.get_pool, installer.INSTALL_ROOT, trust_store.list_active_keys,
         registry.register, installer._check_entitlement) = saved

    patch()
    try:
        out = await APPS.install_app(_Request({"app_id": "gadget-notes"}))
    except Exception as e:  # noqa: BLE001
        out = {"error": f"{type(e).__name__}: {getattr(e, 'detail', e)}"}
    finally:
        restore()
    check("the install route installs a public app on a standalone Host",
          out.get("id") == "gadget-notes" and out.get("version") == "1.2.3", out)
    check("its files are extracted where the installer puts them",
          (install_root / "gadget-notes" / "index.html").is_file()
          and (install_root / "gadget-notes" / "index.html").read_bytes().startswith(b"<!doctype html>"))
    check("the installer asked the entitlement gate, and the public store granted it",
          entitlement_asked == ["gadget-notes"], entitlement_asked)
    check("the trust store's signer was checked: the row is stamped with its fingerprint",
          any("source_signature" in str(a[0]) and a[1] == hashlib.sha256(apps_pub).hexdigest()
              for a in pool.conn.executed), pool.conn.executed)
    check("the shell is told", any(m.get("type") == "app_installed" for m in SHELL))

    # A bundle signed by a key the trust store does not hold is refused at
    # install, however it was served (sign() replaces the signature).
    stranger = Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    alien = bundles.sign(bundle, stranger)
    patch()
    try:
        await installer.install_bundle(pool, alien, source="manual", check_entitlement=False)
        refused_alien = False
    except installer.InstallError:
        refused_alien = True
    finally:
        restore()
    check("a bundle the trust store's keys did not sign is still refused at install", refused_alien)

run(part5())

# ── 6. the first start: the catalog's default apps install themselves ───────
#
# The App Store app is not baked into the image, and it is the only way a
# person adds any other app. The catalog marks it default, and the boot pass
# (default_install.run_loop, started from server/main.py) installs what the
# catalog marks default on a standalone Host, once: an uninstall then holds.


async def part6():
    from server import brain_service
    from server.applications import default_install, installer, registry, trust_store

    MAIN.get_bridge_client = lambda: NoPlatform()
    os.environ["CSERIES_STORE_CACHE"] = str(WORK / "cache6")
    PS._stores.clear()
    pad = make_bundle("plain-pad", "0.2.0")
    (SRV / "rel" / "plain-pad-0.2.0.cseries").write_bytes(pad)
    publish("0.6.0", [
        entry(is_default=True, bundle={"url": "gadget-notes-1.2.3.cseries", "sha256": bsha,
                                       "size": len(bundle)}),
        entry(app_id="plain-pad", version="0.2.0",
              bundle={"url": "plain-pad-0.2.0.cseries",
                      "sha256": hashlib.sha256(pad).hexdigest(), "size": len(pad)}),
    ])

    pool = _Pool()
    install_root = WORK / "apps6"
    install_root.mkdir()
    now = datetime.now(timezone.utc)

    def app(app_id, version="1.0.0"):
        return registry.InstalledApp(
            id=app_id, name=app_id, version=version, provenance="native", entrypoint="index.html",
            permissions=[], source_dir="", source_signature=None, installed_at=now,
            upgraded_at=None, status="active", app_license={}, authored_here=None)

    # A fresh Host: the kernel's three and a built-in app the image laid down.
    active = {a: app(a) for a in ("settings", "oracle", "orchestrator", "calendar")}

    async def list_active(_pool):
        return list(active.values())

    async def register(_pool, manifest, authored_here=None):
        active[manifest.id] = app(manifest.id, manifest.version)
        return active[manifest.id]

    async def keys(_pool):
        return [trust_store.SigningKey(
            fingerprint=hashlib.sha256(apps_pub).hexdigest(), public_key=apps_pub,
            publisher="C-Series", provenance_allowed=["native", "standard"],
            registered_at=now, revoked_at=None)]

    saved = (brain_service.get_pool, installer.INSTALL_ROOT, trust_store.list_active_keys,
             registry.register, registry.list_active, installer.image_baked_ids,
             default_install._OFFERED_MARKER)
    brain_service.get_pool = lambda: pool
    installer.INSTALL_ROOT = install_root
    trust_store.list_active_keys = keys
    registry.register = register
    registry.list_active = list_active
    installer.image_baked_ids = lambda: frozenset({"calendar"})
    default_install._OFFERED_MARKER = WORK / "offered6.json"
    try:
        first = await default_install.run_once(None)
        installed_first = sorted(active)
        active.pop("gadget-notes", None)             # the person uninstalls it
        second = await default_install.run_once(None)
        installed_second = sorted(active)
    finally:
        (brain_service.get_pool, installer.INSTALL_ROOT, trust_store.list_active_keys,
         registry.register, registry.list_active, installer.image_baked_ids,
         default_install._OFFERED_MARKER) = saved

    check("at a standalone Host's first start, the catalog's default app installs from the public store",
          (first.get("default_reconcile") or {}).get("installed") == ["gadget-notes"]
          and "gadget-notes" in installed_first, first)
    check("an app the catalog does not mark default is not installed",
          "plain-pad" not in installed_first, installed_first)
    check("its files are where the installer puts them",
          (install_root / "gadget-notes" / "index.html").is_file())
    check("once uninstalled, the next start leaves it uninstalled",
          "gadget-notes" not in installed_second
          and (second.get("default_reconcile") or {}).get("installed") == [], second)

    main_src = (ROOT / "server" / "main.py").read_text(encoding="utf-8")
    life = main_src[main_src.index("async def lifespan("):]
    check("server/main.py starts the boot pass at every start",
          re.search(r"asyncio\.create_task\(\s*_default_install\.run_loop\(None\)\s*\)", life) is not None)

run(part6())

# ── 7. the first start when the store does not answer ───────────────────────
#
# A default app counts as offered (so an uninstall of it holds) only once it
# has installed: recording one whose download did not finish would leave the
# Host without it for good, and without the App Store a person cannot add any
# other app. The boot pass waits and tries again while the store is not
# reached. And the store's copy of an app never replaces one this Host built
# itself under the same id, while an app installed from the store still
# upgrades.


async def part7():
    from server import brain_service
    from server.applications import default_install, installer, registry, trust_store

    MAIN.get_bridge_client = lambda: NoPlatform()
    os.environ["CSERIES_STORE_CACHE"] = str(WORK / "cache7")
    PS._stores.clear()
    pad = (SRV / "rel" / "plain-pad-0.2.0.cseries").read_bytes()
    own = make_bundle("tool-thing", "2.0.0")
    (SRV / "rel" / "tool-thing-2.0.0.cseries").write_bytes(own)
    publish("0.7.0", [
        entry(is_default=True, bundle={"url": "gadget-notes-1.2.3.cseries", "sha256": bsha,
                                       "size": len(bundle)}),
        entry(app_id="plain-pad", version="0.2.0",
              bundle={"url": "plain-pad-0.2.0.cseries",
                      "sha256": hashlib.sha256(pad).hexdigest(), "size": len(pad)}),
        entry(app_id="tool-thing", version="2.0.0",
              bundle={"url": "tool-thing-2.0.0.cseries",
                      "sha256": hashlib.sha256(own).hexdigest(), "size": len(own)}),
    ])
    # The release server does not have the default app's bundle yet.
    held = SRV / "rel" / "gadget-notes-1.2.3.cseries"
    away = WORK / "gadget-notes-1.2.3.away"
    os.replace(held, away)

    pool = _Pool()
    install_root = WORK / "apps7"
    install_root.mkdir()
    marker = WORK / "offered7.json"
    now = datetime.now(timezone.utc)

    def app(app_id, version="1.0.0", authored_here=None):
        return registry.InstalledApp(
            id=app_id, name=app_id, version=version, provenance="native", entrypoint="index.html",
            permissions=[], source_dir="", source_signature=None, installed_at=now,
            upgraded_at=None, status="active", app_license={}, authored_here=authored_here)

    # The kernel's three, a built-in app, an app installed from the store at an
    # older version, and one the person built in the Workbench under an id the
    # catalog also carries.
    active = {a: app(a) for a in ("settings", "oracle", "orchestrator", "calendar")}
    active["plain-pad"] = app("plain-pad", "0.1.0")
    active["tool-thing"] = app("tool-thing", "1.0.0", authored_here=True)

    async def list_active(_pool):
        return list(active.values())

    async def register(_pool, manifest, authored_here=None):
        prev = active.get(manifest.id)
        sticky = bool(prev is not None and prev.authored_here) or bool(authored_here)
        active[manifest.id] = app(manifest.id, manifest.version, sticky or None)
        return active[manifest.id]

    async def keys(_pool):
        return [trust_store.SigningKey(
            fingerprint=hashlib.sha256(apps_pub).hexdigest(), public_key=apps_pub,
            publisher="C-Series", provenance_allowed=["native", "standard"],
            registered_at=now, revoked_at=None)]

    sleeps: list[float] = []

    class _Clock:
        """default_install's asyncio, with sleep recorded and not slept: the
        release server gets the bundle during the pass's first wait."""

        def __getattr__(self, name):
            return getattr(asyncio, name)

        @staticmethod
        async def sleep(d):
            sleeps.append(float(d))
            if len(sleeps) == 2 and away.exists():
                os.replace(away, held)
            if len(sleeps) > 6:
                raise asyncio.CancelledError()

    saved = (brain_service.get_pool, installer.INSTALL_ROOT, trust_store.list_active_keys,
             registry.register, registry.list_active, installer.image_baked_ids,
             default_install._OFFERED_MARKER, default_install.asyncio)
    brain_service.get_pool = lambda: pool
    installer.INSTALL_ROOT = install_root
    trust_store.list_active_keys = keys
    registry.register = register
    registry.list_active = list_active
    installer.image_baked_ids = lambda: frozenset({"calendar"})
    default_install._OFFERED_MARKER = marker
    try:
        first = await default_install.run_once(None)
        offered_first = json.loads(marker.read_text()) if marker.exists() else []
        installed_first = sorted(active)
        own_after = active["tool-thing"]
        pad_after = active["plain-pad"].version

        marker.unlink(missing_ok=True)               # the first start again, as a loop
        default_install.asyncio = _Clock()
        try:
            await default_install.run_loop(None)
            loop_ended = True
        except asyncio.CancelledError:
            loop_ended = False
        default_install.asyncio = saved[-1]
        installed_loop = sorted(active)

        active.pop("gadget-notes", None)             # the person uninstalls it
        later = await default_install.run_once(None)
    finally:
        if away.exists():
            os.replace(away, held)
        (brain_service.get_pool, installer.INSTALL_ROOT, trust_store.list_active_keys,
         registry.register, registry.list_active, installer.image_baked_ids,
         default_install._OFFERED_MARKER, default_install.asyncio) = saved

    rec = first.get("default_reconcile") or {}
    check("a default app whose download fails at the first start is not installed",
          "gadget-notes" not in installed_first
          and [f[0] for f in rec.get("failed") or []] == ["gadget-notes"], rec)
    check("and it is not recorded as offered, so a later start still installs it",
          "gadget-notes" not in offered_first and "settings" in offered_first, offered_first)
    check("the boot pass waits and tries again, and installs it once the store answers",
          loop_ended and "gadget-notes" in installed_loop and sleeps[1:] == [300.0],
          (loop_ended, installed_loop, sleeps))
    check("once it has installed, an uninstall of it holds",
          "gadget-notes" not in active
          and (later.get("default_reconcile") or {}).get("installed") == [], later)
    sweep = first.get("upgrade_sweep") or {}
    check("an app installed from the store upgrades at a start",
          pad_after == "0.2.0", (pad_after, sweep))
    check("the store's copy never replaces an app this Host built under the same id",
          own_after.version == "1.0.0" and own_after.authored_here is True
          and "tool-thing" not in [u.get("app_id") for u in sweep.get("upgraded") or []]
          and not (install_root / "tool-thing").exists(), (own_after, sweep))

run(part7())

# ── 8. an unattended upgrade never reserves more memory than the installed one
#
# install_bundle lays a new version down before the runtime takes its
# descriptor, and rolls back an over-plan refusal by uninstalling the app. So
# the start-time sweep installs a runtime app's new version only when it
# reserves no more than the installed one (which fitted); a larger one waits for
# the person to update it from the App Store.


def make_proc_bundle(app_id, version, memory_mi):
    src = WORK / f"src-{app_id}-{version}"
    src.mkdir(exist_ok=True)
    manifest = (f"id: {app_id}\nname: {app_id}\ndescription: A small service.\nicon: icon.png\n"
                f'version: "{version}"\nprovenance: standard\nentrypoint: index.html\npermissions: []\n'
                f"network: []\nresources:\n  baseline_memory_mi: {memory_mi}\n"
                "runtime:\n  kind: process\n  image: python\n  command: [\"python3\", \"main.py\"]\n"
                "  ready_path: /health\n")
    (src / "app.yaml").write_text(manifest)
    (src / "index.html").write_bytes(b"<!doctype html><title>svc</title>")
    (src / "main.py").write_text("print('ok')\n")
    (src / "icon.png").write_bytes(ICON)
    return bundles.sign(bundles.pack(src), apps_pem), manifest


async def part8():
    from server import brain_service
    from server.applications import default_install, installer, registry, trust_store

    MAIN.get_bridge_client = lambda: NoPlatform()
    os.environ["CSERIES_STORE_CACHE"] = str(WORK / "cache8")
    PS._stores.clear()
    big2, _ = make_proc_bundle("svc-big", "2.0.0", 512)
    same2, _ = make_proc_bundle("svc-same", "2.0.0", 128)
    _, big1_yaml = make_proc_bundle("svc-big", "1.0.0", 128)
    _, same1_yaml = make_proc_bundle("svc-same", "1.0.0", 128)
    for name, data in (("svc-big-2.0.0.cseries", big2), ("svc-same-2.0.0.cseries", same2)):
        (SRV / "rel" / name).write_bytes(data)
    publish("0.8.0", [
        entry(app_id="svc-big", version="2.0.0", images={},
              bundle={"url": "svc-big-2.0.0.cseries", "sha256": hashlib.sha256(big2).hexdigest(),
                      "size": len(big2)}),
        entry(app_id="svc-same", version="2.0.0", images={},
              bundle={"url": "svc-same-2.0.0.cseries", "sha256": hashlib.sha256(same2).hexdigest(),
                      "size": len(same2)}),
    ])

    pool = _Pool()
    install_root = WORK / "apps8"
    for app_id, text in (("svc-big", big1_yaml), ("svc-same", same1_yaml)):
        (install_root / app_id).mkdir(parents=True)
        (install_root / app_id / "app.yaml").write_text(text)
    now = datetime.now(timezone.utc)

    def app(app_id, version="1.0.0"):
        return registry.InstalledApp(
            id=app_id, name=app_id, version=version, provenance="standard", entrypoint="index.html",
            permissions=[], source_dir="", source_signature=None, installed_at=now,
            upgraded_at=None, status="active", app_license={}, authored_here=None)

    active = {a: app(a) for a in ("settings", "oracle", "orchestrator")}
    active["svc-big"] = app("svc-big")
    active["svc-same"] = app("svc-same")
    synced = []

    async def list_active(_pool):
        return list(active.values())

    async def register(_pool, manifest, authored_here=None):
        active[manifest.id] = app(manifest.id, manifest.version)
        return active[manifest.id]

    async def keys(_pool):
        return [trust_store.SigningKey(
            fingerprint=hashlib.sha256(apps_pub).hexdigest(), public_key=apps_pub,
            publisher="C-Series", provenance_allowed=["native", "standard"],
            registered_at=now, revoked_at=None)]

    async def sync_runtime(app_id, enabled, *, bc=None):
        synced.append((app_id, enabled))
        return True

    saved = (brain_service.get_pool, installer.INSTALL_ROOT, trust_store.list_active_keys,
             registry.register, registry.list_active, installer.image_baked_ids,
             default_install._OFFERED_MARKER, installer.sync_app_runtime)
    brain_service.get_pool = lambda: pool
    installer.INSTALL_ROOT = install_root
    trust_store.list_active_keys = keys
    registry.register = register
    registry.list_active = list_active
    installer.image_baked_ids = lambda: frozenset()
    default_install._OFFERED_MARKER = WORK / "offered8.json"
    installer.sync_app_runtime = sync_runtime
    try:
        out = await default_install.run_once(None)
    finally:
        (brain_service.get_pool, installer.INSTALL_ROOT, trust_store.list_active_keys,
         registry.register, registry.list_active, installer.image_baked_ids,
         default_install._OFFERED_MARKER, installer.sync_app_runtime) = saved

    sweep = out.get("upgrade_sweep") or {}
    skipped = {x.get("app_id"): x.get("reason") for x in sweep.get("skipped") or []}
    check("a runtime app whose new version reserves more memory is not upgraded at a start",
          active["svc-big"].version == "1.0.0" and "reserves more memory" in (skipped.get("svc-big") or "")
          and ("svc-big", True) not in synced, (sweep, synced))
    check("and its installed copy is left in place",
          "baseline_memory_mi: 128" in (install_root / "svc-big" / "app.yaml").read_text())
    check("a runtime app whose new version reserves the same memory still upgrades",
          active["svc-same"].version == "2.0.0" and ("svc-same", True) in synced, (sweep, synced))

run(part8())

# ── the on-demand route describes the same pass ──────────────────────────────────────────
print("the /refresh-store route says what the sweep does now")
import ast  # noqa: E402

_tree = ast.parse((ROOT / "server" / "routes" / "applications.py").read_text(encoding="utf-8"))
_fn = next((n for n in ast.walk(_tree)
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "refresh_store"), None)
_doc = " ".join((ast.get_docstring(_fn) or "").split()) if _fn else ""
check("its docstring no longer says it reinstalls every app that is newer",
      "reinstalls in place if newer" not in _doc, _doc[:300])
check("...it names what the sweep leaves alone: apps this Host built, image apps, and a "
      "version that reserves more memory",
      "authored_here" in _doc and "image" in _doc and "reserves more memory" in _doc, _doc[:600])
check("...that it only moves forward, and skips or defers what it cannot install",
      "strictly newer" in _doc and "skipped" in _doc and "deferred" in _doc, _doc[:600])
check("...and that it goes through the App Store router, refusing only when no store can be reached",
      "App Store router" in _doc and "503" in _doc, _doc[-400:])


async def refresh_on_a_standalone_host():
    from server import brain_service
    from server.applications import installer, registry
    from server.routes import applications as APPS
    MAIN.get_bridge_client = lambda: NoPlatform()
    PS._stores.clear()
    now = datetime.now(timezone.utc)
    kernel = [registry.InstalledApp(
        id=a, name=a, version="1.0.0", provenance="native", entrypoint="index.html",
        permissions=[], source_dir="", source_signature=None, installed_at=now,
        upgraded_at=None, status="active", app_license={}, authored_here=None)
        for a in ("settings", "oracle", "orchestrator")]

    async def list_active(_pool):
        return list(kernel)

    saved = (brain_service.get_pool, registry.list_active, installer.image_baked_ids)
    brain_service.get_pool = lambda: _Pool()
    registry.list_active = list_active
    installer.image_baked_ids = lambda: frozenset()
    try:
        out = await APPS.refresh_store(_Request({}))
    except Exception as e:  # noqa: BLE001
        out = {"error": f"{type(e).__name__}: {getattr(e, 'status_code', '')} {getattr(e, 'detail', e)}"}
    finally:
        (brain_service.get_pool, registry.list_active, installer.image_baked_ids) = saved
    check("on a standalone Host, /refresh-store runs the sweep against the public catalog, not a 503",
          out.get("status") == "ok" and out.get("checked") == 3, out)

run(refresh_on_a_standalone_host())

httpd.shutdown()
if FAILS:
    print(f"\n[public-store] {len(FAILS)} FAILED")
    sys.exit(1)
print("\n[public-store] all checks passed")
