#!/usr/bin/env python3
"""A website's look, read with no model — the gate on
server/applications/site_style.py and desktop_contract.template_with_tokens
(artifact op='desktop_from_url': website-to-template as a tool).

Pins:
  1. it reads what the page says: background / text on html·body·:root, var()
     resolved against the page's own custom properties, named colours, the
     accent from brand/primary variables, links, buttons and theme-color, and
     the body and heading fonts;
  2. whatever the page says, the token set is COMPLETE and passes the floor's
     readability rules — including a page whose text does not read on its own
     background, a page that declares nothing, and 300 random palettes;
  3. the desktop it builds is the template in that palette, as an operation:
     it passes the floor, carries exactly the extracted tokens, and a hostile
     font name or title cannot inject markup or a token outside the grammar;
  4. stylesheet discovery reads <link rel=stylesheet> only, capped;
  5. a HOSTILE page costs bounded time and memory — var() chains, brace-less
     megabytes, nested blocks, unclosed comments and tags (the first cut hit
     456 MB on 2 KB of CSS and 31 s on 80 KB) — and a font the token grammar
     would drop is never taken.
"""
import random
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server.applications.desktop_contract import (  # noqa: E402
    REQUIRED_TOKENS, TEMPLATE_HTML, check_document, check_palette, template_with_tokens)
from server.applications.desktop_tokens import extract_tokens  # noqa: E402
from server.applications.site_style import (  # noqa: E402
    MAX_STYLESHEETS, extract, palette_is_readable, stylesheet_urls, tokens_for)

fails = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        fails.append(name)


LIGHT = """<html><head><title>Acme Payments</title><meta name="theme-color" content="#635bff">
<style>:root{--brand-primary:#635bff;--page-bg:white}
body{background:var(--page-bg);color:#0a2540;font-family:"Sohne",Helvetica,sans-serif}
h1,h2{font-family:'Camphor Pro',serif} a{color:#635bff} .btn{background:#635bff;color:#fff}
.promo{background:#ffeb3b}</style></head><body></body></html>"""
st = extract(LIGHT, [])
check("background from body via var() and a named colour", st["background"] == "#ffffff", str(st))
check("text from body", st["text"] == "#0a2540", str(st))
check("accent from the brand variable / links / theme-color", st["accent"] == "#635bff", str(st))
check("body font, first real family", st["font_body"] == "Sohne", str(st))
check("display font from h1/h2", st["font_display"] == "Camphor Pro", str(st))
check("a colour on an unrelated element is not the background", "#ffeb3b" != st["background"])
check("the page title is read", st["title"] == "Acme Payments")

DARK_CSS = "html{background-color:rgb(13,17,23);color:#e6edf3} a{color:#2f81f7}"
st = extract("<html><head><link rel=\"stylesheet\" href=\"/app.css\"></head></html>", [DARK_CSS])
check("a linked stylesheet's colours are read", st["background"] == "#0d1117" and st["accent"] == "#2f81f7", str(st))

for name, html in (
    ("light", LIGHT),
    ("dark", "<style>" + DARK_CSS + "</style>"),
    ("nothing declared", "<html><body>hi</body></html>"),
    ("text unreadable on its own background", "<style>body{background:#777;color:#888}</style>"),
):
    t = tokens_for(extract(html, []))
    check(f"{name}: complete and readable", palette_is_readable(t) and all(k in t for k in REQUIRED_TOKENS),
          str(check_palette(t)))

rnd = random.Random(20260928)
bad = 0
for _ in range(300):
    bgc = "#%06x" % rnd.randrange(0, 0xFFFFFF)
    txc = "#%06x" % rnd.randrange(0, 0xFFFFFF)
    acc = "#%06x" % rnd.randrange(0, 0xFFFFFF)
    t = tokens_for(extract(f"<style>body{{background:{bgc};color:{txc}}} a{{color:{acc}}}</style>", []))
    if not palette_is_readable(t):
        bad += 1
check("300 random site palettes all come out readable", bad == 0, f"{bad} unreadable")

t = tokens_for(extract(LIGHT, []))
doc = template_with_tokens(t, title="Acme")
r = check_document(doc)
check("the built desktop passes the floor", r["ok"], str(r["errors"]))
got = extract_tokens(doc)
check("it carries exactly the extracted tokens", all(got.get(k) == v for k, v in t.items()),
      str({k: (got.get(k), v) for k, v in t.items() if got.get(k) != v}))
check("its :root block draws in the same palette", "--ink:#ffffff;" in doc and "--dusk:" + t["--cseries-dusk"] in doc)
check("the title is set", "<title>Acme</title>" in doc)
check("the template itself is untouched", "<title>Threshold</title>" in TEMPLATE_HTML)

hostile = extract("<style>body{font-family:'x</style><script>alert(1)</script>';background:#fff;color:#000}</style>", [])
check("a hostile font name is dropped", hostile["font_body"] is None, str(hostile))
doc = template_with_tokens(tokens_for(hostile), title='<script>alert(1)</script>"')
check("a hostile title cannot inject markup", "<script>alert(1)" not in doc.split("<body>")[0])
check("every built token passes the token grammar",
      set(extract_tokens(doc)) >= set(tokens_for(hostile)))

links = "".join(f'<link rel="stylesheet" href="/s{i}.css">' for i in range(10))
links += '<link rel="icon" href="/favicon.ico"><link href="/p.css" rel="preload stylesheet">'
urls = stylesheet_urls(links)
# Every linked sheet is listed; the caller
# fetches until it has the CSS the reader reads (MAX_TOTAL_CSS) or the page's
# MAX_STYLESHEETS fetches, and names the rest (test_caps_app_records.py).
check("stylesheets: rel=stylesheet only, every one listed",
      len(urls) == 11 and all(u.endswith(".css") for u in urls) and MAX_STYLESHEETS >= 11,
      str(urls))

# ── 5. a hostile page costs bounded time and memory ─────────────────────────
import time  # noqa: E402
import tracemalloc  # noqa: E402

# Time is the CPU this thread spends reading the page, not time on the wall
# clock. Wall-clock time also counts every moment the scheduler gives the core
# to another process, and a gate run shares the machine with other gates: on
# 2026-09-30 a check here that passes in 0.47 s alone took 2.19 s, over its
# 2.0 s limit, in a full gate run beside the browser gates. Under 16 busy loops
# on 8 cores, the 5,000-values case below doubled its wall-clock time or more
# while its CPU time moved under 2%. CPU time still counts work done inside C
# code, such as a regex scanning a value to its end, which these limits exist
# to catch; a count of Python lines or calls counts one regex call once,
# however far it scans.
def bounded(name, html, css=(), seconds=3.0, mib=150):
    tracemalloc.start()
    t0 = time.thread_time()
    st = extract(html, list(css))
    tokens_for(st)
    took = time.thread_time() - t0
    peak = tracemalloc.get_traced_memory()[1] / 1048576
    tracemalloc.stop()
    check(f"hostile: {name} ({took:.2f}s CPU, {peak:.0f} MiB)", took < seconds and peak < mib)

chain = ":root{--v0:#123456;" + "".join(
    f"--v{i}:" + " ".join([f"var(--v{i-1})"] * 50) + ";" for i in range(1, 120)) + "}body{background:var(--v119)}"
bounded("var() chains referencing each other 50x", "", [chain])
bounded("1.5 MB of brace-less text", "", ["a" * 1_500_000] * 4)
bounded("100k tiny rules", "", ["a{color:red}" * 100_000] * 4)
bounded("deeply nested blocks", "", ["@media x{" * 50_000 + "body{background:#000}" + "}" * 50_000])
bounded("unclosed comments", "", ["/* a " * 200_000])
bounded("a page of unclosed <style>/<title>/<link>", "<style><title><link " * 200_000)
bounded("declarations with no colon", "", ["b{" + "x" * 3_999 + "}" * 1] * 2000)
fan = ":root{--y:" + " ".join(["red blue green"] * 70) + ";--x:" + " ".join(["var(--y)"] * 110) + "}" \
    + "a{" + "color:var(--x);" * 30_000 + "}"
bounded("one expensive var() repeated 30k times (a 12.7 s case)", "", [fan[:400_000], fan[:400_000]],
        seconds=2.0)
# One rule each, so the 4 KB body cap does not hide the cost being measured.
distinct = "".join(f"a{{color:var(--x) {i}}}" for i in range(40_000))
root_rule = fan[:fan.index("}") + 1]           # --y and --x, complete
bounded("40k DISTINCT expensive values", "", [root_rule + distinct[:390_000]], seconds=3.0)
unclosed = ":root{--p:" + "var(--q," * 124 + "}"
bounded("unclosed var( chains through font-family, 600 KB (a 37 s case)", "",
        [unclosed + "h1{font-family:var(--p)}" * 25_000], seconds=2.0)
bounded("5,000 distinct values resolving to the unclosed chain", "",
        [unclosed + "".join(f"a{{color:var(--p) {i}}}" for i in range(5_000))], seconds=2.0)
bounded("rgb( with no ), 5,000 distinct values", "",
        ["".join(f"a{{color:rgb({'1,' * 300}{i}}}" for i in range(5_000))], seconds=2.0)
nested = extract("<style>@media (min-width:1px){body{background:#101010;color:#fafafa}}</style>", [])
check("rules inside @media are read", nested["background"] == "#101010" and nested["text"] == "#fafafa", str(nested))
check("a font the token grammar drops is not taken", extract(
    "<style>body{font-family:'Curlz MT', Georgia}</style>", [])["font_body"] == "Georgia")

print("RESULT:", "FAIL " + ", ".join(fails) if fails else "ALL PASS")
sys.exit(1 if fails else 0)
