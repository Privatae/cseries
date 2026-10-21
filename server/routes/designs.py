"""The Designs shelf — complete desktops to look at, try with one click, and share.

The design app is a shared repository of desktop designs. Each desktop UI costs
money to come up with, so a Host can add one to a shelf everyone can browse,
preview and apply with one click. The shelf holds a set of UIs the platform
makes as well as the ones people store themselves, so a design can be previewed
and applied instead of sitting unseen among artifacts.

What the Designs app shows, and where each comes from:

- OURS — the house shelf: designs published by the platform's own hosts,
  readable by every host (bridge design.* ops, shelf='house'), plus the
  house designs built into the image (designs/builtin/), which every Host
  has whether or not anything answers on the bridge.
- YOURS — designs this account published (shelf='account', every host on the
  account reads them), plus the desktops made ON THIS HOST that were never
  published (`/local`): a desktop the Host designed used to be findable only as
  a block of text in the Artifacts window.

Every card is the real document rendered live in the app's sandboxed frame —
documents come back as JSON, never as a navigable page on this origin.

Rules carried from the 2026-09-10 account library, which this grows:

1. **CHOOSING IS COPYING.** Installing writes the design's HTML into THIS
   host's own artifact and points its desktop at that. From then on the two
   documents are unrelated: the author editing or removing theirs cannot
   change anyone else's screen.
2. **TRYING IS UNDOABLE.** Install answers with the desktop it replaced
   (`previous`); `/restore` puts that back and discards the trial copy. One
   click to try, one click to go back — no copies piling up in Artifacts.
   What Go back restores is the wear the SERVER recorded when the try began
   (_record_try), never one the client hands back: a client-supplied
   `previous` could name any artifact, and Go back wears without a check.
3. **THE SHELF IS DECIDED BY THE PLATFORM.** Which shelf a publish lands on is
   the bridge's call from the publishing host's tier, never a parameter here.
4. **THE FLOOR IS CHECKED ON THE WAY IN AND OUT.** Publishing and installing
   run the same `check_document` `set_desktop` runs, so the shelf cannot offer
   a design the desktop would refuse.
5. **A BROWSER LOOKS BEFORE ANYONE ELSE DOES.** Publishing, installing and
   wearing also load the document in the real browser the way the shell
   mounts it (server/applications/desktop_render.py): one that throws, paints
   nothing, or does not draw the tiles or bar it declares is refused with the
   error and its line. On one Host, five harness-built desktops reached the
   shared shelf without ever being rendered.

PRIVACY. A host's desktop is private by default; publishing a design is the
deliberate exception, and asking for it is the consent. A house design names
its author host, never a person's address.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status

from server import brain_service
from server.applications import artifacts as artifact_service
from server.applications.artifacts import ArtifactError
from server.applications import desktop_render
from server.applications.desktop_contract import check_document, detect_mode, format_report
from server.applications.desktop_tokens import extract_tokens
from server.applications.middleware import requires_permission

log = logging.getLogger("privatae.pc.designs")

router = APIRouter(prefix="/api/designs", tags=["designs"])

_PERM = [Depends(requires_permission("write:appearance"))]

# The largest design that CAN be published: every publish is first loaded in
# a browser (desktop_render.render_check, below), and that check takes a page
# of at most 4 MiB (sidecars/browser/control/document_verify.py
# MAX_PAGE_CHARS). Refused HERE, before a browser is woken, so an oversized
# document is rejected while its author is looking at it. It was 256 KB, a
# mirror of the Host Manager TILE reader (the dashboard frontend's
# MAX_TILE_HTML) — a thumbnail's limit, which refused exactly the rich
# desktops (inline CSS, SVG, data-URI images) a design library is for.
# The tile is a separate, reduced picture
# (server/applications/shell_snapshot.py).
MAX_DESIGN_BYTES = 4 * 1024 * 1024

# How much of a local artifact's entry is read to decide whether it is a
# desktop — the same static-head window the contract and the shell use.
_HEAD_BYTES = 64 * 1024


def _pool():
    """brain_service.get_pool() returns None before the pool is up
    (brain_service.py:207-209), which is a real state on a cold pod."""
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "brain pool down")
    return pool


async def _bridge(op: str, params: dict) -> dict:
    """One governed call to the platform-side library.

    Every failure surfaces as an HTTP error rather than being swallowed: these
    are all things the user just clicked, and a silent no-op on a click is how
    a feature earns the reputation of being broken.
    """
    from server.main import get_bridge_client
    bc = get_bridge_client()
    if bc is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "the design library is not reachable yet")
    env = await bc.request_full(op, params)
    if not env.get("ok"):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            str(env.get("error") or "design library error"))
    return env.get("data") or {}


def _require_installable_name(name: str) -> None:
    """Refuse a design name that could be published but never installed.

    Installing creates an artifact called `Desktop - <name>`, validated against
    the artifact naming rule (`artifacts.is_valid_artifact_name`). A name
    containing `/` or `#` passes a length check, lands on the shelf looking
    real, and then fails for everyone who picks it.
    """
    if not name or len(name) > 80:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "give the design a name (80 characters or fewer)")
    from server.applications.artifacts import is_valid_artifact_name
    if not is_valid_artifact_name(f"Desktop - {name}"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "that name has characters a design cannot be installed under — "
            "letters, numbers, spaces and . , ( ) - ' \" : ; & ! ? + only")


async def _installed_ids(pool) -> frozenset[str]:
    """App ids this host can launch, for the floor's "draws real apps" check.
    ONE implementation, shared with the desktop route (appearance.py), so the
    shelf and set_desktop judge a document against the same set. An unreadable
    registry yields an empty set, which SKIPS the id check rather than refusing
    every document (desktop_contract.check_document)."""
    from server.routes.appearance import _installed_app_ids
    return await _installed_app_ids(pool)


async def _publishes_to_everyone(pool) -> bool:
    """Whether this host is one of the platform's own (admin tier) — the test
    the bridge applies to put a publish on the house shelf. Read here only to
    TELL the person before they publish; the bridge decides, from its own copy
    of the registry, never from this."""
    try:
        tier = await pool.fetchval(
            "SELECT license_tier FROM entity_registry WHERE id = $1",
            brain_service._entity_id)
    except Exception:  # noqa: BLE001 — unknown reads as "your shelf"
        return False
    return tier == "admin"


async def _author_name(pool) -> Optional[str]:
    """This host's display name, for the card's "by …" line. Best-effort."""
    try:
        return await pool.fetchval(
            "SELECT display_name FROM entity_registry WHERE id = $1",
            brain_service._entity_id)
    except Exception:  # noqa: BLE001 — a card without a byline is fine
        return None


async def publish_document(pool, name: str, html: str) -> dict:
    """Put one complete desktop document on the shelf. The ONE publish path —
    the Designs app's add/publish buttons and the Host's `artifact
    op='publish_design'` all come here, so the name rule, the size ceiling and
    the floor cannot differ between them. Raises HTTPException."""
    name = str(name or "").strip()
    _require_installable_name(name)
    if not isinstance(html, str) or not html.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "html is required — a design is a complete document")
    size = len(html.encode("utf-8"))
    if size > MAX_DESIGN_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            f"that document is {size // 1024} KB; a design is loaded in a "
                            f"browser before it is published, and that check takes at most "
                            f"{MAX_DESIGN_BYTES // (1024 * 1024)} MB")
    report = check_document(html, installed_ids=await _installed_ids(pool))
    if not report["ok"]:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, format_report(report))
    # The render check. Here, and only here, NOT CHECKED REFUSES: a publish
    # puts the document in front of other people — on the house shelf, every
    # Host — and each one who tries it gets a copy (rule 1), so nothing can be
    # undone for them afterwards. This door already fails closed when the
    # library itself is unreachable (_bridge: 503); a browser that could not
    # look is the same kind of answer. Nothing is lost: the document stays
    # where it is, and publishing again once the browser answers is one click.
    # Wearing is the user's own, undoable screen and proceeds with a warning
    # instead (appearance.set_desktop_artifact).
    render = await desktop_render.render_check(pool, html)
    if not render.get("checked"):
        why = ("\nNothing was published: a design is loaded in a browser before it goes "
               "on a shelf other people try.")
        if render.get("over_plan"):
            why += " " + desktop_render.room_sentence(render["over_plan"]) + (
                " Then publish again.")
        elif render.get("outdated"):
            why += (" This Host's browser is older than the check, so publishing comes "
                    "back once the browser is updated.")
        else:
            why += " Publish again once a browser can be started."
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            desktop_render.format_render(render) + why)
    if not render.get("ok"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            desktop_render.format_render(render))
    data = await _bridge("design.publish", {
        "name": name,
        "html": html,
        "tokens": extract_tokens(html) or None,
        # Always this host's own name — never a caller's. On a platform host a
        # publish lands in front of everyone, and a byline must be true there.
        "author_name": await _author_name(pool),
    })
    return {**data, "name": name, "warnings": report.get("warnings") or [],
            "render": desktop_render.summary(render)}


# ── What is worn right now ───────────────────────────────────────────────────

async def _wearing(pool) -> Optional[dict]:
    """What the desktop is right now: {wallpaper, artifact_id, entry}. The
    pointer counts only while the wallpaper IS the HTML desktop — pick another
    swatch and the pointer survives but is not what the user sees."""
    eid = brain_service._entity_id
    from server.routes.appearance import _desktop_pointer
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT wallpaper, wallpaper_mobile, desktop_artifact_id, desktop_entry "
            "FROM entity_appearance WHERE entity_id = $1", eid)
        desk = await _desktop_pointer(conn, eid, row) if row else None
    wallpaper = (row["wallpaper"] if row else None) or "default"
    # The phone's own choice too: wearing a design moves the phone onto it
    # (appearance.set_desktop_artifact), so Go back has to put it back.
    mobile = row["wallpaper_mobile"] if row else None
    if wallpaper == "html" and desk:
        return {"wallpaper": "html", "artifact_id": desk["artifact_id"], "entry": desk["entry"],
                "wallpaper_mobile": mobile}
    out = {"wallpaper": wallpaper, "artifact_id": None, "entry": None,
           "wallpaper_mobile": mobile}
    if mobile == "html" and desk:
        # The desktop wears a swatch while the PHONE wears the Host design:
        # the pointer is what the phone shows, so Go back needs it too. Kept
        # apart from artifact_id, which means "worn on the desktop".
        out.update(phone_artifact_id=desk["artifact_id"], phone_entry=desk["entry"])
    return out


async def _restore(pool, previous: dict) -> dict:
    """Put a desktop captured by `_wearing` back exactly: the HTML desktop it
    pointed at, or the ordinary wallpaper swatch it was on."""
    from server.routes.appearance import WALLPAPERS, set_desktop_artifact
    eid = brain_service._entity_id
    if previous.get("wallpaper") == "html" and previous.get("artifact_id"):
        if "wallpaper_mobile" not in previous:
            # A try recorded before the phone was: that try did not move the
            # phone, so going back must not either — but the wear below sets
            # it to "Same as desktop". Keep what the phone shows now.
            previous = {**previous, "wallpaper_mobile": await pool.fetchval(
                "SELECT wallpaper_mobile FROM entity_appearance WHERE entity_id = $1", eid)}
        # No render check: this desktop was on screen a moment ago, and going
        # back must work even when no browser can be woken.
        result = await set_desktop_artifact(pool, eid, previous["artifact_id"],
                                            previous.get("entry") or "index.html",
                                            render_check=False)
        result = await _restore_phone(pool, eid, previous, result, html_shown=True)
        await _notify_after_restore()
        return result
    wallpaper = str(previous.get("wallpaper") or "default")
    if wallpaper != "html" and wallpaper not in WALLPAPERS:
        # `previous` round-trips through the client; only a real swatch key is
        # ever written back.
        raise ArtifactError(400, f"unknown wallpaper {wallpaper!r}")
    result = await set_desktop_artifact(pool, eid, None)
    if wallpaper not in ("html", "default"):
        # Clearing lands on 'default'; the user had a swatch of their own.
        await pool.execute(
            "UPDATE entity_appearance SET wallpaper = $2, updated_at = NOW() "
            "WHERE entity_id = $1", eid, wallpaper)
        result = {**(result or {}), "wallpaper": wallpaper}
    phone_html = await _restore_phone_pointer(pool, eid, previous)
    if phone_html:
        # Clearing above published the Host Manager tile with no pointer; the
        # tile draws the pointer whatever the wallpaper, and the refresh loop
        # republishes only when the content hash moved since ITS last look —
        # a quick try and Go back return it to that hash, so the tile would
        # stay cleared. Publish what is pointed at again.
        from server.routes.appearance import publish_desktop_snapshot
        await publish_desktop_snapshot(pool, reason="restored")
    result = await _restore_phone(pool, eid, previous, result, html_shown=phone_html)
    await _notify_after_restore()
    return result


async def _restore_phone_pointer(pool, eid: int, previous: dict) -> bool:
    """The desktop wore a swatch while the phone wore the Host design
    (`_wearing` phone_artifact_id): point at that design again for the phone.
    Only an artifact of this Host that still exists; its pin is untouched by
    the try (pins are per artifact), so the phone shows what it showed.
    True when the phone has an HTML desktop to show again."""
    aid = previous.get("phone_artifact_id")
    if previous.get("wallpaper_mobile") != "html" or not isinstance(aid, int):
        return False
    from server.routes.appearance import _clean_entry
    async with pool.acquire() as conn:
        found = await conn.fetchval(
            "SELECT 1 FROM projects WHERE id=$1 AND entity_id=$2 AND kind='artifact'", aid, eid)
        if not found:
            return False
        await conn.execute(
            "UPDATE entity_appearance SET desktop_artifact_id = $2, desktop_entry = $3, "
            "updated_at = NOW() WHERE entity_id = $1",
            eid, aid, _clean_entry(previous.get("phone_entry") or "index.html"))
    return True


async def _notify_after_restore() -> None:
    """Tell the open desktop to re-read appearance once every write of the
    restore has landed. set_desktop_artifact notifies after its own write,
    before the swatch and the phone are put back, so a shell that read then
    kept the trial's values."""
    from server.routes.appearance import _notify_shell
    await _notify_shell()


async def _restore_phone(pool, eid: int, previous: dict, result: dict, *,
                         html_shown: bool) -> dict:
    """Put back the phone's own wallpaper `_wearing` recorded. A record made
    before the phone was recorded has no key and changes nothing; 'html' is
    put back only while there is an HTML desktop to show (clearing the
    pointer leaves nothing for it)."""
    if "wallpaper_mobile" not in previous:
        return result
    from server.routes.appearance import WALLPAPERS
    mobile = previous.get("wallpaper_mobile")
    if mobile is not None and (str(mobile) not in WALLPAPERS
                               or (mobile == "html" and not html_shown)):
        return result
    await pool.execute(
        "UPDATE entity_appearance SET wallpaper_mobile = $2, updated_at = NOW() "
        "WHERE entity_id = $1", eid, mobile)
    return {**(result or {}), "wallpaper_mobile": mobile}


# ── Go back: only to a wear the server recorded ─────────────────────────────
#
# Go back skips the render check (going back must work with no browser), so
# what it puts on screen must be something the server itself saw worn — the
# desktop on screen when the try began, recorded here — never a `previous`
# the client sends back, which could name any of this Host's artifacts. A try
# on top of a try keeps the ORIGINAL desktop (the Designs window's own rule).
# Kernel-private, beside the render check's own state.
_TRY_FILE = "designs-try.json"


def _try_path():
    return desktop_render.state_dir() / _TRY_FILE


def _try_load() -> Optional[dict]:
    import json as _json
    try:
        rec = _json.loads(_try_path().read_text())
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) and isinstance(rec.get("previous"), dict) else None


def _try_clear() -> None:
    try:
        _try_path().unlink()
    except OSError:
        pass


def _record_try(previous: dict, trying: int) -> None:
    """The desktop to go back to from `trying`, the artifact now worn."""
    import json as _json
    import os as _os
    from datetime import datetime as _dt, timezone as _tz
    rec = _try_load()
    if rec and previous.get("artifact_id") is not None \
            and rec.get("trying") == previous.get("artifact_id"):
        first = rec["previous"]             # a try on top of a try
        if "wallpaper_mobile" not in first and "wallpaper_mobile" in previous:
            # The first try was recorded before the phone was, and did not
            # move it: the phone this try replaces is the one to go back to.
            first = {**first, "wallpaper_mobile": previous["wallpaper_mobile"]}
        previous = first
    path = _try_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".tmp-{_os.getpid()}")
        tmp.write_text(_json.dumps({"previous": previous, "trying": int(trying),
                                    "at": _dt.now(_tz.utc).isoformat()}))
        _os.replace(tmp, path)
    except OSError as e:
        # No record: Go back answers that there is nothing to go back to —
        # the safe side; the person can pick their old desktop by hand.
        log.info("try not recorded: %s", e)


# ── Trial copies: the ONLY artifacts this module may ever delete ─────────────
#
# Installing a shelf design makes a copy; "Go back" and a second try discard
# that copy. `discard_artifact_id` arrives from the client, and write:appearance
# (the scope these routes need) is a scope a store app can hold — so a client
# id alone must never be enough to delete an artifact (review of the bar
# branch, B1: a loop over ids would have wiped every artifact and its history).
# A copy is discardable only while ALL of these hold:
#   - install marked it (a file inside its own .history/, which never renders,
#     never snapshots and never becomes a variant), with the fingerprint of the
#     document it installed;
#   - it is the desktop that was on screen at the moment of the switch — the
#     one "Go back" or the next try is replacing. A copy the user kept wearing
#     and later switched away from is no longer a trial anyone is undoing;
#   - nothing has changed it since it was installed: one revision AND the same
#     bytes (the project file lane can write without a revision) — once the
#     Host or the user edits it, it is somebody's work;
#   - nobody pressed Keep (/keep removes the marker).
_TRIAL_MARK = "design-trial"


def _workspace(artifact_id: int):
    return artifact_service._projects_root() / str(int(artifact_id))


def _fingerprint(artifact_id: int) -> Optional[str]:
    """sha256 over every file in the copy's workspace except .history/ (the
    files a snapshot takes; a link is not followed)."""
    import hashlib
    from server.applications.tree_walk import open_tree_no_escape
    workspace = artifact_service._projects_root() / str(int(artifact_id))
    h = hashlib.sha256()
    try:
        with open_tree_no_escape(workspace) as tree:
            for f in artifact_service._workspace_files(tree):
                data = tree.read(f)
                if data is None:
                    return None      # changed under us: not provably the copy
                h.update(f.rel.encode())
                h.update(b"\0")
                h.update(data)
                h.update(b"\0")
    except OSError:
        return None
    return h.hexdigest()


def _mark_trial(artifact_id: int, design_id: int) -> None:
    # Written into the artifact's own .history, never through a link
    # (artifacts.write_history_file).
    try:
        artifact_service.write_history_file(
            _workspace(artifact_id), _TRIAL_MARK,
            f"{int(design_id)} {_fingerprint(artifact_id) or ''}".encode())
    except OSError as e:
        # An unmarked copy is simply kept — the safe side.
        log.info("trial copy %s not marked: %s", artifact_id, e)


def _unmark_trial(artifact_id: int) -> None:
    artifact_service.remove_history_file(_workspace(artifact_id), _TRIAL_MARK)


def _is_discardable_trial(artifact_id: int, worn_before: Optional[int]) -> bool:
    """`worn_before`: the desktop on screen immediately before this request
    switched it. Only THAT copy — the trial being replaced — may go."""
    if worn_before is None or int(worn_before) != int(artifact_id):
        return False
    raw = artifact_service.read_history_file(_workspace(artifact_id), _TRIAL_MARK)
    if raw is None:
        return False
    mark = raw.decode("utf-8", errors="replace").split()
    if len(mark) != 2:
        return False
    workspace = artifact_service._projects_root() / str(int(artifact_id))
    if len(artifact_service._history_log(workspace)) > 1:
        return False
    return _fingerprint(artifact_id) == mark[1]


# ── House designs built into the image ───────────────────────────────────────
#
# designs/builtin/ holds complete desktop documents and an index.json naming
# them ({"designs": [{"id", "name", "file"}]}). Their ids are the ids the same
# designs have on the platform's house shelf, so on a Host whose bridge also
# lists them the two are one card, and preview and try read the local copy
# either way. They are read from disk on each call: two small files.

BUILTIN_DIR = Path(__file__).resolve().parents[2] / "designs" / "builtin"
BUILTIN_AUTHOR = "C-Series"


def _builtin_index() -> list[dict]:
    try:
        rows = json.loads((BUILTIN_DIR / "index.json").read_text()).get("designs") or []
    except (OSError, ValueError, AttributeError) as e:
        log.warning("built-in designs unreadable: %s", e)
        return []
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        try:
            did, name, fname = int(r["id"]), str(r["name"]), str(r["file"])
        except (KeyError, TypeError, ValueError):
            continue
        # A file name, never a path: the index cannot point outside the folder.
        if Path(fname).name != fname:
            continue
        out.append({"id": did, "name": name, "file": fname})
    return out


def builtin_html(design_id: int) -> Optional[dict]:
    """{name, html, author_name} for a built-in design, or None."""
    for r in _builtin_index():
        if r["id"] == int(design_id):
            try:
                html = (BUILTIN_DIR / r["file"]).read_text(encoding="utf-8")
            except OSError as e:
                log.warning("built-in design %s unreadable: %s", design_id, e)
                return None
            return {"name": r["name"], "html": html, "author_name": BUILTIN_AUTHOR}
    return None


def builtin_designs() -> list[dict]:
    """The built-in designs as shelf rows, in the shape the bridge's
    design.list returns them."""
    rows = []
    for r in _builtin_index():
        doc = builtin_html(r["id"])
        if doc is None:
            continue
        rows.append({"id": r["id"], "name": r["name"], "shelf": "house",
                     "author_name": BUILTIN_AUTHOR, "mine": False, "builtin": True,
                     "tokens": extract_tokens(doc["html"][:_HEAD_BYTES]) or None})
    return rows


async def _design_doc(design_id: int) -> dict:
    """A shelf design's {name, html, author_name}: the built-in copy when there
    is one, else the library's over the bridge."""
    local = builtin_html(design_id)
    if local is not None:
        return local
    return await _bridge("design.get", {"design_id": int(design_id)})


def _standalone() -> bool:
    """Whether this Host has no platform at all (a NoPlatform bridge)."""
    from server.bridge.no_platform import NoPlatform
    try:
        from server.main import get_bridge_client
        return isinstance(get_bridge_client(), NoPlatform)
    except Exception:  # noqa: BLE001 — unknown reads as "has a platform"
        return False


# ── Desktops made on this host ───────────────────────────────────────────────

def _head(artifact_id: int) -> str:
    path = artifact_service._projects_root() / str(int(artifact_id)) / "index.html"
    try:
        with open(path, "rb") as f:
            return f.read(_HEAD_BYTES).decode("utf-8", "replace")
    except OSError:
        return ""


def _require_desktop(artifact_id: int) -> None:
    """The /local routes serve DESKTOPS only. write:appearance is not a scope
    that may read an arbitrary artifact (read:projects is) — so an id whose
    entry does not declare itself a desktop is answered exactly like a missing
    one."""
    if not detect_mode(_head(artifact_id)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such desktop on this host")


async def local_desktops(pool) -> list[dict]:
    """This host's artifacts whose entry declares itself a desktop — every
    design the Host made here, published or not. Read from the static head
    only (the same rule set_desktop applies), newest first."""
    eid = brain_service._entity_id
    rows = await pool.fetch(
        "SELECT id, name, description, created_at, updated_at FROM projects "
        "WHERE entity_id=$1 AND kind='artifact' ORDER BY updated_at DESC LIMIT 200",
        eid)
    out = []
    for r in rows:
        head = _head(r["id"])
        mode = detect_mode(head)
        if not mode:
            continue
        out.append({
            "artifact_id": r["id"],
            "name": r["name"],
            "description": r["description"],
            "mode": mode,
            "tokens": extract_tokens(head) or None,
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        })
    return out


# ── Routes ───────────────────────────────────────────────────────────────────

@router.get("", dependencies=_PERM)
async def list_designs() -> dict:
    """Both shelves plus this host's own desktops, and what is worn now.

    Shelf rows are metadata only — documents come one at a time from
    /{id}/document when a card scrolls into view. An unreachable library is
    reported (`library_error`) beside the local desktops rather than failing
    the whole screen: what this host made is still here to look at."""
    pool = _pool()
    shelf, library_error = [], None
    try:
        shelf = (await _bridge("design.list", {})).get("designs") or []
    except HTTPException as e:
        # A standalone Host has no library to reach: its house shelf is the
        # built-in designs, and saying it "can't be reached" would be untrue.
        library_error = None if _standalone() else e.detail
    listed = {d.get("id") for d in shelf if isinstance(d, dict)}
    shelf = list(shelf) + [b for b in builtin_designs() if b["id"] not in listed]
    return {
        "designs": shelf,
        "local": await local_desktops(pool),
        "wearing": await _wearing(pool),
        # The Host's name, for previews: a design greets its Host by name.
        "host": await _author_name(pool),
        # Where a publish from here lands, said BEFORE the click.
        "publishes_to": "everyone" if await _publishes_to_everyone(pool) else "account",
        "library_error": library_error,
    }


@router.get("/{design_id}/document", dependencies=_PERM)
async def design_document(design_id: int) -> dict:
    """One shelf design's complete document, for its live preview. JSON, never
    a page: host-authored HTML is only ever mounted by the app's sandboxed
    frame, never served as a navigable document on this origin."""
    data = await _design_doc(int(design_id))
    return {"design_id": int(design_id), "name": data.get("name"), "html": data.get("html") or ""}


@router.get("/local/{artifact_id}/document", dependencies=_PERM)
async def local_document(artifact_id: int) -> dict:
    """One of this host's own desktops, rendered, for its live preview."""
    _require_desktop(artifact_id)
    try:
        html = await artifact_service.render(_pool(), brain_service._entity_id, int(artifact_id))
    except ArtifactError as e:
        raise HTTPException(e.status, e.detail) from None
    return {"artifact_id": int(artifact_id), "html": html}


@router.post("", dependencies=_PERM)
async def add_design(request: Request) -> dict:
    """Add a complete desktop document to the shelf. {name, html}"""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be a JSON object")
    pool = _pool()
    # A click: answered inside the router's window or 202 + a job
    # (desktop_render.run_door) — the render check can outlast that window.
    return await desktop_render.run_door(
        "published", lambda: publish_document(pool, body.get("name"), body.get("html")))


async def publish_artifact(pool, artifact_id: Optional[int], name: str) -> dict:
    """Publish one of this host's desktops — the named artifact, or the one the
    desktop is wearing when none is named. Renders the artifact rather than
    trusting anything sent in, so what lands on the shelf is exactly what is on
    screen. Shared by the app's Publish button and `artifact op='publish_design'`."""
    eid = brain_service._entity_id
    entry = "index.html"
    if artifact_id is None:
        worn = await _wearing(pool)
        if not worn.get("artifact_id"):
            raise HTTPException(status.HTTP_409_CONFLICT,
                                "this host is not wearing a designed desktop, so "
                                "there is nothing to publish — name the artifact")
        artifact_id, entry = worn["artifact_id"], worn.get("entry") or "index.html"
        # What is ON SCREEN is the pin (server/applications/desktop_pin.py) —
        # an edit that has not passed the render check is not on screen, so
        # it is not what "publish the desktop I'm wearing" sends.
        from server.applications import desktop_pin
        try:
            html = await desktop_pin.worn_html(pool, eid, int(artifact_id), entry)
        except ArtifactError as e:
            raise HTTPException(e.status, e.detail) from None
        return {**await publish_document(pool, name, html), "artifact_id": int(artifact_id)}
    try:
        html = await artifact_service.render(pool, eid, int(artifact_id), entry)
    except ArtifactError as e:
        raise HTTPException(e.status, e.detail) from None
    return {**await publish_document(pool, name, html), "artifact_id": int(artifact_id)}


@router.post("/publish-current", dependencies=_PERM)
async def publish_current_desktop(request: Request) -> dict:
    """Publish the desktop this host is WEARING. {name}"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    pool, name = _pool(), str((body or {}).get("name") or "")
    return await desktop_render.run_door("published", lambda: publish_artifact(pool, None, name))


@router.post("/local/{artifact_id}/publish", dependencies=_PERM)
async def publish_local(artifact_id: int, request: Request) -> dict:
    """Publish one of this host's desktops by id. {name}"""
    _require_desktop(artifact_id)
    try:
        body = await request.json()
    except Exception:
        body = {}
    pool, name = _pool(), str((body or {}).get("name") or "")
    return await desktop_render.run_door(
        "published", lambda: publish_artifact(pool, int(artifact_id), name))


async def _json_body(request: Request) -> dict:
    """The request's JSON object, or {} — read up front, before a door's work
    may outlive the request (desktop_render.run_door)."""
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


async def _discard_trial(pool, body: Optional[dict], keep: Optional[int],
                         worn_before: Optional[int]) -> bool:
    """Delete the trial copy an earlier try left, named in the body as
    `discard_artifact_id` — trying a second design while the first is still
    on trial must not orphan the first copy. Only a discardable TRIAL copy
    (see _is_discardable_trial) is ever deleted; any other id is left alone."""
    discard = (body or {}).get("discard_artifact_id") if isinstance(body, dict) else None
    if discard is None:
        return False
    try:
        discard = int(discard)
    except (TypeError, ValueError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "discard_artifact_id must be an integer") from None
    if discard == keep or not _is_discardable_trial(discard, worn_before):
        return False
    try:
        await artifact_service.delete_artifact(pool, brain_service._entity_id, discard)
        return True
    except ArtifactError as e:
        log.info("trial copy %s not discarded: %s", discard, e.detail)
        return False


@router.post("/{design_id}/install", dependencies=_PERM)
async def install_design(design_id: int, request: Request) -> dict:
    """Try a design: copy its document into THIS host's own artifact and wear
    it. Answers with `previous`, the desktop it replaced, for /restore.
    Optional body {discard_artifact_id}: the previous trial copy to drop.
    A click: answered in time or 202 + a job (desktop_render.run_door)."""
    body = await _json_body(request)
    return await desktop_render.run_door("worn", lambda: _install(int(design_id), body))


async def _install(design_id: int, body: dict) -> dict:
    data = await _design_doc(int(design_id))
    html = data.get("html")
    if not isinstance(html, str) or not html.strip():
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "that design has no document")

    pool = _pool()
    # Re-check the floor on the way IN: it passed when it was published, but
    # THIS host may have a different app set, and set_desktop judges it anyway —
    # better the full report here than an orphan artifact and a refusal.
    report = check_document(html, installed_ids=await _installed_ids(pool))
    if not report["ok"]:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, format_report(report))
    # The render check, on the way in and BEFORE the copy, for the same reason
    # as the floor above: designs published before the check existed are on
    # the shelf, and this Host's app set is its own. A refusal here leaves no
    # orphan artifact; the result rides into set_desktop_artifact so the same
    # bytes are not loaded twice. Not checked proceeds, as any wear does.
    render = await desktop_render.render_check(pool, html)
    if render.get("checked") and not render.get("ok"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            desktop_render.format_render(render))

    previous = await _wearing(pool)
    name = str(data.get("name") or f"Design {design_id}")
    try:
        art = await artifact_service.create_artifact(
            pool, brain_service._entity_id,
            name=f"Desktop - {name}",
            files=[{"path": "index.html", "content": html}],
            description=("Installed from the design shelf (by "
                         f"{data.get('author_name') or 'another host'})."),
        )
        _mark_trial(art["artifact_id"], design_id)
        from server.routes.appearance import set_desktop_artifact
        result = await set_desktop_artifact(
            pool, brain_service._entity_id, art["artifact_id"], "index.html",
            render_check=render)
    except ArtifactError as e:
        raise HTTPException(e.status, e.detail) from None

    _record_try(previous, int(art["artifact_id"]))
    log.info("installed design %s (%s) as artifact %s", design_id, name, art.get("artifact_id"))
    discarded = await _discard_trial(pool, body, art.get("artifact_id"),
                                     previous.get("artifact_id"))
    return {"installed": True, "design_id": int(design_id), "name": name,
            "artifact_id": art.get("artifact_id"), "previous": previous,
            "discarded": discarded, **(result or {})}


@router.post("/local/{artifact_id}/wear", dependencies=_PERM)
async def wear_local(artifact_id: int, request: Request) -> dict:
    """Wear one of this host's own desktops. No copy — it is already this
    host's. Answers with `previous` for /restore, like install; the same
    optional {discard_artifact_id}. A click: answered in time or 202 + a job."""
    body = await _json_body(request)
    return await desktop_render.run_door("worn", lambda: _wear(int(artifact_id), body))


async def _wear(artifact_id: int, body: dict) -> dict:
    pool = _pool()
    previous = await _wearing(pool)
    from server.routes.appearance import set_desktop_artifact
    try:
        result = await set_desktop_artifact(pool, brain_service._entity_id, int(artifact_id))
    except ArtifactError as e:
        raise HTTPException(e.status, e.detail) from None
    _record_try(previous, int(artifact_id))
    discarded = await _discard_trial(pool, body, int(artifact_id),
                                     previous.get("artifact_id"))
    return {"artifact_id": int(artifact_id), "previous": previous,
            "discarded": discarded, **(result or {})}


@router.post("/restore", dependencies=_PERM)
async def restore(request: Request) -> dict:
    """Go back to the desktop worn before a try. {discard_artifact_id?}

    The desktop put back is the one the server recorded when the try began
    (_record_try) — and only while that try is still what is on screen. A
    `previous` in the body is ignored: Go back wears without a render check,
    so it must never wear something the client names.

    `discard_artifact_id` is the trial copy install made; it is deleted once
    the previous desktop is back if it is still a discardable trial
    (_is_discardable_trial) — the copy existed only to be tried, and a gallery
    a person tries ten designs in must not leave ten documents behind. Any
    other id is left alone."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON") from None
    pool = _pool()
    # What is on screen NOW is the trial being undone — read before switching.
    worn_before = (await _wearing(pool)).get("artifact_id")
    rec = _try_load()
    if rec is None or worn_before is None or rec.get("trying") != int(worn_before):
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "There is no try to go back from — nothing was tried, or the "
                            "desktop has changed since. Pick the desktop you want instead.")
    previous = rec["previous"]
    try:
        result = await _restore(pool, previous)
    except ArtifactError as e:
        raise HTTPException(e.status, e.detail) from None
    _try_clear()
    discard = (body or {}).get("discard_artifact_id")
    discarded = False
    if discard is not None:
        try:
            discard = int(discard)
        except (TypeError, ValueError):
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                "discard_artifact_id must be an integer") from None
        # Never the design just put back — on the desktop, or (under a desktop
        # swatch) on the phone, which _restore pointed at again.
        if discard not in (previous.get("artifact_id"), previous.get("phone_artifact_id")) \
                and _is_discardable_trial(discard, worn_before):
            try:
                await artifact_service.delete_artifact(pool, brain_service._entity_id, discard)
                discarded = True
            except ArtifactError as e:
                log.info("restore: trial copy %s not discarded: %s", discard, e.detail)
    return {"restored": True, "discarded": discarded, **(result or {})}


@router.post("/keep", dependencies=_PERM)
async def keep(request: Request) -> dict:
    """Keep the design being tried: its copy stops being a trial, so no later
    try or "Go back" — from this window or a stale one — can discard it.
    {artifact_id}"""
    try:
        body = await request.json()
        aid = int((body or {}).get("artifact_id"))
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "artifact_id (int) is required") from None
    _unmark_trial(aid)
    rec = _try_load()
    if rec is not None and rec.get("trying") == aid:
        _try_clear()          # kept: there is no try left to go back from
    return {"kept": True, "artifact_id": aid}


@router.delete("/{design_id}", dependencies=_PERM)
async def remove_design(design_id: int) -> dict:
    """Take one of this account's designs off the shelf. Hosts already wearing
    it keep their own copy."""
    return await _bridge("design.delete", {"design_id": int(design_id)})
