"""Messaging connectors: the grant, the pairing and the connector capability.

The decisions it holds to:

  1. Revoking while a connector turn runs CANCELS that turn. Every revoke path
     goes through revoke_connector(); after it commits it calls
     cancel_connector_turns(), the hook the connector route registers its
     canceller on (register_turn_canceller). Nothing is registered until the
     route that tracks connector turns exists.
  2. One messaging account per connector app: approving a new account revokes
     the old grant in the same transaction.
  3. Revoking does NOT stop the app. It clears the capability, so every post is
     refused at once, and the app keeps running. Deleting the app is the stop.
  4. The capability is minted when PAIRING STARTS, not on approval. Before the
     grant is approved it opens /api/connector/pair only, and only while that
     app has an open pairing; after approval the same capability opens the
     message routes. Re-pairing an app that already holds a live capability
     keeps it (no restart); a pairing after a revoke mints a fresh one.

WHAT THE BOUNDARY IS (4.1). The kernel never talks to the messaging platform.
The account a pairing binds, and the sender of every message, are what the
connector app reports. The capability is the wall: it is what lets an app
start the person's turns at all, and it is issued to one connector app. The
one-time code ties the reported account to the person who typed it, which is
why the code never reaches the model: it is generated here from os.urandom,
stored as a SHA-256 only, held in this process's memory for its ten minutes,
and shown only by a route that answers the person's own windows
(routes/network.py nac_connector_code). The limit of every NAC widen
applies (nac_stepup's docstring): code running inside the Host's container can
read the database and the authenticator secret.

FAIL CLOSED. No grant means no turn, and a read that fails means no turn: every
check here raises or returns None, and the route dependency
(routes/connector.py require_connector_cap) answers 403 or 503. Nothing is
cached; the first post after a revoke commits is refused.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from server import host_events
from server.network.nac_commit import CommitError, _rows_affected, approver_for

log = logging.getLogger("privatae.network.connector")


class ConnectorError(CommitError):
    """A connector refusal with the HTTP status its route answers. A CommitError,
    so every NAC caller that already maps CommitError (the routes, the host
    tools' `detail`) handles it unchanged."""


# ── the platforms ──────────────────────────────────────────────────────────
#
# Each platform's API hosts, read from the vendor's own documentation on
# 2026-10-04. A connector app's manifest network[] may hold these and nothing
# else (the pin, network_pin_refusal); the app-egress sidecar also lets a
# declared host's subdomains through, so a subdomain of one of these is allowed
# too.
#
#   telegram  api.telegram.org
#             https://core.telegram.org/bots/api — "All queries to the Telegram
#             Bot API must be served over HTTPS and need to be presented in this
#             form: https://api.telegram.org/bot<token>/METHOD_NAME". getUpdates
#             (long polling), sendMessage, sendPhoto (multipart) and file
#             downloads (https://api.telegram.org/file/bot<token>/<file_path>)
#             all go to that one host. The token is IN THE URL PATH.
#   discord   discord.com              REST, "The API base URL is
#                                      https://discord.com/api/v10"
#                                      (https://docs.discord.com/developers/reference),
#                                      `Authorization: Bot <token>`
#             gateway.discord.gg       the Gateway WebSocket: GET /gateway/bot
#                                      returns "url": "wss://gateway.discord.gg/"
#                                      (https://docs.discord.com/developers/events/gateway);
#                                      the token travels INSIDE the Identify
#                                      payload (op 2, d.token), not in a header
#             cdn.discordapp.com       attachments a user sent: "Attachments
#                                      uploaded to Discord's CDN ... have signed
#                                      URLs" (reference page); no token
#             The connector never opens the gateway itself: the kernel holds it
#             (routes/connector.py connector_gateway, connectors/gateway.py),
#             because the token goes inside it. The resume_gateway_url READY
#             gives is followed only to discord.gg or a subdomain of it.
#   slack     slack.com                Web API "https://slack.com/api/METHOD"
#                                      with `Authorization: Bearer xoxb-...`
#                                      (https://docs.slack.dev/apis/web-api/);
#                                      apps.connections.open takes the app-level
#                                      xapp- token the same way
#                                      (https://docs.slack.dev/reference/methods/apps.connections.open)
#             (subdomains)             the Socket Mode WebSocket is on a
#                                      wss*.slack.com host whose name the docs
#                                      give only as an example; file uploads go to
#                                      files.slack.com
#                                      (https://docs.slack.dev/reference/methods/files.getUploadURLExternal).
#                                      Both are subdomains of slack.com.
PLATFORMS: dict[str, dict[str, Any]] = {
    "telegram": {
        "label": "Telegram",
        "hosts": ("api.telegram.org",),
        "api_host": "api.telegram.org",
        # The broker puts the token into the request PATH, as /bot<token>/...
        "token_in": "path",
        "path_prefix": "/bot{secret}/",
        # The sender's numeric user id.
        "account_re": re.compile(r"^[0-9]{1,20}$"),
        "account_words": "the sender's numeric Telegram user id",
    },
    "discord": {
        "label": "Discord",
        "hosts": ("discord.com", "gateway.discord.gg", "cdn.discordapp.com"),
        "api_host": "discord.com",
        "token_in": "header",
        "auth_scheme": "header",
        "auth_prefix": "Bot ",
        # A snowflake: a 64-bit integer written in decimal.
        "account_re": re.compile(r"^[0-9]{5,20}$"),
        "account_words": "the sender's Discord user id",
    },
    "slack": {
        "label": "Slack",
        "hosts": ("slack.com",),
        "api_host": "slack.com",
        "token_in": "header",
        "auth_scheme": "bearer",
        # Slack user ids belong to one workspace, so the account is TEAM:USER.
        "account_re": re.compile(r"^[A-Z0-9]{2,32}:[A-Z0-9]{2,32}$"),
        "account_words": "the workspace (team) id and the user id, as TEAM:USER",
    },
}


def platform_label(platform: str) -> str:
    return (PLATFORMS.get(platform) or {}).get("label") or str(platform)


def _host_ok(host: str, allowed: tuple[str, ...]) -> bool:
    h = str(host or "").strip().lower().rstrip(".")
    return bool(h) and any(h == a or h.endswith("." + a) for a in allowed)


def network_pin_refusal(platform: str, network: list) -> Optional[str]:
    """None when every host in a connector app's network[] is one of the
    platform's API hosts (or a subdomain of one), else the sentence that says
    which host is not. An empty list is refused too: a connector that reaches
    nothing cannot be one."""
    spec = PLATFORMS.get(platform)
    if spec is None:
        return f"unknown platform {platform!r} (telegram, discord or slack)"
    hosts = [str(h).strip() for h in (network or []) if str(h).strip()]
    if not hosts:
        return (f"the app's manifest declares no network hosts; a {spec['label']} connector "
                f"declares {', '.join(spec['hosts'])}")
    foreign = [h for h in hosts if not _host_ok(h, spec["hosts"])]
    if foreign:
        return (f"the app's manifest network[] holds {', '.join(foreign)}, which "
                f"{'is' if len(foreign) == 1 else 'are'} not {spec['label']}'s. A connector "
                f"may reach only {', '.join(spec['hosts'])} (and their subdomains): its "
                f"capability starts turns as the person, so it cannot also talk to anything else")
    return None


def _install_root():
    from server.applications.installer import INSTALL_ROOT
    return INSTALL_ROOT


def _load_manifest(app_id: str):
    from server.applications.manifest import load as load_manifest
    return load_manifest(_install_root() / app_id / "app.yaml")


def app_refusal(app_id: str, platform: str, manifest=None) -> Optional[str]:
    """None when `app_id` can be a connector for `platform`, else why not. It
    must be a process-form app (its own runtime pod, from a toolchain image):
    only those pods are allowed to reach their Host on 8800 (the operator's
    app-runtime-process-egress policy). It must be resident, because a
    connector listens all the time. And its network[] must be pinned to the
    platform's hosts."""
    if platform not in PLATFORMS:
        return f"unknown platform {platform!r} (telegram, discord or slack)"
    if not re.match(r"^[a-z0-9][a-z0-9._-]{0,63}$", str(app_id or "")):
        return "name the connector app by its id"
    try:
        m = manifest if manifest is not None else _load_manifest(app_id)
    except Exception as e:  # noqa: BLE001 — no manifest = not a connector
        return f"the app {app_id!r} is not installed on this Host ({type(e).__name__})"
    rt = m.runtime
    if rt.kind != "process" or rt.image is None:
        return (f"the app {app_id!r} is not a process app in its own runtime pod "
                f"(runtime.kind=process with runtime.image): only those can reach your Host")
    if getattr(rt, "lifecycle", "window") != "resident":
        return (f"the app {app_id!r} is not resident (runtime.lifecycle=resident): a "
                f"connector has to keep listening when no window is open")
    return network_pin_refusal(platform, list(getattr(m, "network", None) or []))


def check_account(platform: str, account_id: Any, display_name: Any) -> tuple[str, str]:
    """(account_id, display_name) cleaned, or ConnectorError(400). The account is
    the connector's report (the kernel never talks to the platform); its shape
    is checked so a card never shows something that cannot be an account."""
    spec = PLATFORMS[platform]
    acc = str(account_id if account_id is not None else "").strip()
    if not spec["account_re"].match(acc):
        raise ConnectorError(400, f"account_id must be {spec['account_words']}")
    return acc, clean_name(display_name)


def clean_name(raw: Any) -> str:
    """A name a connector reports, as it may be shown: control characters and
    the invisible format ones (Unicode Cf: the bidi overrides and isolates,
    zero-width joiners) become spaces, runs of space one, at most 80
    characters. The name sits right before the account id on the person's
    approval card, and a right-to-left override in it would draw that id
    reversed. A forwarded message's author name (connectors/turns.py
    parse_origin) is cleaned the same way."""
    name = "".join(" " if unicodedata.category(c) in ("Cc", "Cf") else c
                   for c in str(raw or ""))
    return re.sub(r"\s+", " ", name).strip()[:80]


# ── the one-time code ──────────────────────────────────────────────────────
#
# RFC 8628's example for a code read on one screen and typed on another device
# (§5.1, §6.1): 8 characters from 20 consonants, ~34.5 bits, shown as BCDF-GHJK.
CODE_ALPHABET = "BCDFGHJKLMNPQRSTVWXZ"
CODE_LEN = 8
PAIRING_TTL_S = 600          # the person is right there when they start it
PAIRING_MAX_WRONG = 5        # counted across every sender; a well-formed code only

# grant id -> (code, expires_at monotonic). The ONLY place the code exists in
# the clear: the database holds its SHA-256. A kernel restart loses it, and the
# pairing then has to be started again — which is the cost of never storing it.
_codes: dict[int, tuple[str, float]] = {}


def new_code() -> str:
    """8 characters from CODE_ALPHABET, from os.urandom, without modulo bias
    (a byte is used only below the largest multiple of 20 that fits)."""
    out: list[str] = []
    limit = 256 - (256 % len(CODE_ALPHABET))
    while len(out) < CODE_LEN:
        for b in os.urandom(CODE_LEN * 2):
            if b < limit:
                out.append(CODE_ALPHABET[b % len(CODE_ALPHABET)])
                if len(out) == CODE_LEN:
                    break
    return "".join(out)


def show_code(code: str) -> str:
    return f"{code[:4]}-{code[4:]}"


def normalize_code(raw: Any) -> str:
    """Upper-cased, spaces and hyphens dropped — the RFC 8628 §6.1 input rule."""
    return re.sub(r"[\s-]+", "", str(raw or "")).upper()


def well_formed(code: str) -> bool:
    return len(code) == CODE_LEN and all(c in CODE_ALPHABET for c in code)


def code_hash(code: str) -> str:
    return hashlib.sha256(("connector-pairing:" + code).encode("ascii")).hexdigest()


def _same(a: str, b: str) -> bool:
    try:
        return hmac.compare_digest(str(a).encode("ascii"), str(b).encode("ascii"))
    except (TypeError, UnicodeError):
        return False


# ── the turn-cancel hook (decision 1) ──────────────────────────────────────
#
# A revoke cancels the connector's turn in flight. Which turn that is, the
# connector route knows (it tracks the turns it started); this module does not.
# So revoke_connector() calls cancel_connector_turns() AFTER its transaction
# commits, and the route registers the function that finds and cancels them:
#
#     nac_connector.register_turn_canceller(my_canceller)
#     async def my_canceller(*, app_id: str, grant_id: int, reason: str) -> None
#
# A canceller that raises is logged and does not undo the revoke: the grant is
# already gone and every later post is refused either way.
TurnCanceller = Callable[..., Awaitable[Any]]
_TURN_CANCELLERS: list[TurnCanceller] = []


def register_turn_canceller(fn: TurnCanceller) -> None:
    if fn not in _TURN_CANCELLERS:
        _TURN_CANCELLERS.append(fn)


async def cancel_connector_turns(*, app_id: str, grant_id: Optional[int], reason: str) -> int:
    """Call every registered canceller for the revoked grant. Returns how many
    ran without raising."""
    ran = 0
    for fn in list(_TURN_CANCELLERS):
        try:
            await fn(app_id=app_id, grant_id=grant_id, reason=reason)
            ran += 1
        except Exception as e:  # noqa: BLE001 — the revoke already stands
            log.warning("connector %s: turn canceller %r failed after revoke: %r",
                        app_id, getattr(fn, "__name__", fn), e)
    return ran


# ── reading ────────────────────────────────────────────────────────────────

_ROW_COLS = ("id, app_id, platform, token_name, app_token_name, pairing, code_expires_at, "
             "wrong_tries, "
             "account_id, display_name, matched_at, ticket_id, proposed_at, approved_at, "
             "approved_by, stepup_at, revoked_at, revoked_by, revoke_reason, asked_in")


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


def _public(row) -> dict:
    """A grant row as the Network app and nac_status read it: never the code,
    never a hash, never the signature."""
    d = dict(row)
    for k in ("code_hash", "signature", "signed_terms"):
        d.pop(k, None)
    for k in ("code_expires_at", "matched_at", "proposed_at", "approved_at", "stepup_at",
              "revoked_at"):
        if k in d:
            d[k] = _iso(d[k])
    # The conversation the pairing was started in (v204): the chat that
    # reopens shows the code card again for an open pairing of its own.
    if isinstance(d.get("asked_in"), str):
        try:
            d["asked_in"] = json.loads(d["asked_in"])
        except ValueError:
            d["asked_in"] = None
    if d.get("revoked_at"):
        d["status"] = "revoked"
    elif d.get("approved_at"):
        d["status"] = "active"
    elif d.get("pairing") == "matched":
        d["status"] = "awaiting_approval"
    else:
        d["status"] = "pairing"
    d["platform_label"] = platform_label(d.get("platform"))
    return d


async def expire_pairings(pool) -> int:
    """Open pairings past their ten minutes are void, and their drafts declined.
    Called by every reader that shows them, so nothing stale is drawn."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "UPDATE nac_connector_grants SET pairing='expired', code_hash=NULL, "
            "  revoked_at=now(), revoked_by='kernel', revoke_reason='the pairing code expired' "
            "WHERE pairing='open' AND revoked_at IS NULL AND code_expires_at <= now() "
            "RETURNING id, app_id")
    for r in rows:
        _codes.pop(int(r["id"]), None)
        await host_events.emit(pool, None, "connector_expired",
                               {"grant_id": r["id"], "app_id": r["app_id"]})
    return len(rows)


async def list_connectors(pool) -> dict:
    """Every connector grant still in play (pairing, awaiting approval, active)
    and those that ended in the last week, newest first, with each app's
    capability state. For the Network app's Ingress lane; no secret in it."""
    if pool is None:
        raise ConnectorError(503, "brain pool not ready")
    await expire_pairings(pool)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT {_ROW_COLS} FROM nac_connector_grants "
            "WHERE revoked_at IS NULL OR revoked_at > now() - interval '7 days' "
            "ORDER BY id DESC LIMIT 100")
        caps = await conn.fetch(
            "SELECT app_id, (cap_hash IS NOT NULL) AS live, (pending_hash IS NOT NULL) AS pending "
            "FROM nac_connector_caps")
    cap_state = {c["app_id"]: ("live" if c["live"] else ("pending" if c["pending"] else "none"))
                 for c in caps}
    out = []
    for r in rows:
        d = _public(r)
        d["capability"] = cap_state.get(d["app_id"], "none")
        d["code_held"] = int(d["id"]) in _codes and d["status"] == "pairing"
        out.append(d)
    return {"connectors": out}


async def pairing_code(pool, grant_id: int) -> dict:
    """The one-time code of an OPEN pairing, for the person's own window ONLY
    (the route checks the caller). Never reached by a Host tool."""
    if pool is None:
        raise ConnectorError(503, "brain pool not ready")
    await expire_pairings(pool)
    row = await pool.fetchrow(
        "SELECT id, app_id, platform, code_expires_at, "
        "       GREATEST(0, EXTRACT(EPOCH FROM (code_expires_at - now())))::int AS left_s "
        "FROM nac_connector_grants "
        "WHERE id=$1 AND pairing='open' AND revoked_at IS NULL", int(grant_id))
    if row is None:
        raise ConnectorError(404, "no open pairing with that id")
    held = _codes.get(int(grant_id))
    if held is None:
        raise ConnectorError(410, "this pairing's code was lost when your Host restarted; "
                                  "decline it and ask your Host to start the pairing again")
    return {"grant_id": int(row["id"]), "app_id": row["app_id"], "platform": row["platform"],
            "code": show_code(held[0]), "expires_at": _iso(row["code_expires_at"]),
            "seconds_left": int(row["left_s"])}


# ── 1. who starts it: the Host's draft and one pairing ─────────────────────

# The person's chat raises the code card from this result itself (app-web-kit
# ask.ts pairingFromTool): it takes draft_id and reads the code from the code
# route above, which answers the person's own windows only (2026-10-05).
PAIRING_CARD = "The pairing code is on a card in your chat, and in Network."


def _turn_room() -> Optional[dict]:
    """The room of the Host's turn proposing the pairing (memory_engine
    CURRENT_TURN_ROOM), or None outside a turn."""
    try:
        from mastercode.memory_engine import current_turn_room
        return current_turn_room()
    except Exception:  # noqa: BLE001 — no runtime in this process
        return None


async def propose_connector(pool, *, app_id: str, platform: str,
                            token_name: Optional[str] = None,
                            app_token_name: Optional[str] = None) -> dict:
    """The Host's propose_nac_policy kind=connector. Refuses an app that cannot
    be a connector; otherwise declines every earlier draft of this app (one
    pairing at a time), writes the draft with its pairing, and makes sure the
    app holds a capability (issue_connector_cap when it holds none).

    The draft records the conversation the Host's turn is in (asked_in, v204):
    the approval ticket is filed later, outside any turn, and copies it, so the
    card comes up in that chat and the person's answer reaches the Host there.

    The result is model-visible, so it carries no code: it says where the
    person finds it, in the kernel's words (PAIRING_CARD)."""
    if pool is None:
        raise ConnectorError(503, "brain pool not ready")
    app_id = str(app_id or "").strip()
    platform = str(platform or "").strip().lower()
    why = app_refusal(app_id, platform)
    if why:
        raise ConnectorError(400, why)
    token_name = (str(token_name).strip() or None) if token_name else None
    app_token_name = (str(app_token_name).strip() or None) if app_token_name else None
    if app_token_name is not None and platform != "slack":
        raise ConnectorError(400, "app_token_name is Slack's app-level token (Socket Mode); "
                                  f"a {platform_label(platform)} connector has none")
    for name in (token_name, app_token_name):
        if name is not None:
            await _require_vault_name(pool, name)
    code = new_code()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))",
                               "nac_connector:" + app_id)
            # One bot per connector app. Until a re-pair is approved, the
            # connector reaches the platform with the LIVE grant's key, so a
            # pairing for another bot (another key) could never see the code
            # sent to that bot. Switching bots is revoke, then pair.
            live = await conn.fetchrow(
                "SELECT platform, token_name, app_token_name FROM nac_connector_grants "
                "WHERE app_id=$1 AND approved_at IS NOT NULL AND revoked_at IS NULL", app_id)
            if live is not None and (live["platform"], live["token_name"], live["app_token_name"]) \
                    != (platform, token_name, app_token_name):
                raise ConnectorError(
                    409, f"this connector already talks to {platform_label(live['platform'])} with "
                         f"the key {live['token_name']!r}; to pair another bot or key, revoke the "
                         "connector first (Network, or revoke_nac_rule kind=connector), then pair again")
            voided = await conn.fetch(
                "UPDATE nac_connector_grants SET "
                "  pairing = CASE WHEN pairing='open' THEN 'void' ELSE pairing END, "
                "  code_hash=NULL, revoked_at=now(), revoked_by='kernel', "
                "  revoke_reason='a newer pairing replaced it' "
                "WHERE app_id=$1 AND approved_at IS NULL AND revoked_at IS NULL "
                "RETURNING id, ticket_id", app_id)
            room = _turn_room()
            row = await conn.fetchrow(
                "INSERT INTO nac_connector_grants "
                "  (app_id, platform, token_name, app_token_name, pairing, code_hash, "
                "   code_expires_at, asked_in) "
                "VALUES ($1, $2, $3, $4, 'open', $5, now() + make_interval(secs => $6), "
                "        $7::jsonb) "
                "RETURNING id, code_expires_at", app_id, platform, token_name, app_token_name,
                code_hash(code), PAIRING_TTL_S, json.dumps(room) if room else None)
    for v in voided:
        _codes.pop(int(v["id"]), None)
        await _close_ticket(pool, v["ticket_id"], "replaced", v["id"])
        await host_events.emit(pool, None, "connector_declined",
                               {"grant_id": v["id"], "app_id": app_id, "why": "replaced"})
    grant_id = int(row["id"])
    _codes[grant_id] = (code, time.monotonic() + PAIRING_TTL_S)
    cap = await ensure_connector_cap(pool, app_id)
    return {
        "draft_id": grant_id, "kind": "connector", "app_id": app_id, "platform": platform,
        "pairing": "waiting for the person: the code card is in their chat and in Network",
        "expires_at": _iso(row["code_expires_at"]),
        "capability": cap,
        "replaced": [int(v["id"]) for v in voided],
        "card": PAIRING_CARD,
    }


async def _require_vault_name(pool, name: str) -> None:
    from server import brain_service
    from server.vault import api as vault_api
    try:
        rows = await vault_api.list_secrets(pool, entity_id=int(brain_service._entity_id))
    except Exception as e:  # noqa: BLE001
        raise ConnectorError(503, f"could not read Keys to check the token name: {e}") from None
    names = {str(r.get("logical_name")) for r in rows
             if r.get("logical_name") and not r.get("deactivated_at")}
    if name not in names:
        raise ConnectorError(400, f"there is no active key named {name!r} in Keys: the person "
                                  "puts the bot token there first")


# ── 2. the code comes back: POST /api/connector/pair ───────────────────────

CONNECTOR_PAIRING_KIND = "connector_pairing"


async def submit_code(pool, *, app_id: str, code: Any, account_id: Any,
                      display_name: Any) -> dict:
    """The connector reports the code an account sent it. The caller is already
    authenticated by app_id's capability at the pairing stage. Only THIS app's
    open pairing is looked at, so a code shown for another connector app is
    refused before anything is compared with it.

    A malformed code is refused and not counted. A wrong well-formed code counts
    against the pairing, from whichever sender; the fifth voids it. A right one
    binds the reported account to the draft, closes the pairing, and files the
    ticket the person answers."""
    if pool is None:
        raise ConnectorError(503, "brain pool not ready")
    norm = normalize_code(code)
    if not well_formed(norm):
        raise ConnectorError(400, "not a pairing code: 8 letters, like BCDF-GHJK")
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT id, platform, code_hash, wrong_tries, (code_expires_at <= now()) AS expired "
                "FROM nac_connector_grants "
                "WHERE app_id=$1 AND pairing='open' AND revoked_at IS NULL FOR UPDATE", app_id)
            if row is None:
                raise ConnectorError(404, "no pairing is open for this connector")
            gid = int(row["id"])
            acc, name = check_account(row["platform"], account_id, display_name)
            if row["expired"]:
                await conn.execute(
                    "UPDATE nac_connector_grants SET pairing='expired', code_hash=NULL, "
                    "revoked_at=now(), revoked_by='kernel', revoke_reason='the pairing code expired' "
                    "WHERE id=$1", gid)
                outcome = "expired"
            elif not row["code_hash"] or not _same(code_hash(norm), row["code_hash"]):
                tries = int(row["wrong_tries"]) + 1
                if tries >= PAIRING_MAX_WRONG:
                    await conn.execute(
                        "UPDATE nac_connector_grants SET wrong_tries=$2, pairing='void', "
                        "code_hash=NULL, revoked_at=now(), revoked_by='kernel', "
                        "revoke_reason='five wrong pairing codes' WHERE id=$1", gid, tries)
                    outcome = "void"
                else:
                    await conn.execute(
                        "UPDATE nac_connector_grants SET wrong_tries=$2 WHERE id=$1", gid, tries)
                    outcome = "wrong"
            else:
                await conn.execute(
                    "UPDATE nac_connector_grants SET pairing='matched', code_hash=NULL, "
                    "account_id=$2, display_name=$3, matched_at=now() WHERE id=$1",
                    gid, acc, name)
                outcome = "matched"
    if outcome == "expired":
        _codes.pop(gid, None)
        await host_events.emit(pool, None, "connector_expired", {"grant_id": gid, "app_id": app_id})
        raise ConnectorError(410, "the pairing code expired; the person starts a new pairing")
    if outcome == "void":
        _codes.pop(gid, None)
        await host_events.emit(pool, None, "connector_declined",
                               {"grant_id": gid, "app_id": app_id, "why": "wrong_codes"})
        raise ConnectorError(403, "wrong code; that was the fifth, so this pairing is void")
    if outcome == "wrong":
        raise ConnectorError(403, "wrong code")
    _codes.pop(gid, None)
    ticket_id = await _file_approval_ticket(pool, gid)
    # After the match committed and its card was filed: a Host waiting on the
    # pairing wakes with the card's id (server/host_events.py).
    await host_events.emit(pool, None, "connector_paired",
                           {"grant_id": gid, "app_id": app_id, "platform": row["platform"],
                            "ticket_id": ticket_id})
    return {"paired": "awaiting_approval", "grant_id": gid, "ticket_id": ticket_id}


def approval_words(platform: str, account_id: str, display_name: str, app_id: str,
                   app_name: Optional[str] = None,
                   token_names: Optional[list] = None) -> str:
    """The approval card, in the kernel's words. The account is as the connector
    reported it; nothing on the card comes from the model.

    It names the Keys entry the connector sends to the platform (the Host
    picked it when it proposed the pairing), so the person approves the key
    being used as well as the account."""
    label = platform_label(platform)
    who = (f"{label} account “{display_name}” (id {account_id})" if display_name
           else f"{label} account id {account_id}")
    app = f"“{app_name}” ({app_id})" if app_name and app_name != app_id else app_id
    names = [str(n) for n in (token_names or []) if n]
    keys = (", ".join(f"“{n}”" for n in names) if names
            else "no key named yet (the connector cannot call the platform)")
    return (f"Let a {label} account talk to your Host\n"
            f"- Account, as the connector reports it: {who}\n"
            f"- Connector app: {app}\n"
            f"- Key it sends to {label}, from your Keys: {keys}\n"
            f"- Messages from this account will start turns in your chat, with your full "
            f"chat permissions. It cannot approve anything.")


APPROVE_OPTION, DECLINE_OPTION = "let_in", "dont_pair"


async def _file_approval_ticket(pool, grant_id: int) -> Optional[int]:
    """File the ticket only the person can answer (user_only), as the pipeline
    proposal does. Its id goes on the grant, and a decide is honoured only for
    the ticket the grant names (decide_from_ticket).

    The ticket carries the conversation the pairing was started in (the
    grant's asked_in, v204) as its own metadata.asked_in: that chat lists it
    and raises it as its card. The person's answer starts a turn there from
    approve/decline (_tell_host), whichever surface they answer on."""
    from server import brain_service
    row = await pool.fetchrow(
        "SELECT app_id, platform, account_id, display_name, token_name, app_token_name,"
        " asked_in FROM nac_connector_grants WHERE id=$1",
        grant_id)
    room = row["asked_in"]
    if isinstance(room, str):
        try:
            room = json.loads(room)
        except ValueError:
            room = None
    app_name = None
    try:
        app_name = await pool.fetchval("SELECT name FROM installed_applications WHERE id=$1",
                                       row["app_id"])
    except Exception:  # noqa: BLE001 — the id alone is enough to name it
        app_name = None
    sitrep = approval_words(row["platform"], row["account_id"], row["display_name"] or "",
                            row["app_id"], app_name,
                            token_names=[row["token_name"], row["app_token_name"]])
    options = [
        {"id": APPROVE_OPTION, "label": "Let this account in",
         "consequence": "Needs a code from your authenticator. Messages from this account "
                        "start turns in your chat.", "recommend": False},
        {"id": DECLINE_OPTION, "label": "Don't pair",
         "consequence": "Nothing changes; the pairing is closed.", "recommend": False},
    ]
    try:
        from server.routes import tickets as _tk
        t = await _tk.create_ticket_service(
            pool, brain_service._entity_id, source="runtime", sitrep=sitrep, options=options,
            priority=70,
            metadata={"kind": CONNECTOR_PAIRING_KIND,
                      "connector_pairing": {"grant_id": int(grant_id),
                                            "stepup_option": APPROVE_OPTION},
                      "user_only": True, "no_pattern": True,
                      **({"asked_in": room} if isinstance(room, dict) and room else {})})
        tid = int(t["id"])
    except Exception as e:  # noqa: BLE001 — the Network app still shows it
        log.warning("connector grant %d: approval ticket not filed: %r", grant_id, e)
        return None
    # Named on the grant only while the grant still waits: a new pairing, a
    # decline or a revoke may have closed it since the code matched (they
    # close the ticket the grant names, and this one was not named yet). A card
    # for a closed grant is dismissed, never left open to fail when clicked.
    named = await pool.execute(
        "UPDATE nac_connector_grants SET ticket_id=$2 "
        "WHERE id=$1 AND approved_at IS NULL AND revoked_at IS NULL", grant_id, tid)
    if not _rows_affected(named):
        await _close_ticket(pool, tid, "closed", grant_id)
        return None
    return tid


# Only the grant's OWN card is closed: the ticket the kernel filed for it, by
# its kind and the grant id in its metadata. The grant names its ticket by id,
# and the grant table is carried over a restore (host_restore.CARRIED_TABLES)
# while entity_tickets comes back as the backup had it, so after a restore that
# id can be a different ticket of the person's, which a bare `WHERE id=` would
# decide as "Let this account in" or dismiss.
def _own_card(grant_param: str) -> str:
    return (f" AND metadata->>'kind' = '{CONNECTOR_PAIRING_KIND}'"
            f" AND metadata->'connector_pairing'->>'grant_id' = {grant_param}")


async def _close_ticket(pool, ticket_id, outcome: str, grant_id) -> None:
    """Close the approval ticket the grant named, whatever surface decided it,
    when it is that grant's own card (_own_card)."""
    if not ticket_id or pool is None or grant_id is None:
        return
    labels = {"approved": ("let_in", "Let this account in"),
              "declined": ("dont_pair", "Don't pair")}
    gid = str(int(grant_id))
    try:
        if outcome in labels:
            oid, label = labels[outcome]
            done = await pool.execute(
                "UPDATE entity_tickets SET status='decided', decided_at=now(), "
                "decision=$2::jsonb WHERE id=$1 AND status='open'" + _own_card("$3"),
                int(ticket_id), json.dumps({"option_id": oid, "option_label": label,
                                            "and_similar": False, "note": ""}), gid)
            closed = {"ticket_id": ticket_id, "status": "decided", "option_id": oid}
        else:
            done = await pool.execute(
                "UPDATE entity_tickets SET status='dismissed', decided_at=now() "
                "WHERE id=$1 AND status='open'" + _own_card("$2"), int(ticket_id), gid)
            closed = {"ticket_id": ticket_id, "status": "dismissed"}
    except Exception as e:  # noqa: BLE001 — the grant is what counts
        log.info("connector: ticket %s not closed (%s): %r", ticket_id, outcome, e)
        return
    if _rows_affected(done):
        await host_events.emit(pool, None, "ticket_decided", closed)


# ── 3./4. approval: one transaction ────────────────────────────────────────

def signed_terms_for(app_id: str, platform: str, account_id: str) -> dict:
    return {"kind": "connector", "app_id": app_id, "platform": platform,
            "account_id": account_id}


_ANSWER_LABELS = {"approved": "Let this account in", "declined": "Don't pair"}


async def _tell_host(pool, eid: Optional[int], grant_id: int, outcome: str,
                     note: str = "") -> dict:
    """The person's answer to a pairing, told to the Host in the conversation
    the pairing was started in (the grant's asked_in, v204), whichever surface
    they answered on: the card in the chat, the Tickets pane, or the Network
    app. Called only by the one call that changed the grant (approve_connector's
    and decline_connector's compare-and-set), so the Host is told exactly once
    however many surfaces show the pairing; a second action on the same grant
    is refused before it gets here.

    The words are the ask card's (app-web-kit/ask.ts answerText) when the
    approval ticket exists, so a copy the card also sends is read once
    (session._ANSWER_RE). Returns {answer_turn_started} for the cards, which
    then follow that turn instead of sending the answer again; empty when there
    is nobody to tell (a pairing started outside a turn). Never raises: the
    decision already stands."""
    try:
        row = await pool.fetchrow(
            "SELECT asked_in, ticket_id, platform FROM nac_connector_grants WHERE id=$1",
            int(grant_id))
    except Exception as e:  # noqa: BLE001
        log.warning("connector grant %s: answer not delivered: %r", grant_id, e)
        return {}
    room = row["asked_in"] if row is not None else None
    if isinstance(room, str):
        try:
            room = json.loads(room)
        except ValueError:
            room = None
    if not isinstance(room, dict) or not room.get("app_id"):
        return {}
    if eid is None:
        from server import brain_service
        eid = brain_service._entity_id
    label = _ANSWER_LABELS[outcome]
    n = (note or "").strip()
    head = (f"Ticket #{int(row['ticket_id'])}" if row["ticket_id"]
            else f"The {platform_label(row['platform'])} pairing (grant {int(grant_id)})")
    text = f"{head} — I chose “{label}”" + (f": {n}" if n else ".")
    try:
        from mastercode.runtime.session import answer_after_turn
        started = await answer_after_turn(eid, room, text)
    except Exception as e:  # noqa: BLE001 — the decision already stands
        log.warning("connector grant %s: answer not delivered: %r", grant_id, e)
        return {}
    return {"answer_turn_started": bool(started)}


async def approve_connector(pool, eid: int, grant_id: int, *, stepup_code: Optional[str],
                            approved_by: Optional[str] = None, via: str = "unknown",
                            note: str = "") -> dict:
    """The person lets the reported account in. Only from the person's own
    surfaces (via='ui': the Network app, or the ticket answered by the person);
    the Host's approve_nac_draft refuses kind connector before it gets here.

    The authenticator code is checked and BURNED before anything changes
    (nac_stepup.verify_stepup). Then ONE transaction: the older live grant of
    this app is revoked (one account per connector app), the terms are signed
    and this grant goes live, and its ticket is closed. The capability is kept:
    it was minted when the pairing started and opens the message routes from
    the moment this commits. After the commit, a replaced grant's turns are
    cancelled through revoke_connector's hook."""
    if pool is None:
        raise ConnectorError(503, "brain pool not ready")
    if via != "ui":
        raise ConnectorError(403, "a connector grant is approved by the person, in the Network "
                                  "app or on its card — never from a Host tool")
    from server.network import nac_stepup
    row = await pool.fetchrow(
        "SELECT id, app_id, platform, account_id, pairing, approved_at, revoked_at "
        "FROM nac_connector_grants WHERE id=$1", int(grant_id))
    if row is None or row["revoked_at"] is not None:
        raise ConnectorError(404, "no such pairing waiting for approval")
    if row["approved_at"] is not None:
        raise ConnectorError(409, "already approved")
    if row["pairing"] != "matched" or not row["account_id"]:
        raise ConnectorError(409, "no account has sent the pairing code yet")
    if not await nac_stepup.verify_stepup(pool, str(stepup_code or "")):
        raise ConnectorError(403, "letting an account in needs a current code from your "
                                  "authenticator (the second factor enrolled in Network): enter it "
                                  "in the Network app, Ingress, Messaging connectors. Nothing was "
                                  "changed.")
    app_id, platform, account_id = row["app_id"], row["platform"], row["account_id"]
    terms = signed_terms_for(app_id, platform, account_id)
    key_id, sig = await nac_stepup.sign_policy_terms(pool, eid, terms)
    approver = approver_for(via, approved_by)
    async with pool.acquire() as conn:
        async with conn.transaction():
            replaced = await revoke_connector(
                pool, app_id=app_id, conn=conn, live_only=True, keep_cap=True,
                reason=f"replaced by the account approved in grant {int(grant_id)}",
                actor=approver)
            done = await conn.execute(
                "UPDATE nac_connector_grants SET approved_at=now(), approved_by=$2, "
                "stepup_at=now(), signing_key_id=$3, signed_terms=$4::jsonb, signature=$5 "
                "WHERE id=$1 AND pairing='matched' AND approved_at IS NULL AND revoked_at IS NULL",
                int(grant_id), approver, key_id, json.dumps(terms), sig)
            if not _rows_affected(done):
                raise ConnectorError(409, "this pairing was declined or replaced while it was "
                                          "being approved; nothing was changed")
            ticket_id = await conn.fetchval(
                "SELECT ticket_id FROM nac_connector_grants WHERE id=$1", int(grant_id))
    await replaced.after_commit()
    await _close_ticket(pool, ticket_id, "approved", grant_id)
    # Both after the commit that changed the grant, so each happens exactly
    # once: the event for a schedule_task wake_on, and the person's
    # answer told to the Host in the conversation that asked.
    await host_events.emit(pool, eid, "connector_approved",
                           {"grant_id": grant_id, "app_id": app_id, "platform": platform})
    out = {"grant_id": int(grant_id), "approved": True, "app_id": app_id,
           "platform": platform, "account_id": account_id, "stepped_up": True,
           "approved_by": approver, "replaced": replaced.grant_ids}
    out.update(await _tell_host(pool, eid, int(grant_id), "approved", note))
    return out


async def decline_connector(pool, grant_id: int, *, actor: str = "user",
                            eid: Optional[int] = None, note: str = "") -> dict:
    """Decline a draft (an open or matched pairing): free, it only tightens. The
    app's capability is not touched: a live grant of the same app keeps working,
    and a pairing started again uses the capability the app already holds.

    The one call that declines it tells the Host (_tell_host), from any surface:
    Network's Decline / Don't pair, the card in the chat, the Tickets pane's
    dismiss."""
    if pool is None:
        raise ConnectorError(503, "brain pool not ready")
    row = await pool.fetchrow(
        "UPDATE nac_connector_grants SET "
        "  pairing = CASE WHEN pairing='open' THEN 'void' ELSE pairing END, "
        "  code_hash=NULL, revoked_at=now(), revoked_by=$2, revoke_reason='declined' "
        "WHERE id=$1 AND approved_at IS NULL AND revoked_at IS NULL "
        "RETURNING id, ticket_id, app_id", int(grant_id), str(actor or "user"))
    if row is None:
        live = await pool.fetchval(
            "SELECT approved_at IS NOT NULL AND revoked_at IS NULL FROM nac_connector_grants "
            "WHERE id=$1", int(grant_id))
        if live:
            raise ConnectorError(409, "this grant is live: revoke it instead")
        raise ConnectorError(404, "no such pairing")
    _codes.pop(int(grant_id), None)
    await _close_ticket(pool, row["ticket_id"], "declined", grant_id)
    # Both after the update that declined it, each exactly once (as above).
    await host_events.emit(pool, None, "connector_declined",
                           {"grant_id": grant_id, "app_id": row["app_id"], "why": "declined"})
    out = {"grant_id": int(grant_id), "declined": True}
    out.update(await _tell_host(pool, eid, int(grant_id), "declined", note))
    return out


async def decide_from_ticket(pool, eid: int, ticket_id: int, md: dict, *, option_id: str,
                             stepup_code: Optional[str], actor: str, note: str = "") -> dict:
    """The approval ticket's decide (tickets.decide_ticket_service hands a
    connector_pairing ticket here). Honoured only for the ticket the grant
    itself names, so a ticket written to look like this one decides nothing.
    The Host is told by approve/decline themselves (_tell_host), the same as
    from Network; answer_turn_started is lifted for the card to follow."""
    if actor != "user":
        raise ConnectorError(403, "this card is the person's to answer")
    gid = (md.get("connector_pairing") or {}).get("grant_id") if isinstance(md, dict) else None
    try:
        gid = int(gid)
    except (TypeError, ValueError):
        raise ConnectorError(400, "this card names no pairing") from None
    named = await pool.fetchval("SELECT ticket_id FROM nac_connector_grants WHERE id=$1", gid)
    if named is None or int(named) != int(ticket_id):
        raise ConnectorError(409, "this card does not belong to that pairing")
    if option_id == APPROVE_OPTION:
        res = await approve_connector(pool, eid, gid, stepup_code=stepup_code, via="ui",
                                      note=note)
    elif option_id == DECLINE_OPTION:
        res = await decline_connector(pool, gid, actor="user", eid=eid, note=note)
    else:
        raise ConnectorError(400, f"option_id must be {APPROVE_OPTION} or {DECLINE_OPTION}")
    out = {"ticket_id": int(ticket_id),
           "decision": {"option_id": option_id,
                        "option_label": "Let this account in" if option_id == APPROVE_OPTION
                        else "Don't pair", "and_similar": False, "note": ""},
           "pattern_id": None, "connector": res}
    if "answer_turn_started" in res:
        out["answer_turn_started"] = res["answer_turn_started"]
    return out


# ── 5. revoke — THE one path ───────────────────────────────────────────────

@dataclass
class Revoked:
    """What revoke_connector took away. When it ran inside a caller's
    transaction (conn=...), the caller MUST await after_commit() once that
    transaction commits: that is where the turn-cancel hook runs. Run on its
    own, revoke_connector has already called it."""
    app_id: str
    grant_ids: list = field(default_factory=list)
    drafts: list = field(default_factory=list)
    cap_dropped: bool = False
    reason: str = ""
    tickets: list = field(default_factory=list)
    pool: Any = None
    done: bool = False

    async def after_commit(self) -> None:
        if self.done:
            return
        self.done = True
        for gid in self.drafts:
            _codes.pop(int(gid), None)
        if self.cap_dropped:
            _pending.pop(self.app_id, None)
        for gid, tid in self.tickets:
            await _close_ticket(self.pool, tid, "revoked", gid)
        for gid in self.grant_ids:
            await cancel_connector_turns(app_id=self.app_id, grant_id=gid, reason=self.reason)
        if self.cap_dropped and not self.grant_ids:
            # A revoke before any grant was approved (a pairing voided): no
            # turn ran, but the capability that held a Discord gateway open
            # (routes/connector.py connector_gateway, open while pairing) is
            # gone, so the hook runs for the app with no grant.
            await cancel_connector_turns(app_id=self.app_id, grant_id=None, reason=self.reason)
        # After the commit, like the turn cancel: a Host waiting on this
        # connector wakes (server/host_events.py).
        for gid in self.grant_ids:
            await host_events.emit(self.pool, None, "connector_revoked",
                                   {"grant_id": gid, "app_id": self.app_id})
        for gid in self.drafts:
            await host_events.emit(self.pool, None, "connector_declined",
                                   {"grant_id": gid, "app_id": self.app_id, "why": "revoked"})


async def revoke_connector(pool, *, app_id: Optional[str] = None, grant_id: Optional[int] = None,
                           reason: str, actor: str = "user", conn=None,
                           live_only: bool = False, keep_cap: bool = False) -> Revoked:
    """THE revoke. Every path calls this: the Network app's Revoke, the Host's
    revoke_nac_rule kind=connector, both app-uninstall paths, and a re-pair's
    approval (live_only, keep_cap, inside its transaction).

    Name the app, or one of its grants. It revokes the app's live grant and
    (unless live_only) voids every draft and pairing the app has, then clears
    the app's capability (unless keep_cap), so the next post is refused: the
    route reads all of this from the database on every request. The app itself
    keeps running: deleting it is the full stop.

    After the commit, cancel_connector_turns() runs for each revoked live grant
    — the hook where the connector route cancels the turn in flight. With
    conn=, this runs inside the caller's transaction and the caller awaits
    Revoked.after_commit() after its commit; otherwise it is called here."""
    if pool is None and conn is None:
        raise ConnectorError(503, "brain pool not ready")
    if conn is None:
        async with pool.acquire() as c:
            async with c.transaction():
                out = await _revoke_in(c, app_id=app_id, grant_id=grant_id, reason=reason,
                                       actor=actor, live_only=live_only, keep_cap=keep_cap)
        out.pool = pool
        await out.after_commit()
        return out
    out = await _revoke_in(conn, app_id=app_id, grant_id=grant_id, reason=reason, actor=actor,
                           live_only=live_only, keep_cap=keep_cap)
    out.pool = pool
    return out


async def _revoke_in(conn, *, app_id, grant_id, reason, actor, live_only, keep_cap) -> Revoked:
    if grant_id is not None:
        found = await conn.fetchval("SELECT app_id FROM nac_connector_grants WHERE id=$1",
                                    int(grant_id))
        if found is None:
            raise ConnectorError(404, "no such connector grant")
        if app_id is not None and found != app_id:
            raise ConnectorError(400, "that grant belongs to another app")
        app_id = found
    if not app_id:
        raise ConnectorError(400, "name the connector app or its grant")
    await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", "nac_connector:" + app_id)
    live = await conn.fetch(
        "UPDATE nac_connector_grants SET revoked_at=now(), revoked_by=$2, revoke_reason=$3 "
        "WHERE app_id=$1 AND approved_at IS NOT NULL AND revoked_at IS NULL "
        "RETURNING id, ticket_id", app_id, actor, reason)
    drafts = []
    if not live_only:
        drafts = await conn.fetch(
            "UPDATE nac_connector_grants SET "
            "  pairing = CASE WHEN pairing='open' THEN 'void' ELSE pairing END, "
            "  code_hash=NULL, revoked_at=now(), revoked_by=$2, revoke_reason=$3 "
            "WHERE app_id=$1 AND approved_at IS NULL AND revoked_at IS NULL "
            "RETURNING id, ticket_id", app_id, actor, reason)
    dropped = False
    if not keep_cap:
        dropped = await drop_connector_cap(None, app_id, conn=conn)
    return Revoked(app_id=app_id, grant_ids=[int(r["id"]) for r in live],
                   drafts=[int(r["id"]) for r in drafts], cap_dropped=dropped, reason=reason,
                   tickets=[(int(r["id"]), r["ticket_id"]) for r in drafts if r["ticket_id"]])


# ── the capability (4.10, as amended) ──────────────────────────────────────
#
# Minted when a pairing starts and the app holds no live capability. Only its
# SHA-256 is stored. The raw value travels once: in the `creds` of the app's
# next wake (harness_backend._wake_app), which the operator writes into the
# app's creds Secret as `connector-cap` on a COLD wake only and answers
# `creds_applied: true`; the pod reads it as env CSERIES_CONNECTOR_CAP. The kernel
# moves the pending hash to the live one only on that true; an absent field (an
# operator without the change) or false keeps it pending. The resident heartbeat
# calls _wake_app every few minutes and never mints: minting happens here, once
# per pairing start, and only when there is no live capability.

CAP_CREDS_KEY = "connector-cap"
APP_ID_HEADER = "X-CSeries-App-Id"
CAP_HEADER = "X-CSeries-Connector-Cap"

# app_id -> (raw capability, its hash) while it is on its way to the pod.
_pending: dict[str, tuple[str, str]] = {}
_deliveries: dict[str, asyncio.Task] = {}
# One per app: minting (ensure_connector_cap) and every wake that carries the
# capability (creds_wake_lock) run one at a time, so the value a wake sends is
# the one pending when it is sent, and two values are never in flight to the
# operator at once. Two in flight could be written in either order, and the
# kernel could switch to the one the pod did not start with.
_mint_locks: dict[str, asyncio.Lock] = {}
_wake_creds_locks: dict[str, asyncio.Lock] = {}


def _lock_in(locks: dict, app_id: str) -> asyncio.Lock:
    lk = locks.get(app_id)
    if lk is None:
        lk = locks[app_id] = asyncio.Lock()
    return lk


def creds_wake_lock(app_id: str) -> asyncio.Lock:
    """Held by harness_backend._wake_app from reading the pending capability
    to handling the operator's answer, for a wake that carries one."""
    return _lock_in(_wake_creds_locks, app_id)


def cap_hash(cap: str) -> str:
    return hashlib.sha256(("connector-cap:" + cap).encode("utf-8")).hexdigest()


async def issue_connector_cap(pool, app_id: str) -> str:
    """Mint a capability for `app_id` and store its hash as PENDING. Returns the
    raw value (kept in memory for delivery; the database never holds it)."""
    import secrets as _secrets
    raw = _secrets.token_urlsafe(32)
    h = cap_hash(raw)
    await pool.execute(
        "INSERT INTO nac_connector_caps (app_id, pending_hash, pending_at, updated_at) "
        "VALUES ($1, $2, now(), now()) "
        "ON CONFLICT (app_id) DO UPDATE SET pending_hash=EXCLUDED.pending_hash, "
        "  pending_at=now(), updated_at=now()", app_id, h)
    _pending[app_id] = (raw, h)
    return raw


async def ensure_connector_cap(pool, app_id: str) -> str:
    """At pairing start: 'live' when the app already holds a live capability
    (kept, no restart); 'delivering' when one minted earlier is still on its
    way (kept too, and its delivery started again if none is running);
    otherwise mint one and start its delivery. Returns the state the pairing
    card shows ('live' | 'delivering').

    Keeping a pending value matters: a second pairing started while the first
    one's cold wake is in flight used to mint a new value, so the operator
    wrote the older one into the pod while the kernel waited for the newer,
    and the connector never got a capability that opens anything."""
    async with _lock_in(_mint_locks, app_id):
        row = await pool.fetchrow(
            "SELECT cap_hash, pending_hash FROM nac_connector_caps WHERE app_id=$1", app_id)
        if row is not None and row["cap_hash"]:
            return "live"
        held = _pending.get(app_id)
        if not (row is not None and held is not None and row["pending_hash"]
                and _same(held[1], row["pending_hash"])):
            await issue_connector_cap(pool, app_id)
    start_delivery(app_id)
    return "delivering"


def pending_creds(app_id: str) -> Optional[tuple[dict, str]]:
    """({connector-cap: raw}, its hash) to send with this app's wake, or None."""
    p = _pending.get(app_id)
    if not p:
        return None
    return {CAP_CREDS_KEY: p[0]}, p[1]


async def on_wake_answer(app_id: str, sent_hash: str, applied: Any) -> bool:
    """The operator answered a wake that carried the pending capability. Switch
    to it only on creds_applied: true — an absent field is not true."""
    if applied is not True:
        return False
    from server import brain_service
    pool = brain_service.get_pool()
    if pool is None:
        return False
    done = await pool.execute(
        "UPDATE nac_connector_caps SET cap_hash=pending_hash, pending_hash=NULL, "
        "  pending_at=NULL, issued_at=now(), updated_at=now() "
        "WHERE app_id=$1 AND pending_hash=$2", app_id, sent_hash)
    if _rows_affected(done):
        cur = _pending.get(app_id)
        if cur and cur[1] == sent_hash:
            _pending.pop(app_id, None)
        log.info("connector %s: capability delivered and live", app_id)
        return True
    return False


def start_delivery(app_id: str) -> None:
    """Restart the app's pod so the next wake is COLD and carries the pending
    capability. In the background: a cold wake can take a minute or two, and the
    Host's tool call does not wait on it."""
    t = _deliveries.get(app_id)
    if t is not None and not t.done():
        return
    try:
        _deliveries[app_id] = asyncio.get_running_loop().create_task(_deliver(app_id))
    except RuntimeError:  # no loop (a bare import): the next cold wake delivers it
        pass


_DELIVERY_ROUNDS = 3


async def _deliver(app_id: str) -> None:
    """Release the pod and cold-wake it with the pending capability. Once more
    when a NEWER value was minted while this ran (a revoke and a new pairing
    in the meantime): the pod that came up holds the older one, and
    start_delivery does not start a second delivery while this one runs. The
    same value still pending afterwards is left for the next cold wake or
    pairing start, so an operator that never applies it is not restarted in a
    loop."""
    from server.applications import harness_backend as hb
    for _ in range(_DELIVERY_ROUNDS):
        p = _pending.get(app_id)
        if p is None:
            return
        try:
            # recycle_app keeps a resident app's hold; it releases only when
            # this kernel holds the pod's address, so a pod this kernel lost
            # track of (a kernel restart) is released directly.
            if not await hb.recycle_app(app_id):
                await hb._release_and_bill_app(app_id)
            await hb.ensure_awake_app(app_id)
        except Exception as e:  # noqa: BLE001 — stays pending; the next cold wake carries it
            log.warning("connector %s: capability delivery wake failed: %r", app_id, e)
            return
        now = _pending.get(app_id)
        if now is None or now[1] == p[1]:
            return


async def drop_connector_cap(pool, app_id: str, *, conn=None) -> bool:
    """Clear the app's capability, live and pending. The next post is refused."""
    q = ("UPDATE nac_connector_caps SET cap_hash=NULL, pending_hash=NULL, pending_at=NULL, "
         "updated_at=now() WHERE app_id=$1 AND (cap_hash IS NOT NULL OR pending_hash IS NOT NULL)")
    done = await (conn.execute(q, app_id) if conn is not None else pool.execute(q, app_id))
    if conn is None:
        _pending.pop(app_id, None)
    return bool(_rows_affected(done))


STAGE_PAIRING = "pairing"   # an open pairing: POST /api/connector/pair only
STAGE_ACTIVE = "active"     # an approved grant: the message routes
STAGE_MATCHED = "matched"   # a code matched, the person has not approved yet: /platform only


@dataclass(frozen=True)
class ConnectorCaller:
    """Who is posting, once check_connector_cap has said yes. `stages` holds
    'active' (an approved grant: the message routes) and/or 'pairing' (an open
    pairing: /api/connector/pair only). The account and platform of an active
    grant come from its SIGNED terms."""
    app_id: str
    stages: frozenset
    grant_id: Optional[int] = None
    platform: Optional[str] = None
    account_id: Optional[str] = None
    display_name: Optional[str] = None
    token_name: Optional[str] = None
    app_token_name: Optional[str] = None
    pairing_id: Optional[int] = None

    @property
    def stage(self) -> str:
        return STAGE_ACTIVE if STAGE_ACTIVE in self.stages else STAGE_PAIRING


async def _verify_pubkey(pool) -> Optional[bytes]:
    from server import brain_service
    from server.network import nac_stepup
    return await nac_stepup.entity_verify_pubkey(pool, brain_service._entity_id)


def _terms_verify(pubkey: Optional[bytes], terms: Any, sig: Any) -> Optional[dict]:
    if pubkey is None or not sig:
        return None
    if isinstance(terms, str):
        try:
            terms = json.loads(terms)
        except ValueError:
            return None
    if not isinstance(terms, dict):
        return None
    from server.network.nac_mgmt import _verify_grant
    return terms if _verify_grant(pubkey, terms, sig) else None


async def check_connector_cap(pool, app_id: Any, presented: Any) -> Optional[ConnectorCaller]:
    """Who this is, or None. A constant-time match of the presented capability's
    hash with the app's live hash, then the stages the app is in right now: an
    approved, unrevoked grant whose signature verifies ('active'), an open,
    unexpired pairing ('pairing'), a code that matched and waits for the
    person's approval ('matched': /platform only). The app must still be
    installed. Read from
    the database on every call. A failed read RAISES — the caller refuses."""
    if pool is None:
        raise ConnectorError(503, "brain pool not ready")
    app_id = str(app_id or "").strip()
    if not app_id or not isinstance(presented, str) or not presented:
        return None
    async with pool.acquire() as conn:
        want = await conn.fetchval(
            "SELECT cap_hash FROM nac_connector_caps WHERE app_id=$1", app_id)
        if not want or not _same(cap_hash(presented), want):
            return None
        installed = await conn.fetchval(
            "SELECT status FROM installed_applications WHERE id=$1", app_id)
        if installed != "active":
            return None
        live = await conn.fetchrow(
            "SELECT id, platform, account_id, display_name, token_name, app_token_name, "
            "       signed_terms, signature "
            "FROM nac_connector_grants "
            "WHERE app_id=$1 AND approved_at IS NOT NULL AND revoked_at IS NULL", app_id)
        pairing = await conn.fetchrow(
            "SELECT id, platform, token_name, app_token_name FROM nac_connector_grants "
            "WHERE app_id=$1 AND pairing='open' AND revoked_at IS NULL "
            "  AND code_expires_at > now()", app_id)
        # A code that matched and waits for the person's approval: the
        # connector still needs the platform (to say the code was accepted,
        # and to keep reading), and nothing else.
        matched = await conn.fetchrow(
            "SELECT id, platform, token_name, app_token_name FROM nac_connector_grants "
            "WHERE app_id=$1 AND pairing='matched' AND approved_at IS NULL "
            "  AND revoked_at IS NULL ORDER BY id DESC LIMIT 1", app_id)
    stages = set()
    terms = None
    if live is not None:
        terms = _terms_verify(await _verify_pubkey(pool), live["signed_terms"], live["signature"])
        if terms is not None and terms.get("kind") == "connector" and terms.get("app_id") == app_id:
            stages.add(STAGE_ACTIVE)
        else:
            log.warning("connector %s: grant %s fails its signature check — refused",
                        app_id, live["id"])
            terms = None
    if pairing is not None:
        stages.add(STAGE_PAIRING)
    if matched is not None:
        stages.add(STAGE_MATCHED)
    if not stages:
        return None
    # Before approval, the binding is the open pairing's, else the matched one's.
    pre = pairing if pairing is not None else matched
    # The network[] pin holds on every request, not only when the capability
    # was minted: a re-install can change the manifest while the pod keeps its
    # capability, and a connector that can also reach another host is refused.
    platform = (terms or {}).get("platform") or (pre["platform"] if pre else None)
    why = app_refusal(app_id, platform)
    if why:
        log.warning("connector %s: refused, the app is no longer a %s connector: %s",
                    app_id, platform, why)
        return None
    # The binding in force: the live grant's, else the pairing's (the
    # connector needs the bot token to receive the pairing code at all).
    bound = live if terms else pre
    return ConnectorCaller(
        app_id=app_id, stages=frozenset(stages),
        grant_id=int(live["id"]) if terms else None,
        platform=(terms or {}).get("platform") or (pre["platform"] if pre else None),
        account_id=(terms or {}).get("account_id"),
        display_name=live["display_name"] if terms else None,
        token_name=bound["token_name"] if bound is not None else None,
        app_token_name=bound["app_token_name"] if bound is not None else None,
        pairing_id=int(pairing["id"]) if pairing is not None else None)


# ── the vault binding: the platform call (4.1) ─────────────────────────────
#
# How an app pod used a vault secret before this (read 2026-10-04): it could
# not. The broker (server/vault/authz.use_credential) runs in the kernel and is
# reached by the Host's tools (keys_manage op=use), by POST
# /api/vault/secrets/{name}/use (an app token with write:vault), and by kernel
# code (reach-out's pinned http_call, the inference and media engines). A
# runtime pod holds no app token (manifest.RuntimeSpec: "There is NO app token
# in the environment") and its only lanes into the kernel are /source and
# /runtime-log (localhost_only._SELF_AUTH_SHAPES), and the app-egress sidecar
# splices TLS without reading it (sidecars/app-egress/egress.py). So the
# connector's binding is this call: the connector names a method and a path,
# and the kernel builds the request to the platform's API host, with the token
# the grant names, injected by the broker where the platform wants it.
#
# Bound three ways, none of them the connector's choice: the HOST is the
# platform's API host (PLATFORMS[..]['api_host']); the TOKEN is the one the
# grant (or, before approval, the open pairing) names, approved with it; the
# APP is the capability holder. Telegram's token goes into the URL path
# (authz path mode, which only this call turns on); Discord's and Slack's into
# the Authorization header. Redirects are never followed.

PLATFORM_CALL_TIMEOUT_MAX_S = 70          # a Telegram long poll is up to 50 s
_PATH_RE = re.compile(r"^/[A-Za-z0-9._~/@:+-]*$")
_SLACK_APP_TOKEN_METHODS = frozenset({"/api/apps.connections.open"})


def build_platform_request(caller: ConnectorCaller, body: dict) -> tuple[str, dict]:
    """(the vault name to open, the broker request) for a connector's platform
    call, or ConnectorError(400). Pure: the caller performs it."""
    platform = caller.platform
    spec = PLATFORMS.get(platform or "")
    if spec is None:
        raise ConnectorError(400, "this connector names no platform")
    method = str(body.get("method") or "POST").upper()
    if method not in ("GET", "POST"):
        raise ConnectorError(400, "method must be GET or POST")
    path = str(body.get("path") or "")
    if not _PATH_RE.match(path) or ".." in path or "//" in path:
        raise ConnectorError(400, "path must be an API path like /sendMessage, with no query")
    use = str(body.get("use") or "bot")
    if use == "app":
        if platform != "slack" or path not in _SLACK_APP_TOKEN_METHODS:
            raise ConnectorError(400, "the app-level token is Slack's, for "
                                      "/api/apps.connections.open only")
        name = caller.app_token_name
    elif use == "bot":
        name = caller.token_name
    else:
        raise ConnectorError(400, "use must be bot or app")
    if not name:
        raise ConnectorError(409, "this connector's grant names no token in Keys for that")
    try:
        timeout = float(body.get("timeout") or 30)
    except (TypeError, ValueError):
        raise ConnectorError(400, "timeout must be a number of seconds") from None
    timeout = max(1.0, min(timeout, float(PLATFORM_CALL_TIMEOUT_MAX_S)))
    from urllib.parse import urlencode
    query = body.get("query")
    if query is not None and not isinstance(query, dict):
        raise ConnectorError(400, "query must be an object")
    qs = ("?" + urlencode({str(k): str(v) for k, v in query.items()})) if query else ""
    base = "https://" + spec["api_host"]
    req: dict = {"method": method, "timeout": timeout, "allow_redirects": False}
    if spec["token_in"] == "path":
        # The placeholder sits in the PATH only; the broker substitutes it
        # there and refuses a request that names it anywhere else.
        if not path.startswith("/") or path.startswith("/file/"):
            raise ConnectorError(400, "file downloads are not brokered yet")
        req["url"] = base + spec["path_prefix"].rstrip("/") + path + qs
        req["auth_scheme"] = "path"
    else:
        if not path.startswith("/api/"):
            raise ConnectorError(400, f"{spec['label']} API paths start with /api/")
        req["url"] = base + path + qs
        req["auth_scheme"] = spec["auth_scheme"]
        if spec.get("auth_prefix"):
            req["auth_header"] = "Authorization"
            req["auth_prefix"] = spec["auth_prefix"]
    files = body.get("files")
    if files is not None:
        # A file upload (Telegram sendPhoto / sendDocument / sendMediaGroup,
        # Discord's files[n]): multipart/form-data, built HERE from the
        # connector's fields and files, so the request still goes only to the
        # platform's API host with only the grant's token (path or header, as
        # above), and nothing in the body can name the token.
        if body.get("json") is not None:
            raise ConnectorError(400, "send files with form fields, not json (multipart)")
        form = body.get("form") if body.get("form") is not None else {}
        if not isinstance(form, dict):
            raise ConnectorError(400, "form must be an object")
        payload, ctype = build_multipart(form, files)
        req["body"] = payload
        req["headers"] = {"Content-Type": ctype}
    elif body.get("json") is not None:
        if not isinstance(body["json"], (dict, list)):
            raise ConnectorError(400, "json must be an object or a list")
        req["json"] = body["json"]
    elif body.get("form") is not None:
        if not isinstance(body["form"], dict):
            raise ConnectorError(400, "form must be an object")
        req["body"] = {str(k): str(v) for k, v in body["form"].items()}
    return name, req


# File uploads through the platform call. Telegram, read 2026-10-04 at
# https://core.telegram.org/bots/api#sending-files: "Post the file using
# multipart/form-data in the usual way that files are uploaded via the
# browser. 10 MB max size for photos, 50 MB for other files"; sendPhoto's file
# field is `photo` ("The photo must be at most 10 MB in size"), sendDocument's
# is `document`, and sendMediaGroup takes 2-10 items, each naming its file as
# "attach://<file_attach_name>". What a connector uploads is what the reply
# carries: a picture the renderer drew (at most connectors/picture.MAX_PNG_BYTES,
# 8 MiB) or a generated image. So one file is bounded at Telegram's photo
# limit, a request at ten files (a media group) and 12 MiB in all: the kernel
# holds a request whole while it builds it, and nothing a reply carries is
# larger. A document past that is not something this route sends.
PLATFORM_FILE_MAX = 10 * 1024 * 1024
PLATFORM_FILES_MAX = 10
PLATFORM_FILES_TOTAL_MAX = 12 * 1024 * 1024
# What a field name, a file name and a media type may be: no quote, no CR or
# LF, nothing a Content-Disposition line would need escaped. Discord's file
# fields are files[0]..files[9].
_FIELD_RE = re.compile(r"^[A-Za-z0-9_\[\].-]{1,64}$")
_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MIME_RE = re.compile(r"^[a-z]+/[a-z0-9.+-]{1,64}$")


def build_multipart(form: dict, files: Any) -> tuple[bytes, str]:
    """(the multipart/form-data body, its Content-Type) for the connector's
    form fields and files. Each file is {field, filename, data_b64, mime?}.
    RFC 7578: one part per field, then one per file, each with a
    Content-Disposition naming it; the boundary is random and appears in no
    value. Refuses (400/413) anything outside the bounds above."""
    import base64 as _b64
    import binascii
    import secrets as _secrets
    if not isinstance(files, list) or not files:
        raise ConnectorError(400, "files must be a list of {field, filename, data_b64, mime}")
    if len(files) > PLATFORM_FILES_MAX:
        raise ConnectorError(413, f"at most {PLATFORM_FILES_MAX} files in one call")
    fields: list[tuple[str, bytes]] = []
    for k, v in form.items():
        k = str(k)
        if not _FIELD_RE.match(k):
            raise ConnectorError(400, f"form field name {k[:40]!r} is not allowed")
        if isinstance(v, (dict, list)):
            v = json.dumps(v)          # Telegram's JSON-serialized fields (media, reply_markup)
        fields.append((k, str(v).encode("utf-8")))
    blobs: list[tuple[str, str, str, bytes]] = []
    total = 0
    for f in files:
        if not isinstance(f, dict):
            raise ConnectorError(400, "each file is {field, filename, data_b64, mime}")
        field_name = str(f.get("field") or "")
        filename = str(f.get("filename") or "")
        mime = str(f.get("mime") or "application/octet-stream").lower()
        if not _FIELD_RE.match(field_name):
            raise ConnectorError(400, "a file's field must be the platform's field name, "
                                      "like photo or document")
        if not _FILENAME_RE.match(filename):
            raise ConnectorError(400, "a file's filename must be letters, digits, dot, dash or "
                                      "underscore, like card.png")
        if not _MIME_RE.match(mime):
            raise ConnectorError(400, "a file's mime must be a media type, like image/png")
        raw = f.get("data_b64")
        if not isinstance(raw, str) or len(raw) > (PLATFORM_FILE_MAX * 4) // 3 + 8:
            raise ConnectorError(413, f"a file is at most {PLATFORM_FILE_MAX} bytes")
        try:
            data = _b64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError):
            raise ConnectorError(400, "a file's data_b64 is not base64") from None
        if not data or len(data) > PLATFORM_FILE_MAX:
            raise ConnectorError(413, f"a file is 1 to {PLATFORM_FILE_MAX} bytes")
        total += len(data)
        if total > PLATFORM_FILES_TOTAL_MAX:
            raise ConnectorError(413, f"the files in one call are at most "
                                      f"{PLATFORM_FILES_TOTAL_MAX} bytes in all")
        blobs.append((field_name, filename, mime, data))
    while True:
        boundary = "cseries-" + _secrets.token_hex(16)
        mark = boundary.encode("ascii")
        if not any(mark in v for _, v in fields) and not any(mark in b[3] for b in blobs):
            break
    crlf = b"\r\n"
    out = bytearray()
    for k, v in fields:
        out += b"--" + mark + crlf
        out += f'Content-Disposition: form-data; name="{k}"'.encode("ascii") + crlf + crlf
        out += v + crlf
    for field_name, filename, mime, data in blobs:
        out += b"--" + mark + crlf
        out += (f'Content-Disposition: form-data; name="{field_name}"; '
                f'filename="{filename}"').encode("ascii") + crlf
        out += f"Content-Type: {mime}".encode("ascii") + crlf + crlf
        out += data + crlf
    out += b"--" + mark + b"--" + crlf
    return bytes(out), f"multipart/form-data; boundary={boundary}"


RETRY_AFTER_MAX_S = 3600.0


def retry_after_s(answer: dict) -> Optional[float]:
    """How long the platform asked the caller to wait, in seconds, or None.
    Its Retry-After header first (RFC 9110 10.2.3: seconds or an HTTP date;
    Slack sends it with HTTP 429, https://docs.slack.dev/apis/web-api/rate-limits),
    else the body's own field: Discord's 429 body `retry_after`, "The number
    of seconds to wait before submitting another request"
    (https://docs.discord.com/developers/topics/rate-limits), and Telegram's
    `parameters.retry_after` (ResponseParameters, https://core.telegram.org/bots/api).
    Bounded to an hour: a wait past that is a platform's mistake to try
    again after, not one to sleep through."""
    import email.utils
    raw = answer.get("retry_after_header")
    secs: Optional[float] = None
    if isinstance(raw, str) and raw:
        try:
            secs = float(raw)
        except ValueError:
            try:
                secs = email.utils.parsedate_to_datetime(raw).timestamp() - time.time()
            except (TypeError, ValueError, IndexError, OverflowError):
                secs = None
    if secs is None:
        js = answer.get("json")
        if isinstance(js, dict):
            params = js.get("parameters") if isinstance(js.get("parameters"), dict) else {}
            for v in (js.get("retry_after"), params.get("retry_after")):
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    secs = float(v)
                    break
    if secs is None or secs != secs:     # NaN
        return None
    return max(0.0, min(secs, RETRY_AFTER_MAX_S))


async def platform_call(pool, caller: ConnectorCaller, body: dict) -> dict:
    """Perform the connector's platform call through the vault broker. Returns
    the broker's {status, body, json, truncated}, and `retry_after` (seconds)
    when the platform asked the caller to wait (retry_after_s); the token
    never comes back."""
    from server import brain_service
    from server.vault import authz
    name, req = build_platform_request(caller, body)
    try:
        out = dict(await authz.use_credential(
            pool, entity_id=int(brain_service._entity_id), name=name, action="http_call",
            request=req, actor=f"connector:{caller.app_id}",
            path_secret=(req.get("auth_scheme") == "path")))
        wait = retry_after_s(out)
        out.pop("retry_after_header", None)
        if wait is not None:
            out["retry_after"] = wait
        return out
    except authz.VaultAuthzError as e:
        raise ConnectorError(502, f"the platform call was refused: {e}") from None
    except Exception as e:  # noqa: BLE001 — a transport failure, named by its type only
        # The header lane (Discord, Slack) lets aiohttp's own errors through,
        # and those carry the URL and the upstream's words. The connector gets
        # the type, never the message, and the kernel answers 502 rather than
        # an unhandled 500 with a traceback in its log. "did not go through",
        # not "failed": the connector tries a 502 again and logs this detail
        # on that line, which the app proof must not read as an error.
        raise ConnectorError(502, f"the call to {PLATFORMS[caller.platform]['api_host']} "
                                  f"did not go through ({type(e).__name__})") from None


# ── what nac_status shows the Host ─────────────────────────────────────────

async def status_rows(pool) -> list:
    """Connector grants for nac_status: no code, no hash, no signature."""
    try:
        return (await list_connectors(pool))["connectors"]
    except Exception as e:  # noqa: BLE001 — one cold table never sinks the snapshot
        log.info("connector status unavailable: %r", e)
        return []
