#!/usr/bin/env python3
"""Gate: the routes a standalone Host's backups are run from (server/routes/backup.py).

  * the owner's own recovery key: only its public half arrives, behind their
    second factor; a private key is refused before the route reads anything;
  * Settings, Backup: where backups go (the network's storage, the owner's
    bucket, or a folder on this machine) and how many the folder keeps;
    moving them, or keeping fewer, is the owner's act with their code; the
    bucket's keys go into the vault, never into the settings;
  * the files on this machine: listed, downloaded as one file, uploaded back
    in chunks (a route that streams, never reading the body whole), restored
    behind the second factor, and an export made on request;
  * every one of them walled from the Host: who backups go to, and what
    leaves or replaces the Host, are the owner's acts.

Stubs for the pool, the vault and the step-up; real files in a temporary
folder. Exit 0 = pass.

    python3 scripts/test_backup_files_routes.py
"""
import asyncio
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "brain"))

from fastapi import HTTPException  # noqa: E402

FAILED = []


def check(label, ok, detail=""):
    print(("  ok    " if ok else "  FAIL  ") + label + ("" if ok else f" — {str(detail)[:300]}"))
    if not ok:
        FAILED.append(label)


def refuses(label, coro, *, status, contains=""):
    try:
        asyncio.run(coro)
    except HTTPException as e:
        check(label, e.status_code == status and contains in str(e.detail),
              f"got {e.status_code}: {str(e.detail)[:200]}")
        return
    except Exception as e:  # noqa: BLE001
        check(label, False, f"raised {type(e).__name__}: {e}")
        return
    check(label, False, "it was ACCEPTED")


from server import age_keys, backup_key, brain_service, backup_stores  # noqa: E402
from server import host_backup as hb  # noqa: E402
from server import host_restore as hr  # noqa: E402
from server.bridge.no_platform import NoPlatform  # noqa: E402
from server.routes import backup as R  # noqa: E402

brain_service._entity_id = 1
tmp = Path(tempfile.mkdtemp(prefix="bfr-test-"))
hb.LOCAL_DIR = tmp / "backups"
hb._raw_bridge = lambda: NoPlatform()
hb._bridge = lambda: NoPlatform()
# The names a bucket's address is checked by (backup_stores.check_endpoint):
# invented, and answered here, so nothing is looked up and nothing is reached.
ADDRS = {"s3.example.test": ["34.120.0.10"], "inside.example.test": ["10.20.30.40"],
         "meta.example.test": ["169.254.169.254"], "mixed.example.test": ["34.120.0.11", "fd00::5"]}


def _resolve(host, port):
    if host not in ADDRS:
        raise OSError("no such host")
    return list(ADDRS[host])
backup_stores._resolve = _resolve


class Pool:
    def __init__(self):
        self.bp = {}
        self.vault = {}          # name -> cred_type of the active key of that name
        self.runs = []           # host_backup_runs rows, newest first

    async def fetchrow(self, sql, *a, **k):
        if "host_backup_runs" in sql:
            states = ("'ok', 'partial', 'failed'" in sql and ("ok", "partial", "failed")) or ("ok", "partial")
            return next((r for r in self.runs if r["state"] in states), None)
        return {"behavioral_params": json.dumps(self.bp)}

    async def fetchval(self, sql, *a):
        if "behavioral_params" in sql:
            return json.dumps(self.bp)
        if "FROM vault_envelope" in sql:
            return self.vault.get(a[1])
        return None

    async def execute(self, sql, *a):
        if "jsonb_set" in sql and "'{backup}'" in sql:
            self.bp = dict(self.bp, backup=json.loads(a[0]))

    async def fetch(self, sql, *a):
        return []

    def transaction(self):
        class T:
            async def __aenter__(self):
                return None

            async def __aexit__(self, *a):
                return False
        return T()

    def acquire(self):
        conn = self

        class A:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False
        return A()


pool = Pool()
R.brain_service.get_pool = lambda: pool


async def _pin(p, eid):
    return {"pinned": None, "offer": None}


async def _no_keys(*a, **k):
    return []
hb.pin_recovery_key = _pin
backup_key.list_all = _no_keys
backup_key.list_active = _no_keys
codes = {"spent": []}


async def enrolled(_p):
    return True


async def verify(_p, code):
    codes["spent"].append(code)
    return code == "123456"
R.nac_stepup.is_enrolled = enrolled
R.nac_stepup.verify_stepup = verify


class AppReq:
    """What a route sees of the request: the app token's app id, and a body."""

    def __init__(self, body=None, app_id="settings", chunks=None, origin_key="", host="localhost"):
        self._body = json.dumps(body).encode() if body is not None else b""
        self._chunks = chunks
        self.state = type("S", (), {"app_token": type("T", (), {"app_id": app_id,
                                                                  "origin_key": origin_key})()})()
        self.headers = {"host": host}
        self.read_whole = False

    async def json(self):
        return json.loads(self._body)

    async def body(self):
        self.read_whole = True
        return self._body

    async def stream(self):
        for c in self._chunks or []:
            yield c


rec_ident, rec_rec = age_keys.new_pair()
REC_FP = backup_key.fingerprint(age_keys.recipient_bytes(rec_rec))

print("== the owner's own recovery key")
made = []


async def _make(p, eid, recipient, fingerprint, label="", replace=False):
    made.append((recipient, fingerprint, label, replace))
    if not recipient.startswith("age1"):
        raise hb.BackupError("that is not a recovery key's public half (age1...)")
    return {"fingerprint": fingerprint, "first": True, "replaced": None}
hb.make_own_recovery_key = _make
refuses("a wrong code changes nothing",
        R.own_recovery_key(R.OwnRecoveryReq(stepup_code="000000", recipient=rec_rec, fingerprint=REC_FP)),
        status=403)
check("  and the key was not touched", made == [])
refuses("what is not a public key is refused, saying so",
        R.own_recovery_key(R.OwnRecoveryReq(stepup_code="123456", recipient="not-a-key", fingerprint=REC_FP)),
        status=409, contains="age1")
got = asyncio.run(R.own_recovery_key(R.OwnRecoveryReq(stepup_code="123456", recipient=rec_rec,
                                                      fingerprint=REC_FP, label="mine", replace=True)))
check("the public half, its fingerprint, label and the owner's replace reach the Host",
      made[-1] == (rec_rec, REC_FP, "mine", True) and got["fingerprint"] == REC_FP and got["note"], got)


class RawReq:
    def __init__(self, body):
        self._b = body

    async def body(self):
        return self._b
refuses("a body with the PRIVATE half is refused before any route reads it",
        R._no_private_keys(RawReq(json.dumps({"recipient": rec_ident}).encode())), status=400,
        contains="private key")

print("== Settings, Backup: where backups go")
pool.bp = {}
s = asyncio.run(R.get_backup_settings())
check("a standalone Host that chose nothing: a folder on this machine, a week of backups",
      s["where"] == "local" and s["where_chosen"] is False and s["mode"] == "standalone"
      and s["local_keep"] == 7, {k: s.get(k) for k in ("where", "where_chosen", "mode", "local_keep")})
refuses("a place that is not one of the three is refused, naming it",
        R.set_backup_settings(AppReq({"where": "nfs"})), status=400, contains="where")
refuses("the owner's bucket with no bucket saved is refused",
        R.set_backup_settings(AppReq({"where": "s3", "stepup_code": "123456"})), status=400, contains="bucket")
refuses("a count the folder cannot keep is refused",
        R.set_backup_settings(AppReq({"where": "local", "local_keep": 0})), status=400, contains="local_keep")
out = asyncio.run(R.set_backup_settings(AppReq({"where": "local", "local_keep": 14})))
check("keeping more, in the place it already goes, needs no code",
      out["where"] == "local" and out["local_keep"] == 14 and pool.bp["backup"]["local_keep"] == 14, out)
refuses("keeping fewer is the owner's act: their code",
        R.set_backup_settings(AppReq({"where": "local", "local_keep": 3})), status=428, contains="fewer")
refuses("and never the Host's, whatever it says",
        R.set_backup_settings(AppReq({"where": "local", "local_keep": 3, "stepup_code": "123456"}, app_id="host")),
        status=403)
target = {"kind": "s3", "address": "my-backups", "cred_name": "backup-storage", "endpoint": "https://s3.example.test"}
refuses("sending backups somewhere else is the owner's act: their code",
        R.set_backup_settings(AppReq({"where": "s3", "target": target})), status=428, contains="where your backups go")
out = asyncio.run(R.set_backup_settings(AppReq({"where": "s3", "target": target, "stepup_code": "123456"})))
check("with it, backups go to the owner's bucket", out["where"] == "s3" and out["where_chosen"]
      and pool.bp["backup"]["where"] == "s3" and pool.bp["backup"]["target"]["address"] == "my-backups", out)
refuses("the network, on a Host not on one, is refused: nothing would be written",
        R.set_backup_settings(AppReq({"where": "network", "stepup_code": "123456"})), status=400,
        contains="not on the network")
for label, endpoint, why in (
        ("a name on a private network", "https://inside.example.test", "private network"),
        ("the cloud's metadata service, by name", "https://meta.example.test", "169.254.169.254"),
        ("the metadata service's address itself", "https://169.254.169.254", "private network"),
        ("this machine (loopback)", "https://127.0.0.1:9000", "private network"),
        ("loopback inside an IPv6 address", "https://[::ffff:127.0.0.1]", "private network"),
        ("a cluster name", "https://minio.storage.svc.cluster.local", "not the internet"),
        ("a name that answers one public and one private address", "https://mixed.example.test",
         "private network"),
        ("a name that cannot be found", "https://gone.example.test", "could not be found")):
    refuses(f"the bucket may not be at {label}, even with the owner's code",
            R.set_backup_settings(AppReq({"where": "s3", "stepup_code": "123456",
                                          "target": dict(target, endpoint=endpoint)})),
            status=400, contains=why)
check("  and those refusals changed nothing",
      pool.bp["backup"]["target"]["endpoint"] == "https://s3.example.test", pool.bp.get("backup"))
del ADDRS["s3.example.test"]
out = asyncio.run(R.set_backup_settings(AppReq({"where": "s3", "target": target, "hour": 5})))
check("a saved bucket is not looked up again when only the time changes (its name may be down a while)",
      out["hour"] == 5, out)
ADDRS["s3.example.test"] = ["34.120.0.10"]
refuses("a second copy for backups kept on this machine is refused: nothing would send it",
        R.set_backup_settings(AppReq({"where": "local", "target": {"kind": "machine", "address": "nas"},
                                      "stepup_code": "123456"})), status=400, contains="not sent anywhere")
check("and the refusal changed nothing", pool.bp["backup"]["where"] == "s3", pool.bp.get("backup"))

print("== the bucket's keys go into the vault")
stored = []


async def _store_secret(p, *, entity_id, name, cred_type, plaintext, **kw):
    stored.append((name, cred_type, json.loads(plaintext)))
    return "env-1"
from server.vault import api as vault_api  # noqa: E402
vault_api.store_secret = _store_secret
refuses("both halves of the key are needed",
        R.save_storage_key(R.StorageKeyReq(access_key_id="AKEXAMPLE", secret_access_key=""), AppReq()),
        status=400)
got = asyncio.run(R.save_storage_key(R.StorageKeyReq(access_key_id="AKEXAMPLE",
                                                     secret_access_key="example-secret"), AppReq()))
check("they are sealed in the vault under a name the settings then hold",
      stored == [("backup-storage", "s3_credentials",
                  {"access_key_id": "AKEXAMPLE", "secret_access_key": "example-secret"})]
      and got == {"ok": True, "cred_name": "backup-storage"}, (stored, got))
check("  and the answer never carries the secret back", "example-secret" not in json.dumps(got))
pool.vault = {"example-model-key": "api_key", "backup-storage": "s3_credentials"}
refuses("a name that holds another of the owner's keys is refused: the vault would replace that key",
        R.save_storage_key(R.StorageKeyReq(access_key_id="AKEXAMPLE", secret_access_key="example-secret",
                                           name="example-model-key"), AppReq()),
        status=409, contains="not a bucket key")
check("  and nothing was stored over it", len(stored) == 1, stored)
got = asyncio.run(R.save_storage_key(R.StorageKeyReq(access_key_id="AKEXAMPLE2", secret_access_key="example-secret2"),
                                     AppReq()))
check("a new key for the bucket replaces the bucket's key", got["cred_name"] == "backup-storage"
      and stored[-1][0] == "backup-storage" and stored[-1][2]["access_key_id"] == "AKEXAMPLE2", stored)
for app in ("backups", "host", "some-app"):
    refuses(f"only Settings saves it, not {app!r}",
            R.save_storage_key(R.StorageKeyReq(access_key_id="AKEXAMPLE", secret_access_key="example-secret"),
                               AppReq(app_id=app)), status=403, contains="Settings")
check("  and those changed nothing", len(stored) == 2, stored)
pool.vault = {}

print("== the files on this machine")
S1, S2 = "20261001T020000Z", "20261002T020000Z"
store = backup_stores.LocalStore(hb.LOCAL_DIR)
for st in (S1, S2):
    u = store.uploader(st)
    u.part(st.encode() * 100)
    u.complete()
    store.put_sidecar(st, b"-----BEGIN AGE ENCRYPTED FILE-----\nk\n")


async def _recipient(p):
    return age_keys.new_pair()[1]


async def _no_recips(p, eid, bp=None):
    return []
hr.host_recipient = _recipient
_real_recipients, _real_configured = hb.recipients, hb.configured
hb.recipients, hb.configured = _no_recips, (lambda: None)
import datetime as _dt  # noqa: E402
_done = _dt.datetime(2026, 10, 2, 2, 3, 4, tzinfo=_dt.timezone.utc)
pool.runs = [{"state": "ok", "finished_at": _done, "error": None}]
lst = asyncio.run(R.list_backup_files())
check("listed newest first, with their sizes", [b["stamp"] for b in lst["backups"]] == [S2, S1]
      and lst["backups"][0]["bytes"] == 1600 and lst["host_recipient"].startswith("age1")
      and lst["entity_id"] == 1, lst)
check("a Host with backups behind it and no recovery key now (restored onto a new machine) says plainly "
      "that backups are paused, and when the last one finished",
      lst["status"]["state"] == "paused" and lst["status"]["headline"] == "Backups are paused: add your recovery key"
      and lst["status"]["last_finished_at"] == _done.isoformat(), lst["status"])
hb.recipients, hb.configured = _real_recipients, _real_configured
pool.runs = []
resp = asyncio.run(R.download_backup_file(S2))


async def drain(resp):
    out = b""
    async for c in resp.body_iterator:
        out += c if isinstance(c, bytes) else c.encode()
    return out
body = asyncio.run(drain(resp))
with tarfile.open(fileobj=io.BytesIO(body), mode="r:") as t:
    names = t.getnames()
check("downloaded as one file: README, key file, backup",
      names == ["README.txt", f"{S2}.key.age", f"{S2}.tar.zst.age"]
      and resp.headers["content-disposition"] == f'attachment; filename="cseries-backup-{S2}.tar"'
      and int(resp.headers["content-length"]) == len(body), (names, dict(resp.headers)))
refuses("a stamp that is not one is refused", R.download_backup_file("../../etc"), status=400)
refuses("a backup that is not there is a 404", R.download_backup_file("20260101T000000Z"), status=404)

print("== a download the browser writes to disk itself (a one-time link)")


def members(raw):
    """Each member's name and bytes (a tar header carries the time it was made)."""
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as t:
        return [(m.name, t.extractfile(m).read()) for m in t]
link = asyncio.run(R.link_backup_file(S2, AppReq()))
ticket = link["url"].rsplit("/", 1)[-1]
check("the link names one download of one file", link["url"].startswith("/api/backup/files/download/dl_")
      and link["name"] == f"cseries-backup-{S2}.tar" and link["expires_in"] == 60, link)
resp = asyncio.run(R.download_backup_link(ticket, AppReq()))
check("followed, it is the same file, as an attachment the browser saves as it arrives",
      members(asyncio.run(drain(resp))) == members(body)
      and resp.headers["content-disposition"] == f'attachment; filename="cseries-backup-{S2}.tar"'
      and resp.headers["cache-control"] == "no-store", dict(resp.headers))
refuses("a link is good once", R.download_backup_link(ticket, AppReq()), status=404, contains="used or has expired")
refuses("a made-up link opens nothing", R.download_backup_link("dl_" + "A" * 32, AppReq()), status=404)
refuses("a stamp in the link's place opens nothing", R.download_backup_link(S2, AppReq()), status=404)
from server.applications import app_origin as AO  # noqa: E402
from server.host_names import HOST_SUFFIXES  # noqa: E402
bk_key, zone = AO.origin_key("backups"), HOST_SUFFIXES[0]
bk_host, desk_host = f"apps-{bk_key}-0a1b2c3d4e5f6a7b{zone}", f"0a1b2c3d4e5f6a7b{zone}"
link = asyncio.run(R.link_backup_file(S2, AppReq(app_id="backups", origin_key=bk_key, host=bk_host)))
refuses("a link the Backups app asked for opens nothing on the desktop's origin",
        R.download_backup_link(link["url"].rsplit("/", 1)[-1], AppReq(host=desk_host)), status=404)
other = f"apps-{AO.origin_key('some-app')}-0a1b2c3d4e5f6a7b{zone}"
link = asyncio.run(R.link_backup_file(S2, AppReq(app_id="backups", origin_key=bk_key, host=bk_host)))
refuses("  nor on another app's origin",
        R.download_backup_link(link["url"].rsplit("/", 1)[-1], AppReq(host=other)), status=404)
link = asyncio.run(R.link_backup_file(S2, AppReq(app_id="backups", origin_key=bk_key, host=bk_host)))
resp = asyncio.run(R.download_backup_link(link["url"].rsplit("/", 1)[-1], AppReq(host=bk_host)))
check("  and downloads on its own", members(asyncio.run(drain(resp))) == members(body))
_ttl = R._LINK_TTL_S
R._LINK_TTL_S = -1.0
link = asyncio.run(R.link_backup_file(S2, AppReq()))
R._LINK_TTL_S = _ttl
refuses("a link past its minute opens nothing", R.download_backup_link(link["url"].rsplit("/", 1)[-1], AppReq()),
        status=404, contains="expired")
refuses("no link is made for a backup that is not there", R.link_backup_file("20260101T000000Z", AppReq()),
        status=404)
refuses("nor for a stamp that is not one", R.link_backup_file("../../etc", AppReq()), status=400)
_rt = next(r for r in R.router.routes if getattr(r, "path", "") == "/api/backup/files/download/{ticket}")
_mint = next(r for r in R.router.routes if getattr(r, "path", "") == "/api/backup/files/{stamp}/link")
from server.applications import route_policy as RP  # noqa: E402
check("the link is made only for a window holding read:backup_settings",
      any(getattr(d.call, "_app_scope", None) == "read:backup_settings" for d in _mint.dependant.dependencies))
check("the download itself is listed as authenticated by its one-shot link (route_policy own-auth)",
      (RP.UNSCOPED_ROUTES.get(("GET", "/api/backup/files/download/{ticket}")) or ("",))[0] == "own-auth"
      and not any(getattr(d.call, "_app_scope", None) for d in _rt.dependant.dependencies))

print("== the link under APP_TOKEN_MODE=strict (the token middleware in front of the routes)")
# A followed link is a navigation, which carries no window token, so strict
# mode must let the link's own route through (server/applications/middleware.py
# _ALLOWLIST_PREFIXES) and still refuse every other backup route a token.
from fastapi import FastAPI  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402
from server.applications import middleware as MW  # noqa: E402
_mode = MW._MODE
MW._MODE = "strict"
try:
    _app = FastAPI()
    _app.add_middleware(MW.ApplicationTokenMiddleware)
    _app.include_router(R.router)
    _app.state.kernel_secret = b"k" * 32
    with TestClient(_app, base_url="http://localhost") as _c:
        link = asyncio.run(R.link_backup_file(S2, AppReq()))
        _r = _c.get(link["url"])
        check("strict mode: the one-time link downloads with no token",
              _r.status_code == 200 and members(_r.content) == members(body), (_r.status_code, _r.text[:200]))
        _r = _c.get("/api/backup/files/download/dl_" + "A" * 32)
        check("  a made-up link reaches the route and is its 404, not the token's 401",
              _r.status_code == 404 and "used or has expired" in _r.text, (_r.status_code, _r.text[:200]))
        # The last is under the link's prefix, and is the stamp route's (it is
        # matched first), whose own scope check still refuses it.
        for _path in (f"/api/backup/files/{S2}/download", "/api/backup/files", "/api/backup/files/download",
                      "/api/backup/files/download/download"):
            _r = _c.get(_path)
            check(f"  {_path} still needs its token", _r.status_code == 401
                  and "application token required" in _r.text, (_r.status_code, _r.text[:200]))
        _r = _c.post(f"/api/backup/files/{S2}/link")
        check("  and so does asking for a link", _r.status_code == 401, (_r.status_code, _r.text[:200]))
finally:
    MW._MODE = _mode

print("== a file uploaded back")
check("the upload route does not read the body whole (no private-key pre-read on its router)",
      not any(getattr(d, "dependency", None) is R._no_private_keys for d in R.upload_router.dependencies))
S3 = "20261003T020000Z"
side = b"-----BEGIN AGE ENCRYPTED FILE-----\nYWdl\n-----END AGE ENCRYPTED FILE-----\n"
req = AppReq(chunks=[side])
got = asyncio.run(R.upload_backup_file(req, stamp=S3, part="key", offset=0, total=len(side), final=True))
check("the key file arrives", got["key_file"] and not req.read_whole, got)
data = os.urandom(5000)
req = AppReq(chunks=[data[:3000], data[3000:]])
got = asyncio.run(R.upload_backup_file(req, stamp=S3, part="data", offset=0, total=len(data), final=True))
check("the backup arrives, streamed", got["done"] and got["size"] == 5000 and not req.read_whole, got)
refuses("a private key in an upload is refused, saying why",
        R.upload_backup_file(AppReq(chunks=[b"AGE-SECRET-KEY-1" + b"Q" * 58]), stamp="20261004T020000Z",
                             part="data", offset=0, total=74, final=True),
        status=400, contains="private key")
st = asyncio.run(R.upload_backup_status(S3))
check("the Host says how much of an upload it holds", st == {"stamp": S3, "key_file": True, "size": 5000, "done": True}, st)

print("== restoring an uploaded file")
staged = []
held = {}


async def _accept(p, stamp, armored, via):
    held["via"] = via
    return {"key_ready": True, "stamp": stamp, "verified": False}


async def _stage(p, eid, stamp, unverified_ok=False, where=None):
    staged.append((stamp, unverified_ok, where))
    return {"state": "staged"}
hr.accept_resealed = _accept
hr.stage = _stage
hr.read_plan = lambda: None
resealed = "-----BEGIN AGE ENCRYPTED FILE-----\nYWdl\n-----END AGE ENCRYPTED FILE-----"
refuses("restoring takes the owner's code",
        R.restore_backup_file(R.FileRestoreReq(stepup_code="000000", stamp=S3, sidecar=resealed)), status=403)
refuses("a backup this Host did not make is restored only when the owner says so",
        R.restore_backup_file(R.FileRestoreReq(stepup_code="123456", stamp=S3, sidecar=resealed)),
        status=409, contains="cannot confirm")
check("  and nothing was staged", staged == [])
got = asyncio.run(R.restore_backup_file(R.FileRestoreReq(stepup_code="123456", stamp=S3, sidecar=resealed,
                                                         unverified_ok=True)))
asyncio.run(asyncio.sleep(0))


async def settle():
    await asyncio.sleep(0.05)
asyncio.run(settle())
check("with it, the key sealed in the browser is taken and the restore starts",
      got["started"] and held["via"] == "file", (got, held, staged))
check("  from the file on this machine, never another backup sharing its stamp in the network's storage",
      staged == [(S3, True, "local")], staged)
refuses("a stamp not on this machine is refused",
        R.restore_backup_file(R.FileRestoreReq(stepup_code="123456", stamp="20250101T000000Z", sidecar=resealed,
                                               unverified_ok=True)), status=409)

print("== the runs say why there is no backup")
pool.bp = {"backup": {"where": "network"}}


async def _recips(p, eid, bp=None):
    return [{"kind": "host-recovery"}]


async def _pin(p, eid):
    return {"pinned": None, "offer": None}
hb.recipients = _recips
hb.pin_recovery_key = _pin
hb.configured = lambda: None
runs = asyncio.run(R.get_backup_runs())
check("told to use the network with none: the runs say so", runs["problem"] == hb.NO_DESTINATION_TEXT, runs)
check("  and the verdict at the top of Settings, Backup says it in one line",
      runs["status"]["state"] == "no_destination" and "choose where" in runs["status"]["headline"], runs["status"])
pool.bp = {"backup": {"where": "local"}}
pool.runs = [{"state": "ok", "finished_at": _done, "error": None}]
runs = asyncio.run(R.get_backup_runs())
check("a Host whose newest backup worked says backups are working, and when the last one finished",
      runs["status"]["state"] == "working" and runs["status"]["headline"] == "Backups are working"
      and runs["status"]["last_finished_at"] == _done.isoformat(), runs["status"])
pool.runs = [{"state": "failed", "finished_at": _done, "error": "the disk was full"},
             {"state": "ok", "finished_at": _done - _dt.timedelta(days=1), "error": None}]
runs = asyncio.run(R.get_backup_runs())
check("one whose newest backup failed says so, with why, and when the last good one finished",
      runs["status"]["state"] == "failing" and "the disk was full" in runs["status"]["detail"]
      and runs["status"]["last_finished_at"] == (_done - _dt.timedelta(days=1)).isoformat(), runs["status"])
pool.runs = []
runs = asyncio.run(R.get_backup_runs())
check("one that has none yet says that", runs["status"]["state"] == "none_yet", runs["status"])
pool.bp = {"backup": {"where": "local", "frequency": "off"}}
runs = asyncio.run(R.get_backup_runs())
check("one with backups turned off says that", runs["status"]["state"] == "off", runs["status"])
pool.bp = {"backup": {"where": "network"}}
refuses("and Back up now says so too", R.start_backup_run(), status=409, contains="not on the network")

print("== a network member whose network does not keep backups yet")


class Door:
    """The member door's client. A door without the backup ops answers each
    of them as it answers any op it does not have."""
    network_member = True
    _connected = True

    def __init__(self, listed):
        self.listed = listed

    async def request_full(self, op, params=None):
        if op == "backup.list":
            return dict(self.listed)
        return {"ok": False, "error": f"Unknown operation: {op}", "code": 400}

    async def request(self, op, params=None, **_):
        return {}


def on_door(listed):
    door = Door(listed)
    hb._network_answer.update(client=None, at=None, offers=None)
    hb._raw_bridge = lambda: door
    hb._bridge = lambda: door
    return door


on_door({"ok": False, "error": "Unknown operation: backup.list", "code": 400})
pool.bp = {}
s = asyncio.run(R.get_backup_settings())
check("a member that chose nothing: a folder on this machine, and Settings is told the network keeps none",
      s["mode"] == "member" and s["where"] == "local" and s["where_chosen"] is False
      and s["network_backups"] is False,
      {k: s.get(k) for k in ("mode", "where", "where_chosen", "network_backups")})
refuses("choosing the network's storage is refused: nothing would be written there",
        R.set_backup_settings(AppReq({"where": "network", "stepup_code": "123456"})), status=400,
        contains="does not keep backups yet")
out = asyncio.run(R.set_backup_settings(AppReq({"where": "local", "hour": 4})))
check("saving the folder it already uses needs no code", out["where"] == "local" and out["where_chosen"], out)
pool.bp = {"backup": {"where": "network"}}
runs = asyncio.run(R.get_backup_runs())
check("one that chose the network's storage: the runs say it keeps no backups yet",
      runs["problem"] == hb.NO_NETWORK_STORAGE_TEXT, runs)
check("  and so does the verdict at the top of Settings, Backup",
      runs["status"]["state"] == "no_destination" and "does not keep backups yet" in runs["status"]["detail"],
      runs["status"])
refuses("  and Back up now", R.start_backup_run(), status=409, contains="does not keep backups yet")
on_door({"ok": True, "data": {"objects": []}})
pool.bp = {}
s = asyncio.run(R.get_backup_settings())
check("a member whose network keeps backups: the network's storage, as before",
      s["where"] == "network" and s["network_backups"] is True, {k: s.get(k) for k in ("where", "network_backups")})
hb._network_answer.update(client=None, at=None, offers=None)
hb._raw_bridge = lambda: NoPlatform()
hb._bridge = lambda: NoPlatform()

print("== an export")
exported = []


async def _export(p, eid):
    exported.append(eid)
    return {"state": "ok", "stamp": S2, "size": 1}
hb.export_to_file = _export
got = asyncio.run(R.start_export())
asyncio.run(settle())
check("an export starts and answers at once", got["started"] is True, got)

print("== walled from the Host")
from mastercode.runtime import kernel_ops as KO  # noqa: E402
for method, path in (("POST", "/api/backup/key/recovery/own"), ("POST", "/api/backup/storage-key"),
                     ("GET", "/api/backup/files"), ("GET", "/api/backup/files/{stamp}/download"),
                     ("POST", "/api/backup/files/{stamp}/link"), ("GET", "/api/backup/files/download/{ticket}"),
                     ("PUT", "/api/backup/files/upload"), ("GET", "/api/backup/files/upload"),
                     ("POST", "/api/backup/files/restore"), ("POST", "/api/backup/export")):
    check(f"{method} {path} is walled, with a reason that says whose act it is",
          path in KO._WALLED_PATHS and KO.exposed_route(method, path) is None
          and "owner" in (KO._WALL_REASONS.get(path) or ""), path)

from server.network import nac_stepup as NS  # noqa: E402
check("the Backups app asks the owner for their code, so its codes count as the owner's, like Settings'",
      {"backups", "settings"} <= NS.OWNER_UI_APP_IDS)

shutil.rmtree(tmp, ignore_errors=True)
print()
print("FAILED: " + ", ".join(FAILED) if FAILED else "all ok")
sys.exit(1 if FAILED else 0)
