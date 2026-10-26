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
  assert.ok(loaded.placeholders.includes('guest.lima-arm64.url'));
  assert.equal(loaded.release.bastion.min_version, '0.53.2');
  assert.throws(() => release.requirePinned(loaded), (e) => e instanceof CliError && /development build/.test(e.message));
});

test('the committed release.json has the shape of section 2', () => {
  const m = JSON.parse(fs.readFileSync(release.defaultPath(), 'utf8'));
  assert.deepEqual(Object.keys(m).sort(), ['bastion', 'guest', 'images', 'version']);
  assert.deepEqual(Object.keys(m.images).sort(), ['door', 'host', 'postgres']);
  assert.deepEqual(Object.keys(m.guest).sort(), ['lima-arm64', 'lima-x64', 'wsl-arm64', 'wsl-x64']);
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
    [{ guest: { ...validRelease().guest, 'lima-x64': undefined } }, 'guest.lima-x64.url'],
    [{ guest: { ...validRelease().guest, 'lima-arm64': { url: 'https://downloads.example/x.qcow2', sha256: 'abc' } } }, 'guest.lima-arm64.sha256'],
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

// The local runtime's images and the coding engines' sources, when the
// release pins them: validated like the Host's, and handed to the guest's
// configure with them.
test('a release that pins the runtime hands its images and sources to the guest', () => {
  const { configInput } = require('../src/host');
  const m = validRelease();
  m.runtime = {
    python: { ref: 'ghcr.io/privatae/cseries-app-runtime-python', digest: `sha256:${'d'.repeat(64)}` },
    browser: { ref: 'ghcr.io/privatae/cseries-browser', digest: `sha256:${'e'.repeat(64)}` },
    harness_src: { url: 'https://github.com/Privatae/cseries/releases/download/v1.2.3/cseries-harness-src-1.2.3.tar.gz',
      sha256: 'f'.repeat(64) },
  };
  const { release: r } = release.validate(m);
  const input = configInput(r);
  assert.match(input, /^host=ghcr\.io\/privatae\/cseries:1\.2\.3@sha256:a{64}\n/);
  assert.ok(input.includes(`python=ghcr.io/privatae/cseries-app-runtime-python@sha256:${'d'.repeat(64)}\n`));
  assert.ok(input.includes(`browser=ghcr.io/privatae/cseries-browser@sha256:${'e'.repeat(64)}\n`));
  assert.ok(input.includes(`harness_src_url=${m.runtime.harness_src.url}\nharness_src_sha256=${'f'.repeat(64)}\n`));
  assert.ok(!input.includes('creative=') && !input.includes('helper='));
  assert.equal(configInput(validRelease()), configInput({ ...validRelease(), runtime: undefined }));
});

test('a malformed runtime section is refused, each field by name', () => {
  const D = `sha256:${'d'.repeat(64)}`;
  const cases = [
    [{ python: { ref: 'ghcr.io/privatae/cseries-app-runtime-python', digest: 'sha256:abc' } }, 'runtime.python.digest'],
    [{ browser: { ref: 'not a ref', digest: D } }, 'runtime.browser.ref'],
    [{ harness_src: { url: 'http://example.test/h.tar.gz', sha256: 'f'.repeat(64) } }, 'runtime.harness_src.url'],
    [{ harness_src: { url: 'https://example.test/h.tar.gz', sha256: 'short' } }, 'runtime.harness_src.sha256'],
    [{ ubuntu: { ref: 'docker.io/library/ubuntu', digest: D } }, 'runtime.ubuntu'],
  ];
  for (const [runtime, name] of cases) {
    assert.throws(() => release.validate({ ...validRelease(), runtime }),
      (e) => e.message.includes(name), name);
  }
});

// The remote-access door's image (sidecars/remote-door): the committed copy
// pins it as a placeholder like the Host's, a manifest that pins it is
// checked like the Host's, and one from before it still loads.
test('the door image is pinned like the Host\'s, and a manifest without it still loads', () => {
  const loaded = release.load({});
  assert.ok(loaded.placeholders.includes('images.door.digest'));
  const door = { ref: 'ghcr.io/privatae/cseries-remote-door:1.2.3', digest: `sha256:${'9'.repeat(64)}` };
  const { release: r, placeholders } = release.validate(validRelease({ images: { ...validRelease().images, door } }));
  assert.deepEqual(placeholders, []);
  assert.equal(release.hasDoor(r), true);
  assert.equal(release.pinnedImage(r, 'door'), `ghcr.io/privatae/cseries-remote-door:1.2.3@sha256:${'9'.repeat(64)}`);
  assert.equal(release.hasDoor(release.validate(validRelease()).release), false);
  for (const [bad, name] of [[{ ...door, digest: 'sha256:abc' }, 'images.door.digest'],
    [{ ...door, ref: 'not a ref' }, 'images.door.ref'], [null, 'images.door.ref']]) {
    assert.throws(() => release.validate(validRelease({ images: { ...validRelease().images, door: bad } })),
      new RegExp(name.replace(/\./g, '\\.')), name);
  }
});

test('configure sends the door image only to a guest that knows it, and only when the release pins one', () => {
  const { configInput, guestHasDoor } = require('../src/host');
  const door = { ref: 'ghcr.io/privatae/cseries-remote-door:1.2.3', digest: `sha256:${'9'.repeat(64)}` };
  const withDoor = validRelease({ images: { ...validRelease().images, door } });
  assert.match(configInput(withDoor, { door: true }),
    new RegExp(`\ndoor=ghcr\\.io/privatae/cseries-remote-door:1\\.2\\.3@sha256:${'9'.repeat(64)}\n`));
  assert.doesNotMatch(configInput(withDoor), /door=/, 'a guest from before the door gets no door= key');
  assert.doesNotMatch(configInput(validRelease(), { door: true }), /door=/, 'a release without a door sends none');
  assert.equal(guestHasDoor({ door: 'inactive' }), true);
  assert.equal(guestHasDoor({ host: 'active' }), false);
});
