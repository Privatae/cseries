#!/usr/bin/env python3
"""The Calendar app's contract.

Runs as pytest AND as a plain script in the container-free gate lane
(APP_ROOT=<repo> python3 scripts/test_calendar_app.py). No DB, no network; the
app in a browser is scripts/test_calendar_app_browser.py, and the two reads it
needed are scripts/test_calendar_order_pg.py.

WHAT THIS FILE DEFENDS.

1. THE APP'S CONTRACT. It declares exactly read:calendar and write:calendar,
   calls only calendar routes that exist under a scope it declares, writes
   nothing through innerHTML (titles, a task's prompt and a run's output come
   from the Host, a model or another app), reflows on a phone, and carries no
   price: it is built in and free, like Ships and Oversight.
2. IT REACHES EVERY HOST. It is image-baked with a current signed bundle in
   both bundle folders, has a dock glyph, and sits right after Circles on a dock
   nobody has changed (it carries the count of what is left today, which the
   Circles bubble carried before).
3. THE ROUTES. Every calendar route takes a calendar scope (GET read, the rest
   write), and the list's soonest-first read is a choice of two words.
"""
from __future__ import annotations

import os
import re
import sys
import typing
import zipfile

_ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain")]

from server.routes import calendar as routes                 # noqa: E402

APP_DIR = os.path.join(_ROOT, "system-apps", "calendar")
SCOPES = {"read:calendar", "write:calendar"}


def _read(rel: str) -> str:
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _manifest():
    from pathlib import Path
    from server.applications import manifest
    return manifest.load(Path(APP_DIR) / "app.yaml")


def test_manifest_declares_exactly_the_calendar_scopes():
    from server.applications import scopes
    m = _manifest()
    assert m.id == "calendar" and m.system, (m.id, m.system)
    assert set(m.permissions) == SCOPES and len(m.permissions) == 2, m.permissions
    for p in m.permissions:
        assert p in scopes.PERMISSIONS, p
    assert not m.target_entities, "every Host has a calendar, so every Host gets the window"
    assert m.prompt_behavior.user_path and m.prompt_behavior.what_it_is
    assert m.window.mobile == "responsive", "the phone is a layout of its own (tested at 390 x 844)"


def test_it_is_built_in_and_free():
    import yaml
    raw = yaml.safe_load(_read("system-apps/calendar/app.yaml"))
    assert not {"price_usd", "billing", "pricing"} & set(raw), sorted(raw)


def test_the_id_is_no_retired_or_reserved_one():
    from server.applications import kernel
    for name in ("RETIRED_APPS", "STORE_DELISTED_APPS", "KERNEL_REQUIRED_APPS"):
        assert "calendar" not in getattr(kernel, name), name


def _code() -> str:
    """app.js without its comments (a comment may name a route it never calls).
    The file has no string holding '//' or '/*' for this to cut."""
    js = _read("system-apps/calendar/app.js")
    js = re.sub(r"/\*.*?\*/", " ", js, flags=re.S)
    return re.sub(r"//[^\n]*", " ", js)


def _called_routes() -> set[str]:
    """Each fetch the app makes, as a route template: a literal, then `{x}`
    where it splices encodeURIComponent(...), then any literal suffix."""
    js = _code()
    rx = re.compile(r"fetch\(\s*'(/api/calendar[a-z/]*?)/?\??'"
                    r"(\s*\+\s*encodeURIComponent\([^)]*\)(?:\s*\+\s*'(/[a-z]+)\??')?)?")
    out = set()
    for lit, spliced, suffix in rx.findall(js):
        out.add(lit + ("/{x}" + (suffix or "") if spliced else ""))
    return out


def test_every_route_the_app_calls_exists_under_a_scope_it_declares():
    called = _called_routes()
    assert called == {"/api/calendar/list", "/api/calendar/read/{x}", "/api/calendar/events/{x}/runs",
                      "/api/calendar", "/api/calendar/complete/{x}", "/api/calendar/cancel/{x}"}, called
    assert len(re.findall(r"\bfetch\(", _code())) == 8, "a fetch this test cannot read would go unchecked"
    paths = {}
    for r in routes.router.routes:
        scope = [getattr(d.call, "_app_scope", None) for d in r.dependant.dependencies]
        paths.setdefault(re.sub(r"\{[^}]+\}", "{x}", r.path), set()).update(s for s in scope if s)
    for c in called:
        assert c in paths, f"the app calls {c}, which no route serves"
        assert paths[c] and paths[c] <= SCOPES, f"{c} needs {paths[c]}, which the app does not declare"


def test_every_route_takes_a_calendar_scope():
    for r in routes.router.routes:
        scope = [getattr(d.call, "_app_scope", None) for d in r.dependant.dependencies]
        want = "read:calendar" if "GET" in r.methods else "write:calendar"
        assert [s for s in scope if s] == [want], f"{sorted(r.methods)} {r.path} should need {want}: {scope}"


def test_the_soonest_first_read_is_a_choice_of_two_words():
    hint = typing.get_type_hints(routes.list_events)["order"]
    assert set(typing.get_args(hint)) == {"asc", "desc"}, hint


def test_nothing_from_the_table_reaches_innerhtml():
    js = _read("system-apps/calendar/app.js")
    for rx in (r"\.innerHTML\s*[+]?=", r"\.outerHTML\s*[+]?=", r"insertAdjacentHTML\s*\(",
               r"document\.write\s*\(", r"\.srcdoc\s*=", r"\beval\s*\(", r"new Function\s*\("):
        assert not re.search(rx, js), f"app.js writes HTML or runs text ({rx})"


def test_the_page_loads_only_its_own_files_and_the_desktops():
    html = _read("system-apps/calendar/index.html")
    srcs = re.findall(r'(?:src|href)="([^"]+)"', html)
    assert srcs and all(s.startswith(("/css/", "/system-apps/calendar/")) for s in srcs), srcs
    assert "<script>" not in html and "onclick=" not in html.lower(), "no inline script or handler"


def test_the_app_is_image_baked_with_a_current_signed_bundle():
    baked = [ln.strip() for ln in _read("scripts/image_baked_apps.txt").splitlines()
             if ln.strip() and not ln.strip().startswith("#")]
    assert "calendar" in baked
    assert baked == sorted(baked), "image_baked_apps.txt is kept sorted"
    members = {"manifest.yaml": "app.yaml"}
    members.update({f"payload/{n}": n for n in ("index.html", "app.js", "style.css", "icon.svg")})
    from server.applications import bundles
    with open(os.path.join(_ROOT, "etc", "cseries-apps.pub.raw"), "rb") as f:
        pub = f.read()
    for d in ("native-bundles", "factory-bundles"):
        path = os.path.join(_ROOT, d, "calendar.cseries")
        assert os.path.exists(path), f"{d}/calendar.cseries is missing"
        with zipfile.ZipFile(path) as z:
            for member, src in members.items():
                with open(os.path.join(APP_DIR, src), "rb") as f:
                    assert z.read(member) == f.read(), \
                        f"{d}/calendar.cseries carries a stale {src} — re-pack and re-sign it"
        with open(path, "rb") as f:
            bundles.verify(f.read(), public_key_raw=pub)


def test_the_dock_has_a_glyph_for_it_and_holds_it_after_circles():
    assert re.search(r"^\s+calendar: `", _read("dashboard/base/js/icons.js"), re.M)
    layout = _read("dashboard/base/js/desktop-layout.js")
    pinned = re.search(r"DEFAULT_PINNED = \[([^\]]*)\]", layout)
    ids = re.findall(r"'([a-z-]+)'", pinned.group(1)) if pinned else []
    assert ids[:2] == ["circles", "calendar"], ids


def _main() -> int:
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"OK    {name}")
            except AssertionError as e:
                fails += 1
                print(f"FAIL  {name}: {e}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"FAIL  {name}: {type(e).__name__}: {e}")
    print(f"\n{'FAILED' if fails else 'PASSED'} — {fails} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(_main())
