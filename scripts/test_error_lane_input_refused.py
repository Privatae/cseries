#!/usr/bin/env python3
"""A tool refusing the model's own input is the MODEL's outcome on the error
lane, not a platform fault (a live end-to-end test's errors_clean, on both Hosts).

Container-free: APP_ROOT=<repo> python3 scripts/test_error_lane_input_refused.py
No pod, no DB, no network, no model: a temp workspace, a stub pool, a scripted
model.

One live run failed errors_clean on entities 9163 and
164 with six "platform" error rows. Every one was a workspace_file answer to
input the model chose, run directly or through operate:

  {"op": "read", "path": "test-attach-6c6d4804.docx"}         file not found
  {"op": "read", "path": "canary.js"}                          file not found
  {"op": "write", "content": "...", "filePath": "app.yaml"}    path is required for op='write'
  {"op": "write", "content": "...", "path": "/api/applications/build"}
                                                               path traversal blocked

The ledger files a failed call as 'platform' when its ledger reason is ERROR,
and classify() reads every unstamped status "error" as ERROR — so a tool that
refuses bad input and a tool that broke were filed alike, against the ledger's own
rule (tool_call_ledger: "a refusal at a gate, bad or missing arguments, an
invented name" are the model's).

  1 the four calls above, through the real workspace_file, are refused AND
    filed 'model' (fails on 6a3a488a0: 'platform')
  2 the same through a real Host turn: the error row the lane writes carries
    fault 'model' (fails on 6a3a488a0)
  3 a failure of the tool itself stays 'platform': file_access raising, an
    I/O error inside a valid op, a mark that is not exactly True
  4 nothing else moved: an unstamped ERROR with no mark is 'platform', every
    other reason is 'model', and `reason` itself is unchanged by the mark

Exit 0 = all pass; exit 1 + FAIL lines otherwise.
"""

import asyncio
import json
import logging
import os
import sys
import tempfile

APP_ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_ROOT)
sys.path.insert(0, os.path.join(APP_ROOT, "brain"))
logging.basicConfig(level=logging.CRITICAL)

_WS = tempfile.mkdtemp(prefix="ws-input-refused-")
os.environ["CEIGAS_WORKSPACE"] = _WS          # read by file_access at import

FAILS: list[str] = []


def check(name, cond, extra=""):
    if cond:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"[input-refused] FAIL {name} {extra}")


from mastercode.runtime import tool_call_ledger as L  # noqa: E402
from mastercode.runtime import tools as T  # noqa: E402
from server.capabilities import file_access as FA  # noqa: E402

FA.WORKSPACE = __import__("pathlib").Path(_WS)


def lane_fault(res) -> str:
    """The fault the error lane files for this result: fault_class over the
    ledger's own reason, given the result (session._error_lane)."""
    reason = L.classify(res)
    try:
        return L.fault_class(reason, res)
    except TypeError:                     # a tree whose fault_class reads only the reason
        return L.fault_class(reason)


RUN_CALLS = [
    ({"op": "read", "path": "test-attach-6c6d4804.docx"}, "file not found"),
    ({"op": "read", "path": "canary.js"}, "file not found"),
    ({"intent": "x", "op": "write",
      "content": "name: Canary App\nversion: 1.0", "filePath": "app.yaml"},
     "path is required for op='write'"),
    ({"op": "write", "content": "{\"project_id\": 4}", "path": "/api/applications/build"},
     "path traversal blocked"),
]

# ── 1. the run's calls, through the real tool ──────────────────────────────
print("1 the run's four calls, through the real workspace_file")
for args, said in RUN_CALLS:
    a = {k: v for k, v in args.items() if k != "intent"}
    res = asyncio.run(T._exec_workspace_file(a))
    label = f"{a.get('op')} {a.get('path') or a.get('filePath')!r}"
    check(f"1: {label} is refused with the run's own words ({said!r})",
          isinstance(res, dict) and res.get("status") == "error"
          and said in str(res.get("result")), res)
    check(f"1: {label} is filed as the model's outcome, not a platform fault",
          lane_fault(res) == "model", f"fault={lane_fault(res)} res={res}")
    check(f"1: {label}'s ledger reason is still ERROR (only the fault class reads the mark)",
          L.classify(res) == L.ERROR, L.classify(res))

# The same tool on a file that IS there still answers ok (the mark is only on
# refusals).
with open(os.path.join(_WS, "present.txt"), "w") as fh:
    fh.write("here")
ok = asyncio.run(T._exec_workspace_file({"op": "read", "path": "present.txt"}))
check("1: a read of a file that is there answers ok, unmarked",
      ok.get("status") == "ok" and not ok.get(getattr(L, "INPUT_REFUSED", "input_refused")), ok)

# ── 2. a real Host turn: the row the error lane writes ─────────────────────
print("2 a Host turn: the error row for the attachment read")
from mastercode.runtime import session as S  # noqa: E402
from mastercode import provider_gateway as PG, inference_config as IC  # noqa: E402
from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS as P_REG  # noqa: E402
import server.error_log as EL  # noqa: E402

ROWS: list[dict] = []
_real_rtl = getattr(EL, "record_tool_result_later", None)


def _capture_rtl(pool, **kw):
    ROWS.append(kw)
    return None


async def _capture_record(pool, **kw):
    ROWS.append(kw)


class Pool:
    async def fetch(self, q, *a):
        return []

    async def fetchrow(self, q, *a):
        return None

    async def fetchval(self, q, *a):
        return None

    async def execute(self, q, *a):
        return "OK"

    def acquire(self):
        pool = self

        class _C:
            async def __aenter__(self):
                return pool

            async def __aexit__(self, *e):
                return False
        return _C()


SCRIPT = [
    {"content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {
        "name": "workspace_file",
        "arguments": json.dumps({"op": "read", "path": "test-attach-6c6d4804.docx"})}}]},
    {"content": "CODEWORD0000"},
    {"content": "(the script ran out)"},
]
CALLS: list = []


async def model(messages=None, *a, **kw):
    CALLS.append(1)
    r = SCRIPT.pop(0) if SCRIPT else {"content": "(the script ran out)"}
    return {"tokens_used": 1, "prompt_tokens": 1, "completion_tokens": 1,
            "tool_calls": None, **r}


async def route(pool, eid, role, thread_override=None, **kw):
    return {"provider": "openrouter", "model": "openai/gpt-4o-mini-2024-07-18"}


async def schemas(pool, eid, interaction_context=None, **kw):
    # The entity's catalog holds workspace_file; the turn advertises its
    # read-only variant and runs op='read' directly, as on the live Hosts.
    s = {k: v for k, v in P_REG["workspace_file"].items() if k != "_meta"}
    return [s]


from mastercode.runtime import meta_tool_caps as MTC  # noqa: E402
from server import brain_service as BS  # noqa: E402


async def _all_scopes(pool, entity_id, **kw):
    # The live Hosts hold read_context (they ran the read and got "file not
    # found"); this Host holds every entity scope, so the gate passes as there.
    return set(MTC.ENTITY_SCOPE_TO_BITS)


_saved = (S._call_entity_model, PG.call_model, IC.resolve_role_route,
          S._AVAILABLE.get("ceigas_domain_controller"), S._build_tool_schemas,
          _real_rtl, EL.record)
_saved_caps = (MTC._resolve_entity_scopes, BS.get_pool)
_POOL = Pool()
try:
    MTC._resolve_entity_scopes = _all_scopes
    BS.get_pool = lambda: _POOL
    S._call_entity_model = model
    PG.call_model = model
    IC.resolve_role_route = route
    S._AVAILABLE["ceigas_domain_controller"] = None
    S._build_tool_schemas = schemas
    if _real_rtl is not None:
        EL.record_tool_result_later = _capture_rtl
    EL.record = _capture_record
    rt = S.EntityRuntime(_POOL)

    async def ctx(entity_id, **kw):
        return S.EntitySession(
            session_id="sess-1", entity_id=entity_id,
            entity={"id": entity_id, "name": "Nova", "behavioral_params": {},
                    "permission_overrides": {"bot_capabilities": 255}},
            interaction_context="app:circles")
    rt._build_chat_context_transient = ctx

    async def _turn():
        out = await rt.send_message(
            entity_id=9163, app_id="circles",
            user_message="What is the code word in the attached document?")
        await asyncio.sleep(0.05)           # let a scheduled record land
        return out
    asyncio.run(_turn())
finally:
    MTC._resolve_entity_scopes, BS.get_pool = _saved_caps
    (S._call_entity_model, PG.call_model, IC.resolve_role_route,
     S._AVAILABLE["ceigas_domain_controller"], S._build_tool_schemas,
     _rtl_back, EL.record) = _saved
    if _rtl_back is not None:
        EL.record_tool_result_later = _rtl_back

ws_rows = [r for r in ROWS if r.get("tool") == "workspace_file"]
check("2: the turn ran the model's read (one error-lane row for workspace_file)",
      len(ws_rows) == 1, f"rows={ROWS}")
if ws_rows:
    _row = ws_rows[0]
    _said = str((_row.get("result") or {}).get("result") if isinstance(_row.get("result"), dict)
                else _row.get("message"))
    check("2: ...the read was the run's: 'file not found'", "file not found" in _said, _row)
    check("2: ...and its row is filed fault='model', not 'platform'",
          _row.get("fault") == "model", f"fault={_row.get('fault')!r}")

# ── 3. the tool's own failures stay platform ───────────────────────────────
print("3 a failure of the tool itself stays 'platform'")
_real_read = FA.read_file


def _boom(*a, **kw):
    raise OSError(5, "Input/output error")


FA.read_file = _boom
try:
    res = asyncio.run(T._exec_workspace_file({"op": "read", "path": "present.txt"}))
finally:
    FA.read_file = _real_read
check("3: file_access raising mid-read is an error", res.get("status") == "error", res)
check("3: ...filed 'platform'", lane_fault(res) == "platform", res)


def _io_error(*a, **kw):
    return {"status": "error", "error": "Input/output error"}


FA.read_file = _io_error
try:
    res = asyncio.run(T._exec_workspace_file({"op": "read", "path": "present.txt"}))
finally:
    FA.read_file = _real_read
check("3: an unmarked file_access error (an I/O failure in a valid op) is filed 'platform'",
      lane_fault(res) == "platform", res)
check("3: a mark that is not exactly True does not move the class",
      lane_fault({"status": "error", "result": "x", "input_refused": "yes"}) == "platform")

# ── 4. nothing else moved ──────────────────────────────────────────────────
print("4 the rest of the fault class is unchanged")
check("4: ERROR with no result is 'platform'", L.fault_class(L.ERROR) == "platform")
check("4: None (no gate spoke) is 'platform'", L.fault_class(None) == "platform")
for r in (L.NOT_ADVERTISED, L.UNKNOWN_NAME, L.ARGS_SCHEMA, L.BAD_ARGS_JSON, L.DEDUP,
          L.BLOCKED, L.POLICY_DENIED, L.CAPABILITY_DENIED, L.OUT_OF_GRANT):
    check(f"4: {r} is 'model'", L.fault_class(r) == "model")

if FAILS:
    print(f"\n{len(FAILS)} FAIL")
    sys.exit(1)
print("\nPASS: a tool refusing the model's own input is the model's outcome; a broken tool is still the platform's")
