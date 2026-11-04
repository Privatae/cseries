#!/usr/bin/env python3
"""A Host with no chat route runs on the model on the person's own machine.

brain/mastercode/inference_config.py: an unrouted chat role, with or without a
key or sign-in for a provider, and a model served by one of the person's online
machines (a bastion's `llm_models` or `models`, or a model server they named)
→ chat resolves to that model, live (rung 6, `local_default` on the route).
Getting Started offers the same model first (GET/POST
/api/inference/local-default).

Pinned:
  1. no key + a local model → the local model; an OpenAI-compatible server's
     model before ollama's; an embedding model never; the route says why
  2. a key + a local model → the local model too (it was left unconfigured
     before 0.1.17, so a person who added any key had no chat at all)
  3. neither → unconfigured, and the turn's message is today's
  4. an explicit route is untouched; no other role is defaulted
  5. what counts as a key: the Keys app's own reading; a deactivated row does
     not count; an unreadable vault counts (never moves a Host by a guess)
  6. the Getting Started routes: what they report, and the press routes chat
     to a served model only, as the person's choice
  7. only the person's own machines: one this Host is in charge of (its
     anchor names this Host) or the machine it runs on (the unix socket, or
     the loopback-only enroll port). A machine where it is a guest is never
     picked by default nor offered by Getting Started; with both, the own
     machine's model wins

Container-free: stub pool, the vault and the owner's model servers stubbed.
APP_ROOT=<repo> python3 scripts/test_local_model_default.py
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import sys

_ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain")]
os.environ.pop("OPENROUTER_API_KEY", None)
os.environ.pop("CSERIES_LANE_HOST", None)

from mastercode import inference_config as ic  # noqa: E402
from mastercode import owner_endpoints  # noqa: E402

EID = 1


class Pool:
    def __init__(self, *, routing=None, beats=None):
        self.bp = {"model_routing": routing or {}}
        self.beats = beats if beats is not None else []

    async def fetchrow(self, q, *a):
        if "FROM entity_registry" in q:
            return {"behavioral_params": json.dumps(self.bp)}
        return None

    async def fetch(self, q, *a):
        if "h.anchor" in q:   # inference_config.own_machine_tokens
            return [{"id": b["token_id"], "notes": b["notes"], "anchor": b["anchor"]}
                    for b in self.beats]
        if "network_bastion_heartbeat" in q:
            return self.beats
        return []


# This Host's names (pairing_grants._own_host_names), the id'd one first.
OWN_NAMES = ("home3f9a1c2e.cseries.localhost", "home.cseries.localhost")
_tids = iter(range(100, 10_000))


def beat(host, *, models=(), llm=(), ip="10.0.0.5", port=18080, transport="unix",
         anchor_host=None):
    """A bastion row. transport: how it redeemed (None: over the platform,
    no local transport). anchor_host: the Host its anchor names (None: none)."""
    notes = {"transport": transport} if transport else {}
    anchor = ({"owner": "someone", "anchor_host": anchor_host} if anchor_host else None)
    return {"token_id": next(_tids), "hostname": host, "reported_ip": ip,
            "models": json.dumps(list(models)), "llm_models": json.dumps(list(llm)),
            "llm_port": port, "notes": json.dumps(notes),
            "anchor": json.dumps(anchor) if anchor else None}


def guest(host, **kw):
    """Another person's machine: paired over the platform, anchored to their Host."""
    return beat(host, transport=None, anchor_host="studio.privatae.ai", **kw)


class _M:
    def __init__(self):
        self.undo = []

    def set(self, obj, name, value):
        self.undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def restore(self):
        for obj, name, old in reversed(self.undo):
            setattr(obj, name, old)


def with_vault(rows=(), *, broken=False, owner_servers=(), names=OWN_NAMES):
    """Run the test with these Keys rows (metadata only, as list_secrets),
    these owner-named model servers, and these names for this Host."""
    def wrap(fn):
        def run():
            m = _M()
            from server.vault import api as vault
            from server.network import pairing_grants

            async def _list(pool, *, entity_id):
                if broken:
                    raise RuntimeError("vault down")
                return [dict(r) for r in rows]

            async def _owner(pool, eid):
                return [dict(o) for o in owner_servers]

            async def _names(eid):
                return tuple(names)
            m.set(vault, "list_secrets", _list)
            m.set(owner_endpoints, "models", _owner)
            m.set(pairing_grants, "_own_host_names", _names)
            try:
                fn()
            finally:
                m.restore()
        run.__name__ = fn.__name__
        return run
    return wrap


def chat(pool):
    return asyncio.run(ic.resolve_role_route(pool, EID, "chat"))


LOCAL = [beat("desk", models=["nomic-embed-text:latest", "all-minilm", "llama3.2:3b"],
              llm=["qwen3-8b-q4"])]
KEY = [{"logical_name": "openrouter", "cred_type": "api_key", "deactivated_at": None}]


# ── 1-3 ──────────────────────────────────────────────────────────────────────

@with_vault()
def test_no_key_and_a_local_model_runs_on_it():
    r = chat(Pool(beats=LOCAL))
    assert (r["provider"], r["model"]) == ("local", "qwen3-8b-q4"), r
    assert r["base_url"] == "http://10.0.0.5:18080", "the server that serves it, resolved live"
    assert {k: r["local_default"][k] for k in ("machine", "kind")} == \
        {"machine": "desk", "kind": "llm-openai"}, r["local_default"]
    assert "qwen3-8b-q4" in r["local_default"]["why"] and "own machine" in r["local_default"]["why"]
    assert not r.get("unconfigured")
    # ollama only: its chat model, never one of its embedders
    r = chat(Pool(beats=[beat("desk", models=["nomic-embed-text:latest", "all-minilm",
                                               "llama3.2:3b"])]))
    assert (r["provider"], r["model"]) == ("local", "llama3.2:3b"), r
    assert r["base_url"] == "http://10.0.0.5:11434"
    # embedders alone: nothing to chat with
    r = chat(Pool(beats=[beat("desk", models=["nomic-embed-text:latest", "all-minilm"])]))
    assert r["unconfigured"] and not r["model"], r


@with_vault(KEY)
def test_a_key_and_a_local_model_still_runs_on_it():
    r = chat(Pool(beats=LOCAL))
    assert (r["provider"], r["model"]) == ("local", "qwen3-8b-q4"), r
    assert r.get("local_default") and not r.get("unconfigured"), r


@with_vault()
def test_neither_is_todays_message():
    r = chat(Pool(beats=[]))
    assert not r["model"] and "pick a provider and model" in r["unconfigured"], r
    from turn_source import turn_source
    assert "I don't have a chat model configured yet. Open the " in turn_source(), \
        "the turn still says what it always said"


# ── 4 ────────────────────────────────────────────────────────────────────────

@with_vault()
def test_an_explicit_route_and_other_roles_are_untouched():
    r = chat(Pool(routing={"chat": {"provider": "openrouter", "model": "some/model"}}, beats=LOCAL))
    assert (r["provider"], r["model"]) == ("openrouter", "some/model") and "local_default" not in r
    for role in ("image_gen", "embedding", "title"):
        r = asyncio.run(ic.resolve_role_route(Pool(beats=LOCAL), EID, role))
        assert not r.get("model") or r.get("provider") != "local", (role, r)


# ── 5 ────────────────────────────────────────────────────────────────────────

def _holds(rows=(), broken=False):
    out = []

    @with_vault(rows, broken=broken)
    def t():
        out.append(asyncio.run(ic.holds_provider_credential(Pool(), EID)))
    t()
    return out[0]


def test_what_counts_as_a_key():
    assert _holds() is False
    assert _holds(KEY) is True
    assert _holds([{"logical_name": "Anthropic key", "cred_type": "api_key", "deactivated_at": None}]) is True
    assert _holds([{"logical_name": "anthropic-oauth", "cred_type": "oauth_token", "deactivated_at": None}]) is True
    off = dt.datetime.now(dt.timezone.utc)
    assert _holds([{"logical_name": "openrouter", "cred_type": "api_key", "deactivated_at": off}]) is False
    assert _holds([{"logical_name": "github-token", "cred_type": "opaque", "deactivated_at": None}]) is False
    assert _holds(broken=True) is True, "an unreadable vault never moves a Host by a guess"
    m = _M()
    m.set(ic, "OPENROUTER_API_KEY", "set-in-the-environment")
    try:
        assert _holds() is True
    finally:
        m.restore()


# ── 6 ────────────────────────────────────────────────────────────────────────

def _routes(pool, applied):
    m = _M()
    from server import brain_service
    from server.routes import inference as inf
    m.set(brain_service, "get_pool", lambda: pool)
    m.set(brain_service, "_entity_id", EID)

    async def _apply(body, *, set_by="user", **kw):
        applied.append((body.role, body.provider, body.model, set_by))
        return {"set": True, "role": body.role, "provider": body.provider, "model": body.model}
    m.set(inf, "apply_role_route", _apply)
    return m, inf


@with_vault()
def test_getting_started_routes():
    applied: list = []
    m, inf = _routes(Pool(beats=LOCAL), applied)
    try:
        got = asyncio.run(inf.get_local_default())
        assert got == {"candidate": {"model": "qwen3-8b-q4", "machine": "desk", "kind": "llm-openai"},
                       "chat_routed": False, "holds_key": False, "by_default": True}, got
        res = asyncio.run(inf.use_local_default(inf.UseLocalModel(model="qwen3-8b-q4")))
        assert applied == [("chat", "local", "qwen3-8b-q4", "user")] and res["machine"] == "desk"
        # a model nobody serves, or an embedder, is refused and nothing is written
        for bad in ("not-served", "nomic-embed-text:latest"):
            try:
                asyncio.run(inf.use_local_default(inf.UseLocalModel(model=bad)))
                assert False, bad
            except inf.HTTPException as e:
                assert e.status_code == 409
        assert len(applied) == 1
    finally:
        m.restore()
    # chat already routed: offered nothing new
    m, inf = _routes(Pool(routing={"chat": {"provider": "openrouter", "model": "x/y"}}, beats=LOCAL), [])
    try:
        got = asyncio.run(inf.get_local_default())
        assert got["chat_routed"] is True and got["by_default"] is False
    finally:
        m.restore()


# ── 7 ────────────────────────────────────────────────────────────────────────

GUEST = [guest("friends-pc", llm=["their-70b"], models=["llama3.1:8b"])]


@with_vault()
def test_a_guest_machine_is_never_the_default():
    r = chat(Pool(beats=GUEST))
    assert not r["model"] and "pick a provider and model" in r["unconfigured"], r
    # a machine nobody is in charge of, reached over the platform: not theirs either
    r = chat(Pool(beats=[beat("lan-box", transport=None, llm=["x-8b"])]))
    assert not r["model"] and r["unconfigured"], r
    # Getting Started neither offers it nor routes chat to it on a press
    applied: list = []
    m, inf = _routes(Pool(beats=GUEST), applied)
    try:
        got = asyncio.run(inf.get_local_default())
        assert got["candidate"] is None and got["by_default"] is False, got
        try:
            asyncio.run(inf.use_local_default(inf.UseLocalModel(model="their-70b")))
            assert False, "a guest machine's model was routed by the slide"
        except inf.HTTPException as e:
            assert e.status_code == 409
        assert applied == []
    finally:
        m.restore()


@with_vault()
def test_a_machine_this_host_is_in_charge_of_is_the_default():
    for anchor in OWN_NAMES:   # the name it pairs under now, and the bare one
        r = chat(Pool(beats=[beat("studio", transport=None, anchor_host=anchor,
                                  models=["llama3.2:3b"])]))
        assert (r["provider"], r["model"]) == ("local", "llama3.2:3b"), (anchor, r)
        assert r["local_default"]["machine"] == "studio"


@with_vault()
def test_the_machine_the_host_runs_on_is_the_default():
    for transport in ("unix", "tcp"):
        r = chat(Pool(beats=[beat("this-pc", transport=transport, llm=["qwen3-8b-q4"])]))
        assert (r["provider"], r["model"]) == ("local", "qwen3-8b-q4"), (transport, r)
    # the enroll port reached from other machines (CSERIES_LANE_HOST): a
    # bastion that came in on it is not proven to be on this machine
    os.environ["CSERIES_LANE_HOST"] = "host.example.net"
    try:
        r = chat(Pool(beats=[beat("far-pc", transport="tcp", llm=["qwen3-8b-q4"])]))
        assert not r["model"] and r["unconfigured"], r
    finally:
        os.environ.pop("CSERIES_LANE_HOST", None)


@with_vault()
def test_with_both_the_own_machine_wins():
    # the guest's OpenAI-compatible server would rank first if it were a candidate
    beats = GUEST + [beat("desk", models=["llama3.2:3b"])]
    r = chat(Pool(beats=beats))
    assert (r["model"], r["local_default"]["machine"]) == ("llama3.2:3b", "desk"), r
    m, inf = _routes(Pool(beats=beats), [])
    try:
        got = asyncio.run(inf.get_local_default())
        assert got["candidate"] == {"model": "llama3.2:3b", "machine": "desk", "kind": "ollama"}, got
    finally:
        m.restore()


@with_vault(names=())
def test_unreadable_names_never_make_a_machine_theirs():
    r = chat(Pool(beats=[beat("studio", transport=None, anchor_host=OWN_NAMES[0],
                              models=["llama3.2:3b"])]))
    assert not r["model"] and r["unconfigured"], r


@with_vault(owner_servers=[{"id": "mistral:7b", "bastion": "192.168.1.20:11434",
                            "endpoint": "http://192.168.1.20:11434", "kind": "ollama",
                            "source": "owner"}])
def test_a_server_the_person_named_still_counts():
    r = chat(Pool(beats=GUEST))
    assert (r["model"], r["local_default"]["machine"]) == ("mistral:7b", "192.168.1.20:11434"), r


def test_the_docstring_names_the_variable_the_bastion_reads():
    src = open(os.path.join(_ROOT, "brain/mastercode/model_backends.py")).read()
    assert "PRIVATAE_LLM_ADDR" in src and "CSERIES_LLM_ADDR" not in src


def main() -> int:
    sys.path.insert(0, os.path.join(_ROOT, "scripts"))
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print("OK   " + name)
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name} -- {type(e).__name__}: {e}")
    print(f"\nFAILURES: {failed} / {len(tests)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
