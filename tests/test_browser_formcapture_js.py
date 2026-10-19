"""Execute the form-capture in-page JavaScript for real.

`_CAPTURE_JS` in sidecars/browser/control/formcapture.py is the one script
in this codebase that deliberately reads field VALUES — everything else
(notably `_SNAPSHOT_JS`) is built never to, because a snapshot goes to a
model. So the thing that must be pinned here is not that it reads values,
but exactly WHICH values it refuses to read, and that a password is
flagged so the preview can withhold it.

Same discipline as tests/test_browser_snapshot_js.py: run the ACTUAL
script under node against a synthetic DOM, and skip loudly if node is
missing rather than pass on a stub. A stubbed `page.evaluate` returning a
canned dict would report "verified" while the script had never run, which
is precisely how the snapshot leak survived.

Run: pytest tests/test_browser_formcapture_js.py
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import textwrap

import pytest

_MODULE = (pathlib.Path(__file__).resolve().parents[1]
           / "sidecars" / "browser" / "control" / "formcapture.py")

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node not available to execute the in-page JS")

PASSWORD = "hunter2-REAL-SIGNUP-PW"
CSRF = "csrf-token-do-not-store-me"


def _extract(name: str) -> str:
    src = _MODULE.read_text()
    marker = f'{name} = r"""'
    assert marker in src, f"{name} missing from formcapture.py — the capture contract moved"
    return src.split(marker, 1)[1].split('"""', 1)[0].strip()


def _run(elements_js: str, *, max_fields=60, max_value=4096,
         url="https://acme.example/signup") -> dict:
    """Run the real _CAPTURE_JS over a synthetic DOM, return its output."""
    harness = textwrap.dedent(f"""
        const capture = ({_extract("_CAPTURE_JS")});
        const mk = (tag, attrs={{}}, extra={{}}) => ({{
          tagName: tag,
          value: extra.value ?? '',
          type: extra.type,
          checked: extra.checked,
          labels: extra.labels,
          shadowRoot: extra.shadowRoot,
          isContentEditable: !!extra.ce,
          getAttribute: n => (attrs[n] ?? null),
          getRootNode: () => ({{ querySelector: () => null }}),
          closest: () => null,
        }});
        const els = ({elements_js});
        global.document = {{
          querySelectorAll: (sel) => (sel === '*' ? els : els),
          title: 'Acme — Sign up',
        }};
        global.location = {{ href: {json.dumps(url)} }};
        global.CSS = {{ escape: (x) => x }};
        global.window = {{}};
        const out = capture({{maxFields:{max_fields}, maxValue:{max_value}}});
        console.log(JSON.stringify(out));
    """)
    out = subprocess.run(["node", "--input-type=module", "-e", harness],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, f"node failed:\n{out.stderr}"
    return json.loads(out.stdout)


def _keys(snap) -> list:
    return [f["key"] for f in snap["fields"]]


# ── what must NEVER be captured ──────────────────────────────────────

def test_hidden_inputs_are_never_captured():
    """A hidden input is a CSRF or session token: per-visit by design, so
    replaying one is useless, and storing one banks a live bearer for no
    benefit. This is the field most likely to be on a signup form and most
    damaging to keep."""
    snap = _run(
        "[mk('INPUT', {name:'csrf'}, {type:'hidden', value:" + json.dumps(CSRF) + "}),"
        " mk('INPUT', {name:'email'}, {type:'email', value:'a@b.test'})]")
    blob = json.dumps(snap)
    assert CSRF not in blob, f"hidden token captured: {blob}"
    assert _keys(snap) == ["email"]


def test_submit_and_button_values_are_never_captured():
    """A submit's .value is its LABEL ('Create account'), not data."""
    snap = _run(
        "[mk('INPUT', {}, {type:'submit', value:'Create account'}),"
        " mk('INPUT', {}, {type:'button', value:'Cancel'}),"
        " mk('INPUT', {}, {type:'reset', value:'Reset'}),"
        " mk('INPUT', {name:'email'}, {type:'email', value:'a@b.test'})]")
    assert _keys(snap) == ["email"]


def test_file_inputs_are_never_captured():
    """A file input's value is a fake path (C:\\fakepath\\…), never the file."""
    snap = _run(
        "[mk('INPUT', {name:'avatar'}, {type:'file', value:'C:\\\\fakepath\\\\me.png'})]")
    assert snap["fields"] == []


def test_empty_fields_are_skipped():
    """Nothing to save, and they would bury the fields that matter."""
    snap = _run(
        "[mk('INPUT', {name:'middle'}, {type:'text', value:''}),"
        " mk('INPUT', {name:'email'}, {type:'email', value:'a@b.test'})]")
    assert _keys(snap) == ["email"]


def test_a_contenteditable_claiming_to_be_a_password_is_not_collected():
    """<div contenteditable type="password"> answers 'password' to
    getAttribute and defeated the guard in sessions.py once. Here it is not
    an input/select/textarea, so it is never collected at all — pinned so a
    future widening of the selector has to face this case."""
    snap = _run(
        "[mk('DIV', {type:'password'}, {ce:true, value:" + json.dumps(PASSWORD) + "})]")
    assert snap["fields"] == []
    assert PASSWORD not in json.dumps(snap)


# ── what must be captured, and how it is flagged ─────────────────────

def test_a_real_password_is_captured_and_flagged_secret():
    """It IS captured — that is the point, it goes to the vault. The flag
    is what lets the preview withhold the value from the screen."""
    snap = _run(
        "[mk('INPUT', {name:'password'}, {type:'password', value:"
        + json.dumps(PASSWORD) + "})]")
    assert len(snap["fields"]) == 1
    f = snap["fields"][0]
    assert f["secret"] is True
    assert f["value"] == PASSWORD


def test_a_text_field_is_not_flagged_secret():
    snap = _run("[mk('INPUT', {name:'email'}, {type:'email', value:'a@b.test'})]")
    assert snap["fields"][0]["secret"] is False


def test_key_prefers_autocomplete_then_name_then_id():
    """autocomplete is the standardised, redesign-surviving name for a
    field, so it wins; name and id are the form's own contract next."""
    snap = _run(
        "[mk('INPUT', {autocomplete:'email', name:'f_1', id:'x1'}, {type:'text', value:'a'}),"
        " mk('INPUT', {name:'given-name', id:'x2'}, {type:'text', value:'b'}),"
        " mk('INPUT', {id:'phone'}, {type:'text', value:'c'})]")
    assert _keys(snap) == ["email", "given-name", "phone"]


def test_autocomplete_off_is_not_used_as_a_key():
    """'off'/'on' are instructions to the browser, not field names — using
    one as the key would collide every such field onto one vault entry."""
    snap = _run(
        "[mk('INPUT', {autocomplete:'off', name:'realname'}, {type:'text', value:'a'})]")
    assert _keys(snap) == ["realname"]


def test_label_is_resolved_for_a_bare_input():
    """<label for=x> is how real signup forms name fields. Without it the
    user is approving a list of blanks."""
    snap = _run(
        "[mk('INPUT', {name:'e'}, {type:'email', value:'a@b.test',"
        " labels:[{innerText:'Email address'}]})]")
    assert snap["fields"][0]["label"] == "Email address"


def test_ticked_checkbox_records_state_not_a_value():
    snap = _run(
        "[mk('INPUT', {name:'tos'}, {type:'checkbox', checked:true}),"
        " mk('INPUT', {name:'marketing'}, {type:'checkbox', checked:false})]")
    assert _keys(snap) == ["tos"]
    assert snap["fields"][0]["checked"] is True
    assert snap["fields"][0]["value"] == ""


def test_select_and_textarea_are_captured():
    snap = _run(
        "[mk('SELECT', {name:'country'}, {value:'GB'}),"
        " mk('TEXTAREA', {name:'bio'}, {value:'hello'})]")
    assert _keys(snap) == ["country", "bio"]
    assert [f["type"] for f in snap["fields"]] == ["select", "textarea"]


# ── bounds ───────────────────────────────────────────────────────────

def test_field_count_is_bounded_and_reports_truncation():
    els = ",".join(
        f"mk('INPUT', {{name:'f{i}'}}, {{type:'text', value:'v'}})" for i in range(10))
    snap = _run(f"[{els}]", max_fields=4)
    assert len(snap["fields"]) == 4
    assert snap["truncated"] is True


def test_value_length_is_bounded():
    snap = _run(
        "[mk('INPUT', {name:'bio'}, {type:'text', value:'x'.repeat(9000)})]",
        max_value=100)
    assert len(snap["fields"][0]["value"]) == 100


def test_url_and_host_come_from_the_live_location():
    snap = _run("[mk('INPUT', {name:'email'}, {type:'email', value:'a@b.test'})]",
                url="https://shop.acme.example/register?ref=1")
    assert snap["url"] == "https://shop.acme.example/register?ref=1"
