"""The ROOT turn type: the Host's root commands, in a thread of their own.

Root on a machine (`machine_exec`) is standing, with no approval per
command, and usable only with nothing untrusted in the turn. Untrusted work is
rare and the Host hands it to an agent, so root turns get a thread of their
own in the multi-threaded chat:

  - ROOT is a built-in turn type. Its thread is marked in circles_threads
    (`builtin_type = 'root'`, migrations/v225); a turn in that thread runs
    under it (turn_types.resolve_session_type). It is not a turn_types row:
    nobody can draft, widen or bind it, and the classifier never moves a
    message into or out of it.
  - `machine_exec` exists only in it. Every other turn has it taken out of
    its catalog (ROOT_ONLY_TOOLS) and the executor refuses it outside a root
    turn (tools._exec_machine_exec, `in_root_turn`).
  - The root turn's tools and context are an ALLOWLIST, built from this
    module, never the normal prompt with things taken out:
      tools    ROOT_TOOLS: machine_exec; gitea reads (list/repo/tree/read/
               grep) of the Host's own and its user's repos, and op='propose'
               to its user's (the forge's list; any other repo is refused);
               the platform-source reads (search_my_code, code_grep,
               code_read, code_tree); close_turn; escalate.
      context  ROOT_PROMPT_BLOCKS: these rules, and the platform's facts —
               the frame, the timeline (kinds and times, never an item's own
               words) and diagnostics; the root thread's own history; the
               words of this turn (the person's, or the brief the Host wrote
               when it opened the thread).
    Everything else is absent: the web, MCP, inbox and messages, people,
    memory and recall, connectors, tickets, skills, agents.
  - The Host opens or continues the root thread from any thread with
    turn_type(op='root', request=...): a visible turn in a visible thread
    titled "Root". The person sees it in the thread list and can write there.
  - The taint flag (runtime/turn_taint) stays as the backstop: a root turn
    opened from a turn that read untrusted content starts marked, and a
    command whose output reached outside the platform's own hosts marks it
    (`untrusted_connections`, bastion part 2), so machine_exec refuses for
    the rest of that turn. The next root turn reads that turn's rows as a
    one-line note (withhold_untrusted), so it starts clean.
  - Only the person's own words go into the root thread: a send or a nudge
    from an app, a connector or another Host is refused (words_refusal).
"""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("cseries.mastercode.root_turn")

ROOT = "root"
ROOT_TITLE = "Root"
ROOT_TYPE_NAME = "Root"

# Tools only a root turn holds.
ROOT_ONLY_TOOLS: frozenset[str] = frozenset({"machine_exec"})

# The platform-source reads (everything in the platform the Host does
# itself). They read the platform's own
# repos and the Host's and its user's: code_* fall back to the forge through
# gitea's own gate, which marks a foreign repo, and search_my_code keeps only
# those repos' hits in a root turn (its index also holds every repo another
# owner granted the Host and the Workbench projects: tools._own_repo_hits).
# So nothing untrusted comes in by them unmarked. A runbook reads source
# between shell steps (a sanity grep, an image build's checks), so
# they live here.
CODE_READ_TOOLS: frozenset[str] = frozenset({"code_grep", "code_read", "code_tree", "search_my_code"})

# The root turn's whole tool list. Nothing else is offered, and the gate
# (turn_types.check_call, session.py's loop) refuses anything else by name.
ROOT_TOOLS: frozenset[str] = frozenset({"machine_exec", "gitea", "close_turn", "escalate"}) | CODE_READ_TOOLS

# The root turn's tools that run directly (session.py), the rest being verbs.
ROOT_EXEC_TOOLS: frozenset[str] = ROOT_TOOLS - {"close_turn", "escalate"}

# gitea in a root turn: the reads, and op='propose' (a pull request to a repo
# of the Host's user; the executor refuses any other party's repo there, from
# the forge's own list, tools._exec_gitea). op='comment' stays outside: a
# comment answers other people's words.
GITEA_READ_OPS: frozenset[str] = frozenset({"list", "repo", "tree", "read", "grep"})
GITEA_ROOT_OPS: frozenset[str] = GITEA_READ_OPS | {"propose"}

# A forge repo name, as gitea takes it (tools._GITEA_REPO_RE).
_REPO_RE = r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$"

# The bits the root turn's tools need (meta_tool_caps.META_TOOL_CAPS):
# machine_exec execute_tool|write_context, gitea external_api, the code reads
# read_context, close_turn write_context|write_memory, escalate write_memory.
# Still intersected with the entity's own bits by the gate.
ROOT_GRANT_NAMES: tuple[str, ...] = ("read_context", "write_context", "write_memory",
                                     "execute_tool", "external_api")

# The root turn's prompt, block by block, in order. The system message is
# these and nothing else; then the root thread's own history; then this
# turn's words.
ROOT_PROMPT_BLOCKS: tuple[str, ...] = ("root_rules", "frame", "timeline", "diagnostics")

ROOT_RULES = """# Your root thread

This is your root thread. Its turns run commands as root on a machine whose
bastion granted you root commands, with `machine_exec`. Nothing untrusted is
in this thread: no web, no MCP, no messages from other hosts or people, no
recalled memory, no agents. What you have here:

- the words of this turn, and this thread's own earlier turns;
- `gitea` on your own repos and your user's: list, repo, tree, read, grep,
  and `op='propose'` to open a pull request on a repo of your user's;
- the platform source: `search_my_code` first, then `code_grep` to pin exact
  lines and `code_read` to read a range, `code_tree` to list a directory;
- `machine_exec(machine, command)`: one command as root, its output fenced as
  data. The machine records every command whole;
- `escalate` to ask the person, and `close_turn` to end the turn.

When your user operates the platform, its runbooks (shipping an image,
rolling Hosts, network changes) are files on one of their machines or in one
of their repos. Read one with `machine_exec` (`cat`, or `sed -n` for a range;
it stays on the box) or with gitea op='read'. Read the whole runbook
before its first step, then run its steps in order, one command per call,
and say what each gate printed.

Inside means the platform's own hosts: the machine itself, and every host the
machine's platform_hosts.json lists (with no list, every connection that
leaves the machine counts as outside). Outside is everything else: the open
web, a package registry, a repo that is neither yours nor your user's,
another person's words. A command
whose output came back from outside marks this turn, and `machine_exec`
refuses for the rest of it. Anything outside (reading the web, a repo of
someone else, a message, an agent's work) is done from your other threads;
bring back only what the person or your own repos say. A fresh turn here
starts clean: what a marked turn read and wrote shows in this thread's
history as one line, "[output withheld: that command reached outside the
platform]".

Every turn here is visible to the person, like any thread: say what you run
and why before you run it, and what came back.
"""


def root_type(thread_id: str | None = None) -> dict:
    """The built-in ROOT type as the dict every turn_types reader takes.
    status 'builtin': never 'active' (no row id, no Once grant, no keyword
    learning, the timeline shows no item's words), never 'closed'."""
    return {
        "id": None, "builtin": ROOT, "name": ROOT_TYPE_NAME, "status": "builtin",
        "keywords": [], "context": {"memory": "none", "documents": False},
        "skill": None, "tool_groups": [], "grant_names": list(ROOT_GRANT_NAMES),
        "keys": [], "bastions": [], "model": None,
        "thread_id": str(thread_id) if thread_id else None, "version": 1, "cards": [],
    }


def is_root(turn_type) -> bool:
    return isinstance(turn_type, dict) and turn_type.get("builtin") == ROOT


def in_root_turn() -> bool:
    """True when the work running now is a root turn (turn_types'
    CURRENT_TURN_TYPE, set by the session once it resolves the type)."""
    from mastercode.runtime.turn_types import CURRENT_TURN_TYPE
    return is_root(CURRENT_TURN_TYPE.get())


def admits(name: str, args) -> bool:
    """Whether a root turn may call `name` with `args`."""
    n = (name or "").strip()
    if n not in ROOT_TOOLS:
        return False
    if n == "gitea":
        a = args if isinstance(args, dict) else {}
        op = str(a.get("op") or "list").strip().lower()
        if op == "propose":
            # Names one repo; whether it is the user's is the forge's answer,
            # read by the executor before anything is sent.
            import re
            return bool(re.match(_REPO_RE, str(a.get("repo") or "").strip().strip("/")))
        return op in GITEA_READ_OPS
    return True


def refusal(name: str) -> dict:
    """The envelope for a call a root turn does not hold."""
    return {
        "status": "denied", "needs_bits": 0, "reason": "root_turn",
        "result": (f"Not in your root thread: {name!r} is not one of its tools "
                   f"({', '.join(sorted(ROOT_TOOLS))}; gitea reads and op='propose' to a "
                   "repo of yours or your user's). Do it from another thread; this one "
                   "reads nothing untrusted."),
    }


_GITEA_ROOT_DESCRIPTION = (
    "Your own repos on the forge and your user's: op='list', 'repo', 'tree', 'read' (a "
    "page of whole lines; start_line/end_line), 'grep'; op='propose' opens a pull request "
    "on a repo of your user's (repo, title, and files, patch or from_project), which your "
    "user reviews and merges.")
_SEARCH_ROOT_DESCRIPTION = (
    "Search code by meaning and keywords. Here only your own repos and your user's on "
    "the forge come back (other projects are named in left_out, never shown). Each "
    "hit names its repo, file and lines; read "
    "the range with code_read (or gitea op='read'), and pin exact lines with code_grep.")


def tool_schemas(authorized: set[str] | None = None) -> list[dict]:
    """The root turn's tool list: ROOT_TOOLS' schemas, `_meta` left behind,
    gitea narrowed to its root ops. `authorized`: the names the entity's
    catalog holds (machine_exec, gitea and the code reads are offered only
    when held); close_turn and escalate ride every turn, as the verbs do."""
    import copy
    from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS
    from mastercode.runtime.triage import _BOT_TOOL_SCHEMAS, _BRIDGE_TOOL_SCHEMAS
    out: list[dict] = []
    for n in ("machine_exec", "gitea", "search_my_code", "code_grep", "code_read", "code_tree"):
        if authorized is not None and n not in authorized:
            continue
        s = PUBLIC_TOOL_SCHEMAS.get(n) or _BRIDGE_TOOL_SCHEMAS.get(n)
        if not s:
            continue
        fn = copy.deepcopy(s["function"])
        if n == "gitea":
            op = (fn.get("parameters") or {}).get("properties", {}).get("op")
            if isinstance(op, dict):
                op["enum"] = sorted(GITEA_ROOT_OPS)
            fn["description"] = _GITEA_ROOT_DESCRIPTION
        elif n == "search_my_code":
            # The catalog's text sends a foreign repo to an agent and a hit to
            # workspace_file; neither is in this thread.
            fn["description"] = _SEARCH_ROOT_DESCRIPTION
        out.append({"type": "function", "function": fn})
    esc = _BOT_TOOL_SCHEMAS.get("_meta_escalate")
    if esc:
        out.append({"type": "function", "function": copy.deepcopy(esc["function"])})
    ct = PUBLIC_TOOL_SCHEMAS.get("close_turn")
    if ct:
        out.append({"type": "function", "function": copy.deepcopy(ct["function"])})
    return out


async def platform_facts(runtime, sess, user_message) -> dict[str, str]:
    """The platform's facts for this root turn, by block name: the frame, the
    timeline and diagnostics, each from its own builder (prompt_schema) and
    nothing else from that schema. A block that fails is left out."""
    from mastercode.runtime import prompt_schema as ps
    out: dict[str, str] = {}
    for name, fn in (("frame", ps._b_frame), ("timeline", ps._b_timeline),
                     ("diagnostics", ps._b_diagnostics)):
        try:
            text = await fn(runtime, sess, user_message)
        except Exception as e:  # noqa: BLE001 - a fact block never fails the turn
            log.warning("root turn: %s block failed (left out): %s", name, e)
            text = None
        if text:
            out[name] = text
    return out


def system_text(facts: dict[str, str], *, crisis: str = "") -> tuple[str, str]:
    """(static, dynamic) system text for a root turn, from ROOT_PROMPT_BLOCKS
    in order. `crisis` is the platform's own notice when the person's words
    hit the crisis floor (session._crisis_flagged)."""
    dyn = [facts[b] for b in ROOT_PROMPT_BLOCKS[1:] if facts.get(b)]
    if crisis:
        dyn.append(crisis)
    return ROOT_RULES.strip(), "\n\n".join(dyn)


def build_messages(static: str, dynamic: str, history: list, words: list) -> list[dict]:
    """The root turn's message list: the rules, the root thread's own
    history, the platform's facts, this turn's words (the same dicts the
    caller holds, so its protected-words list still matches by identity)."""
    msgs: list[dict] = [{"role": "system", "content": static}]
    msgs.extend(m for m in (history or []) if isinstance(m, dict))
    if dynamic:
        msgs.append({"role": "system", "content": dynamic})
    msgs.extend(w for w in (words or []) if isinstance(w, dict))
    return msgs


# ── the root thread ──────────────────────────────────────────────────────────

async def is_root_thread(pool, entity_id: int, thread_id) -> bool:
    """Whether `thread_id` is this Host's root thread. A read that fails is
    "no": a turn that cannot be shown to be root runs without root."""
    if pool is None or not entity_id or not thread_id:
        return False
    try:
        v = await pool.fetchval(
            "SELECT builtin_type FROM circles_threads "
            "WHERE id = $1::uuid AND entity_id = $2 AND deleted_at IS NULL",
            str(thread_id), int(entity_id))
    except Exception as e:  # noqa: BLE001
        log.warning("root thread check failed (eid=%s thread=%s): %s — not a root turn",
                    entity_id, thread_id, e)
        return False
    return v == ROOT


async def ensure_root_thread(pool, entity_id: int) -> str | None:
    """The Host's one live root thread, made on first use and reopened when
    the person closed it. A deleted one is gone; the next open makes a new
    one. Two opens at once: the unique index keeps one, the loser deletes
    its own empty row and takes the winner's."""
    try:
        row = await pool.fetchrow(
            "SELECT id, closed_at FROM circles_threads WHERE entity_id = $1 "
            "AND builtin_type = $2 AND deleted_at IS NULL", int(entity_id), ROOT)
        if row is not None:
            if row["closed_at"] is not None:
                await pool.execute(
                    "UPDATE circles_threads SET closed_at = NULL WHERE id = $1::uuid AND entity_id = $2",
                    str(row["id"]), int(entity_id))
            return str(row["id"])
        from mastercode import circles_threads as _ct
        made = await _ct.create_thread(pool, int(entity_id), title=ROOT_TITLE)
        tid = str(made["id"])
        try:
            await pool.execute(
                "UPDATE circles_threads SET builtin_type = $1 WHERE id = $2::uuid AND entity_id = $3",
                ROOT, tid, int(entity_id))
        except Exception:  # noqa: BLE001 - another open won the unique index
            await pool.execute("DELETE FROM circles_threads WHERE id = $1::uuid AND entity_id = $2",
                               tid, int(entity_id))
            won = await pool.fetchval(
                "SELECT id FROM circles_threads WHERE entity_id = $1 AND builtin_type = $2 "
                "AND deleted_at IS NULL", int(entity_id), ROOT)
            return str(won) if won else None
        return tid
    except Exception as e:  # noqa: BLE001
        log.warning("root thread could not be made (eid=%s): %s", entity_id, e)
        return None


_OPENED: set = set()   # the background turns started here, kept until they end


async def open_root(pool, entity_id: int, request: str, *, from_thread_id=None) -> dict:
    """turn_type(op='root'): open or continue the root thread with `request`,
    as a turn of its own that the person sees in the thread list. Only the
    Host's own turn opens it. A turn that read untrusted content may open it,
    and the root turn then starts marked (runtime/turn_taint), so
    machine_exec refuses there until a fresh turn."""
    from mastercode.runtime import turn_taint
    st = turn_taint.current()
    if st is None or st.agent:
        return {"status": "denied", "needs_bits": 0, "reason": "root_turn",
                "result": "Not run: only the Host opens its root thread, from its own turn."}
    req = str(request or "").strip()
    if not req:
        return {"status": "error", "reason": "refused",
                "result": "turn_type op='root' needs `request`: what the root thread should do."}
    if in_root_turn():
        return {"status": "error", "reason": "refused",
                "result": "You are in your root thread already; carry on here."}
    tid = await ensure_root_thread(pool, entity_id)
    if not tid:
        return {"status": "error", "result": "the root thread could not be opened"}
    from server import brain_service
    runtime = brain_service.get_runtime()
    if runtime is None:
        return {"status": "error", "result": "the Host's runtime is not up, so no turn can be started"}
    untrusted = None
    if st.source is not None:
        untrusted = f"opened from a turn that read untrusted content ({st.source})"
    # The request goes in whole. It is the Host's own words, a row of the
    # root thread like a message the person types there (no length cut either),
    # and the history the next root turn reads is bounded where it is built.
    # A cut here only dropped the end of the instruction without a word.
    brief = (f"[platform] You opened your root thread from another thread"
             f"{' (' + str(from_thread_id) + ')' if from_thread_id else ''} with this request:\n\n"
             f"{req}")
    task = asyncio.create_task(runtime._wake_host_turn(
        # A Circles thread, keyed and tagged as the person's own sends there
        # (routes/circles.py: app_id='circles' + thread_id), so the turn shows
        # in that thread and a message typed meanwhile goes into it.
        entity_id=entity_id, app_id="circles", project_id=None, thread_id=tid,
        brief=brief, origin_request=req, display_message=req,
        anchor=f"root:{tid}", title=ROOT_TITLE, chain=False,
        result_wake=False, untrusted_input=untrusted,
        # The request is a row of the Root thread, marked as the Host's own:
        # the next root turn's history has it (no hidden
        # notes; the thread is the chain of thought).
        host_request=True))
    _OPENED.add(task)
    task.add_done_callback(_OPENED.discard)
    return {"status": "ok", "result": {
        "thread_id": tid, "title": ROOT_TITLE,
        "marked": bool(untrusted),
        "next_step": ("Your root thread is running this request now, as its own turn the user "
                      "sees in the thread list (" + ROOT_TITLE + "). "
                      + ("This turn read untrusted content, so that root turn starts marked and "
                         "machine_exec will refuse there; open it from a fresh turn instead."
                         if untrusted else "Its result lands in that thread."))}}


# ── a fresh root turn starts clean ───────────────────────────────────────────
#
# A root turn that read something untrusted writes its rows stamped
# (turn_taint.stamp). Every other reader still treats the stamp as before. A
# later ROOT turn reads those rows with their words withheld, one line in
# their place, so the turn is not marked by them and the thread never locks:
# one `apt update` that reached outside ends root for THAT turn only.

WITHHELD_COMMAND = "[output withheld: that command reached outside the platform]"


def withheld_note(source: str) -> str:
    """The line a stamped row is read as in a root turn."""
    from mastercode.runtime import turn_taint
    if source == turn_taint.COMMAND_REACHED_OUT:
        return WITHHELD_COMMAND
    return f"[withheld: written in a turn that read untrusted content ({str(source)[:120]})]"


def withhold_untrusted(rows) -> list:
    """`rows` as a root turn reads them: a row stamped untrusted becomes a copy
    with its role, its time and the one-line note, and nothing else (no words,
    no tool calls, no stamp). Other rows are the same objects. The stored rows
    are untouched."""
    from mastercode.runtime import turn_taint
    out: list = []
    for r in rows or []:
        why = turn_taint.row_untrusted(r)
        if not why:
            out.append(r)
            continue
        if "tool_calls" in r and "role" not in r:
            continue                       # a tool-trail entry: left out whole
        w = {"role": r.get("role") or "assistant", "content": withheld_note(why)}
        if r.get("created_at") is not None:
            w["created_at"] = r["created_at"]
        out.append(w)
    return out


# ── who may write into a running root turn ───────────────────────────────────

# The apps a PERSON writes from (routes/tickets.PERSON_SURFACES): the Circles
# and Workbench composers and their nudge buttons.
PERSON_APPS: frozenset[str] = frozenset({"circles", "workbench"})

WORDS_REFUSAL = ("Not delivered: the root thread takes only the person's own words, typed in "
                 "Circles or the Workbench. An app, a connector or another Host cannot write "
                 "into it.")


def person_request(request) -> bool:
    """Whether a route's caller is the person: the app token on the request
    is Circles' or the Workbench's."""
    tok = getattr(getattr(request, "state", None), "app_token", None)
    return str(getattr(tok, "app_id", "") or "") in PERSON_APPS


async def words_refusal(pool, entity_id, thread_id, *, person: bool, frame=None) -> str | None:
    """Why words may not go into `thread_id`, or None. Only the root thread
    refuses: words that are not the person's own (`person` False, or a
    forwarded connector message or a carried People-chat message in
    `frame`)."""
    if not thread_id:
        return None
    fr = frame if isinstance(frame, dict) else {}
    if person and not (fr.get("connector_origin") or fr.get("carry")):
        return None
    if await is_root_thread(pool, entity_id, thread_id):
        return WORDS_REFUSAL
    return None


def untrusted_connections(result) -> list[str]:
    """The destinations outside the platform's own that a command connected
    to, as the bastion reports them (client-go/rootexec.go rootExecResult):
    `untrusted_connections`, a list of
    {addr, proto, port, count, note}, at most the box's cap, the rest counted
    in `untrusted_connections_more`. One line each. An older bastion sends
    neither: empty."""
    r = result if isinstance(result, dict) else {}
    v = r.get("untrusted_connections")
    out: list[str] = []
    if v and not isinstance(v, list):
        # Present but not the list the box sends: never read as "none".
        out.append("an untrusted_connections value this host cannot read")
    for c in (v if isinstance(v, list) else [])[:50]:
        if isinstance(c, dict):
            addr = str(c.get("addr") or "").strip()
            if not addr:
                # An entry with no address is still an entry: the box saw
                # something leave, so the output is not known to be clean.
                out.append("a destination the machine sent without an address")
                continue
            port = c.get("port")
            line = f"{addr}:{port}" if port else addr
            if c.get("proto"):
                line += f"/{c['proto']}"
            if c.get("note"):
                line += f" ({str(c['note'])[:80]})"
            out.append(line[:200])
        elif str(c or "").strip():
            out.append(str(c)[:200])
    try:
        more = int(r.get("untrusted_connections_more") or 0)
    except (TypeError, ValueError):
        more = 0
    if more > 0:
        out.append(f"and {more} more")
    return out


__all__ = [
    "ROOT", "ROOT_TITLE", "ROOT_TOOLS", "ROOT_ONLY_TOOLS", "ROOT_PROMPT_BLOCKS", "ROOT_RULES",
    "GITEA_READ_OPS", "GITEA_ROOT_OPS", "CODE_READ_TOOLS", "ROOT_EXEC_TOOLS", "ROOT_GRANT_NAMES", "root_type", "is_root", "in_root_turn", "admits",
    "refusal", "tool_schemas", "platform_facts", "system_text", "build_messages",
    "is_root_thread", "ensure_root_thread", "open_root", "untrusted_connections",
    "WITHHELD_COMMAND", "withheld_note", "withhold_untrusted", "PERSON_APPS", "WORDS_REFUSAL",
    "person_request", "words_refusal",
]
