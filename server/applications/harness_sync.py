"""Harness reconcile at boot — every harness-backed app (the Browser, anything
whose manifest declares runtime.harness) needs its harness to EXIST on every
entity that has the app installed.

This was once the VS Code editor only, by id, and so was the
install-time hook: the Browser app declares `harness: browser`, was installed from the
store on a Host, and had no Deployment behind it — every open died with
"no harness-browser Deployment … is 'browser' in Entity.spec.harnesses?".
Now the predicate is the manifest's own key, here and at install.

`installer._sync_app_harness` fires at INSTALL time (installer.py,
`install_bundle` → `_sync_app_harness(manifest.runtime.harness, True)`) and is best-effort:
at boot the bridge is usually not connected yet, so the sync logs "skipped" and
is never retried. Seen once: 2 of 6 running entities carried
`spec.harnesses: []` while the editor app was installed fleet-wide, and one
entity's whole boot log had no `harness sync` line at all — the install-time
hook simply never ran in a boot where the bridge was up.

The operator's enable is idempotent (the controller's
`set_entity_harness`: appends the id only when it is
absent and PATCHes the list as-is otherwise; a same-content merge patch is a
no-op at the API server), so re-asserting on every boot costs one no-op PATCH for
a synced entity and provisions the harness (Deployment@0 + Service + workspace
PVC — never a roll of the entity pod) for one that was missed.

Same shape as default_install.run_loop: spawned from lifespan, waits for the
bridge to settle, retries on bridge_not_ready, then exits.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from server import brain_service
from server.applications import installer, registry

if TYPE_CHECKING:
    from server.bridge.bridge_client import BridgeClient

log = logging.getLogger("privatae.applications.harness_sync")

STARTUP_DELAY_SECONDS = 45   # after default_install's 30 s, so a first-boot install lands first
# Retried while the bridge is down or a send failed, for as long as it takes:
# RETRY_SECONDS, doubling up to RETRY_MAX_SECONDS. It never gives up — the
# fixed 12 x 300 s it replaced left a harness un-asserted until the
# next restart when the bridge was down for over an hour.
RETRY_SECONDS = 300
RETRY_MAX_SECONDS = 3600


def retry_wait(attempts: int) -> float:
    return float(min(RETRY_SECONDS * (2 ** min(max(0, int(attempts) - 1), 32)), RETRY_MAX_SECONDS))


async def run_once(bc: "BridgeClient | None") -> dict:
    """One reconcile pass. Returns a status dict:
    pool_not_ready | bridge_not_ready | not_installed | synced | sync_failed.

    Three lanes, same boot-gap reason:
      * every installed app whose manifest declares runtime.harness;
      * every installed app whose manifest declares runtime.kind=helper —
        the app-runtime descriptors,
        whose install-time write is best-effort against the same
        bridge-not-up-yet window;
      * every coding engine connected by key (harness_keys
        .connected_coding_harnesses), whose connect-time provisioning is the
        same best-effort send. The operator's upsert is idempotent, so
        re-asserting costs a no-op PATCH per synced app or engine."""
    pool = brain_service.get_pool()
    if pool is None:
        return {"status": "pool_not_ready"}
    if bc is None or not getattr(bc, "_connected", False):
        return {"status": "bridge_not_ready"}
    results: dict[str, bool] = {}
    # Harness-backed apps: every active install whose manifest declares
    # runtime.harness gets its harness re-asserted (the operator's upsert is
    # idempotent). This was the hardcoded VS Code id only, which is why a
    # Browser app whose install-time sync was missed stayed without a
    # Deployment across every boot.
    # App-runtime descriptors: every active install that runs in its own pod
    # (helper apps and image-declaring process apps — manifest.runs_in_pod is
    # the one placement predicate). Manifest read is per-app fail-soft — one
    # unreadable app must not strand the others.
    for row in await registry.list_active(pool):
        try:
            from server.applications.manifest import load as _load, runs_in_pod
            m = _load(installer.INSTALL_ROOT / row.id / "app.yaml")
            _h = getattr(m.runtime, "harness", None)
            if _h:
                # Its own key: an app that declares BOTH a harness and a
                # pod runtime must not have a failed set_harness masked by
                # a successful descriptor sync under the same name.
                results[f"{row.id}:harness"] = await installer._sync_app_harness(
                    _h, True, bc=bc)
            if runs_in_pod(m):
                results[row.id] = await installer.sync_app_runtime(
                    row.id, True, bc=bc)
        except installer.AppRuntimeOverPlan as e:
            # A refusal, not a blip: this app is installed and the plan cannot
            # hold its reservation, so it will not run until something changes.
            # WARNING, never debug — the same "installed and quietly dead"
            # shape the install path now refuses out loud.
            log.warning("app-rt boot reconcile REFUSED %s: %s", row.id, e)
            results[row.id] = False
        except Exception as e:
            log.debug("app-rt boot reconcile skipped %s: %s", row.id, e)
    # Coding engines connected by key (harness_keys._PRIMARY_CREDS). Their
    # connect-time provisioning is the same best-effort send, so a connect
    # the bridge was not up for — or a key that outlived a re-created Entity
    # on the pod's volume (a test Host: spec.harnesses held
    # only vscode, every build "no harness-opencode Deployment") — left the
    # engine connected and unrunnable until its key changed. Re-asserted here
    # like the apps above; the operator's upsert is idempotent.
    try:
        from server.network.harness_keys import connected_coding_harnesses
        for hid in await connected_coding_harnesses():
            results[f"{hid}:coding"] = await installer._sync_app_harness(hid, True, bc=bc)
    except Exception as e:
        log.debug("coding-engine boot reconcile skipped: %s", e)
    if not results:
        return {"status": "not_installed"}
    failed = [k for k, ok in results.items() if not ok]
    return {"status": "sync_failed" if failed else "synced",
            "apps": sorted(results), "failed": sorted(failed)}


async def run_loop(bc: "BridgeClient | None") -> None:
    """Background task spawned from lifespan. Waits for the bridge to settle, then
    re-asserts every installed app's harness and runtime once; retries with a doubling wait while the
    bridge is down or the send failed, never giving up; ends as soon as there is
    nothing to do (no PG, app not installed, or synced)."""
    try:
        await asyncio.sleep(STARTUP_DELAY_SECONDS)
    except asyncio.CancelledError:
        return

    attempts = 0
    while True:
        attempts += 1
        try:
            result = await run_once(bc)
        except asyncio.CancelledError:
            return
        except Exception:
            wait = retry_wait(attempts)
            log.exception("harness reconcile attempt %d crashed — retrying in %.0fs",
                          attempts, wait)
            await asyncio.sleep(wait)
            continue
        status = result.get("status")
        if status in ("bridge_not_ready", "sync_failed"):
            wait = retry_wait(attempts)
            log.info("harness reconcile attempt %d: %s — retrying in %.0fs",
                     attempts, status, wait)
            await asyncio.sleep(wait)
            continue
        log.info("harness reconcile: %s (attempt %d)", result, attempts)
        return
