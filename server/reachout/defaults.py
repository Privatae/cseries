"""The default channel — the user's own Gmail, built when Gmail is connected.

The Gmail channel with its three
triggers is auto-created at genesis, created_by='platform'; the user may delete
it. The three are the design's email set:

    a decision is waiting      message_kind = decision
    a build finished           system_notification + event build_finished
    a site was published       system_notification + event site_published

WHEN, AND WHY NOT AT GENESIS. The decision assumed every host would already
hold a Gmail connection at genesis. None does: connecting Gmail is a consent
the user gives later, so "at genesis"
becomes "at the moment Gmail is connected" — the first moment the channel can
exist at all. The hook is providers/gmail.handle_callback.

A CHANNEL BELONGS TO A MAILBOX, NOT TO ONE CONNECTION. Every Google consent
creates a new connection row with a new id (integrations/core/connection.
create_connection_vault), and reconnecting is the fix for an expired sign-in —
weekly, while the Google client is in testing mode. A channel pinned to one
connection id therefore broke at the first reconnect, and a second "Your Gmail"
was built beside it. So on every connect, each gmail_send channel whose
connection belongs to the SAME mailbox — the default, and any the user or the
host built — is moved onto the new connection, and the default is built only
when this mailbox has never had one. The old connection's row is still there to
say which mailbox it was: revoking marks a row, it never deletes it
(connection.revoke_vault).

A REMOVED DEFAULT STAYS REMOVED. A channel the user removes is deleted
(service.remove_channel), so the channel table cannot remember it. The
connection can: building the default stamps `reachout_default` on the
connection's config, every later connect of that mailbox carries the stamp, and
a connect that finds it on an earlier connection with no default channel left
knows the user removed it, and builds nothing. That is also why there is still
no boot-time "ensure": it would have no consent to answer to.

NEVER SILENT. A name that is taken moves on to the next one, and a build that
fails anyway raises — the connect hook turns that into a line in the user's
inbox (providers/gmail._ensure_default_channel), never only a log line.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from server.reachout import service

log = logging.getLogger("privatae.reachout.defaults")

GMAIL_CHANNEL_NAME = "Your Gmail"

# Stamped on a connection's config once this mailbox's default was built.
DEFAULT_STAMP = "reachout_default"

# How many names are tried before the build is refused out loud: "Your Gmail",
# then with the masked address, then that numbered. Nine mailboxes on one host
# sharing a first letter and a domain is the case that would need more.
_NAME_TRIES = 10

# The design's email set, verbatim.
# Labels are what the user reads in the pane, so they name the thing that
# happened rather than the machinery.
GMAIL_DEFAULT_TRIGGERS: tuple[dict[str, Any], ...] = (
    {"label": "A decision is waiting for you", "match_kind": "decision"},
    {"label": "A build finished", "match_kind": "system_notification",
     "match_event": "build_finished"},
    {"label": "A site was published", "match_kind": "system_notification",
     "match_event": "site_published"},
)


async def _mailbox_of(connection_id: str) -> str | None:
    """The account address a Gmail connection was consented for, lowercased, or
    None when there is no such connection or it recorded no address."""
    from server.integrations.core import storage
    row = await storage.get_connection(str(connection_id or ""))
    if not row or row.get("provider_key") != "gmail":
        return None
    addr = str((row.get("connection_config") or {}).get("account_email") or "")
    return addr.strip().lower() or None


async def _stamp(connection_id: str) -> None:
    """Record on the connection that its mailbox's default exists or existed.

    Best-effort: the stamp only remembers a removal for next time, so a store
    that refuses the write costs that memory, never the channel just built."""
    from server.integrations.core import storage
    try:
        row = await storage.get_connection(str(connection_id))
        if not row:
            return
        cfg = dict(row.get("connection_config") or {})
        if cfg.get(DEFAULT_STAMP):
            return
        cfg[DEFAULT_STAMP] = "built"
        await storage.update_connection(str(connection_id), connection_config=json.dumps(cfg))
    except Exception as e:  # noqa: BLE001 — see the docstring
        log.warning("reachout: could not stamp connection %s: %s", connection_id, e)


async def _removed_before(mailbox: str, connection_id: str) -> bool:
    """Did an EARLIER connection of this mailbox already have its default built?"""
    from server.integrations.core import storage
    for row in await storage.list_connections(provider_key="gmail"):
        if row.get("id") == connection_id:
            continue
        cfg = row.get("connection_config") or {}
        if (str(cfg.get("account_email") or "").strip().lower() == mailbox
                and cfg.get(DEFAULT_STAMP)):
            return True
    return False


def _action_of(raw: Any) -> dict:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return {}
    return raw if isinstance(raw, dict) else {}


async def _gmail_channels(pool, entity_id: int) -> list[dict]:
    rows = await pool.fetch(
        "SELECT id, name, action, created_by FROM reachout_channels WHERE entity_id=$1",
        int(entity_id))
    out = []
    for r in rows:
        action = _action_of(r["action"])
        if action.get("type") == "gmail_send":
            out.append({"id": r["id"], "name": r["name"], "action": action,
                        "created_by": r["created_by"]})
    return out


async def _has_platform_default(pool, entity_id: int, mailbox: str) -> bool:
    """Is there a platform-built gmail_send channel on this mailbox now?"""
    for ch in await _gmail_channels(pool, entity_id):
        if (ch["created_by"] == "platform"
                and await _mailbox_of(str(ch["action"].get("connection_id") or "")) == mailbox):
            return True
    return False


async def _build(pool, entity_id: int, action: dict, mailbox: str | None) -> dict | None:
    """Insert the default channel under the first free name. None when a
    concurrent connect of the same mailbox built it first."""
    taken = {r["name"] for r in await pool.fetch(
        "SELECT name FROM reachout_channels WHERE entity_id=$1", int(entity_id))}
    masked = action.get("pinned_to") or ""
    names = [GMAIL_CHANNEL_NAME, f"{GMAIL_CHANNEL_NAME} ({masked})"]
    names += [f"{GMAIL_CHANNEL_NAME} ({masked}) {n}" for n in range(2, _NAME_TRIES)]
    for name in names:
        if name in taken:
            continue
        try:
            return await service.add_channel(
                pool, entity_id, name=name, kind="email", action=action,
                created_by="platform")
        except Exception as e:  # noqa: BLE001 — only a lost race is retried
            # Caught by NAME: asyncpg is importable only where it is installed,
            # and this module is imported by the container-free gate too.
            if type(e).__name__ != "UniqueViolationError":
                raise
            # Someone took the name between the read and the insert. If that
            # someone was a second connect of THIS mailbox (two consent
            # callbacks overlapping), its default is the one to keep — building
            # another under the next name would mail every event twice.
            if mailbox is not None and await _has_platform_default(pool, entity_id, mailbox):
                return None
            taken.add(name)
    raise service.ReachoutError(
        f"every name tried for this mailbox's channel is taken ({names[0]!r} to "
        f"{names[-1]!r}) — rename or remove one of them and connect Gmail again")


async def ensure_gmail_channel(pool, entity_id: int, connection_id: str) -> dict:
    """Give this mailbox a working channel through `connection_id`.

    Moves every gmail_send channel of the same mailbox onto this connection,
    then builds the default with its three triggers if the mailbox never had
    one. Returns what it did, for the log line and the tests. Raises when the
    connection cannot carry a channel at all (service.ReachoutError, with the
    reason) — the caller decides how the user hears it.
    """
    # The check at creation and the check at send are the same check.
    action = await service.validate_action(
        pool, entity_id, {"type": "gmail_send", "connection_id": connection_id})
    mailbox = await _mailbox_of(connection_id)

    moved: list[str] = []
    have_default = False
    for ch in await _gmail_channels(pool, entity_id):
        pinned = str(ch["action"].get("connection_id") or "")
        if pinned != connection_id and (mailbox is None or await _mailbox_of(pinned) != mailbox):
            continue
        have_default = have_default or ch["created_by"] == "platform"
        if pinned != connection_id:
            await pool.execute(
                "UPDATE reachout_channels SET action=$1::jsonb WHERE id=$2 AND entity_id=$3",
                json.dumps(action), ch["id"], int(entity_id))
            moved.append(ch["name"])
    if moved:
        log.info("reachout: moved %s onto Gmail connection %s", moved, connection_id)

    if have_default:
        await _stamp(connection_id)
        return {"created": False, "moved": moved,
                "reason": "this mailbox's channel sends through the new connection"
                if moved else "already built"}
    if mailbox is not None and await _removed_before(mailbox, connection_id):
        await _stamp(connection_id)
        return {"created": False, "moved": moved,
                "reason": "the user removed this mailbox's channel, so it is not rebuilt"}

    ch = await _build(pool, entity_id, action, mailbox)
    if ch is None:
        await _stamp(connection_id)
        return {"created": False, "moved": moved,
                "reason": "a concurrent connect of this mailbox built it"}
    # All or nothing. A channel left with fewer than its three triggers would
    # count as "this mailbox has a default" on every later connect and never be
    # completed, so a failure part-way removes the channel and raises — the
    # caller tells the user, and the next connect builds it whole.
    made = []
    try:
        for t in GMAIL_DEFAULT_TRIGGERS:
            tr = await service.add_trigger(
                pool, entity_id, channel=ch["id"], label=t["label"],
                match_kind=t["match_kind"], match_event=t.get("match_event"),
                created_by="platform")
            made.append(tr.get("label"))
    except Exception:
        await pool.execute("DELETE FROM reachout_channels WHERE id=$1 AND entity_id=$2",
                           ch["id"], int(entity_id))
        raise
    await _stamp(connection_id)
    log.info("reachout: default channel %r built on connection %s (%d triggers)",
             ch["name"], connection_id, len(made))
    return {"created": True, "channel": ch["name"], "triggers": made, "moved": moved}
