"""The projects tools: their handlers and the helpers only they use.

Moved out of runtime/tools.py, which re-exports every name defined here. A tool's
category in the tool registry (public_tool_registry `_meta.category`) names the module
its handler lives in."""

import asyncio
import json
import logging
import re

from mastercode.runtime.tool_call_ledger import refused as _refused, refused_if_caller as _refused_if_caller
from mastercode.runtime.tools_build import (
    _PHASE_ARCHIVED, _PHASE_COLS, _PHASE_DONE, _PHASE_FAILED, _PHASE_OPEN_SQL, _PHASE_READY,
    _PHASE_RUNNING, _STEP_RUNNING, _acceptance_tool_refusal, _find_cycle, _normalize_acceptance,
    _normalize_gate, _phase_deps, _phase_validation_for,
)
from mastercode.runtime.tools_shared import _bytes_as_chunks, _read_window, _window_result

log = logging.getLogger("cseries.mastercode.entity_runtime")


# ── bench measurement reads (cost truth + run transcripts) ─────────────────

async def _exec_harness_usage_windows(args: dict, pool) -> dict:
    """Harness token consumption over the subscription refresh windows —
    session / 5h / 24h / 7d — from harness_usage_event (v114).

    This is the SUBSCRIPTION lane's answer to "what did that cost". A flat
    plan has no per-token price, so the meaningful number is not dollars but
    how much of a refresh window the work consumed, split cached vs uncached.

    It reports CONSUMPTION, never quota remaining: the vendor's plan size and
    reset boundary are not published to the platform, and inventing a
    denominator would turn a measured number into a confident wrong one."""
    try:
        clauses, params = ["1=1"], []
        sid = (args.get("session_id") or "").strip()
        harness = (args.get("harness") or "").strip()
        if harness:
            params.append(harness)
            # Fan-out variants persist as "<harness>-s2"/"-s3" (the variant
            # label is what keys a run), so an exact match on "kimi"
            # silently drops its own sampled frames. Match the base id or
            # any of its variants — the same base-id rule _USAGE_AGG uses.
            clauses.append(
                f"(harness = ${len(params)} OR harness LIKE ${len(params)} || '-s%')")
        windows = [("5h", 5 * 3600), ("24h", 24 * 3600), ("7d", 7 * 24 * 3600)]
        if sid:
            windows.insert(0, ("session", None))
        out = []
        for key, secs in windows:
            c, pr = list(clauses), list(params)
            if secs is None:
                pr.append(sid)
                c.append(f"session_id = ${len(pr)}")
            else:
                pr.append(secs)
                c.append(f"created_at > now() - make_interval(secs => ${len(pr)})")
            rows = await pool.fetch(
                f"""SELECT harness, COUNT(*) AS frames,
                           COALESCE(SUM(total_tokens),0) AS tokens,
                           COALESCE(SUM(input_tokens),0) AS input_tokens,
                           COALESCE(SUM(cached_input_tokens),0) AS cached_tokens,
                           ROUND(COALESCE(SUM(cost_usd),0)::numeric,6) AS vendor_cost_usd,
                           COUNT(*) FILTER (WHERE cost_usd > 0) AS cost_frames
                      FROM harness_usage_event
                     WHERE {' AND '.join(c)}
                     GROUP BY harness ORDER BY tokens DESC""", *pr)
            items = []
            for r in rows:
                inp = int(r["input_tokens"])
                # cost_usd is NOT NULL DEFAULT 0 (v114) and a frame stores 0
                # when the vendor sent no cost (compute._usage_frame keeps
                # only a cost > 0), so a sum of 0 over frames none of which
                # carried a cost is "not reported", never $0.00. The Host read
                # that 0 to the user as "the vendor charge is $0.00" three
                # times.
                reported = int(r["cost_frames"] or 0) > 0
                items.append({
                    "harness": r["harness"], "frames": int(r["frames"]),
                    "tokens": int(r["tokens"]), "input_tokens": inp,
                    "cached_tokens": int(r["cached_tokens"]),
                    "cached_share": (round(int(r["cached_tokens"]) / inp, 4)
                                     if inp else None),
                    "vendor_cost_usd": (float(r["vendor_cost_usd"] or 0)
                                        if reported else None),
                    "vendor_cost": (f"${float(r['vendor_cost_usd'] or 0):.4f} reported "
                                    f"by the vendor on {int(r['cost_frames'])} of "
                                    f"{int(r['frames'])} usage frames"
                                    if reported else
                                    "not reported — this harness's stream "
                                    "carried no cost, so the charge is unknown "
                                    "(not $0)")})
            out.append({"window": key, "harnesses": items,
                        "tokens": sum(i["tokens"] for i in items)})
        return {"status": "ok", "result": {
            "windows": out,
            "basis": "measured consumption from each run's own event stream",
            "quota_basis": ("unknown — the vendor's plan size and reset "
                            "boundary are not published to the platform, so "
                            "report consumption, never 'quota remaining'"),
        }}
    except Exception as e:
        return _refused_if_caller(e, f"harness_usage_windows failed: {e}")


async def _exec_harness_costs(args: dict, pool) -> dict:
    """Per-(harness, model) cost/token aggregate from harness_usage — every
    run is priced (meter-then-charge), so these are billed numbers, not
    estimates. Optional session_id / project_id filters, since_days window."""
    try:
        where, params = [], []
        sid = (args.get("session_id") or "").strip()
        if sid:
            params.append(sid)
            where.append(f"session_id = ${len(params)}")
        if args.get("project_id") is not None:
            params.append(int(args["project_id"]))
            where.append(f"project_id = ${len(params)}")
        # Cost for ONE phase of a plan — the unit the user actually
        # approved. Only the phased build door stamps phase_id (v112);
        # fan-outs and merges leave it NULL and are excluded by this
        # filter, which is correct: they are not part of a phase's spend.
        if args.get("phase_id") is not None:
            params.append(int(args["phase_id"]))
            where.append(f"phase_id = ${len(params)}")
        params.append(min(int(args.get("since_days") or 30), 365))
        where.append(f"created_at > now() - make_interval(days => ${len(params)})")
        rows = await pool.fetch(
            f"""SELECT harness, COALESCE(model,'(default)') AS model,
                       COUNT(*) AS runs,
                       COUNT(*) FILTER (WHERE ok) AS ok,
                       SUM(tokens_total) AS tokens,
                       COUNT(*) FILTER (WHERE token_source <> 'unknown') AS runs_with_tokens,
                       ROUND(SUM(cost_microdollars) / 1000000.0, 4) AS cost_usd,
                       COUNT(*) FILTER (WHERE cost_method = 'billed') AS runs_billed,
                       ROUND(SUM(COALESCE(compute_s, 0))::numeric, 1) AS compute_s,
                       ROUND(AVG(wall_clock_s)::numeric, 1) AS avg_wall_s
                  FROM harness_usage
                 WHERE {' AND '.join(where)}
                 GROUP BY harness, COALESCE(model,'(default)')
                 ORDER BY SUM(cost_microdollars) DESC, COUNT(*) DESC
                 LIMIT 40""", *params)
        out = []
        for r in rows:
            d = dict(r)
            # A token total is only a measurement when the harness reported
            # one. kimi's stream carries no usage (34 of 34 runs measured),
            # so its SUM is 0 for want of data — say so instead
            # of letting 0 read as "free".
            if int(d.get("runs_with_tokens") or 0) == 0:
                d["tokens"] = None
                d["tokens_note"] = "no run reported usage — this harness's stream carries no token counts"
            elif int(d["runs_with_tokens"]) < int(d["runs"]):
                d["tokens_note"] = (f"tokens cover {d['runs_with_tokens']} of {d['runs']} runs; "
                                    "the rest reported no usage")
            out.append(d)
        return {"status": "ok", "result": out}
    except Exception as e:
        return _refused_if_caller(e, f"harness_costs failed: {e}")


async def _exec_credential_wiring(args: dict, entity_id: int, pool) -> dict:
    """Read/set the host's per-consumer credential wiring
    (behavioral_params.credential_wiring — the typed-credentials model:
    the HOST selects which Keys entry an application/lane uses)."""
    op = (args.get("op") or "get").strip()
    try:
        if op == "get":
            from server.vault.providers import PROVIDERS, DIRECT_PROVIDERS, canonical_name
            out = {"providers": {}, "wiring": {}}
            if pool is not None and entity_id:
                row = await pool.fetchrow(
                    "SELECT behavioral_params FROM entity_registry WHERE id=$1",
                    entity_id)
                bp = (row and row["behavioral_params"]) or {}
                if isinstance(bp, str):
                    bp = json.loads(bp or "{}")
                out["wiring"] = (bp.get("credential_wiring") or {}) if isinstance(bp, dict) else {}
            for pid in PROVIDERS:
                out["providers"][pid] = {
                    "direct": pid in DIRECT_PROVIDERS,
                    "canonical_api_key": canonical_name(pid),
                    "canonical_oauth": canonical_name(pid, "oauth_token"),
                }
            return {"status": "ok", "result": out}
        if op in ("set", "clear"):
            consumer = (args.get("consumer") or "").strip()
            if not consumer:
                return _refused("consumer required")
            keys_name = (args.get("keys_name") or "").strip()
            if op == "set" and not keys_name:
                return _refused("keys_name required for set")
            if pool is None or not entity_id:
                return {"status": "error", "result": "no pool/host id for wiring write"}
            row = await pool.fetchrow(
                "SELECT behavioral_params FROM entity_registry WHERE id=$1", entity_id)
            bp = (row and row["behavioral_params"]) or {}
            if isinstance(bp, str):
                bp = json.loads(bp or "{}")
            if not isinstance(bp, dict):
                bp = {}
            wiring = bp.get("credential_wiring") or {}
            if not isinstance(wiring, dict):
                wiring = {}
            if op == "set":
                wiring[consumer] = keys_name
            else:
                wiring.pop(consumer, None)
            bp["credential_wiring"] = wiring
            await pool.execute(
                "UPDATE entity_registry SET behavioral_params = $1::jsonb WHERE id = $2",
                json.dumps(bp), entity_id)
            return {"status": "ok", "result": {"wiring": wiring}}
        return _refused(f"unknown op {op!r} (get|set|clear)")
    except Exception as e:
        return _refused_if_caller(e, f"credential_wiring failed: {e}")


async def _exec_harness_transcript(args: dict, pool) -> dict:
    """Full persisted transcript of one harness run (harness_run row —
    written at collect time, so in-flight runs report available:false).

    Addressable two ways. `run_id` is the NUMBER the user reads in the
    Workbench runs log and says out loud ("take a look at run 14") — it is
    harness_run.id, the row's own primary key, so it is unique, stable, and
    survives curation (curate_harness_runs only strips transcripts; it never
    deletes a row). Before this, the only address was (session_id, harness) —
    a UUID plus a name — which the user cannot say in conversation, so a
    spoken run reference had nothing to resolve against. The identity fields
    ride the result so the host can chain from a number to the run's project,
    session and variant without a second lookup.
    """
    max_chars = min(int(args.get("max_chars") or 20000), 60000)
    _run_id = args.get("run_id")
    sid = (args.get("session_id") or "").strip()
    harness = (args.get("harness") or "").strip()
    if _run_id is None and not (sid and harness):
        return _refused("run_id, or both session_id and harness, required")
    try:
        if _run_id is not None:
            try:
                _run_id = int(_run_id)
            except (TypeError, ValueError):
                return _refused("run_id must be an integer")
            row = await pool.fetchrow(
                """SELECT id, session_id, harness, project_id, transcript, ok,
                          error, exit_code, elapsed_seconds, model, diff_bytes, status
                     FROM harness_run WHERE id = $1""", _run_id)
            if not row:
                return {"status": "ok", "result": {
                    "available": False,
                    "reason": f"no run numbered {_run_id} on this host"}}
        else:
            row = await pool.fetchrow(
                """SELECT id, session_id, harness, project_id, transcript, ok,
                          error, exit_code, elapsed_seconds, model, diff_bytes, status
                     FROM harness_run
                    WHERE session_id = $1 AND harness = $2
                    ORDER BY id DESC LIMIT 1""", sid, harness)
            if not row:
                return {"status": "ok", "result": {"available": False}}
        # An archived row keeps its identity but its transcript was stripped by
        # curation — say which it is, so "no transcript" is never mistaken for
        # "no such run".
        if row["status"] == "archived" and not (row["transcript"] or ""):
            return {"status": "ok", "result": {
                "available": False,
                "run_id": row["id"], "session_id": row["session_id"],
                "harness": row["harness"], "project_id": row["project_id"],
                "ok": row["ok"], "model": row["model"],
                "reason": ("this run was archived and its transcript stripped; "
                           "its outcome and identity survive, the scrollback does not")}}
        t = row["transcript"] or ""
        # The transcript carries what the run read, and a pool harness pod
        # reaches the open web, so it marks the turn whatever the project's
        # repo (runtime/turn_taint); the repo only names the target.
        _target = None
        if row["project_id"] is not None:
            try:
                _target = await pool.fetchval("SELECT repo FROM projects WHERE id = $1",
                                              int(row["project_id"]))
            except Exception as e:  # noqa: BLE001 - unknown is never trusted
                log.debug("harness_transcript: project %s repo unread: %s", row["project_id"], e)
        await _mark_harness_target(_target, f"harness run {row['id']}'s transcript")
        clipped = len(t) > max_chars
        # F-014: a harness transcript is ATTACKER-INFLUENCED — a harness that
        # reads a repo echoes file content into it. This tool needs only
        # read_memory (meta_tool_caps.py: required_bits 2) and is code-exec
        # exempt, so the narrowest useful agent grant can hold it. Screen on the
        # way to the model; harness_run and the run stream route (review.py) keep the raw bytes.
        # Screen AFTER the tail cut so exactly what the model sees is what was
        # screened.
        from mastercode.runtime.untrusted_content import screen_for_model
        from mastercode.runtime.run_failure import failure_kind as _failure_kind
        tail, flags = screen_for_model(t[-max_chars:], source="harness_transcript")
        return {"status": "ok", "result": {
            "available": True,
            # Identity first: a run reached by number must report back WHICH
            # run it was, so the host can name it ("run 14, kimi, on Garden plan")
            # and chain to the variant preview or a follow-up build.
            "run_id": row["id"], "session_id": row["session_id"],
            "harness": row["harness"], "project_id": row["project_id"],
            "diff_bytes": row["diff_bytes"],
            "ok": row["ok"], "error": row["error"],
            # What KIND of failure this was (runtime/run_failure.py) — a
            # transport loss is named as one, so the host does not blame the
            # model for it.
            "failure_kind": _failure_kind(row["error"], row["exit_code"], row["ok"]),
            "model": row["model"], "elapsed_seconds": row["elapsed_seconds"],
            "clipped": clipped,
            # Surfaced, not silent: a neutralized transcript must be visibly
            # neutralized or the operator cannot tell a screen from its absence.
            "injection_flags": flags,
            "transcript": tail}}
    except Exception as e:
        return _refused_if_caller(e, f"harness_transcript failed: {e}")


# A clone that has said nothing for this long — no progress line, no byte on
# stderr — is stalled; one that keeps reporting runs as long as it needs.
# git --progress writes at least once a second while counting, receiving,
# resolving deltas and checking out, so two minutes of silence is a dead
# transfer, not a slow one.
_CLONE_IDLE_S = 120


async def _run_with_progress_liveness(cmd: list, *, env: dict | None = None,
                                      idle_s: float | None = None) -> tuple[int, str, bool]:
    """Run `cmd`, judging it by its stderr progress instead of a wall clock.

    Returns (returncode, stderr tail, stalled). stalled=True means no stderr
    output for idle_s seconds — the process was killed. stdout is discarded."""
    import asyncio
    idle = float(_CLONE_IDLE_S if idle_s is None else idle_s)
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE, env=env)
    tail = b""
    stalled = False
    while True:
        try:
            chunk = await asyncio.wait_for(proc.stderr.read(4096), timeout=idle)
        except asyncio.TimeoutError:
            stalled = True
            proc.kill()
            break
        if not chunk:
            break
        tail = (tail + chunk)[-4096:]
    rc = await proc.wait()
    text = tail.decode("utf-8", "ignore").replace("\r", "\n")
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    return rc, "\n".join(lines[-6:]), stalled


# The helper git runs for a credential on the platform forge. The values ride
# the clone's environment, never its argv or the URL, and git asks this helper
# only for a URL on the forge's host (credential.<url>.helper); a redirect or a
# submodule elsewhere gets nothing.
_FORGE_CRED_HELPER = ('!f() { test "$1" = get || exit 0; '
                      'echo "username=$PRIVATAE_FORGE_USER"; '
                      'echo "password=$PRIVATAE_FORGE_TOKEN"; }; f')


def _forge_clone_auth(url: str) -> tuple[list, dict]:
    """(git -c args, env) that let a clone of `url` read a private repository
    on the platform's own forge as this Host — the pod's forge credential,
    which import_repo otherwise turns off with GIT_CONFIG_GLOBAL=/dev/null.
    Empty for any other host: a GitHub or third-party clone never sees the
    forge's credential. Empty too when the URL carries its own credentials or
    the Host has none."""
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(url)
    except ValueError:
        return [], {}
    if parts.scheme != "https" or parts.username or parts.password:
        return [], {}
    try:
        from server.routes.gitea_client import gitea_creds, gitea_proxy
        cred = gitea_creds()
    except Exception:  # noqa: BLE001 — no forge here: a plain clone
        return [], {}
    if not cred:
        return [], {}
    user, token, host = cred
    if (parts.netloc or "").lower() != str(host or "").lower():
        return [], {}
    env = {"PRIVATAE_FORGE_USER": user, "PRIVATAE_FORGE_TOKEN": token}
    try:
        proxy = gitea_proxy()
    except Exception:  # noqa: BLE001
        proxy = ""
    if proxy:
        # The forge is reached through the same egress proxy every forge call
        # uses (gitea_client.gitea_request, gitea_propose._git_work).
        env.update({"https_proxy": proxy, "HTTPS_PROXY": proxy})
    return (["-c", "credential.helper=",
             "-c", f"credential.https://{host}.helper={_FORGE_CRED_HELPER}"], env)


async def _exec_import_repo(args: dict) -> dict:
    """Import an existing codebase from a Git URL (a GitHub pull) into a project
    workspace, so the host can index/search/build on it. Creates a new project (or
    clones into an existing one) and shallow-clones the repo into its workspace at
    /var/lib/cseries/projects/<id>. PUBLIC repos need no credentials. A private repo
    on the platform's own forge is cloned with the Host's forge credential
    (_forge_clone_auth), offered to that host only; anywhere else a private repo
    needs a token embedded in the https URL. Args:
      github_url (required) — an https:// git URL (e.g. https://github.com/owner/repo)
      name (optional)       — project name (defaults to the repo name)
      branch (optional)     — branch/tag to clone (default: the repo's default)
      project_id (optional) — clone into an existing project instead of creating one
    """
    import os
    import re
    import shutil
    a = args or {}
    url = (a.get("github_url") or a.get("url") or "").strip()
    if not url:
        return _refused("github_url is required")
    if not re.match(r"^https://[A-Za-z0-9._~:/?#@!$&'()*+,;=%\[\]-]+$", url):
        return _refused("github_url must be an https:// git URL (no ssh/file URLs)")
    branch = (a.get("branch") or "").strip()
    try:
        from server import brain_service
        from server.routes.applications import PROJECTS_ROOT
        pool = brain_service.get_pool()
        if pool is None:
            return {"status": "error", "result": "import_repo failed: pool unavailable"}
        eid = brain_service._entity_id

        repo_name = url.rstrip("/").split("/")[-1]
        if repo_name.endswith(".git"):
            repo_name = repo_name[:-4]
        name = (a.get("name") or repo_name or "imported-repo").strip()

        created = False
        if a.get("project_id") is not None:
            try:
                pid = int(a.get("project_id"))
            except (TypeError, ValueError):
                return _refused("project_id must be an integer")
            if await pool.fetchrow("SELECT id FROM projects WHERE id=$1 AND entity_id=$2", pid, eid) is None:
                return _refused(f"project {pid} not found")
        else:
            row = await pool.fetchrow(
                "INSERT INTO projects (entity_id, name, description) VALUES ($1,$2,$3) RETURNING id",
                eid, name, f"Imported from {url}")
            pid = row["id"]
            created = True

        project_dir = str(PROJECTS_ROOT / str(pid))
        if os.path.isdir(project_dir) and os.listdir(project_dir):
            if created:
                await pool.execute("DELETE FROM projects WHERE id=$1 AND entity_id=$2", pid, eid)
            return _refused(f"project {pid} workspace is not empty — import into a new project")

        # No-shell exec (no injection); fail fast instead of hanging on auth prompts.
        # --progress makes git report as it goes even without a terminal, and
        # the clone is judged by that liveness, never by a clock (a 180 s
        # wall cut large repos through the egress proxy while they were still
        # arriving).
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0",
               "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
        auth_args, auth_env = _forge_clone_auth(url)
        env.update(auth_env)
        cmd = (["git", *auth_args, "clone", "--progress", "--depth", "1"]
               + (["--branch", branch] if branch else []) + [url, project_dir])
        rc, err_tail, stalled = await _run_with_progress_liveness(cmd, env=env)
        if stalled or rc != 0:
            shutil.rmtree(project_dir, ignore_errors=True)
            if created:
                await pool.execute("DELETE FROM projects WHERE id=$1 AND entity_id=$2", pid, eid)
            if stalled:
                return {"status": "error", "result":
                        f"git clone stopped: no progress for {_CLONE_IDLE_S}s "
                        f"(last output: {err_tail[-200:] or 'none'})"}
            return {"status": "error", "result": f"git clone failed: {err_tail[-300:] or 'unknown error'}"}

        nfiles = sum(len(fs) for root, _, fs in os.walk(project_dir) if ".git" not in root.split(os.sep))
        return {"status": "ok", "result": {
            "project_id": pid, "name": name, "source": url,
            "branch": branch or "default", "files": nfiles, "created_project": created}}
    except Exception as e:
        return _refused_if_caller(e, f"import_repo failed: {getattr(e, 'detail', None) or e}")


async def _exec_review_session(args):
    a = args or {}
    session_id = (a.get("session_id") or "").strip()
    if not session_id:
        return _refused("session_id required")
    try:
        from server import brain_service
        # get_pool is a plain function (brain_service.get_pool); the service
        # reads the session from disk and does not use the pool.
        pool = brain_service.get_pool()
        from server.routes.review import get_session_service
        res = await get_session_service(pool, session_id=session_id)
        return _review_session_view(res, str(a.get("variant") or "").strip())
    except Exception as e:
        return _refused_if_caller(e, f"review_session failed: {getattr(e, 'detail', None) or e}")


# review_session's default view keeps the brief to this many characters; the
# variant view and the Workbench screen read the manifest whole.
_REVIEW_PROMPT_CHARS = 600
# A variant's outcome line, when it is the transcript's tail.
_REVIEW_OUTCOME_CHARS = 300
# Files named per variant in the default view.
_REVIEW_FILES_MAX = 60


def _review_variant_line(vid: str, rq: dict, st: dict, diff: str, result: dict) -> dict:
    """One variant of a compare in review_session's default view: who ran it,
    where it stands, how big its diff is and which files it touched, and a
    short outcome — the error, else the end of what the harness said."""
    from server.routes.review import _parse_diff_files
    files = sorted(_parse_diff_files(diff or "")) if diff else []
    outcome = st.get("error") or (result or {}).get("error")
    if not outcome:
        tail = str((result or {}).get("transcript_tail") or "").strip()
        if tail:
            outcome = ("…" if len(tail) > _REVIEW_OUTCOME_CHARS else "") + tail[-_REVIEW_OUTCOME_CHARS:]
    line = {
        "variant": vid,
        "harness": rq.get("harness") or vid,
        "model": st.get("model") or rq.get("model"),
        "effort": rq.get("effort"),
        "state": st.get("state"),
        "elapsed_seconds": st.get("elapsed_seconds"),
        "diff_bytes": st.get("diff_bytes") if st.get("diff_bytes") is not None else len(diff or ""),
        "files": files[:_REVIEW_FILES_MAX],
        "outcome": outcome,
    }
    if len(files) > _REVIEW_FILES_MAX:
        line["files_more"] = len(files) - _REVIEW_FILES_MAX
    return line


def _review_session_view(res: dict, variant: str = "") -> dict:
    """review_session for the Host: a line per variant (harness, model, state,
    elapsed, diff size, files, a short outcome) and the manifest without its
    per-variant results; with `variant` that one variant whole — its state
    row, its unified diff, the reviewer's verdict and its result record.

    Measured on a seven-harness compare: the session
    service's answer was 258,351 characters, nearly all of it the seven
    diffs, and it rode at first 96k-119k tokens in three turns. The Workbench
    route reads get_session_service itself, unchanged."""
    if not isinstance(res, dict):
        return {"status": "ok", "result": res}
    manifest = res.get("manifest") if isinstance(res.get("manifest"), dict) else {}
    per = res.get("per_harness") if isinstance(res.get("per_harness"), dict) else {}
    diffs = res.get("diffs") if isinstance(res.get("diffs"), dict) else {}
    results = manifest.get("results") if isinstance(manifest.get("results"), dict) else {}
    # The whole string each harness was sent (review._record_sent_prompt):
    # 17,217 chars on one project's run, so a seven-variant compare would
    # put ~120k chars back into the default view. One rides the variant view.
    sent = manifest.get("sent_prompt") if isinstance(manifest.get("sent_prompt"), dict) else {}
    requested = {str(v.get("vid")): v for v in (manifest.get("variants") or [])
                 if isinstance(v, dict) and v.get("vid")}
    vids = list(dict.fromkeys(list(requested) + list(per) + list(diffs)))
    if variant:
        want = variant.lower()
        vid = next((v for v in vids if v.lower() == want), None) or next(
            (v for v in vids if str((requested.get(v) or {}).get("harness") or "").lower() == want), None)
        if vid is None:
            return _refused(f"no variant '{variant}' in compare {res.get('session_id')}. Its variants: "
                            + ", ".join(vids))
        return {"status": "ok", "result": {
            "session_id": res.get("session_id"), "status": res.get("status"),
            "variant": vid, "requested": requested.get(vid),
            "state": per.get(vid), "diff": diffs.get(vid, ""),
            "result": results.get(vid),
            "sent_prompt": sent.get(vid),
            "entrypoint": (res.get("entrypoints") or {}).get(vid)}}
    prompt = manifest.get("prompt")
    short = {k: v for k, v in manifest.items() if k not in ("results", "sent_prompt")}
    if sent:
        short["sent_prompt_chars"] = {v: len(s) for v, s in sent.items() if isinstance(s, str)}
    if isinstance(prompt, str) and len(prompt) > _REVIEW_PROMPT_CHARS:
        short["prompt"] = prompt[:_REVIEW_PROMPT_CHARS] + "…"
        short["prompt_chars"] = len(prompt)
    return {"status": "ok", "result": {
        "session_id": res.get("session_id"),
        "status": res.get("status"),
        "variants": [_review_variant_line(v, requested.get(v) or {}, per.get(v) or {},
                                          diffs.get(v) or "", results.get(v) or {})
                     for v in vids],
        "manifest": short,
        "picks": res.get("picks"),
        "entrypoints": res.get("entrypoints"),
        "app": res.get("app"),
        "detail": "review_session(session_id, variant='<variant or harness>') returns that "
                  "variant whole: its unified diff, state, review and result",
    }}


_codesearch_index_tasks: dict = {}


def _get_codesearch_db():
    """The code-search store's handle: the pod's Postgres pool. Mirrors the
    /api/codesearch route's _get_db()."""
    from codesearch import store
    return store.connect()


# search_my_code returns LOCATORS, the usual shape of a code-search hit:
# per hit its repo or project, file, line range and the start of the chunk,
# never the whole chunk. Whole chunks put every result over the result store's
# threshold (measured over 14 days: 12 of 12 stored, median 179,964
# characters), and the 2,000-character preview held 1 or 2 of the 6 to 15
# hits. A default search (10 hits) is sized to arrive whole: the snippets
# shrink until the result fits under result_store.STORE_OVER_CHARS.
_SEARCH_SNIPPET_DEFAULT = 600
_SEARCH_SNIPPET_MIN = 120
_SEARCH_SNIPPET_MAX = 4000
_SEARCH_READ_NOTE = (
    "Each hit is a locator: file_path and start_line-end_line, with the start of "
    "the chunk as snippet (truncated=true when there is more). Read the range: an "
    "'owner/name' repo with gitea op='read' (start_line, end_line), a Workbench "
    "project with workspace_file. Then pin every exact occurrence with a grep.")


def _search_locator(hit: dict, snippet_chars: int) -> dict:
    """One search hit as a locator: where it is, its kind and name, the first
    snippet_chars of the chunk (cut back to a line end when one is near) and
    the scores that ranked it."""
    content = str(hit.get("content") or "")
    snippet = content[:snippet_chars]
    if len(content) > snippet_chars:
        cut = snippet.rfind("\n")
        if cut >= snippet_chars // 2:
            snippet = snippet[:cut + 1]
    row = {k: hit.get(k) for k in ("project", "file_path", "start_line", "end_line",
                                   "symbol", "chunk_kind", "chunk_index")}
    if len(hit.get("projects") or ()) > 1:   # the same file in more than one project
        row["projects"] = hit["projects"]
    row["snippet"] = snippet
    row["truncated"] = len(snippet) < len(content)
    for k in ("relevance", "rerank_score", "rrf_score", "lex_rank"):
        v = hit.get(k)
        if isinstance(v, (int, float)):
            row[k] = round(float(v), 4)
    return row


def _search_locators(results: list, out: dict, snippet_chars: int | None) -> None:
    """out['results'] as locators. snippet_chars None (the default): the
    snippets start at _SEARCH_SNIPPET_DEFAULT and shrink, down to
    _SEARCH_SNIPPET_MIN, until the whole result is under STORE_OVER_CHARS.
    A size the caller chose is kept as asked."""
    from mastercode.runtime.result_store import STORE_OVER_CHARS
    n = _SEARCH_SNIPPET_DEFAULT if snippet_chars is None else snippet_chars
    while True:
        out["results"] = [_search_locator(h, n) for h in results if isinstance(h, dict)]
        if snippet_chars is not None or n <= _SEARCH_SNIPPET_MIN:
            return
        # Measured as the transcript carries it (result_store.serialize), with
        # room for the envelope around it.
        if len(json.dumps({"status": "ok", "result": out}, default=str)) <= STORE_OVER_CHARS - 200:
            return
        n = max(_SEARCH_SNIPPET_MIN, int(n * 0.75))


async def _own_repo_hits(results: list) -> tuple[list, list[str]]:
    """(the hits whose project is a forge repo of this Host's or its user's,
    the names of the other projects). Judged by _forge_repo_foreign, the
    forge's own list read once per turn; a Workbench project, a name that is
    not owner/name, or a list that cannot be read is not kept. A hit found
    in several projects (searcher.collapse_copies) is kept when one of them
    is the Host's or its user's, and named by those alone."""
    keep: list = []
    dropped: set[str] = set()
    own: dict[str, bool] = {}
    for h in results:
        if not isinstance(h, dict):
            dropped.add("(unnamed)")
            continue
        names = [str(p or "").strip() for p in (h.get("projects") or [h.get("project")])]
        for p in names:
            if p not in own:
                own[p] = bool(p) and (await _forge_repo_foreign(p)) is None
        mine = [p for p in names if own[p]]
        dropped.update(p or "(unnamed)" for p in names if not own[p])
        if mine:
            keep.append(dict(h, project=mine[0], projects=mine) if mine != names else h)
    return keep, sorted(dropped)


async def _exec_search_my_code(args):
    """Hybrid (vector + BM25, cross-encoder reranked) search over everything
    the Host's code index holds — its Workbench projects and every gitea repo
    it can read (codesearch.repo_sync) — or one of them when `project` (or
    `repo`) names it. Each hit is a locator (_search_locator) naming its
    project. Calls codesearch.searcher.search directly."""
    a = args or {}
    project = (a.get("project") or a.get("repo") or "").strip().strip("/") or None
    query = (a.get("query") or a.get("q") or "").strip()
    if not query:
        return _refused("query is required")
    try:
        limit = int(a.get("limit") or 10)
    except (TypeError, ValueError):
        limit = 10
    limit = max(1, min(50, limit))
    snippet_chars = None
    if a.get("max_snippet_chars") is not None:
        try:
            snippet_chars = max(_SEARCH_SNIPPET_MIN,
                                min(_SEARCH_SNIPPET_MAX, int(a.get("max_snippet_chars"))))
        except (TypeError, ValueError):
            snippet_chars = None
    try:
        from codesearch import repo_sync, searcher
        db = _get_codesearch_db()
        meta: dict = {}
        results = await searcher.search(db, project=project, query=query, limit=limit, meta=meta)
        # The index holds every repo the forge lets the Host read (a repo
        # another owner added it to as a collaborator among them: foreign, as
        # gitea judges it) and the Workbench projects, whose code can come from
        # anywhere. The root thread, where machine_exec runs, reads nothing
        # from outside (runtime/root_turn), so there only the hits from the
        # Host's own repos and its user's are kept; the rest are named, never
        # shown.
        left_out: list[str] = []
        from mastercode.runtime import root_turn as _rt_s
        if _rt_s.in_root_turn():
            results, left_out = await _own_repo_hits(results)
        out = {
            "project": project or "all",
            "query": query,
            "count": len(results),
            "results": [],
            "read": _SEARCH_READ_NOTE,
        }
        if left_out:
            out["left_out"] = {
                "projects": left_out,
                "why": ("your root thread shows only your own repos and your user's; search "
                        "these from another thread"),
            }
        # A search the embedding lane did not answer matched words only; it
        # says so, so a thin keyword answer is not taken for a semantic one.
        out.update(searcher.how_it_ran(meta, results))
        # Readable repos the index does not hold whole (too big for the
        # disk, a clone that failed, no embedder yet), so an empty or thin
        # answer is not read as "the code is not there".
        try:
            gaps = [g for g in await repo_sync.not_searchable(db)
                    if project is None or g["repo"] == project]
        except Exception:  # noqa: BLE001 — a Host before v224 has no code_repos
            gaps = []
        if gaps:
            out["not_fully_indexed"] = gaps
        _search_locators(results, out, snippet_chars)
        return {"status": "ok", "result": out}
    except Exception as e:
        return _refused_if_caller(e, f"search_my_code failed: {getattr(e, 'detail', None) or e}")


async def _exec_index_my_code(args):
    """(Re)index ONE of the entity's own named projects incrementally. Kicks the
    walk+chunk+embed+upsert off in the background (it can be long) and returns
    immediately — same fire-and-forget semantics as /api/codesearch/index.
    Calls codesearch.indexer.index_project directly."""
    a = args or {}
    project = (a.get("project") or "").strip()
    if not project:
        return _refused("project is required")
    try:
        from codesearch import indexer
        known = indexer.list_projects()
        if known and project not in known:
            return _refused(f"unknown project {project!r}. Your projects: {', '.join(known) or '(none)'}")

        task = _codesearch_index_tasks.get(project)
        if task is not None and not task.done():
            return {"status": "ok", "result": {"project": project, "status": "already running"}}

        db = _get_codesearch_db()

        async def _run() -> None:
            try:
                summary = await indexer.index_project(db, project=project)
                log.info("codesearch index_my_code: %s", summary)
            except Exception as e:
                log.warning("codesearch index_my_code failed for %s: %s", project, e)

        _codesearch_index_tasks[project] = asyncio.create_task(_run())
        return {"status": "ok", "result": {"project": project, "status": "indexing started"}}
    except Exception as e:
        return _refused_if_caller(e, f"index_my_code failed: {getattr(e, 'detail', None) or e}")


# What the Host's matrix view leaves off each row: the model list (most of the
# size) and the fields only the screen draws.
_MATRIX_ROW_DROP = ("models", "icon_url", "band", "models_source")


def _harness_matrix_row(r: dict) -> dict:
    """One harness in harness_connections(op='matrix')'s default view: is it
    working and what does it run. Every field the matrix row carries except
    those in _MATRIX_ROW_DROP, verbatim: the key's state, name, source and
    expiry, the provider it runs on, the default model and the saved overrides,
    the efforts it takes, and the row's own words for a harness that cannot
    run (models_error, models_loading, action). `model_count` stands in for the
    list.

    Measured: op='matrix' returned 165,985
    characters, every model of every harness's catalog, and a tool result stays
    in every later call of its turn. harness='<id>' returns one harness's whole
    row, models included. The Orchestrator screen reads _build_coding_matrix
    itself, unchanged."""
    row = {k: v for k, v in r.items() if k not in _MATRIX_ROW_DROP}
    row["model_count"] = len(r.get("models") or [])
    return row


def _harness_matrix_view(m: dict, args: dict, app_to_provider: dict) -> dict:
    """op='matrix' for the Host: a line per harness, or with `harness` that one
    harness's whole matrix row. Either spelling of claude names the same row
    (skill 'coding-protocols'): the row id, or the provider entry id behind it
    (harnesses._harness_app_to_provider: app id -> PROVIDERS entry)."""
    rows = m.get("harnesses") if isinstance(m, dict) else None
    if not isinstance(rows, list):
        return {"status": "ok", "result": m}
    want = str((args or {}).get("harness") or "").strip().lower()
    if want:
        ids = {want} | {str(h).lower() for h, p in (app_to_provider or {}).items()
                        if isinstance(p, dict) and str(p.get("id") or "").lower() == want}
        row = next((r for r in rows if str(r.get("id") or "").lower() in ids), None)
        if row is None:
            return _refused(f"no harness '{want}' in the matrix. Its harnesses: "
                            + ", ".join(str(r.get("id")) for r in rows))
        return {"status": "ok", "result": {"harness": row,
                                           "default_engine": m.get("default_engine")}}
    out = {k: v for k, v in m.items() if k != "harnesses"}
    out["harnesses"] = [_harness_matrix_row(r) for r in rows]
    out["full_row"] = ("harness_connections(op='matrix', harness='<id>') returns one "
                       "harness's whole row, its models included")
    return {"status": "ok", "result": out}


async def _exec_harness_connections(args: dict) -> dict:
    """Read-only view of the host's coding-harness connections (Claude Code,
    Codex, Cursor, Grok, opencode, Kimi Code) — which are installed, connected, and whose
    credentials are still valid. Environment-awareness sibling of
    explore_environment / system.app_status. Two ops:

      op="list"   → catalog of all known harness providers + install/auth state
      op="status" → {present, valid, expires_at, auth_type} for one provider

    Calls the connections route module's own service-layer functions directly
    (PROVIDERS catalog, _app_is_installed, _creds_status, harness_validity) —
    never the @router handlers. Read-only: no creds written, no OAuth started."""
    a = args or {}
    op = (a.get("op") or "list").strip()
    try:
        from server import brain_service
        pool = brain_service.get_pool()
        if pool is None:
            return {"status": "error", "result": "pool unavailable"}
        from server.routes import harnesses as conn
        from server.network.harness_keys import (
            keys_name as _keys_name, KEYS_HARNESS_PROVIDERS as _KEYS_HARNESSES,
        )

        # Read the per-harness reconciler state ONCE for the whole listing —
        # both are behavioral_params reads and this loop runs over every
        # provider. Best-effort: an unreadable record degrades to "unknown",
        # never to a wrong claim about what a harness is running on.
        try:
            from server.network import harness_state
            _cred_recs = await harness_state.credential_records()
            _disconnected = await harness_state.disconnected_set()
        except Exception:
            _cred_recs, _disconnected = {}, set()

        if op == "login":
            # Drive the VENDOR'S OWN login inside the harness pod and hand the
            # user the URL it prints. The platform never mints or stores the
            # grant — the binary owns its protocol, the user finishes it in
            # their own browser, and whatever the vendor gives them goes into
            # the Keys app by their own hand.
            #
            # The pod is NO LONGER released unconditionally. A device-code
            # login BLOCKS after printing its URL, so harness_login holds the
            # pod while the vendor waits (done=False) and releases only on
            # completion or failure; the operator idle-sweep bounds the held
            # case at ~900s. Releasing immediately killed the waiting process,
            # and the grant with it.
            provider = (a.get("provider") or "").strip()
            if not provider:
                return _refused("provider required for op='login'")
            from server.routes.review import (harness_login, HarnessLoginReq,
                                              login_status_supported)
            try:
                res = await harness_login(HarnessLoginReq(harness=provider))
            except Exception as e:
                return _refused_if_caller(e, f"login failed: {e}")
            if res.get("done"):
                # The vendor's command EXITED inside the read window — either
                # the already-signed-in short circuit (grant written, nothing
                # for the user to do) or a failure. harness_login has already
                # released the pod, so a URL instruction here would send the
                # host to poll a FRESH pod that has "no login in flight" and
                # hold it until idle-sweep. Say which
                # it was instead.
                if res.get("ok"):
                    res["next_step"] = (
                        "Already signed in — the vendor's login finished on its "
                        "own and the credential is saved to this host. Nothing "
                        "for the user to open or paste; do not call "
                        "op='login_status' for this one.")
                else:
                    res["next_step"] = (
                        "The vendor's login ended without a grant"
                        + (": " + str(res.get("error")) if res.get("error") else "")
                        + ". Tell the user, then run op='login' again for a "
                        "fresh link when they are ready; do not poll "
                        "op='login_status' for a login that already ended.")
            elif res.get("url"):
                # The instruction is keyed on what the harness's SIDECAR can
                # do — never on the shape of the URL. Measured over three
                # turns: "now call op='login_status'" keyed on a separate
                # `user_code` never fired for grok, whose device URL carries
                # its code in the query string -> the host was handed the
                # Keys-app text below, never polled, ran op='status' twice
                # (honestly `present: false`), and the grant the CLI had
                # written in the pod died with it on idle-sweep. The platform
                # path itself was fine.
                res["next_step"] = (
                    "Give the user this link to open in their own browser. When "
                    "the vendor hands them a key or token, they paste it into "
                    "the Keys app — open it for them. The harness pod stays up "
                    "while they finish, because the vendor's login is still "
                    "waiting on them; it shuts itself down within about 15 "
                    "minutes either way, so tell them not to leave it hanging.")
                if login_status_supported(provider):
                    # The vendor's CLI finishes the sign-in by itself inside
                    # the pod (device flow: codex prints a one-time code the
                    # user types on the page; grok's is already in the URL),
                    # and op='login_status' reads the grant back out and
                    # persists it. Nothing goes into Keys by hand.
                    code = str(res.get("user_code") or "").strip()
                    if code:
                        lead = ("Give the user BOTH the link and this one-time "
                                "code, exactly as written: " + code + " . They "
                                "open the link in their own browser, sign in, "
                                "and type the code on that page. ")
                    else:
                        lead = ("Give the user this link to open in their own "
                                "browser and sign in there (the page already "
                                "carries any code it needs). ")
                    res["next_step"] = (
                        lead + "The vendor's CLI then finishes the sign-in by "
                        "itself inside the pod. When they say they are done, "
                        "call op='login_status', provider='" + provider + "' — "
                        "that reads the grant out of the pod and saves it to "
                        "this host; until you do, the host is NOT connected "
                        "even though the user has signed in. There is nothing "
                        "for them to paste into the Keys app. The sign-in "
                        "expires in about 15 minutes and the pod reclaims "
                        "itself on the same clock, so do not leave them "
                        "hanging.")
                if res.get("needs_code"):
                    res["next_step"] = (
                        "Give the user this link to open in their own browser. "
                        "This vendor's login then asks for a code: the user "
                        "copies it off the vendor's own page and gives it to "
                        "you, and you pass it straight through with "
                        "op='login_code', provider='" + provider + "', "
                        "code='<what they gave you>'. Do NOT send them to the "
                        "Keys app for this one — you are relaying the code, not "
                        "storing it. The pod stays up while they finish and "
                        "shuts itself down within about 15 minutes, so tell "
                        "them not to leave it hanging.")
            return {"status": "ok", "result": res}

        if op == "login_code":
            # Relay the code the user copied off the VENDOR'S page into the
            # login still waiting in their pod. This is the step that makes such
            # a login complete: without it the user authorizes, the code is
            # burned, and nothing ever receives it.
            #
            # The host is a courier here and nothing more — it carries a string
            # the user hands it to a process on the user's own compute. Nothing
            # is minted, stored, or exchanged platform-side.
            provider = (a.get("provider") or "").strip()
            code = (a.get("code") or "").strip()
            if not provider:
                return _refused("provider required for op='login_code'")
            if not code:
                return _refused("code required — ask the user for the code the "
                                  "vendor's own page gave them")
            from server.routes.review import harness_login_code, HarnessLoginCodeReq
            try:
                res = await harness_login_code(
                    HarnessLoginCodeReq(harness=provider, code=code))
            except Exception as e:
                return _refused_if_caller(e, f"login_code failed: {e}")
            if res.get("supported") is False:
                res["next_step"] = (
                    "This harness never asks for a code, so there is nothing to "
                    "relay — you have not broken anything. Its login finishes in "
                    "the user's own browser. Whether they finished there is theirs to "
                    "say: put it in the question box (escalate), and open the Keys app "
                    "if the vendor gave them a key to paste.")
            elif res.get("done"):
                res["next_step"] = (
                    "Signed in, and the credential is saved to this host — tell "
                    "them it is done and that they do not need to paste anything "
                    "anywhere else.")
            else:
                res["next_step"] = (
                    "The vendor did not accept that code, so nothing was saved. "
                    "Usually it was already used, expired, or truncated on the "
                    "way over. Start again with op='login' for a fresh link "
                    "rather than asking them to re-send the same code.")
            return {"status": "ok", "result": res}

        if op == "login_status":
            # Has a login started here finished yet? For the case where the user
            # says they authorized but gave you no code — a vendor whose CLI
            # completes on its own writes the grant without prompting.
            provider = (a.get("provider") or "").strip()
            if not provider:
                return _refused("provider required for op='login_status'")
            from server.routes.review import harness_login_finish, HarnessLoginReq
            try:
                res = await harness_login_finish(HarnessLoginReq(harness=provider))
            except Exception as e:
                return _refused_if_caller(e, f"login_status failed: {e}")
            if res.get("supported") is False:
                res["next_step"] = (
                    "This harness cannot be polled — its vendor CLI finishes on "
                    "its own inside the pod. Do not retry this op for it. Whether "
                    "they finished in the browser is theirs to say: put it in the "
                    "question box (escalate), and open the Keys app if the vendor "
                    "gave them a key to paste.")
            elif res.get("done"):
                res["next_step"] = (
                    "Signed in, and the credential is saved to this host — "
                    "nothing further for them to paste anywhere.")
            else:
                res["next_step"] = (
                    "Not signed in yet. If this vendor asked them for a code, "
                    "that is what is missing — get it from them and pass it "
                    "through with op='login_code'. Otherwise they have not "
                    "finished in the browser. Do not poll this in a loop: ask "
                    "them where they are, and note the pod reclaims itself "
                    "about 15 minutes after the login started.")
            return {"status": "ok", "result": res}

        if op == "matrix":
            # The SAME build the Orchestrator's coding matrix renders — one
            # resolution, so what the host says and what the screen shows can
            # never disagree. The
            # Host reads it as one line per harness; harness= is one whole row.
            from server.routes.inference import _build_coding_matrix
            # kick_catalogs: the host is LOOKING at the list, so a CLI-catalog
            # harness (cursor) may be woken to read it (inference._build_coding_matrix).
            return _harness_matrix_view(await _build_coding_matrix(kick_catalogs=True),
                                        a, conn._harness_app_to_provider())

        if op == "list":
            out = []
            for pid, p in conn.PROVIDERS.items():
                gate = p.get("present_if_app_installed")
                installed = (await conn._app_is_installed(gate)) if gate else True
                st = conn._creds_status(p)
                out.append({
                    "id":             pid,
                    "name":           p.get("name"),
                    "vendor":         p.get("vendor"),
                    "subscription":   p.get("subscription"),
                    "auth_type":      p.get("auth_type"),
                    "install_app_id": p.get("install_app_id", pid),
                    "installed":      installed,
                    "connected":      st["present"],
                    "valid":          st["valid"],
                    "expires_at":     st["expires_at"],
                    # Canvas pivot: the Keys-app secret name the user adds their
                    # own key/token under to wire this harness (None = not yet
                    # on the Keys-ingest path, e.g. claude-sdk). The host relays
                    # this exact name; it never runs a login flow.
                    "keys_name":      (_keys_name(pid) if pid in _KEYS_HARNESSES else None),
                    # WHICH Keys entry this harness is actually running on
                    # ({name, cred_type, source}), or None when unknown. None is
                    # honest: a credential written by an in-pod vendor login, or
                    # before this was recorded, has no known Keys source. The
                    # host needs this to answer "which key is opencode using?" —
                    # resolved and then discarded, it could not.
                    "credential":     _cred_recs.get(pid),
                    # True when the user revoked this harness. Distinct from
                    # connected=False: not-connected means "never set up",
                    # disconnected means "set up, then turned off".
                    "disconnected":   pid in _disconnected,
                })
            return {"status": "ok", "result": {"providers": out}}

        if op == "status":
            provider = (a.get("provider") or "").strip()
            if not provider:
                return _refused("provider required for op=status")
            p = conn.PROVIDERS.get(provider)
            if not p:
                return _refused(f"unknown provider: {provider}")
            st = conn.harness_validity(provider)
            return {"status": "ok",
                    "result": {**st, "auth_type": p.get("auth_type")}}

        return _refused(f"unknown op: {op} (expected matrix|list|status|"
                          f"login|login_code|login_status)")
    except Exception as e:
        return _refused_if_caller(e, f"harness_connections failed: {getattr(e, 'detail', None) or e}")


async def _exec_integrations_manage(args):
    """Manage the host's data-source integrations (providers + connections).
    Calls the server.integrations service layer directly (no HTTP shim):
      manifests.load_all / manifests.get  — provider catalog
      connection.list_for_user            — sanitized connection list
      storage.get_connection + list_syncs — one connection's detail
      connection.revoke                   — revoke + delete credentials
    These service fns are synchronous (sqlite-backed), so they are called
    without await. Entity ownership mirrors the route's _entity_user_id()
    (ENTITY_ID env). OAuth authorize/callback + api-key connect are NOT
    handled here — those are browser-popup / credential flows."""
    a = args or {}
    op = (a.get("op") or "").strip()
    if not op:
        return _refused("op is required (list_providers|get_provider|list_connections|get_connection|revoke)")
    try:
        import os as _os
        from server.integrations.core import connection as _conn
        from server.integrations.core import manifests as _manifests
        from server.integrations.core import storage as _storage
        user_id = str(_os.environ.get("ENTITY_ID", "0"))

        if op == "list_providers":
            providers = _manifests.load_all()
            out = []
            for key, p in providers.items():
                out.append({
                    "key": key,
                    "display_name": p.display_name,
                    "categories": p.categories,
                    "connection_kind": p.connection_kind,
                    "connection_provider": p.connection_provider or p.key,
                    "auth_mode": p.auth_mode.value,
                    "resources": [r.name for r in p.resources],
                })
            out.sort(key=lambda p: (tuple(p["categories"]) or ("zzz",), p["display_name"]))
            return {"status": "ok", "result": {"providers": out, "count": len(out)}}

        if op == "get_provider":
            key = (a.get("provider_key") or a.get("key") or "").strip()
            if not key:
                return _refused("provider_key is required for get_provider")
            p = _manifests.get(key)
            if not p:
                return _refused(f"provider not found: {key}")
            return {"status": "ok", "result": {
                "key": p.key,
                "display_name": p.display_name,
                "categories": p.categories,
                "connection_kind": p.connection_kind,
                "connection_provider": p.connection_provider or p.key,
                "auth_mode": p.auth_mode.value,
                "auth_scopes": (p.auth or {}).get("scopes") or [],
                "resources": [
                    {"name": r.name, "canonical_model": r.canonical_model,
                     "sync_strategy": (r.sync or {}).get("strategy")}
                    for r in p.resources
                ],
                "webhooks": list((p.webhooks or {}).keys()),
                "reauth_triggers": p.reauth_triggers,
            }}

        if op == "list_connections":
            conns = await _conn.list_for_user(user_id)
            return {"status": "ok", "result": {"connections": conns, "count": len(conns)}}

        if op == "get_connection":
            connection_id = (a.get("connection_id") or "").strip()
            if not connection_id:
                return _refused("connection_id is required for get_connection")
            row = await _storage.get_connection(connection_id)
            if not row:
                return _refused(f"connection not found: {connection_id}")
            if row["user_id"] != user_id:
                return _refused("not owner of this connection")
            provider = _manifests.get(row["provider_key"])
            syncs = await _storage.list_syncs(connection_id)
            return {"status": "ok", "result": {
                "id": row["id"],
                "provider_key": row["provider_key"],
                "provider_name": provider.display_name if provider else row["provider_key"],
                "status": row["status"],
                "connection_config": row.get("connection_config", {}),
                "created_at": row.get("created_at"),
                "last_sync_at": row.get("last_sync_at"),
                "last_error": row.get("last_error"),
                "syncs": syncs,
            }}

        if op == "revoke":
            connection_id = (a.get("connection_id") or "").strip()
            if not connection_id:
                return _refused("connection_id is required for revoke")
            row = await _storage.get_connection(connection_id)
            if not row:
                return _refused(f"connection not found: {connection_id}")
            if row["user_id"] != user_id:
                return _refused("not owner of this connection")
            ok = await _conn.revoke_vault(connection_id)
            if not ok:
                return {"status": "error", "result": f"failed to revoke {connection_id}"}
            return {"status": "ok", "result": {"revoked": connection_id}}

        # ── The host's own setup ops ──────────────────────────
        # Everything above reads or tears down. These four are how a host gets
        # a provider WORKING. The platform serves a Google client, so the usual
        # answer here is `source: platform` and there is nothing to set up —
        # `set_client` is the escape hatch for a user who wants their own
        # project, not the step everyone has to take first.

        if op == "families":
            from server.integrations.core import oauth as _oauth
            host_rows = {r["family"]: r for r in await _storage.list_oauth_clients()}
            providers = _manifests.load_all()
            fams = {(p.auth or {}).get("family", k) for k, p in providers.items()}
            out = []
            for fam in sorted(fams):
                row = host_rows.get(fam)
                # RESOLVED, not read off the row — the same single answer
                # /api/integrations/families and authorize both use. A row
                # whose sealed secret will not open is SKIPPED by the resolver
                # and the platform's client serves in its place; reporting
                # "host" off the row would tell the Host's AI to walk someone
                # through a Google Cloud setup while the flow itself was using
                # the platform's client, which is the exact outcome this key
                # exists to prevent.
                source = await _oauth.client_source(fam) or "none"
                keys = sorted(k for k, p in providers.items()
                              if (p.auth or {}).get("family", k) == fam)
                out.append({
                    "family": fam, "source": source, "configured": source != "none",
                    # Named only when the row and the resolved answer disagree,
                    # so the contradiction is on the surface rather than left
                    # for someone to discover as an inexplicable 503.
                    "unusable_reason": ("registered client could not be opened from "
                                        "the vault; the platform's client is serving "
                                        "instead" if row and source != "host" else None),
                    "label": (row or {}).get("label"), "providers": keys,
                    # The callback path only — this op has no browser request,
                    # so the full URI can only be built where one supplies it
                    # (GET /api/integrations/families). Reporting the path is
                    # honest; inventing an origin is not.
                    "callback_paths": [f"/api/integrations/callback/{k}" for k in keys],
                    # On the platform's client these are already registered, so
                    # there is nothing for the user to paste anywhere. Saying so
                    # is what stops this tool walking someone through a Google
                    # Cloud setup they do not need.
                    "user_registers_redirect_uri": source == "host",
                })
            return {"status": "ok", "result": {"families": out}}

        if op == "set_client":
            from server.integrations.core import credentials as _creds
            from server.integrations.core import oauth as _oauth
            fam = (a.get("family") or "").strip().lower()
            cid = (a.get("client_id") or "").strip()
            csec = (a.get("client_secret") or "").strip()
            if not fam or not cid or not csec:
                return _refused("family, client_id and client_secret are all required")
            known = {(p.auth or {}).get("family", k)
                     for k, p in _manifests.load_all().items()}
            if fam not in known:
                return _refused(f"no installed manifest uses family '{fam}' (known: {sorted(known)})")
            ref = await _creds.put_vault(
                {"client_id": cid, "client_secret": csec},
                cred_type="oauth_app_client", actor="integrations:oauth_client")
            if not ref:
                return {"status": "error",
                        "result": "host vault unavailable — client NOT stored"}
            previous = await _storage.delete_oauth_client(fam)
            await _storage.put_oauth_client(fam, ref, label=a.get("label"))
            if previous and previous != ref:
                try:
                    await _creds.revoke_vault(previous, actor="integrations:oauth_client")
                except Exception:
                    pass
            _oauth.invalidate_cache()
            return {"status": "ok", "result": {
                "family": fam, "source": "host",
                "next": "the user opens the provider's authorize URL to connect an account"}}

        if op == "grant_app":
            connection_id = (a.get("connection_id") or "").strip()
            app_id = (a.get("app_id") or "").strip()
            if not connection_id or not app_id:
                return _refused("connection_id and app_id are required for grant_app")
            row = await _storage.get_connection(connection_id)
            if not row:
                return _refused(f"connection not found: {connection_id}")
            if row["user_id"] != user_id:
                return _refused("not owner of this connection")
            # The SAME checks the HTTP route applies (routes/integrations.py).
            # Two doors onto one table must not disagree about what an app id
            # is: 'host' is not an app, and a malformed id would sit in the
            # table as a grant that can never match a real caller.
            from server.applications.manifest import RESERVED_APP_IDS as _RESERVED
            from server.peer import app_lane as _app_lane
            if not _app_lane.valid_app_id(app_id) or app_id in _RESERVED:
                return _refused(f"'{app_id}' is not a real installed app id")
            methods = a.get("methods") or None
            try:
                await _storage.grant_app(connection_id, app_id, methods)
            except ValueError as ve:
                return _refused(str(ve))
            return {"status": "ok", "result": {
                "connection_id": connection_id, "app_id": app_id,
                "methods": methods or "all"}}

        if op == "revoke_app":
            connection_id = (a.get("connection_id") or "").strip()
            app_id = (a.get("app_id") or "").strip()
            if not connection_id or not app_id:
                return _refused("connection_id and app_id are required for revoke_app")
            if not await _storage.revoke_app_grant(connection_id, app_id):
                return _refused(f"no grant for {app_id} on {connection_id}")
            return {"status": "ok", "result": {
                "connection_id": connection_id, "app_id": app_id, "revoked": True}}

        if op == "list_grants":
            return {"status": "ok", "result": {"grants": await _storage.list_grants(
                connection_id=(a.get("connection_id") or None),
                app_id=(a.get("app_id") or None))}}

        return _refused(f"unknown op: {op}")
    except Exception as e:
        return _refused_if_caller(e, f"integrations_manage failed: {getattr(e, 'detail', None) or e}")


async def _exec_list_skills(args, *, entity_id: int | None = None, pool=None):
    """List the Host's skills: its OWN skills first (host_skills, migrations/
    v207 — written with context(action='write', path='skills/<name>') or in
    the Skills app), then the platform's skill files on disk. Each is marked
    by author ('host', 'person' or 'platform'); a Host skill carries its
    version and whether it is active or retired. Read-only."""
    try:
        from mastercode.runtime import skill_loader
        out = []
        if pool is not None and entity_id is not None:
            from mastercode.runtime import host_skills
            for d in await host_skills.list_skills(pool, entity_id):
                out.append({
                    "name": d["name"],
                    "description": d["description"],
                    "chars": d["size"],
                    "allowed_tools": d["allowed_tools"],
                    "author": d["author"],
                    "version": d["version"],
                    "versions": int(d.get("versions") or 1),
                    "status": d["status"],
                    "path": f"skills/{d['name']}",
                    **({"untrusted": d["untrusted"]} if d.get("untrusted") else {}),
                })
            # A skill written in a turn that read untrusted content marks the
            # turn its description is listed into (host_skills, v232).
            from mastercode.runtime import turn_taint as _turn_taint
            _turn_taint.mark_rows(out, "a skill listed with list_skills")
        loc = skill_loader.get_location()
        skills = skill_loader.list_skills()
        for s in skills:
            try:
                size = s.path.stat().st_size
            except OSError:
                size = None
            out.append({
                "name": s.name,
                "description": s.description,
                "size_bytes": size,
                "allowed_tools": s.allowed_tools,
                "author": "platform",
                "path": f"default-skills/{s.name}",
            })
        return {"status": "ok", "result": {"skills": out, "location": str(loc), "count": len(out)}}
    except Exception as e:
        return _refused_if_caller(e, f"list_skills failed: {getattr(e, 'detail', None) or e}")


async def _exec_artifact(args: dict) -> dict:
    """Build and manage the host's ARTIFACTS — disposable, lightweight LIVE builds.

    An artifact is DISTINCT from an app and from a project: it lives in a
    project-style workspace (/var/lib/cseries/projects/<id>) but is NOT bound to the
    App SDK until the host converts it. It is fast, in-pod, and disposable —
    write some HTML/JS/CSS, see it render live, throw it away, OR convert it to a
    real app. Calls the artifact SERVICE directly (server.applications.artifacts),
    so every invariant (entity-scope, kind='artifact', workspace jail, the App-SDK
    convert handoff) lives in ONE place. Ops:
      create  -> new artifact (name; optional files=[{path,content}], entry,
                 description, kind). kind='desktop' with NO files SEEDS the entry
                 from the platform's desktop template (desktop_contract.TEMPLATE_HTML)
                 server-side — the kernel copies it, the model never retypes it
                 (switching a shell is an op, not an LLM rewrite).
      write   -> write/overwrite files in an artifact (artifact_id + files). On
                 the WORN desktop the new render is loaded in a browser first
                 (server/applications/desktop_pin.py): `desktop.advanced` false
                 = it does not render and the screen kept the last good one.
      render  -> the live build, a self-contained HTML doc (artifact_id; optional entry)
      list    -> the host's artifacts (optional limit)
      convert -> turn an artifact into an app via the App SDK (artifact_id; needs a
                 provenance=custom app.yaml written first via set_project_manifest)
      delete  -> dispose of an artifact (artifact_id)
      history -> the artifact's change history, newest first (artifact_id)
      revert  -> restore a past revision (artifact_id + rev; the restore is
                 itself recorded, so a revert is always revertable)
      set_desktop -> make an artifact THE DESKTOP (artifact_id; optional entry):
                 the shell renders it full-screen under the windows and keeps it
                 live on every write; omit artifact_id to restore the default
                 wallpaper. The document's
                 <meta name="cseries-tokens"> declares the --cseries-* tokens the
                 shell and every app then follow (design_tokens.py grammar).
                 REFUSED (422 report) when the document misses the desktop
                 floor (server/applications/desktop_contract.py) or fails the
                 browser render check (server/applications/desktop_render.py:
                 an uncaught error with its line, nothing painted, no tiles or
                 bar where declared). No browser to ask → set anyway, with a
                 NOT RENDER-CHECKED warning in the result.
      set_wallpaper -> wear one of the host's own images (image_url
                 '/api/images/<id>': image_generate made it, or an app such as
                 Blender Engine rendered it) as the custom wallpaper, desktop or
                 `variant` mobile, through the Settings upload's own writer
                 (server/routes/appearance.py set_wallpaper_from_image). Takes
                 off an HTML desktop that was worn; set_desktop puts it back.
      promote -> copy one root file over the entry, server-side, as ONE revision
                 (artifact_id + from; optional to='index.html'). The variant pick.
                 Never re-emit a file to copy it — this is the copy.
      import  -> copy files from another of the host's project workspaces into
                 the artifact (artifact_id + project_id + files=[{from, to?}]),
                 server-side, one revision. A landed build becomes a live
                 artifact without a model retyping it.
      publish_design -> put a desktop on the Designs shelf ("send it to the
                 design app"): name + optional artifact_id
                 (default: the desktop being worn). The kernel renders it,
                 checks the floor and the browser render check (no browser to
                 ask → NOT published); the PLATFORM picks the shelf — the house
                 shelf everyone sees for the platform's own hosts, the
                 account's shelf for everyone else (server/routes/designs.py).
      desktop_from_url -> "make my desktop look like <site>" (the old
                 website-to-template lane as a tool): url +
                 optional name. The kernel fetches the page and up to four of
                 its stylesheets (url_reader.fetch_raw — the governed egress
                 path, internal hosts refused), reads the site's background,
                 text, accent and fonts with NO model (site_style), and creates
                 a desktop artifact from the template in that palette — an
                 operation, never a retype. Returns artifact_id, the tokens,
                 what it observed, and the floor's verdict. Not set as the
                 desktop: refine it, then set_desktop.
    Every create/write/promote/import whose entry is HTML attaches `contract` —
    the floor's verdict, with `trigger` saying why it is being told — whenever
    the document could be a desktop, so a REFUSED design is fixed in the same
    turn instead of being discovered by the user. See _attach_desktop_contract
    for the three triggers; create used to be excluded entirely, which is how
    an artifact was written with no meta and no tokens and reported
    to the user as live.
    """
    a = args or {}
    op = (a.get("op") or "create").strip()
    if op not in ("create", "write", "render", "list", "convert", "delete",
                  "history", "revert", "set_desktop", "set_wallpaper", "promote", "import",
                  "publish_design", "desktop_from_url"):
        return _refused(f"op must be one of create/write/render/list/convert/delete/history/revert/set_desktop/set_wallpaper/promote/import/publish_design/desktop_from_url (got {op!r})")
    try:
        from server import brain_service
        from server.applications import artifacts as artifact_service
        from server.applications.artifacts import ArtifactError
        pool = brain_service.get_pool()
        if pool is None:
            return {"status": "error", "result": "artifact failed: pool unavailable"}
        eid = brain_service._entity_id
        try:
            if op == "create":
                entry = a.get("entry") or "index.html"
                kind = str(a.get("kind") or "artifact").strip().lower()
                files = a.get("files")
                if kind == "desktop" and not files:
                    # Seed from the template as an OPERATION. The alternative is
                    # the model retyping a 20 KB document it was handed in its
                    # own skill, which is both the slow path and the one that
                    # loses the mode meta and the tokens on the way through.
                    from server.applications.desktop_contract import TEMPLATE_HTML
                    files = [{"path": entry, "content": TEMPLATE_HTML}]
                name = str(a.get("name") or "").strip()
                description = str(a.get("description") or "")
                name_defaulted = False
                if not name:
                    # A missing name is clerical, not a reason to refuse. The
                    # schema marks only `op` required (a JSON schema cannot say
                    # "required for create"), so the model learned at execution
                    # what the schema never said and retried. The kernel names the artifact itself — from
                    # the description, else the kind and the date — inside a
                    # SUBSET of the name rule's character class, checked against
                    # the rule itself, and says so in the result.
                    import re as _re
                    from datetime import datetime as _dt, timezone as _tz
                    cand = " ".join(_re.sub(r"[^\w .,()\-'’]", " ", description).split())
                    cand = cand[:60].strip(" .,-")
                    if not cand or not artifact_service.is_valid_artifact_name(cand):
                        cand = (f"{'Desktop' if kind == 'desktop' else 'Artifact'} "
                                f"{_dt.now(_tz.utc).strftime('%Y-%m-%d %H:%M')}")
                    name = cand
                    name_defaulted = True
                res = await artifact_service.create_artifact(
                    pool, eid,
                    name=name,
                    files=files,
                    entry=entry,
                    description=description)
                if name_defaulted and isinstance(res, dict):
                    res["name_defaulted"] = True
                # words_known: on create the name and description ARE the
                # call's own arguments, so the projects read below is pure
                # waste — every ordinary create would pay for a row it already
                # holds.
                await _attach_desktop_contract(
                    pool, eid, (res or {}).get("artifact_id"), res, entry=entry,
                    kind=kind, name=name, description=description,
                    words_known=True)
                await _note_missing_images(pool, eid, files, res)
            elif op == "write":
                # A write to the WORN desktop is checked in a browser before it
                # reaches the screen (desktop_pin.after_write); the Host waits
                # for that verdict, so the error and its line come back in
                # this result (`desktop`) and get fixed in the same turn.
                from server.applications.desktop_pin import HOST_WAIT_S
                res = await artifact_service.write_files(
                    pool, eid, a.get("artifact_id"), a.get("files") or [],
                    desktop_wait_s=HOST_WAIT_S)
                await _attach_desktop_contract(pool, eid, a.get("artifact_id"), res)
                await _note_missing_images(pool, eid, a.get("files"), res)
            elif op == "promote":
                from server.applications.desktop_pin import HOST_WAIT_S
                res = await artifact_service.promote_entry(
                    pool, eid, a.get("artifact_id"), str(a.get("from") or ""),
                    str(a.get("to") or "index.html"), actor="host",
                    desktop_wait_s=HOST_WAIT_S)
                await _attach_desktop_contract(pool, eid, a.get("artifact_id"), res,
                                               entry=str(a.get("to") or "index.html"))
            elif op == "import":
                from server.applications.desktop_pin import HOST_WAIT_S
                res = await artifact_service.import_files(
                    pool, eid, a.get("artifact_id"), a.get("project_id"),
                    a.get("files") or [], actor="host", desktop_wait_s=HOST_WAIT_S)
                await _attach_desktop_contract(pool, eid, a.get("artifact_id"), res)
                # The copies are new content in the artifact, as a write's
                # files are, so the image names they carry are checked the same
                # way. They are not in the call's arguments: read them back.
                try:
                    copied = [{"content": t} for t in artifact_service.workspace_texts(
                        a.get("artifact_id"),
                        [c.get("to") for c in ((res or {}).get("files") or [])
                         if isinstance(c, dict)])]
                except Exception:  # noqa: BLE001 - a note, never the import's failure
                    copied = []
                await _note_missing_images(pool, eid, copied, res)
            elif op == "render":
                html = await artifact_service.render(
                    pool, eid, a.get("artifact_id"), a.get("entry", "index.html"))
                res = {"artifact_id": a.get("artifact_id"), "html": html}
            elif op == "list":
                res = await artifact_service.list_artifacts(
                    pool, eid, limit=a.get("limit", 50), offset=a.get("offset", 0))
            elif op == "convert":
                res = await artifact_service.convert_to_app(pool, eid, a.get("artifact_id"))
            elif op == "history":
                # Every revision is kept: a page of them, newest first,
                # with total + next_offset for the rest.
                res = await artifact_service.list_history(
                    pool, eid, a.get("artifact_id"),
                    limit=a.get("limit") or 50, offset=a.get("offset", 0))
            elif op == "revert":
                from server.applications.desktop_pin import HOST_WAIT_S
                res = await artifact_service.revert_artifact(
                    pool, eid, a.get("artifact_id"), str(a.get("rev") or ""),
                    actor="host", desktop_wait_s=HOST_WAIT_S)
            elif op == "set_desktop":
                # The appearance route module owns the pointer (one writer for
                # PUT /api/appearance/desktop and this op). Lazy import: the
                # route module is not otherwise a runtime dependency here.
                from server.routes.appearance import set_desktop_artifact
                res = await set_desktop_artifact(
                    pool, eid, a.get("artifact_id"), a.get("entry") or "index.html")
            elif op == "set_wallpaper":
                from server.routes.appearance import set_wallpaper_from_image
                res = await set_wallpaper_from_image(
                    pool, eid, a.get("image_url"), a.get("variant") or "desktop")
            elif op == "desktop_from_url":
                res = await _desktop_from_url(pool, eid, str(a.get("url") or ""),
                                              str(a.get("name") or "").strip())
                if res.get("error"):
                    return {"status": "error", "result": res["error"]}
            elif op == "publish_design":
                # One publish path with the Designs app's button (name rule,
                # size ceiling, the floor). HTTPException carries its reason.
                from fastapi import HTTPException
                from server.routes.designs import publish_artifact
                aid = a.get("artifact_id")
                try:
                    res = await publish_artifact(
                        pool, int(aid) if aid is not None else None,
                        str(a.get("name") or ""))
                except HTTPException as he:
                    return _refused_if_caller(he, str(he.detail))
            else:  # delete
                res = await artifact_service.delete_artifact(pool, eid, a.get("artifact_id"))
        except ArtifactError as e:
            return _refused_if_caller(e, e.detail)
        return {"status": "ok", "result": res}
    except Exception as e:
        return _refused_if_caller(e, f"artifact failed: {getattr(e, 'detail', None) or e}")


async def _desktop_from_url(pool, eid: int, url: str, name: str) -> dict:
    """artifact op='desktop_from_url' — see the op list on _exec_artifact.
    Every step that can fail says which one, in plain words."""
    from urllib.parse import urljoin, urlsplit
    from mastercode.runtime import url_reader
    from server.applications import artifacts as artifact_service
    from server.applications import site_style
    from server.applications.desktop_contract import check_document, template_with_tokens

    page = await url_reader.fetch_raw(url)
    if not page.get("ok"):
        return {"error": f"could not read {url or 'that address'}: {page.get('error')}"}
    base = page["final_url"]
    # Every linked sheet, in order, until the reader has all the CSS it reads
    # (site_style.MAX_TOTAL_CSS) or the page's fetch backstop; any sheet not
    # read for either reason is named in the answer, never dropped unsaid.
    sheets, skipped, not_read = [], [], []
    css_bytes = fetched = 0
    for href in site_style.stylesheet_urls(page["body"]):
        if css_bytes >= site_style.MAX_TOTAL_CSS or fetched >= site_style.MAX_STYLESHEETS:
            not_read.append(href)
            continue
        fetched += 1
        sheet = await url_reader.fetch_raw(urljoin(base, href))
        if sheet.get("ok"):
            sheets.append(sheet["body"][:site_style.MAX_CSS_BYTES])
            css_bytes += len(sheets[-1])
        else:
            skipped.append(href)
    # Off the event loop: the reading is bounded (site_style caps every input
    # and expansion), but it is CPU work and the pod's HTTP shares this loop.
    import asyncio

    def _read():
        st = site_style.extract(page["body"], sheets)
        return st, site_style.tokens_for(st)

    style, tokens = await asyncio.to_thread(_read)
    host = (urlsplit(base).hostname or "").removeprefix("www.")
    title = name or f"Desktop — {style.get('title') or host}"[:80]
    name_note = None
    if not artifact_service.is_valid_artifact_name(title):
        if name:
            name_note = (f"the name {name!r} has characters an artifact name cannot "
                         "carry, so it was named after the site — rename it if you like")
        title = f"Desktop from {host}"[:80]
    doc = template_with_tokens(tokens, title=title)
    art = await artifact_service.create_artifact(
        pool, eid, name=title,
        files=[{"path": "index.html", "content": doc}],
        description=f"Desktop in the palette of {base}")
    report = check_document(doc)
    return {
        "artifact_id": art.get("artifact_id"),
        "name": title,
        "source": base,
        "stylesheets_read": len(sheets),
        "stylesheets_unreadable": skipped,
        **({"stylesheets_not_read": not_read,
            "stylesheets_note": (f"{len(not_read)} more stylesheet(s) were not read: the "
                                 f"reader had its {site_style.MAX_TOTAL_CSS:,} bytes of CSS, "
                                 f"or the page's {site_style.MAX_STYLESHEETS} fetches")}
           if not_read else {}),
        "taken_from_the_site": {
            "background": style.get("background"), "text": style.get("text"),
            "accent": style.get("accent"), "font_body": style.get("font_body"),
            "font_display": style.get("font_display"),
        },
        "observed": style.get("observed"),
        "tokens": tokens,
        "contract": {"ok": report["ok"], "errors": report["errors"], "warnings": report["warnings"]},
        **({"name_note": name_note} if name_note else {}),
        "next": ("It is NOT the desktop yet. Show it (open_app artifacts, view "
                 "artifact:<id>), refine the design with the user via op='write', then "
                 "op='set_desktop'. Anything taken_from_the_site left null used a "
                 "readable neutral — say so."),
    }


# A name or description that says what the artifact is for. The whole point of
# this trigger is the document that IGNORES the floor: it declares no mode and
# no tokens, so nothing in the bytes says "desktop" and only the host's own
# words do.
_DESKTOP_WORD_RE = re.compile(r"desktop|wallpaper", re.IGNORECASE)


def _is_html_entry(entry: str) -> bool:
    return str(entry or "").strip().lower().endswith((".html", ".htm"))


async def _artifact_words(pool, eid: int, artifact_id) -> str:
    """The artifact's own name + description, for the trigger below. Read only
    when the cheaper signals have already said no, so an ordinary write pays
    nothing for it. Best-effort: an unreadable row just means no trigger."""
    try:
        row = await pool.fetchrow(
            "SELECT name, description FROM projects "
            "WHERE id = $1 AND entity_id = $2 AND kind = 'artifact'",
            int(artifact_id), eid)
    except Exception as e:  # noqa: BLE001 — never fail a write on a lookup
        log.debug("artifact name lookup skipped: %s", e)
        return ""
    if row is None:
        return ""
    return f"{row['name'] or ''} {row['description'] or ''}"


def _prospect_report(report: dict) -> str:
    """The NAME-triggered wording. Nothing was refused here: the host called
    something "desktop" and the kernel is pointing out that the document is not
    one yet. Saying "REFUSED" for that is a lie about what happened, and it
    trains the host to read a real refusal as noise."""
    lines = ["Not a desktop document — nothing was refused, and this artifact is "
             "fine as it is."]
    if report.get("errors"):
        lines.append("If it is meant to BE the desktop, it still needs:")
        lines += [f"  · {e}" for e in report["errors"]]
    lines.append("Or create it with kind='desktop' to start from the template "
                 "(default-skills/desktop.md).")
    return "\n".join(lines)


async def _note_missing_images(pool, eid, files, res) -> None:
    """An artifact may name the Host's own images by /api/images/<id>
    (artifacts.inline_own_images puts them in when it renders). A name that is
    not one of them renders as a broken image, silently, so the write's result
    says which, in the turn that can fix them. Best-effort."""
    try:
        from server.applications import artifacts as artifact_service
        texts = [str((f or {}).get("content") or "") for f in (files or []) if isinstance(f, dict)]
        missing = await artifact_service.missing_own_images(pool, eid, texts)
    except Exception:  # noqa: BLE001 - a note, never the write's failure
        return
    if missing and isinstance(res, dict):
        res["images_missing"] = {
            "names": missing,
            "note": ("not among your images, so they render as broken images; "
                     "image_generate(action='gallery') lists yours")}


async def _attach_desktop_contract(pool, eid: int, artifact_id, res: dict,
                                   entry: str = "index.html", *, kind=None,
                                   name=None, description=None,
                                   words_known: bool = False) -> None:
    """After a create/write/promote/import: attach the floor's verdict to the
    tool result as `contract` = {ok, mode, errors, warnings, trigger} plus a
    readable `contract_report`. Informational here (iteration must stay live);
    the hard refusal is set_desktop's. Best-effort — never fails the write.

    It attaches when the entry is an HTML document AND one of three triggers
    fires, and `contract.trigger` says WHICH, because the three do not mean the
    same thing:
      · 'declared' — the document declares a mode meta or cseries-tokens. It is
        asking to be a desktop, so the floor's verdict is about it.
      · 'kind'     — created with kind='desktop'. The host said so.
      · 'name'     — only the artifact's name or description says
        desktop/wallpaper. Nothing has been refused: a name is not a claim about
        the bytes, so this one gets _prospect_report's wording and never the
        REFUSED prefix. REFUSED stays for set_desktop and for the two triggers
        above.

    The last two triggers exist because of a real case: an artifact
    was created with no meta and no tokens, so the old declares-something test
    said "ordinary artifact" and stayed silent — and create never called this at
    all. The host got no verdict from either op, never called set_desktop, and
    told the user the desktop was live. A document that ignores the floor
    completely is exactly the one that has to hear about it.

    words_known: the caller already holds the artifact's name and description
    (create passes its own arguments), so the projects read is skipped.
    """
    try:
        if not _is_html_entry(entry) or artifact_id is None:
            return
        from server.routes.appearance import desktop_contract_report
        from server.applications.desktop_contract import format_report
        report = await desktop_contract_report(pool, eid, artifact_id, entry)
        if report is None:
            return  # the entry does not render — the write result already says so
        if report.get("mode") is not None or report.get("tokens"):
            trigger = "declared"
        elif str(kind or "").strip().lower() == "desktop":
            trigger = "kind"
        else:
            words = f"{name or ''} {description or ''}"
            if not _DESKTOP_WORD_RE.search(words) and not words_known:
                words = await _artifact_words(pool, eid, artifact_id)
            if not _DESKTOP_WORD_RE.search(words):
                return  # an ordinary artifact — nothing here is about the desktop
            trigger = "name"
        res["contract"] = dict(
            {k: report[k] for k in ("ok", "mode", "errors", "warnings")},
            trigger=trigger)
        text = _prospect_report(report) if trigger == "name" else format_report(report)
        if text:
            res["contract_report"] = text
    except Exception as e:  # noqa: BLE001
        log.debug("desktop contract report skipped: %s", e)


async def _exec_support_collect(args: dict) -> dict:
    """The operator Host's collect of the support queue (runtime/support_collect.py).
    Supplies the pod-local pieces the pure module injects: this host's pool and
    entity id. The module refuses a non-operator host itself; the platform
    refuses it again at the bridge."""
    from mastercode.runtime import support_collect as _sc
    from server import brain_service
    pool = brain_service.get_pool()
    return await _sc.run(args or {}, pool=pool, entity_id=int(brain_service._entity_id))


async def _exec_bug_review(args: dict, *, agent_grant: int | None,
                           session_id: str | None = None) -> dict:
    """The operator Host's door onto the bug cache (runtime/bug_review.py).

    This wrapper supplies the pod-local pieces the pure module injects:
    creating the one-shot "validate pending bug reports" prompt task (the
    same calendar row schedule_task writes) and checking whether one is
    already queued OR fired recently, so the deterministic standing poll can
    run every few minutes at no model cost and wake the host only when there
    is work — and never more than once per WAKE_COOLDOWN while a report sits
    unverdicted. `session_id` is the calling session — for a
    dispatched agent, its own — which ties a verdict to the report that same
    agent read.
    """
    from mastercode.runtime import bug_review as _br

    async def _wake_queued(title: str) -> bool:
        try:
            from server import brain_service
            pool = brain_service.get_pool()
            if pool is None:
                return False
            n = await pool.fetchval(
                "SELECT count(*) FROM calendar_events WHERE entity_id=$1 "
                "AND event_type='scheduled_task' AND title=$2 "
                "AND (status='scheduled' OR (status IN ('running','completed','failed') "
                "     AND event_at > NOW() - ($3::int * interval '1 second')))",
                int(brain_service._entity_id), title, int(_br.WAKE_COOLDOWN_SECONDS))
            return bool(n)
        except Exception as e:  # noqa: BLE001
            log.debug("bug_review wake_queued check failed: %s", e)
            return False

    async def _schedule_wake(title: str, prompt: str) -> dict:
        from datetime import datetime, timedelta, timezone

        from server.bridge.memory_handlers import handle_calendar_append
        fire_at = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
        env = await handle_calendar_append({
            "event_type": "scheduled_task", "summary": title, "event_at": fire_at,
            "status": "scheduled", "source": "entity",
            # One-shot: no recurrence, and no per-task ceiling — the ceiling
            # is per event row, so it would bound nothing here; the cool-down
            # in _wake_queued is what bounds the spend.
            "payload": {"prompt": prompt, "steps": []},
        })
        if not env.get("ok"):
            raise RuntimeError(env.get("error") or "could not schedule the validation turn")
        return {"scheduled": title, "event_id": env.get("event_id"), "fire_at": env.get("event_at")}

    return await _br.run(args, agent_grant=agent_grant, session_id=session_id,
                         schedule_wake=_schedule_wake, wake_queued=_wake_queued)


async def _exec_set_project_manifest(args: dict) -> dict:
    """Write + validate a project's app.yaml so the Workbench can ship it. Calls the projects
    set_project_manifest_service directly (no HTTP shim) — entity-scoped (a project you don't own is
    'not found') and the manifest is rolled back if it fails validation, so an invalid YAML is never
    persisted."""
    a = args or {}
    try:
        from server import brain_service
        from server.routes import projects
        pool = brain_service.get_pool()
        if pool is None:
            return {"status": "error", "result": "set_project_manifest failed: pool unavailable"}
        eid = brain_service._entity_id
        try:
            project_id = int(a.get("project_id"))
        except (TypeError, ValueError):
            return _refused("project_id (int) is required")
        res = await projects.set_project_manifest_service(pool, eid, project_id, a.get("yaml"))
        return {"status": "ok", "result": res}
    except Exception as e:
        return _refused_if_caller(e, f"set_project_manifest failed: {getattr(e, 'detail', None) or e}")


async def _exec_get_project(args: dict) -> dict:
    """Drill into one of the host's own projects: its phases (with step rollups), routing, and
    metadata. Calls the projects get_project_service directly (no HTTP shim) — entity-scoped,
    read-only."""
    a = args or {}
    try:
        from server import brain_service
        from server.routes import projects
        pool = brain_service.get_pool()
        if pool is None:
            return {"status": "error", "result": "get_project failed: pool unavailable"}
        eid = brain_service._entity_id
        try:
            project_id = int(a.get("project_id"))
        except (TypeError, ValueError):
            return _refused("project_id (int) is required")
        res = await projects.get_project_service(pool, eid, project_id)
        return {"status": "ok", "result": res}
    except Exception as e:
        return _refused_if_caller(e, f"get_project failed: {getattr(e, 'detail', None) or e}")


async def _exec_update_project(args: dict) -> dict:
    """Rename, re-status or archive one of the host's own projects. Calls the projects
    update_project_service directly (no HTTP shim) — entity-scoped; only the provided fields change.
    Pass status='archived' to archive. At least one field is required."""
    a = args or {}
    try:
        from server import brain_service
        from server.routes import projects
        pool = brain_service.get_pool()
        if pool is None:
            return {"status": "error", "result": "update_project failed: pool unavailable"}
        eid = brain_service._entity_id
        try:
            project_id = int(a.get("project_id"))
        except (TypeError, ValueError):
            return _refused("project_id (int) is required")
        res = await projects.update_project_service(
            pool, eid, project_id,
            name=a.get("name"), description=a.get("description"), status=a.get("status"),
            repo=a.get("repo"),
        )
        return {"status": "ok", "result": res}
    except Exception as e:
        return _refused_if_caller(e, f"update_project failed: {getattr(e, 'detail', None) or e}")


async def _exec_get_project_activity(args: dict) -> dict:
    """Read a project's live activity feed — meta-tool calls + deep_research findings, newest first.
    Calls the projects get_project_activity_service directly (no HTTP shim) — entity-scoped,
    read-only. Optional `since` (ISO timestamp) returns only newer events; `limit` defaults to 80,
    caps at 200."""
    a = args or {}
    try:
        from server import brain_service
        from server.routes import projects
        pool = brain_service.get_pool()
        if pool is None:
            return {"status": "error", "result": "get_project_activity failed: pool unavailable"}
        eid = brain_service._entity_id
        try:
            project_id = int(a.get("project_id"))
        except (TypeError, ValueError):
            return _refused("project_id (int) is required")
        try:
            limit = int(a.get("limit") or 80)
        except (TypeError, ValueError):
            limit = 80
        res = await projects.get_project_activity_service(
            pool, eid, project_id, since=a.get("since"), limit=limit,
        )
        return {"status": "ok", "result": res}
    except Exception as e:
        return _refused_if_caller(e, f"get_project_activity failed: {getattr(e, 'detail', None) or e}")


async def _exec_set_project_routing(args: dict) -> dict:
    """Set or clear the preferred model for one domain on a project (governs which model the Workbench
    routes that domain's steps to). Calls the projects set_project_routing_service directly (no HTTP
    shim) — entity-scoped. op='set' (default) needs domain + preferred_model; op='clear' needs domain
    only."""
    a = args or {}
    try:
        from server import brain_service
        from server.routes import projects
        pool = brain_service.get_pool()
        if pool is None:
            return {"status": "error", "result": "set_project_routing failed: pool unavailable"}
        eid = brain_service._entity_id
        try:
            project_id = int(a.get("project_id"))
        except (TypeError, ValueError):
            return _refused("project_id (int) is required")
        op = (a.get("op") or "set").strip().lower()
        clear = op == "clear"
        res = await projects.set_project_routing_service(
            pool, eid, project_id, a.get("domain"),
            preferred_model=a.get("preferred_model"), reason=a.get("reason"),
            clear=clear,
        )
        return {"status": "ok", "result": res}
    except Exception as e:
        return _refused_if_caller(e, f"set_project_routing failed: {getattr(e, 'detail', None) or e}")


# ---------------------------------------------------------------
# gitea — the host READS its own forge
# ---------------------------------------------------------------
#
# The consumer of the Gitea app's Permissions grant. server/routes/gitea.py
# grant_host_access adds the host (entity-<id>) as a collaborator on a USER
# repo; until this tool nothing on the host side could read what that grant
# opened — workspace_file / search_my_code are jailed to the host's own
# projects, `github` reads github.com only, and the sole way to clone an
# arbitrary gitea repo was mesh_harness_run on the user's own server.
#
# Every op is a GET through server.routes.gitea_client.gitea_request — the
# same per-entity BasicAuth credential and egress proxy the Gitea app's
# Host-repos panel and the Workbench PR surface use, so the host sees
# exactly the repos gitea says it may see (owned + collaborator). A private
# repo the host was NOT granted is a 404 from gitea, and the tool says so in
# those words instead of guessing.
#
# Bounds — this reads over HTTP one file at a time, so every op is capped
# and REPORTS its cap: a tree lists at most _GITEA_TREE_CAP entries (a
# prefix narrows the walk before the cap bites), a read returns one window of
# whole lines at a time (any size: past _GITEA_READ_CAP the raw blob is
# streamed) and refuses binary bytes, and grep scans at most
# _GITEA_GREP_FILES text blobs of at most _GITEA_GREP_FILE_BYTES each and
# returns at most _GITEA_GREP_HITS hits — with files_unscanned in the
# result whenever the budget cut the walk short. Silent truncation is the
# one failure a code-review tool must never have.

_GITEA_TREE_CAP = 4000            # entries returned by op='tree' (github's cap)
_GITEA_TREE_PAGE = 1000           # gitea's per_page ceiling for git/trees
_GITEA_TREE_MAX_PAGES = 40        # 40k entries walked at most, prefix or not
_GITEA_READ_CAP = 1_048_576       # inline (base64) read; past it, op='read'
                                  # streams the raw blob (_read_window) and grep
                                  # skips the file
_GITEA_GREP_FILES = 150           # text blobs fetched by one op='grep'
_GITEA_GREP_FILE_BYTES = 512 * 1024
_GITEA_GREP_HITS = 100
_GITEA_GREP_CONCURRENCY = 6
_GITEA_REPO_RE = r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$"
# Pull-request reads. A listing pages through the forge's own
# page size; one file's patch in an all-files listing is cut here, and the
# whole of it is one call away with `path` (paged by start_line/end_line).
_GITEA_PR_PAGE = 50
_GITEA_PR_PATCH_CHARS = 20_000
# The PR-review ops and the one comment write, beside the repository reads.
GITEA_READ_OPS_ALL = ("list", "repo", "tree", "read", "grep",
                      "pulls", "pull", "pull_files", "pull_reviews", "commit_status", "branches")
GITEA_WRITE_OPS = ("propose", "comment")
# Extensions never worth fetching for a text grep. Anything else is fetched
# and judged by its bytes (strict utf-8), so an unlisted binary is skipped
# too — this list only saves the round trip.
_GITEA_BINARY_EXT = frozenset({
    "png", "jpg", "jpeg", "gif", "webp", "ico", "svgz", "bmp", "tif", "tiff",
    "woff", "woff2", "ttf", "otf", "eot", "pdf", "zip", "gz", "tgz", "bz2",
    "xz", "7z", "tar", "jar", "war", "cseries", "bin", "so", "dll", "dylib",
    "pyc", "pyo", "class", "o", "a", "wasm", "mp3", "mp4", "wav", "ogg",
    "mov", "avi", "webm", "db", "sqlite", "sqlite3", "parquet", "npy", "npz",
})


async def _gitea_raw_chunks(path: str, params: dict | None = None, chunk_bytes: int = 65536):
    """Stream one blob's raw bytes from gitea (GET /api/v1/repos/…/raw/<path>),
    on the same per-entity credential and egress proxy gitea_request uses. Gitea
    serves a blob of any size raw; streaming lets op='read' keep one window and
    stop. Liveness, not a clock: no byte for 60 s fails; a file that keeps
    arriving does not."""
    import aiohttp
    from server.routes.gitea_client import gitea_creds, gitea_proxy
    cred = gitea_creds()
    if not cred:
        raise RuntimeError("no gitea credential on this host")
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=20, sock_read=60)
    async with aiohttp.ClientSession(timeout=timeout) as sess:
        async with sess.get(f"https://{cred[2]}{path}", params=params, proxy=gitea_proxy(),
                            auth=aiohttp.BasicAuth(cred[0], cred[1])) as r:
            if r.status != 200:
                raise RuntimeError(f"gitea raw fetch HTTP {r.status}: {(await r.text())[:200]}")
            async for chunk in r.content.iter_chunked(chunk_bytes):
                yield chunk


async def _gitea_user_login() -> str | None:
    """The Gitea login of this Host's user: the login on the token the
    platform mints for them (server/routes/gitea._ensure_user_token, which
    keeps it; user_login reads it). None when it cannot be had, and then only
    the Host's own repos count as its own."""
    try:
        from server.routes import gitea as _g
        await _g._ensure_user_token()
        return _g.user_login() or None
    except Exception as e:  # noqa: BLE001 - unknown is never trusted
        log.debug("gitea: the user's login could not be read: %s", e)
        return None


async def _gitea_foreign(repo: str, owner: str, host_login: str, get) -> str | None:
    """None when `repo` is this Host's or its user's: on the forge's own list
    for the Host (/api/v1/user/repos) AND owned, per the forge's `owner`
    field, by the Host's account or its user's. A repo another Gitea user
    owns and added the Host to as a collaborator is foreign. Else what to
    call it. The list is read once per turn and kept in the turn's state. A
    list that cannot be read counts as foreign: unknown is never trusted."""
    from mastercode.runtime import turn_taint
    if (owner or "").lower() == (host_login or "").lower():
        return None
    st = turn_taint.current()
    own = st.cache.get("gitea_user_repos") if st is not None else None
    if own is None:
        owners = {(host_login or "").lower()}
        user = await _gitea_user_login()
        if user:
            owners.add(user.lower())
        owners.discard("")
        own = set()
        for page in range(1, 9):
            code, data = await get("/api/v1/user/repos", params={"limit": 50, "page": page})
            if code != 200 or not isinstance(data, list):
                return f"{repo} (the forge's list could not be read)"
            for r in data:
                if not isinstance(r, dict):
                    continue
                o = str(((r.get("owner") or {}) if isinstance(r.get("owner"), dict) else {})
                        .get("login") or "").lower()
                if o and o in owners:
                    own.add(str(r.get("full_name") or "").lower())
            if len(data) < 50:
                break
        if st is not None:
            st.cache["gitea_user_repos"] = own
    return None if (repo or "").lower() in own else repo


async def _gitea_mark_if_foreign(repo: str, owner: str, host_login: str, get) -> None:
    """Mark the turn tainted when `repo` is not this Host's or its user's
    (_gitea_foreign)."""
    from mastercode.runtime import turn_taint
    st = turn_taint.current()
    if st is None or st.source is not None:
        return
    why = await _gitea_foreign(repo, owner, host_login, get)
    if why:
        turn_taint.mark(turn_taint.FOREIGN_REPO, why)


async def _forge_repo_foreign(repo: str) -> str | None:
    """_gitea_foreign for a caller that holds only a repo's forge name: None
    when `repo` is this Host's or its user's, else what to call it. No forge
    identity, or a name that is not owner/name, is foreign: unknown is never
    trusted."""
    import re
    repo = str(repo or "").strip().strip("/")
    if not re.match(_GITEA_REPO_RE, repo):
        return repo or "a repository that was not named"
    try:
        from server.routes.gitea_client import gitea_creds, gitea_request
    except Exception as e:  # noqa: BLE001 - import shape, not runtime
        return f"{repo} (the forge could not be asked: {e})"
    cred = gitea_creds()
    if not cred:
        return f"{repo} (this Host has no forge identity)"

    async def _get(path: str, params=None):
        try:
            return 200, await gitea_request("GET", path, params=params)
        except Exception as e:  # noqa: BLE001
            st = getattr(e, "status_code", None)
            return (int(st) if st else 0), (getattr(e, "detail", None) or str(e))
    return await _gitea_foreign(repo, repo.split("/", 1)[0], cred[0], _get)


# Why a pool harness run's transcript marks the turn whatever its repo: a
# pool harness pod reaches the public internet through the node's egress
# proxy (privatae controller manifests/0009_harness_egress_proxy.yaml, `http_access
# allow harness_clients`: every public destination outside the platform's
# own), and its agent can fetch a URL through the /mcp 'fetch' scope
# (read_url). What it read on the way is not known here.
POOL_HARNESS_OPEN_WEB = "a harness run in the pool with the public internet in reach"


async def _mark_harness_target(repo: str | None, what: str) -> None:
    """A pool harness run's transcript marks the turn, whatever repo the run
    worked on (runtime/turn_taint), the same way a mesh_harness_run marks.
    A run on the Host's own repo or its user's does not leave the turn
    clean: the run's pod reaches the open web (POOL_HARNESS_OPEN_WEB), so
    an own repo said nothing about what the transcript carries. Fail closed:
    the target only names what the run was for in the turn's record."""
    from mastercode.runtime import turn_taint
    target = str(repo or "").strip() or "no forge repo"
    turn_taint.mark_harness(POOL_HARNESS_OPEN_WEB, f"{what} (target {target})")


def _gitea_pr_number(a: dict) -> int | None:
    try:
        n = int(a.get("number"))
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _gitea_login(u) -> str:
    return str((u or {}).get("login") or "") if isinstance(u, dict) else ""


async def _gitea_mark_foreign_authors(logins, host_login: str, what: str) -> None:
    """A pull request, review or comment written by someone other than this
    Host or its user is another party's words: mark the turn (turn_taint), the
    way a foreign repository's code is marked. Unknown user login counts only
    the Host's own as its own."""
    from mastercode.runtime import turn_taint
    st = turn_taint.current()
    if st is None or st.source is not None:
        return
    own = {(host_login or "").lower()}
    user = await _gitea_user_login()
    if user:
        own.add(user.lower())
    others = sorted({str(x) for x in logins if x and str(x).lower() not in own})
    if others:
        turn_taint.mark(turn_taint.MESSAGE, f"{what} by {', '.join(others[:5])}")


def _split_pr_diff(diff_text: str) -> dict:
    """{path: that file's section of a unified diff} — keyed by the new path
    (the old one for a deletion)."""
    import re
    out: dict = {}
    cur = None
    buf: list = []
    for line in diff_text.split("\n"):
        m = re.match(r"^diff --git a/(.+?) b/(.+)$", line)
        if m:
            if cur is not None:
                out[cur] = "\n".join(buf)
            cur, buf = m.group(2), [line]
            continue
        if cur is not None:
            buf.append(line)
    if cur is not None:
        out[cur] = "\n".join(buf)
    return out


def _gitea_http_failure(what: str, st, body) -> dict:
    """A pull-request read the forge answered with something other than the
    page asked for. A 4xx other than 429 is the forge refusing what the call
    named (a repo, PR, ref or parameter this Host cannot read), which the
    model can see and fix: refused(). Anything else (a 5xx, a 429, no answer,
    a body of the wrong shape) is the forge's or the network's failure, the
    platform's, so it stays a plain error."""
    try:
        code = int(st)
    except (TypeError, ValueError):
        code = 0
    text = f"gitea {what} HTTP {st}: {str(body)[:200]}"
    if 400 <= code < 500 and code != 429:
        return _refused(text)
    return {"status": "error", "result": text}


async def _gitea_pr_read(op: str, a: dict, *, repo: str, base: str, use_ref: str,
                         number: int | None, host_login: str, get, page_of) -> dict:
    """The pull-request reads of `gitea`: what a reviewer
    needs and Claude Code reads over the forge API — the open PRs, one PR with
    its conversation, its changed files with their patches, its reviews, a
    commit's CI state, and the branches. GETs only."""
    from urllib.parse import quote

    if op == "pulls":
        state = str(a.get("state") or "open").strip().lower()
        if state not in ("open", "closed", "all"):
            return _refused("state must be open, closed or all")
        want = page_of("limit", 30, 1, 200)
        params = {"state": state, "limit": _GITEA_PR_PAGE, "sort": "recentupdate"}
        if (a.get("base") or "").strip():
            params["base_branch"] = str(a["base"]).strip()
        rows: list = []
        page = 1
        more = False
        while len(rows) < want:
            st, data = await get(f"{base}/pulls", params={**params, "page": page})
            if st != 200 or not isinstance(data, list):
                return _gitea_http_failure(f"{base}/pulls", st, data)
            for p in data:
                if not isinstance(p, dict):
                    continue
                rows.append({
                    "number": p.get("number"), "title": p.get("title"), "state": p.get("state"),
                    "author": _gitea_login(p.get("user")), "draft": bool(p.get("draft")),
                    "head": (p.get("head") or {}).get("ref"),
                    "head_repo": ((p.get("head") or {}).get("repo") or {}).get("full_name"),
                    "head_sha": (p.get("head") or {}).get("sha"),
                    "base": (p.get("base") or {}).get("ref"),
                    "mergeable": p.get("mergeable"), "merged": bool(p.get("merged")),
                    "comments": p.get("comments"), "updated_at": p.get("updated_at"),
                    "url": p.get("html_url")})
            if len(data) < _GITEA_PR_PAGE:
                break
            page += 1
            more = True
        clipped = len(rows) > want or (more and len(rows) >= want)
        rows = rows[:want]
        await _gitea_mark_foreign_authors([r["author"] for r in rows], host_login,
                                          f"pull requests on {repo}")
        return {"status": "ok", "result": {
            "repo": repo, "state": state, "pulls": rows, "count": len(rows), "clipped": clipped,
            **({"note": f"capped at {want} — raise 'limit' (max 200) or pass 'base'"}
               if clipped else {})}}

    if op == "branches":
        want = page_of("limit", 100, 1, 500)
        rows = []
        page = 1
        clipped = False
        while True:
            st, data = await get(f"{base}/branches", params={"limit": _GITEA_PR_PAGE, "page": page})
            if st != 200 or not isinstance(data, list):
                return _gitea_http_failure(f"{base}/branches", st, data)
            for b in data:
                if isinstance(b, dict):
                    rows.append({"name": b.get("name"),
                                 "sha": (b.get("commit") or {}).get("id"),
                                 "protected": bool(b.get("protected")),
                                 "you_can_push": bool(b.get("user_can_push")),
                                 "you_can_merge": bool(b.get("user_can_merge"))})
            if len(data) < _GITEA_PR_PAGE:
                break
            if len(rows) >= want:
                clipped = True
                break
            page += 1
        clipped = clipped or len(rows) > want
        rows = rows[:want]
        return {"status": "ok", "result": {
            "repo": repo, "branches": rows, "count": len(rows), "clipped": clipped,
            **({"note": f"capped at {want} — raise 'limit' (max 500)"} if clipped else {})}}

    if op == "commit_status":
        ref = (a.get("ref") or "").strip()
        n = _gitea_pr_number(a)
        if not ref and n is not None:
            st, pr = await get(f"{base}/pulls/{n}")
            if st == 404:
                return _refused(f"{repo} has no pull request #{n}")
            if st != 200 or not isinstance(pr, dict):
                return _gitea_http_failure(f"pull #{n}", st, pr)
            ref = (pr.get("head") or {}).get("sha") or ""
        ref = ref or use_ref
        st, data = await get(f"{base}/commits/{quote(ref, safe='')}/status",
                             params={"limit": 50})
        if st == 404:
            return _refused(f"{ref!r} is not a commit or branch of {repo}")
        if st != 200 or not isinstance(data, dict):
            return _gitea_http_failure("commit status", st, data)
        checks = [{"context": c.get("context"), "state": c.get("status") or c.get("state"),
                   "description": c.get("description"), "url": c.get("target_url"),
                   "updated_at": c.get("updated_at") or c.get("created_at")}
                  for c in (data.get("statuses") or []) if isinstance(c, dict)]
        return {"status": "ok", "result": {
            "repo": repo, "ref": ref, "sha": data.get("sha"),
            # The forge reports "" or "pending" for a commit with no checks.
            "state": data.get("state") or ("none" if not checks else None),
            "checks": checks, "count": len(checks)}}

    # One pull request: pull, pull_files, pull_reviews.
    st, pr = await get(f"{base}/pulls/{number}")
    if st == 404:
        return _refused(f"{repo} has no pull request #{number}")
    if st != 200 or not isinstance(pr, dict):
        return _gitea_http_failure(f"pull #{number}", st, pr)
    author = _gitea_login(pr.get("user"))

    if op == "pull":
        st, comments = await get(f"{base}/issues/{number}/comments")
        comments = comments if st == 200 and isinstance(comments, list) else []
        rows = [{"id": c.get("id"), "author": _gitea_login(c.get("user")),
                 "body": c.get("body") or "", "created_at": c.get("created_at")}
                for c in comments if isinstance(c, dict)]
        await _gitea_mark_foreign_authors([author] + [r["author"] for r in rows], host_login,
                                          f"pull request #{number} on {repo}")
        head = pr.get("head") or {}
        return {"status": "ok", "result": {
            "repo": repo, "number": number, "title": pr.get("title"), "body": pr.get("body") or "",
            "state": pr.get("state"), "author": author, "draft": bool(pr.get("draft")),
            "head": head.get("ref"), "head_repo": (head.get("repo") or {}).get("full_name"),
            "head_sha": head.get("sha"), "base": (pr.get("base") or {}).get("ref"),
            "merge_base": pr.get("merge_base"), "mergeable": pr.get("mergeable"),
            "merged": bool(pr.get("merged")), "merged_at": pr.get("merged_at"),
            "merged_by": _gitea_login(pr.get("merged_by")),
            "additions": pr.get("additions"), "deletions": pr.get("deletions"),
            "changed_files": pr.get("changed_files"), "updated_at": pr.get("updated_at"),
            "url": pr.get("html_url"), "comments": rows,
            **({"note": f"the conversation could not be read (HTTP {st})"} if st != 200 else {})}}

    await _gitea_mark_foreign_authors([author], host_login, f"pull request #{number} on {repo}")

    if op == "pull_reviews":
        st, reviews = await get(f"{base}/pulls/{number}/reviews", params={"limit": 50})
        if st != 200 or not isinstance(reviews, list):
            return _gitea_http_failure(f"reviews of #{number}", st, reviews)
        out = []
        for r in reviews:
            if not isinstance(r, dict):
                continue
            row = {"id": r.get("id"), "author": _gitea_login(r.get("user")), "state": r.get("state"),
                   "body": r.get("body") or "", "commit_id": r.get("commit_id"),
                   "submitted_at": r.get("submitted_at"), "stale": bool(r.get("stale")),
                   "dismissed": bool(r.get("dismissed")), "comments": []}
            if int(r.get("comments_count") or 0) > 0 and r.get("id") is not None:
                cst, cs = await get(f"{base}/pulls/{number}/reviews/{r['id']}/comments")
                if cst == 200 and isinstance(cs, list):
                    row["comments"] = [{"path": c.get("path"),
                                        "line": c.get("position") or c.get("original_position"),
                                        "author": _gitea_login(c.get("user")),
                                        "body": c.get("body") or ""}
                                       for c in cs if isinstance(c, dict)]
                else:
                    row["comments_error"] = f"HTTP {cst}"
            out.append(row)
        await _gitea_mark_foreign_authors(
            [r["author"] for r in out] + [c["author"] for r in out for c in r["comments"]],
            host_login, f"reviews of pull request #{number} on {repo}")
        return {"status": "ok", "result": {"repo": repo, "number": number, "reviews": out,
                                           "count": len(out)}}

    # pull_files: the changed files with their patches.
    files: list = []
    page = 1
    while page <= 20:
        st, data = await get(f"{base}/pulls/{number}/files",
                             params={"limit": _GITEA_PR_PAGE, "page": page})
        if st != 200 or not isinstance(data, list):
            return _gitea_http_failure(f"files of #{number}", st, data)
        files.extend({"path": f.get("filename"), "previous_path": f.get("previous_filename") or None,
                      "status": f.get("status"), "additions": f.get("additions"),
                      "deletions": f.get("deletions")} for f in data if isinstance(f, dict))
        if len(data) < _GITEA_PR_PAGE:
            break
        page += 1
    # The diff is text, not JSON: read raw.
    try:
        from server.routes.gitea_client import gitea_request
        diff_text = await gitea_request("GET", f"{base}/pulls/{number}.diff", raw_text=True)
    except Exception as e:  # noqa: BLE001 — the file list stands without patches
        diff_text = None
        log.warning("gitea pull_files: diff of %s#%s unreadable: %s", repo, number, e)
    patches = _split_pr_diff(diff_text) if isinstance(diff_text, str) else {}
    want_path = (a.get("path") or "").strip().strip("/")
    if want_path:
        patch = patches.get(want_path)
        if patch is None:
            return _refused(f"pull request #{number} does not change {want_path!r} "
                            "(op='pull_files' without path lists what it changes)")
        w = await _read_window(_bytes_as_chunks(patch.encode("utf-8")),
                               a.get("start_line"), a.get("end_line"))
        asked = a.get("start_line") is not None or a.get("end_line") is not None
        return {"status": "ok", "result": {
            "repo": repo, "number": number, "path": want_path, "kind": "patch",
            **_window_result(w, asked_window=asked, size=len(patch))}}
    cut = []
    for f in files:
        p = patches.get(f["path"] or "")
        if p is None:
            f["patch"] = None
            continue
        if len(p) > _GITEA_PR_PATCH_CHARS:
            f["patch"] = p[:_GITEA_PR_PATCH_CHARS]
            f["patch_clipped"] = True
            cut.append(f["path"])
        else:
            f["patch"] = p
    notes = []
    if cut:
        notes.append(f"{len(cut)} patch(es) cut at {_GITEA_PR_PATCH_CHARS} characters — read one "
                     "whole with path='<file>' (paged by start_line/end_line)")
    if diff_text is None:
        notes.append("the diff could not be read; the file list stands")
    return {"status": "ok", "result": {
        "repo": repo, "number": number, "head_sha": (pr.get("head") or {}).get("sha"),
        "files": files, "count": len(files),
        **({"note": "; ".join(notes)} if notes else {})}}


async def _gitea_comment(a: dict) -> dict:
    """gitea op='comment': one comment on a pull request or issue of a repo
    the forge shows this Host — the review's findings, posted where the
    author reads them. The forge decides who may comment; nothing is pushed
    and nothing is merged."""
    import re
    from urllib.parse import quote
    repo = (a.get("repo") or "").strip().strip("/")
    if not re.match(_GITEA_REPO_RE, repo):
        return _refused("repo must look like 'owner/name' on the forge")
    n = _gitea_pr_number(a)
    if n is None:
        return _refused("op='comment' needs `number`: the pull request's (or issue's) number")
    body = str(a.get("body") or "").strip()
    if not body:
        return _refused("op='comment' needs a non-empty `body`")
    try:
        from server.routes.gitea_client import gitea_request
        owner, name = repo.split("/", 1)
        r = await gitea_request(
            "POST", f"/api/v1/repos/{quote(owner, safe='')}/{quote(name, safe='')}/issues/{n}/comments",
            json={"body": body})
    except Exception as e:
        st = getattr(e, "status_code", None)
        if st == 404:
            return _refused(f"{repo} has no pull request or issue #{n} this Host can see")
        return _refused_if_caller(e, f"gitea comment failed: {getattr(e, 'detail', None) or e}")
    r = r if isinstance(r, dict) else {}
    return {"status": "ok", "result": {"repo": repo, "number": n, "comment_id": r.get("id"),
                                       "url": r.get("html_url")}}


async def _exec_gitea(args: dict, entity_id: int | None = None, pool=None) -> dict:
    """gitea — op=list|repo|tree|read|grep over the host's own gitea (reads),
    plus op=propose: fork → branch → one commit → PR to a repo the
    host does NOT own. The reads below only ever issue GET; propose lives in
    server/routes/gitea_propose.py, whose only non-GET requests are "create
    fork" and "open/update pull request" — never a push to the upstream.

    Result shapes (all under {"status": "ok", "result": ...}):
      propose {pr_url, pr_number, fork, branch, base, commit, files, updated,
             fork_synced, note}
      list  {host, repos:[{repo, private, default_branch, permission,
             owned_by_host, updated_at, description}], count}
      repo  {repo, private, default_branch, permission, owned_by_host,
             updated_at, description, url}
      tree  {repo, ref, prefix, entries:[{path,type,size}], count, clipped,
             total_count}
      read  {repo, ref, path, content, size, total_lines, lines:[a,b]|None,
             next_start_line?, note?} — one page of whole lines; any size
            or {…, kind:"dir", entries:[{name,type,size}]}
      grep  {repo, ref, pattern, regex, prefix, glob, hits:[{path,line,text}],
             count, clipped, files_scanned, files_skipped, files_unscanned}
    Errors are {"status": "error", "result": <sentence>}; a repo gitea will
    not show this host is reported as not-found-or-not-granted, never guessed.
    """
    import asyncio
    import base64
    import fnmatch
    import re
    from urllib.parse import quote

    a = args or {}
    op = (a.get("op") or "").strip()
    if op == "comment":
        # A comment is an outward channel: its body is published where other
        # people read it. On a turn that read untrusted content, the words may
        # be someone else's, so it waits for a clean turn (the wall a
        # mgmt_run write and a keys_manage use sit behind). Checked before
        # the grant is read: refusing it needs no database.
        from mastercode.runtime import turn_taint
        why = turn_taint.action_refusal("a comment on the forge (gitea op='comment')")
        if why is not None:
            return {"status": "denied", "reason": "untrusted_turn", "result": f"Not run: {why}."}
    if op in GITEA_WRITE_OPS:
        # The writes. The capability gate (_authorize_meta_tool) checks
        # BITS, and bit 32 is what the read ops need too — a read-level
        # external.operator grant holds it. So the writes are gated on the
        # SCOPE here: write:gitea, which only a read_write grant carries
        # (meta_tool_caps.DOMAIN_ACCESS_TO_SCOPES). Fail closed: no pool, no
        # entity, or an unreadable grant table all deny.
        try:
            from mastercode.runtime.meta_tool_caps import _resolve_entity_scopes
            if pool is None:
                from server import brain_service
                pool = brain_service.get_pool()
            if entity_id is None:
                from server import brain_service
                entity_id = brain_service._entity_id
            scopes = await _resolve_entity_scopes(pool, int(entity_id)) if pool is not None else set()
        except Exception as e:  # noqa: BLE001
            scopes = set()
            log.warning("gitea %s: scope resolution failed, denying: %s", op, e)
        if "write:gitea" not in scopes:
            return {"status": "denied",
                    "result": f"op={op!r} needs the write:gitea scope (a read_write grant on "
                              "external.operator); this host holds read access only"}
        if op == "comment":
            return await _gitea_comment(a)
        # The root thread proposes only to a repo that is its user's (the
        # forge's own list, as its reads): another party's repo is refused
        # there before anything is sent (runtime/root_turn.admits lets the
        # call this far).
        from mastercode.runtime import root_turn as _root_p
        if _root_p.in_root_turn():
            _foreign = await _forge_repo_foreign(str(a.get("repo") or ""))
            if _foreign:
                return _refused(f"gitea: {_foreign} is neither yours nor your user's; your root "
                                "thread proposes only to those. Propose it from another thread.")
        # It validates everything before touching gitea and raises
        # HTTPException with a plain sentence on every refusal.
        try:
            from server.routes.gitea_propose import propose
        except Exception as e:  # pragma: no cover — import shape, not runtime
            return _refused_if_caller(e, f"gitea propose unavailable in this pod: {e}")
        try:
            return {"status": "ok", "result": await propose(a)}
        except Exception as e:
            return _refused_if_caller(e, str(getattr(e, "detail", None) or e))
    if op not in GITEA_READ_OPS_ALL:
        return _refused("op must be one of " + "|".join(GITEA_READ_OPS_ALL + GITEA_WRITE_OPS))

    repo = (a.get("repo") or "").strip().strip("/")
    if op != "list" and not re.match(_GITEA_REPO_RE, repo):
        return _refused(f"repo must look like 'owner/name' on the forge (e.g. 'owner/repo') for op={op!r}")
    ref = (a.get("ref") or "").strip()

    # Argument checks that need no request — refuse before touching gitea.
    read_path = ""
    if op == "read":
        read_path = (a.get("path") or "").strip().strip("/")
        if not read_path or ".." in read_path.split("/"):
            return _refused("a clean repo-relative 'path' is required for op='read'")
    pr_number = None
    if op in ("pull", "pull_files", "pull_reviews"):
        pr_number = _gitea_pr_number(a)
        if pr_number is None:
            return _refused(f"op={op!r} needs `number`: the pull request's number "
                            "(op='pulls' lists them)")
    prefix = ""
    if op in ("tree", "grep"):
        prefix = (a.get("path") or "").strip().strip("/")
        if ".." in prefix.split("/"):
            return _refused("'path' must be a clean repo-relative directory prefix")
    matcher = None
    pattern = ""
    if op == "grep":
        pattern = a.get("pattern") or ""
        if not pattern.strip():
            return _refused("op='grep' needs a non-empty 'pattern'")
        flags = re.IGNORECASE if a.get("ignore_case") else 0
        if a.get("regex"):
            try:
                rx = re.compile(pattern, flags)
            except re.error as e:
                return _refused(f"pattern does not compile as a regular expression: {e}")
            matcher = rx.search
        elif flags:
            needle = pattern.lower()
            matcher = lambda line: needle in line.lower()  # noqa: E731
        else:
            matcher = lambda line: pattern in line  # noqa: E731
    glob = (a.get("glob") or "").strip()

    def _int(key, default, lo, hi):
        try:
            v = int(a.get(key)) if a.get(key) is not None else default
        except (TypeError, ValueError):
            v = default
        return max(lo, min(v, hi))

    try:
        from server.routes.gitea_client import gitea_creds, gitea_netloc, gitea_request
    except Exception as e:  # pragma: no cover — import shape, not runtime
        return _refused_if_caller(e, f"gitea client unavailable in this pod: {e}")

    cred = gitea_creds()
    if not cred:
        return {"status": "error",
                "result": "this host has no gitea identity yet (no credential in the pod) — the Gitea app's "
                          "Host repos panel shows the same 'not connected' state; nothing to read until it is wired"}
    host_login = cred[0]
    netloc = gitea_netloc()

    async def _get(path: str, params=None):
        """(status, body). gitea_request raises on non-2xx; fold that into a
        status so every op reports 404 in the same words."""
        try:
            return 200, await gitea_request("GET", path, params=params)
        except Exception as e:
            st = getattr(e, "status_code", None)
            return (int(st) if st else 0), (getattr(e, "detail", None) or str(e))

    def _not_found() -> dict:
        return _refused(f"{repo!r} was not found on {netloc} for this host ({host_login}) — it does not exist, "
                          f"or the user has not granted the host access to it (Gitea app → Permissions). "
                          f"Say that; do not guess at its contents.")

    def _perm(r: dict) -> str:
        p = r.get("permissions") or {}
        if p.get("admin"):
            return "admin"
        if p.get("push"):
            return "write"
        if p.get("pull"):
            return "read"
        return "none"

    def _repo_row(r: dict) -> dict:
        owner = ((r.get("owner") or {}).get("login") or "")
        return {"repo": r.get("full_name"), "private": bool(r.get("private")),
                "default_branch": r.get("default_branch"), "permission": _perm(r),
                "owned_by_host": owner == host_login, "updated_at": r.get("updated_at"),
                "description": (r.get("description") or "")[:200]}

    try:
        # ── list: owned + collaborator repos, as gitea's /user/repos reports ──
        if op == "list":
            want = _int("limit", 50, 1, 200)
            rows: list[dict] = []
            page = 1
            # The root thread lists only the Host's own repos and its user's
            # (_gitea_foreign's owners): another owner's description is that
            # owner's words.
            from mastercode.runtime import root_turn as _root_l
            _list_owners = None
            if _root_l.in_root_turn():
                _list_owners = {host_login.lower()}
                _u = await _gitea_user_login()
                if _u:
                    _list_owners.add(_u.lower())
            while len(rows) < want and page <= 4:
                st, data = await _get("/api/v1/user/repos",
                                      params={"limit": 50, "page": page, "sort": "updated"})
                if st != 200:
                    return {"status": "error", "result": f"gitea /user/repos HTTP {st}: {str(data)[:200]}"}
                batch = [r for r in (data or []) if isinstance(r, dict)]
                rows.extend(_repo_row(r) for r in batch
                            if _list_owners is None
                            or str((r.get("owner") or {}).get("login") or "").lower() in _list_owners)
                if len(batch) < 50:
                    break
                page += 1
            # Review note: the cap must be REPORTED like every other
            # op's — a silently shortened list reads as "that is all of them".
            clipped = len(rows) > want or (len(batch) == 50 and page > 4)
            rows = rows[:want]
            return {"status": "ok", "result": {
                "host": host_login, "gitea": netloc, "repos": rows, "count": len(rows),
                "clipped": clipped,
                "note": "owned_by_host=false rows are repos the user granted this host; "
                        "permission is what gitea reports for this host on each"
                        + (f"; listing capped at {want} — raise 'limit' (max 200)" if clipped else "")}}

        # ── every other op starts from the repo's facts (default branch + access) ──
        owner, name = repo.split("/", 1)
        base = f"/api/v1/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
        # The root thread reads only the Host's own repos and its user's
        # (runtime/root_turn): another party's repo is refused before any of
        # it is read, never read and then marked.
        from mastercode.runtime import root_turn as _root
        if _root.in_root_turn():
            _foreign = await _gitea_foreign(repo, owner, host_login, _get)
            if _foreign:
                return _refused(f"gitea: {_foreign} is neither yours nor one your user granted "
                                "you; your root thread reads only those. Read it from another thread.")
        st, meta = await _get(base)
        if st == 404:
            return _not_found()
        if st != 200 or not isinstance(meta, dict):
            return {"status": "error", "result": f"gitea {base} HTTP {st}: {str(meta)[:200]}"}
        use_ref = ref or meta.get("default_branch") or "main"
        # A repository that is neither this Host's nor its user's taints the
        # turn (runtime/turn_taint): its words are someone else's. "Its
        # user's" is the forge's own answer, /user/repos (owned + the repos
        # the user granted), never a second table here.
        await _gitea_mark_if_foreign(repo, owner, host_login, _get)

        if op == "repo":
            row = _repo_row(meta)
            row.update({"url": meta.get("html_url"), "size_kib": meta.get("size"),
                        "archived": bool(meta.get("archived")), "ref": use_ref})
            return {"status": "ok", "result": row}

        if op in ("pulls", "pull", "pull_files", "pull_reviews", "commit_status", "branches"):
            return await _gitea_pr_read(op, a, repo=repo, base=base, use_ref=use_ref,
                                        number=pr_number, host_login=host_login, get=_get,
                                        page_of=_int)

        async def _walk_tree() -> tuple[list[dict], int, bool]:
            """Recursive tree at use_ref, prefix-filtered, capped. Returns
            (entries, total_count_in_repo, clipped)."""
            out: list[dict] = []
            total = 0
            page = 1
            clipped = False
            pfx = prefix + "/" if prefix else ""
            while page <= _GITEA_TREE_MAX_PAGES:
                st, data = await _get(f"{base}/git/trees/{quote(use_ref, safe='')}",
                                      params={"recursive": "true", "per_page": _GITEA_TREE_PAGE, "page": page})
                if st == 404:
                    raise LookupError(f"ref {use_ref!r} not found in {repo}")
                if st != 200 or not isinstance(data, dict):
                    raise RuntimeError(f"gitea tree fetch HTTP {st}: {str(data)[:200]}")
                total = int(data.get("total_count") or total)
                for e in (data.get("tree") or []):
                    p = e.get("path") or ""
                    if pfx and not p.startswith(pfx):
                        continue
                    if len(out) >= _GITEA_TREE_CAP:
                        clipped = True
                        break
                    out.append({"path": p, "type": e.get("type"), "size": e.get("size")})
                if clipped or not data.get("truncated"):
                    break
                page += 1
            else:
                clipped = True
            return out, total, clipped

        if op == "tree":
            try:
                entries, total, clipped = await _walk_tree()
            except LookupError as e:
                return _refused(str(e))
            return {"status": "ok", "result": {
                "repo": repo, "ref": use_ref, "prefix": prefix or None,
                "entries": entries, "count": len(entries), "clipped": clipped,
                "total_count": total,
                **({"note": f"listing capped at {_GITEA_TREE_CAP} entries — pass a directory prefix in 'path'"}
                   if clipped else {})}}

        async def _read_blob(path: str) -> tuple[str, object]:
            """('ok', text) | ('dir', listing) | ('binary'|'too_large'|'missing'|'error', detail)."""
            st, data = await _get(f"{base}/contents/{quote(path)}", params={"ref": use_ref})
            if st == 404:
                return "missing", f"{path!r} not found in {repo}@{use_ref}"
            if st != 200:
                return "error", f"gitea contents fetch HTTP {st}: {str(data)[:200]}"
            if isinstance(data, list):
                return "dir", [{"name": e.get("name"), "type": e.get("type"), "size": e.get("size")}
                               for e in data if isinstance(e, dict)]
            if not isinstance(data, dict) or data.get("type") != "file":
                kind = data.get("type") if isinstance(data, dict) else "?"
                return "error", f"{path!r} is not a readable file (type={kind})"
            size = int(data.get("size") or 0)
            if size > _GITEA_READ_CAP or data.get("encoding") != "base64" or data.get("content") is None:
                return "too_large", size
            try:
                text = base64.b64decode(data["content"]).decode("utf-8", "strict")
            except (ValueError, UnicodeDecodeError):
                return "binary", size
            if "\x00" in text:
                return "binary", size
            return "ok", (text, size)

        if op == "read":
            kind, payload = await _read_blob(read_path)
            if kind == "dir":
                return {"status": "ok", "result": {"repo": repo, "ref": use_ref, "path": read_path,
                                                   "kind": "dir", "entries": payload}}
            if kind == "binary":
                return _refused(f"{read_path!r} is binary — this tool reads text files only.")
            if kind == "too_large":
                # Past the inline size: stream the raw blob and keep the window
                # (never refused whole, even for one window).
                size = int(payload or 0)
                chunks = _gitea_raw_chunks(f"{base}/raw/{quote(read_path)}", {"ref": use_ref})
            elif kind == "ok":
                text, size = payload
                chunks = _bytes_as_chunks(text.encode("utf-8"))
            else:
                return {"status": "error", "result": str(payload)}
            try:
                w = await _read_window(chunks, a.get("start_line"), a.get("end_line"))
            except Exception as e:  # noqa: BLE001 — a raw fetch that failed mid-way
                return _refused_if_caller(e, f"reading {read_path!r} ({size} bytes) failed: {getattr(e, 'detail', None) or e}")
            if w.get("binary"):
                return _refused(f"{read_path!r} is binary — this tool reads text files only.")
            asked = a.get("start_line") is not None or a.get("end_line") is not None
            return {"status": "ok", "result": {
                "repo": repo, "ref": use_ref, "path": read_path,
                **_window_result(w, asked_window=asked, size=size)}}

        # ── grep: bounded fetch-and-search over the text blobs of the tree ──
        want = _int("limit", 50, 1, _GITEA_GREP_HITS)
        try:
            entries, _total, tree_clipped = await _walk_tree()
        except LookupError as e:
            return _refused(str(e))
        candidates: list[str] = []
        skipped = 0
        for e in entries:
            if e.get("type") != "blob":
                continue
            p = e["path"]
            if glob and not (fnmatch.fnmatch(p, glob) or fnmatch.fnmatch(p.rsplit("/", 1)[-1], glob)):
                continue
            ext = p.rsplit(".", 1)[-1].lower() if "." in p.rsplit("/", 1)[-1] else ""
            if ext in _GITEA_BINARY_EXT or int(e.get("size") or 0) > _GITEA_GREP_FILE_BYTES:
                skipped += 1
                continue
            candidates.append(p)
        to_scan = candidates[:_GITEA_GREP_FILES]
        unscanned = len(candidates) - len(to_scan)
        if tree_clipped:
            # The walk itself was cut short: files past the tree cap were
            # never even listed, so the true unscanned count is unknown.
            unscanned = max(unscanned, 1)

        sem = asyncio.Semaphore(_GITEA_GREP_CONCURRENCY)
        hits: list[dict] = []
        scanned = 0
        fetched = 0   # files whose fetch completed (any outcome) — the rest never ran
        stop = asyncio.Event()

        async def _scan(path: str):
            nonlocal scanned, skipped, fetched
            if stop.is_set():
                return
            async with sem:
                if stop.is_set():
                    return
                kind, payload = await _read_blob(path)
            fetched += 1
            if kind != "ok":
                if kind in ("binary", "too_large"):
                    skipped += 1
                return
            scanned += 1
            text, _size = payload
            for i, line in enumerate(text.split("\n"), 1):
                if matcher(line):
                    hits.append({"path": path, "line": i, "text": line.strip()[:300]})
                    if len(hits) >= want:
                        stop.set()
                        return

        await asyncio.gather(*(_scan(p) for p in to_scan))
        hits.sort(key=lambda h: (h["path"], h["line"]))
        hits = hits[:want]
        clipped = len(hits) >= want and stop.is_set()
        if clipped:
            # Review note: once the hit cap stops the walk, the
            # files in this call's budget that were never fetched are unscanned
            # too — count them, or files_unscanned reads 0 while half the
            # budget never ran.
            unscanned += max(0, len(to_scan) - fetched)
        notes = []
        if clipped:
            notes.append(f"hits capped at {want} — narrow the pattern, or pass 'path'/'glob'")
        if unscanned:
            notes.append(f"{unscanned if not tree_clipped else 'an unknown number of'} matching files were NOT scanned "
                         f"(budget is {_GITEA_GREP_FILES} files per call) — pass 'path' or 'glob' to narrow the walk")
        return {"status": "ok", "result": {
            "repo": repo, "ref": use_ref, "pattern": pattern, "regex": bool(a.get("regex")),
            "prefix": prefix or None, "glob": glob or None,
            "hits": hits, "count": len(hits), "clipped": clipped,
            "files_scanned": scanned, "files_skipped": skipped, "files_unscanned": unscanned,
            **({"note": "; ".join(notes)} if notes else {})}}
    except Exception as e:
        return _refused_if_caller(e, f"gitea failed: {getattr(e, 'detail', None) or e}")


# ── create_project implementation ─────────────────────
#
# create_project used to "carry forward"
# the active conversational arc — every untagged turn of the entity from the
# last four hours back to the newest five-minute silence — by re-tagging those
# rows into the new project. Two faults, both in the selection: it read EVERY
# conversation's untagged rows (any Circles thread, the original conversation,
# other apps' chats), so it could pull another conversation's messages into
# the project; and it ran inside the turn that called it, after the person's
# message was written and before the Host's reply was, so it took the request
# and left the reply. A request typed in a Circles thread moved to the new
# project and the reply stayed in the thread: the thread opened with an answer to nothing and
# the project with a question nobody answered, and no marker said why.
#
# Nothing moves now. The exchange stays whole where it was typed, and the new
# project's thread opens with a marker turn (the thread_marker shape adopt,
# split and rehome already write) naming the conversation it was started from
# and linking to it. Moving a conversation into a project is still there, as
# its own act on request: project_adopt_thread (adopt_thread_core), which
# moves a whole conversation and leaves a marker behind.

_STARTED_FROM_KIND = "started_from"


async def _conversation_label(pool, entity_id: int, src: dict) -> tuple[str, str | None]:
    """(title, link) for the conversation a turn row descriptor names: a
    Circles thread by its title (else its topic), the original conversation by
    its fixed title, anything else by what it is. The link opens it on this
    Host (server/deep_link via reply_parts.thread_link_path, made absolute on
    the Host's own origin by reachout.doorbell_link — a project thread is read
    inside the Workbench's own window, where a host-relative link would open
    that window's origin instead); None for a conversation no app opens by
    link."""
    from mastercode import circles_threads as _ct
    kind, ref = str(src.get("kind") or ""), str(src.get("ref") or "")
    title = None
    if kind == "thread":
        try:
            row = await pool.fetchrow(
                "SELECT title, topic FROM circles_threads"
                " WHERE id = $1::uuid AND entity_id = $2",
                ref, entity_id)
        except Exception as e:  # noqa: BLE001 - the marker stands without a title
            log.info("create_project: thread title read failed (%s): %s", ref, e)
            row = None
        if row:
            title = (str(row["title"] or "").strip() or str(row["topic"] or "").strip()) or None
        title = title or "a Circles thread"
    elif kind == "app" and ref == "circles":
        title = _ct.LEGACY_TITLE
    elif kind == "app":
        title = f"the {ref} conversation"
    elif kind == "peer":
        title = f"the conversation with {ref}"
    else:
        title = "a conversation"
    link = None
    try:
        from server.connectors.reply_parts import thread_link_path
        path = thread_link_path(kind, ref)
        if path:
            from server.reachout.service import doorbell_link, resolve_host_origin
            try:
                origin = await asyncio.wait_for(resolve_host_origin(entity_id), timeout=3.0)
            except Exception:  # noqa: BLE001 - a relative link still opens it on the desktop
                origin = ""
            link = doorbell_link(path, origin) or path
    except Exception as e:  # noqa: BLE001 - the marker stands without a link
        log.info("create_project: no link for %s:%s (%s)", kind, ref, e)
    return title, link


def started_from_text(title: str, link: str | None, *, circles: bool) -> str:
    """The marker's words: where the project was started, and that nothing
    moved."""
    where = f"“{title}”" + (" in Circles" if circles else "")
    return (f"← Started from {where} — that conversation stays where it was "
            f"typed; nothing was moved."
            + (f" [Open it]({link})" if link else ""))


async def _write_started_from(pool, entity_id: int, project_id: int,
                              src: dict) -> dict | None:
    """One marker turn in the new project's thread pointing back to the
    conversation `src` (a turn-row descriptor) it was started from. The shape
    is the thread_marker every move writes (adopt_thread_core, split_thread,
    turn_home.rehome_turn): an assistant turn with source_kind
    'thread_marker', importance 0.1 and no embedding, so no adopt, split or
    pick ever carries it. Returns {kind, ref, title, link}, or None when it was not
    written (the project stands either way)."""
    kind, ref = str(src.get("kind") or ""), str(src.get("ref") or "")
    if not kind or not ref:
        return None
    title, link = await _conversation_label(pool, entity_id, src)
    md = {
        "role": "assistant",
        "source_kind": "thread_marker",
        "source_context": {"kind": "project", "ref": str(project_id)},
        "app_id": "workbench",
        # No `thread_id` key: that one names a thread something MOVED INTO
        # (split_thread), and delete_thread rewrites those markers' words.
        "marker": {_STARTED_FROM_KIND: {"kind": kind, "ref": ref},
                   "title": title, "link": link, "moved_turns": 0},
    }
    try:
        await pool.execute(
            """
            INSERT INTO entity_memory
              (entity_id, content, memory_type, importance, status,
               source_session, project_id, metadata)
            VALUES ($1, $2, 'turn', 0.1, 'active', $3, $4, $5::jsonb)
            """,
            entity_id,
            started_from_text(title, link, circles=(kind == "thread"
                                                    or (kind == "app" and ref == "circles"))),
            f"create:{project_id}", project_id, json.dumps(md))
    except Exception as e:  # noqa: BLE001 - the project stands without its marker
        log.warning("create_project: the started-from marker was not written "
                    "(project %s): %s", project_id, e)
        return None
    return {"kind": kind, "ref": ref, "title": title, "link": link}


async def _exec_archive_project(args: dict, entity_id: int, pool) -> dict:
    """Soft-delete a project (status -> archived). It disappears from
    list_projects and the Workbench's tabs; nothing is destroyed. Use when the
    user says a project is done with / was a test / should be deleted."""
    try:
        project_id = int((args or {}).get("project_id"))
    except (TypeError, ValueError):
        return _refused("project_id (int) required")
    row = await pool.fetchrow(
        """UPDATE projects SET status='archived', updated_at=now()
            WHERE id=$1 AND entity_id=$2 RETURNING id, name""",
        project_id, entity_id)
    if not row:
        return _refused(f"project {project_id} not found")
    return {"status": "ok", "result": {"id": row["id"], "name": row["name"],
                                       "project_status": "archived"}}


async def _exec_project_adopt_thread(args: dict, entity_id: int, pool) -> dict:
    """Move a chat thread (or one episode of it) into a project — retag,
    never delete. The user's 'that general conversation belongs in this
    project' op: the turns get project_id + source_context {project, N} and
    what was learned from them is said in the project too
    (adopt_thread_core); a marker turn stays behind in the source thread.
    Use chat_threads (op='list'/'episodes') to identify the source."""
    a = args or {}
    try:
        project_id = int(a.get("project_id"))
    except (TypeError, ValueError):
        return _refused("project_id (int) required")
    kind = (a.get("kind") or "app").strip()
    ref = str(a.get("ref") or "").strip()
    if not ref:
        return _refused("ref required (e.g. 'workbench' for the general thread)")
    try:
        from server.routes.projects import adopt_thread_core
        result = await adopt_thread_core(
            pool, entity_id, project_id, kind, ref,
            since=a.get("since"), until=a.get("until"))
        return {"status": "ok", "result": result}
    except ValueError as e:
        # The core raises for the TARGET ("project not found") and, on a
        # project source, for the SOURCE ("source project not found", "same
        # project", "ref must be a project id") — say which.
        msg = str(e) or "project not found"
        if msg == "project not found":
            msg = f"project {project_id} not found"
        return _refused(msg)
    except Exception as e:
        return _refused_if_caller(e, f"project_adopt_thread failed: {getattr(e, 'detail', None) or e}")


_UNSET = object()


async def _phase_deps_arg(pool, project_id: int, phase_id: int | None, a: dict):
    """`depends_on` of add_phase / set_phase_spec: (value, refusal). The value
    is _UNSET when the call did not pass it, None for null (back to the
    default — the phase before it in its module), else the phase ids. Ids must
    be phases of this project, never the phase itself, and the result may not
    close a circle (then nothing in it could ever start)."""
    if "depends_on" not in a:
        return _UNSET, None
    raw = a.get("depends_on")
    if raw is None:
        deps = None
    else:
        if not isinstance(raw, list):
            return None, "depends_on must be a list of phase ids (from op='list')"
        try:
            deps = sorted({int(d) for d in raw})
        except (TypeError, ValueError):
            return None, "depends_on must be a list of phase ids (from op='list')"
    rows = await pool.fetch(
        f"SELECT {_PHASE_COLS} FROM project_phases WHERE project_id=$1 "
        "ORDER BY phase_number", project_id)
    ids = {r["id"] for r in rows}
    for d in deps or []:
        if d == phase_id:
            return None, "a phase cannot depend on itself"
        if d not in ids:
            return None, f"depends_on {d} is not a phase of project {project_id}"
    if phase_id is not None:
        view = [({**dict(r), "depends_on": deps} if r["id"] == phase_id else dict(r))
                for r in rows]
        cyc = _find_cycle({r["id"]: _phase_deps(r, view) for r in view})
        if cyc:
            by = {r["id"]: r for r in view}
            return None, ("that would make phases wait for each other in a circle: "
                          + " → ".join(f"phase {by[i]['phase_number']}" for i in cyc))
    return deps, None


def _phase_engine_arg(a: dict):
    """`harness` / `model` of add_phase / set_phase_spec: ({harness, model} or
    None when neither was passed, refusal). An unknown engine is refused by
    name; whether it is CONNECTED is checked when the phase builds."""
    if "harness" not in a and "model" not in a:
        return None, None
    h = str(a.get("harness") or "").strip() or None
    m = str(a.get("model") or "").strip() or None
    try:
        from server.routes.review import HARNESS_APP_IDS as _known
    except Exception:  # noqa: BLE001
        _known = ()
    if h and _known and h not in _known:
        return None, f"harness {h!r} is not a coding engine — one of " + ", ".join(_known)
    return {"harness": h, "model": m}, None


async def _exec_project_phase(args: dict, entity_id: int, pool) -> dict:
    """Drive the project WORKFLOW (phases -> steps) from chat — the ported
    build-plan machinery: phases auto-number; steps auto-classify a domain
    and auto-fill their execute/spec models from the project's per-domain
    routing + codebook (the 'these models are good at this' table). Ops:
      list            -> phases + steps for project_id
      add_phase       -> project_id, title, description?
      add_step        -> project_id, phase_id, title, description?
      set_step_status -> project_id, step_id, step_status
                         (pending|speccing|implementing|checking|done|failed —
                          'in_progress' is accepted as a legacy alias for
                          'implementing'; the enum is the phase_steps CHECK)
    Entity-scoped: a project the host doesn't own returns not-found."""
    a = args or {}
    op = (a.get("op") or "list").strip()
    try:
        project_id = int(a.get("project_id"))
    except (TypeError, ValueError):
        return _refused("project_id (int) required")
    owned = await pool.fetchval(
        "SELECT 1 FROM projects WHERE id=$1 AND entity_id=$2",
        project_id, entity_id)
    if not owned:
        return _refused(f"project {project_id} not found")

    if op == "list":
        phases = await pool.fetch(
            """SELECT ph.id, ph.phase_number, ph.title, ph.description, ph.status,
                      ph.gate, ph.approved_at,
                      COALESCE(jsonb_array_length(ph.acceptance), 0) AS acceptance_checks,
                      ph.module_id, ph.depends_on, ph.harness, ph.model,
                      ph.integration_error
                 FROM project_phases ph WHERE ph.project_id=$1
                ORDER BY ph.phase_number""", project_id)
        steps = await pool.fetch(
            """SELECT s.id, s.phase_id, s.step_number, s.title, s.status,
                      s.domain_tag, s.execute_model
                 FROM phase_steps s
                 JOIN project_phases ph ON ph.id = s.phase_id
                WHERE ph.project_id=$1 ORDER BY ph.phase_number, s.step_number""",
            project_id)
        return {"status": "ok", "result": {
            "phases": [dict(r) for r in phases],
            "steps": [dict(r) for r in steps]}}

    if op == "add_phase":
        title = (a.get("title") or "").strip()
        if not title:
            return _refused("title required")
        # A phase's SPEC is its brief — the text the builder is actually given
        # (_compose_phase_intent). This op could not write one at all, so every
        # host-restructured phase landed spec-NULL and status 'planning', and a
        # plan rewritten in chat was structurally unbuildable: the chain stops
        # at a non-ready phase, and the chat door (before this change) would
        # have built it against a title-only brief. Measured on one project,
        # Phases 6-11 came out of a restructure with spec NULL and
        # description ''. Writing the brief is now possible, and supplying one
        # is what makes the phase ready.
        spec = (a.get("spec") or "").strip()
        description = (a.get("description") or "").strip()
        # Proof + gate (v124), validated before the write like _persist_plan.
        try:
            _acc = _normalize_acceptance(a.get("acceptance"))
            _gate = _normalize_gate(a.get("gate"))
        except ValueError as e:
            return _refused(f"phase refused — {e}")
        # Tool gate: a check no harness image can run never
        # lands. The engine is not known here; a phase build does not check
        # again, because its checks are notes.
        _tool_ref = _acceptance_tool_refusal(_acc, None)
        if _tool_ref:
            return _refused(f"phase refused — {_tool_ref}")
        # The module it belongs to.
        _mod = None
        _mname = str(a.get("module") or "").strip().lower()
        if _mname:
            from server.routes.project_modules import get_module
            _mod = await get_module(pool, project_id, _mname)
            if _mod is None:
                return _refused(f"phase refused — project {project_id} has no module {_mname!r} "
                    "(modules are declared by build(action='plan', modules=[…]))")
        _val = None
        if spec:
            _val, _why = _phase_validation_for(
                _acc, a.get("validation"), _mod["validation"] if _mod else None)
            if _why:
                return _refused(f"phase refused — {_why}")
        # What it waits for and what it builds on.
        _deps, _why = await _phase_deps_arg(pool, project_id, None, a)
        if _why:
            return _refused(f"phase refused — {_why}")
        _eng, _why = _phase_engine_arg(a)
        if _why:
            return _refused(f"phase refused — {_why}")
        nxt = await pool.fetchval(
            "SELECT COALESCE(MAX(phase_number),0)+1 FROM project_phases WHERE project_id=$1",
            project_id)
        row = await pool.fetchrow(
            """INSERT INTO project_phases
                   (project_id, phase_number, title, description, spec, status,
                    acceptance, gate, module_id, validation, depends_on, harness, model)
               VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8,$9,$10,$11::int[],$12,$13)
               RETURNING id, phase_number, status, gate, depends_on""",
            project_id, nxt, title, description or None, spec or None,
            # Matches _persist_plan: a phase WITH a brief is ready to build; one
            # without stays 'planning' honestly, rather than looking buildable.
            _PHASE_READY if spec else "planning",
            json.dumps(_acc) if _acc is not None else None, _gate,
            _mod["id"] if _mod else None, _val,
            None if _deps is _UNSET else _deps,
            (_eng or {}).get("harness"), (_eng or {}).get("model"))
        out = dict(row)
        out["acceptance_checks"] = len(_acc or [])
        if not spec:
            out["next_step"] = (
                f"Phase {row['phase_number']} was created WITHOUT a spec, so it "
                "is 'planning' and nothing will build it — not the chat build "
                "door, not auto-advance. Write its brief now with "
                f"project_phase(op='set_phase_spec', phase_id={row['id']}, "
                "spec='…'), and pass `spec` on add_phase next time. Do not tell "
                "the user this phase will run until it has one.")
        return {"status": "ok", "result": out}

    if op == "set_phase_spec":
        # The repair path. Without it a spec-less phase could only be fixed by
        # deleting and re-adding it, which renumbers everything after it — so
        # a plan restructured into an unbuildable shape (one project's phases
        # 6-11) had no way back. Supplying a brief is what promotes a phase out
        # of 'planning'; clearing one is not offered, because demoting a live
        # phase is not a thing the host should be able to do by accident.
        try:
            phase_id = int(a.get("phase_id"))
        except (TypeError, ValueError):
            return _refused("phase_id (int) required")
        spec = (a.get("spec") or "").strip()
        if not spec:
            return _refused("spec required (non-empty)")
        # Proof + gate ride the repair path too (v124), so a phase whose brief
        # is being rewritten can gain its checks in the same call. Omitted =
        # unchanged; `acceptance: []` clears. approved_at is NEVER written
        # here — only the Workbench route approves.
        _acc_given = "acceptance" in a
        _gate_given = "gate" in a
        try:
            _acc = _normalize_acceptance(a.get("acceptance")) if _acc_given else None
            _gate = _normalize_gate(a.get("gate")) if _gate_given else None
        except ValueError as e:
            return _refused(f"phase refused — {e}")
        _tool_ref = _acceptance_tool_refusal(_acc, None)   # same gate as add_phase
        if _tool_ref:
            return _refused(f"phase refused — {_tool_ref}")
        # The phase's recorded `validation` (decides nothing): from the
        # acceptance it will carry after this call, its own, or its module's.
        _cur = await pool.fetchrow(
            "SELECT p.acceptance, p.validation, m.validation AS module_validation "
            "FROM project_phases p LEFT JOIN project_modules m ON m.id = p.module_id "
            "WHERE p.id=$1 AND p.project_id=$2", phase_id, project_id)
        if _cur is None:
            return _refused(f"phase {phase_id} not in project {project_id}")
        _acc_after = _acc if _acc_given else _cur["acceptance"]
        if isinstance(_acc_after, str):
            _acc_after = json.loads(_acc_after)
        _val, _why = _phase_validation_for(
            _acc_after, a.get("validation") or _cur["validation"], _cur["module_validation"])
        if _why:
            return _refused(f"phase refused — {_why}")
        # The RE-SPLIT lever: `depends_on` rewrites
        # what the phase waits for (null = the phase before it in its module).
        # Rewriting the brief or the split clears a recorded collision — the
        # phase is a different piece of work now, and the scheduler may start
        # it again. Omitted = unchanged.
        _deps, _why = await _phase_deps_arg(pool, project_id, phase_id, a)
        if _why:
            return _refused(f"phase refused — {_why}")
        _eng, _why = _phase_engine_arg(a)
        if _why:
            return _refused(f"phase refused — {_why}")
        row = await pool.fetchrow(
            """UPDATE project_phases SET spec=$1,
                   status = CASE WHEN status = 'planning' THEN $2 ELSE status END,
                   acceptance = CASE WHEN $5::boolean THEN $6::jsonb ELSE acceptance END,
                   gate = CASE WHEN $7::boolean THEN $8 ELSE gate END,
                   validation = $9,
                   depends_on = CASE WHEN $10::boolean THEN $11::int[] ELSE depends_on END,
                   harness = CASE WHEN $12::boolean THEN $13 ELSE harness END,
                   model = CASE WHEN $15::boolean THEN $14 ELSE model END,
                   integration_error = NULL,
                   updated_at = now()
                 WHERE id=$3 AND project_id=$4
             RETURNING id, phase_number, title, status, gate, validation,
                       depends_on, harness, model""",
            spec, _PHASE_READY, phase_id, project_id,
            _acc_given, json.dumps(_acc) if _acc is not None else None,
            _gate_given, _gate, _val,
            _deps is not _UNSET, None if _deps is _UNSET else _deps,
            "harness" in a, (_eng or {}).get("harness"), (_eng or {}).get("model"),
            "model" in a)
        if row is None:
            return _refused(f"phase {phase_id} not in project {project_id}")
        return {"status": "ok", "result": dict(row)}

    if op == "archive_phase":
        # RETIRE a phase the plan has moved past. Until v110 nothing in the
        # tree could remove one: no op here, no REST route, no control in the
        # Workbench. A project that pivoted kept its dead phases forever, and
        # the only lever that cleared them — the supersede inside
        # build(action='plan') — deletes every unfinished phase, including the
        # replacement plan the host had just written. Otherwise, when a
        # project pivots, old phases stay and make a mess.
        #
        # Archiving is not deletion: the row and its steps stay as history and
        # simply stop counting as open work (_PHASE_OPEN_SQL). A COMPLETE phase
        # is refused — what a finished phase produced is the project's record,
        # and retiring it would misreport what was built.
        try:
            phase_id = int(a.get("phase_id"))
        except (TypeError, ValueError):
            return _refused("phase_id (int) required")
        # A phase whose build is RUNNING is refused rather than raced: the
        # harness is still writing, and archiving mid-flight leaves the user
        # unsure whether the work counted. _set_phase_status also refuses to
        # clobber an archive, so the race is survivable either way — this just
        # makes it legible instead.
        _running = await pool.fetchval(
            "SELECT 1 FROM project_phases WHERE id=$1 AND project_id=$2 "
            "AND status=$3", phase_id, project_id, _PHASE_RUNNING)
        if _running:
            return _refused(f"phase {phase_id} is building right now — archiving it "
                "mid-run would leave it unclear whether the work counted. It "
                "reports into the thread when it lands; archive then.")
        row = await pool.fetchrow(
            """UPDATE project_phases SET status=$1
                 WHERE id=$2 AND project_id=$3 AND status <> $4
             RETURNING id, phase_number, title, status""",
            _PHASE_ARCHIVED, phase_id, project_id, _PHASE_DONE)
        if row is None:
            # Say WHICH of the two it was rather than a shrug — the caller
            # cannot tell "wrong project" from "already built" otherwise.
            _cur = await pool.fetchval(
                "SELECT status FROM project_phases WHERE id=$1 AND project_id=$2",
                phase_id, project_id)
            if _cur == _PHASE_DONE:
                return _refused(f"phase {phase_id} is complete — a finished phase is the "
                    "project's record of what was built, so it is not "
                    "archivable. Retire the phases that were never built.")
            return _refused(f"phase {phase_id} not in project {project_id}")
        out = dict(row)
        out["next_step"] = (
            f"Phase {row['phase_number']} is retired: it keeps its steps as "
            "history but no longer counts as open work, so the build door and "
            "auto-advance skip it and the project can reach 'complete' without "
            "it. Re-planning does NOT delete it.")
        return {"status": "ok", "result": out}

    if op == "add_step":
        try:
            phase_id = int(a.get("phase_id"))
        except (TypeError, ValueError):
            return _refused("phase_id (int) required")
        title = (a.get("title") or "").strip()
        if not title:
            return _refused("title required")
        ok_phase = await pool.fetchval(
            "SELECT 1 FROM project_phases WHERE id=$1 AND project_id=$2",
            phase_id, project_id)
        if not ok_phase:
            return _refused(f"phase {phase_id} not in project {project_id}")
        description = (a.get("description") or "").strip()
        # The ported step intelligence (from the retired factory app): classify the domain, then
        # auto-fill the execute/spec models from project routing + codebook.
        domain_tag = (a.get("domain_tag") or "").strip() or None
        if not domain_tag:
            try:
                from mastercode.domain_classifier import classify_domain
                domain_tag = (classify_domain(f"{title} {description}") or {}).get("domain")
            except Exception:
                domain_tag = None
        execute_model = None
        if domain_tag:
            try:
                from server.routes.projects import _resolve_model_for_domain
                execute_model = await _resolve_model_for_domain(pool, project_id, domain_tag)
            except Exception:
                execute_model = None
        nxt = await pool.fetchval(
            "SELECT COALESCE(MAX(step_number),0)+1 FROM phase_steps WHERE phase_id=$1",
            phase_id)
        row = await pool.fetchrow(
            """INSERT INTO phase_steps
                   (phase_id, step_number, title, description, domain_tag, execute_model)
               VALUES ($1,$2,$3,$4,$5,$6)
               RETURNING id, step_number, status, domain_tag, execute_model""",
            phase_id, nxt, title, description, domain_tag, execute_model)
        return {"status": "ok", "result": dict(row)}

    if op == "set_step_status":
        try:
            step_id = int(a.get("step_id"))
        except (TypeError, ValueError):
            return _refused("step_id (int) required")
        new_status = (a.get("step_status") or "").strip()
        # The REAL enum is the phase_steps CHECK constraint, not the shorter set
        # this validator used to advertise. It accepted 'in_progress', which the
        # constraint rejects — so the tool passed its own check and then died on
        # the DB write, surfacing to the user as a failed tool call mid-build
        # (the warning at the top of this module flagged exactly this, but only
        # the internal _STEP_* constants were corrected). 'in_progress' is kept
        # as a legacy ALIAS rather than an error: the host has been calling it
        # for months and translating is strictly better than failing the turn.
        if new_status == "in_progress":
            new_status = _STEP_RUNNING  # 'implementing'
        _VALID_STEP = ("pending", "speccing", "implementing",
                       "checking", "done", "failed")
        if new_status not in _VALID_STEP:
            return _refused("step_status must be " + "|".join(_VALID_STEP))
        updated = await pool.fetchval(
            """UPDATE phase_steps s SET status=$1
                 FROM project_phases ph
                WHERE s.id=$2 AND ph.id=s.phase_id AND ph.project_id=$3
                RETURNING s.id""", new_status, step_id, project_id)
        if not updated:
            return _refused(f"step {step_id} not in project {project_id}")
        return {"status": "ok", "result": {"step_id": step_id, "step_status": new_status}}

    return _refused(f"unknown op {op!r}")


# The resolver's transitional aliases of provider/model (inference_config
# `_route`): new readers use provider/model only.
_ROUTE_ALIASES = ("model_id", "lane")


def _bench_routing_view(r: dict) -> dict:
    """coding_bench's `routing`: what each role runs and what the engines take,
    read from inference.get_current_routing — the picker's own payload — minus
    what only the picker draws.

    Measured: coding_bench ran ~30,000 characters a call
    over 14 calls. What grows in the picker's payload is
    `harness_options[].models` (an engine on the whole OpenRouter catalog lists
    every slug); its provider, favourite and effort tables are the screen's.
    Kept: every role's effective route (provider, model, effort,
    fallback, source, the unconfigured reason) without the resolver's aliases;
    the saved engine entries and the default tick; each engine's label, efforts
    and model count; the providers a key is held for. One engine's models:
    harness_connections(op='matrix', harness=<id>); routing with the keys and
    the recommendations: model_routing op='status' / 'recommend'."""
    if not isinstance(r, dict) or "error" in r:
        return r
    effective = {}
    for role, e in (r.get("effective") or {}).items():
        if isinstance(e, dict):
            effective[role] = {k: v for k, v in e.items()
                               if k not in _ROUTE_ALIASES and v not in (None, "", [], {}, False)}
    held = [p for p in (r.get("providers") or []) if isinstance(p, dict) and p.get("has_key")]
    out = {
        "effective": effective,
        "coding_harnesses": r.get("coding_harnesses") or [],
        "default_coding_harness": r.get("default_coding_harness"),
        "harness_options": [
            {"id": h.get("id"), "label": h.get("label"), "efforts": h.get("efforts") or [],
             "model_count": len(h.get("models") or [])}
            for h in (r.get("harness_options") or []) if isinstance(h, dict)],
        "providers_held": [p.get("id") for p in held],
        "models": "one engine's models: harness_connections(op='matrix', harness='<id>')",
    }
    via = {p.get("id"): p.get("via_label") for p in held if p.get("via_label")}
    if via:
        out["provider_via"] = via
    return out


async def _exec_coding_bench(args: dict, entity_id: int, pool) -> dict:
    """The host's coding-capability awareness in ONE read: which harnesses it
    has under subscription, which models, what each is good at — and if one
    fails, it picks another and logs it. Returns:
      harnesses    — the Coding Engines: every connected engine with its saved
                     default model/effort, which one is the DEFAULT (an unnamed
                     build runs there), plus the connected list
      protocols    — the Multi-Agent Coding files (name, author, shape, stats)
                     — the instructions the host follows for anything beyond
                     one engine (read one with coding_protocol op='get')
      routing      — the per-role provider/model routes (the routing truth)
      track_record — per (harness, model): runs, ok/failed, last error,
                     avg seconds — from harness_run, which logs EVERY run and
                     failure automatically; this is the pick-another-model
                     evidence
      preferences  — the learned per-project taste summary (accepted picks)
    Read-only."""
    a = args or {}
    out: dict = {"status": "ok"}
    try:
        from server.routes.review import (_installed_harnesses, _coding_harness_settings,
                                          _default_coding_harness)
        installed = await _installed_harnesses()
        cfg = await _coding_harness_settings()
        default = await _default_coding_harness()
        engines = []
        for h in installed:
            o = (cfg.get(h) or {}) if isinstance(cfg, dict) else {}
            engines.append({"id": h, "default": h == default,
                            "model": o.get("model"), "effort": o.get("effort")})
        out["harnesses"] = {
            "engines": engines,
            "default": default if default in installed else None,
            "default_not_connected": default if (default and default not in installed) else None,
            "installed": installed,
        }
    except Exception as e:
        out["harnesses"] = {"error": str(e)[:200]}
    try:
        from server.routes.coding_protocols import list_protocols
        out["protocols"] = (await list_protocols()).get("protocols", [])
    except Exception as e:
        out["protocols"] = {"error": str(e)[:200]}
    try:
        from server.routes.inference import get_current_routing
        out["routing"] = _bench_routing_view(await get_current_routing())
    except Exception as e:
        out["routing"] = {"error": str(e)[:200]}
    try:
        # Failures split by KIND (runtime/run_failure.py): a stream that died,
        # a missing credential or a full plan is not the model's failure and
        # must not count against it — a run (claude-code / claude-fable-5,
        # "live stream died after 2 frames") was once scored as a model failure.
        # `model_failed` is the number to pick
        # by; `failed_by_kind` says what the rest were.
        from mastercode.runtime.run_failure import summarize_runs as _summarize_runs
        _TRACK_WINDOW = 400   # most-recent runs the record is drawn from; rides each row as `window`
        rows = await pool.fetch(
            """SELECT id, harness, model, ok, error, exit_code, elapsed_seconds, created_at
                 FROM harness_run
                ORDER BY id DESC LIMIT $1""", _TRACK_WINDOW)
        out["track_record"] = _summarize_runs([dict(r) for r in rows], window=_TRACK_WINDOW)[:30]
    except Exception as e:
        out["track_record"] = {"error": str(e)[:200]}
    try:
        pid = a.get("project_id")
        if pid is not None:
            from server.routes._preference_profile import summarize_for_prompt
            out["preferences"] = summarize_for_prompt(int(pid)) or "(none yet)"
    except Exception as e:
        out["preferences"] = f"(unavailable: {str(e)[:120]})"
    # Standing bans ride every bench read — the host must never propose an arm
    # the dispatcher will refuse. Global by construction, so no project_id needed.
    try:
        from server.routes._preference_profile import load_user_profile
        out["banned"] = sorted((load_user_profile().get("bans") or {}).keys())
    except Exception as e:
        out["banned"] = {"error": str(e)[:200]}
    return out


PIPELINE_PROPOSAL_KIND = "pipeline_proposal"   # = server/routes/tickets.PIPELINE_PROPOSAL_KIND


async def _pipeline_open_proposal(pool, entity_id: int, project_id: int) -> int | None:
    return await pool.fetchval(
        "SELECT id FROM entity_tickets WHERE entity_id=$1 AND status='open' "
        "AND metadata->>'kind' = $2 "
        "AND (metadata->'pipeline_proposal'->>'project_id')::int = $3 "
        "ORDER BY id DESC LIMIT 1", entity_id, PIPELINE_PROPOSAL_KIND, project_id)


async def _pipeline_last_answered(pool, entity_id: int, project_id: int) -> dict | None:
    row = await pool.fetchrow(
        "SELECT id, status, decision->>'option_id' AS option_id, "
        "metadata->'pipeline_apply' AS apply FROM entity_tickets "
        "WHERE entity_id=$1 AND status <> 'open' AND metadata->>'kind' = $2 "
        "AND (metadata->'pipeline_proposal'->>'project_id')::int = $3 "
        "ORDER BY id DESC LIMIT 1", entity_id, PIPELINE_PROPOSAL_KIND, project_id)
    if row is None:
        return None
    apply = row["apply"]
    if isinstance(apply, str):
        apply = json.loads(apply)
    return {"ticket_id": row["id"], "status": row["status"],
            "option_id": row["option_id"], "apply": apply}


async def _pipeline_stepup_enrolled(pool) -> bool:
    try:
        from server.network import nac_stepup
        return bool(await nac_stepup.is_enrolled(pool))
    except Exception:  # noqa: BLE001 — unknown reads as not enrolled
        return False


async def _pipeline_code_servers(pool, entity_id: int) -> list:
    """The code servers on the user's machines this Host may reach now — a
    reach grant approved, unrevoked and unexpired — each with whether this
    Host's token for it is in Keys. Never the token."""
    try:
        rows = await pool.fetch(
            "SELECT DISTINCT server, slot FROM service_reach_grants WHERE entity_id=$1 "
            "AND status='granted' AND revoked_at IS NULL "
            "AND (expires_at IS NULL OR expires_at > now()) ORDER BY server, slot", entity_id)
    except Exception:  # noqa: BLE001 — the bench reports, never raises
        return []
    from server.network.nac_harness import service_push_key_name
    from server.vault import api as vault_api
    try:
        # Names only (no secret is opened to answer "is it there"); a
        # switched-off credential is not usable, so it does not count.
        names = {m["logical_name"] for m in await vault_api.list_secrets(pool, entity_id=entity_id)
                 if not m.get("deactivated_at")}
    except Exception:  # noqa: BLE001
        names = set()
    out = []
    for r in rows:
        key = service_push_key_name(r["server"], r["slot"])
        out.append({"machine": r["server"], "service": r["slot"], "token_key": key,
                    "token_in_keys": key in names})
    return out


async def _exec_pipeline(args: dict, entity_id: int, pool) -> dict:
    """The project's build pipeline, from the host's side. Two ops:

      status   READ. What the pipeline is (stored / effective / an error),
               the providers the platform has and what each needs, what the
               environment offers (the forge), any proposal still waiting on
               the user, and what the last build's checks said.
      propose  WRITE a DRAFT, inert: validated by the one validator, turned
               into plain change lines by the platform (never the host's
               words), and raised as a decision ticket locked to the user
               (user_only, no_pattern) that the chat shows as an ask card. The
               setting changes only when the user picks 'apply' — applied by
               tickets.decide_ticket_service, compare-and-set against the
               setting this draft was made from.

    The host never sets the pipeline itself; there is no op for it."""
    from mastercode.runtime import pipeline as _pl
    op = str(args.get("op") or "").strip().lower()
    if op not in ("status", "propose"):
        return _refused("op must be 'status' or 'propose'")
    try:
        project_id = int(args.get("project_id"))
    except (TypeError, ValueError):
        return _refused("project_id (integer) is required")
    row = await pool.fetchrow(
        "SELECT id, name, repo, config FROM projects WHERE id=$1 AND entity_id=$2",
        project_id, entity_id)
    if row is None:
        return _refused(f"project {project_id} is not one of yours")
    cfg = row["config"]
    if isinstance(cfg, str):
        cfg = json.loads(cfg)
    stored = cfg.get("pipeline") if isinstance(cfg, dict) else None
    approved = await _pl.load_approved(pool, entity_id, project_id, cfg)
    effective = _pl.resolve(cfg, approved=approved)

    if op == "status":
        try:
            from server.routes.gitea_client import gitea_creds
            _cred = gitea_creds()
        except Exception:  # noqa: BLE001 — the bench reports, never raises
            _cred = None
        last = await pool.fetchrow(
            "SELECT task_id, status, result->>'phase_status' AS phase_status, "
            "result->'checks' AS checks FROM entity_tasks "
            "WHERE entity_id=$1 AND task_type='factory_build' "
            "AND (result->>'project_id')::int = $2 AND result ? 'checks' "
            "ORDER BY task_id DESC LIMIT 1", entity_id, project_id)
        last_checks = last["checks"] if last else None
        if isinstance(last_checks, str):
            last_checks = json.loads(last_checks)
        return {"status": "ok", "result": {
            "project_id": project_id,
            # 'default' = no setting: today's pipeline. The stored document is
            # not echoed when it no longer validates (it may carry what the
            # validator refused); `error` says what is wrong.
            "pipeline": {"configured": stored is not None,
                         "setting": stored if "error" not in effective else None,
                         "effective": effective},
            "providers": [{"key": p.key, "stage": p.stage, "where": p.where,
                           "proves": p.proves, "settings": sorted(p.config_schema),
                           "widens": p.where in _pl.WIDEN_WHERE}
                          for p in _pl.PROVIDERS.values()],
            "environment": {
                # The platform forge: whether this Host is connected to it and
                # whether the project has its repo. Never the credential.
                "forge": {"provider": "platform-gitea", "connected": bool(_cred),
                          "host": _cred[2] if _cred else None, "repo": row["repo"]},
                # The code servers this Host built on the user's machines that it
                # may reach now (their reach grant is approved) and whether its
                # token for each is in Keys — what a machine-forge source needs.
                "code_servers": await _pipeline_code_servers(pool, entity_id),
                # Adding a check that runs outside the platform needs a code
                # from the user's authenticator; without one enrolled it
                # cannot be approved at all.
                "step_up_enrolled": await _pipeline_stepup_enrolled(pool),
            },
            # Outside-the-platform checks in the setting (any module) that the
            # user's step-up does not cover: each counts as "could not run".
            "unapproved": (_pl.widens(_pl.validate(stored)[0] or {}, approved)
                           if stored is not None else []),
            "pending_proposal": await _pipeline_open_proposal(pool, entity_id, project_id),
            # The last proposal the user ANSWERED and what came of it — 'apply'
            # can be refused after the click (the setting changed since the
            # draft), and the click alone says only "I chose Apply".
            "last_answered": await _pipeline_last_answered(pool, entity_id, project_id),
            "last_build": ({"task_id": last["task_id"], "status": last["status"],
                            "phase_status": last["phase_status"], "checks": last_checks}
                           if last else None),
        }}

    # ── propose ──
    raw = args.get("pipeline")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as e:
            return _refused(f"pipeline is not valid JSON: {e}")
    # ONE line: newlines would let the reason pass for the platform's own
    # change lines under it on the card.
    reason = " ".join(str(args.get("reason") or "").split())
    if not reason:
        return _refused("reason is required — one line the user reads: "
                                             "what in their environment makes this change right")
    proposed, errors = _pl.validate(raw)
    if proposed is None:
        return _refused("the pipeline does not validate: " + "; ".join(errors))
    current = effective if "error" not in effective else _pl.default_pipeline()
    lines = _pl.changes(current, proposed)
    # Outside-the-platform rows the user's step-up does not cover yet — new,
    # changed, or already stored but never approved.
    wide = _pl.widens(proposed, approved)
    if not lines and not wide and "error" not in effective:
        return {"status": "ok", "result": {
            "proposed": False, "unchanged": True,
            "note": "that is already this project's pipeline — nothing to propose"}}
    if "error" in effective:
        lines = [f"replaces a setting that no longer validates ({effective['error']})"] + lines
    new_src = proposed.get("source") or {}
    moves_to_machine = (new_src.get("provider") == "machine-forge"
                        and new_src != (current.get("source") or {}))
    if moves_to_machine:
        # The user's approval of the reach grant is what lets this Host touch
        # that server at all; a card they could approve but the machine would
        # refuse is never raised.
        servers = await _pipeline_code_servers(pool, entity_id)
        hit = next((c for c in servers if c["machine"] == new_src["machine"]
                    and c["service"] == new_src["service"]), None)
        if hit is None:
            return _refused(f"the user has not approved this Host reaching '{new_src['service']}' on "
                f"{new_src['machine']} (Network → Servers), so its code cannot live there yet. "
                "Nothing was proposed.")
        if not hit["token_in_keys"]:
            return _refused(f"no token for that server in Keys under '{hit['token_key']}' — store the one "
                "you created when you built it (keys_manage op='add'). Nothing was proposed.")
        lines.append(f"keeps this project's code on {new_src['machine']} (the code server "
                     f"{new_src['service']}, repo {new_src['repo']}) from the next save point; "
                     "builds and tests still run on the platform, and the copy already on the "
                     "platform is left as it is")
    old_src = current.get("source") or {}
    if old_src.get("provider") == "machine-forge" and new_src.get("provider") != "machine-forge":
        lines.append(f"moves this project's code back to the platform from the next save point "
                     f"(its platform repo is used, or made again); the copy on "
                     f"{old_src.get('machine')} is left as it is")
    if wide:
        if not await _pipeline_stepup_enrolled(pool):
            return _refused(f"{', '.join(wide)} run outside the platform, so applying needs a code from the "
                "user's authenticator, and no second factor is enrolled yet. Nothing was "
                "proposed: ask the user to enroll one in the Network app first.")
        lines.append(f"runs outside the platform: {', '.join(wide)} — applying needs a code "
                     "from your authenticator")
    pending = await _pipeline_open_proposal(pool, entity_id, project_id)
    if pending:
        return _refused(f"ticket #{pending} already proposes a pipeline for this project and is waiting on "
            "the user. One proposal at a time: they answer that one first (Tickets), then "
            "propose again if it still applies.")
    heading = ("Change where the code of" if (new_src or {}) != (current.get("source") or {})
               else "Change how builds of")
    heading += f" “{row['name']}”" + (" lives:" if heading.startswith("Change where") else " are checked:")
    sitrep = (f"{heading}\n"
              + "\n".join(f"- {l}" for l in lines)
              + f"\nThe Host's reason: {reason[:500]}")
    options = [
        {"id": "apply", "label": "Apply this pipeline",
         "consequence": ("The next build of this project is checked this way. Nothing "
                         "already built changes."
                         + (" Needs a code from your authenticator." if wide else "")),
         "recommend": False},
        {"id": "keep", "label": "Keep the current pipeline",
         "consequence": "Nothing changes.", "recommend": False},
    ]
    try:
        from server.routes import tickets as _tk
        ticket = await _tk.create_ticket_service(
            pool, entity_id, source="runtime", sitrep=sitrep, options=options,
            context={"reason": reason[:500]}, priority=60,
            metadata={"kind": PIPELINE_PROPOSAL_KIND,
                      "pipeline_proposal": {"project_id": project_id, "pipeline": proposed,
                                            # Compare-and-set base: the apply
                                            # writes only if the setting is
                                            # still exactly this.
                                            "base": stored,
                                            # 'apply' takes the user's
                                            # authenticator code (tickets
                                            # decide_ticket_service).
                                            "requires_stepup": bool(wide),
                                            "widens": wide},
                      "user_only": True, "no_pattern": True})
    except Exception as e:  # noqa: BLE001
        return _refused_if_caller(e, f"could not propose the pipeline: "
                                             f"{getattr(e, 'detail', None) or e}")
    return {"status": "ok", "result": {
        "proposed": True, "ticket_id": ticket.get("id"), "changes": lines,
        # What the chat's ask card shows (app-web-kit/ask.ts askFromTool).
        "ask": {"ticket_id": ticket.get("id"), "sitrep": sitrep, "options": options,
                # The option the card takes the authenticator code for.
                **({"stepup_option": "apply"} if wide else {})},
        "note": ("the pipeline changes only when the user picks 'apply' on the card (it is "
                 "also in Circles, under Waiting on you). Work that does not wait on it "
                 "carries on. The proposal is open; a second one is refused while it is, "
                 "and the ticket is locked to the user."),
    }}


async def _exec_coding_protocol(args: dict) -> dict:
    """Orchestrator → Multi-Agent Coding: the protocol FILES.
    A protocol is instructions the host reads, never a program the platform
    runs — the host drives its steps with build / review.fan_out / ladder.run
    and records the user's pass/fail against it. Ops:
      list    → every protocol with its shape + stats
      get     → one file (slug) — read it, then follow it
      status  → grant preflight for one file: is every engine it names
                connected HERE, and the one action that fixes a row that isn't
      save    → write a file (name/slug + the document the user and host agreed)
      delete  → remove one
      record  → the run's outcome on the USER'S say: result pass|fail|abandoned
                (+ the session/task ids of the harness runs, for cost truth)
    Calls the route module's service functions directly — never the handlers."""
    a = args or {}
    op = (a.get("op") or "list").strip().lower()
    try:
        from server.routes import coding_protocols as cp
    except Exception as e:
        return _refused_if_caller(e, f"coding protocols unavailable: {e}")
    slug = str(a.get("slug") or "").strip()
    try:
        if op == "list":
            return {"status": "ok", "result": await cp.list_protocols()}
        if op == "get":
            if not slug:
                return _refused("slug required (coding_protocol op='list' names them)")
            return {"status": "ok", "result": await cp.get_protocol(
                slug, with_stats=True, runs=int(a.get("runs") or 5))}
        if op == "status":
            if not slug:
                return _refused("slug required")
            res = await cp.preflight(slug)
            res["next_step"] = (
                "Every engine this protocol names is connected — follow the file."
                if res.get("ready") else
                "Not every engine is granted yet: " + ", ".join(res.get("missing") or [])
                + ". Each row's action (a vendor login via harness_connections "
                "op='login', or a key in Keys) comes BEFORE running any step: start "
                "it when the user's request covers it, otherwise put it in the "
                "question box (escalate) — never substitute another engine silently.")
            return {"status": "ok", "result": res}
        if op == "save":
            doc = a.get("protocol")
            if isinstance(doc, str):
                try:
                    doc = json.loads(doc)
                except Exception as e:
                    return _refused_if_caller(e, f"protocol is not valid JSON: {e}")
            if not isinstance(doc, dict):
                return _refused("protocol (the JSON document) required")
            if a.get("name") and not doc.get("name"):
                doc["name"] = str(a["name"])
            if slug:
                saved = await cp.save_protocol(doc, slug=slug)
            else:
                saved = await cp.save_protocol(doc, create=True)
            return {"status": "ok", "result": {"saved": saved.get("slug"), "protocol": saved}}
        if op == "delete":
            if not slug:
                return _refused("slug required")
            return {"status": "ok", "result": await cp.delete_protocol(slug)}
        if op == "record":
            if not slug:
                return _refused("slug required")
            run = a.get("run") if isinstance(a.get("run"), dict) else {
                k: a.get(k) for k in ("result", "steps", "session_ids", "task_ids",
                                      "project_id", "note") if a.get(k) is not None}
            return {"status": "ok", "result": await cp.record_run(slug, run)}
        return _refused(f"unknown op {op!r} — list, get, status, save, delete, record")
    except Exception as e:
        detail = getattr(e, "detail", None) or str(e)
        return _refused_if_caller(e, f"coding_protocol {op}: {detail}")


def _project_of_thread(origin_context: str | None) -> int | None:
    """The project a thread context names ("project:<id>"), else None."""
    ic = (origin_context or "").strip()
    if not ic.startswith("project:"):
        return None
    try:
        return int(ic.split(":", 1)[1])
    except (TypeError, ValueError):
        return None


async def _exec_create_project(args: dict, entity_id: int, pool,
                               origin_context: str | None = None,
                               thread_id: str | None = None) -> dict:
    """Create a project, and open its thread with a marker pointing back to
    the conversation it was started from.

    The conversation is never touched (see the note above
    _conversation_label): the exchange that asked for the project stays whole
    where it was typed. `origin_context` + `thread_id` name that conversation
    the way the turn rows are stamped (memory_engine.derive_source_context,
    the function session.py stamps them with). Returns the new project_id and
    `started_from` ({kind, ref, title, link}, or None when no marker was
    written).

    Refused from inside a project thread. The tool schema is
    Circles-only, but `operate` reaches every registry op with no surface
    gate by design (direct_ops.run_direct_op), and that is the door a host
    walked through: in one project's thread it created another project for
    the same work. A project thread already IS a project —
    the plan for an ask typed there lands on that project. The refusal
    names the project so the next call is build(action='plan') on it.
    """
    name = (args.get("name") or "").strip()
    description = (args.get("description") or "").strip()

    here = _project_of_thread(origin_context)
    if here is not None:
        here_name = None
        try:
            here_name = await pool.fetchval(
                "SELECT name FROM projects WHERE id=$1 AND entity_id=$2", here, entity_id)
        except Exception:  # noqa: BLE001 - the refusal stands without the name
            here_name = None
        label = f"project #{here}" + (f" “{here_name}”" if here_name else "")
        return {
            "status": "denied",
            "result": (
                f"This thread already is {label} — a project is never created "
                f"from inside another project's thread. Plan the work here: "
                f"build(action='plan', project_id={here}, …). If it truly is a "
                f"separate project, the user starts it from the Workbench's "
                f"Index (a message sent there lands on a new page when no page "
                f"fits); if it belongs to another EXISTING project, act on "
                f"that project_id."
            ),
            "project_id": here,
        }

    if not name:
        return _refused("name is required")
    if not (3 <= len(name) <= 60):
        return _refused("name must be 3-60 chars")

    # An existing Code Repository (owner/name), as the Workbench's Start
    # offers (routes/projects.create_project): mounted when the Host can
    # write it and no live project holds it, or copied into a new repository
    # for this project with fork=true — the answer for a repository it can
    # only read, such as the platform's own. Checked before any row exists,
    # so a refusal leaves nothing behind. Builds at the root of such a
    # project are repository changes, unless the repository has an app
    # manifest at its root (target_contracts.root_target).
    mount_repo = str(args.get("repo") or "").strip()
    fork = bool(args.get("fork"))
    if fork and not mount_repo:
        return _refused("fork=true needs `repo`: the owner/name to copy")
    mount = None
    if mount_repo:
        from server.routes.projects import ProjectsError, plan_repo_mount
        try:
            mount = await plan_repo_mount(pool, entity_id, mount_repo, fork=fork)
        except ProjectsError as e:
            return _refused(e.detail)
        except Exception as e:  # noqa: BLE001
            return _refused_if_caller(e, f"could not read {mount_repo}: {getattr(e, 'detail', None) or e}")

    try:
        if mount and not mount["fork"]:
            from server.routes.projects import insert_mounted_project
            row = await insert_mounted_project(pool, entity_id, name, description or None, mount)
            row = {"id": row["id"], "name": name, "status": row["status"]}
        else:
            row = await pool.fetchrow(
                """
                INSERT INTO projects (entity_id, name, description, status)
                     VALUES ($1, $2, $3, 'active')
                  RETURNING id, name, status, created_at
                """,
                entity_id, name, description or None,
            )
    except Exception as e:
        log.warning("create_project insert failed for entity %d: %s", entity_id, e)
        if mount:
            from server.routes.projects import _is_unique_violation
            if _is_unique_violation(e):
                return _refused(f"{mount['repo']} already has a project. Fork it into a new "
                                "project instead (fork=true).")
        return _refused_if_caller(e, f"could not create project: {e}")

    project_id = row["id"]

    # Repo-per-project (v081): every project gets its own private gitea
    # repo at creation — the Workbench is the AI interface for that repo.
    # Fail-soft: no gitea connection just leaves repo NULL. A mounted or
    # forked project gets that repository instead.
    repo_note = None
    forked_from = None
    try:
        from server.routes.projects import (ProjectsError, ensure_project_dir,
                                            ensure_project_repo, fork_repo_for_project,
                                            mount_project_repo)
        # The directory is what fan_out, adopt and the repo sync all read to
        # decide whether this project exists on disk — see ensure_project_dir.
        ensure_project_dir(project_id)
        if mount and mount["fork"]:
            try:
                _repo = await fork_repo_for_project(pool, project_id, name, mount)
            except ProjectsError as e:
                # Nothing half-made: the row was created a moment ago for this
                # fork and holds no conversation, plan or files yet.
                await pool.execute("DELETE FROM projects WHERE id = $1 AND entity_id = $2",
                                   project_id, entity_id)
                return _refused(e.detail)
            forked_from = _repo.get("forked_from")
        elif mount:
            _repo = await mount_project_repo(pool, project_id, mount)
        else:
            _repo = await ensure_project_repo(pool, project_id, name)
        repo_note = _repo.get("repo") or _repo.get("reason")
        if mount and _repo.get("reason"):
            repo_note = f"{_repo.get('repo')}: {_repo['reason']}"
    except Exception as _re_err:
        repo_note = f"repo creation failed: {_re_err}"

    # Nothing moves out of the conversation it was asked in: the
    # new project's thread opens with a marker linking back to it instead.
    started_from = None
    try:
        from mastercode import circles_threads as _ct
        from mastercode.memory_engine import derive_source_context
        _src = derive_source_context(origin_context,
                                     thread_id=_ct.canonical_id(thread_id))
        started_from = await _write_started_from(pool, entity_id, project_id, _src)
    except Exception as e:  # noqa: BLE001 - the project stands without its marker
        log.warning("create_project started-from marker failed (non-fatal): %s", e)

    log.info(
        "create_project entity=%d id=%d name=%r started_from=%s",
        entity_id, project_id, name,
        f"{started_from['kind']}:{started_from['ref']}" if started_from else None,
    )
    return {
        "status": "ok",
        "repo": repo_note,
        "result": {
            "project_id": project_id,
            "name": row["name"],
            "status": row["status"],
            # The conversation it was started from — left whole where it was
            # typed; the project's thread opens with a marker linking to it.
            "started_from": started_from,
            **({"mounted": mount["repo"]} if mount and not mount["fork"] else {}),
            **({"forked_from": forked_from} if forked_from else {}),
        },
    }


async def _exec_list_projects(args: dict, entity_id: int, pool) -> dict:
    """Return a compact list of the entity's projects for model use.

    Filterable by case-insensitive substring on `name` and by `status`.
    Always scopes to the calling entity — cross-entity reads are never
    possible from this tool.
    """
    search = (args.get("search") or "").strip()
    status_filter = (args.get("status") or "").strip()
    try:
        limit = int(args.get("limit") or 20)
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(50, limit))
    # Paged: a Host with more projects than one
    # page reaches the rest with offset; total + next_offset say there are more.
    try:
        offset = max(0, int(args.get("offset") or 0))
    except (TypeError, ValueError):
        offset = 0

    where = ["p.entity_id = $1"]
    params: list = [entity_id]
    if search:
        params.append(f"%{search}%")
        where.append(f"p.name ILIKE ${len(params)}")
    if status_filter:
        params.append(status_filter)
        where.append(f"p.status = ${len(params)}")

    params.append(limit)
    params.append(offset)
    # PLAN STATE rides every row. A fresh entity held a
    # "Trading software" project with a repo, zero phases, no routing and an
    # empty description, and nothing in this list told the host it was a stub
    # — so nothing prompted "this project has no plan, want me to draft one?".
    # The build door already refuses without phases (the plan gate); this is
    # the read side of that same fact. One lateral aggregate over
    # project_phases (status: planning|speccing|ready|in_progress|review|
    # complete per init-db.sql, plus failed (v108) and archived (v110)), no
    # second query per row. `next_phase_status` is the status of the phase the
    # door would pick — first OPEN phase by number, same predicate as
    # _PHASE_OPEN_SQL — because has_plan alone lies: a drafted plan of
    # 'planning' placeholders has phases and still cannot build.
    sql = (
        "SELECT p.id, p.name, p.description, p.status, p.kind, p.created_at, p.updated_at, "
        "       ph.phase_count, ph.phases_ready, ph.phases_complete, ph.phases_open, "
        "       ph.last_phase_at, nx.next_phase_status, "
        "       COUNT(*) OVER () AS total_matched "
        "FROM projects p "
        "LEFT JOIN LATERAL ("
        "    SELECT COUNT(*)::int AS phase_count, "
        "           COUNT(*) FILTER (WHERE status IN ('ready','in_progress'))::int AS phases_ready, "
        "           COUNT(*) FILTER (WHERE status IN ('complete','review'))::int AS phases_complete, "
        "           COUNT(*) FILTER (WHERE " + _PHASE_OPEN_SQL + ")::int AS phases_open, "
        "           MAX(updated_at) AS last_phase_at "
        "    FROM project_phases WHERE project_id = p.id"
        ") ph ON TRUE "
        "LEFT JOIN LATERAL ("
        "    SELECT status AS next_phase_status FROM project_phases "
        "    WHERE project_id = p.id AND " + _PHASE_OPEN_SQL + " "
        "    ORDER BY phase_number ASC LIMIT 1"
        ") nx ON TRUE "
        "WHERE " + " AND ".join(where) +
        f" ORDER BY p.updated_at DESC NULLS LAST, p.id DESC"
        f" LIMIT ${len(params) - 1} OFFSET ${len(params)}"
    )

    try:
        rows = await pool.fetch(sql, *params)
    except Exception as e:
        log.warning("list_projects query failed for entity %d: %s", entity_id, e)
        return _refused_if_caller(e, f"could not list projects: {e}")

    # Every listed project's modules and each one's CURRENT version, in one
    # statement — so the host talks about "version 3
    # of the reader", never run ids. A failure never fails the listing.
    try:
        from server.routes.project_modules import modules_by_project
        _mods_by_pid = await modules_by_project(
            pool, [r["id"] for r in rows if (r["kind"] or "project") == "project"])
    except Exception as e:  # noqa: BLE001
        log.warning("list_projects: modules unreadable: %s", e)
        _mods_by_pid = {}

    projects = []
    without_plan = 0
    for r in rows:
        kind = r["kind"] or "project"
        phase_count = int(r["phase_count"] or 0)
        has_plan = phase_count > 0
        row = {
            "project_id": r["id"],
            "name": r["name"],
            "status": r["status"],
            # Artifacts share this table (kind='artifact') and this id space —
            # label them so the host knows an id here also feeds publish_site
            # and the artifact tool, not just the Workbench build lanes.
            "kind": kind,
            "description": (r["description"] or "").strip() or None,
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            # Plan state — the read side of the build door's plan gate.
            "has_plan": has_plan,
            "phase_count": phase_count,
            "phases_ready": int(r["phases_ready"] or 0),
            "phases_complete": int(r["phases_complete"] or 0),
            "phases_open": int(r["phases_open"] or 0),
            "next_phase_status": r["next_phase_status"],
            # What the build door would do RIGHT NOW: it picks the first open
            # phase and builds only 'ready' (or a 'failed' retry).
            "buildable": r["next_phase_status"] in (_PHASE_READY, _PHASE_FAILED),
            "last_phase_at": r["last_phase_at"].isoformat() if r["last_phase_at"] else None,
        }
        if kind == "project":
            # Its modules (fetched above); absent for a single-app project.
            _mods = _mods_by_pid.get(int(r["id"]))
            if _mods:
                row["modules"] = _mods
            nxt = r["next_phase_status"]
            if not has_plan:
                without_plan += 1
                row["hint"] = ("no plan yet. A single task builds now with "
                               "build(action='run', prompt=…); a phased build needs a "
                               "plan first — align on scope, then build(action='plan').")
            elif nxt is None:
                row["hint"] = ("nothing left to build — every phase is complete or archived. "
                               "What follows (publish, a new plan) is not a build: do it when "
                               "the user's request covers it, otherwise say what is done.")
            elif nxt not in (_PHASE_READY, _PHASE_FAILED):
                row["hint"] = (f"plan exists but its next phase is '{nxt}' — the build door "
                               "will refuse until that phase has a brief and is ready. "
                               "Finishing the plan (project_phase set_phase_spec) comes "
                               "first, not a build: do it when the user's request covers "
                               "it, otherwise say so.")
        projects.append(row)

    total = None
    if rows:
        try:
            total = int(rows[0]["total_matched"])
        except (KeyError, TypeError, ValueError):
            total = None
    elif offset == 0:
        total = 0
    result = {
        "count": len(projects),
        "projects": projects,
        "search": search or None,
        "status_filter": status_filter or None,
        "without_plan": without_plan,
        "offset": offset,
        "total": total,
    }
    if total is not None and offset + len(projects) < total:
        result["next_offset"] = offset + len(projects)
    if without_plan:
        result["note"] = (f"{without_plan} project(s) have no plan yet (has_plan=false). A "
                          "project with a repo but no phases has no phased work in "
                          "progress — say so plainly (a stub, not planned yet), without "
                          "waiting to be asked. Plan it (or run a single task) when the "
                          "user's request covers it; when whether to is the user's call, "
                          "put it in the question box (escalate).")
    return {"status": "ok", "result": result}
