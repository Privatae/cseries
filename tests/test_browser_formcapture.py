"""The capture lifecycle in the sidecar: preview masking, single use, TTL.

The JS half (which fields are read at all) is pinned in
tests/test_browser_formcapture_js.py under node. This half pins what the
Python around it promises:

  - a password's VALUE never appears in the preview, but its presence and
    length do, so the user can still see the field is filled;
  - a capture is handed over exactly ONCE, so a leaked id cannot be
    replayed and a retry cannot silently re-read the form;
  - a capture that is never confirmed expires instead of sitting in the
    process holding plaintext.

No Chromium and no Playwright — the page is a stub whose evaluate()
returns what the real script would have returned.

Run: pytest tests/test_browser_formcapture.py
"""
from __future__ import annotations

import pathlib
import sys

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]
                       / "sidecars" / "browser"))

from control import formcapture  # noqa: E402

PASSWORD = "hunter2-REAL-SIGNUP-PW"

PAGE = {
    "url": "https://acme.example/signup",
    "title": "Acme — Sign up",
    "truncated": False,
    "considered": 4,
    "fields": [
        {"key": "email", "label": "Email address", "type": "email",
         "secret": False, "checked": False, "value": "alex@example.test"},
        {"key": "new-password", "label": "Choose a password", "type": "password",
         "secret": True, "checked": False, "value": PASSWORD},
        {"key": "tos", "label": "I agree", "type": "checkbox",
         "secret": False, "checked": True, "value": ""},
    ],
}


class StubPage:
    def __init__(self, result=None, raises=False):
        self.result = PAGE if result is None else result
        self.raises = raises

    async def evaluate(self, _script, _arg):
        if self.raises:
            raise RuntimeError("renderer gone")
        return self.result


@pytest.fixture
def client():
    formcapture._reset_for_tests()
    app = FastAPI()
    app.include_router(formcapture.router)
    yield TestClient(app)
    formcapture._reset_for_tests()


def _capture(client, page=None):
    formcapture.set_page_provider(lambda: page or StubPage())
    r = client.post("/v1/form-capture")
    assert r.status_code == 200, r.text
    return r.json()


# ── the preview withholds secrets ────────────────────────────────────

def test_preview_never_carries_a_password_value(client):
    body = _capture(client)
    assert PASSWORD not in r_text(body), "the password reached the preview"


def r_text(obj) -> str:
    import json
    return json.dumps(obj)


def test_preview_still_shows_that_the_password_field_is_filled(client):
    """Withholding the value must not withhold the FACT. A user who cannot
    see the field was filled cannot tell an empty box from a full one, and
    would approve a list that looks broken."""
    body = _capture(client)
    pw = [f for f in body["fields"] if f["key"] == "new-password"][0]
    assert pw["secret"] is True
    assert pw["value"] == ""
    assert pw["length"] == len(PASSWORD)
    assert pw["label"] == "Choose a password"


def test_non_secret_values_are_shown_in_clear(client):
    """The user typed these into a form on screen in front of them. Hiding
    them would only stop them noticing the page put something else there."""
    body = _capture(client)
    email = [f for f in body["fields"] if f["key"] == "email"][0]
    assert email["value"] == "alex@example.test"


def test_host_is_derived_from_the_live_url(client):
    body = _capture(client)
    assert body["host"] == "acme.example"


# ── single use ───────────────────────────────────────────────────────

def test_emit_returns_the_real_values_once(client):
    body = _capture(client)
    r = client.post(f"/v1/form-capture/{body['capture_id']}/emit",
                    json={"keys": ["email", "new-password"]})
    assert r.status_code == 200
    out = r.json()
    assert {f["key"] for f in out["fields"]} == {"email", "new-password"}
    pw = [f for f in out["fields"] if f["key"] == "new-password"][0]
    assert pw["value"] == PASSWORD, "the kernel must receive the real value to seal it"
    assert out["host"] == "acme.example"


def test_a_capture_cannot_be_emitted_twice(client):
    """Single use is the containment: a leaked id is worthless after the
    confirm, and a retry has to re-read the form rather than replay."""
    body = _capture(client)
    first = client.post(f"/v1/form-capture/{body['capture_id']}/emit",
                        json={"keys": ["email"]})
    assert first.status_code == 200
    second = client.post(f"/v1/form-capture/{body['capture_id']}/emit",
                         json={"keys": ["email"]})
    assert second.status_code == 404


def test_only_the_approved_keys_come_back(client):
    """The user ticked one field. The other two must not be sealed just
    because they were on the page."""
    body = _capture(client)
    r = client.post(f"/v1/form-capture/{body['capture_id']}/emit",
                    json={"keys": ["email"]})
    assert [f["key"] for f in r.json()["fields"]] == ["email"]


def test_approving_nothing_emits_nothing(client):
    body = _capture(client)
    r = client.post(f"/v1/form-capture/{body['capture_id']}/emit", json={"keys": []})
    assert r.status_code == 200
    assert r.json()["fields"] == []


def test_an_unknown_key_is_ignored_not_an_error(client):
    """The page can change under the preview; a vanished field is a normal
    outcome, not a failure that should lose the rest of the save."""
    body = _capture(client)
    r = client.post(f"/v1/form-capture/{body['capture_id']}/emit",
                    json={"keys": ["email", "field-that-went-away"]})
    assert r.status_code == 200
    assert [f["key"] for f in r.json()["fields"]] == ["email"]


def test_an_unknown_capture_id_is_404(client):
    formcapture.set_page_provider(lambda: StubPage())
    r = client.post("/v1/form-capture/nope-not-a-real-id/emit", json={"keys": []})
    assert r.status_code == 404


# ── expiry ───────────────────────────────────────────────────────────

def test_a_capture_expires_rather_than_holding_plaintext(client, monkeypatch):
    body = _capture(client)
    assert formcapture.live_count() == 1
    # Walk the clock past the TTL. monotonic is what _reap reads.
    real = formcapture.time.monotonic
    monkeypatch.setattr(formcapture.time, "monotonic",
                        lambda: real() + formcapture.CAPTURE_TTL_S + 1)
    assert formcapture.live_count() == 0
    r = client.post(f"/v1/form-capture/{body['capture_id']}/emit",
                    json={"keys": ["email"]})
    assert r.status_code == 404


def test_live_count_never_exposes_a_value(client):
    _capture(client)
    assert formcapture.live_count() == 1


# ── refusals ─────────────────────────────────────────────────────────

def test_a_page_with_nothing_filled_in_is_a_clear_409(client):
    empty = dict(PAGE, fields=[])
    formcapture.set_page_provider(lambda: StubPage(result=empty))
    r = client.post("/v1/form-capture")
    assert r.status_code == 409
    assert "fill the form first" in r.json()["detail"]


def test_a_page_with_no_host_is_refused(client):
    """about:blank has no host to file entries under. Sealing them with an
    empty host would put every such capture in one bucket."""
    blank = dict(PAGE, url="about:blank")
    formcapture.set_page_provider(lambda: StubPage(result=blank))
    r = client.post("/v1/form-capture")
    assert r.status_code == 409


def test_no_page_is_503(client):
    formcapture.set_page_provider(lambda: None)
    r = client.post("/v1/form-capture")
    assert r.status_code == 503


def test_a_renderer_failure_does_not_leak_the_exception_text(client):
    formcapture.set_page_provider(lambda: StubPage(raises=True))
    r = client.post("/v1/form-capture")
    assert r.status_code == 502
    assert "RuntimeError" in r.json()["detail"]
    assert "renderer gone" not in r.json()["detail"]
