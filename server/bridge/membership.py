"""Network membership for a standalone Host: join, stay, renew, leave.

The Host runs on its owner's machine. Joining the network gives it a passport
(server/bridge/passport.py) and one outbound connection to the member door
(server/bridge/member_client.py); the rest of the Host reaches the network
through server.main.get_bridge_client() exactly as it reached nothing before.

  Join   The owner types the one-time code from their account page
         (ACCOUNT_NETWORK_URL) in Settings -> Network. The Host makes a key,
         dials the enrolment door
         (enrol.privatae.ai on public port 8643; the edge routes the name to
         the door), verifies it against the network's root certificate, sends
         passport.enrol with a CSR, writes the files and starts the client.
         The door's refusals ("this code is not valid", ...) are shown to the
         owner word for word.
  Stay   The member client keeps the connection. When the door refuses this
         Host, the reason decides: revoked, conflict or unknown delete the
         passport (it is no longer any use, and a copy elsewhere is what a
         conflict means); membership keeps it and says "membership not active";
         mismatch and refused keep it and show the door's words.
  Renew  At start and every hour: once renew_after has passed, a new key, a CSR
         with an empty subject, passport.renew, the files, and a reconnect with
         the new certificate. The old key is deleted only after the new
         certificate is on disk. The door keeps the new certificate as pending
         and the old one as current until a connection presents the new one,
         and that connection promotes it. So an answer that never arrives
         (a timeout, a dropped connection, a stop before the files are
         written) costs nothing: the staged key is dropped, the passport the
         Host has is still the current one, and the next hourly check renews
         again. Nothing about a lost answer deletes the passport.
  Leave  passport.revoke on the open connection, then the files are deleted
         and the Host goes back to having no platform.
  Claim  On a C-Series Cloud machine whose owner's membership comes with the
         machine, the account site sends a code with "Set up your machine",
         and the machine hands it to the Host with the claim
         (CSERIES_MACHINE_DIR/member-enrol.json). Once the claim has made the
         owner, and the owner is the email the code came with, the Host joins
         with it through the same join as Settings -> Network, once
         (join_from_claim). Each code is tried once, whatever the answer, but
         for a network that could not be reached. A Host on a person's own
         computer has no such directory, and none of this runs there.

The Host's own entity id (1, the id inside its own database) does not change.
The network's id for it is the passport's entity_id: shown in Settings and
never sent in a request, since the door knows the Host from its certificate.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import logging
import os
import ssl
from pathlib import Path
from typing import Callable, Optional

from server.bridge import passport as pp
from server.bridge.member_client import (MemberClient, NETWORK_DOOR_HOST, NETWORK_DOOR_PORT,
                                         REFUSAL_REASONS)
from server.bridge.no_platform import NoPlatform

logger = logging.getLogger("cseries.membership")

# Where the owner makes a code and sees and removes their Hosts. The account
# site serves the page here; its /network only redirects to the homepage.
ACCOUNT_NETWORK_URL = "https://privatae.ai/account/network"

ENROL_HOST = "enrol.privatae.ai"
# The edge serves the enrolment name on the same public port as the member
# door and routes by name; the door's own enrolment port is not public.
ENROL_PORT = 8643
ENROL_TIMEOUT_S = 30.0

# The network's root certificate, shipped in the image (etc/ceigas_root_v2.crt
# in the source). The only certificate the Host trusts for either door.
ROOT_CERT = Path(os.environ.get("CSERIES_NETWORK_ROOT_CERT",
                                "/etc/cseries/keys/ceigas_root_v2.crt"))

RENEW_CHECK_S = 3600.0
# Renewal due but the door not reachable yet: look again sooner than hourly.
RENEW_RETRY_S = 300.0

# Seam for the tests that read how long the renewal loop waits.
_sleep = asyncio.sleep

# The Cloud claim's code (module docstring, Claim). Looked for every 30 s while
# the Host waits for its owner or the network, else at the hourly check. Codes
# tried are kept by SHA-256 in the passport directory (which leave and restore
# keep: passport.delete takes only the passport's own files), the newest few.
CLAIM_POLL_S = 30.0
CLAIM_TRIED = "claim-codes.json"
CLAIM_TRIED_KEEP = 16

DELETE_ON: frozenset[str] = frozenset({"revoked", "conflict", "unknown"})
MEMBERSHIP_NOT_ACTIVE = "membership not active"


class MembershipError(Exception):
    """A join or leave that cannot go ahead; `status` is the HTTP answer."""
    status = 400

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.message = message
        if status is not None:
            self.status = status


class BadCode(MembershipError):
    status = 400


class NetworkUnreachable(MembershipError):
    status = 502


class EnrolmentRefused(MembershipError):
    """The door said no. `message` is its text, shown as it is."""

    def __init__(self, message: str, code):
        try:
            code = int(code)
        except (TypeError, ValueError):
            code = 502
        super().__init__(message, code if 400 <= code < 500 else 502)
        self.code = code


def _client_version() -> str:
    try:
        from server.routes.system import _load_build_info
        return str(_load_build_info().get("tag") or "unknown")
    except Exception:
        return "unknown"


def _enrol_context(anchor: Path) -> ssl.SSLContext:
    """Server authentication only: the network's root as the one anchor, and
    the door's name checked."""
    if not anchor.is_file():
        logger.warning("enrolment: no root certificate at %s", anchor)
        raise NetworkUnreachable(
            "this Host has no copy of the network's root certificate, so it cannot "
            "check that it is talking to the network", 503)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_verify_locations(cafile=str(anchor))
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


async def enrol(code: str, *, directory: Path, anchor: Path, host_name: Optional[str] = None,
                host: str = ENROL_HOST, port: int = ENROL_PORT,
                timeout: float = ENROL_TIMEOUT_S) -> pp.Passport:
    """One passport.enrol round trip, then the files. Raises BadCode,
    NetworkUnreachable or EnrolmentRefused; on any of them nothing is written."""
    canonical = pp.canonical_code(code)
    if not pp.valid_code(canonical):
        raise BadCode("a code is 12 letters and digits, shown as XXXX-XXXX-XXXX")
    ctx = _enrol_context(Path(anchor))
    key = pp.new_key()
    request = {"op": "passport.enrol",
               "params": {"code": canonical, "csr_pem": pp.csr_for(key),
                          "host_name": host_name or None,
                          "client": {"product": "cseries", "version": _client_version()}}}

    async def _round_trip() -> bytes:
        reader, writer = await asyncio.open_connection(
            host, port, ssl=ctx, server_hostname=ENROL_HOST, limit=1024 * 1024)
        try:
            writer.write(json.dumps(request).encode() + b"\n")
            await writer.drain()
            return await reader.readline()
        finally:
            try:
                writer.close()
            except Exception:
                pass

    try:
        line = await asyncio.wait_for(_round_trip(), timeout=timeout)
    except ssl.SSLCertVerificationError as e:
        raise NetworkUnreachable(
            f"the enrolment door did not prove it is {ENROL_HOST}: "
            f"{e.verify_message or e}") from None
    except (OSError, asyncio.TimeoutError, ssl.SSLError) as e:
        raise NetworkUnreachable(
            f"could not reach the network: {str(e) or type(e).__name__}") from None
    try:
        answer = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        answer = None
    if not isinstance(answer, dict):
        raise NetworkUnreachable("the enrolment door gave no answer")
    if not answer.get("ok"):
        raise EnrolmentRefused(str(answer.get("error") or "enrolment refused"),
                               answer.get("code"))
    data = answer.get("data") or {}
    try:
        info = pp.check_issued(data, key, enrolment=True)
    except pp.PassportError as e:
        raise NetworkUnreachable(f"the network's answer could not be used: {e}") from None
    root = Path(anchor).read_bytes()
    if str(data.get("root_pem") or "").strip() != root.decode("ascii", "replace").strip():
        # root.crt is the anchor this Host verified the door with, never a
        # root the door names.
        logger.warning("enrolment: the door named a root other than the one this Host "
                       "trusts; keeping this Host's own")
    try:
        return pp.save_enrolment(directory, key=key, cert_pem=data["cert_pem"],
                                 chain_pem=data["chain_pem"], root_pem=root, info=info)
    except (OSError, pp.PassportError) as e:
        # The door has already used the code and recorded the Host, so the
        # owner needs to hear what happened and what to do, and no half
        # passport may stay behind.
        logger.warning("enrolment: the passport could not be saved: %s", e)
        try:
            pp.delete(Path(directory))
        except OSError:
            pass
        raise MembershipError(
            f"the network issued this Host a passport, but it could not be saved here ({e}). "
            f"Remove the Host on your account page ({ACCOUNT_NETWORK_URL}), then join again "
            "with a new code.",
            500) from None


class Membership:
    """This Host's membership and the client that goes with it."""

    def __init__(self, *, directory: Optional[Path] = None, anchor: Optional[Path] = None,
                 enrol_host: str = ENROL_HOST, enrol_port: int = ENROL_PORT,
                 door_host: str = NETWORK_DOOR_HOST, door_port: int = NETWORK_DOOR_PORT,
                 set_client: Optional[Callable] = None,
                 clock: Optional[Callable[[], dt.datetime]] = None,
                 owner_email: Optional[Callable[[], Optional[str]]] = None):
        self.dir = Path(directory or pp.PASSPORT_DIR)
        self.anchor = Path(anchor or ROOT_CERT)
        self.enrol_host, self.enrol_port = enrol_host, enrol_port
        self.door_host, self.door_port = door_host, door_port
        self._set_client = set_client or (lambda c: None)
        self._clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
        self._owner_email = owner_email or _owner_email
        self.passport: Optional[pp.Passport] = None
        self.client = None
        self.last_error: Optional[str] = None
        self.reason: Optional[str] = None
        self.renew_note: Optional[str] = None
        self._lock = asyncio.Lock()
        self._client_task: Optional[asyncio.Task] = None
        self._renew_task: Optional[asyncio.Task] = None
        self._claim_task: Optional[asyncio.Task] = None

    # ── lifecycle ───────────────────────────────────────────────────────────

    async def boot(self):
        """At start: the member client when a passport is on disk, else a
        NoPlatform as on any standalone Host. Returns the client in use."""
        self._start_claim_pickup()
        p = pp.load(self.dir)
        if p is None:
            client = NoPlatform()
            self._set_client(client)
            return client
        # load() has already finished any renewal whose certificate reached the
        # disk, so a host.key.next still here is from a renewal whose answer
        # never did. The door still holds this passport as current; the staged
        # key is of no use.
        pp.discard_next_key(self.dir)
        return self._start(p)

    def _start_claim_pickup(self) -> None:
        """The Cloud claim's code (join_from_claim): on a cloud machine only."""
        from server import cloud_machine
        if not cloud_machine.machine_dir():
            return
        if self._claim_task is None or self._claim_task.done():
            self._claim_task = asyncio.get_running_loop().create_task(self._claim_loop())

    def _start(self, p: pp.Passport):
        self.passport = p
        self.client = MemberClient(p, on_refusal=self._on_refusal, on_accepted=self._on_accepted,
                                   host=self.door_host, port=self.door_port)
        self._client_task = asyncio.get_running_loop().create_task(self.client.start())
        self._set_client(self.client)
        if self._renew_task is None or self._renew_task.done():
            self._renew_task = asyncio.get_running_loop().create_task(self._renew_loop())
        logger.info("network member: entity %s on %s, passport valid until %s",
                    p.entity_id, p.network, p.not_after.isoformat())
        return self.client

    async def _stop_client(self) -> None:
        client, task = self.client, self._client_task
        self.client, self._client_task = None, None
        if client is not None:
            try:
                await client.stop()
            except Exception as e:
                logger.debug("member client stop: %s", e)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._set_client(NoPlatform())

    async def shutdown(self) -> None:
        if self._claim_task is not None and self._claim_task is not asyncio.current_task():
            self._claim_task.cancel()
            try:
                await self._claim_task
            except (asyncio.CancelledError, Exception):
                pass
        self._claim_task = None
        if self._renew_task is not None and self._renew_task is not asyncio.current_task():
            self._renew_task.cancel()
            try:
                await self._renew_task
            except (asyncio.CancelledError, Exception):
                pass
        self._renew_task = None
        client, task = self.client, self._client_task
        self.client, self._client_task = None, None
        if client is not None:
            try:
                await client.stop()
            except Exception:
                pass
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def _drop(self) -> None:
        """Back to no platform, with the passport files gone."""
        await self._stop_client()
        if self._renew_task is not None and self._renew_task is not asyncio.current_task():
            self._renew_task.cancel()
        self._renew_task = None
        pp.delete(self.dir)
        self.passport = None

    # ── join and leave ──────────────────────────────────────────────────────

    async def join(self, code: str, host_name: Optional[str] = None) -> dict:
        if self._lock.locked():
            raise MembershipError("a join or leave is already under way", 409)
        async with self._lock:
            if self.passport is not None:
                raise MembershipError("this Host is already a network member; leave first", 409)
            try:
                p = await enrol(code, directory=self.dir, anchor=self.anchor,
                                host_name=host_name, host=self.enrol_host, port=self.enrol_port)
            except BadCode:
                raise
            except MembershipError as e:
                self.last_error, self.reason = e.message, None
                raise
            self.last_error = self.reason = self.renew_note = None
            self._start(p)
            return self.status()

    async def leave(self) -> dict:
        if self._lock.locked():
            raise MembershipError("a join or leave is already under way", 409)
        async with self._lock:
            if self.passport is None:
                raise MembershipError("this Host is not a network member", 409)
            p, client = self.passport, self.client
            revoked, note = False, None
            if client is not None and client.connected:
                env = await client.request_full("passport.revoke", {})
                revoked = bool(env.get("ok"))
                if not revoked:
                    note = str(env.get("error") or "the network did not answer")
            else:
                note = "the network could not be reached"
            await self._drop()
            self.last_error = self.reason = self.renew_note = None
            out = self.status()
            out["revoked"] = revoked
            if not revoked and self._expired(p):
                out["note"] = ("This Host has left. Its passport had already expired, so the "
                               "network was not told; remove the Host on your account page "
                               f"({ACCOUNT_NETWORK_URL}).")
            elif not revoked:
                out["note"] = (f"This Host has left, but the network was not told ({note}). "
                               f"Its passport stays valid until {p.not_after.isoformat()} "
                               f"unless you remove the Host on your account page "
                               f"({ACCOUNT_NETWORK_URL}).")
            return out

    def _expired(self, p: pp.Passport) -> bool:
        return self._clock() >= p.not_after

    # ── the Cloud claim's code ──────────────────────────────────────────────

    def _claim_tried(self) -> list[str]:
        try:
            doc = json.loads((self.dir / CLAIM_TRIED).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        tried = doc.get("tried") if isinstance(doc, dict) else None
        return [h for h in tried if isinstance(h, str)] if isinstance(tried, list) else []

    def _note_claim_tried(self, digest: str) -> None:
        tried = [h for h in self._claim_tried() if h != digest] + [digest]
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / f".{CLAIM_TRIED}.tmp"
        tmp.write_text(json.dumps({"tried": tried[-CLAIM_TRIED_KEEP:]}) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.dir / CLAIM_TRIED)

    async def join_from_claim(self) -> str:
        """Join with the code the machine handed over with the claim (module
        docstring, Claim), through join(), as Settings -> Network does.
        Answers what happened, in a word or two; raises nothing."""
        from server import cloud_machine
        if not cloud_machine.machine_dir():
            return "not a cloud machine"
        doc = cloud_machine.member_enrol()
        if doc is None:
            return "no code"
        code = pp.canonical_code(doc.get("code") or "")
        email = str(doc.get("email") or "").strip().lower()
        until = doc.get("until")
        if not pp.valid_code(code) or not email or isinstance(until, bool) \
                or not isinstance(until, (int, float)):
            return "no code"
        if self._clock().timestamp() >= until:
            return "expired"
        digest = hashlib.sha256(code.encode("ascii")).hexdigest()
        if digest in self._claim_tried():
            return "tried"
        if self.passport is not None:
            return "member"
        owner = (self._owner_email() or "").strip().lower()
        if not owner:
            return "no owner"
        if owner != email:
            # A code for another account than the Host's owner never joins it.
            logger.warning("network: the claim's code is for another account than this "
                           "Host's owner; not used")
            self._note_claim_tried(digest)
            return "not the owner"
        try:
            await self.join(code)
        except (NetworkUnreachable, EnrolmentRefused) as e:
            if isinstance(e, NetworkUnreachable) or e.code == 429 or e.code >= 500:
                # The door was not reached, or could not answer now: the code
                # is still good, so the next look tries it again.
                logger.info("network: joining with the claim's code waits: %s", e.message)
                return "waiting"
            self._note_claim_tried(digest)
            logger.warning("network: the claim's code was refused: %s", e.message)
            return "refused"
        except MembershipError as e:
            if e.status == 409 and self.passport is None:
                return "waiting"          # a join or leave of the owner's is under way
            self._note_claim_tried(digest)
            if self.passport is not None:
                return "member"
            logger.warning("network: the claim's code could not be used: %s", e.message)
            return "refused"
        self._note_claim_tried(digest)
        logger.info("network: joined with the claim's code")
        return "joined"

    async def _claim_loop(self) -> None:
        while True:
            wait = RENEW_CHECK_S
            try:
                outcome = await self.join_from_claim()
                if outcome in ("no owner", "waiting") or (
                        outcome == "no code" and not (self._owner_email() or "")):
                    # A box before its claim, or a code the network could not
                    # take yet: look again soon.
                    wait = CLAIM_POLL_S
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("network: the claim's code could not be read: %s", e)
                wait = CLAIM_POLL_S
            await _sleep(wait)

    # ── renewal ─────────────────────────────────────────────────────────────

    async def renew_if_due(self) -> str:
        p, client = self.passport, self.client
        if p is None or client is None:
            return "not a member"
        if self._clock() <= p.renew_after:
            return "not due"
        if not client.connected:
            return "not connected"
        if pp.staged_key_in_use(self.dir):
            # An earlier renewal's certificate is on disk with its key still
            # staged, because the swap could not be made. That key is the only
            # one for it: finish the swap and use that passport (the door holds
            # it as pending); never stage over it.
            finished = pp.load(self.dir)
            if finished is None or finished.serial == p.serial:
                self.renew_note = ("the renewed passport could not be written yet; "
                                   "this Host tries again shortly")
                return "failed"
            self.passport, self.renew_note = finished, None
            logger.info("passport renewal finished: serial %s", finished.serial)
            await client.reconnect()
            return "renewed"
        key = pp.new_key()
        pp.stage_next_key(self.dir, key)
        env = await client.request_full("passport.renew", {"csr_pem": pp.csr_for(key)})
        if not env.get("ok"):
            # Nothing was written, and the door changes nothing current on a
            # renewal: the passport on disk is still the one it accepts.
            pp.discard_next_key(self.dir)
            if env.get("reason") in REFUSAL_REASONS:
                return "refused"          # _on_refusal has it
            if env.get("code") == 429:
                self.renew_note = str(env.get("error") or "too early to renew")
                return "too early"
            if env.get("error_kind") == "infra":
                # Timed out, or the connection went: the answer is lost. A
                # later renewal replaces whatever the door kept for it.
                self.renew_note = (
                    f"The passport renewal got no answer ({env.get('error') or 'no answer'}). "
                    "This Host keeps its passport, which is still valid, and renews at the "
                    "next hourly check.")
                return "no answer"
            self.renew_note = f"renewal refused: {env.get('error') or 'no answer'}"
            return "failed"
        data = env.get("data") or {}
        try:
            info = pp.check_issued(data, key, enrolment=False)
        except pp.PassportError as e:
            pp.discard_next_key(self.dir)
            self.renew_note = f"the renewed passport could not be used: {e}"
            return "failed"
        root = self.anchor if self.anchor.is_file() else p.root_path
        try:
            renewed = pp.save_renewal(self.dir, cert_pem=data["cert_pem"],
                                      chain_pem=data["chain_pem"], root_pem=root.read_bytes(),
                                      info=info, previous=p)
        except (OSError, pp.PassportError) as e:
            # The door holds the new certificate as pending and the old one as
            # current, so either works for the next connection. load() pairs
            # the newest certificate on disk with its key; when that is still
            # the old one, the staged key is of no use.
            logger.warning("renewed passport not fully written: %s", e)
            renewed = pp.load(self.dir)
            if renewed is None or renewed.serial == p.serial:
                if renewed is not None:
                    pp.discard_next_key(self.dir)
                self.renew_note = f"the renewed passport could not be written: {e}"
                return "failed"
        self.passport = renewed
        self.renew_note = None
        logger.info("passport renewed: serial %s, valid until %s",
                    renewed.serial, renewed.not_after.isoformat())
        await client.reconnect()
        return "renewed"

    async def _renew_loop(self) -> None:
        # A lost answer ("no answer") and a 429 wait for the next hourly check.
        while True:
            wait = RENEW_CHECK_S
            try:
                outcome = await self.renew_if_due()
                if outcome in ("not connected", "failed"):
                    wait = RENEW_RETRY_S
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("passport renewal check failed: %s", e)
                wait = RENEW_RETRY_S
            await _sleep(wait)

    # ── refusals ────────────────────────────────────────────────────────────

    def _on_accepted(self) -> None:
        self.reason = self.last_error = None

    async def _on_refusal(self, reason: str, error: str, serial: Optional[str]) -> None:
        p = self.passport
        if p is None:
            return
        if serial is not None and serial != p.serial:
            # The connection was made with the certificate before a renewal;
            # the client is already dialling again with the new one.
            logger.info("refusal (%s) on a connection with an earlier certificate; ignored",
                        reason)
            return
        if reason in DELETE_ON:
            async with self._lock:
                if self.passport is not p:
                    return            # left or replaced meanwhile
                logger.warning("passport deleted after the door refused it (%s): %s",
                               reason, error)
                self.reason, self.last_error = reason, error or reason
                await self._drop()
            return
        self.reason = reason
        self.last_error = MEMBERSHIP_NOT_ACTIVE if reason == "membership" else (error or reason)

    # ── status ──────────────────────────────────────────────────────────────

    def status(self) -> dict:
        p, client = self.passport, self.client
        out = {"member": p is not None,
               "connected": bool(client is not None and client.connected),
               "last_error": self.last_error, "reason": self.reason,
               "renew_note": self.renew_note}
        if p is not None:
            expired = self._expired(p)
            out.update(entity_id=p.entity_id, host_uuid=p.host_uuid, network=p.network,
                       door=p.door, not_after=p.not_after.isoformat(),
                       renew_after=p.renew_after.isoformat(), enrolled_at=p.enrolled_at,
                       expired=expired)
            # The door refuses an expired passport during the handshake, before
            # it can send a reason, so without this the owner sees only "Not
            # connected" for as long as they look.
            if expired and not out["last_error"]:
                out["last_error"] = (
                    f"This Host's passport expired on {p.not_after.isoformat()}. Leave the "
                    "network here, then join again with a new code from your account page "
                    f"({ACCOUNT_NETWORK_URL}).")
        return out


def _owner_email() -> Optional[str]:
    """The Host's owner, as the sign-in knows it (server/owner_login.py)."""
    from server import owner_login
    return owner_login.owner_email()


_current: Optional[Membership] = None


def current() -> Membership:
    """The process's Membership (made on first use, so the routes work before
    boot as well)."""
    global _current
    if _current is None:
        _current = Membership()
    return _current


async def boot(set_client: Callable):
    """Called once from the lifespan with the setter for get_bridge_client()."""
    m = current()
    m._set_client = set_client
    return await m.boot()


async def shutdown() -> None:
    if _current is not None:
        await _current.shutdown()
