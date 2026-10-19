#!/usr/bin/env python3
"""The desktop floor — the gate on server/applications/desktop_contract.py.

Pins five things:
  1. the TEMPLATE passes the floor as written (the host and every harness start
     from it, so a template that fails would refuse every desktop);
  2. the document that motivated the floor — one Host's 2026-09-03 desktop, a
     screenshot-in-HTML with no mode, no tokens and ten fictional apps — is
     REFUSED, for the reasons it should be;
  3. the individual rules fire on the individual defects and not on their
     absence;
  4. PALETTE COHERENCE (2026-09-18) — one Host's artifact 234 declared all
     eleven required tokens with the palette inverted (surfaces light, text
     near-black) and the floor passed it, because it only counted names. The
     shell paints windows from --cseries-night and their text from
     --cseries-paper, so the machine rendered black on black. That head is
     refused now, naming the pair and the measured ratio. Three things the
     first cut got wrong and this pins: the parser reads every colour form the
     TOKEN GRAMMAR admits (hsl/hsla and the 148 CSS names, not just hex and
     rgb) or the same inverted palette passes verbatim in another notation;
     surface ORDER is a warning, not an error, because a lighter base clears
     every contrast pair and is a choice a designer may make; and near-opaque
     text over an opaque surface is composited and measured rather than
     skipped;
  5. default-skills/desktop.md §5 carries the template byte-identical, so the
     host's recipe and the kernel's check can never drift apart.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server.applications.desktop_contract import (  # noqa: E402
    CSS_NAMED_COLORS, REQUIRED_TOKENS, TEMPLATE_HTML, check_document, check_palette,
    contrast_ratio, design_brief, detect_mode, format_report, looks_like_desktop_task,
    parse_color, template_html)

fails = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""))
    if not ok:
        fails.append(name)


# ── 1. the template passes ───────────────────────────────────────────────
t = check_document(TEMPLATE_HTML, installed_ids={"circles", "settings", "workbench"})
check("template passes the floor", t["ok"], repr(t["errors"]))
check("template has no warnings", not t["warnings"], repr(t["warnings"]))
check("template declares desktop mode", t["mode"] == "desktop")
check("template declares every required token",
      all(k in t["tokens"] for k in REQUIRED_TOKENS))
check("template_html() is TEMPLATE_HTML", template_html() == TEMPLATE_HTML)
check("template names no external asset", "http" not in TEMPLATE_HTML.lower().replace("http-equiv", ""))

# ── 2. the measured stub is refused ────────────────────────────────────
STUB = """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Nocturne — Dark Desktop</title>
<style>.rail{}</style></head><body><div class="topstrip"><span class="ts-clock">9:41</span></div>
<div class="rail"><button class="launch-item"></button></div>
<div class="window window-b"><div class="titlebar">Mail</div><div class="mail-row">placeholder</div></div>
<script>
var APPS = [
  { id: "mail", name: "Mail", bg: "#123" },
  { id: "calendar", name: "Calendar", bg: "#123" },
  { id: "notes", name: "Notes", bg: "#123" },
  { id: "terminal", name: "Terminal", bg: "#123" },
  { id: "music", name: "Music", bg: "#123" }
];
document.getElementById("x") && document.getElementById("x").addEventListener("click", function(){});
</script></body></html>"""
s = check_document(STUB)
check("stub is refused", not s["ok"])
check("stub: no mode is an error", any("no mode" in e for e in s["errors"]), repr(s["errors"]))
check("stub: no tokens is an error", any("no design tokens" in e for e in s["errors"]))
check("stub: the literal app list is an error", any("app-shaped objects" in e for e in s["errors"]))
check("stub: placeholder text is warned", any("stub text" in w for w in s["warnings"]), repr(s["warnings"]))
rep = format_report(s)
check("report says REFUSED and points at the template",
      rep.startswith("REFUSED") and "template" in rep)

# ── 3. each rule, alone ──────────────────────────────────────────────────
# A real, coherent dark palette. It used to be every required token set to the
# same #123456, which is complete, unreadable, and exactly what section 4 now
# refuses — so the fixture had to become a palette a person could use.
COHERENT_DARK = {
    "--cseries-ink": "#0a0c10", "--cseries-night": "#11141a", "--cseries-twilight": "#171b23",
    "--cseries-silk": "#1f242e", "--cseries-veil": "#2a3038",
    "--cseries-mist": "#39404c",
    "--cseries-paper": "#eef1f6", "--cseries-linen": "#a7b0bf", "--cseries-ash": "#6f7887",
    "--cseries-ember": "#3f6f5f", "--cseries-dusk": "#7fd3b4",
}
TOKENS = "; ".join(f"{k}:{v}" for k, v in COHERENT_DARK.items())
def doc(head="", body=""):
    return f"<!DOCTYPE html><html><head>{head}</head><body>{body}</body></html>"
MODE_D = '<meta name="cseries-desktop" content="desktop">'
MODE_W = '<meta name="cseries-desktop" content="wallpaper">'
TOK = f'<meta name="cseries-tokens" content="{TOKENS}">'

ok_w = check_document(doc(MODE_W + TOK, "<canvas></canvas>"))
check("wallpaper + tokens, no tiles → passes", ok_w["ok"] and not ok_w["warnings"], repr(ok_w))
ok_d = check_document(doc(MODE_D + TOK, '<div data-cseries-apps></div>'), installed_ids={"circles"})
check("desktop + tokens + data-cseries-apps → passes", ok_d["ok"], repr(ok_d["errors"]))

r = check_document(doc(MODE_D + TOK, "<h1>hi</h1>"))
check("desktop mode drawing no apps is an error", any("nothing draws the real apps" in e for e in r["errors"]))

r = check_document(doc(MODE_D + '<meta name="cseries-tokens" content="--cseries-ink:#000; --cseries-paper:#fff">',
                       '<div data-cseries-apps></div>'))
check("partial tokens name the missing ones",
      any("also declare" in e and "--cseries-night" in e for e in r["errors"]), repr(r["errors"]))

r = check_document(doc(MODE_D + TOK + '<link rel="stylesheet" href="https://fonts.example/x.css">',
                       '<div data-cseries-apps></div>'))
check("external stylesheet is an error", any("external reference" in e for e in r["errors"]))
r = check_document(doc(MODE_D + TOK + "<style>body{background:url(//cdn.example/a.png)}</style>",
                       '<div data-cseries-apps></div>'))
check("protocol-relative url() is an error", any("external reference" in e for e in r["errors"]))
r = check_document(doc(MODE_D + TOK, '<button data-cseries-app="circles"></button><button data-cseries-app="mail"></button>'),
                   installed_ids={"circles", "settings"})
check("data-cseries-app naming an uninstalled app is an error",
      any("not installed: mail" in e for e in r["errors"]), repr(r["errors"]))
r = check_document(doc(MODE_D + TOK, '<button data-cseries-app="mail"></button>'))
check("with no installed set, ids are not checked", r["ok"], repr(r["errors"]))
r = check_document(doc(MODE_D + TOK, '<button data-cseries-app="Not An Id!"></button>'))
check("a malformed app id is an error", any("not a valid app id" in e for e in r["errors"]))

r = check_document(doc(MODE_W + TOK, '<div data-cseries-apps></div>'))
check("wallpaper mode that also draws apps is warned (two icon sets)",
      r["ok"] and any("two icon sets" in w for w in r["warnings"]))
r = check_document(doc(MODE_D + TOK, '<div data-cseries-apps></div><a href="#">x</a><button onclick="return false;">y</button>'))
check("dead controls are warned", any("dead control" in w for w in r["warnings"]), repr(r["warnings"]))
r = check_document(doc(MODE_D + TOK, '<div data-cseries-apps></div><script>var a = CSeriesDesktop.apps;</script>'))
check("CSeriesDesktop without .on() is warned", any(".on(cb)" in w for w in r["warnings"]))

check("detect_mode reads attribute order both ways",
      detect_mode('<meta content="desktop" name="cseries-desktop">') == "desktop"
      and detect_mode("<html></html>") is None)
check("mode meta past the static head window is ignored",
      detect_mode("<html>" + " " * 70000 + MODE_D) is None)

# ── the brief + the trigger ──────────────────────────────────────────────
b = design_brief()
check("brief names every required token", all(k in b for k in REQUIRED_TOKENS))
check("brief forbids mocks and stubs", "No mocks" in b and "dead controls" in b)
check("brief separates what is REFUSED from what is WARNED in the palette",
      "REFUSED:" in b and "WARNED:" in b and "4.5:1" in b)
check("brief tells the agent about promote", "copies files as an operation" in b)
for text, want in [
    ("Convert project 216's adopted desktop-concept index.html into a live preview artifact", True),
    ("make my wallpaper aurora", True),
    ("redesign the taskbar", True),
    ("build a desktop app for invoices", False),
    ("add a CSV export to the reports page", False),
]:
    check(f"looks_like_desktop_task({text[:40]!r}) == {want}", looks_like_desktop_task(text) == want)

# ── 4. palette coherence (artifact 234, 2026-09-18) ────────────
# The token line off that artifact's head, verbatim. Every required token is
# declared and the whole palette is inverted.
E234 = {
    "--cseries-ink": "#e8e6f0", "--cseries-night": "#0a0a0f", "--cseries-twilight": "#1a1440",
    "--cseries-silk": "#f2f0fa", "--cseries-veil": "rgba(255,255,255,0.04)",
    "--cseries-mist": "#6b6880",
    "--cseries-paper": "#14141c", "--cseries-linen": "#1c1c26", "--cseries-ash": "#3a3848",
    "--cseries-ember": "#e8a04c", "--cseries-dusk": "#8b7cf6",
}


def tokmeta(d):
    return '<meta name="cseries-tokens" content="' + "; ".join(
        f"{k}:{v}" for k, v in d.items()) + '">'


r = check_document(doc(MODE_D + tokmeta(E234), '<div data-cseries-apps></div>'))
check("234's inverted palette is REFUSED, not warned", not r["ok"], repr(r["errors"]))
check("234: the refusal names the pair and the measured ratio",
      any("--cseries-paper on --cseries-night" in e and "1.08:1" in e for e in r["errors"]),
      repr(r["errors"]))
check("234: it declares every required token, so completeness is NOT the complaint",
      not any("also declare" in e or "no design tokens" in e for e in r["errors"]))

ok_dark = check_document(doc(MODE_D + TOK, '<div data-cseries-apps></div>'))
check("a coherent dark palette passes", ok_dark["ok"], repr(ok_dark["errors"]))
check("...with no palette warning either", not ok_dark["warnings"], repr(ok_dark["warnings"]))
LIGHT = {
    "--cseries-ink": "#ffffff", "--cseries-night": "#f4f5f7", "--cseries-twilight": "#e9ebef",
    "--cseries-silk": "#dfe2e8", "--cseries-veil": "#d3d7df", "--cseries-mist": "#b9bfc9",
    "--cseries-paper": "#14171c", "--cseries-linen": "#4a5260", "--cseries-ash": "#767e8c",
    "--cseries-ember": "#1f6f52", "--cseries-dusk": "#0f4f39",
}
check("...and a coherent LIGHT palette passes too (the ramp may run either way)",
      check_document(doc(MODE_D + tokmeta(LIGHT), '<div data-cseries-apps></div>'))["ok"])

# Review item 2 (2026-09-19). The ramp was an ERROR and refused a design nobody
# can fault: a dark UI whose desktop base is lighter than its chrome clears
# every contrast pair. It is a warning now, and it never had to carry a refusal
# because an unreadable palette always trips a contrast rule as well.
LIFTED_BASE = dict(COHERENT_DARK, **{"--cseries-ink": "#2e3440"})
lb = check_document(doc(MODE_D + tokmeta(LIFTED_BASE), '<div data-cseries-apps></div>'))
check("a readable palette with a lighter base is NOT refused", lb["ok"], repr(lb["errors"]))
check("...it is warned, in the shell's own terms",
      any("not ordered dark → light" in w and "after set_desktop" in w
          for w in lb["warnings"]), repr(lb["warnings"]))
check("...and the warning names both turning points with their luminances",
      any("falls at" in w and "rises at" in w and "luminance" in w
          for w in lb["warnings"]))
e234_errs, e234_warns = check_palette(E234)
check("234's ramp is warned while its TEXT is what refuses it",
      len(e234_warns) == 1 and len(e234_errs) == 3, f"{e234_errs} / {e234_warns}")
check("the ramp alone never refuses anything",
      all("ordered dark" not in e for e in e234_errs))

# Review item 1 (2026-09-19). The parser read hex and rgb() only, so the SAME
# inverted palette passed verbatim in hsl() — and the skill tells the host hsl
# is legal. Every form desktop_tokens.py admits has to be measurable.
# Each value is E234's own hex converted to hsl (worst channel error 0.2/255),
# so this is that palette in another notation and nothing else.
HSL_E234 = {
    "--cseries-ink": "hsl(252.0, 25.0%, 92.2%)", "--cseries-night": "hsl(240.0, 20.0%, 4.9%)",
    "--cseries-twilight": "hsl(248.2, 52.4%, 16.5%)", "--cseries-silk": "hsl(252.0, 50.0%, 96.1%)",
    "--cseries-veil": "hsla(0, 0%, 100%, 0.04)", "--cseries-mist": "hsl(247.5, 10.3%, 45.5%)",
    "--cseries-paper": "hsl(240.0, 16.7%, 9.4%)", "--cseries-linen": "hsl(240.0, 15.2%, 12.9%)",
    "--cseries-ash": "hsl(247.5, 12.5%, 25.1%)", "--cseries-ember": "hsl(32.3, 77.2%, 60.4%)",
    "--cseries-dusk": "hsl(247.4, 87.1%, 72.5%)",
}
hsl_errs, hsl_warns = check_palette(HSL_E234)
check("the same inverted palette in hsl() is refused too",
      any("--cseries-paper on --cseries-night" in e for e in hsl_errs), repr(hsl_errs))
check("...on the same three pairs, and warned on the same ramp",
      len(hsl_errs) == 3 and len(hsl_warns) == 1, f"{hsl_errs} / {hsl_warns}")
check("hsl() is converted correctly (a known triple)",
      parse_color("hsl(210, 20%, 8%)") == (16.32, 20.400000000000002, 24.48, 1.0),
      repr(parse_color("hsl(210, 20%, 8%)")))
check("hsla() carries its alpha", parse_color("hsla(0 0% 100% / 0.5)") == (255.0, 255.0, 255.0, 0.5))
check("the space form and 'deg' are read",
      parse_color("hsl(210deg 20% 8%)") == parse_color("hsl(210, 20%, 8%)"))

check("all 148 CSS colour names are carried", len(CSS_NAMED_COLORS) == 148)
check("...lower-cased, with the Level 4 addition present",
      CSS_NAMED_COLORS.get("rebeccapurple") == "#663399"
      and all(k == k.lower() for k in CSS_NAMED_COLORS))
check("a named colour is read as a colour, case-insensitively",
      parse_color("white") == (255.0, 255.0, 255.0, 1.0)
      and parse_color("Black") == (0.0, 0.0, 0.0, 1.0))
NAMED = dict(COHERENT_DARK, **{"--cseries-paper": "black", "--cseries-ink": "darkslategray"})
named_errs, _ = check_palette(NAMED)
check("...so an unreadable palette written in names is refused",
      any("--cseries-paper on --cseries-ink" in e for e in named_errs), repr(named_errs))

# Review item 4 (2026-09-19). Near-opaque text over an OPAQUE surface is exact
# arithmetic; skipping it left a hole an inverted palette could sit in.
check("near-opaque text over an opaque surface is composited and measured",
      contrast_ratio("rgba(230,230,240,0.9)", "#11141a") is not None)
check("...and a near-opaque near-black on near-black is caught",
      any("--cseries-paper on --cseries-night" in e for e in check_palette(
          dict(E234, **{"--cseries-paper": "rgba(20,20,28,0.92)"}))[0]))
check("a faded wash (alpha below 0.85) is still skipped",
      contrast_ratio("rgba(255,255,255,0.4)", "#11141a") is None)
check("a TRANSLUCENT background is skipped — what is behind it is not in the doc",
      contrast_ratio("#ffffff", "rgba(255,255,255,0.04)") is None
      and contrast_ratio("rgba(255,255,255,0.9)", "rgba(0,0,0,0.5)") is None)
check("...so a dark theme whose veil is a translucent hairline still passes",
      check_document(doc(MODE_D + tokmeta(dict(
          COHERENT_DARK, **{"--cseries-veil": "rgba(255,255,255,0.05)"})),
          '<div data-cseries-apps></div>'))["ok"])

# What carries no colour at all is skipped, never failed: an error that fires
# on an unparsed value would refuse legitimate designs.
check("a font stack, a length and var() carry no colour and are skipped",
      parse_color("Inter, system-ui") is None and parse_color("12px") is None
      and parse_color("var(--x)") is None
      and check_palette({"--cseries-paper": "Inter, system-ui",
                         "--cseries-ink": "12px"}) == ([], []))
check("hex shorthand and rgb() are both read",
      parse_color("#fff") == (255.0, 255.0, 255.0, 1.0)
      and parse_color("rgb(10, 20, 30)")[:3] == (10.0, 20.0, 30.0))
check("an empty palette measures nothing", check_palette({}) == ([], []))
check("a low-contrast pair is an ERROR (the floor refuses it), never a warning",
      any("4.5:1" in e for e in check_palette(
          dict(COHERENT_DARK, **{"--cseries-paper": "#14171c"}))[0]))

# ── 5. the skill carries the same template ───────────────────────────────
skill = (ROOT / "default-skills" / "desktop.md").read_text()
m = re.search(r"## 5\..*?```html\n(.*?)\n```", skill, re.S)
check("desktop.md §5 has a fenced html template", m is not None)
if m:
    check("desktop.md §5 template == TEMPLATE_HTML (byte-identical)",
          m.group(1).strip("\n") == TEMPLATE_HTML.strip("\n"))
    check("desktop.md lists the required tokens",
          all(k in skill for k in REQUIRED_TOKENS))

# ── 5b. THE PHONE — the same document, laid out for a phone ──
from server.applications import desktop_render as _DR  # noqa: E402

p2b = re.search(r"## 2b\. The phone.*?(?=\n## )", skill, re.S)
check("desktop.md has §2b, the phone layout", p2b is not None)
if p2b:
    sec = p2b.group(0)
    check("...it says set_desktop puts the design on the phone too",
          "set_desktop" in sec and "phone" in sec and "Settings" in sec)
    check("...the switch is CSeriesDesktop.phone inside .on(), and no bar on a phone",
          "`.phone`" in sec and "`.on()`" in sec and "`ownsBar` is false" in sec)
    check("...it names the width the check uses (desktop_render.PHONE_VIEWPORT)",
          f"{_DR.PHONE_VIEWPORT[0]} px" in sec
          and f"{_DR.PHONE_VIEWPORT[0]}×{_DR.PHONE_VIEWPORT[1]}" in sec)
    check("...and what is REFUSED on a phone apart from what is WARNED",
          "REFUSED" in sec and "WARNED" in sec and "side" in sec)
check("the brief a coding engine gets carries the phone too, at the check's size",
      "THE PHONE" in b and f"{_DR.PHONE_VIEWPORT[0]}×{_DR.PHONE_VIEWPORT[1]}" in b
      and "ownsBar false" in b)
check("the template lays itself out for a phone: a column, a four-column grid that scrolls down",
      "body.phone { display:flex; flex-direction:column; }" in TEMPLATE_HTML
      and "repeat(4, minmax(0, 1fr))" in TEMPLATE_HTML and "overflow-y:auto" in TEMPLATE_HTML
      and "@media (orientation: landscape)" in TEMPLATE_HTML)

# ── 6. THE BAR (2026-09-28) — a design that takes the bar must do its job ─────
from server.applications.desktop_contract import parse_bar  # noqa: E402

check("parse_bar: well formed", parse_bar('<meta name="cseries-bar" content="bottom 52">') == {"edge": "bottom", "size": 52})
check("parse_bar: px suffix and either attribute order",
      parse_bar('<meta content="top 40px" name="cseries-bar">') == {"edge": "top", "size": 40})
check("parse_bar: absent → None", parse_bar("<head></head>") is None)
check("parse_bar: out of bounds or unknown edge → invalid",
      all("invalid" in (parse_bar(f'<meta name="cseries-bar" content="{v}">') or {})
          for v in ("bottom 12", "bottom 400", "side 48", "48 bottom")))
tpl = check_document(TEMPLATE_HTML)
check("the template takes the bar (bottom 52) and passes with no warnings",
      tpl["ok"] and tpl["bar"] == {"edge": "bottom", "size": 52} and not tpl["warnings"], str(tpl))
_no_switch = (TEMPLATE_HTML.replace("data-cseries-activate", "data-x-activate")
              .replace("data-cseries-window", "data-x-window")
              .replace("CSeriesDesktop.dock", "CSeriesDesktop.apps")
              .replace("S.dock", "S.apps").replace("S.windows", "S.apps"))
r = check_document(_no_switch)
check("REFUSED: a bar with no way back to an open window",
      not r["ok"] and any("no way back to an open window" in e for e in r["errors"]), str(r["errors"]))
r = check_document(_no_switch.replace("<body>", "<body><!-- data-cseries-dock data-cseries-activate -->", 1))
check("REFUSED: a comment naming the dock is not a dock",
      not r["ok"] and any("no way back to an open window" in e for e in r["errors"]), str(r["errors"]))
r = check_document(TEMPLATE_HTML.replace(" data-cseries-launcher", ""))
check("REFUSED: a bar with no launcher",
      not r["ok"] and any("no launcher" in e for e in r["errors"]), str(r["errors"]))
r = check_document(TEMPLATE_HTML.replace(" data-cseries-show-desktop", ""))
check("WARNED (not refused): a bar with no show-desktop",
      r["ok"] and any("show-desktop" in w for w in r["warnings"]), str(r))
r = check_document(TEMPLATE_HTML.replace('content="desktop"', 'content="wallpaper"', 1))
check("REFUSED: a wallpaper cannot take the bar",
      any("needs mode 'desktop'" in e for e in r["errors"]), str(r["errors"]))
r = check_document(TEMPLATE_HTML.replace('content="bottom 52"', 'content="bottom 500"'))
check("REFUSED: a malformed bar is named", any("cseries-bar" in e for e in r["errors"]), str(r["errors"]))
r = check_document(TEMPLATE_HTML.replace('<meta name="cseries-bar" content="bottom 52">\n', ""))
check("no bar declared → the shell keeps its taskbar; the document still passes",
      r["ok"] and r["bar"] is None, str(r))

# An image the render puts in (artifacts.inline_own_images: a Host's
# /api/images name becomes the image) can push the mode and the tokens past
# the 64 KB the shell reads. The Host's own file still has them near the top,
# so "add <meta ...>" would send it looking for something that is there: the
# refusal names the window and what fills it.
_big = "data:image/png;base64," + "A" * 70000
_pushed = TEMPLATE_HTML.replace("<head>", f"<head><style>body{{background:url({_big})}}</style>", 1)
r = check_document(_pushed)
check("REFUSED, and says why: the mode past the first 64 KB after an image in <head>",
      not r["ok"] and any("64 KB" in e and "cseries-desktop" in e for e in r["errors"]),
      str(r["errors"])[:400])
check("...the tokens likewise, in place of 'no design tokens'",
      any(e.startswith('<meta name="cseries-tokens"> is in the document but past') for e in r["errors"])
      and not any(e.startswith("no design tokens") for e in r["errors"]), str(r["errors"])[:400])
r = check_document(TEMPLATE_HTML.replace('<meta name="cseries-tokens" content="',
                                         '<meta name="cseries-tokens" content="--cseries-x:url(y); ', 1)
                   .replace("--cseries-ink:", "--cseries-bad-ink:", 1))
check("a token meta inside the window is never reported as past it",
      not any("past its first" in e for e in r["errors"]), str(r["errors"])[:300])
r = check_document(TEMPLATE_HTML.replace('<meta name="cseries-desktop" content="desktop">', "", 1))
check("a document with no mode at all still gets the plain 'no mode declared'",
      any(e.startswith("no mode declared") for e in r["errors"])
      and not any("64 KB" in e for e in r["errors"]), str(r["errors"])[:300])

print("RESULT:", "FAIL " + ", ".join(fails) if fails else "ALL PASS")
sys.exit(1 if fails else 0)
