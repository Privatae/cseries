"""An app id never reaches Object.prototype for a glyph: the REAL icons.js under node.

CSeriesIcons looked a glyph up as `GLYPHS[id] || GLYPHS[ALIASES[id]]`. A plain
property read walks the prototype chain, so an id like "constructor" got
Object's constructor (rendered as `function Object() { [native code] }` inside
the glyph's <svg>) and "__proto__" got Object.prototype ("[object Object]"),
and has() said both had a glyph. The lookup is glyphFor now, own properties
only, for markup() and has() alike. This
pins, for the prototype's names:

  - has(id) is false;
  - markup(id) is no glyph <svg>: the app's letter, or its uploaded icon_url;
  - a real glyph and an alias (claude-code-harness -> claude-code) still render.

Skips if node is absent: a skipped test says "unverified"; a passing stub would
say "verified" about code that never ran.

Run: pytest tests/test_icons_own_glyph_js.py
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

_ICONS = pathlib.Path(__file__).resolve().parents[1] / "dashboard" / "base" / "js" / "icons.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node not available to execute icons.js")

# Every name an empty object answers through its prototype, plus __proto__.
PROTO_IDS = ["constructor", "__proto__", "toString", "valueOf", "hasOwnProperty",
             "isPrototypeOf", "propertyIsEnumerable", "toLocaleString",
             "__defineGetter__", "__lookupGetter__"]

_RUN = r"""
const window = {};
%s
const ids = %s;
// Pairs, not an object keyed by id: `out['__proto__'] = x` would set out's
// prototype and the id would never reach the JSON.
const out = [];
for (const id of ids) {
  out.push([id, {
    has: window.CSeriesIcons.has(id),
    bare: String(window.CSeriesIcons.markup(id, 20)),
    named: String(window.CSeriesIcons.markup({ id, name: id }, 20)),
    url: String(window.CSeriesIcons.markup({ id, name: id, icon_url: '/system-apps/x/icon.png' }, 20)),
  }]);
}
out.push(['_real', { has: window.CSeriesIcons.has('circles'), m: window.CSeriesIcons.markup('circles', 20) }]);
out.push(['_alias', { has: window.CSeriesIcons.has('claude-code-harness'),
                      m: window.CSeriesIcons.markup('claude-code-harness', 20),
                      target: window.CSeriesIcons.markup('claude-code', 20) }]);
process.stdout.write(JSON.stringify(out));
"""


def _run() -> dict:
    proc = subprocess.run(["node", "-e", _RUN % (_ICONS.read_text(), json.dumps(PROTO_IDS))],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    pairs = json.loads(proc.stdout)
    assert [k for k, _ in pairs] == PROTO_IDS + ["_real", "_alias"], pairs
    return dict(pairs)


@pytest.fixture(scope="module")
def result() -> dict:
    return _run()


@pytest.mark.parametrize("app_id", PROTO_IDS)
def test_a_prototype_name_has_no_glyph(result, app_id):
    assert result[app_id]["has"] is False, app_id


@pytest.mark.parametrize("app_id", PROTO_IDS)
def test_a_prototype_name_renders_its_letter_or_its_icon_never_a_glyph(result, app_id):
    r = result[app_id]
    for m in (r["bare"], r["named"], r["url"]):
        assert 'class="cseries-icon"' not in m, (app_id, m)
        assert "native code" not in m and "[object Object]" not in m, (app_id, m)
    letter = f'<span class="dock-tile-letter">{app_id[0].upper()}</span>'
    assert r["bare"] == letter and r["named"] == letter, (app_id, r)
    assert r["url"].startswith('<img src="/system-apps/x/icon.png"'), (app_id, r["url"])


def test_a_real_glyph_and_an_alias_still_render(result):
    assert result["_real"]["has"] is True
    assert result["_real"]["m"].startswith('<svg class="cseries-icon"')
    assert result["_alias"]["has"] is True
    assert result["_alias"]["m"] == result["_alias"]["target"]
    assert result["_alias"]["m"].startswith('<svg class="cseries-icon"')
