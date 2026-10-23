"""The governance tools: their handlers and the helpers only they use.

Moved out of runtime/tools.py, which re-exports every name defined here. A tool's
category in the tool registry (public_tool_registry `_meta.category`) names the module
its handler lives in."""

from mastercode.runtime.tool_call_ledger import refused as _refused, refused_if_caller as _refused_if_caller


async def _exec_decision_resolve(args: dict) -> dict:
    """Resolve one of the host's OWN pending decisions. The resolve service scopes to this pod's
    entity (WHERE entity_id = _self_entity_id), so a host can only resolve its own queue; the
    platform-change-approval path fires only for cseries_proposal rows, which live solely in the
    operator Host's queue — that is the public-own vs operator split, enforced by scoping.

    Calls the service directly (no route shim): same code path as the FastAPI
    /api/decisions/{id}/resolve route."""
    try:
        from server import brain_service, decisions_service
        from server.routes.decisions import _self_entity_id
        a = args or {}
        did = a.get("decision_id")
        if did is None:
            return _refused("decision_id required")
        chosen = a.get("chosen_key")
        if not chosen:
            return _refused("chosen_key required")
        pool = brain_service.get_pool()
        if pool is None:
            return {"status": "error", "result": "brain pool not ready"}
        eid = _self_entity_id()
        if eid is None:
            return {"status": "error", "result": "this container has no host id bound"}
        try:
            res = await decisions_service.resolve_decision_service(
                pool, eid, int(did), str(chosen), a.get("chosen_rationale"),
            )
        except decisions_service.DecisionResolveError as e:
            return _refused_if_caller(e, f"decision_resolve failed: {e.detail}")
        return {"status": "ok", "result": res}
    except Exception as e:
        return _refused_if_caller(e, f"decision_resolve failed: {getattr(e, 'detail', None) or e}")


async def _exec_propose_capability(args: dict) -> dict:
    """Escalate a VERIFIED capability gap into the community pipeline: create an
    Ideas thread on the board carrying the gap analysis, then open the governed
    community/proposals feature-request PR with this host as its author — the
    exact ladder the board's 'propose this' uses (server/routes/community.py
    propose → capability_propose.propose_feature_request). The PR is filed on a
    branch of the proposals repo itself; there is no fork. Call only after the
    user consents; the tool performs both steps and returns the refs."""
    a = args or {}
    title = str(a.get("title") or "").strip()
    body = str(a.get("body") or "").strip()
    done_looks_like = str(a.get("done_looks_like") or "").strip()
    if not title or not body:
        return _refused("title and body (the gap analysis) are required")
    try:
        import uuid as _uuid

        from server.routes import community as _comm

        # Resolve the Ideas category from the live board (slugs are communityd's).
        cats = ((await _comm._op("community.categories", {})) or {})
        cat_rows = cats.get("categories") if isinstance(cats, dict) else None
        cat_rows = cat_rows if isinstance(cat_rows, list) else (cats if isinstance(cats, list) else [])
        slug = None
        for c in cat_rows:
            label = f"{c.get('slug', '')} {c.get('name', '')}".lower() if isinstance(c, dict) else str(c).lower()
            if "idea" in label:
                slug = (c.get("slug") or c.get("id") or c.get("name")) if isinstance(c, dict) else str(c)
                break
        if slug is None and cat_rows:
            c0 = cat_rows[0]
            slug = (c0.get("slug") or c0.get("id") or c0.get("name")) if isinstance(c0, dict) else str(c0)
        if not slug:
            return {"status": "error", "result": "community board has no categories — board unreachable?"}

        created = await _comm._op("community.post", {
            "op_id": f"cap-{_uuid.uuid4()}",
            "category": str(slug),
            "title": title,
            "body": body,
        })
        thread_id = None
        if isinstance(created, dict):
            thread_id = (created.get("id") or created.get("thread_id")
                         or (created.get("thread") or {}).get("id"))
        if not thread_id:
            return {"status": "error",
                    "result": f"thread create returned no id (got keys: {sorted(created) if isinstance(created, dict) else type(created).__name__})"}

        laddered = await _comm.propose(int(thread_id), done_looks_like=done_looks_like)
        return {"status": "ok", "result": {
            "thread_id": int(thread_id),
            "proposal_ref": (laddered or {}).get("proposal_ref"),
            "url": (laddered or {}).get("url"),
        }}
    except Exception as e:
        return _refused_if_caller(e, f"propose_capability failed: {getattr(e, 'detail', None) or e}")
