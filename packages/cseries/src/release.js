'use strict';

// release.json: everything this copy of the command installs, pinned. The
// release workflow writes it before `npm publish`; the committed file holds
// "dev" placeholders, and a copy with any placeholder refuses to install
// anything. CSERIES_RELEASE_JSON points at another manifest for local testing.

const fs = require('node:fs');
const path = require('node:path');
const { CliError } = require('./errors');
const { isVersion } = require('./version');

const DIGEST = /^sha256:[0-9a-f]{64}$/;
const SHA256 = /^[0-9a-f]{64}$/;
// A registry image name, optionally with a tag; no digest (that is its own field).
const REF = /^[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[0-9]+)?(?:\/[a-z0-9]+(?:[._-][a-z0-9]+)*)+(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?$/;
// The guest image for each VM driver and CPU: WSL's root filesystem, Lima's
// qcow2 disk (macOS and Linux).
const GUEST_KEYS = ['wsl-x64', 'wsl-arm64', 'lima-x64', 'lima-arm64'];
// The local runtime's image families, as the guest's configure takes them.
const RUNTIME_FAMILIES = ['python', 'creative', 'helper', 'browser'];

function defaultPath() {
  return path.join(__dirname, '..', 'release.json');
}

function isPlaceholder(v) {
  return v === 'dev';
}

// Validates a parsed manifest. Returns {release, placeholders}: placeholders
// lists every field still holding "dev"; a field that is neither "dev" nor
// valid is an error.
function validate(m) {
  const problems = [];
  const placeholders = [];
  const field = (name, value, ok) => {
    if (isPlaceholder(value)) placeholders.push(name);
    else if (typeof value !== 'string' || !ok(value)) problems.push(name);
  };
  if (!m || typeof m !== 'object') throw new Error('release.json is not a JSON object');
  field('version', m.version, isVersion);
  for (const which of ['host', 'postgres']) {
    const img = (m.images || {})[which] || {};
    field(`images.${which}.ref`, img.ref, (v) => REF.test(v));
    field(`images.${which}.digest`, img.digest, (v) => DIGEST.test(v));
  }
  for (const key of GUEST_KEYS) {
    const g = (m.guest || {})[key] || {};
    field(`guest.${key}.url`, g.url, (v) => /^https:\/\/[^\s]+$/.test(v));
    field(`guest.${key}.sha256`, g.sha256, (v) => SHA256.test(v));
  }
  const minV = (m.bastion || {}).min_version;
  if (typeof minV !== 'string' || !isVersion(minV)) problems.push('bastion.min_version');
  // The local runtime's images by family, and the coding engines' sources:
  // optional, and when present pinned like the Host's (placeholders allowed
  // in the committed copy, as everywhere else).
  if (m.runtime !== undefined) {
    const rt = m.runtime;
    if (!rt || typeof rt !== 'object') problems.push('runtime');
    else {
      for (const key of Object.keys(rt)) {
        if (!RUNTIME_FAMILIES.includes(key) && key !== 'harness_src') problems.push(`runtime.${key}`);
      }
      for (const fam of RUNTIME_FAMILIES) {
        if (rt[fam] === undefined) continue;
        const img = rt[fam] || {};
        field(`runtime.${fam}.ref`, img.ref, (v) => REF.test(v));
        field(`runtime.${fam}.digest`, img.digest, (v) => DIGEST.test(v));
      }
      if (rt.harness_src !== undefined) {
        const hs = rt.harness_src || {};
        field('runtime.harness_src.url', hs.url, (v) => /^https:\/\/[A-Za-z0-9._~:/%+-]+$/.test(v));
        field('runtime.harness_src.sha256', hs.sha256, (v) => SHA256.test(v));
      }
    }
  }
  if (problems.length) {
    throw new Error(`release.json has invalid fields: ${problems.join(', ')}`);
  }
  return { release: m, placeholders };
}

function load(env = process.env) {
  const file = env.CSERIES_RELEASE_JSON || defaultPath();
  let text;
  try {
    text = fs.readFileSync(file, 'utf8');
  } catch (err) {
    throw new CliError(`Cannot read the release manifest ${file}: ${err.message}`,
      env.CSERIES_RELEASE_JSON ? 'Check the path in CSERIES_RELEASE_JSON.' : 'Reinstall this package: npx cseries@latest');
  }
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch (err) {
    throw new CliError(`The release manifest ${file} is not valid JSON: ${err.message}`, '');
  }
  let result;
  try {
    result = validate(parsed);
  } catch (err) {
    throw new CliError(`The release manifest ${file} is not usable: ${err.message}`, '');
  }
  return { ...result, file };
}

// The image as the guest runs it: name pinned by digest.
function pinnedImage(release, which) {
  const img = release.images[which];
  return `${img.ref}@${img.digest}`;
}

// Refuses when this copy has no pinned release.
function requirePinned(loaded) {
  if (loaded.placeholders.length) {
    throw new CliError(
      'This copy of cseries is a development build: it pins no release, so it has nothing to install.',
      'Install a published release with "npx cseries@latest", or set CSERIES_RELEASE_JSON to a release manifest.');
  }
}

// The guest's configure lines for the release's runtime section ('' without one).
function runtimeLines(release) {
  const rt = release.runtime || {};
  let out = '';
  for (const fam of RUNTIME_FAMILIES) {
    if (rt[fam]) out += `${fam}=${rt[fam].ref}@${rt[fam].digest}\n`;
  }
  if (rt.harness_src) out += `harness_src_url=${rt.harness_src.url}\nharness_src_sha256=${rt.harness_src.sha256}\n`;
  return out;
}

module.exports = { load, validate, pinnedImage, requirePinned, defaultPath, runtimeLines, GUEST_KEYS, RUNTIME_FAMILIES };
