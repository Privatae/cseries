#!/usr/bin/env python3
"""
Gate — realtime voice: ephemeral-token mint + identity instructions
(server/routes/realtime.py).

Proves the two halves of the voice substrate behave:
  * MINT — resolves the user's sealed provider key from the vault, calls the
    provider's client_secrets endpoint through the isolated _mint_upstream helper,
    clamps the TTL, returns the provider payload untouched — and fails clean (not
    opaque) when the key is absent, the provider is unknown, or upstream rejects.
  * INSTRUCTIONS — GET /instructions loads this entity's identity and returns the
    host's voice system prompt (built by prompt_builder.build_voice_system_prompt),
    so any user's voice app fetches its own host identity instead of hardcoding one;
    fails clean (503) when identity isn't provisioned. Also exercises the REAL
    build_voice_system_prompt (identity preamble + voice discipline, public persona).

Hermetic: the heavy server deps (brain_service, applications.middleware,
vault.api) and aiohttp are stubbed before import, so this runs anywhere.
  python3 scripts/test_realtime_client_secrets.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_failures = 0


def check(label, cond, detail=""):
    global _failures
    if cond:
        print(f"OK    {label}")
    else:
        _failures += 1
        print(f"FAIL  {label}" + (f" — {detail}" if detail else ""))


# ── stub the heavy imports realtime.py pulls, before loading it ──────────────
def _install_stubs(open_secret_impl):
    # server + server.routes packages (namespace shells)
    for name in ("server", "server.routes", "server.applications", "server.vault"):
        if name not in sys.modules:
            m = types.ModuleType(name)
            m.__path__ = []  # mark as package
            sys.modules[name] = m

    brain = types.ModuleType("server.brain_service")
    brain.get_pool = lambda: object()  # non-None sentinel pool
    sys.modules["server.brain_service"] = brain
    sys.modules["server"].brain_service = brain

    mw = types.ModuleType("server.applications.middleware")
    mw.requires_permission = lambda perm: (lambda: None)  # no-op dependency
    sys.modules["server.applications.middleware"] = mw

    vapi = types.ModuleType("server.vault.api")
    vapi.open_secret = open_secret_impl
    sys.modules["server.vault.api"] = vapi


_last_open_args = {}


async def _fake_open_secret(pool, *, entity_id, name, actor=None):
    _last_open_args["entity_id"] = entity_id
    _last_open_args["name"] = name
    return _fake_open_secret.value  # bytes | None


_fake_open_secret.value = b"xai-secret-key-abc"

_install_stubs(_fake_open_secret)

_spec = importlib.util.spec_from_file_location(
    "realtime", os.path.join(_ROOT, "server", "routes", "realtime.py")
)
realtime = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(realtime)


# ── a fake Request carrying a JSON body ──────────────────────────────────────
class FakeRequest:
    def __init__(self, body):
        self._body = body
        self.method = "POST"

    async def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def call(body):
    return asyncio.run(realtime.mint_client_secret(FakeRequest(body), _=None))


# ── TTL clamp is pure ────────────────────────────────────────────────────────
check("ttl default when missing", realtime._clamp_ttl(None) == 60)
check("ttl clamps below min", realtime._clamp_ttl(1) == realtime._TTL_MIN)
check("ttl clamps above max", realtime._clamp_ttl(99999) == realtime._TTL_MAX)
check("ttl passes valid", realtime._clamp_ttl(120) == 120)
check("ttl garbage -> default", realtime._clamp_ttl("abc") == 60)


# ── happy path: resolves key, mints, returns provider payload unchanged ──────
_mint_calls = {}


async def _fake_mint(token_url, api_key, payload):
    _mint_calls["token_url"] = token_url
    _mint_calls["api_key"] = api_key
    _mint_calls["payload"] = payload
    return {"client_secret": {"value": "ephemeral-xyz"}, "model": "grok-voice-latest"}


realtime._mint_upstream = _fake_mint
os.environ["ENTITY_ID"] = "9003"
_fake_open_secret.value = b"xai-secret-key-abc"

res = call({"expires_after": 45, "voice": "ara"})
check("happy: returns provider payload untouched",
      res == {"client_secret": {"value": "ephemeral-xyz"}, "model": "grok-voice-latest"}, str(res))
check("happy: resolved the xai vault key for this entity",
      _last_open_args == {"entity_id": 9003, "name": "xai:voice_api_key"}, str(_last_open_args))
check("happy: upstream got the real bearer + clamped ttl (xAI object shape)",
      _mint_calls["api_key"] == "xai-secret-key-abc"
      and _mint_calls["payload"] == {"expires_after": {"seconds": 45}},
      str(_mint_calls))
check("happy: hit the xai token url",
      _mint_calls["token_url"] == "https://api.x.ai/v1/realtime/client_secrets", _mint_calls["token_url"])


# ── CANONICAL-slot fallback (the 2026-07-25 name-mismatch bug) ───────────────
# The Keys app's "Provider key" path seals under the canonical slot — plain
# "xai" (vault/providers.canonical_name) — while this route used to open ONLY
# "xai:voice_api_key", a name NOTHING in the product writes. A user who
# connected xAI the obvious way got a 400 telling them to connect the key they
# had already connected. Ratchet: a key sealed under the canonical name alone
# MUST resolve.
_open_calls: list[str] = []


async def _canonical_only_open(pool, *, entity_id, name, actor=None):
    """Only 'xai' is sealed — the explicit voice slot is absent."""
    _open_calls.append(name)
    _last_open_args["entity_id"] = entity_id
    _last_open_args["name"] = name
    return b"xai-canonical-key" if name == "xai" else None


_install_stubs(_canonical_only_open)
realtime.open_secret = _canonical_only_open
_mint_calls.clear()
res = call({"expires_after": 60})
check("canonical-slot fallback: mint succeeds when only 'xai' is sealed",
      res.get("client_secret", {}).get("value") == "ephemeral-xyz", str(res))
check("canonical-slot fallback: the canonical key reached upstream",
      _mint_calls.get("api_key") == b"xai-canonical-key".decode(),
      str(_mint_calls.get("api_key")))
check("canonical-slot fallback: explicit voice slot is still tried FIRST",
      _open_calls[0] == "xai:voice_api_key", str(_open_calls))
check("canonical-slot fallback: falls through to the canonical name",
      "xai" in _open_calls, str(_open_calls))

# restore the happy-path stub
_install_stubs(_fake_open_secret)
realtime.open_secret = _fake_open_secret


# ── manifest-driven provider (a promoted proposal actually turns voice ON) ───
# Before this, _PROVIDERS was a hardcoded map and the manifest carried no
# token_url, so approving a realtime_voice proposal promoted a file that NOTHING
# read — voice stayed xai-only. Ratchet: an installed realtime_voice manifest
# must be sufficient, with no code change.
import types as _types

_fake_provider = _types.SimpleNamespace(
    key="acmevoice",
    connection_kind="realtime_voice",
    realtime={"token_url": "https://voice.acme.example/v1/client_secrets",
              "ws_url_template": "wss://voice.acme.example/v1/realtime"},
)
# _resolve_provider does `from server.integrations.core import manifests`, so the
# parent packages must exist AND carry the attribute — registering the leaf in
# sys.modules alone is not enough.
for _pkg in ("server.integrations", "server.integrations.core"):
    if _pkg not in sys.modules:
        _m = _types.ModuleType(_pkg)
        _m.__path__ = []
        sys.modules[_pkg] = _m
sys.modules["server"].integrations = sys.modules["server.integrations"]
sys.modules["server.integrations"].core = sys.modules["server.integrations.core"]

_mf = _types.ModuleType("server.integrations.core.manifests")
_mf.get = lambda k: _fake_provider if k == "acmevoice" else None
sys.modules["server.integrations.core.manifests"] = _mf
sys.modules["server.integrations.core"].manifests = _mf

_cfg = realtime._resolve_provider("acmevoice")
check("manifest provider resolves without a code change", _cfg is not None, str(_cfg))
check("manifest provider uses the manifest's token_url",
      _cfg and _cfg["token_url"] == "https://voice.acme.example/v1/client_secrets", str(_cfg))
check("manifest provider gets the same vault lookup order as built-ins",
      _cfg and _cfg["vault_names"] == ("acmevoice:voice_api_key", "acmevoice"), str(_cfg))

# a built-in must WIN over a same-key manifest, so a promoted manifest can never
# silently repoint an existing provider's token mint at another host
_fake_provider_xai = _types.SimpleNamespace(
    key="xai", connection_kind="realtime_voice",
    realtime={"token_url": "https://evil.example/steal"})
_mf.get = lambda k: _fake_provider_xai if k == "xai" else None
check("built-in wins over a same-key manifest (no silent repoint)",
      realtime._resolve_provider("xai")["token_url"]
      == "https://api.x.ai/v1/realtime/client_secrets",
      str(realtime._resolve_provider("xai")))

# a manifest without a usable token_url is ignored rather than half-wired
_mf.get = lambda k: _types.SimpleNamespace(
    key="nourl", connection_kind="realtime_voice", realtime={}) if k == "nourl" else None
check("manifest without token_url is refused", realtime._resolve_provider("nourl") is None)

# a non-voice manifest must not become a voice provider
_mf.get = lambda k: _types.SimpleNamespace(
    key="gmail", connection_kind="email",
    realtime={"token_url": "https://x/y"}) if k == "gmail" else None
check("non-realtime_voice manifest is not a voice provider",
      realtime._resolve_provider("gmail") is None)

_mf.get = lambda k: None


# ── no sealed key under ANY name -> clean 400 (not a 500/opaque failure) ─────
_fake_open_secret.value = None
try:
    call({"expires_after": 60})
    check("missing key -> 400", False, "did not raise")
except Exception as e:
    code = getattr(e, "status_code", None)
    check("missing key -> HTTP 400", code == 400, f"status={code}")
_fake_open_secret.value = b"xai-secret-key-abc"


# ── unknown provider -> clean 400 ────────────────────────────────────────────
try:
    call({"provider": "bogus"})
    check("unknown provider -> 400", False, "did not raise")
except Exception as e:
    check("unknown provider -> HTTP 400", getattr(e, "status_code", None) == 400)


# ── upstream rejects -> 502 (surfaced, not swallowed) ────────────────────────
async def _reject_mint(token_url, api_key, payload):
    raise realtime._UpstreamError(401, "invalid api key")


realtime._mint_upstream = _reject_mint
try:
    call({"expires_after": 60})
    check("upstream reject -> 502", False, "did not raise")
except Exception as e:
    check("upstream reject -> HTTP 502", getattr(e, "status_code", None) == 502)


# ── GET /instructions: identity route wiring (stubbed builder + fake pool) ────
# Register the brain prompt-builder namespace the route lazy-imports, with a stub
# builder, plus a fake pool that returns one identity row.
for _n in ("mastercode", "mastercode.runtime"):
    if _n not in sys.modules:
        _m = types.ModuleType(_n)
        _m.__path__ = []
        sys.modules[_n] = _m
_pb_stub = types.ModuleType("mastercode.runtime.prompt_builder")
_pb_stub.build_voice_system_prompt = lambda entity: f"STUB-VOICE-PROMPT::{entity['display_name']}"
sys.modules["mastercode.runtime.prompt_builder"] = _pb_stub


class _FakePool:
    def __init__(self, row):
        self._row = row

    async def fetchrow(self, _q, *args):
        return self._row


def _instr(row):
    realtime.brain_service.get_pool = lambda: _FakePool(row)
    return asyncio.run(realtime.voice_instructions(FakeRequest(None), _=None))


_ident_row = {
    "display_name": "Sage", "master_directive": "Serve the user with honesty.",
    "current_weights": {}, "seed_weights": {}, "behavioral_params": {}, "inversion_mode": "classic",
}
_r = _instr(_ident_row)
check("instructions: returns the built prompt + display_name",
      _r == {"instructions": "STUB-VOICE-PROMPT::Sage", "display_name": "Sage"}, str(_r))

# identity not provisioned -> clean 503 (never a half-empty "You are None" prompt)
try:
    _instr({"display_name": "", "master_directive": "", "current_weights": {},
            "seed_weights": {}, "behavioral_params": {}, "inversion_mode": "classic"})
    check("unprovisioned identity -> 503", False, "did not raise")
except Exception as e:
    check("unprovisioned identity -> HTTP 503", getattr(e, "status_code", None) == 503)


# ── REAL build_voice_system_prompt (pure; skipped only if prompt_builder can't
#    import by path here — always covered in-container at smoke) ───────────────
try:
    _pspec = importlib.util.spec_from_file_location(
        "pb_real", os.path.join(_ROOT, "brain", "mastercode", "runtime", "prompt_builder.py"))
    _pb = importlib.util.module_from_spec(_pspec)
    _pspec.loader.exec_module(_pb)
    _pb_ok = True
except Exception as e:  # heavy deps absent on the ops box
    _pb_ok = False
    print(f"SKIP  build_voice_system_prompt pure test (prompt_builder import: {e})")

if _pb_ok:
    _p = _pb.build_voice_system_prompt({
        "display_name": "Sage",
        "master_directive": "Serve the user with honesty.",
        "current_weights": {}, "seed_weights": {},
        "behavioral_params": {"response_style": "conversational"},
        "inversion_mode": "classic",
    })
    check("voice prompt names the host", "Sage" in _p, _p[:120])
    check("voice prompt carries the master directive", "Serve the user with honesty." in _p)
    check("voice prompt has spoken-voice guidance",
          "live voice call" in _p and "spoken aloud" in _p, _p[:200])
    check("voice prompt is modern/TTS-aware (no markdown; read numbers naturally)",
          "no markdown" in _p and "read numbers" in _p)
    check("voice prompt exposes no raw weight keys (public persona only)",
          "current_weights" not in _p and "seed_weights" not in _p)
    # No-redundancy ratchet: the chat-era stacked voice/honesty blocks are gone and the
    # single voice-behavior block appears exactly once.
    check("no redundant chat-era voice blocks",
          "## Your Output Medium" not in _p
          and "## Voice Discipline" not in _p
          and "## Voice Quality" not in _p, _p[:300])
    check("single voice-behavior block", _p.count("You're on a live voice call") == 1)
    # Graded renderer (2026-07-21): a neutral host now carries real mid-band
    # persona prose — the old content-free "balanced" filler is gone entirely
    # (review N2: all-1.0 hosts previously shipped personality-empty).
    check("neutral host renders graded mid-band tendencies",
          "balanced across all dimensions" not in _p
          and "evenly warm" in _p, _p[:300])
    # A personality-bearing host renders tendencies as natural language, never numbers.
    _p2 = _pb.build_voice_system_prompt({
        "display_name": "Ada", "master_directive": "Help.",
        "current_weights": {"warmth": 1.6, "directness": 1.5},
        "seed_weights": {}, "behavioral_params": {}, "inversion_mode": "classic",
    })
    check("personality renders in natural language, not numbers",
          "## Who you are" in _p2 and "radiate warmth" in _p2 and "1.6" not in _p2, _p2[:300])


print()
if _failures:
    print(f"FAILED: {_failures} check(s)")
    sys.exit(1)
print("PASSED: realtime client_secrets mint + voice instructions")
