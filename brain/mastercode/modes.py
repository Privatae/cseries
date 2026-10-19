"""
Session modes — the surface-declared context that gates retrieval, codebook
overlays, and dispatch behavior.

The MODE IS DECLARED BY THE API SURFACE that creates the session, not inferred
from message content. The desktop chat bubble creates `chat` sessions; the
workspace project view creates `orchestrator` sessions; the memory app creates
`maintenance` sessions; etc. Spawned agents inherit the parent's mode through
the dispatch envelope explicitly (no thread-locals, no implicit propagation).

This file is the single source of truth for:
  - The mode taxonomy (Mode enum)
  - Per-mode rules consumed by retrieval, codebook overlays, and dispatch
  - The dispatch envelope helper for propagation into spawned agents

Adding a new mode = add to Mode + add an entry to MODE_RULES. Nothing else
in the codebase should hardcode mode names; always go through this module.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Mode(str, Enum):
    """Surface-declared session mode."""
    CHAT = "chat"                    # desktop bubble; conversational, personality-led
    ORCHESTRATOR = "orchestrator"    # workspace project view; decomposing/dispatching
    RESEARCH = "research"            # workspace research panel; fork-don't-build
    MAINTENANCE = "maintenance"      # memory app; admin operations on the substrate
    BENCHMARK = "benchmark"          # benchmark runs; minimal context, deterministic
    DISPATCH_AGENT = "dispatch_agent"  # spawned worker; clean slate with parent's task


DEFAULT_MODE = Mode.CHAT


@dataclass(frozen=True)
class ModeRules:
    """Rules a mode imposes on retrieval / codebook / dispatch."""
    # Memory tier filter: which `metadata.memory_tier` values to load (in addition
    # to rows without any tier set). Empty list = no filter; load all.
    tier_filter: tuple[int, ...] = ()
    # Memory types whose presence boosts retrieval score.
    type_emphasis: tuple[str, ...] = ()
    # Memory types excluded from retrieval entirely.
    type_deny: tuple[str, ...] = ()
    # Reserved mode flag (model routing); no active consumer since MCB5.
    routing_enabled: bool = False
    # Whether CEIGAS should grant outbound network egress in this mode.
    egress_enabled: bool = False
    # Whether personality wrapping is applied to LLM calls in this mode.
    personality_active: bool = True


MODE_RULES: dict[Mode, ModeRules] = {
    # Chat is the default casual conversation mode. Identity + task tier
    # memories load; policy memories (model routing, dispatch templates) do not.
    Mode.CHAT: ModeRules(
        tier_filter=(0, 2),
        type_emphasis=("relationship", "emotion", "preference", "fact"),
        type_deny=(),
        routing_enabled=False,
        egress_enabled=False,
        personality_active=True,
    ),
    # Orchestrator runs at project scope. Constitutional + decision memories
    # surface for routing and dispatch templates.
    # skill_memory rows do NOT surface here — skills feed the SDK harness
    # via skill_loader. Entity prompt stays narrow.
    Mode.ORCHESTRATOR: ModeRules(
        tier_filter=(0, 1, 2),
        type_emphasis=("constitutional", "decision"),
        type_deny=(),
        routing_enabled=True,
        egress_enabled=True,
        personality_active=True,
    ),
    # Research mode strips personality wrapping; emphasizes topic/fact/knowledge
    # so search finds aren't drowned by chat memories.
    Mode.RESEARCH: ModeRules(
        tier_filter=(0, 2),
        type_emphasis=("topic", "fact", "knowledge"),
        type_deny=("relationship", "emotion"),
        routing_enabled=False,
        egress_enabled=True,  # web search needs egress
        personality_active=False,
    ),
    # Memory app — admin/debug only. Identity guards stay loaded; nothing else.
    # No personality, no routing, no egress.
    Mode.MAINTENANCE: ModeRules(
        tier_filter=(0,),
        type_emphasis=(),
        type_deny=(),
        routing_enabled=False,
        egress_enabled=False,
        personality_active=False,
    ),
    # Benchmark runs need deterministic, tight context. Identity only.
    Mode.BENCHMARK: ModeRules(
        tier_filter=(0,),
        type_emphasis=(),
        type_deny=("relationship", "emotion"),
        routing_enabled=False,
        egress_enabled=False,
        personality_active=False,
    ),
    # Spawned agents get the parent's task plus identity guards. The dispatch
    # envelope can override these on a per-spawn basis. Skills load via
    # skill_loader.find_matching_skills() at harness invocation, not via
    # entity_memory type_emphasis.
    Mode.DISPATCH_AGENT: ModeRules(
        tier_filter=(0, 2),
        type_emphasis=(),
        type_deny=("relationship", "emotion"),
        routing_enabled=False,
        egress_enabled=False,
        personality_active=False,
    ),
}


def normalize_mode(mode) -> Mode:
    """Coerce a Mode enum or string into a Mode. Falls back to DEFAULT_MODE."""
    if isinstance(mode, Mode):
        return mode
    if mode is None:
        return DEFAULT_MODE
    try:
        return Mode(mode)
    except (ValueError, TypeError):
        return DEFAULT_MODE


def rules_for(mode) -> ModeRules:
    """Get the rules applicable to a mode."""
    return MODE_RULES[normalize_mode(mode)]


# ---------------------------------------------------------------
# Dispatch envelope — propagation to spawned agents
# ---------------------------------------------------------------

@dataclass(frozen=True)
class DispatchEnvelope:
    """
    What an orchestrator passes to a spawned agent. The child reads these to
    set up its own session. Mode propagation is EXPLICIT — the parent declares
    what mode the child runs in. No implicit inheritance.
    """
    parent_session_id: str
    parent_mode: Mode
    child_mode: Mode
    parent_entity_id: int
    project_id: Optional[int] = None
    task_id: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "parent_session_id": self.parent_session_id,
            "parent_mode": self.parent_mode.value,
            "child_mode": self.child_mode.value,
            "parent_entity_id": self.parent_entity_id,
            "project_id": self.project_id,
            "task_id": self.task_id,
        }


def make_envelope(
    parent_session,
    child_mode=Mode.DISPATCH_AGENT,
    project_id: Optional[int] = None,
    task_id: Optional[str] = None,
) -> DispatchEnvelope:
    """Build an envelope from a parent session for a child agent."""
    return DispatchEnvelope(
        parent_session_id=getattr(parent_session, "session_id", ""),
        parent_mode=normalize_mode(getattr(parent_session, "mode", DEFAULT_MODE)),
        child_mode=normalize_mode(child_mode),
        parent_entity_id=getattr(parent_session, "entity_id", 0),
        project_id=project_id,
        task_id=task_id,
    )
