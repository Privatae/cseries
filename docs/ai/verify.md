# Verifying a release

A release `vX.Y.Z` is built by [`.github/workflows/release.yml`](../../.github/workflows/release.yml)
from the tag; [docs/RELEASING.md](../RELEASING.md) describes everything it
publishes. These checks show that what you downloaded is what that workflow
built, and that the signed manifests were signed with the pinned keys. They
do not show that the code is safe. Replace `X.Y.Z` with the version.

## 1. The npm package and what it pins

The `cseries` package carries `release.json`, which pins the image digests,
the runtime images' digests, and the guest images' URLs and sha256s.

```sh
npm pack cseries@X.Y.Z
tar xzf cseries-X.Y.Z.tgz
cat package/release.json
```

The package is published from the workflow with npm provenance. To check its
registry signature and provenance attestation, install it in an empty
project and run npm's own check:

```sh
npm init -y && npm install --ignore-scripts cseries@X.Y.Z
npm audit signatures
```

## 2. The images

`npx cseries` pulls the images by the digests in `release.json`. Each image
and its multi-arch index carry a build provenance attestation from the
workflow. With GitHub's CLI:

```sh
gh attestation verify oci://ghcr.io/privatae/cseries@<digest> --repo Privatae/cseries
gh attestation verify oci://ghcr.io/privatae/cseries-postgres@<digest> --repo Privatae/cseries
gh attestation verify oci://ghcr.io/privatae/cseries-remote-door@<digest> --repo Privatae/cseries
```

The third is the remote-access door. The runtime images
(`cseries-app-runtime-python`, `cseries-browser`, `cseries-app-helper`,
`cseries-app-runtime-creative`) are checked the same way. They also carry an
SPDX SBOM attestation; docs/RELEASING.md gives the command to read it.

## 3. The guest images

Each guest image on the GitHub release has a `.sha256` beside it, and
`release.json` pins the same value. The installer refuses a guest image whose
sha256 differs.

```sh
base=https://github.com/Privatae/cseries/releases/download/vX.Y.Z
curl -fsSLO "$base/cseries-guest-X.Y.Z-x64.qcow2"
curl -fsSLO "$base/cseries-guest-X.Y.Z-x64.qcow2.sha256"
sha256sum -c cseries-guest-X.Y.Z-x64.qcow2.sha256
```

The WSL images are `cseries-wsl-rootfs-X.Y.Z-<x64|arm64>.tar.gz`, the Lima
images `cseries-guest-X.Y.Z-<x64|arm64>.qcow2`.

## 4. The signed manifests

Three manifests are signed with Ed25519 release keys. The signature sits
beside the manifest as `<name>.<first 16 hex digits of its sha256>.sig`, over
the statement `privatae-release-signature-v1`, the kind, the manifest's
version and its sha256, one per line. Two public keys are pinned in
`packages/cseries/src/verify.js` (`BASTION_RELEASE_KEYS`), and the Host and
the C-Series Cloud server pin the same two.

| Manifest | Where | Kind | Checked by |
|---|---|---|---|
| The bastion's release manifest | `https://privatae.ai/bastion/version.json` | `bastion` | `npx cseries`, before installing the bastion (`packages/cseries/src/bastion.js`) |
| The App Store catalog | `catalog.json` on the GitHub release | `cseries-store` | The Host, before it uses the catalog (`server/applications/public_store.py`) |
| The C-Series Cloud release | `cloud-release.json` on the GitHub release | `cseries-cloud` | A C-Series Cloud server (`vm/cloud/overlay/usr/local/lib/cseries-cloud/cseries_cloud.py`) |

To check one with the installer's own code, from the folder where you
unpacked the npm package in step 1:

```sh
f=catalog.json kind=cseries-store
curl -fsSLO "https://github.com/Privatae/cseries/releases/download/vX.Y.Z/$f"
sig="$f.$(sha256sum "$f" | cut -c1-16).sig"
curl -fsSLO "https://github.com/Privatae/cseries/releases/download/vX.Y.Z/$sig"
node -e 'const v=require("./package/src/verify.js"),fs=require("fs");
  const [k,f,s]=process.argv.slice(1);
  console.log(v.verifySignedManifest(k,fs.readFileSync(f),fs.readFileSync(s,"utf8")).version)' "$kind" "$f" "$sig"
```

It prints the version, or throws if the signature does not verify against a
pinned key. For the bastion, fetch `version.json` and its `.sig` from
`https://privatae.ai/bastion/` and use `kind=bastion`; for the cloud release,
`f=cloud-release.json kind=cseries-cloud`. The bastion's manifest names each
binary's sha256, which `npx cseries` checks after downloading.

The catalog's and the cloud release's signatures are added by hand after the
workflow runs (docs/RELEASING.md, section 3), so for a short time after a
release they may not be there yet. A Host or a cloud server refuses the
manifest until they are.
