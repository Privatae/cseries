"""What the user WEARS: the last render of the desktop artifact that passed
the browser render check — never simply "whatever the artifact holds now",
and never bytes the check refused.

Why. The desktop is a pointer to an artifact, and the shell re-renders it on
every write (desktop-surface.js polls /api/appearance/desktop/meta). So
checking a document when it is SET is not enough: the next write to it went
straight onto the user's screen, broken or not. Now wearing PINS the checked
render, and the pin moves on a pass, or on "not checked" with a warning that
is surfaced — never on a failure:

  - set / wear / Try write the pin from the exact bytes they checked
    (appearance.set_desktop_artifact);
  - every artifact write (artifacts.write_files / promote_entry /
    import_files / revert_artifact — the Host's ops, tune edits, "Use this
    one", Restore) and the project file lane call after_write: if the
    artifact is the worn desktop, its new render is checked; a pass ADVANCES
    the pin, a failure KEEPS the previous good render on screen and the
    write's result carries the error and its line;
  - anything else that writes the workspace (a build run against the
    artifact's id) is caught by reconcile(), which the shell's own 3-second
    poll drives: a worn artifact whose content differs from its pin is
    checked in the background, and the screen moves exactly as above — on a
    pass, or on "not checked" with a surfaced warning, never on a failure.
    Bytes already known to fail (desktop_render.known_failure) are never
    asked about again;
  - when the check cannot run (no browser, a full plan) the pin advances with
    a NOT RENDER-CHECKED warning — unless those exact bytes already FAILED,
    in which case they stay refused. A worn version that was never checked
    is checked again by a later reconcile, and taken off the screen if it
    fails. Never on a fixed clock: each re-check that cannot run doubles the
    wait (RECHECK_S, then 2x, 4x … up to RECHECK_MAX_S), the count is kept in
    the pin's own record so a kernel restart does not reset it, and a browser
    that answered it HAS NO render check (an older sidecar image, HTTP 404) is
    not asked again until its image changes (desktop_render.sidecar_outdated).
    A background re-check releases the browser as soon as it answers — the
    240 s linger is for a burst of edits, not for this.
  - the shell's report of a reload loop (take_off_reloading) takes the worn
    version off, recorded as taken off after a reload report (unconfirmed),
    never as a failure: no browser saw the loop. A wear checks it again.

EVERY MOVE IS SURFACED. The pin's status (status()) is what the shell's poll
reads beside its stamp (/api/appearance/desktop/meta → `pin`, shown as a
notice) and what the Host's per-turn appearance line carries
(mastercode/runtime/prompt_schema.py _b_appearance): "held at the version
from …", "worn WITHOUT a render check", "nothing that rendered can be
shown". Never a discarded return value.

Storage: KERNEL-PRIVATE, beside the projects tree
(desktop_render.state_dir()/pins/<artifact>/), never inside a project — no
project writer (a build landing, the project file lane, the editor) can
reach it. Each worn version is kept by digest with its record, and every read
verifies the bytes against the record: a copy that does not match is never
served; the last one that does is, and the status says so. The pin is the
rendered BYTES, not a revision id. A revision keeps every file now, whatever
its size, and is pruned only when the disk cannot take the next one
(artifacts.py) — but the render is composed from the
workspace at check time, so the bytes that were checked are the only thing
that can promise to be what was checked, and a revision can still be pruned
for space.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("privatae.pc.desktop_pin")

PIN_META = "pin.json"
# Worn versions kept per artifact: the current one and the ones before it,
# so a copy that fails its digest still has a verified version to fall back to.
KEEP = 5
# How long a Host's own write waits for the check before answering "pending".
# Host tool calls run in-kernel (no router window), so this covers a cold wake
# (app_verify.BROWSER_VERIFY_TIMEOUT_S is 150 s) plus the check itself.
HOST_WAIT_S = 180.0
# A version worn WITHOUT a render check (no browser at the time) is asked
# about again by the shell's reconcile: RECHECK_S after the first try that
# could not run, then twice as long after each further one, up to
# RECHECK_MAX_S. Measured 2026-10-02 on a live Host: a fixed 300 s re-ask woke the
# 2-vCPU browser and kept it up 240 s of every 300 for a check its sidecar
# answered 404 to every time. The doubling is the
# CrashLoopBackOff shape; 6 h keeps a Host whose browser stays unavailable to a
# handful of wakes a day, and a wear asks again at once whatever the wait.
RECHECK_S = 300.0
RECHECK_MAX_S = 6 * 3600.0

_seq: dict[int, int] = {}          # artifact -> last check started
_applied: dict[int, int] = {}      # artifact -> newest check whose answer was applied
_inflight: dict[int, asyncio.Task] = {}
_seen_fp: dict[int, tuple] = {}    # artifact -> workspace fingerprint reconcile last read
_lock = threading.Lock()
_deleted: set[int] = set()          # artifacts deleted: a check still in flight must not pin them


def _dir(artifact_id: int) -> Path:
    from server.applications import desktop_render
    return desktop_render.state_dir() / "pins" / str(int(artifact_id))


def digest(html: str) -> str:
    from server.applications import desktop_render
    return desktop_render.digest(html)


def forget(artifact_id: int) -> bool:
    """The artifact is deleted: remove its pins directory. The path is built
    from the integer id under the pins root, so it cannot leave it; a link
    standing where the directory should be is removed as a link, and links
    inside it are removed, never followed. Never raises."""
    import shutil
    aid = int(artifact_id)
    try:
        d = _dir(aid)
        with _lock:
            _deleted.add(aid)       # an in-flight check lands on nothing (see _check_and_advance)
            for m in (_seq, _applied, _seen_fp):
                m.pop(aid, None)
            if d.is_symlink():
                d.unlink()
            elif d.is_dir():
                shutil.rmtree(d, ignore_errors=True)
            else:
                return False
        return True
    except Exception:  # noqa: BLE001 — a leftover pins dir must not fail a delete
        log.warning("could not remove the pins of artifact %d", aid, exc_info=True)
        return False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load(artifact_id: int) -> dict:
    try:
        meta = json.loads((_dir(artifact_id) / PIN_META).read_text())
        return meta if isinstance(meta, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}-{threading.get_ident()}")
    tmp.write_text(data, encoding="utf-8")
    os.replace(tmp, path)


def _save(artifact_id: int, meta: dict) -> None:
    _atomic_write(_dir(artifact_id) / PIN_META, json.dumps(meta))


def _prune(artifact_id: int, meta: dict) -> None:
    keep = {h.get("digest") for h in meta.get("history") or []}
    try:
        for p in _dir(artifact_id).glob("*.html"):
            if p.stem not in keep:
                p.unlink(missing_ok=True)
    except OSError:
        pass


def read_pin(artifact_id: int) -> Optional[dict]:
    """The worn version's record ({entry, digest, at, checked, reason?,
    origin}) or None."""
    meta = _load(artifact_id)
    hist = meta.get("history") or []
    if not hist:
        return None
    return {"entry": meta.get("entry"), **hist[0]}


def status(artifact_id: int) -> Optional[dict]:
    """What the pin last did, for the shell's notice and the Host's line:
    {state, message, at, pinned_at}. state: current | unchecked | held |
    fallback | none."""
    st = _load(artifact_id).get("status")
    return st if isinstance(st, dict) else None


def _set_status(meta: dict, state: str, message: str = "", *, newest: Optional[str] = None) -> None:
    hist = meta.get("history") or []
    meta["status"] = {"state": state, "message": message, "at": _now(),
                      "pinned_at": hist[0].get("at") if hist else None,
                      "newest": newest}


def set_status(artifact_id: int, state: str, message: str = "", *,
               newest: Optional[str] = None) -> None:
    with _lock:
        meta = _load(artifact_id)
        if not meta:
            meta = {"entry": None, "history": []}
        st = meta.get("status") or {}
        if st.get("state") == state and st.get("message") == message:
            return      # the same news is not news twice
        _set_status(meta, state, message, newest=newest)
        _save(artifact_id, meta)


def _verified(artifact_id: int, rec: dict) -> Optional[str]:
    try:
        html = (_dir(artifact_id) / f"{rec.get('digest')}.html").read_text(encoding="utf-8")
    except OSError:
        return None
    return html if digest(html) == rec.get("digest") else None


def pinned_html(artifact_id: int, entry: str) -> Optional[str]:
    """The worn bytes, verified against their record. A stored copy that does
    not match is never served: the newest version that does is, those that
    did not are dropped, and the status says so. None when nothing verifies
    (or nothing is pinned for this entry)."""
    with _lock:
        meta = _load(artifact_id)
        if meta.get("entry") != entry:
            return None
        hist = meta.get("history") or []
        for i, rec in enumerate(hist):
            html = _verified(artifact_id, rec)
            if html is None:
                continue
            if i:
                meta["history"] = hist[i:]
                _set_status(meta, "fallback",
                            f"The stored copy of the worn desktop did not match its record, so "
                            f"it is not shown; the desktop shows the version from {rec.get('at')} "
                            "instead.")
                _save(artifact_id, meta)
                log.warning("desktop pin for artifact %s failed its digest; fell back to %s",
                            artifact_id, rec.get("at"))
            return html
        if hist:
            meta["history"] = []
            _set_status(meta, "none",
                        "No stored version of the worn desktop matches its record, so none is "
                        "shown — the desktop is the default wallpaper until a version renders.")
            _save(artifact_id, meta)
        return None


def write_pin(artifact_id: int, entry: str, html: str, render: Optional[dict], *,
              origin: str) -> dict:
    """Wear `html`. `render` is the check's result (None when no check ran: a
    desktop worn before pins existed, a Go back with no pin of its own). A
    FAILED result is refused here too — failed bytes are never pinned."""
    if render and render.get("checked") and not render.get("ok"):
        raise ValueError("a failed render is never pinned")
    rec = {"digest": digest(html), "at": _now(), "origin": origin,
           "checked": bool(render and render.get("checked")),
           "reason": (render or {}).get("reason") if not (render and render.get("checked")) else None}
    if not rec["checked"] and (render or {}).get("outdated"):
        rec["outdated"] = True
    # Apps past the side of a phone screen, from the check that pinned these
    # bytes: wear_current says so again when it does not re-check them.
    off = int(((render or {}).get("phone") or {}).get("tiles_off") or 0)
    if rec["checked"] and off > 0:
        rec["tiles_off"] = off
    with _lock:
        _deleted.discard(int(artifact_id))   # wearing again: the id is alive (re-created)
        meta = _load(artifact_id)
        hist = [h for h in (meta.get("history") or []) if h.get("digest") != rec["digest"]] \
            if meta.get("entry") == entry else []
        _atomic_write(_dir(artifact_id) / f"{rec['digest']}.html", html)   # bytes first
        meta = {"entry": entry, "history": ([rec] + hist)[:KEEP], "pinned_once": True,
                "status": meta.get("status")}
        if rec["checked"]:
            _set_status(meta, "current")
        else:
            # A check that RAN and could not answer is the first try: the
            # shell's next poll must not ask again at once (it did on a live
            # Host — the set and "asking again" in the same second).
            # No check ran (render None): the caller starts one now.
            meta["recheck"] = {"tries": 1 if render is not None else 0, "last": time.time()}
            _set_status(meta, "unchecked", unchecked_message(rec["reason"], rec.get("outdated")))
        _save(artifact_id, meta)
        _prune(artifact_id, meta)
    # Worn again: a reload report that took these bytes off is history now
    # (desktop_render.reload_report). If they loop again, the shell says so.
    from server.applications import desktop_render
    desktop_render.forget_reload_report(html)
    return {"entry": entry, **rec}


def unchecked_message(reason: Optional[str], outdated: bool = False) -> str:
    """The notice for a version worn WITHOUT a render check: why, and when it
    is asked again — which is true, not hopeful (the old text promised a
    check "when a browser can be woken" while the browser WAS woken, every
    300 s, and answered 404)."""
    text = ("The desktop shows its newest version WITHOUT a render check — "
            + str(reason or "no browser could be asked") + ".")
    if outdated:
        return text + (" This Host's browser is too old to run the check, so it is not asked "
                       "again until the browser is updated; wearing the desktop again asks at "
                       "once.")
    return text + (" It is asked again later — each try that cannot run waits twice as long "
                   "as the one before, up to 6 hours — and at once when the desktop is worn "
                   "again.")


def _recheck_wait(tries: int) -> float:
    return min(RECHECK_S * (2 ** min(max(0, int(tries) - 1), 32)), RECHECK_MAX_S)


def _recheck_due(meta: dict) -> bool:
    """Is the worn version — unchecked — due to be asked about again? Not
    while the browser is known to have no render check and its image has not
    changed (that answer cannot change by waiting); otherwise after the
    doubling wait since the last try. A pin with no record of a try (worn
    before this was kept) is due once."""
    hist = meta.get("history") or []
    if not hist or hist[0].get("checked"):
        return False
    from server.applications import desktop_render
    if desktop_render.sidecar_outdated() is not None:
        return False
    rc = meta.get("recheck") or {}
    tries = int(rc.get("tries") or 0)
    if tries <= 0:
        return True
    return time.time() - float(rc.get("last") or 0.0) >= _recheck_wait(tries)


def _note_unchecked_try(artifact_id: int, render: dict) -> None:
    """A re-check of the worn, unchecked version could not run again: count
    the try (the wait doubles), and when the answer is that the browser has
    no render check, say so once in the notice. The pin stamp is untouched,
    so the shell does not remount."""
    with _lock:
        meta = _load(artifact_id)
        hist = meta.get("history") or []
        if not hist:
            return
        rc = meta.get("recheck") or {}
        meta["recheck"] = {"tries": int(rc.get("tries") or 0) + 1, "last": time.time()}
        if render.get("outdated") and not hist[0].get("outdated"):
            hist[0]["outdated"] = True
            hist[0]["reason"] = render.get("reason") or hist[0].get("reason")
            _set_status(meta, "unchecked", unchecked_message(hist[0]["reason"], True))
        _save(artifact_id, meta)


def _drop_current(artifact_id: int, why: str, *,
                  what: str = "failed its render check") -> None:
    """Take the worn version off the screen: it failed (it was worn without a
    check, or before checks existed), or the shell reported it looping. The
    version before it, if one verifies, is worn instead; otherwise nothing
    is. `what` says which, in the notice."""
    with _lock:
        meta = _load(artifact_id)
        hist = (meta.get("history") or [])[1:]
        meta["history"] = hist
        back = next((h for h in hist if _verified(artifact_id, h) is not None), None)
        if back is not None:
            meta["history"] = hist[hist.index(back):]
            _set_status(meta, "held",
                        f"The version on the desktop {what}, so it was taken "
                        f"off; the desktop shows the version from {back.get('at')}. {why}")
        else:
            meta["history"] = []
            _set_status(meta, "none",
                        f"The version on the desktop {what}, so it was taken "
                        f"off and the desktop is the default wallpaper until a version renders. {why}")
        _save(artifact_id, meta)
        _prune(artifact_id, meta)


LOOP_WHY = ("the document reloads itself over and over — the shell counted more than 3 "
            "loads of its frame in 45 seconds, and a desktop never reloads itself")
# The notice for a version the shell's report took off. Only the shell saw
# the loop, so the notice says so and the bytes are not recorded as failed.
LOOP_WHAT = "was reported reloading itself over and over (unconfirmed)"
LOOP_NOTE = ("It was taken off after a reload report (unconfirmed): the shell counted more "
             "than 3 loads of its frame in 45 seconds, and a desktop never reloads itself. "
             "No browser has confirmed the loop, so wearing it again checks it again.")


# A version must have been on screen this long (the server's clock, from its
# pin stamp) before a reload-loop report can take it off. A real loop of any
# period shows its fourth load within seconds of 3 x period, and the shell
# reports again on every further load, so a slow report is only delayed; a
# forged one cannot roll the history back faster than this.
LOOP_MIN_WORN_S = 10.0


def take_off_reloading(artifact_id: int, entry: str, stamp: Optional[str]) -> bool:
    """The shell saw the worn desktop's frame load more than 3 times in 45 s
    (a reload loop the render check, which watches for ~2 s, cannot see).
    Take it off as a failed check does: the version before it, if one
    verifies, is worn instead, and a notice is raised.

    The report is the shell's word alone, and any page framed on the
    desktop's origin can send it, so the bytes are NOT remembered as failed
    (desktop_render.remember_failure would refuse them at every later wear
    no browser can check). They are recorded as taken off after a reload
    report, unconfirmed (desktop_render.remember_reload_report): the shell's
    reconcile does not put them back on its own, and wearing them again runs
    the normal check, or, with no browser, wears them with a note saying so.

    `stamp` is the pin stamp the shell was showing; a report about a version
    that is no longer the worn one changes nothing. True when it came off."""
    cur = read_pin(artifact_id)
    if cur is None or cur.get("entry") != entry or not stamp or cur.get("at") != stamp:
        return False
    try:
        worn_s = (datetime.now(timezone.utc) - datetime.fromisoformat(str(cur["at"]))).total_seconds()
    except (KeyError, ValueError):
        return False
    if worn_s < LOOP_MIN_WORN_S:
        return False                  # not on screen long enough to have looped
    html = pinned_html(artifact_id, entry)
    if html is not None:
        from server.applications import desktop_render
        desktop_render.remember_reload_report(html, LOOP_WHY)
    _drop_current(artifact_id, LOOP_NOTE, what=LOOP_WHAT)
    return True


def pin_stamp(artifact_id: int, entry: str) -> Optional[str]:
    """The change signal the shell compares (its remount trigger): when the
    pin last moved. None when there is no pin for this entry."""
    meta = read_pin(artifact_id)
    return meta.get("at") if meta and meta.get("entry") == entry else None


async def worn_html(pool, entity_id: int, artifact_id: int, entry: str) -> str:
    """The document the user wears. Raises ArtifactError(404) when nothing
    that may be worn exists — the shell then shows the default wallpaper.

    A desktop worn before pins existed has none: what is on screen IS its
    live render, so that is worn as it stands — unless those bytes already
    FAILED — and checked at once in the background, coming off the screen if
    it fails."""
    from server.applications import artifacts as artifact_service
    from server.applications import desktop_render
    from server.applications.artifacts import ArtifactError
    html = pinned_html(artifact_id, entry)
    if html is not None:
        return html
    meta = _load(artifact_id)
    if meta.get("pinned_once") and meta.get("entry") == entry:
        raise ArtifactError(404, (status(artifact_id) or {}).get("message")
                            or "nothing that passed the render check can be shown")
    live = await artifact_service.render(pool, entity_id, int(artifact_id), entry, exact=True)
    known = desktop_render.known_failure(live)
    if known is not None:
        set_status(artifact_id, "none",
                   f"This desktop failed its render check ({known.get('at')}): "
                   f"{known.get('why')}. Nothing is shown until a version renders.",
                   newest=digest(live))
        raise ArtifactError(404, "this desktop failed its render check: " + str(known.get("why")))
    try:
        write_pin(artifact_id, entry, live, None, origin="legacy")
        _start(pool, int(artifact_id), entry, live, check_kw={"ask_outdated": False})
    except OSError as e:
        log.info("desktop pin for artifact %s not written: %s", artifact_id, e)
    return live


async def worn_entry(pool, entity_id: int, artifact_id: int) -> Optional[str]:
    """The entry when `artifact_id` is the desktop on screen (the pointer, with
    the HTML desktop selected for either viewport); else None."""
    try:
        row = await pool.fetchrow(
            "SELECT wallpaper, wallpaper_mobile, desktop_artifact_id, desktop_entry "
            "FROM entity_appearance WHERE entity_id = $1", entity_id)
    except Exception as e:  # noqa: BLE001 — a write never fails on this read
        log.debug("worn desktop unreadable: %s", e)
        return None
    if row is None or row["desktop_artifact_id"] is None:
        return None
    if int(row["desktop_artifact_id"]) != int(artifact_id):
        return None
    if "html" not in (row["wallpaper"], row["wallpaper_mobile"]):
        return None
    return row["desktop_entry"] or "index.html"


def _baseline_from_history(artifact_id: int, entry: str) -> Optional[str]:
    """For a worn desktop with no pin yet: the render as it stood BEFORE the
    write that just landed — the revision before the newest one.

    The revision id comes from the log, a file in the workspace, so it is
    checked as render_history checks one: it must be revision-shaped
    (_REV_RE), and its folder must be reached from the workspace without a
    link (open_tree_no_escape within .history/<rev>). Otherwise there is no
    baseline."""
    from server.applications import artifacts as artifact_service
    from server.applications.tree_walk import TreeEscape, open_tree_no_escape
    ws = artifact_service._projects_root() / str(int(artifact_id))
    entries = artifact_service._history_log(ws)
    if len(entries) < 2:
        return None
    rev = str(entries[-2].get("rev") or "")
    if not artifact_service._REV_RE.match(rev):
        return None
    try:
        open_tree_no_escape(ws, within=f"{artifact_service._HISTORY_DIR}/{rev}").close()
    except TreeEscape:
        return None
    rev_dir = artifact_service._history_root(ws) / rev
    try:
        return artifact_service.render_workspace(rev_dir, entry)
    except Exception:  # noqa: BLE001
        return None


async def _notify_shell() -> None:
    try:
        from server.routes.appearance import _notify_shell as _n
        await _n()
    except Exception:  # noqa: BLE001
        pass


def _held_message(render: dict) -> str:
    from server.applications import desktop_render
    return ("This change is NOT on the desktop: the desktop stays at the last version that "
            "rendered.\n" + desktop_render.format_render(render))


async def _check_and_advance(pool, artifact_id: int, entry: str, html: str, seq: int,
                             check_kw: Optional[dict] = None) -> dict:
    from server.applications import desktop_render
    render = await desktop_render.render_check(pool, html, **(check_kw or {}))
    summary = desktop_render.summary(render)
    if int(artifact_id) in _deleted:
        # The artifact was deleted while this check ran: writing a pin or a
        # status now would recreate the directory delete_artifact removed.
        return {"worn": False, "advanced": False, "deleted": True, "render": summary}
    if seq < _applied.get(artifact_id, 0):
        # A newer edit's answer is already on screen; this older one must not
        # replace it.
        return {"worn": True, "advanced": False, "superseded": True, "render": summary}
    cur = read_pin(artifact_id)
    if render.get("checked") and not render.get("ok"):
        desktop_render.remember_failure(html, render)
        if cur is not None and cur.get("digest") == digest(html) and cur.get("entry") == entry:
            # The bytes ON SCREEN are the ones that failed — worn before
            # checks existed, or advanced while no browser could be asked.
            _drop_current(artifact_id, str(render.get("why") or ""))
            _applied[artifact_id] = seq
            await _notify_shell()
            return {"worn": True, "advanced": False, "taken_off": True, "render": summary,
                    "message": ("The version on the desktop failed its render check and was "
                                "taken off.\n" + desktop_render.format_render(render))}
        if cur is None:
            set_status(artifact_id, "none",
                       f"The newest edit does not render either: {render.get('why')}. "
                       "Nothing is shown until a version renders.", newest=digest(html))
        else:
            set_status(artifact_id, "held",
                       f"The newest edit is NOT on the desktop — it does not render: "
                       f"{render.get('why')}. The desktop shows the version from "
                       f"{cur.get('at')}.", newest=digest(html))
        return {"worn": True, "advanced": False, "render": summary,
                "message": _held_message(render)}
    if (not render.get("checked") and cur is not None and not cur.get("checked")
            and cur.get("digest") == digest(html) and cur.get("entry") == entry):
        # The periodic re-check of an unchecked worn version found no browser
        # again. The screen is unchanged, so no pin is written: a fresh pin
        # stamp would remount the desktop and the notice would fire again.
        # The try is counted, so the next one waits twice as long.
        _note_unchecked_try(artifact_id, render)
        _applied[artifact_id] = seq
        return {"worn": True, "advanced": False, "unchanged": True, "render": summary}
    write_pin(artifact_id, entry, html, render, origin="edit")
    _applied[artifact_id] = seq
    await _notify_shell()
    out = {"worn": True, "advanced": True, "render": summary}
    if not render.get("checked") or render.get("warnings"):
        # Not checked, or checked with something to fix in this turn (tiles
        # past the edge of a phone screen) — said where the Host reads it.
        out["warning"] = desktop_render.format_render(render)
    return out


def _start(pool, artifact_id: int, entry: str, html: str, *,
           check_kw: Optional[dict] = None) -> asyncio.Task:
    """Run the check in the background. `check_kw` goes to render_check:
    {ask_outdated: False} for a path no person is waiting on (a browser known
    to have no render check is not woken for it), {linger_s: 0} for the
    background re-check (nothing else is coming, so the browser goes back to
    sleep as soon as it answers)."""
    seq = _seq[artifact_id] = _seq.get(artifact_id, 0) + 1
    task = asyncio.ensure_future(_check_and_advance(pool, artifact_id, entry, html, seq,
                                                    check_kw))
    _inflight[artifact_id] = task
    task.add_done_callback(
        lambda t, a=artifact_id: _inflight.pop(a, None) if _inflight.get(a) is t else None)
    return task


async def after_write(pool, entity_id: int, artifact_id: int, *,
                      wait_s: Optional[float] = None) -> Optional[dict]:
    """Called by every artifact write after it lands. None when the artifact
    is not the worn desktop. Otherwise the check's verdict on the new render
    (waited for up to `wait_s`), or {pending: True, job} when it is still
    running — the job's answer is the same dict."""
    entry = await worn_entry(pool, entity_id, artifact_id)
    if entry is None:
        return None
    from server.applications import artifacts as artifact_service
    from server.applications import desktop_render
    try:
        html = await artifact_service.render(pool, entity_id, int(artifact_id), entry,
                                             exact=True)
    except Exception as e:  # noqa: BLE001 — an unrenderable write keeps the pin
        return {"worn": True, "advanced": False,
                "message": f"This change is NOT on the desktop: it does not render ({e})."}
    meta = read_pin(artifact_id)
    if meta is None or meta.get("entry") != entry:
        base = _baseline_from_history(artifact_id, entry)
        if base is not None:
            # Rendered as every other pin is (artifact_service.render): the
            # Host's own /api/images names become the images themselves. The
            # pin is what the shell shows and what publishing the worn desktop
            # sends to the shelf, where nobody can reach this Host's gallery.
            base = await artifact_service.inline_own_images(pool, entity_id, base)
        if base is not None and desktop_render.known_failure(base) is None \
                and not _load(artifact_id).get("pinned_once"):
            write_pin(artifact_id, entry, base, None, origin="legacy")
            meta = read_pin(artifact_id)
    if meta is not None and meta.get("entry") == entry and meta.get("digest") == digest(html):
        return {"worn": True, "advanced": False, "unchanged": True}
    # A write is not a wear: a browser known to have no render check is not
    # woken for each one (every Host write to the worn desktop paid a wake for
    # a 404). The write lands NOT RENDER-CHECKED, says why,
    # and a wear still asks.
    task = _start(pool, int(artifact_id), entry, html, check_kw={"ask_outdated": False})
    wait = desktop_render.DOOR_BUDGET_S if wait_s is None else wait_s
    done, _ = await asyncio.wait({task}, timeout=wait)
    if done:
        return task.result()
    job_id, _t = desktop_render.start_job("worn", lambda t=task: _await(t))
    return {"worn": True, "pending": True, "job": job_id,
            "poll": f"/api/appearance/desktop/jobs/{job_id}",
            "message": "The desktop shows this change once it renders in a browser."}


async def _await(task: asyncio.Task):
    return await asyncio.shield(task)


def _workspace_fingerprint(artifact_id: int) -> tuple:
    """(path, size, mtime) of every snapshot-able file, read through the
    contained walk (tree_walk.py): a link in the workspace is never followed,
    so a swapped-in link cannot make this poll stat a file outside it."""
    from server.applications import artifacts as artifact_service
    from server.applications.tree_walk import open_tree_no_escape
    ws = artifact_service._projects_root() / str(int(artifact_id))
    out = []
    try:
        with open_tree_no_escape(ws) as tree:
            for f in artifact_service._workspace_files(tree):
                out.append((f.rel, f.size, f.mtime))
    except OSError:
        return ()                 # no workspace (or its root is a link): nothing to fingerprint
    return tuple(out)


async def reconcile(pool, entity_id: int, artifact_id: int, entry: str) -> None:
    """The catch-all for writers that do not call after_write, driven by the
    shell's own poll. Cheap when nothing moved (file sizes and mtimes).
    Otherwise a worn artifact whose render differs from its pin is checked in
    the background — unless those bytes already FAILED, which are never asked
    about again — and a pin worn WITHOUT a check is asked about again every
    RECHECK_S. Whatever it decides lands in status(). Never raises."""
    try:
        aid = int(artifact_id)
        if aid in _inflight:
            return
        cur = read_pin(aid)
        if cur is not None and not cur.get("checked"):
            # Cheap and rate-limited: lets a new browser image clear an
            # "outdated" record without waking anything.
            from server.applications import desktop_render as _dr
            await _dr.refresh_browser_image()
        recheck = cur is not None and not cur.get("checked") and _recheck_due(_load(aid))
        fp = _workspace_fingerprint(aid)
        if _seen_fp.get(aid) == fp and not recheck:
            return
        _seen_fp[aid] = fp
        meta = _load(aid)
        if meta.get("entry") != entry or (cur is None and not meta.get("pinned_once")):
            return                      # worn_html decides a desktop never pinned
        from server.applications import artifacts as artifact_service
        from server.applications import desktop_render
        html = await artifact_service.render(pool, entity_id, aid, entry, exact=True)
        if cur is not None and cur.get("digest") == digest(html):
            if recheck:
                log.info("worn desktop %d was worn without a render check — asking again", aid)
                # Nothing else is coming: release the browser as soon as it
                # answers, and never wake one known to
                # have no render check (_recheck_due already said so).
                _start(pool, aid, entry, html,
                       check_kw={"linger_s": 0.0, "ask_outdated": False})
            return
        known = desktop_render.known_failure(html)
        if known is not None:
            if cur is None:
                set_status(aid, "none",
                           f"The newest edit failed its render check ({known.get('at')}): "
                           f"{known.get('why')}. Nothing is shown until a version renders.",
                           newest=digest(html))
            else:
                set_status(aid, "held",
                           f"The newest edit is NOT on the desktop — it failed its render check "
                           f"({known.get('at')}): {known.get('why')}. The desktop shows the "
                           f"version from {cur.get('at')}.", newest=digest(html))
            return
        reported = desktop_render.reload_report(html)
        if reported is not None:
            # Taken off after the shell's reload report (unconfirmed): not a
            # failure, but not put back by this poll either. Its ~2 s check
            # cannot see a slow loop, so a pass would only wear the loop
            # again. A wear asks again, with the check or the note.
            gone = (f"The newest version is NOT on the desktop — it was taken off after a "
                    f"reload report (unconfirmed, {reported.get('at')}). Wear it again to "
                    "have it checked.")
            if cur is None:
                set_status(aid, "none", gone + " Nothing is shown until a version is worn.",
                           newest=digest(html))
            else:
                set_status(aid, "held", gone + f" The desktop shows the version from "
                           f"{cur.get('at')}.", newest=digest(html))
            return
        log.info("worn desktop %d changed outside the artifact ops — checking it", aid)
        _start(pool, aid, entry, html, check_kw={"ask_outdated": False})
    except Exception as e:  # noqa: BLE001
        log.debug("desktop reconcile skipped: %s", e)


async def wear_unchecked(pool, entity_id: int, artifact_id: int, entry: str, html: str, *,
                         origin: str) -> dict:
    """Wear `html` with no check of its own — ONLY to put back a desktop
    that has no pin (Go back to one worn before pins existed). Bytes that
    already FAILED are refused (422); anything else is worn NOT
    RENDER-CHECKED and checked at once in the background."""
    from server.applications import desktop_render
    from server.applications.artifacts import ArtifactError
    known = desktop_render.known_failure(html)
    if known is not None:
        raise ArtifactError(422, desktop_render.format_render(desktop_render.remembered_result(known)))
    write_pin(int(artifact_id), entry, html, None, origin=origin)
    _start(pool, int(artifact_id), entry, html)
    return {"pinned": True, "checked": False}


async def wear_current(pool, entity_id: int, artifact_id: int, entry: str) -> dict:
    """Selecting the HTML desktop again (Settings → "Host design") is a WEAR:
    the artifact's CURRENT render is checked and pinned on a pass, so edits
    made while another wallpaper was showing are not left behind on a stale
    version. A failure keeps the last version that passed on screen and says
    why; with no version that ever passed, it is refused like any wear (422).
    No browser: pinned with NOT RENDER-CHECKED, as set and wear do — unless
    those exact bytes already failed."""
    from server.applications import artifacts as artifact_service
    from server.applications import desktop_render
    from server.applications.artifacts import ArtifactError
    html = await artifact_service.render(pool, entity_id, int(artifact_id), entry, exact=True)
    have_pin = pinned_html(int(artifact_id), entry) is not None
    cur = read_pin(artifact_id)
    if have_pin and cur and cur.get("digest") == digest(html):
        out = {"pinned": True, "current": True}
        # Not checked again, but the check that pinned it saw apps off a phone.
        if int(cur.get("tiles_off") or 0) > 0:
            out["warning"] = _off_a_phone(int(cur["tiles_off"]))
        return out
    render = await desktop_render.render_check(pool, html)
    summary = desktop_render.summary(render)
    if render.get("checked") and not render.get("ok"):
        desktop_render.remember_failure(html, render)
        if not have_pin:
            raise ArtifactError(422, desktop_render.format_render(render))
        set_status(int(artifact_id), "held",
                   f"The newest edit is NOT on the desktop — it does not render: "
                   f"{render.get('why')}. The desktop shows the version from "
                   f"{(cur or {}).get('at')}.", newest=digest(html))
        return {"pinned": False, "render": summary,
                "message": ("Your desktop shows the last version that rendered — the "
                            "newest edit does not.\n" + desktop_render.format_render(render))}
    write_pin(int(artifact_id), entry, html, render, origin="wear")
    out = {"pinned": True, "render": summary}
    if not render.get("checked"):
        out["warning"] = desktop_render.format_render(render)
    elif int((render.get("phone") or {}).get("tiles_off") or 0) > 0:
        # The person chose this in Settings, so it is said in their words,
        # not the Host's: the design is worn, and on a phone some apps sit
        # off the screen until the Host gives it a phone layout.
        out["warning"] = _off_a_phone(int(render["phone"]["tiles_off"]))
    return out


def _off_a_phone(n: int) -> str:
    # The taskbar's start button (dashboard/base/index.html #btn-menu) shows
    # only the logo; "Apps" is its tooltip, which a touch screen never shows.
    return (f"Worn. On a phone, {n} of this design's apps sit off the side of the screen — "
            "ask your Host to give it a phone layout. Every app still opens from the logo "
            "button at the left end of the taskbar.")
