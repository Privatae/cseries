#!/usr/bin/env python3
"""The public wiki's page machinery: leak scan, digests, status, bundle, cards.

What has to hold for privatae.ai/wiki to be honest and safe:
  - nothing internal reaches ANY published string — slug, date, app block
    included — whatever the encoding (entities, zero-width characters,
    look-alike letters, a soft wrap), and ordinary product copy passes;
  - a page goes "changed" when its own text or the code it cites changes,
    decorators included, and only then;
  - page files hold only what is published: sources and checks live in
    docs/wiki/sources.yaml, which the bundle never carries;
  - a missing source, a dead wiki: link, a bad date or a stale status file
    fails the repo check, and --verify records the check and reads it back;
  - the image bundle stamps the tag, attaches app facts, refuses a leak, and
    its --check catches drift;
  - a page card carries its link, and a search hit carries only its opening.

Temp dirs only; no pod, no DB. Run: APP_ROOT=<repo> python3 scripts/test_wiki_pages.py
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import generate_capabilities as gc  # noqa: E402  (module level is stdlib + wiki_pages only)
import generate_wiki as gw  # noqa: E402
import wiki_pages as wp  # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def page_text(title="What a Host is", body="A Host is a machine of its own.\n",
              links="", extra="") -> str:
    lines = ["---", f"title: {title}", "summary: A plain summary.", "section: host",
             "order: 10", "questions:", "  - What is a Host?"]
    if extra:
        lines.append(extra)
    lines.append("---")
    return "\n".join(lines) + "\n" + body + links


MOD = "def f():\n    return 1\n\n\n@route('/x', scope='read')\ndef g():\n    return 2\n"


def make_repo(tmp: Path, pages: dict[str, str], sources: dict[str, list[str]] | None = None) -> Path:
    (tmp / "pkg").mkdir(parents=True, exist_ok=True)
    (tmp / "pkg" / "mod.py").write_text(MOD)
    d = tmp / wp.PAGE_DIR
    d.mkdir(parents=True, exist_ok=True)
    for slug, text in pages.items():
        (d / f"{slug}.md").write_text(text)
    src = sources if sources is not None else {s: ["pkg/mod.py::f"] for s in pages}
    (tmp / wp.SOURCES_FILE).write_text(wp.dump_sources({s: {"sources": v} for s, v in src.items()}))
    return tmp


def quiet(fn, *a):
    buf = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(io.StringIO()):
        rc = fn(*a)
    return rc, buf.getvalue()


def exits(fn, *a) -> bool:
    try:
        quiet(fn, *a)
    except SystemExit:
        return True
    return False


def digest_of(root: Path, slug: str) -> str:
    pages, _d, _p = gw.load_repo(root)
    return wp.page_digest(root, next(p for p in pages if p.slug == slug))[0]


# ── 1. leak scan ──────────────────────────────────────────────────────────
print("1. leak scan")
LEAKS = {
    "internal address": "Dial fd12:3456::10 for it.",
    "credential or config path": "Read secrets.json first.",
    "internal source path": "See default-skills/explore.md for it.",
    "ticket or finding id": "Fixed in PR #771.",
    "dated decision": "Decided 2026-09-12 by the team.",
    "design doc reference": "Per DESIGN-wiki, the layout.",
    "tool-call syntax": "Look it up with op='card'.",
    "API route": "The app calls /api/backup/mirror to do it.",
}
for label, text in LEAKS.items():
    hits = [h[0] for h in wp.leak_scan(text)]
    check(f"catches {label}", label in hits, f"hits={hits}")
HIDDEN = {
    "an HTML entity": "secr&#101;ts.json",
    "a zero-width space": "secr\u200bets.json",
    "a Cyrillic look-alike": "\u0455ecrets.json",
    "a soft wrap": "Decided\n2026-09-12 by us",
    "a CGNAT address": "the relay at 100.64.0.9",
    "SECRETS.JSON in capitals": "SECRETS.JSON said so",
    "a Cyrillic em": "see security-\u043codel",
    "a combining grapheme joiner": "secr\u034fets.json",
    "Markdown emphasis inside the name": "secr**e**ts.json",
    "an empty HTML tag inside the name": "secr<b></b>ets.json",
}
for how, text in HIDDEN.items():
    check(f"sees through {how}", bool(wp.leak_scan(text)), text)
CLEAN = (
    "A Host is a machine of its own in a zone with no port open to the public\n"
    "internet. Your laptop joins it by a tunnel it dials out. The Host runs the\n"
    "models you already pay for, on your own keys, and the platform never bills\n"
    "for tokens. Read more on the pricing page: https://privatae.ai/pricing.\n"
    "Install: `curl -fsSL https://privatae.ai/bastion/install.sh | sh`.\n"
    "Platform-wide rules apply to every Host; see [the network](wiki:network).\n"
)
check("passes ordinary product copy, a public install URL included", wp.leak_scan(CLEAN) == [],
      str(wp.leak_scan(CLEAN)))
for ok_text in ("It keeps finding 3 files that match.",):
    check(f"passes {ok_text[:48]!r}", wp.leak_scan(ok_text) == [], str(wp.leak_scan(ok_text)))
check("reports the line of a hit", wp.leak_scan("fine\n10.1.2.3\n")[0][1] == 2)
check("reports the right line for a hit only visible without markup",
      [h[1] for h in wp.leak_scan("fine\nsecr**e**ts.json here\n")] == [2], str(wp.leak_scan("fine\nsecr**e**ts.json here\n")))
check("scan_published reaches keys, nested values and lists",
      len(wp.scan_published({"a": {"10.1.2.3": ["ok", "secrets.json"]}})) == 2)

# ── 2. digests ────────────────────────────────────────────────────────────
print("2. digests")
with tempfile.TemporaryDirectory() as t:
    root = make_repo(Path(t), {"p": page_text()}, {"p": ["pkg/mod.py::g"]})
    d0 = digest_of(root, "p")
    (root / "pkg" / "mod.py").write_text(MOD.replace("return 1", "return 5"))
    check("an edit to ANOTHER symbol keeps the digest", digest_of(root, "p") == d0)
    (root / "pkg" / "mod.py").write_text(MOD.replace("scope='read'", "scope='admin'"))
    check("an edit to the cited symbol's DECORATOR changes it", digest_of(root, "p") != d0)
    (root / "pkg" / "mod.py").write_text(MOD)
    pg = root / wp.PAGE_DIR / "p.md"
    pg.write_text(pg.read_text().replace("machine of its own", "machine"))
    check("an edit to the page's own text changes it", digest_of(root, "p") != d0)
    check("the last of two definitions is the one read",
          wp._python_symbol_source("def f():\n    return 1\n\ndef f():\n    return 2\n", "f").strip().endswith("2"))
    check("Class.method resolves",
          wp._python_symbol_source("class A:\n    def m(self):\n        return 1\n", "A.m") is not None)
    pages, _d, _p = gw.load_repo(root)
    pages[0].sources = ["pkg/mod.py::nope", "pkg/gone.py"]
    _, probs = wp.page_digest(root, pages[0])
    check("a missing symbol and a missing file are problems",
          any("no top-level def/class" in x for x in probs) and any("does not exist" in x for x in probs), str(probs))

# ── 3. page files, sources.yaml, status, the repo check ──────────────────
print("3. page files, sources, status and the repo check")
with tempfile.TemporaryDirectory() as t:
    root = make_repo(Path(t), {"what-a-host-is": page_text()})
    rc, out = quiet(gw.repo_check, root)
    check("an unverified page fails the check", rc == 1 and "never verified" in out, out[-300:])
    before = (root / wp.PAGE_DIR / "what-a-host-is.md").read_text()
    quiet(gw.verify, root, ["what-a-host-is"])
    check("--verify writes the check to sources.yaml, not the page",
          (root / wp.PAGE_DIR / "what-a-host-is.md").read_text() == before
          and "digest: sha256:" in (root / wp.SOURCES_FILE).read_text())
    rc, out = quiet(gw.repo_check, root)
    check("a verified page with a fresh status file passes", rc == 0, out[-300:])
    st = json.loads((root / wp.STATUS_FILE).read_text())
    check("status says current with the checked date",
          st["pages"]["what-a-host-is"]["status"] == "current" and st["pages"]["what-a-host-is"]["checked"])
    (root / "pkg" / "mod.py").write_text(MOD.replace("return 1", "return 42"))
    rc, out = quiet(gw.repo_check, root)
    check("a changed source with the old status file fails", rc == 1 and "out of date" in out, out[-300:])
    quiet(gw.write_status, root)
    rc, out = quiet(gw.repo_check, root)
    st = json.loads((root / wp.STATUS_FILE).read_text())
    check("after --write-status it passes, marked changed",
          rc == 0 and st["pages"]["what-a-host-is"]["status"] == "changed", out[-300:])
    quiet(gw.verify, root, ["what-a-host-is"])
    st = json.loads((root / wp.STATUS_FILE).read_text())
    check("--verify brings it back to current", st["pages"]["what-a-host-is"]["status"] == "current")

with tempfile.TemporaryDirectory() as t:
    root = make_repo(Path(t), {"a-page": page_text(extra="sources:\n  - pkg/mod.py::f")})
    rc, out = quiet(gw.repo_check, root)
    check("a page file carrying sources fails — they belong in sources.yaml",
          rc == 1 and "not page keys" in out, out[-300:])

with tempfile.TemporaryDirectory() as t:
    root = make_repo(Path(t), {"a-page": page_text(links="See [x](wiki:no-such-page).\n")})
    quiet(gw.verify, root, ["a-page"])
    rc, out = quiet(gw.repo_check, root)
    check("a dead wiki: link fails", rc == 1 and "not a page" in out, out[-300:])

with tempfile.TemporaryDirectory() as t:
    root = make_repo(Path(t), {"a-page": page_text(body="It runs at 10.1.2.3.\n")})
    quiet(gw.verify, root, ["a-page"])
    rc, out = quiet(gw.repo_check, root)
    check("a leak in the body fails", rc == 1 and "internal address" in out, out[-300:])

with tempfile.TemporaryDirectory() as t:
    root = make_repo(Path(t), {"fixed-in-t-2001": page_text()})
    quiet(gw.verify, root, ["fixed-in-t-2001"])
    rc, out = quiet(gw.repo_check, root)
    check("a leak in the SLUG fails", rc == 1 and "in slug" in out, out[-300:])

with tempfile.TemporaryDirectory() as t:
    root = make_repo(Path(t), {"a-page": page_text()})
    quiet(gw.verify, root, ["a-page"])
    s = (root / wp.SOURCES_FILE).read_text()
    import re as _re
    (root / wp.SOURCES_FILE).write_text(_re.sub(r"date: '?[0-9-]+'?", "date: last week", s))
    rc, out = quiet(gw.repo_check, root)
    check("a verified date that is not YYYY-MM-DD fails", rc == 1 and "not YYYY-MM-DD" in out, out[-300:])

with tempfile.TemporaryDirectory() as t:
    root = make_repo(Path(t), {"a-page": page_text()}, {"a-page": [], "ghost": ["pkg/mod.py::f"]})
    rc, out = quiet(gw.repo_check, root)
    check("a page with no sources, and a sources entry with no page, both fail",
          rc == 1 and "no sources" in out and "names no page" in out, out[-400:])

with tempfile.TemporaryDirectory() as t:
    root = make_repo(Path(t), {"a-page": page_text()},
                     {"a-page": ["pkg/mod.py::f", "external:hosted-platform"]})
    quiet(gw.verify, root, ["a-page"])
    rc, out = quiet(gw.repo_check, root)
    st = json.loads((root / wp.STATUS_FILE).read_text())
    check("a page citing another repo is published as unchecked, not current",
          rc == 0 and st["pages"]["a-page"]["status"] == "unchecked", out[-300:])
    (root / "pkg" / "mod.py").write_text("def f():\n    return 7\n")
    quiet(gw.write_status, root)
    st = json.loads((root / wp.STATUS_FILE).read_text())
    check("…and still goes changed when its in-repo source changes",
          st["pages"]["a-page"]["status"] == "changed")

# ── 4. the image bundle ──────────────────────────────────────────────────
print("4. the image bundle")
with tempfile.TemporaryDirectory() as t:
    root = make_repo(Path(t), {
        "what-a-host-is": page_text(),
        "keys-app": page_text(title="The Keys app", extra="app: keys"),
    }, {"what-a-host-is": ["pkg/mod.py::f"], "keys-app": ["pkg/mod.py::g", "external:bastion-agent/x.py"]})
    quiet(gw.verify, root, ["what-a-host-is", "keys-app"])
    docs = root / "docs"
    caps = {
        "apps": [{"id": "keys", "name": "Keys", "version": "1.2.0"}],
        "capability_cards": [{"id": "app:keys", "kind": "app", "app_id": "keys",
                              "app_version": "1.2.0", "delivery": "image-baked"}],
    }
    (docs / "capabilities.json").write_text(json.dumps(caps))
    (root / "system").mkdir()
    (root / "system" / "build-info.json").write_text(json.dumps({"tag": "v0.99.1"}))
    b = gw.build_bundle(root, docs)
    blob = json.dumps(b)
    check("stamps the release tag", b["version"] == "v0.99.1", b["version"])
    check("carries no source path, external or digest",
          all(x not in blob for x in ("pkg/mod.py", "external:", "bastion-agent/x.py", "sha256")))
    keys = next(p for p in b["pages"] if p["slug"] == "keys-app")
    check("attaches the documented app's facts",
          keys.get("app") == {"id": "keys", "name": "Keys", "version": "1.2.0",
                              "delivery": "Comes with every Host"}, str(keys.get("app")))
    caps["capability_cards"][0]["delivery"] = "app-store"
    (docs / "capabilities.json").write_text(json.dumps(caps))
    keys2 = next(p for p in gw.build_bundle(root, docs)["pages"] if p["slug"] == "keys-app")
    check("claims no delivery lane the image cannot vouch for", keys2["app"]["delivery"] == "", str(keys2.get("app")))
    caps["apps"][0]["name"] = "Keys, hosted at 10.1.2.3"
    (docs / "capabilities.json").write_text(json.dumps(caps))
    check("a leak in the app block from the manifests stops the bundle", exits(gw.build_bundle, root, docs))
    caps["apps"][0]["name"] = "Keys"
    (docs / "capabilities.json").write_text(json.dumps(caps))
    rc, _ = quiet(gw.bundle, root, docs, False)
    rc2, _ = quiet(gw.bundle, root, docs, True)
    check("--check passes against what it just wrote", rc == 0 and rc2 == 0)
    (docs / "wiki.json").write_text((docs / "wiki.json").read_text().replace("A plain summary.", "Edited."))
    rc3, _ = quiet(gw.bundle, root, docs, True)
    check("--check catches a drifted bundle", rc3 == 1)
    st = json.loads((root / wp.STATUS_FILE).read_text())
    st["pages"]["what-a-host-is"]["checked"] = "last week"
    (root / wp.STATUS_FILE).write_text(json.dumps(st))
    check("a status date that is not YYYY-MM-DD stops the bundle", exits(gw.build_bundle, root, docs))
    quiet(gw.write_status, root)
    p = root / wp.PAGE_DIR / "what-a-host-is.md"
    p.write_text(p.read_text().replace("machine of its own", "machine at 10.1.2.3"))
    check("a leaking page stops the bundle", exits(gw.build_bundle, root, docs))

# ── 5. page cards and search hits ────────────────────────────────────────
print("5. page cards and search hits")
with tempfile.TemporaryDirectory() as t:
    long_body = "An opening line.\n\n" + ("A sentence about the Host.\n" * 80)
    root = make_repo(Path(t), {"what-a-host-is": page_text(body=long_body, links="See [x](wiki:what-a-host-is).\n")})
    page = wp.load_pages(root / wp.PAGE_DIR)[0]
    card = gc._page_card(page)
    check("a page card is kind page, with the page's questions as its when",
          card["kind"] == "page" and card["id"] == "page:what-a-host-is" and card["when"] == ["What is a Host?"])
    check("a page card carries its public link",
          card["url"] == "https://privatae.ai/wiki/what-a-host-is" and card["url"] in card["returns"])
    check("a page card's related are page ids", card["related"] == ["page:what-a-host-is"])
    check("a clean page card passes the card scan", gc._page_card_leaks([card]) == [])
    check("a leak in a page card's title stops the card scan",
          len(gc._page_card_leaks([dict(card, title="secr<b></b>ets.json kernel")])) == 1)
    tools = (HERE.parent / "brain" / "mastercode" / "runtime" / "tools.py").read_text()
    ns: dict = {}
    exec("_CAPS_PAGE_EXCERPT = 900\n" + wp._python_symbol_source(tools, "_caps_search_view"), ns)
    view = ns["_caps_search_view"](dict(card))
    check("a search hit carries the page's opening and points at the whole page",
          len(view["how"]) < 1000 and "op='card', id='page:what-a-host-is'" in view["how"], view["how"][-120:])
    check("a search hit on any other kind of card is untouched",
          ns["_caps_search_view"]({"kind": "op", "how": "x" * 5000})["how"] == "x" * 5000)

print()
if failures:
    print(f"FAIL: {len(failures)} check(s): {failures}")
    sys.exit(1)
print("all gates passed")
