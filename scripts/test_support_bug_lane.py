#!/usr/bin/env python3
"""
Gate — the support route's bug lane (server/routes/support.py).

Proves that when the user presses Send on a BUG ticket, the approved card
becomes a pull request through `propose_bug_report` and never a
`platform.ticket` row; that every other category still goes to the operator's
queue; and that the pod mirrors a bug PR's state back through
`proposal.status` in the Tickets vocabulary, closing the row on fixed/rejected.
Pure: the pool, the bridge and the propose call are fakes.
  python3 scripts/test_support_bug_lane.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

# brain.mastercode.runtime's __init__ imports the whole session stack; the
# support route imports capability_propose lazily by that name, so seed the
# module the route will find with a recorder.
_calls: list[dict] = []
_env = {"ok": True, "pr": 12, "url": "https://forge.example/Community/bugs/pulls/12",
        "branch": "propose/entity-9003-8f3a1c2e-x", "repo": "Community/bugs",
        "path": "bugs/8f3a1c2e-x.md"}


class _CredUnavailable(RuntimeError):
    pass


class _Conflict(RuntimeError):
    pass


async def _fake_propose(*, gitea_user, ticket, shared_context):
    _calls.append({"gitea_user": gitea_user, "ticket": ticket, "shared": shared_context})
    if isinstance(_env, Exception):
        raise _env
    return dict(_env)


_stub = types.ModuleType("brain.mastercode.runtime.capability_propose")
_stub.propose_bug_report = _fake_propose
_stub.ProposalCredentialUnavailable = _CredUnavailable
_stub.ProposalConflict = _Conflict
for name in ("brain", "brain.mastercode", "brain.mastercode.runtime"):
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules["brain.mastercode.runtime.capability_propose"] = _stub
# reply_service redacts through brain.mastercode.secret_shapes; with the package
# stubbed above, give it the real module's contract (redact -> (text, findings)).
_shapes = types.ModuleType("brain.mastercode.secret_shapes")
_shapes.redact = lambda text: (text, [])
sys.modules["brain.mastercode.secret_shapes"] = _shapes
sys.modules["brain.mastercode"].secret_shapes = _shapes
os.environ["GITEA_USERNAME"] = "entity-9003"

from server.routes import support as sp  # noqa: E402

_failures = 0


def check(label, cond, detail=""):
    global _failures
    if cond:
        print(f"OK    {label}")
    else:
        _failures += 1
        print(f"FAIL  {label}" + (f" — {detail}" if detail else ""))


class FakePool:
    """One support_tickets row; records every UPDATE's params."""
    def __init__(self, row):
        self.row = dict(row)
        self.executes = []

    async def fetchrow(self, sql, *params):
        return dict(self.row)

    async def fetch(self, sql, *params):
        return [dict(self.row)]

    async def execute(self, sql, *params):
        self.executes.append((sql, params))
        return "UPDATE 1"


bridge_calls: list[tuple[str, dict]] = []
bridge_answers: dict[str, dict] = {}


async def _fake_bridge(op, params):
    bridge_calls.append((op, params))
    return dict(bridge_answers.get(op) or {})


sp._bridge = _fake_bridge

SHARED = [
    {"key": "versions", "label": "v", "kind": "versions", "content": "cseries v0.16.83", "included": True},
    {"key": "errors", "label": "e", "kind": "errors", "content": "boom", "included": False},
    {"key": "file:notes.md", "label": "f", "kind": "file", "content": "x", "included": True},
]
ROW = {"id": 5, "entity_id": 9003, "ticket_uuid": "8f3a1c2e-1111-2222-3333-444455556666",
       "category": "bug", "project_name": "", "title": "Tickets pane loses drafts",
       "severity": "high", "problem": "Drafts vanish", "context": "", "recommendation": "",
       "pushback": "", "repro_steps": "1. open", "expected": "stays", "actual": "gone",
       "shared_context": json.dumps(SHARED), "status": "draft", "metadata": "{}",
       "thread": "[]", "platform_id": None, "platform_status": "", "platform_ref": "",
       "resolution": "", "pr_url": ""}


def run(coro):
    return asyncio.run(coro)


# ── Send on a bug: a PR, never a platform ticket ────────────────────────────
pool = FakePool(ROW)
res = run(sp.send_service(pool, 9003, 5))
check("bug send: propose_bug_report called exactly once", len(_calls) == 1, str(len(_calls)))
check("bug send: platform.ticket was NOT called",
      not any(op == "platform.ticket" for op, _ in bridge_calls), str(bridge_calls))
check("bug send: EXACTLY the ticked items ride along (unticked one dropped)",
      [i["key"] for i in _calls[0]["shared"]] == ["versions", "file:notes.md"],
      str([i["key"] for i in _calls[0]["shared"]]))
check("bug send: items are stripped to key/label/kind/content",
      all(set(i) == {"key", "label", "kind", "content"} for i in _calls[0]["shared"]),
      str(_calls[0]["shared"]))
check("bug send: the card fields and the ticket uuid reach the author",
      _calls[0]["ticket"]["problem"] == "Drafts vanish"
      and _calls[0]["ticket"]["ticket_uuid"] == ROW["ticket_uuid"], str(_calls[0]["ticket"]))
check("bug send: the host's gitea identity is the reporter", _calls[0]["gitea_user"] == "entity-9003")
check("bug send: result says sent / received and names the report PR",
      res["status"] == "sent" and res["platform_status"] == "received"
      and res["platform_ref"] == "Community/bugs#12" and res["platform_id"] is None, str(res))
sql, params = pool.executes[-1]
check("bug send: row moves to sent with platform_id NULL and lane=bug_pr in metadata",
      "status = 'sent'" in sql and "platform_id = NULL" in sql
      and json.loads(params[1]).get("lane") == "bug_pr"
      and json.loads(params[1]).get("bug_pr") == 12, str(params))
check("bug send: pr_url is NOT set — it means the FIX, not the report",
      "pr_url" not in sql, sql)

# ── every other category still goes to the operator's queue ────────────────
_calls.clear()
bridge_calls.clear()
bridge_answers["platform.ticket"] = {"ticket_id": 77, "status": "received"}
pool_q = FakePool({**ROW, "category": "question"})
res = run(sp.send_service(pool_q, 9003, 5))
check("question send: platform.ticket open, no PR",
      bridge_calls and bridge_calls[0][0] == "platform.ticket"
      and bridge_calls[0][1]["op"] == "open" and not _calls, str(bridge_calls))
check("question send: platform_id set", res["platform_id"] == 77, str(res))

# ── failures are told plainly and the draft survives ───────────────────────
_env_saved = _env
_env = _CredUnavailable("gitea_proposals credential missing")
pool_f = FakePool(ROW)
try:
    run(sp.send_service(pool_f, 9003, 5))
    check("bug send: missing platform credential is a SupportError", False, "did not raise")
except sp.SupportError as e:
    check("bug send: missing platform credential is a 503 that says so",
          e.status == 503 and "cannot file bug reports" in e.detail, e.detail)
check("bug send: on failure the draft is untouched", pool_f.executes == [], str(pool_f.executes))
_env = _Conflict("branch holds a different file")
try:
    run(sp.send_service(FakePool(ROW), 9003, 5))
    check("bug send: a platform 409 is a 409 in plain words with a way out", False, "did not raise")
except sp.SupportError as e:
    check("bug send: a platform 409 is a 409 in plain words with a way out",
          e.status == 409 and "send again" in e.detail and "branch" not in e.detail, e.detail)
_env = {**_env_saved, "pr": 0}
try:
    run(sp.send_service(FakePool(ROW), 9003, 5))
    check("bug send: an ok with no PR number is a failed send", False, "did not raise")
except sp.SupportError as e:
    check("bug send: an ok with no PR number is a failed send (502)", e.status == 502, e.detail)
_env = _env_saved

# ── the mirror: proposal.status → platform_status, closing on terminal ─────
bridge_calls.clear()
SENT = {**ROW, "status": "sent", "platform_status": "received",
        "platform_ref": "Community/bugs#12",
        "metadata": json.dumps({"lane": "bug_pr", "bug_pr": 12})}
bridge_answers["proposal.status"] = {"repo": "Community/bugs", "reports": [
    {"number": 12, "status": "validated", "pr_url": "", "resolution": "",
     "events": [{"body": "Which browser?", "at": "2026-09-23T10:00:00Z"}]}]}
pool_s = FakePool(SENT)
out = run(sp.refresh_service(pool_s, 9003))
check("refresh: a bug_pr row is mirrored through proposal.status, not platform.ticket",
      [op for op, _ in bridge_calls] == ["proposal.status"]
      and bridge_calls[0][1] == {"kind": "bug", "numbers": [12]}, str(bridge_calls))
check("refresh: one row moved", out == {"refreshed": 1}, str(out))
sql, params = pool_s.executes[-1]
check("refresh: platform_status becomes validated and the note lands in the thread",
      params[0] == "validated" and json.loads(params[3])[0]["body"] == "Which browser?"
      and params[4] is False, str(params))

bridge_calls.clear()
bridge_answers["proposal.status"] = {"reports": [
    {"number": 12, "status": "fixed", "pr_url": "https://forge.example/owner/platform/pulls/700",
     "resolution": "Fixed in #700", "events": []}]}
pool_x = FakePool(SENT)
run(sp.refresh_service(pool_x, 9003))
sql, params = pool_x.executes[-1]
check("refresh: fixed carries the FIX url + resolution and closes the row",
      params[0] == "fixed" and params[2].endswith("/pulls/700") and params[1] == "Fixed in #700"
      and params[4] is True, str(params))

bridge_calls.clear()
bridge_answers["proposal.status"] = {"reports": [{"number": 12, "status": "haunted"}]}
pool_u = FakePool(SENT)
run(sp.refresh_service(pool_u, 9003))
check("refresh: an unknown status from the platform is ignored, not mirrored",
      pool_u.executes == [], str(pool_u.executes))

bridge_calls.clear()
bridge_answers["proposal.status"] = {"reports": [{"number": 12, "status": "received", "pr_url": "",
                                                   "resolution": "", "events": []}]}
pool_n = FakePool({**SENT})
run(sp.refresh_service(pool_n, 9003))
check("refresh: nothing moved → updated_at untouched", pool_n.executes == [], str(pool_n.executes))


# ── many bug rows are batched, and a bug-lane failure never stops
#    the platform-ticket lane ─────────────────────────────────────────────────
class ManyPool(FakePool):
    def __init__(self, rows):
        super().__init__(rows[0])
        self.rows = rows

    async def fetch(self, sql, *params):
        # The selection clause is pinned here rather than parsed: NULL-platform_id
        # rows are picked up ONLY through the bug_pr lane marker.
        check("refresh SQL selects NULL-platform_id rows only by lane=bug_pr",
              "metadata->>'lane' = 'bug_pr'" in sql and "platform_id IS NOT NULL OR" in sql, sql)
        return [dict(r) for r in self.rows]


bridge_calls.clear()
bug_rows = [{**SENT, "id": 100 + i, "metadata": json.dumps({"lane": "bug_pr", "bug_pr": 1000 + i})}
            for i in range(51)]
plat_row = {**ROW, "id": 5, "status": "sent", "platform_id": 77, "platform_status": "received"}
bridge_answers["proposal.status"] = {"reports": []}
bridge_answers["platform.ticket"] = {"tickets": []}
run(sp.refresh_service(ManyPool(bug_rows + [plat_row]), 9003))
status_calls = [p for op, p in bridge_calls if op == "proposal.status"]
check("refresh: 51 bug rows → two proposal.status calls of ≤50 numbers",
      len(status_calls) == 2 and [len(c["numbers"]) for c in status_calls] == [50, 1], str([len(c["numbers"]) for c in status_calls]))
check("refresh: the platform-ticket lane still ran after the bug lane",
      any(op == "platform.ticket" for op, _ in bridge_calls), str([op for op, _ in bridge_calls]))

bridge_calls.clear()
_orig_bridge = sp._bridge


async def _failing_bridge(op, params):
    bridge_calls.append((op, params))
    if op == "proposal.status":
        raise sp.SupportError(503, "the operator holds no proposals credential")
    return {"tickets": []}


sp._bridge = _failing_bridge
out = run(sp.refresh_service(ManyPool([bug_rows[0], plat_row]), 9003))
sp._bridge = _orig_bridge
check("refresh: a bug-lane failure is swallowed and the platform lane still runs",
      out == {"refreshed": 0} and [op for op, _ in bridge_calls] == ["proposal.status", "platform.ticket"],
      str(bridge_calls))

# ── a reply on a bug row lands on its report PR ─────────────────────────────
bridge_calls.clear()
bridge_answers["proposal.reply"] = {"number": 12, "replied": True}
pool_r = FakePool({**SENT, "thread": "[]"})
res = run(sp.reply_service(pool_r, 9003, 5, body="Firefox 131", author="user"))
check("reply on a bug row → proposal.reply with the report number",
      bridge_calls == [("proposal.reply", {"kind": "bug", "number": 12, "body": "Firefox 131"})]
      and res["thread_len"] == 1, str(bridge_calls))
bridge_calls.clear()
try:
    run(sp.reply_service(FakePool({**SENT, "thread": "[]"}), 9003, 5, body="x" * 4001, author="user"))
    check("reply on a bug row longer than the bridge cap is a 400 that says so", False, "did not raise")
except sp.SupportError as e:
    check("reply on a bug row longer than the bridge cap is a 400 that says so",
          e.status == 400 and "4000" in e.detail and bridge_calls == [], e.detail)
bridge_calls.clear()
pool_r2 = FakePool({**SENT, "metadata": "{}", "platform_id": None})
try:
    run(sp.reply_service(pool_r2, 9003, 5, body="x"))
    check("reply on a sent row with no destination is a 409", False, "did not raise")
except sp.SupportError as e:
    check("reply on a sent row with no destination is a 409 that says so",
          e.status == 409 and "nowhere to reply" in e.detail, e.detail)

# ── transport failures are 502s; a missing GITEA_USERNAME does not block a Send
_calls.clear()
_env_saved2 = _env
_env = OSError("connection reset")
try:
    run(sp.send_service(FakePool(ROW), 9003, 5))
    check("bug send: a transport error is a SupportError", False, "did not raise")
except sp.SupportError as e:
    check("bug send: a transport error is a 502, not a 500", e.status == 502 and "OSError" in e.detail, e.detail)
_env = _env_saved2
_calls.clear()
del os.environ["GITEA_USERNAME"]
run(sp.send_service(FakePool(ROW), 9003, 5))
check("bug send: without GITEA_USERNAME the reporter is entity-<id>, and the Send goes through",
      _calls and _calls[0]["gitea_user"] == "entity-9003", str(_calls[:1]))

print()
if _failures:
    print(f"FAILED: {_failures} check(s)")
    sys.exit(1)
print("PASSED: support bug lane (send → PR, mirror ← proposal.status)")
