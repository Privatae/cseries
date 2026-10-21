#!/bin/sh
# Turns a fresh Debian 13 machine into a C-Series Cloud box. Run once as root
# by the provisioner, over the provider's first-boot SSH:
#
#   bootstrap.sh          set the box up from /etc/cseries-cloud/bootstrap.json
#   bootstrap.sh seal     close the provisioner's way in; its last command
#
# Running it again finishes what an earlier run left undone. It prints ONE
# JSON line on stdout (the result cseries-cloud-bootstrap describes);
# everything else goes to stderr.
#
# The overlay (the box's files, vm/cloud/build-overlay.sh) comes from, in
# order: the directory this script sits in, when it is an unpacked overlay;
# bootstrap.json's overlay_archive, a tarball already on this machine; or the
# signed cloud release at bootstrap.json's release_base, verified against the
# pinned release keys before anything in it runs.
set -eu
umask 022

BOOT=/etc/cseries-cloud/bootstrap.json
STAGE=/var/lib/cseries-cloud/bootstrap
PACKAGES="podman crun conmon netavark aardvark-dns nftables caddy lego python3 squid
python3-cryptography unattended-upgrades apt-listchanges needrestart ca-certificates curl jq
age"

die() {
    printf '{"ok": false, "step": "%s", "error": "%s"}\n' "$1" "$2"
    exit 1
}
say() { printf 'bootstrap.sh: %s\n' "$*" >&2; }

[ "$(id -u)" = 0 ] || die start "run bootstrap.sh as root"
case "${1:-}" in
    seal)
        exec python3 -I /usr/local/sbin/cseries-cloud-bootstrap seal
        ;;
    "") ;;
    *)
        echo "usage: bootstrap.sh [seal]" >&2
        exit 2
        ;;
esac
[ -f "$BOOT" ] || die start "$BOOT is missing"

say "packages"
export DEBIAN_FRONTEND=noninteractive
missing=""
for p in $PACKAGES; do
    dpkg-query -W -f='${db:Status-Status}' "$p" 2>/dev/null | grep -qx installed || missing="$missing $p"
done
if [ -n "$missing" ]; then
    say "installing:$missing"
    apt-get update -qq >&2 || die packages "apt-get update failed"
    # shellcheck disable=SC2086 # the list is words
    apt-get install -y -qq --no-install-recommends $PACKAGES >&2 || die packages "apt-get install failed"
fi

here=$(cd "$(dirname "$0")" && pwd)
if [ -d "$here/root" ] && [ -f "$here/VERSION" ]; then
    overlay=$here
else
    archive=$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1])).get("overlay_archive") or "")' "$BOOT") ||
        die overlay "bootstrap.json is not JSON"
    rm -rf "$STAGE"
    mkdir -p "$STAGE/unpacked"
    chmod 0700 "$STAGE"
    if [ -n "$archive" ]; then
        say "overlay from $archive"
        cp "$archive" "$STAGE/overlay.tar.gz" || die overlay "cannot read $archive"
    else
        say "overlay from the signed cloud release"
        # The release's manifest, verified the way packages/cseries/src/verify.js
        # verifies the bastion's, before its overlay is fetched or unpacked.
        python3 -I - "$BOOT" "$STAGE/overlay.tar.gz" <<'EOF' >&2 || die overlay "the cloud release could not be verified or fetched"
import base64, hashlib, json, sys, urllib.parse, urllib.request
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

KIND = "cseries-cloud"
KEYS = ("tWOyZm38DwTwbawSnvExQG/CQsVJBKYWc46arCBZj9E=",
        "CKL+J0VSdqrfY4AmxChvylJ1ha4e8GvcrYF2o17Ah+g=")
DEFAULT_BASE = "https://github.com/Privatae/cseries/releases/latest/download"

def get(url, cap):
    req = urllib.request.Request(url, headers={"User-Agent": "cseries-cloud/1"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read(cap + 1)
    if len(data) > cap:
        raise SystemExit(f"{url} is larger than {cap} bytes")
    return data

def b64(s):
    s = "".join(str(s).split())
    return base64.b64decode(s + "=" * (-len(s) % 4), validate=True)

def verify(kind, body, sig_text, keys=KEYS):
    m = json.loads(body.decode("utf-8"))
    v = m.get("version") if isinstance(m, dict) else None
    if not isinstance(v, str) or not v or "\r" in v or "\n" in v:
        raise SystemExit("the release manifest names no usable version")
    sig = b64(sig_text)
    if len(sig) != 64:
        raise SystemExit("the release signature is malformed")
    msg = f"privatae-release-signature-v1\n{kind}\n{v}\n{hashlib.sha256(body).hexdigest()}\n".encode()
    for k in keys:
        raw = b64(k)
        if len(raw) != 32:
            continue
        try:
            Ed25519PublicKey.from_public_bytes(raw).verify(sig, msg)
            return m
        except InvalidSignature:
            pass
    raise SystemExit("the release signature does not verify against any pinned release key")

if __name__ == "__main__":
    boot = json.load(open(sys.argv[1]))
    url = boot.get("release_base", DEFAULT_BASE).rstrip("/") + "/cloud-release.json"
    body = get(url, 64 * 1024)
    u = urllib.parse.urlsplit(url)
    sig_url = urllib.parse.urlunsplit(u._replace(path=f"{u.path}.{hashlib.sha256(body).hexdigest()[:16]}.sig"))
    sig = get(sig_url, 4096)
    m = verify(KIND, body, sig.decode("ascii", "replace"))
    ov = m["overlay"]
    if not str(ov.get("url", "")).startswith("https://"):
        raise SystemExit("the overlay URL is not https")
    data = get(ov["url"], 64 * 1024 * 1024)
    if hashlib.sha256(data).hexdigest() != ov["sha256"]:
        raise SystemExit("the overlay does not match the signed manifest's sha256")
    open(sys.argv[2], "wb").write(data)
    # Kept beside the overlay, so the bootstrap takes its images from this
    # same release instead of fetching the manifest again.
    stage = sys.argv[2].rsplit("/", 1)[0]
    open(f"{stage}/cloud-release.json", "wb").write(body)
    open(f"{stage}/cloud-release.json.sig", "wb").write(sig)
    print(f"cloud release {m['version']} verified")
EOF
    fi
    # Only files and directories, nothing outside the target (tarfile's data filter).
    python3 -I -c '
import sys, tarfile
with tarfile.open(sys.argv[1], "r:gz") as t:
    for m in t.getmembers():
        if not (m.isfile() or m.isdir()):
            sys.exit(f"the overlay holds {m.name!r}, not a file or directory")
    t.extractall(sys.argv[2], filter="data")
' "$STAGE/overlay.tar.gz" "$STAGE/unpacked" || die overlay "the overlay could not be unpacked"
    overlay=$STAGE/unpacked
fi
[ -f "$overlay/root/usr/local/sbin/cseries-cloud-bootstrap" ] || die overlay "the overlay has no cseries-cloud-bootstrap"

# The library and the bootstrap itself go in place first, so the bootstrap
# runs from where every later run of it, and the updater, will find it.
install -D -m 0644 "$overlay/root/usr/local/lib/cseries-cloud/cseries_cloud.py" \
    /usr/local/lib/cseries-cloud/cseries_cloud.py
install -D -m 0755 "$overlay/root/usr/local/sbin/cseries-cloud-bootstrap" \
    /usr/local/sbin/cseries-cloud-bootstrap
exec python3 -I /usr/local/sbin/cseries-cloud-bootstrap run --overlay "$overlay"
