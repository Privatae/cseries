"""
runtime/tool_call_ledger.py — ONE row per tool call the model made, with the
model that made it and WHY the call went the way it did.

The gap this closes: every tool call already passes a gate that
knows exactly what happened — a name the registry has never heard of, a name
hidden from this surface, a capability the caller lacks, arguments that did
not parse — and every one of those verdicts came back to the model as
`status='denied'` or `status='error'` with a sentence, and to the audit log
as a generic denial row that does not say which MODEL made the call. So the
one number the routing decision needs — "how often does <model> in <role>
invent a tool" — was not derivable from anything the pod keeps. Experience
shows that a mis-wired tool surface makes a host hallucinate tool
calls; that behavior is what the admission bench measures, and this ledger
is where it is counted on live traffic.

How reasons are produced. The gates STAMP a machine-readable `reason` on the
envelope they return (runtime/tools.py `_authorize_meta_tool`,
`_execute_meta_tool`; runtime/session.py's own refusals; scoped_agent.py's
grant wall). `classify()` reads that stamp and falls back to the status only
when no gate spoke. Nothing here parses the sentence the model was shown —
those are prose and they change.

Two writers, one shape: the host turn (lane 'host') and a dispatched agent
(lane 'agent'). Both stamp role 'chat' — since the three-role cut
 that is the only route either runs on, and `lane` is what tells
them apart. Historical rows still carry 'tutor' and 'agents'; nothing
rewrites them. Both call `record_later()`,
which schedules the INSERT as its own task so the tool path never waits on
the brain pool for it (an inline await is one extra round-trip per tool call
on a 6-connection pool, and "never blocks" would not be true). `record()` is the awaitable half, for callers that
want the row landed before they continue (tests, the bench).

`rollup()` is the read the routing tool serves under `measured`: per (role,
provider, model, lane), how many tool calls and how many of each reason; and,
SEPARATELY per (provider, model), the inference ledger's own error count,
latency and estimated cost. The two are not joined onto one row on purpose —
inference_cost_log has no role, so one model serving two roles would carry
its cost twice and a reader summing the list would double it. Arithmetic over
rows; no threshold lives here.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

log = logging.getLogger("cseries.tool_call_ledger")

# The vocabulary. `reason` on a row is always one of these; a CHECK on the
# table pins it, so a new reason is a migration and a test, not a typo.
OK = "ok"
ERROR = "error"                    # executed, the tool itself failed
UNKNOWN_NAME = "unknown_name"      # no registry knows this name — invented
NOT_ADVERTISED = "not_advertised"  # a real tool, but not in the list this caller was handed
BAD_ARGS_JSON = "bad_args_json"    # the arguments string did not parse
ARGS_SCHEMA = "args_schema"        # parsed, but required fields are missing
SURFACE_DENIED = "surface_denied"  # real tool, wrong surface (workbench-only, etc.)
CAPABILITY_DENIED = "capability_denied"  # the CEIGAS bits wall
OUT_OF_GRANT = "out_of_grant"      # a dispatch reached past its grant (door or run-time wall)
POLICY_DENIED = "policy_denied"    # kill switch / trust floor / fail-closed check
BLOCKED = "blocked"                # ephemeral-context write block, code-exec wall
DEDUP = "dedup"                    # refused as a third identical call this turn/run (host lane caches the second; agent lane reruns the second only after an error)

REASONS = (
    OK, ERROR, UNKNOWN_NAME, NOT_ADVERTISED, BAD_ARGS_JSON, ARGS_SCHEMA,
    SURFACE_DENIED, CAPABILITY_DENIED, OUT_OF_GRANT, POLICY_DENIED, BLOCKED, DEDUP,
)

# Reasons that mean the MODEL produced a call the surface could not take —
# the admission bench's headline metric. A denial for capability or policy
# is the platform's wall doing its job and says nothing about the model.
MODEL_FAULT_REASONS = frozenset({UNKNOWN_NAME, NOT_ADVERTISED, BAD_ARGS_JSON, ARGS_SCHEMA})

# The error lane's FAULT CLASS. Every failed
# Host tool call now lands in error_events; most failures are the model's own
# outcome — a refusal at a gate, bad or missing arguments, an invented name, a
# dedup refusal, a call the output limit cut (BLOCKED, status "truncated").
# Only ERROR — the tool ran and failed: it raised, a dependency was down, our
# code timed out — is the platform's. The ship gate's error baselines
# count only `platform` rows; a model outcome is not a platform
# fault and must never block a roll.
PLATFORM_FAULT_REASONS = frozenset({ERROR})

# A tool that REFUSES the call's own input — a path that is not there, a field
# its op needs, a path outside its jail — answers with status "error" exactly
# as a tool that broke does, so classify() reads both as ERROR (it never reads
# the sentence). The tool says which it was with this key on its result.
# Measured in a live test run: workspace_file "file not found" on a
# name the model chose, "path is required for op='write'" (the model sent
# `filePath`) and "path traversal blocked" were all filed as PLATFORM faults
# and failed errors_clean on both Hosts. Only the fault class reads this key;
# `reason` stays what classify() says, so the ledger and its readers are
# unchanged.
INPUT_REFUSED = "input_refused"


def refused(msg, **extra) -> dict:
    """THE one way a tool refuses the call's own input or a precondition the
    model can see and fix — a missing or invalid argument, an op its tool
    does not have, a name the model gave that is not there, a thing in the
    wrong state for what was asked — having done no work. Marked
    INPUT_REFUSED, so the error lane files it as the model's outcome
    (fault_class), never as a platform fault. A failure of the tool itself (it
    raised, a dependency was down, it timed out) is never returned through
    here and stays 'platform'.

    Measured in a live test run: `build` refusing a phase still in
    planning, `manage_site` publish with no slug or bundle_path, a bundle path
    that was not there, `publish_app` with no op and `project_phase` with no
    project_id were all filed as platform faults and failed errors_clean on
    both Hosts. This is the mark for every tool."""
    return {"status": "error", "result": msg, **extra, INPUT_REFUSED: True}


def refused_if_caller(err, msg, **extra) -> dict:
    """The answer for a typed service error that carries the HTTP status its
    route would answer with (ShipError, PublisherError, TicketError,
    ArtifactError, ContextError, HTTPException …). A 4xx other than 429 is
    the service refusing what the caller sent — refused(), the model's
    outcome. A 5xx, a 429 (held off, not wrong) or no status is a failure of
    the platform, answered unmarked. The words are the same either way.

    The class-wide sort: `publish_project_service`
    raises ShipError(400, "this bundle declares no app id") and
    ShipError(403, "publish refused (licence)"), and the publish lanes caught
    them as plain errors — filed 'platform', the same shape again."""
    if type(err).__module__.split(".")[0] not in _OWN_ERROR_MODULES:
        # A dependency's own error (an HTTP client's 401 because OUR token is
        # wrong) carries a status too, and that 4xx is the platform's.
        return {"status": "error", "result": msg, **extra}
    _mark = getattr(type(err), "caller_refusal", None)
    if _mark is True or (_mark is None and isinstance(err, ValueError)
                         and type(err) is not ValueError):
        # Our own ValueError subclasses (ManifestError, PipelineError,
        # SpecError, BundleError, PlanError …) are raised only to refuse what
        # the caller sent; a class that is not a ValueError says so with
        # `caller_refusal = True`. Measured in a live test run: a refusal raised as our
        # own error reached the tool through a broad handler and was filed
        # 'platform'. `caller_refusal = False` opts a class out (a check of
        # the platform's own data that happens to be a ValueError).
        return refused(msg, **extra)
    st = getattr(err, "status", None)
    if not isinstance(st, int):
        st = getattr(err, "status_code", None)
    if isinstance(st, int) and 400 <= st < 500 and st != 429:
        return refused(msg, **extra)
    return {"status": "error", "result": msg, **extra}


# Where an error's status is the platform's own word about the CALLER: our
# services, and the HTTPException our routes raise.
_OWN_ERROR_MODULES = frozenset({"server", "mastercode", "fastapi", "starlette"})


def fault_class(reason: str | None, result=None) -> str:
    """'platform' for a call that ran and failed, 'model' for every other
    failure reason (refusals, arguments, names, dedup, a cut) — and for an
    ERROR whose tool marked it a refusal of the call's own input
    (INPUT_REFUSED)."""
    if (reason or ERROR) not in PLATFORM_FAULT_REASONS:
        return "model"
    if isinstance(result, dict) and result.get(INPUT_REFUSED) is True:
        return "model"
    return "platform"


# Failure-shaped statuses tools answer with that are not the four failure
# words — mapped explicitly, found by
# reading every `"status": "…"` literal in brain/mastercode/runtime:
#   rate_limited  github_lane.py (:296, :593) — the provider held us off
#   failed        agent_dispatch.py:619 — dispatch_sync's run raised
#   ungrounded    web_research.py:376 — a search that could not prove a source
STATUS_ALIASES = {"rate_limited": "unavailable", "failed": "error", "ungrounded": "error"}

# The statuses that are failures (the error lane's list, server/error_log).
FAILURE_STATUSES = frozenset({"error", "denied", "blocked", "unavailable"})

# The rollup's neutral bucket: a call whose status is a tool's own word for
# an outcome ("dispatched", "scheduled", "not_found", "queued", "cancelled" …)
# — not a failure, and not counted as a plain success either.
OTHER = "other"


def normalize_status(status) -> str:
    """A result status in the failure vocabulary where it is failure-shaped
    (STATUS_ALIASES), else as given."""
    st = str(status or "ok")
    return STATUS_ALIASES.get(st, st)


LANES = ("host", "agent")

_ARGS_PREVIEW_CAP = 1500

# Tasks scheduled by record_later(), held until done so the event loop cannot
# garbage-collect a pending INSERT (asyncio keeps only weak references).
_PENDING: set[asyncio.Task] = set()


def known_tool_schema(name: str) -> dict | None:
    """The schema for a tool name from EVERY registry a host can hold —
    the three `_find_tool_schema` walks plus the platform host's own
    (OPERATOR_PLATFORM_TOOL_SCHEMAS: lab_run, bug_review, support_collect).

    `_find_tool_schema` is the surface gate's lookup and deliberately does
    not know the platform schemas (session.py's prefire walk says so in its
    own words). For the ledger the question is different — "does ANY
    registry know this name" — and answering it from three of four
    registries would count the operator's Host naming `lab_run` as an invented tool.
    """
    try:
        from mastercode.runtime.tools import _find_tool_schema
        schema = _find_tool_schema(name)
        if schema is not None:
            return schema
    except Exception:  # noqa: BLE001 — a lookup failure is not a finding
        pass
    try:
        from mastercode.public_tool_registry import OPERATOR_PLATFORM_TOOL_SCHEMAS
        for schema in OPERATOR_PLATFORM_TOOL_SCHEMAS.values():
            if (schema.get("function") or {}).get("name") == name:
                return schema
    except Exception:  # noqa: BLE001
        pass
    # The host's own orchestration verbs — dispatch, operate, recall,
    # agent_status, escalate — are built inline by session.py and live in no
    # registry. They are the highest-traffic tools on the surface, so a
    # required-field check that skipped them would be blind where it matters
    # most. Imported lazily: session.py imports
    # this module's callers, never the other way round at import time.
    try:
        from mastercode.runtime.session import _build_host_orchestration_tools
        for schema in _build_host_orchestration_tools():
            if (schema.get("function") or {}).get("name") == name:
                return schema
    except Exception:  # noqa: BLE001
        pass
    return None


def schema_errors(name: str, args: Any) -> list[str]:
    """Required-field check of `args` against the tool's own JSON schema.

    Only `required` is checked. Type and enum mismatches are real defects too,
    but every executor already answers those with its own error sentence, so
    counting them here would double-count what `status='error'` already
    records. A missing required field, by contrast, is the shape of call a
    model makes when it is guessing at a tool it half-remembers — and most
    executors fill a default and carry on, so nothing else would count it.

    Returns [] for an unknown tool (there is no schema to check against;
    that case is UNKNOWN_NAME, not ARGS_SCHEMA) and for a schema with no
    `required` list.
    """
    schema = known_tool_schema(name)
    if not isinstance(schema, dict):
        return []
    params = ((schema.get("function") or {}).get("parameters") or {})
    required = params.get("required") or []
    if not required:
        return []
    if not isinstance(args, dict):
        return [f"arguments must be an object, got {type(args).__name__}"]
    return [f"missing required field '{k}'" for k in required if k not in args]


def classify(result: Any, *, args_parsed: bool = True,
             schema_missing: list[str] | None = None) -> str:
    """The reason for one call, from the gate's stamp first, the status second.

    `args_parsed=False` wins over everything: an unparseable arguments string
    is the model's fault before any gate saw the call. `schema_missing` only
    matters on a call that otherwise succeeded or errored — a denied call
    never reached an executor, so its arguments were never the problem.
    """
    if not args_parsed:
        return BAD_ARGS_JSON
    if not isinstance(result, dict):
        return OK
    stamped = result.get("reason")
    if isinstance(stamped, str) and stamped in REASONS:
        return stamped
    status = normalize_status(result.get("status"))
    if status == "ok":
        return ARGS_SCHEMA if schema_missing else OK
    if status == "error":
        if result.get("dedup_block"):
            return DEDUP
        return ARGS_SCHEMA if schema_missing else ERROR
    if status == "blocked":
        return BLOCKED
    if status == "denied" or status.startswith("denied_"):
        # A gate that did not stamp. CAP_DENIAL_KEY carrying bits is the
        # capability wall by contract (agent_grant.py); 0 is one of the other
        # walls in the same gate.
        try:
            from mastercode.runtime.agent_grant import CAP_DENIAL_KEY
            needs = result.get(CAP_DENIAL_KEY)
        except Exception:  # noqa: BLE001
            needs = None
        if needs:
            return CAPABILITY_DENIED
        return POLICY_DENIED
    if status == "unavailable":
        # A dependency was not there to answer: the platform's (the error
        # lane's own failure vocabulary, server/error_log).
        return ERROR
    # Any other status is a tool's own word for an outcome that is not a
    # failure — "dispatched", "scheduled", "not_found", "running". Read as
    # ERROR, a background agent that started fine would be ledgered and
    # error-laned as a platform fault.
    return OK


#: Argument names that carry a secret whatever tool they arrive on. The ledger
#: keeps a preview of every tool call's args, so without this a credential
#: handed to a tool is written to `tool_call_log.args` in the clear and stays
#: there. `keys_manage op='add'` has always taken the secret as `value`; the
#: integrations `set_client` op takes `client_secret` the same way. Neither
#: needs its secret in a log to be debuggable — the KEY NAME is the useful part,
#: and that survives.
#:
#: Matched case-insensitively on the exact name, not by substring: a substring
#: rule would redact innocent fields (`key`, `token_count`) and teach nobody
#: anything. Add a name here when a tool starts taking one.
_SECRET_ARG_NAMES = frozenset({
    "client_secret", "api_key", "secret", "password", "passphrase",
    "private_key", "refresh_token", "access_token", "totp_secret",
    # mesh_harness_run mode='service' mirror: the Host's credential for its own server.
    "push_token",
})

#: Names that are secret only on a tool whose JOB is credentials. `value` is
#: the secret on `keys_manage op='add'` and ordinary data everywhere else — the
#: `preference` tool's value is "warmer". Redacting it globally would cost the
#: ledger its diagnostic worth on every tool that happens to use the name, so
#: it is scoped instead of blanket-applied.
_CREDENTIAL_TOOLS = frozenset({"keys_manage", "integrations_manage",
                               "connect_compute", "credential_wiring"})
_CREDENTIAL_ARG_NAMES = frozenset({"value", "value_b64", "credential", "bearer"})
_REDACTED = "[redacted]"


def _scrub(obj: Any, names: frozenset[str], depth: int = 0) -> Any:
    """Replace values whose KEY names a secret with a marker, at any depth."""
    if depth > 6:
        return obj
    if isinstance(obj, dict):
        return {k: (_REDACTED if isinstance(k, str) and k.lower() in names
                    else _scrub(v, names, depth + 1))
                for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_scrub(v, names, depth + 1) for v in obj]
    return obj


_DECLARED: dict[str, frozenset] = {}


def _declared_keys(tool: str | None) -> frozenset:
    """Every property name the tool's own schema declares, at any depth
    (items.properties included). A free-form map declares none."""
    if not tool:
        return frozenset()
    if tool not in _DECLARED:
        names: set = set()
        try:
            from mastercode import public_tool_registry as _reg
            schema = (_reg.PUBLIC_TOOL_SCHEMAS.get(tool)
                      or _reg.OPERATOR_PLATFORM_TOOL_SCHEMAS.get(tool) or {})

            def _walk(props):
                for name, spec in (props or {}).items():
                    names.add(name)
                    spec = spec or {}
                    _walk(spec.get("properties"))
                    _walk((spec.get("items") or {}).get("properties"))
            _walk(((schema.get("function") or {}).get("parameters") or {}).get("properties"))
        except Exception:  # noqa: BLE001 — no schema: no key is kept
            names = set()
        _DECLARED[tool] = frozenset(names)
    return _DECLARED[tool]


def _shape(obj: Any, keep: frozenset = frozenset(), depth: int = 0) -> Any:
    """PII mode: the arguments' shape without their words. Kept: a key the
    tool's schema declares, true/false, nulls, whole numbers under seven
    digits and decimals of at most two places. Replaced by their length:
    every string, every other key (a free-form map's keys are whatever the
    model wrote — a name), and every other number (a phone, an account, a
    coordinate)."""
    if depth > 6:
        return None
    if isinstance(obj, dict):
        # A hidden key is numbered, so two of one length never collapse.
        return {(k if isinstance(k, str) and k in keep else f"<key {i}: {len(str(k))} chars>"):
                _shape(v, keep, depth + 1) for i, (k, v) in enumerate(obj.items())}
    if isinstance(obj, (list, tuple)):
        return [_shape(v, keep, depth + 1) for v in obj]
    if isinstance(obj, str):
        return f"<{len(obj)} chars>"
    if obj is None or isinstance(obj, bool):
        return obj
    if isinstance(obj, int):
        digits = len(str(abs(obj)))
        return obj if digits < 7 else f"<{digits} digits>"
    if isinstance(obj, float):
        if obj == obj and abs(obj) < 1e6 and round(obj, 2) == obj:
            return obj
        return "<number>"
    return f"<{type(obj).__name__}>"


def _preview(args: Any, tool: str | None = None) -> str:
    names = _SECRET_ARG_NAMES
    # operate(tool=X, args={...}) is logged under `operate`; the credential
    # tool it carries is X. keys_manage and connect_compute run through
    # operate, so judging the verb alone would keep the
    # sealed value of op='add' in the clear.
    target = tool
    if tool in ("operate", "read") and isinstance(args, dict) and isinstance(args.get("tool"), str):
        target = args["tool"].strip()
    if tool in _CREDENTIAL_TOOLS or target in _CREDENTIAL_TOOLS:
        names = names | _CREDENTIAL_ARG_NAMES
    try:
        # PII mode keeps no text as the model wrote it (pii_queue): this row
        # would hold every tool's words — a memory, a message, a search — so
        # it keeps only their shape.
        from mastercode import pii_queue as _pii
        pii = _pii.enabled()
    except Exception:  # noqa: BLE001 — cannot tell: keep nothing
        return '"<args withheld>"'
    if pii:
        try:
            return json.dumps(_shape(args, _declared_keys(tool)), default=str,
                              sort_keys=True)[:_ARGS_PREVIEW_CAP]
        except Exception:  # noqa: BLE001 — in PII mode a failure keeps nothing, never the raw words
            return '"<args withheld>"'
    try:
        s = json.dumps(_scrub(args, names), default=str, sort_keys=True)
    except Exception:  # noqa: BLE001
        # A value that will not serialise must not fall back to str(args) —
        # that is the raw object, secrets and all, which is the exact leak the
        # scrub exists to stop. Say the shape instead. _scrub runs inside this
        # try for the same reason: a dict whose .items() raises must not escape
        # into a bare repr.
        s = f"<unserialisable {type(args).__name__}>"
    return s[:_ARGS_PREVIEW_CAP]


# projects.id is a SERIAL (INTEGER): the largest id a project can have.
_PROJECT_ID_MAX = 2**31 - 1


def _project_id(args: Any) -> int | None:
    """The project a call named, for tool_call_log.project_id, or None.

    The args are the model's own, so the value can be anything. Only a whole
    number that can be a projects.id (1 .. 2^31-1) is stamped: anything wider
    fails the INTEGER bind, and record() would lose the whole row, which is
    the one audit of the call. A bool is not a project id (int(True) is 1)."""
    if not isinstance(args, dict):
        return None
    v = args.get("project_id")
    if isinstance(v, bool) or v is None or v == "":
        return None
    if isinstance(v, float) and not v.is_integer():
        return None
    try:
        n = int(v)
    except (TypeError, ValueError, OverflowError):
        return None
    return n if 1 <= n <= _PROJECT_ID_MAX else None


async def record(pool, *, entity_id: int, lane: str, role: str | None,
                 provider: str | None, model: str | None, tool: str,
                 reason: str, status: str, session_id: str | None = None,
                 latency_ms: int | None = None, args: Any = None,
                 schema_missing: list[str] | None = None) -> None:
    """Land one row and return when it has landed. Never raises.

    `model` is the SERVED model — whoever actually answered the round that
    produced this call — not the routed one (session.py `_served`): a failover
    moves the model mid-turn and the call must be scored against the model
    that made it.
    """
    if pool is None:
        return
    if reason not in REASONS:
        log.warning("tool_call_ledger: unknown reason %r for %s — recorded as error", reason, tool)
        reason = ERROR
    if lane not in LANES:
        lane = "host"
    try:
        project_id = _project_id(args)
        await pool.execute(
            "INSERT INTO tool_call_log "
            "(entity_id, session_id, lane, role, provider, model, tool, reason, "
            "status, latency_ms, schema_errors, args, project_id) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, $12::jsonb, $13)",
            int(entity_id), session_id, lane, role, provider, model,
            str(tool)[:200], reason, str(status)[:32],
            int(latency_ms) if latency_ms is not None else None,
            json.dumps(list(schema_missing or [])),
            json.dumps({"preview": _preview(args, tool)}),
            project_id,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("tool_call_log write failed (non-fatal): %s", e)
        return
    # The canary on a route switch (runtime/route_canary): a disqualifying
    # fault inside the window after a switch reverts the role's route. Runs
    # here, already off the turn's path, and only reads the route ledger
    # when the reason is one that can revert.
    try:
        from mastercode.runtime.route_canary import observe as _canary_observe
        await _canary_observe(pool, entity_id, role=role, provider=provider, model=model, reason=reason)
    except Exception as e:  # noqa: BLE001
        log.debug("route canary skipped (non-fatal): %s", e)


def record_later(pool, **kw) -> asyncio.Task | None:
    """Schedule `record(**kw)` as its own task and return at once.

    This is what the tool loops call: the INSERT runs beside the turn, not in
    it, so a saturated brain pool delays the ledger row and never the next
    tool call. The task is held in _PENDING until it finishes. With no
    running loop (a synchronous test, a shutdown path) nothing is scheduled
    and None is returned — a ledger row is never worth a raise.
    """
    if pool is None:
        return None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    task = loop.create_task(record(pool, **kw))
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)
    return task


async def flush_pending() -> None:
    """Wait for every scheduled row to land (tests, the bench's end-of-run)."""
    if _PENDING:
        await asyncio.gather(*list(_PENDING), return_exceptions=True)


async def rollup(pool, entity_id: int, days: int = 7) -> dict:
    """Per (role, provider, model, LANE): tool calls and reason counts from
    this ledger. Per (provider, model), SEPARATELY: the inference ledger's
    counts for the same window.

    Shape:
      {"window_days": 7,
       "models": [{"role", "provider", "model", "lane", "tool_calls",
                   "model_fault", "reasons": {reason: n}}],
       "inference": [{"provider", "model", "calls", "errors",
                      "avg_latency_ms", "est_cost_usd"}]}

    LANE is part of the key, not decoration. Both writers stamp
    role 'chat' since a dispatched agent runs on the host's own route, so
    grouping by role alone would fold every agent's tool calls into the host's
    own row — the host would read one number and be unable to tell "my turns
    are fine, my agents are inventing tool names" from "everything is fine".
    That distinction is the whole reason this rollup is the evidence a route
    suggestion cites, so lane splits the rows and travels with them.

    `model_fault` is the count of MODEL_FAULT_REASONS — the number the bench
    and the routing status both read. No rate is computed here: a rate with no
    denominator shown is the "zero that looks like data" this codebase keeps
    refusing, so both numbers travel and the reader divides. The inference
    block is its own list for the same reason: a model serving two roles has
    ONE cost, and stapling it onto each role row would let a reader sum it
    twice.
    """
    out: dict = {"window_days": int(days), "models": [], "inference": []}
    if pool is None:
        return out
    try:
        rows = await pool.fetch(
            # A row classified 'ok' whose status is a tool's own outcome word
            # (dispatched, scheduled, not_found …) is counted in the neutral
            # OTHER bucket, never as a plain success.
            # (Grouped on the raw reason plus whether the status is 'ok'; the
            # CASE is built from those two grouped expressions only, and the
            # loop below sums the buckets.)
            "SELECT lane, role, provider, model, "
            "CASE WHEN reason = 'ok' AND (status <> 'ok') THEN 'other' ELSE reason END "
            "AS bucket, count(*) AS n "
            "FROM tool_call_log "
            "WHERE entity_id = $1 AND at > now() - make_interval(days => $2) "
            "GROUP BY lane, role, provider, model, reason, (status <> 'ok')",
            int(entity_id), int(days))
    except Exception as e:  # noqa: BLE001
        log.debug("tool_call_log rollup unavailable: %s", e)
        rows = []
    by_key: dict[tuple, dict] = {}
    for r in rows or []:
        key = (r["role"], r["provider"], r["model"], r["lane"])
        ent = by_key.setdefault(key, {
            "role": r["role"], "provider": r["provider"], "model": r["model"],
            "lane": r["lane"],
            "tool_calls": 0, "model_fault": 0, "reasons": {},
        })
        n = int(r["n"] or 0)
        ent["tool_calls"] += n
        _bucket = r.get("bucket") or r.get("reason")
        ent["reasons"][_bucket] = ent["reasons"].get(_bucket, 0) + n
        if _bucket in MODEL_FAULT_REASONS:
            ent["model_fault"] += n
    out["models"] = sorted(by_key.values(),
                           key=lambda e: (-e["tool_calls"], str(e["role"]),
                                          str(e["model"]), str(e["lane"])))

    # The inference ledger, same window, per model. `inference_error` rows are
    # the failed calls (provider_gateway._log_call_failure); every other
    # purpose is a call that landed. Latency and cost are over landed calls
    # only — a failed call bought nothing and took however long the timeout was.
    try:
        irows = await pool.fetch(
            "SELECT model_id, provider, "
            "  count(*) FILTER (WHERE purpose IS DISTINCT FROM 'inference_error') AS calls, "
            "  count(*) FILTER (WHERE purpose = 'inference_error') AS errors, "
            "  avg(latency_ms) FILTER (WHERE purpose IS DISTINCT FROM 'inference_error') AS avg_latency_ms, "
            "  sum(est_cost_usd) FILTER (WHERE purpose IS DISTINCT FROM 'inference_error') AS est_cost_usd "
            "FROM inference_cost_log "
            "WHERE entity_id = $1 AND created_at > now() - make_interval(days => $2) "
            "GROUP BY model_id, provider",
            int(entity_id), int(days))
    except Exception as e:  # noqa: BLE001
        log.debug("inference_cost_log rollup unavailable: %s", e)
        irows = []
    inf = []
    for r in irows or []:
        inf.append({
            "provider": r["provider"], "model": r["model_id"],
            "calls": int(r["calls"] or 0),
            "errors": int(r["errors"] or 0),
            "avg_latency_ms": int(r["avg_latency_ms"]) if r["avg_latency_ms"] is not None else None,
            "est_cost_usd": round(float(r["est_cost_usd"]), 6) if r["est_cost_usd"] is not None else None,
        })
    out["inference"] = sorted(inf, key=lambda e: (-(e["calls"] + e["errors"]), str(e["model"])))
    return out
