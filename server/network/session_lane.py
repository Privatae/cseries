"""
Terminal sessions on a bastion (the host half).

A shell that runs ON the user's machine and is streamed here over the reverse harness
workers the bastion already dials (/bastion-harness). Nothing new on the network: no inbound
port, no SSH server, no host/port fields anywhere — the only target is a bastion.

The bastion side (the gateway client's session.go) is the spec. Over one worker
connection, after its BEARER hello, the host's first frame is 'S' + JSON:

  {"op": "session:open" | "session:root", "shell": "", "cols": N, "rows": N,
   "grant_terms_b64": "…", "grant_sig_b64": "…", "session_id": "…", "approval": "…"}

then host -> box  'I' + bytes (keyboard), 'R' + {"cols","rows"} (resize)
     box  -> host 'O' + bytes (pty output), 'E' + text (a notice), 'X' + decimal exit.

Exit codes the box uses: the shell's own status; 126 = refused (no/invalid grant, op not
listed, denied, root without step-up — the same shape as an ungranted mgmt op); 124 = the
box closed it on a timer (idle / hard cap); 1 = bad spec or spawn failure.

THE GRANT IS THE SAME ONE THE MGMT LANE ENFORCES. run_mgmt_op checks the user's signed
nac_mgmt_grants row here in the pod; the box re-verifies the SAME terms and signature
against the entity key it pinned at first dcn_grant_sync. So the S frame carries the exact
bytes approve_mgmt_grant signed — nac_stepup._canonical(terms) — and the stored signature,
never a re-signing. A grant that lists 'session:open' (or 'session:root') for this server
is what opens a shell; nothing else does.

Every open and close is a mgmt op: audited in nac_mgmt_audit (op = the session op) the way
run_mgmt_op records, and recorded to a transcript the user owns on the box.
"""
from __future__ import annotations

import asyncio
import base64
import datetime as _dt
import json
import logging
import re
import uuid
from typing import Optional

log = logging.getLogger("cseries.session")

SESSION_OPEN_OP = "session:open"
SESSION_ROOT_OP = "session:root"
SESSION_OPS = (SESSION_OPEN_OP, SESSION_ROOT_OP)
# One command as root, no terminal (bastion 0.49.0, client-go/rootexec.go): the
# same S frame, answered 'J' + JSON result then 'X'. Run by run_root_exec below.
SESSION_ROOT_EXEC_OP = "session:root:exec"
# The worker lane the box announces for terminal work (`BEARER <bearer> lane=session` on
# /bastion-harness). S frames go only to these workers; H jobs never take one.
SESSION_LANE = "session"

# The box's own rule for a session id (session.go): 8-64 of [A-Za-z0-9._-]. It names the
# transcript file and the audit rows on the machine, so it is checked here too.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")

EXIT_REFUSED = 126          # the ungranted shape, shared with the box
EXIT_TIMER = 124            # idle / hard cap on the box
EXIT_BAD_SPEC = 1

# How long to wait for an idle worker. Each pairing runs 2 workers; an open session holds
# one, and a worker that just finished a job re-dials within seconds. Longer than that and
# the honest answer is "no worker" rather than a spinner.
WORKER_WAIT_S = 8.0
# The step-up poll for a root shell: the user has to walk to the machine and type a code.
STEPUP_POLL_S = 2.0
STEPUP_WAIT_S = 10 * 60.0


def new_session_id() -> str:
    return uuid.uuid4().hex


def valid_session_id(sid) -> bool:
    return isinstance(sid, str) and bool(_SESSION_ID_RE.match(sid))


def root_purpose(server: str, session_id: str) -> str:
    """The step-up purpose the box binds a root approval to (session.go
    purposeForSessionRoot; stepup.go rebuilds it from the details and refuses a mismatch)."""
    return f"session_root|{server}|{session_id}"


def root_request_details(server: str, session_id: str, note: str = "") -> dict:
    return {"kind": "session_root", "server": server, "session_id": session_id, "note": note or ""}


# ── The signed grant, reused as-is ────────────────────────────────────────────

async def signed_grant_for(pool, verify_pubkey, server: str, op: str) -> Optional[dict]:
    """The user's approved, signature-checked, unexpired management grant that authorizes
    `op` on `server` and is not overruled by a deny — returned WITH the exact bytes the box
    will verify: {"terms_bytes": canonical JSON, "signature": stored b64, "terms": dict}.

    Same rows and same checks as nac_mgmt.load_mgmt_grants (approved, not revoked, not
    expired, signature over _canonical(terms) verifies, signed expiry authoritative), plus
    the deny-beats-allow rule from nac_mgmt.mgmt_decision. It reads the rows itself only
    because load_mgmt_grants drops the signature, and the S frame needs it. None = deny."""
    from server.network import nac_mgmt
    from server.network.nac_stepup import _canonical
    if verify_pubkey is None:
        return None
    try:
        rows = await pool.fetch(
            "SELECT id, server, ops, signed_terms, signature, expires_at FROM nac_mgmt_grants "
            "WHERE approved_at IS NOT NULL AND revoked_at IS NULL "
            "AND (expires_at IS NULL OR expires_at > now()) ORDER BY id")
    except Exception as e:
        log.info("signed_grant_for: grants unavailable: %r", e)
        return None
    verified = []
    for row in rows:
        st, sig = row["signed_terms"], row["signature"]
        if st is None or sig is None:
            continue
        terms = st if isinstance(st, dict) else json.loads(st)
        if not nac_mgmt._verify_grant(verify_pubkey, terms, sig):
            continue
        if nac_mgmt._grant_expired(terms.get("expires")):
            continue
        verified.append((terms, sig))
    # deny beats allow, across every verified grant covering the server
    plain = [{"server": t.get("server"), "ops": list(t.get("ops") or []),
              "deny": list(t.get("deny") or []), "intent": t.get("intent")} for t, _ in verified]
    if nac_mgmt.mgmt_denied_by(plain, server, op) is not None:
        return None
    for terms, sig in verified:
        gs = terms.get("server")
        if gs != "*" and gs != server:
            continue
        if op in (terms.get("ops") or []):
            return {"terms": terms, "terms_bytes": _canonical(terms), "signature": sig}
    return None


# ── The session object ────────────────────────────────────────────────────────

class BastionSession:
    """One shell on one bastion. The caller writes keyboard bytes and resizes; it reads
    (tag, payload) events: ('O', bytes), ('E', str), ('X', int). After 'X' the session is
    over and recv() returns None. close() ends it from this side (the worker socket closes,
    the box kills the shell's process group)."""

    def __init__(self, *, server: str, op: str, session_id: str, spec: dict, user: str,
                 origin: str = "app"):
        self.server = server
        self.op = op
        self.session_id = session_id
        self.spec = spec
        self.user = user
        self.origin = origin
        self.started = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()
        self.last_activity = self.started
        self.exit: Optional[int] = None
        self._to_box: "asyncio.Queue" = asyncio.Queue()
        self._events: "asyncio.Queue" = asyncio.Queue()
        self._closed = False
        self._done: "asyncio.Future" = asyncio.get_event_loop().create_future()
        self._on_exit = None

    # -- what the caller sees --
    def _touch(self) -> None:
        self.last_activity = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()

    def write(self, data: bytes) -> None:
        if data and not self._closed:
            self._touch()
            self._to_box.put_nowait(b"I" + bytes(data))

    def resize(self, cols: int, rows: int) -> None:
        if self._closed:
            return
        try:
            cols, rows = int(cols), int(rows)
        except (TypeError, ValueError):
            return
        if cols > 0 and rows > 0:
            self._to_box.put_nowait(b"R" + json.dumps({"cols": cols, "rows": rows}).encode())

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._to_box.put_nowait(None)

    @property
    def closed(self) -> bool:
        return self._closed

    async def recv(self):
        """The next event from the box, or None once the session has ended."""
        if self._closed and self._events.empty():
            return None
        ev = await self._events.get()
        return ev

    async def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        try:
            await asyncio.wait_for(asyncio.shield(self._done), timeout)
        except asyncio.TimeoutError:
            return None
        return self.exit

    # -- the driver, run inside the worker's serve() --
    def _finish(self, code: Optional[int]) -> None:
        if self.exit is None:
            self.exit = code
        self._closed = True
        self._events.put_nowait(("X", self.exit if self.exit is not None else EXIT_BAD_SPEC))
        self._events.put_nowait(None)
        if not self._done.done():
            self._done.set_result(True)
        cb, self._on_exit = self._on_exit, None
        if cb is not None:
            try:
                asyncio.get_event_loop().create_task(cb(self))
            except Exception as e:  # pragma: no cover - defensive
                log.info("session %s exit hook failed: %r", self.session_id, e)

    async def drive(self, ws) -> None:
        """Pump frames both ways until the box says X or the caller closes."""
        try:
            await ws.send_bytes(b"S" + json.dumps(self.spec).encode())
        except Exception as e:
            self._events.put_nowait(("E", f"could not reach the machine: {e}"))
            self._finish(EXIT_BAD_SPEC)
            return
        recv = asyncio.ensure_future(ws.receive_bytes())
        outq = asyncio.ensure_future(self._to_box.get())
        code: Optional[int] = None
        try:
            while True:
                done, _ = await asyncio.wait({recv, outq}, return_when=asyncio.FIRST_COMPLETED)
                if outq in done:
                    frame = outq.result()
                    if frame is None:          # close() from our side
                        break
                    await ws.send_bytes(frame)
                    outq = asyncio.ensure_future(self._to_box.get())
                if recv in done:
                    try:
                        frame = recv.result()
                    except Exception:
                        # the box dropped the socket without an X — the shell is gone
                        self._events.put_nowait(("E", "the machine closed the session"))
                        break
                    if frame:
                        tag, payload = frame[:1], frame[1:]
                        if tag == b"O":
                            self._touch()
                            self._events.put_nowait(("O", bytes(payload)))
                        elif tag == b"E":
                            self._events.put_nowait(("E", payload.decode("utf-8", "replace").rstrip("\n")))
                        elif tag == b"X":
                            try:
                                code = int(payload.decode() or "0")
                            except ValueError:
                                code = 0
                            break
                    recv = asyncio.ensure_future(ws.receive_bytes())
        finally:
            for t in (recv, outq):
                if not t.done():
                    t.cancel()
            self._finish(code)

    @classmethod
    def refused(cls, *, server: str, op: str, session_id: str, reason: str, code: int = EXIT_REFUSED):
        """A session that never reached the box: reads as E + X the way the box refuses, so
        every caller handles one shape whichever wall said no."""
        s = cls(server=server, op=op, session_id=session_id, spec={}, user="")
        s._events.put_nowait(("E", reason))
        s._finish(code)
        return s


# ── Opening one ───────────────────────────────────────────────────────────────

async def _offered_key(pool, eid: int) -> str:
    """Our own signing key for the S frame's verify_pubkey, standard base64 of the
    raw 32 bytes nac_stepup.entity_verify_pubkey returns. "" when it cannot be read:
    the box then falls back to whatever key it pinned before."""
    from server.network import nac_stepup
    try:
        raw_key = await nac_stepup.entity_verify_pubkey(pool, eid)
        return base64.b64encode(raw_key).decode() if raw_key else ""
    except Exception as e:
        log.info("session: could not read our verify key (%s) — frame carries none", type(e).__name__)
        return ""


def _session_user(op: str) -> str:
    # The box spawns session:open as its unprivileged session user and session:root as root
    # (session.go). It does not report the name in-band, so the status line says which kind.
    return "root" if op == SESSION_ROOT_OP else "session user (unprivileged)"


async def host_gate(pool, eid: int, server: str, op: str):
    """The pod-side gate, on its own so it can run BEFORE anything is asked of the user.
    Returns (grant, None) when `op` on `server` may go ahead, else (None, reason) — and
    the refusal is audited here (denied-user when the user's own deny said no, denied-pod
    otherwise).

    A SHELL IS THE PERSON'S OWN ACT: a standing
    Host grant listing session:open / session:root is no longer REQUIRED. What still
    refuses: a suspended or quarantined machine, and a DENY in any of the user's grants
    (deny beats everything, as in run_mgmt_op). When a grant DOES list the op it is
    returned so the S frame can carry it — a machine on a bastion older than 0.39.0
    requires one — and an empty dict when none does. The machine's own checks are the
    authority: root = the code at the machine; session:open on an anchored box = the
    person's approval bound to this session; on a local box = the pairing itself."""
    from server.network import nac_mgmt, nac_stepup
    verify_pubkey = None
    try:
        verify_pubkey = await nac_stepup.entity_verify_pubkey(pool, eid)
    except Exception:
        verify_pubkey = None
    suspended = await nac_mgmt.load_suspended_servers(pool)
    quarantined = await nac_mgmt.load_quarantined(pool)
    held = server in suspended or server in quarantined
    grants = await nac_mgmt.load_mgmt_grants(pool, verify_pubkey)
    denied = None if held else nac_mgmt.mgmt_denied_by(grants, server, op)
    if not held and denied is None:
        grant = await signed_grant_for(pool, verify_pubkey, server, op)
        return (grant or {}), None
    reason = ("server is quarantined" if server in quarantined
              else "server is suspended" if server in suspended
              else nac_mgmt.deny_reason(denied))
    await nac_mgmt.audit_mgmt(pool, eid, server, op, exit_code=EXIT_REFUSED,
                              decision="denied-user" if denied is not None else "denied-pod")
    return None, f"a shell on '{server}' is not permitted — {reason}"


async def begin_root(pool, eid: int, server: str, session_id: str, *, note: str = "",
                     existing: Optional[dict] = None) -> dict:
    """The first step of a root shell: the HOST GATE, then (only if it passes) the step-up
    request on the machine. Order matters — filing the request first would let any caller
    with write:network ring the user's second factor with 'open a ROOT shell' for a
    server that holds no session:root grant at all. Returns
    {"state": "refused", "detail"} or request_root_approval's shape."""
    server = (server or "").strip()
    _grant, reason = await host_gate(pool, eid, server, SESSION_ROOT_OP)
    if reason is not None:
        return {"state": "refused", "detail": reason}
    return await request_root_approval(pool, eid, server, session_id, note=note, existing=existing)


async def begin(pool, eid: int, server: str, op: str, session_id: str, *, note: str = "",
                existing: Optional[dict] = None) -> dict:
    """The first step of ANY shell: the HOST GATE, then the approval the box will
    require. session:root — the step-up at the machine (begin_root, unchanged: root is
    approved with the code there and never from a device). session:open on a box that
    gates writes (bastion 0.33.0) — the person's approval from their device or the
    machine's console, bound to this session id (nac_approval.obtain, purpose
    mgmt_op|session:open|<server>|<digest of [session_id]>); on any other box
    {"state": "approved", "approval": ""}. Shapes: {"state": "refused"|"pending"|
    "approved"|"denied"|"expired"|"offline"|"not_enrolled"|"no_way", ...}."""
    server = (server or "").strip()
    if op == SESSION_ROOT_OP:
        return await begin_root(pool, eid, server, session_id, note=note, existing=existing)
    _grant, reason = await host_gate(pool, eid, server, SESSION_OPEN_OP)
    if reason is not None:
        return {"state": "refused", "detail": reason}
    from server.network import nac_approval
    appr = await nac_approval.obtain(pool, eid, server, SESSION_OPEN_OP, [session_id],
                                     note=note or "open a shell", wait_s=0)
    if appr["state"] == "not_needed":
        return {"state": "approved", "approval": ""}
    if appr["state"] == "pending":
        return {"state": "pending", "request": appr["request"], "detail": appr.get("next")}
    return appr


async def poll(pool, eid: int, server: str, op: str, session_id: str, *, existing: dict) -> dict:
    """begin() again for a request already filed: reads its answer."""
    if op == SESSION_ROOT_OP:
        return await request_root_approval(pool, eid, server, session_id, existing=existing)
    from server.network import nac_approval
    appr = await nac_approval.obtain(pool, eid, server, SESSION_OPEN_OP, [session_id], wait_s=0)
    if appr["state"] == "not_needed":
        return {"state": "approved", "approval": ""}
    if appr["state"] == "pending":
        return {"state": "pending", "request": appr["request"], "detail": appr.get("next")}
    return appr


# ── Live sessions — what is open right now, from either door ──────────────────
# The app opens through open_session (the host's `session` tool is gone, 2026-09-24:
# a Host holds no shell on a machine), so one registry sees
# every shell. The app lists them and can close any of them from the host side; there
# is no attach. Process-local on purpose: a session IS a socket in this process.

_LIVE: dict = {}   # session_id -> BastionSession


def list_live() -> list:
    out = []
    for s in list(_LIVE.values()):
        if s.closed:
            _LIVE.pop(s.session_id, None)
            continue
        out.append({"session_id": s.session_id, "server": s.server, "op": s.op,
                    "origin": s.origin, "user": s.user, "started_at": s.started,
                    "last_activity": s.last_activity})
    out.sort(key=lambda r: r["started_at"])
    return out


def close_live(session_id: str) -> bool:
    """Close one open session from the host side. True if it was open."""
    s = _LIVE.pop((session_id or "").strip(), None)
    if s is None or s.closed:
        return False
    s.close()
    return True


async def open_session(pool, eid: int, server: str, op: str, *, cols: int = 80, rows: int = 24,
                       shell: str = "", approval: str = "", session_id: Optional[str] = None,
                       origin: str = "app") -> BastionSession:
    """Open a shell on `server`. Gate first, in the pod, exactly as run_mgmt_op does —
    default-DENY on the user's SIGNED grant, suspended / quarantined short-circuit, deny
    beats allow — then take one idle harness worker for that bastion and send S with the
    SAME signed terms the mgmt lane enforces. A refusal comes back as a session whose only
    events are E + X 126 (the box's own shape); missing input / no bastion / no worker raise
    nac_mgmt.MgmtError like run_mgmt_op. Every open is audited (decision 'opened' or the
    denied-* reason); the close is audited when the box sends X. `origin` is who opened it
    ('app' = the user in the Terminal app; 'host-tool' was the host's `session` tool, gone 2026-09-24) and is
    what the live list shows."""
    from server.network import nac_mgmt
    server = (server or "").strip()
    op = (op or "").strip()
    if not server or op not in SESSION_OPS:
        raise nac_mgmt.MgmtError(400, "server and op (session:open | session:root) required")
    session_id = session_id or new_session_id()
    if not valid_session_id(session_id):
        raise nac_mgmt.MgmtError(400, "session_id must be 8-64 characters of A-Z a-z 0-9 . _ -")
    grant, reason = await host_gate(pool, eid, server, op)
    if reason is not None:
        return BastionSession.refused(server=server, op=op, session_id=session_id, reason=reason)
    if op == SESSION_ROOT_OP and not (approval or "").strip():
        # The box would refuse this too (126: root without step-up). Say so here without
        # spending a worker, and record it the way the box would.
        await nac_mgmt.audit_mgmt(pool, eid, server, op, exit_code=EXIT_REFUSED, decision="denied-pod")
        return BastionSession.refused(server=server, op=op, session_id=session_id,
                                      reason=f"a root shell on '{server}' needs your approval on that "
                                             "machine first (no step-up approval was given)")
    token_id = await pool.fetchval(
        "SELECT token_id FROM network_bastion_heartbeat WHERE hostname=$1 "
        "ORDER BY last_seen_at DESC LIMIT 1", server)
    if token_id is None:
        raise nac_mgmt.MgmtError(404, f"no bastion registered for server '{server}'")
    from server.network.nac_harness import get_harness_relay
    relay = get_harness_relay()
    # Only a worker the box announced as `lane=session` may carry a shell. A bastion that
    # dials no session workers (older than the session lane) gets a plain answer, not a
    # harness worker quietly borrowed from a run.
    if relay.worker_count(token_id, SESSION_LANE) == 0:
        await nac_mgmt.audit_mgmt(pool, eid, server, op, exit_code=None, decision="no-worker")
        raise nac_mgmt.MgmtError(502, f"no session worker connected for '{server}' — the machine's "
                                      "bastion may need updating to one that offers terminal sessions")
    try:
        cols, rows = int(cols or 0), int(rows or 0)
    except (TypeError, ValueError):
        cols, rows = 0, 0
    # Our own signing key, read here because host_gate keeps its copy local.
    # Best-effort: if it cannot be read the frame simply carries "" and the box
    # falls back to whatever a DCN sync pinned — today's behaviour exactly.
    # entity_verify_pubkey returns RAW 32 BYTES (nac_stepup), and the box decodes
    # this field with base64 — so it is encoded here. Putting the raw bytes in
    # made the spec unserialisable and NO S frame was sent at all, which broke
    # every terminal rather than just the fleet ones. Caught by the session-lane
    # gate; the contract was one read away.
    verify_pubkey = await _offered_key(pool, eid)
    spec = {
        "op": op,
        "shell": (shell or "").strip(),
        "cols": cols if cols > 0 else 80,
        "rows": rows if rows > 0 else 24,
        # The grant rides only when one lists the op: a box on 0.39.0
        # does not require it; an older one does, and this keeps the Terminal
        # working through the roll until the standing grant is revoked.
        "grant_terms_b64": base64.b64encode(grant["terms_bytes"]).decode() if grant.get("terms_bytes") else "",
        "grant_sig_b64": grant.get("signature") or "",
        "session_id": session_id,
        "approval": (approval or "").strip(),
        # OUR OWN SIGNING KEY, so a box that has never synced a DCN store grant
        # can still pin it and verify the grant above.
        #
        # The box re-verifies every session grant against a key IT pinned, and
        # until bastion 0.24.1 the only writer of that pin was a DCN store sync.
        # So "let this host open a terminal here" required "give this host a
        # FOLDER here" — two different permissions welded together by where the
        # key happened to live. Measured 2026-09-13: 19 of 20 online fleet
        # machines held zero DCN grants, so no terminal could open on any of
        # them, and the box's refusal read "sync a grant first", which sounds
        # like advice and is actually a coupling.
        #
        # Sending it is safe and adds no authority: it is a PUBLIC key, the box
        # TOFU-pins it exactly as it pins the DCN one, and a key disagreeing
        # with one already pinned is refused rather than re-pinned. An older
        # bastion ignores the unknown field and behaves as before.
        "verify_pubkey": verify_pubkey,
    }
    session = BastionSession(server=server, op=op, session_id=session_id, spec=spec,
                             user=_session_user(op), origin=origin)

    async def _on_exit(s: BastionSession):
        _LIVE.pop(s.session_id, None)
        await nac_mgmt.audit_mgmt(pool, eid, server, op, exit_code=s.exit, decision="closed")
    session._on_exit = _on_exit

    if not await relay.acquire(token_id, session.drive, timeout=WORKER_WAIT_S, lane=SESSION_LANE):
        await nac_mgmt.audit_mgmt(pool, eid, server, op, exit_code=None, decision="no-worker")
        raise nac_mgmt.MgmtError(502, f"no idle harness worker for '{server}' (both may be busy)")
    _LIVE[session_id] = session
    await nac_mgmt.audit_mgmt(pool, eid, server, op, exit_code=None, decision="opened")
    return session


# ── Root: the step-up ON THE BOX ──────────────────────────────────────────────

async def request_root_approval(pool, eid: int, server: str, session_id: str, *,
                                note: str = "", existing: Optional[dict] = None) -> dict:
    """File (or poll) the step-up request a root shell needs. The user approves it on the
    machine (`cseries-bastion approvals` / `approve <id>`); the pod never sees the code.
    Returns nac_stepup.box_confirmation's shape: {"state": "pending", "request": {...}} |
    {"state": "approved", "approval": "<id>", ...} | {"state": "not_enrolled" | "denied" |
    "expired" | "offline", "detail": ...}."""
    from server.network import nac_stepup
    return await nac_stepup.box_confirmation(
        pool, eid, server=server, purpose=root_purpose(server, session_id),
        details=root_request_details(server, session_id, note), existing=existing)


# ── Root commands: one command, no terminal (bastion 0.49.0) ──────────────────
#
# The box's contract (client-go/rootexec.go), on a session worker:
#
#   host -> box  'S' + {"op": "session:root:exec", "session_id", "command",
#                       "deadline_s", "max_output_bytes", "verify_pubkey"
#                       [, "grant_terms_b64", "grant_sig_b64"]}
#   box -> host  'J' + {"exit", "duration_ms", "timed_out", "deadline_s",
#                       "max_output_bytes", "stdout", "stderr", "stdout_bytes",
#                       "stderr_bytes", "stdout_truncated", "stderr_truncated",
#                       "user", "session_id"}            only when it ran
#                'E' + text                              a refusal or notice
#                'X' + decimal exit                      last
#
# The box runs it only for its ANCHOR host, under the standing grant the person
# gave at that machine with the code (`privatae-bastion root-exec grant`), and
# records every command whole there. Nothing here can create that grant. An older
# bastion refuses the op as "not a session op" (126).

EXEC_DEFAULT_DEADLINE_S = 600
EXEC_MAX_DEADLINE_S = 8 * 3600        # the box's own ceiling (rootExecMaxDeadline)
EXEC_DEFAULT_OUTPUT = 256 * 1024      # per stream (rootExecDefaultOutput)
EXEC_MAX_OUTPUT = 4 * 1024 * 1024     # the box's ceiling (rootExecMaxOutput)
EXEC_COMMAND_MAX = 64 * 1024          # rootExecCommandMax
# Past the command's own deadline, how long to wait for the box's answer before
# calling the worker lost: the box kills the group with a 3 s grace and then answers.
EXEC_ANSWER_GRACE_S = 60.0


def _bounded_int(v, default: int, lo: int, hi: int) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    if n <= 0:
        return default
    return max(lo, min(hi, n))


async def run_root_exec(pool, eid: int, server: str, command: str, *,
                        deadline_s=None, max_output_bytes=None,
                        session_id: Optional[str] = None) -> dict:
    """Run ONE command as root on `server` through its bastion's session lane.

    The pod-side gate first (host_gate: a suspended or quarantined machine, or a
    deny of session:root:exec in any of the user's grants, refuses before any
    frame), then one idle session worker, one S frame, and the box's answer.
    Every outcome is audited in nac_mgmt_audit under op session:root:exec.

    Returns {"state": "ran"|"refused"|"bad_request"|"no_worker"|"lost",
             "exit": int|None, "result": <the box's J>|None, "detail": str,
             "session_id": str}."""
    from server.network import nac_mgmt
    server = (server or "").strip()
    sid = session_id or new_session_id()
    out = {"state": "bad_request", "exit": None, "result": None, "detail": "", "session_id": sid}
    cmd = command if isinstance(command, str) else ""
    if not server:
        out["detail"] = "machine required"
        return out
    if not cmd.strip() or len(cmd.encode("utf-8", "replace")) > EXEC_COMMAND_MAX or "\x00" in cmd:
        out["detail"] = f"command required: 1 to {EXEC_COMMAND_MAX} bytes of text, no NUL"
        return out
    if not valid_session_id(sid):
        out["detail"] = "session_id must be 8-64 characters of A-Z a-z 0-9 . _ -"
        return out
    grant, reason = await host_gate(pool, eid, server, SESSION_ROOT_EXEC_OP)
    if reason is not None:
        out.update(state="refused", exit=EXIT_REFUSED,
                   detail=reason.replace("a shell on", "root commands on", 1))
        return out
    token_id = await pool.fetchval(
        "SELECT token_id FROM network_bastion_heartbeat WHERE hostname=$1 "
        "ORDER BY last_seen_at DESC LIMIT 1", server)
    if token_id is None:
        out.update(state="refused", detail=f"no bastion registered for machine '{server}'")
        await nac_mgmt.audit_mgmt(pool, eid, server, SESSION_ROOT_EXEC_OP, exit_code=None,
                                  decision="no-bastion")
        return out
    from server.network.nac_harness import get_harness_relay
    relay = get_harness_relay()
    if relay.worker_count(token_id, SESSION_LANE) == 0:
        await nac_mgmt.audit_mgmt(pool, eid, server, SESSION_ROOT_EXEC_OP, exit_code=None,
                                  decision="no-worker")
        out.update(state="no_worker", detail=f"no session worker connected for '{server}' — its "
                                             "bastion may be offline or older than root commands (0.49.0)")
        return out
    deadline = _bounded_int(deadline_s, EXEC_DEFAULT_DEADLINE_S, 1, EXEC_MAX_DEADLINE_S)
    cap = _bounded_int(max_output_bytes, EXEC_DEFAULT_OUTPUT, 1024, EXEC_MAX_OUTPUT)
    spec = {
        "op": SESSION_ROOT_EXEC_OP,
        "session_id": sid,
        "command": cmd,
        "deadline_s": deadline,
        "max_output_bytes": cap,
        "verify_pubkey": await _offered_key(pool, eid),
        # A grant rides only when one lists the op; the box verifies it and a
        # deny in it refuses. The box's own standing grant is what admits.
        "grant_terms_b64": base64.b64encode(grant["terms_bytes"]).decode() if grant.get("terms_bytes") else "",
        "grant_sig_b64": grant.get("signature") or "",
    }
    loop = asyncio.get_event_loop()
    answer: "asyncio.Future" = loop.create_future()

    async def drive(ws) -> None:
        got = {"J": None, "E": [], "X": None}
        try:
            await ws.send_bytes(b"S" + json.dumps(spec).encode())
            while True:
                frame = await ws.receive_bytes()
                if not frame:
                    continue
                tag, payload = frame[:1], frame[1:]
                if tag == b"J":
                    try:
                        got["J"] = json.loads(payload.decode("utf-8", "replace"))
                    except ValueError:
                        got["E"].append("the machine sent an unreadable result")
                elif tag == b"E":
                    got["E"].append(payload.decode("utf-8", "replace").rstrip("\n"))
                elif tag == b"X":
                    try:
                        got["X"] = int(payload.decode() or "0")
                    except ValueError:
                        got["X"] = 1
                    break
        except Exception as e:  # the box dropped the socket without an X
            got["E"].append(f"the machine closed the connection ({type(e).__name__})")
        if not answer.done():
            answer.set_result(got)

    if not await relay.acquire(token_id, drive, timeout=WORKER_WAIT_S, lane=SESSION_LANE):
        await nac_mgmt.audit_mgmt(pool, eid, server, SESSION_ROOT_EXEC_OP, exit_code=None,
                                  decision="no-worker")
        out.update(state="no_worker", detail=f"no idle session worker for '{server}' (all may be busy)")
        return out
    try:
        got = await asyncio.wait_for(asyncio.shield(answer), deadline + EXEC_ANSWER_GRACE_S)
    except asyncio.TimeoutError:
        await nac_mgmt.audit_mgmt(pool, eid, server, SESSION_ROOT_EXEC_OP, exit_code=None,
                                  decision="lost")
        out.update(state="lost", detail=f"no answer from '{server}' within the command's deadline "
                                        f"({deadline}s) and {int(EXEC_ANSWER_GRACE_S)}s more")
        return out
    code = got["X"]
    out["exit"] = code
    out["detail"] = "\n".join(e for e in got["E"] if e)
    if got["J"] is not None:
        out.update(state="ran", result=got["J"])
        decision = ("timed-out" if got["J"].get("timed_out")
                    else "stopped" if got["J"].get("stopped") else "ran")
    elif code == EXIT_REFUSED:
        out["state"] = "refused"
        decision = "refused-by-machine"
    else:
        out["state"] = "lost" if code is None else "refused"
        decision = "lost" if code is None else "failed"
    await nac_mgmt.audit_mgmt(pool, eid, server, SESSION_ROOT_EXEC_OP, exit_code=code, decision=decision)
    return out
