"""mastercode.live_context — software-driven context: data rendered into the
prompt by code before the model runs.

WHY. A Host answered "how are the servers looking" by probing — get_network,
nac_status, oversight reads — one tool call after another before it could say
anything. Curated, relevant live context replaces some of those tool calls,
so the Host does not probe everything just to say something useful. And apps meet AI this way: an app does not build AI in; it
publishes its data as live context, and every Host that has the app reasons
over it.

A CARD is a declaration, not text. At prompt time the platform picks the cards
whose keywords the message scores (the same scoring the turn-type classifier
and tool-group loading use — no model call), reads each one's source, renders
it through a template with no model, and places the result in a fenced "Live
context" block with its source and as-of time. Nothing a card shows is ever
stored: it is rebuilt every turn, so it cannot go stale the way a memory did.

Three authors, one shape:
  platform  cards over the platform's own tables (PLATFORM_CARDS below)
  app       `live_context:` in an app's app.yaml; reads only that app's
            declared data.tables and columns
  host      a card the Host built for the person, in a project module with
            target 'live_context'; live only after the person's one click on
            the rendered preview

A declared card reads under a READ ONLY transaction with a short statement
timeout, selects only declared columns of a declared table, and binds every
value. Values are screened through the live input guard before they reach the
prompt: a row is data a stranger may have typed.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger("cseries.mastercode.live_context")

from server.applications.live_context_spec import (  # noqa: F401 — re-exported
    BLOCK_BUDGET_CHARS, CARD_BUDGET_DEFAULT, CARD_BUDGET_MAX, MAX_CARDS_SHOWN,
    FETCH_TIMEOUT_S, BLOCK_TIMEOUT_S, STATEMENT_TIMEOUT_MS, MAX_KEYWORDS,
    MAX_COLUMNS, MAX_WHERE, MAX_ROWS, TEMPLATE_MAX, VALUE_MAX, REFRESH_MIN_S,
    REFRESH_MAX_S, REFRESH_DEFAULT_S, CARD_ID_RE, IDENT_RE, PLACEHOLDER_RE,
    REFRESH_RE, parse_refresh, normalize_card_spec, is_platform_table,
    platform_tables, app_table_prefix, owns_table,
)


@dataclass
class Card:
    """One card, ready to fetch. `fetch` returns (lines, as_of) — as_of is when
    the SOURCE was last true (not when this read ran), None when unknown."""
    id: str
    title: str
    keywords: list[str]
    source: str
    origin: str                      # platform | app:<id> | host:<card id>
    refresh_s: int = REFRESH_DEFAULT_S
    budget: int = CARD_BUDGET_DEFAULT
    tool_hint: str = ""              # the tool that drills in, named when a card is priced out
    fetch: Optional[Callable[..., Awaitable[tuple[list[str], Any]]]] = None
    spec: dict = field(default_factory=dict)


# ── scoring (the turn-type / tool-group rule, no model) ───────────────────

def score_cards(message: str, cards: list[Card]) -> list[tuple[float, Card]]:
    """Every card whose keywords score at or above the load threshold for this
    message, best first. A keyword that is also an everyday word counts half
    (prefire_tools.WEAK_WORDS), so one strong word or two everyday ones load a
    card — exactly how tool groups and turn types load."""
    from mastercode.runtime.prefire_tools import (
        LOAD_THRESHOLD, STRONG_HIT, WEAK_HIT, WEAK_WORDS, _tokens,
    )
    toks = _tokens(message or "")
    if not toks:
        return []
    out: list[tuple[float, Card]] = []
    for c in cards:
        kws = {k.lower() for k in c.keywords}
        s = sum(WEAK_HIT if k in WEAK_WORDS else STRONG_HIT for k in toks & kws)
        if s >= LOAD_THRESHOLD:
            out.append((s, c))
    out.sort(key=lambda sc: (-sc[0], sc[1].id))
    return out


# ── declared cards (app / host): reading ─────────────────────────────────

def _qi(name: str) -> str:
    """A quoted identifier. Only ever called on names normalize_card_spec
    matched against IDENT_RE and the app's declared names."""
    if not IDENT_RE.match(name):
        raise ValueError(f"not an identifier: {name!r}")
    return '"' + name + '"'


def build_queries(reads: dict) -> tuple[str, str, list]:
    """(rows SQL, count SQL, args) for a validated `reads`. Every value binds."""
    args: list = []
    conds = []
    for k, v in reads["where"].items():
        if v is None:
            conds.append(f"{_qi(k)} IS NULL")
        else:
            args.append(v)
            conds.append(f"{_qi(k)} = ${len(args)}")
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    cols = ", ".join(_qi(c) for c in reads["columns"])
    order = (f" ORDER BY {_qi(reads['order'])}{' DESC' if reads['desc'] else ''} NULLS LAST"
             if reads["order"] else "")
    args_rows = args + [int(reads["limit"])]
    rows_sql = f"SELECT {cols} FROM {_qi(reads['table'])}{where}{order} LIMIT ${len(args_rows)}"
    count_sql = f"SELECT COUNT(*) FROM {_qi(reads['table'])}{where}"
    return rows_sql, count_sql, args_rows


READ_ROLE = "host_recall"   # v139: SELECT on app tables, never kernel_secrets


async def read_declared(pool, reads: dict, *, app_id: str) -> tuple[list[dict], int]:
    """The rows and the count, READ ONLY, under a short statement timeout, as
    the restricted `host_recall` role — never as `entity`, which owns every
    table in the pod database and bypasses row security. The table is
    re-checked here against the platform's
    own tables, and a pod that cannot read its schema files refuses every
    read: a card is never the one thing that reaches a platform table."""
    table = reads.get("table", "")
    if not owns_table(app_id, table):
        raise PermissionError(f"{table!r} is not one of {app_id!r}'s own tables")
    if not platform_tables():
        raise PermissionError("the platform's table list is unreadable; declared cards are off")
    if is_platform_table(table):
        raise PermissionError(f"{table!r} is a platform table")
    rows_sql, count_sql, args = build_queries(reads)
    async with pool.acquire() as conn:
        async with conn.transaction(readonly=True):
            await conn.execute(f"SET LOCAL ROLE {READ_ROLE}")
            await conn.execute(f"SET LOCAL statement_timeout = {int(STATEMENT_TIMEOUT_MS)}")
            rows = await conn.fetch(rows_sql, *args)
            count = await conn.fetchval(count_sql, *args[:-1])
    return [dict(r) for r in rows], int(count or 0)


# ── rendering ─────────────────────────────────────────────────────────────

def _screen(value) -> str:
    """A value headed into the prompt. Rows are data a stranger may have typed;
    the live input guard strips role breaks and override phrasing. Fails closed."""
    if isinstance(value, (_dt.datetime, _dt.date)):
        text = value.isoformat()[:16].replace("T", " ")
    else:
        text = " ".join(str("" if value is None else value).split())
    # Screen the whole value, then clip: the screen normalises (NFKC turns "…"
    # into "..."), so clipping first could leave the result over the cap.
    try:
        from mastercode.runtime.input_guard import sanitize
        text, _flags = sanitize(text[: VALUE_MAX * 4])
    except Exception:  # noqa: BLE001
        return "(withheld)"
    if len(text) > VALUE_MAX:
        text = text[: VALUE_MAX - 3] + "..."
    return text


def fill(template: str, values: dict) -> str:
    return PLACEHOLDER_RE.sub(lambda m: _screen(values.get(m.group(1), "")), template)


LINE_MAX = 400


def _screen_line(line: str) -> str:
    """A whole rendered line: the template text is the card author's — an app
    author or the Host — as untrusted as the values in it."""
    try:
        from mastercode.runtime.input_guard import sanitize
        line, _flags = sanitize(line)
    except Exception:  # noqa: BLE001
        return "(withheld)"
    return line if len(line) <= LINE_MAX else line[: LINE_MAX - 3] + "..."


def render_declared(spec: dict, rows: list[dict], count: int) -> list[str]:
    r = spec["render"]
    lines: list[str] = []
    if not rows and r.get("empty"):
        return [_screen_line(fill(r["empty"], {}))]
    if r.get("head"):
        lines.append(_screen_line(fill(r["head"], {"count": count})))
    if r.get("each"):
        lines.extend(_screen_line(fill(r["each"], row)) for row in rows)
    return lines


def declared_card(spec: dict, *, origin: str, source: str, app_id: str) -> Card:
    async def _fetch(pool, entity_id, sess=None):
        rows, count = await read_declared(pool, spec["reads"], app_id=app_id)
        return render_declared(spec, rows, count), _dt.datetime.now(_dt.timezone.utc)
    return Card(id=spec["id"], title=spec["title"], keywords=spec["keywords"],
                source=source, origin=origin, refresh_s=spec["refresh_s"],
                budget=spec["budget"], tool_hint=spec.get("tool") or "", fetch=_fetch,
                spec=spec)


# ── platform cards ────────────────────────────────────────────────────────

_SEV_RANK = {"critical": 0, "high": 1, "warn": 2, "notice": 3}


async def _fetch_findings(pool, entity_id, sess=None):
    rows = await pool.fetch(
        "SELECT severity, machine, evidence->>'title' AS title, first_seen, last_seen "
        "FROM oversight_findings WHERE resolved_at IS NULL")
    if not rows:
        return ["No open findings on any machine."], None
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["severity"]] = counts.get(r["severity"], 0) + 1
    as_of = max(r["last_seen"] for r in rows)
    head = ", ".join(f"{counts[s]} {s}" for s in sorted(counts, key=lambda s: _SEV_RANK.get(s, 9)))
    lines = [f"{len(rows)} open findings: {head}."]
    worst = sorted(rows, key=lambda r: (_SEV_RANK.get(r["severity"], 9), -r["last_seen"].timestamp()))[:5]
    for r in worst:
        since = r["first_seen"].strftime("%Y-%m-%d") if r["first_seen"] else "?"
        lines.append(f"- [{r['severity']}] {_screen(r['machine'])}: {_screen(r['title'])} (since {since})")
    return lines, as_of


def _when(v):
    """list_bastions returns ISO strings (topology._bastion_row via _iso)."""
    if isinstance(v, _dt.datetime):
        return v if v.tzinfo else v.replace(tzinfo=_dt.timezone.utc)
    if not v:
        return None
    try:
        d = _dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=_dt.timezone.utc)


async def _fetch_machines(pool, entity_id, sess=None):
    """Machines paired with this Host, from list_bastions — the row shape the
    Network app reads (topology._bastion_row: `bastion_version`, ISO
    `last_seen_at`, `online` from the heartbeat window)."""
    from server.network.topology import list_bastions
    bs = await list_bastions(pool, int(entity_id))
    if not bs:
        return ["No machines are paired with this Host."], None
    on = [b for b in bs if b.get("online")]
    off = [b for b in bs if not b.get("online")]
    seen = [w for w in (_when(b.get("last_seen_at")) for b in bs) if w]
    as_of = max(seen) if seen else None
    versions = Counter(str(b.get("bastion_version") or "unknown") for b in bs)
    vtxt = ", ".join(f"{v} ×{n}" for v, n in versions.most_common(4))
    lines = [f"{len(bs)} machines: {len(on)} online, {len(off)} offline. Bastion versions: {vtxt}."]
    for b in sorted(off, key=lambda b: str(b.get("last_seen_at") or ""))[:6]:
        ls = _when(b.get("last_seen_at"))
        lines.append(f"- offline: {_screen(b.get('hostname') or b.get('name'))} "
                     f"(last beat {ls.strftime('%Y-%m-%d %H:%M UTC') if ls else 'never'})")
    return lines, as_of


_SHIP_WORD = {"planned": "asking your device", "awaiting_tap": "waiting for the tap",
              "running": "running", "undoing": "undoing"}


async def _fetch_ships(pool, entity_id, sess=None):
    """The ship runner's rows (v194, server/ships/runner.py): every run under
    way, the latest run of a pipeline when it stopped for the Host (a step
    whose rule is `ask`; a later run answers an earlier one), and how each
    pipeline's last run ended. Records only; no model wrote them. "Ship" here
    is a pipeline run on the person's machines, not an app shipped into the
    Host, so every line says "pipeline"."""
    rows = await pool.fetch(
        "SELECT r.id, p.name, r.state, r.step, r.commit_sha, r.error, r.needs_host, r.last_seen, "
        "       s.step AS step_name, s.machine, s.state AS step_state "
        "  FROM ship_runs r JOIN ship_pipelines p ON p.id = r.pipeline_id "
        "  LEFT JOIN ship_steps s ON s.run_id = r.id AND s.n = r.step "
        " WHERE r.state IN ('planned','awaiting_tap','running','undoing') "
        "    OR r.id IN (SELECT max(id) FROM ship_runs GROUP BY pipeline_id) "
        " ORDER BY r.id DESC LIMIT 12")
    if not rows:
        return ["No pipeline has run on this Host (the Ships app)."], None
    as_of = max(r["last_seen"] for r in rows)
    active = [r for r in rows if r["state"] in _SHIP_WORD]
    lines = [f"{len(active)} pipeline run(s) under way." if active else "No pipeline run is under way."]
    for r in active:
        where = (f", step {r['step']} ({_screen(r['step_name'])}) on {_screen(r['machine'])} {r['step_state']}"
                 if r["state"] in ("running", "undoing") and r["step_name"] else "")
        lines.append(f"- pipeline {_screen(r['name'])} run {r['id']} at {r['commit_sha'][:12]}: "
                     f"{_SHIP_WORD[r['state']]}{where}")
    for r in rows:
        if r["needs_host"]:
            lines.append(f"- NEEDS YOU: pipeline {_screen(r['name'])} run {r['id']} {r['state']}: {_screen(r['error'])}")
    seen = set()
    for r in rows:
        if r["state"] not in _SHIP_WORD and not r["needs_host"] and r["name"] not in seen:
            seen.add(r["name"])
            tail = f": {_screen(r['error'])}" if r["error"] and r["state"] != "passed" else ""
            lines.append(f"- last run of pipeline {_screen(r['name'])}: run {r['id']} at {r['commit_sha'][:12]}, "
                         f"{r['state']}{tail}")
    return lines, as_of


PLATFORM_CARDS: list[Card] = [
    Card(id="machines", title="Machines", source="bastion heartbeats",
         origin="platform", refresh_s=180, budget=700, tool_hint="get_network",
         keywords=["server", "servers", "machine", "machines", "bastion", "bastions",
                   "fleet", "box", "boxes", "online", "offline", "heartbeat", "hosts"],
         fetch=_fetch_machines),
    Card(id="findings", title="Findings on your machines", source="oversight ledger",
         origin="platform", refresh_s=600, budget=900, tool_hint="",
         keywords=["finding", "findings", "alert", "alerts", "security", "oversight",
                   "servers", "fleet", "threat", "threats", "incident", "incidents",
                   "risk", "risks"],
         fetch=_fetch_findings),
    # The ship runner's records (server/ships/runner.py). A platform card
    # because its tables are platform migrations (v194), which declared cards
    # refuse (live_context_spec.platform_tables). Words that name shipping, and
    # not "build": a Workbench build is not a ship.
    Card(id="ships", title="Ships (pipeline runs)", source="ship runner",
         origin="platform", refresh_s=60, budget=900, tool_hint="",
         keywords=["ship", "ships", "shipping", "shipped", "deploy", "deploys", "deployed",
                   "deployment", "release", "releases", "rollout", "pipeline", "pipelines"],
         fetch=_fetch_ships),
]
# Not cards on purpose: host health is the `diagnostics` block and recent work
# is the `timeline` block, both already rendered every turn. A card that
# repeated them would pay twice for the same lines.


# ── assembling the block ──────────────────────────────────────────────────

def _age_line(card: Card, as_of, now: _dt.datetime) -> str:
    as_of = _when(as_of) if as_of is not None else None
    if as_of is None:
        return f"{card.source}, read {now.strftime('%H:%M')} UTC"
    age = (now - as_of).total_seconds()
    stamp = as_of.strftime("%H:%M UTC")
    if age > card.refresh_s:
        mins = int(age // 60)
        when = f"{mins} min ago" if mins < 120 else f"{int(mins // 60)} h ago"
        return f"{card.source}, last read {when} ({stamp}) — may have changed since"
    return f"{card.source}, as of {stamp}"


async def build_block(pool, entity_id: int, message: str, cards: list[Card], *,
                      always: list[str] | None = None, sess=None,
                      now: _dt.datetime | None = None) -> tuple[str | None, list[dict]]:
    """The Live context block for one turn and what it carried (for prompt_log).
    `always` names cards a turn type carries regardless of the message."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    picked = score_cards(message, cards)
    by_id = {c.id: c for c in cards}
    order: list[Card] = [by_id[i] for i in (always or []) if i in by_id]
    for _s, c in picked:
        if c not in order:
            order.append(c)
    if not order:
        return None, []

    async def _one(c: Card):
        return await asyncio.wait_for(c.fetch(pool, entity_id, sess), timeout=FETCH_TIMEOUT_S)

    try:
        results = await asyncio.wait_for(
            asyncio.gather(*(_one(c) for c in order[:MAX_CARDS_SHOWN + 2]),
                           return_exceptions=True),
            timeout=BLOCK_TIMEOUT_S)
    except (asyncio.TimeoutError, Exception) as e:  # noqa: BLE001 — never a failed turn
        log.info("live context: block skipped this turn: %s", e)
        return None, []
    parts = ["## Live context",
             "Read by code this turn from the source named on each card — current, not "
             "remembered. A card says when its source is older than it should be."]
    used = sum(len(p) + 1 for p in parts)
    shown: list[dict] = []
    priced_out: list[Card] = []
    for c, res in zip(order, results):
        if isinstance(res, BaseException):
            log.debug("live context: card %s skipped: %s", c.id, res)
            priced_out.append(c)
            continue
        try:
            lines, as_of = res
            body = "\n".join(str(x) for x in lines)
            if len(body) > c.budget:
                body = body[: c.budget - 1] + "…"
            # The title and source name come from an app author or the Host — as
            # untrusted as a row value, so they are screened the same way.
            section = f"### {_screen(c.title)} — {_screen(_age_line(c, as_of, now))}\n{body}"
        except Exception as e:  # noqa: BLE001 — one bad card never drops the block
            log.debug("live context: card %s did not render: %s", c.id, e)
            priced_out.append(c)
            continue
        if len(shown) >= MAX_CARDS_SHOWN or used + len(section) + 1 > BLOCK_BUDGET_CHARS:
            priced_out.append(c)
            continue
        parts.append(section)
        used += len(section) + 1
        _ao = _when(as_of) if as_of is not None else None
        shown.append({"id": c.id, "origin": c.origin, "source": c.source,
                      "as_of": _ao.isoformat() if _ao else None, "chars": len(section)})
    for c in order[MAX_CARDS_SHOWN + 2:]:
        priced_out.append(c)
    if not shown:
        return None, []
    if priced_out:
        names = ", ".join(_screen(c.title) + (f" (`{_screen(c.tool_hint)}`)" if c.tool_hint else "")
                          for c in priced_out[:4])
        parts.append(f"Also relevant, not shown: {names}.")
    return "\n".join(parts), shown


# ── where the cards come from, per turn ───────────────────────────────────

APP_CARDS_TTL_S = 60.0
_installed_cache: dict = {"at": 0.0, "apps": {}}


async def installed_apps_data(pool) -> dict:
    """{app_id: {"name", "tables": {table: {cols}}, "cards": (spec,…)}} for
    every active installed app, from its manifest. Cached a minute: the install
    set changes at install time, not per turn — so an uninstalled app's cards
    can ride for up to that minute."""
    import time
    now = time.time()
    if now - _installed_cache["at"] < APP_CARDS_TTL_S:
        return dict(_installed_cache["apps"])
    out: dict = {}
    from server.applications import installer, registry
    from server.applications.manifest import load
    for app in await registry.list_active(pool):
        try:
            m = load(installer.INSTALL_ROOT / app.id / "app.yaml")
        except Exception:  # noqa: BLE001 — one unreadable manifest, not all apps
            continue
        out[app.id] = {"name": m.name,
                       "tables": {t.name: set(t.columns) for t in m.data.tables},
                       "cards": tuple(m.live_context or ())}
    _installed_cache.update(at=now, apps=out)
    return dict(out)


def shared_tables(apps: dict) -> set[str]:
    """Table names more than one installed app declares. Names are global in
    the pod database, so a card over one would read another app's rows —
    every card over such a table is refused."""
    owners = Counter(t for a in apps.values() for t in a["tables"])
    return {t for t, n in owners.items() if n > 1}


def spec_within(spec: dict, tables: dict[str, set[str]], shared: set[str],
                app_id: str) -> bool:
    """A stored spec still reads only what its app declares today, under its
    app's own prefix (a live card is re-checked every turn against the
    current manifest)."""
    r = spec.get("reads") or {}
    cols = tables.get(r.get("table"))
    if (cols is None or r.get("table") in shared or is_platform_table(r.get("table", ""))
            or not owns_table(app_id, r.get("table", ""))):
        return False
    used = set(r.get("columns") or []) | set((r.get("where") or {}).keys())
    if r.get("order"):
        used.add(r["order"])
    return used <= cols


async def _app_cards(pool, apps: dict) -> list[Card]:
    """Cards the installed apps publish in their app.yaml (validated at load
    against each app's own declared data), minus any over a shared table."""
    shared = shared_tables(apps)
    out: list[Card] = []
    for app_id, a in apps.items():
        for spec in a["cards"]:
            if not spec_within(spec, a["tables"], shared, app_id):
                continue
            out.append(declared_card(spec, origin=f"app:{app_id}", app_id=app_id,
                                     source=f"{a['name']}'s {spec['reads']['table']}"))
    return out


async def _host_cards(pool, entity_id: int, apps: dict) -> list[Card]:
    """Cards the Host built and the person made live (live_context_host),
    re-checked against the app's CURRENT manifest: an app that dropped the
    column, or was uninstalled, takes the card out of every prompt."""
    from mastercode.live_context_host import live_cards
    shared = shared_tables(apps)
    out: list[Card] = []
    for row in await live_cards(pool, entity_id):
        spec, app = row["spec"], apps.get(row["app_id"])
        if app is None or not spec_within(spec, app["tables"], shared, row["app_id"]):
            continue
        try:
            out.append(declared_card(
                spec, origin=f"host:{row['card_id']}", app_id=row["app_id"],
                source=f"{row['app_id']}'s {spec['reads']['table']}"))
        except Exception:  # noqa: BLE001 — a malformed stored spec is skipped
            continue
    return out


async def cards_for(pool, sess) -> list[Card]:
    """Every card this turn may draw from: the platform's, the installed apps',
    and the ones the person made live. Ids are unique across the three — a
    later author never shadows an earlier one (platform, then app, then host)."""
    try:
        apps = await installed_apps_data(pool)
    except Exception as e:  # noqa: BLE001 — platform cards still ride
        log.debug("live context: installed apps unreadable: %s", e)
        apps = {}
    seen: set[str] = set()
    out: list[Card] = []
    for group in (PLATFORM_CARDS, await _app_cards(pool, apps),
                  await _host_cards(pool, int(sess.entity_id), apps)):
        for c in group:
            if c.id not in seen:
                seen.add(c.id)
                out.append(c)
    return out


def type_cards(turn_type) -> list[str]:
    """The card ids a turn type carries on every turn of its kind (turn type
    spec `cards`), in its order. [] for an untyped turn."""
    if not isinstance(turn_type, dict):
        return []
    return [str(c) for c in (turn_type.get("cards") or []) if str(c).strip()]
