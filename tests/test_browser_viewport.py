"""Tests for sidecars/browser/control/viewport.py — the live viewport.

Runs without Chromium, like the sessions suite: viewport.py never
imports playwright, so fakes for the context / page / CDP session are
enough to exercise the real control flow — the token gate (which the
HTTP middleware can NOT provide, because Starlette HTTP middleware
never runs for websocket scopes), frame relay + ack ordering, input
dispatch, and page switching.

Run: pytest tests/test_browser_viewport.py
"""
import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

_SIDECAR = Path(__file__).resolve().parents[1] / "sidecars" / "browser"
sys.path.insert(0, str(_SIDECAR))

from control import viewport  # noqa: E402


# ── pure mappers ─────────────────────────────────────────────────────


def test_mouse_down_maps_to_mouse_pressed():
    p = viewport.mouse_payload({"type": "down", "x": 10, "y": 20,
                                "button": "left", "buttons": 1,
                                "clickCount": 2, "mods": 8})
    assert p == {"type": "mousePressed", "x": 10.0, "y": 20.0,
                 "button": "left", "buttons": 1, "modifiers": 8,
                 "clickCount": 2}


def test_mouse_wheel_passes_dom_deltas_through():
    """CDP wheel deltas are DOM-convention (positive deltaY scrolls
    down) — verified against real Chromium after
    a negation here shipped every scroll reversed. Do not 'fix' the
    sign without dispatching a raw CDP wheel at a live page first."""
    p = viewport.mouse_payload({"type": "wheel", "x": 1, "y": 1,
                                "dx": 3, "dy": 120})
    assert p["deltaX"] == 3.0 and p["deltaY"] == 120.0


def test_mouse_garbage_is_refused_not_raised():
    assert viewport.mouse_payload({"type": "down"}) is None
    assert viewport.mouse_payload({"type": "teleport", "x": 1, "y": 1}) is None
    assert viewport.mouse_payload({"type": "down", "x": "NaNish", "y": 1}) is None


def test_unknown_button_becomes_none_not_passthrough():
    p = viewport.mouse_payload({"type": "move", "x": 1, "y": 1,
                                "button": "back"})
    assert p["button"] == "none"


def test_key_down_with_text_is_key_down():
    p = viewport.key_payload({"type": "down", "key": "a", "code": "KeyA",
                              "keyCode": 65, "text": "a"})
    assert p["type"] == "keyDown"
    assert p["text"] == "a" and p["unmodifiedText"] == "a"
    assert p["windowsVirtualKeyCode"] == 65


def test_key_down_without_text_is_raw_key_down():
    """Arrows / chords must not produce a character in the page."""
    p = viewport.key_payload({"type": "down", "key": "ArrowLeft",
                              "code": "ArrowLeft", "keyCode": 37, "text": ""})
    assert p["type"] == "rawKeyDown"
    assert "text" not in p


def test_key_up_is_key_up_even_with_text():
    p = viewport.key_payload({"type": "up", "key": "a", "code": "KeyA",
                              "keyCode": 65, "text": "a"})
    assert p["type"] == "keyUp"
    assert "text" not in p


def test_key_garbage_is_refused_not_raised():
    assert viewport.key_payload({"type": "sideways", "key": "a"}) is None


def test_clamp_viewport_bounds_and_garbage():
    assert viewport.clamp_viewport(1280, 800) == (1280, 800)
    assert viewport.clamp_viewport(10, 99999) == (320, 1600)
    assert viewport.clamp_viewport(99999, 10) == (2560, 240)
    assert viewport.clamp_viewport("x", 100) is None
    assert viewport.clamp_viewport(-5, 100) is None
    assert viewport.clamp_viewport(None, None) is None


# ── fakes ────────────────────────────────────────────────────────────


class FakeCDP:
    """Records every send; emits one screencast frame on start."""

    def __init__(self):
        self.sent = []
        self.handlers = {}
        self.detached = False

    def on(self, event, handler):
        self.handlers[event] = handler

    async def send(self, method, params=None):
        self.sent.append((method, params or {}))
        if method == "Page.startScreencast":
            self.handlers["Page.screencastFrame"]({
                "data": "ZmFrZWpwZWc=",
                "sessionId": 7,
                "metadata": {"deviceWidth": 1440, "deviceHeight": 900},
            })

    async def detach(self):
        self.detached = True


class FakePage:
    def __init__(self, url="about:blank"):
        self.url = url
        self.viewport_size = {"width": 1440, "height": 900}
        self.resized_to = None
        self._closed = False
        self.listeners = {}

    async def title(self):
        return "fake page"

    def is_closed(self):
        return self._closed

    async def set_viewport_size(self, size):
        self.resized_to = size
        self.viewport_size = size

    def on(self, event, handler):
        self.listeners.setdefault(event, []).append(handler)

    def remove_listener(self, event, handler):
        if handler in self.listeners.get(event, []):
            self.listeners[event].remove(handler)


class FakeContext:
    def __init__(self, pages):
        self.pages = pages
        self.cdp_sessions = []
        self.listeners = {}

    async def new_cdp_session(self, page):
        cdp = FakeCDP()
        cdp.page = page
        self.cdp_sessions.append(cdp)
        return cdp

    def on(self, event, handler):
        self.listeners[event] = handler

    def remove_listener(self, event, handler):
        self.listeners.pop(event, None)


@pytest.fixture()
def harness(monkeypatch):
    page = FakePage()
    context = FakeContext([page])
    monkeypatch.setattr(viewport, "_context_provider", lambda: context)
    monkeypatch.setattr(viewport, "_page_provider", lambda: page)
    # The endpoint tests are about relay + ack ORDER, not timing; the
    # pacing clock has its own test below.
    monkeypatch.setattr(viewport, "_MIN_FRAME_INTERVAL", 0.0)
    app = FastAPI()
    app.include_router(viewport.router)
    return TestClient(app), context, page


def _recv(ws):
    """One message as the client sees it: a dict for JSON text, bytes for a
    binary frame. Frames are binary, and they
    ride the sender task, so any JSON read may be preceded by one."""
    m = ws.receive()
    if m.get("bytes") is not None:
        return m["bytes"]
    return json.loads(m["text"])


def _recv_json(ws, limit=10):
    for _ in range(limit):
        m = _recv(ws)
        if isinstance(m, dict):
            return m
    raise AssertionError(f"no JSON message in the first {limit}")


def _recv_until(ws, t, limit=10):
    for _ in range(limit):
        m = _recv(ws)
        if isinstance(m, dict) and m["t"] == t:
            return m
    raise AssertionError(f"no {t!r} message in the first {limit}")


# ── the endpoint ─────────────────────────────────────────────────────


def test_connect_streams_hello_pages_and_a_json_frame_by_default(harness):
    """A client that never opts in — the viewport.js inside any kernel that
    predates app 0.5.1 — still gets the JSON frame it knows how to paint.
    That is what lets this sidecar be pinned before the kernel rolls."""
    client, context, page = harness
    with client.websocket_connect("/viewport/ws") as ws:
        hello = json.loads(ws.receive_text())
        assert hello == {"t": "hello", "w": 1440, "h": 900, "binary": True}
        # frame + pages arrive concurrently (the frame rides the sender
        # task) — gather both without assuming an order.
        seen = {}
        while not {"frame", "pages"} <= seen.keys():
            m = _recv(ws)
            assert isinstance(m, dict), "binary frame without opt-in"
            seen[m["t"]] = m
        assert seen["frame"]["data"] == "ZmFrZWpwZWc="
        assert seen["frame"]["w"] == 1440
        assert seen["pages"]["pages"][0]["viewed"] is True
    cdp = context.cdp_sessions[0]
    methods = [m for m, _ in cdp.sent]
    assert "Page.startScreencast" in methods
    assert ("Page.startScreencast", {
        "format": "jpeg", "quality": viewport.JPEG_QUALITY,
        "maxWidth": 2560, "maxHeight": 1600, "everyNthFrame": 1}) in cdp.sent
    # The frame was ACKED — without the ack Chromium sends nothing more.
    assert ("Page.screencastFrameAck", {"sessionId": 7}) in cdp.sent


def test_opted_in_client_gets_binary_frames(harness):
    """After {"t":"binary"} the frame is the decoded JPEG bytes in a BINARY
    message, never base64 in JSON: measured, the JSON shape
    cost a third more bytes and ~0.65 core of json.dumps per stream."""
    client, context, page = harness
    with client.websocket_connect("/viewport/ws") as ws:
        ws.receive_text()  # hello
        ws.send_text(json.dumps({"t": "binary", "on": True}))
        # The first screencast frame may already be in flight as JSON;
        # switching page starts a fresh screencast on the new shape.
        popup = FakePage(url="https://accounts.example/oauth")
        context.pages.append(popup)
        ws.send_text(json.dumps({"t": "switch_page", "idx": 1}))
        frame = None
        for _ in range(10):
            m = _recv(ws)
            if isinstance(m, bytes):
                frame = m
                break
        assert frame == b"fakejpeg"
        # A round-trip the server answers, so the sender task's ack (which
        # follows the send) has run before the socket is torn down.
        ws.send_text(json.dumps({"t": "pages"}))
        _recv_until(ws, "pages")
    assert ("Page.screencastFrameAck", {"sessionId": 7}) in context.cdp_sessions[-1].sent


def test_binary_opt_in_can_be_switched_off_again(harness):
    client, context, _ = harness
    with client.websocket_connect("/viewport/ws") as ws:
        ws.receive_text()  # hello
        ws.send_text(json.dumps({"t": "binary", "on": True}))
        ws.send_text(json.dumps({"t": "binary", "on": False}))
        popup = FakePage(url="https://accounts.example/oauth")
        context.pages.append(popup)
        ws.send_text(json.dumps({"t": "switch_page", "idx": 1}))
        kinds = [type(_recv(ws)) for _ in range(4)]
    assert bytes not in kinds


def test_decode_frame_never_raises():
    assert viewport.decode_frame("ZmFrZWpwZWc=") == b"fakejpeg"
    assert viewport.decode_frame("") == b""
    assert viewport.decode_frame(None) == b""
    assert viewport.decode_frame(123) == b""


def test_frames_are_paced_by_holding_the_ack(monkeypatch):
    """The cap works by delaying the ACK, not the send: Chromium captures
    the next frame only once the previous one is acked, so acks spaced at
    the interval space the frames — and the frame in hand still goes out
    immediately. Two queued frames must produce two sends at once and a
    second ack no sooner than the interval after the first."""
    import asyncio

    monkeypatch.setattr(viewport, "_MIN_FRAME_INTERVAL", 0.15)

    class WS:
        def __init__(self):
            self.sent = []

        async def send_bytes(self, b):
            self.sent.append((asyncio.get_running_loop().time(), b))

    class CDP:
        def __init__(self):
            self.acks = []

        async def send(self, method, params=None):
            if method == "Page.screencastFrameAck":
                self.acks.append((asyncio.get_running_loop().time(), params))

    async def scenario():
        ws, cdp = WS(), CDP()
        stream = viewport._Stream(ws)
        stream.cdp = cdp
        stream.binary = True
        for sid in (1, 2):
            stream._on_frame({"data": "ZmFrZWpwZWc=", "sessionId": sid})
        stream.sender = asyncio.create_task(stream._send_loop())
        await asyncio.sleep(0.4)
        stream.sender.cancel()
        return ws, cdp

    ws, cdp = asyncio.run(scenario())
    assert [b for _, b in ws.sent] == [b"fakejpeg", b"fakejpeg"]
    assert [p["sessionId"] for _, p in cdp.acks] == [1, 2]
    assert cdp.acks[1][0] - cdp.acks[0][0] >= 0.15 - 0.01
    # the SECOND send waited for the first ack (queue order), but never for
    # its own pacing window — the send precedes the wait
    assert ws.sent[1][0] <= cdp.acks[1][0]


def test_mouse_and_key_and_paste_reach_cdp(harness):
    client, context, _ = harness
    with client.websocket_connect("/viewport/ws") as ws:
        ws.receive_text()  # hello
        ws.send_text(json.dumps({"t": "mouse", "type": "down", "x": 5, "y": 6,
                                 "button": "left", "clickCount": 1}))
        ws.send_text(json.dumps({"t": "key", "type": "down", "key": "a",
                                 "code": "KeyA", "keyCode": 65, "text": "a"}))
        ws.send_text(json.dumps({"t": "text", "text": "pasted"}))
        # A message the server answers, so the sends above are processed.
        ws.send_text(json.dumps({"t": "pages"}))
        _recv_until(ws, "pages")
    sent = context.cdp_sessions[0].sent
    assert ("Input.dispatchMouseEvent",
            viewport.mouse_payload({"type": "down", "x": 5, "y": 6,
                                    "button": "left", "clickCount": 1})) in sent
    kinds = [m for m, _ in sent]
    assert "Input.dispatchKeyEvent" in kinds
    assert ("Input.insertText", {"text": "pasted"}) in sent


def test_resize_clamps_and_applies(harness):
    client, _, page = harness
    with client.websocket_connect("/viewport/ws") as ws:
        ws.receive_text()  # hello
        ws.send_text(json.dumps({"t": "resize", "w": 100000, "h": 50}))
        m = _recv_until(ws, "viewport")
        assert m == {"t": "viewport", "w": 2560, "h": 240}
    assert page.resized_to == {"width": 2560, "height": 240}


def test_switch_page_moves_the_screencast(harness):
    client, context, _ = harness
    popup = FakePage(url="https://accounts.example/oauth")
    context.pages.append(popup)
    with client.websocket_connect("/viewport/ws") as ws:
        ws.receive_text()  # hello
        ws.send_text(json.dumps({"t": "switch_page", "idx": 1}))
        for _ in range(10):
            m = _recv(ws)
            if isinstance(m, dict) and m["t"] == "pages" and m["pages"][1]["viewed"]:
                break
        else:
            raise AssertionError("switch_page never reported the popup as viewed")
    # The first CDP session (shared page) was stopped + detached; the
    # second one targets the popup.
    first, second = context.cdp_sessions
    assert first.detached
    assert ("Page.stopScreencast", {}) in first.sent
    assert second.page is popup


def test_attach_hooks_page_close_and_detach_unhooks(harness):
    """The close hook is how an idle viewer's canvas recovers when the
    viewed page dies — attach must register it, and switching away must
    remove it or a dead page keeps firing into an old stream."""
    client, context, page = harness
    popup = FakePage(url="https://accounts.example/oauth")
    context.pages.append(popup)
    with client.websocket_connect("/viewport/ws") as ws:
        ws.receive_text()  # hello
        _recv_until(ws, "pages")
        assert len(page.listeners.get("close", [])) == 1
        ws.send_text(json.dumps({"t": "switch_page", "idx": 1}))
        for _ in range(10):
            m = _recv(ws)
            if isinstance(m, dict) and m["t"] == "pages" and m["pages"][1]["viewed"]:
                break
        assert page.listeners.get("close", []) == []
        assert len(popup.listeners.get("close", [])) == 1


def test_closed_viewed_page_falls_back_to_a_live_one(harness):
    """Site JS closes the viewed page → the stream re-attaches to a
    live page instead of freezing on the last frame."""
    client, context, page = harness
    popup = FakePage(url="https://accounts.example/oauth")
    context.pages.append(popup)
    with client.websocket_connect("/viewport/ws") as ws:
        ws.receive_text()  # hello
        ws.send_text(json.dumps({"t": "switch_page", "idx": 1}))
        for _ in range(10):
            m = _recv(ws)
            if isinstance(m, dict) and m["t"] == "pages" and m["pages"][1]["viewed"]:
                break
        popup._closed = True
        ws.send_text(json.dumps({"t": "pages"}))
        for _ in range(10):
            m = _recv(ws)
            if isinstance(m, dict) and m["t"] == "pages" and m["pages"][0]["viewed"]:
                break
        else:
            raise AssertionError("never fell back to the live shared page")
    # Three CDP sessions total: shared → popup → shared again.
    assert context.cdp_sessions[-1].page is page


def test_attach_after_close_is_refused():
    """The close hook's fallback task can land a tick after endpoint
    teardown. attach() after close() must be a no-op, or it restarts
    the screencast on a dead connection and acks frames into the void."""
    import asyncio

    async def scenario():
        page = FakePage()
        context = FakeContext([page])
        stream = viewport._Stream(websocket=None)
        await stream.attach(context, page)
        await stream.close()
        await stream.attach(context, page)  # the racing fallback task
        return context, stream

    context, stream = asyncio.run(scenario())
    assert len(context.cdp_sessions) == 1      # no second session opened
    assert stream.cdp is None and stream.sender is None
    assert page_close_listeners_empty(context)


def page_close_listeners_empty(context):
    return all(not p.listeners.get("close") for p in context.pages)


def test_token_gate_rejects_a_bare_connection(harness, monkeypatch):
    client, context, _ = harness
    monkeypatch.setattr(viewport, "_CONTROL_TOKEN", "sekrit")
    from starlette.websockets import WebSocketDisconnect as ClientDisconnect
    with pytest.raises(ClientDisconnect) as exc:
        with client.websocket_connect("/viewport/ws") as ws:
            ws.receive_text()
    assert exc.value.code == 1008
    assert context.cdp_sessions == []  # rejected BEFORE touching the browser


@pytest.mark.parametrize("headers", [
    {"X-Browser-Token": "sekrit"},
    {"Authorization": "Bearer sekrit"},
])
def test_token_gate_accepts_header_and_bearer(harness, monkeypatch, headers):
    client, _, _ = harness
    monkeypatch.setattr(viewport, "_CONTROL_TOKEN", "sekrit")
    with client.websocket_connect("/viewport/ws", headers=headers) as ws:
        assert json.loads(ws.receive_text())["t"] == "hello"


def test_not_ready_reports_and_closes(harness, monkeypatch):
    client, _, _ = harness
    monkeypatch.setattr(viewport, "_context_provider", lambda: None)
    with client.websocket_connect("/viewport/ws") as ws:
        m = json.loads(ws.receive_text())
        assert m == {"t": "error", "detail": "chromium not ready"}
