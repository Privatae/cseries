#!/usr/bin/env python3
"""Gate: Circles' deck, against a real kernel schema.

  P. pairing on GET /api/circles/conversations: one Host on two people (both
     carry it, and the Host carries both); a person with no host contact (the
     back is blank); a person whose Host is not recorded yet; a blocked person
     is never on the list, and neither is the Host blocked through them; a
     Host blocked directly leaves the list while its person stays, marked;
     contacts.blocked_sets answers exactly what contacts.block_refuses answers
     for every Host and person seeded; the groups this Host owns or is in are
     on the list for a caller holding read:group, and null for one without.
  O. two operators of one Host: each sees their own conversations only; a
     person who wrote to one operator is never on the other's list; another
     operator's conversation adds no preview or unread to a shared contact;
     a knock addressed to one operator is never at the other's door.
  D. GET /api/circles/door: each of the five kinds (a Host's and a person's
     connection request, a group knock, an introduction, a machine request,
     a Host found on your machine) listed once; a blocked sender's card, a
     found Host you already hold, a dormant or unresolved peering, an
     archived card and an ordinary notice are not; each item's own Accept and
     Not now, sent as they are through the real routes, take it off the door;
     the route needs read:inbox, read:topology and read:group as well as
     read:circles.
  W. GET /api/circles/waiting: every open decision with its room — a thread
     (with its title, or gone), the original conversation, a project (with
     its name, or gone), and none — each room agreeing with
     GET /api/tickets?asked_in= and the thread list's waiting_on_you; decided
     tickets and another Host's are not listed; support drafts waiting for
     Send (not sent ones, not another Host's), with the revision Send names;
     the door's count; the route needs read:tickets.
  S. the list's speed at a few hundred contacts: 300 people on 100 Hosts,
     150 People threads, 100 Host threads, 30 people blocked or behind a
     blocked Host. Median of RUNS calls of the list with the deck and with
     pairing skipped, of pairing alone, and of the door and waiting, printed;
     CEILING_MS is a backstop only.

Fresh clone of the kernel schema on TEST_PG_DSN / PEER_TEST_DSN / TEST_DSN,
else a throwaway cseries-postgres through docker; no pod, no network beyond
loopback (the peer lane, the bridge and the machine's own steps are stubbed
where an Accept would reach them; each Accept's bookkeeping here is real).

  PYTHONPATH=brain python3 scripts/test_circles_deck_pg.py
"""
import asyncio
import contextlib
import json
import os
import pathlib
import statistics
import subprocess
import sys
import time
import types
import uuid

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parents[1])
for p in (str(ROOT / "brain"), str(ROOT), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)
import asyncpg  # noqa: E402

from kernel_schema import kernel_schema_db, throwaway_server  # noqa: E402

SERVER = os.environ.get("TEST_PG_DSN") or os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
if not SERVER and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    print("SKIP-LOUD: no test server (TEST_PG_DSN/PEER_TEST_DSN/TEST_DSN) and docker unavailable — "
          "this gate needs a real database to prove the deck's SQL.")
    sys.exit(2)

FAILED = 0
E = 1
OTHER_E = 2
ROBIN = "robin@example.com"
SAM = "sam@example.com"
RUNS = 7
CEILING_MS = 1_500
ALL_SCOPES = ("read:circles", "write:circles", "read:inbox", "write:inbox", "read:topology",
              "write:network", "resolve:decision", "read:group", "write:group", "read:tickets",
              "write:tickets", "read:address_book", "write:address_book")
DOOR_SCOPES = ("read:circles", "read:inbox", "read:topology", "read:group")

# circles.py imports server.main at module scope, and server.main mounts a
# container path at import time. Stub it: no bridge, no broadcast.
_stub_main = types.ModuleType("server.main")
_stub_main.app = None
_stub_main.get_bridge_client = lambda: None


async def _no_broadcast(*_a, **_k):
    return None
_stub_main.broadcast = _no_broadcast
sys.modules.setdefault("server.main", _stub_main)


def check(label, cond, detail=""):
    global FAILED
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  <- {detail}"))
    if not cond:
        FAILED += 1


async def section(name, coro_fn):
    """One lettered section; an exception fails that section, and the run goes
    on, so a tree without the feature shows every part it lacks."""
    print(f"-- {name}")
    try:
        await coro_fn()
    except Exception as e:  # noqa: BLE001 — reported as the section's failure
        check(f"{name}: ran to the end", False, f"{type(e).__name__}: {e}")


def token(scopes=ALL_SCOPES):
    from server.applications.tokens import AppToken
    return AppToken(app_id="circles", permissions=list(scopes), window_id="w", iat=0)


def request(email=ROBIN, scopes=None):
    from starlette.requests import Request

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    hdrs = [(b"x-authentik-email", email.encode())] if email else []
    scope = {"type": "http", "method": "GET", "path": "/", "headers": hdrs, "query_string": b""}
    if scopes is not None:
        scope["state"] = {"app_token": token(scopes)}
    return Request(scope, receive)


class _TokenApp:
    """The app token ApplicationTokenMiddleware would put on the request, with
    the scopes the test names in its own x-test-scopes header (all of
    ALL_SCOPES by default)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            hdrs = dict(scope.get("headers") or [])
            raw = hdrs.get(b"x-test-scopes")
            scopes = raw.decode().split(",") if raw is not None else ALL_SCOPES
            scope.setdefault("state", {})["app_token"] = token(scopes)
        await self.app(scope, receive, send)


def asgi_app():
    from fastapi import FastAPI
    from server.routes import circles, contacts, groups, inbox, network
    app = FastAPI()
    for mod in (contacts, groups, inbox, network, circles):
        app.include_router(mod.router)
    return _TokenApp(app)


# ── the rows ────────────────────────────────────────────────────────────────

class Rows:
    def __init__(self, pool):
        self.pool = pool
        self._n = 0

    async def contact(self, handle, kind, status="active", peer=None, name=None):
        return await self.pool.fetchval(
            "INSERT INTO contacts (handle, display_name, kind, status, peer_entity_id, last_contact_at) "
            "VALUES ($1, $2, $3, $4, $5, now()) RETURNING id", handle, name, kind, status, peer)

    async def inbox(self, frm, to, body="hi", *, kind="message", from_user=None, to_user=None,
                    payload=None, subject="Message", ago_s=0):
        return str(await self.pool.fetchval(
            "INSERT INTO inbox_messages (from_address, to_address, from_entity_id, to_entity_id, "
            "from_user_email, to_user_email, subject, body, message_kind, payload, sent_at, delivered_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, now() - make_interval(secs => $11), "
            "        now() - make_interval(secs => $11)) RETURNING message_id",
            f"{frm}@entity.localhost", f"{to}@entity.localhost", frm, to, from_user, to_user,
            subject, body, kind, json.dumps(payload or {}), float(ago_s)))

    async def machine(self, hostname, peers):
        """A bastion this Host is paired to, announcing `peers`
        [(host_name, entity_id or None, current[, state])]: the rows
        peer_discovery.list_peerings reads. `state` is the pairing state the
        box reports with the name, "active" when not given; None sends none."""
        self._n += 1
        tid = await self.pool.fetchval(
            "INSERT INTO mcp_client_tokens (entity_id, name, token_hash, url_handle, operator_kind) "
            "VALUES ($1, $2, $3, $4, 'bastion') RETURNING id",
            E, hostname, uuid.uuid4().bytes, f"h{self._n}-{uuid.uuid4().hex[:8]}")
        announced = []
        for h, _eid, current, *state in peers:
            if current:
                st = state[0] if state else "active"
                announced.append({"host_name": h, **({"state": st} if st else {})})
        await self.pool.execute(
            "INSERT INTO network_bastion_heartbeat (token_id, hostname, peers) VALUES ($1, $2, $3::jsonb)",
            tid, hostname, json.dumps(announced))
        for h, eid, _current, *_state in peers:
            await self.pool.execute(
                "INSERT INTO network_bastion_peer (token_id, peer_host_name, peer_handle, peer_entity_id, "
                "notified_at) VALUES ($1, $2, $3, $4, now())", tid, h, h.split(".")[0], eid)
        return tid

    async def new_peer_notice(self, tid, host_name):
        """The notice peer_discovery.record_and_notify writes, in its shape."""
        return await self.inbox(E, E, f"{host_name} is on a machine you own.", kind="system_notification",
                                subject=f"New peer: {host_name.split('.')[0]}",
                                payload={"app": "network", "lens": "topology", "kind": "new_peer",
                                         "token_id": tid, "machine": "studio-mac",
                                         "peer_host_name": host_name,
                                         "peer_handle": host_name.split(".")[0]})


def _deck():
    """server/circles_deck.py, or None on a tree without it: each section
    that needs it then fails on its own, saying so."""
    try:
        from server import circles_deck
        return circles_deck
    except ImportError:
        return None


def by_id(items, key):
    return next((i for i in (items or []) if str(i.get("id")) == str(key)), None)


# ── the behaviour ───────────────────────────────────────────────────────────

async def behaviour(dsn):
    import httpx

    from server import brain_service as bs
    import server.routes.circles as C
    import server.routes.contacts as K
    import server.routes.tickets as TK
    D = _deck()

    pool = await asyncpg.create_pool(dsn, min_size=2, max_size=6)
    saved = (bs._pool, bs._entity_id)
    bs._pool, bs._entity_id = pool, E
    rows = Rows(pool)
    for eid, name in ((E, "Ivy"), (OTHER_E, "other host")):
        await pool.execute("INSERT INTO entity_registry (id, display_name) VALUES ($1, $2) "
                           "ON CONFLICT (id) DO NOTHING", eid, name)

    async def listed(email=ROBIN, scopes=ALL_SCOPES):
        return await C.list_conversations(request(email, scopes))

    try:
        # ── P. pairing ──────────────────────────────────────────────────────
        async def pairing():
            JUNO, DADS, MALS, REX, NOVA = 130, 140, 150, 160, 170
            juno = await rows.contact("juno", "host", "active", JUNO, "Juno")
            ana = await rows.contact("ana@example.com", "human", "active", JUNO, "Ana Ruiz")
            ben = await rows.contact("ben@example.com", "human", "active", JUNO, "Ben")
            await rows.contact("dad@example.com", "human", "active", DADS, "Dad")
            await rows.contact("hand@example.com", "human", "active", None, "Added by hand")
            await rows.contact("mal@example.com", "human", "blocked", MALS, "Mallory")
            await rows.contact("eve@example.com", "human", "active", MALS, "Eve")
            rex = await rows.contact("rex", "host", "blocked", REX, "Rex")
            await rows.contact("pat@example.com", "human", "active", REX, "Pat")
            await rows.inbox(JUNO, E, "dinner?", from_user="ana@example.com", to_user=ROBIN, ago_s=60)
            await rows.inbox(E, JUNO, "sure", from_user=ROBIN, to_user="ana@example.com", ago_s=50)
            await rows.inbox(JUNO, E, "lunch?", from_user="ben@example.com", to_user=ROBIN, ago_s=40)
            await rows.inbox(MALS, E, "let me in", from_user="mal@example.com", to_user=ROBIN, ago_s=30)
            await rows.inbox(REX, E, "hi from pat", from_user="pat@example.com", to_user=ROBIN, ago_s=20)
            for host in (JUNO, MALS, REX):
                await rows.inbox(host, E, f"host {host} here", ago_s=10)
            # A machine of ours announces a Host blocked directly.
            await rows.contact("nova-blocked", "host", "blocked", NOVA, "Nova")
            await rows.machine("studio-mac", [("nova.example", NOVA, True)])

            pl = await listed()
            people, hosts = pl["people"], pl["entities"]
            a, b = by_id(people, "ana@example.com"), by_id(people, "ben@example.com")
            check("P one Host on two people: both carry it and the contact you hold for it",
                  a and b and a["host_entity_id"] == b["host_entity_id"] == JUNO
                  and a["host_contact"] == b["host_contact"] == {"id": juno, "status": "active"}
                  and a["host_blocked"] is False and a["contact_id"] == ana and b["contact_id"] == ben, (a, b))
            j = by_id(hosts, JUNO)
            check("P ...and that Host carries both people",
                  j and sorted(x["id"] for x in j["people"]) == ["ana@example.com", "ben@example.com"]
                  and j["host_contact"] == {"id": juno, "status": "active"}, j)
            d = by_id(people, "dad@example.com")
            check("P a person with no host contact: their Host's id, and no contact for it (the blank back)",
                  d and d["host_entity_id"] == DADS and d["host_contact"] is None and d["host_blocked"] is False, d)
            h = by_id(people, "hand@example.com")
            check("P a person whose Host is not recorded yet: no Host at all",
                  h and h["host_entity_id"] is None and h["host_contact"] is None and h["contact_id"], h)
            check("P a blocked person is never on the list, thread or not",
                  by_id(people, "mal@example.com") is None, [p["id"] for p in people])
            check("P ...and the Host blocked through them is not either",
                  by_id(hosts, MALS) is None, [x["id"] for x in hosts])
            e = by_id(people, "eve@example.com")
            check("P another person on that Host stays, marked host_blocked",
                  e and e["host_entity_id"] == MALS and e["host_blocked"] is True, e)
            check("P a Host blocked directly leaves the list", by_id(hosts, REX) is None, [x["id"] for x in hosts])
            pt = by_id(people, "pat@example.com")
            check("P ...its person stays, with the blocked contact Unblock names",
                  pt and pt["host_blocked"] is True and pt["host_contact"] == {"id": rex, "status": "blocked"}, pt)
            check("P a blocked Host a machine of ours announces is not listed either",
                  by_id(hosts, NOVA) is None, [x["id"] for x in hosts])

            # One rule: the list's sets are block_refuses', Host by Host and
            # person by person.
            hosts_b, people_b = await K.blocked_sets(pool)
            ids = (JUNO, DADS, MALS, REX, NOVA, 999)
            emails = ("ana@example.com", "ben@example.com", "dad@example.com", "hand@example.com",
                      "mal@example.com", "eve@example.com", "pat@example.com", "nobody@example.com")
            bad = [("host", i) for i in ids if (i in hosts_b) != await K.block_refuses(pool, i)]
            bad += [("person", x) for x in emails if (x in people_b) != await K.block_refuses(pool, 999, x)]
            check("P blocked_sets answers exactly what block_refuses answers, for every Host and person",
                  not bad and hosts_b == {MALS, REX, NOVA} and people_b == {"mal@example.com"},
                  (bad, hosts_b, people_b))
            await pool.execute("UPDATE contacts SET status = 'active' WHERE handle = 'mal@example.com'")
            pl2 = await listed()
            check("P unblocking the person brings them and their Host back",
                  by_id(pl2["people"], "mal@example.com") is not None and by_id(pl2["entities"], MALS) is not None,
                  ([p["id"] for p in pl2["people"]], [x["id"] for x in pl2["entities"]]))
            await pool.execute("UPDATE contacts SET status = 'blocked' WHERE handle = 'mal@example.com'")

            # Groups: from the groups service, gated on read:group.
            from server.groups import roster
            mine = await roster.create_group(pool, E, name="Book club")
            theirs = f"grp-300-{uuid.uuid4().hex}"
            left = f"grp-301-{uuid.uuid4().hex}"
            for gid, owner, name in ((theirs, 300, "Lake weekend"), (left, 301, "Old team")):
                await pool.execute("INSERT INTO groups (group_id, owner_entity_id, name) VALUES ($1, $2, $3)",
                                   gid, owner, name)
            await pool.execute("INSERT INTO group_members (group_id, member_entity_id) VALUES ($1, $2)", theirs, E)
            await pool.execute("INSERT INTO group_members (group_id, member_entity_id, removed_at) "
                               "VALUES ($1, $2, now())", left, E)
            gs = (await listed())["groups"]
            want = sorted(x["group_id"] for x in await roster.list_groups(pool, E))
            check("P the groups you own or are in are on the list, as the groups service lists them",
                  isinstance(gs, list) and sorted(g["id"] for g in gs) == want == sorted([mine["group_id"], theirs])
                  and all(g["kind"] == "group" for g in gs)
                  and by_id(gs, mine["group_id"])["owned"] is True and by_id(gs, theirs)["owned"] is False,
                  (gs, want))
            no = await listed(scopes=("read:circles",))
            check("P a caller without read:group gets groups null, never another route's data",
                  "groups" in no and no["groups"] is None, no.get("groups"))

        # ── O. two operators of one Host ────────────────────────────────────
        async def operators():
            BOBS = 180
            # Bob has no contact here, so only the relay stands behind him: the
            # row is stored as the relay stores it (circles_reading.CHECKED_KEY).
            await rows.inbox(BOBS, E, "for Sam only", from_user="bob@example.com", to_user=SAM,
                             payload={"person_checked_by": "relay"})
            await rows.inbox(130, E, "Sam, the files", from_user="ana@example.com", to_user=SAM)
            robin, sam = await listed(ROBIN), await listed(SAM)
            check("O a person who wrote only to Sam is on Sam's list and never Robin's",
                  by_id(sam["people"], "bob@example.com") is not None
                  and by_id(robin["people"], "bob@example.com") is None,
                  ([p["id"] for p in robin["people"]], [p["id"] for p in sam["people"]]))
            ca, sa = by_id(robin["people"], "ana@example.com"), by_id(sam["people"], "ana@example.com")
            check("O each operator's conversation with Ana is their own: preview and unread",
                  ca and sa and ca["preview"] == "sure" and sa["preview"] == "Sam, the files"
                  and ca["unread"] == 1 and sa["unread"] == 1, (ca, sa))
            cb, sb = by_id(robin["people"], "ben@example.com"), by_id(sam["people"], "ben@example.com")
            check("O Robin's conversation with Ben adds nothing to Sam's list: Ben is there only as the "
                  "Host's shared contact, no preview and no unread",
                  cb and cb["preview"] == "lunch?" and sb and sb["preview"] == "" and sb["unread"] == 0, (cb, sb))
            cj, sj = by_id(robin["entities"], 130), by_id(sam["entities"], 130)
            check("O a Host carries only people on that operator's own list",
                  cj and sj and not any(x["id"] == "bob@example.com" for x in cj["people"]), (cj, sj))
            k = await rows.inbox(190, E, "Would like to connect.", kind="endpoint_request",
                                 from_user="yan@example.com", to_user=SAM, subject="Connection request")
            cd = await D.door(pool, E, ROBIN)
            sd = await D.door(pool, E, SAM)
            check("O a knock addressed to Sam is at Sam's door and never Robin's",
                  any(i["id"] == k for i in sd) and not any(i["id"] == k for i in cd),
                  ([i["id"] for i in cd], [i["id"] for i in sd]))
            await pool.execute("UPDATE inbox_messages SET archived_at = now() WHERE message_id = $1::uuid", k)

        # ── D. the door ─────────────────────────────────────────────────────
        async def the_door():
            import server.peer.peer_client as PC
            from server.groups import rendezvous as RV
            from server.network import machine_offers as MO, pairing_grants as PG

            async def lane_down(*_a, **_k):
                raise PC.PeerUnavailable("no peer lane in this test")

            async def bridge(op, params):
                return {"message_id": str(uuid.uuid4())}

            async def rv_bridge(op, params):
                return {"ok": True}

            async def anchored(_pool, _eid, server):
                return {"hostname": server}

            async def stepup(_pool, code):
                return code == "123456"

            async def token_from_peer(_peer, **_k):
                return "one-time"

            async def redeem(_pool, _eid, _server, _token):
                return {"subject": "studio-mac/guest", "handle": "guest"}

            async def grant(*_a, **_k):
                return {"delivered": True}

            async def told(*_a, **_k):
                return None
            stubs = [(PC, "knock_peer", lane_down), (PC, "grant_peer", lane_down),
                     (PC, "send_to_peer", lane_down), (K, "_bridge_op", bridge), (RV, "_bridge", rv_bridge),
                     (PG, "_anchored_here", anchored), (PG, "_refuse_scope_the_box_cannot_enforce", lambda *a: None),
                     (PG, "_verify_stepup", stepup), (MO, "request_token_from_peer", token_from_peer),
                     (PG, "redeem_on_box", redeem), (PG, "sign_and_push_grant", grant), (MO, "_tell_them", told)]
            real = [(m, n, getattr(m, n)) for m, n, _ in stubs]
            for m, n, f in stubs:
                setattr(m, n, f)
            try:
                await door_cases(pool, rows, D, httpx)
            finally:
                for m, n, f in real:
                    setattr(m, n, f)

        # ── W. waiting on you ───────────────────────────────────────────────
        async def the_waiting():
            from mastercode import circles_threads as ct
            tid = str(await pool.fetchval(
                "INSERT INTO circles_threads (entity_id, title) VALUES ($1, 'Thursday dinner') RETURNING id", E))
            gone = str(await pool.fetchval(
                "INSERT INTO circles_threads (entity_id, title, deleted_at) VALUES ($1, 'Old', now()) "
                "RETURNING id", E))
            proj = await pool.fetchval("INSERT INTO projects (entity_id, name) VALUES ($1, 'Garden site') "
                                       "RETURNING id", E)

            async def ticket(asked_in, status="open", eid=E, sitrep="Pick one"):
                return await pool.fetchval(
                    "INSERT INTO entity_tickets (entity_id, source, sitrep, status, options, metadata) "
                    "VALUES ($1, 'manual', $2, $3, $4::jsonb, $5::jsonb) RETURNING id",
                    eid, sitrep, status,
                    json.dumps([{"id": "a", "label": "Headlamp"}, {"id": "b", "label": "Kit"}]),
                    json.dumps({"asked_in": asked_in} if asked_in is not None else {}))
            t_thread = await ticket({"app_id": "circles", "thread_id": tid.upper(), "project_id": None},
                                    sitrep="Gift for Dad: pick one by Friday. " + "x" * 400)
            t_gone = await ticket({"app_id": "circles", "thread_id": gone, "project_id": None})
            t_orig = await ticket({"app_id": "circles", "thread_id": None, "project_id": None})
            t_proj = await ticket({"app_id": "workbench", "thread_id": None, "project_id": proj})
            t_pgone = await ticket({"app_id": "workbench", "thread_id": None, "project_id": 99999})
            t_none = await ticket(None)
            t_app = await ticket({"app_id": "notes", "thread_id": None, "project_id": None})
            await ticket({"app_id": "circles", "thread_id": tid}, status="decided")
            await ticket({"app_id": "circles", "thread_id": tid}, eid=OTHER_E)

            async def draft(status, eid=E, revision=None):
                return await pool.fetchval(
                    "INSERT INTO support_tickets (entity_id, category, title, status, metadata) "
                    "VALUES ($1, 'bug', 'Calendar shows the wrong day', $2, $3::jsonb) RETURNING id",
                    eid, status, json.dumps({"revision": revision} if revision is not None else {}))
            s_draft = await draft("draft", revision=3)
            await draft("sent")
            await draft("draft", eid=OTHER_E)

            w = await D.waiting(pool, E, ROBIN)
            got = {d["id"]: d for d in w["decisions"]}
            want_ids = {t_thread, t_gone, t_orig, t_proj, t_pgone, t_none, t_app}
            check("W every open decision of this Host, and only those (decided and another Host's left out)",
                  set(got) == want_ids, (sorted(got), sorted(want_ids)))
            rooms = {i: got[i]["room"] for i in got}
            check("W a thread: its id (lowercased) and title",
                  rooms.get(t_thread) == {"kind": "thread", "id": tid, "title": "Thursday dinner", "closed": False},
                  rooms.get(t_thread))
            check("W a deleted thread: its id, gone",
                  (rooms.get(t_gone) or {}).get("gone") is True and rooms[t_gone]["kind"] == "thread",
                  rooms.get(t_gone))
            # There is no original conversation: a decision
            # asked there has no room, like one asked nowhere.
            check("W asked in the old original conversation: no room",
                  rooms.get(t_orig) == {"kind": "none", "id": None}, rooms.get(t_orig))
            check("W a project: its id and name",
                  rooms.get(t_proj) == {"kind": "project", "id": str(proj), "name": "Garden site"},
                  rooms.get(t_proj))
            check("W a project that is gone says so", (rooms.get(t_pgone) or {}).get("gone") is True,
                  rooms.get(t_pgone))
            check("W asked nowhere (no room, or an app's ticket): none",
                  rooms.get(t_none) == rooms.get(t_app) == {"kind": "none", "id": None},
                  (rooms.get(t_none), rooms.get(t_app)))
            check("W all three rooms are covered, and no 'original' room is ever named",
                  {r["kind"] for r in rooms.values()} == {"thread", "project", "none"}, rooms)
            mismatch = []
            for i, r in rooms.items():
                if r["kind"] == "none":
                    continue
                room = {"thread": f"thread:{r['id']}", "project": f"project:{r['id']}"}[r["kind"]]
                under = {x["id"] for x in (await TK.list_tickets_service(
                    pool, E, status_filter="open", asked_in=room, limit=200))["tickets"]}
                if i not in under:
                    mismatch.append((i, room))
            check("W each room is the one GET /api/tickets?asked_in= files the decision under", not mismatch,
                  mismatch)
            threads = {x["id"]: x for x in await ct.list_threads(pool, E)}
            check("W ...and the thread list counts it on that page (waiting_on_you), with no original page",
                  threads[tid]["waiting_on_you"] == 1 and "original" not in threads,
                  (threads[tid]["waiting_on_you"], sorted(threads)))
            tt = got.get(t_thread) or {}
            check("W a decision is the Host's own list row: the sitrep's first 160 characters, the options",
                  len(tt.get("sitrep") or "") <= 160 and (tt.get("sitrep_chars") or 0) > 400
                  and [o["label"] for o in tt.get("options") or []] == ["Headlamp", "Kit"], tt)
            check("W support drafts waiting for Send: this Host's drafts only, with the revision Send names",
                  [s["id"] for s in w["support_drafts"]] == [s_draft]
                  and w["support_drafts"][0]["revision"] == 3
                  and w["support_drafts"][0]["link"] == f"support:{s_draft}",
                  w["support_drafts"])
            at_door = len(await D.door(pool, E, ROBIN))
            check("W the door's count is the door's, for the same operator",
                  w["door"] == at_door and w["count"] == len(want_ids) + 1 + at_door,
                  (w["door"], at_door, w["count"]))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=asgi_app()),
                                         base_url="http://t") as cl:
                ok = await cl.get("/api/circles/waiting", headers={"x-authentik-email": ROBIN})
                no = await cl.get("/api/circles/waiting", headers={
                    "x-authentik-email": ROBIN, "x-test-scopes": ",".join(DOOR_SCOPES)})
            check("W the route answers through the app, and needs read:tickets",
                  ok.status_code == 200 and ok.json()["count"] == w["count"] and no.status_code == 403,
                  (ok.status_code, ok.text[:200], no.status_code))

        await section("P. pairing on the conversations list", pairing)
        await section("O. two operators of one Host", operators)
        await section("D. the door", the_door)
        await section("W. waiting on you", the_waiting)
    finally:
        bs._pool, bs._entity_id = saved
        await pool.close()


async def door_cases(pool, rows, D, httpx):
    from server.groups import roster

    # One card of each kind, twice: one answered by its Accept, one by its
    # Not now. Then the cards that must never be listed.
    g = await roster.create_group(pool, E, name="Climbing")
    tid = await rows.machine("studio-mac", [
        ("kiwi.example", 204, True), ("lime.example", 205, True),
        ("juno.example", 130, True), ("gone.example", 206, False), ("unresolved.example", None, True),
        ("waiting.example", 211, True, "pending"), ("nostate.example", 212, True, None)])

    async def offer(peer):
        return await pool.fetchval(
            "INSERT INTO network_machine_offers (direction, peer_entity_id, purpose, models_asked) "
            "VALUES ('in', $1, 'run evals', '[\"llama-4\"]'::jsonb) RETURNING id", peer)
    seeded = {
        "endpoint_request": [
            await rows.inbox(200, E, "Would like to connect.", kind="endpoint_request",
                             subject="Connection request",
                             payload={"from_handle": "nova", "from_display_name": "Nova"}),
            await rows.inbox(201, E, "Hi Robin", kind="endpoint_request", subject="Connection request",
                             from_user="zoe@example.com", to_user=ROBIN,
                             payload={"from_user_name": "Zoe", "from_display_name": "Wren"}),
        ],
        "group_join": [
            await rows.inbox(202, E, "let me in", kind="group_join", subject="Join request",
                             payload={"group_id": g["group_id"], "from_display_name": "Moss"}),
            await rows.inbox(207, E, "me too", kind="group_join", subject="Join request",
                             payload={"group_id": g["group_id"], "from_display_name": "Fern"}),
        ],
        "rendezvous_intro": [
            await rows.inbox(E, E, "An introduction", kind="rendezvous_intro", subject="An introduction",
                             payload={"group_id": 77, "group_slug": "bouldering", "group_title": "Bouldering",
                                      "tag": "bouldering", "others": 2}),
            await rows.inbox(E, E, "Another", kind="rendezvous_intro", subject="An introduction",
                             payload={"group_id": 78, "group_slug": "chess", "group_title": "Chess",
                                      "tag": "chess", "others": 1}),
        ],
        "machine_request": [
            await rows.inbox(203, E, "May we use a model?", kind="machine_request", subject="Machine request",
                             payload={"from_display_name": "Sage", "models_asked": ["llama-4"]}),
            await rows.inbox(208, E, "And us?", kind="machine_request", subject="Machine request",
                             payload={"from_display_name": "Reed"}),
        ],
        "found_host": [
            await rows.new_peer_notice(tid, "kiwi.example"),
            await rows.new_peer_notice(tid, "lime.example"),
        ],
    }
    offers = {203: await offer(203), 208: await offer(208)}
    never = {
        "a blocked Host's request": await rows.inbox(160, E, "x", kind="endpoint_request",
                                                     subject="Connection request"),
        "a blocked person's request": await rows.inbox(150, E, "x", kind="endpoint_request",
                                                       subject="Connection request", from_user="mal@example.com",
                                                       to_user=ROBIN),
        "a found Host you already hold": await rows.new_peer_notice(tid, "juno.example"),
        "a dormant peering": await rows.new_peer_notice(tid, "gone.example"),
        "an unresolved peering": await rows.new_peer_notice(tid, "unresolved.example"),
        # Named on the latest beat, but not as an active pairing: Connect would
        # be refused (contacts.request_peer_service), so there is no card.
        "a peering the machine reports pending": await rows.new_peer_notice(tid, "waiting.example"),
        "a peering named with no state": await rows.new_peer_notice(tid, "nostate.example"),
        "an archived card": await rows.inbox(209, E, "x", kind="endpoint_request", subject="Connection request"),
        "an ordinary notice": await rows.inbox(E, E, "Sites v1.2 is available", kind="system_notification",
                                               subject="Sites v1.2 is available"),
    }
    await pool.execute("UPDATE inbox_messages SET archived_at = now() WHERE message_id = $1::uuid",
                       never["an archived card"])
    all_seeded = [m for ms in seeded.values() for m in ms]

    items = await D.door(pool, E, ROBIN)
    ids = [i["id"] for i in items]
    for kind, mids in seeded.items():
        check(f"D {kind}: each card listed once, as that kind",
              all(ids.count(m) == 1 and by_id(items, m)["kind"] == kind for m in mids),
              [(m, ids.count(m), (by_id(items, m) or {}).get("kind")) for m in mids])
    for label, mid in never.items():
        check(f"D {label} is not at the door", mid not in ids, by_id(items, mid))
    check("D every card's Not now is a list of calls, one shape for every kind",
          all(isinstance(i["not_now"], list) and i["not_now"] for i in items),
          [(i["kind"], type(i["not_now"]).__name__) for i in items])
    check("D nothing else is at the door", sorted(ids) == sorted(all_seeded),
          [(i["kind"], i["id"]) for i in items if i["id"] not in all_seeded])

    zoe = by_id(items, seeded["endpoint_request"][1]) or {}
    check("D a person's request: the person, via their Host, and the accept body the card sends",
          zoe.get("from") == {"entity_id": 201, "name": "Zoe", "email": "zoe@example.com", "person": True,
                              "host_name": "Wren"}
          and zoe["accept"]["body"]["kind"] == "human"
          and zoe["accept"]["body"]["from_user_email"] == "zoe@example.com", zoe)
    mr = by_id(items, seeded["machine_request"][0]) or {}
    check("D a machine request: the open offer, what Accept still needs, and decline as Not now",
          mr.get("offer_id") == offers[203] and mr["accept"]["needs"] == ["server", "models", "stepup_code"]
          and [c["path"] for c in (mr["not_now"] if isinstance(mr["not_now"], list) else [mr["not_now"]])]
          == ["/api/network/bastions/machine-offers/decline"]
          and mr["models_asked"] == ["llama-4"], mr)
    fh = by_id(items, seeded["found_host"][0]) or {}
    check("D a found Host: the machine and the Host, Connect as Accept",
          (fh.get("from") or {}).get("entity_id") == 204 and fh.get("machine") == "studio-mac"
          and fh.get("accept") == {"method": "POST", "path": "/api/contacts/request-peer",
                                   "body": {"peer_entity_id": 204}}, fh)
    gj = by_id(items, seeded["group_join"][0]) or {}
    check("D a group knock names the group from this Host's own groups",
          gj.get("group") == {"id": g["group_id"], "name": "Climbing"}, gj.get("group"))

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=asgi_app()), base_url="http://t") as cl:
        hdr = {"x-authentik-email": ROBIN}
        r = await cl.get("/api/circles/door", headers=hdr)
        check("D the route answers through the app with the same items",
              r.status_code == 200 and [i["id"] for i in r.json()["items"]] == ids, (r.status_code, r.text[:200]))
        for missing in DOOR_SCOPES[1:]:
            scopes = [s for s in DOOR_SCOPES if s != missing]
            r = await cl.get("/api/circles/door", headers={**hdr, "x-test-scopes": ",".join(scopes)})
            check(f"D the route needs {missing}", r.status_code == 403, r.status_code)

        async def send(call, extra=None):
            body = dict(call.get("body") or {})
            body.update(extra or {})
            return await cl.request(call["method"], call["path"], headers=hdr,
                                    json=body if (call.get("body") is not None or extra) else None)

        # What a person fills in on the machine card before Accept (the card's
        # own fields; inbox-pane.tsx MachineRequestWidget).
        fills = {"machine_request": {"server": "studio-mac", "models": ["llama-4"], "stepup_code": "123456"}}
        for kind, (acc, later) in seeded.items():
            item = by_id(await D.door(pool, E, ROBIN), acc)
            r = await send(item["accept"], fills.get(kind))
            left = [i["id"] for i in await D.door(pool, E, ROBIN)]
            check(f"D {kind}: its Accept, sent as the door names it, answers and takes it off the door",
                  r.status_code == 200 and acc not in left and later in left, (r.status_code, r.text[:300]))
            item = by_id(await D.door(pool, E, ROBIN), later)
            # A found Host's Not now is a list of calls (one archive per
            # notice of that Host); every other kind's is one call.
            nn = item["not_now"]
            codes = [(await send(c)).status_code for c in (nn if isinstance(nn, list) else [nn])]
            left = [i["id"] for i in await D.door(pool, E, ROBIN)]
            check(f"D {kind}: its Not now, sent as the door names it, takes it off the door",
                  codes and all(c == 200 for c in codes) and later not in left, codes)
    st = await pool.fetchval("SELECT state FROM network_machine_offers WHERE id = $1", offers[208])
    check("D the machine request's Not now is a decline (the machine is never asked)", st == "declined", st)
    pend = await pool.fetchval("SELECT status FROM contacts WHERE kind = 'host' AND peer_entity_id = 204")
    check("D a found Host's Connect leaves the host contact that keeps it off the door",
          pend in ("pending", "active"), pend)
    left = await D.door(pool, E, ROBIN)
    check("D after every answer the door is empty", left == [], [(i["kind"], i["id"]) for i in left])
    await door_once_per_host(pool, rows, D, httpx)


class _Counting:
    """The pool, counting every statement the door sends through it."""

    def __init__(self, pool):
        self._pool, self.n = pool, 0

    def __getattr__(self, name):
        attr = getattr(self._pool, name)
        if name not in ("fetch", "fetchrow", "fetchval", "execute"):
            return attr

        async def counted(*a, **k):
            self.n += 1
            return await attr(*a, **k)
        return counted


async def door_once_per_host(pool, rows, D, httpx):
    """One card per Host, whatever number of notices or
    machines; the stronger card wins; an addressee's letter case; the door's
    reads do not grow with the number of knocks."""
    from server.groups import roster

    def cards_for(items, pid):
        return [i for i in items if (i.get("from") or {}).get("entity_id") == pid]

    async def send(cl, call):
        return await cl.request(call["method"], call["path"], headers={"x-authentik-email": ROBIN},
                                json=call.get("body"))

    # A Host found on two of your machines: one card, from the newer notice,
    # and its Not now clears both notices.
    PINE = 220
    t1 = await rows.machine("mac-one", [("pine.example", PINE, True)])
    t2 = await rows.machine("mac-two", [("pine.example", PINE, True)])
    older = await rows.new_peer_notice(t1, "pine.example")
    newer = await rows.new_peer_notice(t2, "pine.example")
    await pool.execute("UPDATE inbox_messages SET sent_at = now() - interval '1 hour', "
                       "delivered_at = now() - interval '1 hour' WHERE message_id = $1::uuid", older)
    items = await D.door(pool, E, ROBIN)
    pine = cards_for(items, PINE)
    check("D a Host found on two machines is one card, from the newer notice",
          [(c["kind"], c["id"]) for c in pine] == [("found_host", newer)], [(c["kind"], c["id"]) for c in pine])
    nn = pine[0]["not_now"] if pine else []
    check("D ...and its Not now archives every notice of that Host",
          isinstance(nn, list) and sorted(c["path"] for c in nn)
          == sorted(f"/api/inbox/archive/{m}" for m in (older, newer)), nn)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=asgi_app()), base_url="http://t") as cl:
        codes = [(await send(cl, c)).status_code for c in (nn if isinstance(nn, list) else [nn])]
        left = cards_for(await D.door(pool, E, ROBIN), PINE)
        check("D ...after Not now no card for that Host is left",
              codes and all(c == 200 for c in codes) and left == [], (codes, left))

        # A Host that asked to connect and is on your machine: the request only.
        OAK = 221
        t3 = await rows.machine("mac-three", [("oak.example", OAK, True)])
        await rows.new_peer_notice(t3, "oak.example")
        req = await rows.inbox(OAK, E, "Would like to connect.", kind="endpoint_request",
                               subject="Connection request",
                               payload={"from_handle": "oak", "from_display_name": "Oak"})
        oak = cards_for(await D.door(pool, E, ROBIN), OAK)
        check("D a Host with a connection request on the door has no found-Host card beside it",
              [(c["kind"], c["id"]) for c in oak] == [("endpoint_request", req)],
              [(c["kind"], c["id"]) for c in oak])
        r = await send(cl, oak[0]["accept"]) if oak else None
        left = cards_for(await D.door(pool, E, ROBIN), OAK)
        check("D ...and accepting the request leaves no card for that Host",
              r is not None and r.status_code == 200 and left == [],
              (r.status_code if r is not None else None, [(c["kind"], c["id"]) for c in left]))

        # Found AND asking: the request hides the found card, and the
        # request's Not now clears that Host's found notices too.
        ELM = 223
        t4 = await rows.machine("mac-four", [("elm.example", ELM, True)])
        await rows.new_peer_notice(t4, "elm.example")
        ask = await rows.inbox(ELM, E, "Would like to connect.", kind="endpoint_request",
                               subject="Connection request",
                               payload={"from_handle": "elm", "from_display_name": "Elm"})
        elm = cards_for(await D.door(pool, E, ROBIN), ELM)
        check("D a Host both found and asking is one card, the request",
              [(c["kind"], c["id"]) for c in elm] == [("endpoint_request", ask)],
              [(c["kind"], c["id"]) for c in elm])
        nn = elm[0]["not_now"] if elm else []
        codes = [(await send(cl, c)).status_code for c in (nn if isinstance(nn, list) else [nn])]
        left = cards_for(await D.door(pool, E, ROBIN), ELM)
        check("D ...and after its Not now no card for that Host is left",
              codes and all(c == 200 for c in codes) and left == [],
              (codes, [(c["kind"], c["id"]) for c in left]))

        # Two machine requests from one Host (a second card can land once the
        # first is past the lane's one-week slot, and the offer insert then
        # refuses without taking the card back): one card per Host, from the
        # newer row, and its Not now leaves none.
        async def two_asks(peer):
            old = await rows.inbox(peer, E, "a model?", kind="machine_request", subject="Machine request",
                                   payload={"from_display_name": f"M{peer}"}, ago_s=8 * 86400)
            new = await rows.inbox(peer, E, "a model, again?", kind="machine_request",
                                   subject="Machine request", payload={"from_display_name": f"M{peer}"})
            return old, new
        with_offer, without = 260, 261
        o_old, o_new = await two_asks(with_offer)
        await pool.execute("INSERT INTO network_machine_offers (direction, peer_entity_id, purpose) "
                           "VALUES ('in', $1, 'evals')", with_offer)
        n_old, n_new = await two_asks(without)
        items = await D.door(pool, E, ROBIN)
        for peer, newest in ((with_offer, o_new), (without, n_new)):
            mine = cards_for(items, peer)
            check(f"D two machine requests from one Host ({'with' if peer == with_offer else 'without'} "
                  "an open offer) are one card, the newer",
                  [(c["kind"], c["id"]) for c in mine] == [("machine_request", newest)],
                  [(c["kind"], c["id"]) for c in mine])
            nn = mine[0]["not_now"] if mine else []
            codes = [(await send(cl, c)).status_code for c in (nn if isinstance(nn, list) else [nn])]
            left = cards_for(await D.door(pool, E, ROBIN), peer)
            check(f"D ...and after its Not now no machine card for that Host is left ({peer})",
                  codes and all(c == 200 for c in codes) and left == [],
                  (codes, [(c["kind"], c["id"]) for c in left]))

    # A knock addressed in another letter case is still its addressee's.
    mixed = await rows.inbox(222, E, "Hello", kind="endpoint_request", subject="Connection request",
                             from_user="una@example.com", to_user="Robin@Example.COM")
    check("D a knock addressed in mixed case is at its addressee's door, and not another operator's",
          any(i["id"] == mixed for i in await D.door(pool, E, ROBIN))
          and not any(i["id"] == mixed for i in await D.door(pool, E, SAM)), mixed)

    # The door's reads stay the same however many knocks need a group or an offer.
    g = await roster.create_group(pool, E, name="Bouldering crew")

    async def knocks(peers):
        for p in peers:
            await rows.inbox(p, E, "let me in", kind="group_join", subject="Join request",
                             payload={"group_id": g["group_id"], "from_display_name": f"G{p}"})
            await rows.inbox(p + 10, E, "a model?", kind="machine_request", subject="Machine request",
                             payload={"from_display_name": f"M{p}"})
            await pool.execute("INSERT INTO network_machine_offers (direction, peer_entity_id, purpose) "
                               "VALUES ('in', $1, 'evals')", p + 10)

    async def reads():
        c = _Counting(pool)
        items = await D.door(c, E, ROBIN)
        return c.n, items
    await knocks([240])
    one, _ = await reads()
    await knocks([241, 242, 243])
    four, items = await reads()
    named = {i["from"]["entity_id"]: i for i in items if i["kind"] == "machine_request"}
    offers = {r["peer_entity_id"]: r["id"] for r in await pool.fetch(
        "SELECT id, peer_entity_id FROM network_machine_offers "
        " WHERE direction = 'in' AND state = 'open' AND peer_entity_id BETWEEN 250 AND 253")}
    check("D the door's reads do not grow with the number of group and machine knocks",
          one == four, (one, four))
    check("D ...and each machine card still names its own Host's offer",
          len(named) == 4 and all(named[p].get("offer_id") == o for p, o in offers.items()),
          ({p: i.get("offer_id") for p, i in named.items()}, offers))
    gj = [i for i in items if i["kind"] == "group_join"]
    check("D ...and each group knock still names the group and its answers",
          len(gj) == 4 and all(i["group"]["name"] == "Bouldering crew" and i["accept"] is not None for i in gj),
          [i.get("group") for i in gj])
    await pool.execute("UPDATE inbox_messages SET archived_at = now() WHERE archived_at IS NULL "
                       "  AND message_kind <> 'message'")


# ── S. speed ────────────────────────────────────────────────────────────────

async def speed(dsn):
    from server import brain_service as bs
    import server.routes.circles as C
    D = _deck()

    pool = await asyncpg.create_pool(dsn, min_size=2, max_size=6)
    saved = (bs._pool, bs._entity_id)
    bs._pool, bs._entity_id = pool, E
    HOSTS, PER_HOST, THREADED, MSGS, BLOCKED = 100, 3, 150, 20, 20
    try:
        await pool.execute("INSERT INTO entity_registry (id, display_name) VALUES ($1, 'Ivy') "
                           "ON CONFLICT (id) DO NOTHING", E)
        # 100 Hosts (every tenth blocked directly), 300 people on them, three
        # to a Host, the first BLOCKED blocked.
        await pool.execute(
            "INSERT INTO contacts (handle, display_name, kind, status, peer_entity_id) "
            "SELECT 'host' || h, 'Host ' || h, 'host', CASE WHEN h % 10 = 5 THEN 'blocked' ELSE 'active' END, h "
            "  FROM generate_series(1000, 1000 + $1 - 1) h", HOSTS)
        await pool.execute(
            "INSERT INTO contacts (handle, display_name, kind, status, peer_entity_id) "
            "SELECT 'p' || i || '@example.com', 'Person ' || i, 'human', "
            "       CASE WHEN i < $3 THEN 'blocked' ELSE 'active' END, 1000 + (i / $2) "
            "  FROM generate_series(0, $1 - 1) i", HOSTS * PER_HOST, PER_HOST, BLOCKED)
        await pool.execute(
            "INSERT INTO inbox_messages (from_address, to_address, from_entity_id, to_entity_id, from_user_email, "
            "  to_user_email, subject, body, message_kind, sent_at, delivered_at) "
            "SELECT 'x', 'y', CASE WHEN m % 2 = 0 THEN 1000 + (i / $3) ELSE $4 END, "
            "       CASE WHEN m % 2 = 0 THEN $4 ELSE 1000 + (i / $3) END, "
            "       CASE WHEN m % 2 = 0 THEN 'p' || i || '@example.com' ELSE $5 END, "
            "       CASE WHEN m % 2 = 0 THEN $5 ELSE 'p' || i || '@example.com' END, "
            "       'Message', 'message ' || m, 'message', now() - make_interval(mins => m), "
            "       now() - make_interval(mins => m) "
            "  FROM generate_series(0, $1 - 1) i, generate_series(0, $2 - 1) m",
            THREADED, MSGS, PER_HOST, E, ROBIN)
        await pool.execute(
            "INSERT INTO inbox_messages (from_address, to_address, from_entity_id, to_entity_id, subject, body, "
            "  message_kind, sent_at, delivered_at) "
            "SELECT 'x', 'y', 1000 + h, $2, 'Message', 'host ' || m, 'message', now() - make_interval(mins => m), "
            "       now() - make_interval(mins => m) "
            "  FROM generate_series(0, $1 - 1) h, generate_series(0, 9) m", HOSTS, E)
        await pool.execute("ANALYZE")
        counts = await pool.fetchrow(
            "SELECT (SELECT count(*) FROM contacts WHERE kind='human') AS people, "
            "       (SELECT count(*) FROM contacts WHERE kind='host') AS hosts, "
            "       (SELECT count(*) FROM contacts WHERE status='blocked') AS blocked, "
            "       (SELECT count(*) FROM inbox_messages) AS messages")

        async def median_ms(fn):
            await fn()
            times = []
            for _ in range(RUNS):
                t = time.perf_counter()
                await fn()
                times.append((time.perf_counter() - t) * 1000)
            return statistics.median(times), max(times)

        def req():
            return request(ROBIN, ALL_SCOPES)
        with_deck = await median_ms(lambda: C.list_conversations(req()))
        pl = await C.list_conversations(req())
        real_pair = D.pair

        async def no_pair(_pool, people, entities):
            return people, entities
        D.pair = no_pair
        try:
            without = await median_ms(lambda: C.list_conversations(req()))
            unpaired = await C.list_conversations(req())
        finally:
            D.pair = real_pair
        people_ = await C._list_people_threads(E, ROBIN)
        people_ += await C._list_people_contacts_without_thread(E, {p["id"] for p in people_})
        hosts_ = await C._list_entity_threads(E)
        pair_only = await median_ms(lambda: D.pair(pool, [dict(p) for p in people_], [dict(h) for h in hosts_]))
        door_t = await median_ms(lambda: D.door(pool, E, ROBIN))
        wait_t = await median_ms(lambda: D.waiting(pool, E, ROBIN))
        print(f"   rows: {counts['people']} people contacts on {counts['hosts']} Hosts, "
              f"{counts['blocked']} contacts blocked, {counts['messages']} messages")
        print(f"   listed: {len(pl['people'])} people, {len(pl['entities'])} Hosts "
              f"(pairing skipped: {len(unpaired['people'])} people, {len(unpaired['entities'])} Hosts)")
        for label, (med, mx) in (("GET /api/circles/conversations, with the deck  ", with_deck),
                                 ("GET /api/circles/conversations, pairing skipped", without),
                                 ("circles_deck.pair alone                       ", pair_only),
                                 ("GET /api/circles/door                         ", door_t),
                                 ("GET /api/circles/waiting                      ", wait_t)):
            print(f"   {label}  median {med:7.1f} ms  max {mx:7.1f} ms")
        blocked_people = {f"p{i}@example.com" for i in range(BLOCKED)}
        blocked_hosts = {1000 + h for h in range(HOSTS) if (1000 + h) % 10 == 5} \
            | {1000 + (i // PER_HOST) for i in range(BLOCKED)}
        check("S at size the deck leaves out every blocked person and every blocked Host",
              len(pl["people"]) == HOSTS * PER_HOST - BLOCKED
              and not any(p["id"] in blocked_people for p in pl["people"])
              and not any(int(h["id"]) in blocked_hosts for h in pl["entities"])
              and len(pl["entities"]) == HOSTS - len(blocked_hosts),
              (len(pl["people"]), len(pl["entities"]), HOSTS - len(blocked_hosts)))
        check(f"S the list with the deck answers under {CEILING_MS} ms (median), a backstop only",
              with_deck[0] < CEILING_MS, with_deck)
        check(f"S the door and waiting answer under {CEILING_MS} ms (median)",
              door_t[0] < CEILING_MS and wait_t[0] < CEILING_MS, (door_t, wait_t))
    finally:
        bs._pool, bs._entity_id = saved
        await pool.close()


async def main(dsn, dsn_speed):
    await behaviour(dsn)
    await section("S. the list's speed at a few hundred contacts", lambda: speed(dsn_speed))
    return 1 if FAILED else 0


with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as _srv, \
        kernel_schema_db(_srv) as _db, kernel_schema_db(_srv) as _speed:
    rc = asyncio.run(main(_db, _speed))
print("RESULT: ALL PASS" if FAILED == 0 else f"RESULT: {FAILED} FAILED")
sys.exit(rc)
