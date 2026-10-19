"""Drive the floating-bubble chooser in a real browser.

`dashboard/base/js/floating-apps.js` is shell JavaScript: nothing in the Python
suite loads it, so until this file existed the only check it ever got was
`node --check`, which proves the parser is happy and nothing else.

That gap produced a real bug on the day the chooser was written (2026-09-09).
`closeMenu()` began with `if (!openMenuEl) return;` — an early return AHEAD of
the listener teardown — while `openMenu()` bound its document listeners inside
a `requestAnimationFrame`. Any close that beat the deferred binding therefore
re-added the listeners and then never removed them, and from the third menu
onward both Escape and outside-click silently stopped working. Reading the
source did not reveal it; clicking the thing four times did. The fix removed
the deferral entirely (onDocClick ignores clicks inside the owning bubble, so
synchronous binding is safe) and made the teardown unconditional.

The module is loaded from disk, unmodified, into a page that stubs only what
the shell would otherwise provide (AppRunner, ShellToken, iconMarkup,
window.open). Skips rather than passes when Chromium is unavailable — a
skipped test says "unverified", a passing stub says "verified", and the second
is what leaves bugs like the one above in place.

Run: pytest tests/test_floating_apps_menu_js.py
"""
from __future__ import annotations

import pathlib
import re

import pytest

playwright_api = pytest.importorskip(
    "playwright.sync_api", reason="playwright not available to drive the shell JS"
)

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_JS = _ROOT / "dashboard" / "base" / "js" / "floating-apps.js"
_CSS = _ROOT / "dashboard" / "base" / "css" / "floating-apps.css"
# Loaded so the component's var() references resolve against the REAL token
# set. Without it every var() silently falls back and a reference to a token
# that does not exist looks identical to one that does — which is how the
# first draft of this menu ended up styled with --cseries-surface and
# --cseries-border, neither of which is defined anywhere.
_TOKENS = _ROOT / "dashboard" / "base" / "css" / "tokens.css"

# Only what the shell itself would provide. Everything under test is the real
# file; if this stub list grows, the module has gained a dependency worth
# noticing.
_HARNESS = """<!doctype html><html><head><meta charset="utf-8">
<style>%(css)s</style>
<style>#floating-dock{position:fixed;bottom:24px;right:24px;display:flex;
flex-direction:column;gap:12px}</style></head><body>
<div id="floating-dock"></div>
<script>
  window.__launched = []; window.__opened = [];
  window.AppRunner  = { launch: (id) => window.__launched.push(id) };
  window.ShellToken = { headers: () => ({}), retry: async () => {}, init(){} };
  window.iconMarkup = () => '<span>C</span>';
  window.open = (url) => { window.__opened.push(url); return null; };
</script>
<script>%(js)s</script></body></html>"""


@pytest.fixture()
def page():
    html = _HARNESS % {
        "css": _TOKENS.read_text() + "\n" + _CSS.read_text(),
        "js": _JS.read_text(),
    }
    with playwright_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # no browser binary installed
            pytest.skip(f"chromium unavailable: {exc}")
        pg = browser.new_page()
        errors: list[str] = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.set_content(html)
        pg.wait_for_selector(".floating-bubble", timeout=5000)
        pg.errors = errors  # type: ignore[attr-defined]
        yield pg
        browser.close()


def _open(pg):
    pg.click(".floating-bubble")
    pg.wait_for_selector(".floating-bubble-menu", timeout=3000)


def test_bubble_offers_a_choice_instead_of_launching(page):
    """The ask: two options pop up, Circles and Hostmanager."""
    assert page.evaluate("window.__launched") == []
    _open(page)
    items = page.locator(".floating-bubble-menu-item")
    assert [items.nth(i).inner_text() for i in range(items.count())] == [
        "Circles",
        "Host Manager",
    ]
    # The click that opens the chooser must NOT also launch the app.
    assert page.evaluate("window.__launched") == []


def test_choosing_circles_launches_circles(page):
    _open(page)
    page.locator(".floating-bubble-menu-item").nth(0).click()
    assert page.evaluate("window.__launched") == ["circles"]
    assert page.locator(".floating-bubble-menu").count() == 0


def test_choosing_host_manager_opens_the_account_grid(page):
    _open(page)
    page.locator(".floating-bubble-menu-item").nth(1).click()
    assert page.evaluate("window.__opened") == [
        "https://privatae.ai/account#host-manager"
    ]


def test_menu_survives_the_click_that_opened_it(page):
    """The document listeners bind during that click; if they did not ignore
    clicks inside the bubble, the menu would close the instant it appeared."""
    _open(page)
    assert page.locator(".floating-bubble-menu").count() == 1


@pytest.mark.parametrize("dismiss", ["escape", "outside-click"])
def test_dismissal_still_works_after_repeated_cycles(page, dismiss):
    """THE REGRESSION TEST. The listener leak only showed from the third menu
    onward, so opening once would have passed while the bug was live."""
    for _ in range(4):
        _open(page)
        page.locator(".floating-bubble-menu-item").nth(0).click()

    _open(page)
    if dismiss == "escape":
        page.keyboard.press("Escape")
    else:
        page.mouse.click(10, 10)
    page.wait_for_timeout(150)
    assert page.locator(".floating-bubble-menu").count() == 0


def test_escape_returns_focus_to_the_bubble(page):
    """A keyboard user dismissing the menu must not be dropped at the top of
    the document."""
    _open(page)
    page.keyboard.press("Escape")
    page.wait_for_timeout(150)
    assert page.evaluate("document.activeElement.className").startswith(
        "floating-bubble"
    )


def test_every_token_the_menu_names_is_actually_defined(page):
    """Design tokens, not raw hex.

    Checked DIRECTLY rather than inferred from computed style. The first
    version of this test asserted the menu's computed background was not
    transparent, and it passed even when pointed at a token that does not
    exist: the @supports backdrop-filter rule below it overrides `background`,
    so the broken declaration never reached the computed value. Reading a
    property that something else also sets proves nothing about the
    declaration you meant to test.

    So: pull every var(--x) name out of the component's own stylesheet and ask
    the document whether each one resolves. That is the bug class — a token
    name that was never defined (the first draft named --cseries-surface,
    --cseries-border and --cseries-mint; none of the three exist).
    """
    names = sorted(set(re.findall(r"var\(\s*(--[a-zA-Z0-9-]+)", _CSS.read_text())))
    assert names, "no design tokens referenced — did the stylesheet move?"

    undefined = page.evaluate(
        """(names) => {
            const cs = getComputedStyle(document.documentElement);
            return names.filter((n) => cs.getPropertyValue(n).trim() === '');
        }""",
        names,
    )
    assert undefined == [], (
        f"floating-apps.css names tokens that tokens.css does not define: "
        f"{undefined}"
    )


def test_menu_is_not_nested_inside_the_bubble_button(page):
    """ARIA gives a <button> presentational children, so a <button
    role="menuitem"> nested inside the bubble is not required to be exposed to
    a screen reader at all. It renders in Chromium either way, which is why
    this asserts on the STRUCTURE and not on appearance."""
    _open(page)
    nested = page.evaluate(
        """() => {
            const m = document.querySelector('.floating-bubble-menu');
            return m ? !!m.closest('button') : null;
        }"""
    )
    assert nested is False, "the menu is inside a <button> — menuitems will not be exposed"


def test_menu_closes_when_focus_leaves_for_an_app_window(page):
    """A click inside an app-window iframe never reaches this document, so the
    outside-click listener alone would leave the chooser hanging over an app
    the user has just started using."""
    page.evaluate(
        """() => {
            const f = document.createElement('iframe');
            f.id = 'fake-app';
            f.srcdoc = '<button id="inside">x</button>';
            f.style.cssText = 'position:fixed;left:0;top:0;width:300px;height:200px';
            document.body.appendChild(f);
        }"""
    )
    _open(page)
    assert page.locator(".floating-bubble-menu").count() == 1
    page.frame_locator("#fake-app").locator("#inside").click()
    page.wait_for_timeout(200)
    assert page.locator(".floating-bubble-menu").count() == 0


def test_tooltip_is_hidden_while_the_menu_is_open(page):
    """The bubble is still hovered under the open menu; a tip floating over the
    chooser reads as a rendering bug."""
    _open(page)
    assert page.eval_on_selector(
        ".floating-bubble", "el => el.classList.contains('menu-open')"
    )
    assert page.eval_on_selector(
        ".floating-bubble-tip", "el => getComputedStyle(el).visibility"
    ) == "hidden"
    page.keyboard.press("Escape")
    page.wait_for_timeout(150)
    assert not page.eval_on_selector(
        ".floating-bubble", "el => el.classList.contains('menu-open')"
    )


def test_no_uncaught_errors(page):
    _open(page)
    page.locator(".floating-bubble-menu-item").nth(0).click()
    assert page.errors == []  # type: ignore[attr-defined]
