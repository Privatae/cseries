#!/usr/bin/env python3
"""scripts/build_store_catalog.py, the free public App Store's catalog writer.

  1. The same bundles give the same catalog.json, byte for byte, and the
     bundles are copied out under <app_id>-<version>.cseries.
  2. The store lane only: an app baked into the Host image, a retired id and
     a platform tool are left out.
  3. A bundle the installer would refuse stops the build: one signed by a key
     that is neither the C-Series apps key nor the community key, one signed
     with the C-Series key that declares community, one signed with the
     community key that declares standard.
  4. The entries carry what a Host reads: absolute URLs under --download-base
     (relative without it), the sha256 and size of the copied bytes, the
     runtime images the manifest uses, the licence, and the source link; the
     catalog validates and, once signed, verifies in
     server/applications/public_store.py.
  5. Over the repository's own factory-bundles with its own keys, the build
     succeeds and lists no image-baked app.
  6. The App Store, signed with the C-Series key, is the one default entry: a
     Host installs it at its first start. A community bundle under the same id
     is not a default.

Container-free: python3 scripts/test_build_store_catalog.py
"""
import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT))
sys.modules.setdefault("server.main", types.ModuleType("server.main"))

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from server.applications import bundles  # noqa: E402

FAILS: list[str] = []
SCRIPT = ROOT / "scripts" / "build_store_catalog.py"


def check(label, cond, detail=""):
    print(f"[store-catalog] {'ok  ' if cond else 'FAIL'} {label}"
          + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


if not SCRIPT.is_file():
    check("scripts/build_store_catalog.py exists", False)
    sys.exit(1)

W = Path(tempfile.mkdtemp(prefix="storecat-"))


def newkey():
    k = Ed25519PrivateKey.generate()
    pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                          serialization.NoEncryption())
    pub = k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return k, pem, pub


apps_k, apps_pem, apps_pub = newkey()       # stands in for the C-Series apps key
comm_k, comm_pem, comm_pub = newkey()       # stands in for the community key
_, stranger_pem, _ = newkey()
(W / "apps.pub").write_bytes(apps_pub)
(W / "community.pub").write_bytes(comm_pub)


def bundle(app_id, version, pem, provenance="standard", runtime="", extra=""):
    src = W / "src" / f"{app_id}-{version}-{provenance}"
    src.mkdir(parents=True, exist_ok=True)
    (src / "app.yaml").write_text(
        f"id: {app_id}\nname: {app_id.title()}\ndescription: Invented app {app_id}.\n"
        f'version: "{version}"\nprovenance: {provenance}\nentrypoint: index.html\n'
        f"permissions: []\n{runtime}{extra}")
    (src / "index.html").write_text(f"<!doctype html><title>{app_id}</title>")
    return bundles.sign(bundles.pack(src), pem)


def lane(dirname, items):
    d = W / dirname
    d.mkdir()
    for name, data in items.items():
        (d / name).write_bytes(data)
    return d


baked = W / "baked.txt"
baked.write_text("# invented baked set\nbaked-thing\n")
PROC = ("network: []\nresources:\n  baseline_memory_mi: 128\nruntime:\n  kind: process\n"
        "  image: python\n  command: [\"python3\", \"main.py\"]\n  ready_path: /health\n")
good = {
    "notes.cseries": bundle("note-pad", "1.0.0", apps_pem),
    "bot.cseries": bundle("chat-bot", "2.1.0", apps_pem, runtime=PROC),
    "shared.cseries": bundle("shared-board", "0.3.0", comm_pem, provenance="community",
                             extra="author: Invented Maker\n"),
    "baked.cseries": bundle("baked-thing", "1.0.0", apps_pem),
    "retired.cseries": bundle("pulse", "1.0.0", apps_pem),
    "tool.cseries": bundle("training-memory", "1.0.0", apps_pem),
    "store.cseries": bundle("app-store", "3.0.0", apps_pem),
}
D = "sha256:" + "e" * 64
(W / "images.json").write_text(json.dumps({"python": "ghcr.io/example/runtime-python" + "@" + D}))


def build(bdir, out, *extra):
    return subprocess.run([sys.executable, str(SCRIPT), "--version", "0.7.0", "--bundles", str(bdir),
                           "--out", str(out), "--epoch", "1700000000",
                           "--cseries-key", str(W / "apps.pub"),
                           "--community-key", str(W / "community.pub"),
                           "--baked", str(baked), *extra],
                          capture_output=True, text=True)


bdir = lane("good", good)
r1 = build(bdir, W / "out1", "--download-base", "https://example.test/rel/v0.7.0",
           "--images", str(W / "images.json"), "--source-base", "https://example.test/src/v0.7.0")
r2 = build(bdir, W / "out2", "--download-base", "https://example.test/rel/v0.7.0",
           "--images", str(W / "images.json"), "--source-base", "https://example.test/src/v0.7.0")
check("the build succeeds", r1.returncode == 0, r1.stderr + r1.stdout)
cat1 = (W / "out1" / "catalog.json").read_bytes() if r1.returncode == 0 else b""
cat2 = (W / "out2" / "catalog.json").read_bytes() if r2.returncode == 0 else b""
check("the same bundles give the same catalog.json, byte for byte", cat1 and cat1 == cat2)
doc = json.loads(cat1 or b"{}")
ids = [a["app_id"] for a in doc.get("apps", [])]
check("the store lane only: no baked, retired or platform-tool app",
      ids == ["app-store", "chat-bot", "note-pad", "shared-board"], ids)
check("each listed bundle is copied out under <app_id>-<version>.cseries, byte for byte",
      (W / "out1" / "note-pad-1.0.0.cseries").read_bytes() == good["notes.cseries"]
      and not (W / "out1" / "baked-thing-1.0.0.cseries").exists())
by = {a["app_id"]: a for a in doc.get("apps", [])}
np_ = by.get("note-pad", {})
check("an entry's URL is absolute under --download-base, with the copied bytes' sha256 and size",
      np_.get("bundle") == {"url": "https://example.test/rel/v0.7.0/note-pad-1.0.0.cseries",
                            "sha256": hashlib.sha256(good["notes.cseries"]).hexdigest(),
                            "size": len(good["notes.cseries"])}, np_)
check("an entry is free and not a default", np_.get("price_micros") == 0 and np_.get("is_default") is False)
check("the App Store signed with the C-Series key is the one default entry",
      [a["app_id"] for a in doc.get("apps", []) if a.get("is_default") is True] == ["app-store"],
      [(a["app_id"], a.get("is_default")) for a in doc.get("apps", [])])
check("a first-party app is the repository's licence, published by C-Series",
      np_.get("license") == "AGPL-3.0-only" and np_.get("publisher") == "C-Series")
check("a process app lists the runtime image it pulls, pinned by digest",
      by.get("chat-bot", {}).get("images") == {"python": "ghcr.io/example/runtime-python" + "@" + D}
      and np_.get("images") == {}, by.get("chat-bot"))
check("a community bundle is listed as community, under its author",
      by.get("shared-board", {}).get("provenance") == "community"
      and by.get("shared-board", {}).get("publisher") == "Invented Maker")
check("generated_at comes from the epoch", doc.get("generated_at") == "2023-11-14T22:13:20Z")
# The time the catalog was issued, inside the bytes the signature covers: a
# Host warns when it is older than 45 days. By default the epoch's, so a re-run
# of the release workflow builds the same bytes (asserted above).
check("issued_at is written, from the epoch by default", doc.get("issued_at") == "2023-11-14T22:13:20Z", doc)
r4 = build(bdir, W / "out4", "--issued-at", "1760000000")
doc4 = json.loads((W / "out4" / "catalog.json").read_text()) if r4.returncode == 0 else {}
check("--issued-at sets it", doc4.get("issued_at") == "2025-10-09T08:53:20Z", (r4.stderr, doc4.get("issued_at")))
check("the printed summary names the sha256 and the release-sign command for cseries-store",
      hashlib.sha256(cat1).hexdigest() in r1.stdout
      and "release-sign sign cseries-store 0.7.0 " in r1.stdout, r1.stdout)

r3 = build(bdir, W / "out3")
rel_doc = json.loads((W / "out3" / "catalog.json").read_text()) if r3.returncode == 0 else {}
check("without --download-base the URLs are relative to the catalog",
      [a["bundle"]["url"] for a in rel_doc.get("apps", [])] ==
      ["app-store-3.0.0.cseries", "chat-bot-2.1.0.cseries", "note-pad-1.0.0.cseries",
       "shared-board-0.3.0.cseries"])

d = lane("community-store", {"c.cseries": bundle("app-store", "3.0.0", comm_pem, provenance="community",
                                                extra="author: Invented Maker\n")})
r5 = build(d, W / "out5")
doc5 = json.loads((W / "out5" / "catalog.json").read_text()) if r5.returncode == 0 else {}
check("a community bundle under the App Store's id is not a default",
      [a.get("is_default") for a in doc5.get("apps", [])] == [False], (r5.stderr, doc5.get("apps")))

for what, data in [
    ("a bundle signed by neither key", bundle("odd-one", "1.0.0", stranger_pem)),
    ("a C-Series-signed bundle that declares community",
     bundle("mislabel", "1.0.0", apps_pem, provenance="community")),
    ("a community-signed bundle that declares standard",
     bundle("climber", "1.0.0", comm_pem, provenance="standard")),
]:
    d = lane(f"bad-{what[:12].replace(' ', '-')}", {"x.cseries": data, **{"notes.cseries": good["notes.cseries"]}})
    r = build(d, W / f"o-{d.name}")
    check(f"{what} stops the build", r.returncode != 0 and "x.cseries" in r.stderr
          and not (W / f"o-{d.name}" / "catalog.json").exists(), r.stderr + r.stdout)

# What the Host reads: it validates, and once signed it verifies.
from server.applications import public_store as PS  # noqa: E402
try:
    v = PS.validate_catalog(doc)
    validated = sorted(v["apps"]) == ids and v.get("issued_at") == 1700000000
except PS.StoreError as e:
    validated = False
    print(e)
check("the catalog validates in the Host's reader", validated)
rel = Ed25519PrivateKey.generate()
sig = base64.b64encode(rel.sign(PS.statement(PS.KIND, "0.7.0", hashlib.sha256(cat1).hexdigest()))).decode()
pub = base64.b64encode(rel.public_key().public_bytes(serialization.Encoding.Raw,
                                                     serialization.PublicFormat.Raw)).decode()
check("signed for cseries-store, it verifies in the Host's reader",
      PS.verify_signed(cat1, sig, keys=[pub]) == doc)

# The repository's own bundles, with its own keys.
r = subprocess.run([sys.executable, str(SCRIPT), "--version", "0.0.1", "--out", str(W / "repo"),
                    "--epoch", "0"], capture_output=True, text=True)
check("over the repository's factory-bundles with its own keys, the build succeeds",
      r.returncode == 0, r.stderr[-800:])
if r.returncode == 0:
    repo_ids = {a["app_id"] for a in json.loads((W / "repo" / "catalog.json").read_text())["apps"]}
    baked_ids = {line.split("#", 1)[0].strip()
                 for line in (ROOT / "scripts" / "image_baked_apps.txt").read_text().splitlines()
                 if line.split("#", 1)[0].strip()}
    check("and lists no app baked into the Host image", not (repo_ids & baked_ids),
          sorted(repo_ids & baked_ids))
    check("and lists the messaging connectors", {"telegram", "discord", "slack"} <= repo_ids,
          sorted(repo_ids))
    repo_defaults = [a["app_id"] for a in json.loads((W / "repo" / "catalog.json").read_text())["apps"]
                     if a["is_default"]]
    check("and marks the App Store, and only it, default", repo_defaults == ["app-store"],
          repo_defaults)

if FAILS:
    print(f"\n[store-catalog] {len(FAILS)} FAILED")
    sys.exit(1)
print("\n[store-catalog] all checks passed")
