"""
Host pull loop — rotate the App SDK from the registry (App SDK Phase 0, 0d.3).

Periodically asks the bridge for the current published SDK (`sdk.current`), and if it
is a strictly newer version than what's installed, installs it. Verification is LOCAL:
`sdk_store.install` checks the Privatae Ed25519 signature before writing, so a bridge
that relayed a bad bundle can't poison the host. The image-baked floor remains the
offline anchor; this loop only ever moves the host FORWARD to a newer signed SDK.

Degrades to the installed SDK on any failure (bridge down, op missing, bad bundle) —
never raises, never downgrades. Until the bridge serves `sdk.current` (0d.2 deploy),
this loop is a harmless no-op; it lights up the moment the rebuilt bridge answers.

Cadence: a modest 1h poll (SDK changes are rare; a security fix reaches every host
within the hour at negligible bridge load). A bridge-initiated push could later make
rotation near-instant, but the poll is the floor.
"""

from __future__ import annotations

import asyncio
import base64
import logging

from server.applications import sdk_store

log = logging.getLogger("privatae.applications.sdk_pull")

STARTUP_DELAY_SECONDS = 120
INTERVAL_SECONDS = 3600


def _is_newer(remote: str | None, local: str | None) -> bool:
    """True if semver `remote` is strictly greater than `local`. Conservative —
    unparseable versions sort as (0,), so a malformed remote never wins."""
    def parse(v: str | None) -> tuple:
        try:
            return tuple(int(x) for x in (v or "0").split("."))
        except Exception:
            return (0,)
    return parse(remote) > parse(local)


async def _pull_once(bc) -> None:
    if bc is None:
        return
    try:
        data = await bc.request("sdk.current", {})
    except Exception as e:
        log.debug("sdk.current pull skipped (keeping installed SDK): %s", e)
        return
    if not data or not data.get("bundle_b64"):
        return
    remote_version = data.get("version")
    remote_hash = data.get("sdk_hash")

    try:
        cur = sdk_store.current()
        if cur.get("hash") == remote_hash:
            return  # already running this exact SDK
        if not _is_newer(remote_version, cur.get("version")):
            return  # never downgrade / no-op on same-or-older
    except sdk_store.SdkStoreError:
        pass  # nothing installed yet — install whatever verifies

    try:
        bundle = base64.b64decode(data["bundle_b64"])
        meta = sdk_store.install(bundle)   # verifies the Privatae signature; raises on bad
        log.info(
            "App SDK rotated from registry → v%s (hash %s)",
            meta["version"], meta["hash"][:12],
        )
    except Exception as e:
        log.error("App SDK pull-install REFUSED (kept current SDK): %s", e)


async def run_loop(bridge_client) -> None:
    """Background task spawned from lifespan. Waits for the bridge to settle, then
    polls for a newer signed SDK forever. Best-effort — never fatal."""
    try:
        await asyncio.sleep(STARTUP_DELAY_SECONDS)
    except asyncio.CancelledError:
        return
    while True:
        try:
            await _pull_once(bridge_client)
        except Exception as e:
            log.warning("sdk_pull loop error: %s", e)
        try:
            await asyncio.sleep(INTERVAL_SECONDS)
        except asyncio.CancelledError:
            return
