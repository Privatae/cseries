'use strict';

// The whole `npx cseries` run on a simulated Windows machine (fake-machine.js):
// first run from nothing, the restart for WSL, the resumed run, and a second
// run that changes nothing.

const test = require('node:test');
const assert = require('node:assert/strict');
const { main } = require('../src/cli');
const { captureOutput, testPaths, validRelease } = require('./helpers');
const { fakeMachine } = require('./fake-machine');

function machine(opts = {}) {
  const p = testPaths();
  const m = fakeMachine(p, opts);
  const releaseFor = (over = {}) => {
    const rel = validRelease(over);
    rel.guest['wsl-x64'].sha256 = m.rootfsSha256;
    return rel;
  };
  const run = async (argv = [], rel = releaseFor()) => {
    const out = captureOutput();
    const code = await main(argv, {
      platform: 'win32', arch: 'x64', env: {}, paths: p, out, runner: m.runner, net: m.net,
      sleep: async () => {}, prompt: async () => false, portState: async () => 'free', osRelease: '10.0.22631',
      releaseInfo: { release: rel, placeholders: [], file: 'test' }, bastionKeys: m.bastionKeys,
    });
    return { code, text: out.text(), err: out.errText() };
  };
  return { m, p, run, releaseFor };
}

test('a PC without WSL: the run gets the bastion and installs WSL, then stops for the restart', async () => {
  const { m, run } = machine({ wsl: false });
  const r = await run();
  assert.equal(r.code, 0, r.err);
  assert.match(r.text, /\[2\/8\] Getting the bastion/);
  assert.match(r.text, /\[3\/8\] Setting up WSL/);
  assert.match(r.text, /Restart Windows to finish installing WSL/);
  assert.ok(!/\[4\/8\]/.test(r.text), 'nothing after the WSL step ran');
  assert.equal(m.elevated.length, 1, 'only WSL\'s prompt: the bastion is installed after the restart');
  assert.equal(m.bastionScripts.length, 0);
  assert.ok(m.registry['HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\RunOnce\\cseries-resume']);
});

test('after the restart the resumed run finishes, and a second run repairs nothing', async () => {
  const { m, run } = machine({ wsl: false });
  await run();
  // Windows restarted: RunOnce has run the command again.
  const r1 = await run();
  assert.equal(r1.code, 0, r1.err);
  assert.equal(m.imports.length, 1);
  assert.equal(m.pairCalls.length, 1);
  assert.match(r1.text, /Setup code: {2}abc123-def456-0789ab/);
  assert.equal(m.registry['HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\RunOnce\\cseries-resume'], undefined, 'the resume entry is gone');
  assert.ok(m.registry['HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Run\\cseries'], 'the keep-alive starts at logon');
  assert.equal(m.elevated.length, 2, 'WSL\'s prompt, then one for the bastion');
  assert.equal(m.bastionScripts.length, 1);
  assert.equal(m.serviceInstalls, 1);
  assert.equal(m.bastion.anchor, 'host-a');
  assert.equal(m.bastion.enrolled['host-a'], true);
  assert.ok(m.runner.calls.some((c) => c.kind === 'detached' && c.cmd === m.paths.serviceExe && c.args[0] === 'tray'), 'the tray is started');
  const downloads = m.downloads;

  const r2 = await run();
  assert.equal(r2.code, 0, r2.err);
  assert.equal(m.imports.length, 1, 'not imported again');
  assert.equal(m.pairCalls.length, 1, 'not paired again');
  assert.equal(m.downloads, downloads, 'nothing downloaded again');
  assert.equal(m.elevated.length, 2, 'no prompt at all on a run that changes nothing');
  assert.equal(m.restarts, 1, 'the Host was not restarted: its images did not change');
  assert.match(r2.text, /\[3\/8\] Setting up WSL\n {6}WSL 2\.3\.26\.0 is installed\.\n {6}Already done\./);
  assert.match(r2.text, /\[4\/8\] Creating the C-Series Linux system\n {6}It is here\./);
  assert.match(r2.text, /\[7\/8\] Installing and pairing the bastion\n {6}The bastion runs as this machine's service, anchored to this Host, with your authenticator enrolled\./);
});

test('a new release restarts the Host on the new images, and only that', async () => {
  const { m, run, releaseFor } = machine();
  await run();
  assert.equal(m.restarts, 1);
  const rel = releaseFor({ version: '1.2.4' });
  rel.images.host.ref = 'ghcr.io/privatae/cseries:1.2.4';
  rel.images.host.digest = `sha256:${'9'.repeat(64)}`;
  const before = m.runner.calls.length;
  const r = await run(['update'], rel);
  assert.equal(r.code, 0, r.err);
  assert.equal(m.restarts, 2, 'restarted once, for the new images');
  assert.match(m.images, new RegExp(`host=ghcr\\.io/privatae/cseries:1\\.2\\.4@sha256:${'9'.repeat(64)}\n`));
  assert.match(m.images, /release=1\.2\.4\n/);
  assert.equal(m.imports.length, 1, 'the Linux system is kept');
  assert.ok(!m.runner.calls.slice(before).some((c) => c.kind === 'detached' && c.cmd === 'cmd.exe'), 'update does not open the browser');
});

test('the bastion refused (min_version) stops the run before anything else is installed', async () => {
  const { m, run } = machine({ bastionVersion: '0.53.1' });
  const r = await run();
  assert.equal(r.code, 1);
  assert.match(r.err, /needs version 0\.53\.2 or newer/);
  assert.equal(m.elevated.length, 0);
  assert.equal(m.imports.length, 0);
});

test('uninstall --delete-data, then a new Host: one prompt releases the old anchor, pairs fresh, anchors and enrols', async () => {
  const { m, run } = machine();
  assert.equal((await run()).code, 0);
  assert.equal(m.elevated.length, 1);
  const u = await run(['uninstall', '--delete-data', '--yes']);
  assert.equal(u.code, 0, u.err);
  assert.match(u.text, /The bastion stays installed/);
  // The reinstalled Host has a new CA; the bastion still holds the old pairing.
  m.hostCa = '-----BEGIN CERTIFICATE-----\nMIIBexample1\n-----END CERTIFICATE-----';
  const r = await run();
  assert.equal(r.code, 0, r.err);
  assert.equal(m.elevated.length, 2, 'one more prompt');
  const script = m.bastionScripts.at(-1);
  const at = (re) => script.search(re);
  assert.ok(at(/@\('release-anchor','--host','host-a','--host-ca',\$ca\)/) > 0, 'the dead anchor is released, shown the new Host\'s CA');
  assert.ok(at(/@\('release-anchor'/) < at(/@\('pair'/), 'before the pair');
  assert.ok(at(/@\('pair'/) < at(/@\('claim-anchor'\)/));
  assert.ok(at(/@\('claim-anchor'\)/) < at(/& \$exe 'stepup-enroll'/));
  assert.ok(!/--replace/.test(script));
  assert.ok(!/Say 'step=install'/.test(script), 'the program is already installed');
  assert.deepEqual(m.releases, ['host-a']);
  assert.equal(m.bastion.pairings['host-a'], `${m.hostCa}\n`, 'paired with the new Host CA');
  assert.equal(m.bastion.anchor, 'host-a');
  assert.equal(m.enrolments, 2, 'enrolled again: the old factor was set aside with the old Host');
});

test('a development build installs nothing', async () => {
  const out = captureOutput();
  const code = await main([], { platform: 'win32', arch: 'x64', env: {}, out, runner: { run: async () => { throw new Error('ran something'); } } });
  assert.equal(code, 1);
  assert.match(out.errText(), /development build/);
});
