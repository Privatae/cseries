#!/usr/bin/env python3
"""Two causes of a Host failing a simple file task, at the source.

Container-free: APP_ROOT=<repo> python3 scripts/test_attachment_read_and_redirect_refusal.py
No pod, no DB, no network, no model: a temp workspace, a stub pool, a scripted
model.

An attached document: its text reaches the model in the message, but a small
model calls workspace_file read on the attachment's name. Attachments are kept
in the context store (circles._keep_attachments -> context._ingest_binary_file,
user_context source 'file_upload'), never the workspace, so the read answered
a bare "file not found" and the model could not recover.

A write named directly: workspace_file op='write' is refused with only "run it
through operate(...)". The failure ledger then added "Tell the user what
failed, in the tool's own words.", and the Host narrated the miss and ended
"Would you like me to do that now?".

  1 a workspace read of a document attached to this conversation says where
    its content is, as fields on the result: this turn's message, or the
    kept copy's context_ingest read (before the fix: a bare "file not
    found"); a name that is not an attachment still answers as before
  2 the same in a real Host turn with the document attached: the tool result
    the model reads carries it
  3 a refusal whose whole answer is "use this other tool" adds no "tell the
    user what failed" line; the same redirect twice still declares the tool
    dead; any other failure still gets the line
  4 the direct-write sequence in a real Host turn: the round after the
    redirect ends on the tool result, with no failure instruction

Exit 0 = all pass; exit 1 + FAIL lines otherwise.
"""

import asyncio
import base64
import io
import json
import logging
import os
import sys
import tempfile
import zipfile

APP_ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_ROOT)
sys.path.insert(0, os.path.join(APP_ROOT, "brain"))
logging.basicConfig(level=logging.CRITICAL)

_WS = tempfile.mkdtemp(prefix="ws-attach-redirect-")
os.environ["CEIGAS_WORKSPACE"] = _WS          # read by file_access at import

FAILS: list[str] = []


def check(name, cond, extra=""):
    if cond:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"[attach-redirect] FAIL {name} {str(extra)[:600]}")


from mastercode.runtime import tools as T  # noqa: E402
from mastercode.runtime import tool_failures as TF  # noqa: E402
from mastercode.runtime import tool_call_ledger as L  # noqa: E402
from server.capabilities import file_access as FA  # noqa: E402
from server import brain_service as BS  # noqa: E402

FA.WORKSPACE = __import__("pathlib").Path(_WS)
NAME = "probe-attach-5f2e0a91.docx"


class Pool:
    """A pool whose only answer is the kept-attachment lookup (when told)."""

    def __init__(self, kept: set | None = None):
        self.kept = kept or set()
        self.queries: list = []

    async def fetch(self, q, *a):
        return []

    async def fetchrow(self, q, *a):
        return None

    async def fetchval(self, q, *a):
        self.queries.append((q, a))
        if "FROM user_context" in q and "file_upload" in q and a and a[-1] in self.kept:
            return 1
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


def _read(path, *, attached=(), pool=None):
    _saved = BS.get_pool
    BS.get_pool = lambda: pool
    try:
        async def go():
            ta = getattr(T, "TURN_ATTACHMENTS", None)
            if ta is not None:
                ta.set(tuple(attached))
            return await T._exec_workspace_file({"op": "read", "path": path})
        return asyncio.run(go())
    finally:
        BS.get_pool = _saved


# ── 1. the miss, through the real workspace_file ───────────────────────────
print("1 a workspace read of an attached document says where its content is")
r = _read(NAME, attached=(NAME,))
att = r.get("attachment") if isinstance(r, dict) else None
check("1: attached this turn -> still a refusal of the model's own input",
      r.get("status") == "error" and r.get("input_refused") is True
      and L.fault_class(L.classify(r), r) == "model", r)
check("1: ...whose result names the attachment and where its text is",
      isinstance(att, dict) and att.get("name") == NAME
      and att.get("in_this_turn_message") is True
      and "user's message" in str(r.get("result")), r)

p = Pool(kept={NAME})
r = _read(NAME, attached=(), pool=p)
att = r.get("attachment") if isinstance(r, dict) else None
check("1: kept on this Host from an earlier turn -> the structured read to use",
      isinstance(att, dict) and att.get("kept_on_host") is True
      and att.get("in_this_turn_message") is False
      and att.get("read_with") == {"tool": "context_ingest",
                                   "args": {"op": "read", "file": NAME}}, r)
check("1: ...looked up in the store the Circles route keeps attachments in",
      any("user_context" in q and "file_upload" in q and a == (BS._entity_id, NAME)
          for q, a in p.queries), p.queries)

r = _read("uploads/" + NAME, attached=(NAME,))
check("1: a path ending in the attachment's name is the same attachment",
      isinstance(r.get("attachment"), dict), r)

r = _read("canary.js", attached=(NAME,), pool=Pool())
check("1: a name that is not an attachment answers exactly as before",
      r == {"status": "error", "result": "file not found", "input_refused": True}, r)

# ── 2 + 4. real Host turns ─────────────────────────────────────────────────
from mastercode.runtime import session as S  # noqa: E402
from mastercode import provider_gateway as PG, inference_config as IC  # noqa: E402
from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS as P_REG  # noqa: E402
from mastercode.runtime import meta_tool_caps as MTC  # noqa: E402


def _docx(text):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8"?><Types '
                   'xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        z.writestr("word/document.xml", '<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w='
                   '"http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
                   f"<w:p><w:r><w:t>The code word is {text}</w:t></w:r></w:p></w:body></w:document>")
    return buf.getvalue()


def run_turn(script, *, attachments=None, message="", eid=9163):
    calls: list = []
    events: list = []
    script = list(script)

    async def model(messages=None, *a, **kw):
        msgs = messages if messages is not None else (a[0] if a else [])
        calls.append([dict(m) for m in msgs])
        r = script.pop(0) if script else {"content": "(the script ran out)"}
        cb = kw.get("stream_cb")
        if cb and r.get("content"):
            await cb(r["content"])
        return {"tokens_used": 1, "prompt_tokens": 1, "completion_tokens": 1,
                "tool_calls": None, **r}

    async def route(pool, eid, role, thread_override=None, **kw):
        return {"provider": "openrouter", "model": "openai/gpt-4o-mini-2024-07-18"}

    async def schemas(pool, eid, interaction_context=None, **kw):
        return [{k: v for k, v in P_REG["workspace_file"].items() if k != "_meta"}]

    async def all_scopes(pool, entity_id, **kw):
        return set(MTC.ENTITY_SCOPE_TO_BITS)

    pool = Pool()
    saved = (S._call_entity_model, PG.call_model, IC.resolve_role_route,
             S._AVAILABLE.get("ceigas_domain_controller"), S._build_tool_schemas,
             MTC._resolve_entity_scopes, BS.get_pool)
    try:
        S._call_entity_model = model
        PG.call_model = model
        IC.resolve_role_route = route
        S._AVAILABLE["ceigas_domain_controller"] = None
        S._build_tool_schemas = schemas
        MTC._resolve_entity_scopes = all_scopes
        BS.get_pool = lambda: pool
        rt = S.EntityRuntime(pool)

        async def ctx(entity_id, **kw):
            return S.EntitySession(
                session_id="sess-1", entity_id=entity_id,
                entity={"id": entity_id, "name": "Sage", "behavioral_params": {},
                        "permission_overrides": {"bot_capabilities": 255}},
                interaction_context="app:circles")
        rt._build_chat_context_transient = ctx

        async def pcb(e):
            events.append(e)

        async def go():
            return await rt.send_message(entity_id=eid, user_message=message,
                                         attachments=attachments, app_id="circles",
                                         progress_cb=pcb)
        out = asyncio.run(go())
    finally:
        (S._call_entity_model, PG.call_model, IC.resolve_role_route,
         S._AVAILABLE["ceigas_domain_controller"], S._build_tool_schemas,
         MTC._resolve_entity_scopes, BS.get_pool) = saved
    return out, calls, events


print("2 a Host turn with the document attached: the read the model gets back")
_att = {"name": NAME, "kept_on_host": True,
        "content_block": {"type": "document", "source": {
            "type": "base64",
            "media_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "data": base64.b64encode(_docx("PROBEDEADBEEF")).decode()}}}
_out, _calls, _ev = run_turn([
    {"content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {
        "name": "workspace_file", "arguments": json.dumps({"op": "read", "path": NAME})}}]},
    {"content": "PROBEDEADBEEF"},
], attachments=[_att], message="What is the code word in the attached document?")
_tool_msgs = [m for c in _calls[1:2] for m in c if m.get("role") == "tool"]
_tr = {}
try:
    _tr = json.loads(_tool_msgs[-1]["content"]) if _tool_msgs else {}
except (TypeError, ValueError):
    pass
check("2: the turn's read of the attachment reached the tool",
      bool(_tool_msgs), [m.get("role") for c in _calls for m in c][-6:])
check("2: the tool result the model reads says the text is in this turn's message",
      isinstance(_tr.get("attachment"), dict)
      and _tr["attachment"].get("in_this_turn_message") is True
      and "user's message" in str(_tr.get("result")), _tr)

# ── 3. the failure ledger on a redirect ────────────────────────────────────
print("3 a refusal that only names the tool to use instead")
REDIRECT = ("`workspace_file` is an operation, not a tool on your list: run it yourself "
            "through operate(tool='workspace_file', args={…the same args…}, intent='…') — one "
            "deterministic call, result returns to you now.")
_ta_redirect = {"tool": "workspace_file", "result_status": "error",
                "error_detail": REDIRECT, "use_instead": "operate"}
led: dict = {}
first = TF.note_round(led, [_ta_redirect])
check("3: no 'tell the user what failed' for a redirect",
      first.get("message") == "" and TF.FAILED_HEAD not in str(first), first)
second = TF.note_round(led, [_ta_redirect])
check("3: the same redirect twice still declares the tool dead",
      second.get("dead_tools") == ["workspace_file"], second)
other = TF.note_round({}, [{"tool": "workspace_file", "result_status": "error",
                            "error_detail": "file not found"}])
check("3: any other failure still gets the line",
      str(other.get("message")).startswith(TF.FAILED_HEAD), other)
mixed = TF.note_round({}, [_ta_redirect, {"tool": "recall", "result_status": "error",
                                          "error_detail": "timeout"}])
check("3: beside a real failure, only the real failure is named",
      "recall" in mixed["message"] and "workspace_file" not in mixed["message"], mixed)

print("4 the direct-write sequence in a real Host turn")
ARGS = {"content": "3c7d18e4b092", "encoding": "utf-8", "op": "write",
        "path": "probe-go-3c7d18e4b092.txt"}
_out, _calls, _ev = run_turn([
    {"content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {
        "name": "workspace_file", "arguments": json.dumps(ARGS)}}]},
    {"content": "I wrote the file."},
], message="Put a file called probe-go-3c7d18e4b092.txt in your workspace "
           "containing the word 3c7d18e4b092.", eid=9164)
_c1 = _calls[1] if len(_calls) > 1 else []
_last = _c1[-1] if _c1 else {}
check("4: the round after the redirect ends on the tool result",
      _last.get("role") == "tool" and "operate(" in str(_last.get("content")), _last)
check("4: no failure instruction anywhere in that round",
      not any("Tell the user what failed" in str(m.get("content")) for m in _c1),
      [str(m.get("content"))[:120] for m in _c1[-3:]])
_res = {}
try:
    _res = json.loads(_last.get("content") or "{}")
except (TypeError, ValueError):
    pass
check("4: the redirect is still filed not_advertised (the ledger is unchanged)",
      _res.get("reason") == "not_advertised" and _res.get("use_instead") == "operate", _res)

if FAILS:
    print(f"\n{len(FAILS)} FAILED")
    sys.exit(1)
print("\nALL PASS")
