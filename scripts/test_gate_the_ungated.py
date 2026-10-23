#!/usr/bin/env python3
"""test_gate_the_ungated.py — host-authored ops: gate the ungated: no AI on the machines, and no probing for access when a
call fails.

Three lanes onto a user's machine had no signed grant behind them, and one of
them (`ctx_extract`) is what the host reached for after a refused
read. This pins:

  1. `ctx_extract` runs ONLY on a machine the user's signed grant names for it
     — the same pod gate `mgmt_run` runs (nac_mgmt.gate_op / gate_servers),
     audited the same way (exit 126 on refusal, the box's exit on a run). A
     machine with the engine and no grant is NAMED as needing the grant, in
     the read error, in op='extract_machines' and in the machine-list route —
     so the host drafts a grant for the user instead of trying the machine. A
     refused extraction reaches the tool loop as status 'denied'.
  2. The host's `session` tool (a command through a shell the box opened on
     itself) is gone: schema, caps, executor, run_command, skill guidance.
  3. `compute_job` (SSH from the pod to a registered connection, default user
     root, no host-key check, no grant) is gone, with the SSH connection kind
     and the asyncssh dependency that existed for it.

Container-free: real gate code on stub pools, source pins for the removals.
Run: APP_ROOT=<repo> python3 scripts/test_gate_the_ungated.py
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import types
from pathlib import Path
from tools_source import tools_source  # tools.py and the modules split out of it

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "brain"))

failures: list[str] = []


def check(name: str, cond: bool, detail="") -> None:
    print(("  OK   " if cond else "  FAIL ") + name + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def src(rel: str) -> str:
    return (ROOT / rel).read_text()


# ── 1a. the gate itself, on a stub pool ──────────────────────────────────
from server.network import nac_mgmt  # noqa: E402

print("the gate")


class _NoGrantPool:
    """No grants, no holds — the fleet's state for ctx_extract today."""

    async def fetch(self, sql, *a):
        return []

    async def fetchrow(self, sql, *a):
        return None

    async def fetchval(self, sql, *a):
        return None

    async def execute(self, sql, *a):
        return "INSERT 0 1"


nac_mgmt_stepup = types.ModuleType("server.network.nac_stepup")


async def _no_key(pool, eid):
    return None
nac_mgmt_stepup.entity_verify_pubkey = _no_key
sys.modules["server.network.nac_stepup"] = nac_mgmt_stepup

ok, why, decision = asyncio.run(nac_mgmt.gate_op(_NoGrantPool(), 9003, "web-1", "ctx_extract"))
check("gate_op: no signed grant → refused, in the gate's own words, audited as denied-pod",
      ok is False and why == "no signed grant (default-deny)" and decision == "denied-pod", (ok, why, decision))
g = asyncio.run(nac_mgmt.gate_servers(_NoGrantPool(), 9003, "ctx_extract", ["web-1", "web-2", None]))
check("gate_servers: one load, every server decided, empties skipped",
      set(g) == {"web-1", "web-2"} and all(v[0] is False for v in g.values()), g)
check("gate_op is the SAME decision mgmt_run runs (mgmt_decision + the holds), not a second gate",
      "def _decide(" in src("server/network/nac_mgmt.py")
      and "allowed, reason, decision = _decide(grants, suspended, quarantined, eid, server, op)" in src("server/network/nac_mgmt.py")
      and "return _decide(grants, suspended, quarantined, eid, (server or \"\").strip(), (op or \"\").strip())" in src("server/network/nac_mgmt.py"))
# The positive side of mgmt_decision is pinned by scripts/check_nac_mgmt.py
# (granted op on the granted server ⇒ allow); gate_op adds only the load.

# ── 1b. the extraction lane runs the gate before any machine is asked ────
from server.routes import context_extract as ce  # noqa: E402

print("\nthe extraction lane")
ENTITY, CTX_ID = 9003, 480
PNG = b"\x89PNG\r\n\x1a\n" + os.urandom(64)
OCR = {"server": "laptop.example", "token_id": 11, "tools": ["ocr", "pdf-text"], "prep_tools": [], "pull": False}
OCR2 = {"server": "desk.example", "token_id": 12, "tools": ["ocr"], "prep_tools": [], "pull": False}


class _Conn:
    """The writer's connection (test_host_read_lane's shape): rows land in the sink."""

    def __init__(self, sink):
        self.sink = sink

    def transaction(self):
        conn = self

        class _T:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False
        return _T()

    async def execute(self, sql, *args):
        return "DELETE 0"

    async def fetchval(self, sql, *args):
        if "count(*)" in sql:
            return 0
        self.sink.setdefault("rows", []).append(args)
        return 900 + len(self.sink["rows"])


class _Pool:
    def __init__(self, sink, row):
        self.sink, self.row = sink, row

    async def fetchrow(self, sql, *args):
        return self.row if "FROM user_context" in sql else None

    async def fetch(self, sql, *args):
        return []

    async def fetchval(self, sql, *args):
        return None

    async def execute(self, sql, *args):
        return "UPDATE 0"

    def acquire(self):
        conn = _Conn(self.sink)

        class _A:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False
        return _A()


def _lanes(sink, machines, granted: set):
    class _Relay:
        def worker_count(self, tid):
            return 1

        async def dispatch(self, tid, op, args, wait, approval=""):  # (+ approval since 0.33.0)
            sink["dispatched"] = (tid, op, json.loads(args[0]))
            return (json.dumps({"text": "MACHINE TEXT", "pages": 1}), "", 0)
    sys.modules.setdefault("server.main", types.ModuleType("server.main"))
    # The lane imports nac_mgmt by package attribute, and this file already
    # imported the real one — so the real module's seams are patched (and
    # restored by the caller), not a stand-in put in sys.modules.
    nm = nac_mgmt
    nm.get_mgmt_relay = lambda: _Relay()

    async def _gate_servers(p, eid, op, servers):
        sink.setdefault("gated", []).append((op, [s for s in servers if s]))
        return {str(s): ((True, "", "ran") if s in granted else (False, "no signed grant (default-deny)", "denied-pod"))
                for s in servers if s}
    nm.gate_servers = _gate_servers

    async def _audit(p, eid, server, op, *, exit_code, decision):
        sink.setdefault("audit", []).append((server, op, exit_code, decision))
    nm.audit_mgmt = _audit
    sys.modules["server.network.nac_mgmt"] = nm
    emb = types.ModuleType("server.routes.embed")

    async def _enqueue(*a, **k):
        return {"id": 1}
    emb.enqueue_job = _enqueue
    sys.modules["server.routes.embed"] = emb

    async def _machines(p, eid, t=None):
        return [dict(m) for m in machines if t is None or t in m["tools"]]
    return _machines


def _row(mime="image/png", fname="scan.png"):
    return {"id": CTX_ID, "title": fname, "content": base64.b64encode(PNG).decode(),
            "storage_backend": "pg", "tags": [],
            "metadata": json.dumps({"kind": "file", "mime": mime, "from_file": fname,
                                    "size_bytes": len(PNG), "upload_batch": "b1"})}


_REAL_NM = {k: getattr(nac_mgmt, k) for k in ("get_mgmt_relay", "gate_servers", "audit_mgmt")}


def _restore():
    for k, v in _REAL_NM.items():
        setattr(nac_mgmt, k, v)


async def _extract(machines, granted, server=None):
    sink = {"dispatched": None}
    real = ce.machines_for
    ce.machines_for = _lanes(sink, machines, granted)
    try:
        res = await ce.extract_context_file(_Pool(sink, _row()), ENTITY, CTX_ID, server=server)
    finally:
        ce.machines_for = real
        _restore()
    return res, sink


res, sink = asyncio.run(_extract([OCR], granted=set()))
check("a machine with the engine and NO grant: refused, nothing dispatched",
      res.get("refused") is True and sink["dispatched"] is None, res)
check("… the refusal is the gate's words and names the draft as the next step",
      "not permitted — laptop.example: no signed grant (default-deny)" in res.get("error", "") and "propose_mgmt_grant" in res.get("error", ""), res)
check("… and it is audited as a denied ctx_extract on that machine (exit 126)",
      ("laptop.example", "ctx_extract", 126, "denied-pod") in sink.get("audit", []), sink.get("audit"))
check("… the gate was asked for ctx_extract on the machines that have the engine",
      sink.get("gated") and sink["gated"][0][0] == "ctx_extract" and "laptop.example" in sink["gated"][0][1], sink.get("gated"))

res, sink = asyncio.run(_extract([OCR], granted={"laptop.example"}))
check("the same machine WITH a grant: dispatched, text filed",
      res.get("ok") and sink["dispatched"] and sink["dispatched"][1] == "ctx_extract", res)
check("… and the run is audited with the box's exit",
      ("laptop.example", "ctx_extract", 0, "ran") in sink.get("audit", []), sink.get("audit"))

res, sink = asyncio.run(_extract([OCR, OCR2], granted={"desk.example"}))
check("two machines, one granted: the GRANTED one is chosen, never the first",
      res.get("ok") and sink["dispatched"][0] == 12, (res, sink["dispatched"]))

res, sink = asyncio.run(_extract([OCR, OCR2], granted={"desk.example"}, server="laptop.example"))
check("a named machine without a grant is refused by name, not swapped for a granted one",
      res.get("refused") and res.get("server") == "laptop.example" and sink["dispatched"] is None, res)

# The pdf-text → pdf-ocr upgrade must never trade a GRANTED
# machine for the asked job for an ungranted one that has the better job.
PDFTXT = {"server": "granted.example", "token_id": 21, "tools": ["pdf-text"], "prep_tools": [], "pull": False}
PDFOCR = {"server": "ocr-only.example", "token_id": 22, "tools": ["pdf-ocr"], "prep_tools": [], "pull": False}


async def _extract_pdf(machines, granted, tool="pdf-text"):
    sink = {"dispatched": None}
    real = ce.machines_for
    ce.machines_for = _lanes(sink, machines, granted)
    row = _row(mime="application/pdf", fname="scan.pdf")
    try:
        res = await ce.extract_context_file(_Pool(sink, row), ENTITY, CTX_ID, tool=tool)
    finally:
        ce.machines_for = real
        _restore()
    return res, sink


res, sink = asyncio.run(_extract_pdf([PDFTXT, PDFOCR], granted={"granted.example"}))
check("the upgrade to pdf-ocr happens only onto an APPROVED machine: the granted pdf-text machine runs the job",
      res.get("ok") and sink["dispatched"] and sink["dispatched"][0] == 21 and sink["dispatched"][2]["tool"] == "pdf-text",
      (res, sink["dispatched"]))
res, sink = asyncio.run(_extract_pdf([PDFTXT, PDFOCR], granted={"ocr-only.example"}))
check("… and onto the granted pdf-ocr machine when that is the approved one",
      res.get("ok") and sink["dispatched"] and sink["dispatched"][0] == 22 and sink["dispatched"][2]["tool"] == "pdf-ocr",
      (res, sink["dispatched"]))

# The audit carries the gate's OWN decision (a user's deny is
# audited as denied-user, as run_mgmt_op would), not a guess from the wording.


def _lanes_user_deny(sink, machines):
    fn = _lanes(sink, machines, set())

    async def _gate_servers(p, eid, op, servers):
        return {str(s): (False, "denied by your grant (deny beats allow)", "denied-user") for s in servers if s}
    nac_mgmt.gate_servers = _gate_servers
    return fn


async def _extract_user_deny(server=None):
    sink = {"dispatched": None}
    real = ce.machines_for
    ce.machines_for = _lanes_user_deny(sink, [OCR])
    try:
        res = await ce.extract_context_file(_Pool(sink, _row()), ENTITY, CTX_ID, server=server)
    finally:
        ce.machines_for = real
        _restore()
    return res, sink



# After a scan came back blank, the note names an
# OCR machine as a retry ONLY when the gate would let it run; an ungranted one is
# named as needing the grant. When nothing does the asked job, an ungranted
# machine with the better job is named with the missing grant, never "install
# the tool". And a job swapped in for one whose machines lacked a grant says so.
SCAN_PDF_OK = {"server": "granted.example", "token_id": 21, "tools": ["pdf-text"], "prep_tools": [], "pull": False}


def _lanes_blank(sink, machines, granted):
    fn = _lanes(sink, machines, granted)

    class _Relay:
        def worker_count(self, tid):
            return 1

        async def dispatch(self, tid, op, args, wait, approval=""):  # (+ approval since 0.33.0)
            sink["dispatched"] = (tid, op, json.loads(args[0]))
            return (json.dumps({"text": "", "pages": 3, "pages_ocr": 0}), "", 0)
    nac_mgmt.get_mgmt_relay = lambda: _Relay()
    return fn


async def _extract_blank(machines, granted):
    sink = {"dispatched": None}
    real = ce.machines_for
    ce.machines_for = _lanes_blank(sink, machines, granted)
    try:
        return await ce.extract_context_file(_Pool(sink, _row(mime="application/pdf", fname="scan.pdf")),
                                             ENTITY, CTX_ID, tool="pdf-text"), sink
    finally:
        ce.machines_for = real
        _restore()


res, sink = asyncio.run(_extract_blank([SCAN_PDF_OK, PDFOCR], granted={"granted.example"}))
check("a blank scan never sends the host to retry on an UNGRANTED OCR machine — it names the grant",
      sink["dispatched"] and sink["dispatched"][0] == 21
      and "holds no signed grant" in (res.get("error") or res.get("note") or "")
      and "try again naming that machine" not in (res.get("error") or res.get("note") or ""), res)
res, sink = asyncio.run(_extract_blank([SCAN_PDF_OK, PDFOCR], granted={"granted.example", "ocr-only.example"}))
check("… and names a GRANTED OCR machine as the retry",
      "ocr-only.example can do it; try again naming that machine" in (res.get("error") or res.get("note") or ""), res)

res, sink = asyncio.run(_extract_pdf([PDFOCR], granted=set()))
check("nothing does the asked job, the better-job machine is ungranted: refused naming it and the grant, never 'install the tool'",
      res.get("refused") and "ocr-only.example" in res.get("error", "") and "install" not in res.get("error", ""), res)


async def _extract_clip(machines, granted):
    sink = {"dispatched": None}
    real = ce.machines_for
    ce.machines_for = _lanes(sink, machines, granted)
    row = _row(mime="video/mp4", fname="demo.mp4")
    # The frames WRITER is another lane's contract (test_context_frames_*); here
    # only the choice and its note are under test, so the writer is a stand-in.
    real_frames = ce._file_frames

    async def _frames(pool, eid, cid, payload, **kw):
        sink["framed"] = kw.get("tool")
        return {"ok": True, "frames": 0}
    ce._file_frames = _frames
    try:
        return await ce.extract_context_file(_Pool(sink, row), ENTITY, CTX_ID), sink
    finally:
        ce.machines_for = real
        ce._file_frames = real_frames
        _restore()


STT = {"server": "stt.example", "token_id": 31, "tools": ["av-transcribe"], "prep_tools": [], "pull": False}
FRAMES = {"server": "frames.example", "token_id": 32, "tools": ["video-frames"], "prep_tools": [], "pull": False}
mc = types.ModuleType("mastercode.media_engine")


async def _mcs(pool, eid):
    return ({"model": "m"}, False)
mc.media_config_state = _mcs
sys.modules["mastercode.media_engine"] = mc
res, sink = asyncio.run(_extract_clip([STT, FRAMES], granted={"frames.example"}))
mc_none = types.ModuleType("mastercode.media_engine")


async def _mcs_none(pool, eid):
    return (None, False)
mc_none.media_config_state = _mcs_none
sys.modules["mastercode.media_engine"] = mc_none
res, sink = asyncio.run(_extract_clip([STT, FRAMES], granted={"frames.example"}))
check("… and the media SETUP answer (no media model yet) carries the same note, so the user is not sent to the wrong fix",
      res.get("needs_setup") == "media_embedding" and "holds no signed grant" in (res.get("fallback_note") or "")
      and sink["dispatched"] is None, res)
sys.modules["mastercode.media_engine"] = mc
res, sink = asyncio.run(_extract_clip([STT, FRAMES], granted={"frames.example"}))
check("a job swapped in because the asked job's machines lacked a grant SAYS so",
      sink["dispatched"] and sink["dispatched"][0] == 32
      and "stt.example can do 'av-transcribe' but holds no signed grant" in (res.get("fallback_note") or ""), res)

for named in (None, "laptop.example"):
    res, sink = asyncio.run(_extract_user_deny(server=named))
    check(f"a user's deny is audited as denied-user ({'named' if named else 'unnamed'} machine) and refused in its words",
          ("laptop.example", "ctx_extract", 126, "denied-user") in sink.get("audit", [])
          and "denied by your grant" in res.get("error", "") and sink["dispatched"] is None, (res, sink.get("audit")))


async def _msg(mime, machines, granted):
    sink = {}
    real = ce.machines_for
    ce.machines_for = _lanes(sink, machines, granted)
    try:
        return await ce.no_reader_message(object(), ENTITY, "scan.png", mime, 77)
    finally:
        ce.machines_for = real
        _restore()


m = asyncio.run(_msg("image/png", [OCR], set()))
check("the read error names an ungranted machine as needing the grant — never as a route",
      "holds no signed grant for ctx_extract" in m and "op='extract'" not in m and "propose_mgmt_grant" in m, m)
m = asyncio.run(_msg("image/png", [OCR], {"laptop.example"}))
check("… and a granted one as the route", "op='extract' (context_id=77)" in m and "laptop.example" in m, m)
m = asyncio.run(_msg("image/png", [OCR, OCR2], {"desk.example"}))
check("… naming only the granted machine when both have the engine", "desk.example" in m and "laptop.example" not in m, m)

tools_src = tools_source()
check("tools.py: a refused extraction reaches the loop as status 'denied' with the gate's words",
      'if res.get("refused"):' in tools_src and '"status": "denied", "result": res["error"]' in tools_src)
check("tools.py: op='extract_machines' says which machines are granted (and why not)",
      "_nm.gate_servers(_pool, _bs._entity_id, _XOP," in tools_src and '_m["granted"] = _ok' in tools_src)
check("the machine-list route says the same", 'm["granted"] = ok' in src("server/routes/context_extract.py"))
check("context_extract.py: the gate runs before the choice, and the dispatch is audited",
      "gate = await _gated(machines)" in src("server/routes/context_extract.py")
      and src("server/routes/context_extract.py").index("gate = await _gated(machines)") < src("server/routes/context_extract.py").index("chosen = granted[0]")
      and 'exit_code=(got[2] if got else 124), decision="ran")' in src("server/routes/context_extract.py"))

# ── 2. no session tool ───────────────────────────────────────────────────
print("\nno shell for the host")
from mastercode import public_tool_registry as reg  # noqa: E402
from mastercode.runtime.meta_tool_caps import META_TOOL_CAPS  # noqa: E402
from server.network import session_lane  # noqa: E402

for name in ("session", "compute_job"):
    check(f"{name}: no schema", name not in reg.OPERATOR_PLATFORM_TOOL_SCHEMAS and name not in reg.PUBLIC_META_TOOL_NAMES)
    check(f"{name}: no caps", name not in META_TOOL_CAPS)
    check(f"{name}: no executor branch", f'elif name == "{name}":' not in tools_src)
check("_exec_session and _exec_compute_job are gone", "_exec_session" not in tools_src and "_exec_compute_job" not in tools_src)
check("session_lane: the tool's run_command is gone; the app's lane stays",
      not hasattr(session_lane, "run_command") and hasattr(session_lane, "open_session") and hasattr(session_lane, "begin_root"))
net = src("default-skills/network.md")
check("network.md: no session tool, and says why there is no shell",
      " session," not in net.split("\n")[3] and "## There is no shell — guidance" in net and "no AI on the machines" in net)

# ── 3. no SSH from the pod ───────────────────────────────────────────────
print("\nno SSH from the pod")
check("connect_compute registers no ssh target", '_COMPUTE_KINDS = {"local", "runpod"}' in tools_src
      and 'elif kind == "ssh":' not in tools_src and "kind='ssh'" not in src("brain/mastercode/public_tool_registry.py"))
check("asyncssh is no longer a dependency", "asyncssh" not in src("requirements.txt") and "asyncssh" not in tools_src)
keys = src("default-skills/keys.md")
check("keys.md: no compute_job, no ssh kind", "compute_job" not in keys and "kind='ssh'" not in keys and "There is no SSH kind" in keys)
check("capability coverage no longer lists them", "compute_job" not in src("scripts/check_capability_coverage.py")
      and '"session": ("operator_only"' not in src("scripts/check_capability_coverage.py"))
check("check_direct_ops retires both names", '"compute_job"' in src("scripts/check_direct_ops.py") and '"session",' in src("scripts/check_direct_ops.py"))

print(f"\n{len(failures)} failure(s)" if failures else "\nall checks passed")
sys.exit(1 if failures else 0)
