#!/usr/bin/env python3
"""A dead host↔host message must name the lane that refused it.

Circles sends a host↔host DM over the DIRECT peer lane (:8444) and falls back to
the bridge relay. Both lanes can fail for unrelated causes. Before this gate the
only thing an outside observer got when BOTH failed was the relay's 502
(`bridge_delivery_unavailable`) — the direct lane's refusal was an INFO line, so
"my host can't talk to that host" was un-diagnosable without a pod shell.

That matters more once a Circles contract becomes the Cilium allow on :8444. Once it is, the two
failures mean different things and the fix is different for each:

    transport_unavailable  → no policy allow (no contract, or it expired)
    refused:<err>          → reachable, but the peer rejected the slip

So `_try_direct_peer_send` returns `(data, reason)`, and `reason` travels into the
failure envelope. This gate pins both halves — the function's reasons, and the
caller actually carrying one into the envelope. The second half is the one a unit
test on the function alone would miss: a caller that dropped `direct_lane` on the
floor would leave every reason correct and invisible.

No database, no container: it stubs `server.main` the same way
scripts/test_circles_peer_route.py:105 does, because circles.py imports it at
module scope and server.main mounts a container path at import time.

    python3 scripts/test_circles_direct_lane_reason.py
"""
from __future__ import annotations

import ast
import asyncio
import os
import sys
import types

_ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(__file__), "..")
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain"), os.path.join(_ROOT, "system-apps")]

failures: list[str] = []
notes: list[str] = []


def check(cond, msg):
    if cond:
        notes.append("  PASS  " + msg)
    else:
        failures.append(msg)


ME, PEER = 42, 4242
SLIP = {"terms": {"resource": "peer:inbox"}, "sig": "x"}


def _install_peer_client(*, send):
    """Put a fake server.peer.peer_client in sys.modules. `send` is awaited with
    the real call signature; raise from it to exercise the transport path."""
    pkg = sys.modules.get("server.peer")
    if pkg is None:
        pkg = types.ModuleType("server.peer")
        # The REAL directory, not an empty list. Only peer_client is being
        # replaced; its siblings must still import, and server/peer/deliver.py
        # now carries the wire call this whole file exercises. An empty __path__
        # made that a ModuleNotFoundError that read like a missing module rather
        # than a broken stub.
        pkg.__path__ = [os.path.join(_ROOT, "server", "peer")]
        sys.modules["server.peer"] = pkg
    mod = types.ModuleType("server.peer.peer_client")

    class PeerUnavailable(Exception):
        pass

    mod.PeerUnavailable = PeerUnavailable
    mod.send_to_peer = send
    sys.modules["server.peer.peer_client"] = mod
    pkg.peer_client = mod
    return PeerUnavailable


def _uninstall_peer_client():
    sys.modules.pop("server.peer.peer_client", None)
    pkg = sys.modules.get("server.peer")
    if pkg is not None and hasattr(pkg, "peer_client"):
        del pkg.peer_client
    # Make the `from server.peer.peer_client import ...` inside the function
    # raise, which is the "pod on an image from before Workstream B" case.
    sys.modules["server.peer.peer_client"] = None


async def main() -> int:
    # circles.py does `from server import brain_service, main as server_main`
    # at module scope, and server.main mounts /app/dashboard/base at IMPORT time
    # — a container path. Stub it; nothing under test touches it.
    _stub_main = types.ModuleType("server.main")
    _stub_main.app = None
    sys.modules.setdefault("server.main", _stub_main)

    import server.routes.circles as circles

    # The success path writes our own outbound copy through memory_handlers.
    # Stub it: it is a DB write and it is not what this gate is about.
    import server.bridge.memory_handlers as mh
    mh.handle_inbox_append = lambda *a, **k: asyncio.sleep(0)

    # ---- 1. the peer answers OK ------------------------------------------------
    async def ok_send(target, op, slip, payload):
        return {"ok": True, "data": {"message_id": "m1"}}

    _install_peer_client(send=ok_send)
    got = await circles._try_direct_peer_send(ME, PEER, "hello", SLIP)
    check(isinstance(got, tuple) and len(got) == 2,
          "returns a (data, reason) 2-tuple")
    data, reason = got
    check(reason == "ok", f"success reason is 'ok' (got {reason!r})")
    check((data or {}).get("delivered_via") == "peer_direct",
          "success data still says delivered_via=peer_direct")

    # ---- 2. the peer is unreachable (transport) --------------------------------
    async def raising_send(target, op, slip, payload):
        raise RuntimeError("connect timed out")

    PeerUnavailable = _install_peer_client(send=raising_send)

    async def unavailable_send(target, op, slip, payload):
        raise PeerUnavailable("no route to peer")

    data, reason = await circles._try_direct_peer_send(ME, PEER, "hello", SLIP)
    check(data is None, "unreachable peer falls back (data is None)")
    check(reason == "transport_unavailable:RuntimeError",
          f"unreachable reason is the failure CLASS (got {reason!r})")
    # The reason is returned into an HTTP body. PeerUnavailable formats the dial
    # target into its message, so the text holds the peer's in-cluster service
    # name and its resolved address; the class alone answers "could we reach it".
    check("connect timed out" not in reason,
          "the exception TEXT does not travel into the reason — it stays on the log line")

    _install_peer_client(send=unavailable_send)
    _, reason = await circles._try_direct_peer_send(ME, PEER, "hello", SLIP)
    check(reason == "transport_unavailable:PeerUnavailable",
          f"PeerUnavailable is named, without its message (got {reason!r})")
    check("no route to peer" not in reason,
          "PeerUnavailable's dial-target text is not in the reason either")

    # send_to_peer wraps EVERY transport error in PeerUnavailable, so the outer
    # class alone collapses three different problems into one string. The cause's
    # class is what separates them — and once contracts are the Cilium allow the
    # timeout row is the expected
    # shape of a Cilium policy deny, which is the one an operator most needs to
    # tell apart from "the peer is down".
    async def caused_send(target, op, slip, payload, exc=None):
        try:
            raise exc
        except Exception as inner:
            raise PeerUnavailable(f"connect entity-{target}.svc:8444 failed") from inner

    # socket.gaierror, not a bare OSError: it IS an OSError subclass, so a bare one
    # exercised the mechanism correctly while asserting a string production never
    # emits. A gate that documents the wrong value is a small lie an operator later
    # has to debug against.
    import socket
    for inner_exc, want in ((socket.gaierror(-3, "Temporary failure in name resolution"),
                             "transport_unavailable:PeerUnavailable:gaierror"),
                            (ConnectionRefusedError(111, "Connection refused"),
                             "transport_unavailable:PeerUnavailable:ConnectionRefusedError"),
                            (TimeoutError("timed out"),
                             "transport_unavailable:PeerUnavailable:TimeoutError")):
        _install_peer_client(
            send=lambda t, o, s, p, _e=inner_exc: caused_send(t, o, s, p, _e))
        _, reason = await circles._try_direct_peer_send(ME, PEER, "hello", SLIP)
        check(reason == want,
              f"a {type(inner_exc).__name__} cause is named: {want} (got {reason!r})")
        check("entity-" not in reason and ":8444" not in reason,
              f"...and the dial target stays off the wire (got {reason!r})")

    # ---- 3. the peer answers and says no ---------------------------------------
    async def refusing_send(target, op, slip, payload):
        return {"ok": False, "error": "slip_expired"}

    _install_peer_client(send=refusing_send)
    data, reason = await circles._try_direct_peer_send(ME, PEER, "hello", SLIP)
    check(data is None, "refused send falls back (data is None)")
    check(reason == "refused:slip_expired",
          f"refusal reason names the peer's error (got {reason!r})")
    check(not reason.startswith("transport_unavailable"),
          "a refusal is NOT reported as a transport failure — the whole point")

    # ---- 4. no peer transport on this image ------------------------------------
    _uninstall_peer_client()
    data, reason = await circles._try_direct_peer_send(ME, PEER, "hello", SLIP)
    check(data is None, "missing peer_client falls back (data is None)")
    check(reason.startswith("peer_client_import_failed:"),
          f"older-image reason is peer_client_import_failed:* (got {reason!r})")

    # ---- 5. EVERY target kind reaches the envelope with a reason ---------------
    # Runtime, through the real route. An earlier revision of this change passed a
    # source-level "does it assign direct_lane enough times" check while leaving
    # target_kind='circle' uncovered — the name was unbound on that branch, so the
    # shared failure path raised UnboundLocalError and the route returned 500 with
    # no request_id and no audit row instead of the 502 envelope. Counting
    # assignments cannot see that. Driving the route can.
    from fastapi import HTTPException

    class _Req:
        def __init__(self, body, headers=None):
            self._body = body
            self.headers = headers or {}

        async def json(self):
            return self._body

    circles._entity_id = lambda: ME
    circles._audit = lambda **kw: asyncio.sleep(0)
    circles._bridge_call = lambda op, params=None: asyncio.sleep(0)  # -> None: relay fails
    circles._held_slip_for = lambda t: asyncio.sleep(0)              # -> None: no slip held

    # The dm branch needs three things before it can REACH the failure path, and
    # each one short-circuits to a different status if missing: a non-None pool
    # (else the contact lookup is skipped and it is 409 not_connected), a contact
    # row (409), and a verified email header (else 403 no_identity). Without all
    # three the dm case silently tests nothing.
    import server.routes.contacts as contacts
    circles.brain_service.get_pool = lambda: object()
    contacts.find_human_contact = lambda pool, email: asyncio.sleep(
        0, {"peer_entity_id": PEER, "status": "accepted", "display_name": "P"})
    _EMAIL = {"x-authentik-email": "me@example.com"}

    for kind, tid, want_prefix, hdrs in (
            ("circle", 7, "not_attempted:circle", None),
            ("entity", PEER, "not_attempted:no_slip_held", None),
            # dm is driven because the structural check below keys on the CIRCLE
            # fork: a future reorder putting the dm fork above it, with the
            # placeholder in between, would leave dm unbound and neither the
            # structural check nor a circle-only runtime check would see it.
            #
            # This expectation was 'not_attempted:person_dm' — the person
            # DM was barred from the direct lane outright, so the reason never
            # depended on whether a slip was held. Person-vouching moved to
            # the host and the dm fork now reaches the same slip test
            # as the entity fork, so with _held_slip_for stubbed to None both report
            # no_slip_held. The slip-HELD half of the new behaviour is exercised
            # below, because this loop can only ever see the failure envelope.
            ("dm", "peer@example.com", "not_attempted:no_slip_held", _EMAIL)):
        try:
            await circles.send_message(_Req({"target_kind": kind,
                                             "target_id": tid, "body": "hi"}, hdrs))
            failures.append(f"{kind}: expected the 502 envelope, got a normal return")
        except HTTPException as he:
            err = (he.detail or {}).get("error", {})
            check(he.status_code == 502,
                  f"target_kind={kind}: relay failure is a 502, not a crash "
                  f"(got {he.status_code})")
            check(isinstance(err.get("direct_lane"), str),
                  f"target_kind={kind}: the envelope carries a direct_lane string "
                  f"(got {err.get('direct_lane')!r})")
            check(err.get("direct_lane") == want_prefix,
                  f"target_kind={kind}: direct_lane is {want_prefix!r} "
                  f"(got {err.get('direct_lane')!r})")
            check(bool(err.get("request_id")),
                  f"target_kind={kind}: the envelope still carries a request_id")
        except Exception as e:  # UnboundLocalError lands here — the regression
            failures.append(f"target_kind={kind}: the route raised "
                            f"{type(e).__name__}: {e} instead of the 502 envelope")

    # ── WITH a slip held, BOTH host↔host kinds take the direct lane, and
    # the person travels on it ──────────────────────────────────────────────────
    #
    # The loop above can only observe the failure envelope, so on its own it would
    # let "person DMs are barred from the lane" come back as a silent regression:
    # every reason it can see is a not_attempted string. These drive the success
    # path and assert what actually goes on the wire.
    print("\n── 7. slip held: host↔host and person↔person both go direct ──")
    sent: list[tuple] = []

    async def capture(peer_entity_id, op, slip, params, *, timeout=10.0):
        sent.append((peer_entity_id, op, slip, params))
        return {"ok": True, "data": {"stored": True, "person_stored": True}}

    _install_peer_client(send=capture)
    circles._held_slip_for = lambda t: asyncio.sleep(0, SLIP)
    import server.bridge.memory_handlers as mh
    appended: list[dict] = []

    async def _fake_append(payload):
        appended.append(payload)
        return {"ok": True}

    mh.handle_inbox_append = _fake_append

    # host → host, typed by a human. This is the case the old `not sender_email`
    # guard sent to the bridge; it must now go direct AND carry the author.
    sent.clear(); appended.clear()
    await circles.send_message(_Req({"target_kind": "entity",
                                     "target_id": PEER, "body": "hi"}, _EMAIL))
    check(len(sent) == 1 and sent[0][1] == "inbox.deliver",
          f"human-authored host↔host send takes the direct lane (sent={len(sent)})")
    if sent:
        p = sent[0][3]
        check(p.get("from_user_email") == "me@example.com",
              f"...and asserts its own operator as the author (got {p.get('from_user_email')!r})")
        check(p.get("thread_id"),
              "...and carries a thread_id, so both lanes group the same way")
        check(p.get("subject") == "Direct message",
              f"...and the SAME subject the relay stores, not a body excerpt "
              f"(got {p.get('subject')!r})")
        check(bool(p.get("message_id")),
              "...and a message_id: send_to_peer raises on the READLINE, so a "
              "delivery can commit on the peer and still time out on the way "
              "back — without an id the bridge fallback lands a second copy")
    check(any(a.get("direction") == "outbound" for a in appended),
          "...and still writes the sender's own outbound copy")

    # person → person. Both ends named; we assert ours, the receiver checks theirs.
    sent.clear(); appended.clear()
    await circles.send_message(_Req({"target_kind": "dm",
                                     "target_id": "peer@example.com",
                                     "body": "hi"}, _EMAIL))
    check(len(sent) == 1 and sent[0][1] == "inbox.deliver",
          f"person↔person DM takes the direct lane (sent={len(sent)})")
    if sent:
        p = sent[0][3]
        check(p.get("from_user_email") == "me@example.com"
              and p.get("to_user_email") == "peer@example.com",
              f"...naming BOTH people (got {p.get('from_user_email')!r} → "
              f"{p.get('to_user_email')!r})")
        check("peer_user_name" not in p,
              "...but our private note about them does NOT travel to the peer")
    # The id must be the SAME one the relay would have used, or the dedupe it
    # exists for cannot fire. Minting it inside the direct lane instead of above
    # the fork is the version of this that silently does nothing.
    relayed: list[dict] = []
    circles._bridge_call = lambda op, params=None: (
        relayed.append(params or {}) or asyncio.sleep(0))
    sent.clear()
    _install_peer_client(send=capture)
    await circles.send_message(_Req({"target_kind": "dm",
                                     "target_id": "peer@example.com",
                                     "body": "hi"}, _EMAIL))
    check(bool(relayed) is False,
          "a successful direct send does not also call the relay")
    circles._bridge_call = lambda op, params=None: asyncio.sleep(0)
    own = next((a for a in appended if a.get("direction") == "outbound"), None)
    check(own is not None and own.get("from_user_email") == "me@example.com",
          "the sender's own copy carries from_user_email, or the People thread "
          "shows one side only (its is_self test keys on that column)")
    check(own is not None and (own.get("payload") or {}).get("peer_user_name") == "P",
          "...and peer_user_name, which _list_people_threads reads to title the thread")

    print("\n── 7b. an older receiver: both sides agree, or neither does ──")
    # An older pod reads neither top-level person field: it stores the
    # message host-authored and still answers ok. Without reconciling, we would file
    # OUR copy under People while theirs sat under Hosts. Falling back to the relay
    # cannot repair it — message_id dedupes the second delivery, so the row stays as
    # first stored. So we match what actually happened.
    async def old_receiver(peer_entity_id, op, slip, params, *, timeout=10.0):
        sent.append((peer_entity_id, op, slip, params))
        return {"ok": True, "data": {"message_id": "m1"}}   # no person_stored

    _install_peer_client(send=old_receiver)
    sent.clear(); appended.clear()
    await circles.send_message(_Req({"target_kind": "dm",
                                     "target_id": "peer@example.com",
                                     "body": "hi"}, _EMAIL))
    check(len(sent) == 1, "the send still goes direct and still succeeds")
    own = next((a for a in appended if a.get("direction") == "outbound"), None)
    check(own is not None and "from_user_email" not in own,
          "our own copy drops the person, so it files under Hosts exactly like "
          "the copy the older pod stored")
    check(own is not None and "from_user_name" not in (own.get("payload") or {}),
          "...and carries no person name either")

    # REFUTED is not OLD. The peer understood us and disagreed from its own records;
    # demoting then would move this operator's own sent message out of their private
    # People thread (per-operator) into a Hosts thread every operator of this host
    # can read — their view of their own message changed by the recipient's stale
    # address book. The disagreement is the honest outcome and is kept.
    async def refuting_receiver(peer_entity_id, op, slip, params, *, timeout=10.0):
        sent.append((peer_entity_id, op, slip, params))
        return {"ok": True, "data": {"message_id": "m1", "person_stored": False}}

    _install_peer_client(send=refuting_receiver)
    sent.clear(); appended.clear()
    await circles.send_message(_Req({"target_kind": "dm",
                                     "target_id": "peer@example.com",
                                     "body": "hi"}, _EMAIL))
    own = next((a for a in appended if a.get("direction") == "outbound"), None)
    check(own is not None and own.get("from_user_email") == "me@example.com",
          "a REFUTED claim leaves our own copy as sent — we do not demote our "
          "operator's message out of their private thread on the peer's word")

    # A receiver carrying the change says so, and then we keep the person.
    _install_peer_client(send=capture)
    sent.clear(); appended.clear()
    await circles.send_message(_Req({"target_kind": "dm",
                                     "target_id": "peer@example.com",
                                     "body": "hi"}, _EMAIL))
    own = next((a for a in appended if a.get("direction") == "outbound"), None)
    check(own is not None and own.get("from_user_email") == "me@example.com",
          "a receiver that stored the person keeps it on our copy too")

    circles._held_slip_for = lambda t: asyncio.sleep(0)  # restore for later checks

    # Structural belt-and-braces: the placeholder must be bound before the FIRST
    # fork, so a future branch added below it cannot reintroduce the unbound name.
    src = open(os.path.join(_ROOT, "server", "routes", "circles.py")).read()
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
               and n.name == "send_message"
               and "bridge_delivery_unavailable" in (ast.get_source_segment(src, n) or "")),
              None)
    check(fn is not None, "found the send route that raises bridge_delivery_unavailable")
    if fn is not None:
        first_assign = min(
            (n.lineno for n in ast.walk(fn) if isinstance(n, ast.Assign)
             for t in n.targets if isinstance(t, ast.Name) and t.id == "direct_lane"),
            default=None)
        first_kind_fork = min(
            (n.lineno for n in ast.walk(fn) if isinstance(n, ast.If)
             and "target_kind ==" in (ast.get_source_segment(src, n.test) or "")
             and "circle" in (ast.get_source_segment(src, n.test) or "")),
            default=None)
        check(first_assign is not None and first_kind_fork is not None
              and first_assign < first_kind_fork,
              "direct_lane is bound ABOVE the target_kind fork "
              f"(assigned at {first_assign}, fork at {first_kind_fork})")
        check('"direct_lane": direct_lane' in (ast.get_source_segment(src, fn) or ""),
              "the failure envelope carries direct_lane")

    print("\n".join(notes))
    if failures:
        print("\nFAIL:")
        for f in failures:
            print("  - " + f)
        return 1
    print(f"all direct-lane-reason checks passed ({len(notes)})")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
