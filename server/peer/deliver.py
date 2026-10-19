"""Send one message down the DIRECT peer lane, and say plainly why if it did not.

ONE implementation, because there are now two callers and a second copy would
drift. Circles' composer reaches host↔host and person↔person sends here; the
`host_message` tool's send service (server/routes/messages.py) reaches the same
code for the same reason — both used to hand the message to the bridge relay,
which keeps its connections in an in-process dict per DaemonSet pod and so could
only ever deliver within a node.

What belongs here is the WIRE and the DIAGNOSIS. What does not: the sender's own
copy of the message. The two callers store that differently — circles writes it
through handle_inbox_append after a successful send, messages.py INSERTs it
before trying to deliver at all — and folding that in would force one of them to
change behaviour for no reason.

The reason string is the point of the return, not a decoration. Both lanes can
fail for unrelated causes, and before it existed a dead host↔host message
surfaced as one 502 naming only the relay. Under the network policy the
two failures mean different things and have different fixes:

    transport_unavailable  → no policy allow (no contract, or it expired)
    refused:<err>          → reachable, but the peer rejected the slip
"""
from __future__ import annotations

import logging

log = logging.getLogger("peer.deliver")

#: The subject a host↔host DM is stored under, on BOTH lanes and on BOTH sides.
#: The relay's op_circles_send_entity_dm defaults to this and the composer sends
#: no subject, so it is what every relayed DM has always carried; the direct lane
#: once wrote a body excerpt. It lives here because the sender's own copy
#: has to match what the recipient stored, and two literals is how that drifts.
DM_SUBJECT = "Direct message"


async def deliver_message(sender_id: int, target_id: int, slip: dict, *,
                          subject: str, body: str,
                          thread_id: str | None = None,
                          message_id: str | None = None,
                          from_user_email: str | None = None,
                          from_user_name: str | None = None,
                          to_user_email: str | None = None,
                          topic: dict | None = None,
                          notice: dict | None = None,
                          ) -> tuple[dict | None, str, bool | None]:
    """Returns (data, reason, person_stored).

    `topic` is the nested payload a topic travels in (server/peer/topics.py
    wire): {topic_id, topic_title}, or None/{} for the original conversation,
    which sends no nested payload at all, as before topics.

    `notice` is the nested payload a reading notice travels in
    (server/circles_reading.py NOTICE_KEY), on a person-to-person message.

    `data` is None to mean FALL BACK TO THE RELAY — peer unreachable, on an image
    without this lane, or the slip refused. A caller that holds no slip should not
    call here at all: `message` is slip-only by design on the receiving side.

    `person_stored` is the receiver's answer to "did you keep the person I named":
    True kept, False refuted or unusable, None never asked or an image that does
    not answer. The distinction between False and None is load-bearing and callers
    must not collapse it — absence means an older image that read no person field,
    which deserves a different response from a peer that understood and disagreed.
    """
    try:
        from server.peer.peer_client import send_to_peer  # noqa: F401
    except Exception as ie:
        # The peer transport module is absent — this pod is on an image from
        # before Workstream B. Not an error: the relay is the whole fallback.
        reason = f"peer_client_import_failed:{type(ie).__name__}"
        log.warning("direct peer lane unavailable (entity %s → %s): %s — bridge fallback",
                    sender_id, target_id, reason)
        return None, reason, None

    wire: dict = {"subject": subject, "body": body, "body_format": "text/plain",
                  "thread_id": thread_id, "message_id": message_id}
    if from_user_email:
        wire["from_user_email"] = from_user_email
        if from_user_name:
            wire["from_user_name"] = from_user_name
    if to_user_email:
        wire["to_user_email"] = to_user_email
    if topic or notice:
        # A receiver from before topics drops it (its allow-list is empty) and
        # files the message in the one conversation, which is the whole fallback.
        # A receiver from before the reading notice drops that key the same way
        # and shows the notice as the plain message it also is.
        wire["payload"] = {**(topic or {}), **(notice or {})}

    try:
        resp = await send_to_peer(int(target_id), "inbox.deliver", slip, wire)
    except Exception as e:  # PeerUnavailable (transport) — fall back to the relay
        # The reason goes into an HTTP body, so it carries failure CLASSES and
        # never the exception text. PeerUnavailable formats the dial target into
        # its message (peer_client.py), so the text holds the peer's in-cluster
        # service name and, via the OS error, its resolved ClusterIP. The full
        # text is on the WARNING line below, correlated by the same entity ids.
        #
        # The CAUSE is appended because send_to_peer wraps every transport error
        # in PeerUnavailable, so the outer class alone collapses three different
        # problems — and three different fixes — into one string:
        #
        #   …:gaierror                no such service (peer never existed, wrong id)
        #   …:ConnectionRefusedError  resolves, nothing listening (peer pod down)
        #   …:TimeoutError            packets dropped — under the network
        #                             policy that is the expected shape of a
        #                             policy deny, i.e. no contract or an expired one
        #
        # A cause's class name carries no address, so this costs nothing on the
        # wire. Not every raise has one (peer_client's empty-response path).
        cause = getattr(e, "__cause__", None)
        reason = f"transport_unavailable:{type(e).__name__}"
        if cause is not None:
            reason += f":{type(cause).__name__}"
        log.warning("direct peer lane unavailable (entity %s → %s): %s: %s — bridge fallback",
                    sender_id, target_id, reason, e)
        return None, reason, None

    if not resp.get("ok"):
        # The peer answered and said no: the slip was refused, expired, or the
        # contract behind it is revoked. Distinct from "could not reach" and the
        # distinction is the diagnosis — after A3 an unreachable peer means no
        # Cilium allow, a refusal means no contract.
        reason = f"refused:{resp.get('error')}"
        log.warning("direct peer send refused (entity %s → %s): %s — bridge fallback",
                    sender_id, target_id, reason)
        return None, reason, None

    rdata = resp.get("data") or {}
    person_stored = rdata.get("person_stored") if "person_stored" in rdata else None
    return {"delivered_via": "peer_direct", **rdata}, "ok", person_stored
