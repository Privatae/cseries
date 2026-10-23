#!/usr/bin/env python3
"""Gate: shared credential resolver.

Pins resolve()'s ladder order — explicit wiring name → accepted-types
preference (exact canonical vault name → fuzzy free-typed name, self-healed
to canonical) → env (api_key only) — and BYTE-PARITY of the rewired openrouter
key path (provider_gateway._load_user_openrouter_key) against a faithful copy
of the pre-resolver ladder, including the legacy behavioral_params migration
and the env rung. Vault + pool are stubs — this gate runs in the smoke
container with no live Postgres dependency.

Run: APP_ROOT=<repo> python3 scripts/test_credential_resolver.py
"""
import asyncio
import json
import os
import sys

failures = []


def check(label, ok, detail=""):
    print(f"[cred-resolver] {'ok  ' if ok else 'FAIL'} {label}"
          + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


# ── stubs ───────────────────────────────────────────────────────────
class StubVault:
    """In-memory vault keyed by logical name; mirrors the server/vault/api
    call shapes the resolver uses (open/list/store)."""

    def __init__(self, entries=None):
        self.entries = dict(entries or {})   # name -> secret str
        self.stores = []                     # (name, cred_type) per store

    async def open_secret(self, pool, *, entity_id, name, actor=None):
        v = self.entries.get(name)
        return v.encode("utf-8") if v is not None else None

    async def list_secrets(self, pool, *, entity_id):
        return [{"logical_name": n} for n in self.entries]

    async def store_secret(self, pool, *, entity_id, name, cred_type,
                           plaintext, actor=None, extra_recipients=None):
        self.entries[name] = plaintext.decode("utf-8")
        self.stores.append((name, cred_type))
        return "env-id"


class StubPool:
    def __init__(self, behavioral_params=None):
        self.params = behavioral_params
        self.executes = []

    async def fetchrow(self, q, *a):
        return {"behavioral_params": self.params}

    async def execute(self, q, *a):
        self.executes.append(a)
        if a and isinstance(a[0], str):
            self.params = json.loads(a[0])


def patch_vault(vault_api, stub):
    vault_api.open_secret = stub.open_secret
    vault_api.list_secrets = stub.list_secrets
    vault_api.store_secret = stub.store_secret


# ── faithful copy of the PRE-resolver openrouter ladder (provider_gateway
# @ daed1704, steps 1-4) — the parity baseline the rewired path must match ──
def _old_is_openrouter_name(logical_name):
    normalized = "".join(c for c in logical_name.lower() if c.isalnum())
    return "openrouter" in normalized


async def old_openrouter_ladder(pool, vault, env_key):
    try:
        eid_s = os.environ.get("ENTITY_ID", "")
        if pool is not None and eid_s.isdigit():
            eid = int(eid_s)
            raw = await vault.open_secret(
                pool, entity_id=eid, name="openrouter", actor="inference")
            if raw:
                return raw.decode("utf-8", "strict").strip()
            for cred in await vault.list_secrets(pool, entity_id=eid):
                name = cred.get("logical_name") or ""
                if name != "openrouter" and _old_is_openrouter_name(name):
                    raw = await vault.open_secret(
                        pool, entity_id=eid, name=name, actor="inference")
                    if raw:
                        key = raw.decode("utf-8", "strict").strip()
                        await vault.store_secret(
                            pool, entity_id=eid, name="openrouter",
                            cred_type="api_key",
                            plaintext=key.encode("utf-8"), actor="inference")
                        return key
            row = await pool.fetchrow(
                "SELECT behavioral_params FROM entity_registry WHERE id = $1",
                eid)
            params = row["behavioral_params"] if row else None
            if isinstance(params, str):
                params = json.loads(params)
            legacy = (params or {}).get("user_openrouter_key", "").strip()
            if legacy:
                await vault.store_secret(
                    pool, entity_id=eid, name="openrouter",
                    cred_type="api_key",
                    plaintext=legacy.encode("utf-8"), actor="inference")
                params.pop("user_openrouter_key", None)
                await pool.execute(
                    "UPDATE entity_registry SET behavioral_params = $1::jsonb "
                    "WHERE id = $2", json.dumps(params), eid)
                return legacy
    except Exception:
        pass
    return env_key


def main() -> int:
    app_root = os.environ.get("APP_ROOT", "/app")
    sys.path.insert(0, app_root)
    sys.path.insert(0, os.path.join(app_root, "brain"))
    os.environ["ENTITY_ID"] = "7"
    os.environ.pop("XAI_API_KEY", None)
    os.environ.pop("ANTHROPIC_API_KEY", None)
    os.environ.pop("MOONSHOT_API_KEY", None)

    from server.vault import api as vault_api
    from server.vault import providers as pv

    run = asyncio.run

    # ── resolve(): canonical exact hit ──────────────────────────────
    sv = StubVault({"xai": " sk-xai-1 "})
    patch_vault(vault_api, sv)
    rc = run(pv.resolve(StubPool(), 7, "xai", ("api_key",)))
    check("ladder: canonical exact hit → full metadata",
          rc is not None and (rc.provider, rc.name, rc.cred_type, rc.secret,
                              rc.source)
          == ("xai", "xai", "api_key", "sk-xai-1", "vault"))

    # ── resolve(): type-qualified hit + preference order ────────────
    sv = StubVault({"anthropic-oauth": "tok-oauth"})
    patch_vault(vault_api, sv)
    rc = run(pv.resolve(StubPool(), 7, "anthropic",
                        ("oauth_token", "api_key")))
    check("ladder: type-qualified canonical name (<provider>-oauth)",
          rc is not None and (rc.name, rc.cred_type, rc.secret)
          == ("anthropic-oauth", "oauth_token", "tok-oauth"))
    sv = StubVault({"anthropic": "sk-ant", "anthropic-oauth": "tok-oauth"})
    patch_vault(vault_api, sv)
    a = run(pv.resolve(StubPool(), 7, "anthropic",
                       ("oauth_token", "api_key")))
    b = run(pv.resolve(StubPool(), 7, "anthropic",
                       ("api_key", "oauth_token")))
    check("ladder: accepted_types ORDER decides when both entries exist",
          a is not None and b is not None
          and a.name == "anthropic-oauth" and b.name == "anthropic")

    # ── resolve(): the DEFAULT preference is api_key-first ──────────
    # (orchestrator v2 cut: keys work by default; the OAuth
    # token slot is for login surfaces the user builds themselves)
    import inspect
    check("ladder: resolve() default accepted_types is api_key-first",
          inspect.signature(pv.resolve).parameters["accepted_types"].default
          == ("api_key", "oauth_token"))
    rc = run(pv.resolve(StubPool(), 7, "anthropic"))
    check("ladder: the default preference serves the api_key entry",
          rc is not None and (rc.name, rc.cred_type) == ("anthropic", "api_key"))

    # ── kimi (Moonshot) is a first-class provider ───────────────────
    sv = StubVault({"kimi": " sk-kimi-1 "})
    patch_vault(vault_api, sv)
    rc = run(pv.resolve(StubPool(), 7, "kimi", ("api_key",)))
    check("kimi: canonical vault hit resolves",
          rc is not None and (rc.provider, rc.name, rc.secret, rc.source)
          == ("kimi", "kimi", "sk-kimi-1", "vault"))
    sv = StubVault({})
    patch_vault(vault_api, sv)
    os.environ["MOONSHOT_API_KEY"] = "env-kimi-9"
    rc = run(pv.resolve(None, 0, "kimi", ("api_key",)))
    check("kimi: env rung (MOONSHOT_API_KEY) with no pool",
          rc is not None and (rc.secret, rc.source) == ("env-kimi-9", "env"))
    os.environ.pop("MOONSHOT_API_KEY", None)

    # ── resolve(): fuzzy hit + self-heal ────────────────────────────
    sv = StubVault({"Open Router key": "sk-or-2"})
    patch_vault(vault_api, sv)
    rc = run(pv.resolve(StubPool(), 7, "openrouter", ("api_key",)))
    check("ladder: fuzzy free-typed name resolves",
          rc is not None and (rc.name, rc.secret, rc.source)
          == ("openrouter", "sk-or-2", "vault-aliased"))
    check("ladder: fuzzy hit self-heals to the canonical name",
          sv.stores == [("openrouter", "api_key")]
          and sv.entries.get("openrouter") == "sk-or-2")
    rc2 = run(pv.resolve(StubPool(), 7, "openrouter", ("api_key",)))
    check("ladder: after self-heal the exact rung hits",
          rc2 is not None and rc2.source == "vault")

    # ── resolve(): wiring-name override ─────────────────────────────
    sv = StubVault({"anthropic": "sk-ant", "my-special-key": "sk-wired"})
    patch_vault(vault_api, sv)
    rc = run(pv.resolve(StubPool(), 7, "anthropic",
                        ("api_key",), wiring="my-special-key"))
    check("wiring: explicit Keys name beats the canonical ladder",
          rc is not None and (rc.name, rc.secret, rc.cred_type, rc.source)
          == ("my-special-key", "sk-wired", "api_key", "wired"))
    sv = StubVault({"team-anthropic-oauth": "tok-w"})
    patch_vault(vault_api, sv)
    rc = run(pv.resolve(StubPool(), 7, "anthropic",
                        ("api_key",), wiring="team-anthropic-oauth"))
    check("wiring: 'oauth' in the wired name types it oauth_token",
          rc is not None and rc.cred_type == "oauth_token")
    sv = StubVault({"anthropic": "sk-ant"})
    patch_vault(vault_api, sv)
    rc = run(pv.resolve(StubPool(), 7, "anthropic",
                        ("api_key",), wiring="gone-entry"))
    check("wiring: absent wired entry degrades to the type ladder",
          rc is not None and (rc.name, rc.source) == ("anthropic", "vault"))

    # ── resolve(): miss → None; env rung ────────────────────────────
    sv = StubVault({})
    patch_vault(vault_api, sv)
    rc = run(pv.resolve(StubPool(), 7, "xai", ("api_key", "oauth_token")))
    check("ladder: total miss → None (no env set)", rc is None)
    os.environ["XAI_API_KEY"] = "env-xai-9"
    rc = run(pv.resolve(None, 0, "xai", ("api_key",)))
    check("ladder: env rung for api_key with no pool",
          rc is not None and (rc.secret, rc.source) == ("env-xai-9", "env"))
    rc = run(pv.resolve(None, 0, "xai", ("oauth_token",)))
    check("ladder: oauth NEVER falls back to env", rc is None)
    rc = run(pv.resolve(None, 0, "xai", ("api_key",), env_fallback=False))
    check("ladder: env_fallback=False suppresses the env rung", rc is None)
    os.environ.pop("XAI_API_KEY", None)

    # ── resolve_wired keeps its dict contract over resolve() ────────
    sv = StubVault({"anthropic-oauth": "tok-h"})
    patch_vault(vault_api, sv)
    pool = StubPool({"credential_wiring": {"host-sdk": "anthropic-oauth"}})
    got = run(pv.resolve_wired(pool, 7, "host-sdk", "anthropic"))
    check("resolve_wired: consumer wiring rides resolve(), dict shape kept",
          got == {"name": "anthropic-oauth", "cred_type": "oauth_token",
                  "secret": "tok-h"})

    # ── openrouter parity: rewired path vs the pre-resolver ladder ──
    # brain_service is stubbed so _load_user_openrouter_key sees our pool.
    import types
    bs = types.ModuleType("server.brain_service")
    sys.modules["server.brain_service"] = bs
    import server
    server.brain_service = bs
    from mastercode import provider_gateway as pg

    ENV_KEY = "env-or-key"

    def parity(label, entries, params=None, entity_id="7"):
        os.environ["ENTITY_ID"] = entity_id
        old_v, old_p = StubVault(dict(entries)), StubPool(
            json.loads(json.dumps(params)) if params is not None else None)
        old_key = run(old_openrouter_ladder(old_p, old_v, ENV_KEY))
        new_v, new_p = StubVault(dict(entries)), StubPool(
            json.loads(json.dumps(params)) if params is not None else None)
        patch_vault(vault_api, new_v)
        bs.get_pool = lambda: new_p
        pg.OPENROUTER_API_KEY = ENV_KEY
        new_key = run(pg._load_user_openrouter_key())
        same = (old_key == new_key and old_v.entries == new_v.entries
                and old_v.stores == new_v.stores
                and old_p.params == new_p.params
                and len(old_p.executes) == len(new_p.executes))
        check(f"openrouter parity: {label}", same,
              f"old={old_key!r}/{old_v.entries}/{old_p.params} "
              f"new={new_key!r}/{new_v.entries}/{new_p.params}")
        os.environ["ENTITY_ID"] = "7"
        return new_key

    parity("exact canonical hit", {"openrouter": " sk-or-exact "})
    parity("fuzzy free-typed hit + self-heal",
           {"anthropic": "sk-ant", "Open Router key": "sk-or-fuzzy"})
    k = parity("legacy behavioral_params migration (store + strip)",
               {}, params={"user_openrouter_key": " sk-legacy ",
                           "other": "kept"})
    check("openrouter parity: legacy key value survives the migration",
          k == "sk-legacy")
    parity("nothing anywhere → env constant", {})
    parity("whitespace-only vault entry (degenerate, returns '')",
           {"openrouter": "   "})
    parity("no ENTITY_ID → env constant", {"openrouter": "sk-unused"},
           entity_id="")

    # Deliberate typed-model delta (NOT a parity case): an oauth-marked name
    # is never swallowed by the api_key path — the old intent-only match
    # would have taken it.
    sv = StubVault({"openrouter oauth": "tok-or"})
    patch_vault(vault_api, sv)
    bs.get_pool = lambda: StubPool()
    pg.OPENROUTER_API_KEY = ENV_KEY
    new_key = run(pg._load_user_openrouter_key())
    old_key = run(old_openrouter_ladder(
        StubPool(), StubVault({"openrouter oauth": "tok-or"}), ENV_KEY))
    check("typed delta: api_key path skips an oauth-marked openrouter name",
          new_key == ENV_KEY and old_key == "tok-or")

    # ── follow-ups ──────────────────────────────────────────────────
    # Stored cred_type beats the name marker: an oauth_token deposited under a
    # marker-less name must NOT be served to (or re-stored for) an api_key
    # consumer. Legacy rows with no cred_type keep the name-marker behavior,
    # which the fuzzy/self-heal cases above already pin.
    class TypedVault(StubVault):
        def __init__(self, entries, types):
            super().__init__(entries)
            self.types = types

        async def list_secrets(self, pool, *, entity_id):
            return [{"logical_name": n, "cred_type": self.types.get(n)}
                    for n in self.entries]

    tv = TypedVault({"my openrouter token": "tok-oauth"},
                    {"my openrouter token": "oauth_token"})
    patch_vault(vault_api, tv)
    rc = run(pv.resolve(StubPool(), 9003, "openrouter", ("api_key",),
                        actor="t", env_fallback=False))
    check("typed column: oauth_token row is not served to an api_key consumer",
          rc is None and not tv.stores)

    tv2 = TypedVault({"my openrouter key": "sk-or-typed"},
                     {"my openrouter key": "api_key"})
    patch_vault(vault_api, tv2)
    rc = run(pv.resolve(StubPool(), 9003, "openrouter", ("api_key",),
                        actor="t", env_fallback=False))
    check("typed column: matching api_key row still self-heals to canonical",
          rc is not None and rc.secret == "sk-or-typed"
          and ("openrouter", "api_key") in tv2.stores)

    # A degenerate first type must not shadow a real second type — but with a
    # single accepted type the historical '' still comes back (parity above).
    dv = StubVault({"openrouter-oauth": "   ", "openrouter": "sk-or-real"})
    patch_vault(vault_api, dv)
    rc = run(pv.resolve(StubPool(), 9003, "openrouter", ("oauth_token", "api_key"),
                        actor="t", env_fallback=False))
    check("multi-type ladder: whitespace first type falls through to a real one",
          rc is not None and rc.secret == "sk-or-real")

    dv2 = StubVault({"openrouter": "   "})
    patch_vault(vault_api, dv2)
    rc = run(pv.resolve(StubPool(), 9003, "openrouter", ("api_key",),
                        actor="t", env_fallback=False))
    check("single-type degenerate still returns '' (parity preserved)",
          rc is not None and rc.secret == "")

    # ── deactivated lifecycle ─────────────────────────────────────────
    # vault_envelope.deactivated_at: the temporary off-switch. The stub
    # mirrors the real api: list flags the row, open_secret refuses it.
    class DeactivatingVault(StubVault):
        def __init__(self, entries, deactivated=()):
            super().__init__(entries)
            self.deactivated = set(deactivated)

        async def open_secret(self, pool, *, entity_id, name, actor=None):
            if name in self.deactivated:
                return None
            return await super().open_secret(
                pool, entity_id=entity_id, name=name, actor=actor)

        async def list_secrets(self, pool, *, entity_id):
            return [{"logical_name": n,
                     "deactivated_at": ("2026-08-06T00:00:00+00:00"
                                        if n in self.deactivated else None)}
                    for n in self.entries]

    # A deactivated CANONICAL row is a hard stop: empty-secret marker, and the
    # env rung must NOT resurrect a credential the user switched off.
    xv = DeactivatingVault({"xai": "sk-xai-off"}, {"xai"})
    patch_vault(vault_api, xv)
    os.environ["XAI_API_KEY"] = "env-xai-should-not-serve"
    rc = run(pv.resolve(StubPool(), 7, "xai", ("api_key",)))
    check("deactivated: canonical row is a hard stop (marker, env blocked)",
          rc is not None and (rc.secret, rc.source) == ("", "deactivated"))
    os.environ.pop("XAI_API_KEY", None)

    # A deactivated FUZZY-named row is skipped like any non-match; with no
    # canonical row the env rung still works (a different credential source).
    fv = DeactivatingVault({"Open Router key": "sk-or-off"}, {"Open Router key"})
    patch_vault(vault_api, fv)
    os.environ["OPENROUTER_API_KEY"] = "env-or-ok"
    rc = run(pv.resolve(StubPool(), 7, "openrouter", ("api_key",)))
    check("deactivated: fuzzy row skipped, env rung untouched",
          rc is not None and (rc.secret, rc.source) == ("env-or-ok", "env"))
    os.environ.pop("OPENROUTER_API_KEY", None)

    # Deactivating one TYPE never shadows the provider's other type.
    tv3 = DeactivatingVault({"anthropic": "sk-ant-off",
                             "anthropic-oauth": "tok-ant-on"},
                            {"anthropic"})
    patch_vault(vault_api, tv3)
    rc = run(pv.resolve(StubPool(), 7, "anthropic", ("api_key", "oauth_token")))
    check("deactivated: api_key off still serves the active oauth entry",
          rc is not None and (rc.secret, rc.source) == ("tok-ant-on", "vault"))

    # Single-type convenience wrapper reads deactivated as absent (the marker's
    # empty secret — the same '' the degenerate-entry parity case returns).
    patch_vault(vault_api, DeactivatingVault({"xai": "sk-xai-off"}, {"xai"}))
    rc = run(pv.resolve_credential(StubPool(), 7, "xai", "api_key"))
    check("deactivated: resolve_credential → no secret",
          not rc)

    # The Keys coverage read must agree with the resolver about a deactivated
    # canonical row. Regression found after the fact: the
    # route computed `stored` over the ACTIVE rows only, so a deactivated
    # canonical entry plus a still-active free-typed alias for the same
    # provider read 'Stored' in Keys while _resolve_typed hard-stopped on the
    # canonical row and served nothing — the app-says-connected/routing-says-
    # no-key split GET /providers exists to prevent. Both halves below are the
    # REAL code over identical rows, so they cannot drift apart again silently.
    LIFECYCLE_ROWS = [
        {"logical_name": "openai", "cred_type": "api_key",
         "created_at": None, "deactivated_at": "2026-08-06T00:00:00+00:00"},
        {"logical_name": "my openai key", "cred_type": "api_key",
         "created_at": None, "deactivated_at": None},
    ]

    class LifecycleVault(StubVault):
        """Canonical row deactivated (opens only for the reveal path), a
        free-typed alias for the same provider still active."""
        async def open_secret(self, pool, *, entity_id, name, actor=None,
                              include_deactivated=False):
            if name == "openai" and not include_deactivated:
                return None
            return await StubVault.open_secret(
                self, pool, entity_id=entity_id, name=name, actor=actor)

        async def list_secrets(self, pool, *, entity_id):
            return [dict(r) for r in LIFECYCLE_ROWS]

    patch_vault(vault_api, LifecycleVault({"my openai key": "sk-alias-live"}))
    bs.get_pool = lambda: StubPool()      # the route's _pool(); _entity_id() reads ENTITY_ID=7
    try:
        from server.routes import vault as vroute
        slot = next(s for prov in run(vroute.list_providers())["providers"]
                    if prov["provider"] == "openai"
                    for s in prov["slots"] if s["cred_type"] == "api_key")
    except Exception as exc:
        slot = {"stored": f"<route unavailable: {exc!r}>", "deactivated": None}
    rc = run(pv.resolve(StubPool(), 7, "openai", ("api_key",),
                        env_fallback=False))
    resolver_serves = bool(rc and rc.secret)
    check("deactivated: Keys slot agrees with the resolver (no phantom Stored)",
          slot["stored"] is False and slot["deactivated"] is True
          and not resolver_serves,
          f"slot={slot} resolver_serves={resolver_serves}")

    # ── The OAuth slot is the HOST's, not the user's ──────
    # "OAuth lights up when you've authenticated a harness through the host. no
    # need to build a login service for it." So the Keys app offers no Add on it
    # and GET /providers reads the login artifact the harness lane sealed. Four
    # things have to hold, and each one was a real way to get this wrong:
    #   1. the slot is marked source 'host' (the UI keys its no-Add on this) and
    #      only exists where a harness login can actually fill it — kimi and
    #      every marketplace provider get NO oauth slot, because a slot nothing
    #      can fill is a control the user can only stare at;
    #   2. the host's own login lights it;
    #   3. a PASTED key that merely belongs to the same harness does NOT
    #      (harness:grok:api_key is a console.x.ai paste, not a sign-in — the
    #      whole point of _LOGIN_SLOT naming one file per harness);
    #   4. a legacy pasted '<provider>-oauth' still counts, because resolve()
    #      still serves it — dropping it from this read would blank a working
    #      credential off the screen while it kept routing underneath.
    def _providers_with(rows):
        class OAuthVault(StubVault):
            async def list_secrets(self, pool, *, entity_id):
                return [dict(r) for r in rows]
        patch_vault(vault_api, OAuthVault({}))
        bs.get_pool = lambda: StubPool()
        from server.routes import vault as vroute
        return run(vroute.list_providers())["providers"]

    def _slots_for(rows, provider):
        prov = next(p for p in _providers_with(rows) if p["provider"] == provider)
        return {s["cred_type"]: s for s in prov["slots"]}

    def _live(name, ctype):
        return {"logical_name": name, "cred_type": ctype,
                "created_at": None, "deactivated_at": None}

    has_oauth = sorted(p["provider"] for p in _providers_with([])
                       if "oauth_token" in {s["cred_type"] for s in p["slots"]})
    check("oauth slot exists ONLY where a harness login can fill it",
          has_oauth == ["anthropic", "google", "openai", "xai"],
          f"providers with an oauth slot={has_oauth}")

    empty = _slots_for([], "anthropic")
    check("oauth slot is source 'host' (this is what removes the Add button)",
          empty["oauth_token"]["source"] == "host"
          and empty["api_key"]["source"] == "user"
          and empty["oauth_token"]["harness_name"] == "Claude Code",
          f'oauth={empty["oauth_token"]}')

    lit = _slots_for([_live("harness:claude_oauth", "claude_oauth")], "anthropic")
    check("the host's own harness login LIGHTS the oauth slot",
          lit["oauth_token"]["stored"] is True
          and lit["oauth_token"]["name"] == "harness:claude_oauth",
          f'oauth={lit["oauth_token"]}')

    paste = _slots_for([_live("harness:grok:api_key", "harness_grok")], "xai")
    check("a PASTED harness key does NOT light the sign-in slot",
          paste["oauth_token"]["stored"] is False,
          f'oauth={paste["oauth_token"]}')

    legacy = _slots_for([_live("anthropic-oauth", "oauth_token")], "anthropic")
    check("a legacy pasted <provider>-oauth still counts (it still routes)",
          legacy["oauth_token"]["stored"] is True
          and legacy["oauth_token"]["name"] == "anthropic-oauth",
          f'oauth={legacy["oauth_token"]}')

    def _dead(name, ctype):
        return {"logical_name": name, "cred_type": ctype, "created_at": None,
                "deactivated_at": "2026-08-31T00:00:00+00:00"}

    off = _slots_for([_dead("harness:claude_oauth", "claude_oauth")], "anthropic")
    check("a deactivated sign-in reads OFF, never Stored",
          off["oauth_token"]["stored"] is False
          and off["oauth_token"]["deactivated"] is True,
          f'oauth={off["oauth_token"]}')

    # COEXISTENCE. Both rows can exist at once: a
    # legacy pasted 'anthropic-oauth' and the host-sealed login artifact. The
    # slot must describe the row that is actually ANSWERING, not whichever the
    # candidate tuple happens to list first — reporting the deactivated login
    # while resolve()'s oauth ladder still served the active paste was the
    # app-says-off / routing-says-on split, the same class of bug the typed
    # `stored` read exists to prevent. Asserted in BOTH directions so the fix
    # cannot be "prefer the other one" by accident.
    mixed_a = _slots_for([_live("anthropic-oauth", "oauth_token"),
                          _dead("harness:claude_oauth", "claude_oauth")], "anthropic")
    resolved_a = run(pv.resolve(StubPool(), 7, "anthropic", ("oauth_token",),
                                env_fallback=False))
    check("coexistence: sign-in off + legacy paste ON reads STORED, and names "
          "the row the resolver serves",
          mixed_a["oauth_token"]["stored"] is True
          and mixed_a["oauth_token"]["deactivated"] is False
          and mixed_a["oauth_token"]["name"] == "anthropic-oauth",
          f'oauth={mixed_a["oauth_token"]} resolver={resolved_a}')

    mixed_b = _slots_for([_dead("anthropic-oauth", "oauth_token"),
                          _live("harness:claude_oauth", "claude_oauth")], "anthropic")
    check("coexistence: legacy paste off + sign-in ON reads STORED, and names "
          "the sign-in",
          mixed_b["oauth_token"]["stored"] is True
          and mixed_b["oauth_token"]["deactivated"] is False
          and mixed_b["oauth_token"]["name"] == "harness:claude_oauth",
          f'oauth={mixed_b["oauth_token"]}')

    both_off = _slots_for([_dead("anthropic-oauth", "oauth_token"),
                           _dead("harness:claude_oauth", "claude_oauth")], "anthropic")
    check("coexistence: BOTH off still reads OFF (no active row to prefer)",
          both_off["oauth_token"]["stored"] is False
          and both_off["oauth_token"]["deactivated"] is True,
          f'oauth={both_off["oauth_token"]}')

    # Vault-failure delta (documented, previously unpinned): when rungs 1-2
    # raise, _resolve_typed returns None instead of propagating, so the
    # gateway now reaches its legacy behavioral_params migration where the old
    # ladder went straight to env. Self-healing, no leak — pinned so a future
    # change to it is deliberate.
    class RaisingVault(StubVault):
        async def open_secret(self, pool, *, entity_id, name, actor=None):
            raise RuntimeError("vault unavailable")

        async def list_secrets(self, pool, *, entity_id):
            raise RuntimeError("vault unavailable")

    rv = RaisingVault({})
    patch_vault(vault_api, rv)
    legacy_pool = StubPool({"user_openrouter_key": "sk-or-legacy"})
    bs.get_pool = lambda: legacy_pool
    pg.OPENROUTER_API_KEY = ENV_KEY
    new_key = run(pg._load_user_openrouter_key())
    check("vault-failure delta: raising vault now reaches the legacy migration",
          new_key == "sk-or-legacy")

    print(f"[cred-resolver] {'PASS (0 failing)' if not failures else f'FAIL ({len(failures)} failing): {failures}'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
