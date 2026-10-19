'use strict';

// The bastion's signed release manifest, and sha256 of bytes.
//
// The bastion's manifest is signed the way the bastion checks its own updates
// (privatae-bastion client-go/release_sign.go): beside <manifest> is
// <manifest>.<first 16 hex of its sha256>.sig, the base64 Ed25519 signature
// over the statement
//
//   privatae-release-signature-v1
//   <kind>
//   <version, as the manifest says>
//   <sha256 of the manifest's exact bytes, hex>
//
// and the signature must verify against one of the pinned release keys.

const crypto = require('node:crypto');

// The public halves of the bastion's release keys, base64 of the raw 32-byte
// Ed25519 key, as privatae-bastion pins them (client-go/release_sign.go,
// productionReleaseKeys). Two, so a key can be rotated.
const BASTION_RELEASE_KEYS = [
  'tWOyZm38DwTwbawSnvExQG/CQsVJBKYWc46arCBZj9E=', // primary
  'CKL+J0VSdqrfY4AmxChvylJ1ha4e8GvcrYF2o17Ah+g=', // spare, kept offline
];

// DER prefix of an Ed25519 SubjectPublicKeyInfo; the 32 key bytes follow.
const ED25519_SPKI_PREFIX = Buffer.from('302a300506032b6570032100', 'hex');

function sha256Hex(buf) {
  return crypto.createHash('sha256').update(buf).digest('hex');
}

function signatureURL(manifestURL, body) {
  const u = new URL(manifestURL);
  u.pathname += `.${sha256Hex(body).slice(0, 16)}.sig`;
  return u.toString();
}

function statement(kind, version, sha256hex) {
  return Buffer.from(`privatae-release-signature-v1\n${kind}\n${version}\n${sha256hex}\n`, 'utf8');
}

function publicKey(b64) {
  const raw = Buffer.from(b64, 'base64');
  if (raw.length !== 32) return null;
  return crypto.createPublicKey({ key: Buffer.concat([ED25519_SPKI_PREFIX, raw]), format: 'der', type: 'spki' });
}

// Returns the parsed manifest when sigText is a valid signature over body for
// kind; throws otherwise. Nothing here falls back to an unsigned manifest.
function verifySignedManifest(kind, body, sigText, keys = BASTION_RELEASE_KEYS) {
  if (!keys.length) throw new Error('no release key is pinned, so no manifest can be verified');
  let manifest;
  try {
    manifest = JSON.parse(body.toString('utf8'));
  } catch {
    throw new Error('the release manifest is not valid JSON');
  }
  const version = manifest && manifest.version;
  if (typeof version !== 'string' || !version || /[\r\n]/.test(version)) {
    throw new Error('the release manifest names no usable version');
  }
  const sig = Buffer.from(String(sigText).trim(), 'base64');
  if (sig.length !== 64) throw new Error('the release signature is malformed');
  const msg = statement(kind, version, sha256Hex(body));
  for (const k of keys) {
    const key = publicKey(k);
    if (key && crypto.verify(null, msg, key, sig)) return manifest;
  }
  throw new Error('the release signature does not verify against any pinned release key');
}

module.exports = {
  BASTION_RELEASE_KEYS,
  sha256Hex,
  signatureURL,
  statement,
  verifySignedManifest,
};
