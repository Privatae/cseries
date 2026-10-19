"""Live viewport — CDP screencast over the control API's own port.

The old viewport was noVNC: Xvfb → x11vnc → websockify on :8831, a
second port, a sister app (browser-novnc) to proxy it, and a stream of
the whole X desktop. It never had a delivery path in the on-demand
harness lane — the harness Service exposes only the control port — and
the desktop-capture model was wrong anyway: the user cares about a
PAGE, not a root window with unmanaged Chromium frames stacked on it.

This module replaces that stack with what browser-infrastructure
products (Browserbase, browserless, Steel) ship as their "live view":
Chromium's own screencast. One WebSocket on the control port carries
JPEG frames out (CDP Page.startScreencast → Page.screencastFrame) and
input back in (CDP Input.dispatchMouseEvent / dispatchKeyEvent /
insertText). Backpressure is built into the protocol: Chromium sends
no new frame until the previous one is acked, and we ack only after
the frame has been written to the client socket — a slow viewer
throttles the stream instead of ballooning a queue.

Wire protocol (JSON text messages):

  client → server
    {"t":"resize","w":1280,"h":800}          match the page viewport to
                                             the client element (clamped)
    {"t":"mouse","type":"down|up|move|wheel", "x":..,"y":..,
     "button":"left|right|middle|none","buttons":0,"clickCount":1,
     "dx":0,"dy":0,"mods":0}
    {"t":"key","type":"down|up","key":"a","code":"KeyA","keyCode":65,
     "text":"a","mods":0,"repeat":false}
    {"t":"text","text":"pasted string"}      clipboard paste → insertText
    {"t":"switch_page","idx":1}              view a different page (popup)
    {"t":"pages"}                            ask for the page list
    {"t":"binary","on":true}                 opt in to binary frames (below)

  server → client
    {"t":"hello","w":..,"h":..,"binary":true}  on connect, current viewport;
                                             "binary": the server CAN send
                                             binary frames once opted in
    {"t":"frame","data":"<b64 jpeg>","w":..,"h":..}   a frame, until the
                                             client opts in (the v0.4.x shape)
    <binary message: raw JPEG bytes>         a frame, after opt-in; the client
                                             reads the dimensions off the image
    {"t":"pages","pages":[{"idx":..,"url":..,"title":..,"viewed":bool}]}
    {"t":"viewport","w":..,"h":..}           a resize was applied
    {"t":"error","detail":".."}

FRAMES ARE BINARY, AND THE STREAM IS PACED. Measured on a
pool node (an identical sidecar, scrolling a
Wikipedia article): the screencast ran at 11.9 fps with each frame a
228 KB base64 string inside a JSON text message — 22 Mbit/s to the
viewer, and ~2.1 cores of CPU: two Chromium processes at ~74% each and
THIS process at ~65%, the Python share being almost entirely
`json.dumps` over a quarter-megabyte string per frame. The harness pod
was capped at 1 core, so the container was throttled in 179 of 604
scheduler periods and the picture arrived late and jerky regardless of
the viewer's link. Sending the JPEG bytes as a binary WebSocket message
removes the base64 inflation (a third of the bytes) and the JSON
encode; holding the screencast ack until the frame interval has elapsed
caps the rate, which bounds both the encode CPU and the bandwidth on a
page that animates. The cap defaults to the measured natural scroll
rate so scrolling looks unchanged and only runaway content is limited.

Binary is OPT-IN from the client, not the default, so this sidecar can
be pinned before the kernel that carries the new viewport.js is rolled:
the app inside a kernel that predates 0.5.1 never sends {"t":"binary"}
and keeps receiving the JSON shape it knows how to paint. The pacing
applies to both shapes.

The stream shows whatever the page shows, passwords included as the
site renders them (dots). That is correct here: these frames go to the
USER driving their own browser, never to a model — the model-facing
image surface stays /screenshot and the session `screenshot` action,
which mask password inputs (see sessions.py).

AUTH: the bearer gate in app.py is an HTTP middleware and Starlette
HTTP middleware DOES NOT RUN for websocket scopes, so this endpoint
enforces the same token itself, from the same env var. The kernel
proxy injects X-Browser-Token on the upstream websocket handshake
(server/applications/proxy.py::serve_app_ws) exactly as it does for
HTTP.

Like sessions.py, this module never imports playwright — everything is
duck-typed through the provider set at startup, so the test suite can
drive the endpoint with fakes and no Chromium.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import secrets
from typing import Any, Callable, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

log = logging.getLogger("cseries-browser.viewport")

router = APIRouter()

# Same variable app.py reads for the HTTP gate. Empty = open, which is
# the loopback-contained in-pod default; the harness deployment sets it.
_CONTROL_TOKEN = os.environ.get("BROWSER_CONTROL_TOKEN", "").strip()

# The shared lane's context + active page, provided by app.py at startup
# (viewport streams the SHARED context — the one the Browser app drives).
_context_provider: Optional[Callable[[], Any]] = None
_page_provider: Optional[Callable[[], Any]] = None

# Screencast bounds. Frames arrive at min(viewport, max) — the max only
# caps memory/bandwidth, it never stretches.
_MAX_W, _MAX_H = 2560, 1600
_MIN_W, _MIN_H = 320, 240
# 60, down from 70: JPEG size falls roughly a third between those two
# settings for text-heavy pages while the picture is still a readable
# screen (the industry live views run 50-70). Overridable per deployment.
JPEG_QUALITY = int(os.environ.get("BROWSER_VIEWPORT_JPEG_QUALITY", "60"))
# Frame-rate cap, enforced by holding the screencast ack (see the module
# docstring). 12 is the natural scroll rate measured on the entity pool,
# so a page being scrolled is not slowed; a video or a spinner is. 0 = no
# cap.
MAX_FPS = int(os.environ.get("BROWSER_VIEWPORT_MAX_FPS", "12"))
_MIN_FRAME_INTERVAL = (1.0 / MAX_FPS) if MAX_FPS > 0 else 0.0


def decode_frame(data: Any) -> bytes:
    """CDP hands the JPEG as base64 text; the wire wants the bytes. Garbage
    (or a fake in tests) decodes to what it can rather than raising in the
    sender task — a bad frame is a dropped paint, not a dead stream."""
    if not isinstance(data, str) or not data:
        return b""
    try:
        return base64.b64decode(data, validate=False)
    except (ValueError, TypeError):
        return b""


def set_providers(context: Callable[[], Any], page: Callable[[], Any]) -> None:
    global _context_provider, _page_provider
    _context_provider = context
    _page_provider = page


def clamp_viewport(w: Any, h: Any) -> Optional[tuple[int, int]]:
    """Client-supplied dimensions → a sane viewport, or None if garbage."""
    try:
        w, h = int(w), int(h)
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    return (max(_MIN_W, min(_MAX_W, w)), max(_MIN_H, min(_MAX_H, h)))


# ── input event → CDP payload (pure, unit-tested) ─────────────────────

_MOUSE_TYPES = {"down": "mousePressed", "up": "mouseReleased",
                "move": "mouseMoved", "wheel": "mouseWheel"}
_BUTTONS = {"left", "right", "middle", "none"}


def mouse_payload(msg: dict) -> Optional[dict]:
    """{"t":"mouse",...} → Input.dispatchMouseEvent params, or None."""
    cdp_type = _MOUSE_TYPES.get(msg.get("type"))
    if cdp_type is None:
        return None
    try:
        x, y = float(msg["x"]), float(msg["y"])
    except (KeyError, TypeError, ValueError):
        return None
    button = msg.get("button") if msg.get("button") in _BUTTONS else "none"
    p: dict = {
        "type": cdp_type,
        "x": x,
        "y": y,
        "button": button,
        "buttons": int(msg.get("buttons") or 0),
        "modifiers": int(msg.get("mods") or 0),
    }
    if cdp_type in ("mousePressed", "mouseReleased"):
        p["clickCount"] = max(1, int(msg.get("clickCount") or 1))
    if cdp_type == "mouseWheel":
        # CDP wheel deltas are DOM-convention: positive deltaY scrolls
        # DOWN. Verified empirically by dispatching
        # raw Input.dispatchMouseEvent against the pinned Chromium —
        # an earlier negation here reversed every scroll.
        p["deltaX"] = float(msg.get("dx") or 0)
        p["deltaY"] = float(msg.get("dy") or 0)
    return p


def key_payload(msg: dict) -> Optional[dict]:
    """{"t":"key",...} → Input.dispatchKeyEvent params, or None.

    A keyDown that carries text produces the char event in the renderer
    (what typing needs); a bare keyDown (arrows, F-keys, chorded
    shortcuts) goes as rawKeyDown so no stray character lands.
    """
    direction = msg.get("type")
    if direction not in ("down", "up"):
        return None
    key = str(msg.get("key") or "")
    code = str(msg.get("code") or "")
    text = msg.get("text")
    mods = int(msg.get("mods") or 0)
    try:
        vk = int(msg.get("keyCode") or 0)
    except (TypeError, ValueError):
        vk = 0
    if direction == "up":
        cdp_type = "keyUp"
    elif isinstance(text, str) and text:
        cdp_type = "keyDown"
    else:
        cdp_type = "rawKeyDown"
    p: dict = {
        "type": cdp_type,
        "key": key,
        "code": code,
        "modifiers": mods,
        "windowsVirtualKeyCode": vk,
        "nativeVirtualKeyCode": vk,
        "autoRepeat": bool(msg.get("repeat")),
    }
    if cdp_type == "keyDown":
        p["text"] = text
        p["unmodifiedText"] = text
    return p


def _ws_token(websocket: WebSocket) -> str:
    presented = websocket.headers.get("x-browser-token") or ""
    if not presented:
        auth = websocket.headers.get("authorization") or ""
        scheme, _, rest = auth.partition(" ")
        if scheme.lower() == "bearer":
            presented = rest.strip()
    return presented


# ── the endpoint ──────────────────────────────────────────────────────


class _Stream:
    """One connection's screencast: a CDP session on the viewed page.

    Chromium's flow control does the pacing — after Page.screencastFrame
    we ack only once the frame is on the client socket, and Chromium
    holds the next frame until then.
    """

    def __init__(self, websocket: WebSocket):
        self.ws = websocket
        self.page: Any = None
        self.cdp: Any = None
        self.frames: asyncio.Queue = asyncio.Queue(maxsize=2)
        self.sender: Optional[asyncio.Task] = None
        # Fires when the VIEWED page closes under us (site JS, reaper) —
        # the endpoint re-attaches to a live page so the canvas doesn't
        # freeze on the last frame until the user happens to send input.
        self.on_page_closed: Optional[Callable[[], None]] = None
        self._close_hook: Optional[tuple[Any, Any]] = None
        # attach() is called from the receive loop AND the page-close
        # hook; unserialised, two interleaved attaches can each detach
        # nothing and leak a CDP session.
        self._attach_lock = asyncio.Lock()
        # Loop time of the last ack — the pacing clock. Chromium captures
        # the next frame only after the ack, so spacing the acks spaces
        # the frames without touching the capture itself.
        self._last_ack = 0.0
        # Frame shape. False = the v0.4.x JSON {"t":"frame"} text message;
        # True = raw JPEG bytes. Flipped by the client's {"t":"binary"}.
        self.binary = False
        # Set by close(). A fallback task created by the close hook in
        # the same few ticks as the client disconnect can otherwise
        # re-attach AFTER teardown — restarting the screencast on a
        # dead connection, acking every frame into the void until the
        # page dies.
        self.finished = False

    async def attach(self, context: Any, page: Any) -> None:
        async with self._attach_lock:
            if self.finished:
                return
            await self._detach_locked()
            self.page = page
            self.cdp = await context.new_cdp_session(page)
            self.cdp.on("Page.screencastFrame", self._on_frame)
            await self.cdp.send("Page.startScreencast", {
                "format": "jpeg",
                "quality": JPEG_QUALITY,
                "maxWidth": _MAX_W,
                "maxHeight": _MAX_H,
                "everyNthFrame": 1,
            })
            # Screencast only produces frames while the target is visible —
            # raise the viewed page above its siblings.
            try:
                await self.cdp.send("Page.bringToFront")
            except Exception:
                pass

            def _closed(_p: Any = None) -> None:
                if self.on_page_closed is not None:
                    self.on_page_closed()
            try:
                page.on("close", _closed)
                self._close_hook = (page, _closed)
            except Exception:
                self._close_hook = None
            if self.sender is None:
                self.sender = asyncio.create_task(self._send_loop())

    def _on_frame(self, params: dict) -> None:
        # Sync CDP event handler: hand off to the sender task. A full
        # queue means the client is behind — drop THIS frame but still
        # ack it, otherwise Chromium stops streaming forever.
        try:
            self.frames.put_nowait(params)
        except asyncio.QueueFull:
            asyncio.get_running_loop().create_task(self._ack(params))

    async def _ack(self, params: dict) -> None:
        try:
            await self.cdp.send("Page.screencastFrameAck",
                                {"sessionId": params["sessionId"]})
        except Exception:
            pass

    async def _send_loop(self) -> None:
        try:
            loop = asyncio.get_running_loop()
            while True:
                params = await self.frames.get()
                if self.binary:
                    raw = decode_frame(params.get("data"))
                    if raw:
                        await self.ws.send_bytes(raw)
                else:
                    meta = params.get("metadata") or {}
                    await self.ws.send_text(json.dumps({
                        "t": "frame",
                        "data": params.get("data"),
                        "w": meta.get("deviceWidth"),
                        "h": meta.get("deviceHeight"),
                    }))
                # Pace BEFORE the ack, never before the send: the frame in
                # hand is already the freshest one Chromium has, so it goes
                # out at once; what the cap governs is how soon the next
                # capture may start.
                if _MIN_FRAME_INTERVAL > 0:
                    wait = self._last_ack + _MIN_FRAME_INTERVAL - loop.time()
                    if wait > 0:
                        await asyncio.sleep(wait)
                self._last_ack = loop.time()
                await self._ack(params)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.debug("frame sender ended: %s", e)

    async def detach(self) -> None:
        async with self._attach_lock:
            await self._detach_locked()

    async def _detach_locked(self) -> None:
        cdp, self.cdp = self.cdp, None
        self.page = None
        if self._close_hook is not None:
            page, hook = self._close_hook
            self._close_hook = None
            try:
                page.remove_listener("close", hook)
            except Exception:
                pass
        if cdp is not None:
            try:
                await cdp.send("Page.stopScreencast")
            except Exception:
                pass
            try:
                await cdp.detach()
            except Exception:
                pass
        # Drain frames queued for the old page (their ack is moot).
        while not self.frames.empty():
            self.frames.get_nowait()

    async def close(self) -> None:
        self.finished = True
        if self.sender is not None:
            self.sender.cancel()
            self.sender = None
        await self.detach()


async def _pages_message(context: Any, viewed: Any) -> dict:
    pages = []
    for idx, p in enumerate(list(context.pages)):
        try:
            title = await p.title()
        except Exception:
            title = ""
        pages.append({
            "idx": idx,
            "url": getattr(p, "url", ""),
            "title": title,
            "viewed": p is viewed,
        })
    return {"t": "pages", "pages": pages}


@router.websocket("/viewport/ws")
async def viewport_ws(websocket: WebSocket) -> None:
    if _CONTROL_TOKEN and not secrets.compare_digest(
            _ws_token(websocket).encode("utf-8"),
            _CONTROL_TOKEN.encode("utf-8")):
        # Same posture as the HTTP gate: absent and wrong are one answer.
        log.warning("viewport: rejected an unauthenticated connection")
        await websocket.close(code=1008)
        return

    context = _context_provider() if _context_provider else None
    page = _page_provider() if _page_provider else None
    if context is None or page is None:
        await websocket.accept()
        await websocket.send_text(json.dumps(
            {"t": "error", "detail": "chromium not ready"}))
        await websocket.close(code=1011)
        return

    await websocket.accept()
    stream = _Stream(websocket)
    loop = asyncio.get_running_loop()

    # A popup (OAuth login, window.open) lands as a new page in the
    # shared context — tell the client so it can offer/auto-switch.
    def _on_new_page(_p: Any) -> None:
        async def notify():
            try:
                await websocket.send_text(json.dumps(
                    await _pages_message(context, stream.page)))
            except Exception:
                pass
        loop.create_task(notify())

    context.on("page", _on_new_page)

    # Re-attach to a live page when the viewed one closes. Used from the
    # receive loop AND from the page-close hook, so an idle viewer's
    # canvas recovers without waiting for their next input.
    async def _fallback_to_live_page() -> bool:
        fallback = _page_provider() if _page_provider else None
        try:
            if fallback is None or fallback.is_closed():
                live = [p for p in context.pages if not p.is_closed()]
                fallback = live[0] if live else None
        except Exception:
            fallback = None
        if fallback is None:
            try:
                await websocket.send_text(json.dumps(
                    {"t": "error", "detail": "no live page"}))
            except Exception:
                pass
            return False
        await stream.attach(context, fallback)
        try:
            await websocket.send_text(json.dumps(
                await _pages_message(context, fallback)))
        except Exception:
            pass
        return True

    def _viewed_page_closed() -> None:
        loop.create_task(_fallback_to_live_page())

    stream.on_page_closed = _viewed_page_closed

    try:
        vp = page.viewport_size or {"width": 1440, "height": 900}
        # "binary": this sidecar can send frames as binary messages once
        # the client asks ({"t":"binary","on":true}); until then frames are
        # the JSON shape every earlier client paints.
        await websocket.send_text(json.dumps(
            {"t": "hello", "w": vp["width"], "h": vp["height"], "binary": True}))
        await stream.attach(context, page)
        await websocket.send_text(json.dumps(
            await _pages_message(context, page)))

        while True:
            try:
                raw = await websocket.receive_text()
            except WebSocketDisconnect:
                break
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                continue
            t = msg.get("t")

            # Belt to the close-hook's braces: if the viewed page is
            # somehow closed and the hook didn't fire, recover before
            # acting on it.
            if stream.page is not None and getattr(stream.page, "is_closed", None):
                try:
                    closed = stream.page.is_closed()
                except Exception:
                    closed = False
                if closed and not await _fallback_to_live_page():
                    break

            if t == "mouse":
                p = mouse_payload(msg)
                if p is not None and stream.cdp is not None:
                    try:
                        await stream.cdp.send("Input.dispatchMouseEvent", p)
                    except Exception as e:
                        log.debug("mouse dispatch failed: %s", e)
            elif t == "key":
                p = key_payload(msg)
                if p is not None and stream.cdp is not None:
                    try:
                        await stream.cdp.send("Input.dispatchKeyEvent", p)
                    except Exception as e:
                        log.debug("key dispatch failed: %s", e)
            elif t == "text":
                text = msg.get("text")
                if isinstance(text, str) and text and stream.cdp is not None:
                    try:
                        await stream.cdp.send("Input.insertText", {"text": text})
                    except Exception as e:
                        log.debug("insertText failed: %s", e)
            elif t == "resize":
                vp = clamp_viewport(msg.get("w"), msg.get("h"))
                if vp is not None and stream.page is not None:
                    try:
                        await stream.page.set_viewport_size(
                            {"width": vp[0], "height": vp[1]})
                        await websocket.send_text(json.dumps(
                            {"t": "viewport", "w": vp[0], "h": vp[1]}))
                    except Exception as e:
                        log.debug("resize failed: %s", e)
            elif t == "switch_page":
                try:
                    idx = int(msg.get("idx"))
                except (TypeError, ValueError):
                    continue
                pages = list(context.pages)
                if 0 <= idx < len(pages) and not pages[idx].is_closed():
                    await stream.attach(context, pages[idx])
                    await websocket.send_text(json.dumps(
                        await _pages_message(context, pages[idx])))
            elif t == "pages":
                await websocket.send_text(json.dumps(
                    await _pages_message(context, stream.page)))
            elif t == "binary":
                stream.binary = bool(msg.get("on", True))
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning("viewport connection ended: %s", e)
    finally:
        stream.on_page_closed = None
        try:
            context.remove_listener("page", _on_new_page)
        except Exception:
            pass
        await stream.close()
