#!/usr/bin/env python3
"""Gate: the coding-matrix decision rules.

The matrix feed (inference._build_coding_matrix) composes four decisions that
must never drift, because the screen, the host tool and dispatch all rest on
them:

  1. BAND — first_party vs generic, derived from the fallback rule + registry
     kind flags (harness_keys.harness_band). Never a third hand-kept list.
  2. KEY STATE — one precedence rule, four words (harness_state.key_state):
     disconnected > not_set_up > expired > connected. "Not set up" and
     "disconnected" are different facts and never share a label.
  3. WIRING — the user's provider choice for a generic harness reorders the
     fallback within its allowed set and can NEVER widen it (a kimi wiring
     naming openrouter must stay refused — the manufactured-credential path).
  4. The wired choice is honored where credentials are BUILT (_fallback_bytes),
     not just displayed — the first production read of credential_wiring.

No fastapi, no live DB — the pure pieces plus a stub pool.
Run: APP_ROOT=<repo> python3 scripts/test_coding_matrix.py
"""
import asyncio
import json
import logging
import os
import sys

# smoke reads this gate with `2>&1 | tail -1`. Module logging writes to
# stderr unbuffered while these prints are block-buffered through a pipe,
# so a stray log line lands after the summary and tail -1 reads it instead
# of the verdict — a green gate reported as FAIL. Silence the modules and
# flush our own output.
logging.disable(logging.CRITICAL)

failures = []


def check(label, ok, detail=""):
    print(f"[coding-matrix] {'ok  ' if ok else 'FAIL'} {label}"
          + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


class StubPool:
    def __init__(self, params=None):
        self.params = dict(params or {})

    async def fetchrow(self, sql, *args):
        if "behavioral_params" in sql:
            return {"behavioral_params": json.dumps(self.params)}
        return None


def main() -> int:
    app_root = os.environ.get("APP_ROOT", "/app")
    sys.path.insert(0, app_root)
    # inference._harness_model_options reaches into the brain package for
    # HOST_ENGINE_MODELS (claude-code's servable set), so brain/ has to be
    # importable the way it is in the image — same two lines every other
    # harness gate uses (test_build_harness_resolution.py:61-62).
    sys.path.insert(0, os.path.join(app_root, "brain"))

    from server.network import harness_keys as hk
    from server.network import harness_state as hs
    from server.vault import providers as pv

    run = asyncio.run

    # ── 1. band derivation (harness_keys.harness_band, :106) ─────────
    check("band: opencode is the generic band",
          hk.harness_band("opencode") == "generic")
    check("band: kimi is first-party (bound to its own provider)",
          hk.harness_band("kimi") == "first_party")
    check("band: vendor-account harnesses are first-party",
          all(hk.harness_band(h) == "first_party"
              for h in ("claude-sdk", "codex", "cursor", "grok")))
    check("band: opencode's allowed set is openrouter alone (wrapper limitation)",
          hk._fallback_providers("opencode") == ("openrouter",)
          and set(hk._fallback_providers("opencode")) <= set(pv.generic_providers()))

    # ── 2. key-state precedence (harness_state.key_state) ────────────
    K = hs.key_state
    check("state: disconnected beats a present+valid file "
          "(a lingering file must not read as connected)",
          K(present=True, valid=True, disconnected=True) == "disconnected")
    check("state: present+valid = connected",
          K(present=True, valid=True, disconnected=False) == "connected")
    check("state: present but past expiry = expired, never connected",
          K(present=True, valid=False, disconnected=False) == "expired")
    check("state: absent = not_set_up — a DIFFERENT word from disconnected",
          K(present=False, valid=False, disconnected=False) == "not_set_up"
          and K(present=False, valid=True, disconnected=True) == "disconnected")

    # ── 3. wiring (harness_keys._wired_provider, :126) ───────────────
    check("wiring: unset reads empty",
          run(hk._wired_provider(StubPool(), 7, "opencode")) == "")
    check("wiring: opencode wired to openrouter reads openrouter",
          run(hk._wired_provider(StubPool(
              {"credential_wiring": {"harness:opencode": "openrouter"}}), 7,
              "opencode")) == "openrouter")
    check("wiring: opencode wired to groq is REFUSED "
          "(wiring reorders within the allowed set, it never widens)",
          run(hk._wired_provider(StubPool(
              {"credential_wiring": {"harness:opencode": "groq"}}), 7,
              "opencode")) == "")
    check("wiring: kimi wired to openrouter is REFUSED "
          "(wiring reorders, it never widens — the manufactured-credential path)",
          run(hk._wired_provider(StubPool(
              {"credential_wiring": {"harness:kimi": "openrouter"}}), 7,
              "kimi")) == "")
    check("wiring: a non-registry value is refused",
          run(hk._wired_provider(StubPool(
              {"credential_wiring": {"harness:opencode": "my-random-key"}}), 7,
              "opencode")) == "")
    check("wiring: no pool degrades to unset, never raises",
          run(hk._wired_provider(None, 7, "opencode")) == "")

    # ── 4. the wiring is honored where credentials are BUILT ─────────
    class StubRC:
        def __init__(self, secret):
            self.secret = secret

    async def resolve_or_and_groq(pool, eid, provider, accepted_types=("api_key",),
                                  wiring=None, *, actor=None, env_fallback=True):
        if provider == "openrouter":
            return StubRC("sk-or-1")
        if provider == "groq":
            return StubRC("gsk-1")
        return None

    orig_resolve = pv.resolve
    pv.resolve = resolve_or_and_groq
    try:
        # No wiring: opencode takes its one allowed generic, openrouter.
        raw, canon = run(hk._fallback_bytes(StubPool(), 7, "opencode"))
        check("build: unwired opencode takes openrouter (its one allowed generic)",
              canon == "openrouter" and raw == b"sk-or-1", repr((raw, canon)))
        # Wired to openrouter: the honored choice, same result.
        raw, canon = run(hk._fallback_bytes(StubPool(
            {"credential_wiring": {"harness:opencode": "openrouter"}}), 7, "opencode"))
        check("build: opencode wired to openrouter RUNS on openrouter "
              "(the choice is honored where the credential is built)",
              canon == "openrouter" and raw == b"sk-or-1", repr((raw, canon)))
        # Wired to groq: outside opencode's allowed set — the wiring cannot
        # widen, so the build still lands on openrouter, never groq.
        raw, canon = run(hk._fallback_bytes(StubPool(
            {"credential_wiring": {"harness:opencode": "groq"}}), 7, "opencode"))
        check("build: opencode wired to groq still runs on openrouter (never widens)",
              canon == "openrouter" and raw == b"sk-or-1", repr((raw, canon)))
        # REORDER is untestable on the real rule (no harness has two allowed
        # providers today), so pin it on a STAND-IN rule: a provider_json
        # harness allowed (openrouter, groq) must build on groq when wired
        # to groq and on openrouter (registry order) when unwired — the
        # user's choice wins where the credential is BUILT, and the
        # synthesized spec follows the resolved provider. kimi is the
        # stand-in only because its file shape can carry a groq spec; its
        # real allowed set stays ("kimi",) and is pinned in section 1.
        orig_fb = hk._fallback_providers
        hk._fallback_providers = (lambda p: ("openrouter", "groq")
                                  if p == "kimi" else orig_fb(p))
        try:
            raw, canon = run(hk._fallback_bytes(StubPool(), 7, "kimi"))
            check("build (stand-in rule): unwired takes the FIRST allowed provider (registry order)",
                  canon == "openrouter", repr((raw, canon)))
            raw, canon = run(hk._fallback_bytes(StubPool(
                {"credential_wiring": {"harness:kimi": "groq"}}), 7, "kimi"))
            try:
                spec = json.loads(raw.decode()) if raw else {}
            except Exception:
                spec = {}
            check("build (stand-in rule): wired to groq RUNS on groq — the choice "
                  "is honored where the credential is built, spec follows it",
                  canon == "groq"
                  and spec.get("base_url") == pv.PROVIDERS["groq"]["base_url"],
                  repr((raw, canon)))
        finally:
            hk._fallback_providers = orig_fb
        # Kimi wired to openrouter: still nothing — wiring cannot widen.
        raw, canon = run(hk._fallback_bytes(StubPool(
            {"credential_wiring": {"harness:kimi": "openrouter"}}), 7, "kimi"))
        check("build: kimi wired to openrouter still gets NOTHING",
              raw is None and canon == "", repr((raw, canon)))
    finally:
        pv.resolve = orig_resolve

    # ── 5. the wiring WRITE (harness_state.set_wiring) ───────────────
    # Storage only — the route + _wired_provider enforce which providers a
    # harness may use. Pinned: round-trip, clear, and sibling-key survival.
    from server import brain_service

    class ExecPool(StubPool):
        async def execute(self, sql, *args):
            assert "jsonb_set" in sql, sql
            if len(args) == 3:                       # _write_key(key, value)
                _eid, path, value = args
                self.params[path[0]] = json.loads(value)
            else:                                    # _write_subkey(key, sub, value)
                # harness_state._write_subkey: (eid, [key], key, [key, sub], value).
                # The statement nests two jsonb_sets because a two-element path
                # is a NO-OP when the parent object is missing; assert the
                # parent path matches so this stub cannot pass on SQL that
                # would silently write nothing on a fresh entity.
                _eid, parent_path, _parent, path, value = args
                assert parent_path == [path[0]], (parent_path, path)
                self.params.setdefault(path[0], {})[path[1]] = json.loads(value)
            return "UPDATE 1"

    pool = ExecPool({"credential_wiring": {"host-sdk": "anthropic-oauth"},
                     "coding_harnesses": [{"id": "kimi"}]})
    brain_service._pool = pool
    os.environ["ENTITY_ID"] = "7"
    check("set_wiring: stores the choice under harness:<id>",
          run(hs.set_wiring("opencode", "openrouter"))
          and pool.params["credential_wiring"]["harness:opencode"] == "openrouter")
    check("set_wiring: sibling wiring keys and coding_harnesses survive",
          pool.params["credential_wiring"]["host-sdk"] == "anthropic-oauth"
          and pool.params["coding_harnesses"] == [{"id": "kimi"}])
    check("set_wiring: None clears the choice",
          run(hs.set_wiring("opencode", None))
          and "harness:opencode" not in pool.params["credential_wiring"])
    check("set_wiring: empty harness refused",
          run(hs.set_wiring("", "groq")) is False)

    # ── 6. cursor's model catalog comes from its CLI, never OpenRouter ──
    # One fan-out run showed it: the matrix derived cursor's model
    # list by filtering the OpenRouter catalog by vendor, so the picker offered
    # `gpt-5.6-luna-pro` — a real OpenRouter slug Cursor has never served. The
    # CLI refused it in 7 s ("Cannot use this model: … Available models: …").
    # Cursor's list is per-ACCOUNT (its CLI calls the Cursor backend), so the
    # ONLY sound source is the CLI, cached per entity after a run.
    from server.routes import inference as inf
    check("cursor is NOT in the OpenRouter vendor-derivation map "
          "(absence is what stops the picker inventing cursor ids)",
          "cursor" not in inf.HARNESS_MODEL_VENDORS,
          str(sorted(inf.HARNESS_MODEL_VENDORS)))
    check("no OpenRouter-derived option list exists for cursor",
          not inf._harness_model_options().get("cursor"),
          str(inf._harness_model_options().get("cursor"))[:120])
    check("an unknown cursor model is NOT blocked by the sync validator "
          "(empty list fails OPEN — a save must never hang on a catalog)",
          inf.validate_harness_model("cursor", "anything-at-all") is None)
    # Claude Haiku 5.5 on the claude-code harness: the list is
    # both the picker and the save validator, so an id missing from it can
    # be neither picked nor saved.
    check("claude-code offers Claude Haiku 5.5",
          "claude-haiku-5-5" in (inf._harness_model_options().get("claude-code") or []),
          str(inf._harness_model_options().get("claude-code")))
    check("and a claude-code save of it passes the validator",
          inf.validate_harness_model("claude-code", "claude-haiku-5-5") is None,
          str(inf.validate_harness_model("claude-code", "claude-haiku-5-5")))

    cat_pool = ExecPool({})
    brain_service._pool = cat_pool
    cat_rows = [{"id": "claude-opus-5-thinking-medium", "label": "Claude Opus 5"},
                {"id": "composer-2.5-fast", "default": True}]
    check("record_model_catalog: stores the CLI's rows + a read_at stamp",
          run(hs.record_model_catalog("cursor", cat_rows,
                                      release_tag="2026.05.28-a70ca7c"))
          and cat_pool.params["harness_model_catalog"]["cursor"]["models"] == cat_rows
          and cat_pool.params["harness_model_catalog"]["cursor"]["read_at"],
          str(cat_pool.params.get("harness_model_catalog"))[:160])
    check("model_catalog reads one harness back",
          (run(hs.model_catalog("cursor")) or {}).get("models") == cat_rows)
    check("model_catalog: unknown harness is None, never a guess",
          run(hs.model_catalog("grok")) is None)
    check("record_model_catalog: rows with no id are dropped",
          run(hs.record_model_catalog("cursor", [{"label": "junk"}, "nope"]))
          and cat_pool.params["harness_model_catalog"]["cursor"]["models"] == [])
    # An empty catalog is AMBIGUOUS — measured, a bogus Cursor key
    # exits 0 printing the same "No models available for this account." an
    # empty plan prints — so the HARVESTER must decline to cache one, or the
    # picker reads "none" for a day after the key is fixed.
    _rv_src = open(os.path.join(app_root, "server/routes/review.py")).read()
    # The ONE read-and-cache core both lanes share (post-run harvest and the
    # wake-driven refresh): an empty list is reported as a failure with the
    # vendor's words, never cached.
    _core = _rv_src[_rv_src.index("async def _read_catalog_from_pod"):]
    _core = _core[:_core.index("\nasync def ", 10)]
    check("the catalog read core refuses to cache an EMPTY list "
          "(bad key and empty plan are indistinguishable at exit 0)",
          "if not models:" in _core
          and _core.index("if not models:") < _core.index("record_model_catalog("),
          _core[:80])
    _harv = _rv_src[_rv_src.index("async def _maybe_cache_model_catalog"):]
    _harv = _harv[:_harv.index("\n\n\n", 10)]
    check("the post-run harvest never wakes a pod of its own "
          "(refresh_model_catalog is the lane that does, and it releases)",
          "harness.wake" not in _harv and "_harness_bridge" not in _harv)
    check("record_model_catalog: a sibling harness's entry survives",
          run(hs.record_model_catalog("codex", [{"id": "gpt-5.6-sol"}]))
          and set(cat_pool.params["harness_model_catalog"]) == {"cursor", "codex"})
    check("record_model_catalog: empty harness id refused",
          run(hs.record_model_catalog("", [{"id": "x"}])) is False)

    # ── 7. an EMPTY cursor catalog is READ, not described (the matrix
    # must not put a sentence where the data goes) ─────────────────────────
    from server.routes import review as rv
    _src_inf = open(os.path.join(app_root, "server/routes/inference.py")).read()
    check("the matrix no longer serves a 'not read yet' sentence for cursor",
          "not read yet" not in _src_inf)
    check("review exposes the wake-and-read lane + the background kick",
          callable(getattr(rv, "refresh_model_catalog", None))
          and callable(getattr(rv, "kick_model_catalog_refresh", None))
          and callable(getattr(rv, "model_catalog_last_error", None)))
    _rsrc = __import__("inspect").getsource(rv.refresh_model_catalog)
    check("refresh_model_catalog wakes, waits routable, reads /status, and RELEASES in a shielded finally",
          "harness.wake" in _rsrc and "_await_routable" in _rsrc
          and "_read_catalog_from_pod" in _rsrc
          # The catalog read — a lane that never HOLDS the
          # pod — releases through _release_unless_held (skipped while a build
          # holds the pod; otherwise the same bare release as before), still
          # shielded, still in the finally.
          and "asyncio.shield(_release_unless_held(" in _rsrc)
    check("refresh is single-flight per harness (an asyncio.Lock per id)",
          "_catalog_refresh_locks" in _rsrc and "async with lock" in _rsrc)

    async def _kick_scenario():
        # Stub the real refresh (it would wake a pod) with a FAILING one, then
        # prove: kick starts it (True), the failure text is kept, and a second
        # kick inside the cooldown is refused (no wake storm paced by clicks).
        calls = {"n": 0}
        async def _fake_refresh(h, *, force=False):
            calls["n"] += 1
            return {"ok": False, "models": [], "fresh": False,
                    "error": "cursor returned no models for this account"}
        orig = rv.refresh_model_catalog
        rv.refresh_model_catalog = _fake_refresh
        rv._catalog_refresh_tasks.clear(); rv._catalog_last_attempt.clear(); rv._catalog_last_error.clear()
        try:
            first = rv.kick_model_catalog_refresh("cursor")
            inflight = rv.model_catalog_refresh_in_flight("cursor")
            t = rv._catalog_refresh_tasks.get("cursor")
            if t is not None:
                await t
            await asyncio.sleep(0)            # let the done-callback run
            err = rv.model_catalog_last_error("cursor")
            second = rv.kick_model_catalog_refresh("cursor")
            return first, inflight, err, second, calls["n"], rv.kick_model_catalog_refresh("grok")
        finally:
            rv.refresh_model_catalog = orig
            rv._catalog_refresh_tasks.clear(); rv._catalog_last_attempt.clear(); rv._catalog_last_error.clear()
    first, inflight, err, second, n, grok = run(_kick_scenario())
    check("kick: starts the background refresh and reports it in flight",
          first is True and inflight is True, f"{first} {inflight}")
    check("kick: a FAILED refresh leaves the vendor's words for the row",
          err == "cursor returned no models for this account", err)
    check("kick: a second kick inside the cooldown is REFUSED (no wake storm)",
          second is False and n == 1, f"{second} n={n}")
    check("kick: a harness with no CLI catalog is never woken for one",
          grok is False)

    # The cooldown's clock is monotonic(), which counts from boot. A harness
    # never tried must still kick on a machine up five minutes (a Host's VM
    # boots fresh; so does a CI runner), not wait out the cooldown from 0.
    async def _young_clock_kick():
        async def _ok_refresh(h, *, force=False):
            return {"ok": True, "models": ["m"], "fresh": True}
        orig_refresh, orig_time = rv.refresh_model_catalog, rv.time
        rv.refresh_model_catalog = _ok_refresh
        rv.time = type("_Clock", (), {"monotonic": staticmethod(lambda: 300.0)})
        rv._catalog_refresh_tasks.clear(); rv._catalog_last_attempt.clear(); rv._catalog_last_error.clear()
        try:
            started = rv.kick_model_catalog_refresh("cursor")
            t = rv._catalog_refresh_tasks.get("cursor")
            if t is not None:
                await t
            return started
        finally:
            rv.refresh_model_catalog, rv.time = orig_refresh, orig_time
            rv._catalog_refresh_tasks.clear(); rv._catalog_last_attempt.clear(); rv._catalog_last_error.clear()
    check("kick: the first kick starts on a machine booted five minutes ago",
          run(_young_clock_kick()) is True)
    check("the matrix row carries models_loading (the screen polls on it)",
          '"models_loading": models_loading' in _src_inf)

    # ── 8. the catalog read NEVER kills another lane's pod ─────────────
    # The operator's release is a bare replicas:0 patch (no refcount), and a
    # catalog read is the ONE lane that can land on a pod uninvited, mid-build.
    # It therefore owns the release only when nobody held the pod before its
    # wake (_cap_base is empty for that harness) AND nobody joined during the
    # read (_harness_bridge marks `superseded` on any other wake meanwhile).
    # A wake COUNT cannot express this — the solo build keepalives every 300 s,
    # fan-out wakes per variant, login polls wake per poll: repeated wakes are
    # one lane, not several.
    class _FakeBC:
        def __init__(self): self.ops = []
        async def request_full(self, op, body):
            self.ops.append(op)
            if op == "harness.wake":
                return {"ok": True, "data": {"url": "http://cursor.test:8841", "cap": "cap-1"}}
            return {"ok": True, "data": {"released": True}}
    async def _own_scenario(pre_held: bool, join_mid_read: bool, join_during_wake: bool = False,
                            wake_fails: bool = False):
        bc = _FakeBC()
        if join_during_wake or wake_fails:
            class _BC2(_FakeBC):
                async def request_full(self2, op, body):
                    self2.ops.append(op)
                    if op == "harness.wake":
                        if wake_fails and len([o for o in self2.ops if o == "harness.wake"]) == 1:
                            return {"ok": False, "error": "504 not Ready"}
                        if join_during_wake and asyncio.current_task() is rv._catalog_refresh_task.get("cursor"):
                            # ANOTHER lane's wake returns while OURS is still in flight
                            # it must register as a joiner.
                            await asyncio.create_task(rv._harness_bridge("harness.wake", "cursor"))
                        return {"ok": True, "data": {"url": "http://cursor.test:8841", "cap": "cap-1"}}
                    return {"ok": True, "data": {"released": True}}
            bc = _BC2()
        o_bridge, o_route, o_read, o_creds, o_fresh = (rv._bridge, rv._await_routable,
            rv._read_catalog_from_pod, rv._harness_pod_creds, rv._catalog_is_fresh)
        rv._bridge = lambda: bc
        async def _route(*a, **k): return None
        async def _read(http, base, h):
            if join_mid_read:          # a build's wake lands while we read — from ITS OWN task
                await asyncio.create_task(rv._harness_bridge("harness.wake", "cursor"))
            return {"ok": True, "models": [{"id": "composer-2.5-fast"}], "error": None}
        rv._await_routable = _route; rv._read_catalog_from_pod = _read
        rv._harness_pod_creds = lambda h: {"api_key": "k"}
        async def _stale(h): return False
        rv._catalog_is_fresh = _stale
        rv._control_cap.clear(); rv._cap_base.clear()
        rv._catalog_refresh_active.clear(); rv._catalog_refresh_superseded.clear()
        if pre_held:                   # some lane already woke and holds it
            rv._remember_control_cap("cursor", "http://cursor.test:8841", "cap-1")
        try:
            r = await rv.refresh_model_catalog("cursor", force=True)
            return r.get("ok"), bc.ops.count("harness.wake"), bc.ops.count("harness.release")
        finally:
            rv._bridge, rv._await_routable, rv._read_catalog_from_pod = o_bridge, o_route, o_read
            rv._harness_pod_creds, rv._catalog_is_fresh = o_creds, o_fresh
            rv._control_cap.clear(); rv._cap_base.clear()
            rv._catalog_refresh_active.clear(); rv._catalog_refresh_superseded.clear()
    ok, w, rel = run(_own_scenario(pre_held=False, join_mid_read=False))
    check("catalog read ALONE: wakes, reads, and releases the pod it woke",
          ok and w == 1 and rel == 1, f"ok={ok} wakes={w} releases={rel}")
    ok, w, rel = run(_own_scenario(pre_held=True, join_mid_read=False))
    check("catalog read on a pod ANOTHER LANE already holds: reads, releases NOTHING",
          ok and rel == 0, f"ok={ok} wakes={w} releases={rel}")
    ok, w, rel = run(_own_scenario(pre_held=False, join_mid_read=True))
    check("catalog read that a build JOINED mid-read: reads, releases NOTHING (the build releases)",
          ok and rel == 0, f"ok={ok} wakes={w} releases={rel}")
    ok, w, rel = run(_own_scenario(pre_held=False, join_mid_read=False, join_during_wake=True))
    check("catalog read whose wake another lane's wake OVERLAPS (returns during ours): "
          "releases NOTHING — markers are armed before the wake, own wake recognised by task",
          ok and rel == 0, f"ok={ok} wakes={w} releases={rel}")
    ok, w, rel = run(_own_scenario(pre_held=False, join_mid_read=False, wake_fails=True))
    check("catalog read whose own wake FAILS: no read, no release (nothing of ours is up)",
          (not ok) and rel == 0, f"ok={ok} wakes={w} releases={rel}")
    check("_harness_bridge has NO wake/release hold count (repeated wakes are keepalives)",
          not hasattr(rv, "_harness_holds"))
    _src_inf2 = open(os.path.join(app_root, "server/routes/inference.py")).read()
    check("only the screen route and the host tool may wake for a catalog "
          "(validators/preflight build the matrix with kick_catalogs=False)",
          _src_inf2.count("kick_catalogs=True") == 1
          and "async def _build_coding_matrix(*, kick_catalogs: bool = False)" in _src_inf2
          and "kick_catalogs=True" in open(os.path.join(app_root, "brain/mastercode/runtime/tools.py")).read())
    brain_service._pool = None

    print(f"[coding-matrix] {'PASS (0 failing)' if not failures else f'FAIL ({len(failures)} failing): {failures}'}")
    sys.stdout.flush()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
