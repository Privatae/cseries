"""runtime/discovery_index.py — what find_tools finds besides tools.

find_tools is the Host's one discovery door. Tools come from the turn's own
catalog (tool_doors.DoorCatalog); this module collects the rest, each from
the place the Host already reads it, never a second derivation:

  skill       the platform's skills (skill_loader.list_skills, the folder the
              `context` tool reads as `skills/<name>`) and the Host's
              own active skills (host_skills.list_skills, read as
              `skills/<name>`);
  app         the installed apps (applications.registry.list_active, what
              open_app and describe_app look up) and the App Store catalog
              (tools._exec_list_app_catalog, the list_app_catalog tool itself);
  capability  the generated capability cards the `capabilities` tool reads
              (tools._caps_load_cards), except the op and app cards: every
              tool and every app is already an entry of its own;
  turn_type   this Host's active turn types (turn_types.active_types);
  harness     the coding harnesses `build`'s `harness` argument accepts: the
              connected ones (review._installed_harnesses).

An entry is a dict:

  kind   one of the five above
  name   its key, kind-prefixed so it never collides with a tool name
         ("skill:desktop", "app:notes", "harness:codex")
  title  its own name ("desktop")
  line   one line: what it is
  words  more text to search on (a skill's tools, a card's phrasings);
         searched, never shown
  steps  [{via, args, verb}], best first: the call that uses it. `verb` is
         read | operate | direct (a build door called by name)
  next   the first step written out, e.g.
         read(tool='context', args={"action":"read","path":"skills/desktop"})
  detail what find_tools(name=...) adds when asked for the entry in full

The catalog (DoorCatalog.add_discovery) keeps an entry only when one of its
steps is a call this turn may make (the door admits the tool, the turn type
holds it, the validated gate did not withhold it) and writes `next` from
that step. An entry is never itself callable: read and operate refuse its
name.

Each source fails on its own: one that raises gives [] and a log line, and
the others still arrive.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

log = logging.getLogger("cseries.mastercode.discovery_index")

KINDS = ("skill", "app", "capability", "turn_type", "harness")

# A step whose content is the entry itself: find_tools(name=...) returns it
# whole, so no other call is needed and every turn may take it.
FIND = "find"

# A line is one line: what find_tools shows per hit.
_LINE_MAX = 160
# Search text per entry, so one long card cannot outweigh the rest.
_WORDS_MAX = 600

# The App Store catalog is a bridge round trip. It changes when the store
# publishes, not per turn: kept this long per Host, and a turn waits at
# most _STORE_WAIT_S for it (the entries then come without the store).
_STORE_TTL_S = 600.0
_STORE_WAIT_S = 3.0
_store_cache: dict[int, tuple[float, list[dict]]] = {}

# Files read from disk, kept until they change on disk.
_file_cache: dict[str, tuple[tuple, object]] = {}


def _one_line(text, limit: int = _LINE_MAX) -> str:
    s = " ".join(str(text or "").split())
    if len(s) <= limit:
        return s
    cut = s[:limit].rsplit(" ", 1)[0]
    return (cut or s[:limit]).rstrip(" ,;:-") + "…"


def _words(*parts) -> str:
    return _one_line(" ".join(str(p) for p in parts if p), _WORDS_MAX)


def render_step(step: dict) -> str:
    """One step as the call the Host writes."""
    via = str(step.get("via") or "")
    args = step.get("args") or {}
    verb = step.get("verb")
    if verb == FIND:
        return f"find_tools(name='{via}')"
    a = json.dumps(args, separators=(",", ":"), ensure_ascii=False)
    if verb == "read":
        return f"read(tool='{via}', args={a})"
    if verb == "operate":
        return f"operate(intent, tool='{via}', args={a})"
    inner = ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in args.items())
    return f"{via}({inner})"


def _entry(kind: str, title: str, line: str, steps: list[dict], *, words: str = "",
           detail: dict | None = None) -> dict:
    return {"kind": kind, "name": f"{kind}:{title}", "title": title,
            "line": _one_line(line), "words": words, "steps": steps,
            "next": render_step(steps[0]) if steps else None,
            "detail": detail or {}}


# ── skills ──────────────────────────────────────────────────────────────────
def _platform_skills() -> list[dict]:
    from mastercode.runtime.skill_loader import get_location, list_skills
    root = get_location()
    if not root.is_dir():
        return []
    stamp = tuple(sorted((f.name, f.stat().st_mtime_ns) for f in root.iterdir()
                         if f.is_file() and f.suffix == ".md"))
    hit = _file_cache.get("skills")
    if hit and hit[0] == stamp:
        return list(hit[1])
    out = []
    for s in list_skills():
        path = f"skills/{s.name}"
        out.append(_entry(
            "skill", s.name, s.description or f"The platform's {s.name} skill.",
            [{"via": "context", "args": {"action": "read", "path": path}, "verb": "read"}],
            words=_words(s.description, " ".join(s.allowed_tools)),
            detail={"skill": s.name, "path": path, "author": "platform",
                    "tools": s.allowed_tools}))
    _file_cache["skills"] = (stamp, out)
    return list(out)


async def _host_skills(pool, entity_id) -> list[dict]:
    from mastercode.runtime import host_skills as hs
    out = []
    for d in await hs.list_skills(pool, entity_id, include_retired=False):
        name = str(d.get("name") or "")
        if not name:
            continue
        path = f"{hs.SKILLS_PREFIX}{name}"
        out.append(_entry(
            "skill", name, d.get("description") or f"Your own {name} skill.",
            [{"via": "context", "args": {"action": "read", "path": path}, "verb": "read"}],
            words=_words(d.get("description"), " ".join(d.get("allowed_tools") or [])),
            # `untrusted`: the version was written in a turn that read
            # untrusted content (host_skills, migrations/v232). Whoever puts
            # this entry into a turn marks it (turn_taint.mark_rows reads the
            # key here): find_tools (tool_doors.DoorCatalog.render) and
            # recall's skills store; the per-turn offer leaves it out.
            detail={"skill": name, "path": path, "author": d.get("author"),
                    "version": d.get("version"), "untrusted": d.get("untrusted")}))
    return out


async def _skills(pool, entity_id, *, failed: list[str] | None = None) -> list[dict]:
    """The Host's own skills, then the platform's; a name the Host wrote
    itself shadows the platform's (a context read of `skills/<name>` reads the
    Host's row first). A source that cannot be read is left out and, when
    `failed` is given, named in it ("own: <reason>", "platform: <reason>"):
    recall's skills store (memory_retrieval) says which half it could not
    search instead of showing a shorter list as if it were whole."""
    own: list[dict] = []
    try:
        own = await _host_skills(pool, entity_id)
    except Exception as e:  # noqa: BLE001 — one source, not the index
        log.warning("discovery: the Host's own skills unavailable: %s", e)
        if failed is not None:
            failed.append(f"own: {e}")
    seen = {e["title"] for e in own}
    plat: list[dict] = []
    try:
        plat = [e for e in _platform_skills() if e["title"] not in seen]
    except Exception as e:  # noqa: BLE001
        log.warning("discovery: platform skills unavailable: %s", e)
        if failed is not None:
            failed.append(f"platform: {e}")
    return own + plat


# ── apps ────────────────────────────────────────────────────────────────────
def _app_description(app) -> str:
    src = getattr(app, "source_dir", None)
    if not src:
        return ""
    path = Path(src) / "app.yaml"
    try:
        stamp = (str(path), path.stat().st_mtime_ns)
    except OSError:
        return ""
    key = f"app:{app.id}"
    hit = _file_cache.get(key)
    if hit and hit[0] == stamp:
        return str(hit[1])
    from server.applications import manifest as _manifest
    desc = str(_manifest.load(path).description or "")
    _file_cache[key] = (stamp, desc)
    return desc


async def _store_catalog(entity_id) -> list[dict]:
    """The list_app_catalog tool's own result: store apps joined with this
    Host's install state."""
    eid = int(entity_id or 0)
    hit = _store_cache.get(eid)
    if hit and time.monotonic() - hit[0] < _STORE_TTL_S:
        return hit[1]
    from mastercode.runtime.tools import _exec_list_app_catalog
    res = await asyncio.wait_for(_exec_list_app_catalog({}), timeout=_STORE_WAIT_S)
    if not isinstance(res, dict) or res.get("status") != "ok":
        raise RuntimeError(f"list_app_catalog: {(res or {}).get('result')}")
    apps = list(((res.get("result") or {}).get("apps")) or [])
    _store_cache[eid] = (time.monotonic(), apps)
    return apps


async def _apps(pool, entity_id) -> list[dict]:
    out: dict[str, dict] = {}
    try:
        from server.applications import registry
        for a in await registry.list_active(pool):
            try:
                desc = _app_description(a)
            except Exception as e:  # noqa: BLE001 — the line only
                log.debug("discovery: app %s manifest unreadable: %s", a.id, e)
                desc = ""
            args = {"app_id": a.id}
            out[a.id] = _entry(
                "app", a.id, f"{a.name} (installed): {desc}" if desc else f"{a.name} (installed).",
                [{"via": "open_app", "args": args, "verb": "operate"},
                 {"via": "describe_app", "args": args, "verb": "read"}],
                words=_words(a.name, desc),
                detail={"app_id": a.id, "installed": True, "version": a.version,
                        "provenance": a.provenance,
                        "describe": render_step({"via": "describe_app", "args": args,
                                                 "verb": "read"})})
    except Exception as e:  # noqa: BLE001
        log.warning("discovery: installed apps unavailable: %s", e)
    try:
        for s in await _store_catalog(entity_id):
            aid = str(s.get("app_id") or "")
            if not aid or aid in out:
                continue
            name = s.get("name") or aid
            desc = s.get("description") or ""
            price = s.get("price_micros")
            cost = f", ${int(price) / 1_000_000:.2f}" if price else ""
            args = {"app_id": aid}
            out[aid] = _entry(
                "app", aid,
                f"{name} (App Store, not installed{cost})" + (f": {desc}" if desc else "."),
                [{"via": "install_app", "args": args, "verb": "operate"}],
                words=_words(name, desc),
                detail={"app_id": aid, "installed": False,
                        "latest_version": s.get("latest_version"),
                        "price_micros": price, "provenance": s.get("provenance")})
    except Exception as e:  # noqa: BLE001
        log.warning("discovery: App Store catalog unavailable: %s", e)
    return list(out.values())


# ── capability cards ────────────────────────────────────────────────────────
# Every tool and every app is an entry of its own already.
_CARD_KINDS_SKIPPED = frozenset({"op", "app"})


def _capability_cards() -> list[dict]:
    from mastercode.runtime.tools import _CAPS_DOC_DIR, _caps_load_cards
    path = _CAPS_DOC_DIR / "capabilities.json"
    try:
        stamp = (str(path), path.stat().st_mtime_ns)
    except OSError:
        return []
    hit = _file_cache.get("cards")
    if hit and hit[0] == stamp:
        return list(hit[1])
    cards, _shape, _degraded = _caps_load_cards(path)
    out = []
    for c in cards:
        if str(c.get("kind") or "") in _CARD_KINDS_SKIPPED or not c.get("id"):
            continue
        cid = str(c["id"])
        title = str(c.get("title") or cid)
        what = str(c.get("what") or "")
        out.append(_entry(
            "capability", cid, f"{title}: {what}" if what else title,
            [{"via": "capabilities", "args": {"op": "card", "id": cid}, "verb": "read"}],
            words=_words(" ".join(str(w) for w in (c.get("when") or [])),
                         " ".join(str(k) for k in (c.get("keywords") or []))),
            detail={"card": cid, "card_kind": c.get("kind"), "url": c.get("url")}))
    _file_cache["cards"] = (stamp, out)
    return list(out)


MACHINE_RUNTIME = "machine-runtime"


def _machine_runtime() -> list[dict]:
    """What this machine can run and how delivery is proven
    (build_contract.host_runtime_brief, the text that rode every Circles and
    Workbench prompt until 2026-10-11) as one capability entry whose detail
    is the whole text."""
    from server.applications.build_contract import host_runtime_brief
    text = str(host_runtime_brief() or "").strip()
    if not text:
        return []
    body = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    key = f"capability:{MACHINE_RUNTIME}"
    return [_entry(
        "capability", MACHINE_RUNTIME,
        "What this machine can run, and how delivery is proven: "
        + (body[0].split(". ")[0].rstrip(".") + "." if body else ""),
        [{"via": key, "args": {}, "verb": FIND}],
        words=_words("machine runtime run app process static helper external server "
                     "pod delivery proof deploy", text),
        detail={"text": text})]


# ── turn types ──────────────────────────────────────────────────────────────
async def _turn_types(pool, entity_id) -> list[dict]:
    from mastercode.runtime.turn_types import active_types
    out = []
    for t in await active_types(pool, entity_id):
        tid, name = t.get("id"), str(t.get("name") or "")
        if not tid or not name:
            continue
        groups = list(t.get("tool_groups") or [])
        line = f"Turn type {name}: tool groups {', '.join(groups) or 'none'}"
        if t.get("skill"):
            line += f"; skill {t['skill']}"
        out.append(_entry(
            "turn_type", name, line,
            [{"via": "turn_type", "args": {"op": "show", "type_id": tid}, "verb": "read"}],
            words=_words(" ".join(t.get("keywords") or [])),
            detail={"type_id": tid, "tool_groups": groups, "skill": t.get("skill")}))
    return out


# ── harnesses ───────────────────────────────────────────────────────────────
async def _harnesses(interaction_context) -> list[dict]:
    """The connected coding harnesses, on a surface whose tool list holds
    `build` (tools.host_tool_allowed_on, the same test the list uses)."""
    from mastercode.runtime.tools import host_tool_allowed_on
    if not host_tool_allowed_on("build", interaction_context):
        return []
    from server.routes.review import _installed_harnesses
    out = []
    for h in await _installed_harnesses():
        args = {"action": "run", "harness": h, "project_id": "<project id>",
                "prompt": "<the task>"}
        out.append(_entry(
            "harness", h, f"Coding harness {h}, connected: build and compare runs can use it.",
            [{"via": "build", "args": args, "verb": "direct"}],
            words=_words("coding engine build run compare", h.replace("-", " ")),
            detail={"harness": h, "skill": f"skills/{h}"}))
    return out


# ── the one entry point ─────────────────────────────────────────────────────
async def _safe(label: str, fn) -> list[dict]:
    try:
        res = fn()
        if asyncio.iscoroutine(res):
            res = await res
        return list(res or [])
    except Exception as e:  # noqa: BLE001 — one source never blanks the rest
        log.warning("discovery: %s unavailable: %s", label, e)
        return []


async def discovery_entries(pool, entity_id, *, interaction_context=None,
                            turn_type: dict | None = None) -> list[dict]:
    """Every entry find_tools may find besides tools, for this Host on this
    surface. A closed turn type holds nothing, so it gets nothing. Which of
    them a turn may use is DoorCatalog.add_discovery's decision (the door, the
    turn type, the validated gate)."""
    if turn_type is not None and turn_type.get("status") == "closed":
        return []
    parts = await asyncio.gather(
        _safe("skills", lambda: _skills(pool, entity_id)),
        _safe("apps", lambda: _apps(pool, entity_id)),
        _safe("capability cards", _capability_cards),
        _safe("machine runtime", _machine_runtime),
        _safe("turn types", lambda: _turn_types(pool, entity_id)),
        _safe("harnesses", lambda: _harnesses(interaction_context)),
    )
    return [e for p in parts for e in p]
