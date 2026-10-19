"""runtime/host_skills.py — the skills a Host writes for itself, versioned in
its own Postgres (`host_skills`, migrations/v207).

A Host writes a skill
with the tool it already has for its own files,
`context(action='write', path='skills/<name>')` (runtime/context_ops.py); a
person edits one in the Skills app (server/routes/skills.py). Both come here:

  write    validate against the standard (runtime/skill_standard.py), then
           store a NEW version and supersede the active one. Returns the
           version and a unified diff against the version before it.
  retire   the active version stops being active; every version is kept.
           Refused while an active turn type names the skill: the type
           changes first, and that takes the person's approval.
  restore  writes a new version carrying an old version's text — validated
           again, because the standard (or the Host's registry) may have moved.
  history  every version, newest first.

One row per version, the supersede-not-overwrite shape memory_curate uses for
rules. A partial unique index allows one active version per name, and a write
takes a transaction-scoped advisory lock on (entity, name) so two writers of
the same skill queue instead of colliding.

Every SQL statement here also runs behind context_ops' SandboxedPool (bits
1|2|4): `host_skills` is on its write allowlist (sandbox/sql_proxy.py), and no
read carries a write keyword, so the wall is the same one every other context
op runs behind.

WHERE A HOST SKILL IS USED: as a turn type's skill
(turn_type_approval.normalize_spec, session.py through load_type_skill) and in
list_skills (tools.py). Host skills are looked up before the platform's disk
defaults. The validator refuses a write under a platform skill's name, so a
Host skill cannot take one; but a LATER image can add a platform skill whose
name an active Host skill already holds, and then the Host's wins those two
lookups (a turn type, and a `context` read of skills/<name>) until it is
retired or renamed. `default-skills/<name>` always reads the platform's.

Before a turn type's Host skill rides, its active version is checked against
the standard as it is NOW (ride_check): a version that breaks a rule added or
widened since it was written does not ride. Whenever a typed turn's skill
does not ride — missing, retired, failing the check, unreadable — the turn
carries one line saying which skill and why (type_skill_line) instead of
running silently without it.

A SKILL WRITTEN IN A MARKED TURN (migrations/v232). A version the Host writes
while its turn is marked as having read untrusted content (runtime/turn_taint)
records what marked it in `untrusted`, the same words turn_taint.stamp writes
into a memory row's metadata. Every path that puts such a version's text or
its pointer into a later turn marks that turn the way a recalled marked memory
does (turn_taint.mark_rows): a turn type riding it (load_type_skill), a
context read, list or diff of it (runtime/context_ops), recall's skills store
and find_tools (discovery_index entries carry the mark), list_skills
(tools.py). The per-turn offer of the Host's own skills never offers a marked
one (memory_retrieval.turn_own_skills); recall still finds it, and marks the
turn. The person's edit in the Skills app is a version with no mark: they read
the text they saved, and saving the same text clears the mark the same way. A
restore carries the mark of the version it brings back.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import logging
import uuid
from pathlib import Path
from typing import Any

from mastercode.runtime import skill_standard as std

log = logging.getLogger("cseries.mastercode.host_skills")

SKILLS_PREFIX = "skills/"
DEFAULTS_PREFIX = "default-skills/"
AUTHORS = ("host", "person")
PLATFORM_REFUSAL = "platform skill; write your own under skills/<name>"

_COLS = ("id, entity_id, name, version, body, description, allowed_tools, mcp_server, "
         "sources, author, thread_id, session_id, edited_from, status, created_at, "
         "superseded_at, untrusted")


class HostSkillError(ValueError):
    """A request this store refuses (unknown skill, unknown version, a bad
    author). The caller's to fix, never a platform fault."""


# ── names and the three sets the standard checks against ─────────────────────

def name_from_path(path: Any, prefix: str = SKILLS_PREFIX) -> str | None:
    """`skills/<name>` or `skills/<name>.md` -> `<name>`; None for anything
    else (another prefix, or nothing after it)."""
    if not isinstance(path, str) or not path.startswith(prefix):
        return None
    rest = path[len(prefix):]
    if rest.endswith(".md"):
        rest = rest[:-3]
    return rest or None


def default_skills_dir() -> Path:
    """The image's platform skills, /app/default-skills (Dockerfile.linux
    COPY default-skills/ /app/default-skills/). This file is
    <root>/brain/mastercode/runtime/host_skills.py in the image and in a
    checkout alike, so the root is three levels up from its folder."""
    return Path(__file__).resolve().parents[3] / "default-skills"


def default_skill_names() -> frozenset[str]:
    """Every platform skill's name: the image's default-skills/ and whatever
    the seeded skills folder holds (entrypoint.sh and skill_seeding.py copy
    the defaults there; an app-gated one lands only at install). A Host skill
    may carry none of them."""
    from mastercode.runtime.skill_loader import get_location
    names: set[str] = set()
    for root in (default_skills_dir(), get_location()):
        try:
            for f in root.iterdir():
                if f.is_file() and f.suffix == ".md" and not f.name.startswith("."):
                    names.add(f.stem.lower())
        except OSError:
            continue
    return frozenset(names)


_REGISTRY_CACHE: dict[bool, frozenset[str]] = {}


def registry_tool_names(entity_id: int | None = None) -> frozenset[str]:
    """Every tool name this Host can be offered: the public registry, the bot
    and bridge schemas (the sources tools._find_tool_schema walks), the
    orchestration tools every Host turn carries (session.py
    _build_host_orchestration_tools: dispatch, operate, recall, …), and on
    the operator's Host the platform tools only it holds (tools.py OPERATOR_ENTITY_ID).
    Raises if a registry cannot be read: a skill is never checked against an
    empty registry."""
    from mastercode.runtime.tools import OPERATOR_ENTITY_ID
    operator = entity_id == OPERATOR_ENTITY_ID
    cached = _REGISTRY_CACHE.get(operator)
    if cached is not None:
        return cached
    from mastercode.public_tool_registry import OPERATOR_PLATFORM_TOOL_SCHEMAS, PUBLIC_TOOL_SCHEMAS
    from mastercode.runtime.session import _build_host_orchestration_tools
    from mastercode.runtime.triage import _BOT_TOOL_SCHEMAS, _BRIDGE_TOOL_SCHEMAS
    sources = [PUBLIC_TOOL_SCHEMAS, _BOT_TOOL_SCHEMAS, _BRIDGE_TOOL_SCHEMAS]
    if operator:
        sources.append(OPERATOR_PLATFORM_TOOL_SCHEMAS)
    names: set[str] = set()
    for src in sources:
        for schema in (src or {}).values():
            n = (schema.get("function") or {}).get("name") if isinstance(schema, dict) else None
            if n:
                names.add(n)
    for schema in _build_host_orchestration_tools():
        n = (schema.get("function") or {}).get("name")
        if n:
            names.add(n)
    out = frozenset(names)
    _REGISTRY_CACHE[operator] = out
    return out


async def active_mcp_servers(pool, entity_id: int) -> dict[str, frozenset[str]]:
    """The Host's ACTIVE MCP connections, each with the names of the tools
    its stored list holds (server/mcp_connections.active_servers, the
    mcp_connections table, migrations/v212). The standard refuses a skill
    whose `mcp-server` is not one of them, and an `allowed-tools` entry
    mcp:<server>/<tool> the server does not list. A server whose list has not
    been fetched yet has no tools, so its skill waits for the list."""
    from server.mcp_connections import active_servers
    return await active_servers(pool, entity_id)


def _mcp_key(mcp) -> tuple:
    """A hashable form of active_mcp_servers' answer, for ride_check's cache."""
    if isinstance(mcp, dict):
        return tuple(sorted((str(k), tuple(sorted(v or ()))) for k, v in mcp.items()))
    return tuple(sorted(mcp or ()))


def validate(entity_id: int, name: str, text: str, *, mcp_servers=frozenset()) -> std.Result:
    return std.validate(name, text, registry_tools=registry_tool_names(entity_id),
                        default_skill_names=default_skill_names(),
                        active_mcp_servers=mcp_servers)


# ── rows ─────────────────────────────────────────────────────────────────────

def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


def _row(r, *, with_body: bool = True) -> dict:
    d = dict(r)
    src = d.get("sources")
    if isinstance(src, str):
        try:
            src = json.loads(src)
        except ValueError:
            src = []
    d["sources"] = src or []
    d["allowed_tools"] = list(d.get("allowed_tools") or [])
    d["thread_id"] = str(d["thread_id"]) if d.get("thread_id") is not None else None
    d["created_at"] = _iso(d.get("created_at"))
    d["superseded_at"] = _iso(d.get("superseded_at"))
    d["size"] = len(d.get("body") or "")
    if not with_body:
        d.pop("body", None)
    return d


def _thread_uuid(thread_id) -> str | None:
    """A provenance pointer, never a reason to refuse a write."""
    if not thread_id:
        return None
    try:
        return str(uuid.UUID(str(thread_id)))
    except ValueError:
        return None


def unified_diff(old: str | None, new: str, *, name: str, old_v: int | None, new_v: int) -> str:
    return "".join(difflib.unified_diff(
        (old or "").splitlines(keepends=True), (new or "").splitlines(keepends=True),
        fromfile=f"skills/{name} v{old_v}" if old_v else "/dev/null",
        tofile=f"skills/{name} v{new_v}"))


# ── reads ────────────────────────────────────────────────────────────────────

async def get_active(pool, entity_id: int, name: str) -> dict | None:
    r = await pool.fetchrow(
        f"SELECT {_COLS} FROM host_skills WHERE entity_id = $1 AND name = $2 AND status = 'active'",
        int(entity_id), name)
    return _row(r) if r is not None else None


async def get_latest(pool, entity_id: int, name: str) -> dict | None:
    """The active version, or else the newest retired one."""
    r = await pool.fetchrow(
        f"SELECT {_COLS} FROM host_skills WHERE entity_id = $1 AND name = $2 "
        "ORDER BY (status = 'active') DESC, version DESC LIMIT 1",
        int(entity_id), name)
    return _row(r) if r is not None else None


async def get_version(pool, entity_id: int, name: str, version: int) -> dict | None:
    """One version with its diff against the version before it."""
    r = await pool.fetchrow(
        f"SELECT {_COLS} FROM host_skills WHERE entity_id = $1 AND name = $2 AND version = $3",
        int(entity_id), name, int(version))
    if r is None:
        return None
    d = _row(r)
    prev = await pool.fetchrow(
        "SELECT version, body FROM host_skills WHERE entity_id = $1 AND name = $2 AND version < $3 "
        "ORDER BY version DESC LIMIT 1", int(entity_id), name, int(version))
    d["previous_version"] = int(prev["version"]) if prev is not None else None
    d["diff"] = unified_diff(prev["body"] if prev is not None else None, d["body"], name=name,
                             old_v=d["previous_version"], new_v=d["version"])
    return d


async def history(pool, entity_id: int, name: str) -> list[dict]:
    rows = await pool.fetch(
        f"SELECT {_COLS} FROM host_skills WHERE entity_id = $1 AND name = $2 ORDER BY version DESC",
        int(entity_id), name)
    return [_row(r, with_body=False) for r in rows]


async def list_skills(pool, entity_id: int, *, include_retired: bool = True) -> list[dict]:
    """One entry per name: its active version, or its newest retired one
    (status 'retired' means the skill has no active version), with how many
    versions it has."""
    rows = await pool.fetch(
        f"SELECT DISTINCT ON (name) {_COLS}, count(*) OVER (PARTITION BY name) AS versions "
        "FROM host_skills WHERE entity_id = $1 "
        "ORDER BY name, (status = 'active') DESC, version DESC",
        int(entity_id))
    out = [_row(r, with_body=False) for r in rows]
    return out if include_retired else [d for d in out if d["status"] == "active"]


# ── writes ───────────────────────────────────────────────────────────────────

def _write_mark(author: str, carried: str | None) -> str | None:
    """What the version a write stores records in `untrusted`: the mark of
    the text it carries (`carried`, a restore's old version), and for the
    Host what marked the turn it is written in (turn_taint.stamp: None in a
    clean turn, and when no turn is running here). The person's own edit
    records none: they read what they saved."""
    if author == "person":
        return carried or None
    from mastercode.runtime import turn_taint
    return carried or turn_taint.stamp({}).get(turn_taint.UNTRUSTED_KEY) or None


async def write(pool, entity_id: int, name: str, text: str, *, author: str,
                thread_id=None, session_id: str | None = None,
                edited_from: int | None = None, result: std.Result | None = None,
                carried: str | None = None) -> dict:
    """Validate, then store `text` as the next version of `name` and
    supersede the active one. {"status": "refused", "failures", "message"}
    when the standard refuses it; nothing is written then. Writing the active
    version's exact text again writes nothing ({"unchanged": True}), except
    the person's save of a version that carries a mark (`untrusted`): that
    is a new version with none, the person having read it (_write_mark).

    The answer carries `untrusted` (the new version's mark, or None) and
    `previous_untrusted` (the mark of the version the diff is against): a
    caller that hands the diff to a turn hands that version's lines too."""
    if author not in AUTHORS:
        raise HostSkillError(f"author must be one of {', '.join(AUTHORS)}")
    mark = _write_mark(author, carried)
    if result is None:
        result = validate(entity_id, name, text,
                          mcp_servers=await active_mcp_servers(pool, entity_id))
    if not result.ok:
        return {"status": "refused", "name": name, "rules": result.rules_broken,
                "failures": [f.as_dict() for f in result.failures], "message": result.refusal()}
    eid = int(entity_id)
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Two writers of one skill queue here rather than both reading the
            # same latest version.
            await conn.fetchval("SELECT pg_advisory_xact_lock($1, hashtext($2))", eid, name)
            prev = await conn.fetchrow(
                "SELECT id, version, body, status, untrusted FROM host_skills WHERE entity_id = $1 "
                "AND name = $2 ORDER BY version DESC LIMIT 1", eid, name)
            active = await conn.fetchrow(
                "SELECT id, version, body, untrusted FROM host_skills WHERE entity_id = $1 AND name = $2 "
                "AND status = 'active'", eid, name)
            clears = author == "person" and mark is None and active is not None and bool(active["untrusted"])
            if active is not None and active["body"] == text and not clears:
                return {"status": "ok", "name": name, "version": int(active["version"]),
                        "unchanged": True, "diff": "", "untrusted": active["untrusted"],
                        "previous_untrusted": None}
            new_v = (int(prev["version"]) if prev is not None else 0) + 1
            if active is not None:
                await conn.execute(
                    "UPDATE host_skills SET status = 'retired', superseded_at = now() WHERE id = $1",
                    active["id"])
            src_from = edited_from if edited_from is not None else (
                int(prev["version"]) if prev is not None else None)
            await conn.fetchrow(
                "INSERT INTO host_skills (entity_id, name, version, body, description, allowed_tools, "
                "mcp_server, sources, author, thread_id, session_id, edited_from, status, untrusted) "
                "VALUES ($1, $2, $3, $4, $5, $6::text[], $7, $8::jsonb, $9, $10::uuid, $11, $12, 'active', "
                "$13) RETURNING id",
                eid, name, new_v, text, result.description, list(result.allowed_tools),
                result.mcp_server, json.dumps(result.sources), author, _thread_uuid(thread_id),
                (str(session_id)[:200] if session_id else None), src_from,
                (str(mark)[:200] if mark else None))
    prev_v = int(prev["version"]) if prev is not None else None
    return {"status": "ok", "name": name, "version": new_v, "previous_version": prev_v,
            "superseded": int(active["version"]) if active is not None else None,
            "author": author, "edited_from": src_from,
            "untrusted": (str(mark)[:200] if mark else None),
            "previous_untrusted": (prev["untrusted"] if prev is not None else None),
            "diff": unified_diff(prev["body"] if prev is not None else None, text, name=name,
                                 old_v=prev_v, new_v=new_v)}


async def active_types_naming(conn, entity_id: int, name: str) -> list[str]:
    """The ACTIVE turn types whose skill is `name`, by name. A type names its
    skill exactly as load_type_skill looks it up (turn_types.type_skill:
    the stored name, stripped), so an exact match is the same lookup."""
    rows = await conn.fetch(
        "SELECT name FROM turn_types WHERE entity_id = $1 AND status = 'active' AND skill = $2 "
        "ORDER BY lower(name)", int(entity_id), name)
    return [str(r["name"]) for r in rows]


def _in_use_message(name: str, types: list[str]) -> str:
    names = ", ".join(types)
    if len(types) == 1:
        return (f"skills/{name} was not retired: the turn type {names} uses it as its skill. "
                "Change that type's skill first; a turn type changes only with the person's approval.")
    return (f"skills/{name} was not retired: the turn types {names} use it as their skill. "
            "Change each type's skill first; a turn type changes only with the person's approval.")


async def retire(pool, entity_id: int, name: str) -> dict:
    """The active version stops being active. Its history stays; restore
    brings any version back. Takes write()'s lock: a retire that lands while
    a write is mid-transaction would otherwise re-check the row that write
    just retired, report "already retired", and leave the write's new version
    active (measured in test_host_skills_pg).

    Refused while an ACTIVE turn type names the skill: every turn of that
    type would then run without the skill the person approved it with. The
    refusal, {"status": "refused", "turn_types", "message"}, names the types;
    a type's skill changes through its own approval card (turn_type_approval
    request_change), never here. A type that comes to name a skill after it
    is retired (an approval decided later) is the ride-time line's case
    (load_type_skill)."""
    eid = int(entity_id)
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.fetchval("SELECT pg_advisory_xact_lock($1, hashtext($2))", eid, name)
            active_v = await conn.fetchval(
                "SELECT version FROM host_skills WHERE entity_id = $1 AND name = $2 AND status = 'active'",
                eid, name)
            types = await active_types_naming(conn, eid, name) if active_v is not None else []
            if types:
                return {"status": "refused", "name": name, "turn_types": types,
                        "active_version": int(active_v), "message": _in_use_message(name, types)}
            r = await conn.fetchrow(
                "UPDATE host_skills SET status = 'retired', superseded_at = now() "
                "WHERE entity_id = $1 AND name = $2 AND status = 'active' RETURNING version",
                eid, name)
    if r is None:
        latest = await get_latest(pool, entity_id, name)
        if latest is None:
            raise HostSkillError(f"no skill named skills/{name}")
        return {"status": "ok", "name": name, "retired": None, "already_retired": True,
                "latest_version": latest["version"]}
    return {"status": "ok", "name": name, "retired": int(r["version"])}


async def restore(pool, entity_id: int, name: str, version: int, *, author: str,
                  thread_id=None, session_id: str | None = None) -> dict:
    """A new version carrying version `version`'s text, validated again. It
    carries that version's mark (`untrusted`) too: a restore brings the old
    text back without anyone reading it."""
    old = await pool.fetchrow(
        "SELECT body, untrusted FROM host_skills WHERE entity_id = $1 AND name = $2 AND version = $3",
        int(entity_id), name, int(version))
    if old is None:
        raise HostSkillError(f"skills/{name} has no version {version}")
    out = await write(pool, entity_id, name, old["body"], author=author, thread_id=thread_id,
                      session_id=session_id, edited_from=int(version), carried=old["untrusted"])
    out["restored_from"] = int(version)
    return out


# ── where a skill rides a turn ───────────────────────────────────────────────

def ride_text(name: str, text: str) -> str:
    """What a Host skill puts on a turn: its core (everything before
    `## Reference`), which the standard keeps under the cut, and one line
    pointing at the reference when it has one."""
    from mastercode.runtime.skill_loader import _parse_frontmatter
    _meta, body = _parse_frontmatter(text or "")
    core, ref = std.split_reference(body)
    if ref:
        core += (f"\n\n(This skill's `## Reference` section is one read away: "
                 f"`context(action='read', path='skills/{name}')`.)")
    return core


# A stored version is checked again before it rides (load_type_skill), against
# every rule but this one. name-not-default refuses a WRITE under a platform
# skill's name; a later image that adds a platform skill a Host skill already
# holds changes nothing about the Host skill's text, and the module docstring
# above is the decision for that case: the Host's keeps both lookups until it
# is retired or renamed.
RIDE_EXEMPT_RULES = frozenset({"name-not-default"})
_RIDE_CHECKS: dict[tuple, tuple[str, ...]] = {}
_RIDE_CHECKS_MAX = 256


async def ride_check(pool, entity_id: int, name: str, row: dict) -> tuple[str, ...]:
    """The rules the stored version `row` breaks under the CURRENT standard,
    except RIDE_EXEMPT_RULES; () when it may ride. The standard may have moved
    since the version was written (a rule added or widened, a tool renamed or
    gone from the registry). A version is checked once per process for one
    registry and one set of MCP connections: the answer is a pure function of
    those and the text (skill_standard.validate), and a 64,000-character
    skill takes ~0.1 s to check. Raises if the check cannot run. The MCP
    connections are part of the key with their tool names, so a server whose
    list changed is checked again."""
    mcp = await active_mcp_servers(pool, entity_id)
    registry = registry_tool_names(entity_id)
    body = row.get("body") or ""
    key = (int(entity_id), name, int(row.get("version") or 0),
           hashlib.sha256(body.encode("utf-8", "surrogatepass")).hexdigest(), registry, _mcp_key(mcp))
    hit = _RIDE_CHECKS.get(key)
    if hit is not None:
        return hit
    res = std.validate(name, body, registry_tools=registry, default_skill_names=default_skill_names(),
                       active_mcp_servers=mcp)
    out = tuple(r for r in res.rules_broken if r not in RIDE_EXEMPT_RULES)
    if len(_RIDE_CHECKS) >= _RIDE_CHECKS_MAX:
        _RIDE_CHECKS.clear()
    _RIDE_CHECKS[key] = out
    return out


def _not_riding(name: str, why: str, fix: str, *, source=None, version=None,
                rules: tuple[str, ...] = ()) -> dict:
    return {"name": name, "body": "", "source": source, "version": version,
            "problem": why, "fix": fix, "rules": list(rules)}


async def load_type_skill(pool, entity_id: int, name: str) -> dict | None:
    """A turn type's skill: the Host's own active skill of that name first,
    then the platform's disk default (skill_loader.load_skill).

    None only for an empty name. Otherwise {"name", "body", "source":
    "host"|"platform"|None, "version", "problem"}. When the skill rides,
    `body` is what rides and `problem` is None. When it does not, `body` is
    empty and `problem` says why, for the ONE line the turn carries in its
    place (type_skill_line):
      - the Host's active version breaks the current standard
        (ride_check) — it does not ride, and the platform's skill of that
        name does not stand in for it: the type was approved naming this one;
      - no active version and no platform skill of that name, the newest
        Host version retired;
      - no skill of that name anywhere;
      - it could not be read or checked this turn.
    """
    if not name:
        return None
    read_err = None
    try:
        row = await get_active(pool, entity_id, name)
    except Exception as e:  # noqa: BLE001 - the disk default is still a skill
        log.warning("host skill %r could not be read for a turn type: %s", name, e)
        row, read_err = None, e
    write_it = f"context(action='write', path='skills/{name}')"
    if row is not None:
        try:
            broken = await ride_check(pool, entity_id, name, row)
        except Exception as e:  # noqa: BLE001 - unchecked never rides
            log.warning("host skill %r v%s could not be checked against the standard: %s",
                        name, row.get("version"), e)
            return _not_riding(name, f"skills/{name} v{row['version']} could not be checked against "
                               f"the skill standard this turn ({type(e).__name__})",
                               "It rides again once the check runs", source="host",
                               version=row["version"])
        if broken:
            return _not_riding(name, f"skills/{name} v{row['version']} breaks the current skill "
                               f"standard ({', '.join(broken)})",
                               f"Rewrite it to the standard with {write_it}", source="host",
                               version=row["version"], rules=broken)
        # A version written in a marked turn marks the turn it rides
        # (turn_taint.mark_rows), as a recalled marked memory does.
        from mastercode.runtime import turn_taint
        turn_taint.mark_rows([row], f"skills/{name}, the skill this turn's type rides with")
        return {"name": name, "body": ride_text(name, row["body"]), "source": "host",
                "version": row["version"], "problem": None, "untrusted": row.get("untrusted")}
    from mastercode.runtime.skill_loader import load_skill
    sk = load_skill(name)
    if sk is not None and sk.body:
        return {"name": name, "body": sk.body, "source": "platform", "version": None, "problem": None}
    if sk is not None:
        return _not_riding(name, f"the platform skill {name} has no text", "", source="platform")
    if read_err is not None:
        return _not_riding(name, f"skills/{name} could not be read this turn ({type(read_err).__name__})",
                           "It rides again once your database answers")
    change = "or ask the person to change this type's skill (turn_type op='change')"
    try:
        latest = await get_latest(pool, entity_id, name)
    except Exception as e:  # noqa: BLE001
        log.warning("host skill %r history could not be read for a turn type: %s", name, e)
        latest = None
    if latest is not None:
        return _not_riding(name, f"skills/{name} is retired (its newest version is v{latest['version']})",
                           f"Write it again to the standard with {write_it}, {change}",
                           source="host", version=latest["version"])
    return _not_riding(name, "no skill of yours or the platform's has that name",
                       f"Write it to the standard with {write_it}, {change}")


def type_skill_line(type_name: str, skill: str, loaded: dict | None = None,
                    error: BaseException | None = None) -> str:
    """The ONE line a typed turn carries when its type's skill does not ride:
    which skill, why, and what fixes it. Built from the type's own fields and
    the platform's words, never from the skill's text."""
    tname = " ".join(str(type_name or "").split()) or "(unnamed)"
    if error is not None:
        why, fix = f"it could not be loaded this turn ({type(error).__name__})", "It rides again once it loads"
    elif loaded is None:
        why, fix = "it could not be loaded this turn", "It rides again once it loads"
    else:
        why = loaded.get("problem") or "it has no text"
        fix = loaded.get("fix") or ""
    return (f"Turn type {tname}: its skill `{skill}` is not on this turn — {why}. "
            + (f"{fix}. " if fix else "") + "Until then, work this turn without it.")


__all__ = [
    "SKILLS_PREFIX", "DEFAULTS_PREFIX", "AUTHORS", "PLATFORM_REFUSAL", "HostSkillError",
    "name_from_path", "default_skills_dir", "default_skill_names", "registry_tool_names",
    "active_mcp_servers", "validate", "unified_diff", "get_active", "get_latest", "get_version",
    "history", "list_skills", "write", "retire", "restore", "ride_text", "load_type_skill",
    "active_types_naming", "ride_check", "RIDE_EXEMPT_RULES", "type_skill_line",
]
