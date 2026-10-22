"""mastercode.runtime.kernel_ops — the generic host→kernel-route surface
(software-for-the-AI).

Every platform capability is a kernel /api route; without this the host could
reach one only if someone hand-wrote a meta-tool for it, so every new app
would ship human-only by default. `kernel_op` is the ONE bridge that ends the
hand-tooling: it invokes a declared kernel route in-process, through the
route's own front door — never around it:

  - the call carries a REAL app token, minted with the kernel app-token
    secret (tokens.get_secret — the same bytes the middleware verifies
    against) and EXACTLY the one scope the route's `requires_permission`
    declares (app_id 'host' — attributable in every route-side audit).
    The route validates it exactly as it validates an app's token;
    in-route user gates (step-up, sign-only-by-user) are preserved by
    construction.
  - entity-side, the call must clear the same CEIGAS bit vocabulary as
    every meta-tool: the route's scope translates via ENTITY_SCOPE_TO_BITS
    and must be a subset of the entity's effective bits. A scope OUTSIDE
    the entity vocabulary (e.g. read:billing — the operator's domain)
    translates to 0 bits and is DENIED fail-closed: platform-domain
    surfaces stay structurally unreachable, which is the vocabulary's
    whole point (meta_tool_caps.py module docstring).
  - exposure is deny-by-default: only routes under _EXPOSED_PREFIXES are
    callable, and _WALLED_PATHS carves out routes that must never be
    (credential-byte reveals). Widening exposure is a reviewed one-line
    diff here, ratcheted by the parity ledger.

The generator (scripts/generate_capabilities.py) imports the two sets below
so the parity ledger's kernel_op coverage is DERIVED from this file — the
shown surface and the enforced surface cannot drift.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from mastercode.runtime.tool_call_ledger import refused as _refused, refused_if_caller as _refused_if_caller

log = logging.getLogger("cseries.mastercode.kernel_ops")

# ── Exposure (deny-by-default) ──────────────────────────────────────────────
# The first measured parity gap: appearance (the wallpaper lane) + the Sites
# customer stack.
_EXPOSED_PREFIXES: tuple[str, ...] = (
    "/api/appearance",
    # The user's OWN platform-backup schedule + saved second
    # destination (GET/PUT /api/backup/settings). Entity-domain state
    # like appearance — "back me up weekly at 3am" is a sentence to the Host,
    # not a form. /api/backup/local (read:backup_list, the bastion's copy) is
    # deliberately NOT under this prefix: that scope stays operator-domain.
    "/api/backup/settings",
    # Mirror STATUS — is the user's memory mirrored onto
    # their own machine, and how far behind is it. Same entity-domain scopes as
    # /settings above (read/write:backup_settings), so the bits wall treats it
    # identically. "Is my memory safe on my own hardware?" is a question the
    # Host should be able to answer out loud; that is the entire reason this
    # surface exists. Enabling one still costs a TOTP step-up inside
    # mirror.enable — exposure here does not weaken that, because the step-up
    # is an in-route user gate, which kernel_op preserves by construction.
    # /api/backup/local remains absent from this list on purpose.
    "/api/backup/mirror",
    # The Host's OWN backup runs (server/host_backup.py). Same
    # entity-domain scopes as /settings (read/write:backup_settings). "Did my
    # backup run last night" needs a true answer, and "back me up now" is a
    # deterministic act with nothing to approve.
    "/api/backup/runs",
    # Exports waiting for the owner (GET /api/backup/exports,
    # server/host_export.py pending): which ones, when asked for, and each
    # key's fingerprint. A read, so the Host can tell the owner one is waiting
    # and where to confirm it. Confirming builds a full copy of the Host and is
    # the owner's act alone: /api/backup/exports/confirm is in _WALLED_PATHS,
    # which wins over this prefix.
    "/api/backup/exports",
    "/api/applications/sites/customers",
    "/api/applications/sites/stripe",
    "/api/applications/sites/db",
    "/api/applications/sites/exposure",
    # "is there anything of mine on a site running
    # this app" and "bring it over". Entity-domain and firmly the user's own
    # business — the rows in question are theirs, made by them, on a public site
    # they used — so "did my notes come across from the demo site" is a question
    # the Host should answer out loud and a move it can drive. Gated
    # execute:install (bit 16), the install flow's own scope, so the bits wall
    # treats it exactly like installing the app it belongs to.
    #
    # Reach is bounded inside the routes and NOT by this list: the controller
    # decides identity from the platform's records and refuses any move where
    # the Host's owner is not the site member, so the widest thing the Host can
    # cause here is the owner's own rows arriving in the owner's own Host. The
    # kernel never names a subject; it cannot.
    "/api/applications/sites/data",
    # The apps' kept data on this machine (the local runtime): GET lists
    # each app's data volume, installed or removed, with its size and the disk's
    # floors. A read, so the Host can tell the person what data removed apps
    # left behind and what it takes. Gated execute:install (bit 16), the install
    # flow's own scope. Deleting a removed app's data is permanent and the
    # person's act alone: .../app-data/{app_id} is in _WALLED_PATHS, which wins
    # over this prefix.
    "/api/applications/runtime/app-data",
    # The peer app lane: an installed app's peers, grants, sends, outbox and inbox on the direct
    # host<->host lane. Entity-domain like circles (read:peer_app at the
    # READ_MEMORY bit, write:peer_app at WRITE_MEMORY), so "did the veterans
    # app hear back from host 122" is a question the Host answers out loud,
    # and a send it can drive. Reach is still bounded inside the routes: a
    # grant needs an accepted contact and a send needs the peer's grant.
    #
    # kernel_op mints app_id "host", so the routes let the HOST name the
    # installed app it drives (peer_apps._app_id) — a deliberate widening:
    # the host may act for an installed app only for the
    # half that app DECLARED — read:peer_app for the four reads, write:peer_app
    # for grant and send — so a receive-only app is never made to send. The
    # id 'host' itself is reserved (manifest.RESERVED_APP_IDS). /grant is NOT walled, on
    # purpose and classified rather than left silent: it causes the bridge to
    # mint a peer-facing slip for that app, which is the same class of act as
    # the host accepting a contact through contacts_manage — that mints a
    # dm:contact slip (server/routes/contacts.py, contract.issue) and the host
    # has always been allowed to do it. The slip's reach is one app on one
    # accepted contact's host, and it is revocable like every contract.
    "/api/peer/app",
    # Groups: a roster and a signed vote. Entity-domain like circles and the peer app
    # lane — read:group at the READ_MEMORY bit, write:group at WRITE_MEMORY —
    # so "who is in the builders group and how did the naming vote land" is a
    # question the Host answers out loud, and a roster it can drive.
    #
    # CLASSIFIED, not left silent, because two of these routes are acts and not
    # reads. /members mints a bridge-signed group slip for the invitee, and
    # /app-grant mints one per member: the same class of act as accepting a
    # contact through contacts_manage (which mints a dm:contact slip) and as
    # /api/peer/app/grant, both of which the host has always been allowed to
    # do. Reach stays bounded inside the routes — an invite is refused unless
    # the invitee is already an accepted contact, and every mutation is refused
    # unless this host OWNS the group (server/groups/roster.py _require_owner),
    # so the host can never edit a roster it merely replicates.
    #
    # The VOTE route is deliberately exposed too. A vote here is advisory by
    # construction — nothing in the group layer executes on a count — and a
    # host that can read
    # a tally but not cast its user's vote would be a spectator in its own
    # group. What it signs with is this host's own entity_sign key, which it
    # already signs NAC policy and pairing grants with.
    "/api/groups",
    # The Host's own crash artifacts. GET lists
    # name/size/mtime, DELETE removes one — read:files / write:files, both
    # already in the entity vocabulary. There is deliberately NO content route
    # to expose: a core dump is the user's process memory and the platform must
    # never carry it. Exposing the prefix is what lets the Host tell its user
    # "you have a 1.2 GB dump from claude-code, keep it or delete it" without
    # anything platform-side ever touching the bytes.
    "/api/system/crash",
    # The host's own processes. GET /api/system/processes says what
    # is running, what each process IS, what it costs and how long it has been
    # idle; POST .../terminate stops one. read:processes (bit 1) and
    # write:processes (bit 16) are both entity-domain — this is the host's own
    # pod and nothing else's.
    #
    # This is the point of the surface: a host that can see 400 MB going to a
    # sandbox nobody has touched in an hour, and can say so and offer to close
    # it, is managing its own machine. One that can only read the number back
    # is not. The load-bearing processes (pid 1, the kernel itself, anything in
    # the database container) refuse termination inside the route with a reason
    # rather than relying on this list to withhold the capability.
    "/api/system/processes",
    # The host's own oversight ledger. GET
    # /api/oversight/findings (filtered by severity, machine, open/resolved) and
    # GET /api/oversight/machines — read:oversight, bit 1. The host WRITES
    # this ledger every five minutes; without the read, "how long has this
    # been wrong" — the one question the ledger exists to answer — would be
    # answerable in the Oversight window and nowhere the host could see.
    #
    # WHAT IT HANDS THE HOST, stated in full because "it is what get_network
    # already says" was not true: the finding rows WITH their evidence, and per
    # machine the posture fields, record holes, bastion token id, last-seen time
    # and the five-minute op's last error. Every one of those is data this host
    # already receives on the beat or writes itself — the rows are its own fold
    # of its own machines' heartbeats and records, in its own pod's database
    # (one entity per pod; the table has no entity column to cross) — so nothing
    # here reaches beyond this host. It is more of its own data in one read.
    #
    # The ONE mutation is not the host's through this lane: POST .../resolve is
    # walled below (by template, matched against the concrete path too), and
    # write:oversight has no entity bits. A same-origin browser frame holding
    # the user's session is a different lane with a different boundary (the
    # user's own session; see routes/applications.launch_app).
    "/api/oversight",
    # Ships (server/routes/ships.py): the host's own ship runs,
    # pipelines, machines and key names (read:ships, bit 1), and starting a run
    # (write:ships, bit 4; exposure is exact-path, so a run's templated stop
    # route is the person's Stop button, not the host's). A start builds one plan for the whole
    # ship and files it on the person's approval device: what the host can do
    # with it is ring the person, which is how a host starts a ship. Clearing a
    # machine's pinned encryption key is walled below.
    "/api/ships",
)

# Routes that must NEVER be host-callable regardless of exposure: anything
# that returns credential bytes. Keys posture (keys_manage): reveal is
# TOTP-gated, UI-only, and a host that can read a secret can leak it into a
# model context — structurally prevented, not instructed away.
_WALLED_PATHS: frozenset[str] = frozenset({
    "/api/applications/sites/db/reveal",
    # Mints the desktop SHELL's app token (read:session + read:inbox +
    # read:calendar). Same class as a credential reveal: it does not return a
    # secret the host could read, it returns a BEARER TOKEN the host could
    # spend — a host that can call it holds the shell's scopes and can read the
    # user's mail count and calendar through the front door, with the audit
    # trail reading as the shell. It is a browser-bootstrap route with no host
    # use whatsoever, so walling costs nothing. Walled rather than left
    # unreviewed deliberately: unreviewed means "awaiting classification", and
    # this one is classified.
    "/api/applications/shell-token",
    # The keys the user's backups are encrypted to (server/backup_key.py).
    # /register takes a bastion's key, /retire stops new backups going to one,
    # /recovery/adopt replaces the pinned recovery key: each decides who can
    # open the user's backups, so the classification is "never the host". The
    # schedule and target half (/api/backup/settings) stays exposed above.
    "/api/backup/key/register",
    "/api/backup/key/retire",
    "/api/backup/key/recovery/adopt",
    # The recovery key the owner made on this Host (its public half), and the
    # key to the owner's own bucket: who backups go to, and with what.
    "/api/backup/key/recovery/own",
    "/api/backup/storage-key",
    # The backups on this machine: what leaves the Host as a file (download,
    # export), what comes back to replace it (upload, restore), and the list
    # those are chosen from.
    "/api/backup/files",
    "/api/backup/files/{stamp}/download",
    "/api/backup/files/{stamp}/link",
    "/api/backup/files/download/{ticket}",
    "/api/backup/files/upload",
    "/api/backup/files/restore",
    "/api/backup/export",
    # Retired (410), and walled all the same.
    "/api/backup/key/generate",
    "/api/backup/key/escrow",
    "/api/backup/key/sealed",
    # Confirming an export builds a full copy of the Host for the key named:
    # the owner's act alone (server/host_export.py).
    "/api/backup/exports/confirm",
    # Restoring the Host from a backup (server/host_restore.py)
    # replaces everything on it, and /restore/key gets one backup's key sealed
    # to this Host. The user's act alone, behind their second factor; never
    # something the Host decides to do.
    "/api/backup/restore",
    "/api/backup/restore/key",
    "/api/backup/restore/fetch",
    "/api/backup/restore/sidecar",
    # Sending a copy of the user's backup to their
    # own destination spends their egress and writes into storage they pay for.
    # A button the user presses, not something an agent decides to do. The
    # nightly path runs the same code from the ship worker.
    "/api/backup/ship",
    # Registering a sovereign USER's recipient key. The row is who
    # every later seal-for-user goes to and the write is an upsert, so a host
    # that could call this could redirect the user's own sealed credentials to
    # a key of its choosing. /api/vault is not an exposed prefix today; walled
    # anyway so the classification survives any later exposure of the vault
    # surface. The route itself takes the user's TOTP step-up.
    "/api/vault/user-keys",
    # Marking an oversight finding resolved. It records
    # that a PERSON dealt with it — the one fact the ledger's deterministic fold
    # cannot derive — so a host that could stamp it could also quietly close
    # what it was told to watch. The rest of /api/oversight is exposed above for
    # reading. Keyed by the route's TEMPLATE, which is how the baked contract
    # names it; _walled_path_for matches a concrete path against it too, so
    # `/api/oversight/findings/42/resolve` is refused with this entry's reason
    # rather than falling through to "not an exposed kernel op".
    "/api/oversight/findings/{finding_id}/resolve",
    # Asking for changes on a Code Approval. It records the
    # person's review of the Host's own pull request, and the note goes to the
    # Host in the chat, so a host that could call it would be writing its own
    # review. It is the other half of Approve, which merges as the person who
    # clicks it and never as the Host. /api/workbench is
    # not an exposed prefix and write:review has no entity bits today; walled
    # anyway so the classification survives any later exposure, as
    # /api/vault/user-keys above is. Keyed by the route's TEMPLATE, like the
    # oversight entry.
    "/api/workbench/approvals/{number}/changes",
    # The Workbench's Index: a message the person typed
    # into the Index. It starts the person's own turn on the page the Host
    # picks, after one pick call on their chat model, so a host that could call
    # it would be writing the person's messages. Walled as /api/connector/
    # messages is, and for the same reason; /api/workbench is not an exposed
    # prefix, so this keeps the classification if it ever is.
    "/api/workbench/index/send",
    # Ships: forgetting a machine's pinned encryption key. The next
    # key the machine reports is trusted on first use and ship credentials are
    # sealed to it, so clearing the pin decides which machine is believed: the
    # person's act alone, like the backup keys above.
    "/api/ships/machines/{token_id}/pin",
    # Messaging connectors: server/routes/connector.py, a
    # connector app's own way in, opened only by the capability issued to that
    # app. /pair carries the one-time code that never reaches the model, and
    # /messages starts the person's turns, so none of the five is the Host's.
    # /api/connector is not an exposed prefix; walled anyway so the
    # classification survives any later exposure, as /api/vault/user-keys is.
    # The Host governs connectors through its NAC tools instead.
    # /gateway, the WebSocket through which the kernel holds a
    # Discord connector's gateway connection with the bot token in it.
    "/api/connector/pair",
    "/api/connector/gateway",
    "/api/connector/platform",
    "/api/connector/messages",
    "/api/connector/messages/sent",
    "/api/connector/turns/{turn_id}",
    # Deleting the data a removed app left on this machine. It cannot be undone,
    # so it is the person's Delete in the App Store; the list above it
    # (GET /api/applications/runtime/app-data) stays the Host's to read. Keyed
    # by the route's TEMPLATE, like the oversight entry.
    "/api/applications/runtime/app-data/{app_id}",
    # Signing in to an MCP server (server/routes/mcp_oauth.py): starting a
    # sign-in (which can also set the OAuth client the person entered), signing
    # out, and the authorization server's redirect that finishes one. Each
    # decides which account the Host acts as on that server, so all three are
    # the person's, on the server's card in the Network app. /api/mcp is not an
    # exposed prefix; walled anyway so the classification survives any later
    # exposure, as /api/vault/user-keys is.
    "/api/mcp/oauth/{name}/start",
    "/api/mcp/oauth/{name}/sign-out",
    "/api/mcp/oauth/callback",
    # Naming the Host (server/routes/host_name.py): the name the Host is called
    # by is the owner's choice, made in Settings, About. Only /api/system/crash
    # and /api/system/processes are exposed; walled anyway, like the MCP
    # sign-in routes, so the classification survives any later exposure of
    # /api/system.
    "/api/system/name",
})

_CONNECTOR_WALL = (
    "The /api/connector routes are a messaging connector app's own way in: only "
    "the capability issued to that app opens them, so you never call them. To "
    "connect a messaging account, use propose_nac_policy kind=connector; the person "
    "finishes the pairing in the Network app. nac_status lists connectors, and "
    "revoke_nac_rule kind=connector ends one.")

_BACKUP_FILES_WALL = (
    "The backup files on this machine are the owner's to take away or bring back: "
    "downloading one, making an export, uploading a file and restoring from it are their "
    "acts, in the Backups app (a restore also takes their code). You can read how the "
    "backups went with GET /api/backup/runs.")

_MCP_SIGNIN_WALL = (
    "Signing in to an MCP server, and signing out of one, are the person's own acts: "
    "they decide which account you use on that server. They are the Sign in and Sign "
    "out buttons on the server's card in the Network app, and the sign-in finishes in "
    "the person's browser. Tell them the server needs a sign-in and where to do it.")

# Why a walled path is walled, where it is NOT the credential-bytes reason the
# original entries share. run_kernel_op relays this, so the host tells the user
# something true and actionable instead of "it returns credential bytes".
_WALL_REASONS: dict[str, str] = {
    "/api/oversight/findings/{finding_id}/resolve": (
        "Marking a finding resolved is the user's own action — it records that a "
        "person dealt with it. It is the Mark resolved button on the finding in the "
        "Oversight app. You can read the finding itself with GET "
        "/api/oversight/findings."),
    "/api/workbench/approvals/{number}/changes": (
        "Asking for changes on a Code Approval is the user's own action — it is the "
        "Ask for changes button on the Code Approval in the Workbench, and their note "
        "reaches you in the chat."),
    "/api/workbench/index/send": (
        "Sending from the Workbench's Index is the person's own message: it starts "
        "their turn on the page you pick for it, so it is never yours to call."),
    "/api/ships/machines/{token_id}/pin": (
        "Clearing a machine's pinned key is the user's own action: it decides which "
        "machine is trusted with ship credentials. It is the Clear pin button on the "
        "machine in the Ships app. Tell them why you think the machine's key changed."),
    "/api/connector/pair": _CONNECTOR_WALL,
    "/api/connector/gateway": _CONNECTOR_WALL,
    "/api/connector/platform": _CONNECTOR_WALL,
    "/api/connector/messages": _CONNECTOR_WALL,
    "/api/connector/messages/sent": _CONNECTOR_WALL,
    "/api/connector/turns/{turn_id}": _CONNECTOR_WALL,
    "/api/backup/key/recovery/own": (
        "Choosing the recovery key every backup is encrypted to is the owner's own act: "
        "they make it in the Backups app, keep it, and confirm it with their code. You can "
        "read which key is in use with GET /api/backup/settings."),
    "/api/backup/storage-key": (
        "The key to the owner's own bucket is the owner's to give: they enter it in "
        "Settings, Backup, and it goes straight into the vault."),
    "/api/backup/files": _BACKUP_FILES_WALL,
    "/api/backup/files/{stamp}/download": _BACKUP_FILES_WALL,
    "/api/backup/files/{stamp}/link": _BACKUP_FILES_WALL,
    "/api/backup/files/download/{ticket}": _BACKUP_FILES_WALL,
    "/api/backup/files/upload": _BACKUP_FILES_WALL,
    "/api/backup/files/restore": _BACKUP_FILES_WALL,
    "/api/backup/export": _BACKUP_FILES_WALL,
    "/api/applications/runtime/app-data/{app_id}": (
        "Deleting the data a removed app left on this machine cannot be undone, so it is "
        "the person's own act: the Delete button under 'Data kept from removed apps' in "
        "the App Store. You can read what each app keeps, and how much, with GET "
        "/api/applications/runtime/app-data."),
    "/api/mcp/oauth/{name}/start": _MCP_SIGNIN_WALL,
    "/api/mcp/oauth/{name}/sign-out": _MCP_SIGNIN_WALL,
    "/api/mcp/oauth/callback": _MCP_SIGNIN_WALL,
    "/api/system/name": (
        "The name you are called by is the owner's to choose: they set it in Settings, "
        "About. If they ask you to change it, tell them where, and suggest a name if "
        "they want one."),
}

# Method-aware carve-out. _WALLED_PATHS is keyed by PATH, so it can only say
# "neither method" — and that is not always the right answer. A route pair where
# reading is exactly what the Host is for and writing is not needs this.
#
# /api/backup/mirror is the case that forced it. GET is the whole point
# of exposing the area: the Host should be able to answer "is my memory mirrored,
# and how far behind is it". PUT is not. Enabling a mirror already fails closed
# for the Host — it takes a TOTP code no agent can produce — but DISABLING takes
# none by design, because narrowing should never be gated. That asymmetry is
# right for the user and wrong for an agent: a manipulated Host could quietly
# switch off the mirror the user turned on with their 2FA, destroy the slot, and
# leave the next enable needing a full re-seed. Silent downgrade of a safety
# property is exactly what the bits wall exists to stop, so the write stays
# human-only.
_WALLED_OPS: frozenset[tuple[str, str]] = frozenset({
    ("PUT", "/api/backup/mirror"),
    ("DELETE", "/api/backup/mirror"),
})

_CAPS_JSON = Path("/app/docs/capabilities.json")

_contract_cache: dict | None = None


def _contract() -> dict:
    """The baked, version-exact route table (method,path → contract row).
    Missing doc (pre-capabilities image) → empty contract → every call
    denies: fail closed, never fail open."""
    global _contract_cache
    if _contract_cache is None:
        table: dict[tuple[str, str], dict] = {}
        try:
            data = json.loads(_CAPS_JSON.read_text())
            for r in (data.get("api_contract") or {}).get("routes", []):
                table[(r["method"], r["path"])] = r
        except Exception as e:
            log.warning("kernel_ops: capabilities.json unreadable (%s) — deny-all", e)
        _contract_cache = table
    return _contract_cache


def _template_rx(template: str) -> str:
    """A FastAPI path template as a full-match regex: `{param}` is one path
    segment. The same rule as server/applications/api_contract.template_to_regex,
    kept local so this module does not import the server package to load."""
    return "^" + re.sub(r"\\\{[^/]*?\\\}", r"[^/]+", re.escape(template)) + "$"


def _walled_path_for(path: str) -> str | None:
    """The _WALLED_PATHS entry that walls `path` — the path itself, or the route
    TEMPLATE a concrete path falls under (`.../findings/{finding_id}/resolve`
    walls `.../findings/42/resolve`). None when nothing does.

    Exposure is exact-match, so a concrete path is refused either way; what
    this decides is WHICH refusal the host reads. Without it a host that sent
    a real id got "not an exposed kernel op", and relayed that, instead of the
    wall's own reason."""
    if path in _WALLED_PATHS:
        return path
    for w in _WALLED_PATHS:
        if "{" in w and re.fullmatch(_template_rx(w), path):
            return w
    return None


def exposed_route(method: str, path: str) -> dict | None:
    """The contract row when (method, path) is exposed and not walled; else
    None. Exact-match only — parameterized route templates are not exposed
    in v1 (none of the exposed set carries a template)."""
    if _walled_path_for(path) is not None:
        return None
    if (method.upper(), path) in _WALLED_OPS:
        return None
    if not any(path == p or path.startswith(p + "/") for p in _EXPOSED_PREFIXES):
        return None
    return _contract().get((method.upper(), path))


async def run_kernel_op(args: dict) -> dict:
    """Execute one exposed kernel route in-process. Shapes a meta-tool
    result: {status, http_status?, result}."""
    a = args or {}
    method = str(a.get("method") or "GET").strip().upper()
    path = str(a.get("path") or "").strip()
    params = a.get("params") if isinstance(a.get("params"), dict) else None
    body = a.get("body") if isinstance(a.get("body"), dict) else None
    if not path.startswith("/api/"):
        return _refused("kernel_op `path` must be an /api route.")

    row = exposed_route(method, path)
    if row is None:
        walled = _walled_path_for(path)
        if walled is not None:
            return {"status": "denied",
                    "result": _WALL_REASONS.get(walled) or (
                        f"{path} is walled — it returns credential bytes; "
                        "reveals are user-only (Keys app, TOTP-gated).")}
        if (method, path) in _WALLED_OPS:
            # Say WHY and say what still works, so the host relays something the
            # user can act on instead of "denied".
            return {"status": "denied", "result": (
                f"{method} {path} is user-only. Reading it is not — call GET {path} "
                "for the current state. Changing it is the user's own action: it is "
                "in the Settings app, Backup tab, under 'Your memory mirror', where "
                "they pick the machine and confirm with their authenticator code. "
                "Tell them that; do not tell them to ask you.")}
        return {"status": "denied", "result": (
            f"{method} {path} is not an exposed kernel op. The exposed surface "
            "is listed in the capabilities doc (§7 host parity); anything else "
            "goes through your named tools (find_tools finds the one), run "
            "yourself. If none reaches it, say that it has no door yet."
        )}

    # ── entity-side bits wall (same vocabulary as every meta-tool) ──
    from mastercode.runtime.meta_tool_caps import (
        ENTITY_SCOPE_TO_BITS, _resolve_entity_scopes, scopes_to_bits,
    )
    from server import brain_service
    pool = brain_service.get_pool()
    if pool is None:
        return {"status": "error", "result": "Brain not ready."}
    entity_id = brain_service._entity_id

    scope = row.get("scope")
    if scope is None:
        required = 1 if method == "GET" else 4  # floor for unscoped routes
    else:
        required = ENTITY_SCOPE_TO_BITS.get(scope, 0)
        if required == 0:
            # Outside the entity vocabulary = platform domain. Structural deny.
            log.warning("KERNEL_OP_DENIED eid=%s %s %s scope=%s (platform-domain)",
                        entity_id, method, path, scope)
            return {"status": "denied", "result": (
                f"{path} requires scope {scope!r}, which is outside the "
                "host's capability vocabulary (platform domain) — "
                "structurally not host-callable."
            )}
    entity_bits = scopes_to_bits(await _resolve_entity_scopes(pool, entity_id))
    if (entity_bits & required) != required:
        log.warning("KERNEL_OP_DENIED eid=%s %s %s bits have=%08b need=%08b",
                    entity_id, method, path, entity_bits, required)
        return {"status": "denied", "result": (
            f"insufficient capability bits for {path} "
            f"(have {entity_bits:08b}, need {required:08b})."
        )}

    # ── mint the single-scope token + call the route's front door ──
    # server.main `app` (main.py:1078) is the live FastAPI instance; the
    # ASGI transport calls it in-process, so middleware + dependencies run
    # exactly as for an app-window request. It names itself localhost: the
    # in-process peer is loopback, and the localhost gate admits a loopback
    # request only under a localhost name (localhost_only.loopback_host_ok).
    try:
        from server.main import app as _asgi_app
        from server.applications.middleware import HEADER_NAME
        from server.applications.tokens import get_secret, mint
        secret = await get_secret(pool)
        token, _ = mint(secret, app_id="host",
                        permissions=[scope] if scope else [])
        import httpx
        transport = httpx.ASGITransport(app=_asgi_app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://localhost") as client:
            resp = await client.request(
                method, path, params=params, json=body,
                headers={HEADER_NAME: token}, timeout=30.0,
            )
    except Exception as e:
        log.error("kernel_op: %s %s failed: %s", method, path, e)
        return _refused_if_caller(e, f"kernel_op transport failure: {e}")

    try:
        payload = resp.json()
    except Exception:
        payload = (resp.text or "")[:4000]
    ok = 200 <= resp.status_code < 300
    log.info("KERNEL_OP eid=%s %s %s -> %d", entity_id, method, path, resp.status_code)
    out = payload if isinstance(payload, (dict, list, str)) else str(payload)
    if isinstance(out, (dict, list)):
        _s = json.dumps(out)
        if len(_s) > 20000:
            out = _s[:20000] + "…(truncated)"
    return {"status": "ok" if ok else "error",
            "http_status": resp.status_code, "result": out}
