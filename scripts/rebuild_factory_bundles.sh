#!/usr/bin/env bash
# Rebuild every factory-bundle from system-apps/ source. Used before
# `docker build` so the .cseries files baked into the image carry the latest
# UI source, not whatever was in the repo when the bundles were last
# regenerated. Stale bundles otherwise ship old UI into
# /var/lib/cseries/apps/ through the install pipeline.
set -euo pipefail

# Repo root = the checkout this script lives in. CSERIES_REPO overrides.
REPO="${CSERIES_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
# The Ed25519 private key (PEM) whose public half is etc/cseries-apps.pub.raw.
SIGN_KEY="${SIGN_KEY:?set SIGN_KEY to the Ed25519 private key (PEM) that signs the built-in apps}"
OUT_DIR="$REPO/factory-bundles"
NATIVE_OUT_DIR="$REPO/native-bundles"
CLI="${PYTHON:-python3} $REPO/cli/cseries.py"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# Ids whose .cseries bundle ships in the image (/app/native-bundles/).
# THE LIST LIVES IN scripts/image_baked_apps.txt — one source, three
# consumers (this packer, smoke_test.sh's image gate, and
# test_store_version_bump.py's store-lane ratchet). The per-id history
# notes moved there with it. Two reasons for an id to be on the list:
#   1. Kernel-required (server/applications/kernel.py::KERNEL_REQUIRED_APPS)
#      — installed on every entity; refuse-to-boot if missing.
#   2. Image-baked-conditional — bundle ships in every image but
#      install_native_bundles() gates install via the manifest's
#      `target_entities`. Used for per-entity apps that should not go
#      through the app store.
IMAGE_BAKED_MANIFEST="$REPO/scripts/image_baked_apps.txt"
mapfile -t IMAGE_BAKED_APPS < <(grep -Ev '^[[:space:]]*(#|$)' "$IMAGE_BAKED_MANIFEST" 2>/dev/null)
if [[ ${#IMAGE_BAKED_APPS[@]} -eq 0 ]]; then
    echo "fatal: $IMAGE_BAKED_MANIFEST missing or empty — refusing to build an" >&2
    echo "       image with zero baked bundles (that strands every app's UI" >&2
    echo "       on existing entities; see the 2026-05-19 + 2026-07-10 strands)" >&2
    exit 1
fi

if [[ ! -f "$SIGN_KEY" ]]; then
    echo "fatal: signing key missing at $SIGN_KEY" >&2
    exit 1
fi

mkdir -p "$OUT_DIR" "$NATIVE_OUT_DIR"
# Wipe stale native-bundles so a removed native id doesn't ghost in the image.
rm -f "$NATIVE_OUT_DIR"/*.cseries

shopt -s nullglob
count=0
native_count=0
for app_dir in "$REPO/system-apps"/*/; do
    name="$(basename "$app_dir")"
    [[ "$name" == "_template" ]] && continue
    [[ ! -f "$app_dir/app.yaml" ]] && {
        echo "skip $name (no app.yaml)"
        continue
    }
    unsigned="$TMP/$name.unsigned.cseries"
    signed="$OUT_DIR/$name.cseries"
    cd "$REPO"
    $CLI pack "$app_dir" --out "$unsigned" >/dev/null
    $CLI sign "$unsigned" --key "$SIGN_KEY" --out "$signed" >/dev/null
    size=$(stat -c%s "$signed")
    echo "  built $name.cseries ($size bytes)"
    count=$((count + 1))

    # Mirror to native-bundles/ if this id is in the native set —
    # these are the only bundles baked into the image.
    for n in "${IMAGE_BAKED_APPS[@]}"; do
        if [[ "$name" == "$n" ]]; then
            cp "$signed" "$NATIVE_OUT_DIR/$name.cseries"
            native_count=$((native_count + 1))
            break
        fi
    done
done

echo "rebuilt $count bundles → $OUT_DIR (store + factory)"
echo "mirrored $native_count native bundles → $NATIVE_OUT_DIR (image-baked)"
