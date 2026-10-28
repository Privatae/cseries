#!/usr/bin/env python3
# gate-scope: whole-tree — every wiki page against its sources
"""The public wiki: the repo check, the status file, and the image bundle.

The pages live in docs/wiki/pages/*.md and hold only what is published; what
each one describes, and when it was last checked against that, lives in
docs/wiki/sources.yaml (format: scripts/wiki_pages.py). This script has two
halves because the two places it runs can see different things:

REPO MODE (a source tree — the smoke gate, the pre-merge lane with no
arguments, and whoever edits a page):

    python3 scripts/generate_wiki.py --root . --check
        Every page parses, passes the leak scan, links only to pages that
        exist, and cites sources that exist; and docs/wiki/status.json says
        exactly what the pages and sources say now. Exit 1 otherwise.
    python3 scripts/generate_wiki.py --root . --write-status
        Recompute docs/wiki/status.json. Run it after any change the check
        reports as drift; the diff shows which pages went "changed".
    python3 scripts/generate_wiki.py --root . --verify SLUG [SLUG ...]
        After reading the page against the code and fixing what is wrong:
        record today's date and the current digest in sources.yaml, then
        rewrite the status file.

    The status is computed here, not in the image, because many sources (the
    app manifests under system-apps/, for one) are not in the image, and
    sources.yaml deliberately is not either. The committed status file
    carries the answer across.

IMAGE MODE (Dockerfile.linux, after the capabilities doc is generated):

    python3 /app/scripts/generate_wiki.py --bundle --root /app --out /app/docs [--check]
        Build docs/wiki.json, the published form: each page's published text,
        its status, the facts of the app it documents, and the release tag
        from build-info. Every string that ships is leak-scanned and any hit
        fails the build, so a page that slipped past review still cannot
        ship. --check regenerates and diffs against the baked file (the smoke
        gate's in-container half).

A page's status is one of:
  current    neither the page nor its in-repo sources changed since it was
             checked: published as checked against this release
  changed    the page or a source changed (or a source vanished): published
             with the date it was last checked and a note that it may be out
             of date
  unchecked  unchanged here, but it also cites another repo (external:), which
             this check cannot see: published with the date it was last checked
Fixing a changed page is `--verify` in a PR, never an edit to the status file.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wiki_pages as wp  # noqa: E402

BUNDLE_SCHEMA = 1


def _fail(msg: str) -> None:
    print(f"FATAL generate_wiki: {msg}", file=sys.stderr)
    sys.exit(2)


# ── repo mode ────────────────────────────────────────────────────────────

def load_repo(root: Path) -> tuple[list[wp.Page], dict, list[str]]:
    pages = wp.load_pages(root / wp.PAGE_DIR)
    data, problems = wp.load_sources(root / wp.SOURCES_FILE)
    problems += wp.attach_sources(pages, data)
    return pages, data, problems


def compute_status(root: Path, pages: list[wp.Page]) -> tuple[dict, list[str]]:
    """→ (status document, problems). Problems are anything that must be fixed
    before merge; a page merely going "changed" is not one of them."""
    problems: list[str] = []
    slugs = {p.slug for p in pages}
    status: dict[str, dict] = {}
    for p in pages:
        where = p.path.as_posix()
        problems += [f"{where}: {x}" for x in p.problems]
        for fld, text in p.public_text().items():
            for label, line, excerpt in wp.leak_scan(text):
                problems.append(f"{where}: {label} in {fld} (line {line}): {excerpt!r}")
        for target in p.links():
            if target not in slugs:
                problems.append(f"{where}: link to wiki:{target}, which is not a page")
        digest, src_problems = wp.page_digest(root, p)
        problems += [f"{where}: {x}" for x in src_problems]
        if not p.verified_digest or not p.verified_date:
            problems.append(f"{where}: never verified — run --verify {p.slug} "
                            f"after checking it against the code")
            state, checked = "changed", ""
        elif digest != p.verified_digest or src_problems:
            state, checked = "changed", p.verified_date
        elif any(wp.is_external(s) for s in p.sources):
            # Unchanged here, but part of what it describes lives in a repo this
            # check cannot see, so it cannot be vouched for as this release's.
            state, checked = "unchecked", p.verified_date
        else:
            state, checked = "current", p.verified_date
        status[p.slug] = {"status": state, "checked": checked}
    return {"schema": BUNDLE_SCHEMA, "pages": dict(sorted(status.items()))}, problems


def _status_text(doc: dict) -> str:
    return json.dumps(doc, indent=1, sort_keys=True) + "\n"


def repo_check(root: Path) -> int:
    pages, _data, problems = load_repo(root)
    doc, more = compute_status(root, pages)
    problems += more
    status_path = root / wp.STATUS_FILE
    committed = status_path.read_text() if status_path.exists() else ""
    if committed != _status_text(doc):
        problems.append(f"{wp.STATUS_FILE} is out of date — run "
                        f"`python3 scripts/generate_wiki.py --root . --write-status` "
                        f"and commit it")
    for x in problems:
        print(f"  FAIL  {x}")
    changed = [s for s, v in doc["pages"].items() if v["status"] == "changed"]
    external = [s for s, v in doc["pages"].items() if v["status"] == "unchecked"]
    print(f"  info  {len(pages)} pages; {len(changed)} changed since last checked"
          + (f" ({', '.join(changed)})" if changed else "")
          + f"; {len(external)} cite another repo and show their checked date")
    if problems:
        print(f"FAIL: {len(problems)} wiki problem(s)")
        return 1
    print(f"all gates passed ({len(pages)} pages)")
    return 0


def write_status(root: Path) -> None:
    pages, _data, _problems = load_repo(root)
    doc, _more = compute_status(root, pages)
    (root / wp.STATUS_FILE).parent.mkdir(parents=True, exist_ok=True)
    (root / wp.STATUS_FILE).write_text(_status_text(doc))
    changed = [s for s, v in doc["pages"].items() if v["status"] == "changed"]
    print(f"wrote {wp.STATUS_FILE} ({len(pages)} pages, {len(changed)} changed)")


def verify(root: Path, slugs: list[str]) -> None:
    pages, data, _problems = load_repo(root)
    by_slug = {p.slug: p for p in pages}
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    for slug in slugs:
        p = by_slug.get(slug)
        if p is None:
            _fail(f"no page {slug!r} under {wp.PAGE_DIR}")
        if not p.sources:
            _fail(f"{slug}: name its sources in {wp.SOURCES_FILE} first")
        digest, problems = wp.page_digest(root, p)
        if problems:
            _fail(f"{slug}: cannot verify against sources that do not resolve: {problems}")
        entry = dict(data.get(slug) or {})
        entry["verified"] = {"date": today, "digest": digest}
        data[slug] = entry
        print(f"verified {slug} ({today}, {digest[:19]}…)")
    (root / wp.SOURCES_FILE).write_text(wp.dump_sources(data), encoding="utf-8")
    # Read it back: a write that did not take must not print success.
    pages2, _d, _pr = load_repo(root)
    after = {p.slug: p for p in pages2}
    for slug in slugs:
        if after[slug].verified_date != today or wp.page_digest(root, after[slug])[0] != after[slug].verified_digest:
            _fail(f"{slug}: the recorded check did not read back from {wp.SOURCES_FILE}")
    write_status(root)


# ── image mode ───────────────────────────────────────────────────────────

def _release_tag(root: Path) -> str:
    bi = root / "system" / "build-info.json"
    try:
        return (json.loads(bi.read_text()).get("tag") or "dev").strip() or "dev"
    except Exception:
        return "dev"


def _app_facts(docs: Path) -> dict[str, dict]:
    """app id → the facts a documented app's page shows, from the generated
    capabilities doc (so they are this release's, not the page author's)."""
    try:
        caps = json.loads((docs / "capabilities.json").read_text())
    except Exception as e:  # noqa: BLE001
        _fail(f"cannot read {docs / 'capabilities.json'} ({e}) — the wiki bundle "
              f"runs after generate_capabilities.py")
    facts: dict[str, dict] = {}
    by_id = {a.get("id"): a for a in caps.get("apps") or []}
    for c in caps.get("capability_cards") or []:
        if c.get("kind") != "app":
            continue
        aid = c.get("app_id") or str(c.get("id", "")).removeprefix("app:")
        a = by_id.get(aid) or {}
        facts[aid] = {
            "id": aid,
            "name": a.get("name") or aid,
            "version": str(c.get("app_version") or a.get("version") or ""),
            # Only the image can vouch for "built in". Everything else reaches a
            # Host by a lane the store decides (installed by default, installed
            # on request, harness-only, or not offered to members at all), and
            # none of that is in the image, so the page's own text says it.
            "delivery": "Comes with every Host" if c.get("delivery") == "image-baked" else "",
        }
    return facts


def build_bundle(root: Path, docs: Path) -> dict:
    pages = wp.load_pages(root / wp.PAGE_DIR)
    try:
        status = json.loads((root / wp.STATUS_FILE).read_text()).get("pages") or {}
    except Exception as e:  # noqa: BLE001
        _fail(f"cannot read {root / wp.STATUS_FILE} ({e})")
    facts = _app_facts(docs)
    tag = _release_tag(root)
    out_pages: list[dict] = []
    leaks: list[str] = []
    for p in pages:
        if p.problems:
            _fail(f"{p.path}: {p.problems}")
        st = status.get(p.slug)
        if not isinstance(st, dict) or st.get("status") not in ("current", "changed", "unchecked"):
            _fail(f"{wp.STATUS_FILE} has no valid entry for {p.slug} — run --write-status in the repo")
        checked = str(st.get("checked") or "")
        if checked and not wp._DATE.match(checked):
            _fail(f"{wp.STATUS_FILE}: {p.slug} checked {checked!r} is not YYYY-MM-DD")
        page = {
            "slug": p.slug,
            "title": p.title,
            "summary": p.summary,
            "section": p.section,
            "order": p.order,
            "questions": p.questions,
            "body": p.body,
            "status": st["status"],
            "checked": checked,
        }
        if p.app:
            if p.app not in facts:
                _fail(f"{p.slug}: documents app {p.app!r}, which this image does not carry")
            page["app"] = facts[p.app]
        # Every string that ships, including the slug (it becomes the URL),
        # the date and the app block drawn from the manifests.
        leaks += wp.scan_published(page, p.slug)
        out_pages.append(page)
    if leaks:
        _fail("leak scan hit on published text:\n  " + "\n  ".join(leaks))
    used = {p["section"] for p in out_pages}
    return {
        "schema": BUNDLE_SCHEMA,
        "version": tag,
        "sections": [{"key": k, "title": t} for k, t in wp.SECTIONS if k in used],
        "pages": out_pages,
    }


def bundle(root: Path, docs: Path, check: bool) -> int:
    text = json.dumps(build_bundle(root, docs), indent=1, sort_keys=True,
                      ensure_ascii=False) + "\n"
    path = docs / "wiki.json"
    if check:
        if path.exists() and path.read_text() == text:
            print(f"OK wiki bundle current ({path})")
            return 0
        print("DRIFT: baked wiki bundle differs from regeneration", file=sys.stderr)
        return 1
    path.write_text(text)
    print(f"wrote {path}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", default=".", help="repo root, or /app in the image")
    ap.add_argument("--out", default=None, help="image mode: docs dir (default <root>/docs)")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--write-status", action="store_true")
    ap.add_argument("--verify", nargs="+", metavar="SLUG")
    ap.add_argument("--bundle", action="store_true", help="image mode: write docs/wiki.json")
    a = ap.parse_args()
    root = Path(a.root).resolve()
    if a.bundle:
        return bundle(root, Path(a.out).resolve() if a.out else root / "docs", a.check)
    if a.verify:
        verify(root, a.verify)
        return 0
    if a.write_status:
        write_status(root)
        return 0
    return repo_check(root)


if __name__ == "__main__":
    sys.exit(main())
