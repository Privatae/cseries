#!/usr/bin/env python3
"""Gate: shipping a backup to the user's own destination, end to end.

Two halves, both real:

  * the CONTRACT — what a saved destination may and may not say. Wrong here and
    a user believes a second copy exists that nothing is writing.
  * the TRANSFER — a real multipart upload of a real multi-part artifact into a
    real S3 server, verified by asking the destination what it has, plus every
    failure class the tab has to explain.

THE S3 SERVER IS HAND-ROLLED, and that is a deliberate cost. This box has no
minio, no moto, no s3rver and no S3 image cached (checked), so the
alternatives were a network pull or no end-to-end test at all. The fixture below
implements the five verbs boto3 uses for a streamed multipart upload and nothing
else: PUT object, POST ?uploads, PUT part, POST complete, DELETE ?uploadId, HEAD.
It does not verify signatures — it is a destination, not an authenticator — and
the auth-failure cases drive it to return the real error shapes instead.

No pod, no bridge, no network beyond loopback. Exit 0 = pass.
"""
from __future__ import annotations

import asyncio
import hashlib
import http.server
import os
import re
import shutil
import socket
import sys
import threading
import urllib.parse
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from server import backup_ship as BS  # noqa: E402
from gate_loopback import ClientClosesFirst  # noqa: E402

# ── keep the gate's waits to what its assertions need ───────────────────────
# This gate was 21s of a 108s pre-merge run. Profiled by section: 9s was the
# bastion path's first status poll (a 2s sleep before asking a fake bastion
# that answers instantly, paid by every machine-kind scenario), 2.9s was
# boto3 backing off between three attempts against a fixture that returns
# the same 500 every time, and 5.3s was the heartbeat section's real upload
# and reap windows (halved below, margins kept). Nothing asserted here is
# about how LONG those waits are; they are about what happens, and what
# happens is identical at 10ms.
BS.MACHINE_POLL_FIRST_S = 0.01
import botocore.config as _bc  # noqa: E402
_RealConfig = _bc.Config


def _single_attempt_config(*a, **kw):
    # _s3_client does `from botocore.config import Config` at call time, so
    # this is what it gets. The production value (3 attempts) is not under
    # test anywhere in this file; the REASON a refusal maps to is.
    return _RealConfig(*a, **{**kw, "retries": {"max_attempts": 1, "mode": "standard"}})


_bc.Config = _single_attempt_config

FAILED = 0

# Every temp directory this gate makes. It used to leave each one behind: 46
# backup-ship-test-* directories had piled up in /tmp, a 1M-inode tmpfs on
# one development machine. main() removes them on every path; the
# entry point checks
# from the filesystem that none is left.
_OWN_TMP: list[Path] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    global FAILED
    if ok:
        print(f"  ok    {label}")
    else:
        FAILED = 1
        print(f"  FAIL  {label}" + (f" — {detail}" if detail else ""))


# ── a minimal S3 destination ─────────────────────────────────────────────────

class S3State:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.uploads: dict[str, dict[int, bytes]] = {}
        self.aborted: list[str] = []
        self.fail_with: int | None = None      # force a status on writes
        self.fail_on_complete = False
        self.pose_as_kms = False
        self.saw_content_md5 = False
        self.part_delay = 0.0     # simulate a slow destination
        self.next_upload = 1


STATE = S3State()


class S3Handler(ClientClosesFirst, http.server.BaseHTTPRequestHandler):
    # ClientClosesFirst: the source fetch is urllib, which asks this server to
    # close after every response; closing first left a TIME_WAIT on this port
    # per fetch, and CI's firewall refuses a later fetch that lands on one
    # (scripts/gate_loopback.py).
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # quiet
        pass

    def _send(self, code: int, body: bytes = b"", ctype="application/xml"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _deny(self):
        self._send(STATE.fail_with, (
            b'<?xml version="1.0"?><Error><Code>AccessDenied</Code>'
            b"<Message>the bucket policy said no</Message></Error>"))

    def _key(self):
        return urllib.parse.urlparse(self.path).path.lstrip("/").split("/", 1)[-1]

    def _q(self):
        # keep_blank_values: S3 signals "start a multipart upload" with a bare
        # `?uploads` flag that has no value, and parse_qs drops those by
        # default — which made every multipart upload 400 until it was caught
        # here rather than in production.
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query,
                                     keep_blank_values=True)

    def do_PUT(self):
        if STATE.fail_with:
            self._deny(); return
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        # Content-MD5 is the whole point of sending it: a real endpoint VERIFIES
        # it and rejects the part. The fixture does the same, so the test proves
        # the header is both present and correct rather than merely present.
        cmd5 = self.headers.get("Content-MD5")
        if cmd5 is not None:
            import base64 as _b64
            if cmd5 != _b64.b64encode(hashlib.md5(body).digest()).decode():
                self._send(400, b'<?xml version="1.0"?><Error>'
                                b"<Code>BadDigest</Code></Error>")
                return
            STATE.saw_content_md5 = True
        q = self._q()
        if "uploadId" in q:                      # a part
            if STATE.part_delay:
                import time as _t
                _t.sleep(STATE.part_delay)
            uid = q["uploadId"][0]
            STATE.uploads.setdefault(uid, {})[int(q["partNumber"][0])] = body
            self.send_response(200)
            if STATE.pose_as_kms:
                # An SSE-KMS ETag is 32 hex characters and is NOT an MD5.
                self.send_header("ETag", '"%s"' % ("a" * 32))
                self.send_header("x-amz-server-side-encryption", "aws:kms")
                self.send_header("x-amz-server-side-encryption-aws-kms-key-id",
                                 "arn:aws:kms:us-east-1:1:key/abc")
            else:
                self.send_header("ETag", '"%s"' % hashlib.md5(body).hexdigest())
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        STATE.objects[self._key()] = body        # single PUT
        self._send(200)

    def do_POST(self):
        if STATE.fail_with:
            self._deny(); return
        q = self._q()
        key = self._key()
        if "uploads" in q:
            uid = f"u{STATE.next_upload}"
            STATE.next_upload += 1
            STATE.uploads[uid] = {}
            self._send(200, (
                f'<?xml version="1.0"?><InitiateMultipartUploadResult>'
                f"<Bucket>b</Bucket><Key>{key}</Key><UploadId>{uid}</UploadId>"
                f"</InitiateMultipartUploadResult>").encode())
            return
        if "uploadId" in q:                      # complete
            uid = q["uploadId"][0]
            if STATE.fail_on_complete:
                n = int(self.headers.get("Content-Length", 0))
                self.rfile.read(n)
                self._send(500, b'<?xml version="1.0"?><Error><Code>InternalError</Code>'
                                b"<Message>complete failed</Message></Error>")
                return
            n = int(self.headers.get("Content-Length", 0))
            self.rfile.read(n)
            parts = STATE.uploads.pop(uid, {})
            STATE.objects[key] = b"".join(parts[i] for i in sorted(parts))
            self._send(200, (
                b'<?xml version="1.0"?><CompleteMultipartUploadResult>'
                b"<ETag>&quot;done&quot;</ETag></CompleteMultipartUploadResult>"))
            return
        self._send(400)

    def do_DELETE(self):
        q = self._q()
        if "uploadId" in q:
            uid = q["uploadId"][0]
            STATE.uploads.pop(uid, None)
            STATE.aborted.append(uid)
        self._send(204)

    def do_HEAD(self):
        key = self._key()
        if key not in STATE.objects:
            self._send(404); return
        self.send_response(200)
        self.send_header("Content-Length", str(len(STATE.objects[key])))
        self.end_headers()

    def do_GET(self):
        # Serves the SOURCE artifact too, so the whole pipe is exercised.
        key = self._key()
        if key in STATE.objects:
            body = STATE.objects[key]
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._send(404)


def self_signed(dirpath: Path) -> tuple[str, str]:
    """A throwaway TLS cert for 127.0.0.1.

    The fixture has to speak HTTPS because validate_target refuses a plaintext
    endpoint — the artifact is already encrypted but the CREDENTIAL is not, and
    that rule is worth more than the convenience of an http test server. So the
    test trusts one cert for one loopback port instead of the production code
    growing a loopback exception."""
    import datetime as _dt

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = _dt.datetime.now(_dt.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - _dt.timedelta(minutes=5))
            .not_valid_after(now + _dt.timedelta(hours=1))
            .add_extension(x509.SubjectAlternativeName(
                [x509.IPAddress(__import__("ipaddress").ip_address("127.0.0.1")),
                 x509.DNSName("localhost")]), critical=False)
            .sign(key, hashes.SHA256()))
    cpath = dirpath / "fixture.crt"
    kpath = dirpath / "fixture.key"
    cpath.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    kpath.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    return str(cpath), str(kpath)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ── a pool stand-in ──────────────────────────────────────────────────────────

class Pool:
    """Just enough asyncpg surface for the ship engine's own bookkeeping."""

    def __init__(self, source: dict | None):
        self.source = source
        self.undo: dict | None = None   # a before-restore run not yet sent
        self.rows: list[dict] = []
        self.running = False
        self.stale = False       # a running row the reaper should take
        self.done_keys: set = set()
        self.history: list = []  # prior attempts, newest first (for backoff)
        self.beats = 0
        self.started_at = None
        self.last_beat = None
        self.reaped = 0

    def __post(self):
        pass

    async def execute(self, sql, *a):
        u = " ".join(sql.split()).upper()
        # the stale-lock reaper
        if u.startswith("UPDATE BACKUP_SHIP_LOG SET STATE='FAILED'") \
                and "COALESCE(HEARTBEAT_AT, STARTED_AT)" in u:
            n = 0
            if self.running:
                # Model the real predicate rather than a boolean flag, so a
                # heartbeat genuinely protects a row and its absence genuinely
                # does not. `self.stale` is the shortcut the earlier cases use.
                if self.stale:
                    n = 1
                elif self.started_at is not None:
                    import datetime as _dt
                    thresh = float(a[0]) if a else 1e9
                    ref = self.last_beat or self.started_at
                    quiet = (_dt.datetime.now(_dt.timezone.utc) - ref).total_seconds()
                    n = 1 if quiet > thresh else 0
                if n:
                    self.running = False
                    self.stale = False
                    self.reaped += 1
                    for r in self.rows:
                        if r["state"] == "running":
                            r["state"] = "failed"
            return f"UPDATE {n}"
        if u.startswith("UPDATE BACKUP_SHIP_LOG SET HEARTBEAT_AT=NOW()"):
            import datetime as _dt
            self.beats += 1
            self.last_beat = _dt.datetime.now(_dt.timezone.utc)
            return "UPDATE 1"
        if u.startswith("UPDATE BACKUP_SHIP_LOG"):
            log_id, state, sent, reason, detail = a
            for r in self.rows:
                if r["id"] == log_id:
                    r.update(state=state, bytes_sent=sent, reason=reason, detail=detail)
            self.running = False
        return "UPDATE 0"

    async def fetchrow(self, sql, *a):
        u = " ".join(sql.split()).upper()
        if u.startswith("INSERT INTO BACKUP_SHIP_LOG"):
            if self.running:
                import asyncpg
                raise asyncpg.UniqueViolationError("one running at a time")
            import datetime as _dt
            self.running = True
            self.started_at = _dt.datetime.now(_dt.timezone.utc)
            self.last_beat = None
            rid = len(self.rows) + 1
            self.rows.append({"id": rid, "export_id": a[0], "kind": a[1],
                              "destination": a[2], "state": "running",
                              "bytes_sent": 0, "reason": None, "detail": None})
            return {"id": rid}
        if "FROM HOST_BACKUP_RUNS" in u:
            # The Host's newest backup run, as backup_ship.get_source reads it,
            # or (reason 'before-restore') the undo point get_undo_point reads,
            # which counts only while no send of it is done.
            # Its download link is signed at send time (_signed, faked below
            # to hand back the test server's URL for this source).
            src = self.source
            if "'BEFORE-RESTORE'" in u:
                src = self.undo
                if src and any(k[0] == src["export_id"] for k in self.done_keys):
                    src = None
            if not src:
                return None
            # The places a run's file may be sent FROM, as the query names them.
            if "STORED IN (" in u:
                allowed = u.split("STORED IN (", 1)[1].split(")", 1)[0]
                if f"'{str(src.get('stored')).upper()}'" not in allowed:
                    return None
            SIGNED["url"] = src.get("url")
            return {"id": src["export_id"], "object_key": src["s3_key"],
                    "sidecar_key": src.get("sidecar_key"),
                    "stored": src.get("stored"),
                    "size_bytes": src["size_bytes"], "sha256": src["sha256"],
                    "sidecar_sha256": src.get("sidecar_sha256"),
                    "sidecar_bytes": src.get("sidecar_bytes"),
                    "started_at": src.get("snapshot_time"), "stamp": None}
        if "FROM BACKUP_SHIP_LOG" in u and "STATE='DONE'" in u:
            return {"?column?": 1} if (a[0], a[1]) in self.done_keys else None
        return None

    async def fetch(self, *a, **k):
        u = " ".join(a[0].split()).upper() if a else ""
        if "STATE IN ('DONE','FAILED')" in u:
            return self.history
        return []

    async def execute_reap(self):
        pass

    def acquire(self):
        pool = self

        class Ctx:
            async def __aenter__(self):
                class C:
                    async def execute(self, *a, **k):
                        return None
                return C()

            async def __aexit__(self, *a):
                return False
        return Ctx()


SIGNED: dict = {}


async def _fake_signed(src):
    """The platform signing a link for this Host's own object: here, the test
    server's URL for the source the fake pool just handed out."""
    out = dict(src, url=SIGNED.get("url"))
    if src.get("sidecar_key"):
        out["sidecar_url"] = SIGNED.get("sidecar_url")
    return out


def main() -> int:
    try:
        return _main()
    finally:
        for d in _OWN_TMP:
            shutil.rmtree(d, ignore_errors=True)


def _main() -> int:
    BS._signed = _fake_signed                                 # type: ignore[assignment]
    # ── the contract ─────────────────────────────────────────────────────
    print("contract:")
    check("platform-only (null) is valid", BS.validate_target(None) is None)

    good = BS.validate_target({"kind": "s3", "address": "my-bucket",
                               "cred_name": "my-backup-store",
                               "endpoint": "https://s3.example.com"})
    check("a complete s3 target normalises", good["kind"] == "s3"
          and good["cred_name"] == "my-backup-store")

    m = BS.validate_target({"kind": "machine", "address": "my-nas-box"})
    check("a machine target defaults its directory", m["dir"] == "privatae-backups")
    check("  and carries no credential name — the bastion is itself",
          m["cred_name"] is None)

    for bad, why in (
        ({"kind": "nfs", "address": "//srv/share"}, "nfs is gone"),
        ({"kind": "smb", "address": "//srv/share"}, "smb is gone"),
        ({"kind": "s3", "address": ""}, "no address"),
        ({"kind": "s3", "address": "b"}, "no cred_name"),
        ({"kind": "s3", "address": "b", "cred_name": "k",
          "endpoint": "http://s3.example.com"}, "plaintext endpoint"),
        ({"kind": "s3", "address": "b", "cred_name": "k",
          "secret_key": "AKIA..."}, "an inline secret"),
        ({"kind": "s3", "address": "b", "cred_name": "k",
          "password": "hunter2"}, "an inline password"),
    ):
        try:
            BS.validate_target(bad)
            check(f"refused: {why}", False, "it was ACCEPTED")
        except ValueError as e:
            check(f"refused: {why}", True)
            if "secret" in why or "password" in why:
                check("  and it says to use the Keys app", "Keys app" in str(e))

    # Versioned names, never a fixed 'latest' that a bad upload could destroy.
    n1 = BS.object_name(7, "entity-9003/exports/7-20260818T004008Z.tar.zst.age")
    n2 = BS.object_name(8, "entity-9003/exports/8-20260818T014008Z.tar.zst.age")
    check("object names are versioned per export", n1 != n2 and "7-" in n1)
    check("  and carry no directory from the source key", "/" not in n1)

    # ── the transfer, against a real server ──────────────────────────────
    print("transfer:")
    import ssl
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="backup-ship-test-"))
    _OWN_TMP.append(tmp)
    crt, key = self_signed(tmp)
    # Both clients in the pipe read these: urllib (the source fetch) honours
    # SSL_CERT_FILE, botocore honours AWS_CA_BUNDLE. Neither production path is
    # changed; the test just tells them to trust this one cert.
    os.environ["SSL_CERT_FILE"] = crt
    os.environ["AWS_CA_BUNDLE"] = crt
    # And make sure no proxy env from this box hijacks a loopback call.
    for v in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        os.environ.pop(v, None)
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"

    port = free_port()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), S3Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(crt, key)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"https://127.0.0.1:{port}"
    # The bucket's address must be on the public internet (backup_stores
    # endpoint_problem, held before the client is made and before each request
    # it sends). This gate's destination is on loopback, so that one address,
    # and only it, is let through for the transfers; "the bucket's address"
    # below puts the whole check back.
    from server import backup_stores as _stores
    _real_problem = _stores.endpoint_problem

    def _fixture_only(host, port_=None):
        return None if (host, port_) == ("127.0.0.1", port) else _real_problem(host, port_)
    _stores.endpoint_problem = _fixture_only

    # An artifact bigger than one part, so multipart is genuinely exercised.
    artifact = os.urandom(BS.PART_BYTES + 5_000_000)
    sha = hashlib.sha256(artifact).hexdigest()
    STATE.objects["source/artifact.tar.zst.age"] = artifact

    # stored: every run with a file records where it is (host_backup._run).
    source = {"export_id": 42, "s3_key": "entity-9003/exports/42-x.tar.zst.age",
              "url": f"{base}/src/source/artifact.tar.zst.age",
              "url_expires_at": None, "size_bytes": len(artifact),
              "sha256": sha, "snapshot_time": None, "stored": "backup"}
    target = {"kind": "s3", "address": "bucket", "cred_name": "k",
              "endpoint": base, "prefix": "backups"}

    async def creds(*a, **k):
        return {"access_key_id": "AK", "secret_access_key": "SK"}
    BS._s3_credential = creds                                  # type: ignore[assignment]

    pool = Pool(source)
    out = asyncio.run(BS.ship_now(pool, 9003, dict(target)))
    check("a multi-part artifact ships", out["ok"] is True)
    landed = STATE.objects.get(f"backups/{BS.object_name(42, source['s3_key'])}")
    check("  and the destination holds the exact bytes",
          landed is not None and landed == artifact,
          f"{0 if landed is None else len(landed)} vs {len(artifact)}")
    check("  reported byte count matches", out["bytes"] == len(artifact))
    check("  the attempt is logged done", pool.rows[-1]["state"] == "done")
    check("  and it really was multipart (>1 part)",
          len(artifact) > BS.PART_BYTES)

    # A wrong digest must stop it, and must NOT leave a half object behind.
    STATE.objects.pop(f"backups/{BS.object_name(42, source['s3_key'])}", None)
    bad_src = dict(source, sha256="0" * 64)
    pool = Pool(bad_src)
    try:
        asyncio.run(BS.ship_now(pool, 9003, dict(target)))
        check("a source that does not match its digest is refused", False, "it shipped")
    except BS.ShipError as e:
        check("a source that does not match its digest is refused",
              e.reason == "verify_failed", e.reason)
        check("  and the attempt is logged failed with that class",
              pool.rows[-1]["state"] == "failed" and pool.rows[-1]["reason"] == "verify_failed")

    # The destination refusing must be classed, not raw.
    STATE.fail_with = 403
    pool = Pool(source)
    try:
        asyncio.run(BS.ship_now(pool, 9003, dict(target)))
        check("a 403 from the bucket is classed", False, "it shipped")
    except BS.ShipError as e:
        check("a 403 from the bucket is classed as destination_refused",
              e.reason == "destination_refused", e.reason)
        check("  and the provider's own words are kept",
              "AccessDenied" in e.detail or "403" in e.detail, e.detail[:120])
    STATE.fail_with = None

    # An expired signed URL is its own class — "try again", not "access denied".
    pool = Pool(dict(source, url=f"{base}/src/nope/missing"))
    try:
        asyncio.run(BS.ship_now(pool, 9003, dict(target)))
        check("a dead source URL is classed", False, "it shipped")
    except BS.ShipError as e:
        check("a 404 source is source_unreachable", e.reason == "source_unreachable", e.reason)

    # No destination, no artifact: both are ordinary states with their own class.
    try:
        asyncio.run(BS.ship_now(Pool(source), 9003, None))
        check("no destination is refused", False)
    except BS.ShipError as e:
        check("no destination is refused with its own class", e.reason == "no_target")
    try:
        asyncio.run(BS.ship_now(Pool(None), 9003, dict(target)))
        check("no artifact yet is refused", False)
    except BS.ShipError as e:
        check("no artifact yet is refused with its own class", e.reason == "no_source")

    # One at a time.
    pool = Pool(source)
    pool.running = True
    try:
        asyncio.run(BS.ship_now(pool, 9003, dict(target)))
        check("a second concurrent ship is refused", False)
    except BS.ShipError as e:
        check("a second concurrent ship is refused", e.reason == "in_flight", e.reason)

    # A FAILURE MID-UPLOAD MUST ABORT THE MULTIPART. Abandoned parts keep
    # costing the user storage and are invisible in a bucket listing, so nobody
    # would ever find them.
    STATE.aborted.clear()
    STATE.fail_on_complete = True
    pool = Pool(source)
    try:
        asyncio.run(BS.ship_now(pool, 9003, dict(target)))
        check("a failure at complete is reported", False, "it shipped")
    except BS.ShipError:
        check("a failure at complete is reported", True)
    check("  and the multipart upload was ABORTED, not abandoned",
          len(STATE.aborted) == 1, f"aborted={STATE.aborted}")
    STATE.fail_on_complete = False

    # THE BUCKET'S ADDRESS. The second copy goes to an address the owner
    # typed, from inside the Host, beside services no Host may reach: it is
    # held to the public internet as the owner's bucket is for the backups
    # (backup_stores.check_endpoint), before the client is made and again
    # before each request. No request leaves this gate: the HTTP layer is
    # stopped, and names are answered here.
    print("the bucket's address:")
    import botocore.httpsession as _hs
    _stores.endpoint_problem = _real_problem
    _real_resolve, _real_send = _stores._resolve, _hs.URLLib3Session.send
    asked: dict = {}
    ADDRS = {"inside.ship.test": [["10.20.30.40"]],
             "turns.ship.test": [["34.120.0.20"], ["10.9.8.7"]]}

    def _resolve(host, port_):
        n = asked[host] = asked.get(host, 0) + 1
        answers = ADDRS.get(host) or [[]]
        return list(answers[min(n, len(answers)) - 1])
    sent: list = []

    def _no_send(self, request):
        sent.append(request.url)
        raise RuntimeError("no request leaves this gate")
    _stores._resolve, _hs.URLLib3Session.send = _resolve, _no_send
    try:
        for endpoint, label, why in (
                (base, "this machine (the gate's own bucket, on loopback)", "127.0.0.1"),
                ("https://inside.ship.test", "a name for an address on a private network", "10.20.30.40"),
                ("https://169.254.169.254", "the cloud machine's metadata service", "169.254.169.254"),
                ("https://turns.ship.test", "a name that answers a private address once the client is made",
                 "10.9.8.7")):
            STATE.objects.clear()
            STATE.objects["source/artifact.tar.zst.age"] = artifact
            sent.clear()
            pool = Pool(source)
            try:
                asyncio.run(BS.ship_now(pool, 9003, dict(target, endpoint=endpoint)))
                check(f"the second copy is not sent to {label}", False, "it shipped")
            except BS.ShipError as e:
                check(f"the second copy is not sent to {label}",
                      e.reason == "egress_blocked" and why in e.detail, (e.reason, e.detail[:200]))
            check("  nothing was sent to it, and the attempt is logged failed with that class",
                  not sent and not any(k.startswith("backups/") for k in STATE.objects)
                  and pool.rows[-1]["state"] == "failed" and pool.rows[-1]["reason"] == "egress_blocked",
                  (sent, sorted(STATE.objects), pool.rows[-1]))
        check("the name was asked again before the request, not only when the client was made",
              asked.get("turns.ship.test") == 2, asked)
    finally:
        _stores._resolve, _hs.URLLib3Session.send = _real_resolve, _real_send
        _stores.endpoint_problem = _fixture_only

    # ── the "my own machine" kind: the dispatch half ─────────────────────
    # The bastion half has its own 12 tests in the gateway source
    # (backupfetch_test.go, incl. a real verified delivery). This is the pod
    # side of the same conversation: what it sends, and how it reads each reply.
    print("machine kind:")
    import json as _json

    calls: list[tuple] = []
    script: list[dict] = []

    async def fake_mgmt(pool, eid, server, op, args):
        calls.append((server, op, _json.loads(args[0])))
        return script.pop(0)

    # Patch the ATTRIBUTE on the real module: _ship_to_machine imports the
    # module at call time and then calls nac_mgmt.run_mgmt_op, so this is what
    # it actually reaches. (Patching __import__ did not intercept it — the real
    # signed-grant gate answered instead, which at least proved it default-denies
    # an ungranted op, but is not a controllable fixture.)
    from server.network import nac_mgmt as _nac
    _real_run = _nac.run_mgmt_op
    _nac.run_mgmt_op = fake_mgmt
    try:
        mtarget = {"kind": "machine", "address": "my-nas-box", "dir": "backups"}

        # No grant for the op: the bastion allowlist shape, exit 126.
        script[:] = [{"exit": 126, "stdout": "", "stderr": "not permitted"}]
        try:
            asyncio.run(BS.ship_now(Pool(source), 9003, dict(mtarget)))
            check("an ungranted bastion op is refused", False)
        except BS.ShipError as e:
            check("an ungranted bastion op is classed bastion_refused",
                  e.reason == "bastion_refused", e.reason)
            check("  and the message says how to fix it",
                  "approve it in Network" in e.detail or "granted" in e.detail, e.detail[:120])

        # The happy path: start -> running -> done.
        script[:] = [
            {"exit": 0, "stdout": _json.dumps({"id": "bf-1", "state": "running"}), "stderr": ""},
            {"exit": 0, "stdout": _json.dumps({"state": "running", "got_bytes": 10}), "stderr": ""},
            {"exit": 0, "stdout": _json.dumps({"state": "done", "got_bytes": len(artifact)}),
             "stderr": ""},
        ]
        calls.clear()
        pool = Pool(source)
        out = asyncio.run(BS.ship_now(pool, 9003, dict(mtarget)))
        check("a bastion fetch runs to completion", out["ok"] is True)
        check("  it heartbeats while the bastion is still pulling — the reaper "
              "must not kill a legitimately slow transfer",
              pool.beats >= 1, str(pool.beats))
        check("  and reports the bytes the BASTION says it got",
              out["bytes"] == len(artifact), str(out["bytes"]))
        first = calls[0][2]
        check("  the pod sends the url, the digest and a size cap — never bytes",
              set(first) == {"op", "url", "rpath", "sha256", "size", "max_bytes"}, str(set(first)))
        check("  the digest sent is the artifact's", first["sha256"] == sha)
        check("  the path is the backup's own name: the bastion files it in its backup folder",
              "/" not in first["rpath"] and first["rpath"].endswith(".tar.zst.age"), first["rpath"])
        check("  and it polls with the job id", calls[1][2]["job_id"] == "bf-1")
        check("  the attempt is logged done", pool.rows[-1]["state"] == "done")

        # A backup and its key file land side by side, named by the backup's
        # stamp, in one folder: the bastion's retention keeps and prunes them
        # as a pair by exactly these names.
        script[:] = [
            {"exit": 0, "stdout": _json.dumps({"id": "bf-2", "state": "done"}), "stderr": ""},
            {"exit": 0, "stdout": _json.dumps({"id": "bf-3", "state": "done"}), "stderr": ""},
        ]
        calls.clear()
        paired = dict(source, s3_key="domain-3/b/20260928T020000Z.tar.zst.age", export_id=91,
                      sidecar_key="domain-3/b/20260928T020000Z.key.age",
                      sidecar_sha256="ab" * 32, sidecar_bytes=300)
        SIGNED["sidecar_url"] = f"{base}/src/source/key.age"
        asyncio.run(BS.ship_now(Pool(paired), 9003, dict(mtarget)))
        starts = [c[2] for c in calls if c[2].get("op") == "start"]
        check("the backup and its key file both go to the machine, side by side, named by stamp",
              [x["rpath"] for x in starts] == ["20260928T020000Z.tar.zst.age",
                                               "20260928T020000Z.key.age"]
              and starts[1]["sha256"] == "ab" * 32, [x.get("rpath") for x in starts])

        # Already present (the bastion's idempotency short circuit).
        script[:] = [{"exit": 0, "stdout": _json.dumps(
            {"id": "already-present", "state": "done"}), "stderr": ""}]
        calls.clear()
        out = asyncio.run(BS.ship_now(Pool(source), 9003, dict(mtarget)))
        check("an artifact already on the machine short-circuits",
              out["ok"] and len(calls) == 1, f"{len(calls)} calls")

        # A failed fetch carries the bastion's own reason.
        script[:] = [
            {"exit": 0, "stdout": _json.dumps({"id": "bf-2", "state": "running"}), "stderr": ""},
            {"exit": 0, "stdout": _json.dumps(
                {"state": "failed", "error": "sha256 mismatch"}), "stderr": ""},
        ]
        try:
            asyncio.run(BS.ship_now(Pool(source), 9003, dict(mtarget)))
            check("a failed bastion fetch is reported", False)
        except BS.ShipError as e:
            check("a failed bastion fetch keeps the bastion's own words",
                  e.reason == "bastion_refused" and "sha256 mismatch" in e.detail, e.detail[:80])

        # A restarted bastion is a retry, not a loss.
        script[:] = [
            {"exit": 0, "stdout": _json.dumps({"id": "bf-3", "state": "running"}), "stderr": ""},
            {"exit": 0, "stdout": _json.dumps({"state": "unknown"}), "stderr": ""},
        ]
        try:
            asyncio.run(BS.ship_now(Pool(source), 9003, dict(mtarget)))
            check("a restarted bastion is reported", False)
        except BS.ShipError as e:
            check("a restarted bastion is bastion_unreachable, and says it will retry",
                  e.reason == "bastion_unreachable" and "retry" in e.detail, e.detail[:80])
    finally:
        _nac.run_mgmt_op = _real_run

    # ── the in-flight lock must EXPIRE ───────────────────────────────────
    # SIGKILL, a pod roll or a CancelledError leaves the 'running' row behind.
    # Without an expiry the lane says in_flight forever and the tab says
    # "Sending…" forever, with no way out but a DBA.
    print("locking:")
    pool = Pool(source)
    pool.running = True
    pool.stale = True      # older than SOURCE_TIMEOUT_S
    out = asyncio.run(BS.ship_now(pool, 9003, dict(target)))
    check("a STALE in-flight row is reaped and the ship proceeds", out["ok"] is True)

    pool = Pool(source)
    pool.running = True
    pool.stale = False     # a genuinely live transfer
    try:
        asyncio.run(BS.ship_now(pool, 9003, dict(target)))
        check("a LIVE in-flight row still blocks", False, "it shipped")
    except BS.ShipError as e:
        check("a LIVE in-flight row still blocks", e.reason == "in_flight", e.reason)

    # CancelledError is a BaseException; `except Exception` never saw it, so the
    # lock leaked on every shutdown mid-transfer.
    pool = Pool(source)

    async def cancelled(*a, **k):
        raise asyncio.CancelledError()
    real_cred = BS._s3_credential
    BS._s3_credential = cancelled
    try:
        asyncio.run(BS.ship_now(pool, 9003, dict(target)))
        check("a cancellation propagates", False, "it returned normally")
    except asyncio.CancelledError:
        check("a cancellation propagates as cancellation, not as an error", True)
    except BaseException as e:
        check("a cancellation propagates as cancellation, not as an error",
              False, type(e).__name__)
    finally:
        BS._s3_credential = real_cred
    check("  and the lock is released on the way out", pool.running is False)
    check("  with the attempt recorded, not left running",
          pool.rows[-1]["state"] == "failed", pool.rows[-1]["state"])

    # ── the trigger, and its idempotency ─────────────────────────────────
    print("trigger:")
    pool = Pool(source)
    out = asyncio.run(BS.ship_if_due(pool, 9003, dict(target)))
    check("ship_if_due sends an artifact that has not gone yet", out and out["ok"])
    # Built by the real function, so the test cannot drift from the key the
    # code writes — and so the prefix is genuinely part of it.
    dest = BS.destination_key(BS.validate_target(dict(target)))
    check("  the destination key includes the prefix",
          dest.endswith("/backups"), dest)
    pool.done_keys.add((int(source["export_id"]), dest))
    out2 = asyncio.run(BS.ship_if_due(pool, 9003, dict(target)))
    check("  and does NOT send the same digest to the same place twice",
          out2 is None)
    # A different destination is a legitimate re-send of the same artifact.
    other = dict(target, address="otherbucket")
    check("  but the same artifact DOES go to a newly-chosen destination",
          asyncio.run(BS.ship_if_due(Pool(source), 9003, other))["ok"] is True)
    check("no destination: nothing to do, quietly",
          asyncio.run(BS.ship_if_due(Pool(source), 9003, None)) is None)
    check("no artifact: nothing to do, quietly",
          asyncio.run(BS.ship_if_due(Pool(None), 9003, dict(target))) is None)
    # The copy a restore took of how the Host was just before it: with
    # platform storage off it is only staged, and the Host backs up again
    # right after a restore, so "send the newest" alone would never send it.
    pool = Pool(source)
    undo_id = int(source["export_id"]) + 100
    pool.undo = dict(source, export_id=undo_id, stored="staged")
    out = asyncio.run(BS.ship_if_due(pool, 9003, dict(target)))
    check("the undo point a restore left is sent before the newest backup",
          out and out["ok"] and out["export_id"] == undo_id, out)
    pool.done_keys.add((undo_id, dest))
    out = asyncio.run(BS.ship_if_due(pool, 9003, dict(target)))
    check("  and once it is there, the newest goes next",
          out and out["ok"] and out["export_id"] == int(source["export_id"]), out)
    # A destination saved by an older build must not crash the worker.
    check("a legacy nfs destination is skipped, not raised",
          asyncio.run(BS.ship_if_due(
              Pool(source), 9003, {"kind": "nfs", "address": "//srv/share"})) is None)

    # Two prefixes in the same bucket are two DESTINATIONS. Sharing one key
    # meant a user who moved backups from one prefix to another was told the
    # artifact had "already shipped" and nothing ever landed in the new one.
    a = BS.destination_key(BS.validate_target(
        {"kind": "s3", "address": "bucket", "cred_name": "k", "prefix": "old"}))
    b = BS.destination_key(BS.validate_target(
        {"kind": "s3", "address": "bucket", "cred_name": "k", "prefix": "new"}))
    check("two prefixes in one bucket are different destinations", a != b, f"{a} vs {b}")
    m1 = BS.destination_key(BS.validate_target(
        {"kind": "machine", "address": "nas", "dir": "a"}))
    m2 = BS.destination_key(BS.validate_target(
        {"kind": "machine", "address": "nas", "dir": "b"}))
    check("  and so are two folders on one machine", m1 != m2, f"{m1} vs {m2}")

    # ── ContentMD5, and the SSE-KMS carve-out ────────────────────────────
    print("integrity:")
    check("the destination verified a Content-MD5 on every part",
          STATE.saw_content_md5)
    # A KMS ETag is 32 hex and is NOT an MD5; the hex-shape test alone would
    # have failed every part of a perfectly good upload.
    STATE.pose_as_kms = True
    STATE.objects.pop(f"backups/{BS.object_name(42, source['s3_key'])}", None)
    pool = Pool(source)
    try:
        out = asyncio.run(BS.ship_now(pool, 9003, dict(target)))
        check("an SSE-KMS bucket does not fail on its opaque ETag", out["ok"] is True)
    except BS.ShipError as e:
        check("an SSE-KMS bucket does not fail on its opaque ETag",
              False, f"{e.reason}: {e.detail[:100]}")
    STATE.pose_as_kms = False

    # ── backoff after repeated failure ───────────────────────────────────
    print("backoff:")
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    pool = Pool(source)
    pool.history = [{"state": "failed", "finished_at": now}]
    ok_now, wait = asyncio.run(BS.failure_backoff(pool, 42, "d"))
    check("one recent failure holds the next attempt off",
          not ok_now and 0 < wait <= BS.BACKOFF_BASE_S, f"{ok_now} {wait}")
    pool.history = [{"state": "failed", "finished_at": now - _dt.timedelta(hours=1)}]
    ok_now, _ = asyncio.run(BS.failure_backoff(pool, 42, "d"))
    check("  and releases it once the wait has passed", ok_now)
    pool.history = [{"state": "failed", "finished_at": now}] * 5
    _, wait5 = asyncio.run(BS.failure_backoff(pool, 42, "d"))
    check("repeated failures back off exponentially",
          wait5 > BS.BACKOFF_BASE_S * 8, str(wait5))
    pool.history = [{"state": "failed", "finished_at": now}] * 40
    _, waitmax = asyncio.run(BS.failure_backoff(pool, 42, "d"))
    check("  but never past the ceiling", waitmax <= BS.BACKOFF_MAX_S, str(waitmax))
    pool.history = [{"state": "done", "finished_at": now},
                    {"state": "failed", "finished_at": now}]
    ok_now, _ = asyncio.run(BS.failure_backoff(pool, 42, "d"))
    check("a success resets the count — the next failure is a new problem", ok_now)
    pool.history = []
    check("no history means no wait",
          asyncio.run(BS.failure_backoff(pool, 42, "d"))[0])

    # ── a LONG S3 UPLOAD MUST NOT BE REAPED MID-FLIGHT ───────────────────
    # The regression this exists for: _beat was called only from the machine
    # poll, so an S3 upload — synchronous boto3 in a worker thread, with no loop
    # to await on — went silent for its whole duration. Any upload longer than
    # HEARTBEAT_STALE_S was reaped by the ship worker's next pass, which then
    # started a SECOND multipart upload of the same artifact, and a third twenty
    # minutes later. The suite covered only the machine path, so it passed.
    print("s3 heartbeat:")
    real_stale, real_every = BS.HEARTBEAT_STALE_S, BS.HEARTBEAT_EVERY_S
    # 1 s is the FLOOR for the stale window, not a choice: reap_stale passes it
    # to SQL as str(int(HEARTBEAT_STALE_S)) — whole seconds — so 0.6 becomes 0
    # and the reaper takes every running row, beats or not (found when this
    # was tried at 0.6/0.15: the positive case reaped=1).
    BS.HEARTBEAT_STALE_S, BS.HEARTBEAT_EVERY_S = 1, 0.2
    # The upload must still be RUNNING when the reaper fires, or both the
    # positive and the negative case pass trivially on a row that has already
    # finished — which is exactly what happened first time round. Two parts at
    # 0.9 s is ~1.8 s of upload against a reap at ~1.4 s (was 1.2 s parts,
    # ~2.4 s: the same two facts, 0.4 s margin kept, ~1.2 s less waiting per
    # run).
    STATE.part_delay = 0.9
    STATE.objects.pop(f"backups/{BS.object_name(42, source['s3_key'])}", None)

    async def slow_ship_with_a_reaper_running(beating: bool):
        """Ship, and have a second 'worker pass' try to reap while it runs."""
        pool = Pool(source)
        if not beating:
            # The pre-fix behaviour EXACTLY: no periodic beater AND no beat
            # before the first byte. Stubbing only the beater left the single
            # pre-beat in place, which by itself protected the row for one whole
            # window — so the control reported "not reaped" and would have
            # passed against the broken code. Both go.
            async def _never(*a, **k):
                import asyncio as _a
                await _a.sleep(3600)

            async def _nobeat(*a, **k):
                return None
            BS._s3_heartbeat = _never
            BS._beat = _nobeat
        task = asyncio.ensure_future(BS.ship_now(pool, 9003, dict(target)))
        await asyncio.sleep(BS.HEARTBEAT_STALE_S + 0.4)   # past the window
        in_flight = pool.running                          # must still be true
        reaped = await BS.reap_stale(pool)
        try:
            out = await task
        except BS.ShipError as e:
            out = {"ok": False, "reason": e.reason}
        return pool, reaped, out, in_flight

    real_hb, real_beat = BS._s3_heartbeat, BS._beat
    try:
        pool, reaped, out, in_flight = asyncio.run(
            slow_ship_with_a_reaper_running(True))
        check("the reaper really did fire mid-upload (not after it finished)",
              in_flight, "the upload had already completed — test is vacuous")
        check("a slow S3 upload heartbeats while it runs", pool.beats >= 1,
              f"beats={pool.beats}")
        check("  so a concurrent worker pass reaps NOTHING", reaped == 0,
              f"reaped={reaped}")
        check("  and the upload finishes", out.get("ok") is True, str(out))
        check("  with the bytes actually at the destination",
              STATE.objects.get(f"backups/{BS.object_name(42, source['s3_key'])}")
              == artifact)

        # NEGATIVE CONTROL. Without the heartbeat the same run IS reaped —
        # otherwise this test would pass on the broken code too.
        STATE.objects.pop(f"backups/{BS.object_name(42, source['s3_key'])}", None)
        pool2, reaped2, _, in_flight2 = asyncio.run(
            slow_ship_with_a_reaper_running(False))
        check("without the heartbeat the same upload IS reaped "
              "(so this test can fail)",
              in_flight2 and reaped2 == 1 and pool2.beats == 0,
              f"in_flight={in_flight2} reaped={reaped2} beats={pool2.beats}")
    finally:
        BS._s3_heartbeat, BS._beat = real_hb, real_beat
        BS.HEARTBEAT_STALE_S, BS.HEARTBEAT_EVERY_S = real_stale, real_every
        STATE.part_delay = 0.0

    check("the beat interval leaves room for lost beats",
          BS.HEARTBEAT_EVERY_S * 3 <= BS.HEARTBEAT_STALE_S,
          f"{BS.HEARTBEAT_EVERY_S} vs {BS.HEARTBEAT_STALE_S}")

    # ── the reap threshold must not kill a live bastion transfer ─────────
    check("the pod gives up BEFORE the bastion's own timeout (6 h)",
          BS.MACHINE_POLL_DEADLINE_S < 6 * 3600, str(BS.MACHINE_POLL_DEADLINE_S))
    check("  and a heartbeat window shorter than either",
          BS.HEARTBEAT_STALE_S < BS.MACHINE_POLL_DEADLINE_S)

    # ── bucket shape ─────────────────────────────────────────────────────
    for bad, why in (("my-bucket/backups", "a path in the bucket field"),
                     ("https://s3.example.com/b", "a URL"),
                     ("My-Bucket", "uppercase"),
                     ("ab", "too short")):
        try:
            BS.validate_target({"kind": "s3", "address": bad, "cred_name": "k"})
            check(f"bucket refused: {why}", False, "accepted")
        except ValueError as e:
            check(f"bucket refused: {why}", "bucket name" in str(e))

    # Every class the UI might be handed has a sentence for it.
    check("every reason class has user-facing text",
          all(isinstance(v, str) and v for v in BS.REASONS.values()))

    # ── the backup's own key travels with it; a staged copy is dropped ────
    # A copy at the user's destination is only restorable with its sidecar
    # (the backup's own key, sealed to the user's keys), and a copy staged in
    # platform storage only for this send is dropped once it has arrived.
    print("sidecar and staged:")
    sidecar = b"-----BEGIN AGE ENCRYPTED FILE-----\nxyz\n-----END AGE ENCRYPTED FILE-----\n"
    STATE.objects["source/key.age"] = sidecar
    staged_src = dict(source, s3_key="domain-3/s/20260928T020000Z.tar.zst.age",
                      sidecar_key="domain-3/s/20260928T020000Z.key.age", stored="staged",
                      sidecar_sha256=hashlib.sha256(sidecar).hexdigest(),
                      sidecar_bytes=len(sidecar), export_id=77)
    SIGNED["sidecar_url"] = f"{base}/src/source/key.age"
    dropped = []
    from server import host_backup as _hb

    async def fake_call(op, params=None):
        if op == "backup.drop":
            dropped.append(params["key"])
        return {"ok": True}
    _real_call = _hb.call
    _hb.call = fake_call
    try:
        out = asyncio.run(BS.ship_now(Pool(staged_src), 9003, dict(target)))
        name = BS.object_name(77, staged_src["s3_key"])
        check("the backup ships", out["ok"] and STATE.objects.get(f"backups/{name}") == artifact)
        check("  and its key file lands next to it",
              STATE.objects.get(f"backups/{BS.sidecar_name(name)}") == sidecar,
              sorted(k for k in STATE.objects if k.startswith("backups/")))
        check("  and the staged copy and its key are dropped from platform storage",
              sorted(dropped) == sorted([staged_src["s3_key"], staged_src["sidecar_key"]]), dropped)
        dropped.clear()
        kept_src = dict(staged_src, stored="backup", export_id=78)
        asyncio.run(BS.ship_now(Pool(kept_src), 9003, dict(target)))
        check("a copy kept in platform storage is never dropped", dropped == [], dropped)
        STATE.objects["source/key.age"] = b"tampered"
        try:
            asyncio.run(BS.ship_now(Pool(dict(staged_src, export_id=79)), 3, dict(target)))
            check("a key file that is not what the Host sealed is refused", False)
        except BS.ShipError as e:
            check("a key file that is not what the Host sealed is refused",
                  e.reason == "verify_failed", e.reason)
        check("  and then nothing is dropped", dropped == [], dropped)
    finally:
        _hb.call = _real_call

    # A backup written straight to the owner's bucket or to a folder on the
    # machine (server/host_backup.py, where s3 / local) is already where it
    # goes: there is nothing in the network's storage to send it from.
    print("written where it goes:")
    for stored in ("local", "s3"):
        got = asyncio.run(BS.get_source(Pool(dict(source, stored=stored))))
        check(f"a run stored {stored!r} is never taken as something to send", got is None, got)
    check("a run in the network's storage still is",
          asyncio.run(BS.get_source(Pool(dict(source, stored="backup")))) is not None)

    srv.shutdown()
    _stores.endpoint_problem = _real_problem
    return FAILED


if __name__ == "__main__":
    main()
    left = [str(d) for d in _OWN_TMP if d.exists()]
    check("the gate leaves none of its own temp directories behind",
          not left, ", ".join(left))
    print("PASS" if not FAILED else "FAIL")
    raise SystemExit(FAILED)
