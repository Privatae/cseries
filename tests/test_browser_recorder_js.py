"""Execute the recorder's in-page JavaScript under node.

`_RECORDER_JS` in sidecars/browser/control/recorder.py is handed to
Playwright as an init script; the Python suite never runs it. This runs the
ACTUAL source against a synthetic DOM and pins the two promises that matter:

  - a `type` event names the field (label, key, password-ness) and carries
    NO value — typing into a password box never puts the password on the
    wire, in any event;
  - field values are read only at a commit point (a button click, Enter,
    submit), and only when `__cseries_rec_active()` says a recording is on.

Skips if node is absent — a skipped test says "unverified"; a passing stub
would say "verified" about code that never ran.

Run: pytest tests/test_browser_recorder_js.py
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import textwrap

import pytest

_RECORDER = (pathlib.Path(__file__).resolve().parents[1]
             / "sidecars" / "browser" / "control" / "recorder.py")

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node not available to execute the in-page JS")

SECRET = "hunter2-REAL-BANK-PW"


def _extract() -> str:
    src = _RECORDER.read_text()
    marker = '_RECORDER_JS = r"""'
    assert marker in src
    return src.split(marker, 1)[1].split('"""', 1)[0].strip()


# A tiny DOM: enough of Element/Document/events for the script's needs.
_DOM = r"""
class Ev { constructor(type, target, extra) { this.type = type; this.target = target;
           Object.assign(this, extra || {}); } preventDefault() {} }
class El {
  constructor(tag, attrs, opts) {
    this.tagName = tag.toUpperCase(); this.attrs = attrs || {}; this.children = [];
    this.parent = null; this.value = (opts && opts.value) || ''; this.innerText = (opts && opts.text) || '';
    this.checked = !!(opts && opts.checked); this.disabled = false; this.isContentEditable = false;
    this.labels = []; this.options = []; this.selectedIndex = 0;
    this.type = this.attrs.type || (this.tagName === 'INPUT' ? 'text' : undefined);
    this.shadowRoot = null;
  }
  getAttribute(n) { return n in this.attrs ? this.attrs[n] : null; }
  matches(sel) {
    return sel.split(',').some(s => {
      s = s.trim();
      const m = s.match(/^([a-z]+)?(\[([a-z-]+)(?:="([^"]*)")?\])?$/);
      if (!m) return false;
      if (m[1] && m[1].toUpperCase() !== this.tagName) return false;
      if (m[3]) { const v = this.getAttribute(m[3]); if (v === null) return false; if (m[4] !== undefined && v !== m[4]) return false; }
      return true;
    });
  }
  closest(sel) { let e = this; while (e) { if (e.matches(sel)) return e; e = e.parent; } return null; }
  getRootNode() { return document; }
  append(c) { c.parent = this; this.children.push(c); return c; }
  all() { const out = []; const walk = (n) => { for (const c of n.children) { out.push(c); walk(c); } }; walk(this); return out; }
  querySelectorAll(sel) { return this.all().filter(e => e.matches(sel)); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}
const listeners = {};
const document = {
  body: new El('body'),
  title: 'Example Bank — Sign On',
  addEventListener(t, fn) { (listeners[t] = listeners[t] || []).push(fn); },
  querySelectorAll(sel) { return document.body.querySelectorAll(sel); },
  querySelector(sel) { return document.body.querySelector(sel); },
};
const location = { href: 'https://online.bank.example/login', hostname: 'online.bank.example',
                   ancestorOrigins: [] };
const window = {};
const CSS = { escape: (s) => s };
const sent = [];
window.__cseries_rec = (p) => sent.push(JSON.parse(p));
let ACTIVE = true;
window.__cseries_rec_active = async () => ACTIVE;
const fire = async (t, target, extra) => { for (const fn of (listeners[t] || [])) await fn(new Ev(t, target, extra)); };
"""


def _run(body: str) -> dict:
    js = _DOM + "\n" + _extract() + "\n" + textwrap.dedent(body)
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


_FORM = r"""
const form = document.body.append(new El('form'));
const lu = form.append(new El('label', {for: 'uid'}, {text: 'User ID'}));
const uid = form.append(new El('input', {id: 'uid', name: 'userId', type: 'text'}, {value: 'alex'}));
uid.labels = [lu];
const lp = form.append(new El('label', {for: 'pw'}, {text: 'Password'}));
const pw = form.append(new El('input', {id: 'pw', name: 'password', type: 'password'}, {value: SECRET}));
pw.labels = [lp];
const btn = form.append(new El('button', {type: 'submit'}, {text: 'Sign On'}));
"""


def test_type_event_names_the_field_and_never_carries_the_value():
    res = _run(f"const SECRET = {json.dumps(SECRET)};" + _FORM + r"""
    (async () => {
      await fire('change', uid);
      await fire('change', pw);
      console.log(JSON.stringify({sent}));
    })();
    """)
    ev = res["sent"]
    assert [e["t"] for e in ev] == ["type", "type"]
    assert ev[0]["target"] == {"role": "textbox", "name": "User ID", "tag": "input",
                               "type": "text", "key": "userid", "secret": False}
    assert ev[1]["target"]["name"] == "Password" and ev[1]["target"]["secret"] is True
    assert ev[1]["target"]["key"] == "password"
    assert SECRET not in json.dumps(ev)
    assert "fields" not in ev[0] and "fields" not in ev[1]


def test_values_are_read_only_at_a_commit_point():
    res = _run(f"const SECRET = {json.dumps(SECRET)};" + _FORM + r"""
    (async () => {
      await fire('click', uid);              // a field: no values
      await fire('click', btn);              // the submit button: values read once
      await fire('keydown', uid, {key: 'a'});   // not Enter: nothing
      await fire('keydown', uid, {key: 'Enter'});
      await fire('submit', form);
      console.log(JSON.stringify({sent}));
    })();
    """)
    ev = res["sent"]
    assert [e["t"] for e in ev] == ["click", "click", "press", "submit"]
    assert "fields" not in ev[0]
    fields = {f["key"]: f for f in ev[1]["fields"]}
    assert fields["userid"]["value"] == "alex"
    assert fields["password"]["value"] == SECRET and fields["password"]["secret"] is True
    assert ev[1]["target"] == {"role": "button", "name": "Sign On", "tag": "button", "type": "submit"}
    assert ev[2]["key"] == "Enter" and {f["key"] for f in ev[2]["fields"]} == {"userid", "password"}
    assert {f["key"] for f in ev[3]["fields"]} == {"userid", "password"}
    assert ev[1]["host"] == "online.bank.example"


def test_nothing_is_sent_while_inactive():
    res = _run(f"const SECRET = {json.dumps(SECRET)};" + _FORM + r"""
    (async () => {
      ACTIVE = false;
      await fire('change', pw);
      await fire('click', btn);
      await fire('keydown', uid, {key: 'Enter'});
      await fire('submit', form);
      console.log(JSON.stringify({sent}));
    })();
    """)
    assert res["sent"] == []


def test_select_sends_the_chosen_label_and_top_host_comes_from_ancestor():
    res = _run(r"""
    const SECRET = 'x';
    location.ancestorOrigins = ['https://online.bank.example'];
    location.hostname = 'login.bank.example';
    const sel = document.body.append(new El('select', {name: 'fmt', 'aria-label': 'Format'}));
    sel.options = [{text: 'PDF', value: 'pdf'}, {text: 'CSV', value: 'csv'}];
    sel.selectedIndex = 1;
    (async () => {
      await fire('change', sel);
      console.log(JSON.stringify({sent}));
    })();
    """)
    ev = res["sent"]
    assert ev[0]["t"] == "select" and ev[0]["value"] == "CSV"
    assert ev[0]["target"]["name"] == "Format" and ev[0]["target"]["role"] == "combobox"
    assert ev[0]["host"] == "online.bank.example" and ev[0]["frame_host"] == "login.bank.example"


def test_name_of_a_field_never_falls_through_to_its_value():
    """An unlabelled password box must not be NAMED by its contents."""
    res = _run(f"const SECRET = {json.dumps(SECRET)};" + r"""
    const pw = document.body.append(new El('input', {type: 'password'}, {value: SECRET}));
    (async () => { await fire('change', pw); console.log(JSON.stringify({sent})); })();
    """)
    ev = res["sent"]
    assert ev[0]["target"]["name"] == ""
    assert SECRET not in json.dumps(ev)
