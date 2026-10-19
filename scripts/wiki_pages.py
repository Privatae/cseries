"""The public wiki's pages: parse them, scan them for anything internal, and
tell whether a page, or the code it describes, has changed since it was
checked.

The wiki is the public explanation of the platform, served at
https://privatae.ai/wiki and searched by its Ask box. Its source is
docs/wiki/pages/*.md in this repo, next to the code the pages describe, so a
change to that code and the page that explains it can land in one PR.

Three readers go through this module:

  scripts/generate_capabilities.py  page cards in the Host's capability corpus,
                                    so a Host explains the platform from the
                                    same text the public reads
  scripts/generate_wiki.py          the repo check, the status file, and the
                                    public bundle baked into the image
  the frontend's wiki sync          reads that bundle out of a released image

A page is Markdown under a front-matter block, and holds ONLY what is
published:

  ---
  title: What a Host is
  summary: One or two plain sentences. The search snippet, and the card `what`.
  section: host
  order: 10
  questions:
    - What is a Privatae Host?
  app: workbench            # optional: the app this page documents
  ---

What each page describes lives beside the pages, in docs/wiki/sources.yaml,
which never enters the image — the pods carry the pages, not a map from each
explanation to the code behind it:

  what-a-host-is:
    sources:
      - server/network/nac_egress.py::resolve_exit
      - external:privatae-bastion/client-go/session.go
    verified: {date: 2026-09-30, digest: sha256:...}

A source is a repo path, optionally narrowed to one Python symbol with
`::Name` or `::Class.method` (decorators included: that is where a route's
auth lives). Narrowing matters: a whole-file digest flips on every unrelated
edit to a large module, and a page that is always "changed" teaches everyone
to ignore the flag. A source in another repo is `external:<repo>/<path>`: it
is recorded but cannot be digested here, so a page that cites one is never
published as current for a release.

The digest covers the page's own published text as well as its sources, so
editing a page un-checks it until someone checks it again.

Everything published must pass leak_scan(); scan_published() applies it to
every string of a page as it will ship.
"""
from __future__ import annotations

import ast
import hashlib
import html
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import yaml

PAGE_DIR = Path("docs") / "wiki" / "pages"
SOURCES_FILE = Path("docs") / "wiki" / "sources.yaml"
STATUS_FILE = Path("docs") / "wiki" / "status.json"

# The wiki's sections, in reading order. A page names one by key.
SECTIONS: list[tuple[str, str]] = [
    ("start", "Start here"),
    ("host", "The Host"),
    ("network", "Your network and machines"),
    ("building", "Building"),
    ("apps", "Apps"),
    ("security", "Security and privacy"),
    ("account", "Your account"),
]
SECTION_KEYS = {k for k, _ in SECTIONS}
PAGE_KEYS = {"title", "summary", "section", "order", "questions", "app"}

_FRONT = re.compile(r"\A---\n(.*?)\n---\n?", re.S)
_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_DATE = re.compile(r"^20\d\d-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
# A link to another wiki page is written [text](wiki:slug) and resolved by
# the site; the check refuses one that points at no page.
WIKI_LINK = re.compile(r"\]\(wiki:([a-z0-9-]+)(?:#[a-z0-9-]+)?\)")


# ── leak scan ────────────────────────────────────────────────────────────
# What must never reach a public page. Built from the member-repo publisher's
# gate 5 (the same categories were driven to zero in the two public repos),
# plus the platform's internal name, which is a legal constraint, and the
# shapes that give away how the platform is put together: internal addresses,
# source paths, tool-call syntax and API routes.
#
# A detailed architecture description is reconnaissance material, not just an
# embarrassment: Dockerfile.linux's docs ratchet records two models reading an
# internal architecture doc and choosing their target from it. The wiki says
# what the product does and why, at the level of the published blog, and this
# scan is the mechanical floor under that rule. Review is the rest of it.
LEAK_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("internal address", re.compile(
        r"\bf[cd][0-9a-f]{2}:|\.internal\b|\.svc\b|"
        r"\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b|\b192\.168\.\d{1,3}\.\d{1,3}\b|"
        r"\b172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b|"
        r"\b100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b", re.I)),
    ("credential or config path", re.compile(
        r"/etc/privatae|/root/|/opt/privatae|secrets\.json|gitea-[a-z]+-credentials|"
        r"INTERNAL_API_SECRET|\bPRIVATAE_[A-Z_]{3,}", re.I)),
    # Any path with a slash that ends in a source or config extension: the
    # published pages have no reason to name one.
    ("internal source path", re.compile(
        r"\b[\w.-]+/[\w./-]*\.(py|go|ts|tsx|js|jsx|mjs|sh|sql|yaml|yml|json|html|md|cseries)\b")),
    ("ticket or finding id", re.compile(
        r"\bT-\d{4}\b|\bR-\d{3}\b|\bPT-\d{3}\b|\bPR\s*#\d+|\bfinding\s*#\d+", re.I)),
    ("dated decision", re.compile(r"\b(Decided|Reported|Measured|measured)\s+20\d\d-\d\d")),
    ("design doc reference", re.compile(r"DESIGN-[a-z]|security-model|plan §")),
    ("tool-call syntax", re.compile(r"\b[a-z_]{3,}\(\s*(\{|[a-z_]+\s*=)|\bop\s*=\s*['\"]")),
    ("API route", re.compile(r"(^|[\s`(\"'])/api/")),
]


def _normalize(text: str) -> str:
    """What a reader (or a renderer) would see: HTML entities decoded — Markdown
    renders them — compatibility forms folded, and zero-width and other format
    characters removed, so a name cannot hide behind &#101; or a zero-width
    space or a look-alike letter."""
    t = html.unescape(str(text or ""))
    t = unicodedata.normalize("NFKC", t)
    # Cf: zero-width and other format characters. Mn: combining marks, which
    # after NFKC composition are left only where something was deliberately
    # stacked (the combining grapheme joiner, variation selectors).
    return "".join(ch for ch in t if unicodedata.category(ch) not in ("Cf", "Mn"))


# Letters a scan must read as their Latin twins (Cyrillic and Greek look-alikes
# NFKC does not fold).
_CONFUSABLE = str.maketrans(
    "АВЕКМНОРСТХаеорсухіјѕԁԛѡЅкмнтвгпΑΒΕΖΗΙΚΜΝΟΡΤΥΧοκνρταειυ",
    "ABEKMHOPCTXaeopcyxijsdqwSkmhtbrnABEZHIKMNOPTYXokvptaeiu")

# Markup a renderer removes, so a name split by emphasis or an empty tag
# (secr**e**ts.json, secr<b></b>ets.json) reads whole to the scan as it does to a reader.
_MARKUP = re.compile(r"<[^>]*>|[*_~`]")


# A URL on the public site is a published address, not a path into a private
# source tree — the bastion's install one-liner ends in .sh. Only the
# source-path pattern looks past these; every other pattern still reads them,
# and any other URL is scanned in full.
_URL = re.compile(r"https://(?:www\.)?privatae\.ai/\S*", re.I)
_URL_BLIND = {"internal source path"}


def leak_scan(text: str) -> list[tuple[str, int, str]]:
    """Every leak-pattern hit in one published text → [(label, line, excerpt)].
    Line by line for a useful location, then once over the whole text with
    line breaks folded, so a pattern broken across a soft wrap is still found."""
    norm = _normalize(text).translate(_CONFUSABLE)
    # The text as written, and as a renderer shows it with markup removed;
    # both have the same lines, so a hit's line number is right in either.
    views = [norm]
    stripped = _MARKUP.sub("", norm)
    if stripped != norm:
        views.append(stripped)
    hits: list[tuple[str, int, str]] = []
    seen: set[tuple[str, int]] = set()

    def search(label: str, pat: re.Pattern, s: str):
        return pat.search(_URL.sub(" ", s) if label in _URL_BLIND else s)

    def add(label: str, line: int, m: re.Match) -> None:
        if (label, line) not in seen:
            seen.add((label, line))
            hits.append((label, line, m.string[max(0, m.start() - 30):m.end() + 30].strip()))

    for view in views:
        for lineno, line in enumerate(view.splitlines() or [""], 1):
            for label, pat in LEAK_PATTERNS:
                m = search(label, pat, line)
                if m:
                    add(label, lineno, m)
    found = {label for label, _line in seen}
    for view in views:
        folded = " ".join(view.split())
        for label, pat in LEAK_PATTERNS:
            if label in found:
                continue
            m = search(label, pat, folded)
            if m:
                found.add(label)
                add(label, 0, m)
    return hits


def scan_published(obj, where: str = "") -> list[str]:
    """Leak-scan every string in a published structure (a bundle page, a card)
    — keys and values alike — so no field ships unscanned."""
    out: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out += scan_published(k, f"{where}.{k}(key)")
            out += scan_published(v, f"{where}.{k}")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            out += scan_published(v, f"{where}[{i}]")
    elif isinstance(obj, str):
        for label, line, excerpt in leak_scan(obj):
            out.append(f"{where.lstrip('.')}: {label} (line {line}): {excerpt!r}")
    return out


# ── pages ────────────────────────────────────────────────────────────────

@dataclass
class Page:
    slug: str
    path: Path
    title: str
    summary: str
    section: str
    order: int
    questions: list[str]
    body: str
    app: str = ""
    sources: list[str] = field(default_factory=list)
    verified_date: str = ""
    verified_digest: str = ""
    problems: list[str] = field(default_factory=list)

    def public_text(self) -> dict[str, str]:
        """Every field of the page file that gets published, by name."""
        return {
            "slug": self.slug,
            "title": self.title,
            "summary": self.summary,
            "questions": "\n".join(self.questions),
            "app": self.app,
            "body": self.body,
        }

    def links(self) -> list[str]:
        return WIKI_LINK.findall(self.body)


def _as_str_list(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v.strip()] if v.strip() else []
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    return [str(v).strip()]


def parse_page(path: Path) -> Page:
    """One page file → Page. Structural problems are recorded on the page
    (page.problems), never raised, so one bad page cannot hide the others."""
    text = path.read_text(encoding="utf-8")
    problems: list[str] = []
    m = _FRONT.match(text)
    meta: dict = {}
    if not m:
        problems.append("no front-matter block (--- … ---) at the top")
        body = text
    else:
        body = text[m.end():]
        try:
            loaded = yaml.safe_load(m.group(1))
            meta = loaded if isinstance(loaded, dict) else {}
            if not isinstance(loaded, dict):
                problems.append("front matter is not a mapping")
        except yaml.YAMLError as e:
            problems.append(f"front matter is not valid YAML: {e}")
    extra = sorted(set(meta) - PAGE_KEYS)
    if extra:
        problems.append(f"front matter keys {extra} are not page keys — a page holds only what is "
                        f"published ({sorted(PAGE_KEYS)}); sources and checks go in {SOURCES_FILE}")
    slug = path.stem
    if not _SLUG.match(slug):
        problems.append(f"file name {path.name!r} is not a lowercase-hyphen slug")
    try:
        order = int(meta.get("order", 100))
    except (TypeError, ValueError):
        problems.append(f"order {meta.get('order')!r} is not an integer")
        order = 100
    page = Page(
        slug=slug,
        path=path,
        title=str(meta.get("title") or "").strip(),
        summary=" ".join(str(meta.get("summary") or "").split()),
        section=str(meta.get("section") or "").strip(),
        order=order,
        questions=_as_str_list(meta.get("questions")),
        body=body.strip() + "\n",
        app=str(meta.get("app") or "").strip(),
        problems=problems,
    )
    for f in ("title", "summary", "section"):
        if not getattr(page, f):
            page.problems.append(f"`{f}` is missing")
    if page.section and page.section not in SECTION_KEYS:
        page.problems.append(f"section {page.section!r} is not one of {sorted(SECTION_KEYS)}")
    if not page.questions:
        page.problems.append("`questions` is empty — list the questions this page answers")
    return page


def load_pages(page_dir: Path) -> list[Page]:
    """Every page under page_dir, sorted by (section order, order, slug).
    A missing directory is an empty wiki, not an error. Sources are not
    attached here; the repo check does that (attach_sources)."""
    if not page_dir.is_dir():
        return []
    rank = {k: i for i, (k, _) in enumerate(SECTIONS)}
    pages = [parse_page(p) for p in sorted(page_dir.glob("*.md"))]
    return sorted(pages, key=lambda p: (rank.get(p.section, len(rank)), p.order, p.slug))


def load_sources(path: Path) -> tuple[dict, list[str]]:
    """docs/wiki/sources.yaml → ({slug: entry}, problems)."""
    if not path.exists():
        return {}, [f"{path} is missing"]
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        return {}, [f"{path} is not valid YAML: {e}"]
    if not isinstance(data, dict):
        return {}, [f"{path} is not a mapping of slug → entry"]
    return data, []


def attach_sources(pages: list[Page], data: dict) -> list[str]:
    """Put each page's sources and last check on it. → problems for entries
    that name no page."""
    slugs = {p.slug for p in pages}
    problems = [f"{SOURCES_FILE}: {s!r} names no page" for s in sorted(set(data) - slugs)]
    for p in pages:
        entry = data.get(p.slug) or {}
        if not isinstance(entry, dict):
            p.problems.append(f"{SOURCES_FILE}: the entry for {p.slug} is not a mapping")
            continue
        p.sources = _as_str_list(entry.get("sources"))
        v = entry.get("verified") if isinstance(entry.get("verified"), dict) else {}
        p.verified_date = str(v.get("date") or "").strip()
        p.verified_digest = str(v.get("digest") or "").strip()
        if not p.sources:
            p.problems.append(f"no sources in {SOURCES_FILE} — every page describes code; name it")
        if p.verified_date and not _DATE.match(p.verified_date):
            p.problems.append(f"verified date {p.verified_date!r} is not YYYY-MM-DD")
    return problems


def dump_sources(data: dict) -> str:
    """The sources file as --verify writes it: sorted, one shape, so a diff
    shows only what changed."""
    clean = {}
    for slug in sorted(data):
        e = data[slug] or {}
        out = {"sources": _as_str_list(e.get("sources"))}
        v = e.get("verified") if isinstance(e.get("verified"), dict) else None
        if v:
            out["verified"] = {"date": str(v.get("date") or ""), "digest": str(v.get("digest") or "")}
        # Any other key an author added (a note, say) is kept as it was.
        out.update({k: e[k] for k in e if k not in ("sources", "verified")})
        clean[slug] = out
    header = ("# What each public wiki page describes, and when it was last checked against it.\n"
              "# Authored: `sources`. Written by `scripts/generate_wiki.py --verify`: `verified`.\n"
              "# Never copied into the image. See docs/wiki/README.md.\n"
              "# --verify rewrites this file: keys survive, # comments below this header do not.\n")
    return header + yaml.safe_dump(clean, sort_keys=False, default_flow_style=False, width=100)


# ── digests ──────────────────────────────────────────────────────────────

def _python_symbol_source(src: str, dotted: str) -> str | None:
    """The source of one top-level def/class, or Class.method — from its first
    decorator to its last line — or None. Where a name is defined more than
    once, the LAST definition is the one that runs, so that is the one read."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    body = tree.body
    node = None
    for part in dotted.split("."):
        matches = [n for n in body
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                   and n.name == part]
        if not matches:
            return None
        node = matches[-1]
        body = getattr(node, "body", [])
    if node is None:
        return None
    start = min([d.lineno for d in node.decorator_list] + [node.lineno])
    lines = src.splitlines(keepends=True)
    return "".join(lines[start - 1:node.end_lineno])


EXTERNAL = "external:"


def is_external(source: str) -> bool:
    return source.startswith(EXTERNAL)


def page_digest(root: Path, page: Page) -> tuple[str, list[str]]:
    """→ (digest over the page's published text and every in-repo source,
    problems). A source that cannot be read is a problem, not a silent skip: a
    page citing code that no longer exists is describing something gone.
    External sources are named in the digest but not read."""
    h = hashlib.sha256()
    problems: list[str] = []
    public = page.public_text()
    h.update(b"page\0" + "\0".join(f"{k}={public[k]}" for k in sorted(public)).encode("utf-8") + b"\0")
    for s in sorted(set(page.sources)):
        if is_external(s):
            if not s[len(EXTERNAL):].strip("/"):
                problems.append(f"source {s!r}: name the repo and path")
            h.update(s.encode("utf-8") + b"\0")
            continue
        rel, _, symbol = s.partition("::")
        p = root / rel
        if not p.is_file():
            problems.append(f"source {s!r}: {rel} does not exist")
            continue
        data = p.read_bytes()
        if symbol:
            if p.suffix != ".py":
                problems.append(f"source {s!r}: ::symbol is only supported for .py files")
                continue
            seg = _python_symbol_source(data.decode("utf-8", "replace"), symbol)
            if seg is None:
                problems.append(f"source {s!r}: no top-level def/class {symbol!r} in {rel}")
                continue
            data = seg.encode("utf-8")
        h.update(s.encode("utf-8") + b"\0" + hashlib.sha256(data).digest())
    return "sha256:" + h.hexdigest(), problems
