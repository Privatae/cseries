"""runtime/agent_grant.py — the capability GRANT a host mints for one agent.

THE RULE
An agent's capability floor is ZERO. It holds no capability bit until its host
explicitly elevates it, per dispatch, for that one scoped task. Bits are NOT
derived from the tool list: deriving them would make the grant a rubber stamp —
"whatever tools I picked, the bits follow" — and the whole point is that the
host reasons about what the task needs and whether elevation is warranted.

Without this, "you have read-only access, do not write memory" is a SENTENCE IN
A PROMPT and the agent holds WRITE_MEMORY the entire time. `_authorize_meta_tool`
computed `effective_bits = entity_bits` (tools.py) and a dispatched agent
inherited the entity's bits wholesale, so the host's instruction was a request.
With a grant, it is a capability the agent never had.

This is invariant 5 of the original filing in the form that survives without
hardware: an actor cannot self-elevate, because it never held the bits.

  effective_bits = entity_bits & grant_bits

Never wider than the entity (the agent cannot exceed its host), never wider than
the grant (the agent cannot exceed its task). A read floor was considered and
REJECTED: READ_MEMORY is the host's entire memory, and "harmless by default" is
exactly what made it readable. An agent with an empty grant can still reason
about its task prompt and report back — it simply cannot touch anything.

REACHABILITY IS CHECKED AT DISPATCH, NOT MID-RUN
A host that grants a tool but not the bits that tool needs has asked for
something impossible. Finding that out on turn 9 wastes the run and teaches the
host nothing, so `unreachable_tools` is evaluated up front and the dispatch is
refused with the exact tool -> missing-capability mapping.
"""
from __future__ import annotations

import logging

from mastercode.ceigas_capabilities import CAPABILITY_NAMES, Capability

log = logging.getLogger("cseries.mastercode.agent_grant")

# name -> bit, e.g. "write_memory" -> 8. The single vocabulary a host writes in.
NAME_TO_BIT: dict[str, int] = {v: int(k) for k, v in CAPABILITY_NAMES.items()}

# The key the capability gate (tools._authorize_meta_tool, invariant 2) puts on
# its denial envelope, carrying the bits the op required. It is what tells a
# dispatched agent's run loop that a `denied` result was the GRANT wall and
# not an ordinary tool refusal (a path-jail miss, a propose-mode editor, a
# propose-mode editor) the agent can work around. Only a denial carrying
# this key turns the run's verdict to 'blocked'. The gate's other walls — kill switch, invariant,
# unregistered tool, trust quarantine — carry it with value 0: still a wall
# the run could not act through, but no grant would have passed it, so the
# record names the wall and offers no retry grant.
CAP_DENIAL_KEY = "needs_bits"

# The floor. Zero is deliberate — see the module docstring.
GRANT_FLOOR: int = 0

# Two names a host may NEVER write in a grant. `admin` is
# the entity's own domain administration; `shell_exec` gates subprocess/exec.
# Neither is a task an in-container agent is ever dispatched for — code runs
# only on the harness pool, through the host's own doors. Until this set
# existed the skill and the `dispatch` schema SAID "never grantable" while
# `parse_grant` accepted every name in CAPABILITY_NAMES and the typo refusal
# listed both as valid — a sentence in a prompt, which is exactly what the
# grant exists to replace. Found by the Ep06 review, 2026-08-29.
NEVER_GRANTABLE: frozenset[str] = frozenset({"admin", "shell_exec"})

# The vocabulary a host may actually write: the seven the schema offers.
GRANTABLE_NAMES: tuple[str, ...] = tuple(
    n for _, n in sorted(CAPABILITY_NAMES.items(), key=lambda kv: int(kv[0]))
    if n not in NEVER_GRANTABLE)

# The capabilities that change or run something. An agent reads untrusted
# content from outside the platform and reports back; acting on what it read
# is the Host's, after it has reviewed the report (the Host
# calls an agent only for untrusted work, and does in-platform work itself).
# A grant naming one of these is refused at the dispatch door
# (scoped_agent.dispatch_precheck). The names still parse, so the refusal can
# say which write was asked for instead of calling it a typo.
AGENT_WRITE_NAMES: frozenset[str] = frozenset({
    "write_context", "write_memory", "execute_tool", "spawn_bot"})
AGENT_WRITE_BITS: int = 0
for _n in AGENT_WRITE_NAMES:
    AGENT_WRITE_BITS |= NAME_TO_BIT[_n]
del _n

# What an agent's grant may hold: the read capabilities its readers need
# (external_api for the web, GitHub, an MCP server and a filed report;
# read_memory for an inbox message; read_context).
AGENT_GRANT_NAMES: tuple[str, ...] = tuple(
    n for n in GRANTABLE_NAMES if n not in AGENT_WRITE_NAMES)


def write_grant_refusal(grant_bits: int) -> str | None:
    """The door's refusal for a grant holding a write capability, or None."""
    asked = int(grant_bits) & AGENT_WRITE_BITS
    if not asked:
        return None
    return (f"An agent's grant never holds a write capability, and this one names "
            f"{describe(asked)}. An agent reads content from outside the platform and "
            f"reports back; any change that follows is yours, made with operate or your "
            f"build tools after you have read its report. Dispatch again with a grant "
            f"from {', '.join(AGENT_GRANT_NAMES)}. No task was created and the user has "
            f"not been told anything.")


def without_write(names) -> list[str]:
    """`names` less the write capabilities: a retry line never offers one."""
    return [n for n in names if n not in AGENT_WRITE_NAMES]


def describe(bits: int) -> str:
    """Render a bit mask as the capability names a host writes."""
    if not bits:
        return "(none)"
    return ", ".join(n for b, n in sorted(CAPABILITY_NAMES.items(), key=lambda kv: int(kv[0]))
                     if bits & int(b))


def parse_grant(names) -> tuple[int, list[str]]:
    """Capability NAMES -> (bits, unknown). Unknown names never contribute bits.

    Names rather than a bitmask is deliberate: a model reasoning about
    ["read_context", "read_memory"] is reasoning about what it is authorizing;
    a model emitting `3` is not, and a typo'd integer is silently a different
    grant. An unknown name is surfaced, never rounded down silently.

    A name in NEVER_GRANTABLE is returned in `unknown` too — it contributes
    no bits and the dispatch is refused at the door. `grant_refusal` renders
    the two cases apart so a typo and a forbidden name get different words.
    """
    bits = GRANT_FLOOR
    unknown: list[str] = []
    for n in (names or []):
        key = str(n).strip().lower()
        if key in NEVER_GRANTABLE:
            unknown.append(str(n))
        elif key in NAME_TO_BIT:
            bits |= NAME_TO_BIT[key]
        elif key:
            unknown.append(str(n))
    return bits, unknown


def grant_refusal(unknown) -> str:
    """The door's message for a grant `parse_grant` could not accept: the
    forbidden names first (they are a policy refusal, not a typo), then the
    names that do not exist, then the list the host may choose from."""
    forbidden = sorted(n for n in unknown if str(n).strip().lower() in NEVER_GRANTABLE)
    typos = sorted(n for n in unknown if str(n).strip().lower() not in NEVER_GRANTABLE)
    parts: list[str] = []
    if forbidden:
        parts.append(f"dispatch names capabilities an agent can never hold: "
                     f"{', '.join(forbidden)} — code and shell run only on the harness "
                     f"pool through your own build tools, and admin is never delegated")
    if typos:
        parts.append(f"dispatch names capabilities that do not exist: {', '.join(typos)}")
    return "; ".join(parts) + f". Valid: {', '.join(AGENT_GRANT_NAMES)}."


def _op_bits(tool_name: str, args: dict | None) -> int | None:
    """Bits for the SPECIFIC op being called, when it is a declared read-safe one.

    `required_bits` is declared per TOOL as the union of everything its ops can
    do, so a tool spanning search and promote demands WRITE_MEMORY even from an
    agent that only wanted to search. READ_SAFE_OPS declares the read ops; an op
    that is absent, unrecognized or ambiguous returns None and the caller falls
    back to the union. Tightening-only by construction — a missing entry costs a
    narrower grant, never a hole.
    """
    if not args:
        return None
    try:
        from mastercode.runtime.meta_tool_caps import READ_SAFE_OPS, op_param_for
    except Exception:                                          # noqa: BLE001
        return None
    ops = READ_SAFE_OPS.get(tool_name)
    if not ops:
        return None
    raw = args.get(op_param_for(tool_name))
    if not isinstance(raw, str):
        return None
    return ops.get(raw.strip())


def min_required_bits_for(tool_name: str) -> int | None:
    """The CHEAPEST way to use a tool — its narrowest declared op.

    Dispatch-time reachability asks "can this agent use this tool at all?", not
    "can it use every op?". A host granting `context_ingest` with read_memory
    gets a searchable, non-promoting agent — that is a coherent grant and must
    not be refused here. (Since 2026-09-22 a scope of context_ingest ALONE is
    refused earlier, as clerk work, by scoped_agent.clerk_scope_refusal; beside
    a tool that still needs an agent, this is the rule that applies.) Using the union here would refuse exactly the
    decomposition this table exists to enable.
    """
    union = required_bits_for(tool_name)
    if union is None:
        return None
    try:
        from mastercode.runtime.meta_tool_caps import READ_SAFE_OPS
        ops = READ_SAFE_OPS.get(tool_name) or {}
    except Exception:                                          # noqa: BLE001
        ops = {}
    return min([union, *ops.values()]) if ops else union


def required_bits_for(tool_name: str, args: dict | None = None) -> int | None:
    """Bits a tool needs, or None if the tool is unknown to both registries.

    With `args`, resolves the SPECIFIC op when it is a declared read-safe one;
    otherwise returns the tool's union (current behaviour, never looser).

    Tools declare `required_bits` in META_TOOL_CAPS — the single declaration
    since the bot lane retired (2026-08-22). There used to be a second lookup
    here that read a tool's mask out of its bot FILENAME; it resolved exactly
    one live tool (`context`, now inline with its bits in META_TOOL_CAPS).
    None means "not a known tool"; the caller decides (the grant wall in
    scoped_agent already refuses names outside the scope, and
    _authorize_meta_tool default-denies anything unregistered).
    """
    _op = _op_bits(tool_name, args)
    if _op is not None:
        return int(_op)

    try:
        from mastercode.runtime.meta_tool_caps import META_TOOL_CAPS
        spec = META_TOOL_CAPS.get(tool_name)
        if spec is not None:
            return int(spec.get("required_bits") or 0)
    except Exception as e:                                     # noqa: BLE001
        log.debug("agent_grant: META_TOOL_CAPS lookup failed for %s: %s", tool_name, e)

    return None


def unreachable_tools(tool_scope, grant_bits: int) -> dict[str, str]:
    """{tool: "missing write_memory"} for every granted tool the grant can't run.

    A tool whose required bits are not a subset of the grant would deny on every
    call. That is the host asking for contradictory things, and it is far better
    to say so at dispatch than to let the agent burn its budget discovering it.

    Tools of unknown provenance are NOT reported here — they are the grant
    wall's business (scoped_agent refuses anything outside tool_scope) and the
    meta-tool registry's (default-deny for unregistered names). Reporting them
    would turn an unrelated registry gap into a dispatch failure.
    """
    out: dict[str, str] = {}
    for t in sorted(set(tool_scope or [])):
        need = min_required_bits_for(t)
        if need is None:
            continue
        missing = need & ~grant_bits
        if missing:
            out[t] = f"missing {describe(missing)}"
    return out


def retry_grant(tool_scope, grant_bits: int) -> list[str]:
    """The grant that WOULD compose this same tool_scope: every name already
    granted, PLUS the capabilities the scope is missing.

    Strictly ADDITIVE — nothing the host asked for is taken away. The refusal
    already computes the missing bits per tool; handing back only the diagnosis
    left the host to re-derive the answer from prose, and it re-derived it
    wrong: round 2 of a turn came back
    "ALL 1 tools failed — dispatch(error): ... You granted tools whose
    capabilities you did not grant: manage_site (missing write_context)".
    A whole round bought nothing. This is the line the host can act on
    mechanically.

    OR-ing over the WHOLE scope is the same set as OR-ing the missing bits
    alone — a reachable tool's requirement is already a subset of the grant, so
    it contributes nothing. Tools of unknown provenance contribute nothing
    either (min_required_bits_for → None), matching unreachable_tools.
    """
    bits = int(grant_bits)
    for t in sorted(set(tool_scope or [])):
        need = min_required_bits_for(t)
        if need is not None:
            bits |= int(need)
    return [n for b, n in sorted(CAPABILITY_NAMES.items(),
                                 key=lambda kv: int(kv[0]))
            if bits & int(b)]


def effective_bits(entity_bits: int, grant_bits: int | None) -> int:
    """The mask a dispatched agent actually acts under.

    `grant_bits is None` means "not an agent" — the host itself, or an internal
    flow acting AS the entity. Those keep the entity's bits, which is correct
    and is NOT the narrowing that was removed in 2026-05-24: that change was
    about WHERE a conversation happens (an app surface), and rightly concluded
    capability is about WHO is acting. A dispatched agent is a different WHO.
    """
    if grant_bits is None:
        return int(entity_bits)
    return int(entity_bits) & int(grant_bits)


__all__ = [
    "Capability", "NAME_TO_BIT", "GRANT_FLOOR", "NEVER_GRANTABLE", "GRANTABLE_NAMES",
    "AGENT_WRITE_NAMES", "AGENT_WRITE_BITS", "AGENT_GRANT_NAMES",
    "describe", "parse_grant", "grant_refusal", "write_grant_refusal", "without_write",
    "required_bits_for", "min_required_bits_for", "unreachable_tools",
    "retry_grant", "effective_bits",
]
