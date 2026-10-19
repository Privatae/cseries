"""server.applications.live_context_spec — what a declared live context card
may be, with no dependency outside the standard library and yaml-free input.

Split out of mastercode.live_context (2026-09-27) so the manifest loader can
validate an app's `live_context:` block anywhere a manifest is loaded — the
pod, the publisher, the store tooling on the platform — without importing the
brain. The reader and renderer stay in mastercode.live_context.

A card reads only its app's own declared tables and columns, and never a table
the platform itself creates (platform_tables): app tables and platform tables
share one schema in the pod database, and nothing else stops an app declaring
`kernel_secrets` as its own (review of #live-context, B1).
"""
from __future__ import annotations

import functools
import re
from pathlib import Path

# ── limits ────────────────────────────────────────────────────────────────
BLOCK_BUDGET_CHARS = 2400      # the whole Live context block, all cards
CARD_BUDGET_DEFAULT = 600
CARD_BUDGET_MAX = 1500
MAX_CARDS_SHOWN = 3
FETCH_TIMEOUT_S = 1.5          # per card; the whole block shares 3 s
BLOCK_TIMEOUT_S = 3.0
STATEMENT_TIMEOUT_MS = 800
MAX_KEYWORDS = 24
MAX_COLUMNS = 8
MAX_WHERE = 4
MAX_ROWS = 20
TEMPLATE_MAX = 240
VALUE_MAX = 120
REFRESH_MIN_S = 30
REFRESH_MAX_S = 24 * 3600
REFRESH_DEFAULT_S = 300

CARD_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,39}$")
IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
PLACEHOLDER_RE = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
REFRESH_RE = re.compile(r"^(\d+)\s*([smhd])$")


# ── declared cards (app / host): validation ───────────────────────────────

def parse_refresh(v) -> int | None:
    if v is None:
        return REFRESH_DEFAULT_S
    if isinstance(v, int) and not isinstance(v, bool):
        n = v
    else:
        m = REFRESH_RE.match(str(v).strip().lower())
        if not m:
            return None
        n = int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
    return n if REFRESH_MIN_S <= n <= REFRESH_MAX_S else None


def app_table_prefix(app_id: str) -> str:
    """The only table names a card of this app may read: `<app id>__…`, with
    every character outside [a-z0-9] as `_`. The double underscore keeps one
    app's prefix from being the start of another's ("ledger__" vs
    "ledger_pro__")."""
    return re.sub(r"[^a-z0-9]", "_", str(app_id or "").lower()) + "__"


def owns_table(app_id: str, table: str) -> bool:
    """True when `table` is `<this app's prefix><name>` and <name> neither
    starts with `_` nor contains `__`. App ids allow repeated and trailing
    hyphens, so without the second half `ledger--pro`'s prefix `ledger__pro__`
    would start with `ledger`'s `ledger__`, and `ledger` could read it (review
    of the prefix rule)."""
    if not app_id:
        return False
    pre = app_table_prefix(app_id)
    t = str(table or "")
    if not t.startswith(pre):
        return False
    rest = t[len(pre):]
    return bool(rest) and not rest.startswith("_") and "__" not in rest


def normalize_card_spec(raw, *, tables: dict[str, set[str]],
                        app_id: str) -> tuple[dict | None, list[str]]:
    """Validate one declared card against the tables it may read ({table:
    {columns}} — an app's own data.tables). Returns (spec, errors); spec is
    None when there is any error. Everything the renderer and the reader use
    is checked here, so a card that passes cannot build a query outside its
    app's declared columns or a template that reaches past a plain name.

    A card reads only tables named with its OWN app's prefix
    (app_table_prefix). A name list of platform tables cannot be complete —
    a live pod carries legacy tables no current schema file creates (15 on
    one Host, measured 2026-09-27: entity_genesis, voice_cost_ledger, …) — but
    no platform or legacy table, and no other app's table, starts with this
    app's prefix."""
    errs: list[str] = []
    if not isinstance(raw, dict):
        return None, ["a card is a mapping"]
    allowed = {"id", "title", "keywords", "reads", "render", "refresh", "budget_chars", "tool"}
    extra = sorted(set(raw) - allowed)
    if extra:
        errs.append(f"unknown keys: {', '.join(extra)}")
    cid = str(raw.get("id") or "").strip()
    if not CARD_ID_RE.match(cid):
        errs.append("id: lowercase letters, digits, - or _, 2-40 chars, starting with a letter")
    title = str(raw.get("title") or cid).strip()[:80]
    kws = raw.get("keywords")
    if not isinstance(kws, list) or not kws:
        errs.append("keywords: a non-empty list")
        kws = []
    kws = [str(k).strip().lower() for k in kws if str(k).strip()][:MAX_KEYWORDS]
    reads = raw.get("reads") if isinstance(raw.get("reads"), dict) else None
    if reads is None:
        errs.append("reads: a mapping with table and columns")
        reads = {}
    table = str(reads.get("table") or "").strip()
    if not IDENT_RE.match(table) or table not in tables:
        errs.append(f"reads.table: must be one of this app's declared tables ({', '.join(sorted(tables)) or 'none declared'})")
    elif is_platform_table(table):
        errs.append(f"reads.table: {table!r} is one of the platform's own tables, never an app's")
    elif not owns_table(app_id, table):
        errs.append(f"reads.table: a card reads only its app's own tables, named "
                    f"{app_table_prefix(app_id)}<name> (the name not starting with _ and "
                    f"holding no __); {table!r} is not")
    cols_ok = tables.get(table, set())
    cols = reads.get("columns") if isinstance(reads.get("columns"), list) else []
    cols = [str(c).strip() for c in cols]
    if not cols or len(cols) > MAX_COLUMNS or any(c not in cols_ok for c in cols):
        errs.append(f"reads.columns: 1-{MAX_COLUMNS} of the table's declared columns")
    where = reads.get("where") or {}
    if not isinstance(where, dict) or len(where) > MAX_WHERE:
        errs.append(f"reads.where: a mapping of at most {MAX_WHERE} column: value")
        where = {}
    for k, v in where.items():
        if k not in cols_ok:
            errs.append(f"reads.where: {k} is not a declared column")
        if not (v is None or isinstance(v, (str, int, float, bool))):
            errs.append(f"reads.where.{k}: a plain value")
    order = str(reads.get("order") or "").strip()
    order_col, order_desc = "", False
    if order:
        parts = order.split()
        order_col = parts[0]
        order_desc = len(parts) > 1 and parts[1].lower() == "desc"
        if order_col not in cols_ok or len(parts) > 2 or (len(parts) == 2 and parts[1].lower() not in ("asc", "desc")):
            errs.append("reads.order: '<declared column>' or '<declared column> desc'")
    try:
        limit = int(reads.get("limit") or 5)
    except (TypeError, ValueError):
        limit = 0
    if not 1 <= limit <= MAX_ROWS:
        errs.append(f"reads.limit: 1-{MAX_ROWS}")
    render = raw.get("render") if isinstance(raw.get("render"), dict) else None
    if render is None:
        errs.append("render: a mapping with head and/or each")
        render = {}
    tmpl = {}
    for part, names in (("head", {"count"}), ("each", set(cols)), ("empty", set())):
        t = render.get(part)
        if t is None:
            continue
        t = str(t)
        if len(t) > TEMPLATE_MAX:
            errs.append(f"render.{part}: at most {TEMPLATE_MAX} chars")
        bad = [n for n in PLACEHOLDER_RE.findall(t) if n not in names]
        stray = re.sub(PLACEHOLDER_RE, "", t)
        if bad:
            errs.append(f"render.{part}: unknown placeholder(s) {', '.join(sorted(set(bad)))}")
        if "{" in stray or "}" in stray:
            errs.append(f"render.{part}: braces only as {{name}} placeholders")
        tmpl[part] = t
    if not (tmpl.get("head") or tmpl.get("each")):
        errs.append("render: head or each is required")
    refresh = parse_refresh(raw.get("refresh"))
    if refresh is None:
        errs.append(f"refresh: like 30s, 5m, 2h; {REFRESH_MIN_S}s to 24h")
    try:
        budget = int(raw.get("budget_chars") or CARD_BUDGET_DEFAULT)
    except (TypeError, ValueError):
        budget = 0
    if not 100 <= budget <= CARD_BUDGET_MAX:
        errs.append(f"budget_chars: 100-{CARD_BUDGET_MAX}")
    if errs:
        return None, errs
    return {
        "id": cid, "title": title, "keywords": kws, "app_id": str(app_id),
        "reads": {"table": table, "columns": cols, "where": dict(where),
                  "order": order_col, "desc": order_desc, "limit": limit},
        "render": tmpl, "refresh_s": refresh, "budget": budget,
        "tool": str(raw.get("tool") or "")[:60],
    }, []


# ── the platform's own tables: never a card's ─────────────────────────────

_CREATE_RE = re.compile(
    r"create\s+(?:unlogged\s+|temporary\s+|temp\s+)?(?:table|view|materialized\s+view)\s+"
    r"(?:if\s+not\s+exists\s+)?(?:\"?public\"?\.)?\"?([a-z_][a-z0-9_]*)\"?", re.I)
_RENAME_RE = re.compile(
    r"alter\s+table\s+(?:if\s+exists\s+)?(?:\"?public\"?\.)?\"?[a-z_][a-z0-9_]*\"?\s+"
    r"rename\s+to\s+\"?([a-z_][a-z0-9_]*)\"?", re.I)


def schema_root() -> Path:
    """The tree that holds init-db.sql and migrations/ — /app in the image,
    the repo root in a checkout."""
    return Path(__file__).resolve().parents[2]


@functools.lru_cache(maxsize=1)
def platform_tables(root: str | None = None) -> frozenset:
    """Every table and view the platform's own schema creates or renames to:
    init-db.sql and migrations/*.sql, read from the files the image ships.
    Empty when the files are not there — the READ path treats that as refuse
    everything (mastercode.live_context.read_declared), the publish path as
    nothing to compare against."""
    base = Path(root) if root else schema_root()
    files = []
    if (base / "init-db.sql").is_file():
        files.append(base / "init-db.sql")
    mig = base / "migrations"
    if mig.is_dir():
        files.extend(sorted(mig.glob("*.sql")))
    names: set[str] = set()
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        names.update(m.group(1).lower() for m in _CREATE_RE.finditer(text))
        names.update(m.group(1).lower() for m in _RENAME_RE.finditer(text))
    return frozenset(names)


def is_platform_table(name: str) -> bool:
    n = (name or "").lower()
    return n.startswith("pg_") or n in platform_tables()
