"""Browser agents: the file, the locator, the runner — against a stub sidecar.

No Chromium. The sidecar is a fake that answers the sessions-lane API from a
scripted page model, so every branch of the runner executes: a full run to
`ok` with a download collected through the new /downloads route, a failed
step with its number + reason + masked screenshot, the pause/resume handoff,
the vault refusing a sealed entry without a grant, the login cookies sealed
and restored, and the schedule row created and cancelled.

Run: pytest tests/test_browser_agents.py
"""
from __future__ import annotations

import asyncio
import base64
import json
import pathlib
import sys

import pytest
import yaml
import sys as _ts_sys, os as _ts_os
_ts_sys.path.insert(0, _ts_os.path.join(_ts_os.path.dirname(_ts_os.path.dirname(_ts_os.path.abspath(__file__))), 'scripts'))
from tools_source import tools_source  # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from server import browser_agents as A  # noqa: E402

# One loop for the whole module: a paused run lives across several calls
# (run → resume → wait), so the tests need the SAME loop each time.
_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_LOOP)


@pytest.fixture(autouse=True)
def _root(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(A, "LOCATE_TIMEOUT_S", 0.3)
    monkeypatch.setattr(A, "LOAD_SETTLE_S", 0.0)
    monkeypatch.setattr(A, "DOWNLOAD_TIMEOUT_S", 1.0)
    monkeypatch.setattr(A, "PAUSE_TIMEOUT_S", 2.0)
    A._active.clear()
    yield
    A._active.clear()


SPEC = {
    "name": "bank-ledger",
    "site": "online.bank.example",
    "allow": ["login.bank.example"],
    "steps": [
        {"go": "https://online.bank.example/"},
        {"type": {"field": "User ID", "key": "user-id"}},
        {"secret": {"field": "Password", "key": "password"}},
        {"click": {"role": "button", "name": "Sign On"}},
        {"wait_for_load": True},
        {"pause_if": {"text": "Enter the code", "field": "Code", "submit": True}},
        {"wait_for": "Accounts"},
        {"select": {"field": "Format", "value": "CSV"}},
        {"click": {"name": "Download"}},
        {"expect_download": {"match": "*.csv", "save_as": "exports/bank/{date}.csv"}},
    ],
}


# ── the file ──────────────────────────────────────────────────────────

def test_normalize_canonicalises_and_names_every_refusal():
    canon = A.normalize(SPEC)
    assert canon["steps"][1] == {"type": {"field": "User ID", "key": "user-id"}}
    assert canon["steps"][2] == {"secret": {"field": "Password", "key": "password"}}
    assert canon["steps"][3] == {"click": {"name": "Sign On", "role": "button"}}
    with pytest.raises(A.AgentError, match="step 2 .*secret is never written"):
        A.normalize({**SPEC, "steps": [{"go": "https://online.bank.example/"},
                                       {"secret": {"field": "Password", "key": "p", "literal": "x"}}]})
    with pytest.raises(A.AgentError, match="step 1: .*not the agent's site"):
        A.normalize({**SPEC, "steps": [{"go": "https://evil.example/"}]})
    with pytest.raises(A.AgentError, match="loopback"):
        A.normalize({**SPEC, "site": "127.0.0.1"})
    with pytest.raises(A.AgentError, match="internal suffix"):
        A.normalize({**SPEC, "allow": ["kernel.svc"]})
    with pytest.raises(A.AgentError, match="save_as"):
        A.normalize({**SPEC, "steps": [{"expect_download": {"match": "*", "save_as": "../x"}}]})
    with pytest.raises(A.AgentError, match="unknown step kind"):
        A.normalize({**SPEC, "steps": [{"eval": "x"}]})
    with pytest.raises(A.AgentError, match="recurrence"):
        A.normalize({**SPEC, "schedule": {"recurrence": "sometimes", "fire_at": "2026-09-05T06:00:00Z"}})


# A Host's zone is the rest of its CSERIES_URL after the handle
# (server/home_origin.py). The default zone sits under .localhost, which the
# loopback rule refuses as well, so these tests use a zone behind the owner's
# own name, where only the Host-address rule can refuse it.
PROXIED_URL = "https://home.cseries.example.org"


@pytest.fixture
def host_zone(monkeypatch):
    from server.home_origin import parse
    suffix = parse(PROXIED_URL).zone
    monkeypatch.setattr(A, "_HOST_ZONES", (suffix.strip(".").lower(),))
    return suffix


def test_the_configured_zone_is_a_hosts_own_address():
    """The zone the module refuses is this Host's own, read from
    server/host_names.py, not a copy."""
    from server.applications.app_origin import origin_key
    from server.host_names import HOST_SUFFIXES
    assert HOST_SUFFIXES, "no Host zone to test against"
    for suffix in HOST_SUFFIXES:
        for site in (f"0123456789abcdef{suffix}",
                     f"apps-{origin_key('skills')}-0123456789abcdef{suffix}"):
            assert "a Host's own address" in (A.reject_host(site) or ""), site


def test_an_agent_never_goes_to_a_hosts_own_address(host_zone):
    """A Host's desktop and its apps' origins are where the person's session
    works: the person clears a skill's mark by saving it in the Skills app
    there. An agent the Host can run must never be pointed at one, whether it
    was saved through the API or written into the file by hand."""
    from server.applications.app_origin import origin_key
    handle = "0123456789abcdef"
    for suffix in (host_zone,):
        zone = suffix.lstrip(".")
        desktop = f"{handle}{suffix}"
        skills = f"apps-{origin_key('skills')}-{handle}{suffix}"
        parent = zone.split(".", 1)[1]
        for site in (desktop, skills, zone):
            with pytest.raises(A.AgentError, match="a Host's own address"):
                A.normalize({**SPEC, "site": site, "allow": [],
                             "steps": [{"go": f"https://{site}/"}]})
        for allow in ([desktop], [skills], [f"*{suffix}"], [f"*.{parent}"],
                      # reject_host strips the base, so the zone test must
                      # judge the same stripped name, not the raw text
                      [f"*. {parent}"], [f"*.\t{parent}"], [f"*. {parent}."]):
            with pytest.raises(A.AgentError, match="a Host's own address"):
                A.normalize({**SPEC, "allow": allow})
        with pytest.raises(A.AgentError, match="a Host's own address"):
            A.normalize({**SPEC, "steps": [
                {"secret": {"field": "P", "key": "p", "host": desktop}}]})

        # Written into the file by hand: listed as invalid, and never run.
        d = A._dir("own-host")
        d.mkdir(parents=True, exist_ok=True)
        (d / "agent.yaml").write_text(yaml.safe_dump(
            {"name": "own-host", "site": skills,
             "steps": [{"go": f"https://{skills}/"}, {"click": {"name": "Save"}}]}))
        assert "a Host's own address" in (A.load_agent("own-host") or {}).get("invalid", "")
        with pytest.raises(A.AgentError, match="cannot run"):
            _LOOP.run_until_complete(A.run(None, entity_id=1, name="own-host", actor="t"))

    # Names that only look close are ordinary sites.
    zone = host_zone.lstrip(".")
    for site in (f"my{zone}", f"{zone}.example.com", zone.split(".", 1)[1]):
        assert A.normalize({**SPEC, "site": site, "allow": [],
                            "steps": [{"go": f"https://{site}/"}]})["site"] == site
    assert A.normalize({**SPEC, "allow": ["*.bank.example"]})["allow"] == ["*.bank.example"]


def test_write_load_list_and_a_broken_edit_is_reported_not_hidden():
    A._write_yaml("bank-ledger", A.normalize(SPEC))
    p = A.agents_root() / "bank-ledger" / "agent.yaml"
    assert p.is_file()
    loaded = A.load_agent("bank-ledger")
    assert loaded["site"] == "online.bank.example" and len(loaded["steps"]) == 10
    assert loaded["last_run"] is None
    assert [a["name"] for a in A.list_agents()] == ["bank-ledger"]
    # the user edits the file and breaks a step
    raw = yaml.safe_load(p.read_text())
    raw["steps"].append({"teleport": "x"})
    p.write_text(yaml.safe_dump(raw))
    broken = A.load_agent("bank-ledger")
    assert "unknown step kind" in broken["invalid"]
    assert A.list_agents()[0]["invalid"]
    assert A.load_agent("nope") is None and A.load_agent("Bad Name") is None


@pytest.mark.parametrize("text, why", [
    ("- just a list\n", "not a mapping"),
    ("just a string\n", "not a mapping"),
    ("site: [unclosed\n", "not readable YAML"),
    ("site: example.com\nallow: 5\nsteps: [{go: 'https://example.com/'}]\n", "allow must be a list"),
    ("site: example.com\nsteps: 5\n", "at least one step"),
    ("site: example.com\nsteps: [{wait: .inf}]\n", "needs milliseconds"),
    ("site: example.com\nsteps: [{click: {name: Go, nth: .inf}}]\n", "nth must be a number"),
])
def test_a_file_of_any_shape_is_listed_invalid_never_crashes_the_list(text, why):
    """A file the user broke is listed with the reason, whatever it parses
    to, and a good agent beside it is still listed; it cannot run, and it can
    still be deleted."""
    A._write_yaml("bank-ledger", A.normalize(SPEC))
    d = A._dir("broken")
    d.mkdir(parents=True, exist_ok=True)
    (d / "agent.yaml").write_text(text)
    listed = {a["name"]: a for a in A.list_agents()}
    assert set(listed) == {"broken", "bank-ledger"}
    assert why in listed["broken"]["invalid"] and "invalid" not in listed["bank-ledger"]
    with pytest.raises(A.AgentError, match="cannot run"):
        _LOOP.run_until_complete(A.run(None, entity_id=1, name="broken", actor="t"))
    assert _LOOP.run_until_complete(A.delete_agent(None, entity_id=1, name="broken"))


# ── the locator ───────────────────────────────────────────────────────

ELEMENTS = [
    {"ref": "e1", "role": "textbox", "name": "User ID", "type": "text"},
    {"ref": "e2", "role": "textbox", "name": "Password", "type": "password"},
    {"ref": "e3", "role": "button", "name": "Sign On"},
    {"ref": "e4", "role": "link", "name": "Sign On to Business Banking"},
    {"ref": "e5", "role": "button", "name": "Download", "disabled": True},
    {"ref": "e6", "role": "button", "name": "Download"},
    {"ref": "e7", "role": "combobox", "name": "Format", "options": ["PDF", "CSV"]},
]


def test_match_element_exact_then_prefix_then_contains_role_narrows_nth_picks():
    m = A.match_element
    assert m(ELEMENTS, {"name": "Sign On"})["ref"] == "e3"                  # exact beats prefix
    assert m(ELEMENTS, {"name": "sign on", "role": "link"})["ref"] == "e4"  # role narrows
    assert m(ELEMENTS, {"name": "Business"})["ref"] == "e4"                 # contains
    assert m(ELEMENTS, {"name": "Download"})["ref"] == "e6"                 # disabled skipped
    assert m(ELEMENTS, {"name": "Sign On", "nth": 1}) is None               # nth among EXACT only
    assert m(ELEMENTS, {"name": "Password"}, prefer_roles=A._FIELD_ROLES)["ref"] == "e2"
    assert m(ELEMENTS, {"name": "Format", "role": "textbox"})["ref"] == "e7"  # textbox ≈ combobox
    assert m(ELEMENTS, {"name": "Nope"}) is None


# ── the stub sidecar ──────────────────────────────────────────────────

class FakeSidecar:
    """Answers the sessions-lane API from a page model the test scripts."""

    def __init__(self):
        self.sessions = {}
        self.page = "login"
        self.calls = []
        self.typed = []               # (ref, text) via plain type
        self.downloads = []           # dicts as the sidecar reports them
        self.files = {}               # filename -> bytes
        self.code_shown = False
        self.fail_click = None        # name of a button that "vanishes"

    def elements(self):
        if self.page == "login":
            return [{"ref": "e1", "role": "textbox", "name": "User ID"},
                    {"ref": "e2", "role": "textbox", "name": "Password", "type": "password"},
                    {"ref": "e3", "role": "button", "name": "Sign On"}]
        if self.page == "code":
            return [{"ref": "e1", "role": "textbox", "name": "Code"},
                    {"ref": "e2", "role": "button", "name": "Continue"}]
        els = [{"ref": "e1", "role": "combobox", "name": "Format"},
               {"ref": "e2", "role": "button", "name": "Download"}]
        if self.fail_click == "Download":
            els = els[:1]
        return els

    def text(self):
        return {"login": "Welcome. Sign On.",
                "code": "Enter the code we sent you",
                "accounts": "Accounts · Spend · Export"}[self.page]

    async def call(self, method, path, payload=None, timeout=45.0):
        self.calls.append((method, path, payload))
        if path == "/v1/sessions" and method == "POST":
            self.sessions[payload["name"]] = payload
            return 201, {"name": payload["name"], "allow": payload["allow"]}
        if path.startswith("/v1/sessions/") and method == "DELETE":
            self.sessions.pop(path.rsplit("/", 1)[1], None)
            return 200, {"closed": True}
        if "/downloads/" in path and method == "GET":
            fn = path.rsplit("/", 1)[1]
            if fn in self.files:
                return 200, self.files[fn]
            return 404, {"detail": "no"}
        if path.endswith("/act"):
            a = payload["action"]
            if a == "navigate":
                self.page = "login"
                return 200, {"ok": True, "status": 200}
            if a == "snapshot":
                return 200, {"ok": True, "elements": self.elements(), "url": f"https://online.bank.example/{self.page}"}
            if a == "click":
                if self.page == "login" and payload["ref"] == "e3":
                    self.page = "code" if self.code_shown else "accounts"
                elif self.page == "accounts" and payload["ref"] == "e2":
                    self.downloads.append({"ok": True, "filename": "ledger.csv", "bytes": 5})
                    self.files["ledger.csv"] = b"a,b\n1,2\n"
                return 200, {"ok": True}
            if a == "type":
                self.typed.append((payload["ref"], payload["text"]))
                if self.page == "code" and payload.get("submit"):
                    self.page = "accounts"
                return 200, {"ok": True}
            if a == "wait_for_text":
                if payload["text"].lower() in self.text().lower():
                    return 200, {"ok": True, "found": True}
                return 408, {"detail": f"text {payload['text']!r} did not appear"}
            if a == "text":
                return 200, {"ok": True, "text": self.text()}
            if a == "select":
                return 200, {"ok": True, "selected": payload["values"]}
            if a == "downloads":
                return 200, {"ok": True, "downloads": list(self.downloads)}
            if a == "export_state":
                return 200, {"ok": True, "storage_state": {"cookies": [{"name": "s", "value": "v"}]}, "cookies": 1}
            if a == "screenshot":
                return 200, {"ok": True, "png_base64": base64.b64encode(b"PNG").decode()}
            if a in ("wait", "press", "scroll", "switch_page"):
                return 200, {"ok": True}
        return 400, {"detail": f"unhandled {method} {path}"}


class FakeVault:
    def __init__(self):
        self.store = {}
        self.filled = []
        self.grant = True

    async def store_secret(self, pool, *, entity_id, name, cred_type, plaintext, actor=None):
        self.store[name] = (cred_type, plaintext)
        return "env"

    async def open_secret(self, pool, *, entity_id, name, actor=None, include_deactivated=False):
        v = self.store.get(name)
        return v[1] if v else None

    async def fill(self, pool, *, entity_id, host, session, mapping, actor):
        if not self.grant:
            raise A.autofill.AutofillError(
                f"no standing grant for {host} — unattended autofill is refused.")
        self.filled.append((host, session, mapping[0]["ref"], mapping[0]["key"]))
        return {"filled": [{"ref": mapping[0]["ref"], "key": mapping[0]["key"]}], "skipped": []}


def _mk_async(fn):
    """_hold is awaited by the runner; the fake's is a plain call."""
    async def _wrapped():
        fn()
    return _wrapped


class FakeHarness:
    """The harness lane as the runner sees it: a cached upstream that may be
    stale, a wake that fills it, and HOLDERS that decide when it sleeps.

    Mirrors harness_backend: `ws` is the user's own Browser viewport, `runs`
    is one-shot agent runs, and the pod sleeps only when both reach zero. The
    runner no longer guesses who owns the browser, so neither does this."""

    def __init__(self, side):
        self.side = side
        self.url = None
        self.wakes = 0
        self.releases = []         # True = this leave actually put it to sleep
        self.forgets = 0
        self.dead = False          # the cached upstream refuses connections
        self.ws = 0                # live viewport sessions
        self.runs = 0              # one-shot holders

    async def wake(self):
        already = self.url is not None
        if not already:
            self.url = "harness-browser.entity-7.svc.cluster.local:8830"
            self.wakes += 1
            self.dead = False
        self.side.calls.append(("WAKE", None, None))
        return not already

    def forget(self):
        self.url = None
        self.forgets += 1

    def hold(self):
        self.runs += 1

    async def release(self, _woke=False):
        self.runs = max(0, self.runs - 1)
        slept = self.runs == 0 and self.ws == 0
        self.releases.append(slept)
        if slept:
            self.url = None


@pytest.fixture
def wired(monkeypatch):
    side = FakeSidecar()
    vault = FakeVault()
    harness = FakeHarness(side)

    async def call(method, path, payload=None, timeout=45.0):
        if harness.dead:
            raise A.AgentError("could not reach the browser: ClientConnectorError")
        return await side.call(method, path, payload, timeout)

    async def download(path, dest, *, idle_timeout=120.0):
        # the real _download streams the body to `dest`; the fake writes the
        # same bytes the sidecar holds, and records the GET like _call does
        if harness.dead:
            raise A.AgentError("could not reach the browser: ClientConnectorError")
        st, data = await side.call("GET", path)
        if st >= 400:
            raise A.AgentError(f"could not be fetched (HTTP {st})")
        dest.write_bytes(data)
        return len(data)
    monkeypatch.setattr(A, "_call", call)
    monkeypatch.setattr(A, "_download", download)
    monkeypatch.setattr(A.vault_api, "store_secret", vault.store_secret)
    monkeypatch.setattr(A.vault_api, "open_secret", vault.open_secret)
    monkeypatch.setattr(A.autofill, "fill", vault.fill)
    monkeypatch.setattr(A, "_wake", harness.wake)
    monkeypatch.setattr(A, "_forget_upstream", harness.forget)
    monkeypatch.setattr(A, "_release", harness.release)
    monkeypatch.setattr(A, "_hold", _mk_async(harness.hold))
    A._write_yaml("bank-ledger", A.normalize(SPEC))
    side.harness = harness
    return side, vault


def _run(**kw):
    # REUSE the installed loop; only create one when there is none, or the one
    # there is closed. Python 3.14's asyncio.get_event_loop() raises when none
    # is set, so whichever test ran last used to decide whether the next could
    # get a loop at all — but always creating a FRESH one is just as wrong the
    # other way: test_run_refuses_unknown_or_invalid_agents_and_double_runs
    # calls this twice and then awaits _active[...].done, an asyncio.Event
    # bound to the FIRST loop ("is bound to a different event loop"). One
    # stable loop per session satisfies both.
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError("closed")
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(
        A.run(None, entity_id=7, name="bank-ledger", actor="test", **kw))


# ── runs ──────────────────────────────────────────────────────────────

def test_full_run_ok_collects_download_and_seals_cookies(wired, tmp_path):
    side, vault = wired
    rec = _run()
    assert rec["status"] == "ok", rec
    assert rec["steps"] == 10 and rec["step"] == 10
    # the vault typed both sealed entries into the fields the locator found
    assert vault.filled == [("online.bank.example", "agent-bank-ledger", "e1", "user-id"),
                            ("online.bank.example", "agent-bank-ledger", "e2", "password")]
    assert side.typed == []                          # nothing typed in the clear
    # the download went where the agent said, under the state root
    out = rec["outputs"][0]
    assert out["source"] == "ledger.csv" and out["bytes"] == 8
    assert pathlib.Path(out["path"]).read_bytes() == b"a,b\n1,2\n"
    assert pathlib.Path(out["path"]).parent == tmp_path / "exports" / "bank"
    # cookies sealed under their own cred_type; session opened with the allow-list and closed
    assert vault.store["browser-state:bank-ledger"][0] == "browser_state"
    create = [p for m, p_, p in side.calls if m == "POST" and p_ == "/v1/sessions"][0]
    assert create["allow"] == ["online.bank.example", "login.bank.example"]
    assert "storage_state" not in create                  # first run: signed out
    assert ("DELETE", "/v1/sessions/agent-bank-ledger", None) in side.calls
    assert side.calls[0][0] == "WAKE"
    # a one-shot job: this run was the last holder out, so it slept the browser
    assert side.harness.releases == [True] and side.harness.url is None
    # the run record is on disk and load_agent reports it
    assert A.list_runs("bank-ledger")[0]["run_id"] == rec["run_id"]
    assert A.load_agent("bank-ledger")["last_run"]["status"] == "ok"


def test_a_browser_the_users_viewport_holds_is_left_alone(wired):
    """The user has the Browser app open. The run joins and leaves the holders
    like any other, and the VIEWPORT is what keeps the pod up — not a guess
    about who woke it."""
    side, vault = wired
    side.harness.url = "harness-browser.entity-7.svc.cluster.local:8830"
    side.harness.ws = 1                              # the user's tab holds it
    rec = _run()
    assert rec["status"] == "ok"
    assert side.harness.releases == [False]          # left up, correctly
    assert side.harness.url is not None


def test_a_browser_awake_with_no_holder_is_released_by_the_next_run(wired):
    """THE REGRESSION. A browser awake with nobody holding it — its owner
    crashed before its finally, or a viewport whose ws never reached 0. The
    old rule released only if THIS run woke it, so a run that found it awake
    left it running and refreshed the sweep boundary on the way past, and
    nothing would ever release it again. Measured 2026-09-07: 5.67 h
    continuous on entity-7. The last holder out must now put it to sleep."""
    side, vault = wired
    side.harness.url = "harness-browser.entity-7.svc.cluster.local:8830"
    side.harness.ws = 0                              # nobody is holding it
    rec = _run()
    assert rec["status"] == "ok"
    assert side.harness.releases == [True]
    assert side.harness.url is None


def test_stale_upstream_after_the_idle_sweep_is_forgotten_and_rewoken_cold(wired):
    """Day 2: the cached Service points at a pod the sweep scaled to zero."""
    side, vault = wired
    side.harness.url = "harness-browser.entity-7.svc.cluster.local:8830"
    side.harness.dead = True
    rec = _run()
    assert rec["status"] == "ok", rec
    assert side.harness.forgets == 1 and side.harness.wakes == 1
    assert side.harness.releases == [True]           # last holder out
    assert any("waking it cold" in e["msg"] for e in rec["log"])


def test_sealed_entry_is_typed_under_the_step_host_not_the_site(wired):
    """A sign-in form on login.bank.example for a task on online.bank.example:
    the entry was sealed under the page it was submitted on, and that is the
    host the vault must be asked with."""
    side, vault = wired
    spec = A.normalize({**SPEC, "steps": [
        {"go": "https://online.bank.example/"},
        {"type": {"field": "User ID", "key": "user-id", "host": "login.bank.example"}},
        {"secret": {"field": "Password", "key": "password", "host": "login.bank.example"}},
        {"click": {"role": "button", "name": "Sign On"}},
    ]})
    A._write_yaml("bank-ledger", spec)
    rec = _run()
    assert rec["status"] == "ok", rec
    assert [f[0] for f in vault.filled] == ["login.bank.example", "login.bank.example"]
    with pytest.raises(A.AgentError, match="entry host evil.example is not"):
        A.normalize({**SPEC, "steps": [{"secret": {"field": "P", "key": "p", "host": "evil.example"}}]})


def test_session_name_never_collides_for_long_names():
    a = "a" * 30 + "-first-agent"
    b = "a" * 30 + "-other-agent"
    sa, sb = A._session_name(a), A._session_name(b)
    assert sa != sb and len(sa) <= 40 and len(sb) <= 40
    import re
    rx = re.compile(r"^[a-z0-9][a-z0-9-]{0,38}[a-z0-9]$")
    assert rx.match(sa) and rx.match(sb)
    assert A._session_name("bank-ledger") == "agent-bank-ledger"


def test_second_run_restores_the_sealed_cookies(wired):
    side, vault = wired
    _run()
    _run()
    creates = [p for m, p_, p in side.calls if m == "POST" and p_ == "/v1/sessions"]
    assert "storage_state" not in creates[0]
    assert creates[1]["storage_state"] == {"cookies": [{"name": "s", "value": "v"}]}


def test_sealed_state_for_a_hosts_own_address_is_dropped_on_load(wired, host_zone):
    """A sealed state from an older run can carry a session for a Host's
    address, or a parent-domain cookie the browser would send to one. A run
    loads it without them, keeps the rest, and its record says how many."""
    side, vault = wired
    zone = host_zone                           # ".cseries.example.org"
    parent = zone.split(".", 2)[2]             # "example.org"
    desk = f"0123456789abcdef{zone}"
    keep = [
        {"name": "bank", "value": "1", "domain": "online.bank.example", "path": "/"},
        {"name": "sso", "value": "2", "domain": ".bank.example", "path": "/"},
        # host-only on the parent: sent to example.org alone, never a Host
        {"name": "site", "value": "3", "domain": parent, "path": "/"},
        {"name": "near", "value": "4", "domain": f".evil{parent}", "path": "/"},
        {"name": "s", "value": "v"},           # no domain: nothing to judge
    ]
    drop = [
        {"name": "desk", "value": "x", "domain": desk, "path": "/"},
        {"name": "desk2", "value": "x", "domain": desk.upper() + ".", "path": "/"},
        {"name": "zone", "value": "x", "domain": zone, "path": "/"},
        {"name": "parent", "value": "x", "domain": f".{parent}", "path": "/"},
        {"name": "byurl", "value": "x", "url": f"https://apps-k-{desk}/"},
    ]
    origins_keep = [{"origin": "https://online.bank.example", "localStorage": []}]
    origins_drop = [{"origin": f"https://{desk}", "localStorage": [{"name": "t", "value": "x"}]}]
    vault.store["browser-state:bank-ledger"] = ("browser_state", json.dumps(
        {"cookies": keep + drop, "origins": origins_keep + origins_drop}).encode())
    rec = _run()
    create = [p for m, p_, p in side.calls if m == "POST" and p_ == "/v1/sessions"][0]
    assert create["storage_state"]["cookies"] == keep
    assert create["storage_state"]["origins"] == origins_keep
    assert rec["state_dropped"] == len(drop) + len(origins_drop)
    assert any("6 sealed cookie(s)" in e["msg"] for e in rec["log"])


def test_failed_step_reports_number_reason_screenshot_and_page_text(wired):
    side, vault = wired
    side.fail_click = "Download"
    rec = _run()
    assert rec["status"] == "failed"
    assert rec["step"] == 9
    assert rec["error"].startswith("step 9 of 10: could not find 'Download'")
    assert "Accounts" in rec["page_text"]
    assert pathlib.Path(rec["screenshot"]).read_bytes() == b"PNG"
    assert ("DELETE", "/v1/sessions/agent-bank-ledger", None) in side.calls[-3:]
    assert "bank-ledger" not in A._active


def test_no_grant_fails_the_run_saying_so(wired):
    side, vault = wired
    vault.grant = False
    rec = _run()
    assert rec["status"] == "failed"
    assert "step 2 of 10" in rec["error"] and "no standing grant" in rec["error"]


def test_pause_if_parks_the_run_and_resume_continues(wired):
    side, vault = wired
    side.code_shown = True
    rec = _run(wait_s=0.5)
    assert rec["status"] == "waiting"
    assert "Enter the code" in rec["question"] and "Code" in rec["question"]
    assert rec["step"] == 6
    ok, msg = A.resume("bank-ledger", "123456")
    assert ok, msg
    r = A._active["bank-ledger"]
    asyncio.get_event_loop().run_until_complete(asyncio.wait_for(r.done.wait(), 5))
    final = A.list_runs("bank-ledger")[0]
    assert final["status"] == "ok", final
    assert ("e1", "123456") in side.typed          # the answer, typed + submitted
    assert A.resume("bank-ledger", "x") == (False, "bank-ledger is not running")


def test_pause_nobody_answers_fails_with_the_reason(wired, monkeypatch):
    side, vault = wired
    side.code_shown = True
    monkeypatch.setattr(A, "PAUSE_TIMEOUT_S", 0.2)
    rec = _run(wait_s=5)
    assert rec["status"] == "failed"
    assert "nobody answered" in rec["error"]


def test_a_scheduled_run_that_pauses_tells_the_user_and_again_if_it_then_fails(wired, monkeypatch):
    """The op lane reports only failures and a paused run returns 'waiting'
    (alive), so the runner itself must say so — and say so again when the
    wait times out, because that later failure reaches the scheduler
    nowhere."""
    side, vault = wired
    side.code_shown = True
    notes = []

    async def fake_inbox(payload):
        notes.append(payload)
        return {"ok": True}
    import server.bridge.memory_handlers as mh
    monkeypatch.setattr(mh, "handle_inbox_append", fake_inbox)
    monkeypatch.setattr(A, "PAUSE_TIMEOUT_S", 0.2)
    rec = asyncio.get_event_loop().run_until_complete(
        A.run(None, entity_id=7, name="bank-ledger", actor="t", trigger="schedule", wait_s=5))
    assert rec["status"] == "failed"
    assert [n["payload"]["kind"] for n in notes] == ["waiting", "failed"]
    assert "Enter the code" in notes[0]["body"] and "Agents" in notes[0]["body"]
    assert notes[1]["payload"]["run_id"] == rec["run_id"]
    # a host-triggered run tells the host (the tool result), not the inbox
    notes.clear()
    side.page = "login"
    rec = _run(wait_s=5)
    assert rec["status"] == "failed" and notes == []


def test_run_refuses_unknown_or_invalid_agents_and_double_runs(wired):
    with pytest.raises(A.AgentError, match="no agent named"):
        _run.__wrapped__ if False else asyncio.get_event_loop().run_until_complete(
            A.run(None, entity_id=1, name="ghost", actor="t"))
    side, _ = wired
    side.code_shown = True
    first = _run(wait_s=0.2)
    assert first["status"] == "waiting"
    again = _run(wait_s=0.2)
    assert again["run_id"] == first["run_id"]      # the same run, not a second
    A.resume("bank-ledger", "1")
    asyncio.get_event_loop().run_until_complete(
        asyncio.wait_for(A._active["bank-ledger"].done.wait(), 5))


# ── schedule ──────────────────────────────────────────────────────────

class FakePool:
    def __init__(self):
        self.executed = []

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "UPDATE 1"


def test_save_agent_creates_and_cancels_the_calendar_row(monkeypatch):
    appended = []

    async def fake_append(payload):
        appended.append(payload)
        return {"ok": True, "event_id": f"00000000-0000-0000-0000-00000000000{len(appended)}"}
    import server.bridge.memory_handlers as mh
    monkeypatch.setattr(mh, "handle_calendar_append", fake_append)
    pool = FakePool()
    spec = {**SPEC, "schedule": {"recurrence": "daily", "fire_at": "2026-09-05T06:00:00Z"}}
    saved = asyncio.get_event_loop().run_until_complete(
        A.save_agent(pool, entity_id=7, spec=spec, actor="t"))
    assert saved["schedule"]["event_id"].endswith("1")
    row = appended[0]
    assert row["payload"]["op"] == {"tool": "browser_agent",
                                    "args": {"op": "run", "name": "bank-ledger", "trigger": "schedule"}}
    assert row["recurrence"] == "daily" and row["event_at"] == "2026-09-05T06:00:00Z"
    # re-saving with a new schedule cancels the old row and makes a new one
    spec["schedule"]["fire_at"] = "2026-09-06T06:00:00Z"
    saved2 = asyncio.get_event_loop().run_until_complete(
        A.save_agent(pool, entity_id=7, spec=spec, actor="t"))
    assert saved2["schedule"]["event_id"].endswith("2")
    assert pool.executed and "status='cancelled'" in pool.executed[0][0]
    assert pool.executed[0][1][0].endswith("1")
    # deleting cancels too and removes the directory
    gone = asyncio.get_event_loop().run_until_complete(
        A.delete_agent(pool, entity_id=7, name="bank-ledger"))
    assert gone and pool.executed[-1][1][0].endswith("2")
    assert not (A.agents_root() / "bank-ledger").exists()


def test_a_scheduled_agent_that_became_invalid_still_has_its_row_cancelled(monkeypatch, host_zone):
    """An agent saved with a schedule before a Host's address was refused is
    invalid now, and its calendar row keeps firing. Deleting it, or saving a
    fixed version, must cancel that row, as it does for a valid file;
    otherwise the old row fails every recurrence, or runs beside the new one."""
    old_ev = "00000000-0000-0000-0000-0000000000aa"
    host_site = f"0123456789abcdef{host_zone}"

    def write_old():
        d = A._dir("bank-ledger")
        d.mkdir(parents=True, exist_ok=True)
        (d / "agent.yaml").write_text(yaml.safe_dump(
            {**SPEC, "site": host_site, "allow": [],
             "steps": [{"go": f"https://{host_site}/"}],
             "schedule": {"recurrence": "daily", "fire_at": "2026-09-05T06:00:00Z",
                          "event_id": old_ev}}))

    async def fake_append(payload):
        return {"ok": True, "event_id": "00000000-0000-0000-0000-0000000000bb"}
    import server.bridge.memory_handlers as mh
    monkeypatch.setattr(mh, "handle_calendar_append", fake_append)

    write_old()
    spec = A.load_agent("bank-ledger")
    assert "a Host's own address" in spec["invalid"]
    assert spec["schedule"]["event_id"] == old_ev
    pool = FakePool()
    assert _LOOP.run_until_complete(A.delete_agent(pool, entity_id=1, name="bank-ledger"))
    assert [a[0] for _, a in pool.executed] == [old_ev]

    write_old()
    pool = FakePool()
    fixed = {**SPEC, "schedule": {"recurrence": "daily", "fire_at": "2026-09-05T06:00:00Z"}}
    _LOOP.run_until_complete(A.save_agent(pool, entity_id=1, spec=fixed, actor="t"))
    assert [a[0] for _, a in pool.executed] == [old_ev]


def test_output_path_is_jailed_to_the_state_root(tmp_path):
    p = A._output_path("a", "exports/{name}/{date}.csv", "x.csv")
    assert p.parent == tmp_path / "exports" / "a"
    # the default lives OUTSIDE the agent's directory, so deleting the agent
    # does not delete what it exported
    assert A._output_path("a", None, "x.csv") == tmp_path / "agent-outputs" / "a" / "x.csv"
    assert A._output_path("a", "out/", "f.csv") == tmp_path / "out" / "f.csv"
    with pytest.raises(A.AgentError):
        A._output_path("a", "../../etc/x", "f")


# ── wiring ────────────────────────────────────────────────────────────

def test_wiring_direct_op_unattended_caps_registry_scopes_routes():
    root = pathlib.Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "brain"))
    from mastercode.runtime import direct_ops, meta_tool_caps
    assert "browser_agent" in direct_ops.DIRECT_OP_TOOLS
    assert direct_ops.is_unattended_op_tool("browser_agent")
    assert meta_tool_caps.READ_SAFE_OPS["browser_agent"] == {"list": 1, "get": 1, "runs": 1}
    from server.applications import scopes
    assert {"read:agents", "write:agents", "execute:agents"} <= scopes.PERMISSIONS
    reg = (root / "brain/mastercode/public_tool_registry.py").read_text()
    assert '"browser_agent": {' in reg and "op_tool='browser_agent'" in reg
    tools = tools_source(root)
    assert 'elif name == "browser_agent":' in tools and "async def _exec_browser_agent" in tools
    main = (root / "server/main.py").read_text()
    assert "app.include_router(browser_agents_routes.router)" in main
    from server.routes import browser_agents as R
    paths = {(r.path, tuple(sorted(r.methods))) for r in R.router.routes}
    assert ("/api/browser/agents/{name}/run", ("POST",)) in paths
    assert ("/api/browser/agents/{name}/resume", ("POST",)) in paths
    assert ("/api/browser/agents/{name}", ("PUT",)) in paths
