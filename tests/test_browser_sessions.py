"""Tests for sidecars/browser/control/sessions.py — the general lane.

Runs without Chromium: sessions.py never imports playwright, so a stub
Browser/Context/Page is enough to exercise the real control flow —
allow-list enforcement, the route guard, ref resolution, the session
cap, and state persistence.

The load-bearing assertion is test_session_context_has_no_fs_bindings.
The shared page in app.py deliberately exposes __cseries_fs_* bindings and
the FSA polyfill to page JS; a session context must expose neither, or a
recipe driving a third-party site has a route to the entity's disk.

Run: pytest tests/test_browser_sessions.py
"""
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

_SIDECAR = Path(__file__).resolve().parents[1] / "sidecars" / "browser"
sys.path.insert(0, str(_SIDECAR))

from control import sessions  # noqa: E402
from fastapi import HTTPException  # noqa: E402


# ── stubs ───────────────────────────────────────────────────────────


class StubFrame:
    def __init__(self, parent=None):
        self.parent_frame = parent


class StubRequest:
    def __init__(self, url, navigation=True, subframe=False):
        self.url = url
        self.resource_type = "document" if navigation else "script"
        self._nav = navigation
        self.frame = StubFrame(StubFrame() if subframe else None)

    def is_navigation_request(self):
        return self._nav


class StubRoute:
    def __init__(self, url, navigation=True, subframe=False):
        self.request = StubRequest(url, navigation=navigation, subframe=subframe)
        self.continued = False
        self.aborted = None

    async def continue_(self):
        self.continued = True

    async def abort(self, code=None):
        self.aborted = code or "aborted"


async def call_guard(guard, route):
    """Invoke a route handler the way Playwright 1.48.0 actually does.

    _impl/_helper.py:334 (async branch) and :323 (sync) both call
    `self.handler(route, route.request)` — ALWAYS two positional args,
    with no arity adaptation, despite the public type hint also
    advertising a one-arg callable. The first version of this stub
    called the handler with ONE arg, which happened to match a one-arg
    handler in sessions.py, so 52 tests passed against a guard that
    would have raised TypeError on the first real request and never
    blocked anything. Call it the way the driver does.
    """
    return await guard(route, route.request)


async def _async(v):
    return v


class StubPage:
    def __init__(self):
        self.child_frames = []
        self.url = "about:blank"
        self.goto_calls = []
        self.closed = False
        self.refs = {}
        self.keyboard = type("KB", (), {"press": self._press})()
        self.mouse = type("M", (), {"wheel": self._wheel})()
        self.pressed = []
        self.wheeled = []
        self.handlers = {}

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def emit(self, event, payload):
        for h in self.handlers.get(event, []):
            h(payload)

    async def _press(self, key):
        self.pressed.append(key)

    async def _wheel(self, dx, dy):
        self.wheeled.append((dx, dy))

    async def goto(self, url, wait_until=None, timeout=None):
        self.goto_calls.append(url)
        self.url = url
        return type("Resp", (), {"status": 200})()

    async def wait_for_load_state(self, state="load", timeout=None):
        return None

    async def evaluate(self, script, arg=None):
        if "innerText" in script and "total_chars" in script:
            return {"text": "hello", "truncated": False, "total_chars": 5}
        if "contentEditable" in script and "idl_type" in script:
            el = self.refs.get(arg)
            if el is None:
                return None
            return {"tag": el.tag, "type": el.field_type,
                    "contentEditable": el.content_editable,
                    "idl_type": el.field_type if el.tag == "INPUT" else ""}
        if "includes" in script:
            return True
        return None

    # A page IS its own main frame for our purposes: sessions._snapshot
    # iterates page.frames and calls frame.evaluate_handle.
    @property
    def frames(self):
        return [self] + list(self.child_frames)

    @property
    def main_frame(self):
        return self

    async def evaluate_handle(self, script, arg=None):
        """Return the {info, nodes} handle _snapshot expects."""
        items, nodes = [], {}
        for i, (ref, el) in enumerate(sorted(self.refs.items())):
            items.append({"ref": f"e{i+1}", "role": "textbox", "name": "",
                          "tag": getattr(el, "tag", "INPUT").lower()})
            nodes[str(i)] = type("H", (), {"as_element": lambda s_, e=el: e})()
        info = {"url": self.url, "title": "Stub", "elements": items,
                "element_count": len(items), "truncated": False,
                "considered": len(items)}

        class _Handle:
            async def get_property(self_, name):
                if name == "info":
                    return type("J", (), {
                        "json_value": staticmethod(lambda: _async(info))})()
                return type("J", (), {
                    "get_properties": staticmethod(lambda: _async(nodes))})()

            async def dispose(self_):
                return None

        return _Handle()

    parent_frame = None       # a page IS the main frame

    def locator(self, selector):
        self.located = getattr(self, "located", [])
        self.located.append(selector)
        return f"<locator {selector} @{id(self)}>"

    async def screenshot(self, type=None, full_page=None, mask=None):
        self.screenshot_mask = mask
        return b"\x89PNG-stub"

    async def title(self):
        return "Stub"

    async def wait_for_timeout(self, ms):
        return None

    def is_closed(self):
        return self.closed

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def emit(self, event, payload):
        for h in self.handlers.get(event, []):
            h(payload)

    async def close(self):
        self.closed = True


class StubContext:
    def __init__(self, storage_state=None):
        self.storage_state_arg = storage_state
        self.page = StubPage()
        self.routes = []
        # Recorded so a test can assert these were NEVER called.
        self.exposed_bindings = []
        self.init_scripts = []
        self.handlers = {}
        self.closed = False

    def set_default_timeout(self, ms):
        pass

    def set_default_navigation_timeout(self, ms):
        pass

    async def expose_binding(self, name, fn):
        self.exposed_bindings.append(name)

    async def add_init_script(self, path=None, script=None):
        self.init_scripts.append(path or script)

    async def route(self, pattern, handler):
        self.routes.append((pattern, handler))

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def emit_page(self, page):
        for h in self.handlers.get("page", []):
            h(page)

    async def new_page(self):
        self.emit_page(self.page)
        return self.page

    async def storage_state(self):
        return {"cookies": [{"name": "sid", "domain": ".example.com"}], "origins": []}

    async def close(self):
        self.closed = True


class StubBrowser:
    def __init__(self):
        self.contexts = []

    async def new_context(self, viewport=None, storage_state=None,
                          accept_downloads=None):
        c = StubContext(storage_state=storage_state)
        c.accept_downloads = accept_downloads
        self.contexts.append(c)
        return c


@pytest.fixture(autouse=True)
def _fresh():
    """Clear the registry and install a fresh stub browser per test."""
    sessions._sessions.clear()
    browser = StubBrowser()
    sessions.set_browser_provider(lambda: browser)
    yield browser
    sessions._sessions.clear()


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _create(name="acme", allow=None, **kw):
    body = sessions.CreateBody(
        name=name, allow=allow if allow is not None else ["app.example.com"], **kw)
    return run(sessions.create_session(body))


def _act(name, **kw):
    return run(sessions.act(name, sessions.ActBody(**kw)))


def _json(resp):
    return json.loads(bytes(resp.body))


# ── host matching ───────────────────────────────────────────────────


@pytest.mark.parametrize("host,patterns,expected", [
    ("app.example.com", ("app.example.com",), True),
    ("APP.example.com", ("app.example.com",), True),
    ("other.example.com", ("app.example.com",), False),
    ("example.com", ("*.example.com",), False),   # apex NOT matched by *.
    ("example.com", ("example.com", "*.example.com"), True),  # list both
    ("a.b.example.com", ("*.example.com",), True),
    ("evil-example.com", ("*.example.com",), False),
    ("example.com.evil.net", ("*.example.com",), False),
    ("", ("app.example.com",), False),
    (None, ("app.example.com",), False),
])
def test_host_allowed(host, patterns, expected):
    assert sessions._host_allowed(host, patterns) is expected


@pytest.mark.parametrize("name", ["ac", "acme", "a-b-1", "x" * 40])
def test_valid_names(name):
    assert sessions._validate_name(name) == name


@pytest.mark.parametrize("name", ["", "a", "-acme", "acme-", "Acme!", "x" * 41,
                                  "../escape", "a/b"])
def test_invalid_names(name):
    with pytest.raises(HTTPException) as e:
        sessions._validate_name(name)
    assert e.value.status_code == 400


# ── create-time blast radius ────────────────────────────────────────


def test_create_requires_allow():
    with pytest.raises(HTTPException) as e:
        _create(allow=[])
    assert e.value.status_code == 400
    assert "allow[] is required" in e.value.detail


@pytest.mark.parametrize("bad", ["*", "*com", "https://app.example.com",
                                 "app.example.com:8443", "app.example.com/path",
                                 "localhost"])
def test_create_rejects_bad_allow_entries(bad):
    with pytest.raises(HTTPException) as e:
        _create(allow=[bad])
    assert e.value.status_code == 400


def test_create_accepts_wildcard_subdomain():
    resp = _create(allow=["*.example.com"])
    assert resp.status_code == 201
    assert _json(resp)["allow"] == ["*.example.com"]


def test_duplicate_name_conflicts():
    _create()
    with pytest.raises(HTTPException) as e:
        _create()
    assert e.value.status_code == 409


def test_session_cap(_fresh):
    for i in range(sessions.MAX_SESSIONS):
        _create(name=f"s{i}0")
    with pytest.raises(HTTPException) as e:
        _create(name="one-too-many")
    assert e.value.status_code == 429


# ── the load-bearing isolation assertion ────────────────────────────


def test_session_context_has_no_fs_bindings(_fresh):
    """A session page must have no route to the entity filesystem.

    app._create_context_and_page exposes seven __cseries_fs_* bindings and
    injects the FSA polyfill. A session context must do neither.
    """
    _create()
    ctx = _fresh.contexts[-1]
    assert ctx.exposed_bindings == [], (
        f"session context exposed bindings to page JS: {ctx.exposed_bindings}")
    assert ctx.init_scripts == [], (
        f"session context injected init scripts: {ctx.init_scripts}")


def test_sessions_do_not_share_state(_fresh):
    """Each session's cookies are its own object, and one session's login
    is never visible to another."""
    _create(name="site-a", allow=["a.example.com"],
            storage_state={"cookies": [{"name": "a-sid"}], "origins": []})
    _create(name="site-b", allow=["b.example.com"])
    a, b = sessions._sessions["site-a"], sessions._sessions["site-b"]
    assert a.state is not b.state
    assert b.state is None, "a session given no state must start signed out"
    assert (a.state["cookies"][0]["name"]) == "a-sid"


# ── navigation gating ───────────────────────────────────────────────


def test_navigate_to_declared_host(_fresh):
    _create()
    resp = _act("acme", action="navigate", url="https://app.example.com/in")
    assert _json(resp)["status"] == 200
    assert _fresh.contexts[-1].page.goto_calls == ["https://app.example.com/in"]


def test_navigate_off_allowlist_is_refused_before_goto(_fresh):
    _create()
    with pytest.raises(HTTPException) as e:
        _act("acme", action="navigate", url="https://evil.test/x")
    assert e.value.status_code == 403
    assert _fresh.contexts[-1].page.goto_calls == [], "goto must not be attempted"


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://x.example.com/"])
def test_navigate_rejects_non_http_schemes(url):
    _create()
    with pytest.raises(HTTPException) as e:
        _act("acme", action="navigate", url=url)
    assert e.value.status_code == 400


def test_route_guard_blocks_foreign_document(_fresh):
    """A redirect or link click cannot walk the session off its allow list."""
    _create()
    _pattern, guard = _fresh.contexts[-1].routes[0]
    r = StubRoute("https://tracker.test/steal")
    run(call_guard(guard, r))
    assert r.aborted == "blockedbyclient" and not r.continued
    assert sessions._sessions["acme"].blocked == 1


def test_route_guard_allows_declared_document(_fresh):
    _create()
    _pattern, guard = _fresh.contexts[-1].routes[0]
    r = StubRoute("https://app.example.com/next")
    run(call_guard(guard, r))
    assert r.continued and r.aborted is None


def test_route_guard_passes_subresources_through(_fresh):
    """Subresources are deliberately not filtered — see the docstring."""
    _create()
    _pattern, guard = _fresh.contexts[-1].routes[0]
    r = StubRoute("https://cdn.other.test/app.js", navigation=False)
    run(call_guard(guard, r))
    assert r.continued and r.aborted is None
    assert sessions._sessions["acme"].blocked == 0


def test_route_guard_blocks_non_http_document(_fresh):
    _create()
    _pattern, guard = _fresh.contexts[-1].routes[0]
    r = StubRoute("file:///etc/passwd")
    run(call_guard(guard, r))
    assert r.aborted == "blockedbyclient"


# ── actions ─────────────────────────────────────────────────────────


def test_stale_ref_is_409_not_a_wrong_click(_fresh):
    _create()
    with pytest.raises(HTTPException) as e:
        _act("acme", action="click", ref="e7")
    assert e.value.status_code == 409
    assert "fresh one" in e.value.detail and "snapshot" in e.value.detail


def test_unknown_action_is_400():
    _create()
    with pytest.raises(HTTPException) as e:
        _act("acme", action="eval")
    assert e.value.status_code == 400
    assert "unknown action" in e.value.detail


def test_no_eval_action_exists():
    """Explicit: arbitrary JS is not part of this surface."""
    src = (_SIDECAR / "control" / "sessions.py").read_text()
    assert '== "eval"' not in src


def test_snapshot_and_text_and_screenshot(_fresh):
    _create()
    snap = _json(_act("acme", action="snapshot"))
    assert snap["ok"] and snap["element_count"] == 0
    txt = _json(_act("acme", action="text"))
    assert txt["text"] == "hello"
    shot = _json(_act("acme", action="screenshot"))
    assert shot["bytes"] > 0 and shot["png_base64"]


def test_press_and_scroll(_fresh):
    _create()
    _act("acme", action="press", key="Enter")
    _act("acme", action="scroll", dy=300)
    page = _fresh.contexts[-1].page
    assert page.pressed == ["Enter"] and page.wheeled == [(0, 300)]


def test_act_on_missing_session_is_404():
    with pytest.raises(HTTPException) as e:
        _act("nope-nope", action="snapshot")
    assert e.value.status_code == 404


# ── persistence ─────────────────────────────────────────────────────


# ── regressions for the adversarial pass (2026-08-24) ───────────────


def test_guard_accepts_playwrights_two_arg_call(_fresh):
    """The bug that 52 green tests missed.

    Playwright 1.48.0 always calls a route handler with (route, request)
    — _impl/_helper.py:334. A one-arg handler raises TypeError on the
    first request and the allow-list never runs. call_guard() above
    invokes it the real way; this test names the contract so a revert to
    a one-arg signature fails loudly instead of silently disarming the
    guard.
    """
    import inspect
    _create()
    _pattern, guard = _fresh.contexts[-1].routes[0]
    sig = inspect.signature(guard)
    params = list(sig.parameters.values())
    assert any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in params) or \
        len(params) >= 2, f"guard must accept (route, request); got {sig}"
    r = StubRoute("https://tracker.test/x")
    run(call_guard(guard, r))            # must not raise TypeError
    assert r.aborted == "blockedbyclient"


def test_subframe_navigation_off_allowlist_is_blocked(_fresh):
    _create()
    _pattern, guard = _fresh.contexts[-1].routes[0]
    r = StubRoute("https://idp.other.test/authorize", subframe=True)
    run(call_guard(guard, r))
    assert r.aborted == "blockedbyclient"


def test_subframe_on_allowlist_is_permitted(_fresh):
    _create(allow=["app.example.com", "idp.example.com"])
    _pattern, guard = _fresh.contexts[-1].routes[0]
    r = StubRoute("https://idp.example.com/authorize", subframe=True)
    run(call_guard(guard, r))
    assert r.continued and r.aborted is None


def test_capacity_is_enforced_on_reopen_not_just_create(_fresh):
    """Create MAX, let them reap, create MAX more, then touch the first
    batch: without a check on the reopen path every one of them silently
    reopens and the memory cap is meaningless."""
    first = [f"a{i}0" for i in range(sessions.MAX_SESSIONS)]
    for n in first:
        _create(name=n)
    for n in first:                       # age them out
        sessions._sessions[n].last_used_ms = 0
    run(sessions._reap_idle())
    for i in range(sessions.MAX_SESSIONS):
        _create(name=f"b{i}0")            # fills the quota again
    with pytest.raises(HTTPException) as e:
        _act(first[0], action="snapshot")  # would be context MAX+1
    assert e.value.status_code == 429


def test_actions_on_one_session_are_serialised(_fresh):
    """Two concurrent acts must not interleave on a shared Page."""
    _create()
    s = sessions._sessions["acme"]
    order = []

    async def slow(_s, a, body):
        order.append(f"start:{a}")
        await asyncio.sleep(0.01)
        order.append(f"end:{a}")
        from fastapi.responses import JSONResponse as J
        return J({"ok": True})

    orig = sessions._act_locked
    sessions._act_locked = slow
    try:
        async def both():
            await asyncio.gather(
                sessions.act("acme", sessions.ActBody(action="one")),
                sessions.act("acme", sessions.ActBody(action="two")))
        run(both())
    finally:
        sessions._act_locked = orig
    assert order in (["start:one", "end:one", "start:two", "end:two"],
                     ["start:two", "end:two", "start:one", "end:one"]), order
    assert not s.lock.locked()


def test_popup_is_tracked_and_switchable(_fresh):
    _create()
    ctx = _fresh.contexts[-1]
    popup = StubPage()
    popup.url = "https://app.example.com/oauth-popup"
    ctx.emit_page(popup)                  # what window.open does
    pages = _json(_act("acme", action="pages"))["pages"]
    assert len(pages) == 2 and pages[0]["active"] is True
    _act("acme", action="switch_page", index=1)
    assert sessions._sessions["acme"].page is popup


def test_switch_page_rejects_bad_index():
    _create()
    with pytest.raises(HTTPException) as e:
        _act("acme", action="switch_page", index=9)
    assert e.value.status_code == 404


def test_delete_drops_registry_entry_even_if_close_throws(_fresh):
    _create()
    s = sessions._sessions["acme"]

    async def boom():
        raise RuntimeError("close failed")
    s.context.close = boom
    # _close swallows a per-object close failure (it logs a warning), so
    # the call still succeeds. The point of the try/finally in
    # delete_session is that the registry entry goes either way — proven
    # here against a context whose close() genuinely blows up.
    resp = _json(run(sessions.delete_session("acme", save=False)))
    assert resp["ok"] is True
    assert "acme" not in sessions._sessions, "entry must not survive a failed close"


def test_reaper_start_is_idempotent():
    async def go():
        sessions.start_reaper()
        first = sessions._reaper
        sessions.start_reaper()
        assert sessions._reaper is first
        await sessions.shutdown_all()
        assert sessions._reaper is None
    run(go())


def test_reap_skips_a_session_mid_action(_fresh):
    _create()
    s = sessions._sessions["acme"]
    s.last_used_ms = 0

    async def go():
        async with s.lock:
            await sessions._reap_idle()
    run(go())
    assert s.context is not None, "must not tear down a context mid-action"


# ── vault-backed state: RAM here, sealed by the caller there ────────


def test_caller_supplied_state_is_handed_to_the_browser(_fresh):
    """A login is restored ONLY from what the caller unsealed and passed."""
    blob = {"cookies": [{"name": "sid", "domain": ".bank.example"}], "origins": []}
    _create(storage_state=blob)
    assert _fresh.contexts[-1].storage_state_arg == blob


def test_no_state_means_signed_out(_fresh):
    _create()
    assert _fresh.contexts[-1].storage_state_arg is None


def test_export_state_returns_the_blob_for_sealing(_fresh):
    _create()
    out = _json(_act("acme", action="export_state"))
    assert out["storage_state"]["cookies"][0]["name"] == "sid"
    assert out["cookies"] == 1


def test_no_credential_is_written_to_disk(_fresh):
    """A session cookie is a bearer credential and must not sit in a
    file. Downloads are the ONE deliberate write — the user's own data,
    which is the point of the recipe — so the ban is on the credential
    path, not on all I/O."""
    for gone in ("_write_state", "_state_path", "SESSION_ROOT", "STATE_ROOT"):
        assert not hasattr(sessions, gone), f"{gone} should be gone"
    src = (_SIDECAR / "control" / "sessions.py").read_text()
    # storage_state must only ever move through memory and the response.
    assert "write_text" not in src and "os.replace" not in src, (
        "sessions.py has a file-write path again — check it is not state")
    # the only mkdir is the download directory
    assert src.count("mkdir") == 1, "unexpected second directory write"
    i = src.index("mkdir")
    assert "dest_dir" in src[i - 200:i], "the only mkdir must be for downloads"


def test_info_reports_state_presence_never_contents(_fresh):
    _create(storage_state={"cookies": [{"name": "sid"}], "origins": []})
    info = _json(run(sessions.get_session("acme")))
    assert info["has_state"] is True and info["state_cookies"] == 1
    assert "storage_state" not in info and "cookies" not in str(info.get("url"))


def test_idle_reap_keeps_the_login_in_memory(_fresh):
    """Reaping gives back the browser's RAM without costing the login."""
    _create()
    _act("acme", action="export_state")
    sessions._sessions["acme"].last_used_ms = 0
    run(sessions._reap_idle())
    s = sessions._sessions["acme"]
    assert s.context is None, "context must be closed"
    assert s.state is not None, "state must survive the reap"
    _act("acme", action="snapshot")          # transparently reopens
    assert _fresh.contexts[-1].storage_state_arg == s.state


def test_ephemeral_session_drops_state_on_close(_fresh):
    _create(persist=False)
    s = sessions._sessions["acme"]
    run(sessions.delete_session("acme", save=True))
    assert s.state is None, "persist=false must not retain cookies"


def test_downloads_are_accepted(_fresh):
    """Exporting a statement is a download; Playwright refuses them by
    default, so a recipe's file would silently go nowhere."""
    _create()
    assert _fresh.contexts[-1].accept_downloads is True


# ── egress proxy ────────────────────────────────────────────────────


def _load_proxy_config():
    """Load _proxy_config + _bypass_list out of control/app.py without importing it.

    app.py imports playwright at module level, which is not installed off-pod; these
    are pure env-reading, so exec'ing just those functions keeps them under test
    everywhere instead of skipped where it matters least.
    """
    import ast
    src = (_SIDECAR / "control" / "app.py")
    tree = ast.parse(src.read_text())
    want = {"_bypass_list", "_proxy_config"}
    fns = [n for n in tree.body
           if isinstance(n, ast.FunctionDef) and n.name in want]
    missing = want - {n.name for n in fns}
    assert not missing, f"{missing} missing — chromium would silently stop using the proxy"
    ns = {"os": os, "Optional": None,
          "log": type("L", (), {"info": staticmethod(lambda *a, **k: None),
                                "warning": staticmethod(lambda *a, **k: None)})()}
    exec(compile(ast.Module(body=fns, type_ignores=[]), str(src), "exec"), ns)
    return ns["_proxy_config"]


proxy_config_fn = _load_proxy_config()

_PROXY_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                   "NO_PROXY", "no_proxy", "NAC_EGRESS_LANE",
                   "NAC_EGRESS_SUBJECT_TOKEN", "NAC_EGRESS_SUBJECT", "PRIVATAE_APP_ID")


def _proxy_config(env):
    saved = {k: os.environ.get(k) for k in _PROXY_ENV_KEYS}
    try:
        for k in _PROXY_ENV_KEYS:
            os.environ.pop(k, None)
        os.environ.update(env)
        return proxy_config_fn()
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


def test_proxy_is_set_from_env():
    """Chromium ignores HTTP_PROXY on Linux — it needs the launch argument.
    Without it the browser silently leaves by the pod's default route,
    bypassing the user's NAC egress policy and their bastions."""
    cfg = _proxy_config({"HTTPS_PROXY": "http://[fd00::1]:3128",
                         "NO_PROXY": ".svc,.cluster.local"})
    assert cfg["server"] == "http://[fd00::1]:3128", cfg
    for must in ("127.0.0.1", "localhost", "::1", ".svc"):
        assert must in cfg["bypass"], f"{must} missing from {cfg['bypass']}"
    assert "username" not in cfg, "the node squid takes no credential"


def test_lowercase_and_http_fallbacks_are_read():
    assert _proxy_config({"http_proxy": "http://p:3128"})["server"] == "http://p:3128"
    assert _proxy_config({"HTTP_PROXY": "http://p:3128"})["server"] == "http://p:3128"


def test_https_proxy_wins_over_http_proxy():
    cfg = _proxy_config({"HTTP_PROXY": "http://plain:3128",
                         "HTTPS_PROXY": "http://secure:3128"})
    assert cfg["server"] == "http://secure:3128"


def test_no_proxy_env_means_no_config():
    assert _proxy_config({}) is None


def test_loopback_is_always_bypassed_even_with_empty_no_proxy():
    """The sidecar serves its own test page on 127.0.0.1 and the kernel
    dials it there — proxying to itself would break both."""
    cfg = _proxy_config({"HTTPS_PROXY": "http://p:3128"})
    assert "127.0.0.1" in cfg["bypass"] and "localhost" in cfg["bypass"]


# ── v125: the NAC app lane, and why it wins ─────────────────────────


def test_nac_lane_wins_over_the_node_squid():
    """The whole point of the feature. With the lane configured, pages must go
    through the kernel's egress gate — which applies the user's SIGNED rules and
    can route them out the user's OWN machine — not the node squid, which exits
    from the node's shared public address."""
    cfg = _proxy_config({"HTTPS_PROXY": "http://squid:3128",
                         "NAC_EGRESS_LANE": "http://entity-9-0.ns.svc:13129",
                         "NAC_EGRESS_SUBJECT_TOKEN": "b" * 64,
                         "PRIVATAE_APP_ID": "browser"})
    assert cfg["server"] == "http://entity-9-0.ns.svc:13129", cfg
    assert cfg["username"] == "harness:browser", cfg
    assert cfg["password"] == "b" * 64


def test_nac_lane_credential_rides_basic_not_a_header():
    """Chromium has no flag for a proxy credential; it answers a 407 with Basic.
    So the token MUST be the password Playwright fills that challenge with — if it
    ever moved to an args entry the lane would 407 every request."""
    cfg = _proxy_config({"NAC_EGRESS_LANE": "http://lane:13129",
                         "NAC_EGRESS_SUBJECT_TOKEN": "t" * 64,
                         "NAC_EGRESS_SUBJECT": "harness:browser"})
    assert cfg["username"] and cfg["password"], cfg
    assert isinstance(cfg, dict) and "server" in cfg


def test_lane_without_a_token_falls_back_rather_than_407ing_everything():
    """A lane with no credential answers 407 for every request, so the browser
    would load nothing at all. Falling back to squid loads pages from the wrong
    address — worse than right, better than dead — and the log says so."""
    cfg = _proxy_config({"NAC_EGRESS_LANE": "http://lane:13129",
                         "HTTPS_PROXY": "http://squid:3128"})
    assert cfg["server"] == "http://squid:3128", cfg
    assert "password" not in cfg


def test_subject_defaults_to_the_browser_harness():
    cfg = _proxy_config({"NAC_EGRESS_LANE": "http://lane:13129",
                         "NAC_EGRESS_SUBJECT_TOKEN": "z" * 64})
    assert cfg["username"] == "harness:browser", cfg


# ── downloads: the deliverable of an "export my statement" recipe ───


class StubDownload:
    def __init__(self, name="statement.csv", url="https://app.example.com/x.csv",
                 failure=None, body=b"date,amount\n2026-08-01,-12.34\n"):
        self.suggested_filename = name
        self.url = url
        self._failure = failure
        self._body = body
        self.saved_to = None

    async def save_as(self, path):
        self.saved_to = path
        if self._failure is None:
            pathlib_path = Path(path)
            pathlib_path.parent.mkdir(parents=True, exist_ok=True)
            pathlib_path.write_bytes(self._body)

    async def failure(self):
        return self._failure


@pytest.fixture()
def dl_root(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "DOWNLOAD_ROOT", tmp_path / "dl")
    return tmp_path / "dl"


def _fire_download(session_name, download):
    """Deliver a download the way Playwright does — a page event."""
    run(sessions._capture_download(sessions._sessions[session_name], download))


def test_download_lands_on_the_pvc_and_is_listed(dl_root, _fresh):
    _create()
    _fire_download("acme", StubDownload())
    out = _json(_act("acme", action="downloads"))
    assert len(out["downloads"]) == 1
    d = out["downloads"][0]
    assert d["ok"] is True and d["filename"] == "statement.csv"
    assert Path(d["path"]).read_text().startswith("date,amount")
    assert d["bytes"] > 0
    assert out["download_dir"].endswith("acme")


def test_downloads_are_filed_per_session(dl_root, _fresh):
    _create(name="bank-a", allow=["a.example.com"])
    _create(name="bank-b", allow=["b.example.com"])
    _fire_download("bank-a", StubDownload(name="a.csv"))
    _fire_download("bank-b", StubDownload(name="b.csv"))
    assert (dl_root / "bank-a" / "a.csv").exists()
    assert (dl_root / "bank-b" / "b.csv").exists()
    assert not (dl_root / "bank-a" / "b.csv").exists()


@pytest.mark.parametrize("evil,expect", [
    ("../../etc/passwd", "passwd"),
    ("/etc/shadow", "shadow"),
    ("..", "download-"),           # falls back
    ("state ment;rm -rf.csv", "state_ment_rm_-rf.csv"),
    ("", "download-"),
])
def test_site_chosen_filenames_cannot_escape_the_directory(evil, expect, dl_root, _fresh):
    """suggested_filename comes from the SITE. It is untrusted."""
    _create()
    _fire_download("acme", StubDownload(name=evil))
    d = sessions._sessions["acme"].downloads[0]
    assert expect in d["filename"], d
    assert "/" not in d["filename"] and ".." not in d["filename"]
    assert Path(d["path"]).parent == dl_root / "acme"


def test_a_failed_download_is_recorded_not_raised(dl_root, _fresh):
    _create()
    _fire_download("acme", StubDownload(failure="net::ERR_FAILED"))
    d = sessions._sessions["acme"].downloads[0]
    assert d["ok"] is False and d["error"] == "net::ERR_FAILED"


def test_download_handler_never_throws_into_the_event_loop(dl_root, _fresh):
    """It runs as a bare task off a page event — an exception escaping
    there is an unretrievable crash, not a failed download."""
    class Exploding(StubDownload):
        async def save_as(self, path):
            raise OSError("disk full")
    _create()
    _fire_download("acme", Exploding())          # must not raise
    d = sessions._sessions["acme"].downloads[0]
    assert d["ok"] is False and "disk full" in d["error"]


def test_every_page_including_popups_gets_a_download_listener(_fresh):
    """An export often fires from a popup; a context-level listener does
    not exist in Playwright 1.48, so each page needs its own."""
    _create()
    ctx = _fresh.contexts[-1]
    assert "download" in ctx.page.handlers, "main page has no download listener"
    popup = StubPage()
    ctx.emit_page(popup)
    assert "download" in popup.handlers, "popup has no download listener"


# ── type_secret: the vault broker's landing point ───────────────────


class StubElement:
    def __init__(self, field_type="password", tag="INPUT", content_editable=False,
                 connected=True):
        self.field_type = field_type
        self.tag = tag
        self.content_editable = content_editable
        self.connected = connected
        self.disposed = False
        self.filled = []

    async def get_attribute(self, name):
        return self.field_type if name == "type" else None

    async def evaluate(self, script, arg=None):
        """_element asks the DOM what this is and whether it is connected."""
        return {"tag": self.tag, "type": self.field_type,
                "contentEditable": self.content_editable,
                "idl_type": self.field_type if self.tag == "INPUT" else "",
                "connected": self.connected}

    async def dispose(self):
        self.disposed = True

    async def fill(self, value, timeout=None):
        self.filled.append(value)

    async def click(self, timeout=None):
        pass

    async def press(self, key, timeout=None):
        self.pressed = getattr(self, "pressed", [])
        self.pressed.append(key)


def _with_element(session_name, ref, el):
    """Put an element in the SESSION's ref map — where it lives now that
    the page can no longer reach it."""
    sessions._sessions[session_name].refs[ref] = el
    return el


def test_secret_goes_into_a_password_field(_fresh):
    _create()
    el = _with_element("acme", "e3", StubElement("password"))
    out = _json(_act("acme", action="type_secret", ref="e3", text="hunter2"))
    assert out["typed_into"] == "e3" and out["field_type"] == "password"
    assert el.filled == ["", "hunter2"]


def test_secret_is_refused_on_a_visible_field(_fresh):
    """The model still picks the element. A hostile page can offer a
    plausible decoy that is not a password box and harvest it in the
    clear — this refusal is what closes that."""
    _create()
    el = _with_element("acme", "e3", StubElement("text"))
    with pytest.raises(HTTPException) as e:
        _act("acme", action="type_secret", ref="e3", text="hunter2")
    assert e.value.status_code == 409
    assert "password" in e.value.detail
    assert el.filled == [], "nothing may be typed when the target is refused"


def test_the_override_is_explicit(_fresh):
    _create()
    el = _with_element("acme", "e3", StubElement("text"))
    _act("acme", action="type_secret", ref="e3", text="hunter2",
         allow_non_password=True)
    assert el.filled == ["", "hunter2"]


def test_secret_response_never_echoes_the_value(_fresh):
    _create()
    _with_element("acme", "e3", StubElement("password"))
    out = _json(_act("acme", action="type_secret", ref="e3", text="hunter2"))
    assert "hunter2" not in json.dumps(out)


def test_type_secret_has_no_submit_shortcut(_fresh):
    """A secret type must never also be the action. Enter is a separate,
    visible step."""
    _create()
    el = _with_element("acme", "e3", StubElement("password"))
    _act("acme", action="type_secret", ref="e3", text="s", submit=True)
    assert el.filled == ["", "s"]
    assert getattr(el, "pressed", []) == [], (
        "submit=True must be ignored on type_secret — the plain `type` "
        "action honours it, and a secret type must never also be the "
        "keystroke that performs the login")


def test_a_stale_ref_cannot_receive_a_secret(_fresh):
    _create()
    with pytest.raises(HTTPException) as e:
        _act("acme", action="type_secret", ref="e9", text="hunter2")
    assert e.value.status_code == 409


def test_session_records_that_a_secret_was_typed_not_what(_fresh):
    _create()
    _with_element("acme", "e3", StubElement("password"))
    _act("acme", action="type_secret", ref="e3", text="hunter2")
    info = _json(run(sessions.get_session("acme")))
    assert info["secret_typed"] is True
    assert "hunter2" not in json.dumps(info)


def test_screenshots_always_mask_password_fields(_fresh):
    """The quietest leak path: the image goes to the caller and usually
    onward to a model, where a rendered credential is invisible to every
    other guard. Masked unconditionally, not just after a secret type."""
    _create()
    out = _json(_act("acme", action="screenshot"))
    assert out["masked"] == "input[type=password]"
    page = _fresh.contexts[-1].page
    assert len(page.screenshot_mask) == 1          # one frame, one locator
    assert "input[type=password]" in page.screenshot_mask[0]


# ── B2: the guard must judge the DOM, not an attribute the page chose ──


def test_contenteditable_claiming_password_is_refused(_fresh):
    """`<div contenteditable type="password">` answered "password" to
    get_attribute, and Playwright's fill() writes to a contenteditable —
    so the guard passed AND the screenshot mask (input[type=password])
    matched nothing. Refused outright now, override or not."""
    _create()
    el = _with_element("acme", "e3",
                       StubElement(field_type="password", tag="DIV", content_editable=True))
    with pytest.raises(HTTPException) as e:
        _act("acme", action="type_secret", ref="e3", text="hunter2")
    assert e.value.status_code == 409
    assert "contenteditable" in e.value.detail and "mask" in e.value.detail
    assert el.filled == []


def test_contenteditable_is_refused_even_with_the_override(_fresh):
    """allow_non_password is for a site that uses something odd but
    maskable. It is not a way to reach the unmaskable case."""
    _create()
    el = _with_element("acme", "e3",
                       StubElement(field_type="password", tag="DIV", content_editable=True))
    with pytest.raises(HTTPException) as e:
        _act("acme", action="type_secret", ref="e3", text="x", allow_non_password=True)
    assert e.value.status_code == 409
    assert el.filled == []


@pytest.mark.parametrize("tag", ["DIV", "SPAN", "TEXTAREA", "SELECT"])
def test_non_input_tags_claiming_password_are_refused(tag, _fresh):
    """get_attribute("type") returns the raw attribute off ANY tag."""
    _create()
    el = _with_element("acme", "e3", StubElement(field_type="password", tag=tag))
    with pytest.raises(HTTPException) as e:
        _act("acme", action="type_secret", ref="e3", text="hunter2")
    assert e.value.status_code == 409
    assert el.filled == []


def test_a_real_password_input_still_works(_fresh):
    _create()
    el = _with_element("acme", "e3", StubElement(field_type="password", tag="INPUT"))
    out = _json(_act("acme", action="type_secret", ref="e3", text="hunter2"))
    assert out["field_type"] == "password" and el.filled == ["", "hunter2"]


# ── the ref map is OURS, not the page's ─────────────────────────────


def test_refs_live_in_the_session_not_the_page(_fresh):
    """A ref map in the page's own world would let a page swap the element
    behind a ref between the snapshot and the action: the guard would inspect
    a real password input while fill() wrote to the attacker's sink."""
    _create()
    assert isinstance(sessions._sessions["acme"].refs, dict)


def test_a_removed_element_is_a_409_not_a_timeout(_fresh):
    """A handle to a removed node still resolves — it holds a strong
    reference — so the old staleness check sailed past and the caller got
    a 10s timeout surfaced as 502 instead of the promised 409."""
    _create()
    _with_element("acme", "e1", StubElement(connected=False))
    with pytest.raises(HTTPException) as e:
        _act("acme", action="click", ref="e1")
    assert e.value.status_code == 409 and "removed" in e.value.detail


def test_navigation_drops_the_ref_map(_fresh):
    """Including History-API navigation: a SPA route change issues no
    request, so the route guard never fires and the refs used to stay
    live against a replaced DOM."""
    _create()
    _with_element("acme", "e1", StubElement())
    page = _fresh.contexts[-1].page
    page.url = "https://app.example.com/next"
    page.emit("framenavigated", page)
    assert sessions._sessions["acme"].refs == {}


def test_snapshot_releases_the_previous_handles(_fresh):
    """ElementHandles pin DOM nodes in the renderer until disposed, so a
    recipe that snapshots in a loop leaked a page each time."""
    _create()
    el = _with_element("acme", "e1", StubElement())
    _act("acme", action="snapshot")
    assert el.disposed is True


# ── allow[] cannot name the infrastructure ──────────────────────────


@pytest.mark.parametrize("host", [
    "127.0.0.1", "localhost", "0.0.0.0", "::1",
    "169.254.169.254", "metadata.google.internal",
    "kubernetes.default.svc.cluster.local", "10.0.0.1",
    "anything.svc", "foo.cluster.local", "bar.internal", "baz.local",
])
def test_ssrf_targets_are_refused_in_allow(host, _fresh):
    """A browser in the pod that can reach these is a request-forgery
    tool. Loopback is the worst of them: it reaches this sidecar's own
    unauthenticated API, so a session allowed there could drive itself
    and export every other session's cookies."""
    with pytest.raises(HTTPException) as e:
        _create(allow=[host])
    assert e.value.status_code == 400


@pytest.mark.parametrize("pattern", ["*.com", "*.co.uk", "*.github.io", "*.io"])
def test_public_suffix_wildcards_are_refused(pattern, _fresh):
    """A wildcard must name a registrable domain. `*.com` is every site on
    the internet; `*.github.io` is every project page on GitHub. Single
    labels are caught by the no-dot rule, multi-label suffixes by the
    public-suffix list — both are refusals, which is what matters."""
    with pytest.raises(HTTPException) as e:
        _create(allow=[pattern])
    assert e.value.status_code == 400
    assert "refused" in e.value.detail or "too broad" in e.value.detail


@pytest.mark.parametrize("pattern", ["*.example.com", "*.sub.example.com", "app.bank.example"])
def test_real_domains_are_still_accepted(pattern, _fresh):
    assert _create(allow=[pattern]).status_code == 201


@pytest.mark.parametrize("url", [
    "https://evil.test\\@app.example.com/",
    "https://user:pw@app.example.com/",
])
def test_urls_two_parsers_disagree_about_are_refused(url, _fresh):
    """Python's urlparse and Chromium read these differently. Rather than
    out-parse the browser, refuse the characters they argue over."""
    _create()
    with pytest.raises(HTTPException) as e:
        _act("acme", action="navigate", url=url)
    assert e.value.status_code == 400


# ── capacity under concurrency ──────────────────────────────────────


def test_concurrent_reopens_cannot_exceed_the_cap(_fresh):
    """_ensure_capacity is sync and _open_context awaits. Without the
    lock every concurrent caller passed the same check before any of them
    created anything: measured 6 live contexts against a cap of 3, with
    3 orphaned and unreachable by the reaper."""
    names = [f"s{i}0" for i in range(sessions.MAX_SESSIONS)]
    for n in names:
        _create(name=n)
    for n in names:
        sessions._sessions[n].last_used_ms = 0
    run(sessions._reap_idle())
    before = len(_fresh.contexts)

    async def storm():
        await asyncio.gather(
            *[sessions.act(n, sessions.ActBody(action="snapshot")) for n in names],
            return_exceptions=True)
    run(storm())
    live = sum(1 for s in sessions._sessions.values() if s.context is not None)
    assert live <= sessions.MAX_SESSIONS, f"{live} contexts open, cap is {sessions.MAX_SESSIONS}"
    assert len(_fresh.contexts) - before <= sessions.MAX_SESSIONS


def test_downloads_are_drained_before_the_context_closes(_fresh):
    """Playwright deletes the artifact when the context closes, so a
    statement still being written at shutdown was silently lost."""
    _create()
    s = sessions._sessions["acme"]
    started = asyncio.Event()

    async def slow_capture():
        started.set()
        await asyncio.sleep(0.05)
        s.downloads.append({"ok": True, "filename": "late.csv"})

    async def go():
        t = asyncio.create_task(slow_capture())
        s.download_tasks.add(t)
        t.add_done_callback(s.download_tasks.discard)
        await started.wait()
        await sessions._close(s, save=False)
    run(go())
    assert s.downloads and s.downloads[-1]["filename"] == "late.csv"


# ── review follow-ups ───────────────────────────────────────────────


class StubFramePage(StubPage):
    """A child frame: same surface, but it has a parent."""
    def __init__(self, parent):
        super().__init__()
        self.parent_frame = parent


def test_screenshot_masks_password_fields_in_every_frame(_fresh):
    """page.locator() matches the MAIN frame only, and this design's whole
    premise is that bank logins live in iframes — so a main-frame-only
    mask missed exactly the case it was written for."""
    _create()
    page = _fresh.contexts[-1].page
    page.child_frames = [StubFramePage(page), StubFramePage(page)]
    out = _json(_act("acme", action="screenshot"))
    assert out["masked_frames"] == 3, "expected the main frame plus both children"
    assert len(page.screenshot_mask) == 3


def test_a_subframe_navigating_does_not_wipe_the_ref_map(_fresh):
    """Firing on every frame meant a rotating ad iframe dropped the map
    continuously and the caller could never keep a ref long enough to use
    it. Safe to scope to the main frame because _element re-checks that
    the node is still connected."""
    _create()
    page = _fresh.contexts[-1].page
    _with_element("acme", "e1", StubElement())
    page.emit("framenavigated", StubFramePage(page))
    assert sessions._sessions["acme"].refs, "a subframe must not clear the map"
    page.emit("framenavigated", page)
    assert sessions._sessions["acme"].refs == {}, "the main frame must clear it"


def test_a_stale_subframe_ref_still_fails_closed(_fresh):
    """The safety that lets the above be narrow."""
    _create()
    _with_element("acme", "e1", StubElement(connected=False))
    with pytest.raises(HTTPException) as e:
        _act("acme", action="click", ref="e1")
    assert e.value.status_code == 409
