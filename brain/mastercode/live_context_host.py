"""mastercode.live_context_host — live context cards the Host builds for the
person.

The Host builds a card the way it builds an op: a file in a project module with
target 'live_context', `<module>/<card id>.yaml`, written with workspace_file
(no new public tool — the wire cap has no room for one). The file is an ordinary
card (server.applications.live_context_spec.normalize_card_spec) plus one key, `app:`, naming
the installed app whose declared data it reads — the Host builds live context
for any app the person uses; it never reaches past that app's declared tables.

On the write, the platform (not the model):
  1. validates the card against that app's installed manifest,
  2. renders a preview against the real rows,
  3. files a card in the person's Tickets — what the card reads, the words that
     bring it into a turn, and exactly what it shows right now.
The card goes live on the person's one click on that preview. It reads data the person already owns, so the click narrows or
widens nothing and asks for no step-up; it is the person seeing what will ride
in every matching prompt before it does. Deleting the file retires the card —
a tighten, applied at once.

A change to a live card is a new draft. The live version keeps serving until
the person answers, so a pending edit never takes a working card down.
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import PurePosixPath

import yaml

log = logging.getLogger("cseries.mastercode.live_context_host")

PROPOSAL_KIND = "live_context_proposal"   # = server/routes/tickets.LIVE_CONTEXT_PROPOSAL_KIND
PAYLOAD_KEY = "live_context_proposal"
OPT_LIVE = "live"
OPT_NOT_NOW = "not_now"
MODULE_TARGET = "live_context"
PREVIEW_MAX = 1200
FILE_MAX = 16 * 1024


def _sha(spec: dict) -> str:
    return hashlib.sha256(json.dumps(spec, sort_keys=True, default=str).encode()).hexdigest()


async def _is_card_module(pool, project_id: int, module: str) -> bool:
    try:
        return bool(await pool.fetchval(
            "SELECT 1 FROM project_modules WHERE project_id = $1 AND name = $2 AND target = $3",
            int(project_id), module, MODULE_TARGET))
    except Exception:  # noqa: BLE001 — a pod before v188 has no such target
        return False


async def _app_tables(pool, app_id: str) -> tuple[dict[str, set[str]] | None, set[str]]:
    """(declared tables of an ACTIVE installed app or None when it is not
    installed, table names more than one installed app declares). A card may
    only read an app the person actually has, and never a table another app
    also claims — names are global in the pod database."""
    from mastercode.live_context import installed_apps_data, shared_tables
    apps = await installed_apps_data(pool)
    app = apps.get(app_id)
    return (app["tables"] if app else None), shared_tables(apps)


def _split(path: str) -> tuple[str, str] | None:
    p = PurePosixPath(str(path or "").strip().lstrip("/"))
    if len(p.parts) != 2 or not p.parts[1].endswith(".yaml"):
        return None
    return p.parts[0], p.parts[1][: -len(".yaml")]


async def validate_file(pool, raw_text: str, *, card_id_from_name: str) -> tuple[dict | None, str, list[str]]:
    """(spec, app_id, errors) for one card file's text."""
    from server.applications.live_context_spec import normalize_card_spec
    if len(raw_text.encode()) > FILE_MAX:
        return None, "", [f"the card file is over {FILE_MAX} bytes"]
    try:
        raw = yaml.safe_load(raw_text)
    except yaml.YAMLError as e:
        return None, "", [f"not valid YAML: {str(e)[:160]}"]
    if not isinstance(raw, dict):
        return None, "", ["a card file is a mapping"]
    app_id = str(raw.pop("app", "") or "").strip()
    if not app_id:
        return None, "", ["app: the installed app whose declared data this card reads"]
    tables, shared = await _app_tables(pool, app_id)
    if tables is None:
        return None, app_id, [f"app: {app_id!r} is not installed on this Host"]
    spec, errs = normalize_card_spec(raw, tables=tables, app_id=app_id)
    if spec and spec["reads"]["table"] in shared:
        errs = errs + [f"reads.table: {spec['reads']['table']!r} is declared by more than one "
                       f"installed app, so a card cannot tell whose rows it reads"]
        spec = None
    if spec and spec["id"] != card_id_from_name:
        errs = errs + [f"id {spec['id']!r} must match the file name {card_id_from_name!r}.yaml"]
        spec = None
    return spec, app_id, errs


async def render_preview(pool, spec: dict, app_id: str) -> str:
    from mastercode.live_context import read_declared, render_declared
    try:
        rows, count = await read_declared(pool, spec["reads"], app_id=app_id)
        text = "\n".join(render_declared(spec, rows, count)) or "(no rows yet)"
    except Exception as e:  # noqa: BLE001 — the preview says the read failed
        text = f"(the read failed: {str(e)[:160]})"
    return text[:PREVIEW_MAX]


def _sitrep(spec: dict, app_id: str, preview: str, replacing: bool) -> str:
    r = spec["reads"]
    head = ("Your Host changed a live context card" if replacing
            else "Your Host built a live context card")
    return (f"{head}: “{spec['title']}”.\n"
            f"It reads {app_id}'s {r['table']} ({', '.join(r['columns'])}) and is added to your "
            f"Host's prompt, freshly read, when you mention: {', '.join(spec['keywords'][:12])}.\n"
            + ("Until you answer, the current version keeps serving.\n" if replacing else "")
            + f"Right now it shows:\n{preview}")


def _options() -> list[dict]:
    return [
        {"id": OPT_LIVE, "label": "Make it live", "recommend": True,
         "consequence": "It rides in every matching prompt from now on, read fresh each time."},
        {"id": OPT_NOT_NOW, "label": "Not now",
         "consequence": "Nothing changes; the card stays a draft."},
    ]


async def sync_path(pool, entity_id: int, project_id: int, path: str, *,
                    deleted: bool = False) -> dict | None:
    """React to a Host write or delete inside a project. None when the path is
    not in a live_context module; otherwise what happened, for the tool result."""
    sp = _split(path)
    module = PurePosixPath(str(path or "").lstrip("/")).parts[0] if path else ""
    if not module or not await _is_card_module(pool, project_id, module):
        return None
    if sp is None:
        return {"ok": False, "errors": [f"a card is one file: {module}/<card id>.yaml"]}
    _mod, card_id = sp
    row = await pool.fetchrow(
        "SELECT id, status, live_sha, draft_sha, draft_ticket_id FROM live_context_cards "
        "WHERE entity_id = $1 AND card_id = $2", int(entity_id), card_id)
    if deleted:
        if row is None or row["status"] == "retired":
            return {"ok": True, "retired": False, "note": "no such card was live"}
        await pool.execute(
            "UPDATE live_context_cards SET status = 'retired', retired_at = now(), updated_at = now(), "
            "draft_spec = NULL, draft_sha = NULL, draft_ticket_id = NULL WHERE id = $1", row["id"])
        return {"ok": True, "retired": True, "card": card_id,
                "note": "retired — it no longer rides in any prompt"}
    from server.routes.project_modules import module_dir
    try:
        text = (module_dir(int(project_id), module) / f"{card_id}.yaml").read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "errors": [f"cannot read the card file: {str(e)[:120]}"]}
    spec, app_id, errs = await validate_file(pool, text, card_id_from_name=card_id)
    if errs:
        return {"ok": False, "card": card_id, "errors": errs}
    sha = _sha(spec)
    if row is not None and row["status"] == "live" and row["live_sha"] == sha:
        return {"ok": True, "card": card_id, "note": "unchanged — already live"}
    if row is not None and row["draft_sha"] == sha and row["draft_ticket_id"]:
        open_ = await pool.fetchval(
            "SELECT 1 FROM entity_tickets WHERE id = $1 AND status = 'open'", row["draft_ticket_id"])
        if open_:
            return {"ok": True, "card": card_id, "ticket_id": row["draft_ticket_id"],
                    "note": "this version's preview is already waiting for the person"}
    preview = await render_preview(pool, spec, app_id)
    replacing = row is not None and row["status"] == "live"
    from server.routes import tickets as _tk
    t = await _tk.create_ticket_service(
        pool, entity_id, source="runtime", sitrep=_sitrep(spec, app_id, preview, replacing),
        options=_options(), context={"reason": f"live context card {card_id}"}, priority=55,
        metadata={"kind": PROPOSAL_KIND, "user_only": True, "no_pattern": True,
                  PAYLOAD_KEY: {"card_id": card_id, "draft_sha": sha}})
    tid = int(t.get("id") or t.get("ticket_id"))
    # The preview this one replaces is closed: answering it would apply
    # nothing (the compare-and-set below), and an open card that does nothing
    # is noise in the person's Tickets.
    if row is not None and row["draft_ticket_id"] and row["draft_ticket_id"] != tid:
        try:
            _closed = await pool.execute(
                "UPDATE entity_tickets SET status = 'dismissed', "
                "metadata = COALESCE(metadata, '{}'::jsonb) || jsonb_build_object("
                "'superseded_by_ticket', $2::int) WHERE id = $1 AND status = 'open'",
                row["draft_ticket_id"], tid)
            if str(_closed).endswith(" 1"):
                from server import host_events
                await host_events.emit(pool, entity_id, "ticket_decided",
                                       {"ticket_id": row["draft_ticket_id"], "status": "dismissed"})
        except Exception as e:  # noqa: BLE001
            log.debug("live context: superseded preview %s not closed: %s", row["draft_ticket_id"], e)
    await pool.execute(
        "INSERT INTO live_context_cards (entity_id, card_id, app_id, project_id, module, status, "
        "draft_spec, draft_sha, draft_ticket_id) "
        "VALUES ($1, $2, $3, $4, $5, 'draft', $6::jsonb, $7, $8) "
        "ON CONFLICT (entity_id, card_id) DO UPDATE SET app_id = EXCLUDED.app_id, "
        "project_id = EXCLUDED.project_id, module = EXCLUDED.module, "
        "status = CASE WHEN live_context_cards.status = 'live' THEN 'live' ELSE 'draft' END, "
        "draft_spec = EXCLUDED.draft_spec, draft_sha = EXCLUDED.draft_sha, "
        "draft_ticket_id = EXCLUDED.draft_ticket_id, retired_at = NULL, updated_at = now()",
        int(entity_id), card_id, app_id, int(project_id), module, json.dumps(spec), sha, tid)
    return {"ok": True, "card": card_id, "ticket_id": tid, "preview": preview,
            "note": ("The person makes it live with one click on the card in Circles (Waiting on you); "
                     "until then it does nothing" + (" and the live version keeps serving."
                                                      if replacing else "."))}


async def apply_proposal(pool, entity_id: int, metadata: dict, *, option_id: str,
                         ticket_id: int) -> dict:
    """The person's answer. Compare-and-set on the draft the card showed: a card
    changed again after this preview applies nothing — the person approved what
    they saw. Never raises; the decide already stands."""
    p = (metadata or {}).get(PAYLOAD_KEY) or {}
    card_id, sha = str(p.get("card_id") or ""), str(p.get("draft_sha") or "")
    try:
        if option_id != OPT_LIVE:
            await pool.execute(
                "UPDATE live_context_cards SET draft_spec = NULL, draft_sha = NULL, "
                "draft_ticket_id = NULL, updated_at = now(), "
                "status = CASE WHEN status = 'draft' THEN 'retired' ELSE status END "
                "WHERE entity_id = $1 AND card_id = $2 AND draft_sha = $3",
                int(entity_id), card_id, sha)
            return {"applied": False, "reason": "not now"}
        r = await pool.fetchrow(
            "UPDATE live_context_cards SET live_spec = draft_spec, live_sha = draft_sha, "
            "status = 'live', approved_at = now(), approved_ticket_id = $4, "
            "draft_spec = NULL, draft_sha = NULL, draft_ticket_id = NULL, updated_at = now() "
            "WHERE entity_id = $1 AND card_id = $2 AND draft_sha = $3 RETURNING id",
            int(entity_id), card_id, sha, int(ticket_id))
        if r is None:
            return {"applied": False,
                    "reason": "the card changed after this preview; the newer version has its own card"}
        return {"applied": True, "card": card_id}
    except Exception as e:  # noqa: BLE001
        log.warning("live context: apply failed (card=%s): %s", card_id, e)
        return {"applied": False, "reason": str(e)[:200]}


async def live_cards(pool, entity_id: int) -> list[dict]:
    """[{card_id, app_id, spec}] for every live card."""
    try:
        rows = await pool.fetch(
            "SELECT card_id, app_id, live_spec FROM live_context_cards "
            "WHERE entity_id = $1 AND status = 'live' AND live_spec IS NOT NULL", int(entity_id))
    except Exception:  # noqa: BLE001 — pre-v188 pod
        return []
    out = []
    for r in rows:
        spec = r["live_spec"]
        spec = json.loads(spec) if isinstance(spec, str) else spec
        if isinstance(spec, dict):
            out.append({"card_id": r["card_id"], "app_id": r["app_id"], "spec": spec})
    return out
