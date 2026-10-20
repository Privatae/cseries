"""The desktop document's RENDER check — a real browser loads it before it is
published or worn.

Why (2026-09-30). Five desktop designs a coding harness built were
published to the shared Designs shelf without ever being rendered. The harness
images have no browser, their acceptance checks were shell greps, and every
door a desktop document passes through — publish (routes/designs.py
publish_document), set-desktop and wear (routes/appearance.py
set_desktop_artifact) — ran only the TEXT floor, desktop_contract.check_document.
A document can pass every text rule and still throw on its first animation
tick or paint nothing.

What this does. It composes ONE page from the platform's own mounting code —
the shared sandbox policy (dashboard/base/js/sandbox-csp.js) and the stand-in
desktop bridge (dashboard/base/js/desktop-preview.js), read from the files this
kernel serves — that mounts the document exactly as the Designs shelf does: a
srcdoc frame with sandbox="allow-scripts", under the policy, with an inert
CSeriesDesktop carrying this Host's real app names. The browser sidecar loads
that page (sidecars/browser/control/document_verify.py) and reports what it
saw; this module reads the report against the desktop contract.

It FAILS a document that is BROKEN, and only that — the shell's own
contract (desktop-surface.js BRIDGE_SRC), not a taste in design:
  - it throws an uncaught error, or one inside its CSeriesDesktop.on callback
    (named, with the document's line and its text);
  - it never finishes loading, never paints a frame, or locks its renderer
    after it first paints (a script that never returns);
  - its page CRASHES (a renderer that runs out of memory or dies) or it
    NAVIGATES ITSELF away after it loaded (a reload, about:blank, anywhere):
    in the shell either takes the desktop away;
  - it shows nothing at all — no visible text, image or canvas with a pixel
    drawn, and all but MIN_PAINTED_SHARE of the frame one colour. A flat
    colour with a small clock is a wallpaper, not a blank;
  - in mode 'desktop', NOTHING in it carries data-cseries-app although
    CSeriesDesktop.apps handed it apps (the shell finds tiles by that
    attribute, shown or not — a start menu that reveals them on a click, a
    slow fade-in, both pass), or something covers EVERY visible tile so no
    click can reach one;
  - it declares cseries-bar and draws no bar: no dock, launcher or window list
    (data-cseries-dock / -activate / -window) anywhere, and no visible control
    inside the strip. An auto-hiding bar passes.

AND ON A PHONE. A document that passes at desktop size is loaded again at
PHONE_VIEWPORT, the way a phone mounts it (CSeriesDesktop.phone true, the shell
keeping its own taskbar, so no bar is expected), and the same rules apply
there: one that throws, shows nothing or has no reachable tile on a phone is
refused, with "on a phone" in the reason. App tiles sitting past the side
edges of a phone screen are a WARNING, not a refusal: on a phone the shell's
taskbar and its All Apps launcher stay, so every app can still be opened —
that is a layout to fix, not a broken desktop (one worn design put all 15 apps in one
centred row, 8 of them wholly past the
edges of a 360 px screen). When the phone size cannot be asked, the
desktop-size verdict stands and the result says the phone was not checked.

Every look keeps the browser for the next one (SECOND_LOOK_LINGER_S), so the
phone pass costs one more load in a browser already awake, not another wake.

The same bytes always get the same answer. A FAILED verdict is remembered by
the document's digest (state_dir()/failed.json): when the browser cannot be
asked again, those bytes are still refused — "not checked" never replaces a
failure — until a browser actually passes them.

The settle, SETTLE_MS, is the later of the shell's own two deadlines
(desktop-surface.js BRIDGE_SRC): 1.5 s after the app list arrives it adds a
fallback grid when no [data-cseries-app] exists, and 2 s after it takes its
taskbar back when a declared bar drew nothing. A document that has not drawn
by then is one the shell itself would have patched over.

What it reads cannot be talked round by the document: the sidecar measures
from a CDP isolated world (the document's own querySelectorAll,
getBoundingClientRect, getComputedStyle and requestAnimationFrame are not
the ones it calls) and from the screenshot's pixels. Nor can the document
tell the check from the desktop by the two tells it used to have: the browser
runs without the automation flag (navigator.webdriver is false), and errors
are read over CDP — there is no flag in the document's world.

NOT CHECKED is its own answer, never a pass: the sidecar could not be woken
(plan headroom, cold-wake timeout), an older sidecar has no /v1/verify/document
route, the call failed. Each door decides what not-checked means for it — see
designs.publish_document and appearance.set_desktop_artifact.

An older sidecar's answer cannot change by asking again: it is REMEMBERED
(sidecar_outdated) together with the browser image that gave it, and a path
no person is waiting on (the worn desktop's background re-check, an edit to
the worn desktop, a landed build) does not wake the browser for it again
until a wake reports a different image. A person's own set / wear / publish
still asks.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from server.applications.desktop_contract import detect_mode, parse_bar

log = logging.getLogger("privatae.pc.desktop_render")

# The Designs shelf renders every preview at this size and scales it down
# (system-apps/designs/app.js, STAGE_W / STAGE_H) — "designs are drawn for a
# desktop". The check measures the same stage.
VIEWPORT = (1440, 900)
# The phone pass. On a phone the shell mounts the SAME document in a frame the
# screen's width and its height less the taskbar. 360 CSS px is the narrowest
# width common phones report (and the classic Android portrait viewport), so a
# layout that fits it fits the wider ones; 640 is that portrait screen's height
# — with the browser's bars and the shell's 52 px taskbar taken off, a real
# phone shows less, never more.
PHONE_VIEWPORT = (360, 640)
# A rectangle no tile can sit outside: "shown anywhere at all".
_FAR = 100000.0
SETTLE_MS = 2000
# Below this share of pixels differing from the frame's most common colour, the
# document "paints nothing". Measured 2026-09-30 in the live browser sidecar
# image (Chromium 152, headed) at 1440x900, over several runs: the platform
# template 4.5%-11% and five real Halloween designs 12%-82% (their
# ambient animation moves the number between runs); a blank page and a single
# background colour 0%.
# 0.1% is ~1,300 pixels — one short line of text clears it, a stray 1-px
# border does not.
MIN_PAINTED_SHARE = 0.001
# A content rectangle (visible text, an image, a canvas) with at least this
# many pixels away from the frame's dominant colour has something drawn in it:
# one 13 px glyph clears it, a 1-px rule or a single anti-aliased edge does not.
CONTENT_MIN_PX = 12
# The id of the iframe ELEMENT that mounts the document in the composed page —
# found by the element, never by the frame's window.name, which the document
# itself can set.
SUBJECT = "subject"
SIDECAR_PATH = "/v1/verify/document"
# The browser answers these when it has no render-check route at all (an image
# older than the route): 404 for an unknown path, 405 for a path that exists
# with another method, 501 for "not implemented".
_UNSUPPORTED = ("HTTP 404", "HTTP 405", "HTTP 501")
# A document that has not drawn by SETTLE_MS is looked at again, once, after
# this much longer before it is called blank: a design that fades in or draws
# after a web font loads is slow, not broken. Only the
# "shows nothing" verdict is re-asked; every other failure stands at once.
SETTLE_RETRY_MS = 10000
LINGER_S = 240.0
# How long the browser stays up after a render check, for the next one in the
# same burst (harness_backend.linger). Measured 2026-10-01 on a Host with
# desktop history to read: the 14 gaps between consecutive
# revisions of its 10 desktop artifacts were 17 s (x5), 28, 30, 127, 147, 171
# and 228 s inside a working session, then 739 s, 18 h and 45 h between
# sessions. 240 s keeps every in-session gap warm and lets a session's end go
# cold. A wake the plan refuses releases it at once (ensure_awake).
_MOUNT_SCRIPTS = ("js/sandbox-csp.js", "js/desktop-preview.js")
# What counts as "the bar was drawn". The shell's own test (desktop-surface.js
# barDrawn) is that a dock, an activate control or a window list EXISTS in the
# document — shown or not, so an auto-hiding bar is a bar. A bar drawn in its
# own markup without those attributes still counts when a control a person
# can press is visible inside the declared strip.
_BAR_ELEMENTS = "[data-cseries-dock],[data-cseries-activate],[data-cseries-window]"
_BAR_CONTROLS = ("button,a[href],[role=button],[data-cseries-activate],[data-cseries-window],"
                 "[data-cseries-launcher],[data-cseries-show-desktop],[data-cseries-help],[data-cseries-machine]")
# What the stand-in bridge logs when a CSeriesDesktop.on callback throws
# (dashboard/base/js/desktop-preview.js L): the check reads it from the console.
_CALLBACK_MARK = "cseries desktop: a CSeriesDesktop.on callback threw"
_DOCTYPE_RE = re.compile(r"[\t\n\f\r ]*<!doctype[^>]*>", re.IGNORECASE)


def _dashboard_dirs() -> list[Path]:
    """Where the running kernel serves /js/ from — the custom overlay first,
    then the base (server/main.py DASHBOARD_CUSTOM / DASHBOARD_BASE, the order
    its static handler uses) — then the tree this file sits in, which is the
    base in the image and the only source outside the kernel (a test)."""
    dirs: list[Path] = []
    m = sys.modules.get("server.main")
    if m is not None:
        dirs = [Path(d) for d in (getattr(m, "DASHBOARD_CUSTOM", None),
                                  getattr(m, "DASHBOARD_BASE", None)) if d is not None]
    return dirs + [Path(__file__).resolve().parents[2] / "dashboard" / "base"]


def _mount_script(rel: str) -> str:
    from server.static_paths import inside
    for d in _dashboard_dirs():
        # inside(): the overlay's own rule — a custom/ copy that links out of
        # custom/ is not read (server/main.py OverlayStaticFiles).
        p = inside(d, rel)
        if p is not None and p.is_file():
            return p.read_text(encoding="utf-8")
    raise FileNotFoundError(f"/{rel} is not served by this kernel")


def _json_in_script(value) -> str:
    """JSON that cannot close the <script> it sits in."""
    return json.dumps(value).replace("<", "\\u003c")


def compose_page(html: str, *, apps: list[dict], host: str,
                 width: int = VIEWPORT[0], height: int = VIEWPORT[1],
                 phone: bool = False) -> str:
    """The page the sidecar loads: the document mounted the way the Designs
    shelf mounts it (system-apps/designs/app.js sealed()) — the stand-in
    bridge, then the sandbox policy, in a sandbox="allow-scripts" srcdoc
    frame whose ELEMENT has id SUBJECT. Nothing in the document's world marks
    it as a check: the stand-in is the one every preview gets. `phone`: the
    stand-in says CSeriesDesktop.phone and hands no bar, as a phone does."""
    scripts = []
    for rel in _MOUNT_SCRIPTS:
        src = _mount_script(rel)
        if re.search(r"</script", src, re.IGNORECASE):
            # Inlined below; a literal closing tag would end the script early.
            raise ValueError(f"/{rel} contains a literal </script> and cannot be inlined")
        scripts.append(f"<script>{src}</script>")
    opts = {"apps": [{"id": str(a.get("id") or ""), "name": str(a.get("name") or a.get("id") or "")}
                     for a in apps],
            "host": str(host or "")}
    if phone:
        opts["phone"] = True
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\"><title>desktop render check</title>"
        "<style>html,body{margin:0;padding:0;overflow:hidden;background:#fff}"
        f"iframe{{display:block;border:0;width:{int(width)}px;height:{int(height)}px}}</style>"
        "</head><body>" + "".join(scripts)
        + f"<iframe id=\"{SUBJECT}\" sandbox=\"allow-scripts\"></iframe>"
        "<script>(function(){var f=document.getElementById(" + _json_in_script(SUBJECT) + ");"
        "f.srcdoc=window.CSeriesSandbox.withSandboxCsp(window.DesktopPreview.instrument("
        + _json_in_script(str(html or "")) + "," + _json_in_script(opts) + "));})();</script>"
        "</body></html>")


def expectations(html: str) -> dict:
    """What the contract says this document must visibly draw, from the same
    static-head reads the floor uses."""
    mode = detect_mode(html)
    bar = parse_bar(html)
    if not bar or "invalid" in bar or mode != "desktop":
        bar = None
    return {"mode": mode, "bar": bar}


def _bar_rect(bar: dict, width: int, height: int) -> list[float]:
    size, edge = float(bar["size"]), bar["edge"]
    if edge == "top":
        return [0, 0, width, size]
    if edge == "left":
        return [0, 0, size, height]
    if edge == "right":
        return [width - size, 0, size, height]
    return [0, height - size, width, size]


def probes_for(expect: dict, apps_handed: int,
               width: int = VIEWPORT[0], height: int = VIEWPORT[1],
               phone: bool = False) -> dict:
    out: dict = {}
    if expect.get("mode") == "desktop" and apps_handed:
        out["tiles"] = {"selector": "[data-cseries-app]", "kind": "attached"}
        out["tiles_reach"] = {"selector": "[data-cseries-app]", "kind": "reachable"}
        if phone:
            # Where the tiles are, for the phone warning: shown at any height
            # inside the screen's width, and shown anywhere at all. The
            # difference sits past a side edge.
            out["tiles_across"] = {"selector": "[data-cseries-app]", "kind": "shown",
                                   "rect": [0, -_FAR, float(width), 2 * _FAR]}
            out["tiles_anywhere"] = {"selector": "[data-cseries-app]", "kind": "shown",
                                     "rect": [-_FAR, -_FAR, 2 * _FAR, 2 * _FAR]}
    if expect.get("bar") and not phone:
        out["bar"] = {"selector": _BAR_ELEMENTS, "kind": "attached"}
        out["bar_strip"] = {"selector": _BAR_CONTROLS, "kind": "shown",
                            "rect": _bar_rect(expect["bar"], width, height)}
    return out


def _injection_line(html: str) -> int:
    """The line the stand-in and the policy are inserted on (after a leading
    doctype, else line 1). They carry no newline, so every OTHER line number
    the browser reports is the document's own; on this one the column is
    shifted by the injected text and is not reported."""
    m = _DOCTYPE_RE.match(str(html or ""))
    return (m.group(0).count("\n") + 1) if m else 1


def _describe_error(err: dict, lines: list[str], inj_line: int) -> dict:
    out = {"message": err.get("message") or "", "name": err.get("name") or ""}
    line = err.get("line")
    if err.get("url") == "about:srcdoc" and isinstance(line, int) and line >= 1:
        out["line"] = line
        if line != inj_line and isinstance(err.get("column"), int):
            out["column"] = err["column"]
        if line <= len(lines):
            text = lines[line - 1].strip()
            out["source"] = text if len(text) <= 160 else text[:160] + "…"
    return out


def _error_sentence(e: dict) -> str:
    msg = e.get("message") or "(no message)"
    name = e.get("name")
    if name and not msg.startswith(name):
        msg = f"{name}: {msg}"
    where = ""
    if e.get("line"):
        where = f" at line {e['line']}" + (f", column {e['column']}" if e.get("column") else "")
        if e.get("source"):
            where += f": `{e['source']}`"
    what = "an error in its CSeriesDesktop.on callback" if e.get("callback") else "an uncaught error"
    return f"{what}{where} — {msg}"


def _callback_errors(report: dict) -> list[dict]:
    """What the stand-in bridge logged for a CSeriesDesktop.on callback that
    threw (it swallows the throw, as the real bridge does): the console line
    is the mark, then the error's own stack."""
    out = []
    for c in report.get("console_errors") or []:
        text = str((c or {}).get("text") or "")
        if not text.startswith(_CALLBACK_MARK):
            continue
        first = (text[len(_CALLBACK_MARK):].lstrip("\n").splitlines() or [""])[0].strip()
        name, sep, msg = first.partition(": ")
        if not sep or " " in name:
            name, msg = "", first
        out.append({"message": msg, "name": name, "callback": True,
                    **((c or {}).get("stack_at") or {})})
    return out


def judge(report: dict, *, html: str, expect: dict, apps_handed: int) -> dict:
    """Read the sidecar's report against the contract. Returns
    {checked, ok, why, failures, errors, paint, drawn, expected} — or
    {checked: False, reason} when the report shows the CHECK broke rather
    than the document (the frame could not be captured or probed, the
    browser itself failed)."""
    if not isinstance(report, dict) or "loaded" not in report:
        return {"checked": False, "reason": "the browser returned no render report"}
    failures: list[str] = []
    lines = str(html or "").splitlines()
    inj = _injection_line(html)
    raw = [e for e in (report.get("uncaught") or []) if isinstance(e, dict)] + _callback_errors(report)
    errors = []
    for e in raw:
        d = _describe_error(e, lines, inj)
        if e.get("callback"):
            d["callback"] = True
        errors.append(d)

    # The page went away under the check: the document's doing, kept with
    # every error already seen.
    gone = False
    if report.get("crashed"):
        gone = True
        failures.append(str(report["crashed"]))
    if report.get("navigated"):
        gone = True
        failures.append(
            f"it navigated itself away after it loaded (to {report['navigated']}) — in the "
            "shell that replaces the desktop with that page; a desktop never reloads or "
            "leaves itself"
            + ("; this one reloads itself again after it has already reloaded once, a "
               "reload loop that would never end" if report.get("reload_loop") else ""))

    if report.get("reload_load_error"):
        gone = True
        failures.append(str(report["reload_load_error"])
                        + " — a slow load, not a navigation; a desktop does not reload itself")
    if report.get("deadline") and not gone:
        gone = True
        failures.append(str(report["deadline"]))
    if not gone:
        if not report.get("loaded"):
            failures.append("the document did not load — "
                            + str(report.get("load_error") or "navigation failed"))
        elif not report.get("subject_found"):
            return {"checked": False, "reason": "the check page did not mount the document"}
        elif not report.get("ready"):
            failures.append("the document never painted a frame: "
                            + str(report.get("ready_error") or "no frame"))
    # Errors are read even when loading failed: a script that threw before
    # hanging is the likeliest cause, and it is what the Host must fix.
    failures += [_error_sentence(e) for e in errors[:3]]
    if len(errors) > 3:
        failures.append(f"…and {len(errors) - 3} more uncaught error(s)")

    paint = report.get("paint")
    drawn = report.get("probes") or {}
    if report.get("unresponsive") and not gone:
        # It painted, then locked its renderer: in the shell that freezes the
        # desktop. The document's fault, never "the check broke".
        failures.append(str(report["unresponsive"]))
    problem = None
    if report.get("check_error"):
        problem = f"the browser failed during the check: {report['check_error']}"
    elif report.get("ready") and not report.get("unresponsive") and not gone:
        if report.get("paint_error") or not isinstance(paint, dict):
            problem = f"the browser could not capture the frame: {report.get('paint_error')}"
        elif report.get("probe_error"):
            problem = f"the browser could not read the frame: {report.get('probe_error')}"
    if problem and not failures:
        # The CHECK broke, not (as far as anyone saw) the document.
        return {"checked": False, "reason": problem}
    if report.get("ready") and not problem and not report.get("unresponsive") and not gone:
        content = report.get("content") or {}
        drawn_in = [int(x or 0) for x in (paint.get("content_painted_px") or [])]
        has_content = any(x >= CONTENT_MIN_PX for x in drawn_in)
        if float(paint.get("painted_share") or 0.0) < MIN_PAINTED_SHARE and not has_content:
            failures.append(
                f"it shows nothing — after load and {report.get('settle_ms', SETTLE_MS)} ms "
                f"the whole frame is one colour ({paint.get('dominant')}) with no visible text, "
                f"image or canvas drawn on it ({int(content.get('text') or 0)} text and "
                f"{int(content.get('media') or 0)} image/canvas elements, none painted)")
        if "tiles" in drawn and int(drawn.get("tiles") or 0) < 1 \
                and int((report.get("probes_first") or {}).get("tiles") or 0) < 1:
            failures.append(
                f"mode is 'desktop' but it has no app tile — CSeriesDesktop.apps handed it "
                f"{apps_handed} app(s) and nothing in the document carries data-cseries-app "
                "(the shell finds tiles by that attribute; CSeriesDesktop.render fills one)")
        first = report.get("probes_first") or {}
        if int(first.get("tiles") or 0) > 0 and int(drawn.get("tiles") or 0) < 1:
            failures.append(
                f"it drew {int(first['tiles'])} app tile(s) at first and had removed them by "
                f"the end of the {report.get('settle_ms', SETTLE_MS)} ms settle — a desktop "
                "keeps its tiles (the shell finds them by data-cseries-app)")
        elif int(first.get("tiles") or 0) > 0 and int(drawn.get("tiles") or 0) > 0 \
                and int((first.get("tiles_reach") or {}).get("shown") or 0) > 0 \
                and int((drawn.get("tiles_reach") or {}).get("shown") or 0) == 0:
            failures.append(
                "its app tiles were visible at first and hidden by the end of the "
                f"{report.get('settle_ms', SETTLE_MS)} ms settle — nothing on the desktop can be clicked")
        if int(first.get("bar") or 0) + int(first.get("bar_strip") or 0) > 0 \
                and int(drawn.get("bar") or 0) + int(drawn.get("bar_strip") or 0) < 1:
            failures.append(
                "it drew its declared bar at first and had removed it by the end of the "
                f"{report.get('settle_ms', SETTLE_MS)} ms settle")
        reach = drawn.get("tiles_reach")
        if isinstance(reach, dict) and int(reach.get("shown") or 0) > 0 \
                and int(reach.get("reachable") or 0) == 0:
            failures.append(
                f"something covers every app tile — a click on any of the "
                f"{int(reach['shown'])} visible tile(s) lands on {reach.get('cover') or 'another element'} "
                "instead, so no app can be opened from the desktop")
        if "bar" in drawn and int(drawn.get("bar") or 0) < 1 \
                and int(drawn.get("bar_strip") or 0) < 1 \
                and int(first.get("bar") or 0) + int(first.get("bar_strip") or 0) < 1:
            b = expect.get("bar") or {}
            failures.append(
                f"it declares cseries-bar \"{b.get('edge')} {b.get('size')}\" but draws no bar — no "
                "dock, launcher or window list (data-cseries-dock / -activate / -window) and no "
                "visible control in that strip; the shell hides its own taskbar for this "
                "document, so draw them there while CSeriesDesktop.ownsBar is true")
    return {
        "checked": True, "ok": not failures,
        "why": failures[0] if failures else "rendered: no uncaught error, it shows something, and it draws what it declares",
        "failures": failures, "errors": errors[:5],
        "console_errors": (report.get("console_errors") or [])[:5],
        "paint": paint, "drawn": drawn, "expected": expect,
        "apps_handed": apps_handed,
    }


def tiles_off_phone(report: dict) -> tuple[int, int]:
    """(tiles past a side edge of the phone screen, tiles shown at all), from
    the phone pass's probes (probes_for phone=True)."""
    drawn = (report or {}).get("probes") or {}
    anywhere = max(0, int(drawn.get("tiles_anywhere") or 0))
    across = max(0, int(drawn.get("tiles_across") or 0))
    return max(0, anywhere - across), anywhere


def phone_warnings(report: dict, width: int = PHONE_VIEWPORT[0],
                   height: int = PHONE_VIEWPORT[1]) -> list[str]:
    """What the phone pass warns about: app tiles drawn past a side edge of
    the phone screen (probes_for phone=True). Not a refusal — the phone keeps
    the shell's taskbar and its launcher, so no app is lost — but a layout the
    Host fixes in the same turn."""
    off, anywhere = tiles_off_phone(report)
    if anywhere <= 0 or off <= 0:
        return []
    return [f"on a phone ({width}×{height}) {off} of the {anywhere} app tiles sit past the "
            "screen's side edges, where nobody can tap them — while CSeriesDesktop.phone is true, "
            "lay the tiles out for the phone's width: wrap them into rows, or a grid that "
            "scrolls down (desktop skill §2b). A drawer that slides them in on a tap is fine "
            "when it is visibility:hidden while closed — moved off the screen alone, its "
            "tiles count as past the edge"]


def with_phone(desktop: dict, phone: Optional[dict], report: Optional[dict] = None,
               reason: str = "") -> dict:
    """The desktop-size verdict joined with the phone pass. A phone failure
    fails the document, named "on a phone"; a phone pass adds its warnings;
    a phone size that could not be asked leaves the desktop verdict standing
    and says so."""
    w, h = PHONE_VIEWPORT
    if phone is None or not phone.get("checked"):
        why = reason or (phone or {}).get("reason") or "the browser could not be asked"
        return {**desktop, "phone": {"checked": False, "reason": why},
                "warnings": [*(desktop.get("warnings") or []),
                             f"the phone size ({w}×{h}) was not checked: {why}"]}
    if not phone.get("ok"):
        failures = [f"on a phone ({w}×{h}): {f}" for f in phone.get("failures") or []]
        return {**desktop, "ok": False, "why": failures[0] if failures else "it fails on a phone",
                "failures": failures, "errors": phone.get("errors") or [],
                "console_errors": phone.get("console_errors") or [],
                "phone": {"checked": True, "ok": False, "why": phone.get("why")}}
    warnings = phone_warnings(report or {}, w, h)
    return {**desktop, "why": f"{desktop.get('why')}; on a phone ({w}×{h}) too",
            "phone": {"checked": True, "ok": True, "why": phone.get("why"),
                      "tiles_off": tiles_off_phone(report or {})[0]},
            "warnings": [*(desktop.get("warnings") or []), *warnings]}


# ── The same bytes, the same answer ─────────────────────────────────────────
#
# A failed verdict is a fact about BYTES. Remembered by digest, it stops two
# things: a later "not checked" (no browser, a full plan) turning refused
# bytes into worn ones, and the shell's background reconcile asking the
# browser again about bytes already known to fail. Only a browser that passes
# the same bytes clears it.

def state_dir() -> Path:
    """Kernel-private state for the render check and the worn desktop:
    beside the projects tree (/var/lib/cseries), never inside a project, so no
    project writer — a build landing, the project file lane, the editor —
    can reach it."""
    from server.applications import artifacts as artifact_service
    return artifact_service._projects_root().parent / "desktop-render"


def digest(html: str) -> str:
    return hashlib.sha256(str(html or "").encode("utf-8")).hexdigest()


_FAILED_FILE = "failed.json"
_FAILED_MAX = 200
_failed_lock = threading.Lock()


def _failed_path() -> Path:
    return state_dir() / _FAILED_FILE


def _read_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}-{threading.get_ident()}")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def _failed_load() -> dict:
    return _read_state(_failed_path())


def _failed_save(data: dict) -> None:
    _write_state(_failed_path(), data)


def known_failure(html: str) -> Optional[dict]:
    """{at, why, failures} when these exact bytes FAILED a render check and
    no browser has passed them since; else None."""
    rec = _failed_load().get(digest(html))
    return rec if isinstance(rec, dict) else None


def remember_failure(html: str, result: dict) -> None:
    """Record a FAILED verdict for these bytes (idempotent). render_check
    does this itself; the pin calls it too on every failure it acts on, so
    the memory never depends on which path produced the verdict."""
    if result.get("checked") and not result.get("ok") and not result.get("remembered"):
        _remember(html, result)


def _remember(html: str, result: dict) -> None:
    try:
        with _failed_lock:
            data = _failed_load()
            data[digest(html)] = {"at": datetime.now(timezone.utc).isoformat(),
                                  "why": result.get("why"),
                                  "failures": (result.get("failures") or [])[:6]}
            if len(data) > _FAILED_MAX:
                for k in sorted(data, key=lambda k: str(data[k].get("at")))[:len(data) - _FAILED_MAX]:
                    data.pop(k, None)
            _failed_save(data)
    except OSError as e:
        log.warning("render check: could not remember a failed verdict: %s", e)


def _forget(html: str) -> None:
    try:
        with _failed_lock:
            data = _failed_load()
            if data.pop(digest(html), None) is not None:
                _failed_save(data)
    except OSError as e:
        log.warning("render check: could not clear a failed verdict: %s", e)


def remembered_result(known: dict, *, now: Optional[str] = None) -> dict:
    """A remembered failure as a verdict: checked, failed, with when."""
    out = {"checked": True, "ok": False, "remembered": True, "failed_at": known.get("at"),
           "why": known.get("why") or "it failed the render check",
           "failures": list(known.get("failures") or [known.get("why") or "it failed"])}
    if now:
        out["not_asked_again"] = now
    return out


# ── A reload loop the shell REPORTED: not a verdict ─────────────────────────
#
# The shell counts the worn desktop's frame loads and reports a loop
# (POST /api/appearance/desktop/loop → desktop_pin.take_off_reloading). The
# report takes the version off the screen, but no browser saw the loop, and
# any page framed on the desktop's origin can send the same report. So it is
# kept apart from the failed verdicts above: the bytes are recorded as
# "taken off after a reload report (unconfirmed)". Wearing them again runs
# the normal check, and when no browser can be asked they are worn with a
# note saying what the report was, never refused on its word. The shell's
# background reconcile does not put them back on its own
# (desktop_pin.reconcile). Wearing them clears the record (desktop_pin.
# write_pin): if they loop again, the shell reports them again.
_REPORTS_FILE = "reload-reports.json"


def _reports_path() -> Path:
    return state_dir() / _REPORTS_FILE


def reload_report(html: str) -> Optional[dict]:
    """{at, why} when these exact bytes were taken off after a reload report
    and have not been worn since; else None."""
    rec = _read_state(_reports_path()).get(digest(html))
    return rec if isinstance(rec, dict) else None


def remember_reload_report(html: str, why: str) -> None:
    try:
        with _failed_lock:
            data = _read_state(_reports_path())
            data[digest(html)] = {"at": datetime.now(timezone.utc).isoformat(), "why": why}
            if len(data) > _FAILED_MAX:
                for k in sorted(data, key=lambda k: str(data[k].get("at")))[:len(data) - _FAILED_MAX]:
                    data.pop(k, None)
            _write_state(_reports_path(), data)
    except OSError as e:
        log.warning("could not record a reload report: %s", e)


def forget_reload_report(html: str) -> None:
    try:
        with _failed_lock:
            data = _read_state(_reports_path())
            if data.pop(digest(html), None) is not None:
                _write_state(_reports_path(), data)
    except OSError as e:
        log.warning("could not clear a reload report: %s", e)


def reload_report_note(rec: dict) -> str:
    return (f"this version was taken off after a reload report (unconfirmed, {rec.get('at')}): "
            "the shell counted it reloading itself, and no browser has confirmed that. "
            "If it reloads itself again, the shell takes it off again")


# ── An older browser: no render check at all ───────────────────────────────
#
# Seen 2026-10-02: the operator pinned cseries-browser v0.5.2,
# which predates /v1/verify/document, so every check was a 2-vCPU wake for a
# 404 — and the worn desktop's re-check asked every 300 s forever. That answer is a fact about the browser IMAGE, so it is kept
# with the image that gave it (harness_backend.known_image, from the
# operator's wake answer) and stands until a wake reports another image.
_OUTDATED_FILE = "sidecar-outdated.json"
_BROWSER_APP = "browser"


def _outdated_path() -> Path:
    return state_dir() / _OUTDATED_FILE


def _browser_image() -> Optional[str]:
    try:
        from server.applications import harness_backend as _hb
        return _hb.known_image(_BROWSER_APP)
    except Exception:  # noqa: BLE001 — no lane, no image known
        return None


# When NO image is known — an operator that does not report one yet, and no
# wake or capacity answer has carried it since this kernel started — a change
# of image cannot be seen, so the record is asked about again after
# UNKNOWN_IMAGE_PROBE_S, doubling each time it still answers "no route", up to
# a day: a kernel that ships before the operator that
# reports images still finds the new browser, at a handful of wakes a day.
UNKNOWN_IMAGE_PROBE_S = 3600.0
UNKNOWN_IMAGE_PROBE_MAX_S = 86400.0
# How often the background path asks the operator for the browser's current
# image (bridge harness.capacity — reads the Deployment, wakes nothing) while
# an outdated record stands.
IMAGE_REFRESH_S = 600.0
_image_refreshed = [0.0]


def _probe_wait(probes: int) -> float:
    return min(UNKNOWN_IMAGE_PROBE_S * (2 ** min(max(0, int(probes)), 32)),
               UNKNOWN_IMAGE_PROBE_MAX_S)


def sidecar_outdated() -> Optional[dict]:
    """{at, image, reason} while the browser last answered that it has no
    render check and nothing since says its image changed; else None.
    Known image: the record stands until a wake or the operator reports a
    different one. No image known on either side: it stands until its
    doubling probe wait is up (_probe_wait), then one caller asks again."""
    rec = _read_state(_outdated_path())
    if not rec.get("at"):
        return None
    now = _browser_image()
    if now is not None and now != rec.get("image"):
        return None
    if now is None and rec.get("image") is None:
        try:
            last = float(rec.get("probed_at") or 0.0)
        except (TypeError, ValueError):
            last = 0.0
        if last and time.time() - last >= _probe_wait(int(rec.get("probes") or 0)):
            return None
    return rec


def _note_outdated(reason: str) -> None:
    try:
        with _failed_lock:
            prev = _read_state(_outdated_path())
            image = _browser_image()
            probes = (int(prev.get("probes") or 0) + 1
                      if prev.get("at") and image is None and prev.get("image") is None else 0)
            _write_state(_outdated_path(), {"at": datetime.now(timezone.utc).isoformat(),
                                            "image": image, "reason": reason,
                                            "probes": probes, "probed_at": time.time()})
    except OSError as e:
        log.warning("render check: could not record an outdated browser: %s", e)


async def refresh_browser_image() -> None:
    """While an outdated-browser record stands, ask the operator for the
    browser's current image without waking it (bridge harness.capacity,
    whose answer carries `images` from the operator), at most
    every IMAGE_REFRESH_S. An image it reports is kept like one a wake
    reports (harness_backend.known_image), so a new browser image clears the
    record with no wake. Never raises; an operator that does not report
    images changes nothing."""
    if not _read_state(_outdated_path()).get("at"):
        return
    now = time.monotonic()
    if now - _image_refreshed[0] < IMAGE_REFRESH_S:
        return
    _image_refreshed[0] = now
    try:
        from server.main import get_bridge_client
        bc = get_bridge_client()
        if bc is None or not getattr(bc, "_connected", False):
            return
        env = await bc.request_full("harness.capacity", {"harnesses": [_BROWSER_APP]})
        image = ((env.get("data") or {}).get("images") or {}).get(_BROWSER_APP) \
            if env.get("ok") else None
        if isinstance(image, str) and image:
            from server.applications import harness_backend as _hb
            _hb._last_image[_BROWSER_APP] = image
    except Exception as e:  # noqa: BLE001 — advisory
        log.debug("render check: browser image refresh skipped: %s", e)


def _clear_outdated() -> None:
    p = _outdated_path()
    if not p.exists():
        return
    try:
        with _failed_lock:
            p.unlink(missing_ok=True)
    except OSError as e:
        log.warning("render check: could not clear the outdated-browser record: %s", e)


def _not_asked_outdated(rec: dict) -> dict:
    return {"checked": False, "outdated": True,
            "reason": (f"not asked — this Host's browser answered at {rec.get('at')} that it "
                       f"has no render check ({rec.get('reason') or 'no ' + SIDECAR_PATH + ' route'})"
                       ", and no newer browser image has been seen since")}


async def _stand_in(pool) -> tuple[list[dict], str]:
    """This Host's app names and its name, for the stand-in bridge — what the
    Designs shelf hands its previews. Best-effort: an unreadable registry
    hands no apps, and the tile check is then skipped rather than failed."""
    apps: list[dict] = []
    try:
        from server.applications import registry
        apps = [{"id": a.id, "name": a.name} for a in await registry.list_active(pool)]
    except Exception as e:  # noqa: BLE001
        log.info("render check: installed apps unreadable (%s) — tile check skipped", e)
    host = ""
    try:
        from server.routes.entity import _entity_name
        host = _entity_name()
    except Exception:  # noqa: BLE001 — a greeting without a name still renders
        pass
    return apps, host


async def render_check(pool, html: str, *, linger_s: float = LINGER_S,
                       ask_outdated: bool = True) -> dict:
    """Load `html` in the real browser, as the shell would mount it, and judge
    it. Never raises. {checked: False, reason} when the browser could not be
    asked; otherwise judge()'s verdict.

    `linger_s`: how long a browser this call woke stays up for the next check
    (0 = released as soon as it answers). `ask_outdated=False`: when the
    browser is known to have no render check (sidecar_outdated), answer "not
    checked" without waking it — for paths no person is waiting on.

    Bytes that already FAILED stay failed until a browser passes them: when
    the browser cannot be asked, the remembered failure is the answer, never
    "not checked". (The shell's background reconcile does not ask at all
    about such bytes — desktop_pin.reconcile.)

    Bytes taken off after a shell's reload report are NOT failed bytes
    (reload_report): they are checked like any others, and when the browser
    cannot be asked the "not checked" answer says what the report was."""
    known = known_failure(html)
    stale = None if ask_outdated else sidecar_outdated()
    if stale is not None:
        result = _not_asked_outdated(stale)
    else:
        result = await _render_check(pool, html, linger_s=linger_s)
        if result.get("outdated"):
            _note_outdated(str(result.get("reason") or ""))
        elif result.get("checked"):
            _clear_outdated()
    if result.get("checked"):
        if result.get("ok"):
            if known is not None:
                _forget(html)
        else:
            _remember(html, result)
        return result
    if known is not None:
        return remembered_result(known, now=str(result.get("reason") or ""))
    reported = reload_report(html)
    if reported is not None:
        result = {**result,
                  "reason": f"{result.get('reason') or 'the browser could not be asked'}; "
                            + reload_report_note(reported),
                  "reload_report": {"at": reported.get("at"), "unconfirmed": True}}
    return result


# How long the first look keeps the browser for a possible second one.
SECOND_LOOK_LINGER_S = 30.0


async def _release_after_first_look(linger_s: float) -> None:
    """The first look needed no second one: hand the browser back to the
    caller's own linger (0 = release now), as if only one call was made."""
    try:
        from server.applications import harness_backend as _hb
        if not _hb.lingering(_BROWSER_APP):
            return                      # someone else holds it (a session, a run)
        if linger_s > 0:
            _hb.linger(_BROWSER_APP, _BROWSER_APP, linger_s)
        else:
            await _hb.release_now(_BROWSER_APP, _BROWSER_APP)
    except Exception as e:  # noqa: BLE001
        log.debug("render check: release after the first look failed: %s", e)


def _blank_only(verdict: dict) -> bool:
    """The verdict's ONLY failure is "it shows nothing" — the one a slow
    first paint can produce."""
    f = verdict.get("failures") or []
    return (verdict.get("checked") and not verdict.get("ok") and len(f) == 1
            and str(f[0]).startswith("it shows nothing"))


async def _look(payload: dict, *, html: str, expect: dict, apps_handed: int,
                hold_s: float) -> tuple[Optional[dict], dict]:
    """One size: (verdict, the report it was read from) — or (None, the
    sidecar's answer) when the browser could not be asked. A verdict whose
    only failure is "shows nothing" is looked at once more with a longer
    settle before it stands: drawn late is not blank."""
    from mastercode.runtime.app_verify import browser_sidecar_post
    res = await browser_sidecar_post(SIDECAR_PATH, payload, what="browser render check",
                                     label="desktop", linger_s=hold_s)
    if not res.get("checked"):
        return None, res
    report = res.get("report")
    verdict = judge(report, html=html, expect=expect, apps_handed=apps_handed)
    if _blank_only(verdict):
        again = await browser_sidecar_post(
            SIDECAR_PATH, {**payload, "settle_ms": SETTLE_RETRY_MS},
            what="browser render check", label="desktop", linger_s=hold_s)
        if again.get("checked"):
            report = again.get("report")
            verdict = judge(report, html=html, expect=expect, apps_handed=apps_handed)
    return verdict, (report if isinstance(report, dict) else {})


async def _render_check(pool, html: str, *, linger_s: float = LINGER_S) -> dict:
    try:
        apps, host = await _stand_in(pool)
        # The stand-in shows at most 24 apps (desktop-preview.js instrument).
        handed = len(apps[:24])
        expect = expectations(html)
        payload = {"page": compose_page(html, apps=apps, host=host), "subject": SUBJECT,
                   "width": VIEWPORT[0], "height": VIEWPORT[1],
                   "settle_ms": SETTLE_MS,
                   "probes": probes_for(expect, handed)}
        # Every look keeps the browser up briefly (SECOND_LOOK_LINGER_S) even
        # when the caller asked for none, so a second look at a blank result
        # and the phone pass do not wake it cold again; once the last look has
        # answered it is released as the caller asked.
        hold = max(linger_s, SECOND_LOOK_LINGER_S)
        try:
            verdict, res = await _look(payload, html=html, expect=expect,
                                       apps_handed=handed, hold_s=hold)
            if verdict is not None and verdict.get("checked") and verdict.get("ok"):
                pw, ph = PHONE_VIEWPORT
                phone_expect = {**expect, "bar": None}       # a phone keeps the shell's taskbar
                phone_payload = {**payload,
                                 "page": compose_page(html, apps=apps, host=host,
                                                      width=pw, height=ph, phone=True),
                                 "width": pw, "height": ph,
                                 "probes": probes_for(phone_expect, handed, pw, ph, phone=True)}
                pv, pres = await _look(phone_payload, html=html, expect=phone_expect,
                                       apps_handed=handed, hold_s=hold)
                verdict = with_phone(verdict, pv, pres if pv is not None else None,
                                     reason="" if pv is not None else str(pres.get("reason") or ""))
        finally:
            if linger_s < SECOND_LOOK_LINGER_S:
                await _release_after_first_look(linger_s)
        if verdict is not None:
            return verdict
    except Exception as e:  # noqa: BLE001 — the check must never crash a door
        log.warning("render check could not run: %s", e)
        return {"checked": False, "reason": f"the render check could not run: {e}"}
    reason = str(res.get("reason") or "the browser could not be asked")
    out = {"checked": False, "reason": reason}
    if any(code in reason for code in _UNSUPPORTED):
        out["reason"] = reason + (" — this browser sidecar predates the desktop render "
                                  f"check (it has no {SIDECAR_PATH} route)")
        out["outdated"] = True
    if res.get("over_plan"):
        out["over_plan"] = res["over_plan"]
    return out


def room_sentence(over_plan: Optional[dict]) -> str:
    """What frees room on a plan whose memory is full, from the operator's own
    accounting (controller _refuse_if_over_plan: the Host's pod, every awake
    harness, every app machine — across every Host when the plan's memory is a
    shared pool). States the facts; it never changes an allowance."""
    o = over_plan or {}
    text = ("Room comes back when something else holding the plan's memory stops: a "
            "coding engine or the code editor finishing and going to sleep, the "
            "Browser app being closed, or an app that runs on a machine of its own "
            "being stopped")
    text += (" — on any of your Hosts, since they share this memory." if o.get("pool_add_mi")
             is not None else ".")
    if o.get("upgrade_to"):
        text += f" The {o['upgrade_to']} plan has room for it."
    return text


def format_render(result: Optional[dict]) -> str:
    """One readable block for a refusal body or a tool result."""
    r = result or {}
    if not r.get("checked"):
        return ("NOT RENDER-CHECKED — the document could not be loaded in a browser: "
                + str(r.get("reason") or "unknown reason"))
    if r.get("ok"):
        return "\n".join(["Rendered in a browser: " + str(r.get("why") or "ok"),
                          *[f"  ! {w}" for w in r.get("warnings") or []]])
    if r.get("remembered"):
        lines = [f"REFUSED — this exact version FAILED the render check ({r.get('failed_at')}) "
                 "and stays refused until a browser passes it"
                 + (f"; the browser could not be asked again now ({r['not_asked_again']})"
                    if r.get("not_asked_again") else "") + ":"]
    else:
        lines = ["REFUSED — the document does not render in a browser (loaded the way the "
                 "Designs shelf previews it, with this Host's apps handed to it, at desktop "
                 "size and then at phone size):"]
    lines += [f"  ✗ {f}" for f in r.get("failures") or []]
    lines.append("Fix the document, then try again — the same check runs on every "
                 "publish and every set-desktop.")
    return "\n".join(lines)


def summary(result: Optional[dict]) -> dict:
    """The compact form a door returns beside its own result."""
    r = result or {}
    if not r.get("checked"):
        out = {"checked": False, "reason": r.get("reason")}
        for k in ("over_plan", "outdated", "reload_report"):
            if r.get(k):
                out[k] = r[k]
        return out
    out = {"checked": True, "ok": bool(r.get("ok")), "why": r.get("why"),
           "failures": r.get("failures") or []}
    for k in ("warnings", "phone"):
        if r.get(k):
            out[k] = r[k]
    if r.get("remembered"):
        out["remembered"] = True
        out["failed_at"] = r.get("failed_at")
    return out


# ── Doors a PERSON clicks: answer within the router's window ────────────────
#
# The router in front of every Host gives a request 30 s
# (server/applications/long_request.py). A render check is ~3 s with the
# browser awake, but a cold wake of it measured 10.4 s and 10.5 s on a node
# already holding its 487 MiB image (a test Host, 2026-10-01), and the
# image was cached on 2 of the 10 Host nodes — the rest pull it first.
# So the desktop doors run through long_request: the ordinary answer when it
# is ready in time, else 202 + a job the client polls at
# GET /api/appearance/desktop/jobs/{id} (write:appearance, the doors' own
# scope). The Host's tool calls never pass the router and simply wait.
DOOR_BUDGET_S = 20.0
JOB_AREA = "desktop"
_POLL = "/api/appearance/desktop/jobs/{job}"


def _message(what: str) -> str:
    return (f"Checking the design in a browser before it is {what} — "
            "this takes a little longer while the browser starts.")


def start_job(what: str, work) -> tuple[str, "asyncio.Task"]:
    """Run `work()` detached, as a desktop job (long_request.start)."""
    from server.applications import long_request
    return long_request.start(what, work, area=JOB_AREA)


def pending_body(job_id: str, what: str) -> dict:
    from server.applications import long_request
    return long_request.pending(job_id, poll=_POLL.format(job=job_id), message=_message(what))


async def run_door(what: str, work, *, budget_s: float | None = None):
    """The answer within budget, or a 202 pending body (long_request.run)."""
    from server.applications import long_request
    return await long_request.run(what, work, area=JOB_AREA, poll=_POLL,
                                  message=_message(what),
                                  budget_s=DOOR_BUDGET_S if budget_s is None else budget_s)


def job_status(job_id: str) -> Optional[dict]:
    """A desktop job's outcome, or None (unknown, expired, another area's)."""
    from server.applications import long_request
    return long_request.status(job_id, area=JOB_AREA)


# ── A Workbench build that LANDS a desktop design ───────────────────────────
#
# The Halloween designs were built by a coding harness in a project, with
# shell greps for acceptance. When a build lands and its tree holds desktop
# documents — ANY .html file whose static head declares cseries-desktop,
# wherever it lives (project 251 put its designs at themes/<name>/index.html)
# — each is loaded in the browser and the verdict joins the build's proof as
# the `desktop-render` check (runtime/pipeline.py): a document that throws or
# shows nothing fails the phase like a failing acceptance command, with the
# error in the result.
#
# Behind the browser-proof switch (app_verify.browser_proof_enabled, default
# off): a build lands with nobody present, and that switch is already the
# user's "prove my builds in a browser, on my plan".
PROVIDER = "desktop-render"
# Every desktop document a landed build holds is checked: the person turned on
# browser proof for exactly this, and the browser stays warm between checks
# (LINGER_S), ~3 s each. There is no count past which documents go unchecked
# The scan reads at most this many .html files' heads;
# any past it are NAMED as not scanned, never dropped silently.
_MAX_SCANNED_FILES = 5000
_SKIP_DIRS = {".git", ".history", "node_modules", "__pycache__", ".venv", "venv"}


def landed_desktop_documents(root: Path) -> tuple[list[str], list[str]]:
    """Every .html file in the tree whose static head declares a desktop
    (the floor's own detect_mode, on the first 64 KiB), as paths relative to
    `root`: (to_check, not_scanned). to_check is index.html at the root
    first, then the rest in path order; not_scanned names the .html files
    past _MAX_SCANNED_FILES whose heads were not read. Dot directories,
    dependencies and review machinery are not the build's output and are
    not walked.

    Read through the contained walk (server/applications/tree_walk.py), as
    every reader of a project folder is: a link is neither listed nor read,
    and a folder or file swapped for a link after the walk listed it is not
    followed (each open is O_NOFOLLOW from the pinned root), so nothing
    outside the project is read. A root that is itself a link scans
    nothing."""
    try:
        from server.routes.review import _is_review_dir
    except Exception:  # noqa: BLE001 — a test tree without the review module
        def _is_review_dir(_name):
            return False
    from server.applications.tree_walk import TreeEscape, open_tree_no_escape

    def _skip(parts: tuple, is_dir: bool) -> bool:
        d = parts[-1]
        return is_dir and (d in _SKIP_DIRS or d.startswith(".") or _is_review_dir(d))

    found: list[str] = []
    unscanned: list[str] = []
    scanned = 0
    try:
        tree = open_tree_no_escape(root)
    except TreeEscape:
        return [], []
    with tree:
        for f in tree.walk(skip=_skip):
            if not f.rel.lower().endswith((".html", ".htm")):
                continue
            scanned += 1
            if scanned > _MAX_SCANNED_FILES:
                unscanned.append(f.rel)
                continue
            data = tree.read(f, limit=65536)
            if data is None:
                continue          # gone, or swapped for a link, since it was listed
            if detect_mode(data.decode("utf-8", "replace")):
                found.append(f.rel)
    found.sort(key=lambda n: (n != "index.html", n))
    return found, sorted(unscanned)


async def landed_tree_verdict(pool, entity_id: int, project_id: int,
                              module: Optional[str] = None) -> Optional[dict]:
    """The `desktop-render` verdict for a landed build, or None when the
    browser proof is off or the tree holds no desktop document."""
    from mastercode.runtime.app_verify import browser_proof_enabled
    if not await browser_proof_enabled(pool, entity_id):
        return None
    if module:
        from server.routes.project_modules import module_dir
        root = module_dir(int(project_id), module)
    else:
        from server.routes.applications import PROJECTS_ROOT
        root = Path(PROJECTS_ROOT) / str(int(project_id))
    docs, unscanned = landed_desktop_documents(root)
    if not docs and not unscanned:
        return None
    from server.applications.artifacts import ArtifactError, render_workspace
    results, reasons = [], []
    if unscanned:
        reasons.append(f"{len(unscanned)} .html file(s) past the first {_MAX_SCANNED_FILES} were "
                       "not scanned, so any desktop document among them was not checked: "
                       + ", ".join(unscanned[:10]) + ("…" if len(unscanned) > 10 else ""))
    for name in docs:
        # The document found by the scan, or nothing: one removed since the
        # scan is reported by name, never swapped for another index.html.
        try:
            html = render_workspace(root, name, exact=True)
        except ArtifactError as e:
            reasons.append(f"{name}: not checked, {e.detail}")
            continue
        # A landed build has nobody waiting on it: a browser known to have no
        # render check is not woken once per document.
        r = await render_check(pool, html, ask_outdated=False)
        if not r.get("checked"):
            reasons.append(f"{name}: {r.get('reason')}")
            continue
        results.append({"name": name, "ok": bool(r.get("ok")),
                        "output_tail": "" if r.get("ok") else "\n".join(r.get("failures") or [])})
    base = {"provider": PROVIDER, "where": "browser_sidecar"}
    if not results:
        return {**base, "ran": False, "ok": None, "results": [],
                "reason": "; ".join(reasons) or "not checked"}
    return {**base, "ran": True, "ok": all(x["ok"] for x in results), "results": results,
            **({"not_checked": reasons} if reasons else {})}
