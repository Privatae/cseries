"""runtime/board_post_approval.py — a post on the members' board goes out only
on the owner's click on the card that shows its exact text.

Every member of the network reads the board, and a post goes under this
Host's pinned identity, so a post is the owner's to send. community_board
action='post' used to post whenever the Host called it; the owner's yes was
only an instruction to the Host. Now the call files a card instead (the same
card the Host's other owner-reserved asks use, runtime/scheduled_approval):
an overlay over the chat with the title and body exactly as they will go, and
only the person's 'post' on it sends them. 'keep' sends nothing.

'Post it' takes the person's second factor on the card: the code from their
authenticator, typed on the card, checked and burned by
server/routes/tickets.decide_ticket_service before the card is decided (the
step-up a pipeline widen's 'apply' and a connector pairing's 'Let this
account in' take). A missing, wrong, stale or replayed code leaves the card
open and sends nothing, and the code never reaches the Host.

propose_capability files the same card for its Ideas thread (`then`
{'propose': ...}): the proposal ladder runs only after that post went out on
the person's Post it.

The card is locked to the person (`user_only`, `no_pattern`): the Host cannot
decide it, and no "and similar" pattern can approve the next one. What is sent
is the request stored on the card when it was filed, op_id included, never
anything the Host passes later: the text the person read is the text that
goes, and a retry cannot post it twice.

Votes and flags are not here: the Host has no tool that votes or flags
(community_board's actions are digest, digest_to_inbox, search, thread and
post); only the person's own Community app does, through
server/routes/community.py.
"""
from __future__ import annotations

import logging
import uuid

log = logging.getLogger("mastercode.board_post_approval")

PROPOSAL_KIND = "board_post_proposal"   # = server/routes/tickets.BOARD_POST_PROPOSAL_KIND
PAYLOAD_KEY = "board_post"
THEN_KEY = "board_post_then"   # what runs after the post went out (propose_capability)
OPT_POST = "post"
OPT_KEEP = "keep"


def request_of(args: dict) -> tuple[dict | None, str]:
    """(the community.post request this call would send, '') or (None, why
    not). A new thread needs a category, a title and a body; a reply needs a
    thread_id and a body."""
    a = args or {}
    body = str(a.get("body") or "")
    if not body.strip():
        return None, "body required for post"
    params: dict = {"op_id": f"host-{uuid.uuid4()}", "body": body}
    if a.get("thread_id") is not None:
        try:
            params["thread_id"] = int(a["thread_id"])
            if a.get("parent_id") is not None:
                params["parent_id"] = int(a["parent_id"])
        except (TypeError, ValueError):
            return None, "thread_id and parent_id must be integers"
        return params, ""
    category = str(a.get("category") or "").strip()
    title = str(a.get("title") or "")
    if not category or not title.strip():
        return None, "a new thread needs category, title and body (a reply needs thread_id and body)"
    params["category"] = category
    params["title"] = title
    return params, ""


def card_words(params: dict) -> tuple[str, list]:
    """(sitrep, options): the card shows the post exactly as it will go. PURE."""
    if params.get("thread_id") is not None:
        where = f"a reply in thread {params['thread_id']} on the members' community board"
        text = params["body"]
    else:
        where = f"a new thread in {params['category']} on the members' community board"
        text = f"Title: {params['title']}\n\n{params['body']}"
    sitrep = (f"Your Host wants to post {where}. Every member of the network can read it, "
              "and it goes under your Host's name. Read it first: it is sent exactly as "
              "written below, and only if you press Post it with a code from your "
              "authenticator.\n\n" + text)
    options = [
        {"id": OPT_POST, "label": "Post it",
         "consequence": ("This exact text goes on the board, where every member can read it. "
                         "It needs a current code from your authenticator.")},
        {"id": OPT_KEEP, "label": "Don't post",
         "consequence": "Nothing is posted. Your note goes to your Host, so it can change the text and ask again."},
    ]
    return sitrep, options


async def propose(pool, entity_id: int, args: dict, *, then: dict | None = None) -> dict:
    """community_board action='post' (and propose_capability's Ideas thread):
    file the card instead of posting. `then` is what runs after the post went
    out on the person's Post it ({'propose': {'done_looks_like': ...}}: the
    proposal ladder on the new thread); it is stored on the card beside the
    request, never sent with it."""
    from mastercode.runtime.tool_call_ledger import refused as _refused
    params, why = request_of(args)
    if params is None:
        return _refused(why)
    if pool is None or entity_id is None:
        return {"status": "error", "result": "pool unavailable: nothing was posted"}
    sitrep, options = card_words(params)
    from server.routes import tickets as _tk
    metadata = {"kind": PROPOSAL_KIND, PAYLOAD_KEY: params,
                "user_only": True, "no_pattern": True}
    if then:
        metadata[THEN_KEY] = then
    t = await _tk.create_ticket_service(
        pool, entity_id, source="runtime", sitrep=sitrep, options=options,
        context={"board": "members"}, priority=60, metadata=metadata)
    tid = t.get("id") or t.get("ticket_id")
    return {"status": "ok", "result": {
        "posted": False, "awaiting_approval": True, "ticket_id": tid,
        "ask": {"ticket_id": tid, "sitrep": sitrep, "options": options},
        "note": ("NOT POSTED YET: a board post goes out only when your user presses Post it "
                 "on the card now over the chat, which shows this exact text, and confirms "
                 "it there with the code from their authenticator. Tell them it is there "
                 "and that every member of the network can read it. You cannot decide the "
                 "card, and never ask them for the code: it is typed on the card and you "
                 "never see it. Their Post it sends it, so never call post again for it; "
                 "to change the text, call post with the new text, which puts up a new "
                 "card. Never say it was posted before their answer says they chose Post it.")}}


async def apply_proposal(pool, entity_id: int, metadata: dict, *, option_id: str,
                         ticket_id: int) -> dict:
    """The person decided the card. 'post' sends the stored request as it
    is; anything else sends nothing."""
    if option_id != OPT_POST:
        return {"posted": False, "why": "not posted: the person chose not to post it"}
    params = (metadata or {}).get(PAYLOAD_KEY)
    if not isinstance(params, dict) or not str(params.get("body") or "").strip():
        return {"posted": False, "why": "the card carries no post"}
    from server.main import get_bridge_client
    bc = get_bridge_client()
    if bc is None or not getattr(bc, "_connected", False):
        return {"posted": False, "why": "the network can't be reached right now, so nothing was posted"}
    try:
        env = await bc.request_full("community.post", dict(params))
    except Exception as e:  # noqa: BLE001 — said on the ticket
        return {"posted": False, "why": f"the post did not go: {e}"[:300]}
    if not env.get("ok"):
        return {"posted": False, "why": str(env.get("error") or "the board refused the post")[:300]}
    log.info("ticket %s: board post sent", ticket_id)
    data = env.get("data") or {}
    out = {"posted": True, "data": data}
    then = (metadata or {}).get(THEN_KEY)
    if isinstance(then, dict) and isinstance(then.get("propose"), dict):
        out["proposal"] = await _ladder(data, then["propose"], ticket_id)
    return out


async def _ladder(data: dict, spec: dict, ticket_id: int) -> dict:
    """propose_capability's second step, after its thread went out: the
    governed proposal on that thread (server/routes/community.propose). The
    post stands whatever this answers."""
    thread_id = None
    if isinstance(data, dict):
        thread_id = (data.get("id") or data.get("thread_id")
                     or (data.get("thread") or {}).get("id"))
    try:
        thread_id = int(thread_id)
    except (TypeError, ValueError):
        return {"filed": False, "why": "the board gave the new thread no id, so no proposal was filed"}
    try:
        from server.routes import community as _comm
        got = await _comm.propose(thread_id, done_looks_like=str(spec.get("done_looks_like") or ""))
    except Exception as e:  # noqa: BLE001 — said on the ticket
        log.warning("ticket %s: proposal not filed: %s", ticket_id, e)
        return {"filed": False, "thread_id": thread_id,
                "why": f"the proposal was not filed: {getattr(e, 'detail', None) or e}"[:300]}
    return {"filed": True, "thread_id": thread_id,
            "proposal_ref": (got or {}).get("proposal_ref"), "url": (got or {}).get("url")}
