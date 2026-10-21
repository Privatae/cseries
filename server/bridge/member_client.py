"""The Host's connection to the network's member door, net.privatae.ai:8643.

A Host that holds a passport (server/bridge/passport.py) keeps one mTLS
connection to the member door. It is a BridgeClient with three differences,
each a decision the network's design settled:

  * It verifies the door. The only trust anchor is the network's root
    certificate (root.crt in the passport directory, the same certificate the
    image ships at etc/ceigas_root_v2.crt), the door's name is checked, and
    the name sent is net.privatae.ai whatever address it is reached on.
  * Nothing can be pushed in. register_handler keeps nothing, and any request
    the door sends (a device.dispatch or anything else with an "op") is
    answered {"ok": false, "error": "unsupported", "code": 400} and never
    reaches a handler. The door does not send such frames; this is what
    happens if one ever arrives.
  * Only the network's own ops leave the Host. The rest of the Host calls the
    platform through this object exactly as it would a cloud bridge, and on a
    standalone Host every one of those calls already answers "not connected".
    An op the network does not offer still answers that way here, without
    sending anything: memory, inference and the like stay on the Host.

When the door refuses a connection it first sends one line,
{"ok": false, "error": <text>, "code": 403, "reason": <code>}, then closes.
That line carries no id, even when a request was in flight, so every request
still waiting gets the same refusal as its answer. Only this frame carries a
"reason"; the door's ordinary errors never do. The client passes the reason
and the text to its owner (membership.py), which decides what happens to the
passport, and waits REFUSED_RETRY_S before trying again unless told to stop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Awaitable, Callable, Optional

from cryptography import x509

from server.bridge import bridge_client as _bc
from server.bridge.bridge_client import BridgeClient

logger = logging.getLogger("cseries.member_client")

NETWORK_DOOR_HOST = "net.privatae.ai"
NETWORK_DOOR_PORT = 8643

# The member door's op table (phase 1): the App Store and community ops a Host
# uses, and the door's own three. bundle.upload_part carries a large bundle for
# appstore.publish_bundle (bridge_client.stage_bundle_params). The door leaves
# out appstore.yank_version until the store checks that the caller published
# the app, and appstore.checkout.create and appstore.refund because it holds no
# Stripe key in phase 1; so they are answered here and never sent.
#
# The backup ops carry the Host's encrypted backups to the network's storage
# (server/host_backup.py): the door scopes each to the Host's own folder.
# backup.recovery_key is left out until the door answers it: it would hand the
# Host a recovery key its owner never registered through the network.
NETWORK_OPS: frozenset[str] = frozenset({
    "heartbeat.ping", "passport.renew", "passport.revoke",
    "appstore.list", "appstore.icon", "appstore.fetch_bundle", "appstore.purchase",
    "appstore.refund.request", "appstore.entitlement.check",
    "appstore.agreement", "appstore.publisher.my_apps", "appstore.publisher.sales",
    "appstore.publisher.set_price", "appstore.publisher.set_refund_policy",
    "appstore.publisher.refund_requests", "appstore.publish_bundle",
    "appstore.publish_requests", "appstore.circle_roster", "bundle.upload_part",
    "community.categories", "community.threads", "community.thread", "community.post",
    "community.vote", "community.flag", "community.search", "community.digest",
    "community.set_proposal_ref", "community.group.join",
    "rendezvous.list", "rendezvous.withdraw",
    "backup.upload.start", "backup.upload.part", "backup.upload.complete",
    "backup.upload.abort", "backup.put_url", "backup.list", "backup.get_url",
    "backup.drop", "backup.report",
})

# The reasons the door gives when it refuses or drops a connection.
REFUSAL_REASONS: frozenset[str] = frozenset({
    "revoked", "conflict", "membership", "unknown", "mismatch", "refused"})

# After a refusal the door will say the same thing again until something
# changes on the account page, so the next try waits this long.
REFUSED_RETRY_S = 900.0

UNSUPPORTED = {"ok": False, "error": "unsupported", "code": 400}

RefusalHandler = Callable[[str, str, Optional[str]], Awaitable[None]]


class MemberClient(BridgeClient):
    """Persistent mTLS connection to the member door, outbound only."""

    network_member = True
    # HOST_IP is a cluster node's address; it has nothing to do with the door.
    _prefer_host_ip = False

    def __init__(self, passport, *, on_refusal: Optional[RefusalHandler] = None,
                 on_accepted: Optional[Callable[[], None]] = None,
                 host: str = NETWORK_DOOR_HOST, port: int = NETWORK_DOOR_PORT):
        super().__init__(host=host, port=port, cert=passport.chain_path, key=passport.key_path,
                         ca=passport.root_path, server_hostname=NETWORK_DOOR_HOST,
                         verify_hostname=True)
        self._on_refusal = on_refusal
        # Called when the door first answers a heartbeat on a connection, so an
        # earlier refusal stops being shown once the door lets the Host in.
        self._on_accepted = on_accepted
        self._handler_note_logged = False
        # Serial of the certificate the current connection was made with, so a
        # refusal can be matched to the passport it was about.
        self._conn_serial: Optional[str] = None
        self._refused: Optional[tuple[str, str]] = None
        self._reconnect_now = False
        self._wake: Optional[asyncio.Event] = None
        self._refusal_tasks: set = set()

    # ── nothing inbound ─────────────────────────────────────────────────────

    def register_handler(self, action: str, fn) -> None:
        if not self._handler_note_logged:
            logger.info("network member: inbound handlers are not kept (first: %s); "
                        "the member door sends nothing to run", action)
            self._handler_note_logged = True

    async def _handle_dispatch(self, params: dict):
        await self._answer_unsupported({})

    async def _answer_unsupported(self, msg: dict) -> None:
        reply = dict(UNSUPPORTED)
        if isinstance(msg, dict) and "id" in msg:
            reply["id"] = msg["id"]
        try:
            await self._send(reply)
        except Exception as e:
            logger.debug("could not answer an unsolicited frame: %s", e)

    async def _subscribe_memory_config(self) -> None:
        # The cloud client subscribes to config pushes; nothing is pushed here.
        return None

    # ── only the network's ops leave ────────────────────────────────────────

    @staticmethod
    def _not_offered(op: str) -> dict:
        return {"ok": False, "error": f"not offered by the network: {op}",
                "error_kind": "infra"}

    async def request_full(self, op: str, params: dict = None) -> dict:
        if op not in NETWORK_OPS:
            return self._not_offered(op)
        return await super().request_full(op, params)

    async def request_stream(self, op: str, params: dict, on_delta) -> dict:
        if op not in NETWORK_OPS:
            return self._not_offered(op)
        return await super().request_stream(op, params, on_delta)

    @property
    def status(self) -> dict:
        st = super().status
        st["network_member"] = True
        return st

    # ── connection ──────────────────────────────────────────────────────────

    def _event(self) -> asyncio.Event:
        if self._wake is None:
            self._wake = asyncio.Event()
        return self._wake

    async def _pause(self, seconds: float) -> None:
        """Sleep, or return early when reconnect() or a lost connection wakes us."""
        ev = self._event()
        try:
            await asyncio.wait_for(ev.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
        finally:
            ev.clear()

    def _build_context(self):
        ctx = super()._build_context()
        self._conn_serial = None
        if ctx is not None:
            try:
                leaf = x509.load_pem_x509_certificates(self._cert.read_bytes())[0]
                self._conn_serial = format(leaf.serial_number, "x")
            except (OSError, ValueError, IndexError):
                pass
        return ctx

    async def start(self):
        """Run the connection loop until stop(). A fresh TLS context is built
        for every connection, so a renewed passport is used from the next one."""
        self._running = True
        while self._running:
            self._refused = None
            self._reconnect_now = False
            try:
                ctx = self._build_context()
                if ctx is None:
                    logger.warning("network member: passport files are gone; not connecting")
                    self._running = False
                    break
                await self._connect(ctx)
                self._reader_task = asyncio.create_task(self._reader_loop())
                await self._heartbeat_loop()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("member door connection lost: %s", e)
            finally:
                self._connected = False
                if self._reader_task and not self._reader_task.done():
                    self._reader_task.cancel()
                    try:
                        await self._reader_task
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        pass
                self._reader_task = None
                if self._writer is not None:
                    try:
                        self._writer.close()
                    except Exception:
                        pass

            # The reader sets the wake event when its socket ends, to cut the
            # heartbeat loop's pause short. When a heartbeat was in flight
            # instead (always so for a refusal at connect time) nothing has
            # consumed it, and the wait below would return at once: a refused
            # Host redialled hundreds of times a second. stop() and
            # reconnect() are read from their flags just below, so clearing
            # here loses neither.
            if self._wake is not None:
                self._wake.clear()
            if not self._running:
                break
            if self._reconnect_now:
                continue
            if self._refused:
                await self._pause(REFUSED_RETRY_S)
            else:
                await self._pause(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, _bc.RECONNECT_MAX)

    async def stop(self):
        await super().stop()
        if self._wake is not None:
            self._wake.set()

    async def reconnect(self) -> None:
        """Drop the connection and dial again at once (after a renewal)."""
        self._reconnect_now = True
        self._connected = False
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:
                pass
        self._event().set()

    async def _heartbeat_loop(self):
        """The cloud loop's liveness rule (HEARTBEAT_MISS_LIMIT misses in a row
        and the connection is dead) with an empty ping: the door knows who is
        calling from the certificate, and nothing else is reported."""
        misses = 0
        accepted = False
        while self._running and self._connected:
            try:
                result = await asyncio.wait_for(self.request("heartbeat.ping", {}),
                                                timeout=_bc.REQUEST_TIMEOUT + 5)
                if result is not None:
                    self._last_heartbeat = time.time()
                    misses = 0
                    if not accepted:
                        accepted = True
                        if self._on_accepted is not None:
                            try:
                                self._on_accepted()
                            except Exception as e:
                                logger.debug("on_accepted: %s", e)
                elif self._connected:
                    misses += 1
                    logger.warning("member door heartbeat miss %d/%d",
                                   misses, _bc.HEARTBEAT_MISS_LIMIT)
                    if misses >= _bc.HEARTBEAT_MISS_LIMIT:
                        self._connected = False
                        break
            except Exception as e:
                logger.warning("member door heartbeat failed (%s) — reconnecting", e)
                self._connected = False
                break
            if not self._connected:
                break
            await self._pause(_bc.HEARTBEAT_INTERVAL)

    async def _reader_loop(self):
        """Responses go to their waiters; a refusal frame goes to the owner;
        any request from the door is answered unsupported."""
        try:
            while self._connected and self._reader:
                line = await self._reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    logger.warning("member door sent a malformed line; ignored")
                    continue
                if not isinstance(msg, dict):
                    continue

                if "chunk" in msg and "ok" not in msg:
                    q = self._streams.get(msg.get("id"))
                    if q is not None:
                        q.put_nowait(msg.get("chunk") or "")
                    continue

                if "ok" in msg:
                    rid = msg.get("id")
                    if rid is not None and rid in self._pending:
                        self._streams.pop(rid, None)
                        fut = self._pending.pop(rid)
                        if not fut.done():
                            fut.set_result(msg)
                    if msg.get("ok") is False and msg.get("reason") in REFUSAL_REASONS:
                        self._refusal(str(msg["reason"]), str(msg.get("error") or ""))
                        self._refuse_pending(msg)
                        break
                    continue

                if "op" in msg:
                    logger.warning("member door sent a request (%s); answered unsupported",
                                   str(msg.get("op"))[:80])
                    await self._answer_unsupported(msg)
                    continue

                logger.warning("member door sent an unrecognised frame: %s", list(msg)[:8])
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("member door reader ended: %s", e)
        finally:
            self._connected = False
            # Nothing more will arrive on this socket: let every waiter return
            # now rather than at its timeout.
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(ConnectionError("member door connection closed"))
            if self._wake is not None:
                self._wake.set()

    def _refuse_pending(self, frame: dict) -> None:
        """The refusal is the answer to every request still waiting on this
        connection: the frame has no id, and the door closes after it."""
        for rid, fut in list(self._pending.items()):
            if not fut.done():
                fut.set_result({"ok": False, "error": frame.get("error"),
                                "code": frame.get("code"), "reason": frame.get("reason"),
                                "id": rid})

    def _refusal(self, reason: str, error: str) -> None:
        logger.warning("member door refused this Host (%s): %s", reason, error)
        self._refused = (reason, error)
        self._connected = False
        if self._on_refusal is None:
            return
        # Run apart from the reader: the owner may stop this client, and that
        # cancels the reader task.
        t = asyncio.get_running_loop().create_task(
            self._on_refusal(reason, error, self._conn_serial))
        self._refusal_tasks.add(t)
        t.add_done_callback(self._refusal_tasks.discard)
