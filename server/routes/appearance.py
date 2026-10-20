"""
Appearance preferences — wallpaper, desktop layout, and future UI customisations.

GET /api/appearance        — open, no token required. Dashboard reads on boot.
PUT /api/appearance        — requires write:appearance (settings app).
PUT /api/appearance/layout — shell-trusted, no token (see route docstring).
GET /api/appearance/desktop      — open; the HTML desktop (a pointed artifact's live render).
GET /api/appearance/desktop/meta — open; its change signal for the shell's live poll.
PUT /api/appearance/desktop      — requires write:appearance; point the desktop at an artifact.
POST /api/appearance/desktop/loop — shell origin (or write:appearance); the shell saw the worn desktop's frame reload itself.

Stored as a single row in entity_appearance (created at boot by
migrations/v142_band_b_appearance_and_embed.sql).
The wallpaper value is a named key the dashboard maps to a CSS background string;
desktop_layout is the JSONB the shell round-trips for dock pins + icon order;
desktop_artifact_id/desktop_entry point at the artifact that IS the desktop
when wallpaper='html'.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from pathlib import Path
from starlette.responses import FileResponse, Response

from server import brain_service
from server.applications import artifacts as artifact_service
from server.applications.artifacts import ArtifactError
from server.applications.desktop_contract import check_document, format_report
from server.applications.desktop_tokens import extract_tokens, tokens_css
from server.applications.middleware import requires_permission
from server.applications.route_policy import shell_origin_when_tokenless

log = logging.getLogger("privatae.pc.appearance")

router = APIRouter(prefix="/api/appearance", tags=["appearance"])

# entity_appearance is created by
# migrations/v142_band_b_appearance_and_embed.sql at boot, not from here.
# The columns this module reads — wallpaper, image_bytes,
# image_content_type, updated_at, desktop_layout, the _mobile trio, and the
# desktop_/network_ artifact pointers — all live there.

# The wallpaper routes take a variant: "desktop" (the default everywhere)
# or "mobile" (phone-mode override; unset = follow desktop).
_VARIANTS = ("desktop", "mobile")


def _require_variant(value: str | None) -> str:
    variant = str(value or "desktop")
    if variant not in _VARIANTS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"unknown variant {variant!r} (allowed: {list(_VARIANTS)})",
        )
    return variant

# desktop_layout shape (all keys optional; the shell applies defaults):
#   dock_pinned    — ordered app ids pinned to the dock
#   dock_offered   — app ids the shell has put in an arranged dock once
#                    (js/desktop-layout.js OFFER_ONCE); one taken out after
#                    that stays out
#   desktop_hidden — app ids removed from the desktop surface
#   desktop_order  — ordered app ids for desktop icon placement
#   widgets        — per-widget floating-card state, keyed by widget id:
#                    {x, y, w, h: number, minimized, hidden: bool}
#                    (js/widget-layer.js writes the whole map on every change)
#   walkthrough    — getting-started tour state: {done: bool, step: str,
#                    steps: {stepId: "passed"|"skipped"|"failed"|"seen"}}
#                    (the app writes the whole object on every transition)
_LAYOUT_KEYS = ("dock_pinned", "dock_offered", "desktop_hidden", "desktop_order")
_LAYOUT_MAX_IDS = 256          # far above any real app count — shape guard, not a user cap
_LAYOUT_MAX_ID_LEN = 64
_WIDGET_MAX_ENTRIES = 64       # shape guard, not a user cap
_WIDGET_NUM_KEYS = ("x", "y", "w", "h")
_WIDGET_BOOL_KEYS = ("minimized", "hidden")
_WIDGET_COORD_BOUND = 100_000  # sane pixel bound — rejects garbage, caps no screen
_WALKTHROUGH_MAX_STEPS = 64    # shape guard, not a user cap
_WALKTHROUGH_STEP_STATES = ("passed", "skipped", "failed", "seen")


def _clean_widgets(val) -> dict:
    """Widget-state map validation, same strictness as the id lists: known
    subkeys only (unknown subkeys are dropped, matching how unknown top-level
    layout keys are dropped), finite numbers for geometry, booleans for
    flags. Widget ids are shell-defined, not registry ids."""
    if not isinstance(val, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "widgets must be an object")
    if len(val) > _WIDGET_MAX_ENTRIES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"widgets exceeds {_WIDGET_MAX_ENTRIES} entries")
    cleaned: dict = {}
    for wid, ws in val.items():
        if not isinstance(wid, str) or not wid.strip() or len(wid) > _LAYOUT_MAX_ID_LEN:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "widget ids must be short non-empty strings")
        if not isinstance(ws, dict):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"widgets[{wid!r}] must be an object")
        entry: dict = {}
        for k in _WIDGET_NUM_KEYS:
            v = ws.get(k)
            if v is None:
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not abs(v) < _WIDGET_COORD_BOUND:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, f"widgets[{wid!r}].{k} must be a finite number")
            entry[k] = round(float(v), 1)
        for k in _WIDGET_BOOL_KEYS:
            v = ws.get(k)
            if v is None:
                continue
            if not isinstance(v, bool):
                raise HTTPException(status.HTTP_400_BAD_REQUEST, f"widgets[{wid!r}].{k} must be a boolean")
            entry[k] = v
        cleaned[wid.strip()] = entry
    return cleaned


def _clean_walkthrough(val) -> dict:
    """Getting-started tour state, same discipline as the other keys: the
    container must be an object within the size guard (else 400), unknown
    subkeys are dropped (as unknown top-level layout keys and unknown widget
    subkeys are), and non-conforming members are dropped rather than
    persisted — a malformed tour write must not corrupt the shell's layout."""
    if not isinstance(val, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "walkthrough must be an object")
    cleaned: dict = {}
    done = val.get("done")
    if isinstance(done, bool):
        cleaned["done"] = done
    step = val.get("step")
    if isinstance(step, str) and step.strip() and len(step) <= _LAYOUT_MAX_ID_LEN:
        cleaned["step"] = step.strip()
    steps = val.get("steps")
    if steps is not None:
        if not isinstance(steps, dict):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "walkthrough.steps must be an object")
        if len(steps) > _WALKTHROUGH_MAX_STEPS:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                f"walkthrough.steps exceeds {_WALKTHROUGH_MAX_STEPS} entries")
        cleaned_steps: dict = {}
        for sid, sstate in steps.items():
            if not isinstance(sid, str) or not sid.strip() or len(sid) > _LAYOUT_MAX_ID_LEN:
                continue
            if sstate not in _WALKTHROUGH_STEP_STATES:
                continue
            cleaned_steps[sid.strip()] = sstate
        cleaned["steps"] = cleaned_steps
    return cleaned


def _clean_layout(body: dict) -> dict:
    """Strict shape validation: known keys, lists of short strings. Ids are
    NOT checked against the app registry — a stale id from an uninstalled
    app is harmless (renderers skip unknown ids) and coupling writes to the
    registry would leave orphan-cleanup problems on every uninstall."""
    out: dict = {}
    for key in _LAYOUT_KEYS:
        val = body.get(key)
        if val is None:
            continue
        if not isinstance(val, list):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{key} must be a list")
        if len(val) > _LAYOUT_MAX_IDS:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{key} exceeds {_LAYOUT_MAX_IDS} entries")
        cleaned = []
        for item in val:
            if not isinstance(item, str) or not item.strip():
                raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{key} entries must be non-empty strings")
            if len(item) > _LAYOUT_MAX_ID_LEN:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{key} entry too long")
            cleaned.append(item.strip())
        out[key] = cleaned
    if body.get("widgets") is not None:
        out["widgets"] = _clean_widgets(body["widgets"])
    if body.get("walkthrough") is not None:
        out["walkthrough"] = _clean_walkthrough(body["walkthrough"])
    return out


def _merge_layout(stored: Optional[dict], layout: dict) -> dict:
    """A save merged into the stored layout, key by key, so a dock-only write
    cannot clobber the icon order.

    A dock written WITHOUT dock_offered comes from a desktop page that does not
    know about it: one loaded before the offer existed, still open across an
    update. It cannot tell an app it never knew about from one its owner took
    out, so any offered app that dock leaves out stops counting as offered,
    and the current desktop offers it once more. That page can cost a person
    one extra offer of an app they took out there, but never an app they did
    not take out: a stale dock list saved over a fresh one would otherwise
    drop Calendar for good."""
    merged = dict(stored or {})
    offered = merged.get("dock_offered")
    if "dock_pinned" in layout and "dock_offered" not in layout and isinstance(offered, list):
        merged["dock_offered"] = [i for i in offered if i in layout["dock_pinned"]]
    merged.update(layout)
    return merged


def _parse_layout(raw) -> Optional[dict]:
    """asyncpg hands JSONB back as text unless a codec is registered."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None

# Filesystem path is only kept for legacy migration on a first boot after
# the move to PG. Authoritative storage for custom wallpapers is the
# entity_appearance.image_bytes column in PG, which lives on the
# entity-data PVC and survives pod restarts.
CUSTOM_IMAGE_PATH = Path("/var/lib/cseries/custom/wallpaper")
ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp", "image/avif"}
MAX_SIZE_BYTES = 20 * 1024 * 1024  # 20 MB

WALLPAPERS = {
    "default":  None,           # keeps desktop.css definition — no override
    "ink":      "var(--cseries-ink)",
    "void":     "#050709",
    "midnight": "radial-gradient(circle 1200px at 50% -10%, rgba(30,50,110,0.45) 0%, transparent 65%), var(--cseries-ink)",
    "aurora":   "radial-gradient(circle 900px at 15% 115%, rgba(70,180,100,0.15) 0%, transparent 60%), radial-gradient(circle 900px at 85% -20%, rgba(70,100,210,0.15) 0%, transparent 60%), var(--cseries-ink)",
    "dusk":     "radial-gradient(circle 900px at 85% 115%, rgba(170,70,180,0.15) 0%, transparent 60%), var(--cseries-ink)",
    "ember":    "radial-gradient(circle 1000px at 50% 115%, rgba(200,100,50,0.14) 0%, transparent 60%), var(--cseries-ink)",
    "ice":      "radial-gradient(circle 900px at 50% -10%, rgba(60,150,220,0.2) 0%, transparent 60%), var(--cseries-ink)",
    "custom":   None,  # served via /api/appearance/image, applied as background-image
    "html":     None,  # the HTML desktop — a pointed artifact, served via /api/appearance/desktop
}

# ── HTML desktop ──────────────────────────────────────────────────────────
_DESKTOP_HTML_URL = "/api/appearance/desktop"
_ENTRY_MAX_LEN = 200


def _clean_entry(entry) -> str:
    """The artifact entry file the desktop renders — same jail rules as the
    artifact service's own paths (relative, no traversal)."""
    entry = str(entry or "index.html").strip() or "index.html"
    if len(entry) > _ENTRY_MAX_LEN or os.path.isabs(entry) or ".." in Path(entry).parts:
        raise ArtifactError(400, f"unsafe entry {entry!r}")
    return entry


async def _desktop_pointer(conn, eid: int, row) -> Optional[dict]:
    """The HTML-desktop pointer, verified against the projects row it names.
    A pointer at a deleted or converted artifact reads as no pointer."""
    if row is None or row["desktop_artifact_id"] is None:
        return None
    aid = row["desktop_artifact_id"]
    art = await conn.fetchrow(
        "SELECT updated_at FROM projects WHERE id=$1 AND entity_id=$2 AND kind='artifact'",
        aid, eid)
    if art is None:
        return None
    entry = row["desktop_entry"] or "index.html"
    # The shell remounts when this stamp moves (desktop-surface.js mount /
    # pollTick). What it shows is the PIN (server/applications/desktop_pin.py),
    # so the stamp is the pin's: an edit that has not passed the render check
    # yet — or never will — does not remount anything.
    from server.applications import desktop_pin
    stamp = desktop_pin.pin_stamp(int(aid), entry)
    return {
        "artifact_id": aid,
        "entry": entry,
        "updated_at": stamp or (art["updated_at"].isoformat() if art["updated_at"] else None),
    }


async def desktop_gates(pool, eid: int, artifact_id, entry="index.html", *,
                        with_html: bool = False) -> tuple:
    """Every gate the desktop applies, run WITHOUT setting anything. Returns
    (artifact_id, report).

    Factored out so the "can this be a desktop?" question and the act of making
    it one are literally the same code. They were not, and they could not be
    kept in step by discipline: the Artifacts window showed "Use as desktop" on
    every artifact and the only way to find out was to click it and read a 422.
    A button that offers an action the kernel will refuse is not a button, it is
    a quiz — and a *separate* re-implementation of the check to grey it out
    would drift from the real one the first time either moved.

    Raises ArtifactError for the things that are not floor failures but hard
    errors: a non-integer id, an artifact that is not this entity's renderable
    artifact, a named entry that does not exist.

    The browser RENDER check is not in here on purpose: it wakes a browser,
    and this also answers the Artifacts window's cheap "can this be a
    desktop?" read (GET /desktop/check). set_desktop_artifact runs it after
    these gates pass, on the same rendered bytes — `with_html=True` returns
    them as (artifact_id, html, report).
    """
    entry = _clean_entry(entry)
    try:
        aid = int(artifact_id)
    except (TypeError, ValueError):
        raise ArtifactError(400, "artifact_id must be an integer") from None
    # Ownership + kind='artifact' + a renderable workspace, in the one call the
    # shell will make next …
    html = await artifact_service.render(pool, eid, aid, entry)
    # … and the NAMED entry must exist: render_workspace falls back to any
    # index.html when the entry is missing, so it alone would "validate" a
    # typo and store it.
    from server.routes.applications import PROJECTS_ROOT
    if not (Path(PROJECTS_ROOT) / str(aid) / entry).is_file():
        raise ArtifactError(404, f"entry {entry!r} not found in artifact {aid}")
    # The floor. Checked against the rendered document (assets inlined, the
    # exact bytes the shell will mount) and the installed app set, so a
    # data-cseries-app naming an app this host does not have is refused here
    # rather than dropped silently at click time.
    report = check_document(html, installed_ids=await _installed_app_ids(pool))
    return (aid, html, report) if with_html else (aid, report)


async def set_desktop_artifact(pool, eid: int, artifact_id, entry="index.html", *,
                               render_check: bool | dict = True) -> dict:
    """Point the desktop at one of the host's artifacts (wallpaper → 'html'),
    or clear the pointer (artifact_id=None → the wallpaper goes back to
    'default' if it was 'html'). Shared by PUT /desktop and the `artifact`
    tool's op='set_desktop' so the invariant lives in ONE place: the row must
    be THIS entity's kind='artifact', it must render — artifact_service.render
    checks all three and raises ArtifactError otherwise — and the document must
    meet the desktop FLOOR (server/applications/desktop_contract.py): mode
    declared, the required tokens declared, real apps in desktop mode, no
    external assets, no invented app list. A document that fails is refused
    with the full report (422) so the host fixes the design, not the user
    (a desktop once mounted as a wallpaper with dead buttons and ten
    fictional apps because nothing checked).

    THE RENDER CHECK (server/applications/desktop_render.py) runs after the
    floor: the document is loaded in the real browser the way the shell
    mounts it, and one that throws, paints nothing, or does not draw the
    tiles / bar it declares is refused (422, the error and its line) — the
    same "the host fixes the design, not the user" rule, one layer deeper.

    When the browser cannot be asked, this door PROCEEDS and says so: the
    result carries `render: {checked: False, reason}` and a NOT RENDER-CHECKED
    warning. That follows the platform's standing rule for its real-browser
    check — not checked is neither a pass nor a failure (the delivery proof,
    mastercode/runtime/app_verify.py _run_verify; check_page) — and this is
    the user's own screen, where trying is undoable in one click
    (routes/designs.py /restore) and the wake can be refused for plan
    headroom alone. Blocking would stop someone wearing their own desktop
    because a browser was busy. Publishing, which puts a document in front
    of other people, refuses instead (designs.publish_document).

    `render_check`: True runs it; a dict is a result the caller already
    obtained for these exact bytes (a shelf install checks before it copies);
    False skips it — ONLY for putting back a desktop that was already on
    screen (designs._restore), which must always work."""
    if artifact_id is None:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO entity_appearance (entity_id, updated_at)
                VALUES ($1, NOW())
                ON CONFLICT (entity_id) DO UPDATE
                    SET desktop_artifact_id = NULL,
                        desktop_entry       = NULL,
                        wallpaper = CASE WHEN entity_appearance.wallpaper = 'html'
                                         THEN 'default' ELSE entity_appearance.wallpaper END,
                        updated_at = NOW()
                """,
                eid,
            )
            row = await conn.fetchrow(
                "SELECT wallpaper FROM entity_appearance WHERE entity_id = $1", eid)
        log.info("HTML desktop cleared for entity %d", eid)
        await _notify_shell()
        # Tell the account's Host Manager the tile is now the default wallpaper.
        # Clearing is as much a change as setting: without this the manager
        # would keep drawing the desktop this host just took down.
        await publish_desktop_snapshot(pool, reason="cleared")
        return {"wallpaper": row["wallpaper"] if row else "default",
                "desktop_artifact_id": None, "desktop_html_url": None}

    aid, html, report = await desktop_gates(pool, eid, artifact_id, entry, with_html=True)
    entry = _clean_entry(entry)
    if not report["ok"]:
        raise ArtifactError(422, format_report(report))
    from server.applications import desktop_render
    render = None
    if isinstance(render_check, dict):
        render = render_check
    elif render_check:
        render = await desktop_render.render_check(pool, html)
    if render is not None and render.get("checked") and not render.get("ok"):
        raise ArtifactError(422, desktop_render.format_render(render))
    warnings = list(report["warnings"])
    if render is not None and not render.get("checked"):
        warnings.append(desktop_render.format_render(render))
        log.warning("HTML desktop %d set WITHOUT a render check: %s", aid, render.get("reason"))
    elif render is not None:
        warnings += list(render.get("warnings") or [])
    # WEARING PINS WHAT WAS CHECKED (server/applications/desktop_pin.py): the
    # shell is served these bytes, and a later edit replaces them only by
    # passing the same check. Going back (render_check=False) keeps the pin
    # the desktop already had — that IS what was on screen.
    from server.applications import desktop_pin
    if render is not None:
        desktop_pin.write_pin(aid, entry, html, render, origin="set")
    elif desktop_pin.pinned_html(aid, entry) is None:
        # Going back to a desktop with no pin of its own (worn before pins
        # existed): worn NOT RENDER-CHECKED and checked at once — unless
        # these exact bytes already failed, which are refused.
        await desktop_pin.wear_unchecked(pool, eid, aid, entry, html, origin="restore")
    # THE PHONE FOLLOWS: a desktop put on is worn on a
    # phone too, so the phone variant goes back to "Same as desktop" (NULL).
    # A Host once wore a design on its desktop while its phone kept an
    # uploaded picture, so the design never reached the phone. A phone
    # picture chosen in Settings afterwards still wins; the uploaded bytes
    # are kept, so choosing it again needs no new upload. Go back in Designs
    # puts the phone choice back too (designs._restore).
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO entity_appearance
                (entity_id, wallpaper, desktop_artifact_id, desktop_entry, updated_at)
            VALUES ($1, 'html', $2, $3, NOW())
            ON CONFLICT (entity_id) DO UPDATE
                SET wallpaper           = 'html',
                    wallpaper_mobile    = NULL,
                    desktop_artifact_id = EXCLUDED.desktop_artifact_id,
                    desktop_entry       = EXCLUDED.desktop_entry,
                    updated_at          = NOW()
            """,
            eid, aid, entry,
        )
    log.info("HTML desktop set to artifact %d (%s) for entity %d", aid, entry, eid)
    await _notify_shell()
    # Publish the new desktop as this host's Host Manager tile. Fail-soft by
    # construction (publish_desktop_snapshot never raises), so a thumbnail that
    # cannot be filed can never fail the design the user just approved.
    await publish_desktop_snapshot(pool, reason="set")
    # What the Host Manager tile had to leave out to fit the platform's tile
    # limit (or that there is no tile) is said here, where the person set it.
    from server.applications.shell_snapshot import LAST_TILE
    if LAST_TILE.get("note"):
        warnings = [*warnings, LAST_TILE["note"]]
    out = {"wallpaper": "html", "desktop_artifact_id": aid, "desktop_entry": entry,
           "desktop_html_url": _DESKTOP_HTML_URL, "mode": report["mode"],
           "tokens": len(report["tokens"]), "warnings": warnings}
    if render is not None:
        out["render"] = desktop_render.summary(render)
    return out


async def _installed_app_ids(pool) -> frozenset[str]:
    """Every app id this host can launch — what a desktop document's
    data-cseries-app values are checked against. The registry is per-pod (one
    entity per database), so there is no entity column to filter on.
    Best-effort: an unreadable registry yields an empty set, which SKIPS the
    id check rather than refusing every desktop."""
    try:
        rows = await pool.fetch(
            "SELECT id FROM installed_applications WHERE status = 'active'")
        return frozenset(str(r["id"]) for r in rows if r["id"])
    except Exception as e:  # noqa: BLE001 — the floor must not depend on this read
        log.debug("installed app ids unavailable for the desktop floor: %s", e)
        return frozenset()


async def desktop_contract_report(pool, eid: int, artifact_id, entry="index.html") -> Optional[dict]:
    """The floor's verdict for one artifact WITHOUT mounting it — the `artifact`
    tool attaches this to every write on a desktop-shaped document so the host
    sees 'REFUSED: …' in the same turn it wrote the file. None when the entry
    does not render at all (the write result already says so)."""
    try:
        html = await artifact_service.render(pool, eid, int(artifact_id), _clean_entry(entry))
    except (ArtifactError, TypeError, ValueError):
        return None
    return check_document(html, installed_ids=await _installed_app_ids(pool))


async def _notify_shell() -> None:
    """Tell every open desktop tab to re-read appearance NOW. Without this the
    first set_desktop reaches an open tab only on its next boot / tab switch /
    Settings save — the shell's live poll starts only once a document is
    mounted. Same push lane the open_app and
    close_app tools use (server/routes/mcp.py, ws-client.js). Lazy import:
    server.main imports this module at load."""
    try:
        from server.main import broadcast
        await broadcast({"type": "appearance_changed", "module": "appearance", "data": {}})
    except Exception as e:  # never let a push failure fail the write
        log.debug("appearance_changed broadcast skipped: %s", e)


async def _require_pointer(conn, eid: int) -> None:
    """'html' shows the pointed artifact; with no pointer there is nothing to
    show, and silently landing on the default wallpaper would read as a
    broken choice — for either variant."""
    row = await conn.fetchrow(
        "SELECT desktop_artifact_id, desktop_entry FROM entity_appearance WHERE entity_id = $1",
        eid)
    if await _desktop_pointer(conn, eid, row) is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "no HTML desktop to show yet — ask your host to design one")


def _custom_image_path() -> Path:
    """Return the path to the stored custom wallpaper (any extension)."""
    d = CUSTOM_IMAGE_PATH.parent
    for ext in (".jpg", ".png", ".webp", ".avif"):
        p = d / ("wallpaper" + ext)
        if p.exists():
            return p
    return CUSTOM_IMAGE_PATH.with_suffix(".jpg")  # fallback write target


@router.get("")
async def get_appearance(response: Response) -> dict:
    """Return the entity's appearance preferences. No auth — the dashboard
    reads this on every boot before any app window is open."""
    # Must revalidate every read: a phone that heuristically caches this
    # JSON keeps showing a wallpaper that was changed from another device.
    response.headers["Cache-Control"] = "no-cache"
    pool = brain_service.get_pool()
    if pool is None:
        return {"wallpaper": "default", "css": None}

    eid = brain_service._entity_id

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT wallpaper,
                   image_bytes IS NOT NULL AS has_image_pg,
                   wallpaper_mobile,
                   image_bytes_mobile IS NOT NULL AS has_image_mobile,
                   desktop_layout,
                   desktop_artifact_id,
                   desktop_entry
            FROM entity_appearance
            WHERE entity_id = $1
            """,
            eid,
        )
        desk = await _desktop_pointer(conn, eid, row)

    wallpaper = row["wallpaper"] if row else "default"
    # Prefer PG-backed storage; fall back to legacy filesystem so existing
    # entities still render through one boot after the move to PG.
    has_custom = bool(row and row["has_image_pg"]) or _custom_image_path().exists()
    # Mobile variant: NULL = no override, the shell uses the desktop wallpaper.
    wallpaper_mobile = row["wallpaper_mobile"] if row else None
    has_custom_mobile = bool(row and row["has_image_mobile"])
    return {
        "wallpaper": wallpaper,
        "css": WALLPAPERS.get(wallpaper) if wallpaper != "custom" else None,
        "image_url": "/api/appearance/image" if wallpaper == "custom" and has_custom else None,
        "has_custom_image": has_custom,
        "wallpaper_mobile": wallpaper_mobile,
        "css_mobile": (
            WALLPAPERS.get(wallpaper_mobile)
            if wallpaper_mobile and wallpaper_mobile != "custom" else None
        ),
        "image_url_mobile": (
            "/api/appearance/image?variant=mobile"
            if wallpaper_mobile == "custom" and has_custom_mobile else None
        ),
        "has_custom_image_mobile": has_custom_mobile,
        "available": list(WALLPAPERS.keys()),
        "desktop_layout": _parse_layout(row["desktop_layout"]) if row else None,
        # HTML desktop: the shell mounts desktop_html_url when the effective
        # wallpaper key is 'html'; Settings offers the "Host design" swatch
        # whenever a pointer exists, whatever the current key.
        "has_desktop_html": desk is not None,
        "desktop_html_url": _DESKTOP_HTML_URL if desk else None,
        "desktop_artifact_id": desk["artifact_id"] if desk else None,
        "desktop_entry": desk["entry"] if desk else None,
        "desktop_updated_at": desk["updated_at"] if desk else None,
        # Design tokens the live desktop document declares (server-parsed,
        # the same pairs /css/tokens.css carries). {} = stock tokens.
        "tokens": await current_design_tokens(pool),
    }


@router.put(
    "",
    dependencies=[Depends(requires_permission("write:appearance"))],
)
async def set_appearance(request: Request) -> dict:
    """Persist the entity's appearance preferences. Requires the settings
    app's write:appearance permission."""
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "brain pool down")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be a JSON object")
    # Choosing "Host design" is a WEAR (desktop_pin.wear_current): the
    # artifact's current render goes through the browser render check first,
    # so a person's click is answered inside the router's window or with 202 +
    # a job, like every other desktop door (desktop_render.run_door).
    if body.get("wallpaper") == "html":
        from server.applications import desktop_render
        return await desktop_render.run_door("worn", lambda: _apply_appearance(pool, body))
    return await _apply_appearance(pool, body)


async def _apply_appearance(pool, body: dict) -> dict:
    variant = _require_variant(body.get("variant"))

    eid = brain_service._entity_id
    worn = None
    if body.get("wallpaper") == "html":
        # Checked BEFORE the wallpaper key is written: a desktop with no
        # version that ever rendered is refused (422) and nothing changes.
        desk = await _current_pointer(pool)
        if desk is None:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                "no HTML desktop to show yet — ask your host to design one")
        from server.applications import desktop_pin
        try:
            worn = await desktop_pin.wear_current(pool, eid, int(desk["artifact_id"]),
                                                  desk["entry"])
        except ArtifactError as e:
            raise HTTPException(e.status, e.detail) from None

    if variant == "mobile":
        # JSON null clears the override — phone mode follows the desktop
        # wallpaper again.
        raw_wp = body.get("wallpaper")
        wallpaper_mobile = None if raw_wp is None else str(raw_wp)
        if wallpaper_mobile is not None and wallpaper_mobile not in WALLPAPERS:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"unknown wallpaper {wallpaper_mobile!r}. Available: {sorted(WALLPAPERS)}",
            )
        async with pool.acquire() as conn:
            if wallpaper_mobile == "html":
                await _require_pointer(conn, eid)
            await conn.execute(
                """
                INSERT INTO entity_appearance (entity_id, wallpaper_mobile, updated_at)
                VALUES ($1, $2, NOW())
                ON CONFLICT (entity_id) DO UPDATE
                    SET wallpaper_mobile = EXCLUDED.wallpaper_mobile, updated_at = NOW()
                """,
                eid, wallpaper_mobile,
            )
        log.info("mobile wallpaper set to %r for entity %d", wallpaper_mobile, eid)
        out = {
            "wallpaper_mobile": wallpaper_mobile,
            "css": WALLPAPERS.get(wallpaper_mobile) if wallpaper_mobile else None,
        }
        if worn is not None:
            out["desktop"] = worn
            await _notify_shell()
        return out

    wallpaper = str(body.get("wallpaper") or "default")
    if wallpaper not in WALLPAPERS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"unknown wallpaper {wallpaper!r}. Available: {sorted(WALLPAPERS)}",
        )

    async with pool.acquire() as conn:
        if wallpaper == "html":
            await _require_pointer(conn, eid)
        await conn.execute(
            """
            INSERT INTO entity_appearance (entity_id, wallpaper, updated_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (entity_id) DO UPDATE
                SET wallpaper = EXCLUDED.wallpaper, updated_at = NOW()
            """,
            eid, wallpaper,
        )

    log.info("wallpaper set to %r for entity %d", wallpaper, eid)
    out = {"wallpaper": wallpaper, "css": WALLPAPERS[wallpaper]}
    if worn is not None:
        out["desktop"] = worn
        await _notify_shell()
    return out


@router.put(
    "/layout",
    dependencies=[Depends(shell_origin_when_tokenless(
        "saving the desktop layout", "write:appearance"))],
)
async def set_desktop_layout(request: Request) -> dict:
    """Persist the desktop layout (dock pins, icon order/visibility).

    Shell-trusted, NO app token: the writer is the desktop shell itself —
    a drag ends, the shell saves. The shell is the first-party surface and
    holds no app token (see ApplicationTokenMiddleware's allow-list note;
    /api/appearance is an allow-listed shell prefix). The payload is pure
    per-entity cosmetic state, strictly shape-validated above, and touches
    nothing but its own JSONB column — the same trust class as the /ws
    messages the shell already sends.

    Two callers, two checks (route_policy.shell_origin_when_tokenless): a
    token-less call must come from the desktop's own page, and a call WITH a
    token needs write:appearance. The Settings app's Desktop section writes
    through the shell's own DesktopLayout (window.parent), so it arrives as the
    shell; the Getting Started walkthrough calls with its own token and
    declares write:appearance. Once neither was checked, so any
    app — an isolated community app too — could rewrite the dock, the icon
    order and the walkthrough state.
    """
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "brain pool down")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be a JSON object")

    layout = _clean_layout(body)

    eid = brain_service._entity_id

    # Merge key-by-key so a dock-only write can't clobber the icon order —
    # the shell's dock and desktop save independently.
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT desktop_layout FROM entity_appearance WHERE entity_id = $1", eid,
        )
        merged = _merge_layout(_parse_layout(row["desktop_layout"]) if row else None, layout)
        await conn.execute(
            """
            INSERT INTO entity_appearance (entity_id, desktop_layout, updated_at)
            VALUES ($1, $2::jsonb, NOW())
            ON CONFLICT (entity_id) DO UPDATE
                SET desktop_layout = EXCLUDED.desktop_layout, updated_at = NOW()
            """,
            eid, json.dumps(merged),
        )

    log.info("desktop layout saved for entity %d: %s", eid,
             {k: len(v) for k, v in merged.items()})
    return {"desktop_layout": merged}


@router.post(
    "/upload",
    dependencies=[Depends(requires_permission("write:appearance"))],
)
async def upload_wallpaper(request: Request) -> dict:
    """Accept a raw image body (JPEG / PNG / WebP / AVIF) and save it as
    the entity's custom wallpaper. Max 20 MB.

    Client sends:
        POST /api/appearance/upload[?variant=mobile]
        Content-Type: image/jpeg   (or image/png / image/webp / image/avif)
        [raw image bytes]

    Saving also activates it (wallpaper[_mobile] = 'custom') — the settings
    app's upload slot is one click, not upload-then-apply.
    """
    variant = _require_variant(request.query_params.get("variant"))
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type not in ALLOWED_TYPES:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            f"unsupported image type {content_type!r}. Allowed: {sorted(ALLOWED_TYPES)}",
        )

    data = await request.body()
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "empty body")
    if len(data) > MAX_SIZE_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"image too large ({len(data) // 1024 // 1024} MB, max 20 MB)",
        )

    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "PG pool unavailable")
    return await store_wallpaper_image(pool, brain_service._entity_id, variant, data, content_type)


async def store_wallpaper_image(pool, eid: int, variant: str, data: bytes, content_type: str) -> dict:
    """Save `data` as the entity's custom wallpaper and wear it
    (wallpaper[_mobile] = 'custom'). The ONE writer of the custom image, for
    the Settings upload and the Host's artifact(op='set_wallpaper').

    PG-backed storage: write bytes + content_type into entity_appearance.
    Survives pod restart (PG lives on the entity-data PVC) and is backed
    up alongside the rest of entity state. No filesystem dependency."""
    async with pool.acquire() as conn:
        if variant == "mobile":
            await conn.execute(
                """
                INSERT INTO entity_appearance
                    (entity_id, wallpaper_mobile, image_bytes_mobile,
                     image_content_type_mobile, updated_at)
                VALUES ($1, 'custom', $2, $3, NOW())
                ON CONFLICT (entity_id) DO UPDATE
                    SET wallpaper_mobile          = 'custom',
                        image_bytes_mobile        = EXCLUDED.image_bytes_mobile,
                        image_content_type_mobile = EXCLUDED.image_content_type_mobile,
                        updated_at                = NOW()
                """,
                eid, data, content_type,
            )
        else:
            await conn.execute(
                """
                INSERT INTO entity_appearance
                    (entity_id, wallpaper, image_bytes, image_content_type, updated_at)
                VALUES ($1, 'custom', $2, $3, NOW())
                ON CONFLICT (entity_id) DO UPDATE
                    SET wallpaper          = 'custom',
                        image_bytes        = EXCLUDED.image_bytes,
                        image_content_type = EXCLUDED.image_content_type,
                        updated_at         = NOW()
                """,
                eid, data, content_type,
            )

    log.info("custom wallpaper saved to PG: entity=%d, variant=%s, %d bytes, type=%s",
             eid, variant, len(data), content_type)

    image_url = ("/api/appearance/image?variant=mobile"
                 if variant == "mobile" else "/api/appearance/image")
    return {
        "wallpaper": "custom",
        "variant": variant,
        "image_url": image_url,
        "size_bytes": len(data),
        "content_type": content_type,
    }


_OWN_IMAGE_URL = re.compile(r"/api/images/(gimg_[A-Za-z0-9_-]{22})")


async def set_wallpaper_from_image(pool, eid: int, image_url, variant: str = "desktop") -> dict:
    """artifact(op='set_wallpaper', image_url='/api/images/<id>'): wear one of
    the Host's own images (generated_images: what image_generate made, what an
    app such as Blender Engine rendered) as the custom wallpaper, through the
    same writer as the Settings upload, and tell every open desktop now.
    Wearing an image takes off an HTML desktop that was worn
    (wallpaper = 'custom'); set_desktop puts it back. The person can change it
    in Settings, Appearance, like any wallpaper."""
    if str(variant or "desktop") not in _VARIANTS:
        raise ArtifactError(400, f"variant is one of {', '.join(_VARIANTS)}")
    m = _OWN_IMAGE_URL.fullmatch(str(image_url or "").strip())
    if not m:
        raise ArtifactError(400, "image_url is one of your images by its /api/images/... name "
                                 "(image_generate(action='gallery') lists them)")
    row = await pool.fetchrow(
        "SELECT content_type, bytes FROM generated_images WHERE id = $1 AND entity_id = $2",
        m.group(1), int(eid))
    if row is None:
        raise ArtifactError(404, f"{image_url} is not one of your images")
    ctype, data = row["content_type"], bytes(row["bytes"])
    if ctype not in ALLOWED_TYPES:
        raise ArtifactError(415, f"a wallpaper is one of {', '.join(sorted(ALLOWED_TYPES))}; this "
                                 f"image is {ctype}. Export it as JPEG, PNG or WebP first")
    if len(data) > MAX_SIZE_BYTES:
        raise ArtifactError(413, f"the image is {len(data) // 1024 // 1024} MB; a wallpaper is at "
                                 f"most {MAX_SIZE_BYTES // 1024 // 1024} MB")
    res = await store_wallpaper_image(pool, int(eid), str(variant or "desktop"), data, ctype)
    await _notify_shell()
    return {**res, "source": f"/api/images/{m.group(1)}"}


@router.get("/image")
async def serve_custom_image(request: Request) -> Response:
    """Serve the entity's custom wallpaper image from PG (?variant=mobile
    for the phone-mode image). No auth — the dashboard needs to load it as
    a CSS background-image without a token.

    Legacy fallback (desktop variant only — mobile never had a filesystem
    era): if there's no row in PG but a file exists at the historical
    CUSTOM_IMAGE_PATH, serve that. Lets existing pods serve their wallpaper
    through one boot after the move to PG; once the user re-uploads,
    the row appears in PG and the filesystem is no longer consulted.
    """
    variant = _require_variant(request.query_params.get("variant"))
    pool = brain_service.get_pool()
    if pool is not None:
        if variant == "mobile":
            query = """
                SELECT image_bytes_mobile AS image_bytes,
                       image_content_type_mobile AS image_content_type
                FROM entity_appearance
                WHERE entity_id = $1 AND image_bytes_mobile IS NOT NULL
                """
        else:
            query = """
                SELECT image_bytes, image_content_type
                FROM entity_appearance
                WHERE entity_id = $1 AND image_bytes IS NOT NULL
                """
        async with pool.acquire() as conn:
            row = await conn.fetchrow(query, brain_service._entity_id)
        if row is not None:
            return Response(
                content=bytes(row["image_bytes"]),
                media_type=row["image_content_type"] or "image/jpeg",
                headers={"Cache-Control": "no-cache"},
            )
    if variant == "mobile":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no mobile wallpaper uploaded")

    # Legacy filesystem fallback (only useful during one-boot upgrade window).
    p = _custom_image_path()
    if p.exists():
        mime = {"jpg": "image/jpeg", "png": "image/png",
                "webp": "image/webp", "avif": "image/avif"}.get(p.suffix.lstrip("."), "image/jpeg")
        return FileResponse(str(p), media_type=mime, headers={"Cache-Control": "no-cache"})

    raise HTTPException(status.HTTP_404_NOT_FOUND, "no custom wallpaper uploaded")


# ── HTML desktop routes ──────────────────────────────────────────────────────


async def _current_pointer(pool) -> Optional[dict]:
    eid = brain_service._entity_id
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT desktop_artifact_id, desktop_entry FROM entity_appearance WHERE entity_id = $1",
            eid)
        return await _desktop_pointer(conn, eid, row)


# ── The host-authored NETWORK MAP ───────────────────────────────────────────
#
# The Network app draws its map from ONE graph and hands it to a registered
# renderer. Two renderers ship (logical, physical); this is the third door —
# the host writes its own HTML and the app renders it in a sandboxed frame,
# fed the SAME graph over a bridge. The desktop is the pattern this follows
# rather than a second mechanism.

_NETWORK_HTML_URL = "/api/appearance/network"


async def _network_pointer_row(conn, eid: int, row) -> Optional[dict]:
    """The map pointer, verified against the projects row it names. A pointer
    at a deleted or converted artifact reads as no pointer — the app falls back
    to its built-in renderers rather than showing an empty frame."""
    if row is None or row["network_artifact_id"] is None:
        return None
    aid = row["network_artifact_id"]
    art = await conn.fetchrow(
        "SELECT updated_at FROM projects WHERE id=$1 AND entity_id=$2 AND kind='artifact'",
        aid, eid)
    if art is None:
        return None
    return {
        "artifact_id": aid,
        "entry": row["network_entry"] or "index.html",
        "updated_at": art["updated_at"].isoformat() if art["updated_at"] else None,
    }


async def _current_network_pointer(pool) -> Optional[dict]:
    eid = brain_service._entity_id
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT network_artifact_id, network_entry FROM entity_appearance WHERE entity_id = $1",
            eid)
        return await _network_pointer_row(conn, eid, row)


async def set_network_artifact(pool, eid: int, artifact_id, entry="index.html") -> dict:
    """Point the Network app's map at one of the host's artifacts, or clear it
    (artifact_id=None → the app goes back to its built-in views).

    Same three gates as the desktop, for the same reason: the row must be THIS
    entity's kind='artifact', the named entry must exist, and the RENDERED
    document must meet the map floor (desktop_contract.check_network_document)
    — whose load-bearing rule is that the nodes come from the bridge. A map of
    your own network is where an invented picture does the most harm, so a
    document that types its own node list is refused with the report rather
    than mounted."""
    if artifact_id is None:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO entity_appearance (entity_id, updated_at)
                VALUES ($1, NOW())
                ON CONFLICT (entity_id) DO UPDATE
                    SET network_artifact_id = NULL,
                        network_entry       = NULL,
                        updated_at          = NOW()
                """,
                eid,
            )
        log.info("host-authored network map cleared for entity %d", eid)
        return {"network_artifact_id": None, "network_html_url": None}

    entry = _clean_entry(entry)
    try:
        aid = int(artifact_id)
    except (TypeError, ValueError):
        raise ArtifactError(400, "artifact_id must be an integer") from None
    html = await artifact_service.render(pool, eid, aid, entry)
    from server.routes.applications import PROJECTS_ROOT
    if not (Path(PROJECTS_ROOT) / str(aid) / entry).is_file():
        raise ArtifactError(404, f"entry {entry!r} not found in artifact {aid}")
    from server.applications.desktop_contract import check_network_document
    report = check_network_document(html)
    if not report["ok"]:
        raise ArtifactError(422, format_report(report))
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO entity_appearance
                (entity_id, network_artifact_id, network_entry, updated_at)
            VALUES ($1, $2, $3, NOW())
            ON CONFLICT (entity_id) DO UPDATE
                SET network_artifact_id = EXCLUDED.network_artifact_id,
                    network_entry       = EXCLUDED.network_entry,
                    updated_at          = NOW()
            """,
            eid, aid, entry,
        )
    log.info("network map set to artifact %d (%s) for entity %d", aid, entry, eid)
    return {"network_artifact_id": aid, "network_entry": entry,
            "network_html_url": _NETWORK_HTML_URL, "warnings": report["warnings"]}


# ── Design tokens (the desktop document recolours the shell + every app) ──
#
# A design starts at the desktop: the desktop artifact may
# declare `<meta name="cseries-tokens" content="--cseries-ink:#…; …">` in its
# static head, and those pairs are appended to /css/tokens.css — the one file
# the shell and every system app load — so the taskbar, windows, dock and
# app chrome follow the desktop. Only while the effective wallpaper IS the
# HTML desktop: pick any other swatch and the stock tokens come back, exactly
# as the pointer survives but the document stops rendering.


async def current_design_tokens(pool) -> dict:
    """The accepted token pairs of the LIVE desktop document, or {} — no
    pointer, another wallpaper selected, an unrenderable entry, or a document
    that declares nothing all read the same: stock tokens."""
    eid = brain_service._entity_id
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT wallpaper, wallpaper_mobile, desktop_artifact_id, desktop_entry "
            "FROM entity_appearance WHERE entity_id = $1", eid)
        if row is None or "html" not in (row["wallpaper"], row["wallpaper_mobile"]):
            return {}
        desk = await _desktop_pointer(conn, eid, row)
    if desk is None:
        return {}
    from server.applications import desktop_pin
    try:
        # The tokens of what is WORN (the pin), so the shell's colours never
        # run ahead of a desktop edit that has not passed the render check.
        html = await desktop_pin.worn_html(pool, eid, int(desk["artifact_id"]), desk["entry"])
    except ArtifactError:
        return {}
    return extract_tokens(html)


async def design_tokens_css() -> str:
    """The override block main.py appends to /css/tokens.css ('' when there is
    nothing to append). Never raises — a token read must not take the shell's
    stylesheet down."""
    pool = brain_service.get_pool()
    if pool is None:
        return ""
    try:
        return tokens_css(await current_design_tokens(pool))
    except Exception as e:  # noqa: BLE001 — stylesheet must always serve
        log.debug("design tokens unavailable: %s", e)
        return ""


def base_tokens_css(overlay: bool = True) -> Optional[str]:
    """The base /css/tokens.css text: the custom overlay's copy when there is
    one and `overlay` allows it, else the stock file. main.py passes
    app_origin.overlay_allowed(scope), so an app's own origin gets the image's
    copy. None when it cannot be read as UTF-8 — main.py then serves
    the raw bytes and skips the override, and an artifact gets no style block
    rather than a broken one."""
    from server.main import DASHBOARD_BASE, DASHBOARD_CUSTOM
    from server.static_paths import inside
    # inside(): the overlay's own rule — a custom copy that links out of
    # custom/ is not served (server/main.py OverlayStaticFiles).
    path = inside(DASHBOARD_CUSTOM, "css/tokens.css") if overlay else None
    if path is None or not path.is_file():
        path = DASHBOARD_BASE / "css/tokens.css"
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


async def token_sheet() -> str:
    """Exactly what GET /css/tokens.css serves — the base file plus the live
    desktop's override — or '' when the base is unreadable. The one source for
    the shell's stylesheet AND the sheet every host-made artifact wears
    (server/applications/artifact_style.py), so the two cannot drift."""
    base = base_tokens_css()
    if base is None:
        return ""
    return base + await design_tokens_css()


@router.get(
    "/desktop/check",
    # write:appearance, not a new read scope, and deliberately so. There is no
    # read:appearance in server/applications/scopes.py — inventing one would
    # need a catalog entry AND a manifest entry, and getting that pair wrong is
    # the documented failure where the app silently fails to install at boot
    # (scripts/validate_manifest_scopes.py). It also buys nothing: this endpoint
    # exists only to predict whether the WRITE below would succeed, so its
    # audience is exactly the callers who may already perform it. It tells them
    # nothing they could not learn by doing it.
    dependencies=[Depends(requires_permission("write:appearance"))],
)
async def check_desktop_candidate(artifact_id: int, entry: str = "index.html") -> dict:
    """Would this artifact be accepted as the desktop? Read-only, sets nothing.

    Exists so the Artifacts window can offer "Use as desktop" only where it
    would actually work. Before this the button was on every artifact and the
    only way to find out was to click and read a 422 — the refusal arrived
    after the user had already decided.

    Returns {ok, mode, errors, warnings, tokens, report}. `report` is the same
    formatted text the 422 carries, so the window shows the user exactly what
    the kernel would have said, not a paraphrase of it.

    A hard error (not this entity's artifact, missing entry) still raises —
    those are not "this design needs work", they are "that is not a thing".
    """
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "brain pool down")
    try:
        _aid, report = await desktop_gates(
            pool, brain_service._entity_id, artifact_id, entry)
    except ArtifactError as e:
        raise HTTPException(e.status, e.detail) from None
    return {
        "ok": report["ok"],
        "mode": report["mode"],
        "errors": report["errors"],
        "warnings": report["warnings"],
        "tokens": len(report["tokens"]),
        "report": format_report(report),
    }


async def _compose_tile_document(pool, wallpaper_html: str | None) -> str | None:
    """The document the Host Manager tile draws: this host's shell, with its
    wallpaper underneath. ONE definition, used by both the publisher and the
    change-detector, so the thing we hash is always the thing we publish."""
    from server.applications.shell_snapshot import compose_clean_shell
    from server.routes.entity import _entity_name
    return await compose_clean_shell(
        pool, brain_service._entity_id,
        host_name=_entity_name(),
        wallpaper_html=wallpaper_html,
    )


async def publish_desktop_snapshot(pool, *, reason: str = "change") -> bool:
    """Publish this host's desktop document to the platform so the account's
    Host Manager can draw it as a tile. Returns True when a row was written.

    WHY THE PLATFORM HOLDS IT. Every host is its own origin behind its own
    host-only session cookie, and every host response carries
    X-Frame-Options: SAMEORIGIN — so the manager cannot fetch or frame N hosts
    to build a grid. The document has to be held platform-side for the grid to
    draw every tile from ONE origin. The bridge op is write-only and can only
    ever touch THIS entity's row (`desktop.publish`, bridge/adapter.py): the
    entity id comes from our client cert, never from the payload.

    NO POINTER IS A REAL STATE, NOT A SKIP. A host on the default wallpaper
    publishes html=None; a host that has never published has no row at all.
    The TABLE keeps those apart and this function is what produces the first
    one — returning early on "no desktop" would mean it never existed.

    The manager does not yet DRAW them differently (HostManager.tsx branches on
    `snap?.html`, so a null-html row and a missing row render the same tile).
    That is a reader-side gap, not a reason to stop writing the row: the
    distinction is the difference between "this host chose the default" and
    "this host has not reported in", and the data has to exist before any
    surface can say so.

    FAIL-SOFT, ALWAYS. A desktop redesign must never fail because a thumbnail
    could not be filed; the manager falls back to the host's stable mark, which
    is still a working grid. Every failure path logs and returns False.
    """
    try:
        desk = await _current_pointer(pool)
        wallpaper = None
        artifact_id = None
        if desk is not None:
            artifact_id = str(desk.get("artifact_id"))
            from server.applications import desktop_pin
            wallpaper = await desktop_pin.worn_html(
                pool, brain_service._entity_id, int(desk["artifact_id"]), desk["entry"])

        # THE TILE IS THE SHELL, NOT THE WALLPAPER. Publishing the desktop
        # document alone produced a tile showing only the bottom layer —
        # that document is only the surface the machine is painted on. The
        # taskbar, dock and app icons live in the shell above it, so the
        # snapshot is the shell WITH the wallpaper underneath it.
        html = await _compose_tile_document(pool, wallpaper)
        if html is None:
            # The shell could not be composed. Fall back to the wallpaper
            # rather than publishing nothing: a bottom layer is a worse tile
            # than the machine, but it is a better one than a blank mark.
            html = wallpaper

        # Tokens come from the WALLPAPER document, which is where a host
        # declares them — the composed shell inherits them through the CSS it
        # inlines. They are the fallback tile when a document cannot render at
        # all, so they must survive even when compose fails.
        tokens = extract_tokens(wallpaper) if wallpaper else {}
        content_hash = (hashlib.sha256(html.encode("utf-8")).hexdigest()
                        if html else None)

        # get_bridge_client() returns None before lifespan boot (main.py:112-114),
        # which is a normal state on a cold pod, not a failure.
        from server.main import get_bridge_client
        bc = get_bridge_client()
        if bc is None:
            log.debug("desktop snapshot (%s): bridge not up yet, skipped", reason)
            return False
        env = await bc.request_full("desktop.publish", {
            "html": html,
            "tokens": tokens or None,
            "artifact_id": artifact_id,
            "content_hash": content_hash,
        })
        if not env.get("ok"):
            log.warning("desktop snapshot (%s) refused: %s", reason, env.get("error"))
            return False
        log.info("desktop snapshot published (%s): %s bytes",
                 reason, (env.get("data") or {}).get("bytes", 0))
        return True
    except Exception as e:
        log.warning("desktop snapshot (%s) failed: %s", reason, e)
        return False


async def publish_desktop_snapshot_at_boot() -> None:
    """Publish on start, then keep the tile honest.

    THE BOOT PUBLISH is the half that makes the feature visible on day one.
    Without it the table fills only when someone calls set_desktop, so every
    existing host draws the fallback mark indefinitely — which is exactly how
    the grid looked when it first shipped.

    THE REFRESH exists because the desktop is a POINTER to an artifact, not a
    copy: editing the artifact changes the live desktop without anyone calling
    set_desktop (that is the whole point of the pointer — the shell re-renders
    while the user watches). So a set-only publish would show a tile the host
    stopped using, and the staler it got the more it would look like the grid
    was broken rather than behind.

    Cheap by construction: it re-renders locally and only crosses the bridge
    when the content HASH actually moved, so a desktop nobody is editing costs
    one render per interval and zero writes.
    """
    import asyncio as _aio
    INTERVAL_S = 300
    # Until the FIRST publish lands, retry fast. This task is scheduled from
    # the lifespan (main.py) well before the BridgeClient is constructed and
    # started (main.py:958-970), so the first attempt ALWAYS finds
    # get_bridge_client() is None and fails. That is expected, not an error.
    RETRY_S = 15

    last_hash: str | None = None
    published_once = False          # NOT "have we tried" — "has one LANDED"

    while True:
        try:
            pool = brain_service.get_pool()
            if pool is not None:
                digest = await _desktop_content_hash(pool)
                # `published_once` gates on SUCCESS, never on having attempted.
                # An earlier version flipped a `first` flag whether or not the
                # publish landed, which meant a host on the default wallpaper
                # (digest None, last_hash None -> "unchanged") never published
                # again for the life of the process: its row was never written
                # at all. A host WITH a desktop recovered on the next tick, so
                # the bug was invisible except on exactly the hosts that had
                # nothing to show.
                if (not published_once) or digest != last_hash:
                    if await publish_desktop_snapshot(
                            pool, reason="boot" if not published_once else "refresh"):
                        last_hash = digest
                        published_once = True
        except _aio.CancelledError:
            raise                    # shutdown — never swallow
        except Exception as e:
            log.warning("desktop snapshot task: %s", e)
        await _aio.sleep(INTERVAL_S if published_once else RETRY_S)


async def _desktop_content_hash(pool) -> str | None:
    """Hash of the document the tile would draw right now, or None when there
    is nothing to draw. Used to skip a bridge round-trip when nothing moved.

    Hashes the COMPOSED SHELL, not just the wallpaper. Hashing the wallpaper
    alone would miss every change that shows in the tile without touching the
    desktop artifact — installing an app, renaming the host — so a machine that
    gained two apps would keep showing the old picture until someone happened
    to redesign their wallpaper.

    Any failure returns a sentinel that never equals a real hash, so a
    transient error re-publishes rather than silently pinning the tile.
    """
    try:
        desk = await _current_pointer(pool)
        wallpaper = None
        if desk is not None:
            from server.applications import desktop_pin
            wallpaper = await desktop_pin.worn_html(
                pool, brain_service._entity_id, int(desk["artifact_id"]), desk["entry"])
        doc = await _compose_tile_document(pool, wallpaper) or wallpaper
        if doc is None:
            return None
        return hashlib.sha256(doc.encode("utf-8")).hexdigest()
    except Exception:
        return "unrenderable"


@router.get("/desktop")
async def serve_desktop_html() -> Response:
    """The HTML desktop — the pointed artifact's live render, the same
    self-contained document the Artifacts window shows. No auth, like /image:
    the shell mounts it on boot before any app window exists. Entity-scoped by
    construction — the pointer names this entity's own artifact and render()
    re-checks ownership + kind on every call."""
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "brain pool down")
    desk = await _current_pointer(pool)
    if desk is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no HTML desktop set")
    from server.applications import desktop_pin
    try:
        # What is WORN (server/applications/desktop_pin.py): a version that
        # passed the browser render check, or one worn while no browser could
        # be asked and SAID so — never one that failed. 404 when nothing may
        # be shown; the shell then shows the default wallpaper.
        html = await desktop_pin.worn_html(
            pool, brain_service._entity_id, int(desk["artifact_id"]), desk["entry"])
    except ArtifactError as e:
        raise HTTPException(e.status, e.detail) from None
    await desktop_pin.reconcile(pool, brain_service._entity_id,
                                int(desk["artifact_id"]), desk["entry"])
    # `CSP: sandbox` — this URL is host-authored HTML on the first-party
    # origin. The shell never navigates here (it fetches the text into its own
    # sandboxed srcdoc frame, unaffected by a CSP on the response), but a user
    # following a pasted link WOULD land on it as a same-origin document with
    # their session. The sandbox directive makes that landing an opaque-origin
    # page with no script authority over the shell — the same wall the frame
    # has. main.py's security_headers middleware preserves a route-set CSP —
    # and then SKIPS its X-Frame-Options: SAMEORIGIN, so the framing policy
    # rides along here as frame-ancestors 'self' (measured on the lab pod:
    # without it the response had no framing header at all). The shell never
    # frames this URL (srcdoc), so 'self' costs nothing.
    return HTMLResponse(html, headers={
        "Cache-Control": "no-cache",
        "Content-Security-Policy": "sandbox; frame-ancestors 'self'",
    })


@router.get("/desktop/meta")
async def desktop_html_meta(response: Response) -> dict:
    """The live-poll change signal: {artifact_id, entry, updated_at, pin}. The
    shell asks every 3 s while an HTML desktop is up and remounts when
    updated_at moves — the PIN's stamp (desktop_pin.pin_stamp), which moves
    only when a version passes the render check, or is worn without one and
    says so. `pin` is desktop_pin.status(): what the pin last did, which the
    shell shows as a notice."""
    response.headers["Cache-Control"] = "no-cache"
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "brain pool down")
    desk = await _current_pointer(pool)
    if desk is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no HTML desktop set")
    # The shell's own 3-second poll is what notices a worn artifact changed by
    # a writer that does not go through the artifact ops (a build run against
    # its id, the project file lane) — checked in the background, and on
    # screen only once it passes.
    from server.applications import desktop_pin
    await desktop_pin.reconcile(pool, brain_service._entity_id,
                                int(desk["artifact_id"]), desk["entry"])
    # What the pin last did — held at an older version, worn without a
    # render check, nothing shown — for the shell to show as a notice
    # (desktop-surface.js pollTick). Every move the reconcile above makes
    # lands here; none is a discarded return value.
    return {**desk, "pin": desktop_pin.status(int(desk["artifact_id"]))}


@router.post(
    "/desktop/loop",
    dependencies=[Depends(shell_origin_when_tokenless(
        "reporting a desktop reload loop", "write:appearance"))],
)
async def desktop_reloads_itself(request: Request) -> dict:
    """The shell counted more than 3 loads of the worn desktop's frame in 45 s:
    a document that reloads itself, slower than the render check watches.
    Takes it off (desktop_pin.take_off_reloading) and tells the shell, which
    then mounts what is worn now. The bytes are recorded as taken off after a
    reload report (unconfirmed), never as a failed check: wearing them again
    runs the normal check, or wears them with a note when no browser can be
    asked.

    Who may call (route_policy kind shell-origin, like /layout): a token-less
    call must come from the desktop's own page (the isolated-app origin and
    cross-origin browsers are refused); a call with a token needs
    write:appearance. The shell holds no token that could carry a scope (its
    own is read-only), so a token would not narrow this further: the
    Host-built apps framed on this origin have the shell's own authority
    through window.parent and same-origin fetch, and nothing here can tell
    them from the shell. What the route does is bounded for them instead: it
    can only take the worn version OFF, never put one on; only the version
    whose exact pin stamp the caller names; only once the server's own clock
    says that version has been on screen for LOOP_MIN_WORN_S (so a forged
    report cannot walk the history back faster than that); the bytes stay in
    the artifact's workspace; and the person is shown the same notice a failed
    check gives."""
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "brain pool down")
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    desk = await _current_pointer(pool)
    if desk is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no HTML desktop set")
    from server.applications import desktop_pin
    off = desktop_pin.take_off_reloading(
        int(desk["artifact_id"]), desk["entry"],
        str((body or {}).get("updated_at") or "") or None)
    if off:
        await _notify_shell()
    return {"taken_off": off}


@router.put(
    "/desktop",
    dependencies=[Depends(requires_permission("write:appearance"))],
)
async def set_desktop_html(request: Request) -> dict:
    """Point the desktop at an artifact: {artifact_id, entry?}. {artifact_id:
    null} clears the pointer and restores the default wallpaper. The host's
    own path is the `artifact` tool (op='set_desktop'), which calls the same
    set_desktop_artifact()."""
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "brain pool down")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be a JSON object")

    async def work():
        try:
            return await set_desktop_artifact(
                pool, brain_service._entity_id, body.get("artifact_id"),
                body.get("entry") or "index.html")
        except ArtifactError as e:
            raise HTTPException(e.status, e.detail) from None
    # A person's click: answered within the router's window, or 202 + a job
    # (desktop_render.run_door). The Host's op='set_desktop' calls
    # set_desktop_artifact directly and simply waits.
    from server.applications import desktop_render
    return await desktop_render.run_door("worn", work)


@router.get(
    "/desktop/jobs/{job_id}",
    # The doors it reports on all need write:appearance; so does reading their
    # outcome. A job id is unguessable and lives 15 minutes.
    dependencies=[Depends(requires_permission("write:appearance"))],
)
async def desktop_job(job_id: str) -> dict:
    """How a desktop door that answered 202 ended: {status: running} |
    {status: done, result} | {status: failed, http_status, detail} — the
    result or refusal the request itself would have returned."""
    from server.applications import desktop_render
    job = desktop_render.job_status(job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such job (or it expired)")
    return job


@router.get("/network", dependencies=[Depends(requires_permission("read:topology"))])
async def serve_network_html() -> Response:
    """The host-authored network map — the pointed artifact's live render. Same
    posture as /desktop: the Network app fetches this text into its own
    sandboxed srcdoc frame, so the CSP here protects the case nobody plans for
    — a user following a pasted link and landing on host-authored HTML as a
    same-origin document with their session. `sandbox` makes that landing an
    opaque-origin page with no authority over the shell."""
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "brain pool down")
    ptr = await _current_network_pointer(pool)
    if ptr is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no host-authored network map set")
    try:
        html = await artifact_service.render(
            pool, brain_service._entity_id, ptr["artifact_id"], ptr["entry"])
    except ArtifactError as e:
        raise HTTPException(e.status, e.detail) from None
    # RE-CHECK THE FLOOR ON EVERY RENDER, not only when the pointer was set.
    # set_network_artifact validates and 422s, but the pointer is deliberately
    # not a copy — /network/meta exists so the host can iterate the artifact and
    # watch the map update live. That means the document served here is NOT the
    # document that was checked: point at a compliant artifact, then rewrite it,
    # and every guarantee the floor makes (nodes come from the bridge, no
    # external references, no invented topology) is bypassed by the ordinary
    # editing flow. A one-time gate on a live pointer is not a gate.
    from server.applications.desktop_contract import check_network_document
    report = check_network_document(html)
    if not report["ok"]:
        log.warning("host-authored network map failed the floor on render "
                    "(artifact %s): %s", ptr["artifact_id"], "; ".join(report["errors"]))
        # 409, not 422: the stored pointer is fine, the CURRENT render is not.
        # The app falls back to its built-in views on a non-200, which is the
        # honest outcome — better an unstyled real map than a pretty false one.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "the host-authored map no longer meets the floor: "
            + format_report(report))
    return HTMLResponse(html, headers={
        "Cache-Control": "no-cache",
        # `sandbox` alone leaves the network open; the frame's own meta CSP is
        # the primary control (see HOSTED_CSP in the Network app). These headers
        # cover direct navigation to this URL, so deny by default here too.
        "Content-Security-Policy": (
            "sandbox; frame-ancestors 'self'; default-src 'none'; "
            "style-src 'unsafe-inline'; img-src data:; font-src data:; "
            "script-src 'unsafe-inline'; connect-src 'none'; form-action 'none'; "
            "base-uri 'none'"),
    })


@router.get("/network/meta", dependencies=[Depends(requires_permission("read:topology"))])
async def network_html_meta(response: Response) -> dict:
    """The live-poll change signal: {artifact_id, entry, updated_at}. The
    Network app re-renders when updated_at moves, so the host iterating in the
    Artifacts window updates the map while the user watches — the pointer is
    never a copy, which is the whole reason it is a pointer."""
    response.headers["Cache-Control"] = "no-cache"
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "brain pool down")
    ptr = await _current_network_pointer(pool)
    if ptr is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no host-authored network map set")
    return ptr


@router.put(
    "/network",
    dependencies=[Depends(requires_permission("write:appearance"))],
)
async def set_network_html(request: Request) -> dict:
    """Point the Network map at an artifact: {artifact_id, entry?}.
    {artifact_id: null} clears it and the app returns to its built-in views."""
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "brain pool down")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be a JSON object")
    try:
        return await set_network_artifact(
            pool, brain_service._entity_id, body.get("artifact_id"),
            body.get("entry") or "index.html")
    except ArtifactError as e:
        raise HTTPException(e.status, e.detail) from None


# ── Time zone (handled in the Settings app) ──────────────────────────────────
# The user's IANA timezone, stored in behavioral_params.timezone. The host's
# per-turn "This Turn" block renders the clock in THIS zone (runtime/frame.py)
# and the static communication block instructs it to speak times in it — before this, every timestamp the host saw was raw UTC, so
# "an hour ago" became "last night" the moment the UTC date rolled over.


@router.get(
    "/timezone",
    dependencies=[Depends(requires_permission("read:system_status"))],
)
async def get_timezone() -> dict:
    """The stored timezone (or null → host falls back to UTC and says so)."""
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "brain pool down")
    row = await pool.fetchrow(
        "SELECT behavioral_params FROM entity_registry WHERE id = $1",
        brain_service._entity_id)
    bp = row["behavioral_params"] if row and row["behavioral_params"] else {}
    if isinstance(bp, str):
        try:
            bp = json.loads(bp)
        except (json.JSONDecodeError, TypeError):
            bp = {}
    return {"timezone": (bp or {}).get("timezone") or None}


@router.put(
    "/timezone",
    dependencies=[Depends(requires_permission("write:appearance"))],
)
async def set_timezone(request: Request) -> dict:
    """Persist {"timezone": "<IANA name>"} (null/empty clears → UTC).
    Validated against the zoneinfo database — a typo must not silently
    corrupt every time the host speaks."""
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "brain pool down")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON") from None
    tz = body.get("timezone")
    if tz is not None:
        tz = str(tz).strip()
        if tz:
            from zoneinfo import ZoneInfo
            try:
                ZoneInfo(tz)
            except Exception:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"unknown timezone {tz!r} — use an IANA name like America/New_York",
                ) from None
        else:
            tz = None
    eid = brain_service._entity_id
    row = await pool.fetchrow(
        "SELECT behavioral_params FROM entity_registry WHERE id = $1", eid)
    bp = row["behavioral_params"] if row and row["behavioral_params"] else {}
    if isinstance(bp, str):
        try:
            bp = json.loads(bp)
        except (json.JSONDecodeError, TypeError):
            bp = {}
    bp = dict(bp or {})
    if tz:
        bp["timezone"] = tz
    else:
        bp.pop("timezone", None)
    await pool.execute(
        "UPDATE entity_registry SET behavioral_params = $1::jsonb WHERE id = $2",
        json.dumps(bp), eid)
    log.info("timezone set to %s (eid=%s)", tz or "UTC (cleared)", eid)
    return {"timezone": tz}
