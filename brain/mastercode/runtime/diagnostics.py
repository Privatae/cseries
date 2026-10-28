"""mastercode.runtime.diagnostics — the host's own machine, checked with no model.

No model runs here. One prompt block where the errors are points at the
diagnostic skill; the host is then given deterministic data with every route
it needs to change the configuration. Diagnosis is a tool call, and the
configuration change is another, existing, tool call scoped to configuration.
There is no background model.

Three parts, none of them a model:

  collect_faults(pool, entity_id)   the checks. Each fault is typed data —
                                    what is wrong, measured how, and the
                                    EXACT configuration call that fixes it
                                    (tool + args), or `ask_user` when the
                                    fix is the user's pick or their consent.
  render_faults_block(faults)       the per-turn prompt block: one line per
                                    fault, then "call diagnose". Empty when
                                    nothing is wrong, so a healthy host pays
                                    nothing.
  `diagnose` (tools._exec_diagnose) returns the faults with their fixes.
  collect_failures(pool, entity_id) what FAILED lately, from the Host's own
                                    records: model calls a provider refused
                                    (inference_cost_log), tool calls that
                                    errored (error_events), tasks that failed
                                    (entity_tasks). Grouped, counted, each in
                                    the provider's or tool's own words.

The fixes are the host's EXISTING configuration tools (model_routing,
embed_jobs, coding_protocol, memory_curate, system.*). A failure is read, not
fixed here: no `fix` is ever a code change.

After a Host spent an hour on a wrong theory while the answer sat in its own database ("anthropic 400: …image.title: Extra inputs
are not permitted", 36 rows in inference_cost_log): when anything fails,
diagnose is the FIRST place the Host looks, and it is where a support
ticket's evidence comes from (server/routes/support.py reads
collect_failures, so the ticket carries what diagnose showed).

Every reader below is best-effort and swallows its own errors — a broken
probe is one fault fewer, never a failed turn. Results are cached per entity
for FAULTS_TTL_S so the per-turn block costs one set of reads per window.
"""
from __future__ import annotations

import json
import logging
import re
import time

log = logging.getLogger("cseries.mastercode.diagnostics")

FAULTS_TTL_S = 120.0
STALE_RULE_DAYS = 90
MAX_BLOCK_LINES = 8
# Wall-clock budget for one collection, shared across the probes: the block
# is built inside a live turn, and the routing probe's recommendation walks
# provider catalogs (15 s urllib timeout per provider in inference.py). Same
# posture as the map's 3 s remote cap and the memory prefire's 4 s: a slow
# provider costs one probe, never the turn. A probe that times out is
# skipped this window; the partial result is cached like any other.
COLLECT_BUDGET_S = 4.0
# Roles the routing probe never reports. `image` is in inference.ROLES but
# nothing resolves it (image_engine reads image_gen / vision) and
# `recommend` never fills it, so it would render a permanent "needs a
# decision" on every host. `embedding` is not a
# provider route at all — the embedding probe owns it.
ROUTING_ROLES_IGNORED = frozenset({"image", "embedding"})
# The configuration tools whose SUCCESS changes what the checks would find.
# The tool executor drops the cache on a call to any of them, so the block
# a fix clears is gone on the next turn, not 120 s later.
CONFIG_TOOLS = frozenset({"model_routing", "embed_jobs", "embedding_route",
                          "coding_protocol", "memory_curate",
                          "system.restart_app", "system.start_app", "system.stop_app",
                          # the dispatcher accepts the underscore spellings too
                          "system_restart_app", "system_start_app", "system_stop_app"})

# What the host may apply on its own vs must ask for. Rendered into the
# tool result so the rule travels with the data, not as prose in a prompt.
POLICY = {
    "self_apply": [
        "route a role that has NO route today (nothing runs there yet to break); tell the user what was set and why",
        "queue an embedding fill for rows missing a vector in the configured model's column",
        "save a protocol file the migration wrote with a defect (author 'migrated')",
        "restart a sidecar the system skill's crash policy allows",
    ],
    "ask_first": [
        "move a role that already works to another provider or model (it changes who is billed) — model_routing op='set' files it as a decision card for the user; the route lands only on their 'apply'",
        "switch or re-embed the embedding model (existing vectors cost money)",
        "delete anything, or retire a rule the user taught",
        "anything with no configuration call listed — say what you found and what it needs",
    ],
    "never": [
        "apply a code change as a diagnostic fix — every `fix` here is a configuration call",
    ],
}

# A failure no configuration call fixes is the platform's. Who fixes it
# depends on two grants. The platform source is the `infra.codebase` admin
# grant that puts the source reads (code_grep, code_read) in its tool list
# (tools.py, the bridge catalog gate). The change itself is a pull request
# the Host opens in its own turn with gitea op='propose' (a fork, a branch, a
# PR on the platform repo), the one write path to the platform source, and
# that op needs the write:gitea scope, which
# only a read_write grant on external.operator carries (tools._exec_gitea
# checks it; meta_tool_caps.DOMAIN_ACCESS_TO_SCOPES). So the rule offered is
# chosen from both grants: a Host is never told to call an op it would be
# refused. A source holder without write:gitea reads the code, is told the
# exact grant it lacks, and files a support ticket with what it read. A Host
# without the source files a support ticket, which carries this reading as
# evidence. The platform repo is triage.CODE_DEFAULT_REPO (configuration, no
# default); a Host with none configured has no repo to propose to and files a
# ticket, as a Host without the source does.
PROPOSE_SCOPE = "write:gitea"
PLATFORM_FAULT_WITH_SOURCE = (
    "a fault in the platform itself is yours to fix: you hold the platform source "
    "(platform_source is true). Find the code it names by meaning first "
    "(search_my_code with project='{repo}'), pin it with code_grep and read the "
    "range with code_read, then "
    "open the fix as a pull request yourself, never through an agent: "
    "gitea op='propose' with repo='{repo}', a title, a body and the change "
    "as files (each a whole file, or edits to it) or a patch, and give the user the pr_url; "
    "do not file a support ticket for it")
PLATFORM_FAULT_SOURCE_NO_PROPOSE = (
    "a fault in the platform itself: you hold the platform source (platform_source is "
    "true), so find the code it names by meaning first (search_my_code with "
    "project='{repo}'), pin it with code_grep and read the range with code_read. "
    "You cannot open the pull request yourself: that needs the write:gitea scope, which "
    "only a read_write grant on external.operator carries, and this Host holds none "
    "(can_propose is false). File it with tickets op='support_open', naming the file and "
    "line you read and the change it needs — the draft carries this reading for the "
    "person to approve before anything is sent; the person can grant read_write on "
    "external.operator for you to open such fixes yourself next time")
PLATFORM_FAULT_WITHOUT_SOURCE = (
    "a fault in the platform itself goes to the platform: file it with "
    "tickets op='support_open' — the draft carries this reading (the failures and "
    "the faults) for the person to approve before anything is sent")


def platform_fault_rule(platform_source: bool, can_propose: bool) -> str:
    """The rule for a platform fault, chosen from the two grants it needs:
    the platform source (holds_platform_source) and the write:gitea scope that
    gitea op='propose' checks (holds_propose_scope), and the platform repo
    they point at (triage.CODE_DEFAULT_REPO); with none configured the fault
    goes to a ticket."""
    from mastercode.runtime.triage import CODE_DEFAULT_REPO
    if not platform_source or not CODE_DEFAULT_REPO:
        return PLATFORM_FAULT_WITHOUT_SOURCE
    rule = PLATFORM_FAULT_WITH_SOURCE if can_propose else PLATFORM_FAULT_SOURCE_NO_PROPOSE
    return rule.format(repo=CODE_DEFAULT_REPO)


async def holds_platform_source(pool, entity_id) -> bool:
    """True when this Host holds the platform source: `infra.codebase` admin,
    the exact row tools.py reads to list the source tools at all."""
    if pool is None:
        return False
    try:
        return bool(await pool.fetchval(
            """SELECT 1 FROM entity_domain_access
                WHERE entity_id = $1 AND domain_id = 'infra.codebase'
                  AND access_level = 'admin' LIMIT 1""", int(entity_id)))
    except Exception as e:  # noqa: BLE001 - unknown reads as "no", the safe side
        log.debug("platform-source read failed (non-fatal): %s", e)
        return False


async def holds_propose_scope(pool, entity_id) -> bool:
    """True when this Host holds write:gitea, the scope tools._exec_gitea
    requires for op='propose', read through the same resolver it uses
    (meta_tool_caps._resolve_entity_scopes). A failed read is "no"."""
    if pool is None:
        return False
    try:
        from mastercode.runtime.meta_tool_caps import _resolve_entity_scopes
        return PROPOSE_SCOPE in await _resolve_entity_scopes(pool, int(entity_id))
    except Exception as e:  # noqa: BLE001 - unknown reads as "no", the safe side
        log.debug("propose-scope read failed (non-fatal): %s", e)
        return False


_CACHE: dict[int, tuple[float, list]] = {}


def _fault(fid: str, area: str, severity: str, title: str, *, detail: str = "",
           fix: dict | None = None, ask_user: str | None = None,
           evidence: dict | None = None) -> dict:
    return {
        "id": fid, "area": area,
        # fix = a configuration call exists; review = the host reads and decides;
        # info = nothing to configure (platform side or a harness gap).
        "severity": severity, "title": title, "detail": detail,
        "fix": fix, "ask_user": ask_user, "evidence": evidence or {},
    }


# ── probes (each returns a list of faults; each is best-effort) ──────────

async def _probe_routing(pool, entity_id, probes) -> list[dict]:
    out: list[dict] = []
    status = await probes["routing_status"]()
    if not isinstance(status, dict):
        return out
    unrouted = [r for r in (status.get("unrouted") or []) if r not in ROUTING_ROLES_IGNORED]
    keyed = list(status.get("providers_with_keys") or [])
    # An UNVALIDATED model on a tool-using role (runtime/model_validation):
    # a review fault ONLY while the gate ENFORCES — that
    # is when the route has actually lost something and the user can act.
    # While the gate observes (the list ships empty; enforce=false) this
    # would be a permanent attention block on every host with an ask nobody
    # can satisfy. The routing status carries
    # the flag either way.
    enforced = bool(((status.get("validation") or {}).get("enforced")))
    if enforced:
        for role, st in (status.get("roles") or {}).items():
            if not isinstance(st, dict) or not st.get("routed") or "validated" not in st:
                continue
            # `chat` is the only tool-using role since the three-role cut
            # — a dispatched agent runs on the chat route, so
            # one fault covers both consequences instead of two rows naming
            # the same model.
            if st.get("validated") or role != "chat":
                continue
            consequence = ("keeps chat and read tools; loses operate, dispatch and the build "
                           "doors, and no dispatched agent starts")
            out.append(_fault(
                f"routing.{role}.unvalidated", "routing", "review",
                f"role '{role}' runs on {st.get('provider')} / {st.get('model')}, which has not passed "
                f"the platform's admission bench — {consequence}",
                ask_user=("route the role to a validated model in the Orchestrator, or keep it and "
                          "accept a host that talks and reads but does not act"),
                evidence={"enforced": True, "provider": st.get("provider"), "model": st.get("model")}))
    if not unrouted:
        return out
    recs, notes = {}, []
    if keyed:
        rec = await probes["routing_recommend"]()
        recs = (rec or {}).get("recommendations") or {}
        # Why a keyed provider produced no recommendation (e.g. "deepinfra
        # lists no modality data for its models"). Without it the block says
        # only "no recommendation matched" and the reason stays in the tool.
        notes = [str(n) for n in ((rec or {}).get("notes") or []) if n]
    for role in unrouted:
        r = recs.get(role)
        if (r and r.get("provider") and r.get("model")
                and r.get("capability_published") is False and not r.get("documented")):
            # The provider's catalog states no modality for its models, so the
            # "recommendation" is an arbitrary row, not a model known to do the
            # job. It is never a fix the host may apply on its own — that is
            # how a Host was told to route image_gen to a text-to-speech
            # model. The recommender now skips such providers;
            # this keeps the block honest if an older one is still answering.
            out.append(_fault(
                f"routing.{role}", "routing", "review",
                f"role '{role}' has no route; {r['provider']} publishes no capability data, so no model there can be recommended for it",
                ask_user="the user picks a model they know can do this in the Orchestrator, or adds a key for a provider whose catalog states what its models do",
                fix={"tool": "model_routing", "args": {"op": "recommend"}},
                evidence={"providers_with_keys": keyed, "capability_published": False}))
        elif r and r.get("provider") and r.get("model"):
            out.append(_fault(
                f"routing.{role}", "routing", "fix",
                f"role '{role}' has no route; {r['provider']} / {r['model']} is the recommended pick",
                detail=r.get("why") or "from the platform's recommended routes for a key the user holds",
                fix={"tool": "model_routing",
                     "args": {"op": "set", "role": role, "provider": r["provider"], "model": r["model"]}},
                evidence={"providers_with_keys": keyed}))
        elif keyed:
            # The recommender's note names its role as "... for <role> —";
            # match that phrase exactly, not the role's name anywhere in the
            # sentence (a note about vision that mentions image_gen would
            # otherwise attach to both).
            role_notes = [n for n in notes if f" for {role} " in f" {n} "]
            out.append(_fault(
                f"routing.{role}", "routing", "review",
                f"role '{role}' has no route and no recommendation matched the keys on hand ({', '.join(keyed)})",
                detail="; ".join(role_notes),
                ask_user="which provider should serve this role — the user picks from the keys they hold",
                fix={"tool": "model_routing", "args": {"op": "recommend"}},
                evidence={"providers_with_keys": keyed, "notes": role_notes}))
        else:
            out.append(_fault(
                f"routing.{role}", "routing", "review",
                f"role '{role}' has no route and no provider key is stored",
                ask_user="the user adds a provider key in the Keys app first",
                evidence={"providers_with_keys": []}))
    return out


async def _probe_embeddings(pool, entity_id, probes) -> list[dict]:
    out: list[dict] = []
    cur = await probes["embedding_current"]()
    if not isinstance(cur, dict):
        return out
    if not cur.get("configured"):
        out.append(_fault(
            "embedding.unconfigured", "embedding", "review",
            "embeddings are not set up on this host (a setup step, not an outage)",
            ask_user="the user provides the endpoint: their own machine (embedding_route op='bastions' → 'use_bastion') or a provider they hold a key for",
            fix={"tool": "embedding_route", "args": {"op": "bastions"}}))
        return out
    cov = await probes["embedding_coverage"]()
    if not isinstance(cov, dict) or cov.get("error"):
        return out
    active = int(cov.get("active_jobs") or 0)
    for bank, target in (("entity_memory", "entity_memory"), ("user_context", "user_context")):
        b = cov.get(bank) or {}
        missing = int(b.get("missing") or 0)
        if missing > 0 and active == 0:
            out.append(_fault(
                f"embedding.fill.{bank}", "embedding", "fix",
                f"{missing} of {b.get('total')} {bank.replace('_', ' ')} rows have no vector for {cur.get('model')} and no fill job is running",
                detail="a fill only ADDS to the configured model's own column; nothing is re-embedded or moved",
                fix={"tool": "embed_jobs", "args": {"op": "enqueue", "target": target}},
                evidence={"model": cur.get("model"), "embedded": b.get("embedded"),
                          "total": b.get("total"), "active_jobs": active}))
    return out


async def _probe_protocols(pool, entity_id, probes) -> list[dict]:
    out: list[dict] = []
    connected = set(await probes["connected_engines"]() or [])
    docs = await probes["protocol_docs"]()
    for doc in docs or []:
        if not isinstance(doc, dict):
            continue
        slug = doc.get("slug") or doc.get("name")
        try:
            from server.routes.coding_protocols import repair_migrated_bench
            import copy as _copy
            repaired, changed = repair_migrated_bench(_copy.deepcopy(doc))
        except Exception:  # noqa: BLE001
            repaired, changed = doc, False
        if changed:
            out.append(_fault(
                f"protocol.{slug}.bench", "protocols", "fix",
                f"protocol '{doc.get('name')}' repeats an engine in its last-resort bench step",
                detail="a rung that failed gets no second shot inside the last resort; the repaired file is in the fix",
                fix={"tool": "coding_protocol",
                     "args": {"op": "save", "slug": slug, "protocol": repaired}}))
        missing = sorted({str(e.get("engine")) for s in (doc.get("steps") or [])
                          for e in (s.get("engines") or []) if isinstance(e, dict)
                          and e.get("engine") and str(e.get("engine")) not in connected})
        if missing and connected:
            out.append(_fault(
                f"protocol.{slug}.engines", "protocols", "review",
                f"protocol '{doc.get('name')}' names engines that are not connected: {', '.join(missing)}",
                ask_user="reconnect the engine in the Orchestrator, or drop it from the protocol (coding_protocol op='save')",
                fix={"tool": "coding_protocol", "args": {"op": "get", "slug": slug}},
                evidence={"connected": sorted(connected)}))
    default = await probes["default_engine"]()
    if connected and not default:
        out.append(_fault(
            "engines.default", "protocols", "review",
            f"no default coding engine is ticked ({len(connected)} connected) — an unnamed build refuses",
            ask_user="the user ticks the default in Orchestrator → Coding Engines (no host tool sets it)",
            evidence={"connected": sorted(connected)}))
    return out


async def _probe_usage(pool, entity_id, probes) -> list[dict]:
    out: list[dict] = []
    rows = await probes["usage_provenance"]()
    for r in rows or []:
        if int(r.get("runs") or 0) > 0 and int(r.get("runs_with_tokens") or 0) == 0:
            out.append(_fault(
                f"usage.{r.get('harness')}", "ledger", "info",
                f"{r.get('harness')}: {r.get('runs')} runs in 30 days, none reported token usage — its stream carries no usage line",
                detail="billed cost is still recorded per run; only the token count is missing. Not a configuration: a harness gap the platform owns",
                evidence=dict(r)))
    return out


async def _probe_rules(pool, entity_id, probes) -> list[dict]:
    out: list[dict] = []
    rows = await probes["stale_rules"]()
    if rows:
        out.append(_fault(
            "memory.stale_rules", "memory", "review",
            f"{len(rows)} taught rules are older than {STALE_RULE_DAYS} days and still active",
            detail="a rule about a tool or a bench is a snapshot from its date; retire or correct the ones the map contradicts",
            ask_user="a rule is the user's: ask before you change one, then correct it with memory_curate replace (it stays a rule) or drop it with memory_curate drop, in the open",
            fix={"tool": "memory_curate", "args": {"op": "replace", "memory_id": "<the rule's #id>", "content": "<the corrected rule>", "about": "person"}},
            evidence={"rules": rows[:5]}))
    return out


# ── failures: what went wrong lately, in the provider's or tool's words ──
#
# Three of the Host's own records, read with no model:
#   provider  inference_cost_log rows with purpose='inference_error' — a model
#             call the provider refused or that died; the provider's answer
#             rides in metadata.error ("anthropic 400: {json}").
#   tool      error_events — every failed tool call the host loop sees
#             (server/error_log.py), with context.fault 'platform' (the tool
#             ran and failed) or 'model' (bad arguments, a refusal, a dedup).
#   task      entity_tasks with status 'failed' — an agent or build that
#             failed, with its recorded error.
# Rows are grouped by what they SAY (numbers folded, request ids dropped), so
# 36 refusals of one picture are one line with a count. The message shown is
# the latest row's own words, screened (untrusted_content): an error can echo
# text from the web or a repo, and this lands in a model's context.

FAILURE_WINDOW_H = 24          # diagnose's default window
FAILURE_REPEAT = 3             # this many in the window makes a block fault
FAILURE_ROWS = 2000            # rows read per source per collection
FAILURE_GROUPS = 20            # groups returned per source
FAILURE_MSG_CHARS = 600        # one message, as shown

_PROVIDER_ERR = re.compile(r"^\s*([\w.\- ]+?)\s+(\d{3}):\s*(.*)$", re.S)
_ID_NOISE = re.compile(
    r"\b(?:req|msg|toolu|user)_[A-Za-z0-9]{6,}\b"           # provider request / user ids
    r"|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
    r"|\b[0-9a-f]{16,}\b")
_DIGITS = re.compile(r"\d+")
_FAILURE_CACHE: dict[tuple, tuple[float, dict]] = {}


def provider_message(err) -> tuple[str | None, str]:
    """(status, message) from a provider error string. "anthropic 400:
    {"error": {"message": "…"}}" → ("400", "…"). Any shape that does not parse
    comes back whole, as the message."""
    text = str(err or "").strip()
    m = _PROVIDER_ERR.match(text)
    if not m:
        return None, text
    status, rest = m.group(2), m.group(3).strip()
    try:
        body = json.loads(rest)
    except (ValueError, TypeError):
        return status, rest
    msg = None
    if isinstance(body, dict):
        e = body.get("error")
        if isinstance(e, dict):
            msg = e.get("message") or e.get("type")
        elif isinstance(e, str):
            msg = e
        msg = msg or body.get("message") or body.get("detail")
    return status, str(msg) if msg else rest


def _shape(text: str) -> str:
    """What a message SAYS, for grouping: ids dropped, numbers folded."""
    return _DIGITS.sub("N", _ID_NOISE.sub("ID", str(text or "")))[:400]


def _screened(text: str) -> tuple[str, list[str]]:
    text = str(text or "")
    if len(text) > FAILURE_MSG_CHARS:
        text = text[:FAILURE_MSG_CHARS] + f"…[{len(text) - FAILURE_MSG_CHARS:,} more characters]"
    try:
        from mastercode.runtime.untrusted_content import screen_for_model
        return screen_for_model(text, source="diagnose_failures")
    except Exception as e:  # noqa: BLE001 - the screen fails OPEN, flagged
        log.debug("failure screen unavailable: %s", e)
        return text, ["screen_error"]


def _iso(v):
    try:
        return v.isoformat()
    except AttributeError:
        return str(v) if v is not None else None


def _group(rows, key_of, base_of, message_of, *, when="at", merge=None) -> list[dict]:
    """Rows newest first → groups newest first, each with the latest row's
    message, the count, and first/last seen. `merge(group, row)` folds a
    per-row fact into its group (every row, not only the newest)."""
    groups: dict[tuple, dict] = {}
    for r in rows or []:
        msg = message_of(r)
        k = key_of(r, msg)
        g = groups.get(k)
        if g is None:
            screened, flags = _screened(msg)
            g = groups[k] = {**base_of(r), "message": screened, "count": 0,
                             "first_seen": _iso(r.get(when)), "last_seen": _iso(r.get(when))}
            if flags:
                g["screen_flags"] = flags
        g["count"] += 1
        g["first_seen"] = _iso(r.get(when))      # rows are newest first
        if merge is not None:
            merge(g, r)
    return list(groups.values())


def _short_id(text: str) -> str:
    import hashlib
    return hashlib.sha1(_shape(text).encode()).hexdigest()[:10]


def _md(v) -> dict:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except (ValueError, TypeError):
            return {}
    return v if isinstance(v, dict) else {}


def failure_groups(provider_rows, tool_rows, task_rows) -> dict:
    """Group the three sources' rows (each newest first). Pure: the readers
    fetch, this shapes — so a test feeds rows and reads exactly this."""
    def p_key(r, msg):
        status, _ = provider_message(_md(r.get("metadata")).get("error"))
        return (r.get("provider"), r.get("model_id"), status, _shape(msg))

    def p_base(r):
        status, _ = provider_message(_md(r.get("metadata")).get("error"))
        return {"provider": r.get("provider"), "model": r.get("model_id"), "status": status}

    def p_msg(r):
        md = _md(r.get("metadata"))
        _, msg = provider_message(md.get("error") or md.get("message") or md.get("detail"))
        return msg or "(the provider returned no error text)"

    def p_merge(g, r):
        # provider_gateway stamps picture_refused when it re-sent the call
        # with notes in place of the pictures: the pictures never arrived.
        md = _md(r.get("metadata"))
        if md.get("picture_refused"):
            g["pictures_dropped"] = True
        # A safety-filter decline (provider_gateway `refusal`): its category,
        # and what the route's Orchestrator fallback did — answered (which
        # model), failed too, or there is none. From the group's NEWEST row
        # (rows are newest first): what happens now, not a week ago.
        if md.get("refusal") and not g.get("refusal"):
            g["refusal"] = True
            g["refusal_category"] = md.get("refusal_category")
            if md.get("failed_over_to"):
                g["fallback"] = md["failed_over_to"]
                if isinstance(md.get("fallback_answered"), bool):
                    g["fallback_answered"] = md["fallback_answered"]
            elif md.get("refusal_not_retried"):
                # A category no fallback is tried for: the fix is the request.
                g["not_retried"] = True
            elif md.get("no_fallback"):
                g["no_fallback"] = True

    provider = _group(provider_rows, p_key, p_base, p_msg, when="created_at", merge=p_merge)

    tools = _group(
        tool_rows,
        lambda r, msg: (r.get("tool"), r.get("kind"), r.get("fault") or "platform", _shape(msg)),
        lambda r: {"tool": r.get("tool"), "kind": r.get("kind"), "source": r.get("source"),
                   "fault": r.get("fault") or "platform"},
        lambda r: r.get("message") or "")
    def t_merge(g, r):
        if len(g["task_ids"]) < 10 and r.get("task_id") is not None:
            g["task_ids"].append(int(r["task_id"]))

    tasks = _group(
        task_rows,
        lambda r, msg: (r.get("task_type"), _shape(msg)),
        lambda r: {"task_type": r.get("task_type"), "task_ids": []},
        lambda r: r.get("error") or "(no error recorded)", when="at", merge=t_merge)
    return {"provider": provider[:FAILURE_GROUPS], "tool": tools[:FAILURE_GROUPS],
            "task": tasks[:FAILURE_GROUPS]}


def failure_readers(pool, entity_id) -> dict:
    """The real readers. Each takes the window in hours, returns rows newest first."""
    async def provider(hours):
        rows = await pool.fetch(
            """SELECT created_at, provider, model_id, source_module, metadata
                 FROM inference_cost_log
                WHERE entity_id = $1 AND purpose = 'inference_error'
                  AND created_at > now() - make_interval(hours => $2)
                ORDER BY created_at DESC LIMIT $3""", int(entity_id), int(hours), FAILURE_ROWS)
        return [dict(r) for r in rows]

    async def tool(hours):
        # error_events is the pod's own table (no entity column): one Host per pod.
        rows = await pool.fetch(
            """SELECT at, source, kind, tool, message, context->>'fault' AS fault
                 FROM error_events
                WHERE at > now() - make_interval(hours => $1)
                ORDER BY at DESC LIMIT $2""", int(hours), FAILURE_ROWS)
        return [dict(r) for r in rows]

    async def task(hours):
        rows = await pool.fetch(
            """SELECT task_id, task_type, error, COALESCE(completed_at, created_at) AS at
                 FROM entity_tasks
                WHERE entity_id = $1 AND status = 'failed'
                  AND COALESCE(completed_at, created_at) > now() - make_interval(hours => $2)
                ORDER BY COALESCE(completed_at, created_at) DESC LIMIT $3""",
            int(entity_id), int(hours), FAILURE_ROWS)
        return [dict(r) for r in rows]

    return {"provider": provider, "tool": tool, "task": task}


async def collect_failures(pool, entity_id, *, since_hours: int = FAILURE_WINDOW_H,
                           readers: dict | None = None, refresh: bool = False) -> dict:
    """What failed in the last `since_hours`, grouped. Cached per (entity,
    window) for FAULTS_TTL_S like the faults; `readers` (tests) bypasses the
    cache. A source whose read fails is named in `unread`, never guessed."""
    hours = max(1, min(int(since_hours or FAILURE_WINDOW_H), 24 * 30))
    key = (int(entity_id), hours)
    now = time.monotonic()
    if readers is None and not refresh:
        hit = _FAILURE_CACHE.get(key)
        if hit and now - hit[0] < FAULTS_TTL_S:
            return hit[1]
    rd = readers or failure_readers(pool, entity_id)
    rows, unread = {}, []
    for src in ("provider", "tool", "task"):
        try:
            rows[src] = await rd[src](hours)
        except Exception as e:  # noqa: BLE001 - one unreadable source, named
            log.debug("failures: %s read failed (non-fatal): %s", src, e)
            rows[src] = []
            unread.append(src)
    out = {"since_hours": hours, **failure_groups(rows["provider"], rows["tool"], rows["task"])}
    if unread:
        out["unread"] = unread
    if readers is None:
        _FAILURE_CACHE[key] = (now, out)
    return out


def _failure_line(g: dict) -> str:
    who = (f"{g.get('provider') or '?'} / {g.get('model') or '?'}" if "model" in g
           else g.get("tool") or g.get("task_type") or "?")
    status = f" {g['status']}" if g.get("status") else ""
    dropped = " · pictures were dropped" if g.get("pictures_dropped") else ""
    return (f"{who}{status} · {g['count']}× (first {g.get('first_seen')}, last "
            f"{g.get('last_seen')}){dropped}{_decline_note(g)}: {g['message']}")


def _decline_note(g: dict) -> str:
    """' · safety-filter decline (category cyber) · fallback X answered' for a
    group of declines; '' for any other failure."""
    if not g.get("refusal"):
        return ""
    cat = g.get("refusal_category")
    out = " · safety-filter decline" + (f" (category {cat})" if cat else "")
    if g.get("fallback"):
        out += (f" · fallback {g['fallback']} answered" if g.get("fallback_answered")
                else f" · fallback {g['fallback']} did not answer either"
                if g.get("fallback_answered") is False else f" · fallback {g['fallback']} tried")
    elif g.get("not_retried"):
        out += " · not sent to a fallback (the request asked for the model's internal reasoning)"
    elif g.get("no_fallback"):
        out += " · no fallback is set in the Orchestrator"
    return out


def render_failures_text(failures: dict, sources=("provider", "tool", "task")) -> str:
    """The failures as plain lines — what a support ticket carries."""
    titles = {"provider": "Model calls a provider refused or that failed",
              "tool": "Tool calls that failed", "task": "Agents and builds that failed"}
    parts = []
    for src in sources:
        groups = (failures or {}).get(src) or []
        if groups:
            parts.append(f"{titles[src]} (last {failures.get('since_hours')} h):")
            parts.extend("- " + _failure_line(g) for g in groups)
    return "\n".join(parts)


async def _probe_failures(pool, entity_id, probes) -> list[dict]:
    """A failure that REPEATS is a fault of its own, so the block shows it
    without the Host asking: a provider refusing the same call, or a tool
    failing on the platform's side. A single failure, or a model's own bad
    call, stays in diagnose(area='failures') only — the block is for what
    keeps happening."""
    reader = probes.get("failures")
    if reader is None:
        return []
    f = await reader()
    if not isinstance(f, dict):
        return []
    out: list[dict] = []
    hours = f.get("since_hours") or FAILURE_WINDOW_H
    for g in f.get("provider") or []:
        if g.get("refusal"):
            # A safety-filter decline is shown from the first one: the person
            # either got no answer or got one from another model, and the
            # Host is the one to tell them (skill 'diagnostic').
            out.append(_decline_fault(g, hours))
            continue
        if int(g.get("count") or 0) < FAILURE_REPEAT:
            continue
        what = (f"{g.get('provider')} refused {g['count']} calls to {g.get('model')} in the last "
                f"{hours} h{(' (' + g['status'] + ')') if g.get('status') else ''}: {g['message'][:240]}")
        detail = ("Each refused call was sent again with a note in place of every picture, so the "
                  "pictures never reached the model. " if g.get("pictures_dropped") else "")
        out.append(_fault(
            f"failures.provider.{g.get('provider')}.{_short_id(g['message'])}",
            "failures", "review", what,
            detail=detail + "Not a configuration fix: read diagnose(area='failures') for it in full.",
            evidence={k: g.get(k) for k in ("provider", "model", "status", "count",
                                            "first_seen", "last_seen", "pictures_dropped")}))
    for g in f.get("tool") or []:
        if g.get("fault") != "platform" or int(g.get("count") or 0) < FAILURE_REPEAT:
            continue
        out.append(_fault(
            f"failures.tool.{g.get('tool')}.{_short_id(g['message'])}",
            "failures", "review",
            f"{g.get('tool')} failed {g['count']} times in the last {hours} h: {g['message'][:240]}",
            detail="The tool ran and failed on the platform's side. Read diagnose(area='failures').",
            evidence={k: g.get(k) for k in ("tool", "kind", "count", "first_seen", "last_seen")}))
    return out


def _decline_fault(g: dict, hours) -> dict:
    """The block's fault for a group of safety-filter declines: what was
    declined, which model answered, and the door to a fallback (set only by
    the person's approval of a card)."""
    cat = g.get("refusal_category")
    what = (f"{g.get('provider')}'s safety filter declined {g['count']} request(s) to "
            f"{g.get('model')} in the last {hours} h"
            f"{(' (category ' + str(cat) + ')') if cat else ''}, the last at "
            f"{g.get('last_seen')}")
    if g.get("fallback") and g.get("fallback_answered"):
        what += f"; the Orchestrator's fallback {g['fallback']} answered instead"
        detail = (f"Their message was answered by {g['fallback']}, not {g.get('model')}, "
                  "because of this decline.")
    elif g.get("fallback"):
        what += f"; the Orchestrator's fallback {g['fallback']} did not answer either"
        detail = ("Neither model answered. A different fallback is the person's choice: "
                  "model_routing op='set' files it as a card they approve; nothing changes "
                  "until they do.")
    elif g.get("not_retried"):
        what += "; it asked the model to write out its internal reasoning, so no fallback was tried"
        detail = ("A short explanation of the answer, or a summary of what was done, is "
                  "fine to ask for. No fallback helps with this one: such a decline is not "
                  "retried on another model.")
    else:
        what += "; no fallback is set in the Orchestrator, so nothing answered"
        detail = ("No other model is set to answer it. model_routing op='set' "
                  "(fallback_provider, fallback_model) files a fallback for this role as a "
                  "card the person approves; nothing changes until they do.")
    return _fault(
        f"failures.provider.{g.get('provider')}.refusal.{_short_id(g['message'])}",
        "failures", "review", what,
        detail=detail + " diagnose(area='failures') has each decline.",
        evidence={k: g.get(k) for k in ("provider", "model", "count", "first_seen", "last_seen",
                                        "refusal_category", "fallback", "fallback_answered",
                                        "no_fallback", "not_retried")})


PROBES = (
    ("routing", _probe_routing),
    ("embedding", _probe_embeddings),
    ("protocols", _probe_protocols),
    ("ledger", _probe_usage),
    ("memory", _probe_rules),
    ("failures", _probe_failures),
)


def default_probes(pool, entity_id) -> dict:
    """The real readers — each the SAME function the host's tools or the
    apps read through, so a fault here is a fact the UI would show."""
    async def routing_status():
        from mastercode.runtime.tools import _exec_model_routing
        r = await _exec_model_routing({"op": "status"})
        return r.get("result") if r.get("status") == "ok" else None

    async def routing_recommend():
        from mastercode.runtime.tools import _exec_model_routing
        r = await _exec_model_routing({"op": "recommend"})
        return r.get("result") if r.get("status") == "ok" else None

    async def embedding_current():
        from server.routes import embedding as embedding_route
        return await embedding_route.current()

    async def embedding_coverage():
        from server.routes import embed as embed_routes
        return await embed_routes.coverage()

    async def connected_engines():
        from server.routes.review import _installed_harnesses
        return await _installed_harnesses()

    async def default_engine():
        from server.routes.review import _default_coding_harness
        return await _default_coding_harness()

    async def protocol_docs():
        from server.routes.coding_protocols import list_protocols, get_protocol
        rows = (await list_protocols()).get("protocols") or []
        docs = []
        for row in rows:
            try:
                docs.append(await get_protocol(row["slug"]))
            except Exception:  # noqa: BLE001
                continue
        return docs

    async def usage_provenance():
        rows = await pool.fetch(
            """SELECT harness, COUNT(*) AS runs,
                      COUNT(*) FILTER (WHERE token_source <> 'unknown') AS runs_with_tokens
                 FROM harness_usage
                WHERE created_at > now() - interval '30 days'
                GROUP BY harness""")
        return [dict(r) for r in rows]

    async def stale_rules():
        # A rule of either shape (memory_engine.RULE_KINDS).
        from mastercode.memory_engine import rule_row_sql
        rows = await pool.fetch(
            f"""SELECT id, created_at::date::text AS created, LEFT(content, 140) AS content
                 FROM entity_memory
                WHERE entity_id = $1 AND {rule_row_sql()} AND status = 'active'
                  AND (t_valid_until IS NULL OR t_valid_until > NOW())
                  AND created_at < now() - make_interval(days => $2)
                ORDER BY created_at ASC LIMIT 20""", entity_id, STALE_RULE_DAYS)
        return [dict(r) for r in rows]

    async def failures():
        return await collect_failures(pool, entity_id)

    return {
        "routing_status": routing_status, "routing_recommend": routing_recommend,
        "embedding_current": embedding_current, "embedding_coverage": embedding_coverage,
        "connected_engines": connected_engines, "default_engine": default_engine,
        "protocol_docs": protocol_docs, "usage_provenance": usage_provenance,
        "stale_rules": stale_rules, "failures": failures,
    }


def invalidate(entity_id) -> None:
    """Drop the cached faults for an entity — called by the tool executor on
    any configuration-tool call, so a fix the host applies is reflected on
    the very next turn."""
    try:
        _CACHE.pop(int(entity_id), None)
    except (TypeError, ValueError):
        pass


async def collect_faults(pool, entity_id: int, *, probes: dict | None = None,
                         refresh: bool = False, areas: list | None = None,
                         budget_s: float = COLLECT_BUDGET_S) -> list[dict]:
    """Every fault the checks can see right now, fixes attached. Cached per
    entity for FAULTS_TTL_S; `refresh` recomputes (all areas, so the cache
    is whole) and `areas` filters the result. Every probe shares one
    wall-clock budget; a probe that runs out is skipped this window."""
    import asyncio
    now = time.monotonic()
    if not refresh and probes is None:
        hit = _CACHE.get(int(entity_id))
        if hit and now - hit[0] < FAULTS_TTL_S:
            faults = hit[1]
            return [f for f in faults if not areas or f["area"] in areas]
    p = probes or default_probes(pool, entity_id)
    faults: list[dict] = []
    timed_out: list[str] = []
    budget = max(0.1, float(budget_s))
    # All probes run CONCURRENTLY under one wall-clock budget: a slow one
    # (a provider catalog walk) costs its own area, never the others'.
    results = await asyncio.gather(
        *(asyncio.wait_for(probe(pool, entity_id, p), timeout=budget) for _, probe in PROBES),
        return_exceptions=True)
    for (area, _), res in zip(PROBES, results):
        if isinstance(res, asyncio.TimeoutError):
            timed_out.append(area)
        elif isinstance(res, BaseException):
            log.debug("diagnostics probe %s failed (non-fatal): %s", area, res)
        else:
            faults.extend(res or [])
    if timed_out:
        log.info("diagnostics: probes skipped this window (budget %.1fs): %s", budget_s, timed_out)
    order = {"fix": 0, "review": 1, "info": 2}
    faults.sort(key=lambda f: (order.get(f["severity"], 9), f["area"], f["id"]))
    if probes is None:
        _CACHE[int(entity_id)] = (now, faults)
    return [f for f in faults if not areas or f["area"] in areas]


def render_faults_block(faults: list[dict]) -> str | None:
    """The per-turn block. None when nothing is wrong."""
    if not faults:
        return None
    n_fix = sum(1 for f in faults if f["severity"] == "fix")
    lines = [f"## Your machine needs attention ({len(faults)}: {n_fix} with a fix ready)"]
    for f in faults[:MAX_BLOCK_LINES]:
        if f["area"] == "failures":
            tag = "keeps failing"
        else:
            tag = {"fix": "fix ready", "review": "needs a decision", "info": "no configuration fix"}[f["severity"]]
        lines.append(f"- [{tag}] {f['area']}: {f['title']}")
    if len(faults) > MAX_BLOCK_LINES:
        lines.append(f"- …and {len(faults) - MAX_BLOCK_LINES} more")
    lines.append("`diagnose()` returns each item as data: a configuration fault with the exact call "
                 "that fixes it (tool + args), a failure in the provider's or tool's own words. Apply "
                 "the configuration fixes the diagnostic skill lets you apply yourself, ask for the "
                 "rest, and say what you changed. Skill 'diagnostic'.")
    return "\n".join(lines)
