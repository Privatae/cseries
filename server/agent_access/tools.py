"""What a connected outside agent can call on /mcp.

Two layers decide every call, and neither can widen the other:

  1. this agent's permissions (store.holds): 'read' covers the machine list
     and the ops in READ_GRANT_OPS alone, the box's observe family (its health
     checks, what is installed, the folder tree and open windows) and the
     model, backup and op lists and statuses. Anything else, every op that
     returns the person's data (their files under dcn_*, their context store
     under ctx_*, the machine's record of refused ops) and every change, needs
     an 'always' permission for (machine, op) on this connection, or a 'just
     this once' approval for exactly these arguments;
  2. the Host's own: nac_mgmt.run_mgmt_op runs its signed-grant default-deny
     gate and the machine's write gate for this call as for the Host's own
     work. An agent never gets what the Host itself is not approved for. That
     gate (nac_mgmt.gate_op) is asked before anything else is, so an op the
     Host may not run is refused with its words, never turned into an ask.

What the agent lacks becomes an ask (store.file_ask) and the call ends with
NeedsApproval: the link to a page on this Host where the person answers. A
machine that holds a write for the person's own approval (a card, or its
device or console) ends the same way, with a link to a page that shows it.
Nothing runs before the person answers.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from server.agent_access import store

log = logging.getLogger("cseries.agent_access")

ARGS_MAX = 32
ARG_MAX = 8192

# What an op and a machine may be called. An op is a word of the box's own
# vocabulary ("uptime", "observe:disk", "model:pull"); a machine is the
# hostname its bastion reports. Neither may hold "*": a permission reads "*"
# as every machine or every op (store.holds), so an ask the agent named "*"
# would turn one "Always for this agent" into a permission for everything,
# on a page that showed the person a bare "*".
_OP_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:\-]{0,127}")
SERVER_MAX = 255


def name_problem(server: str, op: str) -> str:
    """Why `server` / `op` cannot name a machine or an op, or ''."""
    if not _OP_NAME.fullmatch(op):
        return "op must be one op name from the machine's list (letters, digits, '_', '.', ':', '-')"
    if store.ANY in server or len(server) > SERVER_MAX or not server.isprintable():
        return f"server must be one machine's name from `machines` (no '*', at most {SERVER_MAX} characters)"
    return ""


# What the 'read' permission ticked at connect covers, and nothing more: the
# bastion's observe family (client-go anchor.go opFamily "observe": observe:*,
# computer:* and uptime) and the lists and statuses of the person's models,
# backups and ops. Not a deny-list over nac_approval.READ_OPS: an op the box
# starts to class as a read later is asked for until it is named here. The
# connect page's words (pages.READ_WORDS) say exactly this list.
READ_GRANT_PREFIXES = ("observe:", "computer:")
READ_GRANT_OPS = frozenset({"uptime", "model:list", "backup_list", "op_list",
                            "op_run_status", "op_plan_status"})


def read_grant_covers(op: str) -> bool:
    """Whether the connect page's 'read' lets an agent run `op` without asking.
    Only ops the box itself classes as reads, and of those only the list above."""
    from server.network import nac_approval
    op = (op or "").strip()
    if nac_approval.op_writes(op):
        return False
    return op in READ_GRANT_OPS or op.startswith(READ_GRANT_PREFIXES)

TOOLS: list[dict] = [
    {
        "name": "machines",
        "title": "Your machines",
        "description": (
            "The person's machines this Host reaches through their bastions: name, whether it "
            "is online, its platform, its bastion version, and the ops it accepts. Read-only."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "machine_op",
        "title": "Run an op on a machine",
        "description": (
            "Run one op on one of the person's machines through their Host (an op the machine "
            "lists in `machines`, e.g. 'uptime'). If you were allowed to see their machines, a "
            "health check or a list runs at once: uptime, observe:*, computer:*, model:list, "
            "backup_list, op_list, op_run_status and op_plan_status. Anything else, including "
            "reading their files (dcn_*) or their context store (ctx_*), and every change, runs "
            "only once the person has approved it: the first call returns a link for them to "
            "open on their Host and nothing runs; call again with the same arguments after they "
            "answer. The machine applies its own rules as well. Returns {exit, stdout, stderr}."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": {"type": "string", "description": "The machine's name, from `machines`."},
                "op": {"type": "string", "description": "The op the machine lists."},
                "args": {"type": "array", "items": {"type": "string"},
                         "description": "The op's arguments."},
                "note": {"type": "string",
                         "description": ("Why, in a sentence. Shown to the person on the approval "
                                         "page as your own words, apart from the Host's; never "
                                         "sent to the machine.")},
            },
            "required": ["server", "op"],
            "additionalProperties": False,
        },
        "annotations": {"destructiveHint": True, "openWorldHint": False},
    },
]


class NeedsApproval(Exception):
    """The call stops here until the person answers on the Host. `url` is the
    page; `message` says it in the Host's words; `ask_id` names the ask."""

    def __init__(self, ask_id: str, url: str, message: str):
        super().__init__(message)
        self.ask_id = ask_id
        self.url = url
        self.message = message


def host_only(op: str) -> bool:
    """Ops an outside agent never runs, whatever the person approves on its
    page: the ones that carry their own approval or are standing work under a
    store the person set up at the machine (nac_approval.OWN_APPROVAL_OPS:
    the box takes no tap for them, so this Host's ask would be the only
    wall), the ones that only remove reach, the pairings, the Host's own
    step-up and device requests to the machine, and every door of the ops the
    Host writes (nac_ops.DOOR_OPS: op_grant asks the person for a standing
    approval of the Host's own op, and is not in OWN_APPROVAL_OPS)."""
    from server.network import nac_approval, nac_ops
    op = (op or "").strip()
    return (op in nac_approval.OWN_APPROVAL_OPS or op in nac_approval.TIGHTEN_OPS
            or op in nac_ops.DOOR_OPS
            or op.startswith("pairing_")
            or op in ("stepup_request", "stepup_status", "device_request", "device_status"))


HOST_REFUSED = "refused: the Host's own permissions"


def _host_refusal(words: str) -> dict:
    """The Host's own gate said no: its words, as an error. Nothing ran, and
    the person's approval cannot change that, so no link is offered."""
    return _text(f"{words}. This Host itself is not allowed to run it, so nothing ran and the "
                 "person was not asked.", error=True)


def ask_url(origin: str, ask_id: str) -> str:
    """The person's approval page for an agent that reached this Host at
    `origin`: on a loopback agent origin, the desktop's own address, where
    the person is signed in (origin.authorization_origin), as the connect page."""
    from server.agent_access.origin import authorization_origin
    return f"{authorization_origin(origin)}/agent-access/ask/{ask_id}"


def _text(payload: Any, *, error: bool = False) -> dict:
    text = payload if isinstance(payload, str) else json.dumps(payload, default=str)
    out: dict = {"content": [{"type": "text", "text": text}], "isError": error}
    if isinstance(payload, dict):
        out["structuredContent"] = payload
    return out


def _clean_args(raw) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > ARGS_MAX:
        raise ValueError(f"args must be a list of at most {ARGS_MAX} strings")
    out = []
    for a in raw:
        if not isinstance(a, (str, int, float)) or isinstance(a, bool):
            raise ValueError("each argument must be a string")
        s = str(a)
        if len(s) > ARG_MAX:
            raise ValueError(f"an argument is longer than {ARG_MAX} characters")
        out.append(s)
    return out


async def _machines(pool, eid: int) -> list[dict]:
    from server.network import nac_commit
    data = await nac_commit.bastions_list(pool, eid)
    out = []
    for b in (data or {}).get("bastions") or []:
        enf = b.get("enforcing")
        if isinstance(enf, str):
            try:
                enf = json.loads(enf)
            except ValueError:
                enf = {}
        out.append({
            "name": b.get("hostname") or b.get("name"),
            "online": bool(b.get("online")),
            "platform": b.get("platform"),
            "bastion_version": b.get("bastion_version"),
            "ops": list((enf or {}).get("mgmt_ops") or []) if isinstance(enf, dict) else [],
        })
    return out


def _waiting_words(state: dict) -> tuple[dict, str]:
    """(what the ask stores, the words) for a machine holding a write: a card
    this Host takes the answer for, or the device or console it named."""
    if state.get("state") == "asked" and state.get("ticket_id"):
        return ({"via": "card", "ticket_id": int(state["ticket_id"])},
                "The machine is holding this for the person's approval.")
    req = state.get("request") or {}
    return ({"via": req.get("via") or "device", "where": state.get("next") or ""},
            state.get("next") or "The machine is waiting for the person's approval.")


async def call(pool, eid: int, conn: dict, name: str, args: dict, origin: str) -> dict:
    """One tools/call from a connected agent: an MCP CallToolResult, or
    NeedsApproval. `conn` is store.resolve_access's row."""
    cid = int(conn["id"])
    held = await store.grants(pool, cid)
    if name == "machines":
        if not store.holds(held, store.READ, "*", "*"):
            await store.record_call(pool, cid, name, "", "", "refused: no read")
            return _text("This agent was not allowed to see the person's machines when it was "
                         "connected. They can reconnect it with that ticked.", error=True)
        await store.record_call(pool, cid, name, "", "", "ok")
        return _text({"machines": await _machines(pool, eid)})

    if name != "machine_op":
        raise KeyError(name)
    server = str(args.get("server") or "").strip()
    op = str(args.get("op") or "").strip()
    note = str(args.get("note") or "")
    if not server or not op:
        return _text("server and op are required", error=True)
    why_not = name_problem(server, op)
    if why_not:
        await store.record_call(pool, cid, name, server[:SERVER_MAX], op[:128], "refused: bad name")
        return _text(why_not, error=True)
    try:
        op_args = _clean_args(args.get("args"))
    except ValueError as exc:
        return _text(str(exc), error=True)

    from server.network import nac_mgmt
    if host_only(op):
        await store.record_call(pool, cid, name, server, op, "refused: the Host's own")
        return _text(f"{op} is part of the Host's own machinery with the machine (its approvals, "
                     "pairings and standing work) and is not run for an outside agent.", error=True)
    # The machine is named from this Host's own records, so the person's page
    # never shows a machine name the agent made up (pages.py writes `server`
    # into the Host's own sentence).
    if server not in {m["name"] for m in await _machines(pool, eid) if m.get("name")}:
        await store.record_call(pool, cid, name, server, op, "refused: no such machine")
        return _text(f"'{server}' is not one of the person's machines. `machines` lists them.",
                     error=True)
    # The Host's own permission first, by the pod gate's own check (the one
    # run_mgmt_op runs): an op this Host may not run itself is refused here,
    # before any ask is filed, so the person is never offered an approval
    # for it and no 'just this once' is spent on it.
    host_ok, host_why, _decision = await nac_mgmt.gate_op(pool, eid, server, op)
    free_read = read_grant_covers(op)
    if not host_ok:
        await store.record_call(pool, cid, name, server, op, HOST_REFUSED)
        # Why, only to an agent the person already let run this op: the
        # reason can be their own words in their grant, or that the machine
        # is quarantined or suspended, and an agent without the op learned
        # none of it before this check (it got the person's link). Every
        # other agent is told only that the Host's own permissions refuse it.
        if store.holds(held, store.READ if free_read else store.OP, server, op):
            words = nac_mgmt.gate_refusal_words(server, op, host_why)
        else:
            words = f"op '{op}' on '{server}' not permitted by this Host's own permissions"
        return _host_refusal(words)
    once_id = None
    if free_read:
        allowed = store.holds(held, store.READ, server, op)
        why = "read"
    elif store.holds(held, store.OP, server, op):
        allowed, why = True, "always"
    else:
        once_id = await store.take_once(pool, cid, server, op, op_args)
        allowed, why = once_id is not None, "once"
    if not allowed:
        if not free_read and await store.refused_recently(pool, cid, server, op, op_args):
            await store.record_call(pool, cid, name, server, op, "refused by the person")
            return _text(f"The person said no to running {op} on {server} with these arguments.",
                         error=True)
        if free_read:
            await store.record_call(pool, cid, name, server, op, "refused: no read")
            return _text("This agent was not allowed to see the person's machines when it was "
                         "connected. They can reconnect it with that ticked.", error=True)
        ask_id = await store.file_ask(pool, cid, store.GRANT_ASK, server, op, op_args, note)
        await store.record_call(pool, cid, name, server, op, "asked the person")
        url = ask_url(origin, ask_id)
        raise NeedsApproval(ask_id, url, (
            f"Running {op} on {server} needs the person's approval. Nothing ran. Ask them to "
            f"open {url} on their Host, then call again with the same arguments."))

    # The agent's note stays on this Host's approval page, shown there as its
    # own words. It is never handed to the machine: the card and the device
    # ask are written in the Host's and the machine's words alone, and the
    # approval binds the op and its arguments only.
    try:
        res = await nac_mgmt.run_mgmt_op(pool, eid, server, op, op_args, note="",
                                         approval_wait_s=0, origin=nac_mgmt.ORIGIN_AGENT)
    except nac_mgmt.MgmtError as e:
        if e.reached:
            # The machine took the job and did not answer in time: it may have
            # run, or still be running. A 'once' stays spent, so the same
            # approval can never run it a second time.
            await store.record_call(pool, cid, name, server, op,
                                    f"sent ({why}), no answer: error {e.status}")
            return _text(f"{op} on {server}: {e.detail}", error=True)
        if once_id:
            await store.give_back_once(pool, once_id)
        await store.record_call(pool, cid, name, server, op, f"not run: error {e.status}")
        return _text(f"{op} on {server} could not run: {e.detail}", error=True)
    if isinstance(res, dict) and res.get("refused_by") == nac_mgmt.REFUSED_BY_HOST:
        # The Host's own gate refused it after the check above (a grant taken
        # back in between): nothing reached the machine, so the person's
        # 'once' is given back, not spent.
        if once_id:
            await store.give_back_once(pool, once_id)
        await store.record_call(pool, cid, name, server, op, HOST_REFUSED)
        return _host_refusal(str(res.get("stderr") or ""))
    if isinstance(res, dict) and res.get("state") in ("asked", "pending_approval"):
        if once_id:
            await store.give_back_once(pool, once_id)
        waiting, words = _waiting_words(res)
        ask_id = await store.file_ask(pool, cid, store.MACHINE_ASK, server, op, op_args, note,
                                      waiting=waiting)
        await store.record_call(pool, cid, name, server, op, "machine asked the person")
        url = ask_url(origin, ask_id)
        raise NeedsApproval(ask_id, url, (
            f"{words} Nothing ran yet. Ask them to open {url} on their Host, then call again "
            "with the same arguments."))
    exit_code = res.get("exit") if isinstance(res, dict) else None
    not_run = res.get(nac_mgmt.NOT_RUN) if isinstance(res, dict) else None
    if not_run:
        # Nothing reached the machine (run_mgmt_op says so in NOT_RUN, a code,
        # never its words): the approval's own state ended it (no way to take
        # one, refused at the device, expired, the device offline), another
        # refusal of this Host's, or the act was already done. The person's
        # 'once' is given back, and the record says what happened.
        if once_id:
            await store.give_back_once(pool, once_id)
        await store.record_call(pool, cid, name, server, op, f"not run: {not_run}")
        out = {k: res.get(k) for k in ("server", "op", "exit", "stdout", "stderr", nac_mgmt.NOT_RUN)}
        return _text(out, error=bool(exit_code))
    await store.record_call(pool, cid, name, server, op, f"ran ({why}), exit {exit_code}")
    out = {k: res.get(k) for k in ("server", "op", "exit", "stdout", "stderr")} \
        if isinstance(res, dict) else {"result": res}
    return _text(out, error=bool(exit_code))
