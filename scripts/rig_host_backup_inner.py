#!/usr/bin/env python3
"""The checks scripts/rig_host_backup.sh runs inside the kernel image.

The real server/host_backup.py and server/host_restore.py against a real Host
database and apps tree, with the image's own age and pg_dump. The platform is
a stand-in in this process: the bridge ops, and a small HTTP server playing
the storage behind the presigned URLs (the Host only ever PUTs and GETs the
URLs it is handed). The owner's side (their Host recovery key, their bastion's
key, their browser reselling a backup's key) is played with the age binary.

THE RULE is checked twice, after the backups and after the restore: no key
that opens a backup is anywhere this Host keeps things — its database, /tmp,
its logs — nor in anything it sent the platform, nor in clear in storage.
"""
import asyncio
import base64
import hashlib
import http.server
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import threading
from pathlib import Path

import asyncpg

from server import age_keys, backup_key, brain_service
from server import host_backup as hb
from server.vault import identity

EID = 3
BIG_MB = int(os.environ.get("RIG_BIG_MB", "96"))
FAILED = []
PASSED = []
AGE = hb.AGE


def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  [{str(detail)[:600]}]"))
    (PASSED if cond else FAILED).append(label)


LOGS = []


class _Capture(logging.Handler):
    def emit(self, record):
        LOGS.append(self.format(record))


logging.getLogger().addHandler(_Capture())
logging.getLogger().setLevel(logging.DEBUG)


# ── the platform's storage: PUT and GET on presigned URLs ───────────────────
# In its OWN process, writing to disk: the rig measures this process's memory
# while it backs up, and a store holding the objects here would be measured
# with it.
STORE_SERVER = r"""
import hashlib, http.server, json, os, sys, urllib.parse
root = sys.argv[1]
signed = {}
inm = set()      # single PUTs signed with If-None-Match: * (privatae host_backup_store put_url)
def fs(path):
    return os.path.join(root, urllib.parse.quote(path, safe=""))
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def reply(self, code, body=b"", extra=None):
        self.send_response(code); self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items(): self.send_header(k, v)
        self.end_headers(); self.wfile.write(body)
    def do_PUT(self):
        n = int(self.headers.get("Content-Length") or 0)
        if signed.get(self.path, n) != n: return self.reply(403)
        if self.path in inm:
            if self.headers.get("If-None-Match") != "*": return self.reply(403)
            if os.path.exists(fs(self.path)): return self.reply(412)
        h = hashlib.md5()
        with open(fs(self.path), "wb") as f:
            left = n
            while left:
                b = self.rfile.read(min(left, 1 << 20)); left -= len(b); h.update(b); f.write(b)
        self.reply(200, extra={"ETag": '"' + h.hexdigest() + '"'})
    def do_GET(self):
        if self.path.startswith("/_list"):
            names = sorted(urllib.parse.unquote(n) for n in os.listdir(root))
            body = json.dumps([[n, os.path.getsize(fs(n))] for n in names]).encode()
            return self.reply(200, body)
        if not os.path.exists(fs(self.path)): return self.reply(404)
        size = os.path.getsize(fs(self.path))
        self.send_response(200); self.send_header("Content-Length", str(size)); self.end_headers()
        with open(fs(self.path), "rb") as f:
            while True:
                b = f.read(1 << 20)
                if not b: break
                self.wfile.write(b)
    def do_DELETE(self):
        if os.path.exists(fs(self.path)): os.unlink(fs(self.path))
        self.reply(204)
    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        if self.path == "/_sign":
            signed[req["path"]] = req["size"]
            if req.get("inm"): inm.add(req["path"])
            return self.reply(200)
        if self.path == "/_complete":
            h, n = hashlib.sha256(), 0
            with open(fs(req["dest"]), "wb") as out:
                for p in req["parts"]:
                    with open(fs(p), "rb") as f:
                        while True:
                            b = f.read(1 << 20)
                            if not b: break
                            h.update(b); n += len(b); out.write(b)
                    os.unlink(fs(p))
            ok = n == req["size"] and h.hexdigest() == req["sha256"]
            if not ok: os.unlink(fs(req["dest"]))
            return self.reply(200, json.dumps({"ok": ok}).encode())
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
print(srv.server_address[1], flush=True)
srv.serve_forever()
"""
_STORE_DIR = os.environ.get("RIG_STORE_DIR", "/store")
os.makedirs(_STORE_DIR, exist_ok=True)
_store_proc = subprocess.Popen([sys.executable, "-c", STORE_SERVER, _STORE_DIR], stdout=subprocess.PIPE, text=True)
BASE = f"http://127.0.0.1:{_store_proc.stdout.readline().strip()}"


def _http(method, path, body=None):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(BASE + path, data=body, method=method)
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""


class _Objects:
    """The store's objects, read over HTTP like a dict: path -> bytes."""

    def _names(self):
        return [n for n, _ in json.loads(_http("GET", "/_list")[1])]

    def __iter__(self):
        return iter(self._names())

    def __contains__(self, path):
        return path in self._names()

    def get(self, path, default=None):
        code, body = _http("GET", path)
        return body if code == 200 else default

    def pop(self, path, default=None):
        _http("DELETE", path)
        return default

    def values(self):
        for n in self._names():
            yield self.get(n, b"")

    def sizes(self):
        return dict(json.loads(_http("GET", "/_list")[1]))


class Store:
    objects = _Objects()

    @staticmethod
    def sign(path, size, inm=False):
        _http("POST", "/_sign", json.dumps({"path": path, "size": size, "inm": inm}).encode())

    @staticmethod
    def complete(parts, dest, size, sha256):
        return json.loads(_http("POST", "/_complete", json.dumps(
            {"parts": parts, "dest": dest, "size": size, "sha256": sha256}).encode())[1])["ok"]


FOLDER = "domain-3-rig"
PREFIX = {"backup": "b", "staged": "s", "export": "exports", "sidecar": "b", "staged_sidecar": "s"}


class Bridge:
    """The platform's half of the bridge ops, as the contract pins them."""
    _connected = True

    def __init__(self):
        self.calls = []
        self.uploads = {}
        self.sha = {}            # the platform's landing record (host_backup_objects)
        self.recovery = {}
        self.exports = []

    async def request_full(self, op, params=None):
        self.calls.append((op, params))
        p = params or {}
        if op == "backup.upload.start":
            stamp = None
            if p["kind"] in ("backup", "staged"):
                if "name" in p:
                    return {"ok": False, "error": "the platform names a backup; no name is taken"}
                import datetime as _dt
                self.clock = max(getattr(self, "clock", _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc))
                                 + _dt.timedelta(seconds=1),
                                 _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0))
                stamp = self.clock.strftime("%Y%m%dT%H%M%SZ")
            key = f"{FOLDER}/{PREFIX[p['kind']]}/{stamp or p['name']}.tar.zst.age"
            if "/" + key in Store.objects:
                return {"ok": False, "error": "that object exists; nothing is ever overwritten"}
            uid = f"u{len(self.uploads) + 1}"
            self.uploads[uid] = key
            return {"ok": True, "data": {"upload_id": uid, "key": key, **({"stamp": stamp} if stamp else {})}}
        if op == "backup.upload.part":
            # Signed for the part's declared size, at most 16 MiB (privatae
            # bridge/adapter.py op_backup_upload_part).
            size = p.get("size")
            if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= 16 * 1024 * 1024:
                return {"ok": False, "error": "size must be the part's exact bytes, 1..16777216"}
            Store.sign(f"/part/{p['upload_id']}/{p['part']}", size)
            return {"ok": True, "data": {"url": f"{BASE}/part/{p['upload_id']}/{p['part']}", "size": size}}
        if op == "backup.upload.complete":
            if not Store.complete([f"/part/{p['upload_id']}/{x['part']}" for x in p["parts"]],
                                  "/" + self.uploads[p["upload_id"]], p["size"], p["sha256"]):
                return {"ok": False, "error": "size or digest mismatch"}
            self.sha[self.uploads[p["upload_id"]]] = p["sha256"]
            return {"ok": True, "data": {"ok": True, "size": p["size"], "key": self.uploads[p["upload_id"]]}}
        if op == "backup.upload.abort":
            return {"ok": True, "data": {"ok": True}}
        if op == "backup.put_url":
            if not isinstance(p.get("size"), int):
                return {"ok": False, "error": "size is required: the URL is signed for it"}
            Store.sign(f"/{FOLDER}/{PREFIX[p['kind']]}/{p['name']}" + (".key.age" if "sidecar" in p["kind"] else ".tar.zst.age"),
                       p["size"], inm=True)
            suffix = ".key.age" if "sidecar" in p["kind"] else ".tar.zst.age"
            key = f"{FOLDER}/{PREFIX[p['kind']]}/{p['name']}{suffix}"
            return {"ok": True, "data": {"url": f"{BASE}/{key}", "key": key, "size": p["size"],
                                         "headers": {"If-None-Match": "*"}}}
        if op == "backup.list":
            # As the platform names them: relative to the Host's folder
            # (b/<stamp>..., s/<stamp>...), with the sha256 recorded at landing.
            pre = f"/{FOLDER}/{PREFIX[p['kind']]}/"
            return {"ok": True, "data": {"objects": [
                {"name": k[len(FOLDER) + 2:], "key": k[1:], "size": n,
                 **({"sha256": self.sha[k[1:]]} if k[1:] in self.sha else {})}
                for k, n in sorted(Store.objects.sizes().items()) if k.startswith(pre)]}}
        if op == "backup.get_url":
            if not p["key"].startswith(FOLDER + "/"):
                return {"ok": False, "error": "not this Host's folder"}
            return {"ok": True, "data": {"url": f"{BASE}/{p['key']}"}}
        if op == "backup.drop":
            Store.objects.pop("/" + p["key"], None)
            return {"ok": True, "data": {"ok": True}}
        if op == "backup.recovery_key":
            return {"ok": True, "data": dict(self.recovery)}
        if op == "backup.exports.pending":
            return {"ok": True, "data": {"exports": list(self.exports)}}
        if op == "backup.exports.claim":
            hit = next((e for e in self.exports if e["id"] == p.get("id")
                        and e["recipient"] == p.get("recipient")), None)
            # The platform's shape (bridge adapter backup.exports.claim).
            if hit is None:
                return {"ok": True, "data": {"exports": []}}
            self.exports.remove(hit)
            return {"ok": True, "data": {"exports": [dict(hit, status="claimed")]}}
        return {"ok": True, "data": {}}

    async def request(self, op, params=None, **_):
        env = await self.request_full(op, params)
        return env.get("data") if env.get("ok") else None

    def last(self, op):
        return next((p for o, p in reversed(self.calls) if o == op), None)


def age_open(identity_text: str, data: bytes) -> subprocess.CompletedProcess:
    argv, fd = hb.age_decrypt_args(identity_text)
    try:
        return subprocess.run(argv, input=data, capture_output=True, pass_fds=(fd,))
    finally:
        os.close(fd)


def members(identity_text, blob):
    got = age_open(identity_text, blob)
    if got.returncode != 0:
        return None
    from compression import zstd
    out = {}
    with tarfile.open(fileobj=io.BytesIO(zstd.decompress(got.stdout)), mode="r:") as t:
        for m in t:
            out[m.name] = t.extractfile(m).read() if m.isfile() else None
    return out


async def the_rule(pool, bridge, when):
    """No key that opens a backup is anywhere this Host keeps things."""
    leaked = []
    for table in ("host_backup_runs", "host_backup_state", "backup_user_key"):
        n = await pool.fetchval(
            f"SELECT count(*) FROM {table} t WHERE row_to_json(t)::text ILIKE '%age-secret-key-%'")
        if n:
            leaked.append(f"{table}: {n} row(s)")
    for p in Path("/tmp").rglob("*"):
        try:
            if p.is_file() and b"AGE-SECRET-KEY-" in p.read_bytes().upper():
                leaked.append(str(p))
        except OSError:
            pass
    check(f"THE RULE ({when}): no private key in this Host's database or /tmp", not leaked, leaked)
    check(f"THE RULE ({when}): no private key in any log line",
          not any("AGE-SECRET-KEY-" in line.upper() for line in LOGS))
    check(f"THE RULE ({when}): no private key in anything sent to the platform",
          b"AGE-SECRET-KEY-" not in json.dumps(bridge.calls, default=str).encode().upper())
    check(f"THE RULE ({when}): no private key in clear in storage",
          not any(b"AGE-SECRET-KEY-" in v.upper() for v in Store.objects.values()))


async def main():
    pool = await asyncpg.create_pool(host="/run/postgresql", user="entity",
                                     database="hostmem", min_size=1, max_size=4)
    brain_service._entity_id = EID
    await identity.ensure_host_keypair(pool)
    await pool.execute(
        "INSERT INTO entity_registry (id, display_name, behavioral_params) VALUES ($1, 'rig', '{}') "
        "ON CONFLICT (id) DO UPDATE SET behavioral_params = '{}'", EID)
    await pool.execute("CREATE TABLE IF NOT EXISTS rig_marker (note text)")
    await pool.execute("INSERT INTO rig_marker VALUES ('first run')")
    bridge = Bridge()
    hb._bridge = lambda: bridge
    from server.routes import gitea_client
    gitea_client.gitea_creds = lambda: None

    print("== setup")
    check("the Host can back up (age is in the image)", hb.configured() is None, hb.configured())
    check("the image's age is the one the Host uses", Path(AGE).is_file(), AGE)

    print("== no recovery key saved: no backup")
    r0 = await hb.run_once(pool, EID, "manual")
    check("nothing is written, and the run says to save the recovery key",
          r0["state"] == "no_recipient" and "save your recovery key" in r0["error"]
          and not any(o.startswith("backup.upload") for o, _ in bridge.calls), r0)

    print("== the owner saves their recovery key and confirms it on the Host")
    rec_ident, rec_rec = age_keys.new_pair()             # made in the owner's browser
    fp = backup_key.fingerprint(age_keys.recipient_bytes(rec_rec))
    bridge.recovery = {"recipient": rec_rec, "fingerprint": fp, "label": "Host 3 recovery key",
                       "saved_at": "2026-09-28T20:00:00Z"}
    offered = await hb.pin_recovery_key(pool, EID)
    check("the key waits for the owner: nothing is pinned on the platform's word",
          offered["pinned"] is None and (offered["offer"] or {}).get("fingerprint") == fp, offered)
    await hb.adopt_recovery_offer(pool, EID, fp)          # Settings: the fingerprints match
    import resource
    rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    r1 = await hb.run_once(pool, EID, "manual")
    grew = (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - rss_before) / 1024
    check("the run ended ok", r1["state"] == "ok", r1)
    check(f"the writer's memory stays well under 64 MiB while it streams a {BIG_MB} MiB file "
          f"(peak grew {grew:.1f} MiB)", grew < 48, grew)
    pinned = await backup_key.get_active(pool, identity.user_identity(str(EID)), "host-recovery")
    check("the recovery key is pinned in this Host's own database",
          pinned and backup_key.fingerprint(pinned["pubkey"]) == fp and pinned["origin"] == "owner-browser")
    blob = Store.objects.get("/" + r1["object"])
    check("one encrypted file in platform storage (b/)", blob and "/b/" in r1["object"]
          and not blob.startswith(b"PGDMP"), r1["object"])
    check("uploaded in parts through presigned URLs",
          sum(1 for o, _ in bridge.calls if o == "backup.upload.part") >= 1)
    got = members(rec_ident, blob)
    check("the recovery key opens it", got is not None)
    host_ident = age_keys.identity_from_pem(await identity.host_privkey(pool))
    check("this Host's own key does not", members(host_ident, blob) is None)
    chunks = sorted(n for n in got if n.startswith("db/hostmem.dump."))
    dump = b"".join(got[n] for n in chunks)
    lst = subprocess.run(["pg_restore", "--list"], input=dump, capture_output=True)
    check("the database pieces join into a pg_dump that holds the table written before the run",
          lst.returncode == 0 and b"rig_marker" in lst.stdout, lst.stderr[-200:])
    check("the user's files are in it", got.get("apps/app-state/user-files/notes.txt") == b"my notes\n")
    check("a real file named core is in it", got.get("apps/proj/lib/vocabularies/core") == b"keep me\n")
    check("what backups leave out is not in it",
          not any("node_modules" in n or "core.1234" in n or "/crash" in n or "scratch" in n or ".cache" in n
                  for n in got))
    man = json.loads(got["manifest.json"])
    # What a manifest costs per file, as this image's writer makes it: the
    # figure host_backup.MANIFEST_MAX is sized from.
    per = len(json.dumps(man.get("files") or [])) / max(1, len(man.get("files") or []))
    print(f"  manifest: {len(got['manifest.json'])} bytes for {len(man.get('files') or [])} entries, "
          f"{per:.0f} bytes an entry; MANIFEST_MAX {hb.MANIFEST_MAX >> 20} MiB lists about "
          f"{int(hb.MANIFEST_MAX / per):,} at that size")
    check("the manifest is far under the most a restore reads", len(got["manifest.json"]) < hb.MANIFEST_MAX)
    check("the manifest names the recovery key as who it is for",
          [(r["kind"], r["fingerprint"]) for r in man["recipients"]] == [("host-recovery", fp)])
    check("the platform was told how it went", (bridge.last("backup.report") or {}).get("state") == "ok")

    print("== a bastion's key joins")
    nas_ident, nas_rec = age_keys.new_pair()
    await backup_key.put_active(pool, ident=identity.user_identity(str(EID)),
                                pub_raw=age_keys.recipient_bytes(nas_rec), sealed=None,
                                origin="self-generated", kind="bastion", label="nas-box")
    await pool.execute("INSERT INTO rig_marker VALUES ('restore point')")
    # Access as it stands at the restore point: a paired bastion with a
    # management grant, and the step-up enrolled on the owner's first phone.
    from server.network import nac_stepup
    await pool.execute(
        "INSERT INTO mcp_client_tokens (id, entity_id, name, token_hash, url_handle, operator_kind, notes) "
        "VALUES (70, 3, 'old-laptop', '\\x01', 'h70', 'bastion', "
        "'{\"wg_bastion_pubkey\": \"pk70\", \"wg_bastion_ip\": \"fd00::70/128\"}'::jsonb)")
    await pool.execute("SELECT setval(pg_get_serial_sequence('mcp_client_tokens', 'id'), 70)")
    await pool.execute(
        "INSERT INTO nac_mgmt_grants (id, entity_id, server, ops, approved_at) "
        "VALUES (70, 3, 'old-laptop', '[\"uptime\"]'::jsonb, now())")
    old_phone = await nac_stepup.ensure_stepup_secret(pool)
    await nac_stepup.mark_enrolled(pool)
    # An approved ingress rule (nac_policy -> nac_profiles) and a memory-domain
    # grant (entity_domain_access -> domain_registry, a table not carried).
    await pool.execute("INSERT INTO nac_profiles (id, name, enforcement) VALUES (901, 'rig-allow', 'allow')")
    await pool.execute("INSERT INTO nac_policy (name, profile_id, proposed_by, approved_at) "
                       "VALUES ('rig ingress rule', 901, 'user', now())")
    await pool.execute("INSERT INTO nac_endpoints (token_id, assigned_profile_id) VALUES (70, 901)")
    await pool.execute("INSERT INTO domain_registry (id, name, domain_group) VALUES ('rig_old', 'rig old', 'g') "
                       "ON CONFLICT DO NOTHING")
    await pool.execute("INSERT INTO entity_domain_access (entity_id, domain_id, access_level) "
                       "VALUES (3, 'rig_old', 'read')")
    (Path("/var/lib/cseries/app-state/user-files/notes.txt")).write_text("restore point notes\n")
    Path("/var/lib/cseries/proj/restore-point.txt").write_text("here at the restore point\n")
    r2 = await hb.run_once(pool, EID, "manual")
    blob2 = Store.objects.get("/" + (r2["object"] or ""))
    check("the next backup opens with the recovery key and with the bastion's key",
          r2["state"] == "ok" and members(rec_ident, blob2) is not None
          and members(nas_ident, blob2) is not None, r2)

    print("== a different recovery key from the platform is not adopted")
    other_ident, other_rec = age_keys.new_pair()
    bridge.recovery = {"recipient": other_rec, "fingerprint": backup_key.fingerprint(
        age_keys.recipient_bytes(other_rec)), "label": "swapped", "saved_at": "2026-09-28T21:00:00Z"}
    offer = await hb.pin_recovery_key(pool, EID)
    check("it waits as an offer, and the key pinned before is no longer used",
          offer["pinned"] is None and offer["offer"]["fingerprint"] != fp
          and not await hb.recipients(pool, EID), offer)
    bridge.recovery = {"recipient": rec_rec, "fingerprint": fp, "label": "Host 3 recovery key",
                       "saved_at": "2026-09-28T20:00:00Z"}
    await hb.pin_recovery_key(pool, EID)
    await hb.adopt_recovery_offer(pool, EID, fp)          # the owner confirms the key they hold

    await the_rule(pool, bridge, "after the backups")

    await restore_checks(pool, bridge, rec_ident, r2)
    pool = await new_pool()
    await the_rule(pool, bridge, "after the restore")

    print("== an export the user asked for")
    from server import host_export as hx
    one_ident, one_rec = age_keys.new_pair()              # made in the user's browser
    _, planted = age_keys.new_pair()                      # what a platform wanting a copy would name
    bridge.exports = [{"id": 41, "recipient": one_rec, "requested_at": "2026-09-28T20:00:00Z"},
                      {"id": 40, "recipient": planted, "requested_at": "2026-09-28T19:00:00Z"}]
    waiting = await hx.pending()
    check("both requests wait for the owner; nothing is claimed or built",
          sorted(w["id"] for w in waiting) == [40, 41]
          and not any(o == "backup.exports.claim" for o, _ in bridge.calls)
          and not any(k.startswith(f"/{FOLDER}/exports/") for k in Store.objects))
    done = await hx.confirm(pool, EID, 41, backup_key.fingerprint(age_keys.recipient_bytes(one_rec)))
    check("the Host built and reported it", done and "/exports/41-" in (done.get("s3_key") or "")
          and not done.get("error"), done)
    check("the one the owner did not confirm is never built",
          not any("/exports/40-" in k for k in Store.objects) and [e["id"] for e in bridge.exports] == [40])
    eblob = Store.objects.get("/" + done["s3_key"])
    got = members(one_ident, eblob)
    check("the one-time key opens it, and it holds the database and the files",
          got is not None and any(n.startswith("db/hostmem.dump.") for n in got)
          and got.get("apps/app-state/user-files/notes.txt") == b"restore point notes\n")

    print("== platform storage off: the copy is staged for the second copy")
    await pool.execute("UPDATE entity_registry SET behavioral_params = $2::jsonb WHERE id = $1", EID,
                       json.dumps({"backup": {"platform": False,
                                              "target": {"kind": "machine", "address": "nas-box"}}}))
    s1 = await hb.run_once(pool, EID, "manual")
    s2 = await hb.run_once(pool, EID, "manual")
    staged = sorted(k for k in Store.objects if k.startswith(f"/{FOLDER}/s/"))
    check("staged (s/) with its key file, and only the newest kept",
          "/s/" in (s2["object"] or "") and len(staged) == 2 and all(s2["stamp"] in k for k in staged),
          staged)

    await pool.close()
    k5_ident = await new_number_checks(bridge)
    await transfer_checks(bridge, k5_ident)

    print()
    print(f"{len(PASSED)} checks passed, {len(FAILED)} failed")
    if FAILED:
        sys.exit(1)
    print("OK: the Host's own backup and restore work end to end")


APPS = Path("/var/lib/cseries")
LOST: dict = {}          # Host 3's last backup and the recovery key that opens it


async def new_pool():
    return await asyncpg.create_pool(host="/run/postgresql", user="entity",
                                     database="hostmem", min_size=1, max_size=4)


def boot(*args):
    """What entrypoint.sh runs at boot, as a separate process."""
    return subprocess.run([sys.executable, "-m", "server.host_restore", *args],
                          capture_output=True, text=True, cwd="/work")


def psql(db, *commands):
    cmd = ["psql", "-h", "/run/postgresql", "-U", "entity", "-d", db, "-v", "ON_ERROR_STOP=1", "-Atq"]
    for c in commands:
        cmd += ["-c", c]
    return subprocess.run(cmd, capture_output=True, text=True)


def databases():
    return set(psql("postgres", "SELECT datname FROM pg_database").stdout.split())


async def notes(pool):
    return [r["note"] for r in await pool.fetch("SELECT note FROM rig_marker ORDER BY note")]


async def restore_checks(pool, bridge, rec_ident, point):
    from server import host_restore as hr
    restarts = []
    hr._restart = lambda: restarts.append(1)

    print("== restore: the owner's browser reseals the backup's key to this Host")
    await pool.execute("INSERT INTO rig_marker VALUES ('after the restore point')")
    (APPS / "app-state/user-files/notes.txt").write_text("changed later\n")
    (APPS / "app-state/user-files/later.txt").write_text("made later\n")
    (APPS / "proj/restore-point.txt").unlink()
    (APPS / "proj/node_modules/y").mkdir(parents=True)
    (APPS / "proj/node_modules/y/index.js").write_text("installed later\n")

    # Since that backup: the laptop was lost. Its bastion is revoked (and with
    # it the management grant), and the step-up re-enrolled on a new phone.
    from server.network import nac_stepup
    old_phone = await nac_stepup.ensure_stepup_secret(pool)
    await pool.execute("UPDATE mcp_client_tokens SET revoked_at = now() WHERE id = 70")
    await pool.execute("UPDATE nac_mgmt_grants SET revoked_at = now() WHERE id = 70")
    await pool.execute("DELETE FROM kernel_secrets WHERE id LIKE 'nac_stepup_%'")
    new_phone = await nac_stepup.ensure_stepup_secret(pool)
    await nac_stepup.mark_enrolled(pool)
    await pool.execute(
        "INSERT INTO mcp_client_tokens (entity_id, name, token_hash, url_handle, operator_kind) "
        "VALUES (3, 'new-desktop', '\\x02', 'h71', 'bastion')")
    # The ingress rule is replaced by a new one, and the domain grant moves to
    # a domain that did not exist at the backup.
    await pool.execute("INSERT INTO nac_profiles (id, name, enforcement) VALUES (902, 'rig-allow-2', 'allow')")
    await pool.execute("DELETE FROM nac_policy WHERE name = 'rig ingress rule'")
    await pool.execute("INSERT INTO nac_policy (name, profile_id, proposed_by, approved_at) "
                       "VALUES ('rig new rule', 902, 'user', now())")
    await pool.execute("UPDATE nac_endpoints SET assigned_profile_id = 902 WHERE token_id = 70")
    await pool.execute("DELETE FROM nac_profiles WHERE id = 901")
    await pool.execute("INSERT INTO domain_registry (id, name, domain_group) VALUES ('rig_new', 'rig new', 'g')")
    await pool.execute("DELETE FROM entity_domain_access WHERE domain_id = 'rig_old'")
    await pool.execute("INSERT INTO entity_domain_access (entity_id, domain_id, access_level) "
                       "VALUES (3, 'rig_new', 'read')")
    # And the bastion's backup key is retired (say, stolen) and the owner made
    # a new recovery key. The restore must not bring any of it back.
    ident = identity.user_identity("3")
    await backup_key.retire(pool, ident, "bastion", "nas-box")
    k2_ident, k2_rec = age_keys.new_pair()
    k2_raw = age_keys.recipient_bytes(k2_rec)
    await backup_key.put_active(pool, ident=ident, pub_raw=k2_raw, sealed=None,
                                origin="owner-browser", kind="host-recovery", label="Host 3 new key")
    # The account page holds that new key now (the Host uses a key only while
    # it is the owner's current one: host_backup.pin_recovery_key).
    bridge.recovery = {"recipient": k2_rec, "fingerprint": backup_key.fingerprint(k2_raw),
                       "label": "Host 3 new key", "saved_at": "2026-09-28T22:00:00Z"}
    bridge.recovery = {"recipient": k2_rec, "fingerprint": backup_key.fingerprint(k2_raw),
                       "label": "Host 3 new key", "saved_at": "2026-09-28T21:30:00Z"}

    avail = await hr.available(pool, 3)
    listed = {b["stamp"]: b for b in avail["backups"]}
    check("Settings lists the backups, newest first, none with its key ready yet",
          point["stamp"] in listed and not listed[point["stamp"]]["key_ready"], avail)
    # The browser: fetch the sealed key file and the Host's recipient…
    side = await hr.sidecar_for_browser(pool, 3, point["stamp"])
    check("what the page is given carries no key that opens anything",
          "AGE-SECRET-KEY" not in json.dumps(side).upper() and side["host_recipient"].startswith("age1"))
    # …open it with the recovery key (in the page), check it, seal it to the Host.
    opened = age_open(rec_ident, side["sidecar"].encode())
    body = json.loads(opened.stdout)
    check("the recovery key opens the backup's key file, and it names this backup of this Host",
          opened.returncode == 0 and body["stamp"] == point["stamp"] and body["entity_id"] == 3)
    resealed = subprocess.run([AGE, "-a", "-r", side["host_recipient"]], input=opened.stdout,
                              capture_output=True).stdout
    del opened, body
    res = await hr.accept_resealed(pool, point["stamp"], resealed, via="browser")
    check("the Host takes the resealed file, and the backup verifies as its own",
          res["key_ready"] and res["verified"] is True, res)

    staged = await hr.stage(pool, 3, point["stamp"])
    check("staging finished and asked for a restart", staged["state"] == "staged" and restarts == [1], staged)
    runs = await hb.last_runs(pool, limit=3)
    check("it backed up how the Host was first", runs[0]["reason"] == "before-restore"
          and runs[0]["state"] == "ok", runs[0])
    check("the restored database waits beside the live one", "hostmem_restore" in databases())

    print("== restore: the swap at boot, the kernel starting, finish")
    inode = (APPS / "app-state/user-files").stat().st_ino
    await pool.close()
    b = boot("swap", "hostmem")
    check("entrypoint's swap succeeds", b.returncode == 0, (b.returncode, b.stdout, b.stderr))
    c = boot("carry", "hostmem")          # entrypoint: after the schema step, before the server
    check("entrypoint keeps the live access and second factor before anything serves",
          c.returncode == 0 and "kept the live access" in c.stdout, (c.returncode, c.stdout, c.stderr))
    pool = await new_pool()
    got = await notes(pool)
    check("the database is the restore point's", "restore point" in got
          and "after the restore point" not in got, got)
    check("the user's file is the restore point's",
          (APPS / "app-state/user-files/notes.txt").read_text() == "restore point notes\n")
    check("a file deleted since is back", (APPS / "proj/restore-point.txt").exists())
    check("a file made since is set aside, not deleted",
          (hr.PREVIOUS / "app-state/user-files/later.txt").read_text() == "made later\n")
    check("what backups leave out stays where it was",
          (APPS / "proj/node_modules/y/index.js").read_text() == "installed later\n")
    check("a folder is the same folder after the swap, so a bind mount of it still sees it",
          (APPS / "app-state/user-files").stat().st_ino == inode)
    check("the kernel says it started on the restored data", hr.mark_started())
    for _ in range(3):
        again = boot("swap", "hostmem")
    check("after that, restarts never undo it", again.returncode == 3
          and (APPS / "app-state/user-files/notes.txt").read_text() == "restore point notes\n", again.stderr)
    fin = await hr.finish(pool, 3)
    check("finished ok, and what was set aside is cleared", fin and fin["state"] == "ok"
          and "hostmem_before_restore" not in databases() and not hr.PREVIOUS.exists()
          and hr.read_plan() is None, fin)
    check("the resealed key is gone with the restore",
          hr.load_resealed(point["stamp"]) is None and not any(hr.RESEALED_DIR.iterdir()))
    print("== restore: data came back, older access did not")
    tok = {r["name"]: r["revoked_at"] for r in await pool.fetch("SELECT name, revoked_at FROM mcp_client_tokens")}
    from server.network import hub_peers
    peers = await pool.fetch(hub_peers._ACTIVE_BASTIONS_SQL)
    check("the lost laptop's bastion is still revoked, so the Hub gives it no peer",
          tok.get("old-laptop") is not None and not any(p["id"] == 70 for p in peers), (tok, peers))
    check("the bastion paired since the backup is still there", "new-desktop" in tok and tok["new-desktop"] is None)
    check("its management grant is still revoked",
          await pool.fetchval("SELECT revoked_at FROM nac_mgmt_grants WHERE id = 70") is not None)
    check("the ingress rule is the live one, with its live profile",
          [tuple(x) for x in await pool.fetch("SELECT p.name, f.name FROM nac_policy p "
                                              "JOIN nac_profiles f ON f.id = p.profile_id WHERE p.name LIKE 'rig%'")]
          == [("rig new rule", "rig-allow-2")])
    dom = {x[0] for x in await pool.fetch("SELECT domain_id FROM entity_domain_access WHERE entity_id = 9003")}
    check("the memory-domain grant is the live one, and the domain it names is there",
          "rig_new" in dom and "rig_old" not in dom
          and await pool.fetchval("SELECT 1 FROM domain_registry WHERE id = 'rig_new'"), dom)
    check("the new phone's code works", await nac_stepup.verify_stepup(pool, nac_stepup.current_code(new_phone)))
    check("the lost phone's does not",
          old_phone != new_phone and not nac_stepup.verify_totp(
              await nac_stepup.ensure_stepup_secret(pool), nac_stepup.current_code(old_phone)))
    after = await hb.run_once(pool, 3, "manual")
    LOST.update(key=k2_ident, stamp=after["stamp"])
    check("backups carry on from the restored Host", after["state"] == "ok", after)
    ablob = Store.objects.get("/" + (after["object"] or ""))
    man = json.loads((members(k2_ident, ablob) or {}).get("manifest.json") or b"{}")
    check("and they go to the LIVE keys: the new recovery key only, never the retired bastion's "
          "or the old recovery key the restored database still listed",
          [r["fingerprint"] for r in man.get("recipients") or []] == [backup_key.fingerprint(k2_raw)]
          and members(rec_ident, ablob) is None, man.get("recipients"))
    await pool.close()


async def new_number_checks(bridge):
    """Host 3 is lost. Its owner's new Host is number 5, with its own bastion,
    its own second factor and its own recovery key. They restore Host 3's
    last backup onto it."""
    from server import host_restore as hr
    from server.network import hub_peers, nac_stepup
    hr._restart = lambda: None
    print("== a lost Host's backup, restored onto a Host with another number")
    psql("postgres", "ALTER DATABASE hostmem RENAME TO hostmem_host3_lost", "CREATE DATABASE hostmem")
    subprocess.run(["psql", "-h", "/run/postgresql", "-U", "entity", "-d", "hostmem",
                    "--single-transaction", "-q", "-f", "/work/init-db.sql"], capture_output=True)
    for f in sorted(Path("/work/migrations").glob("v*.sql"), key=lambda p: int(p.name[1:].split("_")[0])):
        subprocess.run(["psql", "-h", "/run/postgresql", "-U", "entity", "-d", "hostmem",
                        "-v", "ON_ERROR_STOP=1", "-q", "-f", str(f)], capture_output=True)
    psql("hostmem", "UPDATE entity_registry SET id = 5, privatae_entity_id = 5, display_name = 'fresh' WHERE id = 1")
    for child in APPS.iterdir():
        if child.name != ".host-restore":
            shutil.rmtree(child) if child.is_dir() and not child.is_symlink() else child.unlink()
    pool = await new_pool()
    brain_service._entity_id = 5
    await identity.ensure_host_keypair(pool)
    await pool.execute(
        "INSERT INTO mcp_client_tokens (entity_id, name, token_hash, url_handle, operator_kind, notes) "
        "VALUES (5, 'host5-desk', '\\x05', 'h5', 'bastion', "
        "'{\"wg_bastion_pubkey\": \"pk5\", \"wg_bastion_ip\": \"fd00::5/128\"}'::jsonb)")
    phone5 = await nac_stepup.ensure_stepup_secret(pool)
    await nac_stepup.mark_enrolled(pool)
    k5_ident, k5_rec = age_keys.new_pair()
    k5_raw = age_keys.recipient_bytes(k5_rec)
    await backup_key.put_active(pool, ident=identity.user_identity("5"), pub_raw=k5_raw, sealed=None,
                                origin="owner-browser", kind="host-recovery", label="Host 5 recovery key")
    bridge.recovery = {"recipient": k5_rec, "fingerprint": backup_key.fingerprint(k5_raw),
                       "label": "Host 5 recovery key", "saved_at": "2026-09-29T01:00:00Z"}

    # The owner's browser: Host 3's backup, its key resealed to Host 5 (the page
    # showed both numbers and the owner confirmed).
    side = await hr.sidecar_for_browser(pool, 5, LOST["stamp"])
    opened = age_open(LOST["key"], side["sidecar"].encode())
    body = json.loads(opened.stdout)
    check("the backup's key file names Host 3, and this is Host 5",
          opened.returncode == 0 and body["entity_id"] == 3 and side["entity_id"] == 5, (opened.stderr, side))
    resealed = subprocess.run([AGE, "-a", "-r", side["host_recipient"]], input=opened.stdout,
                              capture_output=True).stdout
    got = await hr.accept_resealed(pool, LOST["stamp"], resealed, via="browser")
    check("Host 5 cannot verify Host 3's backup as its own, and says so", got["verified"] is False, got)
    refused = await hr.stage(pool, 5, LOST["stamp"])
    check("  so it is not staged without the owner's say-so",
          refused["state"] == "failed" and "cannot confirm" in (refused.get("error") or ""), refused)
    await hr.accept_resealed(pool, LOST["stamp"], resealed, via="browser")
    staged = await hr.stage(pool, 5, LOST["stamp"], unverified_ok=True)     # the owner said so
    plan = hr.read_plan() or {}
    check("staged, the plan knows the backup is Host 3's and this is Host 5",
          staged["state"] == "staged" and plan.get("backup_entity_id") == 3 and plan.get("live_entity_id") == 5,
          (staged, plan))
    await pool.close()
    b = boot("swap", "hostmem")
    c = boot("carry", "hostmem")
    check("the boot succeeds: swapped, renumbered, live access kept",
          b.returncode == 0 and c.returncode == 0, (b.stderr, c.stdout, c.stderr))
    pool = await new_pool()
    ids = sorted(r["id"] for r in await pool.fetch("SELECT id FROM entity_registry"))
    check("the restored Host is Host 5 now: no row for Host 3 is left",
          5 in ids and 3 not in ids, ids)
    got = await notes(pool)
    check("the restored data is here", "restore point" in got, got)
    check("and it is Host 5's", await pool.fetchval(
        "SELECT display_name FROM entity_registry WHERE id = 5") == "rig")
    check("Host 5's files are the restored ones",
          (APPS / "app-state/user-files/notes.txt").read_text() == "restore point notes\n")
    toks = {r["name"]: r["revoked_at"] for r in await pool.fetch("SELECT name, revoked_at FROM mcp_client_tokens")}
    peers = [p["id"] for p in await pool.fetch(hub_peers._ACTIVE_BASTIONS_SQL)]
    check("Host 5's own bastion is kept, and none of Host 3's bastions are",
          list(toks) == ["host5-desk"] and len(peers) == 1, (toks, peers))
    check("Host 3's management grants are gone",
          await pool.fetchval("SELECT count(*) FROM nac_mgmt_grants") == 0)
    check("Host 5's phone is the second factor", await nac_stepup.verify_stepup(pool, nac_stepup.current_code(phone5)))
    who = await hb.recipients(pool, 5)
    check("backups now go to Host 5's recovery key only",
          [r["fingerprint"] for r in who] == [backup_key.fingerprint(k5_raw)], who)
    lost = await asyncpg.connect(host="/run/postgresql", user="entity", database="hostmem_host3_lost")
    sign3 = await lost.fetchval("SELECT secret FROM kernel_secrets WHERE id = 'entity_sign'")
    await lost.close()
    sign5 = await pool.fetchval("SELECT secret FROM kernel_secrets WHERE id = 'entity_sign'")
    check("Host 3's signing identity is not Host 5's", sign5 is None or sign5 != sign3, (sign3 is None, sign5 is None))
    hr.mark_started()
    fin = await hr.finish(pool, 5)
    check("finished", fin and fin["state"] == "ok", fin)
    last = await hb.run_once(pool, 5, "manual")
    lblob = Store.objects.get("/" + (last["object"] or ""))
    check("Host 5 backs up, and its recovery key opens it (Host 3's does not)",
          last["state"] == "ok" and members(k5_ident, lblob) is not None and members(LOST["key"], lblob) is None,
          last)
    await pool.close()
    return k5_ident



async def transfer_checks(bridge, k5_ident):
    """Host 5 changes hands. The previous owner registered a bastion's key on
    it; that key lives only here, so the transfer cannot retire it on the
    platform. The platform says when the new owner took over (bridge
    backup.recovery_key owner_since), and nothing made after is sealed to a
    key registered before: not a backup, not an export. Against the real
    backup_user_key table."""
    from server import host_export as hx
    print("== the Host changes hands: nothing more is sealed to the previous owner's keys")
    pool = await new_pool()
    ident = identity.user_identity("5")
    prev_ident, prev_rec = age_keys.new_pair()            # the previous owner's bastion
    await backup_key.put_active(pool, ident=ident, pub_raw=age_keys.recipient_bytes(prev_rec), sealed=None,
                                origin="self-generated", kind="bastion", label="prev-nas")
    before = await hb.run_once(pool, 5, "manual")
    bblob = Store.objects.get("/" + (before["object"] or ""))
    check("before the transfer the previous owner's bastion opens the backup",
          before["state"] == "ok" and members(prev_ident, bblob) is not None, before)
    moved = await pool.fetchval("SELECT now()")
    # The transfer retired the previous owner's recovery key on the platform,
    # and the new owner saved one while this Host slept.
    new_ident, new_rec = age_keys.new_pair()
    new_fp = backup_key.fingerprint(age_keys.recipient_bytes(new_rec))
    bridge.recovery = {"recipient": new_rec, "fingerprint": new_fp, "label": "new owner",
                       "saved_at": "2026-09-29T12:00:00Z", "owner_since": moved.isoformat()}
    # Woken only for the new owner's export, before any backup run.
    one_ident, one_rec = age_keys.new_pair()
    bridge.exports = [{"id": 51, "recipient": one_rec, "requested_at": "2026-09-29T12:01:00Z"}]
    done = await hx.confirm(pool, 5, 51, backup_key.fingerprint(age_keys.recipient_bytes(one_rec)))
    eblob = Store.objects.get("/" + (done.get("s3_key") or ""))
    check("an export built right after the transfer opens with its one-time key, and NOT with the "
          "previous owner's bastion or recovery key",
          eblob and members(one_ident, eblob) is not None and members(prev_ident, eblob) is None
          and members(k5_ident, eblob) is None and done.get("sealed_to") == [one_rec], done)
    rows = await pool.fetch("SELECT kind, label, superseded_at FROM backup_user_key WHERE identity = $1 "
                            "ORDER BY id", ident)
    check("both of the previous owner's keys are retired, and kept listed",
          all(r["superseded_at"] is not None for r in rows) and
          {(r["kind"], r["label"]) for r in rows} >= {("bastion", "prev-nas"), ("host-recovery", "Host 5 recovery key")},
          [dict(r) for r in rows])
    await hb.adopt_recovery_offer(pool, 5, new_fp)        # the new owner confirms their key
    their_ident, their_rec = age_keys.new_pair()
    await backup_key.put_active(pool, ident=ident, pub_raw=age_keys.recipient_bytes(their_rec), sealed=None,
                                origin="self-generated", kind="bastion", label="their-nas")
    after = await hb.run_once(pool, 5, "manual")
    ablob = Store.objects.get("/" + (after["object"] or ""))
    check("the next backup opens with the new owner's recovery key and bastion, never the previous "
          "owner's bastion or recovery key",
          after["state"] == "ok" and members(new_ident, ablob) is not None
          and members(their_ident, ablob) is not None and members(prev_ident, ablob) is None
          and members(k5_ident, ablob) is None, after)
    await pool.close()


asyncio.run(main())
