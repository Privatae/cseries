"""
Shipping the user's backup to a destination THEY named.

The POD ships. To any S3-compatible endpoint the user names,
with the credential named by `target.cred_name` opened in-pod — the platform
never holds it. `nfs` and `smb` are replaced by one kind, "my own machine": the
artifact goes to the user's bastion, which writes it into a directory they
configure (their NAS mount, if that is where they point it).

WHY THE POD AND NOT THE MANAGEMENT PLANE. The credential lives in this pod's
vault. Shipping from the management plane would mean lifting a user's S3 secret out of the
pod and holding it in platform memory for the length of a run — the first time
anything on the management plane would read a user credential value at all
(today it reads public keys and nothing else). The bytes have to pass through
somewhere; better here, where the secret already is.

WHAT GETS SHIPPED. The Host's own newest backup (server/host_backup.py): one
age-encrypted file, and its sidecar (the backup's own key, sealed to the same
registered keys). Both are already encrypted to keys the platform does not
hold, so sending them to a third party leaks nothing. They are read from
platform storage through a short-lived signed link the platform issues for
this Host's own folder (bridge op backup.get_url). When the user keeps no copy
in platform storage, that copy was only staged there for this send, and it is
dropped once the destination holds both files.

STREAMED, NEVER BUFFERED. The artifact is ~224 MB for the largest entity today
and the design has to survive it growing. Source bytes are read in parts and
each part is uploaded as it is read; peak memory is one part, not one artifact.
The sha256 is computed over the same pass, so verification costs nothing extra.

"MY OWN MACHINE" IS A PULL, NOT A PUSH, and that is measured rather than argued
— see the gateway's backupfetch.go. The obvious route, ctx_put, caps
out just under 12 MiB per call (uvicorn's 16 MiB ws_max_size against a base64
payload in one JSON frame), which is 1,110 sequential dispatches for a 13 GiB
artifact against a 30 s per-dispatch timeout. So the pod hands the bastion the
signed URL and a digest, and the bastion fetches it over the user's own
connection. The bytes never transit this pod for that kind.

EVERY FAILURE HAS A CLASS. The Backup tab has to tell a user what to do next,
and "ClientError: An error occurred (403)" does not. Each failure resolves to
one of the REASONS below plus the provider's verbatim message; the UI explains
the class and shows the message underneath. Nothing is ever reported green
because an error was swallowed.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any, Optional

import asyncpg

log = logging.getLogger("privatae.pc.backup_ship")

# Read size, and the S3 multipart part size. 16 MiB is above S3's 5 MiB part
# floor with room to spare, and 10,000 parts of it is 160 GB — past any entity.
PART_BYTES = 16 * 1024 * 1024
# A stalled read must not hold the ship lock forever.
SOURCE_TIMEOUT_S = 4 * 3600

# THE LOCK IS REAPED ON SILENCE, NOT ON AGE, and these three numbers have to
# stay in this order or a healthy transfer gets killed and restarted.
#
# The bastion's own fetch may run for six hours (backupFetchTimeout in
# the gateway's backupfetch.go). Reaping any row older than SOURCE_TIMEOUT_S
# would have failed a perfectly healthy 4-to-6-hour download, and because
# nothing tells the bastion to stop, the retry would start a SECOND download of
# the same artifact over the user's home connection.
#
# So a live transfer proves it is alive: every poll of a bastion job that is
# still 'running' bumps heartbeat_at, and the reaper only takes rows that have
# gone quiet. The pod's own poll deadline is deliberately shorter than the
# bastion's timeout, so the pod gives up first and the bastion is never left
# fetching for something nobody is waiting on.
HEARTBEAT_STALE_S = 20 * 60          # no word in 20 min -> the mover is gone
MACHINE_POLL_DEADLINE_S = 5 * 3600   # < the bastion's 6 h, > a slow 4 h fetch
# First status poll after a bastion fetch is started; grows x1.5 to 30s. A
# module constant rather than a literal so the gate can shrink it — every
# machine-kind scenario paid this 2s at least once, 9s of a 108s pre-merge run
# spent waiting for a fake bastion that answers instantly.
MACHINE_POLL_FIRST_S = 2.0

KIND_S3 = "s3"
KIND_MACHINE = "machine"
TARGET_KINDS = (KIND_S3, KIND_MACHINE)

# The closed set of failure classes. The UI maps each to a sentence; anything
# not in here would render as raw text at a user, so `unknown` is deliberate
# and deliberately last-resort.
REASONS = {
    "no_target": "no destination is saved",
    "no_source": "no backup artifact is available to send yet",
    "source_expired": "the download link for the artifact had expired",
    "source_unreachable": "the artifact could not be downloaded",
    "credential_missing": "the key named for this destination is not in the Keys app",
    "credential_off": "the key named for this destination is switched off in the Keys app",
    "credential_malformed": "the key in the Keys app is not in the expected form",
    "egress_blocked": "this host is not allowed to reach that address",
    "destination_refused": "the destination refused the upload",
    "destination_unreachable": "the destination could not be reached",
    "verify_failed": "what arrived at the destination does not match what was sent",
    "bastion_unreachable": "your machine is not connected",
    "bastion_refused": "your machine refused the transfer",
    "in_flight": "a transfer is already running",
    "unknown": "the transfer failed",
}


class ShipError(Exception):
    """A failure with a CLASS. `reason` is a REASONS key; `detail` is whatever
    the far end actually said, kept verbatim so a user chasing a bucket policy
    reads the provider's words and not our paraphrase."""

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason if reason in REASONS else "unknown"
        self.detail = (detail or "")[:2000]
        super().__init__(f"{self.reason}: {self.detail[:200]}")


# ── the contract: what a saved target must look like ─────────────────────────

def validate_target(target: Optional[dict]) -> Optional[dict]:
    """Normalise and check a saved destination. Returns the cleaned dict, or
    None for "platform backup only". Raises ValueError naming the field.

    Kept as a pure function so the route, the ship worker and the tests all
    agree on one definition of a valid destination. A silently-clamped value
    here means a user believing a second copy exists that nothing is writing."""
    if target is None:
        return None
    if not isinstance(target, dict):
        raise ValueError("target must be an object or null")
    kind = str(target.get("kind", "")).strip().lower()
    if kind not in TARGET_KINDS:
        raise ValueError(
            f"target.kind must be one of {', '.join(TARGET_KINDS)} (got {kind!r})")
    # An inline secret must never be accepted: this row lands in every pg_dump.
    for banned in ("password", "secret", "access_key", "secret_key", "token",
                   "credential", "creds"):
        if banned in target:
            raise ValueError(
                f"target.{banned} is not accepted here — store the credential in the "
                "Keys app and reference it by cred_name")
    address = str(target.get("address", "")).strip()
    if not address:
        raise ValueError("target.address is required")
    out: dict[str, Any] = {"kind": kind, "address": address}

    if kind == KIND_S3:
        # Bucket SHAPE, not just non-empty. A path pasted into the bucket field
        # ("my-bucket/backups") produces a signature failure the user cannot
        # read; the prefix field is where a path goes. S3 naming: 3-63 chars,
        # lowercase alphanumeric plus dot and hyphen, starting and ending
        # alphanumeric.
        import re as _re
        if not _re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", address):
            raise ValueError(
                f"target.address must be a bucket name, not a path or URL (got "
                f"{address!r}) — 3-63 characters, lowercase letters, digits, "
                "dots and hyphens. Put any folder in target.prefix instead.")
        cred_name = str(target.get("cred_name", "")).strip()
        if not cred_name:
            raise ValueError(
                "target.cred_name is required for an S3 destination — it names the "
                "key in your Keys app that this host will use")
        endpoint = str(target.get("endpoint", "")).strip()
        if endpoint and not endpoint.startswith("https://"):
            # http would put the credential on the wire in the clear. The
            # artifact is already encrypted; the credential is not.
            raise ValueError("target.endpoint must be https")
        out["cred_name"] = cred_name
        out["endpoint"] = endpoint or None
        out["region"] = str(target.get("region", "")).strip() or None
        out["prefix"] = str(target.get("prefix", "")).strip().strip("/") or None
    else:  # machine
        # `address` is the bastion's server name, the same handle the mgmt lane
        # uses. cred_name is meaningless here — the bastion authenticates as
        # itself over its own tunnel — and accepting one would imply otherwise.
        out["cred_name"] = None
        out["dir"] = str(target.get("dir", "")).strip().strip("/") or "privatae-backups"
    return out


def object_name(export_id: int, s3_key: str, snapshot_time=None) -> str:
    """The name the backup is written under at the destination: the leaf the
    Host gave it, `<stamp>.tar.zst.age`, with its key file `<stamp>.key.age`
    beside it (sidecar_name).

    VERSIONED by its stamp, never a fixed 'latest': overwriting one name would
    mean a corrupt or truncated upload destroying the only good copy the user
    had. And exactly these names, in one folder, because the bastion's own
    retention keeps and prunes pairs by them (privatae-bastion backup
    retention); anything else in that folder it leaves alone."""
    leaf = (s3_key or "").rsplit("/", 1)[-1]
    return leaf or f"{export_id}.tar.zst.age"


# ── state ────────────────────────────────────────────────────────────────────

def destination_key(target: dict) -> str:
    """The string that identifies WHERE an artifact went.

    Includes the prefix (and the directory, for a machine). It did not, and that
    made two different places share one key: a user who moved their backups from
    bucket/old to bucket/new would have had the move recorded as "already
    shipped" and nothing would ever land in the new prefix. Also what the tab
    shows, so it should read like the place it names."""
    if target["kind"] == KIND_MACHINE:
        return f"{target['address']}:{target.get('dir') or 'privatae-backups'}"
    base = f"{target.get('endpoint') or 's3'}/{target['address']}"
    return f"{base}/{target['prefix']}" if target.get("prefix") else base


async def get_source(pool: asyncpg.Pool) -> Optional[dict]:
    """The newest backup this Host took that can be sent: its encrypted file
    and its sidecar, as the platform named them. `export_id` is the run's id,
    so a send is keyed to one immutable file. The download links are signed
    when the send starts (_signed), not here. Only a run kept in the network's
    storage: one written straight to the owner's bucket or to a folder on the
    machine (stored 's3' / 'local') is already where it goes."""
    row = await pool.fetchrow(
        "SELECT id, stamp, stored, object_key, sidecar_key, size_bytes, sha256, "
        "       sidecar_sha256, sidecar_bytes, started_at "
        "FROM host_backup_runs WHERE state IN ('ok', 'partial') "
        "  AND stored IN ('backup', 'staged') "
        "  AND object_key IS NOT NULL AND sidecar_key IS NOT NULL "
        "ORDER BY id DESC LIMIT 1")
    return _source_of(row)


# A staged copy lives about a day in platform storage (the platform drops
# older ones); past this there is nothing left to send.
UNDO_POINT_WINDOW_S = 2 * 86400


async def get_undo_point(pool: asyncpg.Pool) -> Optional[dict]:
    """The backup a restore took of how the Host was just before it, when it
    was only STAGED (platform storage off) and has not reached the second
    copy yet. get_source sends only the newest backup, and the Host backs up
    again soon after a restore, so without this the undo point would never
    leave staging. The restore records it in the restored database
    (host_restore._keep_undo_point)."""
    row = await pool.fetchrow(
        "SELECT r.id, r.stamp, r.stored, r.object_key, r.sidecar_key, r.size_bytes, r.sha256, "
        "       r.sidecar_sha256, r.sidecar_bytes, r.started_at "
        "FROM host_backup_runs r WHERE r.reason = 'before-restore' AND r.stored = 'staged' "
        "  AND r.state IN ('ok', 'partial') "
        "  AND r.object_key IS NOT NULL AND r.sidecar_key IS NOT NULL "
        "  AND r.started_at > now() - ($1::text || ' seconds')::interval "
        "  AND NOT EXISTS (SELECT 1 FROM backup_ship_log l "
        "                  WHERE l.export_id = r.id AND l.state = 'done') "
        "ORDER BY r.id DESC LIMIT 1", str(UNDO_POINT_WINDOW_S))
    return _source_of(row)


def _source_of(row) -> Optional[dict]:
    if not row:
        return None
    return {"export_id": int(row["id"]), "s3_key": row["object_key"],
            "sidecar_key": row["sidecar_key"], "stored": row["stored"],
            "size_bytes": int(row["size_bytes"] or 0), "sha256": row["sha256"],
            "sidecar_sha256": row["sidecar_sha256"],
            "sidecar_bytes": int(row["sidecar_bytes"] or 0),
            "snapshot_time": row["started_at"], "url": None, "sidecar_url": None}


async def _signed(src: dict) -> dict:
    """The source with its download links, signed now by the platform for
    this Host's own folder."""
    if src.get("url"):
        return src
    from server import host_backup
    try:
        src = dict(src, url=(await host_backup.call("backup.get_url", {"key": src["s3_key"]}))["url"])
        if src.get("sidecar_key"):
            src["sidecar_url"] = (await host_backup.call(
                "backup.get_url", {"key": src["sidecar_key"]}))["url"]
    except Exception as e:
        raise ShipError("source_unreachable", f"the platform did not sign a link: {e}") from None
    return src


def sidecar_name(name: str) -> str:
    """The destination name of a backup's sidecar, next to the file."""
    return name[: -len(".tar.zst.age")] + ".key.age" if name.endswith(".tar.zst.age") else name + ".key"


async def last_attempts(pool: asyncpg.Pool, limit: int = 5) -> list[dict]:
    rows = await pool.fetch(
        "SELECT id, export_id, kind, destination, state, bytes_sent, reason, detail, "
        "       started_at, finished_at FROM backup_ship_log "
        "ORDER BY started_at DESC LIMIT $1", limit)
    return [dict(r) for r in rows]


async def reap_stale(pool: asyncpg.Pool) -> int:
    """Fail off any 'running' row older than the transfer timeout.

    The partial unique index on state='running' is the lock, and a lock with no
    expiry is a deadlock waiting for a pod roll. SIGKILL, an evicted pod, or an
    asyncio CancelledError (a BaseException — the `except Exception` in
    ship_now never saw it) all leave the row behind, and from then on every
    attempt returns in_flight and the tab says "Sending…" forever, with no way
    out but a DBA. Reaped here and at startup."""
    res = await pool.execute(
        "UPDATE backup_ship_log SET state='failed', reason='unknown', "
        "detail='the transfer stopped reporting (host restarted?)', "
        "finished_at=now() "
        "WHERE state='running' "
        "  AND coalesce(heartbeat_at, started_at) "
        "      < now() - ($1::text || ' seconds')::interval",
        str(int(HEARTBEAT_STALE_S)))
    try:
        return int(res.split()[-1])
    except Exception:
        return 0


async def _begin(pool: asyncpg.Pool, export_id: int, kind: str, destination: str) -> int:
    await reap_stale(pool)
    try:
        row = await pool.fetchrow(
            "INSERT INTO backup_ship_log (export_id, kind, destination, state) "
            "VALUES ($1,$2,$3,'running') RETURNING id", export_id, kind, destination)
    except asyncpg.UniqueViolationError:
        # The partial unique index on state='running' is the lock. Losing that
        # race is not an error worth logging as a failure — it means the lane is
        # already doing exactly what was asked.
        raise ShipError("in_flight", "another transfer is already running")
    return int(row["id"])


# How often a running transfer says it is alive. A quarter of the staleness
# window, so three beats can be lost to a slow database or a busy loop before
# the reaper takes a healthy row.
HEARTBEAT_EVERY_S = HEARTBEAT_STALE_S // 4


async def _s3_heartbeat(pool: asyncpg.Pool, log_id: int, progress: dict) -> None:
    """Beat for the S3 upload while it runs in its worker thread.

    THE UPLOAD CANNOT BEAT FOR ITSELF. It is synchronous boto3 inside
    asyncio.to_thread, with no loop to await on. Only the machine path was
    beating, so any S3 upload lasting longer than HEARTBEAT_STALE_S — a 13 GiB
    export easily does — was reaped by the ship worker's very next pass, which
    then started a SECOND multipart upload of the same artifact, and a third
    twenty minutes later. Egress multiplied and the tab flapped failed -> done.

    Wall-clock rather than per-part on purpose: a single 16 MiB part stalling on
    a slow source would otherwise go quiet for the whole stall and be reaped for
    it. This still dies with the process, which is the only thing the reaper is
    actually trying to detect."""
    import asyncio
    while True:
        await asyncio.sleep(HEARTBEAT_EVERY_S)
        await _beat(pool, log_id, int(progress.get("sent") or 0))


async def _beat(pool: asyncpg.Pool, log_id: int, sent: int = 0) -> None:
    """This transfer is still moving. Best-effort: a failed heartbeat write must
    not kill a transfer that is otherwise fine — the worst case is the reaper
    taking it, and the ship is idempotent."""
    try:
        await pool.execute(
            "UPDATE backup_ship_log SET heartbeat_at=now(), bytes_sent=$2 "
            "WHERE id=$1", log_id, int(sent))
    except Exception as e:  # pragma: no cover - best effort
        log.debug("backup ship: heartbeat write failed: %r", e)


async def _finish(pool: asyncpg.Pool, log_id: int, *, state: str, sent: int,
                  reason: str = None, detail: str = "") -> None:
    await pool.execute(
        "UPDATE backup_ship_log SET state=$2, bytes_sent=$3, reason=$4, detail=$5, "
        "finished_at=now() WHERE id=$1", log_id, state, int(sent),
        reason, (detail or "")[:2000])


# ── the credential ───────────────────────────────────────────────────────────

async def _s3_credential(pool, entity_id: int, cred_name: str) -> dict:
    """Open the user's S3 credential in-pod, and say precisely which way it was
    missing. "It didn't work" sends a user to the wrong place; "the key is
    switched off in the Keys app" sends them to the right one."""
    from server.vault import api as vault_api
    raw = await vault_api.open_secret(pool, entity_id=entity_id, name=cred_name)
    if not raw:
        # open_secret returns None both for absent and for DEACTIVATED (the
        # user's temporary off-switch, vault/store.py). They are different
        # problems with different fixes, so ask which one this is.
        names = {r["logical_name"]: r for r in
                 await vault_api.list_secrets(pool, entity_id=entity_id)}
        if cred_name in names and names[cred_name].get("deactivated_at"):
            raise ShipError("credential_off", f"'{cred_name}' is switched off in Keys")
        raise ShipError("credential_missing", f"no key named '{cred_name}' in Keys")
    try:
        creds = json.loads(raw.decode("utf-8"))
        if not creds.get("access_key_id") or not creds.get("secret_access_key"):
            raise ValueError("missing access_key_id / secret_access_key")
    except Exception as e:
        raise ShipError(
            "credential_malformed",
            f"'{cred_name}' should hold JSON like "
            '{"access_key_id":"...","secret_access_key":"..."} — ' + str(e)) from None
    return creds


def _classify_boto(e: Exception) -> ShipError:
    """Turn a boto/urllib failure into a class a user can act on."""
    from botocore.exceptions import ClientError, EndpointConnectionError
    if isinstance(e, ClientError):
        code = (e.response.get("Error", {}) or {}).get("Code", "")
        status_code = (e.response.get("ResponseMetadata", {}) or {}).get("HTTPStatusCode")
        if status_code in (401, 403) or code in ("AccessDenied", "InvalidAccessKeyId",
                                                 "SignatureDoesNotMatch"):
            return ShipError("destination_refused", str(e))
        return ShipError("destination_refused", str(e))
    if isinstance(e, EndpointConnectionError):
        return ShipError("destination_unreachable", str(e))
    msg = str(e)
    # The node egress proxy refuses anything that is not 80/443 with a CONNECT
    # denial. That is a configuration answer, not a credential answer, and
    # saying so saves a user rotating a key that was fine.
    if "403" in msg and ("proxy" in msg.lower() or "CONNECT" in msg):
        return ShipError("egress_blocked", msg)
    return ShipError("unknown", msg)


# ── the S3 kind ──────────────────────────────────────────────────────────────

def _open_source(url: str):
    """Open the signed URL through the pod's governed egress proxy.

    urllib honours HTTPS_PROXY from the environment, which is the pod's only
    route off the node (server/network/nac_egress.py _upstream_proxy reads the
    same variable). Going direct would work for nothing and fail confusingly."""
    import urllib.error
    import urllib.request
    try:
        return urllib.request.urlopen(url, timeout=SOURCE_TIMEOUT_S)
    except urllib.error.HTTPError as e:
        if e.code in (403, 401):
            # A signed URL that has aged out reads exactly like this, and it is
            # the common case — the next attempt signs a fresh one, so the
            # honest answer is "try again shortly", not "access denied".
            raise ShipError("source_expired", f"HTTP {e.code} fetching the artifact") from None
        raise ShipError("source_unreachable", f"HTTP {e.code} fetching the artifact") from None
    except Exception as e:
        raise ShipError("source_unreachable", str(e)) from None


def _guard_request(request, **_kw) -> None:
    """botocore's before-send: the address of each request, as sent, held to
    backup_stores.endpoint_problem like the owner's bucket client's."""
    from urllib.parse import urlsplit

    from server import backup_stores
    parts = urlsplit(getattr(request, "url", "") or "")
    why = backup_stores.endpoint_problem(parts.hostname or "", parts.port)
    if why:
        raise ShipError("egress_blocked", f"the bucket's address cannot be used: {why}")


def _ship_to_s3_blocking(creds: dict, target: dict, src, name: str,
                         size: int, want_sha: str,
                         progress: Optional[dict] = None) -> dict:
    """Stream the artifact into the user's bucket. Runs off the event loop.

    `progress` is a plain dict the caller owns and this thread writes `sent`
    into. The event loop reads it to heartbeat the log row (see _s3_heartbeat):
    a transfer that cannot say it is alive gets reaped mid-flight and started
    again alongside itself.

    Multipart above one part, single PUT below it — a small object does not need
    three round trips. The digest is computed over the bytes as they pass, so
    the verification below is of what was actually sent, not of what we meant to
    send."""
    import boto3
    from botocore.config import Config

    # The bucket's address must be on the public internet, as the owner's
    # bucket must for the backups themselves (server/backup_stores.py
    # check_endpoint says why: loopback, the metadata service, the cluster's
    # and the LAN's addresses): checked before the client is made and again
    # before each request it sends, since a name can answer differently later.
    from server import backup_stores
    try:
        backup_stores.check_endpoint(target.get("endpoint"))
    except ValueError as e:
        raise ShipError("egress_blocked", str(e)) from None
    session = boto3.session.Session(
        aws_access_key_id=creds.get("access_key_id"),
        aws_secret_access_key=creds.get("secret_access_key"),
        aws_session_token=creds.get("session_token") or None,
        region_name=target.get("region"),
    )
    client = session.client(
        "s3", endpoint_url=target.get("endpoint"),
        config=Config(
            signature_version="s3v4",
            retries={"max_attempts": 3},
            # THE DESTINATION IS NOT OURS. botocore 1.42 defaults
            # request_checksum_calculation to "when_supported", which puts every
            # upload body in aws-chunked framing with a trailing CRC32
            # (Transfer-Encoding: chunked + Content-Encoding: aws-chunked +
            # X-Amz-Trailer). AWS and current MinIO handle
            # it; plenty of S3-compatible endpoints a user might name do not,
            # and the failure is an opaque 400 or a hung upload rather than
            # anything a user could diagnose. "when_required" sends a plain
            # Content-Length body, which every S3 implementation understands.
            # Integrity is not weakened: the artifact's sha256 is verified over
            # the bytes as they stream, and head_object re-checks the length at
            # the destination afterwards.
            request_checksum_calculation="when_required",
        ))
    client.meta.events.register("before-send", _guard_request)
    bucket = target["address"]
    key = f"{target['prefix']}/{name}" if target.get("prefix") else name

    def check_digest(h, sent):
        """The digest gate. Runs BEFORE the object is published, never after.

        The first version completed the upload and then checked — so a source
        that delivered the wrong bytes left a WRONG OBJECT sitting at the
        destination under a name that says it is a backup. For multipart that
        is what abort is for; for a single PUT the check simply happens first."""
        got = h.hexdigest()
        if want_sha and got != want_sha:
            raise ShipError(
                "verify_failed",
                f"read {sent} bytes with sha256 {got}, expected {want_sha} — "
                "the source did not deliver the artifact we were told to expect")
        if size and sent != size:
            raise ShipError("verify_failed", f"read {sent} bytes, expected {size}")

    def part_md5(chunk: bytes) -> str:
        """ContentMD5 for a part, so the DESTINATION verifies it on arrival.

        request_checksum_calculation="when_required" (chosen so third-party
        endpoints accept the upload at all) removed the CRC32 trailer that would
        otherwise have had the far end check each part. Content-MD5 is the
        oldest and most universally implemented replacement: every S3
        implementation validates it and rejects the part on mismatch, so
        corruption is caught in transit by the receiver rather than inferred
        afterwards by us."""
        import base64 as _b64
        return _b64.b64encode(hashlib.md5(chunk).digest()).decode("ascii")

    def check_part(resp, chunk, n):
        """Second line: compare the returned ETag with the MD5 we sent.

        Skipped when the response says the object is server-side encrypted with
        KMS. An SSE-KMS ETag is 32 hex characters and is NOT an MD5 — the
        hex-shape test alone would have read it as one and failed every part of
        a perfectly good upload into a KMS-encrypted bucket. ContentMD5 above
        has already been checked by the destination in that case, so nothing is
        lost by staying quiet here."""
        if resp.get("SSEKMSKeyId") or resp.get("ServerSideEncryption") in (
                "aws:kms", "aws:kms:dsse"):
            return
        etag = (resp.get("ETag") or "").strip('"')
        if len(etag) == 32 and all(c in "0123456789abcdef" for c in etag.lower()):
            want = hashlib.md5(chunk).hexdigest()
            if etag.lower() != want:
                raise ShipError(
                    "verify_failed",
                    f"part {n} arrived corrupted: the destination reports {etag}, "
                    f"we sent {want}")

    def note(n: int) -> None:
        if progress is not None:
            progress["sent"] = n

    h = hashlib.sha256()
    sent = 0
    first = src.read(PART_BYTES)
    h.update(first)
    sent += len(first)
    note(sent)

    if len(first) < PART_BYTES:
        # One part; no multipart dance. Verified before it is written at all,
        # so a bad source never produces an object.
        check_digest(h, sent)
        client.put_object(Bucket=bucket, Key=key, Body=first,
                          ContentMD5=part_md5(first))
    else:
        up = client.create_multipart_upload(Bucket=bucket, Key=key)
        upload_id = up["UploadId"]
        parts = []
        try:
            n = 1
            chunk = first
            while chunk:
                r = client.upload_part(Bucket=bucket, Key=key, PartNumber=n,
                                       UploadId=upload_id, Body=chunk,
                                       ContentMD5=part_md5(chunk))
                check_part(r, chunk, n)
                parts.append({"ETag": r["ETag"], "PartNumber": n})
                n += 1
                chunk = src.read(PART_BYTES)
                if chunk:
                    h.update(chunk)
                    sent += len(chunk)
                    note(sent)
            # BEFORE complete. Parts already uploaded are invisible until this
            # call, so failing here and aborting leaves nothing behind.
            check_digest(h, sent)
            client.complete_multipart_upload(
                Bucket=bucket, Key=key, UploadId=upload_id,
                MultipartUpload={"Parts": parts})
        except BaseException:
            # ABORT, always. An abandoned multipart upload keeps its parts and
            # the user keeps paying storage for bytes no object references —
            # invisible in a bucket listing, so nobody would ever find them.
            try:
                client.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
            except Exception as ae:  # pragma: no cover - best effort
                log.warning("backup ship: could not abort multipart %s: %r", upload_id, ae)
            raise

    got_sha = h.hexdigest()

    # ASK THE DESTINATION WHAT IT HAS. Everything above measures what we sent;
    # this is the only step that measures what actually landed.
    head = client.head_object(Bucket=bucket, Key=key)
    if int(head.get("ContentLength", -1)) != sent:
        raise ShipError(
            "verify_failed",
            f"the destination reports {head.get('ContentLength')} bytes for {key}, "
            f"but {sent} were sent")
    return {"key": key, "bytes": sent, "sha256": got_sha}


# ── the machine kind ─────────────────────────────────────────────────────────

async def _ship_to_machine(pool, entity_id: int, target: dict, src_row: dict,
                           name: str, log_id: int = 0) -> dict:
    """Hand the bastion the signed URL and let it pull.

    Nothing streams through this pod for this kind, which is the point: see the
    measurement in the gateway's backupfetch.go. The op is dispatched
    through the same signed-grant, default-deny gate as every other management
    op (server/network/nac_mgmt.run_mgmt_op) — no grant covering
    (server, 'backup_fetch') means it is refused, never run."""
    from server.network import nac_mgmt

    # The bastion puts a backup-named file in its own backup folder, whatever
    # directory is named (bastion 0.46.0 backupfetch): the name is all it takes.
    rpath = name
    req = {"op": "start", "url": src_row["url"], "rpath": rpath,
           "sha256": src_row["sha256"], "size": int(src_row["size_bytes"]),
           "max_bytes": int(src_row["size_bytes"])}
    try:
        res = await nac_mgmt.run_mgmt_op(pool, entity_id, target["address"],
                                         "backup_fetch", [json.dumps(req)])
    except Exception as e:
        raise ShipError("bastion_unreachable", str(e)) from None
    if res.get("exit") == 126:
        raise ShipError(
            "bastion_refused",
            "your machine has not been granted this transfer. In the Network app, "
            "approve a management grant for this machine covering the operation "
            "named backup_fetch, then try again — the grant needs your six-digit "
            "code. " + (res.get("stderr") or "")[:300])
    if res.get("exit") != 0:
        raise ShipError("bastion_refused", (res.get("stderr") or "")[:500])

    started = json.loads(res.get("stdout") or "{}")
    if started.get("state") == "done":
        # Already present with the right digest — the bastion short-circuits.
        return {"key": rpath, "bytes": int(src_row["size_bytes"]), "job": "already-present"}

    job_id = started.get("id")
    if not job_id:
        raise ShipError("bastion_refused", f"no job id in the reply: {res.get('stdout')!r}")

    # Poll. Each poll is its own short dispatch, which is why the bastion side
    # is async in the first place: nac_mgmt gives up after 30 s, and a
    # multi-hundred-megabyte fetch over a home connection is longer than that.
    import asyncio
    # SHORTER than the bastion's own 6 h timeout, on purpose: the pod must give
    # up first, so the bastion is never left fetching for something nobody is
    # waiting on, and a retry cannot start a second download alongside a live one.
    deadline = asyncio.get_event_loop().time() + MACHINE_POLL_DEADLINE_S
    delay = MACHINE_POLL_FIRST_S
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(delay)
        delay = min(delay * 1.5, 30.0)
        try:
            res = await nac_mgmt.run_mgmt_op(
                pool, entity_id, target["address"], "backup_fetch",
                [json.dumps({"op": "status", "job_id": job_id})])
        except Exception as e:
            raise ShipError("bastion_unreachable", str(e)) from None
        if res.get("exit") != 0:
            raise ShipError("bastion_refused", (res.get("stderr") or "")[:500])
        job = json.loads(res.get("stdout") or "{}")
        state = job.get("state")
        if state == "running" and log_id:
            # The bastion is still pulling. Prove it to the reaper, which
            # otherwise takes any row that has been running longer than the
            # staleness window — and this transfer is legitimately allowed to
            # take hours.
            await _beat(pool, log_id, int(job.get("got_bytes") or 0))
        if state == "done":
            return {"key": rpath, "bytes": int(job.get("got_bytes") or 0), "job": job_id}
        if state == "failed":
            raise ShipError("bastion_refused", job.get("error") or "the transfer failed")
        if state == "unknown":
            # The bastion restarted mid-fetch. Its in-memory job is gone and the
            # start is idempotent, so this is a retry, not a loss.
            raise ShipError("bastion_unreachable",
                            "your machine restarted during the transfer; it will retry")
    raise ShipError("bastion_unreachable",
                    "your machine did not finish the transfer in time")


# ── the entry point ──────────────────────────────────────────────────────────

async def ship_now(pool: asyncpg.Pool, entity_id: int, target: Optional[dict],
                   src: Optional[dict] = None) -> dict:
    """Ship one artifact to the user's destination: `src` (get_source or
    get_undo_point), else the newest.

    Returns {ok, ...}. Never raises for an expected failure: the attempt is
    recorded with its class either way, because a lane that failed silently is
    the exact thing this surface exists to prevent."""
    import asyncio

    if not target:
        raise ShipError("no_target", "no destination is saved")
    target = validate_target(target)

    src = src or await get_source(pool)
    if not src:
        raise ShipError(
            "no_source",
            "this host has not taken a backup yet — one is sent after its next backup")

    name = object_name(int(src["export_id"]), src["s3_key"], src.get("snapshot_time"))
    dest = destination_key(target)
    log_id = await _begin(pool, int(src["export_id"]), target["kind"], dest)

    try:
        src = await _signed(src)
        if target["kind"] == KIND_MACHINE:
            out = await _ship_to_machine(pool, entity_id, target, src, name, log_id)
            if src.get("sidecar_url"):
                # The backup's own key, next to it: what a restore needs.
                await _ship_to_machine(pool, entity_id, target, {
                    "url": src["sidecar_url"], "sha256": src.get("sidecar_sha256"),
                    "size_bytes": src.get("sidecar_bytes") or 0}, sidecar_name(name), log_id)
        else:
            creds = await _s3_credential(pool, entity_id, target["cred_name"])
            handle = _open_source(src["url"])
            progress: dict = {"sent": 0}
            # Beat once before the first byte: opening the source and starting
            # the multipart upload happen before any part lands, and on a large
            # artifact that alone can outlast the staleness window.
            await _beat(pool, log_id, 0)
            beater = asyncio.create_task(_s3_heartbeat(pool, log_id, progress))
            try:
                out = await asyncio.to_thread(
                    _ship_to_s3_blocking, creds, target, handle, name,
                    int(src["size_bytes"]), src["sha256"], progress)
            except ShipError:
                raise
            except Exception as e:
                raise _classify_boto(e) from None
            finally:
                beater.cancel()
                try:
                    await beater
                except asyncio.CancelledError:
                    # The beater's own cancel, asked for just above. A cancel
                    # of this ship that lands while it waits for the beater
                    # arrives as the same exception, and must go on to the
                    # handler below: cancellation stays cancellation. Dropped,
                    # the ship went on to send the key and record itself done.
                    if asyncio.current_task().cancelling():
                        raise
                except Exception:
                    pass
                finally:
                    try:
                        handle.close()
                    except Exception:
                        pass
            if src.get("sidecar_url"):
                try:
                    handle = _open_source(src["sidecar_url"])
                    try:
                        await asyncio.to_thread(
                            _ship_to_s3_blocking, creds, target, handle, sidecar_name(name),
                            int(src.get("sidecar_bytes") or 0), src.get("sidecar_sha256"), None)
                    finally:
                        handle.close()
                except ShipError:
                    raise
                except Exception as e:
                    raise _classify_boto(e) from None
    except ShipError as e:
        await _finish(pool, log_id, state="failed", sent=0,
                      reason=e.reason, detail=e.detail)
        log.warning("backup ship failed (%s): %s", e.reason, e.detail[:200])
        raise
    except BaseException as e:
        # BaseException, not Exception. A CancelledError — shutdown, a client
        # disconnect, the loop being torn down — is not an Exception, so it
        # slipped past and left the 'running' row holding the lock forever.
        # Clear it and re-raise unchanged: cancellation must stay cancellation.
        await _finish(pool, log_id, state="failed", sent=0,
                      reason="unknown", detail=repr(e)[:400])
        if isinstance(e, Exception):
            log.exception("backup ship failed unexpectedly")
            raise ShipError("unknown", repr(e)) from None
        log.warning("backup ship interrupted: %r", e)
        raise

    await _finish(pool, log_id, state="done", sent=int(out.get("bytes") or 0))
    log.info("backup ship OK: %s -> %s (%s bytes)", name, dest, out.get("bytes"))
    await _drop_staged(src)
    return {"ok": True, "destination": dest, "object": out.get("key"),
            "bytes": int(out.get("bytes") or 0), "export_id": int(src["export_id"])}


async def _drop_staged(src: dict) -> None:
    """A copy staged in platform storage only for this send is dropped once
    the destination holds it. Best effort: the platform drops staged copies
    after a day in any case."""
    if src.get("stored") != "staged":
        return
    from server import host_backup
    for key in (src.get("s3_key"), src.get("sidecar_key")):
        if key:
            try:
                await host_backup.call("backup.drop", {"key": key})
            except Exception as e:  # noqa: BLE001
                log.info("backup ship: staged copy %s not dropped: %s", key, e)


# ── the trigger ──────────────────────────────────────────────────────────────
#
# Until this existed nothing shipped by itself: the only caller of ship_now was
# the button. The docstrings and the tab both implied otherwise, which made them
# false — a user who saved a destination would have got a second copy the first
# time they pressed a button and never again.
#
# Shape follows the other in-pod background workers exactly
# (brain/mastercode/brain/replica_reconciler.py run_replica_reconciler_loop):
# a forever-loop
# dispatched once from server/main.py's lifespan with asyncio.create_task, gated
# on a real entity, catching everything so a transient failure never stops the
# lane and never crashes the lifespan.
#
# WATCHES THE HOST'S OWN RUNS rather than running on a clock of its own. Each
# backup the Host takes (server/host_backup.py) is a new run; this notices one
# it has not already sent to the current destination and sends it. So the cadence is the user's own backup schedule, without this loop
# needing to know anything about schedules.

SHIP_POLL_S = 300


# Backoff after a failure, per (export_id, destination). Without it a refusing
# bucket was retried every SHIP_POLL_S forever, and each attempt READS 16 MiB
# from the source before the destination gets a chance to refuse — 288 attempts
# a day, ~4.6 GB of egress and 288 log rows per user, for a destination that
# is never going to accept. Doubling from 5 minutes to a 12-hour ceiling: quick
# enough that a transient outage recovers within the hour, slow enough that a
# permanently misconfigured bucket costs two attempts a day.
BACKOFF_BASE_S = 300
BACKOFF_MAX_S = 12 * 3600


async def failure_backoff(pool: asyncpg.Pool, export_id: int,
                          destination: str) -> tuple[bool, int]:
    """(may_try_now, seconds_remaining) for this artifact and destination.

    Counts CONSECUTIVE failures — a success in between resets it, because the
    destination demonstrably works and the next failure is a new problem, not a
    continuation of the old one."""
    rows = await pool.fetch(
        "SELECT state, finished_at FROM backup_ship_log "
        "WHERE export_id=$1 AND destination=$2 AND state IN ('done','failed') "
        "ORDER BY started_at DESC LIMIT 12", export_id, destination)
    fails = 0
    last = None
    for r in rows:
        if r["state"] != "failed":
            break
        fails += 1
        if last is None:
            last = r["finished_at"]
    if not fails or last is None:
        return True, 0
    import datetime as _dt
    wait = min(BACKOFF_BASE_S * (2 ** (fails - 1)), BACKOFF_MAX_S)
    age = (_dt.datetime.now(_dt.timezone.utc) - last).total_seconds()
    if age >= wait:
        return True, 0
    return False, int(wait - age)


async def already_shipped(pool: asyncpg.Pool, export_id: int, destination: str) -> bool:
    """Has THIS artifact already reached THIS destination successfully?

    Keyed on (export_id, destination) rather than a timestamp: the export id
    identifies one immutable artifact, so re-running the loop, a pod restart, or
    a re-published URL cannot cause a second upload of bytes that are already
    there. Changing the destination legitimately ships the same artifact again,
    which is why the destination is half the key."""
    row = await pool.fetchrow(
        "SELECT 1 FROM backup_ship_log WHERE export_id=$1 AND destination=$2 "
        "AND state='done' LIMIT 1", export_id, destination)
    return row is not None


async def ship_if_due(pool: asyncpg.Pool, entity_id: int, target: Optional[dict]) -> Optional[dict]:
    """One pass. Returns the ship result, or None when there was nothing to do.

    Everything that is not an error returns None quietly — no destination, no
    artifact, already shipped. This runs every few minutes and the overwhelming
    majority of passes have nothing to do; logging those would bury the ones
    that matter."""
    if not target:
        return None
    try:
        target = validate_target(target)
    except ValueError:
        # A destination saved by an older build with a kind that no longer
        # exists (nfs/smb). Nothing to ship to, and the read path already tells
        # the user their destination needs re-saving.
        return None
    dest = destination_key(target)
    # The undo point a restore left first, then the newest backup: one send
    # per pass.
    for src in (await get_undo_point(pool), await get_source(pool)):
        if not src:
            continue
        if await already_shipped(pool, int(src["export_id"]), dest):
            continue
        ok_now, wait = await failure_backoff(pool, int(src["export_id"]), dest)
        if not ok_now:
            log.debug("backup ship: backing off %ss before retrying %s", wait, dest)
            continue
        return await ship_now(pool, entity_id, target, src=src)
    return None


async def run_backup_ship_loop(pool, entity_id: int) -> None:
    """Ship each new backup artifact to the user's own destination, forever.

    Dispatched once at lifespan as a guarded background task. Cancellation exits
    cleanly; every other error is logged and the loop continues, because a
    destination that is down tonight must not stop the copy that would have
    worked tomorrow."""
    import asyncio
    log.info("backup ship worker online — entity %d, poll %ds", entity_id, SHIP_POLL_S)
    # Clear any lock left by the previous life of this pod before the first
    # pass, or a roll mid-transfer would wedge the lane until the timeout.
    try:
        n = await reap_stale(pool)
        if n:
            log.warning("backup ship: cleared %d stale in-flight row(s) at startup", n)
    except Exception as e:  # noqa: BLE001
        log.warning("backup ship: startup reap failed (non-fatal): %r", e)

    # Phase offset so this does not share a wakeup with the pod's other periodic
    # workers. Capped well under SHIP_POLL_S, so the first poll still lands in
    # the same 300s window it always did — the cadence is untouched.
    from server.loop_splay import splay
    await splay(SHIP_POLL_S, "backup_ship")
    while True:
        try:
            await asyncio.sleep(SHIP_POLL_S)
            from server.routes import backup as backup_routes
            eff = backup_routes._effective(await backup_routes._load_bp(entity_id))
            out = await ship_if_due(pool, entity_id, eff.get("target"))
            if out:
                log.info("backup ship: sent export %s to %s (%d bytes)",
                         out.get("export_id"), out.get("destination"), out.get("bytes"))
        except asyncio.CancelledError:
            return
        except ShipError as e:
            # Already recorded against the attempt, with its class, and already
            # visible in the Backup tab. Info, not a warning: a destination the
            # user has taken offline should not fill the log every five minutes.
            log.info("backup ship: not sent (%s)", e.reason)
        except Exception as e:  # noqa: BLE001
            log.warning("backup ship pass failed (non-fatal): %r", e)
