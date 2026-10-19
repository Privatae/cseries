"""The Memory app labels rules, stored skills and the copies rules hold —
the REAL app (system-apps/oracle) in Chromium against a stubbed kernel.

Rules are preferences marked rule_kind (2026-09-30). The ledger must say
which rows are rules and which are not:
  - a preference marked rule_kind shows its kind as a chip ("taught rule"),
    and its inspector says it is a rule;
  - a skill_memory row the boot pass left is a "stored skill", never a rule,
    and its inspector says how the person removes it;
  - a plain preference in a rule's own words links to the rule that holds it
    (the detail route's held_by_rule).

Skips rather than passes when Chromium is unavailable.

Run: pytest tests/test_memory_app_rule_labels_js.py
"""
from __future__ import annotations

import json
import pathlib

import pytest

playwright_api = pytest.importorskip(
    "playwright.sync_api", reason="playwright not available to drive the app JS"
)

_ROOT = pathlib.Path(__file__).resolve().parents[1]
ORIGIN = "http://host.test"
APP = _ROOT / "system-apps" / "oracle"

RULE, SKILL, HELD = 11, 12, 13
_ROW = {"status": "active", "importance": 1.0, "confidence": 1.0, "access_count": 0,
        "created_at": "2026-09-30T10:00:00Z", "t_valid_until": None}
ROWS = [
    {**_ROW, "id": RULE, "content": "invoices go by post", "memory_type": "preference",
     "metadata": {"rule_kind": "taught"}},
    {**_ROW, "id": SKILL, "content": "research a topic with the web_search tool",
     "memory_type": "skill_memory", "metadata": {}},
    {**_ROW, "id": HELD, "content": "invoices go by post", "memory_type": "preference",
     "metadata": {"about": {"kind": "project", "ref": "7"}}},
]
# The detail route's shape (server/routes/brain.py get_memory_detail).
DETAIL = {
    RULE: {"rule_kind": "taught", "stored_skill": False, "held_by_rule": None},
    SKILL: {"rule_kind": None, "stored_skill": True, "held_by_rule": None},
    HELD: {"rule_kind": None, "stored_skill": False, "held_by_rule": RULE},
}


def _serve(route):
    path = route.request.url[len(ORIGIN):].split("?")[0]
    if path.startswith("/system-apps/oracle/"):
        f = APP / path[len("/system-apps/oracle/"):]
        ctype = {".css": "text/css", ".js": "application/javascript",
                 ".html": "text/html", ".svg": "image/svg+xml"}.get(f.suffix, "application/octet-stream")
        if not f.is_file():
            return route.fulfill(status=404, body="")
        return route.fulfill(status=200, content_type=ctype, body=f.read_bytes())
    if path.startswith("/css/"):
        f = _ROOT / "dashboard/base" / path.lstrip("/")
        return route.fulfill(status=200 if f.is_file() else 404, content_type="text/css",
                             body=f.read_text() if f.is_file() else "")
    if path == "/api/brain/memories/stats":
        return _json(route, {"by_type": {"preference": 2, "skill_memory": 1},
                             "by_status": {"active": 3}, "total": 3})
    if path == "/api/brain/memories":
        return _json(route, {"memories": ROWS, "total": len(ROWS)})
    if path.startswith("/api/brain/memories/"):
        mid = int(path.rsplit("/", 1)[1])
        row = next(r for r in ROWS if r["id"] == mid)
        return _json(route, {**row, **DETAIL[mid], "t_valid_from": row["created_at"]})
    if path == "/api/brain/memory/semantic":
        return _json(route, {"results": []})
    return _json(route, {})


def _json(route, body):
    return route.fulfill(status=200, content_type="application/json", body=json.dumps(body))


@pytest.fixture()
def app():
    with playwright_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # no browser binary installed
            pytest.skip(f"chromium unavailable: {exc}")
        pg = b.new_page(viewport={"width": 1400, "height": 900})
        errors: list[str] = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.route(f"{ORIGIN}/**", _serve)
        pg.goto(f"{ORIGIN}/system-apps/oracle/index.html")
        pg.wait_for_function("document.querySelectorAll('.mem-row').length === 3")
        pg.errors = errors  # type: ignore[attr-defined]
        yield pg
        b.close()


def _row(pg, mid):
    return pg.locator(f'.mem-row[data-id="{mid}"]')


def _text(loc) -> str:
    """The DOM text, one space between words (labels are styled uppercase;
    the words themselves are what the app wrote)."""
    return " ".join((loc.text_content() or "").split())


def _open(pg, mid):
    _row(pg, mid).click()
    pg.wait_for_function(
        "document.querySelector('#inspector .ins-meta-row') && "
        f"document.querySelector('#inspector').textContent.includes('#{mid}')")
    return _text(pg.locator("#inspector"))


def test_a_marked_rule_shows_its_rule_chip(app):
    rule = _text(_row(app, RULE))
    assert "taught rule" in rule, rule
    assert "stored skill" not in rule
    text = _open(app, RULE)
    assert "A taught rule." in text, text
    assert app.errors == []


def test_an_unmoved_skill_row_is_a_stored_skill_not_a_rule(app):
    skill = _text(_row(app, SKILL))
    assert "stored skill" in skill, skill
    assert "rule" not in skill.replace("stored skill", ""), skill
    text = _open(app, SKILL)
    assert "Stored skill" in text and "not a rule" in text and "Archive or Delete" in text, text
    assert "A taught rule." not in text
    assert app.errors == []


def test_a_preference_held_by_a_rule_links_to_it(app):
    held = _text(_row(app, HELD))
    assert "rule" not in held and "stored skill" not in held, held
    text = _open(app, HELD)
    assert "Held by a rule" in text and f"Rule #{RULE} says the same words" in text, text
    link = app.locator(f'#inspector .lin-row[data-id="{RULE}"]')
    assert link.count() == 1
    link.click()
    app.wait_for_function("document.querySelector('#inspector').textContent.includes('A taught rule.')")
    assert app.errors == []
