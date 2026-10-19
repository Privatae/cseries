"""This Host's OWN card facts — what it hands a Host whose connection it has
accepted (name and number are shared on accept), and which
Hosts belong to the same account ("My host").

The pod cannot read the platform's users / operator_entities / handle registry,
so it asks the bridge (op host.self_card, self-scoped by its mTLS cert). Cached
briefly: a name or number rarely changes, and an accept must not wait on it.
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger("privatae.hosts.self_card")

_TTL_S = 300
_cache: dict = {"at": 0.0, "card": None}


async def own_card(*, fresh: bool = False) -> dict | None:
    """{owner_name, phone, account_hosts} or None when the bridge cannot say
    (an older bridge without the op, or no connection) — never guessed."""
    if not fresh and _cache["card"] is not None and time.monotonic() - _cache["at"] < _TTL_S:
        return _cache["card"]
    try:
        from server.routes.contacts import _bridge_op
        got = await _bridge_op("host.self_card", {})
    except Exception as e:
        log.info("host.self_card unavailable: %s", type(e).__name__)
        return None
    if not isinstance(got, dict):
        return None
    hosts = []
    for h in got.get("account_hosts") or []:
        try:
            hosts.append(int(h))
        except (TypeError, ValueError):
            pass
    card = {"owner_name": got.get("owner_name") or None,
            "phone": got.get("phone") or None,
            "account_hosts": hosts}
    _cache.update(at=time.monotonic(), card=card)
    return card


async def shareable_card() -> dict | None:
    """The part of our card that goes to an ACCEPTED contact: the owner's name
    and this Host's number. Never the account's other Hosts."""
    c = await own_card()
    if not c or not (c.get("owner_name") or c.get("phone")):
        return None
    return {k: c[k] for k in ("owner_name", "phone") if c.get(k)}
