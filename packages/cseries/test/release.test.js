'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const release = require('../src/release');
const { CliError } = require('../src/errors');
const { tmpdir, validRelease } = require('./helpers');

test('the committed release.json loads, holds only placeholders, and refuses to install', () => {
  const loaded = release.load({});
  assert.equal(loaded.file, release.defaultPath());
  assert.ok(loaded.placeholders.includes('version'));
  assert.ok(loaded.placeholders.includes('images.host.digest'));
  assert.ok(loaded.placeholders.includes('guest.wsl-x64.sha256'));
  assert.equal(loaded.release.bastion.min_version, '0.53.0');
  assert.throws(() => release.requirePinned(loaded), (e) => e instanceof CliError && /development build/.test(e.message));
});

test('the committed release.json has the shape of section 2', () => {
  const m = JSON.parse(fs.readFileSync(release.defaultPath(), 'utf8'));
  assert.deepEqual(Object.keys(m).sort(), ['bastion', 'guest', 'images', 'version']);
  assert.deepEqual(Object.keys(m.images).sort(), ['host', 'postgres']);
  assert.deepEqual(Object.keys(m.guest).sort(), ['wsl-arm64', 'wsl-x64']);
});

test('a fully pinned manifest validates and pins images by digest', () => {
  const { release: r, placeholders } = release.validate(validRelease());
  assert.deepEqual(placeholders, []);
  assert.equal(release.pinnedImage(r, 'host'), `ghcr.io/privatae/cseries:1.2.3@sha256:${'a'.repeat(64)}`);
  assert.equal(release.pinnedImage(r, 'postgres'), `ghcr.io/privatae/cseries-postgres:1.2.3@sha256:${'b'.repeat(64)}`);
});

test('invalid fields are refused, each by name', () => {
  const cases = [
    [{ version: '1.2' }, 'version'],
    [{ images: { ...validRelease().images, host: { ref: 'ghcr.io/privatae/cseries:1.2.3', digest: 'sha256:abc' } } }, 'images.host.digest'],
    [{ images: { ...validRelease().images, postgres: { ref: 'not a ref', digest: `sha256:${'b'.repeat(64)}` } } }, 'images.postgres.ref'],
    [{ guest: { ...validRelease().guest, 'wsl-x64': { url: 'http://downloads.example/x.tar.gz', sha256: 'c'.repeat(64) } } }, 'guest.wsl-x64.url'],
    [{ guest: { ...validRelease().guest, 'wsl-arm64': { url: 'https://downloads.example/x', sha256: 'D'.repeat(64) } } }, 'guest.wsl-arm64.sha256'],
    [{ bastion: { min_version: 'dev' } }, 'bastion.min_version'],
  ];
  for (const [over, name] of cases) {
    assert.throws(() => release.validate(validRelease(over)), new RegExp(name.replace(/\./g, '\\.')), name);
  }
});

test('CSERIES_RELEASE_JSON points at another manifest', () => {
  const dir = tmpdir();
  const f = path.join(dir, 'release.json');
  fs.writeFileSync(f, JSON.stringify(validRelease()));
  const loaded = release.load({ CSERIES_RELEASE_JSON: f });
  assert.equal(loaded.file, f);
  release.requirePinned(loaded);
  fs.writeFileSync(f, '{not json');
  assert.throws(() => release.load({ CSERIES_RELEASE_JSON: f }), (e) => e instanceof CliError && /not valid JSON/.test(e.message));
  assert.throws(() => release.load({ CSERIES_RELEASE_JSON: path.join(dir, 'missing.json') }), (e) => e instanceof CliError && /CSERIES_RELEASE_JSON/.test(e.next));
});
