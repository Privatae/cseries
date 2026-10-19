"""Execute the sidecar's in-page JavaScript. Nothing else here does.

`_SNAPSHOT_JS`, `_TEXT_JS` and `_FIND_TEXT_JS` in
sidecars/browser/control/sessions.py are handed to Playwright as strings.
The Python test suite stubs `page.evaluate` and returns canned dicts keyed
on substrings of those very scripts, so the JS itself has never run — the
suite is structurally unable to reach it.

That gap produced the worst bug this code has had. `nameOf` ended
`... || el.innerText || el.value`; an <input> has innerText '', so the
chain fell through to the field's CONTENTS and published them as the
element's "name" — inside the payload built specifically to be shown to a
model. For a password box with no aria-label (a <label for=…> plus a bare
input: the canonical accessible login form) that is the password. Found
2026-08-24 by an adversarial pass running the real JS against real
Chromium; reproduced here in ~40 lines and no browser.

These tests run the ACTUAL source of those scripts, extracted from the
module, under node against a synthetic DOM. They skip if node is absent
rather than pass silently — a skipped test says "unverified", a passing
stub says "verified", and the second is what got us here.

Run: pytest tests/test_browser_snapshot_js.py
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import textwrap

import pytest

_SESSIONS = (pathlib.Path(__file__).resolve().parents[1]
             / "sidecars" / "browser" / "control" / "sessions.py")

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node not available to execute the in-page JS")

SECRET = "hunter2-REAL-BANK-PW"


def _extract(name: str) -> str:
    """Pull one `NAME = r\"\"\"…\"\"\"` block out of the module source."""
    src = _SESSIONS.read_text()
    marker = f'{name} = r"""'
    assert marker in src, f"{name} missing from sessions.py — the snapshot contract moved"
    return src.split(marker, 1)[1].split('"""', 1)[0].strip()


def _run_snapshot(elements_js: str, *, max_elements=200, max_name=120) -> dict:
    """Run the real _SNAPSHOT_JS over a synthetic DOM, return its output."""
    harness = textwrap.dedent(f"""
        const snapshot = ({_extract("_SNAPSHOT_JS")});
        global.getComputedStyle = () => ({{visibility:'visible',display:'block',opacity:'1'}});
        const mk = (tag, attrs={{}}, extra={{}}) => ({{
          tagName: tag,
          innerText: extra.innerText ?? '',
          value: extra.value ?? '',
          isContentEditable: !!extra.ce,
          type: extra.type,
          disabled: !!extra.disabled,
          checked: extra.checked,
          href: extra.href,
          options: extra.options ?? [],
          labels: extra.labels,
          shadowRoot: extra.shadowRoot,
          getAttribute: n => (attrs[n] ?? null),
          getRootNode: () => ({{ querySelector: () => null }}),
          closest: () => null,
          checkVisibility: extra.checkVisibility,
          getBoundingClientRect: () =>
            (extra.rect ?? {{width:100,height:20,top:10,left:0,right:100,bottom:30}}),
        }});
        const els = ({elements_js});
        // querySelectorAll(SEL) returns the interactive set; the collector
        // also asks for '*' to find shadow hosts.
        global.document = {{
          querySelectorAll: (sel) => (sel === '*' ? els : els),
          title: 'Bank',
        }};
        global.location = {{ href: 'https://bank.example/login' }};
        global.innerWidth = 1440; global.innerHeight = 900;
        global.CSS = {{ escape: (x) => x }};
        global.window = {{}};
        const out = snapshot({{maxElements:{max_elements}, maxName:{max_name}}});
        // The snapshot returns {{info, nodes}}; nodes are live DOM handles
        // the Python side keeps. Only `info` is JSON.
        console.log(JSON.stringify(out.info));
    """)
    out = subprocess.run(["node", "--input-type=module", "-e", harness],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, f"node failed:\n{out.stderr}"
    return json.loads(out.stdout)


# ── the leak this file exists for ───────────────────────────────────


def test_password_value_is_never_published_as_a_name():
    """The canonical accessible login form: <label for=p> + bare input.
    No aria-label, no placeholder — exactly the case that leaked."""
    snap = _run_snapshot(
        "[mk('INPUT', {type:'password'}, {value:" + json.dumps(SECRET) + ", type:'password'})]")
    blob = json.dumps(snap)
    assert SECRET not in blob, f"password published in the snapshot: {blob}"
    assert snap["elements"][0]["name"] == ""


@pytest.mark.parametrize("field_type", ["text", "email", "tel", "search", "number"])
def test_no_field_contents_leak_for_any_input_type(field_type):
    """Not just passwords — a TOTP code, an account number or a
    memorable-word answer typed through `type` leaked the same way."""
    snap = _run_snapshot(
        f"[mk('INPUT', {{type:'{field_type}'}}, {{value:{json.dumps(SECRET)}, type:'{field_type}'}})]")
    assert SECRET not in json.dumps(snap)


def test_textarea_and_contenteditable_contents_do_not_leak():
    snap = _run_snapshot(
        "[mk('TEXTAREA', {}, {value:" + json.dumps(SECRET) + "}),"
        " mk('DIV', {contenteditable:'true'}, {ce:true, innerText:" + json.dumps(SECRET) + "})]")
    assert SECRET not in json.dumps(snap)


# ── while still being useful ────────────────────────────────────────


def test_submit_button_label_still_comes_from_value():
    """The one legitimate place .value is a LABEL rather than data —
    losing it would make login forms undrivable."""
    snap = _run_snapshot("[mk('INPUT', {type:'submit'}, {value:'Sign in', type:'submit'})]")
    assert snap["elements"][0]["name"] == "Sign in"


def test_accessible_names_still_win():
    snap = _run_snapshot(
        "[mk('INPUT', {type:'password','aria-label':'Password'}, {value:'x', type:'password'}),"
        " mk('INPUT', {type:'text',placeholder:'Username'}, {value:'y', type:'text'}),"
        " mk('BUTTON', {}, {innerText:'Continue'})]")
    assert [e["name"] for e in snap["elements"]] == ["Password", "Username", "Continue"]


def test_link_text_and_href_still_captured():
    snap = _run_snapshot(
        "[mk('A', {}, {innerText:'Statements', href:'https://bank.example/stmt'})]")
    e = snap["elements"][0]
    assert e["name"] == "Statements" and e["role"] == "link"
    assert e["href"] == "https://bank.example/stmt"


# ── the rest of the contract, now actually exercised ────────────────


def test_refs_are_unique_and_sequential():
    snap = _run_snapshot("[mk('BUTTON',{},{innerText:'a'}),mk('BUTTON',{},{innerText:'b'}),"
                         "mk('BUTTON',{},{innerText:'c'})]")
    refs = [e["ref"] for e in snap["elements"]]
    assert refs == ["e1", "e2", "e3"] and len(set(refs)) == 3


def test_zero_sized_elements_are_skipped():
    harness_els = ("[mk('BUTTON',{},{innerText:'hidden',"
                   "rect:{width:0,height:0,top:0,left:0,right:0,bottom:0}}),"
                   "mk('BUTTON',{},{innerText:'shown'})]")
    snap = _run_snapshot(harness_els)
    assert [e["name"] for e in snap["elements"]] == ["shown"]


def test_element_budget_is_reported_when_hit():
    snap = _run_snapshot("Array.from({length:50},(_,i)=>mk('BUTTON',{},{innerText:'b'+i}))",
                         max_elements=10)
    assert snap["element_count"] == 10 and snap["truncated"] is True


def test_names_are_clipped_to_the_budget():
    snap = _run_snapshot("[mk('BUTTON',{},{innerText:'x'.repeat(500)})]", max_name=20)
    assert len(snap["elements"][0]["name"]) <= 21   # 20 + the ellipsis


def test_url_and_title_come_back():
    snap = _run_snapshot("[]")
    assert snap["url"] == "https://bank.example/login" and snap["title"] == "Bank"
