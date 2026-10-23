"""The vault tools: their handlers and the helpers only they use.

Moved out of runtime/tools.py, which re-exports every name defined here. A tool's
category in the tool registry (public_tool_registry `_meta.category`) names the module
its handler lives in."""

import json
import logging

from mastercode.runtime.tool_call_ledger import refused as _refused, refused_if_caller as _refused_if_caller

log = logging.getLogger("cseries.mastercode.entity_runtime")


# Phase-3a BYO compute/inference connections (s2-connect-local + s2-connect-runpod-api).
# Provider-agnostic 'compute' connection kinds the host registers for itself:
#   local  — the host's OWN local inference endpoint (Ollama / vLLM / llama.cpp).
#            base_url required; an optional bearer token is SEALED IN THE VAULT.
#            assign_role wires the base_url into model_routing so the local
#            dispatch (_call_local via resolve_role_route) reads it.
#   runpod — a BYO RunPod serverless endpoint for BATCH only (NOT chat — that
#            would leak the system prompt). endpoint_id + api_key required; the
#            api_key is SEALED IN THE VAULT and used only through the broker.
#            BYO = the platform does not bill inference.
#
# The credential NEVER lands in plaintext: only its vault logical name is stored
# in behavioral_params.compute_connections; the raw bytes live in server/vault/
# sealed to the host and are resolved via the broker (authz.use_credential).
_COMPUTE_KINDS = {"local", "runpod"}
# BYO compute endpoints may serve ONLY non-prompt roles (embedding/image) unless
# the kind is the host's own governed local Ollama. Single source of truth lives
# in inference_config; mirror its allowed set here for assign_role.
_COMPUTE_BATCH_ROLES = {"embedding", "image"}


async def _compute_connections(bp: dict) -> list:
    cc = bp.get("compute_connections")
    return cc if isinstance(cc, list) else []


async def _exec_connect_compute(args: dict) -> dict:
    """Register / manage the host's OWN BYO inference compute connections.
    Service-layer (no HTTP shim): seals any token into server/vault (api.store_secret),
    stores only the non-secret config in behavioral_params.compute_connections, and
    — for the RunPod BATCH path — dispatches through the vault broker
    (authz.use_credential) so the agent never holds the api key. Ops:
      register -> add a connection      (kind, base_url|endpoint_id, [token|api_key], [label])
      list     -> the host's connections (no secret bytes)               (no args)
      assign_role -> point a non-prompt role at a 'local' connection      (connection_id, role)
      revoke   -> remove a connection + its sealed credential             (connection_id)
      dispatch -> BYO RunPod batch run via the brokered key               (connection_id, input)
    The BYO leak-guard is intact: a base_url/runpod endpoint is confined to
    non-prompt roles (embedding/image); chat/plan/review/etc. stay governed.
    """
    a = args or {}
    op = (a.get("op") or "list").strip()
    _OPS = {"register", "list", "assign_role", "revoke", "dispatch"}
    if op not in _OPS:
        return _refused(f"op must be one of {sorted(_OPS)} (got {op!r})")
    try:
        import json as _json
        import uuid as _uuid
        from datetime import datetime as _dt, timezone as _tz
        from server import brain_service
        from server.vault import api as vault_api
        from server.vault import authz as vault_authz
    except Exception as e:
        return _refused_if_caller(e, f"connect_compute unavailable: {e}")
    pool = brain_service.get_pool()
    if pool is None:
        return {"status": "error", "result": "connect_compute failed: pool unavailable"}
    eid = brain_service._entity_id
    actor = f"connect_compute:{eid}"

    async def _load_bp() -> dict:
        row = await pool.fetchrow(
            "SELECT behavioral_params FROM entity_registry WHERE id=$1", eid)
        bp = (row and row["behavioral_params"]) or {}
        if isinstance(bp, str):
            try:
                bp = json.loads(bp)
            except Exception:
                bp = {}
        return bp if isinstance(bp, dict) else {}

    async def _save_bp(bp: dict) -> None:
        await pool.execute(
            "UPDATE entity_registry SET behavioral_params=$1::jsonb WHERE id=$2",
            _json.dumps(bp), eid)

    try:
        if op == "list":
            bp = await _load_bp()
            conns = await _compute_connections(bp)
            # Strip nothing secret is here — cred_name is a vault pointer, not a value.
            return {"status": "ok", "result": {"connections": conns, "count": len(conns)}}

        if op == "register":
            kind = (a.get("kind") or "").strip()
            if kind not in _COMPUTE_KINDS:
                return _refused(f"kind must be one of {sorted(_COMPUTE_KINDS)}")
            label = (a.get("label") or kind).strip()
            conn_id = f"cmp_{_uuid.uuid4().hex[:12]}"
            entry: dict = {
                "id": conn_id, "kind": kind, "label": label,
                "created_at": _dt.now(_tz.utc).isoformat(),
            }
            cred_name = None
            if kind == "local":
                base_url = (a.get("base_url") or "").strip()
                if not (base_url.startswith("http://") or base_url.startswith("https://")):
                    return _refused("local kind requires base_url (http(s) endpoint)")
                entry["base_url"] = base_url
                token = a.get("token")
                if token:
                    cred_name = f"compute:{conn_id}"
                    await vault_api.store_secret(
                        pool, entity_id=eid, name=cred_name, cred_type="compute_token",
                        plaintext=str(token).encode("utf-8"), actor=actor)
                    entry["cred_name"] = cred_name
            else:  # runpod
                endpoint_id = (a.get("endpoint_id") or "").strip()
                api_key = a.get("api_key")
                if not endpoint_id:
                    return _refused("runpod kind requires endpoint_id")
                if not api_key:
                    return _refused("runpod kind requires api_key (sealed in the vault)")
                entry["endpoint_id"] = endpoint_id
                # RunPod serverless base — the BATCH endpoint (NOT a chat path).
                entry["base_url"] = f"https://api.runpod.ai/v2/{endpoint_id}"
                entry["batch_only"] = True
                cred_name = f"runpod:{conn_id}"
                await vault_api.store_secret(
                    pool, entity_id=eid, name=cred_name, cred_type="runpod_api_key",
                    plaintext=str(api_key).encode("utf-8"), actor=actor)
                entry["cred_name"] = cred_name

            bp = await _load_bp()
            conns = await _compute_connections(bp)
            conns.append(entry)
            bp["compute_connections"] = conns
            await _save_bp(bp)
            return {"status": "ok", "result": {"registered": conn_id, "kind": kind,
                                               "base_url": entry.get("base_url"),
                                               "sealed_credential": cred_name is not None}}

        if op == "assign_role":
            connection_id = (a.get("connection_id") or "").strip()
            role = (a.get("role") or "").strip()
            if not connection_id or not role:
                return _refused("connection_id and role are required for assign_role")
            bp = await _load_bp()
            conns = await _compute_connections(bp)
            conn = next((c for c in conns if c.get("id") == connection_id), None)
            if conn is None:
                return _refused(f"connection not found: {connection_id}")
            # LEAK GUARD: a BYO base_url endpoint may only serve a non-prompt role.
            # A 'local' kind (the host's own governed Ollama) may also serve any
            # role per provider_allowed_for_role; here we keep assign_role to the
            # non-prompt batch roles to confine the system prompt regardless of kind.
            from mastercode.inference_config import provider_allowed_for_role
            provider = "local" if conn.get("kind") == "local" else "self"
            if role not in _COMPUTE_BATCH_ROLES or not provider_allowed_for_role(provider, role):
                return _refused(f"role '{role}' can't carry a BYO compute endpoint: "
                                   "a prompt-bearing role would stream the system prompt to a "
                                   "user-controlled host (leak protection). Allowed: "
                                   f"{sorted(_COMPUTE_BATCH_ROLES)}.")
            # Embedding is configured through its own store + re-embed flow. Route
            # the BYO endpoint via apply_embedding_switch so the dim is validated
            # against a real storage column and a re-embed is enqueued.
            if role == "embedding":
                from server.routes.embedding import apply_embedding_switch
                emodel = (a.get("model") or conn.get("label") or "byo-embed").strip()
                edim = a.get("dim") or conn.get("dim")
                res = await apply_embedding_switch(
                    pool, eid, model=emodel, provider=provider,
                    base_url=conn.get("base_url"),
                    dim=int(edim) if edim else None,
                    cred_name=conn.get("cred_name"))
                if isinstance(res, dict) and res.get("error"):
                    return {"status": "error", "result": f"assign embedding failed: {res['error']}"}
                return {"status": "ok", "result": {"assigned": connection_id, "role": "embedding",
                                                   "base_url": conn.get("base_url"), **(res or {})}}
            else:
                # New route shape: {provider, model, base_url,
                # connection_id} — the legacy lane/model_id keys are dead.
                # normalize_provider validates + canonicalizes (a BYO 'self'
                # endpoint rides as 'openrouter'; the image bot dials base_url
                # directly and never reads provider).
                from mastercode.inference_config import normalize_provider
                routing = bp.get("model_routing") or {}
                rentry = routing.get(role) or {}
                _model = (a.get("model") or rentry.get("model")
                          or rentry.get("model_id") or conn.get("label") or "byo").strip()
                _prov, _model = normalize_provider(provider, _model)
                if not _prov:
                    return _refused(f"provider for connection {connection_id} is not "
                                       "routable — pick a Keys provider or a local endpoint.")
                routing[role] = {
                    "provider": _prov,
                    "model": _model,
                    "base_url": conn.get("base_url"),
                    "connection_id": connection_id,
                }
                bp["model_routing"] = routing
                # On the ledger like every other write of this table
                # (v158): a BYO endpoint on a batch role, set by the host.
                # Confined to _COMPUTE_BATCH_ROLES and it mints no approval.
                try:
                    from server.routes.inference import _log_route_apply
                    await _log_route_apply(pool, eid, role, routing[role], "host", None)
                except Exception as _le:  # noqa: BLE001
                    log.debug("model_route_log (assign_role) skipped: %s", _le)
            await _save_bp(bp)
            return {"status": "ok", "result": {"assigned": connection_id, "role": role,
                                               "base_url": conn.get("base_url")}}

        if op == "revoke":
            connection_id = (a.get("connection_id") or "").strip()
            if not connection_id:
                return _refused("connection_id is required for revoke")
            bp = await _load_bp()
            conns = await _compute_connections(bp)
            conn = next((c for c in conns if c.get("id") == connection_id), None)
            if conn is None:
                return _refused(f"connection not found: {connection_id}")
            if conn.get("cred_name"):
                try:
                    await vault_api.revoke_secret(pool, entity_id=eid, name=conn["cred_name"], actor=actor)
                except Exception:
                    pass
            bp["compute_connections"] = [c for c in conns if c.get("id") != connection_id]
            await _save_bp(bp)
            return {"status": "ok", "result": {"revoked": connection_id}}

        # op == "dispatch" — BYO RunPod BATCH run. The api key NEVER reaches the
        # agent: the brokered http_call opens the sealed key in-pod, injects it as
        # Bearer, and returns only the response. BYO = the platform does not
        # bill inference, so nothing is charged here.
        connection_id = (a.get("connection_id") or "").strip()
        if not connection_id:
            return _refused("connection_id is required for dispatch")
        bp = await _load_bp()
        conns = await _compute_connections(bp)
        conn = next((c for c in conns if c.get("id") == connection_id), None)
        if conn is None:
            return _refused(f"connection not found: {connection_id}")
        if conn.get("kind") != "runpod":
            return _refused("dispatch is only for runpod (BYO batch) connections")
        cred_name = conn.get("cred_name")
        if not cred_name:
            return _refused("runpod connection has no sealed api key")
        run_input = a.get("input")
        if run_input is None:
            return _refused("dispatch requires 'input' (the RunPod job payload)")
        try:
            result = await vault_authz.use_credential(
                pool, entity_id=eid, name=cred_name, action="http_call",
                request={
                    "url": f"{conn['base_url']}/run",
                    "method": "POST",
                    "json": {"input": run_input},
                    "auth_scheme": "bearer",
                },
                actor=actor)
        except vault_authz.VaultAuthzError as e:
            return _refused(f"runpod dispatch refused: {e}")
        return {"status": "ok", "result": {"connection_id": connection_id,
                                           "byo_billing": "egress_only",
                                           "response": result}}
    except Exception as e:
        return _refused_if_caller(e, f"connect_compute failed: {getattr(e, 'detail', None) or e}")


# The actions keys_manage's op='use' may broker. Mirrors the enum its schema
# advertises (public_tool_registry: action -> ['http_call', 'sign']); pinned
# equal by tests/test_autofill_broker_guard.py so the two cannot drift again.
# 'browser_type' is deliberately ABSENT: typing a saved form entry into a page
# goes through the `autofill` tool, which applies the user's standing grant.
_KEYS_MANAGE_USE_ACTIONS = frozenset({"http_call", "sign"})


async def _exec_keys_manage(args: dict) -> dict:
    """Manage the host's OWN sealed credential vault. Calls the in-process vault
    service (server.vault.api) + the broker (server.vault.authz) DIRECTLY — no HTTP
    shim, no @router fn. The agent NEVER receives raw secret bytes: list returns
    metadata, add seals a value, use brokers an action server-side (the secret stays
    inside the broker), revoke supersedes. There is intentionally NO reveal op —
    revealing a raw key needs the user's TOTP step-up and lives only in the Keys app
    UI (POST /api/vault/secrets/{name}/reveal). Ops:
      list   -> credential metadata (name/type/created)        (no args)
      add    -> seal a new credential                           (name, cred_type, value | value_b64)
      revoke -> supersede a credential                          (name)
      use    -> broker an action, results only                  (name, action, request)

    Sovereign user-owned keys (opt-in; default behavior unchanged): a host
    can seal a credential TO the user. The user's PRIVATE key
    lives only in their mesh, so the host/platform can seal to the user but can
    NEVER reopen it.
      seal_for_user     -> seal a value TO the user (host can't read) (user_id, name, cred_type, value | value_b64)
    REGISTERING the user's public key is NOT a host op: the
    registered key is the recipient of every later seal_for_user and the register
    is an upsert, so a host that could write it could redirect the user's own
    sealed credentials to a key of its choosing. Registration is the user's act —
    POST /api/vault/user-keys with their TOTP step-up (or the backup key wizard
    in Settings for the owner's own key). Tell the user that; do not try it here.
    """
    a = args or {}
    op = (a.get("op") or "list").strip()
    _OPS = {"list", "add", "revoke", "use", "seal_for_user"}
    if op == "register_user_key":
        return {"status": "denied", "result": (
            "register_user_key is not a host op any more: registering a recipient "
            "key decides who can open everything sealed to that user, so it is the "
            "user's own act — POST /api/vault/user-keys with their authenticator "
            "code, or the backup key wizard in Settings for their own key. "
            "seal_for_user still works once a key is registered.")}
    if op not in _OPS:
        return _refused(f"op must be one of {sorted(_OPS)} (got {op!r})")
    if op in ("add", "revoke", "use"):
        # An MCP server's sign-in (server/mcp_oauth.py) is the person's token
        # for that one server: used only by its connection, ended only from
        # the Network app. A `use` would send it to whatever URL the call names.
        from server.mcp_oauth import is_reserved_key_name
        from server.vault.api import is_git_server_token
        if is_git_server_token(a.get("name")):
            # The account the Host signs in as on the person's git server is the
            # owner's choice, checked when they connect it (server/git_server.py).
            return _refused(f"{str(a.get('name')).strip()!r} is the token of the git server "
                            "connected in Settings: only the Host's own git clients use it, and "
                            "the person connects or disconnects the server in Settings, Git server")
        if is_reserved_key_name(a.get("name")):
            return _refused(f"{str(a.get('name')).strip()!r} is an MCP server's sign-in: only that "
                            "server's connection uses it, and the person signs in or out on its card "
                            "in the Network app")
    if op == "use":
        # use brokers a credential to the URL the call names. On a turn that
        # read untrusted content, that URL may be someone else's choice.
        from mastercode.runtime import turn_taint
        why = turn_taint.action_refusal("a credential use (keys_manage op='use')")
        if why is not None:
            return {"status": "denied", "reason": "untrusted_turn", "result": f"Not run: {why}."}
    # Validate the brokered action HERE, with the other input checks and
    # BEFORE any pool/vault work: rejecting a disallowed action needs no
    # database, and a guard that sits behind a dependency is a guard that
    # does not run when the dependency is missing.
    #
    # The schema advertises enum ['http_call', 'sign'] and nothing enforced it,
    # so `action` reached use_credential verbatim — including 'browser_type',
    # the one action with its own consent model (a standing per-site grant, and
    # a rule about where a saved password may be typed), demonstrated end to
    # end. The broker now
    # guards form entries itself (authz._guard_form_entry), so this is the
    # second layer — but a tool whose declared enum is not enforced is its own
    # defect regardless.
    if op == "use":
        _action = (a.get("action") or "").strip()
        if _action and _action not in _KEYS_MANAGE_USE_ACTIONS:
            return _refused(f"action must be one of {sorted(_KEYS_MANAGE_USE_ACTIONS)} "
                f"(got {_action!r}). Typing a saved form entry into a page is "
                f"the `autofill` tool's job — it applies the user's standing "
                f"per-site grant, which this path does not.")
    try:
        from server import brain_service
        from server.vault import api as vault_api
        from server.vault import authz
    except Exception as e:
        return _refused_if_caller(e, f"keys_manage unavailable: {e}")
    pool = brain_service.get_pool()
    if pool is None:
        return {"status": "error", "result": "keys_manage failed: pool unavailable"}
    eid = brain_service._entity_id
    actor = f"keys_manage:{eid}"

    try:
        if op == "list":
            rows = await vault_api.list_secrets(pool, entity_id=eid)
            return {"status": "ok", "result": {"credentials": [
                {"name": r["logical_name"], "cred_type": r["cred_type"],
                 "created_at": r["created_at"].isoformat(),
                 # A deactivated credential is stored but switched off — it
                 # resolves as absent everywhere; flag it, never blend it.
                 "deactivated": bool(r.get("deactivated_at"))} for r in rows]}}

        if op == "add":
            name = (a.get("name") or "").strip()
            if not name:
                return _refused("name is required for op='add'")
            cred_type = (a.get("cred_type") or "opaque").strip()
            if a.get("value_b64"):
                import base64
                try:
                    pt = base64.b64decode(a["value_b64"])
                except Exception:
                    return {"status": "error", "result": "value_b64 is not valid base64"}
            elif a.get("value") is not None:
                pt = str(a.get("value")).encode("utf-8")
            else:
                return _refused("add requires 'value' (text) or 'value_b64'")
            vid = await vault_api.store_secret(
                pool, entity_id=eid, name=name, cred_type=cred_type, plaintext=pt, actor=actor)
            res: dict = {"stored": name, "cred_type": cred_type, "id": vid}
            # Storing a harness credential RECONNECTS it. Same entry point the
            # HTTP route uses — without it, adding a key through the host after
            # a revoke leaves the tombstone in place and the harness stays dead
            # with a perfectly good credential sitting in Keys.
            try:
                from server.network import harness_state
                reconnected = await harness_state.reconnect_for_credential(name)
                if reconnected:
                    res["reconnected"] = reconnected
            except Exception as e:  # pragma: no cover - defensive
                log.warning("keys_manage: reconnect for %r failed: %r", name, e)
            return {"status": "ok", "result": res}

        if op == "revoke":
            name = (a.get("name") or "").strip()
            if not name:
                return _refused("name is required for op='revoke'")
            # A harness credential's disconnect marker goes first, as on the Vault
            # route: if it cannot be written nothing is revoked, because without it
            # the harness signs back in once its credential is removed.
            from server.network import harness_state
            try:
                marked, newly = await harness_state.record_disconnect(name)
            except harness_state.DisconnectNotRecorded as e:
                return {"status": "error", "result": f"keys_manage revoke failed: {e}"}
            ok = await vault_api.revoke_secret(pool, entity_id=eid, name=name, actor=actor)
            if not ok:
                await harness_state.lift_unused_disconnect(marked, newly)
                return _refused(f"no active credential named {name!r}")
            res = {"revoked": name}
            # Revoking a harness credential DISCONNECTS it. Same entry point the
            # HTTP route uses — without it, revoking through the host left the
            # runtime file on disk and the reconcilers rebuilt it, so the
            # harness kept running and the revoke was cosmetic.
            try:
                from server.network import harness_state
                disconnected = await harness_state.disconnect_for_credential(
                    name, actor=actor)
                if disconnected:
                    res["disconnected"] = disconnected
            except Exception as e:  # pragma: no cover - defensive
                log.warning("keys_manage: disconnect for %r failed: %r", name, e)
            return {"status": "ok", "result": res}

        if op == "seal_for_user":
            # Seal a credential TO the user (sovereign): only the user's private key
            # — held in their mesh — can open it. The host/platform CANNOT reopen it.
            # The user's public key must have been registered by the USER first
            # (POST /api/vault/user-keys, step-up gated) — never by this tool.
            import base64
            user_id = (a.get("user_id") or "").strip()
            name = (a.get("name") or "").strip()
            if not user_id or not name:
                return _refused("user_id and name are required for op='seal_for_user'")
            cred_type = (a.get("cred_type") or "opaque").strip()
            if a.get("value_b64"):
                try:
                    pt = base64.b64decode(a["value_b64"])
                except Exception:
                    return {"status": "error", "result": "value_b64 is not valid base64"}
            elif a.get("value") is not None:
                pt = str(a.get("value")).encode("utf-8")
            else:
                return _refused("seal_for_user requires 'value' (text) or 'value_b64'")
            try:
                vid = await vault_api.seal_for_user(
                    pool, user_id=user_id, name=name, cred_type=cred_type,
                    plaintext=pt, actor=actor)
            except KeyError:
                return _refused(f"no registered public key for user {user_id!r} — the user "
                                   "registers one themselves (POST /api/vault/user-keys with "
                                   "their authenticator code); a host cannot")
            return {"status": "ok", "result": {
                "sealed_for_user": user_id, "name": name, "cred_type": cred_type,
                "id": vid, "host_can_read": False}}

        # op == "use"
        name = (a.get("name") or "").strip()
        action = (a.get("action") or "").strip()
        if not name or not action:
            return _refused("name and action are required for op='use'")
        try:
            result = await authz.use_credential(
                pool, entity_id=eid, name=name, action=action,
                request=a.get("request") or {}, actor=actor)
        except authz.VaultAuthzError as e:
            return _refused(f"use refused: {e}")
        return {"status": "ok", "result": {"name": name, "action": action, "result": result}}
    except Exception as e:
        return _refused_if_caller(e, f"keys_manage failed: {getattr(e, 'detail', None) or e}")


# The old sdk_continue meta-tool retired with the /factory-run sidecar build
# path: a follow-up is build(action='continue') — a seeded rerun
# on the pool, detached + owned + plan-gated like every other build.


async def _exec_autofill(args: dict, entity_id: int, pool) -> dict:
    """Fill a site's form from the user's saved entries, under their grant.

    Thin: every rule lives in server/browser_autofill.py so the HTTP surface
    (server/routes/browser_forms.py) and this one cannot drift on who may
    fill what. This function only shapes the answer for a model.

    No op here can return a saved VALUE — `saved` returns keys and a
    password flag, `fill` returns which refs were filled. That is the same
    promise keys_manage makes, and it is why this tool is safe to hand a
    model at all.
    """
    from server import browser_autofill as autofill

    op = (args.get("op") or "saved").strip()
    host = (args.get("host") or "").strip().lower()
    if pool is None or not entity_id:
        return {"status": "error", "result": "no pool/host id for autofill"}

    try:
        if op == "list":
            grants = await autofill.list_grants(pool, entity_id=entity_id)
            return {"status": "ok", "result": {
                "grants": grants,
                "note": ("Sites with a standing grant may be filled unattended. "
                         "last_used_at null means the grant has never been used."),
            }}

        if not host:
            return _refused(f"op={op!r} needs a host, e.g. 'acme.example'")

        if op == "saved":
            entries = await autofill.saved_for(pool, entity_id=entity_id, host=host)
            granted = await autofill.is_granted(pool, entity_id=entity_id, host=host)
            return {"status": "ok", "result": {
                "host": host, "entries": entries, "granted": granted,
                "note": ("Values are never returned. `secret: true` means the entry "
                         "is a password and can only be typed into a password box."
                         + ("" if granted else
                            " There is NO standing grant for this host yet, so "
                            "filling will be refused. Ask the user to allow this "
                            "site in the Keys app — you cannot create the grant "
                            "yourself."))
            }}

        # No 'grant' arm, deliberately. Creating a standing grant is the user's
        # act: a grant WIDENS what may happen
        # unattended, and the whole point of the consent is that it does not
        # originate from a model that a hostile page can talk to. It is made in
        # the Keys app, beside the entries it governs
        # (POST /api/browser/forms/grants, from a user surface). Revoke stays
        # here because it only TIGHTENS — the same asymmetry the Network app
        # already applies to NAC grants (system-apps/network/app.js:983
        # "Granting is a widen (step-up); revoking a grant only tightens (free)").
        if op == "grant":
            return {"status": "denied", "result": (
                f"You cannot grant autofill for {host} — that is the user's own "
                f"act. Ask them to allow it in the Keys app, beside the entries "
                f"it governs. Once they have, op='fill' will work and op='saved' "
                f"will report granted: true.")}

        if op == "revoke":
            revoked = await autofill.revoke(
                pool, entity_id=entity_id, host=host, actor=f"host:{entity_id}")
            return {"status": "ok", "result": {
                "host": host, "revoked": revoked,
                "note": "The saved entries are kept; only unattended use stops."}}

        if op == "fill":
            session = (args.get("session") or "").strip()
            if not session:
                return _refused("fill needs the browser session name to type into")
            mapping = args.get("mapping")
            if not isinstance(mapping, list):
                return _refused("fill needs mapping: [{ref, key}] from a snapshot")
            return {"status": "ok", "result": await autofill.fill(
                pool, entity_id=entity_id, host=host, session=session,
                mapping=mapping, actor=f"host:{entity_id}")}

        return _refused(f"unknown autofill op {op!r}")
    except autofill.AutofillError as e:
        # The refusal text is written to be shown to the user as-is: it
        # names the host and says what to ask for.
        return {"status": "denied", "result": str(e)}
