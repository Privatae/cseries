#!/usr/bin/env python3
"""
Gate — where a support request can go, decided before anything is drafted
(server/support_reach.py, server/routes/support.py, the tickets tool's
support_open, default-skills/tickets.md).

  1. The three connection states, from the client the Host actually holds
     and that client's own op list: standalone (NoPlatform), a network member
     (the real MemberClient and its NETWORK_OPS: the members' board today,
     the private `support.ticket` op the day the door lists it), and the
     platform's bridge (unchanged).
  2. support_open on a Host that cannot send a private ticket drafts NOTHING
     and answers with the owner's sentence and a PUBLIC text that holds no
     private context.
  3. Send on such a Host (a draft made before it left the network) is refused
     in the owner's words, never "bridge not connected" or "not offered by
     the network"; a member door that offers `support.ticket` is used.
  4. The skill never tells the Host to say it drafted a ticket it can't send.

Pure: the pool and the clients' connections are fakes; invented values only.
  python3 scripts/test_support_reach.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "brain"))

from server import support_reach as sr  # noqa: E402
from server.bridge.bridge_client import BridgeClient  # noqa: E402
from server.bridge.member_client import NETWORK_OPS, MemberClient  # noqa: E402
from server.bridge.no_platform import NoPlatform  # noqa: E402
from server.routes import support as sp  # noqa: E402

_failures = 0


def check(label, cond, detail=""):
    global _failures
    if cond:
        print(f"OK    {label}")
    else:
        _failures += 1
        print(f"FAIL  {label}" + (f" — {detail}" if detail else ""))


def run(coro):
    return asyncio.run(coro)


RAW = ("bridge not connected", "not offered by the network", "no platform: this Host runs standalone")

# ── the clients: real classes, connections faked ─────────────────────────────

_passport = types.SimpleNamespace(chain_path="/nonexistent/chain.pem", key_path="/nonexistent/key.pem",
                                  root_path="/nonexistent/root.crt")


class Recorder:
    """Records what reaches the wire instead of a socket."""
    def __init__(self, answers=None):
        self.calls: list[tuple[str, dict]] = []
        self.answers = answers or {}

    async def __call__(self, op, params=None):
        self.calls.append((op, params))
        return {"ok": True, "data": dict(self.answers.get(op) or {})}


def member(extra_ops=(), connected=True):
    m = MemberClient(_passport)
    m._connected = connected
    if extra_ops:
        m.offered_ops = frozenset(NETWORK_OPS | set(extra_ops))
    rec = Recorder({"support.ticket": {"ticket_id": 41, "status": "received"},
                    "community.categories": {"categories": [
                        {"slug": "ideas", "name": "Ideas"}, {"slug": "help", "name": "Help"}]}})
    # The member client's own filter runs first (MemberClient.request_full);
    # only an offered op reaches the recorder in place of the socket.
    BridgeClient_request_full = rec
    async def request_full(op, params=None, _m=m):
        if not _m.offers(op):
            return _m._not_offered(op)
        return await BridgeClient_request_full(op, params)
    m.request_full = request_full
    return m, rec


def platform(connected=True):
    b = BridgeClient(host="platform.invalid", port=1)
    b._connected = connected
    rec = Recorder({"platform.ticket": {"ticket_id": 77, "status": "received"}})
    b.request_full = rec
    return b, rec


# ── 1. the three connection states ──────────────────────────────────────────

check("standalone: NoPlatform carries no op", NoPlatform().offers("platform.ticket") is False
      and NoPlatform().offers("community.post") is False)
check("standalone: lane", sr.lane_of(NoPlatform()) == sr.LANE_STANDALONE)
check("standalone: no client at all is standalone too", sr.lane_of(None) == sr.LANE_STANDALONE)
m_today, _ = member()
check("member: the real client's op list has the board and no private support op",
      m_today.offers("community.post") and not m_today.offers("platform.ticket")
      and not m_today.offers(sr.SUPPORT_OP))
check("member today: lane is the members' board", sr.lane_of(m_today) == sr.LANE_BOARD)
m_future, _ = member(extra_ops=(sr.SUPPORT_OP,))
check("member whose door lists support.ticket: lane is the private network ticket (feature-detected)",
      sr.lane_of(m_future) == sr.LANE_NETWORK)
m_bare = MemberClient(_passport)
m_bare.offered_ops = frozenset({"heartbeat.ping"})
check("member with neither op: its own lane", sr.lane_of(m_bare) == sr.LANE_MEMBER_NONE)
b, _ = platform()
check("platform bridge: lane unchanged", sr.lane_of(b) == sr.LANE_PLATFORM)

v = sr.route_view(sr.LANE_STANDALONE)
check("route standalone: cannot send, says so, links a new public issue and the security address",
      not v["can_send"] and sr.NOT_ON_NETWORK in v["say"]
      and v["issue_url"] == "https://github.com/Privatae/cseries/issues/new"
      and v["security"]["email"] == "security@privatae.ai", json.dumps(v))
v = sr.route_view(sr.LANE_BOARD)
check("route board: cannot send privately, offers the board, says every member can read it",
      not v["can_send"] and v.get("board") and "Every member can read the board" in v["say"]
      and "issue_url" not in v, json.dumps(v))
# The card shows head and offer as they are (circles-web support-front.tsx).
for _lane in (sr.LANE_STANDALONE, sr.LANE_BOARD, sr.LANE_MEMBER_NONE):
    v = sr.route_view(_lane)
    check(f"route {_lane}: the card's offer says what the cut does and can miss, never that names "
          "and addresses are left out",
          v.get("offer") and "names and addresses" not in v["offer"]
          and "can miss a name or an address written in plain words" in v["offer"]
          and "read the text before it goes" in v["offer"], json.dumps(v)[:600])
v = sr.route_view(sr.LANE_MEMBER_NONE)
check("route member_none: a member is never told this Host isn't on the network",
      v.get("head") == sr.NO_PRIVATE_SUPPORT and "isn't on the Privatae network" not in json.dumps(v),
      json.dumps(v)[:600])
check("route standalone: the head says it isn't on the network",
      sr.route_view(sr.LANE_STANDALONE).get("head") == sr.NOT_ON_NETWORK)
check("route platform / network: can send, nothing to say",
      sr.route_view(sr.LANE_PLATFORM)["can_send"] and sr.route_view(sr.LANE_NETWORK)["can_send"]
      and sr.route_view(sr.LANE_PLATFORM)["say"] == "")

# ── 2. the public draft holds no private context ────────────────────────────

PRIVATE = {
    "email": "dana.wren@mailbox.example", "phone": "+1 555 010 4477", "ip": "192.0.2.41",
    "ip6": "2001:db8::17", "home": "/home/dwren/notes", "url": "https://files.example.net/share/x9",
    "machine": "studio-pc.lan", "host_name": "Juniper", "secret": "sk-ant-api03-" + "Q" * 60,
}
draft = sr.public_draft(
    category="bug", title=f"Calendar sync fails for {PRIVATE['host_name']}",
    problem=(f"Sync stops at 18:40 every day; mail {PRIVATE['email']} or {PRIVATE['phone']}. "
             f"The log is at {PRIVATE['home']}/sync.log, see {PRIVATE['url']}."),
    repro_steps=f"1. Open Calendar on {PRIVATE['machine']} ({PRIVATE['ip']}, {PRIVATE['ip6']})\n2. Press Sync",
    expected="Events arrive", actual=f"Error 502 with key {PRIVATE['secret']}",
    version="v0.21.4", names=[PRIVATE["host_name"]], issue=True)
blob = json.dumps(draft)
leaked = [k for k, val in PRIVATE.items() if val in blob or (k == "home" and "dwren" in blob)]
check("public draft: no e-mail, number, address, home folder, link, machine name, Host name or secret",
      not leaked, f"leaked {leaked}: {blob[:600]}")
check("public draft: keeps the fault, the times and the version",
      "Sync stops at 18:40 every day" in draft["body"] and "Press Sync" in draft["body"]
      and "C-Series v0.21.4" in draft["body"] and "Error 502" in draft["body"], draft["body"])
check("public draft: the new-issue link carries the same title and body",
      draft["url"].startswith(sr.ISSUE_NEW_URL + "?title=") and "Press%20Sync" in draft["url"]
      and not any(val in draft["url"] for val in PRIVATE.values()), draft["url"][:300])
check("public draft: says what it took out and what it never reads",
      {"e-mail address", "phone number", "network address", "link", "folder path", "name",
       "key or token"} <= set(draft["removed"])
      and "memory" in draft["left_out"] and "conversations" in draft["left_out"], json.dumps(draft["removed"]))
long_draft = sr.public_draft(category="bug", title="t", problem="x " * 6000, issue=True)
check("public draft: an over-long body is cut to fit the link and says so",
      len(long_draft["url"]) <= sr.ISSUE_URL_MAX and long_draft.get("link_cut") is True)

# Adversarial fields (invented values): a folder path anywhere, not only a
# home folder's user name; a random-looking key with no known prefix; the
# Host's name or an address inside the version; a LAN name on a router's
# own suffix. Each must be gone from the text and from the link.
ADV = {
    "srv_path": "/srv/data/orchard-merger/board-minutes.docx",
    "home_rest": "/home/kvale/clients/Brightwater/payroll.xlsx",
    "tilde_path": "~/Documents/Kestrel-2026/notes.txt",
    "drive_path": "D:\\Clients\\Fernhill\\tax-return.pdf",
    "token": "a8F3kQ9zP2mX7vB4nR6tY1wE5uI0oL3sZx",
    "lan_name": "printer-7.lantern.box",
}
adv = sr.public_draft(
    category="bug", title="Export fails",
    problem=f"Export of {ADV['srv_path']} stops; the copy at {ADV['home_rest']} too.",
    repro_steps=f"1. Open {ADV['tilde_path']}\n2. Export to {ADV['drive_path']}",
    expected="A file", actual=f"Error 500 with key {ADV['token']} from {ADV['lan_name']}",
    version="v0.21.4-Juniper+192.0.2.41", names=["Juniper"], issue=True)
adv_blob = json.dumps(adv)
adv_leaks = [k for k, val in ADV.items() if val in adv_blob or val in adv["url"]]
adv_leaks += [w for w in ("orchard-merger", "Brightwater", "Kestrel", "Fernhill", "Juniper", "192.0.2.41")
              if w in adv_blob or w in adv["url"]]
check("public draft: no folder path (any kind), random key, Host name or address in the version, LAN name",
      not adv_leaks, f"leaked {adv_leaks}: {adv['body']}")
check("public draft: still keeps the fault around what it cut",
      "Error 500" in adv["body"] and "Export to" in adv["body"] and "C-Series v0.21.4-" in adv["body"],
      adv["body"])
check("owner's sentences: no promise the cut cannot keep (names and addresses in plain words get "
      "through it), and they ask the owner to read the text",
      all("names and addresses" not in t and "read the text" in t
          for t in (sr.ISSUE_OFFER, sr.BOARD_OFFER)), sr.ISSUE_OFFER + " | " + sr.BOARD_OFFER)

# The tickets tool: support_open on a Host that cannot send privately.
from mastercode.runtime import tools_comms as tc  # noqa: E402


class HostPool:
    async def fetchval(self, sql, *params):
        return PRIVATE["host_name"] if "display_name" in sql else None


ARGS = {"op": "support_open", "category": "bug", "title": "Calendar sync fails",
        "problem": f"Sync stops; ask {PRIVATE['host_name']} or {PRIVATE['email']}",
        "context": "PRIVATE-CONTEXT: the person was planning a surprise for their sister",
        "recommendation": "PRIVATE-RECO", "pushback": "PRIVATE-PUSH",
        "repro_steps": "1. Press Sync", "expected": "events", "actual": "error",
        "files": ["notes/diary.md"], "harness_runs": [3], "project_id": 9}

res = run(tc._support_elsewhere(dict(ARGS), NoPlatform(), sr.LANE_STANDALONE, pool=HostPool(), eid=1))
blob = json.dumps(res)
check("tool standalone: nothing drafted, and it says so first",
      res["drafted"] is False and res["lane"] == "standalone" and res["say"].startswith(sr.NOT_ON_NETWORK)
      and "NOT DRAFTED" in res["note"], blob[:400])
check("tool standalone: a bug gets a public issue the owner posts, never the Host",
      res["public_issue"]["url"].startswith(sr.ISSUE_NEW_URL) and "You never post it" in res["note"])
check("tool standalone: the public text holds no context, recommendation, pushback, files, runs, "
      "project, e-mail or Host name",
      not any(s in blob for s in ("PRIVATE-CONTEXT", "PRIVATE-RECO", "PRIVATE-PUSH", "diary",
                                  PRIVATE["email"], PRIVATE["host_name"], "sister")), blob[:600])
check("tool standalone: a security problem goes to security@privatae.ai",
      "security@privatae.ai" in res["security"])
check("tool standalone: no raw transport error anywhere", not any(r in blob for r in RAW))
res = run(tc._support_elsewhere({**ARGS, "category": "billing"}, NoPlatform(), sr.LANE_STANDALONE))
check("tool standalone: a payment question is never offered as a public issue",
      "public_issue" not in res and "can't go in a public issue" in res["say"], json.dumps(res)[:300])

m_today, rec = member()
res = run(tc._support_elsewhere(dict(ARGS), m_today, sr.LANE_BOARD, pool=HostPool(), eid=1))
blob = json.dumps(res)
check("tool member: nothing drafted; offers the board and says every member can read it",
      res["drafted"] is False and res["lane"] == "board" and "Every member can read the board" in res["say"]
      and res["board_post"]["tool"] == "community_board" and res["board_post"]["action"] == "post")
check("tool member: the post goes in the board's help category (read from the board)",
      res["board_post"]["category"] == "help", res["board_post"]["category"])
check("tool member: the post waits on the owner's press on the card",
      "it goes out only when they press Post it there" in res["note"]
      and "Never say it was posted until the card's answer says so" in res["note"], res["note"])
check("tool member: the board text holds no private context",
      not any(s in blob for s in ("PRIVATE-CONTEXT", "PRIVATE-RECO", "diary", PRIVATE["email"],
                                  PRIVATE["host_name"])), blob[:600])
check("tool member: nothing was posted or sent", [op for op, _ in rec.calls] == ["community.categories"],
      repr(rec.calls))


# ── 3. Send, per connection ─────────────────────────────────────────────────

class FakePool:
    def __init__(self, row):
        self.row = dict(row)
        self.executes = []

    async def fetchrow(self, sql, *params):
        return dict(self.row)

    async def execute(self, sql, *params):
        self.executes.append((sql, params))
        return "UPDATE 1"


ROW = {"id": 5, "entity_id": 1, "ticket_uuid": "0c1d2e3f-0000-4000-8000-000000000005",
       "category": "question", "project_name": "", "title": "How do backups rotate?", "severity": "normal",
       "problem": "Unsure", "context": "", "recommendation": "", "pushback": "", "repro_steps": "",
       "expected": "", "actual": "",
       "shared_context": json.dumps([{"key": "versions", "label": "v", "kind": "versions",
                                      "content": "cseries v0.21.4", "included": True}]),
       "status": "draft", "metadata": "{}", "thread": "[]", "platform_id": None}

_orig_client = sr.client


def holding(bc):
    sr.client = lambda: bc


for label, bc, want in (("standalone", NoPlatform(), sr.NOT_ON_NETWORK),
                        ("member today", member()[0], sr.NO_PRIVATE_SUPPORT)):
    for cat in ("question", "bug"):
        holding(bc)
        pool = FakePool({**ROW, "category": cat})
        try:
            run(sp.send_service(pool, 1, 5))
            check(f"send {label} ({cat}): refused", False, "did not raise")
        except sp.SupportError as e:
            check(f"send {label} ({cat}): refused in the owner's words, naming what to do instead",
                  e.status == 422 and want in e.detail and "Nothing was sent" in e.detail
                  and "discard" in e.detail and not any(r in e.detail for r in RAW), f"{e.status} {e.detail}")
        check(f"send {label} ({cat}): the draft is untouched", pool.executes == [])

# A member door that lists support.ticket: used, for every category.
m_future, rec = member(extra_ops=(sr.SUPPORT_OP,))
holding(m_future)
pool = FakePool(ROW)
out = run(sp.send_service(pool, 1, 5))
check("send member with support.ticket: sent through it", [op for op, _ in rec.calls] == [sr.SUPPORT_OP]
      and rec.calls[0][1]["op"] == "open" and out["platform_id"] == 41, repr(rec.calls))
check("send member with support.ticket: the row remembers the op",
      "network_ticket" in pool.executes[-1][0], pool.executes[-1][0])
check("refresh / reply / close then ask the same op",
      sp._ticket_op('{"lane": "network_ticket"}') == sr.SUPPORT_OP and sp._ticket_op("{}") == "platform.ticket")
rec.calls.clear()
pool = FakePool({**ROW, "category": "bug"})
run(sp.send_service(pool, 1, 5))
check("send member with support.ticket: a bug goes there too (the door has no proposal.file)",
      [op for op, _ in rec.calls] == [sr.SUPPORT_OP] and rec.calls[0][1]["category"] == "bug", repr(rec.calls))

# The platform bridge: unchanged when up; a plain temporary fault when down.
b, rec = platform()
holding(b)
out = run(sp.send_service(FakePool(ROW), 1, 5))
check("send platform: platform.ticket as before", [op for op, _ in rec.calls] == ["platform.ticket"]
      and out["platform_id"] == 77, repr(rec.calls))
b_down, rec = platform(connected=False)
holding(b_down)
try:
    run(sp.send_service(FakePool(ROW), 1, 5))
    check("send platform down: refused", False, "did not raise")
except sp.SupportError as e:
    check("send platform down: a plain, temporary fault; the draft is kept",
          e.status == 503 and "can't reach the platform right now" in e.detail and "draft is kept" in e.detail
          and not any(r in e.detail for r in RAW), e.detail)
check("send platform down: nothing reached the wire", rec.calls == [])
m_down, rec = member(extra_ops=(sr.SUPPORT_OP,), connected=False)
holding(m_down)
try:
    run(sp.send_service(FakePool(ROW), 1, 5))
except sp.SupportError as e:
    check("send member door down: the network, named, as a temporary fault",
          e.status == 503 and "can't reach the Privatae network right now" in e.detail, e.detail)

# _bridge itself (reply / refresh) on a client without the op.
holding(member()[0])
try:
    run(sp._bridge("platform.ticket", {"op": "reply"}))
    check("_bridge member: refused", False, "did not raise")
except sp.SupportError as e:
    check("_bridge member: an op the door does not carry is refused in plain words",
          e.status == 422 and sr.NO_PRIVATE_SUPPORT in e.detail and not any(r in e.detail for r in RAW), e.detail)
holding(NoPlatform())
try:
    run(sp._bridge("platform.ticket", {"op": "reply"}))
except sp.SupportError as e:
    check("_bridge standalone: refused in plain words",
          e.status == 422 and sr.NOT_ON_NETWORK in e.detail and not any(r in e.detail for r in RAW), e.detail)

# The owner's own New ticket route: no draft that cannot go.
from fastapi import HTTPException  # noqa: E402


class _Req:
    async def json(self):
        return {"category": "bug", "title": "t", "problem": "p"}


holding(NoPlatform())
try:
    run(sp.open_ticket(_Req()))
    check("POST /api/support standalone: refused", False, "did not raise")
except HTTPException as e:
    check("POST /api/support standalone: no draft, the owner's sentence",
          e.status_code == 422 and sr.NOT_ON_NETWORK in str(e.detail) and "Nothing was written" in str(e.detail),
          str(e.detail))
sr.client = _orig_client

# ── 4. the skill ────────────────────────────────────────────────────────────

skill = open(os.path.join(_ROOT, "default-skills", "tickets.md"), encoding="utf-8").read()
check("skill: the 'I drafted a support ticket' line is said only after a real draft",
      "So after `support_open` you tell the user" not in skill
      and "after a `support_open` that answered `status: 'draft'`" in skill)
check("skill: drafted:false means no ticket, and `say` comes first",
      "drafted: false" in skill and "Never say you drafted, filed or sent a ticket" in skill
      and "Tell the person `say` first" in skill)
check("skill: standalone — can't send to Privatae; a PUBLIC issue the person posts; the Host never posts it",
      "isn't on the Privatae network, so it can't send" in skill and "github.com/Privatae/cseries" in skill
      and "PUBLIC" in skill and "You\n  never post it." in skill)
check("skill: the public text never carries memory, conversations, file contents, names or addresses",
      "never add memory,\n  conversations, file contents, names or addresses back" in skill)
check("skill: member — the board, visible to every member, posted only on a yes",
      "every member can read the members' community board" in skill
      and "the post goes out only when they press Post it there" in skill
      and "only on their yes" not in skill)
check("skill: security goes to security@privatae.ai, never public", "security@privatae.ai" in skill
      and "never goes anywhere public" in skill)
check("skill: no channel the repository does not have (no Security tab)", "Security tab" not in skill)

print()
if _failures:
    print(f"FAIL support_reach: {_failures} check(s)")
    sys.exit(1)
print("PASS support_reach")
