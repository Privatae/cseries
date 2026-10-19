#!/usr/bin/env python3
"""Mutation runner for the desktop render check — the claim "the tests catch
it" made reproducible.

Each mutation below breaks ONE rule the render check, the worn-desktop pin or
a door depends on, in a COPY of this tree (the working tree is never touched),
and runs the suites that are supposed to notice. A mutation the suites do not
notice is a rule nothing proves. Run it before claiming the check is covered,
and after changing any of the files it names.

    python3 scripts/mutate_desktop_render.py            # every mutation
    python3 scripts/mutate_desktop_render.py M3 M17     # just these
    python3 scripts/mutate_desktop_render.py --list

Prints one line per mutation (CAUGHT by which suite, or SURVIVED) and exits 1
when any mutation survives that is not in KNOWN_SURVIVORS, each of which
carries the measured reason it cannot be caught. Needs Chromium (playwright)
for the `chromium` and `preview` suites; a whole run is ~20-30 minutes.

Not a gate (it runs the gates, many times over). The mutation list mixes the
2026-10-01 independent review's (tmp-review/render/mutate.py, M01-M24 there)
with the ones written alongside the code.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DV = "sidecars/browser/control/document_verify.py"
DR = "server/applications/desktop_render.py"
PIN = "server/applications/desktop_pin.py"
DES = "server/routes/designs.py"
APP = "server/routes/appearance.py"
PRE = "dashboard/base/js/desktop-preview.js"
SURF = "dashboard/base/js/desktop-surface.js"
REV = "server/routes/review.py"
TOOLS = "brain/mastercode/runtime/tools.py"
PS = "brain/mastercode/runtime/prompt_schema.py"

_PY = [sys.executable]
SUITES = {
    "doors": _PY + ["scripts/test_desktop_render_doors.py"],
    "pin": _PY + ["scripts/test_desktop_worn_pin.py"],
    "lr": _PY + ["scripts/test_long_request_routes.py"],
    "land": _PY + ["scripts/test_run_landing_truth.py"],
    "wsfile": _PY + ["scripts/test_project_dir_and_host_writes.py"],
    "chromium": _PY + ["-m", "pytest", "-p", "no:cacheprovider", "-q", "-x",
                       "tests/test_desktop_render_check.py"],
    "preview": _PY + ["-m", "pytest", "-p", "no:cacheprovider", "-q", "-x",
                      "tests/test_desktop_preview_js.py"],
    "shelf": _PY + ["-m", "pytest", "-p", "no:cacheprovider", "-q", "-x",
                    "tests/test_designs_shelf.py"],
    "artwin": _PY + ["-m", "pytest", "-p", "no:cacheprovider", "-q", "-x",
                     "tests/test_artifacts_use_as_desktop_js.py"],
    "designwin": _PY + ["-m", "pytest", "-p", "no:cacheprovider", "-q", "-x",
                        "tests/test_designs_try_pending_js.py"],
    "loopguard": _PY + ["-m", "pytest", "-p", "no:cacheprovider", "-q", "-x",
                        "tests/test_desktop_reload_guard_js.py"],
    "secwin": _PY + ["-m", "pytest", "-p", "no:cacheprovider", "-q", "-x",
                     "tests/test_security_scan_job_js.py"],
    "links": _PY + ["scripts/test_folder_readers_never_follow_links_out.py"],
}
ART = "server/applications/artifacts.py"
AV = "brain/mastercode/runtime/app_verify.py"
LRQ = "server/applications/long_request.py"

# (id, what it breaks, file, old, new, occurrence, suites). `occurrence` picks
# which match to change when `old` appears more than once (None = it must be
# unique).
M = [
    # ── the judge ───────────────────────────────────────────────────────────
    ("M01", "judge ignores uncaught errors", DR,
     "    failures += [_error_sentence(e) for e in errors[:3]]\n", "    pass\n", None, ["chromium"]),
    ("M02", "judge: whole-frame paint threshold off", DR,
     '< MIN_PAINTED_SHARE and not has_content:', '< 0 and not has_content:', None, ["chromium"]),
    ("M03", "judge: no app tile is not a failure", DR,
     'if "tiles" in drawn and int(drawn.get("tiles") or 0) < 1 \\\n',
     'if "tiles" in drawn and int(drawn.get("tiles") or 0) < 0 \\\n', None, ["chromium"]),
    ("M04", "judge: an undrawn bar is not a failure", DR,
     'and int(drawn.get("bar_strip") or 0) < 1 \\\n', 'and int(drawn.get("bar_strip") or 0) < 0 \\\n',
     None, ["chromium"]),
    ("M05", "judge ignores a lock-up after painting", DR,
     '        failures.append(str(report["unresponsive"]))\n', '        pass\n', None, ["chromium"]),
    ("M06", "judge: a page that does not load is not a failure", DR,
     '            failures.append("the document did not load — "',
     '            ("the document did not load — "', None, ["chromium"]),
    ("M07", "judge ignores what a CSeriesDesktop.on callback threw", DR,
     "+ _callback_errors(report)", "+ []", None, ["chromium"]),
    ("M08", "judge: a crash is not a failure", DR,
     '        failures.append(str(report["crashed"]))\n', '        pass\n', None, ["chromium"]),
    ("M09", "judge: a self-navigation is not a failure", DR,
     '        failures.append(\n            f"it navigated itself away after it loaded',
     '        (\n            f"it navigated itself away after it loaded', None, ["chromium"]),
    ("M10", "judge: covered tiles are not a failure", DR,
     'and int(reach.get("reachable") or 0) == 0:', 'and False:', None, ["chromium"]),
    ("M11", "judge: any content rectangle counts, painted or not", DR,
     "has_content = any(x >= CONTENT_MIN_PX for x in drawn_in)", "has_content = bool(drawn_in)",
     None, ["chromium"]),
    ("M12", "tiles must be SHOWN, not merely exist (refuses start menus)", DR,
     'out["tiles"] = {"selector": "[data-cseries-app]", "kind": "attached"}',
     'out["tiles"] = {"selector": "[data-cseries-app]", "kind": "shown"}', None, ["chromium"]),
    # ── the sidecar ─────────────────────────────────────────────────────────
    ("M13", "probes run in the document's MAIN world", DV,
     "world.call(_PROBE_JS, {k: v.model_dump() for k, v in body.probes.items()})",
     "frame.evaluate(_PROBE_JS, {k: v.model_dump() for k, v in body.probes.items()})",
     1, ["chromium"]),            # the end-of-settle probe (0 is the first look)
    ("M14", "probe: opacity 0 counts as shown", DV,
     "if (Number(getComputedStyle(n).opacity) === 0) return false;", "", 0, ["chromium"]),
    ("M15", "probe: visibility:hidden counts as shown", DV,
     "if (s.visibility === 'hidden' || s.visibility === 'collapse') return false;", "", 0,
     ["chromium"]),
    ("M16", "probe: the rectangle is ignored", DV,
     "if (r.right <= b[0] || r.bottom <= b[1] || r.left >= b[0] + b[2] || r.top >= b[1] + b[3]) continue;",
     "", None, ["chromium"]),
    ("M17", "probe: a click's target is never checked", DV,
     "if (hit && (hit === el || el.contains(hit))) { reach++; continue; }",
     "{ reach++; continue; }", None, ["chromium"]),
    ("M18", "no two-frame readiness wait", DV,
     "await asyncio.wait_for(world.call(_TWO_FRAMES_JS), STEP_TIMEOUT_S)", "pass", 1,
     ["chromium"]),
    ("M19", "no settle wait", DV,
     "await asyncio.sleep(body.settle_ms / 1000)", "pass", None, ["chromium"]),
    ("M20", "the page's own requests are let through", DV,
     'await route.abort("blockedbyclient")', "await route.continue_()", None, ["chromium"]),
    ("M21", "a crash is not recorded", DV,
     'page.on("crash", _on_crash)', "pass", None, ["chromium"]),
    ("M22", "a new document is not told from the same one (any commit is a navigation)", DV,
     "        if not (u == \"about:srcdoc\" or u.startswith(\"about:srcdoc#\")):",
     "        if True:", None, ["chromium"]),
    ("M22b", "a reload is not detected (the world is never asked)", DV,
     "                            or (world is not None and await world.gone())):",
     "                            or False):", None, ["chromium"]),
    ("M23", "the browser keeps its automation flag", DV,
     'CHECK_BROWSER_ARGS = ("--disable-blink-features=AutomationControlled",)',
     "CHECK_BROWSER_ARGS = ()", None, ["chromium"]),
    ("M24", "the stand-in swallows a callback's throw without logging it", PRE,
     "catch(e){L(e)}", "catch(e){}", None, ["preview", "chromium"]),
    # ── the doors ───────────────────────────────────────────────────────────
    ("M25", "publish proceeds when not checked", DES,
     "        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,\n"
     "                            desktop_render.format_render(render) + why)\n",
     "        pass\n", None, ["doors"]),
    ("M26", "set-desktop ignores a failed render", APP,
     "        raise ArtifactError(422, desktop_render.format_render(render))\n    warnings = list",
     "        pass\n    warnings = list", None, ["doors"]),
    ("M27", "Try skips its check before the copy", DES,
     "    if render.get(\"checked\") and not render.get(\"ok\"):\n"
     "        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,\n"
     "                            desktop_render.format_render(render))\n\n    previous",
     "\n    previous", None, ["doors"]),
    ("M28", "Go back runs the render check", DES,
     "                                          render_check=False)",
     "                                          render_check=True)", None, ["doors"]),
    ("M29", "Go back trusts the client's `previous`", DES,
     '    previous = rec["previous"]\n',
     '    previous = (body or {}).get("previous") or rec["previous"]\n', None, ["shelf"]),
    ("M30", "the desktop job route needs no scope", APP,
     '    dependencies=[Depends(requires_permission("write:appearance"))],\n)\nasync def desktop_job(',
     ')\nasync def desktop_job(', None, ["lr"]),
    ("M31", "set-desktop does not wear what it checked", APP,
     '        desktop_pin.write_pin(aid, entry, html, render, origin="set")', "        pass",
     None, ["doors"]),
    # ── remembered failures and the pin ─────────────────────────────────────
    ("M32", "a failed verdict is not remembered for later 'not checked'", DR,
     "    if known is not None:\n        return remembered_result(known, now=",
     "    if False:\n        return remembered_result(known, now=", None, ["doors", "pin"]),
    ("M33", "the pin acts on a failure without recording it", PIN,
     "        desktop_render.remember_failure(html, render)\n        if cur is not None",
     "        if cur is not None", None, ["pin"]),
    ("M34", "a failing edit advances anyway", PIN,
     '    if render.get("checked") and not render.get("ok"):\n        desktop_render.remember_failure(html, render)\n        if cur',
     '    if False:\n        desktop_render.remember_failure(html, render)\n        if cur', None,
     ["pin"]),
    ("M35", "an older check may roll the screen back", PIN,
     "    if seq < _applied.get(artifact_id, 0):", "    if False:", None, ["pin"]),
    ("M36", "reconcile never checks an outside write", PIN,
     '        log.info("worn desktop %d changed outside the artifact ops — checking it", aid)\n        _start(pool, aid, entry, html)',
     "        pass", None, ["pin"]),
    ("M37", "reconcile re-asks about bytes known to fail", PIN,
     "        known = desktop_render.known_failure(html)\n        if known is not None:\n            if cur is None:\n                set_status(aid, \"none\",",
     "        known = None\n        if known is not None:\n            if cur is None:\n                set_status(aid, \"none\",",
     None, ["pin"]),
    ("M38", "a version worn unchecked is never re-asked", PIN,
     "        recheck = (cur is not None and not cur.get(\"checked\")",
     "        recheck = (False", None, ["pin"]),
    ("M39", "worn bytes on screen that fail are not taken off", PIN,
     "        if cur is not None and cur.get(\"digest\") == digest(html) and cur.get(\"entry\") == entry:",
     "        if False:", None, ["pin"]),
    ("M40", "a stored pin is served without checking its digest", PIN,
     "    return html if digest(html) == rec.get(\"digest\") else None", "    return html", None,
     ["pin"]),
    ("M41", "the pin lives inside the artifact's workspace", PIN,
     '    return desktop_render.state_dir() / "pins" / str(int(artifact_id))',
     '    from server.applications import artifacts as _a\n'
     '    return _a._projects_root() / str(int(artifact_id)) / ".history" / "pin"', None, ["pin"]),
    ("M42", "a pre-check desktop with failed bytes is worn anyway", PIN,
     "    known = desktop_render.known_failure(live)\n    if known is not None:",
     "    known = desktop_render.known_failure(live)\n    if False:", None, ["pin"]),
    ("M43", "the shell is served the working copy, not the pin", APP,
     "        html = await desktop_pin.worn_html(\n            pool, brain_service._entity_id, int(desk[\"artifact_id\"]), desk[\"entry\"])\n    except ArtifactError as e:\n        raise HTTPException(e.status, e.detail) from None\n    await desktop_pin.reconcile(",
     "        html = await artifact_service.render(\n            pool, brain_service._entity_id, int(desk[\"artifact_id\"]), desk[\"entry\"])\n    except ArtifactError as e:\n        raise HTTPException(e.status, e.detail) from None\n    await desktop_pin.reconcile(",
     None, ["pin"]),
    ("M44", "the shell is not told what the pin did", APP,
     '    return {**desk, "pin": desktop_pin.status(int(desk["artifact_id"]))}', "    return desk",
     None, ["pin"]),
    ("M45", "the shell does not show the pin's notice", SURF,
     "            pinNotice(m && m.pin);\n", "\n", None, ["pin"]),
    ("M46", "the Host's appearance line omits what is on screen", PS,
     'if st.get("state") not in (None, "current") and st.get("message"):', "if False:", None,
     ["pin"]),
    # ── writers, and the Workbench ──────────────────────────────────────────
    ("M47", "the project file lane writes an artifact's .history/", TOOLS,
     '                if _kind == "artifact":\n                    return {"status": "error", "result": (\n                        f"{_HIST}/ is',
     '                if False:\n                    return {"status": "error", "result": (\n                        f"{_HIST}/ is',
     None, ["wsfile"]),
    ("M48", "a build land writes an artifact's .history/", REV,
     "                or any(p.lower() == \".git\" for p in parts)\n                or (_hist is not None and _hist in parts)):\n            if skipped_out",
     "                or any(p.lower() == \".git\" for p in parts)):\n            if skipped_out",
     None, ["land"]),
    ("M49", "the Workbench looks only at the tree's top level", DR,
     "        dirnames[:] = sorted(d for d in dirnames", "        dirnames[:] = [] or sorted(d for d in []",
     None, ["pin"]),
    ("M50", "the Workbench ignores the browser-proof switch", DR,
     "    if not await browser_proof_enabled(pool, entity_id):\n        return None",
     "    if False:\n        return None", None, ["pin"]),
    # ── wiring from the first two rounds ────────────────────────────────────
    ("M51", "artifact writes never reach the pin", ART,
     "        return await desktop_pin.after_write(pool, entity_id, int(artifact_id), wait_s=wait_s)",
     "        return None", None, ["pin"]),
    ("M52", "the shell's remount stamp follows the artifact, not the pin", APP,
     '        "updated_at": stamp or (art["updated_at"].isoformat() if art["updated_at"] else None),',
     '        "updated_at": art["updated_at"].isoformat() if art["updated_at"] else None,', None,
     ["pin"]),
    ("M53", "a full plan is not named", AV,
     '            if refusal:\n                out["over_plan"] = refusal',
     '            if False:\n                out["over_plan"] = refusal', None, ["doors"]),
    ("M54", "set-desktop: not checked refuses", APP,
     '    if render is not None and render.get("checked") and not render.get("ok"):',
     '    if render is not None and not render.get("ok"):', None, ["doors"]),
    ("M55", "set-desktop: no warning when not checked", APP,
     '        warnings.append(desktop_render.format_render(render))\n', '        pass\n', None,
     ["doors"]),
    ("M56", "set-desktop: the render check never runs", APP,
     '        render = await desktop_render.render_check(pool, html)\n', '        render = None\n', None,
     ["doors"]),
    ("M57", "Artifacts window: a 202 is not followed", "system-apps/artifacts/app.js",
     "        if (r.status !== 202 || !body || !body.poll) return { ok: r.ok, status: r.status, body };",
     "        return { ok: r.ok, status: r.status, body };", None, ["artwin"]),
    ("M58", "Artifacts window: a refusal is not shown", "system-apps/artifacts/app.js",
     "                showDesktopReport(detail, r.status === 422);", "", None, ["artwin"]),
    ("M59", "Designs window: a 202 is not followed", "system-apps/designs/app.js",
     "        if (r.status === 202 && body && body.poll) return settle(body);", "", None,
     ["designwin"]),
    # ── the router's window (long_request) ──────────────────────────────────
    ("M60", "a job is readable through any area", LRQ,
     'if job is None or job.get("area") != area:', "if job is None:", None, ["lr"]),
    ("M61", "a refusal after the 202 loses its status", LRQ,
     'job.update(status="failed", http_status=code,', 'job.update(status="failed", http_status=500,',
     None, ["lr"]),
    ("M62", "a door never answers early (always waits)", LRQ,
     "    done, _ = await asyncio.wait({task}, timeout=BUDGET_S if budget_s is None else budget_s)",
     "    done, _ = await asyncio.wait({task}, timeout=None)", None, ["lr"]),
    ("M63", "Circles: the body is not kept for the detached turn", "server/routes/circles.py",
     "    await request.body()\n    return await long_request.run(what",
     "    return await long_request.run(what", None, ["lr"]),
    ("M64", "a browser agent run waits past the router's window", "server/routes/browser_agents.py",
     "wait_s = min(body.wait_s if body else long_request.BUDGET_S, long_request.BUDGET_S)",
     "wait_s = body.wait_s if body else 120.0", None, ["lr"]),
    ("M65", "Security window: a pending scan is not followed", "system-apps/security/app.js",
     "const result = await settleJob(await fetchJSON('/api/security/scan', { method: 'POST' }));",
     "const result = await fetchJSON('/api/security/scan', { method: 'POST' });", None, ["secwin"]),
    # ── the 2026-10-01 second review's fixes ────────────────────────────────
    ("M66", "a no-browser re-check of the worn unchecked digest writes the pin again", PIN,
     '    if (not render.get("checked") and cur is not None and not cur.get("checked")\n',
     '    if False and (not render.get("checked") and cur is not None and not cur.get("checked")\n',
     None, ["pin"]),
    ("M67", "a document that reloads itself is a failure again", DV,
     "if attached[0] < _MAX_SELF_RELOADS:", "if False:", None, ["chromium"]),
    ("M68", "a reload loop is failed but not named as one", DV,
     'report["reload_loop"] = True ', 'pass ', None, ["chromium"]),
    ("M69", "tiles drawn at first and removed by the end of the settle pass", DR,
     'if int(first.get("tiles") or 0) > 0 and int(drawn.get("tiles") or 0) < 1:',
     'if False:', None, ["chromium"]),
    ("M70", "tiles shown at first and hidden by the end of the settle pass", DR,
     'and int((drawn.get("tiles_reach") or {}).get("shown") or 0) == 0:',
     'and False:', None, ["chromium"]),
    ("M71", "deleting an artifact leaves its pins", ART,
     "    desktop_pin.forget(artifact_id)", "    pass", None, ["pin"]),
    ("M72", "deleting an artifact follows a link inside its pins", PIN,
     "shutil.rmtree(d, ignore_errors=True)", "shutil.rmtree((d / 'link').resolve(), ignore_errors=True); shutil.rmtree(d, ignore_errors=True)",
     None, ["pin"]),
    ("M73", "a reloaded copy gets no settle of its own (the second settle is dropped)", DV,
     "await asyncio.sleep(body.settle_ms / 1000)",
     "await asyncio.sleep(0 if attached[0] else body.settle_ms / 1000)", None, ["chromium"]),
    ("M74", "any number of reloads is tolerated (no loop rule)", DV,
     "_MAX_SELF_RELOADS = 1 ", "_MAX_SELF_RELOADS = 99 ", None, ["chromium"]),
    ("M75", "a check in flight when its artifact is deleted still pins it", PIN,
     "            _deleted.add(aid) ", "            pass; (aid) ", None, ["pin"]),
    ("M76", "the shell never reports a frame that keeps loading", SURF,
     "if (f._skLoads.length <= LOOP_MAX_LOADS || f._skLoopReported) return;", "return;",
     None, ["loopguard"]),
    ("M77", "the shell's loop window is too short to see a slow loop", SURF,
     "const LOOP_WINDOW_MS = 45000;", "const LOOP_WINDOW_MS = 1;", None, ["loopguard"]),
    ("M78", "a copy that leaves again while it is read is not named a loop", DV,
     "if _context_destroyed(again):", "if False:", None, ["chromium"]),
    ("M79", "a reloaded copy gets a step's load allowance, not the first copy's", DV,
     'frame.wait_for_load_state("load"), LOAD_TIMEOUT_MS / 1000)',
     'frame.wait_for_load_state("load"), STEP_TIMEOUT_S)', None, ["chromium"]),
    ("M80", "a reload that times out is reported as a navigation", DV,
     "elif isinstance(again, asyncio.TimeoutError):", "elif False:", None, ["chromium"]),
    ("M81", "there is no overall deadline", DV,
     "if epoch and ms() > ROUND_DEADLINE_S * 1000:", "if False:", None, ["chromium"]),
    ("M82", "the deleted record is never cleared", PIN,
     "_deleted.discard(int(artifact_id))", "pass", None, ["pin"]),
    ("M83", "a loop report about a stale stamp takes the worn version off", PIN,
     'or not stamp or cur.get("at") != stamp:', "or False:", None, ["pin"]),
    ("M84", "the shell's reload report is stored as a durable failure (a later wear with no "
     "browser is refused on the report alone)", PIN,
     "        desktop_render.remember_reload_report(html, LOOP_WHY)\n",
     "        desktop_render.remember_failure(html, {\"checked\": True, \"ok\": False, "
     "\"why\": LOOP_WHY, \"failures\": [LOOP_WHY]})\n", None, ["pin"]),

    ("M85", "a report about a version just worn takes it off (no minimum worn time)", PIN,
     "    if worn_s < LOOP_MIN_WORN_S:", "    if False:", None, ["pin"]),
    ("M86", "the shell does not report again after a too-early refusal", SURF,
     "if (j && j.taken_off === false) f._skLoopReported = false;", "", None, ["loopguard"]),
    ("M87", "the Workbench's landed-desktop scan lists through the walk but reads by name "
     "(a file swapped for a link after the listing is followed out of the project)", DR,
     "            data = tree.read(f, limit=65536)\n",
     "            data = (Path(root) / f.rel).read_bytes()[:65536]\n", None, ["links"]),
    ("M88", "the pre-edit baseline reads a revision folder without the no-link open "
     "(a linked .history/<rev> is followed)", PIN,
     "    try:\n        open_tree_no_escape(ws, within=f\"{artifact_service._HISTORY_DIR}/{rev}\").close()\n"
     "    except TreeEscape:\n        return None\n",
     "", None, ["pin"]),
    # ── the phone (2026-10-10) ────────────────────────────────────────
    ("M89", "a document that passes at desktop size is never loaded as a phone", DR,
     '            if verdict is not None and verdict.get("checked") and verdict.get("ok"):\n',
     '            if False:\n', None, ["doors", "chromium"]),
    ("M90", "the phone look is mounted as a desktop (no CSeriesDesktop.phone)", DR,
     '    if phone:\n        opts["phone"] = True', '    if False:\n        opts["phone"] = True',
     None, ["doors", "chromium"]),
    ("M91", "a document broken on a phone passes", DR,
     '    if not phone.get("ok"):\n        failures = ', '    if False:\n        failures = ',
     None, ["doors", "chromium"]),
    ("M92", "tiles past the side of a phone screen are not warned", DR,
     "    if anywhere <= 0 or off <= 0:\n        return []", "    if True:\n        return []",
     None, ["doors", "chromium"]),
    ("M93", "set-desktop leaves the phone on its own wallpaper", APP,
     "                    wallpaper_mobile    = NULL,\n", "", None, ["doors"]),
    ("M94", "Go back does not put the phone's own wallpaper back", DES,
     "        result = await _restore_phone(pool, eid, previous, result, html_shown=True)",
     "        result = result", None, ["shelf"]),
    ("M95", "Go back drops the Host design the phone wore under a desktop swatch", DES,
     "    phone_html = await _restore_phone_pointer(pool, eid, previous)",
     "    phone_html = False", None, ["shelf"]),
    ("M96", "Go back does not tell the desktop once its writes have landed", DES,
     "    from server.routes.appearance import _notify_shell\n    await _notify_shell()",
     "    return None", None, ["shelf"]),
    ("M97", "Settings → Host design says nothing when apps run off a phone", PIN,
     '    elif int((render.get("phone") or {}).get("tiles_off") or 0) > 0:',
     "    elif False:", None, ["pin"]),
    ("M98", "Go back points the phone at its design again but leaves the Host Manager tile cleared",
     DES, '        await publish_desktop_snapshot(pool, reason="restored")', "        pass",
     None, ["shelf"]),
    ("M99", "Settings → Host design says nothing about apps off a phone when the pin is current",
     PIN, '        if int(cur.get("tiles_off") or 0) > 0:', "        if False:", None, ["pin"]),
    ("M100", "the pin forgets how many apps its check saw off a phone", PIN,
     '    if rec["checked"] and off > 0:\n        rec["tiles_off"] = off', "    pass",
     None, ["pin"]),
    ("M101", "Go back discards the trial copy the phone was just pointed back at", DES,
     '        if discard not in (previous.get("artifact_id"), previous.get("phone_artifact_id")) \\\n',
     '        if discard not in (previous.get("artifact_id"),) \\\n', None, ["shelf"]),
]

# Mutations the suites cannot catch, each with the measured reason. Empty is
# the goal; an entry needs a reason a reviewer can check.
KNOWN_SURVIVORS: dict[str, str] = {
    "M18": ("opening the isolated world (Page.createIsolatedWorld + a Runtime.evaluate in "
            "it) already needs the frame's renderer to answer, so a renderer locked on its "
            "first frame fails at that step even without the two-frame wait; measured "
            "2026-10-01 with the first-tick lock-up case"),
    "M22b": ("a reload that lands before the probes is already reported by the probes' own "
             "failure (their isolated world is gone: \"Cannot find context\"), which is what the "
             "reload cases hit; asking the world directly covers one landing after the probes "
             "and before the end, a window of milliseconds no test can aim at"),
}


def _copy_tree(dst: Path) -> None:
    shutil.copytree(ROOT, dst, symlinks=True,
                    ignore=shutil.ignore_patterns(".git", "node_modules", "__pycache__", "dist"))


def _valid(path: Path) -> tuple[bool, str]:
    """A mutation must still parse: a syntax error fails every suite and
    proves nothing about the rule it was meant to break."""
    if path.suffix == ".py":
        p = subprocess.run([sys.executable, "-m", "py_compile", str(path)],
                           capture_output=True, text=True)
    elif path.suffix == ".js" and shutil.which("node"):
        p = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
    else:
        return True, ""
    return p.returncode == 0, (p.stderr or p.stdout).strip().splitlines()[-1:] and \
        (p.stderr or p.stdout).strip().splitlines()[-1] or ""


def _run(tree: Path, suite: str) -> tuple[bool, str]:
    env = {**os.environ, "APP_ROOT": str(tree)}
    try:
        p = subprocess.run(SUITES[suite], cwd=tree, env=env, capture_output=True, text=True,
                           timeout=600)
    except subprocess.TimeoutExpired:
        return False, "timeout"
    out = re.sub(r"\x1b\[[0-9;]*m", "", p.stdout + p.stderr)       # colour codes
    first = next((ln.strip() for ln in out.splitlines()
                  if re.search(r"\bFAIL(?:ED)?\b", ln) and " ok " not in ln), f"exit {p.returncode}")
    return p.returncode == 0, first[:160]


def main(argv: list[str]) -> int:
    if "--list" in argv:
        for mid, what, path, *_ in M:
            print(f"{mid}  {what}  ({path})")
        return 0
    only = {a for a in argv if a.startswith("M")}
    with tempfile.TemporaryDirectory(prefix="mutate-desktop-render-") as td:
        tree = Path(td) / "tree"
        _copy_tree(tree)
        # Every suite must be GREEN unmutated, or "caught" means nothing.
        wanted = sorted({su for m in M if not only or m[0] in only for su in m[6]})
        for su in wanted:
            ok, why = _run(tree, su)
            if not ok:
                print(f"BASELINE RED: suite {su!r} fails with nothing mutated ({why}) — fix it first")
                return 2
        print(f"baseline green: {', '.join(wanted)}", flush=True)
        unexpected = []
        for mid, what, rel, old, new, nth, suites in M:
            if only and mid not in only:
                continue
            path = tree / rel
            src = path.read_text()
            n = src.count(old)
            if (nth is None and n != 1) or (nth is not None and n <= nth):
                print(f"{mid} PATTERN NOT FOUND ({n}x) — {what}: update this mutation")
                unexpected.append(mid)
                continue
            i = -1
            for _ in range((nth or 0) + 1):
                i = src.index(old, i + 1)
            path.write_text(src[:i] + new + src[i + len(old):])
            ok, why = _valid(path)
            if not ok:
                path.write_text(src)
                print(f"{mid} INVALID  {what}: the mutated file does not parse ({why[:120]})")
                unexpected.append(mid)
                continue
            try:
                caught = []
                for s in suites:
                    ok, why = _run(tree, s)
                    if not ok:
                        caught.append(f"{s}: {why}")
                        break
            finally:
                path.write_text(src)
            if caught:
                print(f"{mid} CAUGHT   {what}  [{caught[0]}]", flush=True)
            elif mid in KNOWN_SURVIVORS:
                print(f"{mid} SURVIVED {what}  (known: {KNOWN_SURVIVORS[mid]})", flush=True)
            else:
                print(f"{mid} SURVIVED {what}", flush=True)
                unexpected.append(mid)
    if unexpected:
        print(f"\n{len(unexpected)} mutation(s) not caught: {' '.join(unexpected)}")
        return 1
    print("\nevery mutation caught (or a known survivor, with its reason)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
