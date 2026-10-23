#!/usr/bin/env python3
"""
ONE text role — the ratchet that keeps a second from growing back. (The file
keeps the name it had when there were three; other files point at it.)

The orchestrator was too complicated; model calls belong only
where reasoning happens. Nine roles became three — `chat`
(the host), `review` (the strong model reading a landed diff) and
`consolidation` (memory, labelled "Memory" in the UI). plan, research,
agents, tutor and journal are gone. `consolidation` followed
(memory ops are plain tool calls): memory forms in the Host's
own turn, and the background loop, when it runs, resolves the chat route.
`review` followed with the diff reviewer, its one consumer: the Host reviews
in its own turn, or has a coding engine review. `chat` is the one text role
left.

Measured on one Host over the 14 days before the cut: plan 0 attributable
calls, review 0, tutor 0, journal 0 (chat sessions are ephemeral so the
session-close recap never fired), research 17, agents 15 — against chat 291
and consolidation 545. Four of the five never fired at all, and every one of
them was a seat the user had to fill before the feature behind it would work.

The failure this exists to prevent is a SILENT one, in both directions:

  - A new consumer resolves a role that no longer exists. resolve_role_route
    does not validate against ROLES (deliberately — it is the read path and
    an unknown role must resolve empty, not raise), so such a call does not
    error. It returns unconfigured forever, and the user is told to route a
    role the Orchestrator will not let them set. §2 scans every call site.
  - The UI and the server drift apart. The role list lives in three places
    that cannot import each other — inference.ROLES (python), model-picker.js
    (the browser) and recommended-routes.json (data shipped in the bundle) —
    and a role present in one but not the others renders a row that cannot be
    saved, or a saved route with no row. §1, §3 and §4 pin them together.
  - A stored route under a removed key comes back. Nothing migrates those
    (no migration code for a rename), so they sit in behavioral_params
    forever; the reader must drop them or the picker round-trips one into
    every save, the way a retired harness id used to. §3 exercises it.

Usage: APP_ROOT=<repo> python3 scripts/test_three_text_roles.py
"""
from __future__ import annotations

import ast
import io
import json
import logging
import os
import re
import sys
import zipfile
from pathlib import Path
from tools_source import tools_source  # tools.py and the modules split out of it

logging.disable(logging.CRITICAL)

_ROOT = Path(os.environ.get("APP_ROOT") or (
    "/app" if os.path.exists("/app/brain/mastercode/inference_config.py")
    else Path(__file__).resolve().parents[1]))
for _p in (str(_ROOT), str(_ROOT / "brain")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
os.environ.setdefault("ENTITY_ID", "1")

TEXT_ROLES = {"chat"}
# 'vision' left: a picture goes to the Host model itself, so no second model
# is routed to read one. 'review' left with the diff reviewer: the Host
# reviews in its own turn.
REMOVED = ("plan", "research", "agents", "tutor", "journal", "consolidation", "vision", "review")
# Not text roles and never were — they pick from a provider's image-capable
# catalog, and embedding routes through /api/embedding/switch.
NON_TEXT = ("embedding", "image", "image_gen")

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("ok   " if ok else "FAIL ") + name
          + (("  -- " + str(detail)) if not ok and detail else ""))
    if not ok:
        FAILS.append(name)


# ── 1. the declaration: ROLES carries exactly chat, plus the non-text ──────

print("== 1. server/routes/inference.py — the declaration ==")
from server.routes import inference as I            # noqa: E402

declared_text = {r for r in I.ROLES if r not in NON_TEXT}
check("ROLES' text set is exactly {chat}",
      declared_text == TEXT_ROLES, str(sorted(declared_text)))
from mastercode.runtime.model_standard import LIVE_TEXT_ROLES   # noqa: E402
check("model_standard judges exactly the text roles (a retired role's stored route is never judged)",
      set(LIVE_TEXT_ROLES) == TEXT_ROLES, str(LIVE_TEXT_ROLES))
from mastercode.runtime import model_standard as _MS   # noqa: E402
_audit_roles = {a.get("role") for a in _MS.audit_routes({
    "chat": {"provider": "openai", "model": "invented-model-a"},
    "review": {"provider": "openai", "model": "invented-model-b"}}) if isinstance(a, dict)}
check("...so a route still stored under 'review' is never judged",
      "chat" in _audit_roles and "review" not in _audit_roles, str(_audit_roles))
check("no removed role survives in ROLES",
      not (set(REMOVED) & set(I.ROLES)),
      str(sorted(set(REMOVED) & set(I.ROLES))))
check("every text role has a description the picker can render",
      TEXT_ROLES <= set(I.ROLE_DESCRIPTIONS),
      str(sorted(TEXT_ROLES - set(I.ROLE_DESCRIPTIONS))))
check("no description describes a role that no longer exists",
      not (set(REMOVED) & set(I.ROLE_DESCRIPTIONS)),
      str(sorted(set(REMOVED) & set(I.ROLE_DESCRIPTIONS))))
# The PUT is what a stale UI would hit. It must refuse, not store.
check("the write surface validates against ROLES",
      "if body.role not in ROLES" in (_ROOT / "server/routes/inference.py").read_text(),
      "set_role_model no longer gates on ROLES")


# ── 2. static scan: every resolve_role_route call names a live role ────────
#
# A literal argument is checkable; a variable is not, so a non-literal is
# reported rather than passed over — it is exactly how a removed role would
# re-enter without tripping this.

print("\n== 2. every resolve_role_route call site names a live role ==")

_CALLERS = {"resolve_role_route", "_rrr", "_rrr_sum"}
ALLOWED_ROLE_ARGS = TEXT_ROLES | set(NON_TEXT)

# The four role-GENERIC sites: each takes the role from its caller or walks
# ROLES, so there is no literal to check and checking one would be theatre.
# They are enumerated rather than waved through by shape, because the whole
# point of this scan is that a new dynamic site is where a dead role would
# re-enter unseen. Adding one is a deliberate act: put it here with why.
GENERIC_SITES = {
    # Walks ROLES itself, so it cannot name anything ROLES does not hold.
    ("server/routes/inference.py", "get_current_routing"),
    # The per-role test endpoint; it validates `role in ROLES` before this.
    ("server/routes/inference.py", "test_role_model"),
    # The 2-tuple wrapper for stragglers — role is its own parameter.
    ("brain/mastercode/inference_config.py", "resolve_role_model"),
    # image_gen, passed in by image_engine's caller.
    ("brain/mastercode/image_engine.py", "_resolve_image_route"),
    # Walks web_research._TEXT_ROLES to find which providers the host already
    # runs on. Safe for the same reason get_current_routing is — it iterates a
    # role list rather than naming one — and that list is pinned against the
    # server's below, so it cannot drift into holding a dead role.
    ("brain/mastercode/runtime/web_research.py", "routed_providers"),
}


def _role_arg(node: ast.Call) -> object:
    """The role argument of a resolver call: 3rd positional, or role=.
    Returns the string, or None when it is not a literal."""
    for kw in node.keywords:
        if kw.arg == "role":
            return kw.value.value if isinstance(kw.value, ast.Constant) else None
    if len(node.args) >= 3:
        a = node.args[2]
        return a.value if isinstance(a, ast.Constant) else None
    return None


def _enclosing(tree: ast.AST, lineno: int) -> str:
    best = None
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and n.lineno <= lineno <= (n.end_lineno or n.lineno):
            if best is None or n.lineno > best.lineno:
                best = n
    return best.name if best else "<module>"


scanned = bad = 0
seen_generic: set = set()
for root in ("brain", "server"):
    for path in sorted((_ROOT / root).rglob("*.py")):
        if "node_modules" in path.parts:
            continue
        src = path.read_text(encoding="utf-8", errors="replace")
        if "resolve_role_route" not in src and "_rrr" not in src:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        rel = str(path.relative_to(_ROOT))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else (
                fn.attr if isinstance(fn, ast.Attribute) else None)
            if name not in _CALLERS:
                continue
            scanned += 1
            role = _role_arg(node)
            if role is None:
                site = (rel, _enclosing(tree, node.lineno))
                if site in GENERIC_SITES:
                    seen_generic.add(site)
                    continue
                check(f"{rel}:{node.lineno} ({site[1]}) resolves a NON-LITERAL role and is "
                      f"not a declared generic site — a dead role reaches it unseen", False)
                bad += 1
            elif role not in ALLOWED_ROLE_ARGS:
                check(f"{rel}:{node.lineno} resolves '{role}', which is not a role", False)
                bad += 1

check(f"every resolve_role_route call site names a live role ({scanned} scanned)",
      bad == 0, f"{bad} bad")
check("the scan actually found call sites (a broken scan must not read green)",
      scanned >= 15, f"only {scanned} found")
# A generic site that disappeared means the allowlist is describing code that
# is gone — stale permission is how an allowlist stops being a ratchet.
check("every declared generic site still exists",
      seen_generic == GENERIC_SITES,
      f"missing: {sorted(GENERIC_SITES - seen_generic)}")


# ── 3. a stale stored route is ignored, and nothing crashes ─────────────────
#
# The real read path, against behavioral_params that still carry plan/agents
# from the nine-role era. Two things must hold: they never reach the caller,
# and reading them raises nothing.

print("\n== 3. a stored plan/agents/review route is dropped, not reported ==")

import asyncio                                       # noqa: E402
import types as _types                               # noqa: E402

STALE_PARAMS = {
    "model_routing": {
        "chat":   {"provider": "kimi", "model": "k3"},
        # Left behind by a user who routed them before 2026-09-19.
        "plan":   {"provider": "openai", "model": "gpt-5.5"},
        "agents": {"provider": "anthropic", "model": "claude-opus-5"},
        "tutor":  {"provider": "kimi", "model": "k3"},
        # Left behind by a user who routed review before 2026-10-20.
        "review": {"provider": "anthropic", "model": "claude-opus-5"},
    },
}


class _Pool:
    async def fetchrow(self, _q, *_a):
        return {"behavioral_params": json.dumps(STALE_PARAMS)}

    async def fetch(self, _q, *_a):
        return []

    async def execute(self, _q, *_a):
        return None


_bs = _types.ModuleType("server.brain_service")
_bs._entity_id = 7
_bs.get_pool = lambda: _Pool()
sys.modules["server.brain_service"] = _bs
import server as _server                             # noqa: E402
_server.brain_service = _bs
# inference.py did `from server import brain_service` at import time, so it
# holds its own binding — replacing the sys.modules entry alone leaves it on
# the real module and the read below silently returns the empty base dict.
I.brain_service = _bs


async def _nothing_installed():
    return []


async def _no_providers(_pool, _eid):
    return []


I._installed_harness_options = _nothing_installed
I._provider_status = _no_providers

try:
    status = asyncio.run(I.get_current_routing())
    crashed = None
except Exception as e:  # noqa: BLE001
    status, crashed = {}, e

check("reading a routing table with stale keys does not raise", crashed is None, repr(crashed))
check("the stale keys are dropped from `routing` — the picker round-trips "
      "what it is handed, so one left in would be re-saved forever",
      not (set(REMOVED) & set(status.get("routing") or {})),
      str(sorted(set(REMOVED) & set(status.get("routing") or {}))))
check("the live route survives the filter",
      (status.get("routing") or {}).get("chat", {}).get("model") == "k3",
      str(status.get("routing")))
check("`effective` resolves only live roles",
      not (set(REMOVED) & set(status.get("effective") or {})),
      str(sorted(set(REMOVED) & set(status.get("effective") or {}))))
check("the role list handed to the UI carries no removed role",
      not (set(REMOVED) & set(status.get("roles") or [])),
      str(status.get("roles")))

# The host-facing status (model_routing op='status') derives roles_state and
# `unrouted` by walking inf.ROLES, so a stale key cannot be reported as
# unrouted. Pin the loop, since that is the property — not the value.
_tools = tools_source(_ROOT)
check("the routing status walks inf.ROLES, so `unrouted` can only name live roles",
      "for role in inf.ROLES:" in _tools)


# ── 4. the Orchestrator UI and its bundle offer one text row ───────────────

print("\n== 4. the Orchestrator surface ==")

_mp_src = (_ROOT / "dashboard/base/js/model-picker.js").read_text()


def _js_list(src: str, name: str) -> list[str]:
    m = re.search(r"const\s+" + name + r"\s*=\s*\[([^\]]*)\]", src)
    return re.findall(r"'([^']+)'", m.group(1)) if m else []


def _js_obj_keys(src: str, decl: str) -> list[str]:
    """Keys of a JS object literal. Several keys may share a line, so match
    on the delimiter before each key rather than on line starts."""
    m = re.search(re.escape(decl) + r"\s*=\s*\{(.*?)\n\s*\};", src, re.S)
    if not m:
        return []
    return re.findall(r"(?:^|[{,])\s*([A-Za-z_][A-Za-z0-9_]*)\s*:", m.group(1))


mp_roles = _js_list(_mp_src, "ROLES")
check("model-picker.js ROLES is exactly the text roles, in the server's order",
      mp_roles == [r for r in I.ROLES if r not in NON_TEXT], str(mp_roles))
mp_labels = _js_obj_keys(_mp_src, "const ROLE_LABELS")
check("every rendered row has a label and no label names a dead role",
      set(mp_labels) == TEXT_ROLES, str(sorted(mp_labels)))
mp_act = _js_obj_keys(_mp_src, "const ACT")
check("the 'what answers when' overlay describes exactly the text roles",
      set(mp_act) == TEXT_ROLES, str(sorted(mp_act)))

_recs = json.loads((_ROOT / "system-apps/orchestrator/recommended-routes.json").read_text())
for prov, spec in (_recs.get("providers") or {}).items():
    check(f"recommended-routes {prov}: the one text role, no more",
          set((spec.get("roles") or {})) == TEXT_ROLES,
          str(sorted(spec.get("roles") or {})))

# The bundle is what an entity INSTALLS; source that was never re-packed
# ships nothing (scripts/test_bundle_freshness.py exists for that class of
# bug). Read the payload, not the tree.
for bundle in ("native-bundles/orchestrator.cseries", "factory-bundles/orchestrator.cseries"):
    path = _ROOT / bundle
    if not path.exists():
        check(f"{bundle} exists", False)
        continue
    with zipfile.ZipFile(io.BytesIO(path.read_bytes())) as z:
        packed = json.loads(z.read("payload/recommended-routes.json").decode("utf-8"))
        manifest = z.read("manifest.yaml").decode("utf-8", "replace")
    packed_roles = {r for spec in (packed.get("providers") or {}).values()
                    for r in (spec.get("roles") or {})}
    check(f"{bundle}: the packed recommendations carry only the text roles",
          packed_roles == TEXT_ROLES, str(sorted(packed_roles)))
    # The manifest is the app's host-facing description — whatever it says is
    # what the host will tell the user the Orchestrator offers. Only
    # prompt_behavior is checked: the changelog above it names the removed
    # roles deliberately, as the record of their removal.
    #
    # The test is whether a removed name is OFFERED as a role, not whether the
    # word appears — "web research" and "the agents it dispatches" are the
    # correct description of what the host model now covers, and a check that
    # banned those words would push the copy into saying less than it knows.
    behavior = manifest.split("prompt_behavior:", 1)[-1]
    _removed_alt = "|".join(REMOVED)
    offered = sorted(set(
        re.findall(r"\b(" + _removed_alt + r")\b(?=\s+role\b)", behavior)
        + re.findall(r"\brole\s+\b(" + _removed_alt + r")\b", behavior)))
    check(f"{bundle}: prompt_behavior offers no removed role", not offered, str(offered))
    check(f"{bundle}: prompt_behavior carries no stale role enumeration",
          "review, plan, research, agents, consolidation, journal" not in behavior)
    # The positive half — a negative check alone passes on copy that says
    # nothing at all.
    check(f"{bundle}: prompt_behavior says there is one text role, and names it",
          "one text role" in behavior and "HOST MODEL" in behavior)
    # REVIEW was the role's display name, which the REMOVED scan above
    # (lowercase, next to the word 'role') cannot see.
    check(f"{bundle}: prompt_behavior names no two-role count and no Review role",
          "exactly two" not in behavior and "both text roles" not in behavior
          and "two text roles" not in behavior and "REVIEW" not in behavior,
          [w for w in ("exactly two", "both text roles", "two text roles", "REVIEW") if w in behavior])
    # The Memory role went by its DISPLAY name, which the REMOVED scan above
    # cannot see: "MEMORY (distilling conversations…)" survived in this text
    # while the Routing tab already showed two rows.
    check(f"{bundle}: prompt_behavior names no three-role count and no Memory role",
          "exactly three" not in behavior and "three text roles" not in behavior
          and "MEMORY (" not in behavior, "")


# ── 5. web search survives a chat model that cannot search ─────────────────
#
# Removing the `research` role took away the only way a host on local /
# google / deepinfra / together / fireworks / groq could reach the web: those
# providers ship no search mechanism, and the role could previously be
# pointed at OpenRouter. Chat-only would have dropped the capability in
# silence. research_routes borrows a ROUTED search-capable provider instead —
# so what follows is the ratchet on a CAPABILITY, not on a role.
#
# Since 2026-10-20 (review gone) chat is the only routed text role, so the
# borrow is reached when the chat MODEL cannot search on a provider that can
# (web_research.MODEL_WEBSEARCH_OVERRIDES); a route stored under the retired
# review role is never borrowed. The borrow mechanics are exercised that way.

print("\n== 5. web search without a research role ==")

from mastercode.runtime import web_research as WR      # noqa: E402


def _routes(routing, served=None, keyed=()):
    """research_routes against a faked ROUTING TABLE.

    `routing` is {role: (provider, model)} for chat (and retired roles) — the
    real routed_providers runs over it, so the narrowing is under test rather
    than stubbed. `keyed` is the provider-key registry: it is faked too, and
    deliberately WIDER than `routing`, so a provider the user holds a key for
    but routes nothing to must not be borrowed. `served` is the live catalog
    (None = unreachable, the unverified path)."""
    async def _resolve(pool, eid, role, **kw):
        pair = routing.get(role)
        if not pair:
            return {"provider": "", "model": "", "unconfigured": f"no {role} route"}
        return {"provider": pair[0], "model": pair[1],
                "base_url": None, "fallback_provider": None, "fallback_model": None}

    async def _provider_status(pool, eid):
        return [{"id": p, "has_key": True} for p in keyed]

    async def _direct(p):
        return ([{"id": m} for m in served], None) if served is not None \
            else ([], "catalog unreachable")

    import mastercode.inference_config as _ic
    import server.routes.inference as _I
    _o = (_ic.resolve_role_route, _I._direct_models, _I._provider_status)
    _ic.resolve_role_route, _I._direct_models, _I._provider_status = \
        _resolve, _direct, _provider_status
    try:
        return asyncio.run(WR.research_routes(object(), 3))
    finally:
        _ic.resolve_role_route, _I._direct_models, _I._provider_status = _o


# The list routed_providers walks. Pinned against the server's, because the
# static scan waives that call site on the grounds that it iterates a role
# list — which is only sound while the list is the right one.
check("web_research._TEXT_ROLES is exactly the text roles",
      set(WR._TEXT_ROLES) == TEXT_ROLES, str(WR._TEXT_ROLES))

_OR = WR.recommended_chat_candidates("openrouter")
_AN = WR.recommended_chat_candidates("anthropic")
check("the recommendation data has chat candidates to borrow",
      bool(_OR) and bool(_AN), f"openrouter={_OR} anthropic={_AN}")

# A chat model that cannot search, on a provider that can (an invented id).
_NOSEARCH = "invented/no-web-model"
_saved_ov = dict(WR.MODEL_WEBSEARCH_OVERRIDES)
WR.MODEL_WEBSEARCH_OVERRIDES[_NOSEARCH] = False

# a) the host model can search — use it, and borrow nothing.
r = _routes({"chat": ("openrouter", "x/y")})
check("chat on a search-capable provider searches on the chat model itself",
      len(r) == 1 and r[0]["provider"] == "openrouter" and r[0]["model"] == "x/y"
      and not r[0].get("_fallback") and not r[0].get("_route_note"), str(r))

# b) the host model cannot search, but its ROUTED provider can.
r = _routes({"chat": ("openrouter", _NOSEARCH)}, served=[_OR[0]])
check("a chat model that cannot search, on openrouter, borrows openrouter's recommended model",
      len(r) == 1 and r[0]["provider"] == "openrouter" and r[0].get("_fallback") is True,
      str(r))
check("the borrowed model comes from recommended-routes.json, never a pin",
      bool(r) and r[0]["model"] in _OR, str(r))
check("the borrowed route is STATED — provider, model, and why",
      bool(r) and "searched via openrouter/" in (r[0].get("_route_note") or "")
      and "cannot search" in (r[0].get("_route_note") or ""), str(r))
check("the route carries the TRUE failure sentence for a borrowed call",
      bool(r) and "borrowed openrouter/" in (r[0].get("_fail_note") or "")
      and "host model itself cannot search" in (r[0].get("_fail_note") or ""), str(r))

# b2) a route stored under a retired role is not a routing decision any
#     more: nothing reads it, so it is never borrowed — Memory (2026-09-30)
#     and Review (2026-10-20) alike.
r = _routes({"chat": ("local", "l"), "consolidation": ("anthropic", "claude-x")},
            served=[_AN[0]])
check("a provider routed only under the retired MEMORY role is not borrowed",
      r == [], str(r))
r = _routes({"chat": ("local", "llama-local"), "review": ("openrouter", "z/w")},
            served=[_OR[0]])
check("a provider routed only under the retired REVIEW role is not borrowed",
      r == [], str(r))

# c) THE NARROWING: keyed is not routed. A key is permission to spend, not a
#    decision to — borrowing one would open a bill the user never chose.
r = _routes({"chat": ("local", "l")}, served=[_OR[0]],
            keyed=["openrouter", "anthropic", "openai", "xai", "kimi"])
check("every search-capable provider keyed, none routed → no borrow",
      r == [], str(r))

# d) nothing routed can search — say so, never fabricate a route.
r = _routes({"chat": ("together", "t/m"), "consolidation": ("groq", "g/m")})
check("chat on a provider with no web mechanism + nothing else routed yields NO route",
      r == [], str(r))
check("SEARCH_CAPABLE_PROVIDERS is the whole test of who may be borrowed",
      WR.SEARCH_CAPABLE_PROVIDERS
      == frozenset({"openrouter", "anthropic", "openai", "xai", "kimi"}),
      str(sorted(WR.SEARCH_CAPABLE_PROVIDERS)))

# e) the catalog decides WHICH candidate; unreachable still answers, flagged.
if len(_OR) > 1:
    r2 = _routes({"chat": ("openrouter", _NOSEARCH)}, served=[_OR[1]])
    check("the live catalog picks among the candidates, skipping what it won't serve",
          bool(r2) and r2[0]["model"] == _OR[1], str(r2))
r3 = _routes({"chat": ("openrouter", _NOSEARCH)}, served=None)
check("an unreachable catalog still answers, flagged unverified",
      bool(r3) and r3[0]["model"] == _OR[0]
      and "unverified" in (r3[0].get("_route_note") or ""), str(r3))

# f) the borrowed provider is the ROUTED one — chat's — never one stored
#    under a retired role, even one that would sort first.
r = _routes({"chat": ("xai", _NOSEARCH), "consolidation": ("anthropic", "claude-x"),
             "review": ("anthropic", "claude-y")}, served=None)
check("the borrowed provider is chat's, not a retired role's that sorts first",
      bool(r) and r[0]["provider"] == "xai", str(r))
WR.MODEL_WEBSEARCH_OVERRIDES.clear()
WR.MODEL_WEBSEARCH_OVERRIDES.update(_saved_ov)

# g) the disclosure survives EVERY envelope shape, not just the dict ones.
#    A borrowed call that fails used to read as the host's own model failing.
_borrow = {"provider": "kimi", "model": "k3", "_role": "chat", "_fallback": True,
           "_route_note": "searched via kimi/k3 because the chat model cannot search",
           "_fail_note": "the borrowed kimi/k3 could not search; the host model "
                         "itself cannot search the web, so there was nothing else to try"}
_ok = WR._disclose_borrow({"status": "ok", "result": {"answer": "a", "sources": []}}, _borrow)
check("ok (dict result) carries route_note as a field",
      _ok["result"].get("route_note") == _borrow["_route_note"], str(_ok))
_ug = WR._disclose_borrow({"status": "ungrounded", "result": {"reason": "no citations"}}, _borrow)
check("ungrounded (dict result) carries route_note as a field",
      _ug["result"].get("route_note") == _borrow["_route_note"], str(_ug))
_un = WR._disclose_borrow({"status": "unavailable", "result": WR.KIMI_NOT_INJECTED}, _borrow)
check("unavailable (STRING result) still discloses the borrow",
      isinstance(_un["result"], str) and _un["result"].startswith("the borrowed kimi/k3"),
      str(_un)[:200])
check("the string-shape advice does NOT blame the host model for answering",
      "host model itself cannot search" in _un["result"]
      and _un["result"].index("the borrowed") == 0, str(_un)[:200])
_er = WR._disclose_borrow({"status": "error", "result": "boom"}, _borrow)
check("error (STRING result) still discloses the borrow",
      _er["result"].startswith("the borrowed kimi/k3") and "boom" in _er["result"], str(_er))
check("a NON-borrowed envelope is returned untouched",
      WR._disclose_borrow({"status": "ok", "result": {"answer": "a"}},
                          {"provider": "openrouter", "_role": "chat"})["result"]
      == {"answer": "a"})

# h) the honest message when there is nothing to borrow.
async def _no_routes(pool, eid):
    return []


_orig_rr = WR.research_routes
WR.research_routes = _no_routes
try:
    _out = asyncio.run(WR.search("q", pool=object(), entity_id=9003))
finally:
    WR.research_routes = _orig_rr
check("the no-route message says the model cannot search AND names the Orchestrator",
      _out["status"] == "unavailable" and "cannot search the web" in _out["result"]
      and "Orchestrator" in _out["result"], str(_out)[:240])
check("the no-route message says a key alone is not enough",
      "key alone is not enough" in _out["result"], str(_out)[:240])
# The fix it names must be a role that exists. "the host model — or review,
# or memory" survived the Memory role's removal because the
# checks above test only what the message says, never which roles it offers.
check("the no-route message offers no removed role",
      not re.search(r"\b(memory|consolidation|review)\b", _out["result"], re.I),
      str(_out)[:240])
for _skill in ("default-skills/routing.md", "default-skills/research.md"):
    _txt = (_ROOT / _skill).read_text()
    check(f"{_skill}: the web-search borrow names the one text role, no more",
          not re.search(r"\b(three|two|both) (text )?roles\b", _txt, re.I)
          and "review or memory" not in _txt and "either role" not in _txt,
          [m.group(0) for m in re.finditer(r"\b(?:three|two|both) (?:text )?roles\b|either role", _txt, re.I)])

# i) the borrowed route reaches the ANSWER and the stored FINDING, not just the log.
_sop = (_ROOT / "default-skills/routing.md").read_text()
_wr_src = (_ROOT / "brain/mastercode/runtime/web_research.py").read_text()
check("search() runs every envelope through the disclosure",
      "res = _disclose_borrow(res, r)" in _wr_src)
check("routing.md tells the host to relay route_note, failures included",
      "route_note" in _sop and "including failures" in _sop)
check("a stored research finding records which provider answered the web leg",
      'meta["web_route_note"] = web_route_note'
      in (_ROOT / "brain/mastercode/skills/deep_research.py").read_text())
check("the research SUMMARY row records it too",
      'summary_meta["web_route_note"] = web_route_note'
      in (_ROOT / "brain/mastercode/skills/deep_research.py").read_text())


# ── 6. the ledger rollup keeps host and agent rows apart ───────────────────
#
# Both writers stamp role 'chat' now, so grouping by role alone would sum a
# dispatched agent's faults into the host's own row — and "my agents invent
# tool names" and "I invent tool names" are different findings.

print("\n== 6. measured rows split host from agent ==")

_led = (_ROOT / "brain/mastercode/runtime/tool_call_ledger.py").read_text()
check("rollup groups by lane as well as (role, provider, model)",
      "GROUP BY lane, role, provider, model, reason" in _led
      and 'key = (r["role"], r["provider"], r["model"], r["lane"])' in _led)
check("the lane travels on each row, so the reader can see it",
      '"lane": r["lane"],' in _led)
check("the routing SOP tells the host to read the lane before blaming a model",
      "'agent' is the agents you dispatched" in _sop)


print()
if FAILS:
    print(f"FAIL: {len(FAILS)} check(s) failed")
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("PASS: one text role — chat; every consumer "
      "names a live role; stale stored routes are ignored, not reported; web "
      "search survives a chat model that cannot search; measured rows split "
      "host from agent")
