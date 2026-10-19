#!/usr/bin/env python3
"""Condensed tool results, detail on request, and a used result leaving the turn
(NAC, bastions and harness are the three that need condensed
results, with detailed results scoped).

Measured on one turn (Grok): harness_connections
op='matrix' returned 165,985 characters and coding_bench 30,729; input went
from 28,497 tokens on call 1 to 108,437 on call 2, and both results rode along
in all five remaining calls, ~400k of the turn's 609,633 input tokens. Over 30
days the largest results were nac_status (up to 145k, since condensed),
harness_connections matrix (84k-166k), get_network (55-74k), manage_bastion
op=list (57k) and coding_bench (~30k).

Part 1, each through its real executor with its service stubbed, on a fleet
and catalogs shaped like one Host's:
  - harness_connections op='matrix' is one line per harness with every
    working-state field of the matrix row verbatim and a model count;
    harness=<id> (either spelling of claude) returns the whole row, models
    included; the Orchestrator route still returns the build untouched;
  - coding_bench's routing drops the per-engine model lists and the picker's
    tables, and keeps every role's route and each engine's efforts and count;
  - get_network is a roster line per bastion, the counts and the active
    agents; server=<hostname|token id> returns the bastion's whole card (the
    old per-bastion summary, unchanged) and every agent seen on it; a typed
    turn checks the machine that card belongs to;
  - manage_bastion op='list' is nac_status's roster; token_id is one record;
  - nac_status's default on the same fleet is already small;
  - the registry descriptions and the skills name the scoped arguments.
Part 2, through context_budget.drop_used_tool_results and through the real
EntityRuntime.send_message loop with a scripted model (the harness shape of
scripts/test_turn_cut_output.py):
  - a big read result is replaced only after the model has responded to it,
    pairing (role, tool_call_id) intact, by a stub carrying the history digest
    (tool_trail.digest_tool_action) and "call it again";
  - D >= S decides the cut, and every eligible result goes in the one cut;
  - a re-call runs the tool again and returns it whole;
  - context_ledger.dropped records {tool, chars, at_call, prefix_moved,
    recalled}, and per_call carries sys_sha / prefix_sha / sha: the cut call's
    prefix_sha differs from the call before's sha, the others match.

Run: APP_ROOT=<repo> PYTHONPATH=brain:. python3 scripts/test_condensed_tool_results.py
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import sys
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "brain"))
logging.disable(logging.CRITICAL)

failures: list = []
SIZES: list = []


def check(label, ok, detail=""):
    print(f"[condensed] {'ok  ' if ok else 'FAIL'} {label}"
          + (f" — {str(detail)[:400]}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def size(x) -> int:
    return len(json.dumps(x, default=str))


def run(coro):
    return asyncio.run(coro)


# ── Fixtures ────────────────────────────────────────────────────────────────
def _or_row(i: int) -> dict:
    """A row in _direct_models' OpenRouter shape."""
    return {"id": f"vendor{i % 40}/model-{i}-instruct", "name": f"Vendor {i % 40}: Model {i} Instruct",
            "created": 1757000000 + i, "context_length": 131072 + i, "type": "chat",
            "input_per_token": 0.0000003, "output_per_token": 0.0000012,
            "cache_read_per_token": 0.00000003, "price_source": "openrouter"}


def matrix_fixture() -> dict:
    """_build_coding_matrix's shape (server/routes/inference.py), a row per
    HARNESS_APP_IDS entry, with catalogs like one Host's: the generic engine
    on the whole OpenRouter catalog, cursor's per-account list."""
    def row(h, label, band, provider, key, models, *, src, efforts=(), default_model=None,
            action=None, models_error=None, options=()):
        return {"id": h, "label": label, "icon_url": f"/assets/harness/{h}.png", "band": band,
                "vendor": label.split()[0], "provider": provider, "provider_options": list(options),
                "wired": None, "key": key, "models": models, "models_error": models_error,
                "models_loading": False, "models_source": src, "efforts": list(efforts),
                "default_model": default_model, "default": h == "claude-code",
                "model_override": "claude-opus-5" if h == "claude-code" else None,
                "effort_override": "high" if h == "claude-code" else None, "action": action}
    ok_key = {"state": "connected", "name": "claude-oauth", "source": "login", "expires_at": 1799999999000}
    return {"harnesses": [
        row("claude-code", "Claude Code", "first_party", None, ok_key,
            [{"id": m} for m in ("claude-opus-5", "claude-opus-5-5", "claude-sonnet-5", "claude-fable-5",
                                 "claude-fable-5-1", "claude-haiku-5")],
            src="engine", efforts=("low", "medium", "high", "max"), default_model="claude-opus-5"),
        row("codex", "Codex", "first_party", None,
            {"state": "expired", "name": "codex-chatgpt", "source": "login", "expires_at": 1759000000000},
            [{"id": f"gpt-5.{i}", "label": f"GPT-5.{i}", "efforts": ["low", "medium", "high", "xhigh"],
              "default_effort": "medium", "vendor_default_effort": "medium"} for i in range(6)],
            src="cli_catalog", efforts=("low", "medium", "high", "xhigh", "max", "ultra"),
            default_model="gpt-6-astra", action={"label": "Sign in to Codex again", "kind": "login"}),
        row("cursor", "Cursor", "first_party", None,
            {"state": "connected", "name": "cursor-key", "source": "keys", "expires_at": None},
            [{"id": f"cursor-model-{i}-thinking-medium", "label": f"Cursor model {i} (thinking, medium)"}
             for i in range(200)], src="cli_catalog"),
        row("grok", "Grok", "first_party", None,
            {"state": "not_set_up", "name": None, "source": None, "expires_at": None},
            [{"id": f"grok-4.{i}"} for i in range(10)], src="vendor_catalog",
            action={"label": "Connect Grok", "kind": "login"}),
        row("opencode", "opencode", "generic", "openrouter",
            {"state": "connected", "name": "openrouter", "source": "keys", "expires_at": None},
            [_or_row(i) for i in range(300)], src="provider",
            options=("openrouter", "together", "groq")),
        row("kimi", "Kimi Code", "first_party", "moonshot",
            {"state": "connected", "name": "moonshot", "source": "keys", "expires_at": None},
            [{"id": f"kimi-k2.{i}", "name": f"Kimi K2.{i}", "context_length": 262144,
              "subscription": True} for i in range(14)],
            src="provider", efforts=("low", "medium", "high")),
        row("antigravity", "Antigravity CLI", "first_party", None,
            {"state": "connected", "name": "google-oauth", "source": "login", "expires_at": 1799999999000},
            [], src="cli_catalog", efforts=("low", "medium", "high"),
            models_error="No models available for this account."),
    ], "default_engine": "claude-code", "generic_providers_held": ["groq", "openrouter", "together"]}


def routing_fixture() -> dict:
    """inference.get_current_routing's shape."""
    roles = ["chat", "review", "memory"]

    def route(p, m):
        return {"provider": p, "model": m, "model_id": m, "lane": p, "base_url": None,
                "connection_id": None, "fallback_provider": None, "fallback_model": None,
                "effort": "high", "reasoning_inline": False}
    return {
        "routing": {"chat": {"provider": "xai", "model": "grok-4.7"}},
        "roles": roles,
        "role_descriptions": {r: f"The {r} role: " + "what it does and when it fires. " * 6 for r in roles},
        "harness_options": [
            {"id": h, "label": h.title(), "icon_url": f"/assets/harness/{h}.png",
             "models": ms, "efforts": ["low", "medium", "high"]}
            for h, ms in (("claude-code", ["claude-opus-5"] * 6),
                          ("opencode", [r["id"] for r in (_or_row(i) for i in range(400))]),
                          ("grok", [f"grok-4.{i}" for i in range(12)]),
                          ("kimi", [f"kimi-k2.{i}" for i in range(14)]))],
        "coding_harnesses": [{"id": "claude-code", "model": "claude-opus-5", "effort": "high"}],
        "default_coding_harness": "claude-code",
        "effective": {"chat": {**route("xai", "grok-4.7"), "source": "explicit"},
                      "review": {**route("anthropic", "claude-opus-5"), "source": "derived"},
                      "memory": {"provider": None, "model": None, "source": "unconfigured",
                                 "unconfigured": "pick a provider and model in the Orchestrator"}},
        "providers": ([{"id": f"prov{i}", "has_key": i % 3 == 0} for i in range(26)]
                      + [{"id": "xai", "has_key": True, "via": "signin", "via_label": "xAI sign-in",
                          "last_served": "xAI sign-in"},
                         {"id": "local", "has_key": True, "embeddings": True}]),
        "favorites_by_provider": {"openrouter": [f"vendor{i}/model-{i}-instruct" for i in range(8)]},
        "role_efforts": {p: ["low", "medium", "high", "max"] for p in ("anthropic", "xai", "openai", "moonshot")},
    }


def bastion(i: int) -> dict:
    """A bastion row in topology._bastion_row's shape (as nac_commit.bastions_list
    decorates it), with check-in fields like a real box's."""
    return {
        "id": 100 + i, "name": f"box-{i}", "url_handle": f"h{i}", "scopes": ["mgmt", "mcp"],
        "hardware_fingerprint": "f" * 64, "machine_anchor_id": i,
        "created_at": "2026-09-01T00:00:00+00:00", "last_used_at": "2026-10-06T00:00:00+00:00",
        "hostname": f"fleet-box-{i}", "bastion_version": "0.44.2",
        "platform": "darwin" if i % 9 == 4 else "linux", "tunnel_status": "up",
        "reported_ip": f"10.0.0.{i}", "wg_pubkey": "k" * 44,
        "capabilities": ["embed", "mgmt", "observe", "agent-observe", "computer"],
        "models": ["nomic-embed-text:latest", "mxbai-embed-large:latest", "llama3.1:8b"] if i % 5 == 0 else [],
        "last_seen_at": "2026-10-06T01:00:00+00:00", "online": i != 7,
        "enforcing": {"mgmt_ops": ["uptime", "ctx_put", "ctx_del", "ctx_get", "backup_fetch",
                                   "computer:inventory", "computer:windows", "computer:tree",
                                   "observe:alerts", "observe:metrics", "observe:logs"],
                      "grants": [{"signature": "s" * 88, "store_id": n} for n in range(4)],
                      "tunnel_allowed_ips": ["10.0.0.1/32", "fd00::1/128"], "binary_sha256": "b" * 64,
                      "anchored": True, "is_anchor": True, "pairing_state": "active",
                      "pairing_caps": ["observe", "mgmt", "models"]},
        "nac_posture": "allow", "nac_decision": "allow",
        "detect": {"baseline": "ok", "critical": i % 3, "high": 0, "warn": 1, "notice": 2,
                   "coverage": "all-processes", "note": "processes and standing jobs compared",
                   "at": "2026-10-06T00:55:00Z", "short_lived": 4, "filtered": 6},
        "alerts": None,
        "schedulers": [{"kind": "crontab", "name": f"job-{n}", "command": "/usr/local/bin/agent-run --nightly",
                        "schedule": "0 3 * * *", "user": "ops", "signature": "claude-code"} for n in range(3)],
        "scheduler_scan": {"kinds": ["crontab", "systemd-timer"], "entries": 14},
        "mode": "anchored",
        "anchor": {"owner": "alex", "anchor_host": "nova", "anchor_handle": "nova.example",
                   "claimed_at": "2026-09-08T00:00:00Z", "verified": True},
        "pairings": [{"host": f"peer-{n}", "state": "pending" if (i == 2 and n == 0) else "active",
                      "caps": ["observe", "models"], "since": "2026-09-10T00:00:00Z"} for n in range(2)],
        "unapproved_pairing_attempts": [], "other_bastions": [], "other_bastions_scanned_at": None,
        "pairing_grants": [{"subject": "peer-1", "caps": ["models"], "state": "active",
                            "expires_at": "2026-11-01T00:00:00Z"}],
        "posture": {"firewall": "on", "updates": "current", "disk_free_pct": 41},
        "update_to_version": "0.45.0" if i == 3 else None, "update_approved": i == 3,
        "contain_pending": [{"pid": 4242, "signature": "x", "requested_at": "t"}] if i == 5 else [],
        "contain_findings_pending": [],
    }


FLEET = [bastion(i) for i in range(25)]


def observation(n: int) -> dict:
    """A row in nac_agents.list_observations' shape."""
    return {"token_id": 100 + n % 25, "hostname": f"fleet-box-{n % 25}", "pid": 1000 + n, "ppid": 1,
            "agent_user": "alex", "signature": "claude-code" if n % 2 else "codex",
            "kind": "agent" if n % 4 else "app", "family": f"fam-{n}", "origin": "interactive",
            "cmdline": "/usr/local/bin/node /home/alex/.local/bin/claude --dangerously-skip " + "x" * 120,
            "llm_endpoints": ["api.anthropic.com:443", "local:11434 (ollama)"],
            "evidence": [{"signal": "process", "tier": "verified", "value": "claude"},
                         {"signal": "endpoint", "tier": "known", "value": "api.anthropic.com"}],
            "active": n < 14, "requested": n % 3 == 0, "acknowledged": False,
            "first_seen": "2026-10-01T00:00:00Z", "last_seen": "2026-10-06T01:00:00Z"}


OBSERVED = [observation(n) for n in range(60)]
POLICY = {"profiles": [{"id": 5, "name": "home", "enforcement": "allow", "is_default": False,
                        "permits": [{"lane": "mcp"}]}],
          "policy": [{"id": n, "name": f"rule-{n}", "priority": n, "match": {"server": f"fleet-box-{n}"},
                      "profile_id": 5, "proposed_by": "host", "approved_at": "2026-09-20" if n % 2 else None,
                      "approved_by": "alex", "stepup_at": None, "revoke_needs_code": bool(n % 2),
                      "revoke_code_reason": "keeps_out" if n % 2 else None,
                      "integrity": "ok" if n % 2 else None, "enforcement": "allow",
                      "permits": [{"lane": "mcp"}], "status": "approved" if n % 2 else "draft"}
                     for n in range(8)],
          "stepup_enrolled": True, "engine": {"running": True, "tamper": False, "armed": True}}
EGRESS = {"rules": [{"id": n, "pattern": f"*.vendor{n}.com", "action": "allow", "priority": n, "note": "",
                     "approved_at": "2026-09-20" if n % 2 else None, "subject": "*", "exit": "default",
                     "terms_differ": False, "status": "approved" if n % 2 else "draft"} for n in range(10)],
          "default": "open", "subjects": ["harness:browser"], "stepup_enrolled": True}
GRANTS = {"grants": [{"id": n, "server": f"fleet-box-{n}", "ops": ["uptime", "observe:alerts"], "deny": [],
                      "intent": {"why": "watch it"}, "approved_at": "2026-09-20" if n % 2 else None,
                      "expires_at": "2026-11-01", "status": "approved" if n % 2 else "draft",
                      "widening": True, "expired": False, "revoke_needs_code": False} for n in range(12)],
          "default": "deny", "stepup_enrolled": True}


# ── Part 1a: harness_connections op='matrix' ───────────────────────────────
def part_harness():
    from server import brain_service
    from server.routes import inference as INF
    from mastercode.runtime import tools as T
    m = matrix_fixture()
    saved = (INF._build_coding_matrix, brain_service.get_pool)

    async def _build(*, kick_catalogs=False):
        return copy.deepcopy(m)
    INF._build_coding_matrix = _build
    brain_service.get_pool = lambda: object()
    try:
        r = run(T._exec_harness_connections({"op": "matrix"}))
        res = r.get("result") or {}
        rows = res.get("harnesses") or []
        before, after = size({"status": "ok", "result": m}), size(r)
        SIZES.append(("harness_connections op='matrix'", before, after, "harness"))
        check("matrix default: one line per harness, every harness, in the build's order",
              r.get("status") == "ok" and [x.get("id") for x in rows] == [x["id"] for x in m["harnesses"]],
              repr(r)[:300])
        dropped = {"models", "icon_url", "band", "models_source"}
        check("matrix default: no row carries its model list or the screen's presentation fields",
              not any(dropped & set(x) for x in rows), [sorted(dropped & set(x)) for x in rows])
        working = ("label", "key", "provider", "wired", "default_model", "default", "model_override",
                   "effort_override", "efforts", "models_error", "models_loading", "action",
                   "provider_options", "vendor")
        check("matrix default: every working-state field of each row, verbatim (key state/name/source/"
              "expiry, provider, default model, overrides, efforts, models_error, action)",
              len(rows) == 7 and all(set(working) <= set(x) and all(x.get(k) == full.get(k) for k in working)
                                     for x, full in zip(rows, m["harnesses"])),
              [(x.get("id"), [k for k in working if x.get(k) != full.get(k)])
               for x, full in zip(rows, m["harnesses"])])
        check("matrix default: model_count is each row's model total",
              [x.get("model_count") for x in rows] == [len(x["models"]) for x in m["harnesses"]],
              [x.get("model_count") for x in rows])
        check("matrix default: the broken rows say why in the matrix's own words "
              "(codex expired + its action, antigravity's models_error)",
              len(rows) == 7 and rows[1]["key"]["state"] == "expired" and rows[1]["action"]["kind"] == "login"
              and rows[6]["models_error"] == "No models available for this account.")
        check("matrix default: the top-level fields ride as built, and the result names the scoped call",
              res.get("default_engine") == "claude-code"
              and res.get("generic_providers_held") == ["groq", "openrouter", "together"]
              and "harness=" in str(res.get("full_row")), repr(res.get("full_row")))
        check(f"matrix default: {before:,} -> {after:,} chars (under a tenth)", after * 10 < before,
              f"{before} -> {after}")
        r = run(T._exec_harness_connections({"op": "matrix", "harness": "cursor"}))
        check("harness='cursor': that harness's whole matrix row, its 200 models included",
              (r.get("result") or {}).get("harness") == m["harnesses"][2], repr(r)[:300])
        r = run(T._exec_harness_connections({"op": "matrix", "harness": "claude-sdk"}))
        check("harness='claude-sdk' (op='list' spelling): the claude-code row",
              ((r.get("result") or {}).get("harness") or {}).get("id") == "claude-code", repr(r)[:300])
        r = run(T._exec_harness_connections({"op": "matrix", "harness": "nope"}))
        check("harness=<unknown>: refused as the model's own error, naming the harness ids",
              r.get("status") == "error" and r.get("input_refused") is True
              and "cursor" in str(r.get("result")), repr(r)[:300])
        screen = run(INF.get_coding_matrix())
        check("the Orchestrator's GET /coding-matrix still returns the build untouched (byte-identical)",
              json.dumps(screen, sort_keys=True) == json.dumps(m, sort_keys=True))
    finally:
        INF._build_coding_matrix, brain_service.get_pool = saved


# ── Part 1b: coding_bench ───────────────────────────────────────────────────
def part_bench():
    from server.routes import inference as INF
    from server.routes import review as RV
    from server.routes import coding_protocols as CP
    from mastercode.runtime import tools as T
    rt = routing_fixture()

    class _Pool:
        async def fetch(self, q, *a):
            return [{"id": 900 - n, "harness": "claude-code" if n % 2 else "codex",
                     "model": f"model-{n % 6}", "ok": n % 5 != 0, "error": "exit 1" if n % 5 == 0 else None,
                     "exit_code": 1 if n % 5 == 0 else 0, "elapsed_seconds": 120.0 + n,
                     "created_at": "2026-10-05T00:00:00Z"} for n in range(60)]

    async def _installed():
        return ["claude-code", "codex", "opencode", "kimi"]

    async def _settings():
        return {"claude-code": {"model": "claude-opus-5", "effort": "high"}}

    async def _default():
        return "claude-code"

    async def _protocols():
        return {"protocols": [{"slug": f"p{n}", "name": f"Protocol {n}", "author": "alex",
                               "shape": "ladder", "stats": {"runs": n}} for n in range(3)]}

    async def _routing():
        return copy.deepcopy(rt)
    saved = (INF.get_current_routing, RV._installed_harnesses, RV._coding_harness_settings,
             RV._default_coding_harness, CP.list_protocols)
    INF.get_current_routing, CP.list_protocols = _routing, _protocols
    RV._installed_harnesses, RV._coding_harness_settings, RV._default_coding_harness = (
        _installed, _settings, _default)
    try:
        out = run(T._exec_coding_bench({}, 3, _Pool()))
        before, after = size({**out, "routing": rt}), size(out)
        SIZES.append(("coding_bench", before, after, "one engine: harness_connections harness="))
        ro = out.get("routing") or {}
        check("coding_bench: engines, default, protocols and track record ride as before",
              (out.get("harnesses") or {}).get("default") == "claude-code" and len(out.get("protocols") or []) == 3
              and isinstance(out.get("track_record"), list) and out["track_record"], repr(out)[:300])
        check("coding_bench routing: each engine's label, efforts and model count, no model list",
              [(h.get("id"), h.get("model_count")) for h in ro.get("harness_options") or []]
              == [("claude-code", 6), ("opencode", 400), ("grok", 12), ("kimi", 14)]
              and not any("models" in h or "icon_url" in h for h in ro.get("harness_options") or []),
              repr(ro.get("harness_options"))[:300])
        check("coding_bench routing: every role's effective route, without the resolver's aliases",
              (ro.get("effective") or {}).get("chat") == {"provider": "xai", "model": "grok-4.7",
                                                          "effort": "high", "source": "explicit"}
              and ro["effective"]["memory"]["source"] == "unconfigured"
              and "unconfigured" in ro["effective"]["memory"], repr(ro.get("effective")))
        check("coding_bench routing: the held providers (and how xai is held), the saved engines, "
              "the default tick",
              ro.get("providers_held") == [f"prov{i}" for i in range(0, 26, 3)] + ["xai", "local"]
              and ro.get("provider_via") == {"xai": "xAI sign-in"}
              and ro.get("coding_harnesses") == rt["coding_harnesses"]
              and ro.get("default_coding_harness") == "claude-code", repr(ro)[:400])
        check("coding_bench routing: says where one engine's models are",
              "harness_connections(op='matrix', harness=" in str(ro.get("models")))
        check(f"coding_bench: {before:,} -> {after:,} chars (under a third)", after * 3 < before,
              f"{before} -> {after}")
    finally:
        (INF.get_current_routing, RV._installed_harnesses, RV._coding_harness_settings,
         RV._default_coding_harness, CP.list_protocols) = saved


# ── Part 1c: get_network ────────────────────────────────────────────────────
def part_network():
    from server import brain_service
    from server.network import nac_commit, nac_agents, topology, machine_offers
    from server.routes import network as NET
    from mastercode.runtime import tools as T

    async def _bl(pool, eid):
        return {"bastions": copy.deepcopy(FLEET)}

    async def _peers(pool, eid):
        return {"contacts": [{"id": n, "handle": f"peer-{n}"} for n in range(3)]}

    async def _pol():
        return copy.deepcopy(POLICY)

    async def _egr():
        return copy.deepcopy(EGRESS)

    async def _gr():
        return copy.deepcopy(GRANTS)

    async def _obs(pool, eid, window_seconds=300):
        return copy.deepcopy(OBSERVED)

    async def _offers(pool, **kw):
        return [{"id": 1, "direction": "in", "from": "peer-0", "models": ["llama3.1:8b"]}]
    saved = (nac_commit.bastions_list, topology.list_peers, NET.nac_policy_list, NET.nac_egress_list,
             NET.nac_mgmt_grants_list, nac_agents.list_observations, machine_offers.list_offers,
             brain_service.get_pool)
    (nac_commit.bastions_list, topology.list_peers, NET.nac_policy_list, NET.nac_egress_list,
     NET.nac_mgmt_grants_list, nac_agents.list_observations, machine_offers.list_offers) = (
        _bl, _peers, _pol, _egr, _gr, _obs, _offers)
    brain_service.get_pool = lambda: object()
    try:
        r = run(T._exec_get_network({}))
        res = r.get("result") or {}
        roster = res.get("bastions") or []
        full_cards = [T._bastion_summary(b) for b in FLEET]
        agents_full = [T._agent_summary(a) for a in OBSERVED]
        # The read as it was before condensing: every card and every list whole.
        before = size({"status": "ok", "result": {
            **{k: v for k, v in res.items() if k not in ("bastions", "detail")},
            "bastions": full_cards,
            "nac_policy": {**(res.get("nac_policy") or {}), "rules": POLICY["policy"]},
            "egress": {**(res.get("egress") or {}), "rules": EGRESS["rules"]},
            "mgmt_grants": {**(res.get("mgmt_grants") or {}), "grants": GRANTS["grants"]},
            "agents": {**{k: v for k, v in (res.get("agents") or {}).items() if k != "active_list"},
                       "observed": T._cap_agents(agents_full)}}})
        after = size(r)
        SIZES.append(("get_network", before, after, "server"))
        check("get_network default: a line per bastion, each with its token_id and hostname",
              r.get("status") == "ok" and [b.get("token_id") for b in roster] == [100 + i for i in range(25)]
              and all(b.get("hostname") == f"fleet-box-{i}" for i, b in enumerate(roster)), repr(roster[:1]))
        check("get_network default: each line says whether the machine is healthy (online, version, "
              "agent_watch, detect_state, alerts_state, tunnel, platform)",
              len(roster) == 25
              and all({"online", "bastion_version", "agent_watch", "detect_state", "alerts_state",
                       "tunnel_status", "platform", "last_seen_at"} <= set(b) for b in roster)
              and roster[7]["online"] is False and roster[4]["agent_watch"] == full_cards[4]["agent_watch"]
              and [b["detect_state"] for b in roster] == [c["detect_state"] for c in full_cards],
              repr(roster[4:5]))
        heavy = {"schedulers", "enforcing", "pairings", "capabilities", "embed_models", "detect_reason",
                 "alerts_reason", "agent_watch_reason", "anchor", "pairing_grants", "detect"}
        check("get_network default: no line carries its card (reasons, schedulers, enforcing, pairings)",
              roster and not any(heavy & set(b) for b in roster), [sorted(heavy & set(b)) for b in roster][:2])
        check("get_network default: a waiting pairing, update and containment still show on the line",
              len(roster) == 25 and roster[2].get("pending_pairings") == 1
              and roster[3].get("update_to_version") == "0.45.0"
              and roster[5].get("contain_pending") == 1 and "pending_pairings" not in roster[0])
        check("get_network default: rule and grant COUNTS with the defaults and the NAC engine; the "
              "rows themselves are nac_status's",
              res.get("nac_policy") == {"approved": 4, "draft": 4, "engine": POLICY["engine"]}
              and res.get("egress") == {"default": "open", "approved": 5, "draft": 5}
              and res.get("mgmt_grants") == {"default": "deny", "approved": 6, "draft": 6}
              and "nac_status" in str(res.get("detail")),
              repr({k: res.get(k) for k in ("nac_policy", "egress")})[:300])
        ag = res.get("agents") or {}
        check("get_network default: the agent counts as before, and one line per ACTIVE agent with the "
              "pid mgmt_run op='contain' takes",
              ag.get("active") == 14 and ag.get("total_observed") == 60 and ag.get("truncated") is True
              and len(ag.get("active_list") or []) == 14 and "observed" not in ag
              and all(set(x) == {"server", "pid", "signature", "kind", "family", "state", "origin"}
                      for x in ag.get("active_list") or []), repr(ag)[:300])
        check("get_network default: peers, machine offers, embed-capable boxes, step-up ride as before",
              res.get("peers") == 3 and res.get("machine_offers") and res.get("stepup_enrolled") is True
              and res.get("embed_capable_bastions") == [c["hostname"] for c in full_cards if c["embed_ready"]])
        check(f"get_network default on a 25-bastion fleet: {before:,} -> {after:,} chars (under a fifth)",
              after * 5 < before, f"{before} -> {after}")
        r = run(T._exec_get_network({"server": "fleet-box-12"}))
        card = (r.get("result") or {}).get("bastion") or {}
        check("get_network(server=<hostname>): the bastion's whole card, the per-bastion summary "
              "get_network returned before, plus its token_id",
              card == {"token_id": 112, **full_cards[12]}, repr(card)[:300])
        on_box = [a for a in agents_full if a["server"] == "fleet-box-12"]
        check("get_network(server=...): every agent seen on that machine, active or idle, in full "
              "(active first, as the old list)",
              (r.get("result") or {}).get("agents") == T._cap_agents(on_box)
              and r["result"].get("agents_total") == 2
              and [a["active"] for a in r["result"]["agents"]] == [True, False]
              and all("cmdline" in a and "evidence" in a for a in r["result"]["agents"]),
              repr((r.get("result") or {}).get("agents"))[:300])
        r = run(T._exec_get_network({"server": "105"}))
        check("get_network(server=<token id>): that machine",
              ((r.get("result") or {}).get("bastion") or {}).get("hostname") == "fleet-box-5", repr(r)[:200])
        r = run(T._exec_get_network({"server": "nope"}))
        check("get_network(server=<unknown>): refused as the model's error, naming the hostnames",
              r.get("status") == "error" and r.get("input_refused") is True
              and "fleet-box-0" in str(r.get("result")), repr(r)[:200])

        # The typed-turn wall: get_network's server is a checked door, and the
        # machine whose card would come back is the one checked.
        from mastercode.runtime import turn_types as TT
        typed = {"id": "00000000-0000-0000-0000-000000000001", "name": "Ops", "status": "active",
                 "keywords": [], "context": {}, "skill": None,
                 "tool_groups": [TT.tool_category("get_network")], "grant_names": [], "keys": [],
                 "bastions": ["fleet-box-1", "112"], "model": None, "thread_id": None, "version": 1}
        saved_lb = topology.list_bastions

        async def _lb(pool, eid):
            return copy.deepcopy(FLEET)
        topology.list_bastions = _lb
        try:
            def gate(args):
                t = run(TT.resolve_targets(object(), 1, "get_network", args))
                return TT.check_call(typed, "get_network", args, targets=t)
            inside, outside = gate({"server": "fleet-box-1"}), gate({"server": "fleet-box-2"})
            by_id, roster_ok = gate({"server": "112"}), gate({})
        finally:
            topology.list_bastions = saved_lb
        check("typed turn: get_network(server=) is a checked door (TYPE_SERVER_ARGS)",
              TT.TYPE_SERVER_ARGS.get("get_network") == "server")
        check("typed turn: a listed machine's card passes, an unlisted one is refused, the roster passes",
              inside is None and isinstance(outside, dict) and outside.get("item") == "fleet-box-2"
              and roster_ok is None, repr((inside, outside, roster_ok)))
        check("typed turn: a listed string that is a token id is checked as the hostname behind it",
              isinstance(by_id, dict) and by_id.get("item") == "fleet-box-12", repr(by_id))
    finally:
        (nac_commit.bastions_list, topology.list_peers, NET.nac_policy_list, NET.nac_egress_list,
         NET.nac_mgmt_grants_list, nac_agents.list_observations, machine_offers.list_offers,
         brain_service.get_pool) = saved


# ── Part 1d: manage_bastion op='list', and nac_status on the same fleet ────
def part_bastion_list():
    from server import brain_service
    from server.network import nac_commit
    from mastercode.runtime import tools as T

    async def _mb(pool, eid, op, **kw):
        if op in ("list", "status"):
            return {"bastions": copy.deepcopy(FLEET)}
        return {"version": "0.45.0", "auto_update": {"on": True}}

    async def _snap(pool, eid):
        return {"policy": copy.deepcopy(POLICY), "egress": copy.deepcopy(EGRESS),
                "mgmt_grants": copy.deepcopy(GRANTS), "dcn_stores": {"stores": []},
                "suspended": {"suspended": []}, "quarantined": {"quarantined": []},
                "bastions": {"bastions": copy.deepcopy(FLEET)}, "connectors": {"connectors": []}}
    saved = (nac_commit.manage_bastion, nac_commit.status_snapshot, brain_service.get_pool)
    nac_commit.manage_bastion, nac_commit.status_snapshot = _mb, _snap
    brain_service.get_pool = lambda: object()
    try:
        r = run(T._exec_manage_bastion({"op": "list"}))
        before, after = size({"status": "ok", "result": {"bastions": FLEET}}), size(r)
        SIZES.append(("manage_bastion op='list'", before, after, "token_id"))
        rows = (r.get("result") or {}).get("bastions") or []
        check("manage_bastion op='list': nac_status's roster line per machine",
              rows == [T._nac_bastion_row(b) for b in FLEET] and (r.get("result") or {}).get("online") == 24,
              repr(rows[:1]))
        check(f"manage_bastion op='list': {before:,} -> {after:,} chars (under a fifth)",
              after * 5 < before, f"{before} -> {after}")
        r = run(T._exec_manage_bastion({"op": "list", "token_id": 109}))
        check("manage_bastion op='list', token_id=<id>: that machine's whole record",
              (r.get("result") or {}).get("bastion") == FLEET[9], repr(r)[:200])
        r = run(T._exec_manage_bastion({"op": "list", "token_id": 999}))
        check("manage_bastion op='list', token_id=<unknown>: refused as the model's error",
              r.get("status") == "error" and r.get("input_refused") is True and "109" in str(r.get("result")),
              repr(r)[:200])
        r = run(T._exec_manage_bastion({"op": "latest"}))
        check("manage_bastion op='latest': the service's answer as it came",
              r == {"status": "ok", "result": {"version": "0.45.0", "auto_update": {"on": True}}}, repr(r))
        r = run(T._exec_nac_status({}))
        snap = run(_snap(None, 3))
        n_before, n_after = size({"status": "ok", "result": snap}), size(r)
        SIZES.append(("nac_status (condensed earlier, unchanged)", n_before, n_after, "server"))
        check(f"nac_status default on the same 25-bastion fleet stays small ({n_before:,} -> {n_after:,}; "
              f"the rest is the NAC rows with their ids)", n_after * 3 < n_before and n_after < 25_000,
              f"{n_before} -> {n_after}")
    finally:
        nac_commit.manage_bastion, nac_commit.status_snapshot, brain_service.get_pool = saved


# ── Part 1e: what the Host is told ──────────────────────────────────────────
def part_text():
    from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS as P
    hc = P["harness_connections"]["function"]
    gn = P["get_network"]["function"]
    mb = P["manage_bastion"]["function"]
    check("registry: harness_connections takes `harness` and says matrix gives one whole row with it",
          "harness" in hc["parameters"]["properties"] and "harness=" in hc["description"])
    check("registry: get_network takes `server` and says it is one bastion's whole card",
          "server" in gn["parameters"]["properties"] and "whole card" in gn["description"])
    check("registry: manage_bastion op='list' says token_id gives one whole record",
          "token_id one whole record" in mb["description"]
          and "list" in mb["parameters"]["properties"]["token_id"]["description"])
    skills = _ROOT / "default-skills"
    net = " ".join((skills / "network.md").read_text().split())
    cp = " ".join((skills / "coding-protocols.md").read_text().split())
    check("skill network: the roster, the card and where the rules are",
          "get_network(server=<hostname or token id>)" in net and "`active_list`" in net
          and "are `nac_status`'s" in net and "with `token_id` it returns that one machine's whole record" in net)
    check("skill coding-protocols: the matrix line and harness=<id> for the models",
          "harness_connections(op='matrix', harness='<id>')" in cp and "`model_count`" in cp)


# ── Part 2a: context_budget.drop_used_tool_results ─────────────────────────
def part_drop_unit():
    from mastercode import context_budget as cb
    if not hasattr(cb, "drop_used_tool_results"):
        check("context_budget.drop_used_tool_results exists", False, "missing")
        return

    def tc(i, name):
        return {"role": "assistant", "content": None,
                "tool_calls": [{"id": f"c{i}", "type": "function",
                                "function": {"name": name, "arguments": "{}"}}]}

    def res(i, n):
        return {"role": "tool", "tool_call_id": f"c{i}", "name": f"t{i}", "content": json.dumps({"x": "y" * n})}
    sysm, user = {"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}
    drop = {f"c{i}": {"tool": f"t{i}", "fp": f"fp{i}", "trail": {"op": "matrix", "digest": {"state": "s"}}}
            for i in range(9)}
    big, small = 40_000, 3_000
    msgs = [sysm, user, tc(1, "t1"), res(1, big)]
    out, gone = cb.drop_used_tool_results(msgs, len(msgs), drop)
    check("drop: a result the model has not answered after stays whole", out is msgs and not gone)
    msgs = [sysm, user, tc(1, "t1"), res(1, big), tc(2, "t2"), res(2, small)]
    out, gone = cb.drop_used_tool_results(msgs, 4, drop)   # the previous call sent through res(1)
    stub = out[3]["content"] if out is not msgs else ""
    check("drop: once answered after, a big read becomes a stub; role, tool_call_id and name stay",
          [g["tool_call_id"] for g in gone] == ["c1"] and out[3]["role"] == "tool"
          and out[3]["tool_call_id"] == "c1" and out[3]["name"] == "t1" and cb.is_dropped_stub(stub), gone)
    body = json.loads(stub)[cb.DROPPED_RESULT_KEY] if stub else {}
    check("drop: the stub names the tool, the size, the history digest, and to call it again",
          body.get("tool") == "t1" and body.get("chars") == len(msgs[3]["content"])
          and body.get("op") == "matrix" and body.get("digest") == {"state": "s"}
          and "Call t1 again with the same arguments" in body.get("note", ""), body)
    check("drop: a new dict, the input list and message untouched (identity readers see it gone)",
          out[3] is not msgs[3] and not cb.is_dropped_stub(msgs[3]["content"])
          and out[5] is msgs[5] and out[2] is msgs[2])
    out2, gone2 = cb.drop_used_tool_results(out, len(out), drop)
    check("drop: a stub is never dropped again", out2 is out and not gone2)
    msgs = [sysm, user, tc(1, "t1"), res(1, big), tc(2, "t2"), res(2, small), tc(3, "t3")]
    out, gone = cb.drop_used_tool_results(msgs, 6, drop)
    check("drop: under the floor (16,000 chars) stays",
          [g["tool_call_id"] for g in gone] == ["c1"] and out[5] is msgs[5])
    out, gone = cb.drop_used_tool_results(msgs, 6, drop, protect=[msgs[3]])
    check("drop: a protected message stays", not gone and out is msgs)
    out, gone = cb.drop_used_tool_results(msgs, 6, {k: v for k, v in drop.items() if k != "c1"})
    check("drop: a result nobody registered (not a read) stays", not gone and out is msgs)
    # D >= S: the cut moves the cached prefix from the earliest eligible
    # result on; it is made only when what it removes covers what it re-sends.
    filler = {"role": "tool", "tool_call_id": "c2", "content": "z" * 15_000}
    three = {"role": "assistant", "content": None, "tool_calls": [
        {"id": f"c{n}", "type": "function", "function": {"name": f"t{n}", "arguments": "{}"}} for n in (1, 2, 3)]}
    msgs = [sysm, user, three, res(1, 20_000), filler, {**filler, "tool_call_id": "c3"}, tc(4, "t4")]
    out, gone = cb.drop_used_tool_results(msgs, 6, drop)
    check("drop: D < S (20k removed, 30k sent behind it re-sent) — no cut", not gone and out is msgs, gone)
    out, gone = cb.drop_used_tool_results(msgs, 4, drop)
    check("drop: the same result with nothing sent behind it — cut (D >= S)",
          [g["tool_call_id"] for g in gone] == ["c1"], gone)
    msgs = [sysm, user, tc(1, "t1"), res(1, 30_000), tc(2, "t2"), res(2, 25_000), tc(3, "t3"),
            res(3, 2_000)]
    out, gone = cb.drop_used_tool_results(msgs, 7, drop)
    check("drop: every eligible result goes in ONE cut", [g["tool_call_id"] for g in gone] == ["c1", "c2"], gone)
    # A provider that reuses call ids across rounds: the registration names
    # its own message, and only that one is taken.
    again = [sysm, user, tc(1, "t1"), res(1, 30_000), tc(1, "t1"), res(1, 30_000), tc(2, "t2")]
    reg = {"c1": {"tool": "t1", "fp": "fp1", "msg": again[5], "trail": {}}}
    out, gone = cb.drop_used_tool_results(again, 6, reg)
    check("drop: a reused call id matches only the message its registration names",
          [g["index"] for g in gone] == [5] and out[3] is again[3], gone)
    h0 = cb.messages_hashes(msgs, 0)
    h1 = cb.messages_hashes(msgs + [{"role": "user", "content": "more"}], len(msgs))
    h2 = cb.messages_hashes(out + [{"role": "user", "content": "more"}], len(msgs))
    check("hashes: the next call's prefix_sha equals this call's sha when nothing before moved, "
          "and differs after a cut; sys_sha is messages[0]'s",
          h0["prefix_sha"] is None and h1["prefix_sha"] == h0["sha"] and h2["prefix_sha"] != h0["sha"]
          and h1["sys_sha"] == h0["sys_sha"] and len(h0["sha"]) == 12, (h0, h1, h2))


# ── Part 2b: through the real turn loop ─────────────────────────────────────
def part_drop_turn():
    from mastercode.runtime import session as S
    from mastercode import provider_gateway as PG
    from mastercode import inference_config as IC
    import mastercode.memory_engine as ME
    from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS as P
    from mastercode.runtime.tool_trail import digest_tool_action
    from mastercode import context_budget as cb
    from server.routes import inference as INF
    INF._CATALOG_CACHE["data"] = {"openrouter_models": [], "total_openrouter": 0}
    INF._CATALOG_CACHE["exp"] = 1e18
    floor = getattr(cb, "DROP_FLOOR_CHARS", 16_000)

    def is_stub(c):
        return isinstance(c, str) and c.startswith('{"result_dropped"')

    class Pool:
        async def fetch(self, q, *a):
            return []

        async def fetchrow(self, q, *a):
            return None

        async def fetchval(self, q, *a):
            return None

        async def execute(self, q, *a):
            return "OK"

        def acquire(self):
            pool = self

            class _C:
                async def __aenter__(self):
                    return pool

                async def __aexit__(self, *e):
                    return False
            return _C()

    def _tc(name, args, i):
        return {"id": f"call_{name}_{i}", "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)}}

    MATRIX = {"status": "ok", "result": {"harnesses": [{"id": f"h{n}", "models": ["m" * 60] * 40}
                                                       for n in range(14)], "default_engine": "h0"}}
    BENCH = {"status": "ok", "result": {"routing": {"rows": ["r" * 100] * 250}}}
    SMALL = {"status": "ok", "result": {"bastions": [{"hostname": "b1"}], "peers": 0}}

    class Harness:
        def __init__(self, script, results):
            self.script, self.results = list(script), {k: list(v) for k, v in results.items()}
            self.calls, self.runs, self.rows = [], [], []

        async def model(self, messages=None, *a, **kw):
            msgs = messages if messages is not None else (a[0] if a else [])
            self.calls.append([dict(m) for m in msgs])
            r = self.script.pop(0) if self.script else {"content": "(the script ran out)"}
            cb_ = kw.get("stream_cb")
            if cb_ and r.get("content"):
                await cb_(r["content"])
            return {"tokens_used": 1, "prompt_tokens": 1, "completion_tokens": 1,
                    "input_total_tokens": 1000 * len(self.calls), "tool_calls": None, **r}

        async def go(self):
            S._pending_nudges.clear()
            S._turn_events.clear()
            S._task_holds.clear()
            saved = (S._call_entity_model, PG.call_model, IC.resolve_role_route,
                     S._AVAILABLE.get("ceigas_domain_controller"), S._run_close_turn,
                     S._execute_meta_tool, S._build_tool_schemas, ME.write_turn_row,
                     ME.write_turn_row_by_token)

            async def route(pool, eid, role, thread_override=None, **kw):
                return {"provider": "anthropic", "model": "claude-opus-5-5"}

            async def run_close_turn(pool, sess, args, *, prompt_console=None):
                return {"status": "ok", "result": "recorded"}

            async def meta(name, args, **kw):
                self.runs.append((name, dict(args or {})))
                seq = self.results.get(name)
                return copy.deepcopy(seq.pop(0) if seq else {"status": "ok", "result": {}})

            async def schemas(pool, eid, interaction_context=None, **kw):
                return [{"type": "function", "function": copy.deepcopy(P[n]["function"])}
                        for n in ("harness_connections", "coding_bench", "get_network", "nac_status")]

            async def wtr(pool, entity_id, session_id, role, content, *, tags=None, **kw):
                self.rows.append({"role": role, "tags": dict(tags or {})})
                return len(self.rows)

            async def wtr_tok(pool, entity_id, session_id, role, content, *, token, held, tags=None, **kw):
                self.rows.append({"role": role, "tags": dict(tags or {})})
                return len(self.rows)
            S._call_entity_model = self.model
            PG.call_model = self.model
            IC.resolve_role_route = route
            S._AVAILABLE["ceigas_domain_controller"] = None
            S._run_close_turn = run_close_turn
            S._execute_meta_tool = meta
            S._build_tool_schemas = schemas
            ME.write_turn_row, ME.write_turn_row_by_token = wtr, wtr_tok
            rt = S.EntityRuntime(Pool())

            async def ctx(entity_id, **kw):
                return S.EntitySession(
                    session_id="sess-drop", entity_id=entity_id,
                    entity={"id": entity_id, "name": "Nova", "behavioral_params": {},
                            "permission_overrides": {"bot_capabilities": 255}},
                    interaction_context="app:circles")
            rt._build_chat_context_transient = ctx
            try:
                self.out = await rt.send_message(entity_id=9003, user_message="are the harnesses working")
                await asyncio.sleep(0.05)
            finally:
                (S._call_entity_model, PG.call_model, IC.resolve_role_route,
                 S._AVAILABLE["ceigas_domain_controller"], S._run_close_turn,
                 S._execute_meta_tool, S._build_tool_schemas, ME.write_turn_row,
                 ME.write_turn_row_by_token) = saved
            return self

        def tool(self, call_i, tc_id):
            return next((m for m in (self.calls[call_i] if len(self.calls) > call_i else [])
                         if m.get("role") == "tool" and m.get("tool_call_id") == tc_id), None)

        def ledger(self):
            for r in reversed(self.rows):
                led = (r.get("tags") or {}).get("context_ledger")
                if r["role"] == "assistant" and led:
                    return led
            return {}

    # Turn 9262's shape: matrix + bench in one round, then a small read, then
    # the matrix again, then the reply.
    h = run(Harness([
        {"content": "", "tool_calls": [_tc("harness_connections", {"op": "matrix"}, 1),
                                       _tc("coding_bench", {}, 2)]},
        {"content": "", "tool_calls": [_tc("get_network", {}, 3)]},
        {"content": "", "tool_calls": [_tc("harness_connections", {"op": "matrix"}, 4)]},
        {"content": "All seven harnesses answer; codex needs a fresh sign-in."},
    ], {"harness_connections": [MATRIX, MATRIX], "coding_bench": [BENCH], "get_network": [SMALL]}).go())
    m1, b1 = "call_harness_connections_1", "call_coding_bench_2"
    c2_m, c2_b = h.tool(1, m1), h.tool(1, b1)
    check("turn: the reads ran (matrix, bench, network, matrix again) and the turn made 4 calls",
          [n for n, _ in h.runs] == ["harness_connections", "coding_bench", "get_network",
                                     "harness_connections"] and len(h.calls) == 4,
          (h.runs, len(h.calls)))
    check("turn: call 2 (the model has not answered after them yet) carries both results whole",
          c2_m is not None and c2_b is not None and not is_stub(c2_m["content"])
          and len(c2_m["content"]) > floor and len(c2_b["content"]) > floor,
          (len((c2_m or {}).get("content") or ""), len((c2_b or {}).get("content") or "")))
    c3_m, c3_b = h.tool(2, m1), h.tool(2, b1)
    check("turn: call 3 (the model answered after them) carries both as stubs — one cut",
          c3_m is not None and c3_b is not None and is_stub(c3_m["content"]) and is_stub(c3_b["content"]),
          (str(c3_m)[:200], str(c3_b)[:200]))
    asst3 = [m for m in (h.calls[2] if len(h.calls) > 2 else []) if m.get("role") == "assistant"
             and m.get("tool_calls")]
    check("turn: the call/result pairing holds: the asking message and the ids are intact",
          bool(asst3) and [t["id"] for t in asst3[0]["tool_calls"]] == [m1, b1]
          and (c3_m or {}).get("tool_call_id") == m1 and (c3_b or {}).get("tool_call_id") == b1)
    body = json.loads(c3_m["content"]).get("result_dropped", {}) if c3_m and is_stub(c3_m["content"]) else {}
    dig = digest_tool_action("harness_connections", {"op": "matrix"}, MATRIX)
    check("turn: the stub carries the history digest of that call (tool_trail.digest_tool_action) "
          "and says to call harness_connections again",
          body.get("op") == dig.get("op") == "matrix" and body.get("digest") == dig.get("digest")
          and body.get("chars") == len((c2_m or {}).get("content") or "")
          and "Call harness_connections again" in body.get("note", ""), (body, dig))
    c3_n = h.tool(2, "call_get_network_3")
    check("turn: the small result stays whole", c3_n is not None and not is_stub(c3_n["content"]))
    c4_new = h.tool(3, "call_harness_connections_4")
    check("turn: a re-call of the dropped matrix RUNS again and returns it whole — not 'cached', "
          "not 'unchanged since', not refused",
          c4_new is not None and c2_m is not None and c4_new["content"] == c2_m["content"]
          and "unchanged_since_round" not in c4_new["content"] and "Cached from this turn" not in c4_new["content"],
          str(c4_new)[:300])
    check("turn: the earlier stubs stay stubs after the re-call", is_stub((h.tool(3, m1) or {}).get("content")))
    led = h.ledger()
    dropped = led.get("dropped") or []
    check("ledger: dropped = both results, one cut at call 3, the prefix moved, the matrix recalled",
          [(d.get("tool"), d.get("at_call"), d.get("prefix_moved")) for d in dropped]
          == [("harness_connections", 3, True), ("coding_bench", 3, True)]
          and dropped[0].get("chars") == len((c2_m or {}).get("content") or "")
          and dropped[0].get("recalled") is True and "recalled" not in dropped[1], dropped)
    pc = led.get("per_call") or []
    check("ledger: each call carries sys_sha / prefix_sha / sha",
          len(pc) == 4 and all({"sys_sha", "prefix_sha", "sha"} <= set(c) for c in pc), pc[:1])
    check("ledger: call 2's prefix is call 1's list (prefix_sha == sha), the cut call 3's is not, "
          "call 4's is call 3's again",
          len(pc) == 4 and all({"prefix_sha", "sha"} <= set(c) for c in pc)
          and pc[1]["prefix_sha"] == pc[0]["sha"] and pc[2]["prefix_sha"] != pc[1]["sha"]
          and pc[3]["prefix_sha"] == pc[2]["sha"] and len({c.get("sys_sha") for c in pc}) == 1,
          [(c.get("prefix_sha"), c.get("sha")) for c in pc])
    check("ledger: the hashes are rig measurements, kept out of the Host's own mirror",
          {"sys_sha", "prefix_sha", "sha"} <= set(getattr(cb, "_LEDGER_ONLY_CALL_KEYS", ())))

    # D < S through the loop: one eligible result with more already-sent
    # content behind it than it would free — kept.
    MID = {"status": "ok", "result": {"rows": ["q" * 100] * 140}}    # ~14.6k, under the floor
    BIG = {"status": "ok", "result": {"rows": ["g" * 100] * 190}}    # ~19.7k, over it
    h2 = run(Harness([
        {"content": "", "tool_calls": [_tc("get_network", {}, 1), _tc("nac_status", {}, 2),
                                       _tc("coding_bench", {}, 3)]},
        {"content": "", "tool_calls": [_tc("harness_connections", {"op": "status", "provider": "codex"}, 4)]},
        {"content": "Done."},
    ], {"get_network": [BIG], "nac_status": [MID], "coding_bench": [MID]}).go())
    g3 = h2.tool(2, "call_get_network_1")
    check("turn: D < S — a 19.7k result with 29k of results sent behind it stays whole",
          len(h2.calls) == 3 and g3 is not None and not is_stub(g3["content"]) and len(g3["content"]) > floor
          and not (h2.ledger().get("dropped") or []),
          (len(h2.calls), len((g3 or {}).get("content") or ""), h2.ledger().get("dropped")))

    # A reply cut at the output limit is continued from the results it was
    # being written from: the continuation call keeps them whole.
    h3 = run(Harness([
        {"content": "", "tool_calls": [_tc("harness_connections", {"op": "matrix"}, 1),
                                       _tc("coding_bench", {}, 2)]},
        {"content": "Claude Code: signed in. Codex: ", "truncated": True, "output_limit": 64},
        {"content": "needs a fresh sign-in."},
    ], {"harness_connections": [MATRIX], "coding_bench": [BENCH]}).go())
    k3_m, k3_b = h3.tool(2, "call_harness_connections_1"), h3.tool(2, "call_coding_bench_2")
    check("turn: the continuation of a cut reply carries the results it was written from whole",
          len(h3.calls) == 3 and k3_m is not None and k3_b is not None
          and not is_stub(k3_m["content"]) and not is_stub(k3_b["content"])
          and not (h3.ledger().get("dropped") or []),
          (len(h3.calls), str(k3_m)[:120], h3.ledger().get("dropped")))


def main() -> int:
    for part in (part_harness, part_bench, part_network, part_bastion_list, part_text,
                 part_drop_unit, part_drop_turn):
        try:
            part()
        except Exception as e:  # noqa: BLE001 — a part that cannot run is a failure
            import traceback
            traceback.print_exc()
            check(f"{part.__name__} ran", False, repr(e))
    print("\n[condensed] default sizes on the fixtures (chars, before -> after; scoped arg):")
    for name, b, a, arg in SIZES:
        print(f"[condensed]   {name}: {b:,} -> {a:,}  ({arg})")
    print(f"[condensed] {'all passed' if not failures else f'{len(failures)} FAIL(s): {failures}'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
