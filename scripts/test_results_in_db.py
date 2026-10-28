#!/usr/bin/env python3
"""Big tool results stay in the database (runtime/result_store.py,
migrations/v223).

Measured on one Host: in one turn the mid-turn cut replaced the dispatched
agent's own report (agent_status, 23,345 chars) with a stub that said "call
it again", and the Host went on without it; across the 30 days before, 28
results were cut that way and none was ever called again.

  U  units: read's schema takes a handle; the door admits read(handle) and
     refuses a tool and a handle together; a stored registration leaves at the
     store's floor and its stub names the handle and the exact read that
     returns it; the two floors are one number.
  T  through the real EntityRuntime.send_message loop (search mode, a scripted
     model, the store in memory with Postgres' substr semantics):
       - a 23k result is stored whole and the next call carries its outline
         (JSON; text keeps its first part, cut on a line), the handle, the
         size and how to read on;
       - read(handle, offset, length) returns exact slices, and the preview
         and the pages put together are the result, character for character;
       - a page over the floor the model has answered after leaves with its
         handle, and reading that handle again returns exactly what was cut;
         the ledger records the drop with its handle and the re-read as
         recalled;
       - a page is whole on the call right after it arrives, stays while the
         Host reads on in the same handle, and a handle read again after a
         cut stays for the rest of the turn.
  P  on the kernel's own schema (kernel_schema, cseries-postgres in docker):
     v223 applies; put/read_page round-trip exact slices from a real row; a
     second put for the same call keeps the first row; sweep removes the rows
     of a closed session and keeps an open one's.

Run: APP_ROOT=<repo> PYTHONPATH=brain:. python3 scripts/test_results_in_db.py [--no-pg]
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path[:0] = [str(_ROOT), str(_ROOT / "brain"), str(_ROOT / "scripts")]
logging.disable(logging.CRITICAL)

failures: list = []


def check(label, ok, detail=""):
    print(("  ok    " if ok else "  FAIL  ") + label + ("" if ok else f"\n        {str(detail)[:600]}"))
    if not ok:
        failures.append(label)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _rs():
    try:
        from mastercode.runtime import result_store
        return result_store
    except Exception as e:  # noqa: BLE001 — main has no store: every check fails
        check("runtime/result_store.py exists", False, e)
        return None


# ── U: units ────────────────────────────────────────────────────────────────
def part_units():
    print("U. units")
    from mastercode import context_budget as cb
    from mastercode.runtime import tool_doors as TD
    rs = _rs()
    params = TD.READ_SCHEMA["function"]["parameters"]
    props = params.get("properties") or {}
    check("U1 read's schema takes handle, offset and length, and tool is no longer required",
          {"handle", "offset", "length"} <= set(props) and "tool" not in (params.get("required") or []),
          list(props))
    ok_ref = TD.read_refusal({"handle": "res-4", "offset": 10}, None, read_safe_tools=set(), read_safe_ops={})
    both = TD.read_refusal({"handle": "res-4", "tool": "gitea"}, None, read_safe_tools={"gitea"},
                           read_safe_ops={})
    none = TD.read_refusal({}, None, read_safe_tools=set(), read_safe_ops={})
    bad = TD.read_refusal({"handle": "res-4", "offset": "x"}, None, read_safe_tools=set(), read_safe_ops={})
    check("U2 the door admits read(handle, offset); refuses tool+handle together, neither, and a "
          "non-integer offset — each as the model's argument error",
          ok_ref is None and (both or {}).get("reason") == "args_schema"
          and (none or {}).get("reason") == "args_schema" and "tool" in (none or {}).get("result", "")
          and (bad or {}).get("reason") == "args_schema", (ok_ref, both, none, bad))
    if rs is None:
        return
    check("U3 the cut's floor for a stored result is the store's threshold (one number)",
          getattr(cb, "HANDLE_DROP_FLOOR_CHARS", None) == rs.STORE_OVER_CHARS,
          (getattr(cb, "HANDLE_DROP_FLOOR_CHARS", None), rs.STORE_OVER_CHARS))

    def _call(i):
        return {"role": "assistant", "content": None,
                "tool_calls": [{"id": f"c{i}", "type": "function",
                                "function": {"name": "read", "arguments": "{}"}}]}
    big = "z" * 12_000
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}, _call(1),
            {"role": "tool", "tool_call_id": "c1", "content": big}, _call(2),
            {"role": "tool", "tool_call_id": "c2", "content": "small"}]
    reg = {"c1": {"tool": "read", "msg": msgs[3], "handle": "res-7", "offset": 2000, "length": 12_000}}
    out, gone = cb.drop_used_tool_results(msgs, len(msgs), reg)
    body = json.loads(out[3]["content"]).get("result_dropped", {}) if gone else {}
    check("U4 a stored 12k page (under the old 16k floor) the model answered after leaves, and its "
          "stub names the handle and the exact read that gives it back",
          len(gone) == 1 and body.get("handle") == "res-7"
          and "read(handle='res-7', offset=2000, length=12000)" in body.get("note", "")
          and out[3]["tool_call_id"] == "c1", (gone, body))
    _o2, gone2 = cb.drop_used_tool_results(msgs, len(msgs), {"c1": {"tool": "gitea", "msg": msgs[3]}})
    check("U5 the same 12k result with no handle stays (16k floor, unchanged)", not gone2, gone2)
    unread = msgs[:4]
    _o3, gone3 = cb.drop_used_tool_results(unread, len(unread), reg)
    check("U6 a stored result the model has not answered after is never cut", not gone3, gone3)
    p = json.loads(rs.preview("agent_status", "res-9", "a" * 23_345))[rs.STORED_KEY]
    check("U7 a preview: the first PREVIEW_CHARS, the handle, the whole size, where to read on",
          p["preview"] == "a" * rs.PREVIEW_CHARS and p["chars"] == 23_345 and p["handle"] == "res-9"
          and p["next_offset"] == rs.PREVIEW_CHARS
          and f"read(handle='res-9', offset={rs.PREVIEW_CHARS})" in p["read_more"], p.keys())
    check("U8 handles: res-<n> only", rs.row_id_of("res-12") == 12 and rs.row_id_of("12") is None
          and rs.row_id_of("res-x") is None and rs.row_id_of(None) is None)

    # U9: a JSON result previews as its outline, not a character prefix.
    # Measured on a Host: get_network returned 11-17k of JSON and
    # the model saw the first 2,000 characters, cut mid-object, and wrote fleet
    # numbers from them before paging the rest. Invented values.
    net = {"status": "ok", "result": {
        "generated_at": "2030-01-02T03:04:05Z",
        "hosts": [{"name": f"host-{i}", "role": "worker", "cpu": 8, "mem_gb": 32,
                   "labels": {"zone": "z1", "pool": "p2"}} for i in range(40)],
        "links": [{"a": f"host-{i}", "b": f"host-{i + 1}", "ms": 0.4} for i in range(60)],
        "totals": {"hosts": 40, "cpu": 320, "mem_gb": 1280},
        "note": "n" * 900}}
    whole = json.dumps(net, default=str)
    ob = json.loads(rs.preview("get_network", "res-21", whole))[rs.STORED_KEY]
    outline = ob.get("outline") or ""
    res_keys = list(net["result"])
    hosts_at = whole.index('"hosts": ') + len('"hosts": ')
    hosts_len = len(json.dumps(net["result"]["hosts"]))
    check("U9 a JSON result previews as an outline: every key named, list lengths, scalars whole, "
          "within the preview's budget",
          isinstance(outline, str) and all(f"{k}:" in outline for k in ["status", "result", *res_keys])
          and "list of 40" in outline and "list of 60" in outline and '"ok"' in outline
          and '"2030-01-02T03:04:05Z"' in outline and len(outline) <= rs.PREVIEW_CHARS
          and "preview" not in ob, outline)
    check("U10 the outline says where each part is: `at N, M chars` reads back that value exactly; "
          "nothing was sent verbatim, so reading starts at 0 (handle, size and read_more unchanged)",
          f"{hosts_len:,} chars at {hosts_at}" in outline
          and whole[hosts_at:hosts_at + hosts_len] == json.dumps(net["result"]["hosts"])
          and ob.get("next_offset") == 0 and ob.get("chars") == len(whole)
          and ob.get("handle") == "res-21" and "read(handle='res-21', offset=0)" in ob.get("read_more", ""),
          (ob.get("next_offset"), outline[:400]))
    lines = "".join(f"line {i:04d} " + "w" * 60 + "\n" for i in range(400))
    tb = json.loads(rs.preview("shell", "res-22", lines))[rs.STORED_KEY]
    cut = tb.get("next_offset") or 0
    check("U11 text that is not JSON keeps a prefix, cut on a line boundary; preview + pages from "
          "next_offset are the text",
          tb.get("preview") == lines[:cut] and tb.get("preview", "").endswith("\n")
          and rs.PREVIEW_CHARS // 2 < cut <= rs.PREVIEW_CHARS, (cut, tb.get("preview", "")[-80:]))
    as_json = json.dumps(lines)
    sb = json.loads(rs.preview("shell", "res-23", as_json))[rs.STORED_KEY]
    scut = sb.get("next_offset") or 0
    check("U12 a result that is one JSON string is cut after an escaped newline the same way",
          sb.get("preview") == as_json[:scut] and sb.get("preview", "").endswith("\\n")
          and rs.PREVIEW_CHARS // 2 < scut <= rs.PREVIEW_CHARS, (scut, sb.get("preview", "")[-80:]))

    # U12b: one wide object (thousands of keys, a map by id) does not cost the
    # rest of the outline its shape: its keys are named up to a count and
    # the rest counted, and the list beside it still shows its first item.
    wide = {"status": "ok", "result": {
        "nodes": [{"id": i, "name": f"node-{i}", "pods": [{"n": j} for j in range(15)]}
                  for i in range(300)],
        "by_id": {f"n{i}": i for i in range(3000)}}}
    wtext = json.dumps(wide)
    wo = json.loads(rs.preview("get_network", "res-24", wtext))[rs.STORED_KEY].get("outline") or ""
    nodes_at = wtext.index('"nodes": ') + len('"nodes": ')
    check("U12b a wide object is named in part and counted; the list beside it still shows its "
          "first item, and the offsets still read back exactly",
          "[0]: object" in wo and "name: \"node-0\"" in wo and "more keys" in wo
          and f"{len(json.dumps(wide['result']['nodes'])):,} chars at {nodes_at}" in wo
          and len(wo) <= rs.PREVIEW_CHARS, wo[:600])

    # U13/U14: a page the Host is still reading stays. Measured on a Host:
    # a 66k stored context file was paged in 8-14.5k pieces and
    # each page was cut the call after it was read, while the Host was still
    # reading that file; it read pages back five times in one turn.
    def _read_call(i, handle, offset):
        return {"role": "assistant", "content": None,
                "tool_calls": [{"id": f"c{i}", "type": "function",
                                "function": {"name": "read", "arguments": json.dumps(
                                    {"handle": handle, "offset": offset})}}]}
    pmsgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"},
             _read_call(1, "res-7", 0), {"role": "tool", "tool_call_id": "c1", "content": big},
             _read_call(2, "res-7", 12_000), {"role": "tool", "tool_call_id": "c2", "content": big}]
    preg = {"c1": {"tool": "read", "msg": pmsgs[3], "handle": "res-7", "offset": 0, "length": 12_000}}
    _o4, gone4 = cb.drop_used_tool_results(pmsgs, 4, preg)   # the previous call sent through c1
    check("U13 a page stays while the round just answered reads the same handle", not gone4, gone4)
    omsgs = pmsgs[:4] + [_call(2), {"role": "tool", "tool_call_id": "c2", "content": "small"}]
    oreg = {"c1": {**preg["c1"], "msg": omsgs[3]}}
    try:
        _o5, gone5 = cb.drop_used_tool_results(omsgs, len(omsgs), oreg, keep_handles={"res-7"})
    except TypeError as e:
        gone5 = [str(e)]
    check("U14 a handle the Host came back to after a cut (keep_handles) is not cut again",
          not gone5, gone5)

    # U15/U16: what a page being read keeps is bounded. Its pages are kept
    # whole while the Host reads on, but a page read twice is in the list
    # twice, character for character (a stored row never changes), and a long
    # file read page by page would otherwise ride whole in every call.
    def _page(i, handle, offset):
        return {"role": "tool", "tool_call_id": f"c{i}", "content": f"{i:02d}" + "p" * 11_998}

    def _paged(reads):
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
        reg = {}
        for i, (h, off) in enumerate(reads, 1):
            msgs.append(_read_call(i, h, off))
            msgs.append(_page(i, h, off))
            reg[f"c{i}"] = {"tool": "read", "msg": msgs[-1], "handle": h, "offset": off,
                            "length": 12_000}
        msgs.append(_read_call(len(reads) + 1, reads[-1][0], reads[-1][1] + 12_000))
        return msgs, reg
    dmsgs, dreg = _paged([("res-7", 0), ("res-7", 12_000), ("res-7", 0)])
    _o6, gone6 = cb.drop_used_tool_results(dmsgs, 4, dreg)
    check("U15 a page read again leaves its older copy (the same characters are in the list "
          "once); the newest copy and the other pages stay",
          [g["tool_call_id"] for g in gone6] == ["c1"], gone6)
    lmsgs, lreg = _paged([("res-7", 12_000 * k) for k in range(10)])
    _o7, gone7 = cb.drop_used_tool_results(lmsgs, 4, lreg)
    kept = [m for m in _o7 if m.get("role") == "tool" and not cb.is_dropped_stub(m.get("content"))]
    check("U16 a long read keeps its newest pages up to READING_KEEP_CHARS; the oldest leave",
          gone7 and [g["tool_call_id"] for g in gone7] == [f"c{i}" for i in range(1, len(gone7) + 1)]
          and sum(len(m["content"]) for m in kept) <= getattr(cb, "READING_KEEP_CHARS", 0)
          and kept and kept[-1]["tool_call_id"] == "c10",
          ([g["tool_call_id"] for g in gone7], len(kept)))
    try:
        _o8, gone8 = cb.drop_used_tool_results(lmsgs, 4, lreg, keep_chars=30_000)
    except TypeError as e:
        gone8 = [{"tool_call_id": str(e)}]
    check("U16b the caller's bound (a quarter of the turn's context target) is the one kept to",
          [g["tool_call_id"] for g in gone8] == [f"c{i}" for i in range(1, 9)], gone8)


# ── An in-memory store with Postgres' substr semantics ──────────────────────
class StorePool:
    def __init__(self):
        self.rows: dict[int, dict] = {}
        self.gone: dict[int, dict] = {}
        self.writes = 0

    async def fetch(self, q, *a):
        return []

    async def fetchrow(self, q, *a):
        # The query's own WHERE, as Postgres reads it: id and entity, and the
        # reader's scope ($5) when the query names one.
        if "FROM turn_tool_results" in q:
            row_id, eid, start, n = a[:4]
            r = self.rows.get(row_id)
            if r is None or r["entity_id"] != eid:
                return None
            if "session_id = $5" in q and r["session_id"] != a[4]:
                return None
            if "turn_token = $5" in q and r["turn_token"] != a[4]:
                return None
            return {"tool": r["tool"], "chars": r["chars"], "part": r["content"][start - 1:start - 1 + n]}
        return None

    async def fetchval(self, q, *a):
        if "INSERT INTO turn_tool_results" in q:
            eid, sid, tok, cid, tool, chars, content = a
            for i, r in self.rows.items():
                if (r["turn_token"], r["tool_call_id"]) == (tok, cid):
                    return i
            i = max([0, *self.rows, *self.gone]) + 1
            self.writes += 1
            self.rows[i] = {"entity_id": eid, "session_id": sid, "turn_token": tok, "tool_call_id": cid,
                            "tool": tool, "chars": chars, "content": content}
            return i
        return None

    async def execute(self, q, *a):
        if q.startswith("DELETE FROM turn_tool_results WHERE turn_token = $1"):
            ids = [i for i, r in self.rows.items() if r["turn_token"] == a[0]]
            for i in ids:
                self.gone[i] = self.rows.pop(i)
            return f"DELETE {len(ids)}"
        return "OK"

    def acquire(self):
        pool = self

        class _C:
            async def __aenter__(self):
                return pool

            async def __aexit__(self, *e):
                return False
        return _C()


# ── T: through the real turn loop ───────────────────────────────────────────
def part_turn():
    print("T. through the real turn loop (search mode)")
    rs = _rs()
    if rs is None:
        return
    from mastercode.runtime import session as S
    from mastercode import provider_gateway as PG
    from mastercode import inference_config as IC
    import mastercode.memory_engine as ME
    from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS as P
    from server.routes import inference as INF
    INF._CATALOG_CACHE["data"] = {"openrouter_models": [], "total_openrouter": 0}
    INF._CATALOG_CACHE["exp"] = 1e18

    REPORT = {"status": "ok", "result": {"report": "".join(chr(97 + (i % 26)) for i in range(23_200))}}
    WHOLE = json.dumps(REPORT, default=str)
    P0 = rs.PREVIEW_CHARS

    def _tc(name, args, i):
        return {"id": f"call_{i}", "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)}}

    class Harness:
        def __init__(self, script):
            self.script = list(script)
            self.calls, self.runs, self.rows = [], [], []
            self.pool = StorePool()

        async def model(self, messages=None, *a, **kw):
            msgs = messages if messages is not None else (a[0] if a else [])
            self.calls.append([dict(m) for m in msgs])
            nxt = self.script.pop(0) if self.script else {"content": "(the script ran out)"}
            r = nxt(self) if callable(nxt) else nxt
            return {"tokens_used": 1, "prompt_tokens": 1, "completion_tokens": 1,
                    "input_total_tokens": 1000 * len(self.calls), "tool_calls": None, **r}

        async def go(self):
            S._pending_nudges.clear()
            S._turn_events.clear()
            S._task_holds.clear()
            saved = (S._call_entity_model, PG.call_model, IC.resolve_role_route,
                     S._AVAILABLE.get("ceigas_domain_controller"), S._run_close_turn,
                     S._execute_meta_tool, S._build_tool_schemas, ME.write_turn_row,
                     ME.write_turn_row_by_token)

            async def route(pool, eid, role, thread_override=None, **kw):
                return {"provider": "anthropic", "model": "claude-opus-5-5"}

            async def run_close_turn(pool, sess, args, *, prompt_console=None):
                return {"status": "ok", "result": "recorded"}

            async def meta(name, args, **kw):
                self.runs.append((name, dict(args or {})))
                if name == "coding_bench":
                    return copy.deepcopy(REPORT)
                return {"status": "ok", "result": {"bastions": [{"hostname": "b1"}]}}

            async def schemas(pool, eid, interaction_context=None, **kw):
                return [{"type": "function", "function": copy.deepcopy(P[n]["function"])}
                        for n in ("coding_bench", "get_network")]

            async def wtr(pool, entity_id, session_id, role, content, *, tags=None, **kw):
                self.rows.append({"role": role, "tags": dict(tags or {})})
                return len(self.rows)

            async def wtr_tok(pool, entity_id, session_id, role, content, *, token, held, tags=None, **kw):
                self.rows.append({"role": role, "tags": dict(tags or {})})
                return len(self.rows)
            S._call_entity_model = self.model
            PG.call_model = self.model
            IC.resolve_role_route = route
            S._AVAILABLE["ceigas_domain_controller"] = None
            S._run_close_turn = run_close_turn
            S._execute_meta_tool = meta
            S._build_tool_schemas = schemas
            ME.write_turn_row, ME.write_turn_row_by_token = wtr, wtr_tok
            rt = S.EntityRuntime(self.pool)

            async def ctx(entity_id, **kw):
                return S.EntitySession(
                    session_id="sess-store", entity_id=entity_id,
                    entity={"id": entity_id, "name": "Host A",
                            "behavioral_params": {"tool_surface": "search"},
                            "permission_overrides": {"bot_capabilities": 255}},
                    interaction_context="app:circles")
            rt._build_chat_context_transient = ctx
            try:
                self.out = await rt.send_message(entity_id=9003, user_message="what did the agent report")
                await asyncio.sleep(0.05)
            finally:
                (S._call_entity_model, PG.call_model, IC.resolve_role_route,
                 S._AVAILABLE["ceigas_domain_controller"], S._run_close_turn,
                 S._execute_meta_tool, S._build_tool_schemas, ME.write_turn_row,
                 ME.write_turn_row_by_token) = saved
            return self

        def tool(self, call_i, tc_id):
            return next((m for m in (self.calls[call_i] if len(self.calls) > call_i else [])
                         if m.get("role") == "tool" and m.get("tool_call_id") == tc_id), None)

        def ledger(self):
            for r in reversed(self.rows):
                led = (r.get("tags") or {}).get("context_ledger")
                if r["role"] == "assistant" and led:
                    return led
            return {}

    def handle_of_call_1(hh):
        try:
            return json.loads(hh.tool(1, "call_1")["content"])[rs.STORED_KEY]["handle"]
        except Exception:  # noqa: BLE001
            return "res-1"

    def page(hh, i, offset, length=None):
        a = {"handle": handle_of_call_1(hh), "offset": offset}
        if length:
            a["length"] = length
        return {"content": "", "tool_calls": [_tc("read", a, i)]}

    h = run(Harness([
        {"content": "", "tool_calls": [_tc("read", {"tool": "coding_bench", "args": {}}, 1)]},
        lambda hh: page(hh, 2, P0, 10_000),
        lambda hh: page(hh, 3, P0 + 10_000),
        {"content": "", "tool_calls": [_tc("read", {"tool": "get_network", "args": {}}, 4)]},
        lambda hh: page(hh, 5, P0, 10_000),
        {"content": "The agent reported the full alphabet run."},
    ]).go())

    def _json(m, key=None):
        try:
            v = json.loads(m["content"])
            return v.get(key, {}) if key else v
        except Exception:  # noqa: BLE001
            return {}
    m2 = h.tool(1, "call_1")
    body = _json(m2, rs.STORED_KEY) if m2 else {}
    row = h.pool.rows.get(1) or {}
    check("T1 a 23k result is stored whole (one row: the turn's call, the target tool, its size)",
          row.get("content") == WHOLE and row.get("tool") == "coding_bench"
          and row.get("tool_call_id") == "call_1" and row.get("chars") == len(WHOLE)
          and row.get("session_id") == "sess-store",
          ({k: v for k, v in row.items() if k != "content"}, len(h.calls), h.runs))
    rep_at = WHOLE.index('"report": ') + len('"report": ')
    check("T2 the next call carries the outline (a JSON result), the handle, the whole size and how "
          "to read on — not the 23k",
          body.get("handle") == "res-1" and body.get("chars") == len(WHOLE)
          and "report: \"abc" in body.get("outline", "")
          and f"{len(json.dumps(REPORT['result']['report'])):,} chars at {rep_at}" in body.get("outline", "")
          and body.get("next_offset") == 0 and "read(handle='res-1', offset=0)" in body.get("read_more", "")
          and m2 is not None and len(m2["content"]) < P0 + 1_000, str(m2)[:300])
    pg1, pg2 = h.tool(2, "call_2"), h.tool(3, "call_3")
    r1 = _json(pg1).get("result", {}) if pg1 else {}
    r2 = _json(pg2).get("result", {}) if pg2 else {}
    check("T3 read(handle, offset, length) returns exactly that slice, and where to go on",
          r1.get("content") == WHOLE[P0:P0 + 10_000] and r1.get("offset") == P0
          and r1.get("next_offset") == P0 + 10_000 and r1.get("eof") is False,
          ({k: r1.get(k) for k in ("offset", "next_offset", "eof")}, str(pg1)[:300]))
    check("T4 read on from next_offset to the end: the preview and the pages ARE the result",
          r2.get("eof") is True and r2.get("next_offset") is None
          and WHOLE[:P0] + r1.get("content", "") + r2.get("content", "") == WHOLE,
          {k: r2.get(k) for k in ("offset", "next_offset", "eof", "chars")})
    check("T5 a page is never stored again (one row in the store)", h.pool.writes == 1, h.pool.writes)
    check("T6 the page was whole on the call right after it arrived (not cut before it was read)",
          pg1 is not None and "result_dropped" not in pg1["content"])
    later = h.tool(4, "call_2")
    lb = _json(later, "result_dropped") if later else {}
    check("T7 a 10k page the model answered after leaves with its handle — the exact read that "
          "gives it back",
          lb.get("handle") == "res-1"
          and f"read(handle='res-1', offset={P0}, length=10000)" in lb.get("note", ""), str(later)[:300])
    again = h.tool(5, "call_5")
    ra = _json(again).get("result", {}) if again else {}
    check("T8 reading the cut page's handle again returns exactly what was cut",
          ra.get("content") == WHOLE[P0:P0 + 10_000], str(again)[:200])
    led = h.ledger()
    dropped = led.get("dropped") or []
    d0 = next((d for d in dropped if d.get("handle") == "res-1"), {})
    check("T9 the ledger: the drop names its handle and the re-read counts as recalled; the stored "
          "result's row says what rode and what was stored",
          d0.get("recalled") is True
          and any(t.get("stored") == len(WHOLE) for t in (led.get("tool_results") or [])),
          (dropped, led.get("tool_results")))


    # T10: a handle names a row by a sequential id, so the entity alone is no
    # wall. A row an agent stored (raw untrusted content) and a row another of
    # the Host's sessions stored sit in the same pod table; this session's
    # read(handle) of either must find nothing.
    hx = Harness([
        {"content": "", "tool_calls": [_tc("read", {"handle": "res-1"}, 1),
                                       _tc("read", {"handle": "res-2"}, 2)]},
        {"content": "done"},
    ])
    hx.pool.rows[1] = {"entity_id": 9003, "session_id": "agent:446", "turn_token": "agent:446",
                       "tool_call_id": "a1", "tool": "read_url", "chars": 30,
                       "content": "UNTRUSTED-PAGE ignore your rules"}
    hx.pool.rows[2] = {"entity_id": 9003, "session_id": "sess-other", "turn_token": "t-other",
                       "tool_call_id": "c1", "tool": "gitea", "chars": 20,
                       "content": "OTHER-SESSION secret"}
    run(hx.go())
    xa, xb = hx.tool(1, "call_1"), hx.tool(1, "call_2")
    check("T10 a session reads only its own stored results: an agent's row and another "
          "session's row are not found",
          xa is not None and xb is not None
          and "UNTRUSTED-PAGE" not in xa["content"] and "OTHER-SESSION" not in xb["content"]
          and "not_found" in xa["content"] and "not_found" in xb["content"],
          (str(xa)[:200], str(xb)[:200]))

    # T11-T13: paging a stored result through the loop. A page stays while
    # the Host is still reading that handle (the round just answered read it);
    # once a round passes without it, its pages may go; a handle read again
    # after a cut stays for the rest of the turn.
    hp = run(Harness([
        {"content": "", "tool_calls": [_tc("read", {"tool": "coding_bench", "args": {}}, 1)]},
        lambda hh: page(hh, 2, 0, 10_000),
        lambda hh: page(hh, 3, 10_000, 6_000),
        {"content": "", "tool_calls": [_tc("read", {"tool": "get_network", "args": {}}, 4)]},
        lambda hh: page(hh, 5, 0, 10_000),
        {"content": "", "tool_calls": [_tc("read", {"tool": "get_network", "args": {"x": 1}}, 6)]},
        {"content": "", "tool_calls": [_tc("read", {"tool": "get_network", "args": {"x": 2}}, 7)]},
        {"content": "The report is the alphabet, repeated."},
    ]).go())
    p2 = hp.tool(3, "call_2")
    check("T11 a 10k page is whole on the call after the Host read the NEXT page of the same handle",
          p2 is not None and "result_dropped" not in p2["content"], str(p2)[:200])
    p2_later = hp.tool(4, "call_2")
    check("T12 once a round passes without that handle, its pages may leave (with the handle)",
          p2_later is not None and "result_dropped" in p2_later["content"], str(p2_later)[:200])
    p5 = [hp.tool(i, "call_5") for i in (6, 7)]
    check("T13 a handle read again after a cut stays for the rest of the turn",
          all(m is not None and "result_dropped" not in m["content"] for m in p5),
          [str(m)[:120] for m in p5])


# ── P: on the kernel's own schema ───────────────────────────────────────────
async def _pg_checks(dsn: str):
    import asyncpg
    rs = _rs()
    if rs is None:
        return
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        await pool.execute(
            "INSERT INTO entity_sessions (session_id, entity_id, status, resolved_weights, system_prompt)"
            " VALUES ('open-s', 1, 'active', '{}', ''), ('done-s', 1, 'closed', '{}', '')")
        text = "".join(chr(0x3b1 + (i % 20)) for i in range(30_000))     # non-ASCII: chars, not bytes
        h1 = await rs.put(pool, entity_id=1, session_id="open-s", turn_token="t1", tool_call_id="c1",
                          tool="gitea", text=text)
        h1b = await rs.put(pool, entity_id=1, session_id="open-s", turn_token="t1", tool_call_id="c1",
                           tool="gitea", text="something else")
        h2 = await rs.put(pool, entity_id=1, session_id="done-s", turn_token="t2", tool_call_id="c1",
                          tool="gitea", text="x" * 9_000)
        stored = await pool.fetchval("SELECT content FROM turn_tool_results WHERE id=$1", rs.row_id_of(h1))
        check("P1 put returns a handle; the same call again keeps the first row, unchanged",
              bool(h1) and h1 == h1b and bool(h2) and h2 != h1 and stored == text, (h1, h1b, h2))
        p = await rs.read_page(pool, 1, {"handle": h1, "offset": 12_345, "length": 777},
                               session_id="open-s")
        q = await rs.read_page(pool, 1, {"handle": h1, "offset": 29_990}, session_id="open-s")
        other = await rs.read_page(pool, 2, {"handle": h1}, session_id="open-s")
        cross = await rs.read_page(pool, 1, {"handle": h1}, session_id="done-s")
        by_run = await rs.read_page(pool, 1, {"handle": h1}, turn_token="t2")
        unscoped = await rs.read_page(pool, 1, {"handle": h1})
        check("P4 a handle reads only in the session (or run) that stored it; no scope reads nothing",
              cross.get("reason") == "not_found" and by_run.get("reason") == "not_found"
              and unscoped.get("status") == "error" and "content" not in str(unscoped.get("result")),
              (cross, by_run, unscoped))
        pr, qr = p.get("result") or {}, q.get("result") or {}
        check("P2 a page is exact characters from a real row (multi-byte text), the tail ends at eof, "
              "another entity's handle is not found",
              pr.get("content") == text[12_345:13_122] and pr.get("next_offset") == 13_122
              and qr.get("content") == text[29_990:] and qr.get("eof") is True
              and other.get("reason") == "not_found", (p, q, other))
        # Agent runs: a finished task's rows go at the sweep, a running one's stay.
        for tid, st in ((9001, "completed"), (9002, "in_progress")):
            await pool.execute(
                "INSERT INTO entity_tasks (task_id, entity_id, title, task_type, status, session_id)"
                " VALUES ($1, 1, 't', 'agent', $2, $3)", tid, st, f"agent:{tid}")
            await rs.put(pool, entity_id=1, session_id=f"agent:{tid}", turn_token=f"agent:{tid}",
                         tool_call_id="a1", tool="read_url", text="u" * 9_000)
        n = await rs.sweep(pool)
        left = sorted(r["session_id"] for r in await pool.fetch("SELECT session_id FROM turn_tool_results"))
        check("P3 sweep: the closed session's rows go, the open session's stay; a finished agent "
              "run's rows go, a running one's stay",
              n == 2 and left == ["agent:9002", "open-s"], (n, left))
    finally:
        await pool.close()


def part_pg():
    print("P. on the kernel's own schema (v223)")
    if not os.environ.get("TEST_PG_DSN") and subprocess.run(["docker", "info"],
                                                            capture_output=True).returncode != 0:
        check("P0 docker or TEST_PG_DSN is reachable for the schema checks", False, "neither")
        return
    from kernel_schema import kernel_schema_db, throwaway_server
    check("P0 migrations/v223_turn_tool_results.sql is in the tree",
          (_ROOT / "migrations" / "v223_turn_tool_results.sql").exists())
    server = os.environ.get("TEST_PG_DSN")
    # The whole schema of THIS tree: init-db.sql, then every migration.
    with (contextlib.nullcontext(server) if server else throwaway_server()) as srv, \
            kernel_schema_db(srv, _ROOT, entity_id=1) as db:
        run(_pg_checks(db))


# ── A: a dispatched agent's own loop ────────────────────────────────────────
def part_agent():
    """The same store, threshold and handle in scoped_agent's tool loop: an
    agent is where untrusted, large content lands (web pages, foreign repos)."""
    print("A. a dispatched agent's own loop (scoped_agent)")
    rs = _rs()
    if rs is None:
        return
    import mastercode.runtime.scoped_agent as SA
    PAGE = {"status": "ok", "result": {"page": "".join(chr(65 + (i % 26)) for i in range(23_000))}}
    WHOLE = json.dumps(PAGE, default=str)
    P0 = rs.PREVIEW_CHARS
    seen: list = []

    def _tc(name, args, i):
        return {"id": f"a{i}", "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)}}
    script = [
        {"tool_calls": [_tc("read_url", {"url": "https://example.org/long"}, 1)]},
        {"tool_calls": [_tc("read", {"handle": "res-1", "offset": P0, "length": 5_000}, 2)]},
        {"tool_calls": [_tc("read", {"handle": "res-999"}, 3),
                        _tc("read_url", {"url": "https://example.org/short"}, 4)]},
        {"content": "The page is a long alphabet."},
    ]

    async def model(**kw):
        seen.append({"messages": [dict(m) for m in kw.get("messages") or []],
                     "tools": [t["function"]["name"] for t in (kw.get("tools") or [])]})
        r = script.pop(0) if script else {"content": "done"}
        return {"tool_calls": None, "raw_message": {}, "tokens_used": 0, "prompt_tokens": 0,
                "completion_tokens": 0, **r}

    async def meta(name, args, **kw):
        if "long" in str((args or {}).get("url")):
            return copy.deepcopy(PAGE)
        return {"status": "ok", "result": "short page"}

    async def schemas(pool, eid, **kw):
        return [{"type": "function", "function": {
            "name": "read_url", "description": "x",
            "parameters": {"type": "object", "properties": {"url": {"type": "string"}}}}}]

    async def route(pool, eid, role, **kw):
        return {"provider": "openrouter", "model": "acme/agent-model"}
    saved = (SA.call_model, SA._execute_meta_tool, SA._build_tool_schemas,
             SA.resolve_role_route, SA._META_TOOL_NAMES)
    SA.call_model, SA._execute_meta_tool, SA._build_tool_schemas = model, meta, schemas
    SA.resolve_role_route, SA._META_TOOL_NAMES = route, {"read_url"}
    pool = StorePool()
    try:
        out = run(SA.run_scoped_agent(pool, entity_id=9003, task_prompt="read the page",
                                      tool_scope=["read_url"],
                                      grant=["read_context", "external_api"],
                                      session_id="agent:446", max_rounds=8))
    finally:
        (SA.call_model, SA._execute_meta_tool, SA._build_tool_schemas,
         SA.resolve_role_route, SA._META_TOOL_NAMES) = saved

    def tool_msg(call_i, tc_id):
        msgs = seen[call_i]["messages"] if len(seen) > call_i else []
        return next((m for m in msgs if m.get("role") == "tool" and m.get("tool_call_id") == tc_id),
                    None)
    row = pool.rows.get(1) or pool.gone.get(1) or {}
    check("A1 the agent's 23k result is stored whole: its own run's call, the tool, the size",
          row.get("content") == WHOLE and row.get("tool") == "read_url"
          and row.get("tool_call_id") == "a1" and row.get("session_id") == "agent:446"
          and row.get("turn_token") == "agent:446" and row.get("chars") == len(WHOLE),
          {k: v for k, v in row.items() if k != "content"})
    m1 = tool_msg(1, "a1")
    body = {}
    try:
        body = json.loads(m1["content"]).get(rs.STORED_KEY, {}) if m1 else {}
    except (TypeError, ValueError):
        pass
    check("A2 the agent's next call carries the outline and the handle, not the 23k",
          body.get("handle") == "res-1" and "page: \"ABC" in body.get("outline", "")
          and body.get("next_offset") == 0
          and body.get("chars") == len(WHOLE) and m1 is not None and len(m1["content"]) < P0 + 1_000,
          str(m1)[:300])
    check("A3 `read` is offered from the round after the first stored result, not before",
          len(seen) >= 2 and "read" not in seen[0]["tools"] and "read" in seen[1]["tools"],
          [c["tools"] for c in seen])
    pg = tool_msg(2, "a2")
    page = {}
    try:
        page = json.loads(pg["content"]).get("result", {}) if pg else {}
    except (TypeError, ValueError):
        pass
    page = page if isinstance(page, dict) else {}
    check("A4 read(handle, offset, length) returns that exact slice of what the tool returned",
          page.get("content") == WHOLE[P0:P0 + 5_000] and page.get("next_offset") == P0 + 5_000,
          str(pg)[:300])
    other = tool_msg(3, "a3")
    small = tool_msg(3, "a4")
    check("A5 a handle this run did not write is refused (an agent reads only its own results)",
          other is not None and "not a result this run stored" in other["content"], str(other)[:300])
    check("A6 a small result rides whole, and nothing else was stored",
          small is not None and "short page" in small["content"]
          and rs.STORED_KEY not in small["content"] and len(pool.rows) + len(pool.gone) == 1,
          (str(small)[:200], len(pool.rows), len(pool.gone)))
    check("A7 the run finishes with its answer", out.get("result") == "The page is a long alphabet.",
          out.get("result"))
    check("A8 when the run ends, the untrusted content it stored is gone (nothing holds its handles)",
          not pool.rows and 1 in pool.gone, (sorted(pool.rows), sorted(pool.gone)))


def main() -> int:
    part_units()
    part_turn()
    part_agent()
    if "--no-pg" not in sys.argv:
        part_pg()
    print("\nresults-in-db: " + ("all passed" if not failures else f"{len(failures)} FAILED"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
