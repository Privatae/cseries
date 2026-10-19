"""
Installer service — verify a signed bundle, extract it to the per-app
install path, and register it in the kernel.

Trust pipeline:
  1. Parse the bundle (zip layout).
  2. Verify the Ed25519 signature against every active key in the
     trust store. The matching key's `tier_allowed` must include the
     manifest's declared tier; otherwise the install is refused even
     though the signature is valid (right shape, wrong authority).
  3. Validate the manifest (existing manifest.py rules, incl. native
     gate against KERNEL_REQUIRED_APPS).
  4. Extract payload + manifest to INSTALL_ROOT/<app_id>/.
  5. Register/upgrade in `installed_applications` with signer
     fingerprint recorded.

Uninstall is the inverse: refuse native, remove the install dir,
delete the registry row.

Factory bootstrap walks /app/factory-bundles/*.cseries on first boot and
installs each, so the shipping image lights up without anyone
manually invoking the install API.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tempfile
from pathlib import Path

import asyncpg

from server.applications import bundles, registry, storage, trust_store


def _force_clear(install_dir: Path) -> None:
    """Robust removal of an existing install directory.

    shutil.rmtree fails with ENOTEMPTY when filesystem semantics on the
    PVC don't atomically reflect removal. We retry once, then fall back
    to renaming the dir aside so the install path is free immediately —
    the stale tree gets cleaned up by next-boot housekeeping.
    """
    try:
        shutil.rmtree(install_dir)
        return
    except OSError:
        pass
    try:
        os.sync()
    except Exception:
        pass
    try:
        shutil.rmtree(install_dir)
        return
    except OSError:
        pass
    import time as _time
    stash = install_dir.with_name(f"{install_dir.name}.stash-{int(_time.time())}")
    os.rename(install_dir, stash)
from server.applications.manifest import AppManifest, ManifestError, load as load_manifest

log = logging.getLogger("privatae.applications.installer")


INSTALL_ROOT = Path("/var/lib/cseries/apps")
# Subdirs of an app's install dir that are RUNTIME CACHES the app provisions
# for itself post-install (never part of the signed bundle payload). These are
# carried across an upgrade's dir-replace instead of being wiped with the old
# payload. `python-libs` was claude-code's Claude Agent SDK cache; that app
# no longer provisions it and deletes it at start, so
# nothing creates it now — kept so an app that adopts the convention again is
# not wiped on upgrade. If a second convention appears, consider graduating
# this to a manifest field.
_RUNTIME_CACHE_DIRS = ("python-libs",)
# Image-baked bundles for kernel-required native apps only.
# Installable apps now live in the central app store, fetched via the
# bridge at first boot, not in the image. The legacy path is kept as a
# read-fallback so an in-place upgrade from an older image doesn't churn —
# but new builds populate /app/native-bundles/ only.
NATIVE_BUNDLE_DIR = Path("/app/native-bundles")
LEGACY_FACTORY_BUNDLE_DIR = Path("/app/factory-bundles")


class InstallError(RuntimeError):
    """Refuse to install a bundle."""
    # Refuses what the caller asked for (tool_call_ledger.refused_if_caller
    # files it as the caller's outcome, never a platform fault).
    caller_refusal = True


class EntitlementUnverifiable(InstallError):
    """Couldn't verify entitlement because the bridge was unreachable or
    errored — distinct from a *verified* "not entitled" refusal.

    Subclasses InstallError so every existing ``except InstallError`` caller
    still fails closed (a paid bundle never installs on an unverifiable
    check). The store-upgrade sweep catches this SPECIFICALLY to DEFER —
    retry next sweep, log loudly — instead of treating a transient bridge
    blip as a permanent "install crashed" that silently strands a free app
    on an old version. See default_install._sync_store_upgrades.
    """
    # The bridge did not answer: the platform's failure, not a refusal.
    caller_refusal = False


class UninstallError(RuntimeError):
    """Refuse to uninstall an app."""
    # Refuses what the caller asked for (tool_call_ledger.refused_if_caller
    # files it as the caller's outcome, never a platform fault).
    caller_refusal = True


async def _check_entitlement(app_id: str) -> None:
    """Per-entity premium gate (Layer 2, defense in depth).

    Asks the bridge whether this entity holds an active entitlement for
    `app_id`. The central store gate already refuses to serve
    bytes for paid apps without an entitlement, but those bytes could
    still reach the installer via the factory bundle dir, ssh, or
    another out-of-band path. This gate runs at install time and
    refuses to extract a paid bundle locally without an active
    entitlement.

    Decision matrix from the bridge's `appstore.entitlement.check`:
      entitled=True               → pass
      entitled=False, price=0     → pass (free app, or factory app
                                          not in the central catalog)
      entitled=False, price>0     → refuse (InstallError) — VERIFIED denial,
                                    e.g. an unpaid or lapsed paid app
      bridge unavailable / error  → raise EntitlementUnverifiable (fail
                                    closed) — paid bundles can't slip through
                                    during an outage, but the store-upgrade
                                    sweep catches this distinctly to DEFER +
                                    retry, so a transient blip never strands a
                                    free app on an old version. Callers that
                                    legitimately bypass the catalog (factory
                                    bundles, entity self-signed) pass
                                    `check_entitlement=False` instead.
    """
    try:
        from server.main import get_bridge_client
    except Exception as e:
        raise EntitlementUnverifiable(f"entitlement gate: bridge import failed: {e}") from None
    bc = get_bridge_client()
    if bc is None or not getattr(bc, "_connected", False):
        raise EntitlementUnverifiable(
            f"entitlement gate: bridge not connected (cannot verify entitlement "
            f"for {app_id!r}). Retry once the bridge is up; or call install via "
            f"a path that bypasses the gate (factory / host self-signed)."
        )
    envelope = await bc.request_full("appstore.entitlement.check", {"app_id": app_id})
    if not envelope.get("ok"):
        raise EntitlementUnverifiable(
            f"entitlement gate: bridge returned error checking {app_id!r}: "
            f"{envelope.get('error') or 'unknown'}"
        )
    data = envelope.get("data") or {}
    if data.get("entitled"):
        return
    price = int(data.get("price_micros") or 0)
    if price == 0:
        return  # free app or app not in catalog
    raise InstallError(
        f"no entitlement for {app_id!r} (price ${price/1e6:.2f}). "
        f"Purchase via the App Store before installing."
    )


def this_entity_id() -> int:
    try:
        return int(os.environ.get("ENTITY_ID", "0"))
    except (TypeError, ValueError):
        return 0


def installs_on_this_entity(manifest) -> bool:
    """target_entities gate (the SHIPPED→INSTALLED per-entity filter): an empty
    list installs everywhere; a non-empty list installs only on a listed entity
    (e.g. the security app ships in the image but only installs into the entity it lists).
    Shared by every install source — see install_bundle + install_image_bundles."""
    return (not manifest.target_entities) or (this_entity_id() in manifest.target_entities)


# The distinct SOURCES an install can arrive from (docs/app-architecture.md):
#   image  — baked into the kernel image (native/factory bundles), installed at boot
#   store  — fetched from the central app store (first-boot defaults + upgrades)
#   manual — user installs a store app from the App Store UI
#   self   — entity self-built bundle (tier=custom, the Workbench)
INSTALL_SOURCES = ("image", "store", "manual", "self")


def _image_bundle_dir(*, warn: bool = False) -> Path | None:
    """The directory install_image_bundles installs FROM, or None if neither
    exists.

    NATIVE_BUNDLE_DIR is preferred; LEGACY_FACTORY_BUNDLE_DIR is the fallback so
    an in-place upgrade from an older image whose PVC still matches the older
    bundle set boots cleanly. Factored out so image_baked_ids() below answers
    "will the image refresh this app?" from the SAME directory the boot install
    actually walks — two copies of this selection would drift, and the direction
    it would drift in is the downgrade described below.
    """
    if NATIVE_BUNDLE_DIR.exists():
        return NATIVE_BUNDLE_DIR
    if LEGACY_FACTORY_BUNDLE_DIR.exists():
        if warn:
            log.warning("native-bundles dir missing; falling back to legacy "
                        "factory-bundles at %s", LEGACY_FACTORY_BUNDLE_DIR)
        return LEGACY_FACTORY_BUNDLE_DIR
    if warn:
        log.warning("no image-baked bundle directory found (checked %s, %s)",
                    NATIVE_BUNDLE_DIR, LEGACY_FACTORY_BUNDLE_DIR)
    return None


_IMAGE_BAKED_IDS: frozenset[str] | None = None


def image_baked_ids() -> frozenset[str]:
    """App ids shipped in this image — the apps install_image_bundles refreshes.

    Read from the bundles' own manifests rather than from a filename convention
    or a second hardcoded list, so it cannot disagree with what boot installs.
    Cached: the directory is baked into the image and cannot change while the
    pod lives.

    Callers use it to answer "does the IMAGE own this app's version?". That
    question has one wrong answer and it is expensive: pulling a store bundle
    over an image-baked app is a real incident (sites 0.5.3 -> 0.5.2,
    skills 0.1.2 -> 0.1.1). kernel.KERNEL_REQUIRED_APPS is NOT a substitute — it holds
    the 3 kernel-required ids, while the image bakes 19.

    An empty set on a machine with no bundle directory (a dev checkout, a test)
    is correct and safe: callers treat "not image-baked" as "the store may own
    it", which is the pre-existing behaviour everywhere off-pod.
    """
    global _IMAGE_BAKED_IDS
    if _IMAGE_BAKED_IDS is not None:
        return _IMAGE_BAKED_IDS
    ids: set[str] = set()
    bundle_dir = _image_bundle_dir()
    if bundle_dir is not None:
        for bundle_path in sorted(bundle_dir.glob("*.cseries")):
            try:
                contents = bundles._read_zip(bundle_path.read_bytes())
                with tempfile.TemporaryDirectory() as tmp:
                    (Path(tmp) / "app.yaml").write_bytes(contents.manifest_yaml)
                    ids.add(load_manifest(Path(tmp) / "app.yaml").id)
            except Exception:
                # One unreadable bundle must not blank the whole set — that
                # would silently re-open the downgrade path for every other
                # image-baked app. Skip it; install_image_bundles reports the
                # same file as a failure with detail.
                log.warning("could not read image bundle %s for the "
                            "image-baked id set", bundle_path.name, exc_info=True)
    _IMAGE_BAKED_IDS = frozenset(ids)
    return _IMAGE_BAKED_IDS


def _retired_app_ids() -> frozenset[str]:
    """kernel.RETIRED_APPS, read at call time.

    Deliberately not a module-level `from kernel import RETIRED_APPS`: reading
    it per call is what lets a gate test swap the set without reimporting this
    module, and the kernel constant is a frozenset literal so the lookup costs
    nothing.
    """
    from server.applications.kernel import RETIRED_APPS
    return RETIRED_APPS


async def _sync_app_harness(harness_id: str, enabled: bool, *, bc=None) -> bool:
    """Provision/deprovision the app's harness on install/uninstall (self-serve).

    Fires the bridge op workspace.set_harness — the bridge fills the cert-verified
    entity_id and the operator adds/removes the harness from Entity.spec.harnesses
    (provisions the harness Deployment@0 + Service + workspace PVC on enable; deletes
    the Deployment + Service but KEEPS the PVC on disable, so data survives). The
    harness is woken on app-open and released on close by the proxy. Best-effort:
    never raises, so an install/uninstall can't fail on a delivery hiccup; boot may
    precede the bridge connection — `harness_sync.run_loop` re-asserts every
    installed app's harness once the bridge is up, which is why this returns whether it was sent.
    `bc` lets a caller pass the bridge client it already holds (tests, the boot
    loop); by default it is the process-wide one."""
    try:
        if bc is None:
            from server.main import get_bridge_client as _gbc
            bc = _gbc()
        if bc is None or not getattr(bc, "_connected", False):
            log.info("harness sync (%s=%s) skipped — bridge not connected", harness_id, enabled)
            return False
        await bc.request("workspace.set_harness", {"harness_id": harness_id, "enabled": enabled})
        log.info("harness sync -> %s=%s", harness_id, enabled)
        return True
    except Exception as e:
        log.warning("harness sync (%s=%s) failed: %s", harness_id, enabled, e)
        return False


class AppRuntimeOverPlan(Exception):
    """The operator refused this app's runtime: its memory does not fit the plan.

    A REFUSAL, not a failure — and the difference has to survive the trip back
    to the user. The operator answers 402 with needs_mi / headroom_mi /
    upgrade_to (controller features/lifecycle/service.py
    _refuse_install_over_plan) precisely so the surface can name the capability,
    the memory it needs and the tier that carries it. Swallowing it into
    "sync returned False" is what turns a designed upsell into an app that
    installs, reports success, and is quietly dead — the failure shape the
    rule about transparent tiers exists to prevent.
    """

    def __init__(self, app_id: str, detail: dict | str):
        d = detail if isinstance(detail, dict) else {}
        self.app_id = app_id
        self.needs_mi = d.get("needs_mi")
        self.headroom_mi = d.get("headroom_mi")
        self.upgrade_to = d.get("upgrade_to")
        self.detail = detail
        # The sentence is composed HERE, from the numbers. The operator sends
        # numbers only: the bridge caps its body at 200 chars, and a payload
        # carrying a sentence measured 238 bytes, so the numbers used to be cut
        # off mid-JSON and this exception carried None for all of them.
        if self.needs_mi is not None:
            msg = (f"{app_id} reserves {self.needs_mi}MiB of your machine and "
                   f"this plan has {self.headroom_mi}MiB spare.")
            if self.upgrade_to:
                msg += f" {self.upgrade_to} has room for it."
        else:
            msg = str(detail) if detail else f"{app_id} does not fit this plan"
        super().__init__(msg)


def _over_plan_detail(err: str) -> dict | str:
    """Pull the operator's 402 payload back out of the bridge's error string.

    The bridge flattens the operator's HTTP body into
    `operator /set-app-runtime 402: {"detail": {...}}` (bridge adapter
    op_workspace_set_app_runtime), so the numbers survive as text. Parse them
    when they are there; hand back the raw string when they are not, because a
    refusal the user can read beats a refusal we dropped for being unparseable.
    """
    import json as _json
    start = err.find("{")
    if start < 0:
        return err
    try:
        body = _json.loads(err[start:])
    except Exception:
        return err
    detail = body.get("detail", body)
    return detail if isinstance(detail, dict) else err


async def sync_app_runtime(app_id: str, enabled: bool, *, bc=None) -> bool:
    """Write/remove the app's runtime DESCRIPTOR on install/uninstall —
    the app-runtime sibling of
    `_sync_app_harness`, generalized instead of hardcoded to one id.

    Fires the bridge op workspace.set_app_runtime — the bridge fills the
    cert-verified entity_id; the OPERATOR assigns the pod port (never the
    kernel) and, for store apps, verifies budget + network against the
    signed store manifest before writing Entity.spec.appRuntimes. The
    descriptor here is read from the INSTALLED manifest, so the numbers the
    store displayed are the numbers the pod gets.

    Best-effort like its sibling — but a helper app whose descriptor write
    missed is a dead app, not a degraded one, so the open path re-syncs
    once at wake time (harness_backend.ensure_awake_app).

    ONE exception to best-effort: a 402 from the operator's plan gate raises
    AppRuntimeOverPlan. Best-effort is right for "the bridge was down, try
    again" and wrong for "this will never fit" — the second is an answer the
    user is owed, and a retry cannot change it."""
    try:
        params: dict = {"app_id": app_id, "enabled": enabled}
        if enabled:
            m = load_manifest(INSTALL_ROOT / app_id / "app.yaml")
            from server.applications.manifest import runs_in_pod
            if not runs_in_pod(m):
                return False  # runs beside the kernel — nothing to declare
            # A helper must name its hosts; a process app may name none (it
            # reaches nothing outside the platform: manifest.load).
            if not m.resources.baseline_memory_mi or (
                    m.runtime.kind != "process" and not m.network):
                log.warning("app-rt sync %s skipped: runtime app missing "
                            "baseline_memory_mi or (a helper) network[] — "
                            "admission should have refused this", app_id)
                return False
            params["descriptor"] = {
                "budgetMi": int(m.resources.baseline_memory_mi),
                "network": list(m.network),
                "version": m.version,
            }
            if m.runtime.ttls:
                params["descriptor"]["ttls"] = dict(m.runtime.ttls)
            if m.runtime.kind == "process":
                # Form 2: the user's own process in its own pod.
                # The operator renders the pod from the toolchain image for
                # `image`, runs `command` in the fetched install dir, probes
                # readyPath on the assigned port, and hands the pod ONE
                # capability — the source token — with which its init
                # container pulls this app's install dir from the kernel
                # (routes/applications.py app_source). The token is derived,
                # not stored: HMAC over the app id under the kernel's app-token
                # secret, so the kernel can verify it without a row.
                from server import brain_service as _bs
                from server.applications.harness_backend import (
                    app_source_token, kernel_url_for_pods)
                pool = _bs.get_pool()
                params["descriptor"].update({
                    "form": "process",
                    "image": m.runtime.image,
                    "command": list(m.runtime.command),
                    "readyPath": m.runtime.ready_path,
                    "portEnv": m.runtime.port_env,
                    "readyTimeoutS": int(m.runtime.ready_timeout_seconds),
                    # The pod's CPU ceiling (manifest resources.vcpus, unset
                    # = 1); the operator renders it and bills the window at it.
                    "cpuLimitM": 1000 * int(m.resources.vcpus or 1),
                    # The wake waits as long as the manifest says the app
                    # takes to become ready (operator-capped). Without this
                    # the operator's 60s default outran every real first
                    # boot: a Python app's wake 504'd at 60s
                    # while the pod was still installing.
                    "wakeTimeoutS": int(m.runtime.ready_timeout_seconds),
                    "kernelUrl": kernel_url_for_pods(),
                    "sourceToken": await app_source_token(pool, app_id),
                })
        if bc is None:
            # Same process-wide bridge handle the wake path uses (one
            # acquisition helper instead of a third import copy).
            from server.applications.harness_backend import _bridge
            bc = _bridge()
        if bc is None or not getattr(bc, "_connected", False):
            log.info("app-rt sync (%s=%s) skipped — bridge not connected",
                     app_id, enabled)
            return False
        # request_full, not request: request() collapses every not-ok envelope
        # to None, which is exactly the distinction that matters here. The
        # envelope's `code` is the operator's own HTTP status (bridge
        # protocol.encode_response carries it), so 402 is a signal, not a
        # string to grep for.
        msg = await bc.request_full("workspace.set_app_runtime", params)
        if not msg.get("ok"):
            err = str(msg.get("error") or "")
            if msg.get("code") == 402:
                raise AppRuntimeOverPlan(app_id, _over_plan_detail(err))
            log.warning("app-rt sync (%s=%s) refused: %s", app_id, enabled, err)
            return False
        log.info("app-rt sync -> %s=%s", app_id, enabled)
        return True
    except AppRuntimeOverPlan:
        raise                    # a refusal the caller must surface, not eat
    except Exception as e:
        log.warning("app-rt sync (%s=%s) failed: %s", app_id, enabled, e)
        return False


async def _refuse_origin_key_collision(pool: asyncpg.Pool, app_id: str) -> None:
    """InstallError when another installed or image-baked app's origin key
    equals `app_id`'s. Its own id (an upgrade) is not a collision."""
    from server.applications import app_origin
    key = app_origin.origin_key(app_id)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id FROM installed_applications WHERE status = 'active' AND id <> $1",
            app_id)
    others = {r["id"] for r in rows} | set(image_baked_ids())
    others.discard(app_id)
    clash = sorted(o for o in others if app_origin.origin_key(o) == key)
    if clash:
        raise InstallError(
            f"{app_id!r} would share its web address with {clash[0]!r} (the "
            f"two ids hash to one origin key), so the two apps could read each "
            f"other. Choose another app id.")


async def install_bundle(
    pool: asyncpg.Pool,
    bundle_bytes: bytes,
    *,
    source: str = "store",
    check_entitlement: bool = True,
) -> registry.InstalledApp:
    """Verify + gate + extract + register a bundle. The ONE install entry every
    source routes through. Returns the installed app row.

    `source` (one of INSTALL_SOURCES) names where the bundle came from — it's
    recorded for legibility (docs/app-architecture.md), not a trust input; the
    gates below are the trust boundary regardless of source.

    Gates (in order): signature/signer authority → claimed-tier authorization →
    target_entities (per-entity scope, ALL sources) → premium entitlement
    (when check_entitlement=True).

    Raises InstallError if the signature can't be verified, the signer isn't
    permitted to sign the claimed tier, the manifest is invalid, the app is
    scoped to other entities, or the entity lacks an entitlement for a priced app.

    `check_entitlement=False` skips ONLY the premium gate — used by image-baked
    bundles (never priced) and entity self-built bundles (tier=custom, free for
    the owner). It never skips signature/tier/target_entities.
    """
    if source not in INSTALL_SOURCES:
        raise InstallError(f"unknown install source {source!r} (valid: {INSTALL_SOURCES})")
    keys = await trust_store.list_active_keys(pool)
    if not keys:
        raise InstallError("trust store empty — no keys to verify against")

    try:
        contents, matching_key = bundles.verify_any(
            bundle_bytes,
            trusted_keys=[k.public_key for k in keys],
        )
    except bundles.BundleError as e:
        raise InstallError(f"bundle rejected: {e}") from None

    signer = next(k for k in keys if k.public_key == matching_key)
    signer_fingerprint = signer.fingerprint

    # Parse the manifest by writing it to a temp dir; load_manifest
    # expects a yaml path. Cheap.
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        (tmp_dir / "app.yaml").write_bytes(contents.manifest_yaml)
        try:
            manifest = load_manifest(tmp_dir / "app.yaml")
        except ManifestError as e:
            raise InstallError(f"manifest invalid: {e}") from None

    # Provenance gate — the signer must be authorized for this provenance class.
    # This is THE trust anchor: provenance is issued by who signed (a community
    # key can only sign community), never trusted from the self-declared manifest.
    if manifest.provenance not in signer.provenance_allowed:
        raise InstallError(
            f"signer {signer.publisher!r} is not authorized to publish "
            f"provenance={manifest.provenance!r} (allowed: {signer.provenance_allowed})"
        )

    # Untrusted (community) apps may be STATIC (browser sandbox), HELPER
    # (the platform's own fetch+cache
    # runtime in its OWN scale-to-zero pod, walled to the app's declared
    # hosts), or a
    # PROCESS app that names a platform toolchain image, so it too runs in
    # its own pod (manifest.runs_in_pod is the placement predicate) behind
    # the same wall. What is refused is author code BESIDE the kernel: a
    # process app without an image, or an `external` custom sidecar image.
    # The store bar (validate_publish) already refused those before signing;
    # this is the installer's own anchor so a bundle that dodged the bar
    # still cannot land here.
    from server.applications.manifest import runs_in_pod as _runs_in_pod_gate
    if manifest.provenance == "community" and not (
            manifest.runtime.kind == "static" or _runs_in_pod_gate(manifest)):
        raise InstallError(
            f"community app {manifest.id!r} may declare runtime=static, "
            f"runtime=helper, or runtime=process with a platform runtime.image "
            f"(got kind={manifest.runtime.kind!r}, image="
            f"{getattr(manifest.runtime, 'image', None)!r}) — untrusted code "
            f"runs only in its own pod from a platform toolchain, never beside "
            f"the kernel and never from a custom sidecar image"
        )

    # Retirement gate — a culled id may not install from ANY source.
    #
    # kernel.RETIRED_APPS is the retirement decision; until now it was enforced
    # in exactly one place, the boot sweep in server/main.py, which uninstalls a
    # retired id AFTER it has already landed. Everything that installs ran
    # unaware of it: the first-boot default set, the store-upgrade sweep, and
    # POST /api/applications/install (whose only id gate is PLATFORM_TOOLS —
    # two ids, so every retired id passed it).
    #
    # The gap is live (measured): `connections` was retired
    # for a vendor-ToS reason and its source deleted, yet its store
    # row is still audience='public' + is_default — so a fresh entity installs
    # it, shows it, and only loses it at the next pod restart. The 12 other
    # retired ids are audience='internal' (invisible to the catalog) but
    # /appstore/bundle/<id> has no audience gate, so they still serve 200.
    #
    # Refusing here makes the entity correct on its own: a stale catalog row, a
    # hand-rolled install call, or a bundle kept on disk can no longer resurrect
    # a culled app between boots. Refuse-only — it can never change what a live
    # id does.
    #
    # A self-built app claiming a retired id is refused for the same reason, and
    # that is not a new loss: the boot sweep already uninstalls it at the next
    # restart. This makes it fail loudly at install instead of vanishing later.
    if manifest.id in _retired_app_ids():
        raise InstallError(
            f"{manifest.id!r} is a retired app id (server/applications/"
            f"kernel.py::RETIRED_APPS) — the boot sweep uninstalls it, so this "
            f"install would last only until the next restart. If the id is "
            f"meant to be live again, take it out of RETIRED_APPS; if this is "
            f"your own app, choose an id that is not on that list."
        )

    # Per-entity scope gate — applies to EVERY source (previously only the image
    # path enforced it, so a target_entities-scoped app could install on the
    # wrong entity via store/manual). An app scoped to other entities is refused
    # regardless of how it arrived.
    if not installs_on_this_entity(manifest):
        raise InstallError(
            f"{manifest.id}: target_entities={list(manifest.target_entities)} "
            f"does not include this host (Host #{this_entity_id()})"
        )

    # One origin per isolated app (server/applications/app_origin.py): a
    # community app is served from apps-<key>-<handle>, the key derived from its
    # id. Two ids with one key would share an origin and see each other, so a
    # community app whose key another app on this Host (or in this image)
    # already holds is refused here. 100-bit keys make this a refusal that is
    # not expected to fire; routes/applications.py keys_for is the serve-time
    # half, for an image-baked app that arrives later.
    if manifest.provenance == "community":
        await _refuse_origin_key_collision(pool, manifest.id)

    # Per-entity premium gate. Runs after signature + tier check
    # so a malformed bundle is rejected before we ask the bridge.
    if check_entitlement:
        await _check_entitlement(manifest.id)

    # Extract to install path. Replace any existing install — atomic
    # swap via a sibling temp dir so we don't half-update on failure.
    # RUNTIME CACHES SURVIVE THE REPLACE: an app may provision
    # a runtime payload into its own install dir AFTER install (claude-code's
    # `python-libs/` — the Claude Agent SDK + its bundled CLI, ~230MB pulled
    # from PyPI by the app's _bootstrap_sdk). The bundle payload never
    # contains these dirs, so replacing the whole dir silently destroys them:
    # the 0.1.8→0.1.9 boot upgrade on one Host wiped python-libs, the host
    # SDK engine's canary died ("Claude Code not found"), chat fell back to
    # the open lane, and builds/research were dead until a hand re-provision
    # + forced canary re-run. Carry the cache dirs across the replace.
    install_dir = INSTALL_ROOT / manifest.id
    preserved: list[tuple[str, Path]] = []
    if install_dir.exists():
        import time as _time
        for cache_name in _RUNTIME_CACHE_DIRS:
            cache_dir = install_dir / cache_name
            if cache_dir.is_dir():
                keep = install_dir.with_name(
                    f"{install_dir.name}.keep-{cache_name}-{int(_time.time())}")
                try:
                    os.rename(cache_dir, keep)
                    preserved.append((cache_name, keep))
                except OSError as e:
                    log.warning("could not preserve %s/%s across upgrade: %s",
                                manifest.id, cache_name, e)
        _force_clear(install_dir)
    bundles.extract_payload(contents, install_dir)
    for cache_name, keep in preserved:
        try:
            os.rename(keep, install_dir / cache_name)
            log.info("preserved runtime cache %s/%s across upgrade",
                     manifest.id, cache_name)
        except OSError as e:
            # The cache is a re-downloadable artifact — losing it degrades
            # (the app re-bootstraps) rather than breaks; say so loudly.
            log.warning("could not restore %s/%s after upgrade (app will "
                        "re-bootstrap it): %s", manifest.id, cache_name, e)
    # Drop a fingerprint of the bundle bytes next to the manifest so the
    # factory-bundle installer can detect "same version string, different
    # bytes" — a recurring failure mode when an app's UI changes without
    # the author bumping the version field. Hidden, single-line file.
    try:
        (install_dir / ".factory-fingerprint").write_text(
            hashlib.sha256(bundle_bytes).hexdigest(),
            encoding="ascii",
        )
    except OSError as e:
        log.warning("failed to write factory fingerprint for %s: %s", manifest.id, e)
    log.info("extracted %s to %s", manifest.id, install_dir)

    # Re-load the manifest from the extracted location so source_dir is set
    # to where the app actually lives now.
    installed_manifest = load_manifest(install_dir / "app.yaml")

    # Register in the kernel. source=='self' is the entity packing and signing
    # its OWN project (routes/applications.py ship path) — the one source that
    # means "this machine wrote it", which is what lets the licence gate tell
    # an author's own work from someone else's app on the same disk.
    installed = await registry.register(
        pool, installed_manifest, authored_here=(source == "self"),
    )

    # Stamp the signing key onto the row
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE installed_applications SET source_signature = $1 WHERE id = $2",
            signer_fingerprint, manifest.id,
        )

    # Provision the workspace dir if the manifest asked for one. Config
    # data needs no provisioning; the table is global.
    if installed_manifest.storage.workspace:
        storage.ensure_workspace(installed_manifest.id)

    log.info(
        "installed %s v%s provenance=%s source=%s (signer=%s, workspace=%s)",
        manifest.id, manifest.version, manifest.provenance, source, signer.publisher,
        installed_manifest.storage.workspace,
    )

    # Post-install: seed any default-skill files gated on this
    # app_id. Declarative file-copy only (no scripted
    # execution). Best-effort — a skill-seed failure must not roll
    # back an otherwise-successful install.
    try:
        from server.applications.skill_seeding import seed_for_app
        seeded = seed_for_app(installed_manifest.id)
        if seeded:
            log.info("post-install skill seed for %s: %s", manifest.id, seeded)
    except Exception as e:
        log.warning("post-install skill seed failed for %s: %s", manifest.id, e)

    # Calendar timeline: log an app_installed event. Best-effort.
    try:
        from server.main import get_bridge_client as _gbc
        bc = _gbc()
        if bc is not None and getattr(bc, "_connected", False):
            await bc.request("calendar.log", {
                "event_type": "app_installed",
                "summary": f"Installed {manifest.id} v{manifest.version} (provenance={manifest.provenance})",
                "payload": {
                    "app_id": manifest.id,
                    "version": manifest.version,
                    "provenance": manifest.provenance,
                    "signer": signer.publisher,
                },
                "source": "system",
            })
    except Exception as e:
        log.debug("Calendar log (app_installed) failed: %s", e)

    # A harness-backed app: installing it provisions its harness on the pool
    # (Deployment@0 + Service; woken on open, released on close). The
    # manifest's own runtime.harness is the predicate — NEVER a hardcoded id.
    # This was `if manifest.id == "vscode"`, so only the VS Code editor was ever
    # provisioned; the Browser app (runtime.harness: browser) installed from
    # the store on one Host had no Deployment behind it, every open was
    # answered "no harness-browser Deployment in entity-N — is 'browser' in
    # Entity.spec.harnesses?", and the window read "viewport disconnected".
    _harness = getattr(manifest.runtime, "harness", None)
    if _harness:
        await _sync_app_harness(_harness, True)

    # App runtime apps: installing writes the runtime descriptor so the
    # operator provisions the per-user pod — every helper app, and every
    # process app that names a toolchain image (manifest.runs_in_pod is the
    # one placement predicate; never a hardcoded id or kind here).
    from server.applications.manifest import runs_in_pod as _runs_in_pod
    if _runs_in_pod(manifest):
        try:
            await sync_app_runtime(manifest.id, True)
        except AppRuntimeOverPlan:
            # The plan cannot hold this app's runtime. Roll the install back
            # rather than leave a registry row the operator holds no reserve
            # for: that pair disagreeing is what would put an app in the
            # machine total that nothing is actually paying for, and it would
            # present as a successful install of a permanently dead app.
            # Uninstall first, then re-raise — the API turns this into the
            # upsell (needs/headroom/upgrade_to), which is the whole point of
            # the operator answering 402 instead of 500.
            try:
                await uninstall_app(pool, manifest.id)
            except Exception as e:  # noqa: BLE001 - the refusal is the news
                log.warning("app-rt %s: rollback after over-plan refusal "
                            "failed: %s", manifest.id, e)
            raise
        # A runtime pod fetches the app's source ONCE, at creation
        # (harness_backend.recycle_app). Without this, re-installing an app
        # whose pod is already up left that pod serving the OLD source
        # indefinitely and reported success anyway (seen:
        # v0.4.0 installed, v0.3.0 still running). The
        # descriptor sync above covers a version change operator-side; this
        # covers the pod itself, including a re-ship of the same version.
        # Fail-soft: a recycle that cannot run must never fail the install.
        try:
            from server.applications import harness_backend as _hb
            await _hb.recycle_app(manifest.id)
        except Exception as e:  # noqa: BLE001 - see comment
            log.warning("app-rt %s: post-install pod recycle failed: %s",
                        manifest.id, e)

    # An app that runs an MCP server for its own Host (manifest.McpSpec): it
    # becomes one of this Host's connections, named after the app, signed now.
    # Installing is the consent; nothing leaves the namespace. On an upgrade
    # its tool list is fetched again on next use, so a changed list marks the
    # Host's skill for it stale. Best-effort: reinstalling runs it again.
    try:
        from server import mcp_connections as _mcp
        _done = await _mcp.sync_app(pool, this_entity_id(), installed_manifest)
        if _done:
            log.info("install %s: MCP connection %s", manifest.id, _done)
    except Exception as e:  # noqa: BLE001 - the install stands
        log.warning("install %s: MCP connection sync failed: %r", manifest.id, e)

    # TELL THE SHELL. The desktop grid (desktop-surface.js), the taskbar
    # (taskbar.js) and the launcher (ws-client.js → dock-menu.js) all listen
    # for `app_installed` and re-list on it — and nothing ever sent it. Seen
    # live: Browser installed from the store; the row, the
    # install dir and GET /api/applications were all correct; the desktop
    # loaded earlier that day never heard about it ("doesnt show up
    # anywhere"). Three listeners, zero senders. This is the one place every
    # install path converges — manual upload, the store, a project ship, the
    # host's install_app tool — so it is the one place to say so.
    # Best-effort, like the recycle above: a dropped push is a stale grid
    # until the next reload, never a failed install.
    try:
        from server.main import broadcast
        await broadcast({
            "type": "app_installed",
            "data": {"app_id": manifest.id, "version": manifest.version,
                     "source": source},
        })
    except Exception as e:  # noqa: BLE001 — the install stands
        log.debug("app_installed push for %s not delivered: %s", manifest.id, e)

    return installed


async def uninstall_app(
    pool: asyncpg.Pool,
    app_id: str,
    *,
    purge: bool = False,
) -> dict:
    """Refuses native. Removes the registry row + install dir.

    By default config rows and the workspace dir survive — orphaned —
    so a reinstall of the same id reattaches them. Pass purge=True to
    delete every survivable artifact too.
    """
    app = await registry.get(pool, app_id)
    if app is None:
        raise UninstallError(f"not installed: {app_id}")
    # Gate on the LIVE kernel set, not the install row's stale tier. An app
    # row can still carry tier=='native' after being removed from KERNEL_REQUIRED_APPS
    # (de-native-ized, e.g. the Pulse/monitor cull). Those
    # stranded installs MUST be uninstallable — checking app.tier=='native'
    # left them un-removable, which read as "I culled it" while it lingered.
    from server.applications.kernel import KERNEL_REQUIRED_APPS as _LIVE_NATIVE_APPS
    if app_id in _LIVE_NATIVE_APPS:
        raise UninstallError(
            f"cannot uninstall native app {app_id!r} — kernel dependency"
        )

    install_dir = INSTALL_ROOT / app_id
    # Read the harness this app declares BEFORE the install dir goes: the
    # manifest is the only record of it, and deprovisioning below needs it.
    _harness: str | None = None
    try:
        from server.applications.manifest import load as _load_manifest
        _harness = getattr(_load_manifest(install_dir / "app.yaml").runtime,
                           "harness", None)
    except Exception:  # noqa: BLE001 — no manifest, nothing to deprovision
        _harness = None
    if install_dir.exists():
        shutil.rmtree(install_dir)
        log.info("removed install dir %s", install_dir)

    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM installed_applications WHERE id = $1",
            app_id,
        )
        # The peer app lane's CREDENTIALS and QUEUE go with the app (v153) —
        # same cleanup registry.uninstall does, because this path deletes the
        # row itself. Otherwise a reinstall silently resurrects old peer grants.
        # The INBOX is user data (what people sent them) and follows this
        # function's own contract above: it survives, orphaned, unless purge.
        lane_tables = ["peer_app_slips", "peer_app_outbox"] + (["peer_app_inbox"] if purge else [])
        for t in lane_tables:
            try:
                await conn.execute(f"DELETE FROM {t} WHERE app_id = $1", app_id)
            except Exception as e:  # noqa: BLE001 — pre-v153 pod has no such table
                log.info("uninstall %s: %s cleanup skipped (%s)", app_id, t, type(e).__name__)

    # Data-source grants go with the app too, and UNCONDITIONALLY — not under
    # purge. A grant is not the app's data, it is the user's standing answer to
    # "may this app use my Gmail", and app ids are reusable: left behind, the
    # next thing installed under the same id inherits reach into the user's
    # mail with no fresh decision anywhere. That is the same reasoning the peer
    # lane rows above are removed under, so it gets the same treatment.
    #
    # Outside the `conn` block on purpose: the integrations store takes its own
    # connections from the pod pool (integration_grants, v201), and grants
    # cascade only on connection_id — nothing else cleans up by app_id.
    # registry.uninstall carries a copy of this, since the two uninstall paths
    # are independent (this one deletes the row itself).
    try:
        from server.integrations.core import storage as _integ_storage
        removed = [g["connection_id"] for g in await _integ_storage.list_grants(app_id=app_id)]
        for cid in removed:
            await _integ_storage.revoke_app_grant(cid, app_id)
        if removed:
            log.info("uninstall %s: revoked %d integration grant(s)", app_id, len(removed))
    except Exception as e:  # noqa: BLE001 — store may not exist on this pod
        log.info("uninstall %s: integration grant cleanup skipped (%s)",
                 app_id, type(e).__name__)

    # A messaging connector's grant and capability go with the app, unconditionally,
    # for the same reason: an app id is reusable, and the next app installed under
    # it must not inherit a way to start the person's turns. The one revoke path
    # (nac_connector.revoke_connector): grant revoked, capability cleared, the
    # turn-cancel hook run. registry.uninstall carries the same call.
    try:
        from server.network import nac_connector as _nac_connector
        _rv = await _nac_connector.revoke_connector(pool, app_id=app_id, reason="app uninstalled",
                                                   actor="user")
        if _rv.grant_ids or _rv.drafts or _rv.cap_dropped:
            log.info("uninstall %s: connector grant(s) %s revoked, capability cleared=%s",
                     app_id, _rv.grant_ids + _rv.drafts, _rv.cap_dropped)
    except Exception as e:  # noqa: BLE001 — the capability check also refuses an uninstalled app
        log.warning("uninstall %s: connector grant revoke failed: %r", app_id, e)

    # An app's own MCP server (manifest.McpSpec) stops being one of the Host's
    # connections with it. verify() refuses an uninstalled app's connection
    # anyway (its install dir is gone); this makes the list say so.
    try:
        from server import mcp_connections as _mcp
        if await _mcp.drop_app(pool, this_entity_id(), app_id):
            log.info("uninstall %s: its MCP connection was revoked", app_id)
    except Exception as e:  # noqa: BLE001
        log.warning("uninstall %s: MCP connection revoke failed: %r", app_id, e)

    purged = {"config_rows_deleted": 0, "workspace_removed": False}
    if purge:
        purged = await storage.purge_app_data(pool, app_id)

    log.info(
        "uninstalled %s (purge=%s, config_purged=%d, workspace_purged=%s)",
        app_id, purge, purged["config_rows_deleted"], purged["workspace_removed"],
    )

    # A harness-backed app: uninstalling turns its harness off (the operator
    # deletes the Deployment + Service, keeps the PVC). The manifest's
    # runtime.harness is the predicate, read above before the dir was removed
    # — this was keyed on the editor's id by name, so a Browser uninstall
    # would have left its harness provisioned forever.
    if _harness:
        await _sync_app_harness(_harness, False)

    # App runtime apps: remove the descriptor (operator deletes the
    # Deployment + ConfigMap). Sent unconditionally on uninstall — the
    # manifest is already gone from disk, so we cannot check the kind here;
    # a disable for an app with no descriptor is a no-op operator-side.
    await sync_app_runtime(app_id, False)
    # ...and drop the wake lane's cached state: a reinstall
    # must never inherit a stale mark/url from the previous install.
    _hb.forget_app(app_id)

    return {"uninstalled": app_id, "purge": purge, **purged}


def prune_stash_dirs(max_age_seconds: int = 86400) -> dict:
    """Remove install-dir stashes left behind by `_force_clear`.

    When rmtree fails (PVC semantics), the install path is renamed aside
    as `<id>.stash-<epoch>` so install proceeds. The docstring at
    `_force_clear` claims "next-boot housekeeping" cleans them up — this
    function IS that housekeeping. Without it stash dirs accumulate on
    the PVC forever.

    Removes any directory under INSTALL_ROOT matching `<name>.stash-<digits>`
    whose epoch is older than `max_age_seconds` (default 24h). Younger
    stashes are kept so they can be inspected if an install just failed.

    Also covers `<name>.keep-<cache>-<digits>` — runtime-cache dirs set aside
    during an upgrade that died before restoring them. Same age
    rule; a fresh one may still be mid-upgrade.
    """
    removed: list[str] = []
    kept: list[str] = []
    failed: list[tuple[str, str]] = []
    if not INSTALL_ROOT.is_dir():
        return {"removed": removed, "kept": kept, "failed": failed}
    import re as _re
    import time as _time
    pattern = _re.compile(r"\.(?:stash|keep-[A-Za-z0-9_-]+)-(\d+)$")
    now = int(_time.time())
    for entry in INSTALL_ROOT.iterdir():
        m = pattern.search(entry.name)
        if not m:
            continue
        try:
            age = now - int(m.group(1))
        except ValueError:
            kept.append(entry.name)
            continue
        if age < max_age_seconds:
            kept.append(entry.name)
            continue
        try:
            shutil.rmtree(entry)
            removed.append(entry.name)
        except OSError as e:
            failed.append((entry.name, str(e)))
    if removed:
        log.info("pruned %d stale stash dirs (>%ds): %s",
                 len(removed), max_age_seconds, removed)
    if failed:
        log.warning("could not prune stash dirs: %s", failed)
    return {"removed": removed, "kept": kept, "failed": failed}


async def prune_orphan_registry_rows(pool: asyncpg.Pool) -> dict:
    """Reconcile installed_applications rows whose on-disk install_dir is gone.

    CLEANUP SEMANTIC (two states, on purpose, not an
    inconsistency): AUTOMATIC drift detection here DISABLES the row (status =
    'disabled') — it's recoverable (re-enabled if the dir reappears / reinstalled)
    and preserves the record for diagnosis. An EXPLICIT user uninstall (registry.
    uninstall / installer.uninstall_app) DELETES the row — a deliberate removal.
    So: drift => disable; uninstall => delete.

    This drift used to occur whenever a pod rolled and /var/lib/cseries/apps was
    ephemeral; a dedicated PVC is mounted there now, but a human (or a botched
    migration) can still delete a dir out from under the registry.

    Returns {"pruned": [app_id, ...]} for the boot log.
    """
    pruned: list[str] = []
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id FROM installed_applications WHERE status = 'active'",
        )
        for r in rows:
            install_dir = INSTALL_ROOT / r["id"]
            if install_dir.is_dir() and (install_dir / "app.yaml").exists():
                continue
            await conn.execute(
                "UPDATE installed_applications SET status = 'disabled' WHERE id = $1",
                r["id"],
            )
            pruned.append(r["id"])
    if pruned:
        log.warning(
            "registry drift: marked %d apps inactive (no install dir): %s",
            len(pruned), pruned,
        )
    return {"pruned": pruned}


async def install_image_bundles(pool: asyncpg.Pool) -> dict:
    """Install every app SHIPPED in the image (the 'shipped' state — see
    docs/app-architecture.md). NOTE the legacy name `install_native_bundles`
    (aliased below) is misleading: this installs native AND factory bundles.

    Only kernel-required native apps ship in the image. The
    NATIVE_BUNDLE_DIR (default /app/native-bundles) is preferred; the
    LEGACY_FACTORY_BUNDLE_DIR (/app/factory-bundles) is consulted as a
    fallback so an in-place upgrade from an older image whose PVC still
    matches the older bundle set boots cleanly.

    Returns a summary dict for the boot log:
        {"installed": [...], "upgraded": [...], "skipped": [...],
         "failed": [(filename, reason), ...]}
    """
    INSTALL_ROOT.mkdir(parents=True, exist_ok=True)

    summary = {"installed": [], "upgraded": [], "skipped": [], "failed": []}

    bundle_dir = _image_bundle_dir(warn=True)
    if bundle_dir is None:
        return summary

    for bundle_path in sorted(bundle_dir.glob("*.cseries")):
        try:
            bundle_bytes = bundle_path.read_bytes()
            # Peek the manifest to decide install vs. skip BEFORE doing
            # heavy work, so a boot loop doesn't re-extract on every start.
            try:
                contents = bundles._read_zip(bundle_bytes)
            except bundles.BundleError as e:
                summary["failed"].append((bundle_path.name, f"unreadable: {e}"))
                continue

            with tempfile.TemporaryDirectory() as tmp:
                (Path(tmp) / "app.yaml").write_bytes(contents.manifest_yaml)
                try:
                    peek = load_manifest(Path(tmp) / "app.yaml")
                except ManifestError as e:
                    summary["failed"].append((bundle_path.name, f"manifest: {e}"))
                    continue

            # Per-entity scope gate (shared with install_bundle). At boot we SKIP
            # gracefully rather than raising — an off-entity image bundle is
            # expected, not an error.
            if not installs_on_this_entity(peek):
                summary["skipped"].append(peek.id)
                continue

            existing = await registry.get(pool, peek.id)
            install_dir = INSTALL_ROOT / peek.id
            # Skip only when the install on disk matches the bundle BYTE
            # FOR BYTE. Version string equality is too coarse — an author
            # can ship UI fixes without bumping the version, and that
            # once silently dropped a whole class of UI changes.
            fingerprint_path = install_dir / ".factory-fingerprint"
            bundle_hash = hashlib.sha256(bundle_bytes).hexdigest()
            if (
                existing
                and existing.version == peek.version
                and install_dir.is_dir()
                and (install_dir / "app.yaml").exists()
                and fingerprint_path.exists()
                and fingerprint_path.read_text(encoding="ascii").strip() == bundle_hash
            ):
                summary["skipped"].append(peek.id)
                continue

            # Image-baked native/factory bundles bypass the per-entity
            # premium gate — they're whitelisted by being in the image,
            # not by the central catalog. Bridge may not be reachable
            # yet at this point in boot anyway.
            installed = await install_bundle(
                pool, bundle_bytes, source="image", check_entitlement=False,
            )
            if existing:
                summary["upgraded"].append(installed.id)
            else:
                summary["installed"].append(installed.id)

        except Exception as e:
            log.exception("factory install failed: %s", bundle_path.name)
            summary["failed"].append((bundle_path.name, str(e)))

    return summary



# Back-compat alias: the function was renamed install_native_bundles ->
# install_image_bundles (it installs native AND factory bundles). Keep the old
# name working for one release. Prefer install_image_bundles in new code.
install_native_bundles = install_image_bundles


# Register the open-time descriptor repair — see the comment
# on harness_backend._descriptor_resync for why this is a registration and
# not an import. Module-bottom so the function above is defined.
from server.applications import harness_backend as _hb  # noqa: E402
_hb.set_descriptor_resync(sync_app_runtime)
