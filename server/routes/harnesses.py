"""Coding-harness dispatch substrate — the provider catalog + credential
status/validity/notify + cred-maintenance loops that the REST of the platform
reads to decide "which harnesses can I fan out to, and are their credentials
live."

Relocated here from server/routes/connections.py during the canvas pivot
(2026-07). The Connections *app* and its OAuth *login flows* are being removed
— the platform ships no third-party login surface — but the harness dispatch
path stays: the Orchestrator routing grid, the compare's pre-dispatch gate,
the harness-health loop, and the harness_connections host tool all read this
substrate. So it lives in its own module, independent of the connect-flow UI
that is going away.

Credential INGEST is moving to the Keys app (the user brings an API key or their
own OAuth token; the host wires the harness template to that named vault entry).
This module is deliberately ingest-agnostic: it reads whatever credential is
materialized at the harness's creds_path, exactly as before — the *how it got
there* is what changes, not the *is it present / still valid* read.

NOTE (canvas pivot, transitional): the PROVIDERS records still carry OAuth-flow
fields (authorize_url, token_url, client_id, scope, …). Those are vestigial —
they were read only by the connect flows that are being deleted — and get
stripped in the Keys-ingest reshape. The runtime fields this module actually
uses are: id, name, vendor, auth_type, creds_path, present_if_app_installed,
install_app_id.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("privatae.pc.harnesses")


# ── Provider catalog ────────────────────────────────────────────────
#
# First-party harness plugins. `present_if_app_installed` gates whether the
# provider shows up in the catalog at all — no use surfacing Claude Code if the
# user hasn't installed the claude-code app.
PROVIDERS: dict[str, dict[str, Any]] = {
    "claude-sdk": {
        "id":            "claude-sdk",
        "name":          "Claude Code",
        "vendor":        "Anthropic",
        "subscription":  "Claude Pro / Max",
        "auth_type":     "oauth_pkce",
        "client_id":     "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
        # The browser popup must hit the CANONICAL authorize host directly.
        # claude.com/cai/oauth/authorize is only a 307 redirect SHIM to
        # claude.ai/oauth/authorize — bouncing a cross-origin OAuth popup
        # through claude.com first is what produced "claude.com refused to
        # connect" (codex, which opens auth.openai.com directly, was fine).
        # Go straight to claude.ai (the host the user already signs into);
        # same flow, one less cross-origin hop.
        "authorize_url": "https://claude.ai/oauth/authorize",
        # Anthropic's manual-code page: show the code on the success page for
        # copy-paste instead of auto-redirecting. Matches `claude setup-token`
        # + the proven sdk_auth.py build; without it the IdP won't render a
        # pasteable code.
        "authorize_extra": {"code": "true"},
        "token_url":     "https://platform.claude.com/v1/oauth/token",
        "redirect_uri":  "https://platform.claude.com/oauth/code/callback",
        # The harness runs the bundled Claude Code CLI/Agent SDK, which needs
        # user:sessions:claude_code (+ profile/mcp/file) — NOT just user:inference. A
        # user:inference-only token authenticates a direct /v1/messages call but the
        # bundled `claude` CLI gets 401 "Invalid authentication credentials". Matches
        # `claude setup-token` (without --inferenceOnly) + the frontend OAuth; verified
        # on the edge bastion: full-scope token -> CLI OK, inference-only -> 401.
        "scope":         "user:inference user:sessions:claude_code user:profile user:mcp_servers user:file_upload",
        "user_agent":    "claude-cli/2.1.145 (cseries entity)",
        "creds_path":    "/home/privatae/.claude/.credentials.json",
        "present_if_app_installed": "claude-code",
        # The installable bundle id differs from the provider id: the
        # provider is "claude-sdk" (the auth surface) but the app the
        # portal must install is "claude-code". Without this the Install
        # button posts app_id="claude-sdk" → no such bundle.
        "install_app_id": "claude-code",
    },
    "codex": {
        "id":            "codex",
        "name":          "Codex",
        "vendor":        "OpenAI",
        "subscription":  "ChatGPT Plus / Pro / Business / Enterprise",
        "auth_type":     "oauth_device_code",
        # OpenAI's Codex CLI does NOT use RFC 8628. It uses its own
        # `/api/accounts/deviceauth/...` shape against auth.openai.com
        # with JSON bodies (not form-encoded) and a `device_auth_id`
        # key on the response (not `device_code`). The token poll body
        # is `{device_auth_id, user_code}`, not the RFC 8628
        # `{grant_type, device_code, client_id}`. We dispatch on
        # `flow_dialect` below.
        # Constants pulled verbatim from openai/codex source
        # (codex-rs/login/src/auth/manager.rs CLIENT_ID +
        # codex-rs/login/src/server.rs DEFAULT_ISSUER +
        # codex-rs/login/src/device_code_auth.rs endpoint patterns,
        # confirmed 2026-05-28).
        "client_id":         "app_EMoamEEZ73f0CkXaXp7hrann",
        "device_code_url":   "https://auth.openai.com/api/accounts/deviceauth/usercode",
        "token_url":         "https://auth.openai.com/api/accounts/deviceauth/token",
        "scope":             "openid email profile",
        "creds_path":        "/home/privatae/.codex/credentials.json",
        "flow_dialect":      "chatgpt_deviceauth",
        "user_agent":        "codex_cli_rs/0.50.0",
    },
    "cursor": {
        "id":            "cursor",
        "name":          "Cursor",
        "vendor":        "Cursor",
        "subscription":  "Cursor Pro",
        "auth_type":     "api_key",
        "validation_url":    None,
        "creds_path":        "/home/privatae/.cursor/api_key",
        "signup_url":        "https://cursor.com/dashboard/integrations",
        "instructions":      (
            "Generate an API key at cursor.com/dashboard (Integrations → "
            "API Keys). Keys start with `crsr_`. Paste below — stored "
            "only on this pod."
        ),
    },
    "opencode": {
        "id":            "opencode",
        "name":          "opencode",
        "vendor":        "open source",
        "subscription":  "BYO OpenRouter key (pay-per-use)",
        "auth_type":     "api_key",
        "validation_url":    None,
        "creds_path":        "/home/privatae/.opencode/api_key",
        "signup_url":        "https://openrouter.ai/settings/keys",
        "instructions":      (
            "Create an API key at openrouter.ai (Settings → Keys). Keys start "
            "with `sk-or-`. Paste below — stored only on this pod. opencode "
            "runs ANY OpenRouter model per task (the open-source harness lane)."
        ),
    },
    "grok": {
        "id":            "grok",
        "name":          "Grok",
        "vendor":        "xAI",
        "subscription":  "x.ai account / Grok CLI",
        # Grok's normal user flow is `grok login --device-auth` — the
        # binary spawns its own device-code polling against xAI's
        # accounts.x.ai/oauth2/device endpoint and writes
        # ~/.grok/auth.json on success. We drive that subprocess from
        # _start_device_code (grok_cli_subprocess dialect) and watch
        # for the auth.json file in _finish_device_code. No client_id
        # or PKCE for us to manage — the binary owns the protocol.
        "auth_type":     "oauth_device_code",
        "flow_dialect":  "grok_cli_subprocess",
        "creds_path":        "/home/privatae/.grok/auth.json",
        # Path to the binary we spawn. Matches the grok sidecar's
        # CACHE_DIR/GROK_BIN. The Grok app must be launched at least
        # once first (which triggers the binary bootstrap) before the
        # connect flow can run.
        "grok_binary_path":  "/var/lib/cseries/apps/grok/bin/grok",
    },
    "kimi": {
        "id":            "kimi",
        "name":          "Kimi Code",
        "vendor":        "Moonshot AI",
        "subscription":  "BYO Kimi/Moonshot key — or any endpoint it can dial",
        "auth_type":     "api_key",
        "validation_url":    None,
        # provider_json shape (harness_core.py). The
        # credential is EITHER a bare key — auto-wrapped to the vault registry's
        # own `kimi` base_url (server/vault/providers.py), the Moonshot vendor
        # API — OR a JSON provider spec {base_url, model?, api_key?, type?} for
        # any endpoint the CLI can dial. `type` selects the wire the binary
        # speaks: kimi (default) / openai / anthropic, verified against the
        # 0.32.0 binary. That second shape is also how a managed Kimi Code
        # subscription is used here: point base_url at the coding endpoint and
        # pass the token as api_key — no platform-hosted login flow, per the
        # Keys doctrine in server/network/harness_keys.py.
        "creds_format":  "provider_json",
        "creds_path":        "/home/privatae/.kimi-code/provider.json",
        "signup_url":        "https://platform.kimi.ai",
        "instructions":      (
            "Store your Kimi (Moonshot) API key — or a JSON provider spec "
            "{\"base_url\", \"model\", \"api_key\"} to point Kimi Code at any "
            "endpoint it can reach — in the Keys app as `harness-kimi`. "
            "Stored only on this pod. Kimi Code honours the per-harness "
            "reasoning-effort setting."
        ),
    },
    "antigravity": {
        "id":            "antigravity",
        "name":          "Antigravity CLI",
        "vendor":        "Google",
        "subscription":  "Google AI Pro / Ultra (Google account) — or a Gemini API key",
        # The vendor's OWN sign-in, driven from the host in conversation: the
        # sidecar runs `agy -p …` in the user's pod, the binary prints its
        # Google OAuth URL and then waits on stdin for the code Google's page
        # shows the user (measured against agy 1.1.17); the host
        # relays that code with harness_connections op='login_code'. The grant
        # the binary writes (its file token storage,
        # $HOME/.gemini/antigravity-cli/antigravity-oauth-token) rides back as
        # rotated_creds and is stored here. No client_id, no PKCE, no token
        # endpoint on our side — the binary owns the protocol, as grok's does.
        # NOTE: Gemini CLI's Google-account sign-in stopped serving free /
        # AI Pro / AI Ultra accounts on 2026-06-18 (google-gemini/gemini-cli
        # discussion #27274); for those plans Antigravity CLI IS the vendor
        # tool, which is why this harness is agy and not gemini.
        "auth_type":     "oauth_device_code",
        "flow_dialect":  "antigravity_cli_subprocess",
        # The DIRECTORY, not one file: two lanes live here — the vendor's own
        # token (antigravity-oauth-token, written by its login) and the
        # Keys-app Gemini key (api_key, harness_keys._PRIMARY_CREDS). Naming
        # only the token file made the key lane invisible to every kernel
        # connected-check (_creds_present reads creds_path alone — fan_out's
        # pre-dispatch gate, connected_harness_ids, the matrix, op=status),
        # so a key-only user was told to sign in.
        # _creds_present already reads a directory-shaped store: any
        # non-empty regular child counts; _creds_expiry_ms is None for a dir.
        "creds_path":        "/home/privatae/.antigravity",
        "signup_url":        "https://antigravity.google/",
        "instructions":      (
            "Ask the host to connect Antigravity — it runs Google's own sign-in "
            "inside your Antigravity pod and relays the code Google shows you. "
            "Or, to run it on a Gemini API key instead of your Google account, "
            "store the key in the Keys app as `harness-antigravity`. Stored only "
            "on this pod. Antigravity honours the per-harness reasoning-effort "
            "setting (low / medium / high)."
        ),
    },
}


# ── App-installed check ────────────────────────────────────────────
async def _app_is_installed(app_id: str) -> bool:
    from server import brain_service
    pool = brain_service.get_pool()
    if pool is None:
        return False
    try:
        row = await pool.fetchrow(
            "SELECT status FROM installed_applications WHERE id=$1",
            app_id,
        )
        return bool(row and row["status"] == "active")
    except Exception:
        return False


# ── Credential presence / expiry / validity ─────────────────────────
def _creds_present(provider: dict) -> bool:
    """True iff there's a usable credential at creds_path. Handles
    both shapes: file (cursor api_key, codex auth.json, grok auth.json)
    AND directory (a CLI that writes opaque files under its own dir).
    For dirs, ANY non-empty regular child counts."""
    path = Path(provider["creds_path"])
    if not path.exists():
        return False
    try:
        if path.is_dir():
            for child in path.iterdir():
                try:
                    if child.is_file() and child.stat().st_size > 0:
                        return True
                except OSError:
                    continue
            return False
        if path.stat().st_size <= 0:
            return False
        return not _is_hollow_oauth_blob(path)
    except OSError:
        return False


def _is_hollow_oauth_blob(path: Path) -> bool:
    """A claude-code `.credentials.json` whose claudeAiOauth block carries
    NEITHER an accessToken NOR a refreshToken is a shell, not a credential:
    the harness pod's own gate (claude-code-harness/bin/main.py:_get_token →
    claude_creds.get_local_token) has nothing to send and nothing to refresh
    with, so it 401s "not connected" on every wake. Presence-only read that
    file as CONNECTED (one Host: 36 builds, 36 failures, and the matrix said
    ● connected the whole time — the resolver kept picking it). Only the
    claudeAiOauth shape is judged here; every other file (bare keys, vendor
    auth.json) stays presence-only."""
    try:
        raw = path.read_text().strip()
        if not raw.startswith("{"):
            return False
        obj = json.loads(raw)
    except Exception:
        return False
    if not isinstance(obj, dict) or "claudeAiOauth" not in obj:
        return False
    oauth = obj.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return True
    return not (str(oauth.get("accessToken") or "").strip()
                or str(oauth.get("refreshToken") or "").strip())


# ── Harness availability = CONNECTED, not dock-app-installed ──
# A coding harness is "available" to fan_out when its credentials are present,
# independent of any dock app. HARNESS_APP_IDS name the harness identities (also
# the harness image ids); each maps to a PROVIDERS entry by present_if_app_installed
# (or the provider id). This is the single source both the compare dispatch and the
# inference picker read, replacing the old registry-intersection.
def _harness_app_to_provider() -> dict:
    """Map harness app_id -> its PROVIDERS entry (by present_if_app_installed,
    falling back to the provider id when they coincide, e.g. codex/codex)."""
    m = {}
    for pid, p in PROVIDERS.items():
        app_id = p.get("present_if_app_installed") or pid
        m[app_id] = p
    return m


def connected_harness_ids(harness_app_ids) -> list:
    """Subset of harness_app_ids whose credentials are present on disk. The
    creds check is the SAME _creds_present used by the connect status portal, so
    'available to code' and 'shows connected' can never disagree."""
    prov = _harness_app_to_provider()
    out = []
    for h in harness_app_ids:
        p = prov.get(h)
        if p and _creds_present(p):
            out.append(h)
    return out


def expiry_ms_from_text(text: str) -> int | None:
    """Best-effort credential expiry (epoch ms) from credential BYTES, or None
    for keyless/unknown. Handles the three live shapes: top-level
    expires_at/expiresAt in ms (codex/claude-sdk device+pkce creds), grok's
    nested auth.json (key 'https://auth.x.ai::<uuid>' -> expires_at ISO
    string), and bare api keys (cursor) which have no expiry.

    Public because harness_keys compares a Keys-app copy against the on-disk
    file before overwriting it, and that copy is bytes in memory, never a path.
    ONE parser for both — two would drift, and the comparison is only sound if
    both sides are read the same way."""
    try:
        raw = (text or "").strip()
        if not raw or not raw.startswith("{"):
            return None  # bare api key / opaque token
        obj = json.loads(raw)
    except Exception:
        return None
    for k in ("expires_at", "expiresAt"):
        v = obj.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return int(v)
    # grok auth.json: {"https://auth.x.ai::<uuid>": {"expires_at": "<ISO>", ...}}
    for v in obj.values():
        if isinstance(v, dict) and isinstance(v.get("expires_at"), str):
            try:
                from datetime import datetime
                t = datetime.fromisoformat(v["expires_at"].replace("Z", "+00:00"))
                return int(t.timestamp() * 1000)
            except Exception:
                pass
    return None


def access_token_from_text(text: str) -> str | None:
    """The bearer token inside grok's auth.json bytes, or None.

    Shape: {"https://auth.x.ai::<uuid>": {"key": "<access token>",
    "refresh_token": ..., "expires_at": "<ISO>", ...}}. `key` is the field the
    machine-side refresh rewrites with the vendor's new access_token
    (_refresh_harness_cred_locked below), and like that refresh this reads the
    FIRST entry — so the token returned is the one the platform keeps renewed.

    Public because the vault resolver hands it to the Host's inference lane
    (server/vault/providers.resolve_signin) from the SEALED copy of these bytes,
    never from the file. Anything else — a bare api key, junk, a codex/claude
    shape — is None: only grok's login is wired to serve model calls."""
    try:
        raw = (text or "").strip()
        if not raw.startswith("{"):
            return None
        obj = json.loads(raw)
        entry = next(iter(obj.values()), None) if isinstance(obj, dict) else None
    except Exception:
        return None
    if not isinstance(entry, dict):
        return None
    tok = entry.get("key")
    return tok.strip() if isinstance(tok, str) and tok.strip() else None


def _creds_expiry_ms(path: Path) -> int | None:
    """expiry_ms_from_text for a credential FILE. None when unreadable."""
    try:
        if path.is_dir():
            return None
        return expiry_ms_from_text(path.read_text())
    except Exception:
        return None


def _creds_status(provider: dict) -> dict:
    """Validity-aware status: presence is necessary but NOT sufficient — an
    expired/revoked-but-still-on-disk credential is present yet invalid. This
    is the truth the Connections UI + the pre-dispatch check read (the old
    presence-only gate is why a dead grok token showed as 'connected')."""
    if not _creds_present(provider):
        return {"present": False, "valid": False, "expires_at": None}
    exp = _creds_expiry_ms(Path(provider["creds_path"]))
    now_ms = int(time.time() * 1000)
    return {"present": True, "valid": (exp is None or exp > now_ms), "expires_at": exp}


def harness_validity(harness_id: str) -> dict:
    """Public: {present, valid, expires_at} for a harness id (review.py +
    the harness-health bot use this for the pre-dispatch check + the sweep)."""
    p = PROVIDERS.get(harness_id)
    return _creds_status(p) if p else {"present": False, "valid": False, "expires_at": None}


# ── Refreshable-token maintenance ────────────────────────────────────
# The last SUCCESSFUL read of this pod's refresh gate. A pod serves exactly one
# entity, so one slot is the whole cache. None = never read.
_LAST_GATE_READ: bool | None = None

# Per-harness refresh serialisation + failure backoff. Two callers race for the
# same grant — the 600s maintenance loop and fan_out's pre-dispatch check
# (review.py) — and both fire on exactly the same condition, so without the lock
# the loser POSTs a refresh token the winner has already consumed. x.ai ROTATES
# refresh tokens, so a double-spend is not merely wasted, it can revoke.
_refresh_locks: dict = {}
# {harness_id: (retry_not_before_epoch, consecutive_failures)}. A revoked grant
# is unfixable by retrying, and without this the loop POSTs to the vendor every
# 600s forever (~144 calls/day) from the entity's named egress address.
_refresh_backoff: dict = {}
_REFRESH_BACKOFF_BASE_S = 600      # one loop interval
_REFRESH_BACKOFF_MAX_S = 6 * 3600  # ~4 attempts/day on a permanently dead grant
# Harnesses we have already told the user about THIS outage. Cleared on the
# first success, so the next outage notifies again but one outage never does.
_refresh_notified: set = set()


def _expiring_within(st: dict, margin_s: float) -> bool:
    """Is this credential invalid, or valid but inside `margin_s` of expiry?
    The nearness test the maintenance loop and the in-lock short-circuit must
    agree on — if they disagree, a proactive refresh is silently swallowed."""
    if not st.get("valid"):
        return True
    if margin_s <= 0:
        return False
    import time as _t
    exp_ms = st.get("expires_at")
    if not isinstance(exp_ms, (int, float)):
        return False          # no expiry to be near (bare key)
    return exp_ms / 1000 - _t.time() < margin_s


def _backoff_blocked(harness_id: str, now: float) -> bool:
    not_before, _fails = _refresh_backoff.get(harness_id, (0.0, 0))
    return now < not_before


def _note_refresh_result(harness_id: str, ok: bool, now: float) -> int:
    """Clear the backoff on success, widen it on failure. Returns the new
    consecutive-failure count (0 on success)."""
    if ok:
        _refresh_backoff.pop(harness_id, None)
        return 0
    _nb, fails = _refresh_backoff.get(harness_id, (0.0, 0))
    fails += 1
    delay = min(_REFRESH_BACKOFF_BASE_S * (2 ** (fails - 1)), _REFRESH_BACKOFF_MAX_S)
    _refresh_backoff[harness_id] = (now + delay, fails)
    return fails


async def platform_refresh_enabled() -> bool:
    """Whether the platform keeps this entity's refreshable VENDOR harness tokens
    alive on its own timer. ON FOR EVERY HOST, reversing an earlier
    default: renewing a grant the user already minted is what every
    comparable platform does for its users, so the platform does it too.

    What the old default assumed, and why it did not hold: it deferred to the
    vendor CLI self-refreshing IN-POD on the user's rented compute. The harness
    Deployments are scale-to-zero, so between runs there is no CLI process in the
    pod and nothing can renew anything. The x.ai grant runs exactly 6h
    (measured), and the seal/restore cycle means a pod can boot
    holding one already part-spent — one Host's came back with 1h55m left, died,
    and sat dead. The Orchestrator's Grok row reads expires_at, so it showed
    `expired` until a hand reconnect: the whole 'grok won't stay connected' loop.

    Refresh works from a fully EXPIRED access token — verified live on one Host
    2h35m past expiry, so enabling this never costs the user a re-auth.

    A user can still opt OUT per entity with
    behavioral_params.platform_token_refresh=false. An ABSENT key means ON.
    Fails OPEN (enabled) on any read error — ON is now the default, so a
    transient DB blip must not silently stop renewing a host's tokens.

    NOTE: the HOST's OWN SDK engine token (its own chat) was already exempt and
    stays auto-refreshed (server/main.py _sdk_token_refresher). This gate is only
    for the rented vendor harnesses handled in this module."""
    global _LAST_GATE_READ
    try:
        from server import brain_service
        pool = brain_service.get_pool()
        eid = os.environ.get("ENTITY_ID", "")
        if pool is None or not eid.isdigit():
            return True if _LAST_GATE_READ is None else _LAST_GATE_READ
        row = await pool.fetchrow(
            "SELECT behavioral_params FROM entity_registry WHERE id=$1", int(eid))
        bp = (row and row["behavioral_params"]) or {}
        if isinstance(bp, str):
            bp = json.loads(bp)
        val = bp.get("platform_token_refresh") if isinstance(bp, dict) else None
        if val is None:
            enabled = True
        elif isinstance(val, str):
            enabled = val.strip().lower() not in ("false", "0", "no", "off")
        else:
            enabled = bool(val)
        _LAST_GATE_READ = enabled
        return enabled
    except Exception:
        # Fail open, but never OVER an opt-out we have already seen: once this
        # entity's flag has been read successfully, a later transient DB error
        # must not resurrect platform-side renewal for a user who turned it off.
        # Only a gate that has NEVER been read falls back to the ON default.
        return True if _LAST_GATE_READ is None else _LAST_GATE_READ


async def maintain_refreshable_creds(within_s: int = 3600) -> dict:
    """Proactively refresh vendor harness OAuth tokens before expiry. ON for
    every host (platform_refresh_enabled; default TRUE) — the
    scale-to-zero harness pods cannot self-refresh between runs, so without this
    a short-lived vendor grant simply dies. Best-effort; never raises."""
    if not await platform_refresh_enabled():
        return {"disabled": "platform_token_refresh=false on this host — the "
                            "user opted out of platform-side renewal"}
    import time as _t
    now = _t.time()
    out: dict = {}
    for provider in ("grok",):  # the refreshable-token harness (claude self-refreshes in-pod)
        try:
            st = harness_validity(provider)
        except Exception:
            continue
        if not st.get("present"):
            continue
        exp_ms = st.get("expires_at")
        near = (not st.get("valid")) or (
            isinstance(exp_ms, (int, float)) and exp_ms / 1000 - now < within_s)
        if not near:
            continue
        if _backoff_blocked(provider, now):
            out[provider] = "backoff"
            continue
        try:
            # margin_s = this call's nearness window. Without it the in-lock
            # "already valid" short-circuit would swallow every PROACTIVE
            # refresh — the whole point of `within_s` is to renew a token that
            # is still valid, so "valid" alone must not count as done here.
            ok = await refresh_harness_cred(provider, margin_s=within_s)
            fails = _note_refresh_result(provider, ok, now)
            out[provider] = "refreshed" if ok else f"refresh_failed({fails})"
            # A grant that is DEAD and will not refresh needs the user, and this
            # loop is the only thing watching it between runs — say so once per
            # outage rather than retrying into the void. fan_out surfaces its
            # own skip; without this a user who never dispatches hears nothing.
            # Latched on the OUTAGE, not on fails==1: a proactive refresh can
            # fail while the token is still valid, and then the failure count is
            # already past 1 by the time it actually expires.
            if not ok and not st.get("valid"):
                if provider not in _refresh_notified:
                    _refresh_notified.add(provider)
                    await notify_harness_auth(provider, "expired")
            elif ok:
                _refresh_notified.discard(provider)
        except Exception as e:  # pragma: no cover - defensive
            _note_refresh_result(provider, False, now)
            out[provider] = f"error:{e!r}"[:60]
    return out


async def run_cred_maintenance_loop(interval_s: int = 600) -> None:
    """Background loop: keep refreshable harness tokens fresh. Dispatched from
    the server lifespan next to the seal sweep."""
    while True:
        try:
            res = await maintain_refreshable_creds()
            if res:
                log.info("harness cred maintenance: %s", res)
        except Exception as e:  # pragma: no cover - defensive
            log.info("cred maintenance loop error: %r", e)
        await asyncio.sleep(interval_s)


async def refresh_harness_cred(harness_id: str, *, margin_s: float = 0.0) -> bool:
    """Machine-side OAuth refresh for harnesses whose stored grant carries a
    refresh token: OAuth tokens refresh automatically where
    possible. claude-sdk already self-refreshes (nac_harness + in-pod); grok
    is the other refreshable one: ~/.grok/auth.json carries refresh_token +
    oidc_issuer/client_id. Refresh via the issuer token endpoint and rewrite
    the file atomically — expires_at MUST stay RFC3339 (the Go CLI unmarshals
    time.Time; an epoch int reads as corrupt → interactive login → the run
    hangs to the cap; seen live). Re-seals the vault after (x.ai
    ROTATES refresh tokens — an unsealed rotation is the same grant-loss the
    claude write-back fixed). Returns True iff the file now holds a fresh
    grant. Best-effort, never raises.

    Serialised per harness: the maintenance loop and fan_out's pre-dispatch
    check fire on the same condition, and x.ai rotates refresh tokens, so a
    concurrent second POST would spend a token the winner already consumed.
    The waiter re-checks inside the lock and returns the winner's result
    instead of dialling again.

    `margin_s` is the CALLER's nearness window, and it must match the reason
    the caller decided to refresh. The maintenance loop renews a token that is
    still valid but close to expiry, so for it "valid" is NOT done — passing
    within_s makes the in-lock check agree with the loop's own test. fan_out
    only calls on an invalid grant, so its default 0 is right: there, a token
    that reads valid inside the lock really was fixed by the winner."""
    if harness_id != "grok":
        return False

    lock = _refresh_locks.get(harness_id)
    if lock is None:
        lock = _refresh_locks.setdefault(harness_id, asyncio.Lock())
    async with lock:
        if not _expiring_within(harness_validity(harness_id), margin_s):
            # Someone refreshed it while we waited — that IS the success.
            return True
        return await _refresh_harness_cred_locked(harness_id)


async def _refresh_harness_cred_locked(harness_id: str) -> bool:
    """The refresh itself. Callers must hold the per-harness lock."""

    def _do() -> bool:
        import datetime as _dt
        import tempfile as _tf
        import urllib.parse as _up
        import urllib.request as _ur
        path = Path("/home/privatae/.grok/auth.json")
        try:
            data = json.loads(path.read_text())
            key = next(iter(data))
            entry = data[key]
            issuer = (entry.get("oidc_issuer") or "").rstrip("/")
            cid = entry.get("oidc_client_id")
            rt = entry.get("refresh_token")
            if not (issuer and cid and rt):
                return False
            with _ur.urlopen(issuer + "/.well-known/openid-configuration",
                             timeout=15) as r:
                te = json.loads(r.read())["token_endpoint"]
            body = _up.urlencode({"grant_type": "refresh_token",
                                  "refresh_token": rt,
                                  "client_id": cid}).encode()
            req = _ur.Request(te, data=body, method="POST")
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            with _ur.urlopen(req, timeout=20) as r:
                new = json.loads(r.read())
            entry["key"] = new["access_token"]
            if new.get("refresh_token"):
                entry["refresh_token"] = new["refresh_token"]
            if new.get("expires_in"):
                exp = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(
                    seconds=int(new["expires_in"]))
                entry["expires_at"] = exp.strftime("%Y-%m-%dT%H:%M:%S.%f000Z")
            data[key] = entry
            fd, tmp = _tf.mkstemp(dir=str(path.parent))
            os.write(fd, json.dumps(data).encode())
            os.close(fd)
            os.chmod(tmp, 0o600)
            os.replace(tmp, str(path))
            return True
        except Exception as e:  # noqa: BLE001 - refresh is best-effort
            log.info("grok token refresh failed: %r", e)
            return False

    ok = await asyncio.to_thread(_do)
    if ok:
        log.info("grok token refreshed machine-side (validity gate)")
        try:
            from server.routes import harness_vault as _hv
            await _hv.seal_harness_cred("grok", actor="harness:auto-refresh")
        except Exception as e:  # noqa: BLE001
            log.info("grok post-refresh vault seal failed: %r", e)
    return ok


async def notify_harness_auth(harness_id: str, reason: str = "expired") -> None:
    """Inbox notification (durable, has email) when a harness credential is
    expired/invalid, so the user sees 'reconnect <harness>' in their alerts
    surface — not buried in a failed run. Mirrors _notify_sdk_auth_expired.
    Best-effort; the dispatch error still surfaces in session state regardless."""
    name = (PROVIDERS.get(harness_id) or {}).get("name", harness_id)
    try:
        from server.main import get_bridge_client
        bc = get_bridge_client()
        if bc is None or not getattr(bc, "_connected", False):
            return
        await bc.request_full("inbox.send", {
            "subject": f"Reconnect {name} — credentials {reason}",
            "body": (f"Your **{name}** harness credentials are {reason}, so it "
                     f"can't run coding tasks right now. Add or update the "
                     f"**{name}** key in the **Keys** app to restore it."),
            "body_format": "text/markdown",
            "kind": "system_notification",
            "payload": {"reason": f"harness_auth_{reason}", "harness": harness_id},
        })
    except Exception:
        pass
