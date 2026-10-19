#!/usr/bin/env python3
"""
Turn types — what a typed turn reaches with no tool argument naming it
(a typed turn reaches only the tools, keys and servers its type lists).

Two paths the platform takes on the turn's behalf, outside the gate's
argument checks:

  1. A dispatched agent's model. A model a machine on the user's network
     serves runs there (model_backends.match_local_model). Under a typed
     turn it runs only on a server the type lists ('*' = any). A model
     served only on servers outside the type is refused at dispatch and is
     never sent to a cloud model in its place. No local match at all keeps
     today's route.
  2. The browser's cold wake. harness_backend.ensure_awake restores the
     kept profile (vault entry browser:profile, the user's sign-ins). A
     typed turn whose keys do not hold it, or a closed turn, wakes a clean
     browser, and the next release cannot overwrite the kept copy with it.

Untyped is asserted unchanged FIRST for both. Container-free:
APP_ROOT=<repo> python3 scripts/<this>. No pod, no DB, no network; the
real functions run against stubs.

Exit 0 = all pass; exit 1 + [turn-types-reach] FAIL lines otherwise.
"""

import asyncio
import os
import sys

FAILS = []


def check(name: str, ok: bool, detail: str = ""):
    print(f"[turn-types-reach] {'ok  ' if ok else 'FAIL'} {name}"
          + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


class _Pool:
    """Answers every read with nothing; nothing here may need a row."""
    async def execute(self, sql, *a, **k): return None
    async def fetch(self, sql, *a, **k): return []
    async def fetchval(self, sql, *a, **k): return None
    async def fetchrow(self, sql, *a, **k): return None


def _type(**over):
    base = {
        "id": "00000000-0000-0000-0000-000000000001", "name": "Finance",
        "status": "active", "keywords": ["invoice"], "context": {},
        "skill": "finance", "tool_groups": ["vault", "comms"],
        "grant_names": ["read_context"], "keys": ["stripe-live"],
        "bastions": ["books-1"], "model": None, "thread_id": None, "version": 1,
    }
    base.update(over)
    return base


# What the user's machines serve right now, in list_local_models' shape
# (model_backends.py: [{id, bastion, endpoint, kind}]). qwen3 is on two
# servers, the unlisted one FIRST; llama-x is only on the unlisted one.
SERVED = [
    {"id": "qwen3:latest", "bastion": "gpu-a", "endpoint": "http://[fd00::a]:11434", "kind": "ollama"},
    {"id": "qwen3", "bastion": "books-1", "endpoint": "http://[fd00::b]:11434", "kind": "ollama"},
    {"id": "llama-x", "bastion": "gpu-a", "endpoint": "http://[fd00::a]:18080", "kind": "llm-openai"},
]
GPU_A_OLLAMA, BOOKS_1_OLLAMA, GPU_A_LLM = (m["endpoint"] for m in SERVED)


def _try(fn):
    """Run one case; an exception is that case's answer, not the test's end."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        return e


def main() -> int:
    app_root = os.environ.get("APP_ROOT", "/app")
    sys.path.insert(0, os.path.join(app_root, "brain"))
    sys.path.insert(1, app_root)

    from mastercode import model_backends as mb
    from mastercode.runtime import turn_types as tt
    from mastercode.runtime import model_validation as mv
    import mastercode.runtime.scoped_agent as sa

    async def fake_list_local_models(pool, entity_id, **kw):
        return [dict(m) for m in SERVED]
    mb.list_local_models = fake_list_local_models

    # ── 1. match_local_model ────────────────────────────────────────────
    def _match(model, **kw):
        return asyncio.run(mb.match_local_model(_Pool(), 1, model, **kw))

    r = _try(lambda: _match("qwen3"))
    check("match: untyped (no `allowed`) is unchanged — the first server",
          isinstance(r, dict) and r.get("bastion") == "gpu-a", f"got {r!r}")
    r = _try(lambda: _match("qwen3", allowed=None))
    check("match: allowed=None is any server",
          isinstance(r, dict) and r.get("bastion") == "gpu-a", f"got {r!r}")
    r = _try(lambda: _match("qwen3", allowed={"books-1"}))
    check("match: `allowed` picks the first match on a listed server",
          isinstance(r, dict) and r.get("bastion") == "books-1", f"got {r!r}")
    r = _try(lambda: _match("qwen3", allowed={"*"}))
    check("match: '*' in `allowed` is any server",
          isinstance(r, dict) and r.get("bastion") == "gpu-a", f"got {r!r}")
    r = _try(lambda: _match("llama-x", allowed={"books-1"}))
    check("match: a model served only off the list matches nothing", r is None, f"got {r!r}")
    r = _try(lambda: _match("qwen3", allowed=set()))
    check("match: an empty list matches nothing", r is None, f"got {r!r}")

    # ── 2. a dispatched agent's model ───────────────────────────────────
    calls = []

    async def fake_call_model(**kw):
        calls.append(kw)
        return {"content": "done", "tool_calls": None, "raw_message": {},
                "tokens_used": 0, "prompt_tokens": 0, "completion_tokens": 0}

    async def fake_schemas(pool, eid, **kw):
        return []

    async def fake_route(pool, eid, role):
        return {"provider": "openrouter", "model": "test/host-model", "base_url": None,
                "fallback_provider": None, "fallback_model": None}

    sa.call_model = fake_call_model
    sa._build_tool_schemas = fake_schemas
    sa.resolve_role_route = fake_route
    # The admission bench is a separate wall with its own gate; out of the way here.
    mv.status_for = lambda provider, model: {"enforced": False, "validated": True}

    def _agent(model, provider=None, turn_type=None):
        calls.clear()
        out = asyncio.run(sa.run_scoped_agent(
            _Pool(), entity_id=1, task_prompt="t", tool_scope=[], grant=[],
            model=model, provider=provider, session_id=None,
            max_rounds=2, turn_type=turn_type))
        return out, [(c.get("provider"), c.get("base_url")) for c in calls]

    def _ran_on(res, provider, base_url):
        if isinstance(res, Exception):
            return False
        _out, routes = res
        return bool(routes) and all(r == (provider, base_url) for r in routes)

    def _refused(res, *names):
        if isinstance(res, Exception):
            return False
        out, routes = res
        text = str(out.get("result") or "")
        return (not routes and "not started" in text and out.get("rounds") == 0
                and all(n in text for n in names))

    # Untyped first: exactly today's routes.
    res = _try(lambda: _agent("qwen3"))
    check("agent untyped: a model a machine serves runs LOCAL on the first server",
          _ran_on(res, "local", GPU_A_OLLAMA), f"got {res!r}")
    res = _try(lambda: _agent("no-such-model"))
    check("agent untyped: a model no machine serves stays on OpenRouter",
          _ran_on(res, "openrouter", None), f"got {res!r}")
    res = _try(lambda: _agent("llama-x", provider="local"))
    check("agent untyped: provider='local' runs on the server that serves it",
          _ran_on(res, "local", GPU_A_LLM), f"got {res!r}")

    fin = _type()
    res = _try(lambda: _agent("llama-x", turn_type=fin))
    check("agent typed: a model served only on an unlisted server is not started",
          _refused(res, "gpu-a", "Finance"), f"got {res!r}")
    check("agent typed: ...and no model call was made, cloud or local",
          not isinstance(res, Exception) and res[1] == [], f"got {res!r}")
    check("agent typed: ...and the refusal is not scored against the model",
          not isinstance(res, Exception) and res[0].get("lane_unavailable") is True,
          f"got {res!r}")
    res = _try(lambda: _agent("llama-x", provider="local", turn_type=fin))
    check("agent typed: provider='local' on an unlisted server is not started",
          _refused(res, "gpu-a", "Finance"), f"got {res!r}")
    res = _try(lambda: _agent("qwen3", turn_type=fin))
    check("agent typed: a model on a listed server runs THERE, not on the first one",
          _ran_on(res, "local", BOOKS_1_OLLAMA), f"got {res!r}")
    res = _try(lambda: _agent("qwen3", provider="local", turn_type=fin))
    check("agent typed: provider='local' runs on the listed server",
          _ran_on(res, "local", BOOKS_1_OLLAMA), f"got {res!r}")
    res = _try(lambda: _agent("llama-x", turn_type=_type(bastions=["*"])))
    check("agent typed: '*' reaches any server",
          _ran_on(res, "local", GPU_A_LLM), f"got {res!r}")
    res = _try(lambda: _agent("no-such-model", turn_type=fin))
    check("agent typed: a model no machine serves keeps today's route (OpenRouter)",
          _ran_on(res, "openrouter", None), f"got {res!r}")
    res = _try(lambda: _agent("qwen3", turn_type=tt.closed_type("store unreadable")))
    check("agent closed: a closed turn lists no server, so a local model is not started",
          _refused(res, "gpu-a", "books-1"), f"got {res!r}")

    # The dispatch record scores the refusal 'unavailable', never 'fail'.
    try:
        from mastercode.runtime.agent_dispatch import _finalize_record
        res = _agent("llama-x", turn_type=fin)
        verdict = _finalize_record(res[0], None, "llama-x").get("verdict")
    except Exception as e:  # noqa: BLE001
        verdict = e
    check("agent typed: the run record's verdict is 'unavailable'",
          verdict == "unavailable", f"got {verdict!r}")

    # ── 3. the browser's cold wake ──────────────────────────────────────
    from server import brain_service
    from server.applications import harness_backend as hb
    from server.applications import browser_profile as bp

    restored = []

    async def fake_restore(pool, entity_id):
        restored.append(entity_id)
        return True

    async def fake_wake(harness_id, creds=None):
        return {"ok": True, "url": f"http://harness-{harness_id}.entity-1.svc.cluster.local:8832"}

    async def fake_egress_token(pool, subject):
        return "subject-token"

    bp.restore = fake_restore
    hb._wake = fake_wake
    hb.egress_subject_token = fake_egress_token
    brain_service._pool = _Pool()

    def _cold_wake(turn_type):
        restored.clear()
        hb._state.pop("browser", None)
        hb._wake_locks.pop("browser", None)

        async def _go():
            tok = tt.set_current_turn_type(turn_type)
            try:
                return await hb.ensure_awake("browser", "browser")
            finally:
                tt.CURRENT_TURN_TYPE.reset(tok)
        up = asyncio.run(_go())
        return up, len(restored)

    res = _try(lambda: _cold_wake(None))
    check("browser untyped: a cold wake restores the kept profile",
          not isinstance(res, Exception) and res[0] and res[1] == 1, f"got {res!r}")
    res = _try(lambda: _cold_wake(fin))
    check("browser typed: a type without browser:profile wakes WITHOUT the restore",
          not isinstance(res, Exception) and res[1] == 0, f"got {res!r}")
    check("browser typed: ...and the wake itself still succeeds",
          not isinstance(res, Exception) and bool(res[0]), f"got {res!r}")
    res = _try(lambda: _cold_wake(_type(keys=["stripe-live", bp.VAULT_NAME])))
    check("browser typed: a type that lists browser:profile restores it",
          not isinstance(res, Exception) and res[1] == 1, f"got {res!r}")
    res = _try(lambda: _cold_wake(tt.closed_type("store unreadable")))
    check("browser closed: a closed turn wakes WITHOUT the restore",
          not isinstance(res, Exception) and res[1] == 0, f"got {res!r}")

    # The skip is safe only because release cannot overwrite the kept copy
    # with a context that was never restored (browser_profile.save).
    sealed = []

    async def _on(pool): return True
    async def _kept(pool, entity_id): return True

    async def _clean_sidecar(method, path, json_body=None):
        return {"loaded": False,
                "storage_state": {"cookies": [{"name": "consent"}], "origins": []}}

    async def _store(pool, **kw):
        sealed.append(kw)

    from server.vault import api as vault_api
    _orig_store = vault_api.store_secret
    bp.is_enabled, bp._has_sealed, bp._sidecar = _on, _kept, _clean_sidecar
    vault_api.store_secret = _store
    try:
        saved = asyncio.run(bp.save(_Pool(), 1))
    except Exception as e:  # noqa: BLE001
        saved = e
    finally:
        vault_api.store_secret = _orig_store
    check("browser: release cannot overwrite the kept profile with a never-restored context",
          saved is False and sealed == [], f"saved={saved!r} sealed={sealed!r}")

    print()
    if FAILS:
        print(f"[turn-types-reach] {len(FAILS)} FAILED")
        return 1
    print("[turn-types-reach] all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
