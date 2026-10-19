"""
Permission catalog — every API surface an app can declare.

Format: "<verb>:<resource>".  Matches the convention already in use
across the 17 existing app.yaml files; this module makes it
enforced rather than advisory.

  read    — list / fetch / status
  write   — create / update / delete
  execute — run a side-effecting call that isn't a simple CRUD shape
            (inference, code run, security scan, websearch)

Adding a new permission is a deliberate decision: routes pick the
permission they require; an app that does not declare it cannot reach
the route. Apps that need new surface get the permission added here
AND in their app.yaml.

Wildcards are not allowed. Apps enumerate.
"""

from __future__ import annotations

from typing import Final


PERMISSIONS: Final[frozenset[str]] = frozenset({
    # Execute surfaces
    "execute:benchmark",
    "execute:code",
    "execute:inference",
    "execute:install",
    # Shipping the user's OWN completed Workbench project as an
    # app (POST /api/applications/build) — pack + entity-key sign + install a
    # self-authored, entitlement-free bundle. Deliberately NOT execute:install:
    # that scope also opens /install (any signed bundle), the four /store/*
    # money routes, /store/entitlement and DELETE /{app_id} (uninstall ANY
    # app), and Workbench needs none of them to ship its own project.
    # Why it exists: /build was gated on execute:install, Workbench's manifest
    # never declared it, and "Install as my app" 403'd for every user (entity
    # 128 — reproduced in-pod with a real window token). The host's
    # own ship path never hit this: it posts to /build with x-cseries-internal-auth
    # and is allow-listed in middleware._INTERNAL_LOOPBACK_PATHS, so only the
    # browser button was refused.
    "execute:ship_project",
    "execute:scan",
    "execute:websearch",
    # Browser agents: a task the user recorded once in the
    # Browser app and a machine replays — no model. read = list agents and
    # their runs; write = save/delete an agent (the Browser app's Create/Save
    # flow); execute = start a run or answer a paused one. An app that wants
    # "export my ledger" on its own trigger declares execute:agents and
    # nothing about the vault — the agent's sign-in is the vault's business.
    "read:agents",
    "write:agents",
    "execute:agents",

    # Benchmark (read-only surface for runs + learnings)
    "read:benchmark",

    # Billing
    "read:billing",
    "write:billing",

    # Community board (the members' discussion board — bridge community.* ops).
    # read = browse/search; write = post/reply/vote/flag under the member's
    # own bridge-pinned identity.
    "read:community",
    "write:community",

    # App publishing — operator's surface to push an app to the central
    # store. Distinct from write:billing (publishing isn't billing).
    "publish:apps",

    # Site hosting — the Sites app's surface to publish + control the
    # host's public web sites (served from the public webapp zone, never
    # from this pod). read:sites = list + analytics; publish:sites =
    # publish/unpublish/suspend/resume/restart.
    "read:sites",
    "publish:sites",

    # Backup
    "read:backup_list",
    "write:backup_restore",
    "write:backup_trigger",
    # The user's own platform-backup schedule and saved second destination
    # (GET/PUT /api/backup/settings, server/routes/backup.py). Distinct from
    # backup_list above, which is the read-only view of the bastion's
    # local-master copy.
    "read:backup_settings",
    "write:backup_settings",

    # Code search (per-user in-pod hybrid search over the entity's own projects)
    "read:codesearch",
    "write:codesearch",

    # Brain
    "read:brain_corrections",
    "read:brain_functions",
    "read:brain_memory",
    # Full-corpus memory read. read:brain_memory alone is audience-scoped — an app
    # sees only memories tagged for it (metadata.share_scope contains app:<id>).
    # read:brain_memory_all lifts that to the entire corpus. Privileged + first-party
    # only: it is deliberately NOT in build_contract.COMMUNITY_ALLOWED_SCOPES, so an
    # untrusted (community) app can never request it.
    "read:brain_memory_all",
    "read:brain_personality",
    "read:brain_projects",
    "read:brain_sessions",
    "read:brain_signals",
    "read:brain_state",
    "read:brain_tasks",
    "write:brain_corrections",
    "write:brain_memory",
    "write:brain_personality",
    "write:brain_tasks",

    # Clipboard
    "read:clipboard",
    "write:clipboard",

    # Files
    "read:files",
    "read:workspace_files",
    "write:workspace_files",
    "write:files",

    # Memory (user-visible memory facade, distinct from brain_memory)
    "read:memory",
    "write:memory",

    # The one index: a single surface for finding context, files and the
    # rest. POST /api/search runs the SAME federated recall
    # the host's own per-turn pre-fire runs (memory_retrieval.federated_recall:
    # memory + the user's documents), plus the project index, the journal and
    # the in-pod code index, each store reported honestly. One scope, so an app
    # declares one thing and calls one route instead of five. The person
    # searching is the owner, so no per-model visibility filter applies here
    # — the scopes that hide a memory from a
    # given MODEL were never about hiding it from the user. Deliberately NOT
    # in build_contract.COMMUNITY_ALLOWED_SCOPES: it reads the whole memory
    # corpus, the same posture as read:brain_memory_all.
    "read:search",

    # User context — the USER-OWNED corpus (user_context + the host's
    # entity_context content), and the per-host CONTEXT-STORAGE control surface
    # (which machine/bucket/bastion holds each piece, per-prefix routing,
    # single-vs-replicate availability). This is the Context app's control plane
    # and is DISTINCT from read:memory/write:memory (the host's internalized
    # memory). read:context = the where-it-lives index + placement + choosable
    # machines + embedded status; write:context = set/clear a route, set the
    # host-wide default, and trigger an embed. Backed by
    # server/routes/context_manager.py (reuses brain.mastercode.context_backends).
    "read:context",
    "write:context",

    # CEIGAS dispatch / security status. (write:incident_decision is RETIRED,
    # see RETIRED_PERMISSIONS below. read:incidents stays: it guards
    # /api/security/status and /history.)
    "read:incidents",

    # Inference
    "read:inference_catalog",
    "read:inference_log",
    "write:inference_routing",

    # (Removed: read:awareness + write:awareness — gated routes were
    # deleted but the scopes lingered in the registry. The live daily
    # awareness pipeline is unaffected; only
    # the kernel-side surface that exposed it ever used these scopes,
    # and that surface no longer exists.)

    # Entity appearance / wallpaper preferences
    "write:appearance",

    # Network + system
    "read:network",
    "write:network",
    # Network topology — aggregate read of the host's place in its sovereign
    # network: gateways (machines it reaches), peers/secured channels, and
    # bridge/peer/egress health. Drives the Network app's overview/gateways/peers
    # reads (server/routes/network.py). Read-only; mutations stay on write:network.
    "read:topology",
    # Network membership (server/routes/network_membership.py): whether this
    # Host is a member of the network, and joining or leaving it with a code
    # from the owner's account page. Settings declares both. Deliberately absent
    # from meta_tool_caps (no capability bit, so the Host's tools never hold
    # it) and from build_contract.COMMUNITY_ALLOWED_SCOPES: membership is the
    # owner's decision, and join and leave also need the owner's session.
    "read:network_membership",
    "write:network_membership",
    # The oversight ledger: the findings
    # the host folded from what its machines reported — one row per thing, with
    # a first_seen that never moves. `read` is the whole ledger; `write` is the
    # single mutation the routes expose, POST .../resolve, which is a person
    # saying they dealt with it — the one fact the deterministic fold can never
    # derive. Both are DELIBERATELY absent from
    # build_contract.COMMUNITY_ALLOWED_SCOPES: this names a customer's machines,
    # their firewall posture and what is firing on them, so an untrusted app
    # must not be able to request it at all — the same treatment
    # read:brain_memory_all gets, for the same reason.
    "read:oversight",
    "write:oversight",
    # Ships (server/routes/ships.py): the pipelines, the runs with each step's
    # signed receipt, the machines and the credential names each step takes.
    # `write` starts a run (which only files it on the person's approval
    # device), stops one, or clears a machine's pinned key. Both absent from
    # build_contract.COMMUNITY_ALLOWED_SCOPES, as the oversight pair is.
    "read:ships",
    "write:ships",
    "read:processes",
    # Stopping a process in the host's OWN pod. Separate from read:processes
    # because seeing what is running and ending it are different powers, and
    # the read is what every panel needs. Entity-domain execute, like
    # system.restart_app — never platform-wide.
    "write:processes",
    "read:system",
    "read:system_status",
    "read:update_state",
    "read:version",
    "write:update_request",

    # Policy. (write:policy_proposals is RETIRED, see RETIRED_PERMISSIONS.)
    "read:policy",

    # Portal sources
    "read:portal_sources",
    "write:portal_sources",
    # Use a data-source connection the HOST granted this app — POST
    # /api/integrations/connections/{id}/call, nothing else. The app names a
    # path, the host attaches the access token and makes the call; the token
    # never reaches the app.
    #
    # Holding this grants NOTHING on its own: the broker refuses unless a row in
    # integration_grants says the host gave THIS app THAT connection. That is
    # what makes it safe for a community app — the reach is a decision the user
    # made per account, revocable on its own, and not implied by installing.
    # Distinct from write:portal_sources, which CREATES and revokes connections
    # and must stay with the host.
    "use:integration",

    # The Host's own database: /api/world — SQL, describe, rows, snapshots — all run as
    # host_world, which writes nothing outside the schema `world` and reads no
    # secret. Deliberately NOT in build_contract.COMMUNITY_ALLOWED_SCOPES: it is
    # the Host's own data, for the Host's own apps and the Memory app.
    "use:world",

    # Projects
    "read:projects",
    "write:projects",

    # Screen / input
    "read:screen",
    "write:input",

    # Session
    "read:session",
    "write:session",

    # Signals
    "read:signals",

    # Circles — multi-party conversations (humans, entities, groups).
    # The Circles app also writes the domain_policy table that governs
    # which other containers are allowed to reach this one and what
    # actions they may invoke.
    "read:circles",
    "write:circles",
    "read:address_book",
    "write:address_book",
    # Ask a Host found on one of your machines to connect, and nothing else:
    # POST /api/contacts/request-peer takes it beside write:address_book
    # (requires_any_permission). The Network app's Request connection holds
    # it in place of write:address_book, which also opens contact delete,
    # block and accept.
    # Not a community-app scope: it reaches another identity.
    "request:peer",
    "read:domain_policy",
    "write:domain_policy",

    # Tickets — entity-private escalation queue. Circles UI reads
    # tickets to draw badges and the Reviews queue; the handler writes
    # decisions; the entity runtime writes new tickets when it
    # escalates.
    "read:tickets",
    "write:tickets",

    # Inbox — pod-local platform-update queue (permission grants,
    # contracts, software orders). Backed by inbox_messages table;
    # dual-written from the platform via bridge handlers.
    "read:inbox",
    "write:inbox",
    # Delivery ONLY — POST /api/inbox/deliver, nothing else. Deliberately
    # separate from write:inbox, which also carries /send and /archive: /send
    # takes an arbitrary from_address and message_kind, so an app holding it
    # could author something that reads as a platform notification or a
    # contract. Delivery can do none of that. The route pins message_kind to
    # 'app_delivery', pins the row to a self-send, and stamps the delivering
    # app id from the window token, so the worst an app can do with this is
    # put its OWN name on something in its installer's own inbox — which is
    # the thing the user installed it to do. That narrowness is what lets it
    # into COMMUNITY_ALLOWED_SCOPES, where write:inbox must never go.
    "write:inbox_delivery",

    # Calendar — pod-local event timeline (entity-created events,
    # daily summaries, scheduled tasks). Backed by calendar_events
    # table; dual-written from the platform via bridge handlers.
    "read:calendar",
    "write:calendar",

    # Reach-out — how the host gets the
    # user's attention when work finishes while they are away. read = the
    # channels, the triggers and the recent log (/api/reachout); write =
    # delete a channel or a trigger, and fire a test. There is deliberately no
    # create route: building a channel is a conversation with the host (the
    # `reachout` tool), because a settings surface is never a config form for
    # what the host does. The Circles pane that will hold these is not built
    # yet — the scopes land with the routes so the pane is a UI
    # change and nothing else.
    "read:reachout",
    "write:reachout",

    # Decision substrate — host proposes ambiguous picks,
    # user resolves via inbox, answer forwards as next-turn prompt.
    # write:decision — host posts a proposal (entity-runtime / host app).
    # resolve:decision — user submits a choice (inbox app or dashboard widget).
    # read:decision — anything that needs to render decision state.
    "write:decision",
    "resolve:decision",
    "read:decision",

    # Multi-harness review — fan_out / compare / propose_commit
    # all SPEND subscription quota (each harness call) and write to the
    # workspace. write:review is the gate. read:review lets the codespace
    # session-picker enumerate sessions without write authority.
    "read:review",
    "write:review",

    # Gitea propose — the host's one write lane to a repo it does
    # NOT own: fork under its own account, branch, one commit, pull request
    # upstream. The upstream never receives a push; the user or the upstream
    # owner merges. write:review gates the host's OWN repos (Workbench PR
    # merge, grant/revoke); this is a different power, so a different scope.
    "write:gitea",

    # cseries-security — platform security service. Apps holding these
    # scopes get to call /api/cseries-security/* which proxies mTLS to the
    # platform service container. The 503 gate from the proxy (missing
    # cert mount) is the second layer; this scope is the first.
    "read:cseries_security",
    "execute:cseries_security",

    # Peer app lane — the same app on
    # two hosts talking over the direct host<->host lane. read = this app's
    # inbox, outbox and peer grants, and ack; write = grant a peer's copy of
    # this app the right to call ours, and send. An app that declares neither
    # is unreachable from any peer: app.deliver refuses for an app without
    # read:peer_app (server/peer/app_lane.py), so consent at install is the
    # gate on the receiving side too.
    "read:peer_app",
    "write:peer_app",

    # Groups — a roster
    # and a signed vote. read = the groups this host owns or belongs to, their
    # rosters, ballots and tallies; write = create a group, add/remove members,
    # set roles, grant an app to a whole group, open a ballot and cast this
    # host's signed vote. An app holding these acts on the USER's own rosters
    # and no wider one: every mutation is refused unless this host owns the
    # group (server/groups/roster.py _require_owner), and a member's replica of
    # someone else's roster is read-only by the same gate.
    "read:group",
    "write:group",

    # Credential vault — the identity-scoped sealed-envelope store (server/vault/).
    # read:vault  = list credential metadata (names/types/dates) — never bytes.
    # write:vault = add / revoke / rotate, and (with a TOTP step-up) reveal or
    # broker a USE. The agent surface (keys_manage) never returns raw bytes; the
    # only byte-returning path is the user's TOTP-gated reveal.
    "read:vault",
    "write:vault",
})


# RETIRED: no route requires these and they grant nothing, but manifests that
# were already published still declare them, and a Host keeps an installed
# store app until its store sync upgrades it. validate() accepts them so that
# manifest still loads (an unknown permission fails the whole manifest, and the
# app would not open). Then they are dropped at every door: the manifest loader
# (manifest.load), every token (tokens.mint, which also covers a registry row
# an older kernel wrote) and the install-consent preview (routes/applications.py
# store_preview). An old registry row may still LIST one until the app is
# upgraded; it grants nothing. They are not in PERMISSIONS, so nothing new is
# offered or built with them.
RETIRED_PERMISSIONS: Final[frozenset[str]] = frozenset({
    # The CEIGAS incident lane (POST /api/ceigas/incidents/{id}/grant
    # and /deny, /policy/revoke). Every Security manifest up to 2.0.6 declares
    # both.
    "write:incident_decision",
    "write:policy_proposals",
})


def validate(perms: list[str]) -> list[str]:
    """Return the list unchanged if every permission is in the catalog or
    retired. Raises ValueError listing any unknown permissions.
    """
    unknown = [p for p in perms if p not in PERMISSIONS and p not in RETIRED_PERMISSIONS]
    if unknown:
        raise ValueError(f"Unknown permission(s): {unknown}")
    return perms
