"""The apps tools: their handlers and the helpers only they use.

Moved out of runtime/tools.py, which re-exports every name defined here. A tool's
category in the tool registry (public_tool_registry `_meta.category`) names the module
its handler lives in."""

from mastercode.runtime.tool_call_ledger import refused as _refused, refused_if_caller as _refused_if_caller
from mastercode.runtime.tools_shared import _active_review_hold, _ci_merge_gate, _review_hold_reason


_PROBE_BODY_MAX = 20_000
_PROBE_LOG_MAX = 40_000


async def _exec_browser_agent(args: dict) -> dict:
    """Run / resume / list a browser agent the USER recorded.

    The host's part is small on purpose: it names the agent. The steps were
    recorded by the user in the Browser app, the replay is
    server/browser_agents.py through the sidecar's sessions lane, and every
    credential is typed by the vault broker. No model decides anything
    inside a run — which is why the same op is schedulable
    (direct_ops.UNATTENDED_OP_TOOLS) and callable by an app.

    Ops:
      list            -> the agents on this host, with their last run
      get             -> one agent: its steps, schedule, outputs
      run             -> start a run now (returns when it finishes, or when
                         it pauses for the user — see `resume`)
      runs            -> recent runs of one agent
      resume          -> answer a paused run (a one-time code the site asked
                         for): name + answer
    """
    a = args or {}
    op = (a.get("op") or "list").strip()
    if op not in {"list", "get", "run", "runs", "resume"}:
        return _refused("op must be one of ['list', 'get', 'run', 'runs', 'resume']")
    try:
        from server import brain_service
        from server import browser_agents as agents
        pool = brain_service.get_pool()
        eid = int(brain_service._entity_id)
        if op == "list":
            return {"status": "ok", "result": {"agents": agents.list_agents()}}
        name = (a.get("name") or "").strip().lower()
        if not name:
            return _refused("name is required")
        if op == "get":
            spec = agents.load_agent(name)
            if spec is None:
                return _refused(f"no agent named {name!r}")
            return {"status": "ok", "result": spec}
        if op == "runs":
            return {"status": "ok", "result": {"runs": agents.list_runs(name)}}
        if op == "resume":
            answer = str(a.get("answer") or "")
            if not answer:
                return _refused("answer is required for op='resume'")
            ok, msg = agents.resume(name, answer)
            return {"status": "ok" if ok else "error", "result": msg}
        # op == "run"
        if pool is None:
            return {"status": "error", "result": "browser_agent failed: pool unavailable"}
        trigger = (a.get("trigger") or "host").strip()[:20]
        rec = await agents.run(pool, entity_id=eid, name=name,
                               actor=f"{trigger}:browser_agent", trigger=trigger)
        # `waiting` and `running` are not failures: the run is alive and the
        # record carries the question. Only a run that ENDED without success
        # is an error — the scheduled lane records that as a failed op.
        st = rec.get("status")
        return {"status": "ok" if st in ("ok", "waiting", "running") else "error",
                "result": rec}
    except Exception as e:
        if type(e).__name__ == "AgentError":
            return _refused_if_caller(e, str(e))
        return _refused_if_caller(e, f"browser_agent failed: {type(e).__name__}: {e}")


async def _exec_app_probe(args: dict) -> dict:
    """INTERROGATE one of the host's own running apps.

    Why this exists: the point of the platform is for the host to babysit
    the app, monitor it, and make changes until the desired result is
    achieved.

    Before this, the host's entire surface for a running app was four tools —
    app_status, start_app, stop_app, restart_app — plus workspace_file, which
    is jailed to the PROJECT SOURCE (/var/lib/cseries/projects/<id>), not the
    running app's data. So the host could see THAT its app was alive and
    restart it, and could not ask it anything.

    Two real questions on one Host the same day, neither answerable by the
    host, both answered from outside the platform with curl:

      "why are no trades coming through" -> the app's own API said
      {"current":0,"max":3} and its trades DB had 0 rows. The host had no way
      to call /api/v1/count.

      "why is the bot UI blank" -> the page was served with <base href=
      "/ui/ui/">, so a relative Vite chunk resolved one level too
      deep and came back as HTML with content-type text/html instead of
      JavaScript. A dynamic import of an HTML document throws and the app
      never mounts. Finding that needs the RESPONSE HEADERS of a specific
      URL. No host tool could fetch one.

    The app answered its ready check the whole time, which is exactly why
    liveness was never the missing piece.

    Jailed by construction:
      - the app must be in this host's own registry; another entity's app is
        not addressable from here at all.
      - `path` is a PATH. The origin comes from the supervisor's own
        base_url — the same resolution the ready check uses, so it reaches
        both placements (in-container sidecar and the app's own pod) and the
        host can never point this at an arbitrary address.
      - files are jailed to that app's appdata root, resolved and re-checked
        after symlink expansion, and read-only.

    Ops:
      request (default) -> GET a path on the app: status, content-type,
                           headers, body (truncated), elapsed_ms.
      logs              -> the app's log with a time window and a filter,
                           rather than app_status's fixed tail.
      files             -> list/read under the app's own appdata.
    """
    a = args or {}
    op = (a.get("op") or "request").strip()
    app_id = (a.get("app_id") or "").strip()
    if not app_id:
        return _refused("app_id required")
    if op not in {"request", "logs", "files"}:
        return _refused("op must be one of ['request', 'logs', 'files']")
    try:
        from pathlib import Path as _P
        from server import brain_service
        from server.applications import registry, storage
        from server.applications.supervisor import (
            LOG_FILENAME, LOG_SUBDIR, get_supervisor)

        pool = brain_service.get_pool()
        reg_row = await registry.get(pool, app_id) if pool else None
        if reg_row is None:
            return _refused(f"`{app_id}` is not installed on this host. This reads your OWN "
                "apps only — list them with explore_environment.")

        # ── files: the app's own runtime data, read-only ──────────────────
        if op == "files":
            root = _P(storage.workspace_dir(app_id)).resolve()
            rel = (a.get("path") or "").strip().lstrip("/")
            target = (root / rel).resolve() if rel else root
            # Re-checked AFTER resolution so a symlink inside the app's data
            # cannot walk out of it.
            if not (target == root or root in target.parents):
                return _refused(f"path escapes {app_id}'s data directory")
            if not target.exists():
                return _refused(f"no such path: {rel or '.'}")
            if target.is_dir():
                entries = []
                for p in sorted(target.iterdir())[:400]:
                    try:
                        entries.append({
                            "name": p.name,
                            "kind": "dir" if p.is_dir() else "file",
                            "bytes": (p.stat().st_size if p.is_file() else None),
                        })
                    except OSError:
                        continue
                return {"status": "ok", "result": {
                    "app_id": app_id, "path": rel or ".", "entries": entries}}
            try:
                # Bounded READ, not read-then-slice. read_bytes() pulls the
                # whole file into the kernel process first: measured, a 300MB
                # file in an app's data dir raised peak RSS by 300MiB, and the
                # pod's memory IS the user's plan budget — so one host tool
                # call could OOM the kernel. A trading app's sqlite lives
                # exactly here, and this tool advertises reading it.
                with open(target, "rb") as _fh:
                    raw = _fh.read(_PROBE_BODY_MAX + 1)
            except OSError as e:
                return _refused_if_caller(e, f"unreadable: {e}")
            body = raw[:_PROBE_BODY_MAX].decode("utf-8", errors="replace")
            return {"status": "ok", "result": {
                "app_id": app_id, "path": rel,
                "bytes": target.stat().st_size,
                "truncated": len(raw) > _PROBE_BODY_MAX,
                "content": body}}

        # ── logs: a window and a filter over what the app printed ─────────
        if op == "logs":
            log_path = _P(storage.workspace_dir(app_id)) / LOG_SUBDIR / LOG_FILENAME
            if not log_path.exists():
                return {"status": "ok", "result": {
                    "app_id": app_id, "lines": [],
                    "note": "no log file yet — the app has not printed anything"}}
            try:
                text = log_path.read_text(errors="replace")[-_PROBE_LOG_MAX:]
            except OSError as e:
                return _refused_if_caller(e, f"log unreadable: {e}")
            lines = text.splitlines()
            contains = (a.get("contains") or "").strip()
            if contains:
                lines = [ln for ln in lines if contains.lower() in ln.lower()]
            try:
                limit = max(1, min(int(a.get("limit") or 120), 500))
            except (TypeError, ValueError):
                limit = 120
            return {"status": "ok", "result": {
                "app_id": app_id,
                "matched": len(lines),
                "filter": contains or None,
                "lines": lines[-limit:]}}

        # ── request: ask the app itself ───────────────────────────────────
        sup = get_supervisor()
        running = sup.get(app_id) if sup else None
        if running is None or not running.is_alive():
            return _refused(f"`{app_id}` is not running, so there is nothing to ask. Start "
                "it (system.start_app) or open it, then probe.")
        path = (a.get("path") or "/").strip()
        if "://" in path or path.startswith("//"):
            return _refused("path is a PATH on your own app (e.g. '/api/v1/count'), not a "
                "URL — the address comes from the app itself.")
        url = running.base_url.rstrip("/") + "/" + path.lstrip("/")
        try:
            timeout_s = max(1, min(int(a.get("timeout_s") or 10), 30))
        except (TypeError, ValueError):
            timeout_s = 10
        import time as _time
        t0 = _time.monotonic()
        try:
            import aiohttp
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=timeout_s),
                                 allow_redirects=False) as r:
                    # LOOP to the cap. StreamReader.read(n) returns as soon as
                    # the buffer is non-empty, so a single call yields whatever
                    # arrived in the first chunk: measured, a 15,150-byte body
                    # came back as 1,009 bytes reported `truncated: false`. A
                    # partial document labelled complete is worse than no body,
                    # because the host reasons about what it was handed.
                    _want = _PROBE_BODY_MAX + 1
                    _buf = bytearray()
                    while len(_buf) < _want:
                        _chunk = await r.content.read(_want - len(_buf))
                        if not _chunk:
                            break
                        _buf.extend(_chunk)
                    raw = bytes(_buf)
                    elapsed = int((_time.monotonic() - t0) * 1000)
                    ctype = r.headers.get("content-type", "")
                    body = raw[:_PROBE_BODY_MAX].decode("utf-8", errors="replace")
                    return {"status": "ok", "result": {
                        "app_id": app_id, "url": url, "http_status": r.status,
                        # The header that answers "why is my page blank":
                        # an asset coming back as text/html is a dead import.
                        "content_type": ctype,
                        "location": r.headers.get("location"),
                        "bytes": len(raw),
                        "truncated": len(raw) > _PROBE_BODY_MAX,
                        "elapsed_ms": elapsed,
                        "body": body}}
        except Exception as e:  # noqa: BLE001 — an unreachable path IS the finding
            return {"status": "ok", "result": {
                "app_id": app_id, "url": url, "http_status": None,
                "error": str(e)[:300],
                "elapsed_ms": int((_time.monotonic() - t0) * 1000)}}
    except Exception as e:  # noqa: BLE001
        return _refused_if_caller(e, f"app_probe failed: {e}")


async def _exec_install_app(args):
    a = args or {}
    app_id = (a.get("app_id") or "").strip()
    if not app_id:
        return _refused("app_id is required")
    version = (a.get("version") or "").strip() or None
    try:
        from server import brain_service
        pool = brain_service.get_pool()
        if pool is None:
            return {"status": "error", "result": "pool unavailable"}
        from server.applications import installer
        from server.applications.platform_tools import (
            allowed_for_entity, current_entity_id,
        )
        from server.routes.applications import _fetch_bundle_from_store, _StoreError
        eid = current_entity_id()
        # Platform-tool gate (defense in depth — the store gate also enforces).
        if not allowed_for_entity(app_id, eid):
            return _refused(f"{app_id!r} is a platform tool; this host is not in its install allow-list")
        try:
            data = await _fetch_bundle_from_store(app_id, version)
        except _StoreError as e:
            return _refused_if_caller(e, f"store fetch failed: {e}")
        try:
            app = await installer.install_bundle(pool, data, source="manual")
        except installer.AppRuntimeOverPlan as e:
            # The host is the one telling the user, so it needs the sentence,
            # not a traceback: what the app needs, what is left, which tier
            # carries it. The install already rolled itself back.
            return _refused(str(e), over_plan={
                "app_id": e.app_id, "needs_mi": e.needs_mi,
                "headroom_mi": e.headroom_mi, "upgrade_to": e.upgrade_to})
        # Post-install allow-list re-check + rollback (mirrors the route).
        if not allowed_for_entity(app.id, eid):
            try:
                await installer.uninstall_app(pool, app.id, purge=True)
            except Exception:
                pass
            return _refused(f"{app.id!r} is a platform tool; this host is not in its install allow-list")
        return {"status": "ok", "result": {
            "app_id": app.id, "name": app.name, "version": app.version,
            "provenance": app.provenance, "status": app.status, "installed": True,
        }}
    except Exception as e:
        return _refused_if_caller(e, f"install_app failed: {getattr(e, 'detail', None) or e}")


async def _exec_uninstall_app(args):
    a = args or {}
    app_id = (a.get("app_id") or "").strip()
    if not app_id:
        return _refused("app_id is required")
    purge = bool(a.get("purge", False))
    try:
        from server import brain_service
        pool = brain_service.get_pool()
        if pool is None:
            return {"status": "error", "result": "pool unavailable"}
        from server.applications import installer
        from server.applications.supervisor import get_supervisor
        # Stop any running sidecar before tearing the bundle out from under it.
        sup = get_supervisor()
        if sup is not None:
            running = sup.get(app_id)
            if running is not None:
                await sup._terminate(running)
                async with sup._lock:
                    sup._by_app.pop(app_id, None)
                    sup._refs.pop(app_id, None)
        res = await installer.uninstall_app(pool, app_id, purge=purge)
        return {"status": "ok", "result": res}
    except Exception as e:
        return _refused_if_caller(e, f"uninstall_app failed: {getattr(e, 'detail', None) or e}")


async def _exec_refresh_app_store(args):
    try:
        from server import brain_service
        pool = brain_service.get_pool()
        if pool is None:
            return {"status": "error", "result": "pool unavailable"}
        from server.applications.public_store import store_client
        bc = store_client()
        if not bc._connected:
            return {"status": "error", "result": "no App Store is reachable"}
        from server.applications.default_install import _sync_store_upgrades
        res = await _sync_store_upgrades(bc, pool)
        return {"status": "ok", "result": res}
    except Exception as e:
        return _refused_if_caller(e, f"refresh_app_store failed: {getattr(e, 'detail', None) or e}")


async def _exec_list_app_catalog(args):
    try:
        from server import brain_service
        pool = brain_service.get_pool()
        if pool is None:
            return {"status": "error", "result": "pool unavailable"}
        from server.applications.public_store import store_client
        bc = store_client()
        if not bc._connected:
            return {"status": "error", "result": "no App Store is reachable"}
        envelope = await bc.request_full("appstore.list", {})
        if not envelope.get("ok"):
            return {"status": "error", "result": envelope.get("error") or "store error"}
        catalog = (envelope.get("data") or {}).get("apps", [])
        from server.applications import registry
        from server.applications.platform_tools import (
            allowed_for_entity, current_entity_id,
        )
        installed_by_id = {x.id: x for x in await registry.list_active(pool)}
        eid = current_entity_id()
        out = []
        for entry in catalog:
            aid = entry.get("app_id")
            if not aid or not allowed_for_entity(aid, eid):
                continue
            local = installed_by_id.get(aid)
            installed_ver = local.version if local else None
            latest = entry.get("latest_version")
            if local:
                if latest and installed_ver == latest:
                    status_str = "up_to_date"
                elif latest and installed_ver != latest:
                    status_str = "update_available"
                else:
                    status_str = "installed"
            else:
                status_str = "not_installed"
            out.append({
                "app_id": aid,
                "name": entry.get("name") or aid,
                "latest_version": latest,
                "installed_version": installed_ver,
                "install_status": status_str,
                "price_micros": entry.get("price_micros"),
                "provenance": entry.get("provenance") or entry.get("tier"),
            })
        return {"status": "ok", "result": {"apps": out}}
    except Exception as e:
        return _refused_if_caller(e, f"list_app_catalog failed: {getattr(e, 'detail', None) or e}")


async def _exec_purchase_app(args):
    a = args or {}
    app_id = (a.get("app_id") or "").strip()
    if not app_id:
        return _refused("app_id is required")
    try:
        from server.applications.platform_tools import (
            allowed_for_entity, current_entity_id,
        )
        eid = current_entity_id()
        if not allowed_for_entity(app_id, eid):
            return _refused(f"{app_id!r} is a platform tool; this host is not in its install allow-list")
        from server.applications.public_store import store_client
        bc = store_client()
        if not bc._connected:
            return {"status": "error", "result": "no App Store is reachable"}
        # Stripe Connect is the single purchase path for paid apps, so an agent can install FREE apps but cannot spend
        # money on the host's behalf. This op records free-path entitlements;
        # a paid app comes back as checkout_required and the human buys it in
        # the Store app.
        envelope = await bc.request_full("appstore.purchase", {"app_id": app_id})
        if not envelope.get("ok"):
            return {"status": "error", "result": envelope.get("error") or "purchase failed"}
        data = envelope.get("data") or {}
        if data.get("checkout_required"):
            # Not a failure the agent can retry its way out of — say plainly
            # that a person has to make this purchase, and how.
            price = data.get("price_micros")
            price_str = f"${int(price) / 1_000_000:.2f}" if price else "a fee"
            return _refused({
                "checkout_required": True,
                "message": (
                    f"{app_id!r} is a paid app ({price_str}). Paid apps are "
                    f"bought by you, not by me: open the Store app and use "
                    f"Buy, which goes through Stripe. The seller sets their "
                    f"own refund policy — it's shown on the listing before "
                    f"you pay."),
                **data})
        return {"status": "ok", "result": data}
    except Exception as e:
        return _refused_if_caller(e, f"purchase_app failed: {getattr(e, 'detail', None) or e}")


# manage_site / manage_site_domain dispatch over the caller-scoped webapp.* bridge
# ops the Sites app uses. The bridge fixes entity_id from the mTLS handshake, so a
# host can only touch its OWN sites — these are thin surfaced dispatchers.
#
# op='publish': the host publishes a site bundle (a zip) it
# already BUILT into its OWN governed workspace ($CEIGAS_WORKSPACE) — NOT an in-chat
# raw upload. The handler reads the zip through the sandboxed file_access capability
# (path-traversal / size guarded) and forwards {slug, zip_b64} to the SAME
# webapp.publish_bundle bridge op the Sites app's upload uses. The bridge fixes the
# publisher from the mTLS cert, so a host can only publish under its OWN identity.
_MANAGE_SITE_OPS = {
    "list":      ("webapp.sites", ()),
    "analytics": ("webapp.analytics", ()),
    "publish":   ("webapp.publish_bundle", ("slug",)),
    "unpublish": ("webapp.unpublish", ("slug",)),
    "suspend":   ("webapp.suspend", ("slug",)),
    "resume":    ("webapp.resume", ("slug",)),
    "restart":   ("webapp.restart", ("slug",)),
    "expose":    ("webapp.set_exposure", ("slug", "policy")),
}


# A published bundle up to this size rides in-band (zip_b64), exactly as every
# bundle did when the kernel read it whole (it refused past 10 MB), so a
# bridge that predates staged parts still takes it. Past it the bundle is sent
# in parts, read from disk one part at a time, so memory is bounded by a part,
# never by the bundle.
_PUBLISH_IN_BAND_MAX = 10 * 1024 * 1024
# One part, under the bridge's own per-part maximum (the bridge's
# _BUNDLE_PART_MAX, 16 MiB) — and small enough that a part,
# its base64 and the JSON line carrying it stay a few tens of MB.
_PUBLISH_PART_BYTES = 8 * 1024 * 1024


async def _publish_bundle_body(fh, size: int, send) -> dict:
    """What the publish op carries for the open bundle `fh`: {"zip_b64": ...}
    for a bundle that fits in-band, else {"upload_id", "parts", "sha256"}
    after sending each part with `send("bundle.upload_part", ...)`.
    {"error": ...} when the platform refuses a part."""
    import base64 as _b64
    import hashlib as _hl
    import secrets as _sec
    if size <= _PUBLISH_IN_BAND_MAX:
        return {"zip_b64": _b64.b64encode(fh.read()).decode("ascii")}
    upload_id = _sec.token_hex(16)
    digest = _hl.sha256()
    seq = 0
    while True:
        chunk = fh.read(_PUBLISH_PART_BYTES)
        if not chunk:
            break
        digest.update(chunk)
        env = await send("bundle.upload_part", {
            "upload_id": upload_id, "seq": seq,
            "data_b64": _b64.b64encode(chunk).decode("ascii")})
        del chunk
        if not (isinstance(env, dict) and env.get("ok")):
            err = (env or {}).get("error") if isinstance(env, dict) else env
            return {"error": (f"publish failed: part {seq + 1} of the {size}-byte bundle "
                              f"was not taken by the platform: {err}. A bundle over "
                              f"{_PUBLISH_IN_BAND_MAX // (1024 * 1024)} MB is sent in parts, "
                              "which the platform's bridge must accept.")}
        seq += 1
    return {"upload_id": upload_id, "parts": seq, "sha256": digest.hexdigest()}


async def _exec_manage_site(args):
    a = args or {}
    op = (a.get("op") or "").strip()
    if op not in _MANAGE_SITE_OPS:
        return _refused(f"op must be one of {sorted(_MANAGE_SITE_OPS)} (got {op!r})")
    bridge_op, required = _MANAGE_SITE_OPS[op]
    params = {}
    for key in required:
        if key == "policy":
            # policy is a dict, not a plain string — handled in the op=='expose'
            # branch below; do NOT run it through the strip/lower string coercion.
            continue
        val = (a.get(key) or "").strip().lower()
        if not val:
            return _refused(f"{key} is required for op={op!r}")
        params[key] = val
    if op == "expose":
        # Pass the policy dict through INTACT (not lowercased/stringified). Like
        # the HTTP route, this is a dumb forward — the bridge's webapp.set_exposure
        # does the creator-email seed + fail-closed validation from the mTLS
        # entity_id. Validate only that a non-empty 'tier' is present; the
        # controller validates tier membership.
        policy = a.get("policy")
        if not isinstance(policy, dict) or not (
            isinstance(policy.get("tier"), str) and policy.get("tier").strip()
        ):
            return _refused("policy (a dict with a non-empty 'tier') is required for op='expose'")
        params["policy"] = policy
    if op == "analytics" and a.get("hours") is not None:
        try:
            params["hours"] = int(a.get("hours"))
        except (TypeError, ValueError):
            return _refused("hours must be an integer")
    if op == "publish":
        # The bundle is a zip the host built into its governed workspace; read it
        # through the sandboxed file_access capability (NOT an in-chat upload), then
        # forward base64 to webapp.publish_bundle alongside the slug.
        bundle_path = (a.get("bundle_path") or "").strip()
        if not bundle_path:
            return _refused("bundle_path is required for op='publish' "
                            "(a .zip built into the host's workspace)")
        try:
            from server.capabilities import file_access
        except Exception as e:
            return _refused_if_caller(e, f"publish failed: file_access capability unavailable: {e}")
        # Streamed, never through read_file. The bundle is opened ONCE: the licence check reads that
        # open file (its central directory and manifest only), and the same
        # file is what goes out, so the check and the publish see the same
        # bytes. Memory stays bounded whatever the bundle's size:
        #   - up to _PUBLISH_IN_BAND_MAX it rides in-band as zip_b64, as every
        #     bundle did before (a bridge without parts still takes it);
        #   - past that it is sent in parts of _PUBLISH_PART_BYTES with
        #     bundle.upload_part, read from the file one part at a time, and
        #     the publish names the upload (upload_id + parts + sha256) instead
        #     of carrying the bytes (the bridge's op_bundle_upload_part).
        # How big a bundle may be is the platform's call — its zip-bomb guard
        # on what a bundle unpacks to — and its refusal comes back in its words.
        fh, _bundle_size, _open_err = file_access.open_file(bundle_path)
        if fh is None:
            # A path the model named that is not there, not a file or outside
            # the workspace is its outcome (file_access.Refusal); an OSError
            # opening a file that IS there stays the platform's.
            if getattr(_open_err, "input_refused", False):
                return _refused(f"publish failed reading bundle: {_open_err}")
            return {"status": "error",
                    "result": f"publish failed reading bundle: {_open_err}"}
        try:
            if _bundle_size == 0:
                # The bundle the model built and named is empty.
                return _refused("publish failed: empty bundle")
            # The author's licence gates THIS lane too. It publishes BYTES rather
            # than a tree, so neither publish_site_service gate ever saw it — and
            # the manifest survives inside the bundle, so a zip of someone else's
            # installed app would otherwise reach the public web through here.
            # get_pool() is Optional (brain_service.py:149); no pool means the
            # check cannot run, and an unrunnable rights check refuses.
            try:
                from server import brain_service as _brain
                from server.routes.applications import (
                    ShipError as _ShipError,
                    _license_gate_site_zip as _lic_zip,
                )
            except Exception as e:
                return _refused_if_caller(e, f"publish refused: licence check unavailable: {e}")
            _pool = _brain.get_pool()
            if _pool is None:
                return {"status": "error",
                        "result": "publish refused: cannot check the app licence "
                                  "right now (no database) — refused, not guessed"}
            try:
                await _lic_zip(_pool, fh)
            except _ShipError as e:
                return _refused_if_caller(e, e.detail)
            fh.seek(0)
            try:
                from server.main import get_bridge_client
                _bc = get_bridge_client()
            except Exception:
                _bc = None
            if _bc is None or not getattr(_bc, "_connected", False):
                return {"status": "error", "result": "bridge not connected"}
            _staged = await _publish_bundle_body(fh, _bundle_size, _bc.request_full)
        except OSError as e:
            return _refused_if_caller(e, f"publish failed reading bundle: {e}")
        finally:
            fh.close()
        if _staged.get("error"):
            return {"status": "error", "result": _staged["error"]}
        params.update(_staged)
    try:
        from server.main import get_bridge_client
        bc = get_bridge_client()
        if bc is None or not getattr(bc, "_connected", False):
            return {"status": "error", "result": "bridge not connected"}
        envelope = await bc.request_full(bridge_op, params)
        if not envelope.get("ok"):
            return {"status": "error", "result": envelope.get("error") or f"{bridge_op} failed"}
        return {"status": "ok", "result": envelope.get("data") or {}}
    except Exception as e:
        return _refused_if_caller(e, f"manage_site failed: {getattr(e, 'detail', None) or e}")


# Custom-domain ops on a host's own sites — caller-scoped webapp.* bridge ops.
_MANAGE_SITE_DOMAIN_OPS = {
    "list":          ("webapp.domain_list", ()),
    "add":           ("webapp.domain_add", ("slug", "domain")),
    "verify":        ("webapp.domain_verify", ("domain",)),
    "remove":        ("webapp.domain_remove", ("domain",)),
    "records":       ("webapp.dns_records_list", ("domain",)),
    "record_set":    ("webapp.dns_record_set", ("domain", "name", "type")),
    "record_delete": ("webapp.dns_record_delete", ("domain", "name", "type")),
}


async def _exec_manage_site_domain(args):
    a = args or {}
    op = (a.get("op") or "").strip()
    if op not in _MANAGE_SITE_DOMAIN_OPS:
        return _refused(f"op must be one of {sorted(_MANAGE_SITE_DOMAIN_OPS)} (got {op!r})")
    bridge_op, required = _MANAGE_SITE_DOMAIN_OPS[op]
    params = {}
    for key in required:
        raw = a.get(key)
        val = (str(raw) if raw is not None else "").strip()
        if key in ("slug", "domain"):
            val = val.lower()
        elif key == "type":
            val = val.upper()
        if not val:
            return _refused(f"{key} is required for op={op!r}")
        params[key] = val
    if op == "add":
        method = (a.get("method") or "connect").strip().lower()
        if method not in ("connect", "delegate"):
            return _refused("method must be 'connect' or 'delegate'")
        params["method"] = method
    if op == "record_set":
        values = a.get("values") or []
        if not isinstance(values, list) or not [v for v in values if str(v).strip()]:
            return _refused("at least one value is required for op='record_set'")
        params["values"] = values
        try:
            params["ttl"] = int(a.get("ttl") or 3600)
        except (TypeError, ValueError):
            return _refused("ttl must be an integer")
    try:
        from server.main import get_bridge_client
        bc = get_bridge_client()
        if bc is None or not getattr(bc, "_connected", False):
            return {"status": "error", "result": "bridge not connected"}
        envelope = await bc.request_full(bridge_op, params)
        if not envelope.get("ok"):
            return {"status": "error", "result": envelope.get("error") or f"{bridge_op} failed"}
        return {"status": "ok", "result": envelope.get("data") or {}}
    except Exception as e:
        return _refused_if_caller(e, f"manage_site_domain failed: {getattr(e, 'detail', None) or e}")


async def _exec_publish_site(args: dict) -> dict:
    """One-click "Publish as site" (Sites Funnel batch 3): zip a Workbench
    project's built web output SERVER-SIDE and publish it to the public webapp
    zone via the caller-scoped webapp.publish_bundle op — the same service the
    Workbench UI button calls (server.routes.applications.publish_site_service).
    The controller annotates the HTML for live-edit and signs; a NEW site goes
    live public, and its owner sets other visibility in Sites."""
    from server import brain_service
    from server.routes.applications import (
        PublisherError, ShipError, publish_site_repo_service, publish_site_service)
    a = args or {}
    slug = (a.get("slug") or "").strip().lower()
    if not slug:
        return _refused("slug is required (site label — becomes <slug>.<zone>)")
    repo = (a.get("repo") or "").strip()
    if repo and a.get("project_id") is not None:
        return _refused("pass EITHER project_id (Workbench project) OR repo (owner/name on this host's gitea), not both")
    try:
        if repo:
            res = await publish_site_repo_service(
                repo=repo, slug=slug, ref=(a.get("ref") or "").strip())
            return {"status": "ok", "result": res}
        if a.get("project_id") is None:
            return _refused("project_id or repo is required")
        try:
            pid = int(a.get("project_id"))
        except (TypeError, ValueError):
            return _refused("project_id must be an integer")
        pool = brain_service.get_pool()
        if pool is None:
            return {"status": "error", "result": "publish_site failed: pool unavailable"}
        res = await publish_site_service(pool, pid, slug=slug, module=(str(a.get("module") or "").strip().lower() or None))
        return {"status": "ok", "result": res}
    except (ShipError, PublisherError) as e:
        return _refused_if_caller(e, f"publish_site failed: {e.detail}")
    except Exception as e:
        return _refused_if_caller(e, f"publish_site failed: {getattr(e, 'detail', None) or e}")


async def _exec_publish_app(args: dict) -> dict:
    """Manage the host's OWN published App-Store apps. Op-dispatch over the publisher
    proxy SERVICE (server.routes.applications.publisher_op_service) — calls the caller-
    scoped bridge ops directly (no HTTP shim, no @router fn). The bridge fixes the
    publisher from its mTLS cert, so the host can only ever read its OWN books / set
    its OWN prices / yank its OWN versions. Ops:
      my_apps   -> appstore.publisher.my_apps   (no args)
      sales     -> appstore.publisher.sales     (optional app_id, limit)
      set_price -> appstore.publisher.set_price (app_id, price_micros;
                   platform_fee_bps is platform-owned and not settable)
      yank      -> appstore.yank_version        (app_id, version; optional reason)
      agreement -> appstore.agreement           (no args) — the publishing
                   agreement text + version; present it to the USER before
                   any publish and only proceed on their explicit yes
      requests  -> appstore.publish_requests    (no args) — this publisher's
                   review-queue requests (status/reason)
      publish   -> publish_project_service      (project_id; agreement_version
                   REQUIRED [the version the user accepted]; optional
                   price_micros [must be 0 — store is free at launch],
                   audience, summary, version, fork_policy open|closed) —
                   packs + signs a Workbench project (any state except a
                   build in flight) and SUBMITS it to the review queue:
                   every publish is human-reviewed before it appears, so
                   success is a pending request, not a live listing. THIS
                   is the host's publish lane; the Publisher app's Publish
                   tab is the hand-upload alternative for a zip the user
                   already has."""
    a = args or {}
    op = (a.get("op") or "").strip()
    _OPS = {"my_apps", "sales", "set_price", "yank", "publish",
            "agreement", "requests"}
    if op not in _OPS:
        return _refused(f"op must be one of {sorted(_OPS)} (got {op!r}).")

    # publish: pack+sign a Workbench project (any state except a build in flight)
    # and SUBMIT it to the App Store review queue (every publish is human-
    # reviewed before it appears — the result is a pending request, not a
    # published row). Shares the validate→pack→sign with ship_project_app;
    # submits via the caller-scoped bridge op. agreement_version is the
    # publishing-agreement version the USER explicitly accepted (op='agreement'
    # fetches the text) — the store refuses a publish without it.
    if op == "publish":
        from server import brain_service
        from server.routes.applications import publish_project_service, ShipError
        pool = brain_service.get_pool()
        if pool is None:
            return {"status": "error", "result": "publish_app failed: pool unavailable"}
        if a.get("project_id") is None:
            return _refused("project_id is required for op='publish'")
        try:
            pid = int(a.get("project_id"))
        except (TypeError, ValueError):
            return _refused("project_id must be an integer")
        price_micros = 0
        if a.get("price_micros") is not None:
            try:
                price_micros = int(a.get("price_micros"))
            except (TypeError, ValueError):
                return _refused("price_micros must be an integer")
        audience = (a.get("audience") or "public").strip()
        if audience not in ("public", "internal", "circle"):
            return _refused("audience must be 'public', 'internal' or 'circle'")
        # A circle app is gated on a GROUP ROSTER, so the group is not optional,
        # and it is refused HERE rather than at the platform: a publish uploads
        # a bundle, and discovering a missing field after the upload wastes the
        # whole round trip. That the group must be one this host OWNS is checked
        # in appstore_circle.record_publication, which is the only place that
        # can know, and which the publish service calls.
        group_ref = (a.get("group_ref") or "").strip()
        if audience == "circle" and not group_ref:
            return _refused("audience 'circle' needs group_ref — the group whose "
                              "roster may see and fetch this app. List your groups "
                              "with GET /api/groups.")
        if group_ref and audience != "circle":
            return _refused("group_ref only applies to audience 'circle'")
        agreement_version = None
        if a.get("agreement_version") is not None:
            try:
                agreement_version = int(a.get("agreement_version"))
            except (TypeError, ValueError):
                return _refused("agreement_version must be an integer")
        fork_policy = (a.get("fork_policy") or "open").strip()
        if fork_policy not in ("open", "closed"):
            return _refused("fork_policy must be 'open' or 'closed' "
                              "('invited' arrives with the circles build)")
        try:
            res = await publish_project_service(
                pool, pid, price_micros=price_micros, audience=audience,
                summary=a.get("summary"), version=a.get("version"),
                agreement_version=agreement_version, fork_policy=fork_policy,
                group_ref=group_ref or None, module=(str(a.get("module") or "").strip().lower() or None))
            return {"status": "ok", "result": res}
        except ShipError as e:
            return _refused_if_caller(e, f"publish failed: {e.detail}")
        except Exception as e:
            return _refused_if_caller(e, f"publish failed: {getattr(e, 'detail', None) or e}")

    try:
        from server.routes.applications import publisher_op_service, PublisherError
    except Exception as e:
        return _refused_if_caller(e, f"publish_app unavailable: {e}")

    if op == "my_apps":
        bridge_op, params = "appstore.publisher.my_apps", {}
    elif op == "agreement":
        bridge_op, params = "appstore.agreement", {}
    elif op == "requests":
        bridge_op, params = "appstore.publish_requests", {}
    elif op == "sales":
        bridge_op, params = "appstore.publisher.sales", {}
        app_id = (a.get("app_id") or "").strip()
        if app_id:
            params["app_id"] = app_id
        if a.get("limit") is not None:
            try:
                params["limit"] = int(a.get("limit"))
            except (TypeError, ValueError):
                return _refused("limit must be an integer")
    elif op == "set_price":
        app_id = (a.get("app_id") or "").strip()
        if not app_id:
            return _refused("app_id is required for op='set_price'")
        if a.get("price_micros") is None:
            return _refused("price_micros is required for op='set_price'")
        try:
            price_micros = int(a.get("price_micros"))
        except (TypeError, ValueError):
            return _refused("price_micros must be an integer")
        if a.get("platform_fee_bps") is not None:
            # Platform-owned (10% standard) — the bridge
            # rejects it; fail fast here with the same message.
            return _refused("platform_fee_bps is platform-owned")
        params = {"app_id": app_id, "price_micros": price_micros}
        bridge_op = "appstore.publisher.set_price"
    else:  # yank
        app_id = (a.get("app_id") or "").strip()
        version = (a.get("version") or "").strip()
        if not app_id or not version:
            return _refused("app_id and version are required for op='yank'")
        params = {"app_id": app_id, "version": version}
        reason = (a.get("reason") or "").strip()
        if reason:
            params["reason"] = reason
        bridge_op = "appstore.yank_version"

    try:
        res = await publisher_op_service(bridge_op, params)
        return {"status": "ok", "result": res}
    except PublisherError as e:
        return _refused_if_caller(e, f"publish_app failed: {e.detail}")
    except Exception as e:
        return _refused_if_caller(e, f"publish_app failed: {getattr(e, 'detail', None) or e}")


async def _exec_ship_project_app(args: dict) -> dict:
    """Ship a Workbench project: pack it into a signed .cseries bundle, sign it
    with this host's entity key, and install it onto the host. Calls the
    ship_project_service directly (no HTTP shim, no @router fn) — EVERY security
    invariant lives in the service: any project state ships except a build in
    flight (shippability is a property of the TREE; unfinished phases surface as
    open_phases), its app.yaml must declare provenance=custom, it passes the
    build-contract validator (static analysis + api conformance — NO runtime.kind
    restriction here: custom apps may declare process/external; the
    static-or-helper bar is the STORE's, in validate_publish), and the sign-time
    App-SDK integrity gate must hold (a poisoned validator cannot certify
    apps). Returns the installed app (id, name, version, provenance) + bundle size."""
    a = args or {}
    if a.get("project_id") is None:
        return _refused("project_id is required")
    try:
        project_id = int(a.get("project_id"))
    except (TypeError, ValueError):
        return _refused("project_id must be an integer")
    try:
        from server import brain_service
        from server.routes.applications import ship_project_service, ShipError
        pool = brain_service.get_pool()
        if pool is None:
            return {"status": "error", "result": "ship_project_app failed: pool unavailable"}
        # A change the code review blocked is the user's to accept; the host
        # installing it on a chat word would skip the one step that releases
        # it. The user's own Install button stays theirs.
        _hold = await _active_review_hold(pool, project_id)
        if _hold is not None:
            return _refused(f"not installed: {_review_hold_reason(_hold)}. The user can also "
                "press Install on the project themselves.")
        # …and so is a tree the project's CI (gitea-actions) has not passed.
        _ci_held, _ = await _ci_merge_gate(pool, project_id)
        if _ci_held:
            return _refused(f"not installed: {_ci_held}. The user can also press Install on the "
                "project themselves.")
        res = await ship_project_service(pool, project_id, module=(str(a.get("module") or "").strip().lower() or None))
        return {"status": "ok", "result": res}
    except Exception as e:
        # ShipError carries .detail; other exceptions fall back to str.
        return _refused_if_caller(e, f"ship_project_app failed: {getattr(e, 'detail', None) or e}")
