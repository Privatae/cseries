'use strict';

// The bastion's signed manifest. bastion-version.json and its .sig are the
// manifest privatae.ai published, fetched unchanged, so the first test checks
// a real signature against the pinned keys.

const test = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const v = require('../src/verify');
const { fixture } = require('./helpers');

const body = fixture('bastion-version.json');
const sig = fixture('bastion-version.json.9fee703d23448dbe.sig').toString('utf8');

test('the published manifest verifies against the pinned release keys', () => {
  const m = v.verifySignedManifest('bastion', body, sig);
  assert.equal(typeof m.version, 'string');
  assert.ok(m.binaries['windows-amd64.exe'].sha256);
});

test('the signature URL is the manifest URL plus the first 16 hex of its sha256', () => {
  assert.equal(v.signatureURL('https://privatae.ai/bastion/version.json', body),
    'https://privatae.ai/bastion/version.json.9fee703d23448dbe.sig');
});

test('one changed byte in the manifest is refused', () => {
  const tampered = Buffer.from(body);
  const i = tampered.indexOf('"sha256": "') + 11;
  tampered[i] = tampered[i] === 0x30 ? 0x31 : 0x30;
  assert.throws(() => v.verifySignedManifest('bastion', tampered, sig), /does not verify/);
});

test('the signature of another kind of release is refused', () => {
  assert.throws(() => v.verifySignedManifest('bastion-agent', body, sig), /does not verify/);
});

test('a malformed or empty signature is refused', () => {
  assert.throws(() => v.verifySignedManifest('bastion', body, 'not base64 at all!!'), /malformed/);
  assert.throws(() => v.verifySignedManifest('bastion', body, ''), /malformed/);
});

test('a signature by a key that is not pinned is refused, and one that is pinned passes', () => {
  const { publicKey, privateKey } = crypto.generateKeyPairSync('ed25519');
  const raw = publicKey.export({ format: 'der', type: 'spki' }).subarray(12).toString('base64');
  const own = Buffer.from(JSON.stringify({ version: '9.9.9', binaries: {} }));
  const s = crypto.sign(null, v.statement('bastion', '9.9.9', v.sha256Hex(own)), privateKey).toString('base64');
  assert.throws(() => v.verifySignedManifest('bastion', own, s), /does not verify/);
  assert.equal(v.verifySignedManifest('bastion', own, s, [raw]).version, '9.9.9');
  assert.throws(() => v.verifySignedManifest('bastion', own, s, []), /no release key/);
});

test('a manifest that names no version is refused before any key is tried', () => {
  assert.throws(() => v.verifySignedManifest('bastion', Buffer.from('{"binaries":{}}'), sig), /no usable version/);
  assert.throws(() => v.verifySignedManifest('bastion', Buffer.from('{"version":"1\\n2"}'), sig), /no usable version/);
  assert.throws(() => v.verifySignedManifest('bastion', Buffer.from('nope'), sig), /not valid JSON/);
});
