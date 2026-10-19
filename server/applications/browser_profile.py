"""The Browser app's profile — cookies + localStorage, sealed in the vault.

WHY THIS EXISTS. The browser harness pod scales to zero when nobody is looking at
it, and its context state is RAM-only, so every visit used to be a brand-new
device. A bank re-challenges a device it has never seen, every single time. That
gate is INDEPENDENT of which address the request came from: v127's egress pin
makes a site see the user's home IP and does nothing at all about device
recognition. A working bank login needs both; neither is sufficient alone.

WHERE THE BYTES LIVE, and why not anywhere easier. A site session cookie is a
BEARER credential — whoever holds it IS the user, with no password and no second
factor — so this is the vault, sealed to the host's key, and never the entity PVC.
That is the judgement control/sessions.py already made for recipe sessions ("in
RAM here, sealed in the vault there") and the reason browser 0.4.0 deleted the
on-PVC blob its predecessor wrote.

It is also a deliberate REVERSAL of "the shared page always starts clean", which
0.4.0 introduced. That rule was right while nothing could hold the state safely;
it is the wrong trade once the vault can, because its cost is that the user can
never stay signed in to anything. The reversal is bounded:

  * OPT-IN. Nothing is kept until the user turns it on. With the switch off this
    module does nothing at all and the browser behaves exactly as it does today.
  * SIGN OUT drops the live context AND revokes the sealed copy — in that order,
    so a half-failure never leaves the sealed copy gone while the live session is
    still signed in.
  * Nothing here reads a cookie. The blob is opaque to this module: it comes out
    of the sidecar, goes into an envelope, and comes back. `status()` says whether
    one exists, never what is in it.

The layering, unchanged from sessions.py — the sidecar cannot reach the vault (no
host key, no DB pool), so the kernel is the only place this can live:

    vault (durable, sealed)  <->  kernel (holds the key + the pool)
                                       |  supplies on wake, collects on release
                                       v
                                 sidecar (in-memory only)
"""
from __future__ import annotations

import json
import logging
from typing import Optional

log = logging.getLogger("cseries.browser_profile")

#: The vault logical name. One profile per host — the Browser app is one shared
#: page, so there is exactly one of these.
VAULT_NAME = "browser:profile"
#: Descriptive only; the vault does not branch on it. "opaque" is what every
#: non-API-key blob uses.
CRED_TYPE = "opaque"
#: kernel_secrets id marking "the user turned this on". A preference, not a
#: credential, and it has to be readable before any profile exists.
#: (kernel_secrets is id TEXT PK + secret BYTEA NOT NULL — tokens.py:50-54.)
ENABLED_KEY = "browser_profile_enabled"

_HTTP_TIMEOUT_S = 15.0


async def _sidecar(method: str, path: str, json_body: Optional[dict] = None):
    """One call to the browser sidecar's control API, or None on any failure.

    Address and auth header come from server.vault.authz — the same pair every
    other kernel->browser call uses — so this cannot drift onto a different host
    or miss the token. browser_base_url() re-resolves per call on purpose: the
    harness scales to zero and its Service host is only known after a wake."""
    import aiohttp
    from server.vault.authz import browser_base_url, browser_headers
    base = browser_base_url()
    if not base:
        return None
    try:
        async with aiohttp.ClientSession() as s:
            async with s.request(
                method, base.rstrip("/") + path, json=json_body,
                headers=browser_headers(),
                timeout=aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_S),
            ) as r:
                if r.status >= 400:
                    log.info("browser profile: %s %s -> %s", method, path, r.status)
                    return None
                if r.content_type == "application/json":
                    # The browser pod's storage_state: capped and
                    # value-budgeted (server/harness_input.py, 2026-09-28).
                    from server import harness_input as _hin
                    return await _hin.read_json(r, _PROFILE_JSON_MAX, "browser profile")
                return {}
    except Exception as e:  # noqa: BLE001
        log.info("browser profile: %s %s failed: %r", method, path, e)
        return None


# The most of a storage_state (cookies and each origin's localStorage) read
# back from the browser pod. json.loads holds the GIL for the whole parse, so
# this is a loop-time bound too: 64 MiB stalled the loop 143-159 ms, 16 MiB
# of it 65 ms. Real storage states are kilobytes to a few MB.
_PROFILE_JSON_MAX = 16 * 1024 * 1024


async def is_enabled(pool) -> bool:
    """Has the user turned profile-keeping on? Default FALSE, and that default is
    the whole safety of the feature: a host that never opts in keeps nothing and
    gets the pre-existing always-clean behaviour. A read failure is False too —
    failing to keep a credential is the safe direction to fail in."""
    if pool is None:
        return False
    try:
        row = await pool.fetchrow(
            "SELECT 1 FROM kernel_secrets WHERE id = $1", ENABLED_KEY)
        return row is not None
    except Exception:
        return False


async def set_enabled(pool, entity_id: int, enabled: bool) -> bool:
    """Turn profile-keeping on or off.

    Turning it OFF also forgets what is stored. A switch that leaves the
    credential sealed behind it is not an off switch — the user would read "off"
    while their bank session sat in the vault."""
    if pool is None:
        return False
    from server.applications import tokens
    await tokens.get_secret(pool)      # ensures the kernel_secrets schema exists
    if enabled:
        await pool.execute(
            "INSERT INTO kernel_secrets (id, secret) VALUES ($1, $2) "
            "ON CONFLICT (id) DO NOTHING", ENABLED_KEY, b"1")
        return True
    await pool.execute("DELETE FROM kernel_secrets WHERE id = $1", ENABLED_KEY)
    await forget(pool, entity_id, only_sealed=True)
    return False


async def save(pool, entity_id: int) -> bool:
    """Collect the live profile from the sidecar and seal it. Called on release.

    True only when a blob was actually sealed. Every failure is soft and logged:
    losing a profile costs the user one re-login, and nothing here is worth
    failing a release over."""
    if not await is_enabled(pool):
        return False
    got = await _sidecar("GET", "/profile")
    if not got or not isinstance(got.get("storage_state"), dict):
        return False
    st = got["storage_state"]
    # An empty state is not worth sealing, and sealing it would OVERWRITE a good
    # profile with nothing — which is exactly how an idle reap of a browser
    # nobody had signed into could silently sign the user out of everything.
    if not st.get("cookies") and not st.get("origins"):
        log.info("browser profile: nothing to keep (empty state) — leaving the "
                 "sealed copy alone")
        return False
    # THE EMPTY CHECK IS NOT ENOUGH ON ITS OWN, because "not empty" and "is the
    # user's profile" are different things. If restore() failed soft on the cold
    # wake — a 15s timeout, a 503 while the sidecar was still booting, a refused
    # connection — the context came up CLEAN. The user then opens any site at all,
    # a cookie is set, and this state is no longer empty. Sealing it supersedes
    # the envelope holding their bank login, and they are signed out of everything
    # with nothing to point at.
    #
    # `loaded` is the sidecar telling us whether THIS context was built from a
    # restored profile, which is exactly the question. It is only safe to
    # overwrite a kept profile with a context that came from that profile; a
    # never-restored context may only CREATE the first one.
    if not got.get("loaded") and await _has_sealed(pool, entity_id):
        log.warning("browser profile: this browser was not restored from the kept "
                    "profile, so its state is not that profile — refusing to "
                    "overwrite. The kept sign-ins are intact; the next wake will "
                    "restore them.")
        return False
    try:
        from server.vault import api as vault_api
        await vault_api.store_secret(
            pool, entity_id=entity_id, name=VAULT_NAME, cred_type=CRED_TYPE,
            plaintext=json.dumps(st).encode("utf-8"), actor="browser-profile")
        log.info("browser profile sealed (%d cookies)", len(st.get("cookies") or []))
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("browser profile: seal failed (%r) — the user will be asked to "
                    "sign in again next time", e)
        return False


async def restore(pool, entity_id: int) -> bool:
    """Open the sealed profile and hand it to the sidecar. Called on a cold wake.

    True when a profile was restored. False covers every ordinary case — switched
    off, nothing stored yet, sidecar not up — none of which is an error."""
    if not await is_enabled(pool):
        return False
    try:
        from server.vault import api as vault_api
        raw = await vault_api.open_secret(
            pool, entity_id=entity_id, name=VAULT_NAME, actor="browser-profile")
    except Exception as e:  # noqa: BLE001
        log.warning("browser profile: unseal failed: %r", e)
        return False
    if not raw:
        return False
    try:
        st = json.loads(raw.decode("utf-8"))
    except Exception:
        log.warning("browser profile: the sealed blob is not readable JSON — ignoring it")
        return False
    if await _sidecar("POST", "/profile", {"storage_state": st}) is None:
        return False
    log.info("browser profile restored into the sidecar")
    return True


async def forget(pool, entity_id: int, only_sealed: bool = False) -> bool:
    """Sign out: drop the live context, THEN revoke the sealed copy.

    THAT ORDER MATTERS. Revoking the seal first and then failing to clear the live
    context leaves the user signed in with nothing left to revoke — the worst of
    both. Clearing the live context first means a failure at the second step is
    recoverable: the kept copy is still there and the next wake restores it.

    `only_sealed` is the switch-off path, where the live context is being torn
    down anyway. The envelope is SUPERSEDED, not deleted — vault rows are kept for
    audit (store.revoke) and a superseded envelope reads as absent everywhere."""
    if not only_sealed:
        # The result MATTERS, and ignoring it undoes the whole ordering argument.
        # If the browser is awake and the clear FAILED, the live context is still
        # signed in — revoking now would delete the seal, leave the session live,
        # and the next release would simply re-seal it. Sign-out silently undone.
        # So: refuse, and say so. If the browser is not awake there is no live
        # context to clear, and revoking is the whole job.
        awake = False
        try:
            from server.applications import harness_backend
            awake = bool(harness_backend.upstream_host("browser"))
        except Exception:
            awake = False
        cleared = await _sidecar("DELETE", "/profile")
        if awake and cleared is None:
            log.warning("browser profile: could not clear the live browser, so the "
                        "sealed copy was NOT revoked — signing out halfway would "
                        "leave the session live with nothing left to revoke")
            return False
    try:
        from server.vault import identity as vault_identity
        from server.vault import store as vault_store
        await vault_store.revoke(
            pool, owner_identity=vault_identity.host_identity(entity_id),
            logical_name=VAULT_NAME, actor="browser-profile")
    except Exception as e:  # noqa: BLE001
        log.info("browser profile: nothing sealed to forget (%r)", e)
    return True


async def _has_sealed(pool, entity_id: int) -> bool:
    """Is there an active sealed profile? WITHOUT unsealing it.

    store.cred_type_of answers "does an active envelope of this name exist" from
    the row, where open_secret would decrypt the credential just to learn it is
    there. A status poll has no business holding the user's cookies in memory."""
    try:
        from server.vault import identity as vault_identity
        from server.vault import store as vault_store
        return bool(await vault_store.cred_type_of(
            pool, owner_identity=vault_identity.host_identity(entity_id),
            logical_name=VAULT_NAME))
    except Exception:
        return False


async def status(pool, entity_id: int) -> dict:
    """What the Browser app shows: whether keeping is on and whether anything is
    kept. Never the contents — this module does not read cookies, and a status
    surface that did would become a place to read them from."""
    enabled = await is_enabled(pool)
    kept = bool(enabled and await _has_sealed(pool, entity_id))
    return {"enabled": enabled, "kept": kept}
