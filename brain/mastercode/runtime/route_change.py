"""
runtime/route_change.py — what a host-initiated route write is allowed to DO.

A host that can rewrite its own routing table is choosing
its own successor. Until now the only thing between `model_routing op='set'`
and the table was a sentence in a skill ("get the user's agreement"). This
module is the structural rule, the same one the Network app applies to
grants — widening needs the user, tightening is free:

  apply_unrouted   the role has no route; nothing runs there to break.
  unchanged        the request equals what is already in force; a no-op.
  apply_revert     the request is a route the USER approved before for this
                   role (an Orchestrator write or an approved proposal) —
                   going back is free.
  propose          anything else: a decision the user resolves in Circles;
                   the route lands only on 'apply'.

The user's own writes in the Orchestrator never pass through here. Pure and
synchronous so it is testable without a pool; the callers supply the current
entry and the history answer.
"""

from __future__ import annotations

APPLY_UNROUTED = "apply_unrouted"
UNCHANGED = "unchanged"
APPLY_REVERT = "apply_revert"
PROPOSE = "propose"

# set_by values that make a route "approved" for the revert rule. The host's
# own writes are deliberately not among them — it cannot launder a route into
# approved by writing it itself.
APPROVED_SET_BY = ("user", "decision")


def _norm(v) -> str:
    return str(v or "").strip().lower()


# The writer flips an openrouter pick to 'local' when a machine on the user's
# own network serves that model (inference.apply_role_route, local-first).
# For "is this the same route" the two spellings are one route.
_EQUIV_PROVIDERS = frozenset({"openrouter", "local"})


def same_route(a: dict | None, b: dict | None) -> bool:
    """(provider, model) equality, case-insensitive, with the local-first
    flip folded in: openrouter/X and local/X are the same route."""
    ap, am = _norm((a or {}).get("provider")), _norm((a or {}).get("model") or (a or {}).get("model_id"))
    bp, bm = _norm((b or {}).get("provider")), _norm((b or {}).get("model") or (b or {}).get("model_id"))
    if not (am and bm) or am != bm:
        return False
    if ap == bp:
        return True
    return ap in _EQUIV_PROVIDERS and bp in _EQUIV_PROVIDERS


def classify(current: dict | None, requested: dict, *, previous_approved: dict | None) -> str:
    """Decide the verb for a host-initiated route write.

    `current` is the role's route in force ({provider, model, effort?,
    fallback_provider?, ...}) or None / an entry with no provider+model when
    the role is unrouted. `requested` is the tool's {provider, model,
    effort?, fallback_provider?, fallback_model?}, already normalized the way the writer
    normalizes (a Claude-shaped pick reads as provider 'anthropic').
    `previous_approved` is the one route the host may go back to for free —
    the newest USER-approved route for the role that is not the current one
    (inference.previous_approved_route) — or None. Effort and fallback only
    matter when the request names them: a request that leaves them out and
    matches the route in force is a no-op, not a change.

    The caller decides what to do when the current route cannot be READ;
    this function is never handed an unknown — an unreadable table must not
    reach a verb that applies (review of 64c651c0, B3).
    """
    cur_p = _norm((current or {}).get("provider"))
    cur_m = _norm((current or {}).get("model") or (current or {}).get("model_id"))
    if not (cur_p and cur_m):
        return APPLY_UNROUTED
    same = same_route(current, requested)
    if same:
        for k in ("effort", "fallback_provider", "fallback_model"):
            want = _norm(requested.get(k))
            if want and want != _norm((current or {}).get(k)):
                same = False
                break
    if same:
        return UNCHANGED
    if previous_approved and same_route(previous_approved, requested):
        return APPLY_REVERT
    return PROPOSE


def carry_unchanged(current: dict | None, requested: dict) -> dict:
    """The request with what it leaves out of the SAME route taken from the
    route in force. The writer replaces a role's whole entry
    (inference.apply_role_route), so a request that only adds a fallback
    would otherwise drop the route's effort; `classify` already reads a
    left-out field as "unchanged", and this makes the write say the same.
    A request for another route is returned as it is: its settings are its
    own. A fallback model is carried only with its own fallback provider."""
    out = dict(requested)
    if not (current and same_route(current, requested)):
        return out
    if not out.get("effort") and current.get("effort"):
        out["effort"] = current["effort"]
    if current.get("reasoning_inline") is True and out.get("reasoning_inline") is None:
        out["reasoning_inline"] = True
    if not out.get("fallback_provider") and current.get("fallback_provider"):
        out["fallback_provider"] = current["fallback_provider"]
        if not out.get("fallback_model") and current.get("fallback_model"):
            out["fallback_model"] = current["fallback_model"]
    elif (not out.get("fallback_model") and current.get("fallback_model")
          and _norm(out.get("fallback_provider")) == _norm(current.get("fallback_provider"))):
        out["fallback_model"] = current["fallback_model"]
    return out


def _changes(current: dict | None, requested: dict) -> list[str]:
    """What a request for the SAME route changes, in the card's words."""
    out = []
    if _norm(requested.get("fallback_provider")) and (
            _norm(requested.get("fallback_provider")) != _norm((current or {}).get("fallback_provider"))
            or _norm(requested.get("fallback_model")) != _norm((current or {}).get("fallback_model"))):
        # A blank fallback model is the primary's id on the fallback provider.
        out.append(f"fallback to {requested['fallback_provider']} / "
                   f"{requested.get('fallback_model') or requested.get('model')}")
    if _norm(requested.get("effort")) and _norm(requested.get("effort")) != _norm((current or {}).get("effort")):
        out.append(f"effort to {requested['effort']}")
    return out


def proposal_prompt(role: str, current: dict | None, requested: dict, reason: str | None) -> str:
    """The sentence the decision card shows. The host's reason is quoted as
    the host's, never presented as the platform's finding. A request that
    keeps the route and changes only its fallback or effort says that, not
    "move"."""
    cur = (f"{current.get('provider')} / {current.get('model')}"
           if current and current.get("provider") else "no route")
    new = f"{requested.get('provider')} / {requested.get('model')}"
    changes = _changes(current, requested) if current and same_route(current, requested) else []
    if changes:
        s = f"Keep the {role} role on {cur} and set its {' and '.join(changes)}?"
        if requested.get("fallback_provider") and any(c.startswith("fallback") for c in changes):
            s += (" The fallback answers only when the primary errors or its safety filter "
                  "declines a request.")
    else:
        extras = []
        if requested.get("effort"):
            extras.append(f"effort {requested['effort']}")
        if requested.get("fallback_provider"):
            extras.append(f"fallback {requested['fallback_provider']}"
                          + (f" / {requested['fallback_model']}" if requested.get("fallback_model") else ""))
        tail = f" ({', '.join(extras)})" if extras else ""
        s = f"Move the {role} role from {cur} to {new}{tail}?"
    if reason:
        s += f" Your host's reason: {str(reason).strip()[:300]}"
    return s


def proposal_options(role: str, current: dict | None, requested: dict) -> list[dict]:
    """The card's two buttons, each saying what it does: 'apply' moves the
    role, or, when the route stays the same, sets its fallback or effort;
    'keep' changes nothing."""
    cur_p, cur_m = (current or {}).get("provider"), (current or {}).get("model")
    changes = _changes(current, requested) if current and same_route(current, requested) else []
    if changes:
        fb = any(c.startswith("fallback") for c in changes)
        return [
            {"id": "apply", "label": f"Set {role} {' and '.join(changes)}",
             "consequence": (f"{role} stays on {cur_p} / {cur_m}. "
                             + ("The fallback answers only when that model errors or its safety "
                                "filter declines a request, and the fallback's provider bills you "
                                "for those calls." if fb else "The new setting applies from the next call.")),
             "recommend": False},
            {"id": "keep", "label": f"Keep {role} as it is",
             "consequence": "Nothing changes.", "recommend": True},
        ]
    return [
        {"id": "apply", "label": f"Move {role} to {requested.get('provider')} / {requested.get('model')}",
         "consequence": ("The route changes now and the host may revert to the "
                         "previous one for free later. This changes which provider bills you."),
         "recommend": False},
        {"id": "keep", "label": f"Keep {cur_p} / {cur_m}",
         "consequence": "Nothing changes.", "recommend": True},
    ]
