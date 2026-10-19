"""Untrusted content in a Host turn, and the tool it withdraws.

The Host holds a standing root command on the machine whose
bastion granted it, with no approval per command, only in turns with
nothing untrusted. So the moment a turn takes in
content it did not get from its own person or its own work, `machine_exec` is
refused for the rest of that turn, in code:

  - the web: web_search, read_url, research, a browser agent's run;
  - a foreign repository: GitHub, or a forge repo that the forge's
    own list (/api/v1/user/repos: the Host's and the ones its user granted)
    does not hold (tools._exec_gitea marks it);
  - MCP output: a remote server's tool (an mcp:<server>/<tool> call), and
    mcp_connections' tool list. An installed app's own server is inside the
    platform when the app is first-party or the Host's own build and declares
    no outside host (server/mcp_connections.call notes it per call,
    note_mcp_server); any server not noted that way marks;
  - another Host's or person's message: host_message reads, inbox_read reads,
    the community board, a forwarded or bot-relayed connector message, a
    People-chat message carried into the turn, the inbox block naming
    another host;
  - an agent's report: agent_status, a lab run, a result handed into a
    running turn, a turn woken by a result. A harness run's report marks it
    whatever repo the run worked on, a run on the Host's own repo included:
    a pool harness pod reaches the public internet through the egress proxy
    (tools.POOL_HARNESS_OPEN_WEB, marked by tools._mark_harness_target when
    harness_transcript reads it), and a mesh_harness_run runs on the user's
    machine with the open web in reach (tools.MESH_HARNESS_OPEN_WEB). What
    the run read on the way is not known, so the report is outside content;
  - an uploaded file: chat attachments and context_ingest;
  - words carried from a tainted turn (`stamp` / `mark_rows`): a memory row,
    a turn row of the conversation or a scheduled task written while a turn
    was marked carries `untrusted` in its metadata, and a later turn that
    reads it back (recall, the standing memory block, the conversation's
    history, a fired task) is marked too. A Host skill version written in a
    marked turn records it in host_skills.untrusted (migrations/v232), and
    every path that puts its text or pointer into a later turn marks that
    turn (runtime/host_skills module docstring names them); the per-turn
    offer of the Host's own skills never carries one. Cited facts and documents from the
    research store are quoted sources, so a recall that returns one marks the
    turn (the memory pre-fire renders neither);
  - a turn the platform started marks only when what it carries is
    untrusted (an agent's report, a watched call's answer, a task written in
    a marked turn). A plain timer firing the Host's own clean instruction
    stays clean.

The flag is set in ONE place per entry point (session.py and tools.py call
`mark`), it is never cleared inside a turn, and a new turn starts clean
(`begin_turn`, at session.EntityRuntime.send_message). A dispatched agent runs
under an `agent` state that never admits a command.

What is NOT marked, said plainly: a command's own output, unless the bastion
reports that the command reached a host outside the platform's own
(`untrusted_connections`, bastion part 2; an older bastion reports none), and
files the user keeps in their own stores (dcn).

machine_exec exists only in the ROOT turn type
(runtime/root_turn), whose tools and context are an allowlist: most of the
sources above never reach that turn at all. This flag is the backstop behind
the allowlist, kept as built, not the wall itself.
"""
from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass, field

# The tool this gate withdraws. Kept here so the refusal and the gate share a name.
GATED_TOOL = "machine_exec"


@dataclass
class TurnTaint:
    """One turn's record of untrusted input. `source` is the FIRST thing that
    tainted it (what the refusal names); `sources` keeps every distinct one."""
    source: str | None = None
    detail: str = ""
    sources: list[str] = field(default_factory=list)
    agent: bool = False
    cache: dict = field(default_factory=dict)


_TURN_TAINT: ContextVar[TurnTaint | None] = ContextVar("turn_taint", default=None)


def begin_turn() -> Token:
    """A new turn starts clean. Returns the token for end_turn."""
    return _TURN_TAINT.set(TurnTaint())


def end_turn(token: Token) -> None:
    try:
        _TURN_TAINT.reset(token)
    except (ValueError, RuntimeError):  # pragma: no cover - a token from another context
        _TURN_TAINT.set(None)


def begin_agent() -> Token:
    """A dispatched agent's run: never admits a command, whatever it reads."""
    return _TURN_TAINT.set(TurnTaint(agent=True, source="a dispatched agent's run"))


def current() -> TurnTaint | None:
    return _TURN_TAINT.get()


def mark(source: str, detail: str = "") -> None:
    """The turn took in untrusted content. Idempotent; never clears."""
    st = _TURN_TAINT.get()
    if st is None or not source:
        return
    if st.source is None:
        st.source, st.detail = source, (detail or "")[:200]
    if source not in st.sources:
        st.sources.append(source)


def tainted() -> bool:
    st = _TURN_TAINT.get()
    return st is not None and st.source is not None


def refusal() -> str | None:
    """Why machine_exec may not run in this turn, or None when it may."""
    st = _TURN_TAINT.get()
    if st is None:
        return f"{GATED_TOOL} runs only inside a Host turn, and none is running here"
    if st.agent:
        return f"a dispatched agent never runs {GATED_TOOL}; only the Host does, in its own turn"
    if st.source is not None:
        where = f" ({st.detail})" if st.detail else ""
        return ("this turn read untrusted content: " + st.source + where
                + "; start a fresh turn to run commands")
    return None


def action_refusal(what: str) -> str | None:
    """Why an in-platform action that reaches outward (a write on a machine, a
    credential brokered to a URL) may not run in this turn, or None when it
    may. A dispatched agent's run never runs it; a turn that read untrusted
    content does not either. No turn running here (a background caller) is
    left to that caller's own walls: these tools are not schedulable
    (direct_ops.UNATTENDED_OP_TOOLS)."""
    st = _TURN_TAINT.get()
    if st is None:
        return None
    if st.agent:
        return f"a dispatched agent never runs {what}; only the Host does, in its own turn"
    if st.source is not None:
        where = f" ({st.detail})" if st.detail else ""
        return (f"this turn read untrusted content: {st.source}{where}; {what} runs only in "
                "a turn with nothing untrusted, so start a fresh turn for it")
    return None


# ── tool results (marked at ONE choke point: session.py, before the result is
#    bounded and appended for the model) ─────────────────────────────────────

WEB = "the web"
FOREIGN_REPO = "a repository that is neither this Host's nor its user's"
MCP = "an MCP tool's output"
MESSAGE = "another Host's or person's message"
AGENT = "an agent's report"
UPLOAD = "an uploaded file"
# A root command whose output came back from a host outside the platform's own
# (the bastion reports where it connected: `untrusted_connections`, bastion
# part 2). Set by tools._exec_machine_exec, the one place that reads it.
COMMAND_REACHED_OUT = "a command that reached a host outside the platform"

# Every op of these tools returns content from outside.
_ALL_OPS: dict[str, str] = {
    "web_search": WEB,
    "read_url": WEB,
    "research": WEB,
    "github": "a repository on GitHub",
    "agent_status": AGENT,
    # harness_transcript marks in its executor (tools._mark_harness_target),
    # mesh_harness_run in its own: both whatever the run's repo.
    "lab_run": AGENT,
    "community_board": "a post on the community board",
}

# These return outside content only on the named ops (the rest send, list
# the Host's own things, or change a setting).
_SOME_OPS: dict[str, tuple[str, str, frozenset]] = {
    # tool: (the op argument's name, source, the ops that read outside content)
    "host_message": ("op", MESSAGE, frozenset({"conversations", "thread"})),
    "inbox_read": ("op", MESSAGE, frozenset({"list_conversations", "read_thread", "list_circles"})),
    "browser_agent": ("op", WEB, frozenset({"run", "resume", "get", "runs"})),
    "mcp_connections": ("op", MCP, frozenset({"tools"})),
    "context_ingest": ("op", UPLOAD, frozenset({"read", "extract", "extract_machines", "search"})),
}


# The turn-state key for the MCP servers this turn called that are inside the
# platform: {server: True}. Only server/mcp_connections.call writes it, after
# it has read the connection row.
MCP_INSIDE_KEY = "mcp_inside"


def note_mcp_server(server: str, inside: bool) -> None:
    """Record, for this turn, whether `server`'s tool output is from inside the
    platform. A server never noted counts as outside."""
    st = _TURN_TAINT.get()
    if st is None or not server:
        return
    seen = st.cache.setdefault(MCP_INSIDE_KEY, {})
    # Once outside in a turn, always outside in it: a server whose row changed
    # mid-turn is judged by the worse answer.
    seen[server] = bool(inside) and seen.get(server, True)


def _mcp_inside(name: str) -> bool:
    st = _TURN_TAINT.get()
    if st is None:
        return False
    server = name[len("mcp:"):].partition("/")[0]
    return bool((st.cache.get(MCP_INSIDE_KEY) or {}).get(server))


def source_for_tool(name: str, args) -> str | None:
    """The untrusted source a tool call's RESULT brings into the turn, or None.
    `name`/`args` are the call's TARGET (tool_doors.effective_call), so a
    search-mode read(tool=X) or operate(tool=X) is judged as X."""
    n = (name or "").strip()
    if not n:
        return None
    if n.startswith("mcp:"):
        # By server: a remote server is outside the platform; an installed
        # app's own server the call noted as inside is not.
        return None if _mcp_inside(n) else MCP
    if n in _ALL_OPS:
        return _ALL_OPS[n]
    spec = _SOME_OPS.get(n)
    if spec is not None:
        key, src, ops = spec
        a = args if isinstance(args, dict) else {}
        op = str(a.get(key) or "").strip()
        return src if op in ops else None
    return None


# The sources that are untrusted WORK: content from outside the platform or
# from someone other than the Host and its person (the Host
# calls an agent only for that). An agent's report and the person's own upload
# mark a turn, but neither is work an agent is sent to read.
_UNTRUSTED_WORK_SOURCES = frozenset({WEB, "a repository on GitHub", MCP, MESSAGE,
                                     "a post on the community board"})
# Tools whose untrusted reach is decided per call, outside the tables above:
# gitea marks a foreign repo itself (tools._exec_gitea), and bug_review's
# read/verdict are the validator agent's reads of a report another Host filed
# (direct_ops.DIRECT_OP_EXCLUDED_OPS).
_PER_CALL_SOURCES: dict[str, str] = {"gitea": FOREIGN_REPO, "bug_review": MESSAGE}


def untrusted_source_tools() -> frozenset[str]:
    """Every tool whose result can bring untrusted work into a run: the one
    set the dispatch door asks an agent's scope to reach (scoped_agent
    .dispatch_precheck). A connected MCP server's tool counts too; test a
    name with is_untrusted_source_tool."""
    out = {n for n, src in _ALL_OPS.items() if src in _UNTRUSTED_WORK_SOURCES}
    out |= {n for n, (_k, src, _o) in _SOME_OPS.items() if src in _UNTRUSTED_WORK_SOURCES}
    out |= set(_PER_CALL_SOURCES)
    return frozenset(out)


def is_untrusted_source_tool(name: str) -> bool:
    n = (name or "").strip()
    return n.startswith("mcp:") or n in untrusted_source_tools()


def mark_tool(name: str, args) -> None:
    src = source_for_tool(name, args)
    if src is not None:
        mark(src, name)


def mark_harness(foreign: str | None, what: str) -> None:
    """A harness run's report: `foreign` is why it is outside content (the
    callers pass tools.POOL_HARNESS_OPEN_WEB or MESH_HARNESS_OPEN_WEB, so
    every harness report marks); None marks nothing."""
    if foreign:
        mark(AGENT, f"{what}: {foreign}"[:200])


# ── words carried from a marked turn into a later one ──────────

CARRIED = "words written in an earlier turn that read untrusted content"
QUOTED_SOURCE = "a quoted source from the Host's research and documents"
UNTRUSTED_KEY = "untrusted"


def stamp(md: dict | None) -> dict:
    """`md` for a row this turn writes, with UNTRUSTED_KEY set to what marked
    the turn when it is marked (an agent's run counts). Unchanged when the
    turn is clean, or when no turn is running here (a background writer)."""
    out = dict(md or {})
    st = _TURN_TAINT.get()
    if st is not None and st.source is not None and not out.get(UNTRUSTED_KEY):
        out[UNTRUSTED_KEY] = str(st.source)[:200]
    return out


def row_untrusted(row) -> str | None:
    """What a stored row carries from a marked turn, or None. Reads the key
    on the row itself or in its metadata (a dict or the JSON text asyncpg
    returns for jsonb)."""
    if not isinstance(row, dict):
        return None
    if row.get(UNTRUSTED_KEY):
        return str(row[UNTRUSTED_KEY])
    md = row.get("metadata")
    if isinstance(md, str):
        try:
            import json as _json
            md = _json.loads(md)
        except ValueError:
            return None
    if isinstance(md, dict) and md.get(UNTRUSTED_KEY):
        return str(md[UNTRUSTED_KEY])
    return None


def mark_rows(rows, where: str) -> None:
    """Mark the turn when any row read into it was written in a marked turn."""
    for r in rows or []:
        if row_untrusted(r):
            mark(CARRIED, where)
            return


# The memory_type of a cited fact (task_record._promote writes it): what a
# source said, quoted. Named here, not imported, so this
# module stays import-light.
_CITED_FACT_TYPE = "cited_fact"


def mark_quoted(rows, where: str) -> None:
    """Rows that ARE quoted sources: a cited fact, a document from the
    research and documents store. Rendering any one marks the turn."""
    if rows:
        mark(QUOTED_SOURCE, where)


def mark_recall(fed) -> None:
    """A recall result (memory_retrieval.federated_recall) as the turn reads it:
    a memory row written in a marked turn, a cited fact, or any document."""
    f = fed if isinstance(fed, dict) else {}
    hits = (f.get("memory") or {}).get("hits") or []
    mark_rows(hits, "a recalled memory")
    mark_quoted([h for h in hits if isinstance(h, dict) and h.get("memory_type") == _CITED_FACT_TYPE],
                "a recalled cited fact")
    mark_quoted((f.get("documents") or {}).get("results") or [], "a recalled document")


# ── what a turn starts with ────────────────────────────────────────────────────


def mark_turn_input(*, frame=None, attachments=None, untrusted_input: str | None = None,
                    platform_started: bool = False) -> None:
    """The turn's own input: a forwarded or bot-relayed connector message
    (frame['connector_origin']), a People-chat message carried in
    (frame['carry']), uploaded files, or a turn the platform started with
    something untrusted in its brief (untrusted_input: an agent's report or a
    watched call's answer from session._wake_host_turn, or a task written in a
    marked turn from offline_task_runner). A platform-started turn with none
    of these is clean: a timer firing the Host's own instruction, written in
    a clean turn, carries nothing from outside. Its history and memory are
    judged where they are read (mark_rows). `platform_started` is kept for
    the callers' signature and decides nothing on its own."""
    del platform_started
    fr = frame if isinstance(frame, dict) else {}
    if fr.get("connector_origin"):
        mark(MESSAGE, "a forwarded or relayed message")
    if fr.get("carry"):
        mark(MESSAGE, "a message from a People chat")
    if attachments:
        mark(UPLOAD, "attached to the message")
    if untrusted_input:
        mark(AGENT, str(untrusted_input))


def mark_nudge(tags) -> None:
    """A message typed into a running turn: the person's own words, unless it
    came forwarded through a connector or carried from a People chat."""
    t = tags if isinstance(tags, dict) else {}
    if t.get("connector_origin") or t.get("carry"):
        mark(MESSAGE, "a forwarded or carried message mid-turn")


# Inbox notices about OTHER hosts (server/network/pairing_grants.py writes them
# with these actions): their text names a host the user does not govern.
_INBOX_FOREIGN_ACTIONS = frozenset({"other_bastion", "pairing_pending", "pairing_unapproved"})


def mark_inbox(messages) -> None:
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        if m.get("action") in _INBOX_FOREIGN_ACTIONS or m.get("source") not in ("gitea", "system", None):
            mark(MESSAGE, "an inbox notice about another host")
            return
