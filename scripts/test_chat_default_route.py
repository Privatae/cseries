#!/usr/bin/env python3
"""Chat with no route still answers, on a default the person can see and change.

server/routes/inference.py ensure_default_chat_route, brain/mastercode/
inference_config.py rung 6, the turn (runtime/session.py) and Keys
(server/routes/vault.py store_secret).

Pinned:
  1. a key held, no model on the person's machine, no chat route -> chat is
     written to that provider's recommended chat model (recommended-routes.json,
     the first the live catalog serves), set_by 'host', with the reason kept
     on the route; GET /routing shows the reason on effective.chat
  2. the person's own write replaces it and the reason is gone
  3. a model on the person's machine comes first, key or not: nothing is
     written, and the live route says why
  4. no credential: nothing written; a catalog that serves none of the
     candidates: nothing written, and the next pass waits DEFAULT_CHAT_RETRY_S
  5. a sign-in comes before a key
  6. the turn asks for the default before it says "no chat model", and adding
     a provider key in Keys asks for it too
  9. a sign-in's chat route never replaces one the person saved meanwhile
  7. a route the person writes while the pass reads the catalog stands
  8. a key stored in Keys is not held back by an earlier pass's miss

Container-free: stub pool; the provider status and the catalog stubbed.
APP_ROOT=<repo> python3 scripts/test_chat_default_route.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

_ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain")]
os.environ.pop("OPENROUTER_API_KEY", None)
os.environ.pop("CSERIES_LANE_HOST", None)

from mastercode import inference_config as ic  # noqa: E402
from mastercode import owner_endpoints  # noqa: E402
from mastercode.runtime import web_research  # noqa: E402
from server import brain_service  # noqa: E402
from server.routes import inference as inf  # noqa: E402

EID = 1
FAILS: list[str] = []


def check(name, ok, detail=""):
    print(("OK   " if ok else "FAIL ") + name + ("" if ok else f"  -- {detail}"))
    if not ok:
        FAILS.append(name)


class Pool:
    def __init__(self, *, routing=None, beats=None):
        self.bp = {"model_routing": routing or {}}
        self.beats = beats or []
        self.log: list[tuple] = []

    async def fetchrow(self, q, *a):
        if "FROM entity_registry" in q:
            return {"behavioral_params": json.dumps(self.bp)}
        return None

    async def fetch(self, q, *a):
        if "h.anchor" in q:
            return [{"id": b["token_id"], "notes": b["notes"], "anchor": None} for b in self.beats]
        if "network_bastion_heartbeat" in q:
            return self.beats
        return []

    async def execute(self, q, *a):
        if q.startswith("UPDATE entity_registry SET behavioral_params"):
            self.bp = json.loads(a[0])
        elif "INSERT INTO model_route_log" in q:
            self.log.append(a)
        return "OK"


def beat(host, *, llm=()):
    return {"token_id": 101, "hostname": host, "reported_ip": "10.0.0.5",
            "models": "[]", "llm_models": json.dumps(list(llm)), "llm_port": 18080,
            "notes": json.dumps({"transport": "unix"}), "anchor": None}


class _M:
    def __init__(self):
        self.undo = []

    def set(self, obj, name, value):
        self.undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def restore(self):
        for obj, name, old in reversed(self.undo):
            setattr(obj, name, old)


ANTHROPIC = web_research.recommended_chat_candidates("anthropic")
SUBSCRIPTION = web_research.recommended_chat_candidates("anthropic-subscription")


def setup(pool, *, held=(), serves=None, catalog_calls=None):
    """held: [(provider, via)]. serves: {provider: [ids]} the live catalogs list."""
    m = _M()
    m.set(brain_service, "get_pool", lambda: pool)
    m.set(brain_service, "_entity_id", EID)
    inf._DEFAULT_CHAT_MISS["at"] = 0.0
    inf._DEFAULT_CHAT_LOCK = None

    async def _status(p, eid):
        rows = [{"id": prov, "has_key": True, **({"via": via} if via else {})}
                for prov, via in held]
        return rows + [{"id": "local", "has_key": True}]

    async def _direct(p):
        if catalog_calls is not None:
            catalog_calls.append(p)
        ids = (serves or {}).get(p)
        if ids is None:
            return [], f"{p} catalog unreachable"
        return [{"id": i} for i in ids], None

    async def _no_wait(*a, **k):
        return None

    async def _no_harness():
        return []

    async def _no_owner(pool, eid):
        return []

    async def _names(eid):
        return ()
    from server.vault import api as vault
    from server.network import pairing_grants

    async def _vault_rows(pool, *, entity_id):
        return [{"logical_name": prov, "cred_type": "api_key", "deactivated_at": None}
                for prov, _ in held]
    m.set(inf, "_provider_status", _status)
    m.set(inf, "_direct_models", _direct)
    m.set(inf, "await_effort_catalog", _no_wait)
    m.set(inf, "_installed_harness_options", _no_harness)
    m.set(owner_endpoints, "models", _no_owner)
    m.set(pairing_grants, "_own_host_names", _names)
    m.set(vault, "list_secrets", _vault_rows)
    return m


def run(coro):
    return asyncio.run(coro)


def test_a_key_and_nothing_local_gets_the_recommended_model():
    pool = Pool()
    m = setup(pool, held=[("anthropic", "api_key")], serves={"anthropic": ANTHROPIC})
    try:
        got = run(inf.ensure_default_chat_route(pool, EID))
        stored = pool.bp["model_routing"].get("chat") or {}
        check("1. a key, nothing local, no route -> chat set to the recommended model",
              bool(ANTHROPIC) and got and got["provider"] == "anthropic"
              and got["model"] == ANTHROPIC[0]
              and (stored.get("provider"), stored.get("model")) == ("anthropic", ANTHROPIC[0]),
              repr((got, stored)))
        check("1. the route keeps why it was chosen, in words",
              "recommended model" in (stored.get("default_why") or "")
              and "Anthropic key" in (stored.get("default_why") or ""), repr(stored))
        check("1. logged as the host filling an unrouted role",
              pool.log and pool.log[-1][7] == "host", repr(pool.log))
        r = run(ic.resolve_role_route(pool, EID, "chat"))
        check("1. chat now resolves to it", (r["provider"], r["model"]) == ("anthropic", ANTHROPIC[0]),
              repr(r))
        view = run(inf.get_current_routing())
        eff = view["effective"]["chat"]
        check("1. GET /routing shows the route and why on effective.chat",
              eff.get("model") == ANTHROPIC[0] and "recommended model" in (eff.get("default_why") or "")
              and "recommended model" in (view["routing"]["chat"].get("default_why") or ""),
              repr(eff))
        again = run(inf.ensure_default_chat_route(pool, EID))
        check("1. a second pass writes nothing (chat has a route)", again is None and len(pool.log) == 1,
              repr((again, pool.log)))
        # 2. the person's own pick replaces it
        run(inf.set_role_model(inf.SetRoleModel(role="chat", provider="anthropic",
                                                model=ANTHROPIC[-1])))
        stored = pool.bp["model_routing"]["chat"]
        view = run(inf.get_current_routing())
        check("2. the person's own write replaces the default and its reason",
              stored.get("model") == ANTHROPIC[-1] and "default_why" not in stored
              and "default_why" not in view["effective"]["chat"]
              and pool.log[-1][7] == "user", repr((stored, view["effective"]["chat"])))
    finally:
        m.restore()


def test_a_local_model_comes_first():
    pool = Pool(beats=[beat("desk", llm=["qwen3-8b-q4"])])
    calls: list = []
    m = setup(pool, held=[("anthropic", "api_key")], serves={"anthropic": ANTHROPIC},
              catalog_calls=calls)
    try:
        got = run(inf.ensure_default_chat_route(pool, EID))
        check("3. with a model on the person's machine nothing is written, key or not",
              got is None and "chat" not in pool.bp["model_routing"] and not calls,
              repr((got, pool.bp, calls)))
        view = run(inf.get_current_routing())
        eff = view["effective"]["chat"]
        check("3. the live route is the local model, and says why",
              (eff.get("provider"), eff.get("model")) == ("local", "qwen3-8b-q4")
              and "own machine" in (eff.get("default_why") or ""), repr(eff))
    finally:
        m.restore()


def test_nothing_held_or_nothing_served():
    pool = Pool()
    m = setup(pool, held=[])
    try:
        check("4. no credential: nothing written",
              run(inf.ensure_default_chat_route(pool, EID)) is None
              and "chat" not in pool.bp["model_routing"])
    finally:
        m.restore()
    pool = Pool()
    calls: list = []
    m = setup(pool, held=[("anthropic", "api_key")], serves={"anthropic": ["not-a-candidate"]},
              catalog_calls=calls)
    try:
        got = run(inf.ensure_default_chat_route(pool, EID))
        n = len(calls)
        got2 = run(inf.ensure_default_chat_route(pool, EID))
        check("4. a catalog serving none of the candidates: nothing written, and the "
              "next pass does not ask again within the retry window",
              got is None and got2 is None and "chat" not in pool.bp["model_routing"]
              and n == 1 and len(calls) == 1, repr((got, got2, calls)))
    finally:
        m.restore()


def test_a_signin_comes_before_a_key():
    pool = Pool()
    m = setup(pool, held=[("anthropic", "api_key"), ("anthropic-subscription", "signin")],
              serves={"anthropic": ANTHROPIC, "anthropic-subscription": SUBSCRIPTION})
    try:
        got = run(inf.ensure_default_chat_route(pool, EID))
        check("5. a Claude subscription sign-in is used before an Anthropic key",
              bool(SUBSCRIPTION) and got and got["provider"] == "anthropic-subscription"
              and "Claude subscription" in got["why"], repr(got))
    finally:
        m.restore()


def test_a_route_written_during_the_pass_stands():
    pool = Pool()
    m = setup(pool, held=[("anthropic", "api_key")], serves={"anthropic": ANTHROPIC})
    try:
        async def _direct_while_person_picks(p):
            # The person picks a chat model in the Orchestrator while the
            # Host is still reading the catalog (up to 15 s).
            pool.bp["model_routing"]["chat"] = {"provider": "anthropic", "model": ANTHROPIC[-1]}
            return [{"id": i} for i in ANTHROPIC], None
        m.set(inf, "_direct_models", _direct_while_person_picks)
        got = run(inf.ensure_default_chat_route(pool, EID))
        stored = pool.bp["model_routing"].get("chat") or {}
        check("7. a route the person wrote while the pass read the catalog is never replaced",
              got is None and stored == {"provider": "anthropic", "model": ANTHROPIC[-1]}
              and not pool.log, repr((got, stored, pool.log)))
        check("7. that is not a miss: the next pass is not held back",
              inf._DEFAULT_CHAT_MISS["at"] == 0.0, repr(inf._DEFAULT_CHAT_MISS))
    finally:
        m.restore()


def test_a_route_written_during_a_signin_stands():
    pool = Pool()
    m = setup(pool, held=[("anthropic-subscription", "signin")],
              serves={"anthropic-subscription": SUBSCRIPTION})
    try:
        from server.routes import vault as vault_routes

        async def _direct_while_person_picks(p):
            pool.bp["model_routing"]["chat"] = {"provider": "anthropic", "model": ANTHROPIC[-1]}
            return [{"id": i} for i in SUBSCRIPTION], None
        m.set(inf, "_direct_models", _direct_while_person_picks)
        routed, note = run(vault_routes._route_chat_if_unrouted("anthropic-subscription", pool, EID))
        stored = pool.bp["model_routing"].get("chat") or {}
        check("9. a sign-in never replaces a route the person saved while its catalog was read",
              routed is None and note is None
              and stored == {"provider": "anthropic", "model": ANTHROPIC[-1]} and not pool.log,
              repr((routed, note, stored, pool.log)))
        pool2 = Pool()

        async def _served(p):
            return [{"id": i} for i in SUBSCRIPTION], None
        m.set(inf, "_direct_models", _served)
        m.set(brain_service, "get_pool", lambda: pool2)
        routed, note = run(vault_routes._route_chat_if_unrouted("anthropic-subscription", pool2, EID))
        check("9. with nothing saved meanwhile, the sign-in routes chat as before (set_by user)",
              routed and routed["model"] == SUBSCRIPTION[0]
              and pool2.bp["model_routing"]["chat"]["provider"] == "anthropic-subscription"
              and pool2.log and pool2.log[-1][7] == "user", repr((routed, note, pool2.bp)))
    finally:
        m.restore()


def test_a_new_key_is_not_held_back_by_an_earlier_miss():
    pool = Pool()
    serves = {"anthropic": ["not-a-candidate"]}
    m = setup(pool, held=[("anthropic", "api_key")], serves=serves)
    try:
        run(inf.ensure_default_chat_route(pool, EID))
        check("8. setup: the first pass missed and set the retry window",
              inf._DEFAULT_CHAT_MISS["at"] > 0.0 and "chat" not in pool.bp["model_routing"])
        # The person now adds a key that does serve a candidate (stand-in: the
        # catalog serves it from now on). Keys asks for the default at once.
        serves["anthropic"] = ANTHROPIC
        from server.routes import vault as vroutes
        from mastercode.runtime import identity_hold
        spawned: list = []
        m.set(vroutes, "_pool", lambda: pool)
        m.set(vroutes, "_entity_id", lambda: EID)
        m.set(identity_hold, "spawn", lambda coro: spawned.append(coro))
        vroutes._default_chat_after_key("anthropic")
        first = list(spawned)
        for c in first:
            run(c)
        for c in spawned[len(first):]:
            c.close()   # what the route write spawns (held requests): not this test's
        stored = pool.bp["model_routing"].get("chat") or {}
        check("8. a key stored in Keys gets its default without waiting out an earlier miss",
              len(first) == 1 and stored.get("model") == ANTHROPIC[0], repr((first, stored)))
    finally:
        m.restore()


def test_the_turn_and_keys_ask_for_it():
    sys.path.insert(0, os.path.join(_ROOT, "scripts"))
    from turn_source import turn_source
    src = turn_source()
    i_def = src.find("ensure_default_chat_route")
    i_msg = src.find("I don't have a chat model configured yet")
    check("6. the turn asks for the default before it says there is no chat model",
          0 <= i_def < i_msg, repr((i_def, i_msg)))
    vsrc = open(os.path.join(_ROOT, "server/routes/vault.py"), encoding="utf-8").read()
    body = vsrc[vsrc.index("async def store_secret"):vsrc.index("async def _engine_key_landed")]
    check("6. storing a key in Keys asks for the default chat route",
          "_default_chat_after_key(req.name)" in body
          and "ensure_default_chat_route" in vsrc[vsrc.index("def _default_chat_after_key"):])


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                check(name, False, repr(e))
    print(f"\nFAILURES: {len(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
