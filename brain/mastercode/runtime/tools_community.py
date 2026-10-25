"""The community tools: their handlers and the helpers only they use.

Moved out of runtime/tools.py, which re-exports every name defined here. A tool's
category in the tool registry (public_tool_registry `_meta.category`) names the module
its handler lives in."""

from mastercode.runtime.tool_call_ledger import refused as _refused


async def _exec_community_board(entity_id: int, args: dict) -> dict:
    """community_board meta-tool — the host's window onto the members' board.

    All calls ride the pod's bridge client (community.* ops: pinned identity,
    op_id-idempotent writes). digest_to_inbox composes the watch-loop
    primitive: digest -> compact markdown -> inbox.send to the host's own
    user. Pair with schedule_task for standing watches."""
    from server.main import get_bridge_client

    bc = get_bridge_client()
    if bc is None or not getattr(bc, "_connected", False):
        return {"status": "error", "result": "bridge not connected — board unavailable"}

    async def op(op_name: str, params: dict) -> tuple[dict | None, str | None]:
        try:
            env = await bc.request_full(op_name, params)
        except Exception as exc:
            return None, f"bridge call failed: {exc}"
        if env.get("ok"):
            return env.get("data") or {}, None
        return None, env.get("error") or f"{op_name} failed"

    action = (args or {}).get("action") or ""

    if action in ("digest", "digest_to_inbox"):
        params = {
            "since": str(args.get("since") or ""),
            "topics": [str(t) for t in (args.get("topics") or [])],
            "categories": [str(c) for c in (args.get("categories") or [])],
            "limit": int(args.get("limit") or 50),
        }
        data, err = await op("community.digest", params)
        if err:
            return {"status": "error", "result": err}
        items = data.get("items") or []
        if action == "digest":
            return {"status": "ok", "result": data}
        if not items:
            # An empty watch delivers nothing — a "nothing new" inbox message
            # every cycle is noise, not signal.
            return {"status": "ok", "result": {"delivered": False, "items": 0}}
        topics_label = ", ".join(params["topics"]) or "all topics"
        lines = []
        for it in items[:20]:
            lines.append(
                f"- **{it.get('title', '?')}** ({it.get('category')}) — "
                f"{it.get('votes', 0)} vote(s), {it.get('new_posts', 0)} new "
                f"post(s), by {it.get('author_name', '?')} · Community app → "
                f"#/thread/{it.get('id')}"
            )
        body = (
            f"Community board activity for your watch on {topics_label} "
            f"(since {data.get('since', 'the last day')}):\n\n" + "\n".join(lines)
        )
        _, ierr = await op("inbox.send", {
            "subject": f"Community digest: {topics_label}",
            "body": body,
            "body_format": "text/markdown",
            "kind": "system_notification",
            "payload": {"reason": "community_digest", "items": len(items)},
        })
        if ierr:
            return {"status": "error", "result": f"digest built but inbox delivery failed: {ierr}"}
        return {"status": "ok", "result": {"delivered": True, "items": len(items)}}

    if action == "search":
        q = str(args.get("q") or "").strip()
        if not q:
            return _refused("q required for search")
        data, err = await op("community.search", {"q": q, "limit": int(args.get("limit") or 50)})
        return {"status": "error", "result": err} if err else {"status": "ok", "result": data}

    if action == "thread":
        try:
            tid = int(args.get("id"))
        except (TypeError, ValueError):
            return _refused("id (int) required for thread")
        data, err = await op("community.thread", {"id": tid})
        return {"status": "error", "result": err} if err else {"status": "ok", "result": data}

    if action == "post":
        # Never sent from here: the call files the card that shows the exact
        # text over the chat, and only the person's 'Post it' on it sends
        # that text (runtime/board_post_approval, applied by
        # server/routes/tickets.decide_ticket_service).
        from mastercode.runtime import board_post_approval as _bpa
        from server import brain_service
        return await _bpa.propose(brain_service.get_pool(), brain_service._entity_id, args)

    return _refused(f"unknown community_board action: {action!r}")
