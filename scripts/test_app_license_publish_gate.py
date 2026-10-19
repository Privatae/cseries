#!/usr/bin/env python3
"""The app licence gate: an author decides whether an installer may publish a
website from their app.

Covers the manifest half (the `license:` block parses, validates, and defaults
to DENY on the transferable rights) and the enforcement half
(applications.py:2363 _license_gate_site — allows / refuses on the installed
ROW, never on the tree's own claims).

ShipError's contract is applications.py:2076-2082 — (status, detail).

Run: python3 scripts/test_app_license_publish_gate.py
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.applications.manifest import (  # noqa: E402
    ALLOWED_FLAVOR_POLICY, LicenseSpec, ManifestError, load,
)

FAILED = []


def check(desc, ok, detail=""):
    print(("  ok  " if ok else "  FAIL") + "  " + desc
          + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILED.append(desc)


def write_manifest(d: Path, body: str) -> Path:
    p = d / "app.yaml"
    p.write_text(
        "id: demo-app\nname: Demo\nentrypoint: index.html\n" + body,
        encoding="utf-8",
    )
    return p


# -- 1. the manifest half --------------------------------------------------
print("manifest:")
with tempfile.TemporaryDirectory() as td:
    d = Path(td)

    m = load(write_manifest(d, ""))
    check("absent license block defaults to deny/deny/open",
          m.license == LicenseSpec(publish_site=False, redistribute=False,
                                   flavors="open", notes=""),
          repr(m.license))

    m = load(write_manifest(d, "license:\n  publish_site: true\n"))
    check("publish_site: true is read", m.license.publish_site is True)

    m = load(write_manifest(
        d, "license:\n  flavors: closed\n  notes: 'ask me first'\n"))
    check("flavors + notes are read",
          m.license.flavors == "closed" and m.license.notes == "ask me first",
          repr(m.license))

    try:
        load(write_manifest(d, "license:\n  flavors: whatever\n"))
        check("unknown flavors value is refused", False, "no ManifestError")
    except ManifestError as e:
        check("unknown flavors value is refused", "whatever" in str(e))

    try:
        load(write_manifest(d, "license:\n  publish_sites: true\n"))
        check("misspelled licence key is refused (not silently ignored)",
              False, "no ManifestError")
    except ManifestError as e:
        check("misspelled licence key is refused (not silently ignored)",
              "publish_sites" in str(e), str(e))

    try:
        load(write_manifest(d, "license: notamapping\n"))
        check("non-mapping license is refused", False, "no ManifestError")
    except ManifestError:
        check("non-mapping license is refused", True)

    # 'invited' is a store-schema value the manifest deliberately does not
    # offer yet — assert the manifest and the constant agree on that.
    check("manifest offers exactly open|closed",
          ALLOWED_FLAVOR_POLICY == frozenset({"open", "closed"}),
          repr(ALLOWED_FLAVOR_POLICY))

    # REGRESSION: bool("false") is True, so a QUOTED value would
    # have granted the right its author wrote out to withhold.
    for bad in ('"false"', '"true"', "'no'", "0", "1"):
        try:
            load(write_manifest(d, f"license:\n  publish_site: {bad}\n"))
            check(f"non-boolean publish_site {bad} is refused", False,
                  "no ManifestError — a string would read as a GRANT")
        except ManifestError as e:
            check(f"non-boolean publish_site {bad} is refused",
                  "must be a boolean" in str(e), str(e))
    m = load(write_manifest(d, "license:\n  publish_site: true\n"))
    check("a real YAML boolean still grants", m.license.publish_site is True)


# -- 2. the enforcement half -----------------------------------------------
print("gate:")
from server.routes import applications as A  # noqa: E402
from server.applications import registry as R  # noqa: E402


class FakeApp:
    """Stands in for registry.InstalledApp — only the fields the gate reads."""
    def __init__(self, name, license_, authored_here, provenance="community"):
        self.name = name
        self.app_license = license_
        self.authored_here = authored_here
        self.provenance = provenance


def run_gate(tree_manifest, installed):
    """Drive _license_gate_site over a temp tree, with registry.get stubbed.
    The gate imports the registry module and calls .get at call time, so
    replacing the module attribute is enough."""
    real_get = R.get

    async def fake_get(pool, app_id):
        return installed

    R.get = fake_get
    try:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            if tree_manifest is not None:
                (d / "app.yaml").write_text(tree_manifest, encoding="utf-8")
            return asyncio.run(A._license_gate_site(object(), d))
    finally:
        R.get = real_get


DENY = {"publish_site": False, "redistribute": False, "flavors": "open"}
ALLOW = {"publish_site": True, "redistribute": False, "flavors": "open"}

try:
    run_gate(None, None)
    check("a tree with no app.yaml publishes", True)
except A.ShipError as e:
    check("a tree with no app.yaml publishes", False, e.detail)

try:
    run_gate("id: demo-app\n", None)
    check("a declared app that is not installed publishes", True)
except A.ShipError as e:
    check("a declared app that is not installed publishes", False, e.detail)

try:
    run_gate("id: demo-app\n", FakeApp("Demo", DENY, False))
    check("someone else's app with publish_site=false is REFUSED", False,
          "no ShipError")
except A.ShipError as e:
    check("someone else's app with publish_site=false is REFUSED",
          e.status == 403 and "licence" in e.detail, f"{e.status}: {e.detail}")

try:
    run_gate("id: demo-app\n", FakeApp("Demo", ALLOW, False))
    check("someone else's app with publish_site=true publishes", True)
except A.ShipError as e:
    check("someone else's app with publish_site=true publishes", False, e.detail)

try:
    run_gate("id: demo-app\n", FakeApp("Demo", DENY, True))
    check("your own app publishes even when its licence denies", True)
except A.ShipError as e:
    check("your own app publishes even when its licence denies", False, e.detail)

# REGRESSION: a Workbench app built BEFORE the licence
# columns existed carries authored_here=NULL and an empty licence. The first
# cut refused its own author with "published by someone else". provenance
# 'custom' is the authorship signal — the entity's own key is the only one
# registered for it (entity_signing.py:85-89 vs trust_store.py:105/:144).
try:
    run_gate("id: demo-app\n", FakeApp("Demo", {}, None, provenance="custom"))
    check("a pre-column app of YOUR OWN still publishes (authored_here NULL)", True)
except A.ShipError as e:
    check("a pre-column app of YOUR OWN still publishes (authored_here NULL)",
          False, e.detail)

# The same NULL row, but the app came from the store: still deny.
try:
    run_gate("id: demo-app\n", FakeApp("Demo", {}, None, provenance="community"))
    check("a pre-column row from the STORE is treated as DENY", False,
          "no ShipError")
except A.ShipError as e:
    check("a pre-column row from the STORE is treated as DENY", e.status == 403)

try:
    run_gate("id: demo-app\nlicense:\n  publish_site: true\n",
             FakeApp("Demo", DENY, False))
    check("a copied manifest cannot grant itself the right", False,
          "no ShipError")
except A.ShipError as e:
    check("a copied manifest cannot grant itself the right", e.status == 403)

# a registry read that blows up must refuse, never open the gate
_real_get = R.get


async def _boom(pool, app_id):
    raise RuntimeError("db down")


R.get = _boom
try:
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "app.yaml").write_text("id: demo-app\n", encoding="utf-8")
        asyncio.run(A._license_gate_site(object(), d))
    check("a failed licence read REFUSES the publish", False, "no ShipError")
except A.ShipError as e:
    check("a failed licence read REFUSES the publish", e.status == 503,
          f"{e.status}")
finally:
    R.get = _real_get


# ── 3. the zip lane ───────────────────────────────────────────────────────
# runtime/tools.py manage_site op='publish' hands prebuilt BYTES to
# webapp.publish_bundle. Neither tree gate saw it, and the manifest survives
# inside the bundle, so it was a working way to put someone else's app on the
# public web.
print("zip lane:")
import io  # noqa: E402
import zipfile  # noqa: E402


def make_zip(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, body in entries.items():
            zf.writestr(name, body)
    return buf.getvalue()


def run_zip_gate(entries, installed):
    real_get = R.get

    async def fake_get(pool, app_id):
        return installed

    R.get = fake_get
    try:
        return asyncio.run(A._license_gate_site_zip(object(), make_zip(entries)))
    finally:
        R.get = real_get


check("a bundle's app id is read out of the zip",
      A._declared_app_id_in_zip(make_zip({"app.yaml": "id: demo-app\n"})) == "demo-app")
check("the SHALLOWEST app.yaml wins, so a decoy cannot shadow it",
      A._declared_app_id_in_zip(make_zip({
          "app.yaml": "id: real-app\n", "vendor/x/app.yaml": "id: decoy\n",
      })) == "real-app")
check("a non-zip is tolerated, not raised",
      A._declared_app_id_in_zip(b"not a zip at all") == "")

try:
    run_zip_gate({"app.yaml": "id: demo-app\n", "index.html": "<i>"},
                 FakeApp("Demo", DENY, False))
    check("ZIP LANE: someone else's app is REFUSED", False, "no ShipError")
except A.ShipError as e:
    check("ZIP LANE: someone else's app is REFUSED", e.status == 403,
          f"{e.status}: {e.detail}")

try:
    run_zip_gate({"app.yaml": "id: demo-app\n"}, FakeApp("Demo", ALLOW, False))
    check("ZIP LANE: an allowed licence publishes", True)
except A.ShipError as e:
    check("ZIP LANE: an allowed licence publishes", False, e.detail)

try:
    run_zip_gate({"index.html": "<i>"}, None)
    check("ZIP LANE: a plain site bundle publishes", True)
except A.ShipError as e:
    check("ZIP LANE: a plain site bundle publishes", False, e.detail)


print()
if FAILED:
    print(f"{len(FAILED)} FAILED:")
    for f in FAILED:
        print("  - " + f)
    sys.exit(1)
print("all green")
