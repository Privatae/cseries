#!/usr/bin/env python3
# gate-scope: whole-tree — every bundle in factory-bundles/ and native-bundles/
"""Retirement ratchet — a RETIRED app id can never be installed, from any lane.

THE DEFECT (measured).

`server/applications/kernel.py::RETIRED_APPS` is the retirement decision: the
source is deleted from system-apps/ and the id goes in that set. Until now the
set was read in exactly ONE place — the boot sweep in server/main.py, which
uninstalls a retired id *after* it has already landed. Every path that installs
ran unaware of it:

  * default_install._install_default_set  — installs whatever the catalog marks
    is_default. `connections` was retired (vendor ToS) and its
    store row is still audience='public' + is_default, so a fresh entity
    installed it 30s after boot and carried it until the next pod restart.
  * default_install._sync_store_upgrades  — skipped NATIVE_APPS only, so a
    retired id still on disk would be UPGRADED rather than left for the sweep.
  * routes/applications.py::install_app   — its only id gate is
    platform_tools.PLATFORM_TOOLS, which holds two ids, so every retired id
    passed. GET /appstore/bundle/<id> has no audience gate: copilot, ceigas and
    app-factory all answered 200 with real bundle bytes despite
    being audience='internal' and therefore invisible in the catalog.

So the retirement held only between restarts, and only where the sweep ran.

WHAT THIS GATE PROVES. Three refusals, exercised against the real functions —
not grepped for, not asserted structurally:

  1. install_bundle (installer.py:220) refuses a retired id and lets a live one
     through. It is the choke point every source routes through (image, store,
     manual, self), so proving it here covers the HTTP install route without
     standing up HTTP.
  2. _install_default_set drops a retired id from the first-boot default set
     even when the catalog says is_default.
  3. _sync_store_upgrades drops a retired id from the upgrade candidates even
     when it is installed locally and the store offers a newer version.

Plus a consistency check: no RETIRED id may ship in the image. kernel.py already
asserts disjointness against NATIVE_APPS, but that is the 3 kernel-required ids;
the image carries 27 factory bundles and 19 native ones. Retiring an id that is
still baked in would have install_native_bundles install it at every boot and
the sweep uninstall it the same boot — and with the refusal above, boot would
log an install failure forever.

Container-free: no DB, no network, no container. Only the trust-store lookup and
the signature check are stubbed — the manifest that install_bundle gates on is a
REAL one, parsed by the real loader, so the id/provenance/runtime values under
test are the ones production would compute.
Run: APP_ROOT=<repo> python3 scripts/test_retired_apps_never_install.py
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import zipfile
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(_ROOT))

from server.applications import default_install, installer  # noqa: E402
from server.applications.kernel import NATIVE_APPS, RETIRED_APPS  # noqa: E402

FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global FAILED
    print(("  ok    " if cond else "  FAIL  ") + name)
    if not cond:
        FAILED += 1
        if detail:
            for line in str(detail).strip().splitlines():
                print("          " + line)


# Sample a retired id that is also a real, historically-published store row, so
# the gate is about the live failure rather than an invented one. Read from the
# set rather than hardcoded, so removing it from RETIRED_APPS surfaces here.
RETIRED_SAMPLE = "connections" if "connections" in RETIRED_APPS else sorted(RETIRED_APPS)[0]
LIVE_SAMPLE = "settings"


# ── 1. install_bundle: the choke point ────────────────────────────────────────

class _FakeKey:
    """A trust_store key row, narrowed to the four fields install_bundle reads:
    public_key (matched against verify_any's second return), fingerprint,
    publisher, provenance_allowed (the provenance gate).

    provenance_allowed is {'standard'} because that is what the real manifest
    loader assigns a bundle with no explicit provenance — asserted below rather
    than assumed, so a change in that default fails here instead of silently
    turning these cases into provenance-gate rejections.
    """

    def __init__(self) -> None:
        self.public_key = b"fake-public-key"
        self.fingerprint = "fake-fingerprint"
        self.publisher = "Privatae"
        self.provenance_allowed = {"standard"}


def _manifest_yaml(app_id: str) -> bytes:
    """A minimal REAL manifest. manifest.REQUIRED_FIELDS is (id, name,
    entrypoint); everything else takes its documented default."""
    return (
        f"id: {app_id}\n"
        f"name: Retirement gate stub\n"
        f"entrypoint: index.html\n"
        f'version: "1.0.0"\n'
    ).encode()


class _FakeContents:
    """bundles.verify_any's return shape, narrowed to the one field
    install_bundle touches before the gate under test."""

    def __init__(self, app_id: str) -> None:
        self.manifest_yaml = _manifest_yaml(app_id)


def _install_bundle_reaching(app_id: str) -> str:
    """Call the REAL install_bundle for `app_id`; return the InstallError text.

    Stubbed: the trust-store lookup and the signature check — the two steps that
    need a pool and a signing key. NOT stubbed: the manifest parse and every
    gate, so the return value says exactly which gate stopped the install.

    The gate immediately AFTER the retirement check, installs_on_this_entity
    (installer.py:170), is forced False to give a sentinel error. Getting that
    error back proves the retirement gate let the id through — which is exactly
    what a live id must do.
    """
    saved_keys = installer.trust_store.list_active_keys
    saved_verify = installer.bundles.verify_any
    saved_scope = installer.installs_on_this_entity

    async def _keys(_pool):
        return [_FakeKey()]

    installer.trust_store.list_active_keys = _keys
    installer.bundles.verify_any = (
        lambda b, trusted_keys: (_FakeContents(app_id), b"fake-public-key"))
    installer.installs_on_this_entity = lambda _m: False
    try:
        asyncio.run(installer.install_bundle(None, b"stub-bundle", source="store"))
        return "<no error raised>"
    except installer.InstallError as e:
        return str(e)
    except Exception as e:  # a stub drifted from the real signature
        return f"<unexpected {type(e).__name__}: {e}>"
    finally:
        installer.trust_store.list_active_keys = saved_keys
        installer.bundles.verify_any = saved_verify
        installer.installs_on_this_entity = saved_scope


print("1. installer.install_bundle — the one entry every source routes through")
retired_err = _install_bundle_reaching(RETIRED_SAMPLE)
check(f"a retired id ({RETIRED_SAMPLE}) is refused",
      "retired app id" in retired_err, retired_err)
check("the refusal names RETIRED_APPS so the reader can find the decision",
      "RETIRED_APPS" in retired_err, retired_err)

live_err = _install_bundle_reaching(LIVE_SAMPLE)
check(f"a live id ({LIVE_SAMPLE}) passes the retirement gate",
      "retired app id" not in live_err, live_err)
check("...and reaches the NEXT gate (proves it was not stopped earlier, e.g. "
      "by the provenance gate, which would make the case above vacuous)",
      "target_entities" in live_err, live_err)


# ── 2 & 3. the two store-sync lanes ───────────────────────────────────────────

class _FakeBridge:
    """Serves one catalog and, by default, refuses every bundle fetch.

    Refusing the fetch is deliberate: if a retired id ever reaches the fetch
    step the test records the id, which is a louder signal than a silent pass.

    `by_id` lets a case answer an UNPINNED fetch the way the real store does —
    latest non-yanked bundle plus its version — so the unlisted-app path can be
    exercised without a store.
    """

    def __init__(self, catalog: list[dict], by_id: dict[str, str] | None = None,
                 transient: set[str] | None = None) -> None:
        self.catalog = catalog
        self.by_id = by_id or {}
        self.transient = transient or set()
        self.fetched: list[str] = []
        self.unpinned: list[str] = []

    async def request_full(self, op: str, params: dict) -> dict:
        if op == "appstore.list":
            return {"ok": True, "data": {"apps": self.catalog}}
        if op == "appstore.fetch_bundle":
            app_id = params.get("app_id", "?")
            self.fetched.append(app_id)
            if not params.get("version"):
                self.unpinned.append(app_id)
                if app_id in self.by_id:
                    return {"ok": True, "data": {
                        "app_id": app_id,
                        "version": self.by_id[app_id],
                        "bundle_b64": "",  # install is not the subject here
                    }}
                if app_id in self.transient:
                    # What a store outage looks like on this lane: the adapter
                    # renders the controller status as "bundle fetch <status>".
                    return {"ok": False, "error": "bundle fetch 502"}
                return {"ok": False, "error": "bundle fetch 404"}
            return {"ok": False, "error": "fetch blocked by the gate test"}
        raise AssertionError(f"unexpected bridge op {op!r}")


class _App:
    """registry.list_active's row shape, narrowed to .id, .version and
    .authored_here (None: not built on this Host)."""

    def __init__(self, app_id: str, version: str) -> None:
        self.id = app_id
        self.version = version
        self.authored_here = None


def _with_registry(rows: list[_App], fn):
    """Run `fn` with registry.list_active and the offered-marker IO stubbed.

    _load_offered / _save_offered (default_install.py:53,60) read and write the
    marker on the entity's persistent workspace. Off-pod that path does not
    exist — _load_offered swallows the error and returns an empty set, which is
    what this test wants anyway, but stubbing makes that explicit rather than
    leaning on an except-Exception fallback, and keeps _save_offered from
    creating directories on whatever machine runs the gate.
    """
    saved_list = default_install.registry.list_active
    saved_load = default_install._load_offered
    saved_save = default_install._save_offered

    async def _list_active(_pool):
        return rows

    default_install.registry.list_active = _list_active
    default_install._load_offered = lambda: set()
    default_install._save_offered = lambda _s: None
    try:
        return fn()
    finally:
        default_install.registry.list_active = saved_list
        default_install._load_offered = saved_load
        default_install._save_offered = saved_save


print("\n2. default_install._install_default_set — the first-boot default set")
bridge = _FakeBridge([
    {"app_id": RETIRED_SAMPLE, "is_default": True, "latest_version": "9.9.9"},
    {"app_id": "app-store", "is_default": True, "latest_version": "1.1.3"},
])
# A fresh entity: only the kernel natives are present, nothing else.
natives = [_App(n, "1.0.0") for n in sorted(NATIVE_APPS)]
result = _with_registry(natives, lambda: asyncio.run(
    default_install._install_default_set(bridge, None)))
check(f"a retired is_default id ({RETIRED_SAMPLE}) is never fetched",
      RETIRED_SAMPLE not in bridge.fetched,
      f"fetched: {bridge.fetched}")
check("a live is_default id is still fetched (the filter is not a blanket stop)",
      "app-store" in bridge.fetched,
      f"fetched: {bridge.fetched} / result: {result}")

print("\n3. default_install._sync_store_upgrades — the upgrade sweep")
bridge2 = _FakeBridge([
    {"app_id": RETIRED_SAMPLE, "latest_version": "9.9.9"},
    {"app_id": "app-store", "latest_version": "1.1.3"},
])
installed = [_App(RETIRED_SAMPLE, "1.0.0"), _App("app-store", "1.1.2")]
result2 = _with_registry(installed, lambda: asyncio.run(
    default_install._sync_store_upgrades(bridge2, None)))
check(f"an installed retired id ({RETIRED_SAMPLE}) is not upgraded",
      RETIRED_SAMPLE not in bridge2.fetched,
      f"fetched: {bridge2.fetched}")
check("a live id with a newer store version is still upgraded",
      "app-store" in bridge2.fetched,
      f"fetched: {bridge2.fetched} / result: {result2}")


# ── 3b. the unlisted (audience='internal') lane ───────────────────────────────
#
# An app whose store row is audience='internal' never appears in appstore.list,
# so the old `if not store_row: continue` skipped it forever. Measured:
# four entities sat on claude-code 0.1.9 while the store served
# 0.2.1. Resolving by id fixes that WITHOUT widening discovery — the loop only
# walks apps already installed — but it must not become a way to pull store
# bundles over IMAGE-BAKED apps, which would downgrade them.

print("\n3b. an installed app that is not in the public catalog")
saved_baked = installer.image_baked_ids
try:
    installer.image_baked_ids = lambda: frozenset({"sites"})
    bridge3 = _FakeBridge(
        # Catalog carries neither app: 'unlisted-app' is internal, 'sites' is
        # image-baked and internal. Both are invisible here.
        [],
        by_id={"unlisted-app": "0.2.1", "sites": "0.5.2", "already-current": "1.0.0"},
        transient={"store-blipped"},
    )
    rows = [_App("unlisted-app", "0.1.9"),
            _App("sites", "0.13.1"),
            _App("already-current", "1.0.0"),
            _App("self-built", "0.1.0"),
            _App("store-blipped", "1.0.0")]
    result3 = _with_registry(rows, lambda: asyncio.run(
        default_install._sync_store_upgrades(bridge3, None)))
    upgraded = [u[0] if isinstance(u, tuple) else u for u in result3.get("upgraded", [])]
    check("an internal app IS resolved by id (the claude-code case)",
          "unlisted-app" in bridge3.unpinned,
          f"unpinned fetches: {bridge3.unpinned}")
    check("an IMAGE-BAKED app is never fetched from the store "
          "(the 2026-07-10 downgrade)",
          "sites" not in bridge3.fetched,
          f"fetched: {bridge3.fetched}")
    skipped_reasons = {s.get("app_id"): s.get("reason")
                       for s in result3.get("skipped", []) if isinstance(s, dict)}
    check("an unlisted app already at the store's latest is not reinstalled",
          skipped_reasons.get("already-current") == "already at the store's latest",
          f"skipped: {result3.get('skipped')}")
    check("an app with no store row at all is a quiet skip, not a failure",
          skipped_reasons.get("self-built") == "not in store"
          and not any((f[0] if isinstance(f, tuple) else f) == "self-built"
                      for f in result3.get("failed", [])),
          f"skipped: {result3.get('skipped')} / failed: {result3.get('failed')}")
    # A store outage must not be reported as "this app was withdrawn". Same
    # not-ok envelope, different meaning; conflating them would, during a 5xx,
    # tell the operator every internal app had vanished from the store.
    failed_ids = {(f[0] if isinstance(f, tuple) else f)
                  for f in result3.get("failed", [])}
    check("a transient store error is a FAILURE, not 'not in store'",
          "store-blipped" in failed_ids
          and skipped_reasons.get("store-blipped") != "not in store",
          f"failed: {result3.get('failed')} / skipped: {result3.get('skipped')}")
finally:
    installer.image_baked_ids = saved_baked


# ── 4. retired vs image-baked ─────────────────────────────────────────────────

print("\n4. set consistency — nothing retired may ship in the image")


def _bundled_ids(subdir: str) -> set[str]:
    """App ids of the signed bundles baked into the image, read from the
    bundles THEMSELVES rather than from the packer's NATIVE_APPS array.

    Reading the array would only cover the 19 native bundles; factory-bundles/
    carries 27, and it is install_native_bundles walking that directory at boot
    that would hit the new refusal. Both directories are tracked in git, so this
    stays a pure filesystem read.
    """
    d = _ROOT / subdir
    ids: set[str] = set()
    if not d.is_dir():
        return ids
    for f in sorted(d.glob("*.cseries")):
        with zipfile.ZipFile(f) as z:
            raw = z.read("manifest.yaml").decode("utf-8", "replace")
        m = re.search(r'^id:\s*"?([^"\s]+)"?', raw, re.M)
        if m:
            ids.add(m.group(1))
    return ids


factory = _bundled_ids("factory-bundles")
native = _bundled_ids("native-bundles")
check("the image bundles parse (a silent empty set would make this vacuous)",
      bool(factory), "no id read out of factory-bundles/*.cseries")
clash = sorted((factory | native) & RETIRED_APPS)
check(f"no retired id is baked into the image "
      f"({len(factory)} factory + {len(native)} native bundles)",
      not clash,
      "install_native_bundles installs these at every boot and the retirement "
      "sweep uninstalls them the same boot — and install_bundle now refuses "
      "them outright, so boot would log a failure forever:\n  "
      + ", ".join(clash))

# The packer's array is the other half of the same invariant: it decides what
# lands in native-bundles/ on the next rebuild, so a retired id added there
# would only show up in the check above AFTER someone reran the packer.
manifest = _ROOT / "scripts" / "image_baked_apps.txt"
try:
    packer_ids = {ln.strip() for ln in
                  manifest.read_text(encoding="utf-8").splitlines()
                  if ln.strip() and not ln.lstrip().startswith("#")}
except OSError:
    packer_ids = set()
check("the image-baked list reads out of scripts/image_baked_apps.txt",
      bool(packer_ids),
      "manifest missing or empty — a read that silently returns an empty set "
      "would make the next check vacuous")
packer_clash = sorted(packer_ids & RETIRED_APPS)
check("no retired id is queued for baking by the packer", not packer_clash,
      "rebuild_factory_bundles.sh would bake these on the next run:\n  "
      + ", ".join(packer_clash))

print("\n" + ("PASS" if not FAILED else f"{FAILED} FAILED"))
sys.exit(1 if FAILED else 0)
