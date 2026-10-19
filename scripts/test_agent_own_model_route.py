"""A dispatch that names the host's OWN model runs on the host's provider.

2026-09-22 18:42:41: the host (routed kimi / k3) dispatched an agent
with model='k3'. A bare model id fell to the OpenRouter default in
run_scoped_agent, OpenRouter answered "k3 is not a valid model ID", and the
agent died before its first call — so every dispatch naming the host's own
model on a non-OpenRouter route failed.

  1. model == the host's chat model, no provider  -> the chat route's provider
  2. a different bare model                        -> still the OpenRouter default
  3. an explicit provider still wins

No pod, no DB, no model: the route resolver and call_model are scripted.
Run: APP_ROOT=<repo> python3 scripts/test_agent_own_model_route.py
"""

import asyncio
import os
import sys

FAILS = []


def check(name, ok, detail=""):
    print(f"[own-model-route] {'ok  ' if ok else 'FAIL'} {name}"
          + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


class _Pool:
    async def execute(self, *a, **k): return None
    async def fetch(self, *a, **k): return []
    async def fetchval(self, *a, **k): return None
    async def fetchrow(self, *a, **k): return None


async def _run(sa, **kw):
    seen = {}

    async def fake_call_model(**k):
        seen.setdefault("provider", k.get("provider"))
        seen.setdefault("model", k.get("provider_model") or k.get("model"))
        return {"content": "done", "tool_calls": None, "raw_message": {},
                "tokens_used": 0, "prompt_tokens": 0, "completion_tokens": 0}

    async def fake_route(pool, eid, role):
        return {"provider": "kimi", "model": "k3", "base_url": None,
                "fallback_provider": None, "fallback_model": None}

    async def fake_schemas(pool, eid, **k):
        return [{"type": "function", "function": {
            "name": "inbox_send", "description": "x",
            "parameters": {"type": "object", "properties": {}}}}]

    async def no_local(*a, **k):
        return None

    sa.call_model = fake_call_model
    sa.resolve_role_route = fake_route
    sa._build_tool_schemas = fake_schemas
    sa._META_TOOL_NAMES = {"inbox_send"}
    sa._BRIDGE_TOOL_NAMES = set()
    import mastercode.model_backends as mb
    mb.match_local_model = no_local
    await sa.run_scoped_agent(_Pool(), entity_id=9003, task_prompt="t",
                              tool_scope=["inbox_send"], grant=["write_memory"],
                              session_id=None, max_rounds=1, **kw)
    return seen


def main() -> int:
    root = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, root)
    sys.path.insert(0, os.path.join(root, "brain"))
    import mastercode.runtime.scoped_agent as sa

    got = asyncio.run(_run(sa, model="k3"))
    check("the host's own model, named bare, runs on the host's provider",
          got.get("provider") == "kimi" and got.get("model") == "k3", got)
    got = asyncio.run(_run(sa, model="K3 "))
    check("matched without regard to case or stray spaces", got.get("provider") == "kimi", got)
    got = asyncio.run(_run(sa, model="vendor/other-model"))
    check("a different bare model keeps the OpenRouter default",
          got.get("provider") == "openrouter", got)
    got = asyncio.run(_run(sa, model="k3", provider="openrouter"))
    check("an explicit provider still wins", got.get("provider") == "openrouter", got)

    if FAILS:
        print(f"[own-model-route] FAIL: {len(FAILS)} check(s)")
        return 1
    print("[own-model-route] PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
