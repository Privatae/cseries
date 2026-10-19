"""mastercode.agent_work — the kinds of untrusted work a Host sends an
agent to do, and which model that agent runs on.

A Host needs an agent only to handle untrusted operations, and it holds
preferences for that by classification of the work. A Host reads what it owns itself (its repos, the
repos its person granted it, its data, the platform source). It sends an
agent to content it does not own, because that content can carry
instructions. These are those kinds of content, the one set the dispatch
door, the `preference` tool and the store all read.

The model an agent runs on, in order:
  1. the model the dispatch names;
  2. else the stored preference for the dispatch's kind of work
     (`preference` op='prefer', set by the person or by the Host, listed by
     op='list');
  3. else the Host's own route, as before.
A model a standing ban covers is refused at the door, whether it was named or
chosen by a preference, before any task row exists.

Stdlib only at import: the preference store is read inside the call.

Outside mastercode.runtime on purpose: public_tool_registry imports this, and
importing the runtime package from the registry runs runtime/__init__, whose
tools import then reads a registry that is not built yet. A process that
imported the registry first had no close_turn in the Host's tool table (the
image build's capability step failed on it with KeyError 'close_turn').
"""
from __future__ import annotations

# The kinds of untrusted work. Keys are what a dispatch and a preference
# carry; values say what each covers, in the words the schemas show.
WORK_CLASSES: dict[str, str] = {
    "foreign_repo": "a repo the Host does not own and holds no grant to",
    "web": "the web: pages, search results, downloads",
    "message": "another Host's or another person's message",
}

# The dispatch verb's `work` argument (session._orchestration_verbs).
DISPATCH_WORK_PARAM: dict = {
    "type": "string",
    "enum": list(WORK_CLASSES),
    "description": ("Untrusted content the agent reads: foreign_repo (a repo you neither own "
                    "nor were granted), web, or message (another Host's or person's). No "
                    "`model`: it runs on the model preferred for this work, else yours."),
}


def work_refusal(work) -> str | None:
    """The refusal for a dispatch whose `work` is missing or outside the set,
    or None. Required since 2026-10-12 (the Host calls an agent only for
    untrusted work): a dispatch that cannot name the untrusted content it
    reads is work the Host does itself."""
    if work is None or (isinstance(work, str) and not work.strip()):
        return ("No agent was started: an agent reads only content you do not own, and "
                "`work` names which: "
                + ", ".join(f"'{k}' ({v})" for k, v in WORK_CLASSES.items())
                + ". Work with none of these is yours: do it yourself, with read for "
                "reads and operate for one operation, in this turn.")
    if isinstance(work, str) and work.strip() in WORK_CLASSES:
        return None
    return (f"No agent was started: `work` is {work!r}, and the kinds of untrusted work are "
            + ", ".join(f"'{k}' ({v})" for k, v in WORK_CLASSES.items())
            + ". Dispatch again with one of them.")


def _norm_work(work) -> str | None:
    w = work.strip() if isinstance(work, str) else ""
    return w if w in WORK_CLASSES else None


def choice_label(choice: dict) -> str:
    """One line for the Agents app: where the agent's model came from."""
    src = (choice or {}).get("from")
    work = (choice or {}).get("work")
    if src == "preference":
        pref = choice.get("preference") or {}
        who = "your" if pref.get("by") == "person" else "the Host's"
        return f"chosen by {who} preference for {work} work"
    if src == "dispatch":
        return "named by the Host on this dispatch"
    return "the Host's own model" + (f" (no preference set for {work} work)" if work else "")


def choose_agent_model(named_model, work, *, pp=None) -> dict:
    """Which model a dispatched agent runs on, and why.

    Returns {"model": str | None, "choice": dict, "refusal": str | None}.
    `model` None means the Host's own route (run_scoped_agent resolves it).
    `choice` is what the agent's task record carries (agent_dispatch seeds it,
    the Agents app shows its `label`). `refusal` is set, and nothing may
    start, when `work` is outside the set or a standing ban covers the model.
    `pp` is the preference store module (server.routes._preference_profile);
    tests pass their own. A store that cannot be read refuses nothing and
    prefers nothing: the Host's own route, as before this existed."""
    bad = work_refusal(work)
    w = _norm_work(work)
    if bad is not None:
        return {"model": None, "choice": {}, "refusal": bad}
    if pp is None:
        try:
            from server.routes import _preference_profile as pp  # noqa: PLW0127
        except Exception:  # noqa: BLE001 — no store: own route, nothing refused
            pp = None
    named = named_model.strip() if isinstance(named_model, str) else ""
    model, choice = None, {"from": "own_route", "work": w}
    if named:
        model, choice = named, {"from": "dispatch", "work": w}
    elif w and pp is not None:
        try:
            pref = pp.work_model_for(w)
        except Exception:  # noqa: BLE001
            pref = None
        if pref and pref.get("model"):
            model = str(pref["model"])
            choice = {"from": "preference", "work": w,
                      "preference": {k: pref.get(k) for k in ("model", "by", "text", "at")}}
    choice["model"] = model or ""
    choice["label"] = choice_label(choice)
    refusal = ban_refusal_for(model, pp=pp) if model and pp is not None else None
    if refusal and choice["from"] == "preference":
        refusal += (f" It was chosen by the preference for {w} work; set another "
                    f"with preference op='prefer', or remove it with op='unprefer'.")
    return {"model": model, "choice": choice, "refusal": refusal}


def ban_refusal_for(*models, pp=None) -> str | None:
    """The refusal when a standing ban (preference op='ban') covers any of
    `models`, else None. Fails open, like ban_for: a store that cannot be read
    refuses nothing."""
    if pp is None:
        try:
            from server.routes import _preference_profile as pp  # noqa: PLW0127
        except Exception:  # noqa: BLE001
            return None
    for m in models:
        m = m.strip() if isinstance(m, str) else ""
        if not m:
            continue
        try:
            ban = pp.ban_for(m)
        except Exception:  # noqa: BLE001
            ban = None
        if ban:
            return "No agent was started: " + pp.ban_refusal(m, ban)
    return None
