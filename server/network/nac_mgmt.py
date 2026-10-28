"""
NAC management lane (P8) — the entity OPERATES the servers in its mesh.

TACACS+ for the entity: per-command authorization + command accounting on every server, with
the decision made LOCALLY at each box (the bastion), not centrally. The transport is the egress
reverse-relay flipped to a new job type — the pod can't reach a bastion's tunnel IP, so the
bastion opens worker WebSockets TO the host on :8443 (/bastion-mgmt) and pulls COMMAND jobs over
them; the bastion runs the op on its OWN host and relays stdout/stderr/exit back. The bastion is
the AUTHORITATIVE wall (it checks its own allowlist before running), so even a compromised pod
cannot make a server run an ungranted op.

Posture is the INVERSE of egress: egress is default-OPEN (the user's sovereignty to reach out,
never logged); management is default-DENY and ALWAYS audited (the entity acting ON the user's
machine). Different posture + different audit ⇒ a separate lane, never egress's.

Frames (binary):
  pod -> bastion : C + json({"op": str, "args": [str]})
  bastion -> pod : O + stdout, R + stderr, X + exit_code   # X is terminal

TRUST BOUNDARY (honest, as built): the POD gate (load_mgmt_grants + mgmt_decision) is the ONLY
place the user's signed grant is verified. The command frame the pod sends the bastion carries
ONLY {op, args} — NOT the signed grant terms/signature (see MgmtWorker._run). So the bastion does
NOT independently re-verify the user's grant on its side; it runs what the pod-gate already
authorized, over its own bearer-authed reverse worker. A bastion-side re-verification of the
signed grant (sending grant_terms/grant_sig on the wire, the "P8.1b" idea) is NOT implemented. If
that transport-integrity property is wanted later, _run must send the terms+sig and the bastion
must verify them before exec — do not assume it exists today.
"""
import asyncio
import base64
import json
import logging
from typing import Optional

# Own visible logger (uvicorn leaves the app logger at WARNING root; mirror egress's handler).
log = logging.getLogger("cseries.mgmt")
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s mgmt %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)
    log.propagate = False


# ── P8.1 signed grants (replaces the P8.0 hardcoded allowlist) ────────────────
# A grant is the USER's step-up-authorized, signed authorization for (entity, server, ops,
# expiry). The host-gate verifies it; DEFAULT-DENY. The entity signing key lives in the pod
# (kernel_secrets), so the TOTP step-up is what stops an AGENT OUTSIDE THIS POD from
# self-granting (it can't produce the user's code) — the same doctrine as nac_stepup/egress.
# It does NOT stop the entity's own workload, which reads both secrets off the trust-auth
# socket; that limit is open (see nac_stepup's docstring).
# NB: the POD gate is the sole verifier of the signed grant. The bastion does NOT re-verify it
# (the command frame carries only {op, args}, not the grant terms/sig — see the module docstring's
# TRUST BOUNDARY). A bastion-side re-verification ("P8.1b") is unimplemented; don't rely on it.

# The op id that terminates a detected agent on a box (NAC agent, 2026-08-29). A
# grant must list it like any other op; it never reaches the box's shell allowlist
# — run_mgmt_op turns it into a heartbeat directive the box applies to agents only.
CONTAIN_OP = "contain"

# ── HOW LONG THE HOST WAITS FOR ONE OP, and why it is per-op ─────────────────
#
# Every management op used to be dispatched with a flat 30 s, which is right for
# all but one of them and wrong for that one in the worst possible way.
# model:pull downloads a model onto the user's disk and is bounded on the BOX at
# 20 minutes (the gateway client's modelswap.go `modelPullTimeout`)
# plus its behavioural probe. At 30 s the host gave up, told the user "no live
# management worker" — a false statement about a worker that was connected and
# working — and the box finished the download anyway, burning the user's
# single-use machine-side approval on a pull this host had recorded as failed.
# The user then had to walk back to the machine and approve a SECOND time for a
# model already sitting on their disk.
#
# WHY A TABLE AND NOT A BLANKET RAISE. A bastion runs mgmtWorkerCount = 2
# management workers (mgmt.go:18), so a budget is a licence to park half the
# lane; handing 26 minutes to every op means any wedged op parks it. The raise
# goes to exactly the op whose box-side bound requires it, and every other op
# keeps the default it was already right for. Derived per op from the box's OWN
# constants, all read 2026-09-13:
#
#   p8 exec ops (uptime, df, ...)     mgmt.go:19         mgmtCmdTimeout     25 s
#   computer:inventory|windows|tree   computer.go:76     computerCmdTimeout 20 s
#   observe:* (net probe 5 s, :694)   observe_ops.go:54  observeOpBudget    20 s
#   ctx_put | ctx_get | ctx_del       no exec — file IO under the sandboxed root
#   backup_fetch                      backupfetch.go:24-28 — start AND status
#                                     both return at once; the download is async
#   dcn_*                             bounded reads (dcn.go:39-40 — 512 KiB,
#                                     1000 entries); dcn_plan_apply is a local move
#   stepup_request | stepup_status    file IO
#   pairing_*                         file IO
#   model:list | model:drop           modelswap.go:216 modelListTimeout   10 s
#   model:pull                        modelswap.go:217 modelPullTimeout  20 min
#                                     + modelswap.go:256 modelProbeTimeout 2 min,
#                                     and probeEmbedModel makes THREE probe calls
#                                     (modelswap.go:485-501) — 26 minutes of box.
#   contain                           never reaches dispatch at all: run_mgmt_op
#                                     turns it into a heartbeat directive.
#   ctx_extract                       NOT dispatched from here. Its caller sizes
#                                     its own wait from the budget it granted on
#                                     the wire (server/routes/context_extract.py).
#
# THE PULL IS NOW ASYNC, and the 1590 s below is a COMPATIBILITY CEILING rather
# than a wait anyone expects to serve. The gateway's modelswap.go took the shape
# backup_fetch already had: `model:pull {"op":"start"}` validates, checks the
# user's machine-side approval, spawns the download and answers at once with a
# job id; `model:pull {"op":"status","job_id":...}` reports progress. Both ride
# the SAME op id, because a signed grant names op ids exactly (mgmt_decision
# below) and a separate `model:status` would be ungranted in every grant already
# written — a user whose grant says model:pull could start a download and then
# not be allowed to watch it finish.
#
# So why keep 1590 s at all. The bastion is published at 0.24.0 and 0.24.0 has
# the SYNCHRONOUS handler; it ignores the "op" field it does not know and does
# the whole download on the frame. This host must not break against it. A start
# therefore keeps the ceiling and an old box still answers inside it; a new box
# returns in milliseconds and never approaches it. A STATUS keeps the 30 s
# default, because on any box that understands it, it returns at once — which is
# the whole point, and is what stops a poll parking one of TWO management
# workers (mgmtWorkerCount, mgmt.go:18) the way the download used to.
#
# The ordering against an OLD box is still an agreement between two numbers, a
# weaker shape than the extraction lane's grant-on-the-wire where the box clamps
# DOWN to one number. Against a NEW box there is no ordering to get wrong,
# because nothing waits. The constants below name the box lines they are derived
# from, and a change that moves one has to move this in the same commit.
MGMT_DISPATCH_TIMEOUT_S = 30.0

# The most of ONE command frame the box reads (client-go/mgmt.go mgmtFrameMaxBytes,
# its SetReadLimit): a larger frame is dropped by the box unread, so a job whose
# frame is larger can never run. job_frame is the frame exactly as MgmtWorker._run
# sends it, so a caller can measure a job before it is filed.
MGMT_FRAME_MAX_BYTES = 12 << 20


def job_frame(op: str, args: list, approval: str = "") -> bytes:
    """The command frame for one job: C + json({"op", "args"[, "approval"]})."""
    job = {"op": op, "args": args}
    if approval:
        job["approval"] = approval
    return b"C" + json.dumps(job).encode()

# The box's own bounds for a pull, named so a drift is a visible edit rather
# than a silent re-break. If any of these move in the gateway, move them here
# in the same change.
MODEL_PULL_BOX_TIMEOUT_S = 20 * 60.0      # modelswap.go:217 modelPullTimeout
MODEL_PROBE_BOX_TIMEOUT_S = 2 * 60.0      # modelswap.go:256 modelProbeTimeout
MODEL_PROBE_CALLS = 3                     # modelswap.go:485-501, the embed probe
MGMT_REPLY_ROUND_TRIP_S = 30.0            # the frame back, on a home connection

MODEL_PULL_OP = "model:pull"
MODEL_LIST_OP = "model:list"

# The sub-ops model:pull carries, matching the gateway's modelswap.go dispatch.
# An absent "op" means start, which is how backup_fetch spells the same choice
# (backupfetch.go:139-146).
MODEL_PULL_START = "start"
MODEL_PULL_STATUS = "status"

# An op's sandbox (nac_ops, ops_exec_linux.go): the manifest's time_limit is
# at most 10 minutes; an apply runs the sandbox twice (the run, then verify)
# and commits between them.
OP_TIME_LIMIT_MAX_S = 10 * 60.0

MGMT_OP_BUDGET_S = {
    MODEL_PULL_OP: (MODEL_PULL_BOX_TIMEOUT_S
                    + MODEL_PROBE_CALLS * MODEL_PROBE_BOX_TIMEOUT_S
                    + MGMT_REPLY_ROUND_TRIP_S),
    "op_dry_run": OP_TIME_LIMIT_MAX_S + MGMT_DISPATCH_TIMEOUT_S,
    "op_apply": 2 * OP_TIME_LIMIT_MAX_S + 2 * MGMT_DISPATCH_TIMEOUT_S,
    "op_grant_run": 2 * OP_TIME_LIMIT_MAX_S + 2 * MGMT_DISPATCH_TIMEOUT_S,
}


def model_pull_sub_op(args) -> str:
    """Which sub-op a model:pull argument asks for: 'start' (also the default for a
    caller that names none) or whatever it says. Unparsable args are 'start', because
    the box will refuse them and a refusal is short."""
    try:
        req = json.loads(str(args[0])) if args else {}
    except Exception:
        return MODEL_PULL_START
    if not isinstance(req, dict):
        return MODEL_PULL_START
    return str(req.get("op") or MODEL_PULL_START).strip() or MODEL_PULL_START


def mgmt_op_budget(op: str, args=None) -> float:
    """How long this host waits for ONE dispatch of `op`. The default is right for
    every op whose box-side bound sits under it; the table above is the derivation.

    `args` matters for exactly one op. model:pull carries two sub-ops on one op id,
    and they have opposite shapes: a START may still be answered synchronously by a
    published-0.24.0 box and needs the ceiling, while a STATUS returns at once on any
    box that understands it and must not be allowed to park a worker for 26 minutes
    if it is sent to one that does not."""
    op = (op or "").strip()
    if op == MODEL_PULL_OP and model_pull_sub_op(args) != MODEL_PULL_START:
        return MGMT_DISPATCH_TIMEOUT_S
    return MGMT_OP_BUDGET_S.get(op, MGMT_DISPATCH_TIMEOUT_S)


# THE TWO WAYS A DISPATCH PRODUCES NO RESULT are different facts about the
# machine and must never be reported as one. They were collapsed into "no live
# management worker", which is a diagnosis pointing at the wrong component: the
# worker was connected, it had the job, and it was working.
DISPATCH_NO_WORKER = "no-worker"   # nothing ever picked the job up
DISPATCH_NO_REPLY = "no-reply"     # a worker TOOK the job and did not answer

# Plain-text grants: "I want you to do X, I do NOT want you to
# do Y" becomes ONE signed grant with an allow list (`ops`) AND a deny list (`deny`), plus the
# user's own words (`intent` = {want, do_not_want}) so the approval card reads their sentence
# back instead of tokens. DENY BEATS ALLOW: the gate consults every approved grant's deny
# list for the server before any allow, so a broad allow in one grant can never outrun a
# deny in another (a tighten composing across grants is the safe direction; allows still do
# not compose — see harness_image_allowed). A deny token is an op id, an allowlist token
# ("image:<ref>", "mode:lab"), or DENY_ALL. Tokens outside the op-id space (a future
# "app:<name>" / "window:<title>" / "path:<prefix>") are carried and signed like any other
# but only a gate that understands them can enforce them — today's gates match op ids.
DENY_ALL = "*"

# nac_mgmt_grants is declared in migrations/v060_nac_mgmt.sql:17, and its `deny`
# and `intent` columns in migrations/v125_mgmt_grant_deny_intent.sql. This module
# used to mirror both defensively on every grant read; that is gone, so
# the schema arrives at boot like every other. Reads and writes are unchanged.


# ── Per-bastion SUSPEND (temporary, distinct from revoke) ─────────────────────
# A suspend is a TIGHTEN with no step-up: it short-circuits the gate so a server
# denies even with valid signed grants — without touching those grants. Resuming
# (a WIDEN, needs step-up) just deletes the row and the standing grants take effect
# again. Revoke, by contrast, tears the grant down for good. Suspend is the "pause
# this box right now" lever; the grants are the durable authorization.
# nac_bastion_suspend had no declaration anywhere until
# migrations/v144_band_b_network_nac.sql — it existed on five of eight hosts,
# created lazily by whoever first suspended a box. That is what v144 fixes.


async def load_suspended_servers(pool) -> set:
    """The set of currently-suspended server hostnames. A suspended server denies at
    the gate regardless of its grants (the safe direction — a tighten always holds)."""
    try:
        rows = await pool.fetch("SELECT server FROM nac_bastion_suspend")
    except Exception as e:
        log.info("load_suspended_servers unavailable: %r", e)
        return set()
    return {r["server"] for r in rows}


# ── C1: REVERSIBLE box-level QUARANTINE (whole-mesh, distinct from suspend) ────
# A quarantine cuts a bastion's ENTIRE WireGuard mesh reach — not just the mgmt +
# harness lanes (that's suspend) — by dropping its concentrator peer, while leaving
# the bearer in mcp_client_tokens intact so a release brings the box back with no
# re-enrol. The gate treats a quarantined server exactly like a suspended one (an
# unconditional deny, before any grant is consulted); the peer-drop additionally
# kills egress/inbound at the network level. Quarantine is a TIGHTEN (no step-up);
# release is a WIDEN (step-up), the same posture pair as suspend/resume.
# nac_bastion_quarantine is declared in
# migrations/v069_nac_bastion_quarantine.sql:25; there is no runtime mirror.


async def load_quarantined(pool) -> set:
    """The set of currently-quarantined server hostnames. A quarantined server denies
    at the gate unconditionally — like suspend, the safe direction (a tighten always
    holds), and on top of that its concentrator peer is dropped so the whole tunnel is
    dead. Releasing (a WIDEN) deletes the row and restores the peer."""
    try:
        rows = await pool.fetch("SELECT server FROM nac_bastion_quarantine")
    except Exception as e:
        log.info("load_quarantined unavailable: %r", e)
        return set()
    return {r["server"] for r in rows}


def _verify_grant(pubkey, terms: dict, sig_b64: str) -> bool:
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from server.network.nac_stepup import _canonical
        pub = pubkey if isinstance(pubkey, Ed25519PublicKey) else Ed25519PublicKey.from_public_bytes(pubkey)
        pub.verify(base64.b64decode(sig_b64), _canonical(terms))
        return True
    except Exception:
        return False


def _grant_expired(iso) -> bool:
    if not iso:
        return False  # no expiry set = does not expire
    try:
        import datetime as _dt
        return _dt.datetime.fromisoformat(iso) <= _dt.datetime.now(_dt.timezone.utc)
    except Exception:
        return True  # unparseable expiry = treat as expired (deny)


async def load_mgmt_grants(pool, verify_pubkey=None) -> list:
    """Approved + non-revoked + non-expired grants, signature-checked. DEFAULT-DENY: when the
    gate is ARMED (verify_pubkey present), an unsigned or bad-signature grant is SKIPPED — and
    because management defaults to DENY, a skipped grant authorizes nothing (the safe direction;
    a forged WIDEN can never take effect, the mirror of egress's forged-tighten).

    FAIL-CLOSED on a missing verify key. This is the INVERSE of egress: egress defaults OPEN, so an
    absent key falls back to the stored columns (skipping can only widen). Management defaults DENY,
    so trusting unsigned columns when the key won't load is a fail-OPEN downgrade — a direct DB write
    of an unsigned grant would authorize an op the user never signed. So `verify_pubkey is None` here
    returns [] (deny all): no grants → nothing is permitted, which is the SOLE wall today. A
    belt-and-suspenders 503 in the callers (network.mgmt_run / harness_run) when the key can't load
    is a recommended follow-up but is NOT yet implemented."""
    if verify_pubkey is None:
        # Default-DENY lane with no armed verify key: trusting the unsigned stored columns would be
        # a fail-open downgrade. Authorize nothing until the key loads.
        log.info("load_mgmt_grants: no verify key — default-deny, authorizing no grants")
        return []
    try:
        rows = await pool.fetch(
            "SELECT id, server, ops, signed_terms, signature, expires_at FROM nac_mgmt_grants "
            "WHERE approved_at IS NOT NULL AND revoked_at IS NULL "
            "AND (expires_at IS NULL OR expires_at > now()) ORDER BY id")
    except Exception as e:
        log.info("load_mgmt_grants unavailable: %r", e)
        return []
    out = []
    for row in rows:
        st, sig = row["signed_terms"], row["signature"]
        if st is None or sig is None:
            continue  # unsigned approved grant under an armed gate — not user-authorized
        terms = st if isinstance(st, dict) else json.loads(st)
        if not _verify_grant(verify_pubkey, terms, sig):
            continue  # tampered / forged — skip (deny)
        if _grant_expired(terms.get("expires")):
            continue  # signed expiry is authoritative — a mutated column can't extend it
        server, ops = terms.get("server", row["server"]), terms.get("ops", [])
        # deny + intent come from the SIGNED terms only — a grant signed before v125 has
        # neither, and the stored columns are never trusted over the signature.
        intent = terms.get("intent")
        out.append({"server": server, "ops": list(ops or []),
                    "deny": list(terms.get("deny") or []),
                    "intent": intent if isinstance(intent, dict) else None})
    return out


def _covers(g: dict, server: str) -> bool:
    gs = g.get("server")
    return gs == "*" or gs == server


def mgmt_denied_by(grants: list, server: str, token: str) -> Optional[dict]:
    """The first verified grant whose DENY list forbids `token` on `server`, or None. A deny
    matches the token exactly or is DENY_ALL. Consulted before any allow — deny beats allow —
    and across ALL grants covering the server (a tighten composing is the safe direction). The
    returned grant carries the user's `intent`, so a refusal can quote their own words."""
    for g in grants:
        if not _covers(g, server):
            continue
        deny = g.get("deny") or []
        if token in deny or DENY_ALL in deny:
            return g
    return None


def mgmt_decision(grants: list, entity_id: int, server: str, op_id: str,
                  suspended: Optional[set] = None,
                  quarantined: Optional[set] = None) -> bool:
    """DEFAULT-DENY — the inverse of egress_decision. Allow ONLY if a verified grant covers this
    (server, op) AND no verified grant covering the server DENIES it (deny beats allow). A
    grant's server is a hostname or '*' (every server in the mesh).

    A SUSPENDED or QUARANTINED server denies unconditionally, before any grant is consulted —
    both are tightens that override standing grants without deleting them (resume/release
    restores them). '*' grants don't rescue such a server: the hold is on the concrete `server`,
    so the short-circuit fires regardless of how broad the grant is. (Quarantine additionally
    drops the concentrator peer, so the box is also unreachable at the network level — this
    gate-deny is belt-and-braces for the mgmt lane.)"""
    if suspended and server in suspended:
        return False
    if quarantined and server in quarantined:
        return False
    if mgmt_denied_by(grants, server, op_id) is not None:
        return False
    for g in grants:
        if _covers(g, server) and op_id in (g.get("ops") or []):
            return True
    return False


# ── H4 harness-run gate: signed grant + image allowlist ───────────────────────
# A harness dispatch (H lane) reads as a REQUESTED agent — same default-DENY posture as mgmt,
# PLUS an image allowlist: the grant enumerates which container image(s) may run on that server.
# The allowlist rides INSIDE the same signed `ops` (so it's tamper-evident for free, no new
# column): a harness grant lists the op token HARNESS_RUN_OP *and* one image token per permitted
# image — "image:<ref>", or "image:*" for any image. A dispatch that names no image is requesting
# the bastion's built-in default container; that maps to the sentinel HARNESS_DEFAULT_IMAGE_TOKEN
# so it can't slip past the allowlist by omission (default-deny holds for the empty case too).
HARNESS_RUN_OP = "harness:run"
HARNESS_DEFAULT_IMAGE_TOKEN = "image:default"
# The Lab Harness: a mode=lab dispatch builds a sealed lab of
# hosts on a LAB bastion and is billed at its own disclosed rate. It needs the same grant to
# ALSO carry this token — so a grant for a home box never covers a lab run, and a lab grant
# never covers a home box (wall #2 of "spun up in the lab, nowhere else").
LAB_MODE_TOKEN = "mode:lab"
# The SERVICE SLOT (bastion 0.34.0 service.go): mode=service starts, stops or lists a
# long-running server the host built on the user's machine. A grant to run jobs there is
# not a grant to leave a server running, so the same grant must ALSO carry this token —
# and, like the lab token, 'image:*' does not imply it.
SERVICE_MODE_TOKEN = "mode:service"


def _mode_token(mode) -> str | None:
    """The extra allowlist token a dispatch MODE requires, or None for the ordinary modes."""
    m = (mode or "").strip().lower()
    if m == "lab":
        return LAB_MODE_TOKEN
    if m == "service":
        return SERVICE_MODE_TOKEN
    return None


def _needs_image(image, mode) -> bool:
    """Whether a dispatch is gated on an image token. Every mode is, EXCEPT a service
    verb that names no image — `down` and `status` act on a slot, not on an image, so
    they need the run op and the service token and nothing that would be a fake image."""
    return not ((mode or "").strip().lower() == "service" and not (image or "").strip())


def _image_token(image) -> str:
    """The allowlist token a requested image must match. An omitted/blank image (the bastion's
    built-in default container) maps to the explicit default sentinel — never a free pass."""
    image = (image or "").strip()
    return ("image:" + image) if image else HARNESS_DEFAULT_IMAGE_TOKEN


def harness_denied_by(grants: list, server: str, image, mode: Optional[str] = None) -> Optional[dict]:
    """The grant whose deny list forbids this harness dispatch — harness:run itself, the
    requested image's token, or the mode token — or None."""
    tokens = [HARNESS_RUN_OP]
    if _needs_image(image, mode):
        tokens.append(_image_token(image))
    need_mode = _mode_token(mode)
    if need_mode:
        tokens.append(need_mode)
    for t in tokens:
        g = mgmt_denied_by(grants, server, t)
        if g is not None:
            return g
    return None


def harness_image_allowed(grants: list, entity_id: int, server: str, image,
                          suspended: Optional[set] = None,
                          quarantined: Optional[set] = None,
                          mode: Optional[str] = None) -> bool:
    """DEFAULT-DENY harness gate. Allow ONLY if a SINGLE verified grant covers this server AND
    authorizes BOTH the harness-run op and this image. Both tokens must come from the SAME grant
    (you can't compose 'may run a harness here' from one grant with 'this image is fine' from
    another). 'image:*' in a grant's ops authorizes any image, including the default container.

    mode='lab' (the Lab Harness) additionally requires LAB_MODE_TOKEN in that SAME grant — the
    ordinary modes ignore it, and 'image:*' does not imply it (a wildcard image is not a
    licence to build a lab, which is billed at its own rate).

    A SUSPENDED or QUARANTINED server denies unconditionally, before any grant is consulted —
    the same short-circuit as mgmt_decision, so both pause harness dispatch too (quarantine
    also drops the concentrator peer, so the box is unreachable network-wide).

    DENY BEATS ALLOW here too: a deny of harness:run, of this image's token, or of the mode
    token in ANY grant covering the server refuses the dispatch (see mgmt_denied_by)."""
    if suspended and server in suspended:
        return False
    if quarantined and server in quarantined:
        return False
    want = _image_token(image)
    need_mode = _mode_token(mode)
    if harness_denied_by(grants, server, image, mode) is not None:
        return False
    for g in grants:
        gs = g.get("server")
        if gs != "*" and gs != server:
            continue
        ops = g.get("ops") or []
        if HARNESS_RUN_OP not in ops:
            continue
        if need_mode and need_mode not in ops:
            continue
        if not _needs_image(image, mode) or "image:*" in ops or want in ops:
            return True
    return False


class MgmtWorker:
    """One bastion->host worker WebSocket = one management job. The endpoint coroutine owns all
    WS I/O; dispatch hands it (op, args, result) and awaits the collected (stdout, stderr, exit)."""

    def __init__(self, ws, bastion_token_id: int):
        self.ws = ws
        self.bastion_token_id = bastion_token_id
        self._job: "asyncio.Future" = asyncio.get_event_loop().create_future()

    def assign(self, job) -> bool:
        if self._job.done():
            return False
        self._job.set_result(job)
        return True

    async def serve(self, relay: "MgmtRelay"):
        """Endpoint-owned: register idle, await one job, run it, return (the WS then closes).
        An idle worker also keeps a receive() pending so a bastion disconnect is noticed."""
        relay._register_idle(self)
        recv = asyncio.ensure_future(self.ws.receive())
        started = False
        try:
            await asyncio.wait({recv, self._job}, return_when=asyncio.FIRST_COMPLETED)
            if not self._job.done():
                # Idle bastion dropped: cancel our slot so a dispatch that already pulled us
                # off the idle queue gets a failed assign() and moves to the next worker.
                self._job.cancel()
                log.info("mgmt worker %s dropped while idle", self.bastion_token_id)
                return
            recv.cancel()
            try:
                await recv
            except asyncio.CancelledError:
                # recv's own cancel, asked for just above. A Stop of this worker that lands
                # while it waits for recv arrives as the same exception (Task.cancel hands
                # it to the task being awaited); only this task's count of pending cancels
                # tells the two apart. Dropped, the worker ran the op anyway.
                if asyncio.current_task().cancelling():
                    raise
            except Exception:
                pass
            job = self._job.result()
            if job is None:
                return
            # (op, args, result) or, since 0.33.0, (op, args, result, approval).
            op, args, result = job[0], job[1], job[2]
            approval = job[3] if len(job) > 3 else ""
            started = True
            try:
                await self._run(op, args, result, approval)
            except Exception as e:
                log.info("mgmt worker %s run error: %r", self.bastion_token_id, e)
                if not result.done():
                    result.set_result((b"", f"relay error: {e!r}".encode(), 1))
        except asyncio.CancelledError:
            # A Stop. No job yet: close the slot the way a drop while idle does. An
            # op not yet started never reached the box, so it is answered None, which
            # dispatch_detailed reports as no worker. Not as a relay error: ships/runner.py
            # reads that as an op that runs on and waits for its receipt. Nor left to the
            # budget, which reports "no reply" and says the same.
            if not self._job.done():
                self._job.cancel()
            elif not started and not self._job.cancelled():
                job = self._job.result()
                if job is not None and not job[2].done():
                    log.info("mgmt worker %s stopped before its op", self.bastion_token_id)
                    job[2].set_result(None)
            return
        finally:
            if not recv.done():
                recv.cancel()

    async def _run(self, op: str, args: list, result: "asyncio.Future", approval: str = ""):
        # `approval` (bastion 0.33.0, mgmt.go mgmtJob.Approval): the person's
        # approval for THIS run of a write on an anchored box. Carried only when
        # there is one, so the frame an older box reads is unchanged.
        await self.ws.send_bytes(job_frame(op, args, approval))
        out, err, code = bytearray(), bytearray(), None
        while True:
            frame = await self.ws.receive_bytes()
            if not frame:
                continue
            tag, payload = frame[:1], frame[1:]
            if tag == b"O":
                out += payload
            elif tag == b"R":
                err += payload
            elif tag == b"X":
                try:
                    code = int(payload.decode() or "0")
                except Exception:
                    code = 0
                break
        if not result.done():
            result.set_result((bytes(out), bytes(err), code if code is not None else 0))


class MgmtRelay:
    """Pool of idle bastion worker WebSockets, KEYED BY bastion token_id — management must reach a
    SPECIFIC server's bastion (unlike egress, where any worker will do)."""

    def __init__(self):
        self._idle: dict = {}     # token_id -> asyncio.Queue[MgmtWorker]
        self._workers: dict = {}  # token_id -> set[MgmtWorker]

    def _q(self, token_id: int) -> "asyncio.Queue":
        if token_id not in self._idle:
            self._idle[token_id] = asyncio.Queue()
            self._workers[token_id] = set()
        return self._idle[token_id]

    def _register_idle(self, w: MgmtWorker):
        self._q(w.bastion_token_id)
        self._workers[w.bastion_token_id].add(w)
        self._idle[w.bastion_token_id].put_nowait(w)

    def discard(self, w: MgmtWorker):
        s = self._workers.get(w.bastion_token_id)
        if s:
            s.discard(w)

    def worker_count(self, token_id: int) -> int:
        return len(self._workers.get(token_id, ()))

    async def dispatch_detailed(self, token_id: int, op: str, args: list, timeout: float,
                                approval: str = ""):
        """Hand a command to an idle worker for THIS server's bastion; collect its output.

        Returns ((stdout, stderr, exit), "") on a reply, else (None, reason) where reason is
        DISPATCH_NO_WORKER (nothing ever picked the job up — the machine really has no
        management worker for this window — or the worker that took it was stopped before
        sending it: either way the job never reached the machine) or DISPATCH_NO_REPLY (a
        worker TOOK the job and did not answer inside the budget — the work is very likely
        still running on the user's machine). Those are different facts and the caller must
        be able to tell them apart: reporting the second as the first is a false diagnosis
        about a live worker, and it is what made a 20-minute model pull read as a missing
        machine."""
        deadline = asyncio.get_event_loop().time() + timeout
        q = self._q(token_id)
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                return None, DISPATCH_NO_WORKER
            try:
                worker = await asyncio.wait_for(q.get(), remaining)
            except asyncio.TimeoutError:
                return None, DISPATCH_NO_WORKER
            self._workers[token_id].discard(worker)  # leaving idle; serve() re-adds if reused
            result = asyncio.get_event_loop().create_future()
            if not worker.assign((op, args, result, approval or "")):
                continue  # stale worker (dropped while idle); try the next
            try:
                got = await asyncio.wait_for(
                    result, max(0.1, deadline - asyncio.get_event_loop().time()))
            except asyncio.TimeoutError:
                # The job WAS accepted. Whatever it is doing, it is doing it.
                return None, DISPATCH_NO_REPLY
            if got is None:
                # Only from a worker stopped before it sent the job (MgmtWorker.serve).
                return None, DISPATCH_NO_WORKER
            return got, ""

    async def dispatch(self, token_id: int, op: str, args: list, timeout: float, approval: str = ""):
        """The result-only form, for callers that report the two no-result cases the same
        way. Returns (stdout, stderr, exit) or None."""
        got, _ = await self.dispatch_detailed(token_id, op, args, timeout, approval=approval)
        return got


_relay: Optional[MgmtRelay] = None


def get_mgmt_relay() -> MgmtRelay:
    global _relay
    if _relay is None:
        _relay = MgmtRelay()
    return _relay


# ── Management AUDIT + the OPERATE service (2026-06-24) ───────────────────────────────────────
# Moved here from server/routes/network.py so the POST /mgmt/run route AND the mgmt_run host tool
# share ONE audited code path with ONE signed-grant gate — neither reimplements it, and the host
# tool no longer fakes an HTTP Request to reach it. The route keeps a thin `_audit_mgmt = audit_mgmt`
# alias so /harness/run (which also audits) is unchanged.
# nac_mgmt_audit is declared in migrations/v060_nac_mgmt.sql:38. The runtime
# CREATE that used to run before every audit write is gone.


async def audit_mgmt(pool, entity_id: int, server: str, op: str, *, exit_code, decision: str) -> None:
    """Command accounting — the bright line vs egress (which logs NOTHING, for sovereignty).
    Every management action ON the user's machine is recorded: who, what, where, when, result."""
    try:
        await pool.execute(
            "INSERT INTO nac_mgmt_audit (entity_id, server, op, decision, exit_code) "
            "VALUES ($1,$2,$3,$4,$5)", entity_id, server, op, decision, exit_code)
    except Exception as e:  # pragma: no cover - defensive
        log.warning("mgmt audit write failed: %s", e)


def deny_reason(grant: Optional[dict]) -> str:
    """The refusal line for a user-denied op: the user's own words when the grant carries
    them, else the token-level fact. Read by the host and echoed to the user, so it must
    say WHO said no — the user did, in their grant — not 'the platform'."""
    intent = (grant or {}).get("intent") or {}
    words = str(intent.get("do_not_want") or "").strip()
    if words:
        return f"denied by your grant — you said: \"{words}\""
    return "denied by your grant (deny beats allow)"


class MgmtError(Exception):
    """A management dispatch failure the caller surfaces. `status` is the HTTP code the /mgmt/run
    route maps it to; the mgmt_run host tool just reports `detail`. The default-DENY refusal is NOT
    an error — it returns a normal exit-126 result, the bastion-allowlist shape."""
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


async def _model_pull_reconcile(pool, eid: int, server: str, token_id: int, args,
                                grants, suspended, quarantined) -> Optional[dict]:
    """A pull whose reply never arrived: ask the machine's OWN ledger whether the bytes
    landed, and never make the user approve the same download twice.

    THE INVARIANT THIS RESTS ON, read in the box rather than assumed. handleModelPull
    calls stepupMarkUsed and recordPlaced ONLY after modelPullStreaming returns without
    error (the gateway client's modelswap.go). So on that box the approval is
    burned exactly when the bytes are on the disk and the ledger entry is written, and the
    three facts move together:

      * download FAILED    -> approval intact, ledger unchanged. The same approval still
                              funds a retry; the user is asked for nothing.
      * download LANDED    -> approval spent, ledger has the model. model:list reports it
                              with placed_by_this_host true (modelEntry, modelswap.go:583-589;
                              filled from the ledger at :629-634), so this host
                              can learn the pull succeeded without the reply that was lost,
                              and MUST NOT propose a second approval for it.

    Returns the reconciled result dict when the ledger says the model is here, else None
    (the caller then reports the honest "still working" answer). Returns None rather than
    guessing whenever the reconcile cannot be done — an unreadable ledger is not evidence
    of anything, in either direction.

    model:list is a DIFFERENT OP and gets the gate every op gets: if the user's grant does
    not cover it on this server, it is not run. The point of this lane is that acting on
    someone's machine is authorized, and a recovery path is still acting on it."""
    try:
        req = json.loads(str(args[0])) if args else {}
        model = str(req.get("model") or "").strip()
    except Exception:
        return None
    if not model:
        return None
    if not mgmt_decision(grants, eid, server, MODEL_LIST_OP, suspended, quarantined):
        return None
    got, _why = await get_mgmt_relay().dispatch_detailed(
        token_id, MODEL_LIST_OP, [], timeout=mgmt_op_budget(MODEL_LIST_OP))
    if got is None:
        return None
    out, _err, code = got
    await audit_mgmt(pool, eid, server, MODEL_LIST_OP, exit_code=code, decision="ran")
    if code != 0:
        return None
    try:
        listed = json.loads(out.decode("utf-8", "replace") or "{}").get("models") or []
    except Exception:
        return None
    entry = next((m for m in listed
                  if isinstance(m, dict) and m.get("model") == model
                  and m.get("placed_by_this_host")), None)
    if entry is None:
        return None
    await audit_mgmt(pool, eid, server, MODEL_PULL_OP, exit_code=0, decision="reconciled")
    return {"server": server, "op": MODEL_PULL_OP, "exit": 0,
            "stdout": json.dumps({
                "model": model,
                "downloaded": True,
                "placed_by_this_host": True,
                "role": entry.get("role"),
                "dim": entry.get("dim"),
                "reconciled": True,
                "note": (f"the download finished on {server} but its reply did not reach "
                         f"this host. The machine's own record shows the model is here and "
                         f"that this host placed it, so the approval already spent on it "
                         f"paid for this download — do not ask for another one."),
            }),
            "stderr": ""}


async def _model_pull_restarted(pool, eid: int, server: str, token_id: int, args, res: dict,
                                grants, suspended, quarantined) -> dict:
    """A status poll for a job the machine has no record of. Answer it with the LEDGER,
    never with a failure.

    The box answers 'unknown' rather than 'failed' for a job it cannot find, and the
    reason is the same one backupfetch.go:168-172 gives for backup_fetch: a restart
    loses the in-memory job table, and a lost record is not a lost download. Two
    branches, and the ledger is what tells them apart:

      * the download had NOT finished when the box restarted -> stepupMarkUsed never
        ran (the gateway's modelswap.go, runModelPull), so the approval is intact and
        single-use. The host starts again with THE SAME approval and the user is asked
        for nothing.
      * the download HAD finished -> the approval is spent and the ledger has the
        model, so _model_pull_reconcile finds it through model:list and this host must
        never propose a second approval for it.

    THE MODEL NAME COMES FROM THE REQUEST, not the reply. A status poll carries a job
    id and the box has forgotten what that id was for — that is the whole premise. The
    HOST knows what it started, so it says so: a status arg may carry `model` beside
    `job_id`, and it is used for nothing except naming what to look for in the ledger.

    Anything that cannot be decided is left as the box's own honest 'unknown'."""
    try:
        body = json.loads(res.get("stdout") or "{}")
    except Exception:
        return res
    if not isinstance(body, dict) or body.get("state") != "unknown":
        return res
    try:
        req = json.loads(str(args[0])) if args else {}
    except Exception:
        req = {}
    model = str((req or {}).get("model") or "").strip()
    if model:
        landed = await _model_pull_reconcile(
            pool, eid, server, token_id, [json.dumps({"model": model})],
            grants, suspended, quarantined)
        if landed is not None:
            return landed
    body["note"] = (
        (body.get("note") or "") +
        " Nothing was cancelled and nothing was spent: the approval is burned only "
        "when the bytes land, so if this download had not finished, the same approval "
        "still funds it and the user is asked for nothing.")
    res = dict(res)
    res["stdout"] = json.dumps(body)
    return res


def contain_queue_result(server: str, pid: int, queued) -> tuple:
    """The audit decision and the op result for a containment request, from
    what request_contain said: "queued" (a new order — `ran`), "open" (an
    order for this pid was already pending, nothing added — `already-queued`,
    so the audit never says a click did something it did not), or False
    (`no-bastion`)."""
    if not queued:
        return "no-bastion", None
    already = queued == "open"
    return ("already-queued" if already else "ran"), {
        "server": server, "op": CONTAIN_OP, "exit": 0,
        "stdout": json.dumps({"contain": "already queued" if already else "queued", "pid": pid,
                              "delivered": "on the bastion's next check-in that can see its agents"}),
        "stderr": ""}


# `contain finding:<fp>` stops a delta finding by fingerprint; a bare integer is
# still an agent pid.
FINDING_ARG_PREFIX = "finding:"


def contain_finding_queue_result(server: str, fp: str, queued) -> tuple:
    """contain_queue_result for a finding order: `ran`, `already-queued`, or
    `no-bastion`, and a result that says what "queued" means here — the box
    answers, and the order is cleared on its answer, never on silence."""
    if not queued:
        return "no-bastion", None
    already = queued == "open"
    return ("already-queued" if already else "ran"), {
        "server": server, "op": CONTAIN_OP, "exit": 0,
        "stdout": json.dumps({"contain": "already queued" if already else "queued", "finding": fp,
                              "delivered": "on the bastion's next check-in; it stops the program only "
                                           "if the fingerprint is still a finding and not in the "
                                           "machine's accounting, and reports what it did"}),
        "stderr": ""}


async def _gate_state(pool, eid: int) -> tuple[list, set, set]:
    """The user's SIGNED grants (tamper-verified with the host entity pubkey — an
    unsigned/forged/expired grant is skipped, which denies) and the two holds, loaded
    once for however many decisions follow."""
    from server.network import nac_stepup
    verify_pubkey = None
    try:
        verify_pubkey = await nac_stepup.entity_verify_pubkey(pool, eid)
    except Exception:
        verify_pubkey = None
    grants = await load_mgmt_grants(pool, verify_pubkey)
    suspended = await load_suspended_servers(pool)
    quarantined = await load_quarantined(pool)
    return grants, suspended, quarantined


def _decide(grants: list, suspended: set, quarantined: set, eid: int, server: str,
            op: str) -> tuple[bool, str, str]:
    """(allowed, reason, audit decision) for ONE (server, op) against loaded state."""
    if mgmt_decision(grants, eid, server, op, suspended, quarantined):
        return True, "", "ran"
    denied = mgmt_denied_by(grants, server, op)
    reason = ("server is quarantined" if server in quarantined
              else "server is suspended" if server in suspended
              else deny_reason(denied) if denied is not None
              else "no signed grant (default-deny)")
    decision = ("denied-user" if denied is not None and server not in suspended
                and server not in quarantined else "denied-pod")
    return False, reason, decision


async def gate_op(pool, eid: int, server: str, op: str) -> tuple[bool, str, str]:
    """THE POD GATE, as one call: may this host run `op` on `server` under the user's signed
    grants? Returns (allowed, reason, audit decision). Default-DENY. Every lane that reaches a
    machine runs it — the mgmt ops (run_mgmt_op) and a context-extraction job (server/routes/context_extract.py), which used to
    go by the box's self-reported capability alone."""
    grants, suspended, quarantined = await _gate_state(pool, eid)
    return _decide(grants, suspended, quarantined, eid, (server or "").strip(), (op or "").strip())


async def gate_servers(pool, eid: int, op: str, servers) -> dict:
    """gate_op for several servers with ONE load of the grants:
    {server: (allowed, reason, audit decision)} — the same three answers gate_op gives, so a
    refusal is audited as the user's deny or the pod's exactly as run_mgmt_op would audit it.
    The extraction lane uses it to say which of the user's machines are APPROVED for a job,
    so a machine that has the engine and no grant is named as that — the host drafts a grant
    for the user instead of asking the machine."""
    grants, suspended, quarantined = await _gate_state(pool, eid)
    out = {}
    for server in servers or []:
        if not server:
            continue
        out[str(server)] = _decide(grants, suspended, quarantined, eid, str(server).strip(), (op or "").strip())
    return out


# THE HOST'S OWN STEP-UP ASKS (2026-09-27). A model pull is the one approval a
# Host files itself through mgmt_run (network skill, "Putting a model on
# someone's machine"): stepup_request with a model_pull purpose, then
# stepup_status until the person approves at the machine's console. Every other
# console ask is filed by the pod for its own act (nac_stepup.box_confirmation,
# nac_approval) and its answer is collected by the pod: the box hands an
# approval out once (stepup.go handleStepupStatus), so a Host reading one of
# THOSE would take the pod's approval and the person would be asked again. So
# through mgmt_run a Host files only a model_pull ask and reads only the asks it
# filed. Held in this process (the pod runs one): after a restart the Host files
# its ask again.
HOST_STEPUP_KIND = "model_pull"
_HOST_STEPUP_OPS = ("stepup_request", "stepup_status")
_host_stepup_filed: dict = {}   # (eid, token_id) -> {request ids this Host filed}


STEPUP_REQUEST_SHAPE = ("args=['model_pull|<model>|<role>|<max_bytes>|<replaces>', <base64 of "
                        "JSON {\"kind\": \"model_pull\", \"model\", \"role\", \"max_bytes\", "
                        "\"replaces\"}>]")

NOT_FILED_HERE = ("this Host has no record of filing that request (a restart forgets them). If it "
                  "was your model_pull ask, file the same ask again with stepup_request: the machine "
                  "answers with that same request, or with its approval if the person already gave "
                  "it. If it was the platform's own ask, the platform reads its answer: tell the "
                  "person to approve it at the machine and wait for the result.")


def host_stepup_refusal(eid: int, token_id, op: str, args) -> Optional[str]:
    """Why a Host's stepup_request is refused, or None. Only a model_pull ask
    is the Host's to file. A model_pull ask the box cannot read (a missing
    argument, details that are not base64 JSON) goes to the box, whose own
    words say what is wrong with it."""
    if op != "stepup_request":
        return None
    purpose = str(args[0]) if args else ""
    if not purpose.startswith(HOST_STEPUP_KIND + "|"):
        return ("a Host files only a model_pull ask at a machine's console; every other "
                "approval is asked by the platform itself, for its own act. A model pull: "
                + STEPUP_REQUEST_SHAPE)
    try:
        details = json.loads(base64.b64decode(str(args[1]).strip(), validate=True))
    except Exception:
        return None
    if isinstance(details, dict) and details.get("kind") not in (None, HOST_STEPUP_KIND):
        return ("a model_pull ask carries details of kind model_pull: " + STEPUP_REQUEST_SHAPE)
    return None


def host_stepup_known(eid: int, token_id, args) -> bool:
    """Whether this Host filed the request a stepup_status names."""
    rid = str(args[0]).strip() if args else ""
    return bool(rid) and rid in _host_stepup_filed.get((eid, token_id), set())


def note_host_stepup(eid: int, token_id, stdout: str) -> None:
    """Remember a request this Host filed, so it may read its answer."""
    try:
        rid = str((json.loads(stdout) or {}).get("request") or "")
    except Exception:
        return
    if rid:
        _host_stepup_filed.setdefault((eid, token_id), set()).add(rid)


# The callers whose words a model or an outside party wrote: the Host's own
# mgmt_run tool ("tool"), POST /mgmt/run ("route"), and an outside agent
# connected over MCP ("agent", server/agent_access/tools.py). Each gets the
# per-run tap on the context store's writes (TOOL_ASKS_ANYWAY) and the step-up
# checks below, which the Host's own standing work ("service") does not.
ORIGIN_AGENT = "agent"
_ASKING_ORIGINS = ("tool", "route", ORIGIN_AGENT)


async def run_mgmt_op(pool, eid: int, server: str, op: str, args, *,
                      token_id: int | None = None, note: str = "",
                      approval_wait_s: float | None = None, origin: str = "service",
                      budget_s: float | None = None) -> dict:
    """OPERATE a server in the entity's mesh — the SINGLE source of truth for both POST /mgmt/run
    (the route) and the mgmt_run host tool. Enforces the user's SIGNED grant (default-DENY), dispatches
    the op to the bastion's management worker, and ALWAYS audits. Returns {server, op, exit, stdout,
    stderr}; an ungranted / suspended / quarantined (server, op) is refused in the bastion-allowlist
    shape (exit 126), never run. Raises MgmtError(status, detail) for missing input / no-bastion /
    no-worker. The signed-grant default-deny gate lives HERE so neither caller can bypass it."""
    server = (server or "").strip()
    op = (op or "").strip()
    if not server or not op:
        raise MgmtError(400, "server and op required")
    args = args or []
    # POD GATE — default-DENY, enforcing the user's SIGNED grants: the one decision every lane
    # onto a machine runs (_decide, also behind gate_op / gate_servers — ctx_extract runs it
    # since 2026-09-24). The loaded state stays in scope: the ops below read the grants again.
    grants, suspended, quarantined = await _gate_state(pool, eid)
    allowed, reason, decision = _decide(grants, suspended, quarantined, eid, server, op)
    if not allowed:
        # Default-deny: refuse in the bastion-allowlist's own shape (exit 126), not an error — an
        # ungranted op reads the same whether the pod-gate or the box refused it. A suspended or
        # quarantined server lands here too (grants intact but the hold short-circuits the decision).
        await audit_mgmt(pool, eid, server, op, exit_code=126, decision=decision)
        return {"server": server, "op": op, "exit": 126, "stdout": "",
                "stderr": f"op '{op}' on '{server}' not permitted — {reason}"}
    if token_id is not None:
        # A caller that already resolved the bastion (the routes do, by the
        # token the user clicked) passes it, and it is used — after checking it
        # really is this server. Looking it up again by hostname picked the most
        # recently seen token of that NAME, so two bastions sharing a hostname
        # could have an order queued against the wrong box.
        owns = await pool.fetchval(
            "SELECT 1 FROM network_bastion_heartbeat WHERE token_id=$1 AND hostname=$2",
            token_id, server)
        if not owns:
            raise MgmtError(404, f"bastion {token_id} does not report the hostname '{server}'")
    else:
        # Addressed by hostname only (the mgmt_run host tool). Two live bastions
        # reporting the same hostname are ambiguous, and picking the most
        # recently seen one could send an order to the wrong machine — so that case is refused with a way out, never guessed.
        n_live = await pool.fetchval(
            "SELECT count(DISTINCT h.token_id) FROM network_bastion_heartbeat h "
            "  JOIN mcp_client_tokens t ON t.id = h.token_id "
            " WHERE h.hostname=$1 AND t.revoked_at IS NULL AND t.operator_kind='bastion'",
            server)
        if isinstance(n_live, int) and n_live > 1:
            await audit_mgmt(pool, eid, server, op, exit_code=1, decision="ambiguous-hostname")
            raise MgmtError(409, f"{n_live} bastions report the hostname '{server}' — act from "
                                 "that machine's bastion card, which names the exact one")
        token_id = await pool.fetchval(
            "SELECT token_id FROM network_bastion_heartbeat WHERE hostname=$1 "
            "ORDER BY last_seen_at DESC LIMIT 1", server)
    if token_id is None:
        raise MgmtError(404, f"no bastion registered for server '{server}'")
    # THE PERSON'S APPROVAL (nac_approval, bastion 0.33.0). A box anchored to
    # this Host runs a write only on an approval it verified — from the
    # person's own device or its console — bound to this op, this machine and
    # exactly these args. Obtained HERE, after the pod gate (so a server with
    # no grant never rings the person) and before anything reaches the box.
    # A read, an op carrying its own approval, and a box that gates nothing
    # (older than 0.33.0, or unanchored) go straight through.
    # It is asked AFTER every check the pod makes on its own (below: the
    # contain argument checks, the worker check), so the person is never rung
    # for something the pod would then refuse anyway. By token, so two boxes
    # sharing a hostname cannot answer for each other.
    from server.network import nac_approval
    wait = nac_approval.TOOL_WAIT_S if approval_wait_s is None else approval_wait_s

    # THE OP DOOR (nac_ops): an op the Host
    # wrote, run on this machine under signed terms — the dry run, the apply
    # bound to what the dry run showed (its approval obtained there, inside
    # the terms), and the standing grant. Same pod gate above; the pod's own
    # refusals come back in the lane's 126 shape, never as errors to route
    # around; a silent or missing worker is the same MgmtError as any op's.
    from server.network import nac_ops
    if op in nac_ops.DOOR_OPS:
        try:
            return await nac_ops.run_door(pool, eid, server, token_id, op, args, note=note, wait_s=wait)
        except nac_ops.OpError as e:
            if e.status >= 500:
                # A silent or missing worker, or this side's own failure: an
                # error, never dressed as a refusal.
                raise MgmtError(e.status, e.detail)
            return {"server": server, "op": op, "exit": 126, "stdout": "", "stderr": e.detail}

    async def _approval(a_args):
        # origin="tool" (the model's mgmt_run) or "route" (POST /mgmt/run, any
        # write:network holder): not the context store's own writer. ctx_put / ctx_del are
        # standing work when the Host's own context store writes them (the box
        # takes no tap, writes.go ownApproval); a model naming a path and a
        # body through mgmt_run is not that, so the POD asks for the tap here
        # — a pod-side wall only, until the op format carries the caller
        # to the box. (The exemption is for the store's own
        # writes.)
        # Through the person's CARD (nac_approval.obtain_via_card):
        # the ask is an overlay over the chat and a ticket, in the platform's
        # words; their click files the device ask; this call's successor
        # waits on the tap.
        try:
            appr = await nac_approval.obtain_via_card(pool, eid, server, op, a_args, note=note, wait_s=wait,
                                                      token_id=token_id,
                                                      force=(origin in _ASKING_ORIGINS and op in nac_approval.TOOL_ASKS_ANYWAY))
        except nac_ops.OpError as e:
            # The card could not be filed (this side's own failure): the
            # lane's error, as the op door says it — never a bare 500.
            raise MgmtError(e.status, e.detail)
        if appr["state"] == "asked":
            await audit_mgmt(pool, eid, server, op, exit_code=None, decision="card-filed")
            return appr, ""
        if appr["state"] == "pending":
            await audit_mgmt(pool, eid, server, op, exit_code=None, decision="awaiting-approval")
            return appr, ""
        if appr["state"] not in ("approved", "not_needed"):
            decision = "no-way" if appr["state"] == "no_way" else "denied-person"
            await audit_mgmt(pool, eid, server, op, exit_code=126, decision=decision)
            return appr, ""
        return appr, (appr.get("approval") or "")

    def _refused(appr):
        return {"server": server, "op": op, "exit": 126, "stdout": "",
                "stderr": f"refused: {op} on '{server}' — {appr.get('detail') or appr['state']}"}

    if op == CONTAIN_OP:
        # NAC agent: terminating a detected agent on the box is a
        # management action, so it needs the same signed grant as any other — the
        # gate above already held. It rides the heartbeat directive (contain.go),
        # not the relay: the box refuses any pid that is not a detected agent, so
        # this can never reach an arbitrary process. Audited like every op.
        from server.network import topology
        # A FINDING, named by fingerprint (v172): a program
        # the box's delta reported that the agent plane does not recognise. The
        # same grant, the same op; the box resolves the fingerprint itself and
        # refuses anything that is not a finding, a baseline entry included.
        first = str(args[0]).strip() if args else ""
        if first.startswith(FINDING_ARG_PREFIX):
            fp = first[len(FINDING_ARG_PREFIX):].strip().lower()
            if not topology._FINDING_FP_OK.match(fp):
                # Checked BEFORE the person is asked: a malformed fingerprint
                # must not ring their device for an order that cannot be placed.
                await audit_mgmt(pool, eid, server, op, exit_code=1, decision="bad-fingerprint")
                raise MgmtError(400, "contain finding:<fp> needs the finding's 32-hex fingerprint "
                                     "(the `fp` in the finding's evidence)")
            # The order is queued even while the approval is pending — it is
            # held off the beat until the tap lands (topology withholds an
            # unapproved directive from a gating box) and the card shows it.
            # An open order that already carries its approval is not asked for
            # again; one that lacks it takes the tap this call may have read
            # (request_contain_finding fills an open order's NULL approval).
            # An order for this finding that already RAN (cleared inside the
            # delivery TTL) is reported, never placed again: a re-call after
            # the tap would otherwise file a second card for a program gone.
            # Only an order that RAN (outcome killed) short-circuits: one that
            # lapsed, was refused or came back unrecognised is placed again.
            done = await pool.fetchrow(
                "SELECT outcome, cleared_at FROM nac_finding_contain WHERE token_id=$1 AND fp=$2 "
                "   AND cleared_at > now() - $3::interval AND outcome = 'killed' "
                " ORDER BY cleared_at DESC LIMIT 1",
                token_id, fp, topology.CONTAIN_PENDING_TTL)
            held = await pool.fetchval(
                "SELECT approval FROM nac_finding_contain WHERE token_id=$1 AND fp=$2 AND cleared_at IS NULL",
                token_id, fp)
            if done and held is None and not await pool.fetchval(
                    "SELECT 1 FROM nac_finding_contain WHERE token_id=$1 AND fp=$2 AND cleared_at IS NULL",
                    token_id, fp):
                await audit_mgmt(pool, eid, server, op, exit_code=0, decision=f"already {done['outcome'] or 'done'}")
                return {"server": server, "op": op, "exit": 0, "stderr": "",
                        "stdout": json.dumps({"contain": "already done", "finding": fp,
                                              "outcome": done["outcome"], "cleared_at": str(done["cleared_at"])})}
            if held:
                appr, approval = {"state": "approved"}, held
            else:
                appr, approval = await _approval([FINDING_ARG_PREFIX + fp])
                if appr["state"] not in ("approved", "not_needed", "pending", "asked"):
                    return _refused(appr)
            queued = await topology.request_contain_finding(pool, eid, token_id, fp, approval=approval)
            if queued == "bad_fp":
                await audit_mgmt(pool, eid, server, op, exit_code=1, decision="bad-fingerprint")
                raise MgmtError(400, "contain finding:<fp> needs the finding's 32-hex fingerprint "
                                     "(the `fp` in the finding's evidence)")
            decision, result = contain_finding_queue_result(server, fp, queued)
            await audit_mgmt(pool, eid, server, op, exit_code=0 if queued else 1, decision=decision)
            if not queued:
                raise MgmtError(404, f"'{server}' is not one of this host's bastions")
            if appr["state"] in ("pending", "asked"):
                result = {**result, **nac_approval.pending_result(server, op, appr), "queued": True}
            return result
        try:
            pid = int(args[0])
        except (IndexError, TypeError, ValueError):
            raise MgmtError(400, "contain needs one arg: the detected agent's pid, "
                                 "or finding:<fp> for a delta finding")
        # Apps are visibility, never a target (2026-09-02): the Claude desktop
        # app, an IDE, a browser — software the user is running that talks to a
        # model. The bastion refuses these too; the host says why first.
        # ("app" is nac_agents.KIND_APP.)
        from server.network import nac_agents
        kind = await nac_agents.observation_kind(pool, token_id, pid)
        if kind == "app":
            await audit_mgmt(pool, eid, server, op, exit_code=1, decision="refused-app")
            raise MgmtError(409, f"pid {pid} on '{server}' is an app the user is running, not an "
                                 "agent — apps are shown, never contained")
        # Only an order that RAN (outcome done) short-circuits, and only while
        # the pid has not been handed to another program: the signature the
        # beat reports for the pid now must be the one the order killed (the
        # box tells reused pids apart the same way). Expired, pid_reused and
        # every other outcome place a new order as before.
        done = await pool.fetchrow(
            "SELECT outcome, cleared_at, signature FROM nac_agent_contain WHERE token_id=$1 AND pid=$2 "
            "   AND cleared_at > now() - $3::interval AND outcome = 'done' "
            " ORDER BY cleared_at DESC LIMIT 1",
            token_id, pid, topology.CONTAIN_PENDING_TTL)
        if done:
            now_sig = await pool.fetchval(
                "SELECT signature FROM nac_agent_activity WHERE token_id=$1 AND pid=$2 "
                " ORDER BY last_seen DESC LIMIT 1", token_id, pid)
            if now_sig and str(now_sig).strip()[:64] != str(done["signature"] or "").strip()[:64]:
                done = None  # the pid wears another program's signature now: a new act
        held = await pool.fetchval(
            "SELECT approval FROM nac_agent_contain WHERE token_id=$1 AND pid=$2 AND cleared_at IS NULL",
            token_id, pid)
        if done and held is None and not await pool.fetchval(
                "SELECT 1 FROM nac_agent_contain WHERE token_id=$1 AND pid=$2 AND cleared_at IS NULL",
                token_id, pid):
            await audit_mgmt(pool, eid, server, op, exit_code=0, decision=f"already {done['outcome'] or 'done'}")
            return {"server": server, "op": op, "exit": 0, "stderr": "",
                    "stdout": json.dumps({"contain": "already done", "pid": pid,
                                          "outcome": done["outcome"], "cleared_at": str(done["cleared_at"])})}
        if held:
            appr, approval = {"state": "approved"}, held
        else:
            appr, approval = await _approval([str(pid)])
            if appr["state"] not in ("approved", "not_needed", "pending", "asked"):
                return _refused(appr)
        queued = await topology.request_contain(pool, eid, token_id, pid, approval=approval)
        decision, result = contain_queue_result(server, pid, queued)
        await audit_mgmt(pool, eid, server, op, exit_code=0 if queued else 1, decision=decision)
        if not queued:
            raise MgmtError(404, f"'{server}' is not one of this host's bastions")
        if appr["state"] in ("pending", "asked"):
            result = {**result, **nac_approval.pending_result(server, op, appr), "queued": True}
        return result
    host_asks = origin in _ASKING_ORIGINS and op in _HOST_STEPUP_OPS
    if host_asks:
        why = host_stepup_refusal(eid, token_id, op, args)
        if why:
            await audit_mgmt(pool, eid, server, op, exit_code=126, decision="denied-pod")
            return {"server": server, "op": op, "exit": 126, "stdout": "",
                    "stderr": f"op '{op}' on '{server}' not permitted — {why}"}
        if op == "stepup_status" and not host_stepup_known(eid, token_id, args):
            # An ANSWER, not a refusal: the Host's next step (file its ask
            # again) must not read as "do not try another way". Nothing is sent
            # to the box, so the platform's own one-time handout stays its own.
            await audit_mgmt(pool, eid, server, op, exit_code=0, decision="not-filed-here")
            return {"server": server, "op": op, "exit": 0, "stderr": "",
                    "stdout": json.dumps({"state": "not_filed_here", "detail": NOT_FILED_HERE})}
    relay = get_mgmt_relay()
    if relay.worker_count(token_id) == 0:
        await audit_mgmt(pool, eid, server, op, exit_code=None, decision="no-worker")
        raise MgmtError(502, f"no management worker connected for '{server}'")
    appr, approval = await _approval(args)
    if appr["state"] in ("pending", "asked"):
        return nac_approval.pending_result(server, op, appr)
    if appr["state"] not in ("approved", "not_needed"):
        return _refused(appr)
    # budget_s: a caller that dispatches and returns (the ship runner, whose
    # line keeps running on the machine and is read back with op_plan_status)
    # waits less than the op's own ceiling.
    budget = mgmt_op_budget(op, args) if budget_s is None else float(budget_s)
    res, why = await relay.dispatch_detailed(
        token_id, op, [str(a) for a in args], timeout=budget, approval=approval)
    if res is None and why == DISPATCH_NO_REPLY:
        # A WORKER TOOK THIS JOB AND IS STILL ON IT. Saying "no live management
        # worker" here is a lie about the machine, and the reason it mattered is
        # what comes next for a pull: the box finishes, burns the user's
        # single-use approval and writes its ledger, while this host has
        # recorded a failure. So before reporting anything, ASK THE LEDGER.
        if op == MODEL_PULL_OP:
            landed = await _model_pull_reconcile(
                pool, eid, server, token_id, args, grants, suspended, quarantined)
            if landed is not None:
                return landed
        await audit_mgmt(pool, eid, server, op, exit_code=None, decision="no-reply")
        raise MgmtError(504, f"'{server}' took this job and had not answered after "
                             f"{int(budget)}s. Its management worker is connected and "
                             f"working — nothing was cancelled, and the work may still "
                             f"be running on that machine. Ask again for its result "
                             f"rather than starting it over.")
    if res is None:
        await audit_mgmt(pool, eid, server, op, exit_code=None, decision="no-worker")
        raise MgmtError(502, "no live management worker")
    stdout, stderr, code = res
    await audit_mgmt(pool, eid, server, op, exit_code=code, decision="ran")
    out = {"server": server, "op": op, "exit": code,
           "stdout": stdout.decode("utf-8", "replace"),
           "stderr": stderr.decode("utf-8", "replace")}
    if host_asks and op == "stepup_request" and code == 0:
        note_host_stepup(eid, token_id, out["stdout"])
    if op == MODEL_PULL_OP and code == 0 and model_pull_sub_op(args) == MODEL_PULL_STATUS:
        # A poll for a job the machine has forgotten is a question for the LEDGER,
        # never a failure. See _model_pull_restarted.
        out = await _model_pull_restarted(
            pool, eid, server, token_id, args, out, grants, suspended, quarantined)
    return out
