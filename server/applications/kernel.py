"""
Kernel constants — what cseries considers structurally required.

KERNEL_REQUIRED_APPS is the hardcoded list of app ids the kernel depends
on. (Renamed from NATIVE_APPS: rebuild_factory_bundles.sh's
19-id image-baked list carried the SAME name for a DIFFERENT set, and the
collision produced real misreadings. The image-baked list now lives in
scripts/image_baked_apps.txt; this set stays the 3-id refuse-to-boot core.
NATIVE_APPS remains as a permanent back-compat alias, same policy as
ALLOWED_TIERS below.)
At boot, the kernel verifies every id in this set is installed and
present; missing natives are a refuse-to-boot condition.

The manifest validator uses this list to refuse `tier: native` from
any bundle whose id is not in the set, so a third-party app cannot
claim native status.

Changing this set is a kernel-version decision. Adding an id here
means "cseries cannot run without this app." Removing one means "cseries
can now run without this app" (and existing installs should retire
that app on the next upgrade).

Set rationale (app-layer collapse):
  settings     — entity identity + personality config
  oracle       — memory pipeline manager
  orchestrator — model routing + engine power switch (absorbed the
                 `privatae` app's pin)
"""

from __future__ import annotations

from typing import Final


KERNEL_REQUIRED_APPS: Final[frozenset[str]] = frozenset({
    "settings",
    "oracle",
    "orchestrator",
})

# Permanent back-compat alias (same policy as ALLOWED_TIERS at the bottom of
# this file): external importers and signed history may reference the old
# name. New code uses KERNEL_REQUIRED_APPS.
NATIVE_APPS = KERNEL_REQUIRED_APPS


# Apps culled by the app-layer collapse whose installs must be RETIRED on
# entities that still carry them (per the contract above: removing a native
# means existing installs retire it on the next upgrade). The boot path
# sweeps these AFTER image bundles install and BEFORE the natives check —
# fail-soft, uninstall only (config rows/workspace survive orphaned).
#   privatae / openrouter-direct — absorbed by orchestrator
#   ceigas — deprecated viewer app retired; the /api/ceigas
#            backend surface is untouched by this cull.
#
# RETIRING AN ID HAS TWO HALVES, AND THE SECOND ONE KEEPS GETTING SKIPPED.
#   (a) code   — add the id here. Three enforcement points read this set: the
#                boot sweep (server/main.py) uninstalls it, installer.
#                install_bundle refuses it from every source, and
#                default_install skips it in both the first-boot default set
#                and the store-upgrade sweep.
#   (b) store  — TWO writes, not one:
#                  b1. yank EVERY non-yanked version of the id. Deleting the
#                      source from system-apps/ is not withdrawal: the bundle
#                      bytes live in app_store_versions, and
#                      GET /appstore/bundle/<id> has no audience gate, so an
#                      unyanked row keeps serving a deleted app to anyone who
#                      asks for it by id.
#                  b2. if the row is audience='public', set it to 'internal'
#                      (and is_default=FALSE). b1 alone LOOKS complete — the
#                      catalog joins versions with INNER JOIN ... WHERE NOT
#                      yanked, so an all-yanked app drops out of /appstore/list
#                      and the endpoint 404s. But `audience` is a standing
#                      property of the app, not of a version: publish anything
#                      to that id later and it is instantly back in the public
#                      catalog, is_default and all. Measured on `connections`
#                      — after yanking all 5 versions every
#                      distribution probe went green while the row was still
#                      public.
#                scripts/check_store_delisted.py is the ratchet for both and
#                prints the exact remediation for each. b2 is the one write
#                here with no API: publish sets audience, yank does not touch
#                it, and un-yank is deliberately not surfaced — so it is a
#                direct UPDATE, and the `public` boolean follows it via trigger
#                app_store_sync_public (is_default does NOT — set it too).
# Half (b) had never been done for ANY id in this set. Measured: 12
# retired ids were still serving 64 live versions, and `connections` was still
# audience='public' + is_default, i.e. installing on fresh entities. The
# instruction was even written down at the time — commit 03db8b8b, which
# deleted the privatae, openrouter-direct and ceigas sources, ends with "Also
# withdraw any store listings for the three retired ids so the upgrade sweep
# can't resurrect them". Nothing enforced it, so it did not happen.
RETIRED_APPS: Final[frozenset[str]] = frozenset({
    "privatae",
    "openrouter-direct",
    "ceigas",
    # Pre-sweep zombies (found in a test pass): every id whose
    # source left the tree BEFORE the retirement sweep existed kept its
    # installed copy — start-menu entries that can't open. The 5 harness dock
    # apps (phase 3b cull), the budget+computeview pair (collapsed into
    # compute), and the pre-collapse strays.
    "codex", "cursor", "copilot", "grok", "opencode",
    "budget", "computeview",
    "pulse", "monitor", "timeline", "understand", "hello-sidecar",
    "model-catalog", "workspace", "gateway-audit",
    # Codespace: its read-only browse/reader is subsumed by the VS Code app (real
    # editing) + Gitea (repo browse) + Workbench (review). Only its 3D CodeCity map
    # is a deliberate drop. The backend /api/codespace/{tree,file} routes and the
    # read/write:workspace_files scope are KEPT (host meta-tools + smoke_test use
    # them) — this retires only the app tile.
    "codespace",
    # Connections: the shipped third-party login
    # surface violated vendor ToS — users build their own login surface; keys
    # live in the Keys app. The backend substrate (routes/harnesses.py,
    # network/harness_keys.py) stays; only the app + its dead scope go.
    "connections",
    # app-factory: retired (absorbed by the Workbench), then once REMOVED
    # from this set in favour of the
    # one-shot `DELETE FROM installed_applications WHERE id='app-factory'` in
    # migrations/v087, on the reasoning that "the sweep re-ran the uninstall
    # every boot forever; one row delete finishes the retirement".
    #
    # Restored for two measured reasons:
    #   1. The premise was wrong about cost. The sweep in server/main.py does
    #      `if registry.get(id) is None: continue` — when the app is absent it
    #      is one indexed lookup, not an uninstall. It never re-ran anything.
    #   2. A one-shot migration cannot hold a retirement. It removes the rows
    #      that exist when it runs; it cannot refuse a new install. app-factory's
    #      store row is still live — GET /appstore/bundle/app-factory returned
    #      200 with a 125,745-byte bundle when measured — so it was the one
    #      retired id with no runtime defence left at all.
    # No entity carries it now: none of the six live pods has an
    # /var/lib/cseries/apps/app-factory directory (checked), so this
    # restores the guard without uninstalling anything.
    "app-factory",
    # browser-novnc: retired with the noVNC viewport lane.
    # The Browser app's viewport is now a CDP screencast on the control
    # port itself (sidecars/browser/control/viewport.py) — there is no
    # 8831/websockify upstream left to proxy to. Never installable
    # anyway: operator_only + its only store version yanked; this
    # closes the id for good and sweeps any stray install dir.
    "browser-novnc",
    # bench: folded into the Workbench (the book of pages). Its run board (the
    # strands, the scoreboard, the pod clock) and each engine's card are on
    # every page's right side and conversation; the app had no input and
    # nothing else of its own. Image-baked until now, so the sweep is what
    # takes it off every Host. Store half: its row is audience='internal'
    # and not default; versions 0.1.0, 0.2.0, 0.2.1, 0.2.2 and 0.3.0 were
    # still unyanked when it retired (scripts/app_store_yank_retired.py).
    "bench",
    # vscode: the VS Code editor and its Coding Tutor, removed
    # (the editor returned 502s). The Workbench is where the Host builds. Image-baked
    # until now, so the sweep is what takes it off every Host. The sweep does
    # NOT take the harness off: it runs in lifespan before the bridge client
    # exists (server/main.py sets _bridge_client_ref after it), so its
    # uninstall's _sync_app_harness("vscode", False) is always skipped, and
    # the row it deletes means it never runs again. The operator retires the
    # `vscode` harness from every Entity (its retired-harness list). The
    # editor's workspace volume is left in place: it
    # is the person's data.
    "vscode",
})

# A retired app must never be kernel-required — the boot sweep uninstalls
# retired ids and uninstall refuses live natives, so overlap would wedge.
assert not (RETIRED_APPS & KERNEL_REQUIRED_APPS), "RETIRED_APPS overlaps KERNEL_REQUIRED_APPS"


# Apps that stay ALIVE on entities (image-baked native bundles) but are withdrawn
# from the central app-store catalog — they ship with the image, so a public store
# row is at best a stale duplicate and at worst re-installs an old version over the
# image-baked one. DISTINCT from RETIRED_APPS: a retired id is UNINSTALLED by the
# boot sweep above; a store-delisted id keeps running everywhere — only its catalog
# row must never be audience='public' (the catalog gate; app_store_apps.public is a
# legacy derived boolean per the platform database's migration
# 014_app_store_audience.sql, and flipping it alone delists NOTHING — an
# incident that recurred twice).
# scripts/check_store_delisted.py is the invariant ratchet over
# RETIRED_APPS | STORE_DELISTED_APPS.
STORE_DELISTED_APPS: Final[frozenset[str]] = frozenset({
    # circles: native-in-image since the React rebuild (image-baked pin,
    # scripts/smoke_test.sh EXPECTED_IMAGE_BAKED) — store-delisted.
    "circles",
})

# One lifecycle per id: retired means swept off entities (which implies gone from
# the public store too — the delist check covers both sets); store-delisted means
# alive in-image. An id in both would be contradictory vocabulary.
assert not (STORE_DELISTED_APPS & RETIRED_APPS), "STORE_DELISTED_APPS overlaps RETIRED_APPS"
assert not (STORE_DELISTED_APPS & KERNEL_REQUIRED_APPS), "STORE_DELISTED_APPS overlaps KERNEL_REQUIRED_APPS"


# Apps migrated to the App SDK origin-isolation path (app-origin isolation).
# The shell loads each of these cross-origin from its OWN origin,
# apps-<key>-<handle><zone> (applications/app_origin.py), so the browser's
# same-origin policy walls it off from the desktop and from every other app; the
# frame gets its per-window token on that origin (dashboard/base/js/
# app-frame-init.js). Every other app stays same-origin on the legacy
# contentWindow token path. Grows one verified app at a time; `settings` is the
# pilot.
SDK_MANAGED_APPS: Final[frozenset[str]] = frozenset({
    # The pilot.
    "settings",
    # Clean status/config/list/viewer apps — no
    # localStorage state (verified against the installed app files), so moving
    # to the apps-<handle> origin has no state-loss; cross-origin /api is
    # same-origin to the app (no CORS).
    "oracle", "skills",
    "compute", "sites", "publisher",
    "backups", "app-store",
    "training-memory",
    # app-layer collapse: orchestrator replaces the SDK-managed
    # privatae + openrouter-direct apps (verified: no localStorage in
    # orchestrator/app.js or the shared model-picker.js); ceigas retired.
    "orchestrator",
    # DEFERRED (need per-app work before isolating):
    #   circles — own localStorage state would reset
    #   claude-code, codespace — harness/complex, verify each individually
    #   (the codex/cursor/copilot/grok dock apps are deleted —
    #    harness availability is creds-connected, not an app)
    #   (training-memory uses EventSource: works in audit mode, but EventSource
    #    can't carry the app-token header — revisit before the strict flip.)
})


# App PROVENANCE — who vouched for an app and how isolated it runs. This is the
# trust axis, NOT a billing concept (the billing/subscription tier is users.tier,
# a different field entirely). Renamed off "tier" to end the overload.
# Monetization (free vs paid) is price_micros/app_entitlements, never a provenance
# value — the old "premium" value was dead vocabulary and was dropped.
ALLOWED_PROVENANCE: Final[frozenset[str]] = frozenset({
    "native",      # Kernel-required. Listed in KERNEL_REQUIRED_APPS. Cannot be uninstalled.
    "standard",    # First-party, signed by Privatae. Shipped with the image.
    "community",   # Third-party, signed by a community-authorized publisher key.
    "custom",      # Entity-built / entity-signed (built via the Workbench).
})

# PERMANENT back-compat (not transitional): the manifest key was
# renamed `tier`->`provenance`. The field is SIGNATURE-COVERED, so already-published,
# immutable `tier:`-signed bundles can never be rewritten in place — and across a fleet
# with hibernated / not-yet-provisioned entities, zero such bundles can never be
# guaranteed. manifest.load() therefore reads `provenance` then falls back to `tier`
# permanently. All NEW source + bundles are `provenance:`-native; this fallback is the
# read-path for legacy signed bundles only. Do NOT "clean it up" — removing it breaks
# serve-time isolation for any un-converged installed app.
PROVENANCE_MANIFEST_KEYS: Final[tuple[str, str]] = ("provenance", "tier")

# Permanent back-compat alias for any external importer. Do not use in new code.
ALLOWED_TIERS = ALLOWED_PROVENANCE
