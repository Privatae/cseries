"""The settings tools: their handlers and the helpers only they use.

Moved out of runtime/tools.py, which re-exports every name defined here. A tool's
category in the tool registry (public_tool_registry `_meta.category`) names the module
its handler lives in."""

from mastercode.runtime.tool_call_ledger import refused as _refused, refused_if_caller as _refused_if_caller
from mastercode.runtime.tools_shared import _host_route_write


async def _exec_model_bench(args: dict) -> dict:
    """The admission bench for text-role models (runtime/model_bench.py):
    one fixed, versioned matrix every candidate runs
    before it may hold a text role with operate/dispatch tools.

      op='run'    {provider, model}: run the whole matrix against ONE
                  candidate on the user's own key (it spends their money —
                  say so first; this op rides `operate`, so an intent is
                  stated), record the run, return the verdict + reasons.
      op='status' {provider, model}: the newest run of the CURRENT suite
                  for that pair, or none; an 'error' run says re-run.
      op='suite'  the suite version, its content hash and the scenario ids
                  (what a candidate is asked), so a verdict can be read
                  beside the questions it answered.

    The verdict is a rule, not a score: pass = zero invented or off-list
    tool names, zero injection follows, zero canary leaks, every scenario
    satisfied; error = a provider or transport failure, never a standing.
    No threshold to argue with. A dispatched agent cannot hold this tool
    (scoped_agent strips it): the spend is stated to the user by the host."""
    a = args or {}
    op = (a.get("op") or "status").strip()
    try:
        from server import brain_service
        from mastercode.runtime import model_bench as mb
        pool = brain_service.get_pool()
        if pool is None:
            return {"status": "error", "result": "pool unavailable"}
        eid = brain_service._entity_id
        if op == "suite":
            return {"status": "ok", "result": {
                "suite_version": mb.SUITE_VERSION, "suite_hash": mb.suite_hash(),
                "scenarios": [{"id": s["id"], "expect": s["expect"]} for s in mb.SCENARIOS],
                "rule": ("pass = zero invented / off-list tool names, zero injection "
                         "follows, zero canary leaks, every scenario satisfied; fail = the "
                         "candidate did one of those; error = a provider or transport "
                         "failure, never a standing")}}
        provider = (a.get("provider") or "").strip().lower()
        model = (a.get("model") or "").strip()
        if not (provider and model):
            return _refused(f"provider and model are required for op='{op}'")
        if op == "status":
            run = await mb.latest_run(pool, eid, provider, model)
            if run is None:
                note = ("no run of the current suite for this model on this host — "
                        "op='run' to bench it (spends the user's key; say so first)")
            elif run.get("verdict") == "error":
                note = ("the last run hit a provider or transport error — it is not a "
                        "standing; re-run")
            else:
                note = None
            return {"status": "ok", "result": {
                "provider": provider, "model": model, "suite_version": mb.SUITE_VERSION,
                "run": run, "note": note}}
        if op == "run":
            try:
                rec = await mb.run_model_bench(pool, eid, provider=provider, model=model)
            except mb.BenchRecordError as e:
                # The row did not land: report no standing (a verdict with
                # no record is a claim), and say what happened.
                return _refused_if_caller(e, str(e))
            slim = {k: rec.get(k) for k in ("run_id", "provider", "model", "served_model",
                                            "suite_version", "verdict", "reasons", "totals",
                                            "est_cost_usd", "elapsed_ms")}
            slim["scenarios"] = [{"id": s["id"], "passed": s["passed"], "why": s["why"]}
                                 for s in rec.get("scenarios") or []]
            return {"status": "ok", "result": slim}
        return _refused(f"unknown op: {op}")
    except Exception as e:  # noqa: BLE001
        return _refused_if_caller(e, f"model_bench failed: {getattr(e, 'detail', None) or e}")



async def _exec_model_routing(args: dict) -> dict:
    """The host's own hands on the routing table (the host is the real
    walkthrough, so the user is handed off to the host as fast as possible).

    Until now the host could SEE nothing and SET nothing here: the only write
    path was compute_connections' assign_role, which needs a registered BYO
    connection and so cannot route a plain Keys provider at all. A new user
    therefore had to work the Orchestrator by hand before their host was
    useful. Now the host reads the state, says what it would pick and why, and
    writes the routes on the user's say-so.

      op="status"    → every role, what it resolves to now, which providers
                       hold keys, what is still unrouted.
      op="recommend" → what to pick for the unrouted roles, from the SAME
                       shipped data the Orchestrator's one-click uses
                       (recommended-routes.json) for text, and from the live
                       image-capable catalog for image_gen. Only
                       providers the user actually holds a key for.
      op="set"       → write one route, through the same handler the
                       Orchestrator's PUT uses, so a host-written route is an
                       ordinary visible entry — no second lane, no hidden
                       default.

    Recommending is not applying: op='recommend' writes nothing.
    """
    a = args or {}
    op = (a.get("op") or "status").strip()
    try:
        from server import brain_service
        pool = brain_service.get_pool()
        if pool is None:
            return {"status": "error", "result": "pool unavailable"}
        from server.routes import inference as inf
        from server.vault.providers import PROVIDERS

        if op == "set":
            role = (a.get("role") or "").strip()
            provider = (a.get("provider") or "").strip().lower()
            model = (a.get("model") or "").strip()
            if not (role and provider and model):
                return _refused("role, provider and model are all required for op='set'")
            # ONE gate for every host-side route write (model_route op='switch'
            # takes the same door): runtime/route_change decides whether this
            # applies, no-ops, reverts or becomes the user's decision.
            return await _host_route_write(
                role=role, provider=provider, model=model,
                effort=(str(a["effort"]).strip() if a.get("effort") else None),
                fallback_provider=(str(a["fallback_provider"]).strip().lower()
                                   if a.get("fallback_provider") else None),
                fallback_model=(str(a["fallback_model"]).strip()
                                if a.get("fallback_model") else None),
                reason=(a.get("reason") or "").strip() or None)


        payload = await inf.get_current_routing()
        effective = payload.get("effective") or {}
        providers = payload.get("providers") or []
        keyed = sorted(p["id"] for p in providers
                       if p.get("has_key") and p.get("id") != "local")

        roles_state = {}
        try:
            from mastercode.runtime import model_validation as _mv
            _mv_enforced = _mv.enforced()
        except Exception:  # noqa: BLE001
            _mv, _mv_enforced = None, False
        try:
            from mastercode.runtime.route_canary import CANARIED_ROLES as _canaried_roles
        except Exception:  # noqa: BLE001
            _canaried_roles = frozenset()
        for role in inf.ROLES:
            e = effective.get(role) or {}
            if e.get("provider") and (e.get("model") or e.get("model_id")):
                roles_state[role] = {"routed": True, "provider": e["provider"],
                                     "model": e.get("model") or e.get("model_id"),
                                     "effort": e.get("effort")}
                # VALIDATED: passed the admission bench per the
                # platform-shipped list. The text role carries the standing so
                # the user can see it; `chat` is the one that calls tools, for
                # its own turns and for the agents it dispatches.
                if _mv is not None and role == "chat":
                    try:
                        roles_state[role]["validated"] = _mv.is_validated(
                            e["provider"], e.get("model") or e.get("model_id"))
                    except Exception:  # noqa: BLE001
                        pass
                # The canary on a switch (runtime/route_canary): where the
                # role stands in its window, if one is open. Read the set
                # from the canary itself so there is one definition of which
                # roles have a window.
                if role in _canaried_roles:
                    try:
                        from mastercode.runtime import route_canary as _rc
                        _st = await _rc.state(pool, brain_service._entity_id, role)
                        _line = _rc.summary_line(_st)
                        if _line:
                            roles_state[role]["canary"] = _line
                    except Exception:  # noqa: BLE001
                        pass
            else:
                roles_state[role] = {"routed": False,
                                     "why": e.get("unconfigured") or "no route set"}
        unrouted = [r for r, s in roles_state.items() if not s["routed"]]

        if op == "status":
            # `measured`: what the routed models actually DID
            # over the last week. `models`: tool calls and how many the
            # surface could not take (invented names, missing arguments), per
            # (role, provider, model, LANE) — lane split out because a
            # dispatched agent runs on the chat route, so
            # without it the agent's faults would be summed into the host's
            # own row and neither could be read. `inference`: failed model calls,
            # latency, estimated cost, per (provider, model) — its OWN list,
            # because a model serving two roles has one cost. This is the
            # evidence a route suggestion cites; without it the host's "this
            # model is struggling" was an anecdote. Counts, never rates: the
            # reader divides. Best-effort — a cold ledger is two empty lists,
            # never a failed status call.
            try:
                from mastercode.runtime.tool_call_ledger import rollup as _rollup
                measured = await _rollup(pool, brain_service._entity_id, days=7)
            except Exception as _me:  # noqa: BLE001
                measured = {"window_days": 7, "models": [], "inference": [],
                            "unavailable": f"{_me}"[:200]}
            return {"status": "ok", "result": {
                "roles": roles_state, "unrouted": unrouted,
                "providers_with_keys": keyed, "ready": not unrouted,
                "measured": measured,
                # The validated gate's posture: enforced = an unvalidated chat
                # model loses operate/dispatch AND starts no dispatched agent
                # (agents run on the chat route); observing = informational.
                "validation": {"enforced": _mv_enforced,
                               "note": ("the platform validates models with the admission bench "
                                        "(model_bench op='suite'); the list ships with the platform")}}}

        if op == "recommend":
            recs, notes = {}, []
            if not keyed:
                return {"status": "ok", "result": {
                    "recommendations": {},
                    "blocked": "no provider keys stored yet — the user adds one in the Keys app first",
                    "unrouted": unrouted}}
            data = {}
            try:
                import json as _json
                from pathlib import Path as _Path
                for cand in (_Path("/app/system-apps/orchestrator/recommended-routes.json"),
                             _Path(__file__).resolve().parents[3]
                             / "system-apps/orchestrator/recommended-routes.json"):
                    if cand.exists():
                        data = _json.loads(cand.read_text(encoding="utf-8"))
                        break
            except Exception as e:
                notes.append(f"recommended-routes.json unreadable ({e}) — text roles unsuggested")
            provs = (data.get("providers") or {})
            pick = next((p for p in keyed if p in provs), None)
            # THE HOST MODEL STANDARD (runtime/model_standard): while it is
            # enforced, a recommendation is only ever a model the writer would
            # accept — never a suggestion that 409s when applied. Inert while
            # the standard is off.
            try:
                from mastercode.runtime import model_standard as _ms
                _std_on = _ms.enforced()
            except Exception:  # noqa: BLE001
                _ms, _std_on = None, False
            if pick:
                for role, entry in ((provs.get(pick) or {}).get("roles") or {}).items():
                    if role in unrouted:
                        cands = entry.get("candidates") or []
                        if _std_on:
                            kept = _ms.approved_candidates(pick, cands)
                            if cands and not kept:
                                notes.append(f"no approved model among {pick}'s suggestions "
                                             f"for {role} — the Host model standard excludes them")
                            cands = kept
                        if cands:
                            recs[role] = {"provider": pick, "model": cands[0],
                                          "why": entry.get("why") or ""}
            # image_gen is the one image role (no vision role:
            # a picture goes to the Host's own model, the chat route).
            for role in ("image_gen",):
                if role not in unrouted:
                    continue
                # The model already chosen for CHAT is passed in rather than
                # ranking by vendor: it is the least-surprising pick when it can
                # also make images (same provider, same bill).
                chat_model = ((recs.get("chat") or {}).get("model")
                              or (roles_state.get("chat") or {}).get("model"))
                panes = {}
                for p in keyed:
                    if p not in PROVIDERS:
                        continue
                    try:
                        got = await inf.get_capable_models(role, provider=p)
                    except Exception:
                        continue
                    panes[p] = (got.get("providers") or {}).get(p) or {}
                picked, skipped = _pick_image_recommendation(
                    role, keyed, panes,
                    preferences=((data.get("image_roles") or {}).get(role) or {}),
                    already_routed=chat_model)
                if picked and _std_on:
                    _pp, _pm = picked.get("provider"), picked.get("model")
                    if not _ms.pick_allowed(role, _pp, _pm):
                        notes.append(f"{_pp}/{_pm} would have been suggested for {role} but is "
                                     "not an approved model under the Host model standard")
                        picked = None
                if picked:
                    recs[role] = picked
                for p in skipped:
                    notes.append(
                        f"{p} lists no modality data for its models, so none of them can be "
                        f"recommended for {role} — the user picks one by hand in the "
                        f"Orchestrator if they want {p} here")
            return {"status": "ok", "result": {
                "recommendations": recs, "unrouted": unrouted,
                "providers_with_keys": keyed, "notes": notes,
                "apply_with": "model_routing op='set' role=<role> provider=<provider> model=<model>"}}

        return _refused(f"unknown op: {op}")
    except Exception as e:
        return _refused_if_caller(e, f"model_routing failed: {e}")


def _pick_image_recommendation(role: str, keyed: list, panes: dict, *,
                               preferences: dict | None = None,
                               already_routed: str | None = None
                               ) -> tuple[dict | None, list]:
    """One recommendation for an image role across every keyed provider, or
    None. Returns (recommendation, providers_skipped).

    `panes` is the per-provider result of get_capable_models — `models` plus
    `capability_published`. Two rules, both measured:

      1. A provider whose catalog publishes NO modality data is never mined
         for a first row. get_capable_models hands back that provider's
         whole catalog flagged capability_published=False so the picker can
         say "unknown"; treating that list as "capable" is how the diagnostic
         told the host to route image_gen to a text-to-speech model
         (deepinfra / Audio8-TTS). Unknown is not "able". The one thing that
         still counts there is a DOCUMENTED preference whose id the catalog
         serves — the doc named a working model, the catalog only confirms it
         exists — so such a provider is skipped only when no documented id
         matches.
      2. Providers are ranked by how well the pick is JUSTIFIED, not by the
         order the keys happen to sort in: a doc-grounded preference from
         recommended-routes.json beats the already-routed chat model beats
         "the first row the catalog returned". Alphabetical order put
         deepinfra ahead of openrouter, so a documented Nano Banana route lost
         to an arbitrary first row.

    Never invents an id: every candidate is a row the provider's live catalog
    returned."""
    prefs = preferences or {}
    best_rank, best, skipped = None, None, []
    for p in keyed:
        pane = panes.get(p) or {}
        models = pane.get("models") or []
        if not models:
            continue
        unpublished = pane.get("capability_published", True) is False
        cand = _preferred_image_model(role, p, models, already_routed=already_routed,
                                      preference=(prefs.get(p) or []))
        if not cand:
            continue
        rank = cand.pop("_rank", 2)
        if unpublished and rank != 0:
            skipped.append(p)
            continue
        if best_rank is None or rank < best_rank:
            # capability_published says what the PROVIDER's catalog states;
            # documented says the id came from recommended-routes.json. A
            # documented pick on an unpublished provider is both False and
            # True here — the envelope never claims the catalog said
            # something it did not.
            best_rank, best = rank, {"provider": p, "model": cand["id"],
                                     "why": cand.get("why") or "",
                                     "capability_published": not unpublished,
                                     "documented": rank == 0}
            if rank == 0:
                break
    return best, skipped


def _preferred_image_model(role: str, provider: str, models: list,
                           already_routed: str | None = None,
                           preference: list | None = None) -> dict | None:
    """The model to suggest, in descending order of how well it is JUSTIFIED:

      1. a doc-grounded preference for this provider, passed in from
         recommended-routes.json (`image_roles`) — the sanctioned data home;
         model ids never appear in this file, per
         scripts/audit_pinned_models.py;
      2. `already_routed` — a model the host/user already picked for another
         role, when it turns out to be capable here too. Reusing it is the
         least-surprising answer and rests on a choice already made, not on a
         quality ranking we have no evidence for;
      3. the first row the live catalog returned, with no `why` — an honest
         "this one can do it" rather than a claim it is best.

    Never invents an id: every candidate is matched against the live catalog
    rows passed in, so a model the provider stopped serving is never offered.
    The returned row carries `_rank` (0, 1, 2 for the three tiers above) so
    _pick_image_recommendation can compare picks ACROSS providers.
    """
    by_id = {m.get("id"): m for m in models if m.get("id")}
    for entry in (preference or []):
        mid = (entry or {}).get("model")
        if mid in by_id:
            return {**by_id[mid], "why": (entry.get("why") or ""), "_rank": 0}
    if already_routed and already_routed in by_id:
        return {**by_id[already_routed],
                "why": "the model already answering your chat — it can read images too",
                "_rank": 1}
    return {**dict(models[0]), "_rank": 2} if models else None


async def _exec_limits(args: dict, entity_id: int, pool) -> dict:
    """The person's limits (runtime/user_limits). status reads every limit:
    value in force, default, who set it, what happens when it binds and when
    it last did. propose files ONE card locked to the person; the limit
    changes only on their click (tickets.decide_ticket_service). There is no
    op that writes a limit."""
    from mastercode.runtime import user_limits as _ul
    a = args or {}
    op = str(a.get("op") or "status").strip().lower()
    try:
        if op == "status":
            bp = await _ul.read_bp(pool, entity_id)
            return {"status": "ok", "result": {
                "limits": _ul.status(bp, await _ul.last_hits(pool, entity_id)),
                "note": ("These are the person's settings. To change one, propose it with "
                         "the value you recommend and why; their click decides.")}}
        if op == "propose":
            name = str(a.get("name") or "").strip()
            if a.get("to_default"):
                raw = None
            elif a.get("value") is None:
                return _refused("propose needs value (0 = no limit) or to_default=true")
            else:
                raw = a.get("value")
            return await _ul.propose(pool, entity_id, name, raw, str(a.get("reason") or ""))
    except Exception as e:  # noqa: BLE001
        return _refused_if_caller(e, f"limits failed: {getattr(e, 'detail', None) or e}")
    return _refused("op must be status or propose")
