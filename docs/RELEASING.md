# Releasing C-Series

A release has one version number, X.Y.Z, taken from a tag `vX.Y.Z` on
github.com/Privatae/cseries. Pushing the tag runs
`.github/workflows/release.yml`, which publishes:

- **The images**, `ghcr.io/privatae/cseries:X.Y.Z` and
  `ghcr.io/privatae/cseries-postgres:X.Y.Z`, each a multi-arch index
  (linux/amd64 and linux/arm64). Each architecture is built on a runner of
  that architecture, then checked before anything is pushed: trivy's secret
  scan, the vulnerability gate (`trivy.yaml`, with the accepted findings in
  `.trivyignore.yaml`), `scripts/test_public_source_clean.py` over the image's
  `/app`. The full smoke test (`scripts/smoke_test.sh`) is not run here: it
  runs on the build machine before the tag, in the full gate below. The
  checked image is pushed by digest with a provenance attestation, and the
  index is made from exactly those digests. The remote-access door's image,
  `ghcr.io/privatae/cseries-remote-door:X.Y.Z` (`sidecars/remote-door/
  Dockerfile`; a member's machine runs its door and certificate job from
  it), is built, scanned, pushed and tagged in the same jobs and the same
  way; it has no `/app`, so the public-source gate over the image does not
  apply to it.
- **The runtime images**, which a machine's local runtime starts the Host's
  app, browser and creative containers from, each built from this tree at
  the tag, checked the same way as the two images above (trivy's secret
  scan, the vulnerability gate and a short smoke test of what the runtime
  uses), pushed by digest with a provenance attestation and an SPDX SBOM
  attestation (trivy's list of every package in the tested archive; read it
  with `gh attestation verify oci://<image>@<digest> --repo Privatae/cseries
  --predicate-type https://spdx.dev/Document/v2.3 --format json`), and tagged
  X.Y.Z from exactly the tested digests:

  | Image | Built from | Architectures |
  |---|---|---|
  | `ghcr.io/privatae/cseries-app-runtime-python` | `sidecars/app-runtime-python/Dockerfile` | amd64, arm64 |
  | `ghcr.io/privatae/cseries-browser` | `sidecars/browser/Dockerfile` | amd64, arm64 |
  | `ghcr.io/privatae/cseries-app-helper` | `sidecars/app-helper/Dockerfile` | amd64, arm64 |
  | `ghcr.io/privatae/cseries-app-runtime-creative` | `sidecars/app-runtime-creative/Dockerfile` | amd64 only |

  The creative image is amd64 only because Blender and the ArtCraft
  programs it carries are downloaded as x86-64 builds. The coding engines'
  images are not published at all: each machine builds the ones its owner
  turns on, from the harness source tarball below.
- **The GitHub release** `vX.Y.Z`, created as a pre-release (it becomes the
  latest release only at the end of section 3), with the WSL guest images
  `cseries-wsl-rootfs-X.Y.Z-x64.tar.gz` and `cseries-wsl-rootfs-X.Y.Z-arm64.tar.gz`
  and the Lima guest images (macOS and Linux, built by `vm/lima/build-image.sh`)
  `cseries-guest-X.Y.Z-x64.qcow2` and `cseries-guest-X.Y.Z-arm64.qcow2`,
  each with its `.sha256`, and beside them:
  - `cseries-harness-src-X.Y.Z.tar.gz` and its `.sha256`, built by
    `vm/runtime/build-harness-src.sh`: the coding engines' Dockerfiles and
    the files they copy, which a machine builds an engine's image from. The
    manifests below pin its sha256.
  - `blender-<version>.tar.xz` and its `.sha256`: the source for exactly
    the Blender the creative image carries, which the GPL requires us to
    offer with the image. blender.org publishes only an md5sum beside its
    source archives, so the workflow checks that md5sum and also the
    sha256 pinned in the workflow (`BLENDER_SOURCE_VERSION` and
    `BLENDER_SOURCE_SHA256`), and refuses a creative Dockerfile whose
    Blender is another version. Bump the two together with the Dockerfile.
  - one `<image>-X.Y.Z-<arch>-notices.txt` per runtime image and
    architecture: every Debian package in the image with its source
    package and the licences its copyright file names, every Python
    distribution with its licence, and what the image adds.
- **The C-Series Cloud release**, on the same GitHub release:
  `cseries-cloud-overlay-X.Y.Z.tar.gz` (every file a cloud box installs, built
  by `vm/cloud/build-overlay.sh`) with its `.sha256`, and `cloud-release.json`,
  which pins the overlay, the two images above, the four runtime images and
  the harness source tarball. A cloud box takes it only once its signature
  is beside it, which the maintainer adds by hand (section 3).
- **The free App Store**, on the same GitHub release: `catalog.json`, which
  lists every store app with its bundle's URL on this release and sha256,
  the runtime images it needs pinned by digest, its licence and its source,
  and every `<app_id>-<version>.cseries` bundle it names, written by
  `scripts/build_store_catalog.py` from `factory-bundles/`. A Host takes the
  catalog only once its signature is beside it, which the maintainer adds
  by hand (section 3).
- **The npm package** `cseries` (`packages/cseries`), at X.Y.Z, with trusted
  publishing and provenance. First the workflow writes `packages/cseries/release.json`
  with the images' digests, the runtime images' digests, the harness source
  tarball's URL and sha256, and the guest images' URLs and sha256s, and
  checks that anyone can fetch all of them without logging in.

Nothing published is ever replaced. If a job is re-run, it accepts an image
tag, release asset or npm version that already exists only when it is exactly
what this run built, and fails otherwise.

## 1. Before tagging, on the release machine

First, **update ROADMAP.md statuses**, in a change merged before the tag:

- Mark each item this release makes available as Available, and each item
  whose work has started as In progress.
- Change each estimate that has moved, counted from today.
- Set the "Last updated" line to today's date.
- Check that the prices in README.md, and the status in each of its paid
  sections, still match [privatae.ai/pricing](https://privatae.ai/pricing)
  and [privatae.ai/cloud](https://privatae.ai/cloud).

ROADMAP.md is the one place this repository says when each part is expected.
README.md says only whether each paid offering is on sale, and the other
documents link to ROADMAP.md, so a release that skips this step leaves the
plan wrong on GitHub.

Opening C-Series Cloud or the network membership for orders is not a
release, but it changes the same lines, in one change merged the day
privatae.ai/cloud or privatae.ai/pricing stops saying "Waitlist open" or
"Not on sale yet":

- README.md: the section heading ("C-Series Cloud: waitlist open" becomes
  "C-Series Cloud: available"; "Network membership: not on sale yet" becomes
  "Network membership: available"), the line under it, and the opening
  paragraph's "neither on sale yet".
- ROADMAP.md: each Waitlist open and Built item under that offering becomes
  Available, and the membership section's "It is not on sale yet." goes.
- For the membership, docs/wiki/pages/the-privatae-network.md, which says
  membership is not on sale yet.

Then, from a clean checkout of the commit you will tag:

1. The full gate:

   ```sh
   scripts/pre_merge_gates.sh
   docker build -f Dockerfile.postgres -t cseries-postgres:release-check .
   docker build -f Dockerfile.linux -t cseries:release-check .
   IMAGE=cseries:release-check PG_IMAGE=cseries-postgres:release-check scripts/smoke_test.sh
   (cd packages/cseries && npm test)
   docker build -f sidecars/app-runtime-python/Dockerfile -t cseries-app-runtime-python:release-check .
   docker build -f sidecars/browser/Dockerfile -t cseries-browser:release-check sidecars/browser
   docker build -f sidecars/app-helper/Dockerfile -t cseries-app-helper:release-check sidecars/app-helper
   docker build -f sidecars/app-runtime-creative/Dockerfile -t cseries-app-runtime-creative:release-check .
   docker build -f sidecars/remote-door/Dockerfile -t cseries-remote-door:release-check .
   vm/wsl/build-rootfs.sh --arch x64 --version X.Y.Z --out /var/tmp/guest-check
   vm/wsl/test/boot-test.sh /var/tmp/guest-check/cseries-wsl-rootfs-X.Y.Z-x64.tar.gz \
     cseries:release-check cseries-postgres:release-check cseries-remote-door:release-check
   ```

   The boot test runs the guest without the door and then with it, as every
   install of a release that pins the door runs it: the Host must start on
   the remote network at 10.89.79.2 beside the door at 10.89.79.3, and still
   answer on the guest's 127.0.0.1:8800, before the tag.

2. The release term check must exit 0:

   ```sh
   python3 scripts/release_term_check.py --digests <digests file>
   ```

   It needs the maintainers' HMAC key (`--key`, else
   `$NO_PERSONAL_DATA_KEY_FILE`, else `/etc/privatae/keys/no-personal-data.hmac`).
   The key and the digests never leave the release machine, which is why this
   step is not in the workflow. Exit 1 prints `path:line` for each listed term
   found; exit 2 means the key or the digests are missing.

3. The public-source gate must pass:

   ```sh
   python3 scripts/test_public_source_clean.py
   ```

## 2. Tag the release

```sh
git tag -a vX.Y.Z -m "C-Series X.Y.Z"
git push origin vX.Y.Z
```

The tag must be exactly `vX.Y.Z`; anything else stops the workflow at its
first job.

## 3. Sign the C-Series Cloud release and the App Store catalog

The workflow's `cloud` job uploads `cloud-release.json` and stops there: until
a signature sits beside it, every cloud box refuses the release (it checks
against the release keys pinned in
`vm/cloud/overlay/usr/local/lib/cseries-cloud/cseries_cloud.py`, the bastion's
own two). The job's summary prints the two commands with this release's
version and sha256 filled in. On the computer that holds the release key:

```sh
gh release download vX.Y.Z --repo Privatae/cseries --pattern cloud-release.json
sha=$(sha256sum cloud-release.json | cut -d' ' -f1)
release-sign sign cseries-cloud X.Y.Z "$sha" > "cloud-release.json.${sha:0:16}.sig"
release-sign verify cseries-cloud cloud-release.json "cloud-release.json.${sha:0:16}.sig" \
  tWOyZm38DwTwbawSnvExQG/CQsVJBKYWc46arCBZj9E=
gh release upload vX.Y.Z "cloud-release.json.${sha:0:16}.sig" --repo Privatae/cseries
```

`release-sign` is the bastion's signing tool (its `deploy/release-sign`); it
asks for the key's passphrase and prints the signature. Check that the sha256
it reports is the one in the job's summary before typing the passphrase. Boxes
look for the latest release's manifest once a day and move to it on their next
run after the release becomes the latest (the last step of this section).

The workflow's `store` job uploads `catalog.json` and the bundles and stops
there in the same way: until a signature for kind `cseries-store` sits beside
the catalog, every Host refuses it. Its summary prints the commands with the
version and sha256 filled in:

```sh
gh release download vX.Y.Z --repo Privatae/cseries --pattern catalog.json
sha=$(sha256sum catalog.json | cut -d' ' -f1)
release-sign sign cseries-store X.Y.Z "$sha" > "catalog.json.${sha:0:16}.sig"
release-sign verify cseries-store catalog.json "catalog.json.${sha:0:16}.sig" \
  tWOyZm38DwTwbawSnvExQG/CQsVJBKYWc46arCBZj9E=
gh release upload vX.Y.Z "catalog.json.${sha:0:16}.sig" --repo Privatae/cseries
```

The catalog pins each bundle's sha256, so this one signature covers every app
in it; the bundles themselves stay signed by the keys that signed them
(`factory-bundles/` by the C-Series apps key), and a Host checks both.

Last, once both signatures are uploaded, make the release the latest by
running the `mark-latest` workflow with the tag. Any one of these does it,
and none needs anything on the computer that holds the release key:

- In a browser signed in to GitHub with write access to Privatae/cseries: the
  repository's **Actions** tab, **mark-latest**, **Run workflow**, type
  `vX.Y.Z` as the tag, **Run workflow**.
- From the machine that uploaded the signatures, through the REST API, with a
  token that has **Actions: read and write** on Privatae/cseries (a
  fine-grained token; a classic token needs the `repo` scope). Uploading the
  signatures needs only Contents: write, so check the token has both:

  ```sh
  curl -fsS -X POST \
    -H "Authorization: Bearer $GITHUB_TOKEN" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    https://api.github.com/repos/Privatae/cseries/actions/workflows/mark-latest.yml/dispatches \
    -d '{"ref": "main", "inputs": {"tag": "vX.Y.Z"}}'
  ```

- With gh: `gh workflow run mark-latest.yml --repo Privatae/cseries -f tag=vX.Y.Z`

The workflow has to be on the default branch (`main`) to be run. Its run in
the Actions tab says whether it marked the release; afterwards anyone can
check what is the latest, with no login:

```sh
curl -fsS https://api.github.com/repos/Privatae/cseries/releases/latest | jq -r .tag_name
```

Until then the release is a pre-release and GitHub's latest release is the
previous one. The cloud boxes, the service that sets them up and every Host's
App Store read the latest release (`releases/latest/download/cloud-release.json`
and `.../catalog.json`), so none of them can see a manifest before its
signature exists, and none is held waiting for one. `mark-latest.yml` refuses
unless `cloud-release.json` and `catalog.json` each name the tag's version
and each have a signature file named for the sha256 of the manifest as it
sits on the release, holding a valid signature of the right kind
(`cseries-cloud`, `cseries-store`) for that version and sha256 by one of the
pinned release keys. That is the check every reader makes, so a signature
made for another manifest or by another key, or uploaded under the other
manifest's name, stops the run here instead of every box and Host refusing
the latest release. It also refuses a tag older than the current latest
release, because the boxes, the App Store and the provisioner each refuse to
go back; going back means releasing the older code again under a newer
version. Then it marks the release the latest and reads back what GitHub now
calls the latest. A run that refuses changes nothing; fix the
signature and run it again. A release that is never marked (a tag that was
replaced by a fix-up tag, say) stays a pre-release and is never read.

A pre-release and not a draft, because a draft's files are not public and the
npm job refuses to publish a CLI whose pinned downloads cannot be fetched
without a login. Pinned URLs (`releases/download/vX.Y.Z/...`, which
`release.json` and the manifests use) work for a pre-release, so the npm
package and the manifests' own downloads need nothing from the latest mark. If
a job of the release workflow is re-run later, the release keeps whatever
state it is in.

The catalog also carries `issued_at`, the release commit's time, inside the
signed bytes. A Host whose catalog is more than 45 days old shows a notice in
its App Store (installs still work), because anyone serving the catalog's URL
could keep serving an old one. So a stretch of more than 45 days without a
release shows on every Host until the next one.

## 4. One-time owner steps

These were done once for Privatae/cseries before its first public release,
by the owner of the Privatae accounts on GitHub and npm, and are kept here for
a new account. After them, a tag releases everything, and the only steps by
hand are the two signatures and making the release the latest, in section 3.

The repository has to be public before the first release anyone installs.
The overlay, the harness source tarball, the store catalog and its bundles,
the Blender source and the guest images are all fetched anonymously from the
release's download URLs, which a private repository answers only to a
signed-in account. The npm job checks that the guest images and the harness
source tarball can be fetched without a login, and stops if they cannot.

With the repository public:

- **Turn on GitHub Pages.** In the repository's Settings, open Pages and set
  Source to "GitHub Actions". Without it `.github/workflows/pages.yml` never
  deploys. It deploys from the default branch when `.github/pages` changes;
  to publish the first time, run it from the Actions tab (pages, Run
  workflow). It publishes C-Series's OAuth Client ID Metadata Document at
  https://privatae.github.io/cseries/oauth/client-metadata.json.
- **Never give the Privatae account's user site, privatae.github.io, a custom
  domain**, and give this site none either. That URL is itself the
  `client_id` every Host signs in to an MCP server with, and an authorization
  server does not follow a redirect to it. With a custom domain on the user
  site, GitHub would serve this project site under that domain and redirect
  the URL. Renaming the repository or the account moves it the same way.

On the first release, in this order:

1. **Make every GHCR package public.** The first run's image jobs create seven
   packages under the Privatae account: `cseries`, `cseries-postgres`,
   `cseries-remote-door`, `cseries-app-runtime-python`, `cseries-browser`,
   `cseries-app-helper` and `cseries-app-runtime-creative`. Each image carries
   `org.opencontainers.image.source`, so GHCR links every package to the
   repository. If any is not public, the npm job stops at "Check what
   release.json pins is public". On GitHub open each package, then Package
   settings, then Change visibility, and choose Public. Then re-run the failed
   jobs.

2. **Publish the npm package once by hand.** Trusted publishing cannot
   create a package, so on the first run the npm job stops at "Publish" and
   names the package. Download that run's `npm-packages` artifact (the run's
   summary page, under Artifacts). It holds the exact tarball the workflow
   packed, `release.json` included. Publish it from your own machine:

   ```sh
   npm login
   npm publish ./cseries-X.Y.Z.tgz --access public
   ```

   A version published this way has no provenance statement, because only a
   publish from the workflow can carry one. Every later version has one.

3. **Add the trusted publisher for the package.** On npmjs.com open the
   package's Settings, then Trusted publishing, and choose GitHub Actions with
   user `Privatae`, repository `cseries` and workflow `release.yml`, and no
   environment. Then set Publishing access to require two-factor
   authentication and disallow tokens, so that only the workflow and you can
   publish.

4. **Re-run the npm job.** It finds X.Y.Z already on npm with the same
   contents and finishes.
