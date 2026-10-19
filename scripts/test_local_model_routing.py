#!/usr/bin/env python3
"""
Local-lane model routing — the two failures that made a locally-served model
unreachable from the Orchestrator (2026-08-03).

1. WRONG PORT. list_local_models derived every endpoint as <ip>:11434, ollama's
   port, even for a model only an OpenAI-compatible server (llama.cpp
   llama-server, vLLM) serves on a different port. Discovery worked; the call
   opened the wrong socket.
2. MISSING PATH. provider_gateway._call_local POSTed the resolver's bare origin
   verbatim. Neither ollama nor llama.cpp serves chat completions at "/", so
   even the right socket 404s. This had been true for the ollama case all along.

No container, no DB — a stub pool and the pure URL logic. Repo-side ship gate:
scripts/smoke_test.sh runs this offline and fails the ship on a non-OK last line.

Usage: APP_ROOT=<repo> python3 scripts/test_local_model_routing.py
"""

import asyncio
import os
import sys
import types
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT / "brain"))

# list_local_models reads ONLINE_WINDOW_SECONDS from server.network.topology
# inside its try/except, so without the server package on the path it would
# degrade to [] and every assertion below would pass vacuously. Stub the one
# constant instead of importing the DB-bound server package.
_topology = types.ModuleType("server.network.topology")
_topology.ONLINE_WINDOW_SECONDS = 120
_server = sys.modules.setdefault("server", types.ModuleType("server"))
_network = sys.modules.setdefault("server.network", types.ModuleType("server.network"))
_server.network = _network
_network.topology = _topology
sys.modules["server.network.topology"] = _topology

PASSED = 0
FAILED = []


def check(name, got, want):
    global PASSED
    if got == want:
        PASSED += 1
    else:
        FAILED.append(f"{name}\n     got:  {got!r}\n     want: {want!r}")


class StubPool:
    """Minimal asyncpg-pool stand-in: one heartbeat row per bastion."""

    def __init__(self, rows):
        self._rows = rows

    async def fetch(self, *_args, **_kw):
        return self._rows


def test_endpoint_derivation():
    from mastercode.model_backends import (
        DEFAULT_LLM_PORT, DEFAULT_OLLAMA_PORT, resolve_model_endpoint,
    )

    check("ollama port default",
          resolve_model_endpoint({"reported_ip": "10.0.0.5"}),
          "http://10.0.0.5:11434")
    check("explicit port honored",
          resolve_model_endpoint({"reported_ip": "10.0.0.5"}, DEFAULT_LLM_PORT),
          "http://10.0.0.5:18080")
    # The mesh is IPv6 (2001:db8::a:N here) — a bare v6 literal MUST be bracketed or
    # the port parse eats the last hextet.
    check("ipv6 bracketed",
          resolve_model_endpoint({"reported_ip": "2001:db8::a:2"}, 18080),
          "http://[2001:db8::a:2]:18080")
    check("already-bracketed v6 not double-wrapped",
          resolve_model_endpoint({"reported_ip": "[2001:db8::a:2]"}, DEFAULT_OLLAMA_PORT),
          "http://[2001:db8::a:2]:11434")
    check("explicit model_endpoint wins untouched",
          resolve_model_endpoint(
              {"reported_ip": "10.0.0.5", "model_endpoint": "http://box.lan:9999/v1"}, 18080),
          "http://box.lan:9999/v1")
    check("no ip -> None", resolve_model_endpoint({"reported_ip": ""}), None)


def test_two_servers_two_ports():
    """THE REGRESSION. One box, ollama and llama.cpp side by side: each model
    must carry ITS OWN server's endpoint."""
    from mastercode.model_backends import list_local_models

    pool = StubPool([{
        "hostname": "ryzen",
        "reported_ip": "2001:db8::a:2",
        "models": ["nomic-embed-text", "qwen3:8b"],
        "llm_models": ["/models/qwen3.6-35b-a3b-q4km.gguf"],
        "llm_port": 18080,
    }])
    got = asyncio.run(list_local_models(pool, 1))
    by_id = {m["id"]: m for m in got}

    check("all three models surfaced", sorted(by_id), sorted([
        "nomic-embed-text", "qwen3:8b", "/models/qwen3.6-35b-a3b-q4km.gguf"]))
    check("ollama model -> ollama port",
          by_id["qwen3:8b"]["endpoint"], "http://[2001:db8::a:2]:11434")
    check("llama.cpp model -> its own port",
          by_id["/models/qwen3.6-35b-a3b-q4km.gguf"]["endpoint"],
          "http://[2001:db8::a:2]:18080")
    check("kind tags the serving lane",
          by_id["/models/qwen3.6-35b-a3b-q4km.gguf"]["kind"], "llm-openai")
    check("bastion identity carried", by_id["qwen3:8b"]["bastion"], "ryzen")


def test_llm_only_box_and_degradation():
    from mastercode.model_backends import list_local_models

    # A box before ollama exists on it: `models` empty, llm_models set.
    # The pre-fix code skipped the whole row here, which is why nothing appeared.
    pool = StubPool([{
        "hostname": "ryzen", "reported_ip": "10.0.0.9",
        "models": [], "llm_models": ["qwen3.6"], "llm_port": 18080,
    }])
    got = asyncio.run(list_local_models(pool, 1))
    check("llm-only box still surfaces its model", [m["id"] for m in got], ["qwen3.6"])
    check("llm-only endpoint", got[0]["endpoint"], "http://10.0.0.9:18080")

    # A bastion that predates the split reports no llm_port -> fall back, don't crash.
    pool = StubPool([{
        "hostname": "old", "reported_ip": "10.0.0.9",
        "models": [], "llm_models": ["m"], "llm_port": None,
    }])
    got = asyncio.run(list_local_models(pool, 1))
    check("null llm_port falls back to default", got[0]["endpoint"], "http://10.0.0.9:18080")

    # jsonb may arrive as a string; and a box serving nothing yields nothing.
    pool = StubPool([{
        "hostname": "s", "reported_ip": "10.0.0.9",
        "models": '["a"]', "llm_models": '[]', "llm_port": None,
    }])
    check("string jsonb parsed", [m["id"] for m in asyncio.run(list_local_models(pool, 1))], ["a"])

    pool = StubPool([{
        "hostname": "empty", "reported_ip": "10.0.0.9",
        "models": [], "llm_models": [], "llm_port": None,
    }])
    check("box serving nothing -> []", asyncio.run(list_local_models(pool, 1)), [])


def test_chat_route_normalization():
    """The REAL local_chat_url _call_local uses — imported, not re-implemented,
    so this cannot drift from the shipped behavior. A bare origin 404s on every
    known local server, which is why this normalization exists."""
    # provider_gateway pulls aiohttp and the wider brain package; the routing
    # helper is pure, so load just that function from source when the heavy
    # import isn't available in this bare environment.
    try:
        from mastercode.provider_gateway import local_chat_url as normalize
    except Exception:
        import ast, textwrap
        src = (ROOT / "brain" / "mastercode" / "provider_gateway.py").read_text()
        tree = ast.parse(src)
        fn = next(n for n in tree.body
                  if isinstance(n, ast.FunctionDef) and n.name == "local_chat_url")
        ns = {}
        exec(compile(ast.Module([fn], []), "<local_chat_url>", "exec"), ns)
        normalize = ns["local_chat_url"]

    check("empty stays empty", normalize(""), "")
    check("bare origin gets full route",
          normalize("http://[2001:db8::a:2]:18080"),
          "http://[2001:db8::a:2]:18080/v1/chat/completions")
    check("trailing slash not doubled",
          normalize("http://10.0.0.9:11434/"),
          "http://10.0.0.9:11434/v1/chat/completions")
    check("/v1 extended, not replaced",
          normalize("http://box.lan:8090/v1"),
          "http://box.lan:8090/v1/chat/completions")
    check("full route left alone (LOCAL_URL)",
          normalize("http://127.0.0.1:11434/v1/chat/completions"),
          "http://127.0.0.1:11434/v1/chat/completions")


def main():
    for fn in (test_endpoint_derivation, test_two_servers_two_ports,
               test_llm_only_box_and_degradation, test_chat_route_normalization):
        try:
            fn()
        except Exception as e:  # a broken import is a failure, not a crash
            FAILED.append(f"{fn.__name__} raised {type(e).__name__}: {e}")

    if FAILED:
        print(f"FAIL: {len(FAILED)} local-lane routing defect(s):")
        for f in FAILED:
            print("  " + f)
        return 1
    # Last line is the gate's verdict (smoke_test.sh reads `| tail -1`).
    print(f"OK — local lane resolves each model against its OWN server's port "
          f"(ollama vs OpenAI-compatible) and sends a real /v1/chat/completions "
          f"route, not a bare origin ({PASSED} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
