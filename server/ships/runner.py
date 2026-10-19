"""runner.py — the ship runner: one run of a pipeline, moved on from receipts.

No model is in this path. A run is one ship of one pipeline at one commit:

    planned → awaiting_tap → running → passed | failed
                                     ↘ undoing → undone
    stopped: the device said no or the ask lapsed, the plan expired, a machine
    would not take a line, or the person pressed Stop.

1. start_run reads the pipeline file, builds the draft of ONE plan for the
   whole ship (server/ships/plan.py), checks every machine can run it, and
   files a `ship_plan` ask on the person's approval device.
2. On the tap, the device signs the plan. The runner keeps the envelope the
   first time it reads it (the device hands an approval out once) and checks it
   is exactly the draft it filed.
3. Each line goes to its machine through the signed-grant gate
   (nac_mgmt.run_mgmt_op, op_plan_line), with every earlier line's receipt and
   the credentials sealed to that machine (server/ships/seal.py). It waits at
   most DISPATCH_WAIT_S: a longer line keeps running on the machine and its
   receipt is read back with op_plan_status on a later tick. A dropped
   connection is read back the same way, never taken as a failure.
4. A receipt counts only once it verifies against the signed plan. A failed
   line takes its step's rule: retry (the machine's own attempts), undo (the
   undoable steps, newest first, on the failed receipt), stop, or ask (the run
   waits and the Host is woken under the pipeline's turn type).

The machines check the plan themselves; nothing here can make a machine run
what the person did not approve. What this module decides is only when to
send the next line, and what to show.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from datetime import datetime, timedelta, timezone

from server.ships import pipeline as pl
from server.ships import plan as sp
from server.ships import seal

log = logging.getLogger("cseries.ships")

TICK_S = 15.0
DISPATCH_WAIT_S = 45.0
STATUS_WAIT_S = 30.0
# How long a sent line may go without a receipt before the runner stops and
# says so, rather than guess: the program's apply and its verify (each up to
# the manifest's time_limit, ops.go), every verb at the six minutes a rollout
# or a unit restart may take (ops_verbs_linux.go opCmdDeadline), and a margin.
VERB_WAIT_S = 6 * 60
STALE_MARGIN_S = 10 * 60
# A Host may not ask again for 24h after the person refused a ship of that
# pipeline on their device, or stopped it before approving: the device's own
# 24h refusal lock keys on the whole plan, whose expiry differs every time.
HOST_REASK_AFTER_REFUSAL = timedelta(hours=24)
HOST = "your Host"


def line_budget_s(manifest: dict) -> int:
    tl = int(manifest.get("time_limit") or 600)
    return 2 * tl + VERB_WAIT_S * len(manifest.get("verbs") or []) + STALE_MARGIN_S


def undo_budget_s(verbs: int) -> int:
    return VERB_WAIT_S * int(verbs) + STALE_MARGIN_S
ACTIVE_STATES = ("planned", "awaiting_tap", "running", "undoing")
SHIP_OPS = ("op_plan_line", "op_plan_undo", "op_plan_status")


class RunnerError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


def _j(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


# ── machines ────────────────────────────────────────────────────────────────

async def resolve_bastion(pool, eid: int, hostname: str) -> dict:
    """The one live bastion of this Host reporting `hostname`. Two is refused:
    a line sent to the wrong one of two same-named machines would be refused
    there at best (as run_mgmt_op does)."""
    rows = await pool.fetch(
        "SELECT h.token_id, h.gateway_version, h.last_seen_at FROM network_bastion_heartbeat h "
        "  JOIN mcp_client_tokens t ON t.id = h.token_id "
        " WHERE h.hostname=$1 AND t.entity_id=$2 AND t.revoked_at IS NULL AND t.operator_kind='bastion' "
        " ORDER BY h.last_seen_at DESC", hostname, int(eid))
    tokens = {r["token_id"] for r in rows or []}
    if not tokens:
        raise RunnerError(404, f"no bastion of this Host reports the hostname '{hostname}'")
    if len(tokens) > 1:
        raise RunnerError(409, f"{len(tokens)} bastions report the hostname '{hostname}'; revoke the one "
                               f"that is not this machine")
    r = rows[0]
    return {"token_id": r["token_id"], "version": r["gateway_version"],
            "last_seen": r["last_seen_at"].isoformat() if r["last_seen_at"] else None}


async def machine_state(pool, eid: int, hostname: str) -> dict:
    """What the Ships app shows for a machine, and why a ship cannot use it:
    its bastion, whether it runs action ops, the devices pinned there, the
    grant each ship op needs, and the encryption key pinned for it."""
    from server.network import nac_approval, nac_mgmt
    out = {"hostname": hostname, "problems": []}
    try:
        b = await resolve_bastion(pool, eid, hostname)
    except RunnerError as e:
        out["problems"].append(e.detail)
        return out
    out.update(b)
    g = await nac_approval.box_gate(pool, hostname, token_id=b["token_id"])
    out.update({"anchored": g["anchored"], "write_gate": g["write_gate"], "action_ops": g["action_ops"],
                "action_ops_reason": g["action_ops_reason"], "device_keys": g["device_keys"],
                "encryption_key": bool(g["encryption_key"])})
    if g["action_ops"] is not True:
        out["problems"].append(f"{hostname} cannot run a ship step: "
                               f"{g['action_ops_reason'] or 'its bastion is older than 0.46.0'}")
    if not g["device_keys"]:
        out["problems"].append(f"{hostname} has no approval device pinned, so no plan can be approved for it")
    grant = {}
    for op in SHIP_OPS:
        ok, _reason, _d = await nac_mgmt.gate_op(pool, eid, hostname, op)
        grant[op] = ok
    out["grant"] = grant
    missing = [op for op, ok in grant.items() if not ok]
    if missing:
        out["problems"].append(f"{hostname}: no signed grant covers {', '.join(missing)} "
                               f"(a management grant with purpose 'ship')")
    pin = await pool.fetchrow("SELECT encryption_key, first_seen FROM ship_machine_keys WHERE token_id=$1",
                              int(b["token_id"]))
    out["pinned_key"] = ({"key": pin["encryption_key"], "since": pin["first_seen"].isoformat()}
                         if pin else None)
    return out


# ── ops and pipelines ───────────────────────────────────────────────────────

async def load_action_op(pool, name: str) -> dict:
    from server.network import nac_ops
    try:
        o = await nac_ops.load_op(pool, name)
    except nac_ops.OpError as e:
        raise RunnerError(e.status, e.detail)
    if o["manifest"].get("kind") != "action":
        raise RunnerError(400, f"op '{name}' is a files op; a ship step runs an action op (kind: action)")
    return {"name": o["manifest"]["name"], "sha256": o["sha256"], "manifest": o["manifest"], "files": o["files"]}


async def upsert_pipeline(pool, found: dict) -> int:
    spec = found["spec"]
    source = f"project {found['project_id']} module {found['module']} {found['path']}"
    return await pool.fetchval(
        "INSERT INTO ship_pipelines (name, spec, spec_sha256, source) VALUES ($1,$2::jsonb,$3,$4) "
        "ON CONFLICT (name) DO UPDATE SET spec=EXCLUDED.spec, spec_sha256=EXCLUDED.spec_sha256, "
        " source=EXCLUDED.source, last_seen=now() RETURNING id",
        spec["name"], json.dumps(spec), pl.spec_sha256(spec), source)


# ── rows ────────────────────────────────────────────────────────────────────

_RUN_COLS = ("id, pipeline_id, spec, spec_sha256, commit_sha, trigger, started_by, state, step, plan_draft, "
             "device_ask, plan_envelope, plan_nonce, plan_sha256, plan_expires, undo_from, stop_requested, "
             "needs_host, host_woken, refused_at, error, finished_at, first_seen, last_seen")


async def _run(pool, rid: int) -> dict | None:
    r = await pool.fetchrow(f"SELECT {_RUN_COLS} FROM ship_runs WHERE id=$1", int(rid))
    if r is None:
        return None
    out = dict(r)
    for k in ("spec", "plan_draft", "device_ask"):
        out[k] = _j(out[k])
    return out


async def _steps(pool, rid: int) -> dict:
    rows = await pool.fetch(
        "SELECT n, step, machine, state, attempts, sent_at, stale_s, receipt, undo_receipt, error FROM ship_steps "
        " WHERE run_id=$1 ORDER BY n", int(rid))
    out = {}
    for r in rows or []:
        d = dict(r)
        d["receipt"], d["undo_receipt"] = _j(d["receipt"]), _j(d["undo_receipt"])
        out[d["n"]] = d
    return out


async def _finish(pool, rid: int, state: str, error: str | None = None, *, needs_host: bool = False,
                  refused: bool = False, from_states: tuple = ACTIVE_STATES) -> bool:
    """Settle a run, only from a state it may be settled from: a run another
    tick or the person settled first is left as they left it. `refused`: the
    person said no to it before approving (on the device, or Stop)."""
    got = await pool.fetchval(
        "UPDATE ship_runs SET state=$2, error=$3, needs_host=$4, finished_at=now(), last_seen=now(), "
        " refused_at=CASE WHEN $5 THEN now() ELSE refused_at END "
        " WHERE id=$1 AND state = ANY($6::text[]) RETURNING id",
        int(rid), state, (error or None) and error[:1000], needs_host, refused, list(from_states))
    if got is not None and state == "passed":
        # A later run that passed answers what an earlier one stopped for.
        await pool.execute(
            "UPDATE ship_runs SET needs_host=false WHERE needs_host AND id < $1 "
            "   AND pipeline_id = (SELECT pipeline_id FROM ship_runs WHERE id=$1)", int(rid))
    return got is not None


async def _note(pool, rid: int, error: str) -> None:
    """A reason the run is waiting, shown in the app; the run goes on."""
    await pool.execute("UPDATE ship_runs SET error=$2, last_seen=now() WHERE id=$1", int(rid), error[:1000])


async def _halt(pool, run: dict, n: int, why: str) -> None:
    """A line that could not be sent or read: nothing signed says what became
    of it, so nothing can be retried or undone on its strength. The run stops
    and says why."""
    await pool.execute(
        "UPDATE ship_steps SET state=CASE WHEN state IN ('waiting','sent') THEN 'failed' ELSE state END, "
        " error=$3, last_seen=now() WHERE run_id=$1 AND n=$2", int(run["id"]), n, why[:1000])
    await _finish(pool, run["id"], "stopped", why)


# ── start ───────────────────────────────────────────────────────────────────

async def start_run(pool, eid: int, name: str, commit: str, *, trigger: str = "manual", by: str = "") -> dict:
    """Start a ship of pipeline `name` at `commit`: build the plan, check every
    machine, and ask the person's device. Refuses (RunnerError) before any row
    or ask when the ship cannot run, naming every reason at once."""
    from server.network import nac_approval
    try:
        found = await pl.find(pool, name)
    except pl.PipelineError as e:
        raise RunnerError(404 if "no pipeline" in str(e) else 400, str(e))
    spec = found["spec"]
    commit = (commit or "").strip().lower()
    if not sp.COMMIT_RE.match(commit):
        raise RunnerError(400, "a run ships a full commit id (40 or 64 hex), never a branch name")
    if trigger not in ("merge", "schedule", "manual"):
        raise RunnerError(400, "trigger must be merge, schedule or manual")
    ops = {}
    for s in spec["steps"]:
        ops[s["step"]] = await load_action_op(pool, s["op"])
    try:
        draft = sp.build_draft(spec, commit, ops)
    except sp.PlanError as e:
        raise RunnerError(400, str(e))
    hosts = list(dict.fromkeys(s["machine"] for s in spec["steps"]))
    if by == HOST:
        # Keyed on the MACHINES, not the pipeline's name: a Host writes
        # pipeline files, so a renamed copy of a pipeline the person turned
        # down would otherwise ring their device again at once.
        refused = await pool.fetchrow(
            "SELECT max(r.refused_at) AS at, array_agg(DISTINCT s.machine) AS machines "
            "  FROM ship_runs r JOIN ship_steps s ON s.run_id = r.id "
            " WHERE r.refused_at > now() - $1::interval AND s.machine = ANY($2::text[])",
            HOST_REASK_AFTER_REFUSAL, hosts)
        if refused is not None and refused["at"] is not None:
            raise RunnerError(409, f"the person turned down a ship to {', '.join(sorted(refused['machines']))} at "
                                   f"{refused['at'].strftime('%Y-%m-%d %H:%M UTC')}; you may not ask again for a "
                                   f"ship to those machines for 24 hours. They can start one themselves in the "
                                   f"Ships app")
    states = [await machine_state(pool, eid, h) for h in hosts]
    problems = [p for st in states for p in st["problems"]]
    problems += await _credential_problems(pool, eid, spec, ops, states)
    keysets = [set(st.get("device_keys") or []) for st in states]
    keys = set.intersection(*keysets) if keysets else set()
    device = None
    if not problems:
        if not keys:
            problems.append("no one approval device is pinned on every machine of this pipeline, so one "
                            "plan cannot be approved for all of them")
        else:
            device = await nac_approval.find_device(pool, eid, sorted(keys))
            if device is None:
                problems.append("the approval device pinned on these machines has not checked in with this Host")
    if problems:
        raise RunnerError(409, "this ship cannot start: " + "; ".join(problems))
    pid = await upsert_pipeline(pool, found)
    ask = {"token_id": device["token_id"], "hostname": device["hostname"], "keys": sorted(keys)}
    expires = datetime.strptime(draft["expires"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    import asyncpg
    async with pool.acquire() as con:
        try:
            async with con.transaction():
                rid = await con.fetchval(
                    "INSERT INTO ship_runs (pipeline_id, spec, spec_sha256, commit_sha, trigger, started_by, state, "
                    " plan_draft, device_ask, plan_expires) "
                    "VALUES ($1,$2::jsonb,$3,$4,$5,$6,'planned',$7::jsonb,$8::jsonb,$9) RETURNING id",
                    pid, json.dumps(spec), pl.spec_sha256(spec), commit, trigger, (by or "")[:200],
                    json.dumps(draft), json.dumps(ask), expires)
                for ln in draft["lines"]:
                    await con.execute(
                        "INSERT INTO ship_steps (run_id, n, step, machine, state) VALUES ($1,$2,$3,$4,'waiting')",
                        rid, ln["n"], ln["step"], ln["machine"])
        except asyncpg.UniqueViolationError:
            raise RunnerError(409, f"a run of '{name}' is already under way; one run of a pipeline at a time")
    await _file_ask(pool, eid, await _run(pool, rid))
    return await run_view(pool, rid)


async def _credential_problems(pool, eid: int, spec: dict, ops: dict, states: list) -> list:
    """Before the person is rung: every credential a step's op declares is in
    this Host's vault, and a machine that takes one reports an encryption key
    that verifies under its own key and matches the key pinned for it."""
    from server.network import nac_approval
    from server.vault import api as vault_api
    need = {}
    for s in spec["steps"]:
        for c in ops[s["step"]]["manifest"].get("credentials") or []:
            need.setdefault(s["machine"], []).append((s["step"], c))
    if not need:
        return []
    out = []
    held = {x["logical_name"] for x in await vault_api.list_secrets(pool, entity_id=eid)
            if not x.get("deactivated_at")}
    by_host = {st["hostname"]: st for st in states}
    for host, items in need.items():
        for step, c in items:
            if c not in held:
                out.append(f"step {step}: this Host's vault holds no credential named '{c}'")
        st = by_host.get(host) or {}
        if st.get("token_id") is None:
            continue
        g = await nac_approval.box_gate(pool, host, token_id=st["token_id"])
        claim = g.get("encryption_key")
        try:
            pub = seal.verify_claim(claim, (claim or {}).get("machine_key") or "")
        except seal.SealError as e:
            out.append(f"{host}: {e}")
            continue
        pin = await pool.fetchval("SELECT encryption_key FROM ship_machine_keys WHERE token_id=$1",
                                  int(st["token_id"]))
        if pin is not None and pin != pub:
            out.append(f"{host} reports another encryption key than the one pinned for it; nothing is "
                       f"sealed to it until the pin is cleared in the Ships app")
    return out


async def _file_ask(pool, eid: int, run: dict) -> None:
    from server.network import nac_approval
    ask = run["device_ask"]
    device = {"token_id": ask["token_id"], "hostname": ask["hostname"]}
    res = await nac_approval._device_file(pool, eid, device, {"kind": sp.PLAN_KIND, "plan": run["plan_draft"]},
                                          server="")
    if res["state"] == "approved":
        await _take_envelope(pool, run, res["approval"])
    elif res["state"] == "pending":
        await pool.execute(
            "UPDATE ship_runs SET state='awaiting_tap', device_ask=$2::jsonb, error=NULL, last_seen=now() "
            " WHERE id=$1 AND state IN ('planned','awaiting_tap')",
            run["id"], json.dumps({**ask, "id": res["id"]}))
    elif res["state"] == "offline":
        await _note(pool, run["id"], res["detail"])
    else:
        await _finish(pool, run["id"], "stopped", res.get("detail") or "the device refused the ask",
                      refused=bool(res.get("by_person")))


async def _take_envelope(pool, run: dict, approval: str) -> None:
    """The device's answer, kept on first read, and only if it is the plan this
    run filed, signed by the device pinned on its machines."""
    try:
        p, sha = sp.open_envelope(approval, device_keys=run["device_ask"]["keys"])
        sp.plan_matches_draft(p, run["plan_draft"])
    except sp.PlanError as e:
        await _finish(pool, run["id"], "stopped", f"the device's answer is not this run's plan: {e}")
        return
    exp = sp._parse_time(p["expires"])
    await pool.execute(
        "UPDATE ship_runs SET plan_envelope=$2, plan_nonce=$3, plan_sha256=$4, plan_expires=$5, state='running', "
        " step=1, error=NULL, last_seen=now() WHERE id=$1 AND state IN ('planned','awaiting_tap')",
        run["id"], approval, p["nonce"], sha, exp)


# ── the tick ────────────────────────────────────────────────────────────────

_locks: dict = {}


async def tick(pool, eid: int) -> None:
    rows = await pool.fetch("SELECT id FROM ship_runs WHERE state = ANY($1::text[]) ORDER BY id",
                            list(ACTIVE_STATES))
    for r in rows or []:
        try:
            await advance(pool, eid, r["id"])
        except Exception:
            log.exception("ships: run %s did not advance", r["id"])


async def advance(pool, eid: int, rid: int) -> None:
    """Move a run as far as it can go now: a passed line leads straight to the
    next, and the run stops at the first thing it must wait for."""
    lock = _locks.setdefault(int(rid), asyncio.Lock())
    if lock.locked():
        return
    async with lock:
        for _ in range(4 * sp.LINES_MAX + 8):
            run = await _run(pool, rid)
            if run is None or run["state"] not in ACTIVE_STATES:
                return
            if not await _advance_once(pool, eid, run):
                return


async def _advance_once(pool, eid: int, run: dict) -> bool:
    """One move. True when the run may move again at once."""
    if run["state"] == "planned":
        # Filed only while the plan is live: a device that comes back after
        # the expiry must not ring the person (or spend their code) for it.
        if run["plan_expires"] and run["plan_expires"] <= _now():
            await _finish(pool, run["id"], "stopped", "the plan lapsed before your approval device could be "
                                                      "reached")
            return False
        await _file_ask(pool, eid, run)
        return False
    if run["state"] == "awaiting_tap":
        return await _collect_tap(pool, eid, run)
    p, sha = sp.open_envelope(run["plan_envelope"])
    steps = await _steps(pool, run["id"])
    if run["state"] == "undoing":
        return await _drive_undo(pool, eid, run, p, sha, steps)
    return await _drive(pool, eid, run, p, sha, steps)


async def _collect_tap(pool, eid: int, run: dict) -> bool:
    from server.network import nac_approval
    if run["stop_requested"]:
        await _finish(pool, run["id"], "stopped", "stopped by the person before it was approved", refused=True)
        return False
    if run["plan_expires"] and run["plan_expires"] <= _now():
        await _finish(pool, run["id"], "stopped", "the plan lapsed before it was approved on your device")
        return False
    ask = run["device_ask"]
    if not ask.get("id"):
        await _file_ask(pool, eid, run)
        return False
    device = {"token_id": ask["token_id"], "hostname": ask["hostname"]}
    st = await nac_approval._device_status(pool, eid, device, ask["id"])
    if st["state"] == "approved":
        await _take_envelope(pool, run, st["approval"])
        return True
    if st["state"] == "pending":
        return False
    if st["state"] == "offline":
        await _note(pool, run["id"], st["detail"])
        return False
    if st["state"] == "denied":
        await _finish(pool, run["id"], "stopped", f"you refused this ship on {ask['hostname']}", refused=True)
        return False
    await _finish(pool, run["id"], "stopped", "your device no longer holds this ask (it lapsed, or its "
                                              "answer was already read); start the run again")
    return False


async def _drive(pool, eid: int, run: dict, p: dict, sha: str, steps: dict) -> bool:
    n = int(run["step"])
    s = steps[n]
    last = len(p["lines"])
    if s["state"] == "sent":
        return await _poll(pool, eid, run, p, sha, n, steps)
    if s["state"] == "waiting":
        if run["stop_requested"]:
            await _finish(pool, run["id"], "stopped", f"stopped by the person before step {n} ({s['step']})")
            return False
        if sp._parse_time(p["expires"]) <= _now():
            await _finish(pool, run["id"], "stopped", f"the plan expired before step {n} ({s['step']}) ran; "
                                                      f"start a new run")
            return False
        return await _send(pool, eid, run, p, sha, n, steps)
    if s["state"] == "passed":
        if n >= last:
            await _finish(pool, run["id"], "passed")
            return False
        if run["stop_requested"]:
            await _finish(pool, run["id"], "stopped", f"stopped by the person after step {n} ({s['step']})")
            return False
        await pool.execute("UPDATE ship_runs SET step=$2, error=NULL, last_seen=now() WHERE id=$1", run["id"], n + 1)
        return True
    if s["state"] == "failed":
        return await _on_fail(pool, eid, run, p, sha, n, steps)
    return False


async def _send(pool, eid: int, run: dict, p: dict, sha: str, n: int, steps: dict) -> bool:
    from server.network import nac_approval, nac_mgmt, nac_ops
    line = p["lines"][n - 1]
    host = line["machine"]
    try:
        b = await resolve_bastion(pool, eid, host)
    except RunnerError as e:
        await _halt(pool, run, n, e.detail)
        return False
    ok, reason, _d = await nac_mgmt.gate_op(pool, eid, host, "op_plan_line")
    if not ok:
        await _halt(pool, run, n, f"{host}: op_plan_line is not permitted — {reason}")
        return False
    try:
        op = await load_action_op(pool, line["op"])
    except RunnerError as e:
        await _halt(pool, run, n, e.detail)
        return False
    if op["sha256"] != line["op_sha256"]:
        await _halt(pool, run, n, f"op {line['op']} changed after this plan was approved (it is "
                                  f"{op['sha256'][:12]} now, {line['op_sha256'][:12]} in the plan); start a new run")
        return False
    # The machine runs only an op it holds (ops_plan_linux.go opLoad): sync it,
    # under terms this Host signs, as the op door does before a dry run.
    terms = nac_ops.run_terms(eid, host, op["manifest"], op["sha256"], line["params"], nac_ops.new_nonce(),
                              p["expires"])
    tb, sig = await nac_ops._sign(pool, eid, terms)
    try:
        code, _out, err = await nac_ops._box(pool, b["token_id"], host, "op_sync",
                                             [tb, sig, nac_ops.bundle_b64(op["files"])], STATUS_WAIT_S)
    except nac_ops.OpError as e:
        # No worker, or one that did not answer in time: the sync is sent
        # again on the next tick (a machine that holds the op already takes it
        # again as a no-op, ops.go opInstall).
        await nac_mgmt.audit_mgmt(pool, eid, host, "op_sync", exit_code=None, decision=f"ship-sync-{e.status}")
        await _note(pool, run["id"], f"{host}: {e.detail}; step {n} waits for it")
        return False
    await nac_mgmt.audit_mgmt(pool, eid, host, "op_sync", exit_code=code,
                              decision="ship-sync" if code == 0 else "refused-box-sync")
    if code != 0:
        await _halt(pool, run, n, f"{host} would not hold op {line['op']}: {err.strip()[:500]}")
        return False
    blobs = []
    names = op["manifest"].get("credentials") or []
    if names:
        g = await nac_approval.box_gate(pool, host, token_id=b["token_id"])
        try:
            pub = await seal.pinned_key(pool, b["token_id"], host, g["encryption_key"], p["machines"][host])
            blobs = await seal.sealed_for_line(pool, eid, names, pub, p["nonce"], p["expires"])
        except seal.SealError as e:
            await _halt(pool, run, n, str(e))
            return False
    # EVERY earlier line's receipt: the machine runs a line only after every
    # line before it succeeded, shown by their signed receipts (planInputs).
    receipts = [steps[k]["receipt"] for k in range(1, n) if steps[k]["receipt"]]
    args = [_b64(run["plan_envelope"]), str(n), _b64(json.dumps(receipts)) if receipts else "", *blobs]
    # Moved to 'sent' only from 'waiting': a second tick (or a second pod
    # during a roll) that got this far finds it moved and sends nothing.
    moved = await pool.fetchval(
        "UPDATE ship_steps SET state='sent', attempts=attempts+1, sent_at=now(), stale_s=$3, error=NULL, "
        " last_seen=now() WHERE run_id=$1 AND n=$2 AND state='waiting' RETURNING n",
        run["id"], n, line_budget_s(op["manifest"]))
    if moved is None:
        return False
    try:
        res = await nac_mgmt.run_mgmt_op(pool, eid, host, "op_plan_line", args, token_id=b["token_id"],
                                         note=f"ship {p['pipeline']} run {run['id']} step {n}",
                                         budget_s=DISPATCH_WAIT_S)
    except nac_mgmt.MgmtError as e:
        if e.status == 504:
            return False  # the machine took it and runs on; its receipt is read back
        if e.status == 502:
            # Nothing picked the job up: it never reached the machine.
            await pool.execute(
                "UPDATE ship_steps SET state='waiting', attempts=GREATEST(attempts-1,0), sent_at=NULL, "
                " last_seen=now() WHERE run_id=$1 AND n=$2 AND state='sent'", run["id"], n)
            await _note(pool, run["id"], f"{host}: {e.detail}; step {n} waits for it")
            return False
        await _halt(pool, run, n, f"{host}: {e.detail}")
        return False
    return await _answer(pool, eid, run, p, sha, n, steps, res)


async def _answer(pool, eid: int, run: dict, p: dict, sha: str, n: int, steps: dict, res: dict) -> bool:
    code, out, err = res.get("exit"), res.get("stdout") or "", res.get("stderr") or ""
    if code == 0:
        try:
            body = json.loads(out)
        except ValueError:
            body = {}
        if not isinstance(body, dict) or not body.get("receipt"):
            await _halt(pool, run, n, f"{p['lines'][n-1]['machine']} answered step {n} with no receipt")
            return False
        return await _take_receipt(pool, run, p, sha, n, body["receipt"])
    if err.startswith("relay error"):
        return False  # the connection dropped under a long line; it runs on and is read back
    # A refusal. The line may be running already from an earlier send, so the
    # machine is asked what it holds before the run is stopped.
    return await _poll(pool, eid, run, p, sha, n, steps, refusal=(err.strip() or f"exit {code}"))


async def _read_status(pool, eid: int, host: str, p: dict, sha: str, n: int) -> list:
    """Every receipt the machine keeps for line n (every attempt, and its undo),
    each verified. Raises RunnerError when the machine cannot be read, or
    returns a receipt that does not verify."""
    from server.network import nac_mgmt
    b = await resolve_bastion(pool, eid, host)
    try:
        res = await nac_mgmt.run_mgmt_op(pool, eid, host, "op_plan_status", [p["nonce"], str(n)],
                                         token_id=b["token_id"], budget_s=STATUS_WAIT_S)
    except nac_mgmt.MgmtError as e:
        raise RunnerError(e.status, f"{host}: {e.detail}")
    if res.get("exit") != 0:
        # 126: refused (no grant for op_plan_status, a quarantine): waiting
        # does not change that. Anything else may pass.
        raise RunnerError(403 if res.get("exit") == 126 else 502,
                          f"{host} would not read step {n}'s receipts back: "
                          f"{(res.get('stderr') or '').strip()[:300]}")
    try:
        got = json.loads(res.get("stdout") or "{}").get("receipts") or []
    except (ValueError, AttributeError):
        raise RunnerError(502, f"{host} answered step {n}'s receipts with something unreadable")
    out = []
    for s in got:
        try:
            out.append((sp.verify_receipt(s, p, sha), s))
        except sp.PlanError as e:
            raise RunnerError(502, f"{host} returned a receipt for step {n} that does not verify: {e}")
    return out


def _stale(sent_at, stale_s=None) -> bool:
    limit = int(stale_s) if stale_s else line_budget_s({})
    return sent_at is not None and (_now() - sent_at).total_seconds() > limit


async def _poll(pool, eid: int, run: dict, p: dict, sha: str, n: int, steps: dict, refusal: str | None = None) -> bool:
    host = p["lines"][n - 1]["machine"]
    step = (await _steps(pool, run["id"]))[n]
    try:
        got = await _read_status(pool, eid, host, p, sha, n)
    except RunnerError as e:
        # Only a refusal or a receipt that does not verify stops the run now.
        # A read that did not answer (no worker, 504, a dropped connection)
        # waits: the line runs on at the machine and keeps its receipts.
        if refusal:
            await _halt(pool, run, n, f"{host} refused step {n}: {refusal}")
        elif e.status == 403 or "does not verify" in e.detail:
            await _halt(pool, run, n, e.detail)
        elif _stale(step["sent_at"], step.get("stale_s")):
            await _halt(pool, run, n, f"step {n}'s receipt could not be read back from {host}: {e.detail}")
        else:
            await _note(pool, run["id"], f"step {n} is on {host}; its receipt is being read back ({e.detail})")
        return False
    lines = [(r, s) for r, s in got if not r.get("undo") and r["n"] == n]
    passed = [x for x in lines if not sp.receipt_failed(x[0])]
    newest = max(lines, key=lambda x: int(x[0].get("attempt") or 1)) if lines else None
    # A passed line never runs again, whichever send it came from; a failed
    # one counts only once it is the attempt last sent.
    if passed:
        return await _take_receipt(pool, run, p, sha, n, passed[0][1])
    if newest and int(newest[0].get("attempt") or 1) >= int(step["attempts"] or 1):
        return await _take_receipt(pool, run, p, sha, n, newest[1])
    if refusal:
        await _halt(pool, run, n, f"{host} refused step {n}: {refusal}")
        return False
    if _stale(step["sent_at"], step.get("stale_s")):
        mins = int(step.get("stale_s") or line_budget_s({})) // 60
        await _halt(pool, run, n, f"no receipt of step {n} came back from {host} in {mins} "
                                  f"minutes; it may still be running there, so read the machine before "
                                  f"starting another run")
    return False


async def _take_receipt(pool, run: dict, p: dict, sha: str, n: int, signed) -> bool:
    try:
        r = sp.verify_receipt(signed, p, sha)
    except sp.PlanError as e:
        await _halt(pool, run, n, f"step {n}'s receipt does not verify: {e}")
        return False
    if r.get("undo") or r["n"] != n:
        await _halt(pool, run, n, f"step {n} was answered with another line's receipt")
        return False
    failed = sp.receipt_failed(r)
    err = (r.get("error") or r.get("verify_error") or "")[:1000] or None
    await pool.execute(
        "UPDATE ship_steps SET state=$3, receipt=$4::jsonb, attempts=GREATEST(attempts,$5), error=$6, "
        " last_seen=now() WHERE run_id=$1 AND n=$2",
        run["id"], n, "failed" if failed else "passed", json.dumps(signed), int(r.get("attempt") or 1), err)
    return True


async def _on_fail(pool, eid: int, run: dict, p: dict, sha: str, n: int, steps: dict) -> bool:
    s = steps[n]
    rule = run["spec"]["steps"][n - 1].get("on_fail") or {"rule": "stop"}
    line = p["lines"][n - 1]
    why = f"step {n} ({s['step']}) failed on {s['machine']}: {s['error'] or 'its check did not pass'}"
    if s["receipt"] is None:
        await _finish(pool, run["id"], "stopped", why)
        return False
    r = sp.verify_receipt(s["receipt"], p, sha)
    allowed = int(line.get("attempts") or 1)
    if (rule["rule"] == "retry" and int(r.get("attempt") or 1) < allowed and not run["stop_requested"]
            and sp._parse_time(p["expires"]) > _now()):
        # Back to 'waiting' only from 'failed': a second process that already
        # sent the next attempt is not put back behind it.
        await pool.execute("UPDATE ship_steps SET state='waiting', last_seen=now() "
                           " WHERE run_id=$1 AND n=$2 AND state='failed'", run["id"], n)
        return True
    if rule["rule"] == "undo":
        # A Stop pressed while the step ran does not cancel its rollback:
        # taking the machines back to what was approved is what the rule
        # promised. A Stop pressed during the undo itself is honoured.
        await pool.execute("UPDATE ship_runs SET state='undoing', undo_from=$2, error=$3, stop_requested=false, "
                           " last_seen=now() WHERE id=$1 AND state='running'", run["id"], n, why[:1000])
        return True
    if rule["rule"] == "ask":
        await _finish(pool, run["id"], "failed", why, needs_host=True)
        await _wake(pool, eid, run, why)
        return False
    if rule["rule"] == "retry":
        why = f"{why} (after {r.get('attempt') or 1} attempt(s), all the rule allows)"
    await _finish(pool, run["id"], "failed", why)
    return False


# ── undo ────────────────────────────────────────────────────────────────────

def undo_targets(p: dict, steps: dict, n0: int, fr: dict) -> tuple[list, str]:
    """The steps an undo takes back, newest first, by the machine's own rule
    (ops_plan_linux.go planUndoLine / planUndoAllowed), and why any earlier
    undoable step stays. `fr` is the failed step's receipt.

      - the failed line itself, when it may be undone: its own failure;
      - an earlier line that PASSED, only when the failed line's CHECK failed
        (its apply ran: ok, with a verify_error) on the last attempt the plan
        allows it. A failed apply (a verb or the program) undoes nothing
        before it: the machines refuse that, and every refusal is a strike
        toward the Host's quarantine."""
    out = []
    if p["lines"][n0 - 1].get("undo") and steps[n0]["receipt"] is not None:
        out.append(n0)
    earlier = [k for k in range(n0 - 1, 0, -1) if p["lines"][k - 1].get("undo")]
    check_failed = bool(fr.get("ok")) and bool(fr.get("verify_error"))
    last = int(p["lines"][n0 - 1].get("attempts") or 1)
    if not earlier:
        return out, ""
    if not check_failed:
        return out, (f"step(s) {', '.join(map(str, earlier))} stay as they are: an earlier step is taken back "
                     f"only when a later step's check fails, and step {n0} failed before its check ran")
    if int(fr.get("attempt") or 1) != last:
        return out, (f"step(s) {', '.join(map(str, earlier))} stay as they are: step {n0}'s check failed on "
                     f"attempt {fr.get('attempt') or 1} of {last}")
    for k in earlier:
        if steps[k]["state"] in ("passed", "undoing", "undone") and steps[k]["receipt"] is not None:
            out.append(k)
    return out, ""


async def _drive_undo(pool, eid: int, run: dict, p: dict, sha: str, steps: dict) -> bool:
    from server.network import nac_mgmt
    n0 = int(run["undo_from"])
    fr = sp.verify_receipt(steps[n0]["receipt"], p, sha)
    targets, stays = undo_targets(p, steps, n0, fr)
    pending = [k for k in targets if steps[k]["state"] != "undone"]
    base = run["error"] or f"step {n0} failed"
    tail = f"; {stays}" if stays else ""
    if not targets:
        await _finish(pool, run["id"], "failed", f"{base}; no step may be undone{tail}")
        return False
    if not pending:
        done = ", ".join(str(k) for k in targets)
        await _finish(pool, run["id"], "undone", f"{base}; step(s) {done} were taken back{tail}")
        return False
    k = pending[0]
    host = p["lines"][k - 1]["machine"]
    before = "failed" if k == n0 else "passed"
    if steps[k]["state"] == "undoing":
        return await _read_undo(pool, eid, run, p, sha, k, steps[k], base)
    if sp._parse_time(p["expires"]) <= _now():
        # A machine refuses a line of an expired plan, undo included, and the
        # refusal is a strike: stop, and say what was and was not taken back.
        done = [x for x in targets if steps[x]["state"] == "undone"]
        await _finish(pool, run["id"], "failed", f"{base}; the plan expired during the undo"
                      + (f" after step(s) {', '.join(map(str, done))} were taken back" if done else "")
                      + f"; step(s) {', '.join(map(str, pending))} were not taken back")
        return False
    if run["stop_requested"]:
        done = [x for x in targets if steps[x]["state"] == "undone"]
        await _finish(pool, run["id"], "stopped", f"{base}; the person stopped the undo"
                      + (f" after step(s) {', '.join(map(str, done))} were taken back" if done else " before any step was taken back")
                      + f"; step(s) {', '.join(map(str, pending))} were not")
        return False
    ok, reason, _d = await nac_mgmt.gate_op(pool, eid, host, "op_plan_undo")
    if not ok:
        await _finish(pool, run["id"], "failed", f"{base}; step {k} could not be undone: op_plan_undo on "
                                                 f"{host} is not permitted — {reason}")
        return False
    try:
        b = await resolve_bastion(pool, eid, host)
    except RunnerError as e:
        await _note(pool, run["id"], f"{base}; the undo of step {k} waits: {e.detail}")
        return False
    verbs = len(p["lines"][k - 1].get("verbs") or [])
    moved = await pool.fetchval(
        "UPDATE ship_steps SET state='undoing', sent_at=now(), stale_s=$4, last_seen=now() "
        " WHERE run_id=$1 AND n=$2 AND state=$3 RETURNING n", run["id"], k, before, undo_budget_s(verbs))
    if moved is None:
        return False  # another tick moved it first
    try:
        res = await nac_mgmt.run_mgmt_op(pool, eid, host, "op_plan_undo",
                                         [_b64(run["plan_envelope"]), str(k), _b64(json.dumps(steps[n0]["receipt"]))],
                                         token_id=b["token_id"], budget_s=DISPATCH_WAIT_S,
                                         note=f"ship {p['pipeline']} run {run['id']} undo step {k}")
    except nac_mgmt.MgmtError as e:
        if e.status == 504:
            return False  # the machine took it; its receipt is read back
        if e.status == 502:
            # Nothing picked the job up: it never reached the machine. Try again.
            await pool.execute("UPDATE ship_steps SET state=$3, sent_at=NULL, last_seen=now() "
                               " WHERE run_id=$1 AND n=$2 AND state='undoing'", run["id"], k, before)
            await _note(pool, run["id"], f"{base}; the undo of step {k} waits: {e.detail}")
            return False
        await _finish(pool, run["id"], "failed", f"{base}; step {k} could not be undone: {e.detail}")
        return False
    if res.get("exit") != 0:
        err = (res.get("stderr") or "").strip()
        if err.startswith("relay error"):
            return False
        # Refused: the machine may hold this undo already (a send whose answer
        # was lost). Its own record says; otherwise the refusal stands.
        return await _read_undo(pool, eid, run, p, sha, k, {**steps[k], "sent_at": _now()}, base,
                                refusal=f"{host} would not undo step {k}: {err[:500]}")
    try:
        body = json.loads(res.get("stdout") or "{}")
        r = sp.verify_receipt(body["receipt"], p, sha)
    except (ValueError, KeyError, TypeError, sp.PlanError) as e:
        await _finish(pool, run["id"], "failed", f"{base}; step {k}'s undo came back without a receipt that "
                                                 f"verifies ({e})")
        return False
    return await _take_undo(pool, run, k, r, body["receipt"], base)


async def _read_undo(pool, eid: int, run: dict, p: dict, sha: str, k: int, step: dict, base: str,
                     refusal: str | None = None) -> bool:
    host = p["lines"][k - 1]["machine"]
    try:
        got = await _read_status(pool, eid, host, p, sha, k)
    except RunnerError as e:
        if refusal or e.status == 403 or "does not verify" in e.detail \
                or _stale(step["sent_at"], step.get("stale_s")):
            await _finish(pool, run["id"], "failed", f"{base}; {refusal or ('the undo of step ' + str(k) + ' could not be read back from ' + host + ': ' + e.detail)}")
        else:
            await _note(pool, run["id"], f"{base}; the undo of step {k} is being read back: {e.detail}")
        return False
    undos = [(r, s) for r, s in got if r.get("undo") and r["n"] == k]
    if undos:
        return await _take_undo(pool, run, k, undos[-1][0], undos[-1][1], base)
    if refusal:
        await _finish(pool, run["id"], "failed", f"{base}; {refusal}")
    elif _stale(step["sent_at"], step.get("stale_s")):
        await _finish(pool, run["id"], "failed", f"{base}; no receipt of step {k}'s undo came back from {host}")
    return False


async def _take_undo(pool, run: dict, k: int, r: dict, signed, base: str) -> bool:
    if not r.get("undo") or r["n"] != k:
        await _finish(pool, run["id"], "failed", f"{base}; step {k}'s undo was answered with another receipt")
        return False
    if not r.get("ok"):
        await pool.execute("UPDATE ship_steps SET undo_receipt=$3::jsonb, error=$4, last_seen=now() "
                           " WHERE run_id=$1 AND n=$2", run["id"], k, json.dumps(signed),
                           (r.get("error") or "")[:1000] or None)
        await _finish(pool, run["id"], "failed", f"{base}; the undo of step {k} failed: {r.get('error') or '?'}")
        return False
    await pool.execute("UPDATE ship_steps SET state='undone', undo_receipt=$3::jsonb, last_seen=now() "
                       " WHERE run_id=$1 AND n=$2", run["id"], k, json.dumps(signed))
    return True


# ── the Host, for `ask` ─────────────────────────────────────────────────────

async def _schedule(pool, eid: int, type_row, title: str, prompt: str) -> dict:
    """A wake-up under the pipeline's turn type, through schedule_task itself
    (the plan is written into the type's thread first, and the runner checks
    the type again at fire time). The seam the tests replace."""
    from mastercode.runtime import tools as _tools
    from mastercode.runtime.turn_types import load_turn_type_by_id
    tt = await load_turn_type_by_id(pool, eid, str(type_row["id"]))
    fire = (_now() + timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return await _tools._exec_schedule_task({"op": "create", "title": title, "fire_at": fire, "prompt": prompt},
                                            turn_type=tt,
                                            thread_id=str(type_row["thread_id"]) if type_row["thread_id"] else None)


def _type_can_propose(turn_type: dict | None) -> bool:
    """True when a turn under `turn_type` may call gitea op='propose': the
    type's four checks admit the call (turn_types.check_call: its groups, or
    the root allowlist) and its grant holds the bits gitea needs. The
    person's write:gitea scope is checked by the executor itself."""
    from mastercode.runtime.agent_grant import required_bits_for
    from mastercode.runtime.turn_types import check_call, grant_bits_for
    if turn_type is None:
        return True
    if check_call(turn_type, "gitea", {"op": "propose", "repo": "owner/repo"}) is not None:
        return False
    bits = grant_bits_for(turn_type)
    need = required_bits_for("gitea") or 0
    return bits is None or (int(bits) & int(need)) == int(need)


def wake_prompt(run: dict, why: str, turn_type: dict | None) -> str:
    """What the Host is woken with when a run stops for it. A change to a
    repo it does not own is a pull request through gitea op='propose'; the
    prompt names that call only when the ship turn's type can make it. A
    pipeline names a turn type the person wrote, and one without gitea's
    group would be told to call a tool its turn refuses."""
    head = (f"Ship run {run['id']} of pipeline {run['spec']['name']} at commit {run['commit_sha'][:12]} "
            f"stopped for you: {why}. Read the run in the ships card and the Ships app, find the cause, and "
            f"make the fix yourself, never through an agent: a pipeline file or code in your own "
            f"project you change in that project; ")
    if _type_can_propose(turn_type):
        rest = ("a change to a repo you do not own (such as the platform source) is a pull "
                "request you open with gitea op='propose'.")
    else:
        rest = (f"a change to a repo you do not own (such as the platform source) is a pull "
                f"request, and this turn's type {str((turn_type or {}).get('name') or '')!r} cannot "
                f"open one: write in this thread which change is needed and where, so it is opened "
                f"from a turn that holds the forge.")
    return head + rest + " The run does not go on by itself."


async def _wake(pool, eid: int, run: dict, why: str) -> None:
    name = (run["spec"].get("turn_type") or "").strip()
    if not name:
        await _note(pool, run["id"], f"{why}. The pipeline names no turn type, so the Host reads this on its "
                                     f"next turn (the ships card)")
        return
    row = await pool.fetchrow("SELECT id, thread_id FROM turn_types WHERE entity_id=$1 AND name=$2 "
                              " AND status='active'", int(eid), name)
    if row is None:
        await _note(pool, run["id"], f"{why}. No active turn type named '{name}' to wake; the Host reads this "
                                     f"on its next turn (the ships card)")
        return
    try:
        from mastercode.runtime.turn_types import load_turn_type_by_id
        tt = await load_turn_type_by_id(pool, eid, str(row["id"]))
    except Exception as e:  # noqa: BLE001 - an unread type names no call it may not hold
        from mastercode.runtime.turn_types import closed_type
        tt = closed_type(f"the ship turn's type could not be read ({e})")
    prompt = wake_prompt(run, why, tt)
    try:
        res = await _schedule(pool, eid, row, f"Ship {run['spec']['name']} run {run['id']} needs you", prompt)
    except Exception as e:  # noqa: BLE001
        res = {"status": "error", "result": repr(e)}
    if isinstance(res, dict) and res.get("status") == "error":
        await _note(pool, run["id"], f"{why}. Waking the Host failed ({str(res.get('result'))[:300]}); it reads "
                                     f"this on its next turn")
        return
    await pool.execute("UPDATE ship_runs SET host_woken=true, last_seen=now() WHERE id=$1", run["id"])


# ── the person ──────────────────────────────────────────────────────────────

async def stop_run(pool, rid: int) -> dict:
    """Stop: no further line is sent. A line already on a machine runs to its
    end there and its receipt is still read back; an unapproved run stops at
    once (a later tap on the device is ignored)."""
    run = await _run(pool, rid)
    if run is None:
        raise RunnerError(404, "no such run")
    if run["state"] not in ACTIVE_STATES:
        raise RunnerError(409, f"the run is {run['state']} already")
    await pool.execute("UPDATE ship_runs SET stop_requested=true, last_seen=now() WHERE id=$1", int(rid))
    if run["state"] in ("planned", "awaiting_tap"):
        # Only from those states: a tap that moved it to running in between
        # wins, and the flag stops it before its first line instead.
        await _finish(pool, rid, "stopped", "stopped by the person before it was approved", refused=True,
                      from_states=("planned", "awaiting_tap"))
    return await run_view(pool, rid)


# ── what the app and the card read ──────────────────────────────────────────

def _receipt_view(signed) -> dict | None:
    """The fields of a receipt a person reads. Every stored receipt verified
    against the signed plan before it was stored."""
    if not signed:
        return None
    try:
        r = json.loads(base64.b64decode(signed["payload"]))
    except (ValueError, KeyError, TypeError):
        return None
    return {k: r.get(k) for k in ("at", "attempt", "ok", "undo", "error", "verify_error", "images", "verbs",
                                  "checks", "machine", "op_sha256")}


async def run_view(pool, rid: int) -> dict:
    run = await _run(pool, rid)
    if run is None:
        raise RunnerError(404, "no such run")
    steps = await _steps(pool, rid)
    draft = run["plan_draft"] or {}
    lines = {ln["n"]: ln for ln in draft.get("lines") or []}
    rules = {i + 1: s.get("on_fail") for i, s in enumerate(run["spec"].get("steps") or [])}
    return {
        "id": run["id"], "pipeline": run["spec"]["name"], "commit": run["commit_sha"], "trigger": run["trigger"],
        "started_by": run["started_by"], "state": run["state"], "step": run["step"], "error": run["error"],
        "needs_host": run["needs_host"], "host_woken": run["host_woken"], "stop_requested": run["stop_requested"],
        "device": {k: (run["device_ask"] or {}).get(k) for k in ("hostname", "id")},
        "approved": bool(run["plan_envelope"]), "plan_sha256": run["plan_sha256"],
        "expires": run["plan_expires"].isoformat() if run["plan_expires"] else None,
        "started": run["first_seen"].isoformat() if run["first_seen"] else None,
        "finished": run["finished_at"].isoformat() if run["finished_at"] else None,
        "steps": [{
            "n": n, "step": s["step"], "machine": s["machine"], "state": s["state"], "attempts": s["attempts"],
            "op": lines.get(n, {}).get("op"), "root": bool(lines.get(n, {}).get("verbs")),
            "verbs": lines.get(n, {}).get("verbs") or [], "inputs": lines.get(n, {}).get("inputs") or {},
            "on_fail": rules.get(n), "error": s["error"],
            "sent_at": s["sent_at"].isoformat() if s["sent_at"] else None,
            "receipt": _receipt_view(s["receipt"]), "undo_receipt": _receipt_view(s["undo_receipt"]),
        } for n, s in sorted(steps.items())],
    }


async def recent_runs(pool, limit: int = 20, before: int | None = None) -> list:
    """Newest first; `before` (a run id) pages older — runs with a smaller id."""
    if before is not None:
        rows = await pool.fetch("SELECT id FROM ship_runs WHERE id < $2 ORDER BY id DESC LIMIT $1",
                                int(limit), int(before))
    else:
        rows = await pool.fetch("SELECT id FROM ship_runs ORDER BY id DESC LIMIT $1", int(limit))
    return [await run_view(pool, r["id"]) for r in rows or []]


# ── the loop ────────────────────────────────────────────────────────────────

async def run_ships_loop(pool, eid: int) -> None:
    """The pod's runner: every TICK_S, move every active run as far as it goes.
    Started once per pod in the lifespan, like the other standing loops."""
    while True:
        try:
            await tick(pool, eid)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("ships: tick failed")
        await asyncio.sleep(TICK_S)
