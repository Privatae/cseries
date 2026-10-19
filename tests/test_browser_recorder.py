"""The recorder's Python half: events become steps, values never do.

The in-page JS is executed under node in tests/test_browser_recorder_js.py.
This pins what the process around it promises:

  - nothing is recorded while no recording is active — the binding drops
    events and returns False;
  - a `type` event becomes a step carrying the field's identity only; a
    password field becomes a `secret` step; no value is ever in a step;
  - field VALUES arrive only on commit events (click / press / submit), are
    held under the site, masked in every preview, and on stop are handed to
    formcapture under a single-use capture id — the same path "save these
    entries" uses;
  - navigation attribution: a load right after a click is wait_for_load, a
    load with no recent action is `go`; the address bar's own navigate is
    written once;
  - a recording that is never stopped expires.

Run: pytest tests/test_browser_recorder.py
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]
                       / "sidecars" / "browser"))

from control import formcapture, recorder  # noqa: E402

# Python 3.14 has no implicit event loop; the binding is a coroutine.
#
# THIS FILE DRIVES ITS OWN LOOP AND NEVER READS THE GLOBAL ONE (2026-09-15).
# It used to set the loop here at import and then call asyncio.get_event_loop()
# in _fire(). Both halves are wrong once anything else in the suite runs:
#
#   * the set happens at COLLECTION time, and every module that does the same
#     overwrites it, so the loop this file gets is whichever module imported
#     last -- not necessarily its own;
#   * asyncio.run() leaves the current loop UNSET on 3.14 (verified on 3.14.4),
#     and the files that sort before this one -- test_autofill_*,
#     test_automated, test_bastion_* -- all call it.
#
# So by the time these tests executed, the loop set above was gone and
# get_event_loop() raised "There is no current event loop in thread
# 'MainThread'". 8 of the 15 failed in a full run and ALL 15 passed when the
# file was run alone, which is why it survived: the obvious check is green.
#
# Holding the loop in a module global and using it directly makes this file
# independent of what any other module does to the interpreter's state.
_LOOP = asyncio.new_event_loop()

PASSWORD = "hunter2-REAL-BANK-PW"


def _app() -> TestClient:
    app = FastAPI()
    app.include_router(recorder.router)
    app.include_router(formcapture.router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clean():
    recorder._reset_for_tests()
    formcapture._reset_for_tests()
    recorder.set_page_provider(lambda: None)
    yield
    recorder._reset_for_tests()
    formcapture._reset_for_tests()


def _ev(t: str, **kw) -> str:
    base = {"t": t, "host": "online.bank.example", "frame_host": "online.bank.example",
            "url": "https://online.bank.example/login", "ms": int(time.time() * 1000)}
    base.update(kw)
    return json.dumps(base)


def _send(payload: str) -> bool:
    # _LOOP, never asyncio.get_event_loop(): see the note at the top of the file.
    return _LOOP.run_until_complete(recorder.on_event(None, payload))


# ── inert until started ───────────────────────────────────────────────

def test_nothing_recorded_while_inactive():
    assert recorder.is_active() is False
    assert _send(_ev("click", target={"role": "button", "name": "Sign On"})) is False
    assert recorder.current() is None


def test_status_when_idle():
    c = _app()
    assert c.get("/v1/record").json() == {"recording": False}
    r = c.post("/v1/record/stop", json={})
    assert r.status_code == 409


# ── steps ─────────────────────────────────────────────────────────────

def test_type_step_carries_identity_only_and_password_becomes_secret():
    c = _app()
    assert c.post("/v1/record/start", json={"name": "bank-ledger"}).status_code == 200
    assert _send(_ev("type", target={"role": "textbox", "name": "User ID", "tag": "input",
                                      "type": "text", "key": "user-id", "secret": False}))
    assert _send(_ev("type", target={"role": "textbox", "name": "Password", "tag": "input",
                                      "type": "password", "key": "password", "secret": True}))
    # editing the same field again is not a second step
    assert _send(_ev("type", target={"role": "textbox", "name": "Password", "tag": "input",
                                      "type": "password", "key": "password", "secret": True}))
    steps = recorder.current().steps
    assert steps == [
        {"type": {"field": "User ID", "key": "user-id", "secret": False,
                  "host": "online.bank.example"}},
        {"secret": {"field": "Password", "key": "password", "host": "online.bank.example"}},
    ]
    assert PASSWORD not in json.dumps(steps)


def test_click_records_role_and_name_and_reads_fields_only_at_commit():
    c = _app()
    c.post("/v1/record/start", json={"name": "bank-ledger"})
    # a click on a plain textbox carries no fields (not a commit point)
    assert _send(_ev("click", target={"role": "textbox", "name": "User ID", "tag": "input"}))
    assert recorder.current().fields == {}
    # a click on the submit button carries the page's filled fields
    assert _send(_ev("click", target={"role": "button", "name": "Sign On", "tag": "button"},
                     fields=[{"key": "user-id", "label": "User ID", "type": "text",
                              "secret": False, "checked": False, "value": "alex"},
                             {"key": "password", "label": "Password", "type": "password",
                              "secret": True, "checked": False, "value": PASSWORD}]))
    rec = recorder.current()
    assert rec.steps[-1] == {"click": {"role": "button", "name": "Sign On"}}
    assert set(rec.fields["online.bank.example"]) == {"user-id", "password"}
    # preview masks the password's VALUE but keeps its presence and length
    pv = rec.preview()
    assert PASSWORD not in json.dumps(pv)
    pw = [f for f in pv["fields"][0]["fields"] if f["key"] == "password"][0]
    assert pw["secret"] is True and pw["value"] == "" and pw["length"] == len(PASSWORD)
    uid = [f for f in pv["fields"][0]["fields"] if f["key"] == "user-id"][0]
    assert uid["value"] == "alex"


def test_select_and_press_and_submit():
    c = _app()
    c.post("/v1/record/start", json={"name": "bank-ledger"})
    assert _send(_ev("select", target={"role": "combobox", "name": "Format", "tag": "select"},
                     value="CSV"))
    assert _send(_ev("press", key="Enter", target={"role": "textbox", "name": "Code"},
                     fields=[{"key": "code", "label": "Code", "type": "text", "secret": False,
                              "checked": False, "value": "123456"}]))
    assert _send(_ev("submit", fields=[{"key": "code", "label": "Code", "type": "text",
                                        "secret": False, "checked": False, "value": "123456"}]))
    rec = recorder.current()
    assert rec.steps == [{"select": {"field": "Format", "value": "CSV"}}, {"press": "Enter"}]
    assert rec.fields["online.bank.example"]["code"]["value"] == "123456"


def test_iframe_host_is_kept_for_the_allow_list():
    c = _app()
    c.post("/v1/record/start", json={"name": "bank-ledger"})
    _send(_ev("click", target={"role": "button", "name": "Sign On"},
              frame_host="login.bank.example"))
    assert recorder.current().hosts == ["online.bank.example", "login.bank.example"]
    assert recorder.current().site() == "online.bank.example"


# ── navigation attribution ────────────────────────────────────────────

class _Frame:
    def __init__(self, url, parent=None):
        self.url = url
        self.parent_frame = parent


def test_bar_navigate_is_one_go_step():
    c = _app()
    c.post("/v1/record/start", json={"name": "bank-ledger"})
    recorder.note_bar_navigate("https://online.bank.example/")
    recorder.on_frame_navigated(_Frame("https://online.bank.example/"))
    assert recorder.current().steps == [{"go": "https://online.bank.example/"}]


def test_load_after_click_is_wait_for_load_and_cold_load_is_go():
    c = _app()
    c.post("/v1/record/start", json={"name": "bank-ledger"})
    _send(_ev("click", target={"role": "button", "name": "Sign On"}))
    recorder.on_frame_navigated(_Frame("https://online.bank.example/accounts"))
    recorder.on_frame_navigated(_Frame("https://online.bank.example/accounts"))  # same url: ignored
    rec = recorder.current()
    assert rec.steps == [{"click": {"role": "button", "name": "Sign On"}},
                         {"wait_for_load": True}]
    rec.last_action_ms = int(time.time() * 1000) - 60_000   # a minute of reading
    recorder.on_frame_navigated(_Frame("https://online.bank.example/spend"))
    assert rec.steps[-1] == {"go": "https://online.bank.example/spend"}
    # sub-frames never write a step
    recorder.on_frame_navigated(_Frame("https://ads.example/x", parent=_Frame("https://a/")))
    assert rec.steps[-1] == {"go": "https://online.bank.example/spend"}


def test_download_becomes_a_pattern_and_popup_a_switch():
    c = _app()
    c.post("/v1/record/start", json={"name": "bank-ledger"})

    class D:
        suggested_filename = "ledger-2026-09-04.csv"
    recorder.on_download(D())
    recorder.on_popup(1)
    assert recorder.current().steps == [
        {"expect_download": {"match": "*.csv", "example": "ledger-2026-09-04.csv"}},
        {"switch_page": {"index": 1}},
    ]


# ── start / stop / hand-off ───────────────────────────────────────────

def test_start_on_an_open_page_records_where_it_starts():
    class P:
        url = "https://online.bank.example/login"
    recorder.set_page_provider(lambda: P())
    c = _app()
    c.post("/v1/record/start", json={"name": "bank-ledger"})
    assert recorder.current().steps == [{"go": "https://online.bank.example/login"}]
    assert recorder.current().site() == "online.bank.example"


def test_start_rejects_bad_names_and_double_start():
    c = _app()
    assert c.post("/v1/record/start", json={"name": "Bad Name"}).status_code == 400
    assert c.post("/v1/record/start", json={"name": "bank-ledger"}).status_code == 200
    assert c.post("/v1/record/start", json={"name": "other"}).status_code == 409


def test_stop_hands_values_to_formcapture_once_and_masks_the_preview():
    c = _app()
    c.post("/v1/record/start", json={"name": "bank-ledger"})
    _send(_ev("click", target={"role": "button", "name": "Sign On"},
              fields=[{"key": "password", "label": "Password", "type": "password",
                       "secret": True, "checked": False, "value": PASSWORD}]))
    r = c.post("/v1/record/stop", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["recording"] is False and body["name"] == "bank-ledger"
    assert PASSWORD not in r.text
    cid = body["captures"]["online.bank.example"]
    # the kernel's save route emits it exactly once through formcapture
    e = c.post(f"/v1/form-capture/{cid}/emit", json={"keys": ["password"]})
    assert e.status_code == 200
    assert e.json()["fields"][0]["value"] == PASSWORD
    assert e.json()["fields"][0]["secret"] is True
    assert c.post(f"/v1/form-capture/{cid}/emit", json={"keys": ["password"]}).status_code == 404
    assert recorder.is_active() is False


def test_discard_drops_everything():
    c = _app()
    c.post("/v1/record/start", json={"name": "bank-ledger"})
    _send(_ev("click", target={"role": "button", "name": "Go"},
              fields=[{"key": "x", "label": "X", "type": "text", "secret": False,
                       "checked": False, "value": "v"}]))
    r = c.post("/v1/record/stop", json={"discard": True})
    assert r.json() == {"recording": False, "discarded": True, "name": "bank-ledger"}
    assert formcapture.live_count() == 0


def test_recording_expires(monkeypatch):
    c = _app()
    c.post("/v1/record/start", json={"name": "bank-ledger"})
    monkeypatch.setattr(recorder, "RECORD_TTL_S", 0)
    time.sleep(0.01)
    assert recorder.is_active() is False
    # Past its TTL a recording is auto-stopped and KEPT: only the typed values are dropped, never the steps.
    j = c.get("/v1/record").json()
    assert j.get("auto_stopped") is True and j.get("name") == "bank-ledger", j


def test_recorder_js_is_installed_on_the_shared_context():
    """app.py wires the bindings + init script on the SHARED context and the
    page events on every page, and the address bar notes its navigate."""
    src = (pathlib.Path(__file__).resolve().parents[1]
           / "sidecars" / "browser" / "control" / "app.py").read_text()
    assert 'expose_binding("__cseries_rec_active", _b_rec_active)' in src
    assert 'expose_binding("__cseries_rec", _b_rec)' in src
    assert "add_init_script(recorder._RECORDER_JS)" in src
    assert 'p.on("framenavigated", recorder.on_frame_navigated)' in src
    assert 'p.on("download", recorder.on_download)' in src
    assert "recorder.note_bar_navigate(body.url)" in src
    assert "app.include_router(recorder.router)" in src
    assert "recorder.set_page_provider(lambda: state.page)" in src
