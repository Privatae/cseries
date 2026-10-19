"""The Discord gateway, held by the Host: WebSocket /api/connector/gateway.

Discord delivers a bot's direct messages over its Gateway, a WebSocket, and
the bot token travels INSIDE that socket: in the Identify payload (op 2,
d.token) and the Resume payload (op 6, d.token)
(https://docs.discord.com/developers/events/gateway). A connector app is never
given a token (nac_connector.py: the token stays in Keys and the kernel makes
the platform call), so the Host holds the connection:

  connector app  --ws-->  this route  --wss-->  gateway.discord.gg

and relays text frames both ways. On a frame from the app whose op is 2 or 6,
the kernel fills d.token with the grant's bot token, opened by the vault broker
(authz.use_credential action ws_token_frame, the same token lookup the
platform call makes: build_platform_request's name). The app sends a fixed
placeholder there (TOKEN_PLACEHOLDER). The token goes into no other frame,
into no log line, and never back to the app.

What the kernel decides, and nothing more:
  - the upstream host: gateway.discord.gg, or the resume_gateway_url READY
    gave (the app passes it as ?url=), and only when that host is discord.gg
    or a subdomain of it; anything else is refused before the socket opens;
  - the query: v=10&encoding=json, no compression, whatever the app asked;
  - the frame size: FRAME_MAX_BYTES each way;
  - the send rate: SEND_LIMIT frames per SEND_WINDOW_S per connection,
    Discord's own;
  - one socket per connector app: a second replaces the first;
  - a revoke closes it at once (nac_connector's revoke hook, the one that
    cancels the connector's turns).
Heartbeats, sequence numbers, sessions and resuming are the app's: the kernel
reads no Discord state beyond the op of a frame from the app.

Discord, read 2026-10-05 at https://docs.discord.com/developers/events/gateway:
  "Apps can send 120 gateway events per connection every 60 seconds, meaning
  an average of 2 commands per second." (SEND_LIMIT / SEND_WINDOW_S)
  A sent payload "Must not exceed 4096 bytes. If an event payload does exceed
  4096 bytes, the connection will be closed with a 4002 close event code."
  The connection URL takes v, encoding (json or etf) and an optional compress.
  READY's resume_gateway_url is "a WebSocket URL that your app should use
  when it Resumes after a disconnect."
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import logging
import re
import time
import urllib.request
from typing import Any, Optional
from urllib.parse import urlsplit

log = logging.getLogger("privatae.connectors.gateway")

GATEWAY_HOST = "gateway.discord.gg"
GATEWAY_DOMAIN = "discord.gg"
GATEWAY_QUERY = "v=10&encoding=json"
TOKEN_PLACEHOLDER = "cseries-connector-token"
TOKEN_OPS = (2, 6)                  # Identify, Resume
SEND_FRAME_MAX = 4096               # app -> Discord: Discord's own limit
# Discord -> app. Discord does not state a size for what it sends; a READY or
# a GUILD_CREATE grows with the servers the bot is in, and this connector's
# bot is in the person's own server or none. 2 MiB holds those with room and
# bounds what the kernel buffers for one frame.
RECV_FRAME_MAX = 2 * 1024 * 1024
FRAME_MAX_BYTES = {"send": SEND_FRAME_MAX, "recv": RECV_FRAME_MAX}
SEND_LIMIT = 120
SEND_WINDOW_S = 60.0
CONNECT_TIMEOUT_S = 30.0
# How often an open socket reads its capability again. /platform reads it on
# every call; a socket is one long call, so it reads it on a clock and before
# every frame the token goes into. A revoke closes the socket at once through
# the hook (close_for_revoke); this catches every other way the capability
# stops holding, none of which runs the hook: a pairing declined or run out
# (decline_connector, the code's ten minutes), the app no longer installed and
# active, its manifest no longer a Discord connector's, its grant's signature
# failing (nac_connector.check_connector_cap).
CAP_RECHECK_S = 5.0

# WebSocket close codes the kernel uses toward the app (RFC 6455 7.4: 4000-4999
# are the application's). Discord's own codes (4000-4014) pass through as
# Discord sent them.
CLOSE_REVOKED = 4401                # the grant was revoked
CLOSE_REPLACED = 4409               # another connection of this app took over
CLOSE_TOO_BIG = 4413                # a frame over the size limit
CLOSE_RATE = 4429                   # past Discord's send limit
CLOSE_UPSTREAM = 4502               # Discord could not be reached, or went away
# The capability could not be read again (the database): try again shortly,
# as the route's own check answers a failed read (1013), not a revoke.
CLOSE_UNREAD = 1013
_UNREAD_REASON = "your Host could not read this connector's grant; try again shortly"


_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class Refused(Exception):
    def __init__(self, code: int, reason: str):
        super().__init__(reason)
        self.code, self.reason = code, reason


def upstream_url(resume_url: Optional[str]) -> str:
    """The wss URL the kernel opens: gateway.discord.gg, or the resume URL's
    host when it is discord.gg or a subdomain of it, always with the fixed
    query and path. Refused (1008) for any other host or scheme."""
    if not resume_url:
        return f"wss://{GATEWAY_HOST}/?{GATEWAY_QUERY}"
    try:
        u = urlsplit(str(resume_url))
        host = (u.hostname or "").lower().rstrip(".")
        port = u.port
    except ValueError:
        raise Refused(1008, "the resume URL is not a URL") from None
    if u.scheme != "wss":
        raise Refused(1008, "the resume URL must be wss")
    if not host or not (host == GATEWAY_DOMAIN or host.endswith("." + GATEWAY_DOMAIN)):
        raise Refused(1008, f"the resume URL's host must be {GATEWAY_DOMAIN} or a subdomain of it")
    # Every label a DNS name's (letters, digits, hyphens), so the name that
    # ends in discord.gg is the name the connection goes to. A NUL in it
    # reached getaddrinfo, which reads a C string and stops there:
    # "evil.example\x00.discord.gg" opened TCP to evil.example.
    if len(host) > 253 or not all(_DNS_LABEL.match(lb) for lb in host.split(".")):
        raise Refused(1008, "the resume URL's host is not a DNS name")
    if port not in (None, 443) or u.username or u.password:
        raise Refused(1008, "the resume URL must be on port 443 with no user")
    return f"wss://{host}/?{GATEWAY_QUERY}"


def _proxy_for(url: str) -> Optional[str]:
    """The egress proxy a wss URL goes through, as the vault broker's
    trust_env calls go (HTTPS_PROXY, NO_PROXY). aiohttp's trust_env reads a
    WSS_PROXY for a wss URL, which the Host's pod does not set, so the https
    one is taken here."""
    host = urlsplit(url).hostname or ""
    proxies = urllib.request.getproxies()
    p = proxies.get("wss") or proxies.get("https")
    if not p or urllib.request.proxy_bypass(host):
        return None
    return p


class Link:
    """One app's relay: the app's socket and Discord's."""

    def __init__(self, app_id: str, grant_id: Optional[int]):
        self.app_id, self.grant_id = app_id, grant_id
        self.closed = asyncio.Event()
        self.close_code: Optional[int] = None
        self.close_reason = ""
        self.app_closed = False
        self.sent: collections.deque = collections.deque()

    def end(self, code: int, reason: str) -> None:
        if not self.closed.is_set():
            self.close_code, self.close_reason = code, reason
            self.closed.set()

    def may_send(self, now: Optional[float] = None) -> bool:
        now = time.monotonic() if now is None else now
        while self.sent and now - self.sent[0] >= SEND_WINDOW_S:
            self.sent.popleft()
        if len(self.sent) >= SEND_LIMIT:
            return False
        self.sent.append(now)
        return True


_LINKS: dict[str, Link] = {}


def link_of(app_id: str) -> Optional[Link]:
    return _LINKS.get(app_id)


async def close_for_revoke(*, app_id: str, grant_id: Optional[int], reason: str) -> int:
    """nac_connector's revoke hook: the app's gateway socket is closed at
    once, whatever grant it was opened under (the capability that opened it
    is what the revoke took away)."""
    link = _LINKS.get(app_id)
    if link is None:
        return 0
    link.end(CLOSE_REVOKED, "revoked")
    log.info("connector %s: gateway closed by revoke (%s)", app_id, reason)
    return 1


def frame_op(text: str) -> tuple[Optional[dict], Any]:
    """(the parsed frame, its op) for a frame from the app, or (None, None)."""
    try:
        obj = json.loads(text)
    except ValueError:
        return None, None
    if not isinstance(obj, dict):
        return None, None
    return obj, obj.get("op")


async def relay(ws, caller, *, resume_url: Optional[str], pool, recheck) -> None:
    """Run one app's gateway connection to its end. `ws` is the app's
    WebSocket (Starlette), not yet accepted; `caller` the ConnectorCaller the
    route's capability check returned; `recheck` an async callable that runs
    that same check again now (the same capability, at the same stages) and
    returns its ConnectorCaller, or None when it no longer passes."""
    import aiohttp
    from server import brain_service
    from server.vault import authz

    url = upstream_url(resume_url)            # Refused before anything opens
    name = caller.token_name
    if not name:
        raise Refused(1008, "this connector's grant names no token in Keys")

    async def still_holds() -> Optional[bool]:
        """True while the capability still passes, for this platform and the
        token this socket puts into its frames; False once it does not; None
        when it could not be read. Anything but True closes the socket: a
        failed read refuses, as the route's check does."""
        try:
            again = await recheck()
        except Exception as e:  # noqa: BLE001 — named by its type only
            log.warning("connector %s: gateway capability re-read failed, closing (%s)",
                        caller.app_id, type(e).__name__)
            return None
        return (again is not None and again.platform == caller.platform
                and again.token_name == name)

    def end_unheld(held: Optional[bool]) -> None:
        if held is None:
            link.end(CLOSE_UNREAD, _UNREAD_REASON)
        else:
            log.info("connector %s: gateway closed, its capability no longer holds",
                     caller.app_id)
            link.end(CLOSE_REVOKED, "connector capability required")

    link = Link(caller.app_id, caller.grant_id)
    old = _LINKS.get(caller.app_id)
    _LINKS[caller.app_id] = link
    if old is not None:
        old.end(CLOSE_REPLACED, "another connection of this app took over")
    # Read again once the link is where the revoke hook finds it: a revoke
    # that committed after the route's check and before this line ran its
    # hook on no link, and this read sees it.
    held = await still_holds()
    if not held:
        if _LINKS.get(caller.app_id) is link:
            _LINKS.pop(caller.app_id, None)
        raise (Refused(CLOSE_UNREAD, _UNREAD_REASON) if held is None
               else Refused(1008, "connector capability required"))
    host = urlsplit(url).hostname or GATEWAY_HOST
    await ws.accept()
    session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None,
                                                                  sock_connect=CONNECT_TIMEOUT_S))
    up = None
    try:
        try:
            up = await session.ws_connect(url, proxy=_proxy_for(url), autoping=True,
                                          max_msg_size=RECV_FRAME_MAX, compress=0,
                                          timeout=aiohttp.ClientWSTimeout(ws_close=10.0))
        except Exception as e:  # noqa: BLE001 — named by its type only
            log.info("connector %s: gateway to %s failed (%s)", caller.app_id, host,
                     type(e).__name__)
            link.end(CLOSE_UPSTREAM, f"{host} could not be reached ({type(e).__name__})")
            return

        async def to_discord() -> None:
            from starlette.websockets import WebSocketDisconnect
            while True:
                try:
                    msg = await ws.receive()
                except (WebSocketDisconnect, RuntimeError):
                    link.end(1000, "the app closed")
                    return
                kind = msg.get("type")
                if kind == "websocket.disconnect":
                    code = msg.get("code") or 1000
                    # The app's own close code goes on to Discord: a client
                    # that wants to resume closes with something other than
                    # 1000/1001, which end the session.
                    link.end(int(code) if 1000 <= int(code) < 5000 else 1000, "the app closed")
                    return
                text = msg.get("text")
                if text is None:
                    continue               # json encoding: text frames only
                if len(text.encode("utf-8")) > SEND_FRAME_MAX:
                    link.end(CLOSE_TOO_BIG, f"a frame over {SEND_FRAME_MAX} bytes")
                    return
                if not link.may_send():
                    link.end(CLOSE_RATE, f"over {SEND_LIMIT} frames in {int(SEND_WINDOW_S)} s")
                    return
                obj, op = frame_op(text)
                if obj is not None and op in TOKEN_OPS and isinstance(obj.get("d"), dict):
                    # The token goes only under a capability that holds now,
                    # as /platform reads it for every call.
                    held = await still_holds()
                    if not held:
                        end_unheld(held)
                        return
                    try:
                        await authz.use_credential(
                            pool, entity_id=int(brain_service._entity_id), name=name,
                            action="ws_token_frame",
                            request={"frame": obj, "host": host, "op": op,
                                     "max_bytes": SEND_FRAME_MAX},
                            actor=f"connector:{caller.app_id}", frame_send=up.send_str)
                    except authz.VaultAuthzError as e:
                        link.end(1008, f"the token could not be sent: {e}")
                        return
                    continue
                await up.send_str(text)

        async def to_app() -> None:
            async for m in up:
                if m.type == aiohttp.WSMsgType.TEXT:
                    await ws.send_text(m.data)
                elif m.type == aiohttp.WSMsgType.ERROR:
                    break
            code = up.close_code
            link.end(int(code) if code and 1000 <= int(code) < 5000 else CLOSE_UPSTREAM,
                     "Discord closed the connection")

        async def watch() -> None:
            # Ends only with the link: closed by something else, or ended
            # here when the capability no longer holds.
            while not link.closed.is_set():
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(link.closed.wait(), CAP_RECHECK_S)
                    return
                held = await still_holds()
                if not held:
                    end_unheld(held)
                    return

        tasks = [asyncio.create_task(to_discord(), name=f"gw-out-{caller.app_id}"),
                 asyncio.create_task(to_app(), name=f"gw-in-{caller.app_id}"),
                 asyncio.create_task(link.closed.wait(), name=f"gw-end-{caller.app_id}"),
                 asyncio.create_task(watch(), name=f"gw-cap-{caller.app_id}")]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if link.close_code is None:
                link.end(CLOSE_UPSTREAM, "the relay ended")
            # Both sockets are closed while their readers still read, so each
            # close is a clean handshake with its code; the readers then end
            # on their own, and only what is left is cancelled.
            await _close_both(link, up, ws)
        finally:
            for t in tasks:
                t.cancel()
            for t in tasks:
                with contextlib.suppress(BaseException):
                    await t
    finally:
        if _LINKS.get(caller.app_id) is link:
            _LINKS.pop(caller.app_id, None)
        await _close_both(link, up, ws)
        with contextlib.suppress(Exception):
            await session.close()


async def _close_both(link: "Link", up, ws) -> None:
    code = link.close_code or CLOSE_UPSTREAM
    if up is not None and not up.closed:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(up.close(code=upstream_close_code(code)), 12)
    if not link.app_closed:
        link.app_closed = True
        with contextlib.suppress(Exception):
            await ws.close(code=app_close_code(code), reason=link.close_reason[:120])


def app_close_code(code: int) -> int:
    """A code that may go in a close frame to the app (RFC 6455 7.4.1: 1005,
    1006 and 1015 never do)."""
    return code if 1000 <= code < 5000 and code not in (1004, 1005, 1006, 1015) else 1011


def upstream_close_code(code: int) -> int:
    """The code the kernel closes Discord's socket with. Discord: closing with
    1000 or 1001 invalidates the session and the bot shows offline; any other
    code leaves it resumable. A revoke ends the session (1000). Any other
    close the kernel makes (a replaced, oversized or over-rate connection, or
    Discord gone) leaves it resumable (4000), so the app's next connection can
    Resume. The app's own close code goes on as it sent it."""
    if code == CLOSE_REVOKED:
        return 1000
    if code in (CLOSE_REPLACED, CLOSE_TOO_BIG, CLOSE_RATE, CLOSE_UPSTREAM, CLOSE_UNREAD):
        return 4000
    # The app went away without a close frame (1006), or with a code no close
    # frame may carry (RFC 6455 7.4.1): it will be back, so keep the session.
    if not 1000 <= code < 5000 or code in (1004, 1005, 1006, 1015):
        return 4000
    return code
