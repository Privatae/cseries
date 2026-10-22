"""The Host's orchestration tools and the orchestrator prompt built from them.

Moved out of runtime/session.py, which re-exports every name defined here."""

from mastercode.runtime.triage import _BOT_TOOL_SCHEMAS, PUBLIC_TOOL_SCHEMAS
from mastercode.runtime.tool_lanes import lane_for as _lane_for


def _build_host_orchestration_tools() -> list[dict]:
    """The Host's verbs, as native tool schemas: dispatch, operate, recall,
    agent_status, escalate, close_turn. Their descriptions are the ONE copy of
    the verb rules (no prompt block restates them). escalate and close_turn
    reuse their registry schemas, `_meta` stripped."""
    return _orchestration_verbs()


def _orchestration_verbs() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "dispatch",
                "description": (
                    "Run a governed agent in the background to read content "
                    "you do not own (someone else's repo, the web, another "
                    "Host's or person's message) so it cannot act as you. "
                    "Nothing else is an agent's work: name the content with "
                    "`work`, and scope a tool that reads it. It holds only tool_scope and grant; "
                    "its result reaches you in a later turn, so your reply now "
                    "says what you started. What is yours (your and your "
                    "person's granted repos, your data, platform source) you "
                    "read yourself with `read`; one exact operation is "
                    "`operate`; code runs only through your build tools."
                ),
                "parameters": {
                    "type": "object",
                    "required": ["intent", "task", "tool_scope", "grant", "system_prompt", "work"],
                    "properties": {
                        "intent": {
                            "type": "string",
                            "description": "Shown to the user before the agent starts: one first-person sentence saying what you are doing.",
                        },
                        "task": {
                            "type": "string",
                            "description": "What the agent must do.",
                        },
                        "tool_scope": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "The tool names the agent holds (find_tools finds them).",
                        },
                        "grant": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "The capabilities the agent holds; it starts with none. Grant the least its readers need, from: read_context, read_memory, external_api. An agent reads and reports, so a write capability (write_context, write_memory, execute_tool, spawn_bot) is refused. A tool whose capability is missing is refused, naming it. Skill 'dispatch-agent' has the table.",
                        },
                        "system_prompt": {
                            "type": "string",
                            "description": "The agent's own system prompt, written for this task: what it is, the goal, what done looks like, and what it must not do. Not your persona.",
                        },
                        "model": {
                            "type": "string",
                            "description": "Optional model for this agent; omit to use yours. agent_status shows model_performance.",
                        },
                        # The kind of untrusted work, and the model preferred for
                        # it. Written out (not built from agent_work) because the
                        # verb schemas are read as literals by smoke's narration
                        # gate; test_agent_model_by_work holds it equal to
                        # agent_work.DISPATCH_WORK_PARAM.
                        "work": {
                            "type": "string",
                            "enum": ["foreign_repo", "web", "message"],
                            "description": ("Untrusted content the agent reads: foreign_repo (a repo you neither own "
                                            "nor were granted), web, or message (another Host's or person's). No "
                                            "`model`: it runs on the model preferred for this work, else yours."),
                        },
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "operate",
                "description": (
                    "Run ONE registered operation now, with exact arguments: no "
                    "agent, no model in the loop, the result returns this turn. "
                    "The same capability gates and user step-ups apply; an op "
                    "outside the registry is refused and names the door to use. "
                    "Resolve what the user meant first; if it is ambiguous, ask "
                    "or read."
                ),
                "parameters": {
                    "type": "object",
                    "required": ["intent", "tool"],
                    "properties": {
                        "intent": {
                            "type": "string",
                            "description": "Shown to the user before it runs: one first-person sentence stating the operation.",
                        },
                        "tool": {
                            "type": "string",
                            "description": "The tool's name (find_tools finds it).",
                        },
                        "args": {
                            "type": "object",
                            "description": "Its exact arguments.",
                        },
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "recall",
                "description": (
                    "One search over everything you hold: memory (incl. past "
                    "turns in every thread), the user's documents, projects, "
                    "indexed code, skills and your own database. Results come grouped by "
                    "store, each saying how it was searched; an unavailable "
                    "store is named, never shown as empty. Open a hit with the "
                    "tool it names."
                ),
                "parameters": {
                    "type": "object",
                    "required": ["query"],
                    "properties": {
                        "query": {"type": "string", "description": "What to look up."},
                        "scope": {"type": "string",
                                  "enum": ["all", "memory", "documents", "projects", "code", "skills",
                                           "world"],
                                  "description": "One store only (default all)."},
                        "limit": {"type": "integer", "description": "Hits per store (default 10)."},
                        "offset": {"type": "integer", "description": "Skip this many: the next page."},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "agent_status",
                "description": (
                    "Your dispatched agents: state, result preview, cost, and the "
                    "per-model rollup. Each result is also delivered to you when "
                    "it finishes. task_id gives one task's full record."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task_id": {
                            "type": "integer",
                            "description": "One task's full record.",
                        },
                    },
                },
            },
        },
        {
            "type": "function",
            "function": _BOT_TOOL_SCHEMAS["_meta_escalate"]["function"],
        },
        # The Host's end of turn (tool_lanes lane 'verb'): on every turn, every surface, every turn type. The schema
        # lives in the public registry; `_meta` stays behind, as for escalate.
        {
            "type": "function",
            "function": PUBLIC_TOOL_SCHEMAS["close_turn"]["function"],
        },
    ]


def _build_search_door_tools() -> list[dict]:
    """`find_tools` and `read(tool, args)`, the two doors every orchestrating
    Host holds beside the verbs: find_tools searches everything the Host may use on this turn,
    read runs a read-only tool."""
    from mastercode.runtime.tool_doors import search_door_tools
    return search_door_tools()


# How a turn ends. Rides the cached static
# message, so it is a constant: no per-turn value may enter it.
_CLOSE_TURN_RULE = (
    "### Ending a turn\n"
    "End every turn with `close_turn` once its work is done or started, in the "
    "SAME message as your reply, as its only tool call: the two together end "
    "the turn. It carries structured signal only, and the user can see it: the turn "
    "type, which calls helped and which were dead, what would have made the turn "
    "faster, the thread's topic (and a title when it has none), anything the "
    "USER said they want, asked you to remember or corrected (with the reason, "
    "and a scope by what it is about: "
    "global only when it is about the person, else its project, app or thread; "
    "never system state), "
    "the thread's goal set or met, and the handoff with identifiers verbatim.\n\n"
)


def _build_orchestrator_prompt(cap_schemas: list[dict], *, withheld=None,
                               unvalidated_route=None, build_doors_listed: bool = True) -> str:
    """The facts about the Host's tool surface that no tool returns: how the
    list is shaped, the lane counts, what the validated gate withheld on this
    route, the names of its own build doors, and how a turn ends. The verb
    rules live in the verb schemas only (_orchestration_verbs); everything
    else is found with find_tools.

    The lanes are counted from ONE classifier, tool_lanes.lane_for, the one
    the tool list and the tool loop use. Retired tools are never counted."""
    from mastercode.runtime.tool_access import read_safe_ops
    from mastercode.runtime.scoped_agent import agent_reader_tools
    # What an agent can hold is what the dispatch door admits a scope for: a
    # tool that reads untrusted content (scoped_agent.agent_reader_tools).
    # Counting the dispatch LANE here told the Host "1 tools an agent can
    # hold" while the door admits web_search, read_url, research and the rest,
    # and before the lane fix it counted 19 in-platform tools as agent work.
    _readers = set(agent_reader_tools())
    n_read = n_mixed = n_operate = n_agent = 0
    own_names: list[str] = []
    for s in cap_schemas or []:
        if not isinstance(s, dict):
            continue
        fn = s.get("function") or {}
        name = fn.get("name")
        if not name:
            continue
        lane = _lane_for(name, s)
        if lane in ("retired", "verb"):
            continue
        if lane == "host":
            own_names.append(name)
            continue
        if name in _readers:
            n_agent += 1
        if lane == "read":
            n_read += 1
            continue
        if read_safe_ops(name, s):
            n_mixed += 1
        if lane == "operate":
            n_operate += 1
    withheld = set(withheld or ())
    acts_withheld = bool(withheld & {"operate", "dispatch"})
    lanes = [f"{n_read} read tools" + (f" and the read ops of {n_mixed} more" if n_mixed else "")
             + " (run with `read`)"]
    if "operate" in withheld:
        lanes.append("no `operate` on this route")
    else:
        lanes.append(f"{n_operate} operations (run with `operate`)")
    if "dispatch" in withheld:
        lanes.append("no `dispatch` on this route")
    else:
        lanes.append(f"{n_agent} of these read untrusted content from outside the platform "
                     "(the web, a foreign repo, another's message), the only work a "
                     "dispatched agent takes: name one in its tool_scope; everything "
                     "else you do yourself")
    parts = [
        "## Your tools",
        "Your tool list is fixed for the turn. `find_tools` searches everything "
        "else you can use (tools, skills, apps installed and in the store, "
        "capabilities, turn types, harnesses) and says how to reach each one; "
        "find_tools(name=...) gives one in full. You hold " + "; ".join(lanes) + ".",
    ]
    if acts_withheld:
        route = (f" ({unvalidated_route[0]} / {unvalidated_route[1]}, primary or failover)"
                 if unvalidated_route else "")
        parts.append(
            f"The model on this route{route} is not validated for tool use: you can read "
            "and answer, not act. Say so when asked to act; the user can route to a "
            "validated model in the Orchestrator.")
        own_names = []      # the build doors went with them
    if own_names and build_doors_listed:
        parts.append("Your own build tools, called by name (never dispatched or operated): "
                     + ", ".join(f"`{n}`" for n in own_names)
                     + ". Their full schemas: find_tools(name=...).")
    elif own_names:
        # A surface whose build doors are found, not listed
        # (tool_core.BUILD_DOORS_FOUND_ON): operate runs them.
        parts.append("Your own build tools, run with operate(intent, tool, args), never "
                     "dispatched: " + ", ".join(f"`{n}`" for n in own_names)
                     + ". find_tools(name=...) gives each one's schema.")
    return "\n".join(parts) + "\n\n" + _CLOSE_TURN_RULE
