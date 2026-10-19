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
  `/app`, and `scripts/smoke_test.sh`. The tested image is pushed by digest
  with a provenance attestation, and the index is made from exactly those
  digests.
- **The GitHub release** `vX.Y.Z`, with the WSL guest images
  `cseries-wsl-rootfs-X.Y.Z-x64.tar.gz` and `cseries-wsl-rootfs-X.Y.Z-arm64.tar.gz`,
  each with its `.sha256`.
- **The npm packages** under `packages/`, at X.Y.Z, with trusted publishing
  and provenance. First the workflow writes `packages/cseries/release.json`
  with the images' digests and the guest images' URLs and sha256s, and checks
  that anyone can fetch all of them without logging in.

Nothing published is ever replaced. If a job is re-run, it accepts an image
tag, release asset or npm version that already exists only when it is exactly
what this run built, and fails otherwise.

## 1. Before tagging, on the release machine

From a clean checkout of the commit you will tag:

1. The full gate:

   ```sh
   scripts/pre_merge_gates.sh
   docker build -f Dockerfile.postgres -t cseries-postgres:release-check .
   docker build -f Dockerfile.linux -t cseries:release-check .
   IMAGE=cseries:release-check PG_IMAGE=cseries-postgres:release-check scripts/smoke_test.sh
   (cd packages/cseries && npm test)
   vm/wsl/build-rootfs.sh --arch x64 --version X.Y.Z --out /var/tmp/guest-check
   vm/wsl/test/boot-test.sh /var/tmp/guest-check/cseries-wsl-rootfs-X.Y.Z-x64.tar.gz \
     cseries:release-check cseries-postgres:release-check
   ```

2. The release term check must exit 0:

   ```sh
   python3 scripts/release_term_check.py --digests /srv/work/plans/denylist-src/cseries-digests.txt
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

## 3. One-time owner steps

These are done once, by the owner of the Privatae accounts on GitHub and npm.
After them, a tag releases everything with no further steps.

When the repository goes public:

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

1. **Make both GHCR packages public.** The first run's image jobs create
   `cseries` and `cseries-postgres` under the Privatae account. Each image
   carries `org.opencontainers.image.source`, so GHCR links both packages to
   the repository. If they are not public, the npm job stops at "Check what
   release.json pins is public". On GitHub open each package, then Package
   settings, then Change visibility, and choose Public. Then re-run the failed
   jobs.

2. **Publish each npm package once by hand.** Trusted publishing cannot
   create a package, so on the first run the npm job stops at "Publish" and
   names the package. Download that run's `npm-packages` artifact (the run's
   summary page, under Artifacts). It holds the exact tarballs the workflow
   packed, `release.json` included. Publish them from your own machine,
   signing in with your passkey, per-platform packages first and `cseries`
   last:

   ```sh
   npm login
   npm publish ./cseries-X.Y.Z.tgz --access public
   ```

   A version published this way has no provenance statement, because only a
   publish from the workflow can carry one. Every later version has one.

3. **Add the trusted publisher for each package.** On npmjs.com open the
   package's Settings, then Trusted publishing, and choose GitHub Actions with
   user `Privatae`, repository `cseries` and workflow `release.yml`, and no
   environment. Then set Publishing access to require two-factor
   authentication and disallow tokens, so that only the workflow and you can
   publish.

4. **Re-run the npm job.** It finds X.Y.Z already on npm with the same
   contents and finishes.
