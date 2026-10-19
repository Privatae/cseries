#!/usr/bin/env python3
"""Host-made artifacts wear the desktop — the gate on
server/applications/artifact_style.py.

Every artifact defaults to the current UI. The block
is added when an artifact is SHOWN (and baked when it is published as a site),
so this pins where it lands, what it never touches, and that it can never
override what an artifact styles itself:
  1. it lands FIRST in <head>, so every rule the artifact declares comes later
     and wins; with no <head> it goes after the doctype, else at the start;
  2. its base rules are zero-specificity (:where), so they lose to any
     selector the artifact writes;
  3. a desktop design (cseries-desktop meta) is left byte-identical, and so is a
     page that already carries the block (no stacking on re-publish);
  4. the sheet's @font-face rules are dropped (the frame cannot fetch them)
     and a literal </style> in the sheet cannot close the element early;
  5. no sheet → the artifact is shown exactly as written.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server.applications.artifact_style import (  # noqa: E402
    STYLE_MARK, is_desktop_document, style_block, wear_desktop)

fails = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        fails.append(name)


SHEET = """@font-face { font-family: "Inter"; src: url("/fonts/inter.woff2"); }
:root { --cseries-ink: #0a0e0f; --cseries-paper: #e5e7eb; --font-body: Inter, sans-serif; }"""

doc = ('<!DOCTYPE html><html><head><meta charset="utf-8"><title>x</title>'
       '<style>body{background:#fff}</style></head><body>hi</body></html>')
out = wear_desktop(doc, SHEET)
head_at = out.find('<meta charset="utf-8">') + len('<meta charset="utf-8">')
check("block lands right after <meta charset> (the encoding hint stays in the first 1 KB)",
      out.find("<head>") < out.find('<meta charset') and out[head_at:].startswith(f"<style {STYLE_MARK}>"))
nocs = wear_desktop("<html><head><title>t</title></head></html>", SHEET)
check("no charset → block lands first in <head>", nocs.startswith(f"<html><head><style {STYLE_MARK}>"))
styled_first = wear_desktop('<html><head><style>b{}</style><meta charset="utf-8"></head></html>', SHEET)
check("a style before the charset still comes after the block",
      styled_first.find(STYLE_MARK) < styled_first.find("b{}"))
check("the artifact's own style comes after the block",
      out.find(STYLE_MARK) < out.find("body{background:#fff}"))
check("body untouched", out.endswith("<body>hi</body></html>"))
check("the tokens are in the block", "--cseries-ink: #0a0e0f" in out)

check("base rules are zero-specificity",
      all(line.strip().startswith(":where(") for line in style_block(SHEET).split("\n")
          if "{" in line and "--cseries-ink" not in line and "@" not in line))

bare = "<!doctype html><p>no head</p>"
o2 = wear_desktop(bare, SHEET)
check("no <head> → after the doctype", o2.startswith("<!doctype html><style ") and o2.endswith("<p>no head</p>"))
check("no doctype, no head → at the start", wear_desktop("<p>x</p>", SHEET).startswith(f"<style {STYLE_MARK}>"))

desk = '<!DOCTYPE html><html><head><meta name="cseries-desktop" content="desktop"></head><body></body></html>'
check("desktop design detected", is_desktop_document(desk))
check("desktop design left byte-identical", wear_desktop(desk, SHEET) == desk)
check("already-worn page not stacked", wear_desktop(out, SHEET) == out)

check("@font-face dropped", "@font-face" not in style_block(SHEET) and "/fonts/" not in style_block(SHEET))
evil = ":root{--cseries-ink:#000}</style><script>alert(1)</script>"
check("a </style> in the sheet cannot close the element",
      "</style><script>" not in style_block(evil))
# Exactly one end tag may remain: the block's own closing </style>.
check("nor a </StYlE> in any case",
      style_block(":root{}</StYlE><b>").lower().count("</style") == 1)

exporter = ("<html><head><title>Export</title><script>function exportDoc(){ return "
            "'<!doctype html><head><meta charset=\"utf-8\"></head>'; }</script></head><body></body></html>")
ex = wear_desktop(exporter, SHEET)
check("a charset or <head> inside a script string is never the target",
      ex.startswith(f"<html><head><style {STYLE_MARK}>") and "</script>" in ex
      and ex.count(STYLE_MARK) == 1 and ex.index(STYLE_MARK) < ex.index("<script>"))
check("no sheet → unchanged", wear_desktop(doc, "") == doc and wear_desktop(doc, "   ") == doc)
check("<head> with attributes is found",
      wear_desktop('<html><head lang="en"><title>t</title></head></html>', SHEET)
      .startswith(f'<html><head lang="en"><style {STYLE_MARK}>'))

print("RESULT:", "FAIL " + ", ".join(fails) if fails else "ALL PASS")
sys.exit(1 if fails else 0)
