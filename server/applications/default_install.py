"""
First-boot default-install bootstrap.

The image is kernel-only: only KERNEL_REQUIRED_APPS ship baked
in. Everything else lives in the central app store and is fetched at
first boot.

This module runs in a background task after the bridge connects. It:

  1. Checks whether the local registry already has at least one
     non-native app installed. If yes, this is an upgrade from an
     older image — skip.
  2. Otherwise: fetches the catalog via ``appstore.list``, filters to
     rows with ``is_default = true``, and installs each via the
     standard install pipeline.

Failures are logged but never raise. A missing default-set install is
a degraded experience (user gets fewer apps), not a refuse-to-boot
condition. The user can always install via the App Store manually.

"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from server import brain_service
from server.applications import installer, registry
from server.applications.kernel import KERNEL_REQUIRED_APPS

if TYPE_CHECKING:
    from server.bridge.bridge_client import BridgeClient


log = logging.getLogger("privatae.applications.default_install")

STARTUP_DELAY_SECONDS = 30  # let the bridge connect

# Per-entity record of default app ids this entity has ALREADY been
# offered (auto-installed at least once). Lives in the persistent
# workspace so it survives pod restarts. An app id here is never
# auto-installed again — so a user who uninstalls a default app does not
# get it resurrected on the next boot. A genuinely new default app (not
# in this set) installs once, then joins it.
_OFFERED_MARKER = Path("/var/lib/cseries/.default-apps-offered.json")


def _load_offered() -> set[str]:
    try:
        return set(json.loads(_OFFERED_MARKER.read_text()))
    except Exception:
        return set()


def _save_offered(offered: set[str]) -> None:
    try:
        _OFFERED_MARKER.parent.mkdir(parents=True, exist_ok=True)
        _OFFERED_MARKER.write_text(json.dumps(sorted(offered)))
    except Exception:
        log.warning("could not persist default-apps-offered marker", exc_info=True)


def _retired_app_ids() -> frozenset[str]:
    """kernel.RETIRED_APPS, read at call time (see installer._retired_app_ids)."""
    from server.applications.kernel import RETIRED_APPS
    return RETIRED_APPS


async def _has_non_native_installs(pool) -> bool:
    rows = await registry.list_active(pool)
    for r in rows:
        if r.id not in KERNEL_REQUIRED_APPS:
            return True
    return False


async def _install_default_set(bc: "BridgeClient", pool) -> dict:
    """Pull catalog, install every default app not already present."""
    try:
        envelope = await bc.request_full("appstore.list", {})
    except Exception as exc:
        return {"status": "store_unreachable", "error": str(exc)}
    if not envelope.get("ok"):
        return {"status": "store_error", "error": envelope.get("error")}
    catalog = (envelope.get("data") or {}).get("apps", [])

    installed_ids = {a.id for a in await registry.list_active(pool)}
    offered = _load_offered()
    default_ids = {a["app_id"] for a in catalog if a.get("is_default")}

    # First run on a pre-existing entity has no marker yet: seed it with
    # whatever's already installed so we don't treat long-present apps as
    # "new". (An app uninstalled before this marker existed may install
    # once — unavoidable without historical tombstones, and benign.)
    if not offered and installed_ids:
        offered = set(installed_ids)

    # A default app installs only if it's not present AND has never been
    # offered to this entity before (respects user uninstalls) AND is not a
    # RETIRED id.
    #
    # The retirement filter is not theoretical (measured):
    # `connections` is in kernel.RETIRED_APPS — retired, source
    # deleted — and its store row is STILL audience='public' with
    # is_default=true, so it is served in the live catalog. Without this filter
    # a fresh entity installs it 30s after boot, carries it until the next pod
    # restart, and only then loses it to the boot sweep. The store row should
    # be yanked too (scripts/check_store_delisted.py is the gate for that), but
    # the entity must not depend on the catalog being correct.
    retired = _retired_app_ids()
    candidates = [a for a in catalog
                  if a.get("is_default")
                  and a["app_id"] not in installed_ids
                  and a["app_id"] not in offered
                  and a["app_id"] not in retired]
    if not candidates:
        # Still record the current default set so future uninstalls stick.
        _save_offered(offered | default_ids)
        return {"status": "ok", "installed": [], "note": "nothing to install"}

    results = {"installed": [], "failed": []}
    for app in candidates:
        app_id = app["app_id"]
        version = app.get("latest_version")
        if not version:
            results["failed"].append((app_id, "no version in catalog"))
            continue
        try:
            envelope = await bc.request_full(
                "appstore.fetch_bundle",
                {"app_id": app_id, "version": version},
            )
        except Exception as exc:
            results["failed"].append((app_id, f"fetch raised: {exc}"))
            continue
        if not envelope.get("ok"):
            results["failed"].append((app_id, envelope.get("error") or "fetch failed"))
            continue
        data = envelope.get("data") or {}
        b64 = data.get("bundle_b64")
        if not b64:
            results["failed"].append((app_id, "no bundle bytes"))
            continue
        try:
            bundle = base64.b64decode(b64)
        except Exception as exc:
            results["failed"].append((app_id, f"b64 invalid: {exc}"))
            continue
        try:
            await installer.install_bundle(pool, bundle, source="store")
        except installer.AppRuntimeOverPlan as exc:
            # Neither a crash nor a rejection of the app: the plan cannot hold
            # its runtime. Recorded in its own words so this report never reads
            # as "the app is broken" when the answer is "upgrade".
            results["failed"].append((app_id, f"over plan: {exc}"))
            continue
        except installer.InstallError as exc:
            results["failed"].append((app_id, f"install rejected: {exc}"))
            continue
        except Exception as exc:
            log.exception("default install crashed for %s", app_id)
            results["failed"].append((app_id, f"install crashed: {exc}"))
            continue
        results["installed"].append(app_id)
        log.info("default-installed %s v%s from store", app_id, version)

    # Record every current default id as "offered" so an uninstall of any
    # of them now sticks across future boots.
    _save_offered(offered | default_ids)
    results["status"] = "ok"
    return results


def _version_tuple(v) -> tuple:
    """Dotted-numeric version key ("0.5.10" > "0.5.9"). Non-numeric parts
    compare as 0 so a malformed version can never look newer than a real one."""
    parts = []
    for p in str(v or "").strip().split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts) or (0,)


async def _sync_store_upgrades(bc: "BridgeClient", pool) -> dict:
    """Upgrade-only refresh against the central app store.

    After first boot the entity skips the
    default-install pass (`_has_non_native_installs` returns True
    once anything's installed), but image rolls never re-checked the
    store for newer versions of already-installed factory-bundle apps.
    Their on-disk copies under /var/lib/cseries/apps/ persisted across
    pod restarts and went stale (caught once: the factory app's
    index.html was missing a chat-filter div).

    This function closes the gap: for every locally-installed non-
    native app, ask the store for the latest version; if newer than
    local, fetch the bundle and install in place. Apps the user
    uninstalled are absent from the registry → skipped, respecting
    the uninstall.
    """
    try:
        envelope = await bc.request_full("appstore.list", {})
    except Exception as exc:
        return {"status": "store_unreachable", "error": str(exc), "upgraded": [], "failed": []}
    if not envelope.get("ok"):
        return {"status": "store_error", "error": envelope.get("error"),
                "upgraded": [], "failed": []}
    catalog = (envelope.get("data") or {}).get("apps") or []
    by_id = {a.get("app_id"): a for a in catalog if a.get("app_id")}

    local = {a.id: a for a in await registry.list_active(pool)}

    retired = _retired_app_ids()
    image_baked = installer.image_baked_ids()
    candidates: list[tuple[str, str, str | None]] = []
    for app_id, local_app in local.items():
        if app_id in KERNEL_REQUIRED_APPS:
            continue  # native apps refresh via install_native_bundles
        if app_id in retired:
            # A retired id still on disk is the boot sweep's business — it
            # uninstalls it. Pulling a NEWER bundle for it first would only
            # refresh something about to be removed, and on an entity whose
            # sweep is failing (fail-soft, retries next boot) it would keep
            # the zombie current forever.
            continue
        if app_id in image_baked:
            # The IMAGE owns this app's version — install_image_bundles laid it
            # down at boot and re-lays it byte-for-byte on every roll. The store
            # must never second-guess it: that is a real incident (the
            # store's stale sites 0.5.2 reinstalled over a freshly image-baked
            # 0.5.3, skills 0.1.2 -> 0.1.1 identically).
            #
            # Nothing used to say so. Counted once: of the 19 apps the
            # image bakes, 15 are simply ABSENT from the public catalog, so the
            # `store_row is None` branch below swallowed them by accident. Of
            # the 4 that are listed, `oracle` and `settings` are kernel natives
            # already skipped above — leaving `sites` and `skills`, the exact
            # two apps in that incident, held back by nothing but the
            # strictly-newer comparison.
            #
            # Both of those protections are the wrong shape for the rule. The
            # accident stops holding the moment anything resolves an app by id
            # instead of reading the catalog, which is precisely what the branch
            # below now does; and strictly-newer only prevents a DOWNGRADE, not
            # a store copy overwriting an image copy at a higher version. Say
            # the actual rule.
            continue
        store_row = by_id.get(app_id)
        if store_row is None:
            # Installed, not image-baked, and NOT in the public catalog.
            #
            # This used to be `continue  # locally installed but no longer in
            # store`, which conflated two different situations: an app whose
            # store row is genuinely gone, and an app whose row is
            # audience='internal' and therefore never in the catalog at all.
            # The second is a real, populated case and it meant those apps could
            # never upgrade. Measured: claude-code is internal, four
            # entities sat on 0.1.9 while the store served 0.2.1, and 0.1.10 /
            # 0.2.0 / 0.2.1 had reached nobody. One of the stranded changes was
            # the removal of the retired Copilot harness from its description.
            #
            # `audience` gates DISCOVERY (the platform database's migration 014: "the catalog
            # SQL must explicitly query WHERE audience='public'"), not
            # DISTRIBUTION — GET /appstore/bundle/<id> has no audience gate.
            # So resolving by id adds no visibility: this loop only walks apps
            # the entity ALREADY has installed, and it cannot surface one it
            # does not. A version of None means "ask the store by id"; the
            # strictly-newer rule below still decides whether to install.
            candidates.append((app_id, local_app.version, None))
            continue
        target_version = store_row.get("latest_version")
        # STRICTLY newer only. This was a bare != — which made a STALE store
        # row silently DOWNGRADE a freshly image-installed app on every boot
        # (install_image_bundles once laid down sites 0.5.3 fleet-wide
        # and this sync immediately reinstalled the store's old 0.5.2 over it;
        # skills 0.1.2 -> 0.1.1 hit identically). The store sync's documented
        # job is "if newer than local, upgrade" — it may only move FORWARD.
        if not target_version or _version_tuple(target_version) <= _version_tuple(local_app.version):
            continue
        candidates.append((app_id, local_app.version, target_version))

    if not candidates:
        return {"status": "ok", "upgraded": [], "failed": [],
                "deferred": [], "skipped": [], "checked": len(local)}

    # `checked` was previously set only on the nothing-to-do early return above,
    # so a caller that actually had work got None for it — including the
    # /api/applications/refresh-store response the operator reads.
    results: dict = {"upgraded": [], "failed": [], "deferred": [], "skipped": [],
                     "checked": len(local)}
    for app_id, from_v, to_v in candidates:
        # to_v None = the unlisted case: ask the store for this id's latest and
        # find out the version from the response. An unpinned
        # /appstore/bundle/<id> returns the newest non-yanked release and its
        # version in X-App-Version, which the bridge surfaces as data.version.
        params = {"app_id": app_id}
        if to_v:
            params["version"] = to_v
        try:
            envelope = await bc.request_full("appstore.fetch_bundle", params)
        except Exception as exc:
            results["failed"].append((app_id, f"fetch raised: {exc}"))
            continue
        if not envelope.get("ok"):
            err = envelope.get("error") or "fetch failed"
            if to_v is None:
                # By-id lane. "No such app" and "the store is having a bad day"
                # are different answers and must not share a reason string — a
                # transient 5xx reported as "not in store" would read, during an
                # outage, as every internal app having been withdrawn.
                #
                # Absence is the ONLY case that is quiet, because it is the
                # normal steady state of a self-built app and would otherwise
                # log a failure on every sweep forever. The adapter renders a
                # controller status as "bundle fetch <status>"
                # (op_appstore_fetch_bundle), and the bridge client tags a
                # TRANSPORT failure — not connected / circuit open / timeout —
                # with error_kind="infra".
                if envelope.get("error_kind") != "infra" and "404" in str(err):
                    results["skipped"].append({"app_id": app_id, "from": from_v,
                                               "to": from_v, "reason": "not in store"})
                else:
                    results["failed"].append((app_id, f"store lookup failed: {err}"))
                continue
            results["failed"].append((app_id, err))
            continue
        data = envelope.get("data") or {}
        if data.get("payment_required"):
            # A priced app whose entitlement is absent or lapsed. ok=True with
            # no bundle (the soft-result shape), so it must be caught BEFORE the
            # missing-bytes branch below or a lapsed subscription reports as
            # "no bundle bytes". The app stays on its current version.
            results["skipped"].append({
                "app_id": app_id, "from": from_v, "to": from_v,
                "reason": "payment required (no active entitlement)"})
            continue
        resolved = (data.get("version") or "").strip()
        if to_v is None:
            # Same strictly-newer rule as the catalog path — applied after the
            # fetch because that is when the version becomes known. The bundle
            # is discarded when it is not newer, which is the cost of the store
            # having no unauthenticated way to ask "what is the latest version
            # of this internal app" without also handing over the bundle.
            if not resolved or _version_tuple(resolved) <= _version_tuple(from_v):
                results["skipped"].append({
                    "app_id": app_id, "from": from_v, "to": resolved or from_v,
                    "reason": "already at the store's latest"})
                continue
            to_v = resolved
        b64 = data.get("bundle_b64")
        if not b64:
            results["failed"].append((app_id, "no bundle bytes"))
            continue
        try:
            bundle = base64.b64decode(b64)
        except Exception as exc:
            results["failed"].append((app_id, f"b64 invalid: {exc}"))
            continue
        try:
            # Re-verify entitlement on UPGRADE, not just first install. This is
            # what makes a subscription model enforceable: a lapsed/cancelled
            # subscription means no entitlement, so the paid app stops receiving
            # upgrades (it stays on its current version; a one-time purchase keeps
            # its permanent entitlement so the check just passes).
            #
            # Three distinct outcomes, kept distinct so nothing fails silently:
            #   - EntitlementUnverifiable (bridge blipped mid-sweep): DEFER. The
            #     app stays on its current version and retries next sweep. A
            #     transient bridge outage must NOT strand a free app on an old
            #     version, and it must NOT read as a crash.
            #   - InstallError (verified no entitlement): the lapse case. Skip
            #     the upgrade, record it loudly — this is the intended outcome.
            #   - anything else: a genuine crash.
            await installer.install_bundle(pool, bundle, source="store", check_entitlement=True)
        except installer.AppRuntimeOverPlan as exc:
            # The NEW version reserves more than the plan has left. Recorded as
            # a skip with a nameable reason rather than a crash, and it is an
            # upsell the user can act on.
            #
            # NOT "the old version keeps running": install_bundle extracts and
            # registers BEFORE the descriptor sync, and its rollback is
            # uninstall_app — so an over-plan refusal on this unattended sweep
            # removes the app (config rows and workspace survive, purge=False).
            # Rare (it needs a plan to have shrunk under an installed app, or a
            # form change), but it happens with nobody present, so it is said
            # plainly here rather than discovered later.
            log.warning("store-upgrade SKIPPED %s %s→%s: over plan: %s",
                        app_id, from_v, to_v, exc)
            results["skipped"].append({"app_id": app_id, "from": from_v, "to": to_v,
                                       "reason": f"over plan: {exc}"})
            continue
        except installer.EntitlementUnverifiable as exc:
            log.warning("store-upgrade DEFERRED %s %s→%s: entitlement unverifiable "
                        "(bridge); will retry next sweep: %s", app_id, from_v, to_v, exc)
            results["deferred"].append({"app_id": app_id, "from": from_v, "to": to_v,
                                        "reason": str(exc)})
            continue
        except installer.InstallError as exc:
            log.warning("store-upgrade SKIPPED %s %s→%s: no active entitlement "
                        "(subscription lapsed / unpaid): %s", app_id, from_v, to_v, exc)
            results["skipped"].append({"app_id": app_id, "from": from_v, "to": to_v,
                                       "reason": str(exc)})
            continue
        except Exception as exc:
            log.exception("store-upgrade crashed for %s v%s", app_id, to_v)
            results["failed"].append((app_id, f"install crashed: {exc}"))
            continue
        results["upgraded"].append({"app_id": app_id, "from": from_v, "to": to_v})
        log.info("store-upgraded %s %s → %s", app_id, from_v, to_v)

    results["status"] = "ok"
    return results


async def run_once(bc: "BridgeClient | None") -> dict:
    """Single bootstrap pass. Returns a status dict.

    Two phases:
      1. First-boot default install — fresh pod with no non-native
         apps → install the default-marked set from the store.
      2. Store upgrade sweep — for every already-installed non-native
         app, check the store and upgrade if newer. Runs on every boot
         (not just first), so image rolls propagate factory-bundle app
         updates without manual intervention.
    """
    pool = brain_service.get_pool()
    if pool is None:
        return {"status": "pool_not_ready"}
    # Every call below is an App Store op: the network's door when it
    # answers, else the free public catalog (applications/public_store.py).
    from server.applications.public_store import store_client
    bc = store_client(bc)
    if not getattr(bc, "_connected", False):
        return {"status": "bridge_not_ready"}

    if await _has_non_native_installs(pool):
        # Existing entity. Still reconcile the default set: install any
        # NEWLY-published default app that isn't present yet (e.g. a new
        # first-party system app like Sites). _install_default_set only
        # installs is_default apps not already installed, so this never
        # reinstalls or fights an uninstall of a non-default app. Then
        # upgrade everything already present. This is what makes
        # "publish a new default app" actually reach the existing fleet,
        # not just fresh sign-ups.
        install_result = await _install_default_set(bc, pool)
        log.info("default-set reconcile (existing entity): %s", install_result)
        upgrade_result = await _sync_store_upgrades(bc, pool)
        log.info("store-upgrade sweep result: %s", upgrade_result)
        return {
            "status": "ok",
            "note": "existing host — reconciled default set + upgrade sweep",
            "default_reconcile": install_result,
            "upgrade_sweep": upgrade_result,
        }

    log.info("default-install bootstrap: fresh pod, fetching default set from store")
    result = await _install_default_set(bc, pool)
    log.info("default-install bootstrap result: %s", result)

    # Run the upgrade sweep after first-boot install too — covers the
    # rare race where install lands an older version than what's now
    # in the store.
    upgrade_result = await _sync_store_upgrades(bc, pool)
    log.info("store-upgrade sweep after first-boot install: %s", upgrade_result)
    result["upgrade_sweep"] = upgrade_result
    return result


# A boot reconcile that cannot run yet (the bridge is not up, or the pass
# crashed) is retried for as long as it takes: RETRY_BASE_S, doubling up to
# RETRY_MAX_S. It is kernel-local and cheap, so there is no reason to give up
# — the fixed 12 x 300 s it replaced stopped silently after an hour, and a
# bridge that came back later left the default apps uninstalled until the next
# restart.
RETRY_BASE_S = 300
RETRY_MAX_S = 3600


def retry_wait(attempts: int) -> float:
    return float(min(RETRY_BASE_S * (2 ** min(max(0, int(attempts) - 1), 32)), RETRY_MAX_S))


async def run_loop(bc: "BridgeClient | None") -> None:
    """Background task spawned from lifespan. Waits for the bridge to
    settle, then runs the bootstrap once. While the bridge is not connected
    (or a pass crashes) it retries with a doubling wait, never giving up;
    it ends when the install pass ran or there is no PG (nothing to do),
    and says which."""
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
            log.exception("default-install attempt %d crashed — retrying in %.0fs",
                          attempts, wait)
            await asyncio.sleep(wait)
            continue
        if result.get("status") == "bridge_not_ready":
            wait = retry_wait(attempts)
            log.info("default-install attempt %d: bridge not ready — retrying in %.0fs",
                     attempts, wait)
            await asyncio.sleep(wait)
            continue
        if result.get("status") == "pool_not_ready":
            log.warning("default-install: no database pool — nothing installed (attempt %d)",
                        attempts)
            return
        log.info("default-install finished after %d attempt(s): %s", attempts,
                 result.get("status"))
        return
