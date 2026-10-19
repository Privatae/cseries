#!/usr/bin/env python3
"""The Host's own backups (server/host_backup.py): the parts that decide.

When a backup is due; that age keys are named exactly as age names them; that
a backup file is one age file the registered keys open, laid out as promised
(database pieces in order, apps without what backups leave out, Git mirrors,
the manifest last, naming who it is encrypted to); that the per-backup key's
sidecar opens with the registered keys and names its backup; that with no
registered key nothing is written; that a failed dump aborts the upload
instead of leaving half a file; and that platform storage off stages the copy
for the second copy instead.

Real `age` (AGE_BIN, default /usr/local/bin/age, else the one on PATH). The
database is a stand-in pg_dump, the platform a fake bridge plus a tiny HTTP
server taking the presigned PUTs. The whole thing in the kernel image against
a real Postgres is scripts/rig_host_backup.sh.

    python3 scripts/test_host_backup.py
"""
import asyncio
import datetime as dt
import hashlib
import http.server
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

for v in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
    os.environ.pop(v, None)
if not Path(os.environ.get("AGE_BIN", "/usr/local/bin/age")).is_file():
    os.environ["AGE_BIN"] = shutil.which("age") or "/usr/local/bin/age"

from server import age_keys  # noqa: E402
from server import host_backup as hb  # noqa: E402
from gate_loopback import ClientClosesFirst  # noqa: E402

FAILED = []


def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  [{str(detail)[:400]}]"))
    if not cond:
        FAILED.append(label)


def utc(*a):
    return dt.datetime(*a, tzinfo=dt.timezone.utc)


# ── the schedule ────────────────────────────────────────────────────────────
print("== schedule")
s = hb.schedule_of({}, 7)
check("a Host that never set a schedule runs in the 02 UTC hour, spread by id",
      s == {"frequency": "daily", "hour": 2, "minute": 7, "weekday": 0, "tz": "UTC"}, s)
check("frequency off means no backups", hb.schedule_of({"backup": {"frequency": "off"}}, 7) is None)
check("a storage choice alone is not a schedule (the time must not move)",
      hb.schedule_of({"backup": {"platform": False}, "timezone": "America/New_York"}, 7)["tz"] == "UTC")
s = hb.schedule_of({"backup": {"frequency": "daily", "hour": 3, "minute": 15},
                    "timezone": "America/New_York"}, 7)
check("a set schedule is in the user's timezone (03:15 New York = 07:15 UTC in September)",
      hb.last_slot(s, utc(2026, 9, 27, 12, 0)) == utc(2026, 9, 27, 7, 15))
check("before today's time, the last slot is yesterday's",
      hb.last_slot(s, utc(2026, 9, 27, 6, 0)) == utc(2026, 9, 26, 7, 15))
w = hb.schedule_of({"backup": {"frequency": "weekly", "weekday": 0, "hour": 2, "minute": 30}}, 7)
check("weekly lands on the chosen weekday", hb.last_slot(w, utc(2026, 9, 27, 12, 0)) == utc(2026, 9, 21, 2, 30))

print("== due")
d = hb.schedule_of({}, 7)
now = utc(2026, 9, 27, 2, 30)
check("never backed up: due on schedule",
      hb.due(d, now, last_ok=None, last_attempt=None, last_failed=False, last_request=None) == "schedule")
check("slept through the slot: catch-up once awake",
      hb.due(d, utc(2026, 9, 27, 9, 0), last_ok=utc(2026, 9, 26, 2, 8), last_attempt=utc(2026, 9, 26, 2, 7),
             last_failed=False, last_request=None) == "catch-up")
check("backed up after the slot: nothing due",
      hb.due(d, now, last_ok=utc(2026, 9, 27, 2, 8), last_attempt=utc(2026, 9, 27, 2, 7),
             last_failed=False, last_request=None) is None)
check("a failure is not retried inside 30 minutes",
      hb.due(d, now, last_ok=utc(2026, 9, 26, 2, 8), last_attempt=utc(2026, 9, 27, 2, 10),
             last_failed=True, last_request=None) is None)
check("a failure is retried after 30 minutes",
      hb.due(d, utc(2026, 9, 27, 2, 41), last_ok=utc(2026, 9, 26, 2, 8),
             last_attempt=utc(2026, 9, 27, 2, 10), last_failed=True, last_request=None) == "schedule")
# A Host over the file limit was refused, and retried every
# 30 minutes, each time after dumping, encrypting and sending its database.
_ra = getattr(hb, "retry_after", None)
_tmf = [{"parts": json.dumps({"too_many_files": 400000})}]
check("after other failures the retry is 30 minutes",
      _ra is not None and _ra([{"parts": "{}"}] + _tmf) == dt.timedelta(minutes=30) and _ra([]) == dt.timedelta(minutes=30))
check("after refusals for too many files it doubles with each in a row, to a day at most",
      _ra is not None and [_ra(_tmf * k) for k in (1, 2, 3, 6, 12)]
      == [dt.timedelta(hours=1), dt.timedelta(hours=2), dt.timedelta(hours=4), dt.timedelta(hours=24),
          dt.timedelta(hours=24)], _ra and [_ra(_tmf * k) for k in (1, 2, 3, 6, 12)])
check("  and due() holds a retry for as long as it is told",
      _ra is not None and hb.due(d, utc(2026, 9, 27, 2, 41), last_ok=utc(2026, 9, 26, 2, 8),
                                 last_attempt=utc(2026, 9, 27, 2, 10), last_failed=True, last_request=None,
                                 retry=_ra(_tmf)) is None
      and hb.due(d, utc(2026, 9, 27, 3, 11), last_ok=utc(2026, 9, 26, 2, 8),
                 last_attempt=utc(2026, 9, 27, 2, 10), last_failed=True, last_request=None,
                 retry=_ra(_tmf)) is not None)
ok_at = utc(2026, 9, 27, 2, 8)
check("used, then quiet for 10 minutes, last backup over an hour old: quiet backup",
      hb.due(d, utc(2026, 9, 27, 14, 0), last_ok=ok_at, last_attempt=ok_at, last_failed=False,
             last_request=utc(2026, 9, 27, 13, 49)) == "quiet")
check("still in use (quiet for 5 minutes): not yet",
      hb.due(d, utc(2026, 9, 27, 14, 0), last_ok=ok_at, last_attempt=ok_at, last_failed=False,
             last_request=utc(2026, 9, 27, 13, 55)) is None)
check("the last backup is under an hour old: a quiet spell does not add another",
      hb.due(d, utc(2026, 9, 27, 2, 50), last_ok=ok_at, last_attempt=ok_at, last_failed=False,
             last_request=utc(2026, 9, 27, 2, 30)) is None)
boot = utc(2026, 9, 28, 3, 0)
check("a first backup waits its id's minute of the spread (Host 7: 7 minutes)",
      hb.first_backup_waits(7, boot, boot + dt.timedelta(minutes=6, seconds=59))
      and not hb.first_backup_waits(7, boot, boot + dt.timedelta(minutes=7)))
hb._last_request_at = None
hb.note_request("/api/system/health/deep")
check("the readiness probe is not a person", hb._last_request_at is None)
hb.note_request("/api/circles/threads")
check("a real request stamps the time", hb._last_request_at is not None)

print("== what backups leave out")
for rel, out in (("proj/node_modules/x/index.js", True), ("a/b/.venv/bin/python", True),
                 ("proj/.cache/c", True), ("proj/core.1234", True), ("proj/lib/vocabularies/core", False),
                 ("crash/core.1", True), ("host-sdk/scratch/t", True), ("host-sdk/tools/t", False),
                 (".host-restore/plan.json", True), ("app-state/user-files/notes.txt", False)):
    check(f"{rel}: {'left out' if out else 'backed up'}", hb.excluded(rel) is out)


# ── age keys, as age names them ─────────────────────────────────────────────
AGE = hb.AGE
print("== age keys")


def age_decrypt(identity: str, data: bytes) -> subprocess.CompletedProcess:
    argv, fd = hb.age_decrypt_args(identity)
    try:
        return subprocess.run(argv, input=data, capture_output=True, pass_fds=(fd,))
    finally:
        os.close(fd)


ident, rec = age_keys.new_pair()
ct = subprocess.run([AGE, "-r", rec], input=b"hello", capture_output=True).stdout
check("a new pair: age encrypts to the recipient and the identity opens it",
      age_decrypt(ident, ct).stdout == b"hello")
check("the identity is written the way age-keygen writes it",
      ident.startswith("AGE-SECRET-KEY-1") and ident == ident.upper() and rec.startswith("age1"))
check("the recipient of an identity is its own", age_keys.recipient_of_identity(ident) == rec)
other, _ = age_keys.new_pair()
check("any other identity does not open it", age_decrypt(other, ct).returncode != 0)
check("a mangled recipient is refused", not age_keys.valid_recipient(rec[:-1] + ("q" if rec[-1] != "q" else "p")))
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey  # noqa: E402
k = X25519PrivateKey.generate()
pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                      serialization.NoEncryption())
vid = age_keys.identity_from_pem(pem)
vrec = age_keys.recipient(k.public_key().public_bytes(serialization.Encoding.Raw,
                                                        serialization.PublicFormat.Raw))
ct2 = subprocess.run([AGE, "-r", vrec], input=b"vault", capture_output=True).stdout
check("a vault key (PKCS8 PEM) is an age identity for its own recipient",
      age_decrypt(vid, ct2).stdout == b"vault")


# ── the platform: a fake bridge and a PUT/GET server ────────────────────────
class Store(ClientClosesFirst, http.server.BaseHTTPRequestHandler):
    # Every reply carries a Content-Length so the client knows where it ends,
    # and ClientClosesFirst then lets the client hang up first. Closing first
    # (an HTTP/1.0 reply that ended at the close) left a TIME_WAIT on this
    # port per request, 142 in one run, and CI's firewall refuses a later
    # request that lands on one (scripts/gate_loopback.py).
    objects: dict = {}
    signed_sizes: dict = {}
    # Single PUTs signed with If-None-Match: * (the platform's backup store
    # put_url): without the header the signature fails (403, measured on
    # S3-compatible storage);
    # over an object that exists the storage refuses (412).
    if_none_match: set = set()
    refused: list = []
    fail_puts = 0
    slow_s = 0

    def log_message(self, *a):
        pass

    def _empty(self, code, etag=None):
        self.send_response(code)
        if etag:
            self.send_header("ETag", etag)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_PUT(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        if Store.slow_s and self.path.startswith("/part/") and int(self.path.rsplit("/", 1)[1]) > 40:
            import time as _t                # the last parts crawl, after age's input has ended
            _t.sleep(Store.slow_s)
        if Store.fail_puts:
            Store.fail_puts -= 1
            self._empty(500)
            return
        signed = Store.signed_sizes.get(self.path)
        if signed is not None and signed != len(body):
            self._empty(403)                 # a URL signed for another Content-Length
            return
        if self.path in Store.if_none_match:
            if self.headers.get("If-None-Match") != "*":
                Store.refused.append((403, self.path))
                self._empty(403)             # the header is signed into the URL
                return
            if self.path in Store.objects:
                Store.refused.append((412, self.path))
                self._empty(412)             # never over an object that exists
                return
        Store.objects[self.path] = body
        self._empty(200, '"' + hashlib.md5(body).hexdigest() + '"')

    def do_GET(self):
        body = Store.objects.get(self.path)
        self.send_response(200 if body is not None else 404)
        self.send_header("Content-Length", str(len(body or b"")))
        self.end_headers()
        if body is not None:
            self.wfile.write(body)


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Store)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"
PREFIX = {"backup": "b", "staged": "s", "export": "exports", "sidecar": "b", "staged_sidecar": "s"}


class Bridge:
    _connected = True

    def __init__(self):
        self.calls = []
        self.uploads = {}
        self.done = {}
        self.sha = {}
        self.no_stamp = False
        self.clock = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)

    async def request_full(self, op, params=None):
        self.calls.append((op, params))
        p = params or {}
        if op == "backup.upload.start":
            stamp = None
            if p["kind"] in ("backup", "staged"):
                if "name" in p:
                    return {"ok": False, "error": "the platform names a backup; no name is taken"}
                if self.no_stamp:
                    stamp = None
                else:
                    # The platform's clock, one second on at least from the last.
                    self.clock = max(self.clock + dt.timedelta(seconds=1),
                                     dt.datetime.now(dt.timezone.utc).replace(microsecond=0))
                    stamp = self.clock.strftime("%Y%m%dT%H%M%SZ")
                name = stamp or "none"
            else:
                name = p["name"]
            key = f"domain-7/{PREFIX[p['kind']]}/{name}.tar.zst.age"
            if "/" + key in Store.objects:
                return {"ok": False, "error": "that object exists; nothing is ever overwritten"}
            uid = f"u{len(self.uploads) + 1}"
            self.uploads[uid] = {"key": key, "parts": {}}
            return {"ok": True, "data": {"upload_id": uid, "key": key, **({"stamp": stamp} if stamp else {})}}
        if op == "backup.upload.part":
            # Signed for the part's declared size, at most 16 MiB (the platform
            # bridge's op_backup_upload_part).
            size = p.get("size")
            if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= 16 * 1024 * 1024:
                return {"ok": False, "error": "size must be the part's exact bytes, 1..16777216"}
            path = f"/part/{p['upload_id']}/{p['part']}"
            Store.signed_sizes[path] = size
            return {"ok": True, "data": {"url": f"{BASE}{path}", "size": size}}
        if op == "backup.upload.complete":
            up = self.uploads[p["upload_id"]]
            data = b"".join(Store.objects[f"/part/{p['upload_id']}/{x['part']}"] for x in p["parts"])
            if len(data) != p["size"] or hashlib.sha256(data).hexdigest() != p["sha256"]:
                return {"ok": False, "error": "size or digest mismatch"}
            Store.objects["/" + up["key"]] = data
            self.done[up["key"]] = data
            self.sha[up["key"]] = p["sha256"]      # the platform's landing record
            return {"ok": True, "data": {"ok": True, "size": len(data), "key": up["key"]}}
        if op == "backup.upload.abort":
            return {"ok": True, "data": {"ok": True}}
        if op == "backup.put_url":
            if not isinstance(p.get("size"), int):
                return {"ok": False, "error": "size is required: the URL is signed for it"}
            suffix = ".key.age" if "sidecar" in p["kind"] else ".tar.zst.age"
            key = f"domain-7/{PREFIX[p['kind']]}/{p['name']}{suffix}"
            Store.signed_sizes["/" + key] = p["size"]
            Store.if_none_match.add("/" + key)
            return {"ok": True, "data": {"url": f"{BASE}/{key}", "key": key, "size": p["size"],
                                         "headers": {"If-None-Match": "*"}}}
        if op == "backup.list":
            # As the platform names them (the platform's backup store,
            # Store.list): the key relative to the Host's folder, so a staged
            # copy is listed as s/<stamp>.tar.zst.age.
            folder = "/domain-7/"
            pre = f"{folder}{PREFIX[p['kind']]}/"
            return {"ok": True, "data": {"objects": [
                {"name": k[len(folder):], "key": k[1:], "size": len(v),
                 **({"sha256": self.sha[k[1:]]} if k[1:] in self.sha else {})}
                for k, v in Store.objects.items() if k.startswith(pre)]}}
        if op == "backup.drop":
            Store.objects.pop("/" + p["key"], None)
            return {"ok": True, "data": {"ok": True}}
        if op == "backup.recovery_key":
            if RECOVERY.get("down"):
                return {"ok": False, "error_kind": "infra", "error": "bridge down"}
            return {"ok": True, "data": dict(RECOVERY)}
        return {"ok": True, "data": {}}

    async def request(self, op, params=None, **_):
        env = await self.request_full(op, params)
        return env.get("data") if env.get("ok") else None

    def ops(self):
        return [o for o, _ in self.calls]


bridge = Bridge()
hb._bridge = lambda: bridge

# ── a stand-in pg_dump and an apps tree ─────────────────────────────────────
tmp = Path(tempfile.mkdtemp(prefix="hb-test-"))
fake_dump = tmp / "pg_dump"
fake_dump.write_text('#!/bin/sh\n'
                     'if [ -n "$FAKE_DUMP_FAIL" ]; then echo "pg_dump: connection refused" >&2; exit 1; fi\n'
                     'if [ -n "$FAKE_DUMP_HANG" ]; then exec sleep 60; fi\n'
                     'if [ -n "$FAKE_DUMP_MARK" ]; then echo run >> "$FAKE_DUMP_MARK"; fi\n'
                     f'exec python3 -c "import sys; sys.stdout.buffer.write(b\'PGDMP\' + bytes(range(256)) * $((${{FAKE_DUMP_KB:-1}} * 4)))"\n')
fake_dump.chmod(fake_dump.stat().st_mode | stat.S_IEXEC)
hb.PG_DUMP = str(fake_dump)
apps = tmp / "apps"
for rel, text in (("app-state/user-files/notes.txt", "my notes\n"),
                  ("proj/lib/vocabularies/core", "keep me\n"),
                  ("proj/node_modules/x/index.js", "dep\n"),
                  ("proj/core.1234", "dump\n"), ("crash/core.1", "dump\n"),
                  ("host-sdk/scratch/t", "tmp\n"), ("proj/.cache/c", "c\n")):
    (apps / rel).parent.mkdir(parents=True, exist_ok=True)
    (apps / rel).write_text(text)
(apps / "link").symlink_to("app-state")
(apps / "proj/blob.bin").write_bytes(os.urandom(12000))   # does not compress: several parts
(apps / "proj/.git/objects").mkdir(parents=True)
os.link(apps / "proj/lib/vocabularies/core", apps / "proj/.git/objects/core-link")   # a hard link
hb.APPS_ROOT = str(apps)
hb.APPS_EXCLUDES = tuple(p.replace("/var/lib/cseries", str(apps)) for p in hb.APPS_EXCLUDES)
hb.WORKDIR = tmp / "work"
hb.DB_CHUNK = 300       # small, so the dump is really cut into pieces
hb.PART_BYTES = 5000    # small, so the upload really has parts


class Pool:
    def __init__(self):
        self.bp = {}
        self.runs = []
        self.state = {}
        self.secrets = {}         # kernel_secrets

    async def fetchval(self, q, *a):
        if q.startswith("SELECT secret FROM kernel_secrets"):
            return self.secrets.get(a[0])
        if "behavioral_params" in q:
            return json.dumps(self.bp)
        if "current_database" in q:
            return "hostmem"
        if q.startswith("INSERT INTO host_backup_runs"):
            self.runs.append({"id": len(self.runs) + 1, "args": a})
            return len(self.runs)
        if q.startswith("UPDATE host_backup_runs"):
            self.runs[a[0] - 1]["final"] = a
            return dt.datetime.now(dt.timezone.utc)
        if "host_backup_state" in q:
            return self.state.get(a[0])
        raise AssertionError(q)

    async def execute(self, q, *a):
        if q.startswith("INSERT INTO kernel_secrets"):
            self.secrets.setdefault(a[0], a[1])
            return
        if "host_backup_state" in q:
            self.state[a[0]] = a[1] if len(a) > 1 else None
            return
        if q.startswith("UPDATE host_backup_runs SET stamp"):
            self.runs[a[0] - 1]["stamp"] = a[1]
            return
        raise AssertionError(q)

    async def fetchrow(self, q, *a):
        raise AssertionError(q)

    shipped: set = set()          # run ids whose second copy is done

    async def fetch(self, q, *a):
        if "FROM kernel_secrets" in q:
            return [{"id": k, "secret": v} for k, v in self.secrets.items()
                    if k == a[0] or k.startswith(a[1].rstrip("%"))]
        if "FROM host_backup_state WHERE k LIKE 'push:%'" in q:
            return [{"k": k} for k in self.state if k.startswith("push:")]
        if "FROM host_backup_runs r WHERE r.stored = 'staged'" in q:
            return [{"stamp": r["stamp"]} for r in self.runs
                    if len(r["args"]) > 1 and r["args"][1] == "staged" and r["args"][0] != "before-restore"
                    and r.get("stamp") and r["id"] not in self.shipped]
        raise AssertionError(q)


# ── the keys: backup_key's table and this Host's own vault key, faked ──────
from server import backup_key  # noqa: E402
from server.vault import identity as vault_identity  # noqa: E402

rec_ident, rec_rec = age_keys.new_pair()          # the owner's Host recovery key
nas_ident, nas_rec = age_keys.new_pair()          # a bastion's key
host_ident, host_rec = age_keys.new_pair()        # this Host's own vault key
HOST_PUB = age_keys.recipient_bytes(host_rec)
keyrows: list = []


def raw(recipient):
    return age_keys.recipient_bytes(recipient)


async def _list_active(pool, ident):
    return [r for r in keyrows if not r.get("gone")]     # everything: recipients() must filter


async def _get_active(pool, ident, kind="host-recovery", label=None):
    for r in reversed(keyrows):
        if r["kind"] == kind and not r.get("gone") and (label is None or r["label"] == label):
            return r
    return None


async def _put_active(pool, *, ident, pub_raw, sealed, origin, kind="bastion", label=""):
    for r in keyrows:
        if r["kind"] == kind and (kind == "host-recovery" or r["label"] == label):
            r["gone"] = True
    keyrows.append({"kind": kind, "label": label, "pubkey": pub_raw, "origin": origin,
                    "created_at": dt.datetime.now(dt.timezone.utc)})


async def _host_pubkey(pool):
    return HOST_PUB
backup_key.list_active = _list_active
backup_key.get_active = _get_active
_real_put_active = backup_key.put_active
backup_key.put_active = _put_active


async def _retire_recovery(pool, ident):
    hit = False
    for r in keyrows:
        if r["kind"] == "host-recovery" and not r.get("gone"):
            r["gone"] = hit = True
    return hit
backup_key.retire_recovery = _retire_recovery


async def _retire_registered_before(pool, ident, when):
    out = []
    for r in keyrows:
        if r["kind"] in backup_key.RECIPIENT_KINDS and not r.get("gone") and r["created_at"] < when:
            r["gone"] = True
            out.append({"kind": r["kind"], "label": r["label"],
                        "fingerprint": backup_key.fingerprint(r["pubkey"])})
    return out
backup_key.retire_registered_before = _retire_registered_before
vault_identity.host_pubkey = _host_pubkey
RECOVERY = {}                                     # what the platform answers backup.recovery_key


def offer(recipient, label="Host 7 recovery key", fp=None):
    RECOVERY.clear()
    RECOVERY.update(recipient=recipient, label=label, saved_at="2026-09-28T20:00:00Z",
                    fingerprint=fp or backup_key.fingerprint(raw(recipient)))


def run(coro):
    return asyncio.run(coro)


def members(identity, blob):
    got = age_decrypt(identity, blob)
    if got.returncode != 0:
        return None
    from compression import zstd
    out = {}
    with tarfile.open(fileobj=io.BytesIO(zstd.decompress(got.stdout)), mode="r:") as t:
        for m in t:
            out[m.name] = t.extractfile(m).read() if m.isfile() else ("->" + m.linkname if m.issym() else None)
    return out


from server.routes import gitea_client  # noqa: E402
gitea_client.gitea_creds = lambda: None      # no Git server here until the Git part below

print("== nobody to encrypt to")
pool = Pool()
res = run(hb.run_once(pool, 7, "manual"))
check("no recovery key saved: nothing is written, and the run says so",
      res["state"] == "no_recipient" and "save your recovery key" in res["error"]
      and not any(o.startswith("backup.upload") for o in bridge.ops()), res)
keyrows.append({"kind": "bastion", "label": "nas-box", "pubkey": raw(nas_rec), "origin": "self-generated"})
res = run(hb.run_once(pool, 7, "manual"))
check("a bastion's key alone is not enough: no recovery key, no backup",
      res["state"] == "no_recipient" and not any(o.startswith("backup.upload") for o in bridge.ops()), res)

print("== THE RULE: who a backup may be encrypted to")
keyrows.append({"kind": "user", "label": "", "pubkey": raw(age_keys.new_pair()[1]), "origin": "in-pod"})
keyrows.append({"kind": "platform", "label": "", "pubkey": raw(age_keys.new_pair()[1]), "origin": "in-pod"})
keyrows.append({"kind": "bastion", "label": "sneaky", "pubkey": HOST_PUB, "origin": "self-generated"})
keyrows.append({"kind": "host-recovery", "label": "Host 7 recovery key", "pubkey": raw(rec_rec),
                "origin": "owner-browser"})
who = run(hb.recipients(pool, 7))
check("exactly the recovery key and the bastions' keys: no other kind, ever",
      sorted((r["kind"], r["label"]) for r in who)
      == [("bastion", "nas-box"), ("host-recovery", "Host 7 recovery key")], who)
check("and never this Host's own vault key, whatever row claims it",
      host_rec not in [r["recipient"] for r in who])
for bad in ("user", "platform", "host"):
    try:
        run(_real_put_active(None, ident="user:7", pub_raw=bytes(32), sealed=None,
                             origin="self-generated", kind=bad))
        check(f"the key table refuses a {bad!r} key", False)
    except ValueError:
        check(f"the key table refuses a {bad!r} key", True)
keyrows[:] = [r for r in keyrows if r["kind"] in ("bastion",) and r["label"] == "nas-box"]

print("== the recovery key: never pinned on the platform's word")
offer(rec_rec, fp="0" * 16)
check("a first key whose fingerprint does not match is not pinned",
      run(hb.pin_recovery_key(pool, 7))["pinned"] is None)
offer(rec_rec)
RECOVERY["saved_at"] = None
check("a key the owner has not confirmed saving is not pinned",
      run(hb.pin_recovery_key(pool, 7))["pinned"] is None)
offer(rec_rec, label="AGE-SECRET-KEY-1" + "Q" * 58)
check("a label carrying a private key is never kept",
      "AGE-SECRET-KEY" not in json.dumps(run(hb.pin_recovery_key(pool, 7))).upper())
keyrows[:] = [r for r in keyrows if r["kind"] != "host-recovery"]
offer(rec_rec)
pinned = run(hb.pin_recovery_key(pool, 7))
REC_FP = backup_key.fingerprint(raw(rec_rec))
check("the FIRST recovery key the platform relays is not pinned: it waits for the owner (F2)",
      pinned["pinned"] is None and pinned["offer"]["fingerprint"] == REC_FP, pinned)
check("  so nothing is encrypted to it yet", run(hb.recipients(pool, 7)) == [])
res = run(hb.run_once(pool, 7, "manual"))
check("  and no backup is taken; the run says to confirm the key in Settings",
      res["state"] == "no_recipient" and "check its fingerprint" in res["error"], res)
try:
    run(hb.adopt_recovery_offer(pool, 7, "0" * 16))
    check("a fingerprint other than the offer's adopts nothing", False)
except hb.BackupError:
    check("a fingerprint other than the offer's adopts nothing",
          run(hb.pin_recovery_key(pool, 7))["pinned"] is None)
got = run(hb.adopt_recovery_offer(pool, 7, REC_FP))
pinned = run(hb.pin_recovery_key(pool, 7))
check("the owner's confirmation (the fingerprint they compared) pins it",
      got == {"fingerprint": REC_FP, "first": True}
      and pinned["pinned"] and pinned["pinned"]["fingerprint"] == REC_FP and pinned["offer"] is None, pinned)
other_ident, other_rec = age_keys.new_pair()
offer(other_rec)
pinned = run(hb.pin_recovery_key(pool, 7))
check("a DIFFERENT key later (a new key, or a new owner after a transfer) is not adopted: it waits",
      pinned["pinned"] is None and pinned["offer"]["fingerprint"] == backup_key.fingerprint(raw(other_rec)),
      pinned)
who = run(hb.recipients(pool, 7))
check("  and the key pinned before is not the owner's current one: nothing is sealed to it, or to the new",
      who == [] and any(r["kind"] == "host-recovery" and r.get("gone") and r["pubkey"] == raw(rec_rec)
                        for r in keyrows), who)
offer(rec_rec)
run(hb.pin_recovery_key(pool, 7))
run(hb.adopt_recovery_offer(pool, 7, REC_FP))
check("the owner confirms the key they hold, and it is used again",
      run(hb.pin_recovery_key(pool, 7))["pinned"]["fingerprint"] == REC_FP)
offer(age_keys.new_pair()[1], fp="0" * 16)
pinned = run(hb.pin_recovery_key(pool, 7))
check("a key whose fingerprint does not match is ignored: not pinned, not offered",
      pinned["pinned"]["fingerprint"] == backup_key.fingerprint(raw(rec_rec)) and pinned["offer"] is None,
      pinned)
RECOVERY["down"] = True
pinned = run(hb.pin_recovery_key(pool, 7))
check("the platform unreachable: the pinned key stands", pinned["pinned"] is not None)
RECOVERY.clear()                                  # answered: {} (the owner holds no key)
pinned = run(hb.pin_recovery_key(pool, 7))
check("the platform answers that the owner holds no recovery key: the pinned one is no longer used",
      pinned["pinned"] is None and pinned["offer"] is None and run(hb.recipients(pool, 7)) == [], pinned)
res = run(hb.run_once(pool, 7, "manual"))
check("  and no backup is sealed to it", res["state"] == "no_recipient", res)
offer(rec_rec)
run(hb.pin_recovery_key(pool, 7))
run(hb.adopt_recovery_offer(pool, 7, REC_FP))
pool.state["recovery_offer"] = json.dumps({"fingerprint": "stale"})
run(hb.pin_recovery_key(pool, 7))
check("the pinned key offered again clears the waiting offer", run(hb.pin_recovery_key(pool, 7))["offer"] is None)

print("== the Host changes hands: the previous owner's keys, bastions included, seal nothing more")
import copy  # noqa: E402
kept_rows, kept_state = copy.deepcopy(keyrows), dict(pool.state)
for r in keyrows:
    r.setdefault("created_at", utc(2026, 9, 1))           # registered by the previous owner
moved = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)
check("before the transfer the previous owner's bastion is sealed to",
      ("bastion", "nas-box") in [(r["kind"], r["label"]) for r in run(hb.recipients(pool, 7))])
RECOVERY.clear()
RECOVERY["owner_since"] = moved.isoformat()               # the transfer retired their key; none saved yet
pinned = run(hb.pin_recovery_key(pool, 7))
check("the transfer: no key, and the previous owner's recovery AND bastion keys are retired",
      pinned["pinned"] is None and run(hb.recipients(pool, 7)) == []
      and all(r.get("gone") for r in keyrows if r["pubkey"] in (raw(nas_rec), raw(rec_rec))), keyrows)
new_ident, new_rec = age_keys.new_pair()                  # the new owner's recovery key
offer(new_rec)
RECOVERY["owner_since"] = moved.isoformat()
run(hb.pin_recovery_key(pool, 7))
run(hb.adopt_recovery_offer(pool, 7, backup_key.fingerprint(raw(new_rec))))
who = run(hb.recipients(pool, 7))
check("the new owner confirms their key: backups are sealed to it ALONE, never the previous "
      "owner's bastion", [(r["kind"], r["recipient"]) for r in who] == [("host-recovery", new_rec)], who)
their_ident, their_rec = age_keys.new_pair()
run(_put_active(None, ident="user:7", pub_raw=raw(their_rec), sealed=None, origin="self-generated",
                kind="bastion", label="their-nas"))
run(hb.pin_recovery_key(pool, 7))
check("a bastion the NEW owner registers is used, and stays in use",
      sorted(r["recipient"] for r in run(hb.recipients(pool, 7))) == sorted([new_rec, their_rec]))
# The Host slept through the transfer: the new owner had already saved a key.
keyrows[:] = copy.deepcopy(kept_rows)
for r in keyrows:
    r.setdefault("created_at", utc(2026, 9, 1))
offer(new_rec)
RECOVERY["owner_since"] = moved.isoformat()
pinned = run(hb.pin_recovery_key(pool, 7))
check("a Host that slept through the transfer retires the previous owner's keys the first time it asks",
      pinned["pinned"] is None and pinned["offer"]["fingerprint"] == backup_key.fingerprint(raw(new_rec))
      and run(hb.recipients(pool, 7)) == [], pinned)
keyrows[:] = copy.deepcopy(kept_rows)
for r in keyrows:
    r.setdefault("created_at", utc(2026, 9, 1))
for bogus in ((dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat(), "yesterday",
              "2026-09-20T00:00:00"):
    offer(rec_rec)
    RECOVERY["owner_since"] = bogus
    run(hb.pin_recovery_key(pool, 7))
    check(f"an owner_since that is not a past moment ({bogus[:20]}) retires nothing",
          len(run(hb.recipients(pool, 7))) == 2)
keyrows[:] = kept_rows
pool.state.clear()
pool.state.update(kept_state)
offer(rec_rec)

print("== one backup")
import logging  # noqa: E402
log_lines = []


class _Capture(logging.Handler):
    def emit(self, record):
        log_lines.append(self.format(record))
logging.getLogger().addHandler(_Capture())
logging.getLogger().setLevel(logging.DEBUG)
os.environ["FAKE_DUMP_KB"] = "2"
res = run(hb.run_once(pool, 7, "manual"))
check("the run ended ok", res["state"] == "ok", res)
key = res["object"]
blob = bridge.done.get(key)
check("into platform storage (b/) as one encrypted file", key.endswith(".tar.zst.age") and "/b/" in key
      and blob and not blob.startswith(b"PGDMP"), key)
check("uploaded in parts, each through its own presigned URL",
      bridge.ops().count("backup.upload.part") >= 2, bridge.ops().count("backup.upload.part"))
got = members(rec_ident, blob)
check("the recovery key opens it", got is not None)
check("and so does the bastion's key", members(nas_ident, blob) is not None)
check("no other key does", members(other, blob) is None)
check("THE RULE: this Host's own vault key does not open the backup", members(host_ident, blob) is None)
header = []
for line in blob.split(b"\n"):
    if line.startswith(b"---"):
        break
    header.append(line)
stanzas = [l for l in header if l.startswith(b"-> X25519 ")]
check("THE RULE: the file is sealed to exactly three keys: its own, the recovery key, the bastion's",
      len(stanzas) == 3, len(stanzas))
names = list(got)
chunks = sorted(n for n in names if n.startswith("db/hostmem.dump."))
dump = b"".join(got[n] for n in chunks)
check("the database is cut into pieces that join back into the dump",
      len(chunks) > 1 and dump.startswith(b"PGDMP") and len(dump) == 5 + 256 * 8
      and chunks[0].endswith(".00000"), chunks[:3])
check("the user's files are in it", got.get("apps/app-state/user-files/notes.txt") == b"my notes\n")
check("a real file named core is in it", got.get("apps/proj/lib/vocabularies/core") == b"keep me\n")
check("a link is kept as a link", got.get("apps/link") == "->app-state")
check("a hard-linked file is stored whole under each of its names",
      got.get("apps/proj/.git/objects/core-link") == b"keep me\n"
      and got.get("apps/proj/lib/vocabularies/core") == b"keep me\n")
check("what backups leave out is not in it",
      not any("node_modules" in n or "core.1234" in n or "/crash" in n or "scratch" in n or ".cache" in n
              for n in names), [n for n in names if "node_modules" in n or "crash" in n])
check("the manifest is the last file but the README", names[-2:] == ["manifest.json", "README.txt"],
      names[-3:])
man = json.loads(got["manifest.json"])
check("the manifest names who it is encrypted to, never a key",
      sorted((r["kind"], r["label"]) for r in man["recipients"])
      == [("bastion", "nas-box"), ("host-recovery", "Host 7 recovery key")]
      and "recipient" not in json.dumps(man["recipients"]), man["recipients"])
check("and every file's hash, and the whole dump's",
      man["parts"]["db"]["sha256"] == hashlib.sha256(dump).hexdigest()
      and all(hashlib.sha256(got[f["path"]]).hexdigest() == f["sha256"] for f in man["files"] if "link" not in f))
check("and every link with its target, so a restore checks links as it checks files",
      [(f["path"], f["link"]) for f in man["files"] if "link" in f] == [("apps/link", "app-state")],
      [f for f in man["files"] if "link" in f])
readme = got["README.txt"].decode()
check("the README says how to open it with plain age, and promises no other tool",
      "age -d -i" in readme and "privatae-bastion" not in readme)

print("== the sidecar")
side_key = key.replace(".tar.zst.age", ".key.age")
armored = Store.objects.get("/" + side_key)
check("the sidecar is next to it, sealed as armored age",
      armored and armored.startswith(b"-----BEGIN AGE ENCRYPTED FILE-----"))
stamp = res["stamp"]
body = hb.open_sidecar(armored, rec_ident, stamp=stamp)
check("the recovery key opens it, and it names its backup",
      body["stamp"] == stamp and body["object"].endswith(f"{stamp}.tar.zst.age"))
check("the bastion's key opens it too", hb.open_sidecar(armored, nas_ident, stamp=stamp)["identity"] == body["identity"])
check("the key inside opens the backup: a restore needs nothing else",
      members(body["identity"], blob) is not None)
try:
    hb.open_sidecar(armored, rec_ident, stamp="20200101T000000Z")
    check("a sidecar for a different backup is refused", False)
except hb.BackupError:
    check("a sidecar for a different backup is refused", True)
resealed = subprocess.run([AGE, "-a", "-r", host_rec], input=age_decrypt(rec_ident, armored).stdout,
                          capture_output=True).stdout
check("resealed to a restoring Host's key, that Host opens it",
      hb.open_sidecar(resealed, host_ident, stamp=stamp)["identity"] == body["identity"])
try:
    hb.open_sidecar(armored, host_ident, stamp=stamp)
    check("the Host's own key never opens an original sidecar", False)
except hb.BackupError:
    check("the Host's own key never opens an original sidecar", True)

print("== proof that a backup is this Host's own")
auth = pool.secrets.get(hb.AUTH_KEY_ID)
check("this Host has a backup-auth key, made on its first backup and kept in kernel_secrets",
      isinstance(auth, bytes) and len(auth) == 32)
check("the key file names the encrypted file exactly: its sha256 and size",
      body["data_sha256"] == hashlib.sha256(blob).hexdigest() and body["data_bytes"] == len(blob), body.get("data_bytes"))
keys_here = run(hb.auth_keys(pool))
check("its tag verifies with this Host's key", hb.auth_verifies(body, keys_here))
check("and with no other key", not hb.auth_verifies(body, {hb.auth_kid(os.urandom(32)): os.urandom(32)}))
forged = dict(body, data_sha256="0" * 64)
check("a key file changed after it was made does not verify (it names another file)",
      not hb.auth_verifies(forged, keys_here))
untagged = {k: v for k, v in body.items() if k != "auth"}
check("a key file with no tag does not verify", not hb.auth_verifies(untagged, keys_here))
own_forger = dict(untagged)
own_forger["auth"] = hb.auth_tag(untagged, os.urandom(32))
check("nor one tagged by anyone who does not hold this Host's key (what platform storage could make)",
      not hb.auth_verifies(own_forger, keys_here))
check("THE RULE holds: the tag key is a MAC key, not an age key, and never went to the platform",
      auth.hex() not in json.dumps(bridge.calls, default=str) and b"AGE-SECRET-KEY" not in auth.upper())
check("the key is the same for every run (made once)", run(hb.auth_key(pool)) == auth)
final = pool.runs[-1]["final"]
check("the run records where the file and its key went, and who they are for",
      final[2] == key and final[3] == side_key
      and {r["kind"] for r in json.loads(pool.runs[-1]["args"][2])} == {"host-recovery", "bastion"})

# THE RULE: once a run ends, no key that opens a backup is anywhere the
# platform could look: not in this Host's database, not in its scratch, not
# in its logs, not in what it sent the platform, not in clear in storage.
SECRET = b"AGE-SECRET-KEY-"
written = json.dumps([r.get("args") for r in pool.runs] + [r.get("final") for r in pool.runs]
                     + [pool.state], default=str).encode()
check("no private key is in anything the run wrote to the database", SECRET not in written.upper())
check("nothing is left in the run's scratch", not hb.WORKDIR.exists()
      or not any(SECRET in p.read_bytes().upper() for p in hb.WORKDIR.rglob("*") if p.is_file()))
check("no private key is in any log line", not any("AGE-SECRET-KEY-" in l.upper() for l in log_lines))
check("no private key went to the platform", SECRET not in json.dumps(bridge.calls).encode().upper())
check("no private key is in clear in storage", not any(SECRET in v.upper() for v in Store.objects.values()))
report = next(p for o, p in reversed(bridge.calls) if o == "backup.report")
check("the platform is told how it went, with no key in it",
      report["state"] == "ok" and report["repo"] is None
      and "recipient" not in json.dumps(report["parts"]["recipients"]), report)

print("== the platform dates a backup, never the Host")
starts = [p for o, p in bridge.calls if o == "backup.upload.start" and p["kind"] in ("backup", "staged")]
check("the Host asks to start a backup with its kind only: no name, no date",
      starts and all(set(p) == {"kind"} for p in starts), starts[:2])
check("the stamp the platform gave names the file, the key file and the run, and is inside both",
      key.endswith(f"/b/{stamp}.tar.zst.age") and side_key.endswith(f"/b/{stamp}.key.age")
      and man["stamp"] == stamp and body["stamp"] == stamp and pool.runs[-1]["stamp"] == stamp)
side_puts = [p for o, p in bridge.calls if o == "backup.put_url"]
check("the key file's upload is signed for its exact size",
      side_puts and side_puts[-1]["size"] == len(armored), side_puts[-1:])
parts_asked = [p for o, p in bridge.calls if o == "backup.upload.part"]
check("every part's URL is asked for with that part's exact size (the platform signs it)",
      parts_asked and all(isinstance(p.get("size"), int) and 0 < p["size"] <= hb.PART_BYTES for p in parts_asked)
      and sum(p["size"] for p in parts_asked if p["upload_id"] == parts_asked[0]["upload_id"]) > 0, parts_asked[:2])
check("the key file went up with If-None-Match: * (no refusal from storage)", not Store.refused, Store.refused)
try:
    hb._put(f"{BASE}/{side_key}", armored, 30, {"If-None-Match": "*"})
    check("a second write to the key file is refused by storage (412)", False)
except Exception:
    check("a second write to the key file is refused by storage (412)", (412, "/" + side_key) in Store.refused)
listed = run(hb.call("backup.list", {"kind": "backup"}))["objects"]
check("the platform lists what landed with the sha256 the Host declared",
      any(o["key"] == key and o.get("sha256") == hashlib.sha256(blob).hexdigest() for o in listed), listed[:2])
Store.signed_sizes["/domain-7/b/x.key.age"] = 3
try:
    hb._put(f"{BASE}/domain-7/b/x.key.age", b"four")
    check("storage refuses bytes the URL was not signed for", False)
except Exception:
    check("storage refuses bytes the URL was not signed for", True)
a1 = run(hb.run_once(pool, 7, "manual"))
a2 = run(hb.run_once(pool, 7, "manual"))
check("two backups back to back each get their own stamp from the platform",
      a1["state"] == a2["state"] == "ok" and a1["stamp"] != a2["stamp"], (a1["stamp"], a2["stamp"], a2.get("error")))
bridge.no_stamp = True
n_abort = sum(1 for o, _ in bridge.calls if o == "backup.upload.abort")
res = run(hb.run_once(pool, 7, "manual"))
bridge.no_stamp = False
check("a platform that gives no stamp gets no backup, and the upload is aborted",
      res["state"] == "failed" and "time stamp" in (res["error"] or "")
      and sum(1 for o, _ in bridge.calls if o == "backup.upload.abort") == n_abort + 1, res)

print("== a dump that fails")
os.environ["FAKE_DUMP_FAIL"] = "1"
n_complete = bridge.ops().count("backup.upload.complete")
res = run(hb.run_once(pool, 7, "manual"))
os.environ.pop("FAKE_DUMP_FAIL")
check("a backup without its database fails, saying why",
      res["state"] == "failed" and "database dump failed" in (res["error"] or ""), res)
check("and its upload is aborted, never completed",
      bridge.ops().count("backup.upload.complete") == n_complete and bridge.ops()[-3:].count("backup.upload.abort") >= 1
      or "backup.upload.abort" in bridge.ops()[n_complete:], bridge.ops()[-6:])

print("== a Host with more files than one backup can list")
check("the list's limit is well under what a restore could parse without trouble (256 MiB before)",
      hb.MANIFEST_MAX <= 64 * 1024 * 1024)
_real_max = hb.MANIFEST_MAX
hb.MANIFEST_MAX = 200
n_complete = bridge.ops().count("backup.upload.complete")
n_start = bridge.ops().count("backup.upload.start")
_mark = tmp / "dump-runs"
os.environ["FAKE_DUMP_MARK"] = str(_mark)
res = run(hb.run_once(pool, 7, "manual"))
os.environ.pop("FAKE_DUMP_MARK")
hb.MANIFEST_MAX = _real_max
check("it is refused a backup, saying why, rather than given one its restore would refuse",
      res["state"] == "failed" and "more files than one backup can list" in (res["error"] or ""), res)
check("  and nothing is kept", bridge.ops().count("backup.upload.complete") == n_complete)
# The database was dumped first, so each refusal came
# after the whole database had been dumped, encrypted and sent.
check("  and it is refused before the database is dumped or an upload is started",
      not _mark.exists() and bridge.ops().count("backup.upload.start") == n_start,
      (_mark.exists(), bridge.ops().count("backup.upload.start") - n_start))
check("  the run records the refusal, so the next retry backs off",
      (res.get("parts") or {}).get("too_many_files", 0) > 0, res.get("parts"))


class _NextPool(Pool):
    """next_action's reads: the latest finished runs, as host_backup_runs holds them."""

    def __init__(self, rows):
        super().__init__()
        self.rows = rows            # newest first: {"started_at", "state", "parts"}

    async def fetchrow(self, q, *a):
        if "FROM host_backup_runs" in q:
            return self.rows[0] if self.rows else None
        raise AssertionError(q)

    async def fetchval(self, q, *a):
        if "max(started_at)" in q and "'ok', 'partial'" in q:
            oks = [r["started_at"] for r in self.rows if r["state"] in ("ok", "partial")]
            return max(oks) if oks else None
        return await super().fetchval(q, *a)

    async def fetch(self, q, *a):
        if "SELECT parts FROM host_backup_runs" in q:
            return [{"parts": r["parts"]} for r in self.rows]
        return await super().fetch(q, *a)


_refused = json.dumps({"too_many_files": 400000})
_t = utc(2026, 9, 27, 12, 0)
_rows = [{"started_at": _t - dt.timedelta(hours=h), "state": "failed", "parts": _refused} for h in (0, 2, 4)]
_boot = _t - dt.timedelta(days=1)
_na = [run(hb.next_action(_NextPool(_rows), 7, _t + dt.timedelta(hours=h), _boot)) for h in (1, 3.9, 4.1)]
check("three refusals in a row: the loop waits 4 hours, not 30 minutes, before it tries again",
      _na[0] is None and _na[1] is None and _na[2] is not None and _na[2][0] == "run", _na)
_na = run(hb.next_action(_NextPool([{"started_at": _t, "state": "failed", "parts": "{}"}]), 7,
                         _t + dt.timedelta(minutes=31), _boot))
check("  another failure is still retried after 30 minutes", _na is not None and _na[0] == "run", _na)
# Once the back-off reached a day, removing
# the files did not bring the scheduled backup back sooner, and the refusal
# did not say to press Back up now.
_rows6 =[{"started_at": _t - dt.timedelta(days=h), "state": "failed", "parts": _refused} for h in range(6)]
check("  six refusals in a row: the scheduled backup waits a day",
      hb.retry_after(_rows6) == dt.timedelta(hours=24))
_real_write = hb.write_file


def _grew(**kw):
    # The list passed its count, then grew past the limit while the run read it.
    raise hb.TooManyFiles(400001)


hb.write_file = _grew
try:
    _sched = run(hb.run_once(pool, 7, "schedule"))
    _manual = run(hb.run_once(pool, 7, "manual"))
finally:
    hb.write_file = _real_write
_said = _sched["error"] or ""
check("the refusal says to press Back up now once files are removed, and that the schedule backs off",
      "more files than one backup can list" in _said and "press Back up now in Settings" in _said
      and _said.endswith("up to a day") and len(_said) <= hb.RUN_ERROR_MAX, _said)
check("  a scheduled run that passes its count and is then refused still counts toward the back-off",
      _sched["state"] == "failed" and (_sched.get("parts") or {}).get("too_many_files"), _sched.get("parts"))
check("  a Back up now that passes its count does not, whatever happens after",
      _manual["state"] == "failed" and "too_many_files" not in (_manual.get("parts") or {})
      and "more files than one backup can list" in (_manual["error"] or ""), (_manual.get("parts"), _manual["error"]))
_after = [{"started_at": _t, "state": "failed", "parts": json.dumps(_manual.get("parts") or {})}] + _rows6
_na = [run(hb.next_action(_NextPool(_after), 7, _t + dt.timedelta(minutes=m), _boot)) for m in (29, 31)]
check("  so after it the scheduled backup tries again in 30 minutes, not a day",
      _na[0] is None and _na[1] is not None and _na[1][0] == "run", _na)
_na = run(hb.next_action(_NextPool([{"started_at": _t, "state": "failed", "parts": _refused}] + _rows6), 7,
                         _t + dt.timedelta(hours=23), _boot))
check("  while a Back up now refused by its count adds to the back-off (still waiting 23 h on)", _na is None, _na)
# The limit was checked only after the database, the apps
# and the Git trees had all been read and sent. It is now counted as the list
# grows, so the walk stops at the entry that crosses it.
_real_add, walked = hb._add_member, []


def _counting_add(tout, full, arc, files, notes, **kw):
    walked.append(arc)
    return _real_add(tout, full, arc, files, notes, **kw)


hb._add_member = _counting_add
res_full = run(hb.run_once(pool, 7, "manual"))
walked_full, walked[:] = len(walked), []
hb.MANIFEST_MAX = 200
res = run(hb.run_once(pool, 7, "manual"))
hb.MANIFEST_MAX = _real_max
hb._add_member = _real_add
check(f"it stops as the list reaches the limit: {len(walked)} of the {walked_full} names walked",
      res_full["state"] == "ok" and res["state"] == "failed"
      and "more files than one backup can list" in (res["error"] or "") and len(walked) < walked_full / 2,
      (len(walked), walked_full, res["error"]))
_entries = [{"path": "apps/a", "bytes": 1, "sha256": "0" * 64}, {"path": "apps/b" + chr(0xe9), "link": "x\"y"},
            {"path": "apps/c", "bytes": 12345678, "sha256": "f" * 64}]
_head = {"format": "x", "parts": {}, "files": []}
_listing = hb._Listing(len(json.dumps(_head))) if hasattr(hb, "_Listing") else None
if _listing is not None:
    for _e in _entries:
        _listing.append(_e)
check("  and the running count is the manifest's size exactly (with the parts it has so far)",
      _listing is not None and _listing.size == len(json.dumps({**_head, "files": list(_listing)}).encode()),
      _listing and (_listing.size, len(json.dumps({**_head, "files": list(_listing)}))))

print("== a name with a line break in it")
# A name holding a line break reached the owner's run list
# (Settings shows it with white-space: pre-line). A restore now refuses such a
# name, so a backup leaves it out, rather than being one its restore refuses.
(apps / "odd\nname.txt").write_text("x\n")
(apps / ("sep" + chr(0x2028) + "dir")).mkdir()
(apps / ("sep" + chr(0x2028) + "dir") / "inner.txt").write_text("y\n")
(apps / ("sep" + chr(0x2028) + "dir") / "sub").mkdir()
(apps / ("sep" + chr(0x2028) + "dir") / "sub" / "deeper.txt").write_text("z\n")
(apps / "proj" / ("mark" + chr(0x200e) + ".txt")).write_text("m\n")
res = run(hb.run_once(pool, 7, "manual"))
got = members(rec_ident, bridge.done.get(res.get("object") or "", b"")) or {}
_unshowable = getattr(hb, "unshowable", lambda n: any(ord(c) < 32 or c == chr(0x2028) for c in n))
check("the backup is made, and says what it left out and why",
      res["state"] == "partial" and "3 name(s) with a line break" in (res["error"] or ""), res)
# A folder counted as one name though everything in it
# was gone too, and the sentence did not say where any of them was.
_err = res["error"] or ""
check("  it says where each is on the Host, its odd characters written as codes, and how many files went",
      f"{apps}/odd\\nname.txt" in _err and f"{apps}/sep\\u2028dir (a folder, 2 file(s) in it)" in _err
      and f"{apps}/proj/mark\\u200e.txt" in _err and "4 file(s) in all" in _err
      and not _unshowable(_err), _err)
check("  a bidi mark (U+200E) is left out like the others", not any(chr(0x200e) in n for n in got), sorted(got)[:5])
check("  it holds no such name, in the file or its manifest, and the rest is there",
      got and not any(_unshowable(n) for n in got)
      and not any(_unshowable(f["path"]) for f in json.loads(got["manifest.json"])["files"])
      and got.get("apps/app-state/user-files/notes.txt") == b"my notes\n", sorted(got)[:8])
(apps / "odd\nname.txt").unlink()
shutil.rmtree(apps / ("sep" + chr(0x2028) + "dir"))
(apps / "proj" / ("mark" + chr(0x200e) + ".txt")).unlink()
res = run(hb.run_once(pool, 7, "manual"))
check("once the names are gone the next backup is whole again, not partial",
      res["state"] == "ok" and not res.get("error"), (res["state"], res.get("error")))
# The run's error is cut at 800 characters and
# the instruction came after the paths, so long paths cut it off, and the Git
# clause with it.
_long = [{"path": "apps/" + ("p%02d-" % i) + "x" * (117 - len(str(hb.APPS_ROOT)) - 7) + "\\n"}
         for i in range(8)]
_apps_notes = {"ok": False, "left_out": 8, "left_out_files": 8, "left_out_names": _long}
_apps_notes["error"] = hb._left_out_text(_apps_notes, hb.APPS_ROOT)
_git_names = [{"path": "gitea-repos/owner__r%d.git/refs/heads/b%d\\n" % (i, i)} for i in range(2)]
_git_notes = {"ok": False, "repos": 2, "left_out": 2, "left_out_files": 2, "left_out_names": _git_names}
_git_notes["error"] = hb._left_out_text(_git_notes, None)
# (Before _partial_error, the run joined the clauses and cut them at 800.)
_join = getattr(hb, "_partial_error", lambda parts: "; ".join(
    f"{p}: {(parts.get(p) or {}).get('error') or 'some files could not be read'}"
    for p in ("apps", "gitea") if not (parts.get(p) or {}).get("ok"))[:800])
_err = _join({"db": {"ok": True}, "apps": _apps_notes, "gitea": _git_notes})
_shown = [f"{hb.APPS_ROOT}/" + e["path"][len("apps/"):] for e in _long]
check(f"117-character paths: every path the paths clause names is whole ({sum(s in _err for s in _shown)} "
      f"named), and the rest are counted",
      all(len(s) == 117 for s in _shown) and sum(s in _err for s in _shown) >= 1
      and f", and {8 - sum(s in _err for s in _shown)} more" in _err, _err)
check("  both clauses keep their instruction, the Git one included, inside 800 characters",
      len(_err) <= 800 and _err.count("Rename or remove them and the next backup is whole again") == 2
      and "gitea: 2 name(s)" in _err and all(e["path"] in _err for e in _git_names), (len(_err), _err))
_one = _join({"db": {"ok": True}, "apps": _apps_notes, "gitea": {"ok": True}})
check(f"  with only the apps clause, it spells out more of them ({sum(s in _one for s in _shown)})",
      len(_one) <= 800 and sum(s in _one for s in _shown) > sum(s in _err for s in _shown)
      and "Rename or remove them" in _one, _one)
# Both clauses long: neither may take the 800 and leave the other nothing.
_git_long = [{"path": "gitea-repos/" + ("r%02d-" % i) + "y" * 100 + "\\n"} for i in range(8)]
_git_long_notes = {"ok": False, "repos": 8, "left_out": 8, "left_out_files": 8, "left_out_names": _git_long}
_git_long_notes["error"] = hb._left_out_text(_git_long_notes, None)
_both = _join({"db": {"ok": True}, "apps": _apps_notes, "gitea": _git_long_notes})
check("  with both clauses long, each keeps its instruction and names a whole path, inside 800 characters",
      len(_both) <= 800 and _both.count("Rename or remove them and the next backup is whole again") == 2
      and any(s in _both for s in _shown) and any(e["path"] in _both for e in _git_long),
      (len(_both), _both))

print("== a slow upload is waited for")
(apps / "proj/big.bin").write_bytes(os.urandom(400_000))   # more than a pipe holds
Store.slow_s = 0.2
_real_wait = hb.AGE_EXIT_WAIT_S
hb.AGE_EXIT_WAIT_S = 1
res = run(hb.run_once(pool, 7, "manual"))
hb.AGE_EXIT_WAIT_S = _real_wait
Store.slow_s = 0
(apps / "proj/big.bin").unlink()
check("a part that takes longer than age's exit wait does not fail the backup",
      res["state"] == "ok", res.get("error"))

print("== a run that goes past its time limit")
os.environ["FAKE_DUMP_HANG"] = "1"
_real_limit = hb.RUN_LIMIT_S
hb.RUN_LIMIT_S = 1
n_complete = bridge.ops().count("backup.upload.complete")
n_abort = bridge.ops().count("backup.upload.abort")
import time as _time  # noqa: E402
t0 = _time.monotonic()
res = run(hb.run_once(pool, 7, "manual"))
took = _time.monotonic() - t0
hb.RUN_LIMIT_S = _real_limit
os.environ.pop("FAKE_DUMP_HANG")
check("a run that hangs is stopped at its limit, says so, and its upload is aborted, never completed",
      res["state"] == "failed" and "time limit" in (res["error"] or "") and took < 30
      and bridge.ops().count("backup.upload.complete") == n_complete
      and bridge.ops().count("backup.upload.abort") == n_abort + 1, (res.get("error"), took))
check("the whole run is bounded below the platform's six hours for an unfinished upload",
      hb.RUN_LIMIT_S < 6 * 3600)

print("== a part that fails once is sent again")
Store.fail_puts = 1
res = run(hb.run_once(pool, 7, "manual"))
check("the run still ends ok", res["state"] == "ok", res)

print("== platform storage off")
pool.bp = {"backup": {"platform": False, "target": {"kind": "machine", "address": "nas-box"}}}
first = run(hb.run_once(pool, 7, "manual"))
second = run(hb.run_once(pool, 7, "manual"))
check("the file and its key are staged (s/) for the second copy",
      "/s/" in (first["object"] or "") and "/s/" in (second["object"] or ""), first["object"])
listed_names = [o["name"] for o in run(hb.call("backup.list", {"kind": "staged"}))["objects"]]
check("the stand-in lists staged copies by the platform's names (s/<stamp>...)",
      listed_names and all(n.startswith("s/") for n in listed_names), listed_names)
staged = sorted(k for k in Store.objects if k.startswith("/domain-7/s/"))
check("the copy just staged and its key survive; an older one this Host wrote and never sent is dropped",
      all(second["stamp"] in k for k in staged) and len(staged) == 2, staged)


def staged_now():
    return sorted(k for k in Store.objects if k.startswith("/domain-7/s/"))


def plant(stamp):
    """A staged copy and its key put there by something else (a bastion's
    push-back), as a restore then reads it."""
    for suffix in (".tar.zst.age", ".key.age"):
        Store.objects[f"/domain-7/s/{stamp}{suffix}"] = b"x"


pushed = "20250101T000000Z"
plant(pushed)
third = run(hb.run_once(pool, 7, "manual"))
check("a staged copy this Host has no record of writing (a bastion put it back) is never dropped",
      sum(pushed in k for k in staged_now()) == 2 and third["state"] == "ok", staged_now())
# The copy a restore is about to read may be one this Host wrote itself and
# never sent: a restore with platform storage off backs the Host up
# first, and that staged run must not drop the source.
source = third["stamp"]
before = run(hb._run(pool, 7, "before-restore", keep=(source,)))


def outcome(*runs):
    """Each run's own state and error, then the listing (check() keeps the
    first 400 characters). On CI (two runs) one
    run here left its .tar.zst.age without its .key.age, and the listing alone
    could not say why."""
    return [(r.get("stamp"), r.get("state"), r.get("error")) for r in runs] + [staged_now()]


check("the backup a restore takes first never drops the copy being restored (keep)",
      before["state"] == "ok" and sum(source in k for k in staged_now()) == 2, outcome(before))
Store.objects.pop(f"/domain-7/s/{pushed}.tar.zst.age")
Store.objects.pop(f"/domain-7/s/{pushed}.key.age")
pool.state[f"push:{source}"] = json.dumps({"state": "done"})
after = run(hb.run_once(pool, 7, "manual"))
check("a copy a bastion put back for a restore (push:<stamp>) is kept by later runs too",
      sum(source in k for k in staged_now()) == 2, staged_now())
check("the copy of how the Host was before the restore stays until the second copy has it",
      sum(before["stamp"] in k for k in staged_now()) == 2, outcome(before, after))
pool.state.pop(f"push:{source}")
pool.shipped = {r["id"] for r in pool.runs}
last = run(hb.run_once(pool, 7, "manual"))
check("a copy whose second copy is done is left to the platform (a restore may have brought it back)",
      sum(after["stamp"] in k for k in staged_now()) == 2 and last["state"] == "ok",
      outcome(after, last))
pool.shipped = set()
final = run(hb.run_once(pool, 7, "manual"))
check("older copies this Host wrote and never sent go once a newer one exists",
      not any(last["stamp"] in k or after["stamp"] in k for k in staged_now())
      and sum(final["stamp"] in k for k in staged_now()) == 2, staged_now())
for k in staged_now():
    Store.objects.pop(k)

print("== the Git repositories part")
fake_git = tmp / "bin" / "git"
fake_git.parent.mkdir()
fake_git.write_text('#!/bin/sh\nfor a; do last="$a"; done\n'
                    'echo "$@" >> "' + str(tmp / "calls") + '"\n'
                    'case "$*" in *broken*) echo "fatal: repository not found" >&2; exit 128;; esac\n'
                    'mkdir -p "$last" && echo mirror > "$last/HEAD"\n')
fake_git.chmod(fake_git.stat().st_mode | stat.S_IEXEC)
os.environ["PATH"] = f"{fake_git.parent}:{os.environ['PATH']}"
pages = {
    1: [{"full_name": f"entity-7/r{i}", "owner": {"login": "entity-7"}} for i in range(49)]
       + [{"full_name": "owner/shared", "owner": {"login": "owner"}}],
    2: [{"full_name": "entity-7/last", "owner": {"login": "entity-7"}}],
}


async def _gitea_request(method, path, params=None, **_):
    return pages.get(params["page"], [])
gitea_client.gitea_request = _gitea_request
gitea_client.gitea_creds = lambda: ("entity-7", "tok", "git.example")
gitea_client.gitea_proxy = lambda: "http://proxy.example:3128"
work = tmp / "gitwork"
work.mkdir()
folder, part = run(hb.mirror_repos(work))
calls = (tmp / "calls").read_text().splitlines()
check("only the repos the Host OWNS are mirrored, across every page",
      len(calls) == 50 and not any("owner/shared" in c for c in calls), len(calls))
check("and the part says how many", part == {"ok": True, "repos": 50}, part)
pages[2] = [{"full_name": "entity-7/broken", "owner": {"login": "entity-7"}}]
work2 = tmp / "gitwork2"
work2.mkdir()
_, part = run(hb.mirror_repos(work2))
check("a clone that fails fails the part, naming the repository, and the rest are saved",
      not part["ok"] and "entity-7/broken" in part["error"] and part["repos"] == 49, part)
pool.bp = {}
res = run(hb.run_once(pool, 7, "manual"))
got = members(rec_ident, bridge.done[res["object"]])
check("a run with a failed clone is partial, and still carries the mirrors that worked",
      res["state"] == "partial" and any(n.startswith("gitea-repos/entity-7__r0.git/") for n in got), res["state"])

shutil.rmtree(tmp, ignore_errors=True)
srv.shutdown()
print()
if FAILED:
    print(f"FAILED: {len(FAILED)}")
    sys.exit(1)
print("OK: host backup gates pass")
