#!/usr/bin/env python3
"""Routes: the person's words and the Host's lists are never cut or walled
silently.

The rule: a Host handles continuous 24/7 work on large data, efficiently.
Large data is paged and says when there is more; nothing is cut
silently; a limit stays only where it protects the platform, and then says so.

Each block below pins one bound by an input only that fix can move. The
modules are EXECUTED against stub pools that answer the statements the code
issues; no pod, no database, no network.

  - the phase-approval card carries the WHOLE spec (was cut at 1,500).
  - the MCP per-token limiter is a high runaway backstop (was 60/min),
    0 turns it off, each harness pod spends its own bucket, and a hit
    says when the next request is accepted.
  - the MCP body bound is the real downstream one (16 MiB, was 1 MiB),
    parsed under the shared JSON node budget; read_url pages by offset.
  - a host-to-host message has no sender-side length policy of its own;
  - the RECEIVER bounds one message from another host (224 KiB, its own
    direct lane's request less the envelope), refuses in words and stores
    nothing, and the sender checks the same bound first.
  - ladder evidence is carried whole and fitted to the argv bound with a
    marker (was a silent 4,000 cut).
  - a fork-and-PR proposal takes any number of files of any size.
  - every listed list endpoint pages (offset or a before-cursor) and
    answers where the next page starts.
  - a support ticket's cut field is MARKED in place and named in the
    draft's answer; a file past the bridge's item cap is listed as not
    included; a long reply goes in parts; over-ticking is refused.
  - coding-protocol prose is kept whole; author refuses in words.
  - the GitHub picker lists every repository, with a search.

Run: APP_ROOT=<repo> python3 scripts/test_caps_routes.py
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import sys
import types
from pathlib import Path
from tools_source import tools_source  # tools.py and the modules split out of it

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path[:0] = [str(ROOT), str(ROOT / "brain")]
logging.disable(logging.CRITICAL)

FAILED: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'ok  ' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILED.append(label)


def run(coro):
    return asyncio.run(coro)


def src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def guard(label: str, fn) -> None:
    """Run one block; an exception is that block's failure, never the gate's."""
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        check(label, False, f"{type(e).__name__}: {e}")


class Rec:
    """A stub pool: records every statement, answers from a script."""

    def __init__(self, fetch=None, fetchrow=None, fetchval=None):
        self.calls: list[tuple[str, tuple]] = []
        self._fetch = fetch or (lambda sql, args: [])
        self._fetchrow = fetchrow or (lambda sql, args: None)
        self._fetchval = fetchval or (lambda sql, args: None)

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return self._fetch(sql, args)

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        return self._fetchrow(sql, args)

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        return self._fetchval(sql, args)

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        return "UPDATE 1"


def b16():
    from server.routes import tickets as T
    spec = "Build the trading dashboard. " + ("Every detail the person approves. " * 200) + "THE END."

    def fr(sql, args):
        if "FROM project_phases" in sql:
            return {"id": 42, "phase_number": 2, "title": "Web UI", "spec": spec,
                    "description": "", "gate": "user", "approved_at": None,
                    "updated_at": None, "project_name": "Trading"}
        return None
    pool = Rec(fetchrow=fr)
    seen = {}

    async def fake_create(pool, eid, **kw):
        seen.update(kw)
        return {"id": 7}
    real = T.create_ticket_service
    T.create_ticket_service = fake_create
    try:
        run(T.raise_phase_gate_ticket(pool, 3, project_id=9, phase_id=42))
    finally:
        T.create_ticket_service = real
    sitrep = seen.get("sitrep") or ""
    check("the approval card carries the whole spec (its last words included)",
          len(spec) > 1500 and spec in sitrep and "THE END." in sitrep,
          f"spec={len(spec)} sitrep={len(sitrep)}")


def b14_b15():
    os.environ.pop("CSERIES_MCP_RATE_MAX_REQUESTS", None)
    from server.routes import mcp as M
    check("the per-token limiter is a runaway backstop (>= 600 a minute)",
          M._RATE_MAX_REQUESTS >= 600, str(M._RATE_MAX_REQUESTS))
    M._rate_buckets.clear()
    burst = [M._rate_check("token:burst") for _ in range(150)]
    check("an agent's burst of 150 calls in a second is never refused", all(burst))
    os.environ["CSERIES_MCP_RATE_MAX_REQUESTS"] = "0"
    try:
        check("CSERIES_MCP_RATE_MAX_REQUESTS=0 turns the backstop off",
              M._rate_max_requests() == 0)
    finally:
        os.environ.pop("CSERIES_MCP_RATE_MAX_REQUESTS", None)
    k1 = M._rate_key({"id": 0, "_harness": True}, "cap-one")
    k2 = M._rate_key({"id": 0, "_harness": True}, "cap-two")
    check("two harness pods spend two buckets, never one shared",
          k1 != k2 and k1.startswith("harness:"), f"{k1} {k2}")
    check("a bearer token keeps its own bucket",
          M._rate_key({"id": 12}, None) == "token:12")
    s = src("server/routes/mcp.py")
    check("a hit says when the next request is accepted (and sends Retry-After)",
          "the next is accepted in" in s and '"Retry-After"' in s)
    # A harness pod is a confined peer — 60 a minute as
    # before, but per pod; only outside clients get the 600 backstop.
    M._rate_buckets.clear()
    pod1 = [M._rate_check(k1) for _ in range(61)]
    check("a harness pod is limited at 60 a minute (the 61st is refused)",
          all(pod1[:60]) and pod1[60] is False, f"{pod1.count(True)} accepted")
    check("a second harness pod has its own 60 (never shares the first's)",
          M._rate_check(k2) is True)
    M._rate_buckets.clear()
    outside = [M._rate_check("token:12") for _ in range(600)]
    check("an outside client still gets the 600 backstop",
          all(outside) and M._rate_check("token:12") is False,
          f"{outside.count(True)} accepted")
    os.environ["CSERIES_MCP_RATE_MAX_REQUESTS"] = "0"
    saved = M._RATE_MAX_REQUESTS
    try:
        M._RATE_MAX_REQUESTS = M._rate_max_requests()
        M._rate_buckets.clear()
        check("turning the backstop off never lifts a harness pod's 60",
              all(M._rate_check("token:12") for _ in range(700))
              and [M._rate_check(k1) for _ in range(61)][-1] is False)
    finally:
        M._RATE_MAX_REQUESTS = saved
        os.environ.pop("CSERIES_MCP_RATE_MAX_REQUESTS", None)
    check("the 429 says which bound applied",
          "harness pod rate limit" in s and "runaway backstop: this token" in s)
    M._rate_buckets.clear()
    check("the MCP body bound is 16 MiB (the bridge frame), not 1 MiB",
          M._MCP_MAX_BODY_BYTES >= 16 * 1024 * 1024, str(M._MCP_MAX_BODY_BYTES))
    check("the body is parsed under the shared JSON node budget",
          'await _hin.parse_json(raw, "mcp request")' in s)
    check("a harness pod (untrusted, read_url only) keeps the 1 MiB bound",
          M._MCP_HARNESS_BODY_BYTES == 1024 * 1024
          and 'body_cap = _MCP_HARNESS_BODY_BYTES if auth.get("_harness") else _MCP_MAX_BODY_BYTES' in s)
    check("read_url over MCP pages: offset is passed through and in the schema",
          "offset=args.get(\"offset\")" in s and '"offset": {"type": "integer"' in s)


# A real permission slip, signed the way the bridge signs one (canonical terms,
# Ed25519), and a pinned trust view the receiver checks it against.
def _slip_kit():
    import base64 as _b64
    from datetime import datetime, timedelta, timezone
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    from server.peer.slip_verify import canonical_terms

    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)

    def slip(issuer=3, grantee=9, ops=("message",), cid="slip-real-1", hours=1,
             exp_s=None, issued_s=None):
        now = datetime.now(timezone.utc)
        exp = now + (timedelta(seconds=exp_s) if exp_s is not None else timedelta(hours=hours))
        terms = {"contract_id": cid, "issuer": issuer, "grantee": grantee,
                 "resource": "dm:contact", "allowed_ops": list(ops), "key_id": "k1",
                 "expires_at": exp.isoformat()}
        if issued_s is not None:
            terms["issued_at"] = (now + timedelta(seconds=issued_s)).isoformat()
        return {"contract_id": cid, "terms": terms,
                "signature": _b64.b64encode(key.sign(canonical_terms(terms))).decode()}

    class Trust:
        ready = True
        keys = {"k1": pub}
        revoked: set = set()
        pub_raw = pub

        async def ensure_key(self, key_id):
            return key_id in self.keys

        async def refresh(self):
            return None
    return slip, Trust()


def b17():
    from server.routes import messages as MS
    from server.bridge import memory_handlers as MH
    from server import brain_service as bs
    long = "x" * 30_000
    body = MS.SendBody(to_entity_id=5, body=long)
    check("a 30,000-character message is accepted (no 20,000 Field cap)",
          body.body == long)
    bound = getattr(MH, "CROSS_HOST_BODY_MAX", None)
    check("the receiving Host has its own bound on one message from another host",
          isinstance(bound, int) and bound > 0, bound)
    from server.peer import peer_server as PS
    check("the bound is the receiver's own direct-lane request (256 KiB) less 32 KiB "
          "of envelope, so a message under it crosses either lane",
          bound == PS._MAX_REQ - 32 * 1024, (bound, PS._MAX_REQ))
    if not isinstance(bound, int):
        bound = PS._MAX_REQ - 32 * 1024       # the size the receiver must refuse past
    big = "y" * (bound + 1)
    refusal = getattr(MH, "cross_host_body_refusal", lambda _b: None)
    try:
        run(MS.send_message_service(Rec(), 3, to_entity_id=5, body=big))
        check("the sender refuses past the receiver's bound, in the receiver's words", False)
    except MS.MessageError as e:
        check("the sender refuses past the receiver's bound, in the receiver's words",
              e.status == 413 and e.detail == refusal(big)
              and "share it as a file" in e.detail, e.detail)
    except Exception as e:  # noqa: BLE001 — the old sender went on to the bridge
        check("the sender refuses past the receiver's bound, in the receiver's words",
              False, repr(e)[:120])

    _mk_slip, _trust = _slip_kit()

    def _arrive(text, *, from_eid=9):
        pool = Rec(fetchval=lambda sql, a: "11111111-1111-1111-1111-111111111111")
        real_gp, real_eid = bs.get_pool, MH._entity_id
        real_trust = getattr(MH, "_slip_trust", None)
        bs.get_pool = lambda: pool
        MH._entity_id = lambda: 3

        async def _t():
            return _trust
        MH._slip_trust = _t
        try:
            out = run(MH.handle_inbox_append({
                "subject": "Message", "body": text, "message_kind": "message",
                "from_entity_id": from_eid, "from_address": f"{from_eid}@entity.localhost",
                "slip": _mk_slip(grantee=from_eid or 9),
                # the old attestation flags too, so this also passes where they ruled
                "slip_authorized": True, "sender_verified": True,
                "payload": {"authorized_via_slip": True}}))
        finally:
            bs.get_pool, MH._entity_id = real_gp, real_eid
            if real_trust is not None:
                MH._slip_trust = real_trust
        stored = any("INSERT INTO inbox_messages" in c[0] for c in pool.calls)
        return out, stored

    out, stored = _arrive("z" * (bound + 1))
    check("the receiver refuses an oversize message from another host in words "
          "and stores nothing",
          out.get("ok") is False and out.get("too_large") is True and not stored
          and "KB" in (out.get("error") or ""), (out, stored))
    out, stored = _arrive("w" * 30_000)
    check("a 30,000-character message from another host is stored",
          out.get("ok") is True and stored, out)
    out, stored = _arrive("v" * (bound + 1), from_eid=None)
    check("the Host's OWN notifications are not bounded (not cross-host)",
          out.get("ok") is True and stored, out)
    # The Circles composer sends host-to-host too (person DMs and host DMs):
    # it is held to the same receiver bound before anything travels.
    _csrc = (ROOT / "server" / "routes" / "circles.py").read_text()
    _i_chk = _csrc.find("_too_big = cross_host_body_refusal(text)")
    _i_dm = _csrc.find('if target_kind == "dm":', _i_chk if _i_chk > 0 else 0)
    _i_send = _csrc.find("await _try_direct_peer_send(", _i_chk if _i_chk > 0 else 0)
    check("the Circles send refuses past the receiver's bound before either lane is tried",
          0 < _i_chk < _i_dm < _i_send and "message_too_large" in _csrc[_i_chk:_i_dm])


def b18():
    from server.routes import ladder as L
    err = "E" * 9000 + " the discriminating clue"
    ev = L._evidence_from_failure({"kimi": {"error": err}}, {"kimi": "crashed"}, {})
    check("a failure's whole error climbs (no 4,000 / 300 cut)",
          "the discriminating clue" in ev and len(ev) > 9000, str(len(ev)))
    p = L._with_evidence("do the task", ev)
    check("evidence that fits the prompt's argument rides whole",
          p.endswith(ev))
    huge = "z" * (200 * 1024)
    p2 = L._with_evidence("do the task", huge)
    check("evidence past the 128 KiB argv bound is cut WITH a marker, under the bound",
          len(p2.encode()) <= L._PROMPT_ARG_MAX and "more bytes of evidence not carried" in p2,
          str(len(p2.encode())))


def b20():
    import base64
    from server.routes import gitea_propose as GP
    files = [{"path": f"src/f{i}.py", "content": "x"} for i in range(120)]
    files.append({"path": "package-lock.json", "encoding": "base64",
                  "content": base64.b64encode(b"\0" * ((1 << 20) * 3)).decode()})
    out = GP._validate({"repo": "someone/thing", "title": "refactor", "files": files})
    check("a proposal of 121 files, one of 3 MiB, validates (no 50 / 1 MiB cap)",
          len(out["files"]) == 121 and len(out["files"][-1]["data"]) == 3 * (1 << 20))


def b21():
    from server.routes import (inbox, brain, tickets, projects, calendar, contacts,
                               groups, reachout, ships, compute)
    pages = {
        "inbox.list_messages": (inbox.list_messages, "offset"),
        "brain.list_sessions": (brain.list_sessions, "offset"),
        "brain.threads": (brain.threads, "offset"),
        "brain.list_tasks": (brain.list_tasks, "offset"),
        "brain.scan_duplicates": (brain.scan_duplicates, "offset"),
        "tickets.list_tickets": (tickets.list_tickets, "offset"),
        "projects.list_projects": (projects.list_projects, "offset"),
        "projects.project_activity": (projects.project_activity, "before"),
        "calendar.list_events": (calendar.list_events, "offset"),
        "contacts.list_contacts": (contacts.list_contacts, "offset"),
        "groups.list_ballots": (groups.list_ballots, "offset"),
        "reachout.get_reachout": (reachout.get_reachout, "offset"),
        "ships.list_runs": (ships.list_runs, "before"),
        "compute.recent_sessions": (compute.recent_sessions, "offset"),
    }
    for name, (fn, param) in pages.items():
        if fn is None:
            continue
        check(f"{name} takes `{param}`", param in inspect.signature(fn).parameters)

    # tickets: the offset reaches the SQL and the answer says where to read on
    pool = Rec(fetch=lambda sql, args: [{} for _ in range(2)])
    T = tickets
    real = T._row_to_dict
    T._row_to_dict = lambda r: {}
    try:
        out = run(T.list_tickets_service(pool, 3, limit=2, offset=4))
    finally:
        T._row_to_dict = real
    check("tickets page — OFFSET in the statement, next_offset in the answer",
          "OFFSET 4" in pool.calls[-1][0] and out.get("next_offset") == 6, str(out.get("next_offset")))

    # contacts: same
    pool = Rec(fetch=lambda sql, args: [])
    out = run(contacts.list_contacts_service(pool, limit=50, offset=100))
    check("contacts page — OFFSET in the statement; a short page ends (null next)",
          "OFFSET 100" in pool.calls[-1][0] and out.get("next_offset") is None)

    # duplicates: totals count every group, not the page
    from server import brain_service as bs
    rows = [{"hash": f"h{i}", "ids": [1, 2], "copies": 2, "content": "c", "memory_type": "fact",
             "all_groups": 37, "all_excess": 51} for i in range(5)]
    pool = Rec(fetch=lambda sql, args: rows)
    real_gp = bs.get_pool
    bs.get_pool = lambda: pool
    try:
        out = run(brain.scan_duplicates(limit=5, offset=10))
    finally:
        bs.get_pool = real_gp
    check("duplicate totals are the real totals, not the page length",
          out.get("total_groups") == 37 and out.get("total_excess") == 51
          and out.get("next_offset") == 15, json.dumps({k: out.get(k) for k in
                                                        ("total_groups", "total_excess", "next_offset")}))

    # inbox: the bridge handler pages
    from server.bridge import memory_handlers as MH
    pool = Rec(fetch=lambda sql, args: [])
    real_gp, real_eid = bs.get_pool, MH._entity_id
    bs.get_pool = lambda: pool
    MH._entity_id = lambda: 3
    try:
        out = run(MH.handle_inbox_list({"limit": 20, "offset": 40}))
        out_c = run(MH.handle_calendar_list({"limit": 20, "offset": 60}))
    finally:
        bs.get_pool, MH._entity_id = real_gp, real_eid
    inbox_call = [c for c in pool.calls if "inbox_messages" in c[0]][-1]
    cal_call = [c for c in pool.calls if "calendar_events" in c[0]][-1]
    check("inbox handler — OFFSET bound to the request's offset",
          "OFFSET $3" in inbox_call[0] and inbox_call[1][-1] == 40 and out.get("offset") == 40)
    check("calendar handler — OFFSET bound to the request's offset",
          "OFFSET" in cal_call[0] and cal_call[1][-1] == 60 and out_c.get("offset") == 60)

    s = src("server/routes/projects.py")
    check("project activity answers next_before to read older events",
          '"next_before":' in s and "def get_project_activity_service" in s)


# ── the activity feed's compound cursor ───────────────────────────────
def _activity_pool(tool_rows, research_rows):
    """A stub pool that runs the feed's own WHERE/ORDER/LIMIT over in-memory
    rows: created_at < / <= $n, (created_at, id) < ($n, $m), DESC, LIMIT."""
    import re as _re

    def _apply(sql, args, rows):
        out = list(rows)
        for m in _re.finditer(r"\(created_at, id\) < \(\$(\d+), \$(\d+)\)", sql):
            ts, rid = args[int(m.group(1)) - 1], args[int(m.group(2)) - 1]
            out = [r for r in out if (r["created_at"], r["id"]) < (ts, rid)]
        for m in _re.finditer(r"AND created_at (<=|<|>) \$(\d+)", sql):
            op, v = m.group(1), args[int(m.group(2)) - 1]
            out = [r for r in out if (r["created_at"] < v if op == "<" else
                                      r["created_at"] <= v if op == "<=" else
                                      r["created_at"] > v)]
        if "id DESC" in sql:
            out.sort(key=lambda r: (r["created_at"], r["id"]), reverse=True)
        else:
            out.sort(key=lambda r: r["created_at"], reverse=True)
        lim = _re.search(r"LIMIT (\d+)", sql)
        return out[:int(lim.group(1))] if lim else out

    def fetch(sql, args):
        if "tool_call_log" in sql:
            return _apply(sql, args, tool_rows)
        if "entity_memory" in sql:
            return _apply(sql, args, research_rows)
        return []

    return Rec(fetch=fetch, fetchval=lambda sql, a: 1)


def n6():
    from datetime import datetime, timedelta, timezone
    from server.routes import projects as P
    base = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
    tool_rows, research_rows, rid = [], [], 0
    # 30 tool calls, one tool_call_log row each; six share ONE instant each
    # (a burst), so page boundaries land inside same-timestamp runs.
    def _call(rid, tool, args, ts):
        return {"id": rid, "tool": tool, "reason": "ok", "status": "ok",
                "args": json.dumps({"preview": json.dumps(args)}), "latency_ms": 1,
                "created_at": ts}
    for i in range(30):
        ts = base - timedelta(seconds=10 * (i // 6))
        rid += 1
        tool_rows.append(_call(rid, "read_url", {"url": f"u{i}"}, ts))
    # 50 research findings (the feed fetched at most 40), five per instant.
    for j in range(50):
        research_rows.append({"id": 1000 + j, "content": f"finding {j}", "metadata": "{}",
                              "created_at": base - timedelta(seconds=3 + 10 * (j // 5))})
    pool = _activity_pool(tool_rows, research_rows)
    seen, before, pages = [], None, 0
    while pages < 60:
        out = run(P.get_project_activity_service(pool, 3, 7, limit=7, before=before))
        pages += 1
        for e in out["events"]:
            seen.append(e.get("args", {}).get("url") if e["kind"] == "tool_call" else e["memory_id"])
        before = out.get("next_before")
        if not before:
            break
    want = {f"u{i}" for i in range(30)} | {1000 + j for j in range(50)}
    check("paging by next_before returns every event exactly once (30 calls + 50 findings, "
          "same-instant bursts across page edges)",
          len(seen) == 80 and set(seen) == want,
          f"{len(seen)} events, {len(set(seen))} distinct, missing {len(want - set(seen))}")
    # Twenty calls at one instant, then five older ones: paging walks
    # through the burst and reaches every older call exactly once.
    dup_rows, rid = [], 5000
    t0 = base
    for k in range(20):
        rid += 1
        dup_rows.append(_call(rid, "context", {"action": "status"}, t0))
    for k in range(5):
        rid += 1
        dup_rows.append(_call(rid, "recall", {"query": f"q{k}"}, t0 - timedelta(minutes=5 + k)))
    pool = _activity_pool(dup_rows, [])
    got, before, pages = [], None, 0
    while pages < 40:
        out = run(P.get_project_activity_service(pool, 3, 7, limit=3, before=before))
        pages += 1
        got += [e["args"].get("query") for e in out["events"] if e["tool"] == "recall"]
        before = out.get("next_before")
        if not before:
            break
    check("a burst of same-instant calls pages through to the older calls, each once",
          sorted(got) == [f"q{k}" for k in range(5)], got)
    # The first form of the cursor, a bare timestamp, still means strictly older.
    pool = _activity_pool(tool_rows, research_rows)
    out = run(P.get_project_activity_service(pool, 3, 7, limit=200,
                                             before=(base - timedelta(seconds=25)).isoformat()))
    check("a bare ISO `before` is still accepted as strictly older",
          out["events"] and all(e["at"] < (base - timedelta(seconds=25)).isoformat()
                                for e in out["events"]), len(out["events"]))


def b22():
    from server.routes import support as S
    cuts: list = []
    out = S._fit("a" * 20_000, S.TEXT_CAP, cuts, "problem")
    check("a cut field stays within the bridge's 8,000 and carries the marker",
          len(out) <= S.TEXT_CAP and "more characters not sent" in out and cuts
          and cuts[0]["field"] == "problem", str(len(out)))
    check("a field that fits is untouched", S._fit("short", S.TEXT_CAP) == "short")
    parts = S._reply_parts("r" * 20_000, S.TEXT_CAP)
    check("a long reply goes in numbered parts, each within the cap, nothing lost",
          len(parts) == 3 and all(len(p) <= S.TEXT_CAP for p in parts)
          and "".join(p.split("\n", 1)[1] for p in parts) == "r" * 20_000
          and parts[0].startswith("(part 1 of 3)"))

    # the draft: long problem + more files than the bridge carries
    fake = types.ModuleType("brain.mastercode.context_store")

    class CS:
        def __init__(self, pool, eid):
            pass

        async def get(self, path):
            return f"contents of {path}"
    fake.ContextStore = CS
    saved = sys.modules.get("brain.mastercode.context_store")
    sys.modules["brain.mastercode.context_store"] = fake
    stored = {}

    def fr(sql, args):
        if "INSERT INTO support_tickets" in sql:
            stored["args"] = args
            return {"id": 11, "ticket_uuid": "00000000-0000-0000-0000-000000000011",
                    "opened_at": None}
        return None
    try:
        res = run(S.open_service(Rec(fetchrow=fr), 3, category="question", title="t",
                                 problem="p" * 12_000,
                                 files=[f"f{i}.md" for i in range(14)],
                                 include_errors=False, include_inference=False))
    finally:
        if saved is not None:
            sys.modules["brain.mastercode.context_store"] = saved
        else:
            sys.modules.pop("brain.mastercode.context_store", None)
    args = stored.get("args") or ()
    problem_sent = args[6] if len(args) > 6 else ""
    shared = json.loads(args[13]) if len(args) > 13 else []
    included = [i for i in shared if i.get("included")]
    listed_out = [i for i in shared if not i.get("included") and "not included" in i.get("label", "")]
    check("the stored problem is marked where it was cut",
          "more characters not sent" in problem_sent and len(problem_sent) <= S.TEXT_CAP)
    check("the draft's answer names every cut",
          any(c.get("field") == "problem" for c in res.get("cut", []))
          and "tell the user" in res.get("note", ""))
    check("shared items never pass the bridge's 12; the overflow files are LISTED, not dropped",
          len(included) <= S.SHARED_MAX and len(listed_out) >= 1
          and len([i for i in shared if i["key"].startswith("file:")]) == 14,
          f"included={len(included)} listed={len(listed_out)}")
    s = src("server/routes/support.py")
    check("sending more ticked items than the bridge carries is refused in words",
          "untick" in s and "len(approved) > SHARED_MAX" in s)
    check("the bug-report reply cap (the bridge's and operator's) still refuses in words",
          "a reply on a bug report is at most" in s)


def b33():
    from server.routes import coding_protocols as cp
    notes = "N" * 3000 + " end"
    doc, err = cp.validate_protocol({
        "name": "p", "description": "D" * 900 + " end", "notes": notes,
        "steps": [{"id": "a", "engines": [{"engine": "opencode"}], "notes": "S" * 1500 + " end"}]},
        model_opts={"opencode": []})
    check("protocol prose is kept whole (description / notes / step notes)",
          err is None and doc and doc.get("notes") == notes
          and doc.get("description", "").endswith(" end")
          and doc["steps"][0].get("notes", "").endswith(" end"), str(err))
    doc, err = cp.validate_protocol({"name": "p", "author": "A" * 200,
                                     "steps": [{"id": "a", "engines": [{"engine": "opencode"}]}]},
                                    model_opts={"opencode": []})
    check("an over-long author is refused in words, never cut",
          doc is None and "author" in (err or ""), str(err))


def b34():
    from server.routes import github_mirror as GM

    async def tok():
        return "t"

    async def gh(method, url, token=None, **kw):
        if url.endswith("/user/installations?per_page=100"):
            return 200, {"installations": [{"id": 1, "app_slug": "privatae"}]}
        page = int(url.rsplit("page=", 1)[1])
        n = 100 if page <= 4 else 37
        return 200, {"repositories": [
            {"full_name": f"me/repo-{page}-{i}", "updated_at": f"2026-01-{page:02d}",
             "description": "special" if (page, i) == (5, 3) else ""} for i in range(n)]}
    real_t, real_g = GM._keys_github_token, GM._gh
    GM._keys_github_token, GM._gh = tok, gh
    try:
        out = run(GM.github_repos())
        found = run(GM.github_repos(q="special"))
    finally:
        GM._keys_github_token, GM._gh = real_t, real_g
    check("every repository is listed (437 > the old 300)",
          len(out["repos"]) == 437 and out.get("total") == 437 and out["clipped"] is False,
          str(len(out["repos"])))
    check("q searches instead of capping",
          [r["full_name"] for r in found["repos"]] == ["me/repo-5-3"])


# ── a DM thread is read a page at a time ─────────────────────────────────────
class _ThreadPool:
    """Runs the thread readers' own paging: rows newer-first under the
    (sent_at, message_id) cursor, LIMIT, and the count — the same predicates
    the SQL states (pinned by the source checks below)."""

    def __init__(self, rows, *, asc_outer: bool):
        self.rows = rows            # dicts with sent_at, message_id, ...
        self.asc_outer = asc_outer  # circles re-orders the page oldest-first in SQL

    async def fetchval(self, sql, *a):
        return len(self.rows)

    async def execute(self, sql, *a):
        return "UPDATE 0"

    async def fetch(self, sql, *a):
        # The topics of the pair (server/peer/topics.list_topics), which
        # messages.thread_service reads for each message's topic title (the
        # cards): these rows carry no topic, so there is none but the
        # original conversation, which list_topics adds itself.
        if "GROUP BY p.topic" in sql:
            return []
        # messages.sized_page_sql: newest first under the cursor, LIMIT, then
        # rows kept while the running body bytes fit the budget (the first
        # row always), each carrying how many rows the LIMIT allowed.
        cur_ts, cur_id, lim, budget = a[-4], a[-3], a[-2], a[-1]
        self.last_args = a
        key = lambda r: (r["sent_at"], r["message_id"])  # noqa: E731
        older = [r for r in self.rows
                 if cur_ts is None or key(r) < (cur_ts, cur_id)]
        page = sorted(older, key=key, reverse=True)[:lim]
        out, running = [], 0
        for i, r in enumerate(page):
            running += len((r["body"] or "").encode())
            if i == 0 or running <= budget:
                # `topic`: the column the Circles host thread selects
                # (server/peer/topics.topic_sql); a row with no topic reads as
                # the original conversation.
                out.append({"topic": "original", **r, "fetched": len(page)})
            else:
                break
        return out


def r4():
    import uuid as _uuid
    from datetime import datetime, timedelta, timezone
    from server.routes import messages as MS

    t0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
    rows = []
    for i in range(250):
        # 60 messages share ONE timestamp across a page edge — a timestamp
        # cursor alone would skip or repeat them.
        ts = t0 + timedelta(seconds=(i if i < 120 else 120 if i < 180 else i))
        rows.append({"message_id": _uuid.uuid4(), "from_entity_id": 3 if i % 2 else 7,
                     "to_entity_id": 7 if i % 2 else 3, "body": f"m{i}", "sent_at": ts,
                     "read_at": None, "from_name": None, "peer_name": None,
                     "from_user_email": None, "to_user_email": None, "from_user_name": None,
                     "payload": {}, "ts": ts})
    want = sorted(r["body"] for r in rows)
    tid = str(_uuid.uuid4())

    pool = _ThreadPool(rows, asc_outer=False)
    try:
        first = run(MS.thread_service(pool, 3, tid, limit=100))
    except TypeError as e:
        check("thread_service takes limit and before", False, repr(e))
        first = None
    if first is not None:
        seen, page, n = [], first, 0
        while True:
            seen += [m["body"] for m in page["messages"]]
            n += 1
            if not page.get("next_before") or n > 10:
                break
            page = run(MS.thread_service(pool, 3, tid, limit=100, before=page["next_before"]))
        check("a 250-message thread comes back a page at a time with its true total",
              first.get("total") == 250 and len(first["messages"]) == 100
              and first["messages"][-1]["body"] == "m249", (first.get("total"), len(first["messages"])))
        check("paging by next_before returns every message exactly once (60 share a timestamp)",
              sorted(seen) == want and len(seen) == 250 and n == 3, (len(seen), len(set(seen)), n))
        try:
            run(MS.thread_service(pool, 3, tid, before="not-a-cursor"))
            bad = None
        except MS.MessageError as e:
            bad = e
        check("a cursor that is not ours is refused in words (400)",
              bad is not None and bad.status == 400 and "next_before" in bad.detail, bad)

    # ── a page is bounded by rows and by bytes, and its cursor follows ─
    pool = _ThreadPool(rows, asc_outer=False)
    huge = run(MS.thread_service(pool, 3, tid, limit=10**9))
    check("a huge limit is held to the page ceiling (never the whole thread in one read)",
          len(huge["messages"]) <= MS.THREAD_PAGE_MAX and pool.last_args[-2] <= MS.THREAD_PAGE_MAX + 1,
          (len(huge["messages"]), pool.last_args[-2]))
    big = []
    for i in range(40):
        big.append({**rows[i], "message_id": _uuid.uuid4(), "body": f"B{i}:" + "y" * 20_000,
                    "sent_at": t0 + timedelta(minutes=10, seconds=i)})
    bpool = _ThreadPool(big, asc_outer=False)
    budget = MS.thread_page_budget()
    p1 = run(MS.thread_service(bpool, 3, tid, limit=100))
    body_bytes = sum(len(m["body"].encode()) for m in p1["messages"])
    check("a page of long messages stops at the byte budget, and says there is more",
          0 < len(p1["messages"]) < 40 and body_bytes <= max(budget, 20_004)
          and p1.get("next_before"), (len(p1["messages"]), body_bytes, budget))
    seen, page, n = [], p1, 0
    while True:
        seen += [m["body"][:4] for m in page["messages"]]
        n += 1
        if not page.get("next_before") or n > 60:
            break
        page = run(MS.thread_service(bpool, 3, tid, limit=100, before=page["next_before"]))
    check("next_before follows the rows actually returned — every long message once",
          sorted(seen) == sorted(m["body"][:4] for m in big) and len(seen) == 40, (len(seen), n))
    one = [{**rows[0], "message_id": _uuid.uuid4(), "body": "Z" * (budget + 50_000)}]
    po = run(MS.thread_service(_ThreadPool(one, asc_outer=False), 3, tid))
    check("one message larger than the budget still comes back on its own page",
          len(po["messages"]) == 1 and po.get("next_before") is None, len(po["messages"]))

    msrc = src("server/routes/messages.py")
    check("the SQL pages by the compound (sent_at, message_id) cursor, newest first",
          "(sent_at, message_id) < (${cur}::timestamptz, ${cur + 1}::uuid)" in msrc
          and "ORDER BY sent_at DESC, message_id DESC" in msrc
          and "running <= ${budget}::bigint" in msrc)
    tsrc = tools_source()
    check("the host_message tool passes limit and before through",
          'limit=a.get("limit"), before=a.get("before")' in tsrc)
    from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS as S
    hm = S["host_message"]["function"]
    check("the host_message schema says the thread pages and takes before",
          "before" in hm["parameters"]["properties"] and "next_before" in hm["description"])

    # The Circles views of the same threads (Peers = entity, People = dm).
    from server.routes import circles as C
    from server import brain_service as BS
    saved = (BS.get_pool, C._entity_id, C._entity_display_name)
    try:
        cpool = _ThreadPool(rows, asc_outer=True)
        BS.get_pool = lambda: cpool
        C._entity_id = lambda: 3

        async def _name():
            return "Me"
        C._entity_display_name = _name
        try:
            p1 = run(C._read_entity_thread(7, limit=100))
        except TypeError as e:
            check("the Circles host thread takes before and limit", False, repr(e))
            p1 = None
        if p1 is not None:
            seen, page, n = [], p1, 0
            while True:
                seen += [m["body"] for m in page["messages"]]
                n += 1
                if not page.get("next_before") or n > 10:
                    break
                page = run(C._read_entity_thread(7, limit=100, before=page["next_before"]))
            check("the Circles host thread pages to the start, every message once, with total",
                  p1.get("total") == 250 and sorted(seen) == want and len(seen) == 250
                  and p1["messages"][-1]["body"] == "m249", (p1.get("total"), len(seen), n))
        pp = run(C._read_people_thread("peer@x.io", "me@x.io", limit=100)) if p1 is not None else {}
        check("the Circles people thread pages the same way",
              pp.get("total") == 250 and len(pp.get("messages") or []) == 100
              and bool(pp.get("next_before")), {k: v for k, v in pp.items() if k != "messages"})
    finally:
        BS.get_pool, C._entity_id, C._entity_display_name = saved
    csrc = src("server/routes/circles.py")
    check("the Circles thread route passes before through for both DM kinds",
          csrc.count('before=request.query_params.get("before")') >= 2)

    # The Circles UI reads the next page (the cards): the fetcher takes a cursor, the
    # hook keeps it and puts the page before above, the faces offer it.
    api = src("circles-web/src/lib/cards-api.ts")
    hook = src("circles-web/src/cards/use-earlier.tsx")
    faces = src("circles-web/src/cards/faces.tsx") + src("circles-web/src/cards/person-front.tsx")
    check("the Circles UI loads earlier pages (fetchChat before, loadEarlier, the button)",
          "before?: string | null" in api and "next_before" in api
          and "const loadEarlier" in hook and "setOlder((o) => [...(r.data.messages ?? []), ...o])" in hook
          and "Load earlier messages" in hook
          and 'useEarlier("dm", email)' in faces and "useEarlier(\"entity\"" in faces)


# ── security: a peer cannot label its message "outbound" ──────────────
def r6b1():
    from server.bridge import memory_handlers as MH
    from server import brain_service as bs
    import server.routes.domain_policy as DP

    def _send(payload, *, deny_hosts=()):
        pool = Rec(fetchval=lambda sql, a: "11111111-1111-1111-1111-111111111111")
        real = (bs.get_pool, MH._entity_id, DP.check_inbound_allowed)
        asked = []

        async def _policy(pool_, from_address, from_eid, entity_id, kind):
            asked.append((from_eid, kind))
            return not (from_eid is not None and int(from_eid) in deny_hosts)
        bs.get_pool, MH._entity_id, DP.check_inbound_allowed = (lambda: pool), (lambda: 3), _policy
        try:
            out = run(MH.handle_inbox_append(payload))
        finally:
            bs.get_pool, MH._entity_id, DP.check_inbound_allowed = real
        ins = [c for c in pool.calls if "INSERT INTO inbox_messages" in c[0]]
        row = (ins[0][1][2], ins[0][1][3]) if ins else None   # from_entity_id, to_entity_id
        return out, row, asked

    # Exactly what the relay's inbox.send forwards: the SENDING Host's params,
    # with from_entity_id forced to the sender (9) by the bridge.
    spoof = {"subject": "Message", "message_kind": "message", "from_entity_id": 9,
             "from_address": "9@entity.localhost", "sender_verified": True,
             "payload": {}, "direction": "outbound", "outbound_to_entity_id": 3}
    out, row, _ = _send({**spoof, "body": "x" * (2 * 1024 * 1024)})
    check("a 2 MiB message from another host labelled 'outbound' is refused by the bound",
          out.get("too_large") is True and row is None, (out, row))
    out, row, _ = _send({**spoof, "body": "hello"})
    check("a small one labelled 'outbound' still needs the slip like any message (no slip: blocked)",
          out.get("blocked") is True and row is None, (out, row))
    out, row, asked = _send({**spoof, "body": "let me in", "message_kind": "contact_request"},
                            deny_hosts=(9,))
    check("a blocked host stays blocked through the spoof (the admission check runs)",
          out.get("blocked") is True and row is None and (9, "contact_request") in asked,
          (out, row, asked))
    # The bridge's genuine outbound copy carries THIS Host's own id.
    out, row, asked = _send({"subject": "Message", "message_kind": "message", "body": "my own send",
                             "from_entity_id": 3, "from_address": "3@entity.localhost",
                             "direction": "outbound", "outbound_to_entity_id": 9, "payload": {}},
                            deny_hosts=(9,))
    check("a genuine outbound copy (from this Host) still lands, to the peer, unchecked",
          out.get("ok") is True and row == (3, 9)
          and not asked, (out, row, asked))
    out, row, _ = _send({"subject": "Message", "message_kind": "message", "body": "x" * 300_000,
                         "direction": "outbound", "outbound_to_entity_id": 9, "payload": {}})
    check("the local write path (no from_entity_id) still writes an outbound copy, unbounded",
          out.get("ok") is True and row == (3, 9), (out, row))


# ── security: the receiving Host checks the slip itself ──────────────
def r7():
    from server.bridge import memory_handlers as MH
    from server import brain_service as bs
    import server.routes.domain_policy as DP

    mk, trust = _slip_kit()

    def _send(payload, *, verified_slip=None, trust_obj=trust, deny_hosts=()):
        pool = Rec(fetchval=lambda sql, a: "11111111-1111-1111-1111-111111111111")
        real = (bs.get_pool, MH._entity_id, DP.check_inbound_allowed, getattr(MH, "_slip_trust", None))

        async def _policy(pool_, from_address, from_eid, entity_id, kind):
            return not (from_eid is not None and int(from_eid) in deny_hosts)

        async def _t():
            return trust_obj
        bs.get_pool, MH._entity_id, DP.check_inbound_allowed = (lambda: pool), (lambda: 3), _policy
        MH._slip_trust = _t
        try:
            kw = {"verified_slip": verified_slip} if verified_slip is not None else {}
            try:
                out = run(MH.handle_inbox_append(payload, **kw))
            except TypeError as e:
                out = {"ok": False, "error": f"TypeError: {e}"}
        finally:
            bs.get_pool, MH._entity_id, DP.check_inbound_allowed = real[:3]
            if real[3] is not None:
                MH._slip_trust = real[3]
        ins = [c for c in pool.calls if "INSERT INTO inbox_messages" in c[0]]
        stored_payload = None
        if ins:
            import json as _j
            for v in ins[0][1]:
                if isinstance(v, str) and v.startswith("{"):
                    try:
                        stored_payload = _j.loads(v)
                    except ValueError:
                        pass
        return out, bool(ins), stored_payload

    base = {"subject": "Message", "body": "hello", "message_kind": "message",
            "from_entity_id": 9, "from_address": "9@entity.localhost", "payload": {}}
    forged = {**base, "slip_authorized": True, "sender_verified": True,
              "payload": {"authorized_via_slip": True}}

    out, stored, _ = _send(forged)
    check("forged slip_authorized / authorized_via_slip with NO slip are refused, nothing stored",
          out.get("blocked") is True and not stored, (out, stored))

    out, stored, sp = _send({**base, "slip": mk()})
    check("a peer presenting a REAL slip this Host issued it is admitted",
          out.get("ok") is True and stored and (sp or {}).get("authorized_via_slip") is True,
          (out, stored, sp))

    trust.revoked = {"slip-real-1"}
    out, stored, _ = _send({**forged, "slip": mk()})
    trust.revoked = set()
    check("a revoked slip stays refused, whatever flags ride with it",
          out.get("blocked") is True and not stored, (out, stored))

    out, stored, _ = _send({**forged, "message_kind": "contact_request", "body": "let me in"},
                           deny_hosts=(9,))
    check("a blocked host stays blocked — forged flags no longer bypass its domain policy",
          out.get("blocked") is True and not stored, (out, stored))

    out, stored, _ = _send({**base, "slip": mk(issuer=5)})
    check("a slip another host issued is refused (issuer must be this Host)",
          out.get("blocked") is True and not stored, (out, stored))
    out, stored, _ = _send({**base, "slip": mk(grantee=11)})
    check("a slip granted to another host is refused (grantee must be the sender)",
          out.get("blocked") is True and not stored, (out, stored))

    out, stored, _ = _send(base, verified_slip={"issuer": 3, "grantee": 9, "allowed_ops": ["message"]})
    check("the direct lane's in-process verification admits (verified_slip=, not a payload field)",
          out.get("ok") is True and stored, (out, stored))

    out, stored, _ = _send({**base, "slip": mk()}, trust_obj=None)
    check("with no pinned keys / revocation list the slip is not taken on trust (retryable refusal)",
          out.get("ok") is False and out.get("retryable") is True and not stored, (out, stored))

    # ── through the REAL trust lookup (MH._slip_trust unpatched) ──
    import types as _types
    import base64 as _b64
    import server as _srvpkg
    real_lookup = getattr(MH, "_slip_trust", None)

    def _real(payload, *, peer_trust, client=None):
        """Deliver with the real _slip_trust, server.main stubbed to hold the
        peer lane's trust (or none) and a bridge client."""
        had = sys.modules.get("server.main")
        stub = _types.ModuleType("server.main")
        stub._peer_trust_ref = peer_trust
        stub.get_bridge_client = lambda: client
        sys.modules["server.main"] = stub
        _srvpkg.main = stub
        pool = Rec(fetchval=lambda sql, a: "11111111-1111-1111-1111-111111111111")
        saved = (bs.get_pool, MH._entity_id)
        bs.get_pool, MH._entity_id = (lambda: pool), (lambda: 3)
        if real_lookup is not None:
            MH._slip_trust = real_lookup
        try:
            try:
                out = run(MH.handle_inbox_append(payload))
            except Exception as e:  # noqa: BLE001 — a raise is itself the finding
                out = {"ok": False, "raised": f"{type(e).__name__}: {e}"}
        finally:
            bs.get_pool, MH._entity_id = saved
            if had is not None:
                sys.modules["server.main"] = had
                _srvpkg.main = had
            else:
                sys.modules.pop("server.main", None)
        return out, any("INSERT INTO inbox_messages" in c[0] for c in pool.calls)

    out, stored = _real({**base, "slip": mk()}, peer_trust=trust)
    check("through the real lookup, the peer lane's trust admits a real slip",
          out.get("ok") is True and stored, out)

    class _NotReady:
        ready = False
        keys: dict = {}
        revoked: set = set()

        async def refresh(self):
            return None              # cannot sync: stays not ready

        async def ensure_key(self, key_id):
            return False
    out, stored = _real({**base, "slip": mk()}, peer_trust=_NotReady())
    check("a genuinely not-ready trust (real lookup) is a retryable refusal, never an admission",
          out.get("ok") is False and out.get("retryable") is True and not stored, out)

    class _Bridge:
        """contract.signing_keys / revocation_epoch / revoked_since, as the
        bridge answers them; `revoked` can grow between calls."""
        def __init__(self):
            self.revoked, self.epoch, self.calls = [], 1, []

        async def request(self, op, params):
            self.calls.append(op)
            if op == "contract.signing_keys":
                return {"keys": [{"key_id": "k1", "public_key": _b64.b64encode(trust.pub_raw).decode()}]}
            if op == "contract.revocation_epoch":
                return {"epoch": self.epoch}
            if op == "contract.revoked_since":
                return {"revoked": [{"contract_id": c} for c in self.revoked],
                        "epoch": self.epoch, "has_more": False}
            return None
    br = _Bridge()
    MH._SLIP_TRUST, MH._SLIP_TRUST_AT = None, 0.0
    out1, stored1 = _real({**base, "slip": mk()}, peer_trust=None, client=br)
    check("with no peer lane the fallback PeerTrust syncs keys + revocation list and admits a real slip",
          out1.get("ok") is True and stored1 and "contract.signing_keys" in br.calls, (out1, br.calls))
    br.revoked, br.epoch = ["slip-real-1"], 2
    if hasattr(MH, "_SLIP_TRUST_AT"):
        MH._SLIP_TRUST_AT -= getattr(MH, "_TRUST_REFRESH_S", 120.0) + 1
    out2, stored2 = _real({**base, "slip": mk()}, peer_trust=None, client=br)
    check("a slip revoked after the fallback trust was first ready is refused at the next "
          "refresh (every 120 s, the peer lane's own cadence)",
          out2.get("blocked") is True and not stored2, out2)
    check("the fallback refreshes on the peer lane's interval",
          getattr(MH, "_TRUST_REFRESH_S", None) == 120.0 and "run_periodic(120)" in src("server/main.py"))
    MH._SLIP_TRUST, MH._SLIP_TRUST_AT = None, 0.0

    good = mk()
    # Each case breaks ONE field. Where the broken field is a signed term and
    # the shape check must come after the signature (grantee), the slip is
    # RE-SIGNED with it, so the refusal is about the field and never "signature
    # invalid". key_id and terms are checked for shape
    # before any signature work, and the signature case breaks the signature.
    bads = {"terms not a dict": {**good, "terms": "x"},
            "key_id unhashable": {**good, "terms": {**good["terms"], "key_id": ["k1"]}},
            "signature not a string": {**good, "signature": 123},
            "grantee not a number (re-signed)": mk(grantee="abc")}
    for what, sl in bads.items():
        out, stored = _real({**base, "slip": sl}, peer_trust=trust)
        check(f"a malformed slip ({what}) is refused cleanly — no exception, nothing stored",
              "raised" not in out and out.get("blocked") is True and not stored, out)
    from server.peer import slip_verify as SV
    for what, sl in (("not a dict", "x"), ("terms a list", {**good, "terms": [1]}),
                     ("key_id a list", {**good, "terms": {**good["terms"], "key_id": ["k1"]}}),
                     ("grantee not a number (re-signed)", mk(grantee="abc"))):
        try:
            ok, reason, _ = SV.verify_slip(sl, 9, "message", trusted_keys=trust.keys,
                                           revoked_ids=set(), expected_issuer=3)
            r = (ok, reason)
        except Exception as e:  # noqa: BLE001
            r = ("raised", repr(e))
        check(f"verify_slip refuses a slip whose {what} with a plain reason, never raises",
              r[0] is False and "malformed" in r[1], r)

    # The DIRECT lane (peer_server) refuses the same malformed shapes in
    # words — it used to raise on them and close the connection unanswered.
    from server.peer.peer_server import PeerServer

    class _Ready:
        ready = True
        keys = trust.keys
        revoked: set = set()

        async def ensure_key(self, key_id):
            return key_id in self.keys
    delivered = []

    async def _deliver(peer_id, info, params):
        delivered.append(params)
        return {"ok": True}
    ps = PeerServer(trust=_Ready(), handlers={"inbox.deliver": _deliver}, my_entity_id=3)
    for what, sl in (("terms a string", {**good, "terms": "x"}),
                     ("key_id a list", {**good, "terms": {**good["terms"], "key_id": ["k1"]}}),
                     ("grantee not a number (re-signed)", mk(grantee="abc"))):
        delivered.clear()
        try:
            r = run(ps._serve(9, {"op": "inbox.deliver", "slip": sl, "params": {"body": "hi"}}))
        except Exception as e:  # noqa: BLE001 — a raise is the finding
            r = {"raised": f"{type(e).__name__}: {e}"}
        check(f"the direct lane refuses a slip whose {what} as 'malformed slip', answered, "
              "nothing delivered",
              "raised" not in r and r.get("ok") is False and "malformed" in str(r.get("error"))
              and not delivered, r)
    delivered.clear()
    r = run(ps._serve(9, {"op": "inbox.deliver", "slip": good, "params": {"body": "hi"}}))
    check("...and a well-formed slip still delivers on the direct lane",
          r.get("ok") is True and len(delivered) == 1, r)
    # A frame past the reader's line limit is ANSWERED "frame too large".
    class _W:
        def __init__(self):
            self.out = b""

        def get_extra_info(self, k):
            return None

        def write(self, b):
            self.out += b

        async def drain(self):
            return None

        def close(self):
            return None

    async def _big_frame():
        rd = asyncio.StreamReader(limit=1024)
        rd.feed_data(b"x" * 5000 + b"\n")
        rd.feed_eof()
        wr = _W()
        await ps._handle(rd, wr)
        return wr.out
    try:
        out_g = run(_big_frame())
    except Exception as e:  # noqa: BLE001
        out_g = repr(e).encode()
    check("a peer frame past the line limit is answered 'frame too large', not dropped",
          b"frame too large" in out_g, out_g[:120])
    # Over a real socket, a frame of 2 MiB+, 20 MiB with no newline, and a
    # slow sender past the limit all READ the refusal — they used to get
    # a connection reset (the server closed with input unread). An idle half
    # frame is answered when the frame timeout runs out, and a sender that never
    # stops is cut off once that timeout has passed, not held open.
    from server.peer import peer_server as _PS12
    import time as _time12

    async def _r12_peer():
        saved_t = getattr(_PS12, "_FRAME_TIMEOUT_S", None)
        _PS12._FRAME_TIMEOUT_S = 0.6
        server = await asyncio.start_server(ps._handle, "127.0.0.1", 0, limit=_PS12._MAX_REQ + 4096)
        port = server.sockets[0].getsockname()[1]

        async def client(payload, chunk=65536, delay=0.0, forever=False, read_s=5.0):
            r, w = await asyncio.open_connection("127.0.0.1", port, limit=1 << 20)
            t0 = _time12.monotonic()
            try:
                if forever:
                    # like peer_client: read the ONE answer line, and keep
                    # sending — the server must stop reading (the writes then
                    # fail) once the frame timeout has passed
                    async def pump():
                        try:
                            while True:
                                w.write(b"z" * chunk)
                                await w.drain()
                                await asyncio.sleep(0)
                        except Exception:                # noqa: BLE001
                            return _time12.monotonic() - t0
                    task = asyncio.ensure_future(pump())
                    try:
                        out = await asyncio.wait_for(r.readline(), read_s)
                        try:
                            ended = await asyncio.wait_for(asyncio.shield(task), read_s)
                        except asyncio.TimeoutError:
                            ended = None
                    finally:
                        task.cancel()
                    return out, ended
                for i in range(0, len(payload), chunk):
                    w.write(payload[i:i + chunk])
                    await w.drain()
                    if delay:
                        await asyncio.sleep(delay)
                return await asyncio.wait_for(r.read(), read_s), _time12.monotonic() - t0
            except Exception as e:                       # noqa: BLE001
                return ("ERR " + type(e).__name__).encode(), _time12.monotonic() - t0
            finally:
                w.close()
        try:
            return {
                "2MiB": await client(b"x" * (2 << 20) + b"\n"),
                "20MiB no newline": await client(b"x" * (20 << 20)),
                "slow 300KiB": await client(b"x" * (300 * 1024) + b"\n", chunk=4096, delay=0.002),
                "idle half frame": await client(b'{"op":"x"'),
                "never stops": await client(b"", chunk=65536, forever=True, read_s=8.0),
            }
        finally:
            server.close()
            await server.wait_closed()
            if saved_t is None:
                del _PS12._FRAME_TIMEOUT_S
            else:
                _PS12._FRAME_TIMEOUT_S = saved_t
    try:
        r12 = run(_r12_peer())
    except Exception as e:  # noqa: BLE001
        r12 = {"raised": (repr(e).encode(), 0)}
    for case in ("2MiB", "20MiB no newline", "slow 300KiB"):
        out12, _t = r12.get(case, (b"", 0))
        check(f"a peer frame ({case}) past the line limit reads the refusal, not a reset",
              b"frame too large" in out12, out12[:100])
    out12, _t = r12.get("idle half frame", (b"", 0))
    check("a half frame left idle is answered when the frame timeout runs out",
          b"not completed" in out12, out12[:100])
    out12, t12 = r12.get("never stops", (b"", None))
    check("a sender that never stops gets the refusal and is cut off once the frame "
          "timeout has passed (not read from for ever)",
          b"frame too large" in out12 and t12 is not None and t12 < 4.0,
          (out12[:80], t12 if t12 is None else round(t12, 2)))
    # Connections at once are capped per peer and in all (over the cap:
    # closed UNANSWERED so the sender falls back to the relay), and a refused
    # frame's read + drain together stay within ONE frame timeout.
    async def _r13_peer():
        saved = {k: getattr(_PS12, k, None) for k in ("_FRAME_TIMEOUT_S", "_MAX_CONNS_PER_PEER", "_MAX_CONNS")}
        _PS12._FRAME_TIMEOUT_S = 3.0
        out = {}
        server = await asyncio.start_server(ps._handle, "127.0.0.1", 0, limit=_PS12._MAX_REQ + 4096)
        port = server.sockets[0].getsockname()[1]

        async def idle_conn():
            r, w = await asyncio.open_connection("127.0.0.1", port)
            w.write(b'{"op":')              # half a frame: held open by the server
            await w.drain()
            return r, w

        async def extra_conn():
            r, w = await asyncio.open_connection("127.0.0.1", port)
            t0 = _time12.monotonic()
            try:
                w.write(b'{"op":"x"}\n')
                await w.drain()
                got = await asyncio.wait_for(r.read(), 2.0)
            except Exception as e:                       # noqa: BLE001
                got = ("ERR " + type(e).__name__).encode()
            finally:
                w.close()
            return got, _time12.monotonic() - t0
        try:
            for name, per, total in (("per-peer", 2, 100), ("overall", 100, 2)):
                _PS12._MAX_CONNS_PER_PEER, _PS12._MAX_CONNS = per, total
                held = [await idle_conn() for _ in range(2)]
                await asyncio.sleep(0.05)
                out[name] = await extra_conn()
                for _r, _w in held:
                    _w.close()
                await asyncio.sleep(0.1)
            _PS12._MAX_CONNS_PER_PEER, _PS12._MAX_CONNS = 6, 100
            out["after"] = await extra_conn()
            # total time: a slow sender reaches the line limit at ~1 s and keeps
            # sending; the server must stop by the 3 s frame deadline, not 1 + 3 s
            _PS12._FRAME_TIMEOUT_S = 3.0
            r, w = await asyncio.open_connection("127.0.0.1", port)
            t0 = _time12.monotonic()
            ended = None
            try:
                while _time12.monotonic() - t0 < 8:
                    w.write(b"x" * 8192)
                    await w.drain()
                    await asyncio.sleep(0.03)        # ~270 KiB/s: the limit at ~1 s
            except Exception:                        # noqa: BLE001
                ended = _time12.monotonic() - t0
            finally:
                w.close()
            out["slow total"] = ended
        finally:
            server.close()
            await server.wait_closed()
            for k, v in saved.items():
                if v is None:
                    if hasattr(_PS12, k):
                        delattr(_PS12, k)
                else:
                    setattr(_PS12, k, v)
        return out
    try:
        r13 = run(_r13_peer())
    except Exception as e:  # noqa: BLE001
        r13 = {"raised": repr(e)}
    for name in ("per-peer", "overall"):
        got, took = r13.get(name, (b"?", 9))
        check(f"past the {name} connection cap a connection is closed unanswered at once "
              "(the sender falls back to the relay)",
              (got == b"" or got.startswith(b"ERR Connection")) and took < 1.0,
              (got[:60], round(took, 2), r13.get("raised")))
    got, took = r13.get("after", (b"", 9))
    check("once the held connections close, a new one is served again",
          b'"ok"' in got, (got[:80], r13.get("raised")))
    ended = r13.get("slow total")
    check("a slow sender past the line limit is cut off within ONE frame timeout in total "
          "(read + drain), not the read time plus a fresh timeout",
          ended is not None and ended < 3.0 + 0.6, (ended, r13.get("raised")))

    # The per-peer count is keyed on the VERIFIED identity (the
    # client certificate's entity), counts come back on every exit, and a burst
    # from our own sender never meets the cap.
    class _Cert:
        def __init__(self, eid):
            self.eid = eid

        def getpeercert(self):
            return {"subject": ((("commonName", f"domain-{self.eid}-relay"),),)}

    class _CW(_W):
        def __init__(self, eid):
            super().__init__()
            self.ssl = _Cert(eid)
            self.closed = False

        def get_extra_info(self, k):
            return self.ssl if k == "ssl_object" else None

        def close(self):
            self.closed = True

    async def _ident_and_release():
        saved = (_PS12._FRAME_TIMEOUT_S, _PS12._MAX_CONNS_PER_PEER, _PS12._MAX_CONNS)
        _PS12._FRAME_TIMEOUT_S, _PS12._MAX_CONNS_PER_PEER, _PS12._MAX_CONNS = 0.4, 2, 100
        res = {}
        try:
            held = []
            for _ in range(2):                    # entity 11 at its cap, idle frames
                rd = asyncio.StreamReader()
                held.append(asyncio.ensure_future(ps._handle(rd, _CW(11))))
            await asyncio.sleep(0.05)
            res["open11"] = ps._open.get(11)
            w11 = _CW(11)
            await ps._handle(asyncio.StreamReader(), w11)   # a third from 11: closed at once
            res["third11"] = (w11.closed, w11.out)
            rd12 = asyncio.StreamReader()
            rd12.feed_data(b'{"op":"x"}\n')
            rd12.feed_eof()
            w12 = _CW(12)
            await ps._handle(rd12, w12)                      # entity 12 is still served
            res["other12"] = w12.out
            await asyncio.gather(*held)                      # the idle frames time out
            res["after_timeouts"] = (dict(ps._open), ps._open_total)
            # a frame whose handling raises, and one cancelled mid-read
            real = ps._handle_frame

            async def _boom(*a, **k):
                raise RuntimeError("boom")
            ps._handle_frame = _boom
            try:
                await ps._handle(asyncio.StreamReader(), _CW(11))
            except RuntimeError:
                pass
            finally:
                ps._handle_frame = real
            t = asyncio.ensure_future(ps._handle(asyncio.StreamReader(), _CW(11)))
            await asyncio.sleep(0.05)
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
            res["after_raise_cancel"] = (dict(ps._open), ps._open_total)
        finally:
            _PS12._FRAME_TIMEOUT_S, _PS12._MAX_CONNS_PER_PEER, _PS12._MAX_CONNS = saved
        return res
    try:
        r13i = run(_ident_and_release())
    except Exception as e:  # noqa: BLE001
        r13i = {"raised": repr(e)}
    check("the per-peer cap counts the certificate's entity: a third connection from "
          "entity 11 is closed unanswered while entity 12 is served",
          r13i.get("open11") == 2 and r13i.get("third11") == (True, b"")
          and b'"ok"' in (r13i.get("other12") or b""), r13i)
    check("connection counts come back after frame timeouts",
          r13i.get("after_timeouts") == ({}, 0), r13i)
    check("...and after a frame that raises and one that is cancelled",
          r13i.get("after_raise_cancel") == ({}, 0), r13i)

    import server.peer.peer_client as _PC13
    check("the sender's per-peer connection limit is the receiver's per-peer cap",
          _PC13.MAX_CONNS_PER_PEER == _PS12._MAX_CONNS_PER_PEER,
          (_PC13.MAX_CONNS_PER_PEER, _PS12._MAX_CONNS_PER_PEER))

    async def _burst():
        # 30 messages at once from one Host to one peer, over a real socket,
        # each frame held 50 ms by the receiver so they overlap: every one is
        # ANSWERED on the direct lane (none closed into the relay).
        real_frame = ps._handle_frame
        peak = {"n": 0}

        async def _slow(*a, **k):
            peak["n"] = max(peak["n"], ps._open_total)
            await asyncio.sleep(0.05)
            return await real_frame(*a, **k)
        ps._handle_frame = _slow
        # Over a unix socket, not TCP loopback: the peer server answers one frame
        # and closes (peer_server._handle_frame), so each of the 30 connections
        # left a TIME_WAIT on the server's port, and CI's firewall refuses a
        # later connection that lands on one (scripts/gate_loopback.py).
        # The burst tests the caps, which count by peer identity, not address.
        import shutil as _shutil13
        import tempfile as _tempfile13
        sock_dir = _tempfile13.mkdtemp(prefix="caps-g2-burst-")
        sock_path = os.path.join(sock_dir, "peer.sock")
        server = await asyncio.start_unix_server(ps._handle, sock_path, limit=_PS12._MAX_REQ + 4096)
        real_open = asyncio.open_unix_connection

        async def _oc(host, p, ssl=None, server_hostname=None, limit=2 ** 16):
            return await real_open(sock_path, limit=limit)
        saved = (_PC13.asyncio, _PC13._ctx, _PC13._peer_host)
        _PC13.asyncio = types.SimpleNamespace(
            **{k: getattr(asyncio, k) for k in dir(asyncio) if not k.startswith("__")})
        _PC13.asyncio.open_connection = _oc
        _PC13._ctx, _PC13._peer_host = (lambda: None), (lambda eid: "peer")
        try:
            got = await asyncio.gather(*[_PC13.send_to_peer(5, "x", None, {"i": i}) for i in range(30)],
                                       return_exceptions=True)
        finally:
            _PC13.asyncio, _PC13._ctx, _PC13._peer_host = saved
            ps._handle_frame = real_frame
            server.close()
            await server.wait_closed()
            _shutil13.rmtree(sock_dir, ignore_errors=True)
        return got, peak["n"], dict(_PC13._SLOTS)
    try:
        burst, peak13, slots13 = run(_burst())
    except Exception as e:  # noqa: BLE001
        burst, peak13, slots13 = [repr(e)], -1, None
    unanswered = [b for b in burst if not isinstance(b, dict)]
    check("a burst of 30 messages to one peer is answered on the direct lane, none "
          "closed into the relay (the sender queues past the per-peer cap)",
          len(burst) == 30 and not unanswered and 0 < peak13 <= _PS12._MAX_CONNS_PER_PEER,
          (len(burst), [str(u)[:80] for u in unanswered[:2]], peak13))
    check("...and the sender's slot table is empty afterwards", slots13 == {}, slots13)
    # A request that is not one is answered, never raised on.
    for what, req in (("a list", []), ("a string", "x"), ("an op that is a list", {"op": ["inbox.deliver"]}),
                      ("an op that is a dict", {"op": {"a": 1}})):
        try:
            r = run(ps._serve(9, req))
        except Exception as e:  # noqa: BLE001 — a raise is the finding
            r = {"raised": f"{type(e).__name__}: {e}"}
        check(f"the direct lane answers a request that is {what} with 'malformed request'",
              "raised" not in r and r.get("ok") is False and r.get("error") == "malformed request", r)

    skew = getattr(SV, "CLOCK_SKEW_S", None)
    check("a symmetric clock-skew allowance exists, under the bridge's shortest slip (60 s)",
          isinstance(skew, int) and 0 < skew < 60, skew)
    out, stored = _real({**base, "slip": mk(exp_s=-10)}, peer_trust=trust)
    check("a slip expired 10 s ago (inside the skew) is still honoured",
          out.get("ok") is True and stored, out)
    out, stored = _real({**base, "slip": mk(exp_s=-90)}, peer_trust=trust)
    check("a slip expired 90 s ago (past the skew) is refused",
          out.get("blocked") is True and not stored, out)
    out, stored = _real({**base, "slip": mk(issued_s=600)}, peer_trust=trust)
    check("a slip issued 10 minutes in the future (not-before) is refused",
          out.get("blocked") is True and not stored, out)

    hsrc = src("server/peer/handlers.py")
    i = hsrc.find("async def inbox_deliver")
    body = hsrc[i:hsrc.find("\nasync def ", i + 10)]
    check("the direct lane passes its verified slip in-process and sets no attestation flags",
          "handle_inbox_append(payload, verified_slip=slip_info)" in body
          and '"slip_authorized": True' not in body and 'inner["authorized_via_slip"] = True' not in body)


# ── the REAL _dispatch_box runs (only the relay transport is faked) ──
def r11b1():
    import base64 as _b64
    from server.network import nac_dcn as D
    from server.network import nac_approval as NA
    files = {"baseline.json": b'{"taken_at":"x","entries":[]}'}

    class _Relay:
        def worker_count(self, t):
            return 1

        async def dispatch(self, token_id, op, args, timeout):
            if op == "device_request":
                return json.dumps({"state": "pending", "id": 5}).encode(), b"", 0
            p = json.loads(args[1])
            if op == "dcn_stat":
                out = {"state": "ok", "path": p["path"], "size_bytes": len(files[p["path"]])}
            else:
                body = files[p["path"]][p["start_byte"]:p["end_byte"]]
                out = {"state": "ok", "path": p["path"], "start_byte": p["start_byte"],
                       "end_byte": p["start_byte"] + len(body), "size_bytes": len(files[p["path"]]),
                       "content_b64": _b64.b64encode(body).decode()}
            return json.dumps(out).encode(), b"", 0

    class _Pool:
        async def fetchval(self, *a):
            return 7

    async def _gate(pool, eid, store_id, op):
        return {"store_id": store_id, "terms": {"server": "box1", "class": "machine"}}, True, ""

    async def _noop(*a, **k):
        return None
    saved = (D.get_mgmt_relay, D._gate, D.audit_mgmt, D._tombstone)
    D.get_mgmt_relay = lambda: _Relay()
    D._gate, D.audit_mgmt, D._tombstone = _gate, _noop, _noop
    try:
        for op in ("stat", "read_range"):
            try:
                r = run(D.run_dcn_op(_Pool(), 1, 7, op, {"path": "baseline.json", "start_byte": 0}))
            except Exception as e:  # noqa: BLE001 — a raise is the finding
                r = {"raised": f"{type(e).__name__}: {e}"}
            check(f"a machine DCN {op} through the REAL _dispatch_box answers ok",
                  r.get("state") == "ok" and "raised" not in r, r)
        try:
            r = run(NA._device_file(_Pool(), 1, {"hostname": "box1", "token_id": 7},
                                    {"server": "box1"}, "box1"))
        except Exception as e:  # noqa: BLE001
            r = {"raised": f"{type(e).__name__}: {e}"}
        check("a non-DCN caller (nac_approval device_request) through the REAL _dispatch_box",
              "raised" not in r and r.get("state") == "pending", r)
    finally:
        D.get_mgmt_relay, D._gate, D.audit_mgmt, D._tombstone = saved


# ── a group thread pages through the bridge's cursor ─────
class _CircleBridge:
    """circles.messages as the platform bridge states it: newest
    first by (created_at, id), `limit`, an opaque `before` it issued, and
    total / limit / next_before; a 400 sentence for a cursor it did not
    issue. `paged=False` is an older bridge: the newest `limit`, no new keys."""
    _connected = True
    SIG = "0f" * 16

    def __init__(self, rows, paged=True):
        self.rows, self.paged, self.calls = rows, paged, []

    def _page(self, params):
        key = lambda r: (r["created_at"], r["id"])  # noqa: E731
        lim = max(1, min(int(params.get("limit") or 100), 500))
        rows = sorted(self.rows, key=key, reverse=True)
        b = params.get("before")
        if b:
            try:
                ts, rid, sig = b.split("|")
                assert sig == self.SIG
                cut = (ts, int(rid))
            except Exception:  # noqa: BLE001
                return None
            rows = [r for r in rows if key(r) < cut]
        page = rows[:lim]
        out = {"messages": page}
        if self.paged:
            more = len(rows) > lim
            out.update(total=len(self.rows), limit=lim,
                       next_before=(f"{page[-1]['created_at']}|{page[-1]['id']}|{self.SIG}"
                                    if more and page else None))
        return out

    async def request(self, op, params):
        self.calls.append(dict(params))
        return self._page(params)

    async def request_full(self, op, params):
        self.calls.append(dict(params))
        data = self._page(params)
        if data is None:
            return {"ok": False, "error": ("before must be a next_before this op returned "
                                           "for this circle; start again without it to read "
                                           "from the newest message")}
        return {"ok": True, "data": data}


def g4x4():
    from datetime import datetime, timedelta, timezone
    from starlette.requests import Request
    from fastapi import HTTPException
    from server.routes import circles as C

    t0 = datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc)
    rows = []
    for i in range(250):
        # 60 messages share ONE timestamp across a page edge.
        sec = i if i < 120 else 120 if i < 180 else i
        rows.append({"id": 1000 + i, "thread_id": 1, "session_id": 9,
                     "sender_entity_id": 4 if i % 2 else 3, "sender_type": "entity",
                     "content": f"g{i}", "created_at": (t0 + timedelta(seconds=sec)).isoformat()})
    want = sorted(r["content"] for r in rows)

    def req(before=None):
        qs = f"before={before}".encode() if before else b""
        from urllib.parse import quote
        if before:
            qs = b"before=" + quote(before, safe="").encode()
        return Request({"type": "http", "method": "GET", "path": "/", "headers": [],
                        "query_string": qs})

    saved = (C.server_main.get_bridge_client, C._entity_id, C._entity_display_name)
    try:
        C._entity_id = lambda: 3

        async def _name():
            return "Me"
        C._entity_display_name = _name

        fb = _CircleBridge(rows)
        C.server_main.get_bridge_client = lambda: fb
        p1 = run(C.get_thread("circle", "5", req()))
        seen, page, n = [], p1, 0
        while True:
            seen += [m["body"] for m in page["messages"]]
            n += 1
            if not page.get("next_before") or n > 10:
                break
            page = run(C.get_thread("circle", "5", req(page["next_before"])))
        check("a 250-message group thread pages to the start, every message exactly once",
              p1.get("total") == 250 and sorted(seen) == want and len(seen) == 250 and n >= 2,
              (p1.get("total"), len(seen), len(set(seen)), n))
        check("each page is shown oldest-first, newest page first, with stable ids",
              p1["messages"][-1]["body"] == "g249"
              and all(m.get("id", "").startswith("circle-") for m in p1["messages"]))
        check("the cursor goes to the bridge unchanged",
              any(c.get("before") == p1.get("next_before") for c in fb.calls) and p1.get("next_before"))
        try:
            run(C.get_thread("circle", "5", req("2026-10-02T09:00:00+00:00|1|forged")))
            bad = None
        except HTTPException as e:
            bad = e
        check("a cursor the bridge did not issue is its 400 in words, not an empty thread",
              bad is not None and bad.status_code == 400 and "next_before" in str(bad.detail), bad)

        old = _CircleBridge(rows, paged=False)
        C.server_main.get_bridge_client = lambda: old
        po = run(C.get_thread("circle", "5", req()))
        check("an older bridge (no next_before) still shows what came back, no earlier control",
              len(po.get("messages") or []) > 0 and po.get("next_before") is None
              and "total" not in po and po["messages"][-1]["body"] == "g249", 
              {k: v for k, v in po.items() if k != "messages"})
    finally:
        C.server_main.get_bridge_client, C._entity_id, C._entity_display_name = saved

    faces = src("circles-web/src/cards/faces.tsx")
    hook = src("circles-web/src/cards/use-earlier.tsx")
    check("the Circles UI offers earlier pages on a group thread too",
          'useEarlier("circle", id)' in faces and "<Earlier pages={pages}" in faces)
    # An earlier page is applied only to the conversation it was asked
    # for: a change of conversation bumps the request count, and an answer
    # for an older request is dropped.
    i = hook.find("const loadEarlier")
    body = hook[i:i + 900]
    check("a load-earlier answer is tied to its request and conversation; a stale one is dropped",
          "const req = ++reqRef.current;" in body and "if (req !== reqRef.current) return;" in body
          and "reqRef.current += 1;" in hook and "}, [key]);" in hook)
    # The merge keeps an older row sharing the window's first timestamp
    # (its behaviour is circles-web/test/merge-history.test.ts).
    hm = src("circles-web/src/lib/history-merge.ts")
    check("mergeHistory keeps a row at the window's first timestamp that is not in the window",
          "m.createdAt <= firstServer.createdAt" in hm)


for label, fn in [("approval card", b16), ("MCP limiter and body", b14_b15), ("host-to-host message", b17),
                  ("ladder evidence", b18), ("proposal files", b20), ("list paging", b21),
                  ("activity cursor", n6), ("support ticket", b22), ("coding protocol", b33),
                  ("GitHub picker", b34), ("DM thread paging", r4), ("group thread paging", g4x4),
                  ("outbound label", r6b1), ("slip check", r7), ("dispatch box", r11b1)]:
    guard(label, fn)

print(f"\n{'PASS' if not FAILED else 'FAIL'} ({len(FAILED)} failing)")
sys.exit(1 if FAILED else 0)
