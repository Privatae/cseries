'use strict';

// What the terminal shows during a run, from the staged npx cseries install on
// Linux (2026-10-21): one line per step in place of the programs' own output,
// which goes to the install log; lines only where they apply; and the
// authenticator entry a deleted Host leaves behind. Invented values only.

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { Writable } = require('node:stream');
const { main } = require('../src/cli');
const { Output } = require('../src/output');
const { InstallLog, MAX_BYTES } = require('../src/log');
const host = require('../src/host');
const setup = require('../src/setup');
const { captureOutput, fakeRunner, tmpdir, validRelease, testPaths } = require('./helpers');
const { fakeLima, limaPaths } = require('./fake-lima');
const { fakeMachine } = require('./fake-machine');

function linux(opts = {}, platform = 'linux') {
  const p = limaPaths(tmpdir(), platform);
  const m = fakeLima(p, { platform, ...opts });
  const rel = validRelease();
  for (const k of ['lima-x64', 'lima-arm64']) rel.guest[k] = { ...rel.guest[k], sha256: m.qcow2Sha256 };
  const run = async (argv = []) => {
    const out = captureOutput();
    const code = await main(argv, {
      platform, arch: 'x64', env: {}, paths: p, out, runner: m.runner, net: m.net,
      sleep: async () => {}, prompt: async () => false, portState: async () => 'free', freeBytes: () => 64 * 1024 ** 3,
      releaseInfo: { release: rel, placeholders: [], file: 'test' }, bastionKeys: m.bastionKeys, sudoPaths: m.sudoPaths, hostname: 'test-machine',
    });
    return { code, text: out.text(), err: out.errText() };
  };
  return { m, p, run };
}

const log = (p) => (fs.existsSync(p.log) ? fs.readFileSync(p.log, 'utf8') : '');

test('QEMU\'s install: sudo\'s output goes to the install log, the terminal gets one line, sudo keeps the terminal for its password', async () => {
  const { m, p, run } = linux({ qemu: false });
  const r = await run();
  assert.equal(r.code, 0, r.text + r.err);
  const sudo = m.runner.calls.filter((c) => /sudo$/.test(c.cmd) && c.args.includes('-c') && /apt-get install/.test(c.args[c.args.indexOf('-c') + 1]));
  assert.equal(sudo.length, 1);
  assert.equal(sudo[0].kind, 'run', 'captured, not handed the terminal\'s output');
  assert.equal(sudo[0].stdin, 'inherit', 'the terminal\'s input stays sudo\'s, for its password');
  assert.doesNotMatch(r.text, /Unpacking|Setting up libexample/);
  assert.match(r.text, /Installing qemu-system-x86 qemu-utils ovmf with apt-get\n/);
  assert.match(r.text, /Installed qemu-system-x86 qemu-utils ovmf\./);
  assert.equal((log(p).match(/Unpacking libexample/g) || []).length, 40, 'the whole output is in the log');
});

test('QEMU\'s install fails: the end of its output and the install log\'s path are said', async () => {
  const { p, run } = linux({ qemu: false, qemuFail: 'E: Unable to locate package qemu-system-x86' });
  const r = await run();
  assert.equal(r.code, 1);
  assert.match(r.err, /Installing QEMU did not finish.*\n.*Unable to locate package/);
  assert.ok(r.err.includes(`The whole output is in ${p.log}.`), r.err);
  assert.match(log(p), /Unable to locate package/);
});

test('the image pull: one line naming the image, no layers or image id on the terminal, the whole output in the log; nothing once they are here', async () => {
  const { p, run } = linux({ qemu: true });
  const first = await run();
  assert.equal(first.code, 0, first.text + first.err);
  assert.match(first.text, /Downloading the database image/);
  assert.match(first.text, /Downloaded the Host's 2 images\./);
  assert.doesNotMatch(first.text, /Copying blob|Writing manifest|6966a79c67b4|Trying to pull/);
  assert.doesNotMatch(first.text, /about 2 GB the first time/);
  assert.match(log(p), /Copying blob 0a1b2c3d4e5f/);
  assert.match(log(p), /6966a79c67b4/);
  const again = await run();
  assert.equal(again.code, 0, again.text + again.err);
  assert.doesNotMatch(again.text, /Downloading|Downloaded|about 2 GB/, 'the images are here: nothing said about them');
});

test('a pull that fails says podman\'s last lines and where the whole output is', async () => {
  const { p, run } = linux({ qemu: true, pullFail: 'Error: initializing source docker://ghcr.io/privatae/cseries: pinging container registry: connection refused' });
  const r = await run();
  assert.equal(r.code, 1);
  assert.match(r.err, /could not be downloaded:\nError: initializing source/);
  assert.ok(r.err.includes(`The whole output is in ${p.log}.`), r.err);
});

test('the pull\'s status line follows podman\'s progress bars: the image, its layers and the bytes', () => {
  const lines = [];
  const out = {
    status(t) { lines.push(t); return { update: (u) => lines.push(u), end: (f) => lines.push(`end:${f}`) }; },
  };
  const rel = validRelease();
  const pr = host.pullProgress({ out, releaseInfo: { release: rel } });
  pr.feed(Buffer.from(`Trying to pull ${rel.images.host.ref}@${rel.images.host.digest}...\nGetting image source signatures\n`));
  pr.feed(Buffer.from('Copying blob 0a1b2c3d4e5f [==>------] 100.0MiB / 400.0MiB | 9 MiB/s\r'));
  pr.feed(Buffer.from('Copying blob 9f8e7d6c5b4a [=>-------] 1.0MiB / 100.0MiB | 1 MiB/s\r'));
  pr.feed(Buffer.from('Copying blob 9f8e7d6c5b4a done   | \n'));
  pr.end(true);
  assert.equal(lines[0], 'Downloading the Host\'s image');
  assert.ok(lines.includes('Downloading the Host\'s image: 0 of 2 layers, 101.0 MB of 500.0 MB'), lines.join('\n'));
  assert.ok(lines.includes('Downloading the Host\'s image: 1 of 2 layers, 200.0 MB of 500.0 MB'), lines.join('\n'));
  assert.equal(lines.at(-1), 'end:Downloaded the Host\'s image.');
});

test('the pull\'s status line counts layers from podman\'s plain lines, which say only when a layer starts', () => {
  // podman 5.4 through `limactl shell --tty=false` prints one line per layer
  // as it starts, then the config once every layer is in (invented ids).
  const lines = [];
  const out = {
    status(t) { lines.push(t); return { update: (u) => lines.push(u), end: (f) => lines.push(`end:${f}`) }; },
  };
  const rel = validRelease();
  const pr = host.pullProgress({ out, releaseInfo: { release: rel } });
  pr.feed(Buffer.from(`Trying to pull ${rel.images.host.ref}@${rel.images.host.digest}...\nGetting image source signatures\n`));
  pr.feed(Buffer.from(`Copying blob sha256:${'1a'.repeat(32)}\n`));
  pr.feed(Buffer.from(`Copying blob sha256:${'2b'.repeat(32)}\nCopying blob sha256:${'3c'.repeat(32)}\n`));
  pr.feed(Buffer.from(`Copying config sha256:${'4d'.repeat(32)}\nWriting manifest to image destination\n`));
  pr.end(true);
  assert.ok(lines.includes('Downloading the Host\'s image: 1 layer so far'), lines.join('\n'));
  assert.ok(lines.includes('Downloading the Host\'s image: 3 layers so far'), lines.join('\n'));
  assert.ok(lines.includes('Downloading the Host\'s image: 3 of 3 layers'), lines.join('\n'));
  assert.ok(!lines.some((l) => / 0 of /.test(l)), lines.join('\n'));
  assert.equal(lines.at(-1), 'end:Downloaded the Host\'s image.');
});

test('a status line on a terminal redraws in place and counts seconds; in a log it prints only what changes', () => {
  const chunks = [];
  const stream = new Writable({ write(c, _e, cb) { chunks.push(String(c)); cb(); } });
  stream.isTTY = true;
  const out = new Output(stream, stream);
  let now = 1000;
  out.now = () => now;
  const s = out.status('Starting the VM');
  now += 65000;
  s.update('Starting the VM, still');
  s.end('The VM is started.');
  assert.equal(chunks.join(''), '\r      Starting the VM (0s)\x1b[K\r      Starting the VM, still (1m 05s)\x1b[K\r\x1b[K      The VM is started.\n');

  const plain = captureOutput();
  const t = plain.status('Downloading the Host\'s image', 'host');
  t.update('Downloading the Host\'s image: 1 of 2 layers', 'host');
  t.update('Downloading the database image', 'postgres');
  t.end();
  assert.equal(plain.text(), '      Downloading the Host\'s image\n      Downloading the database image\n');
});

test('the install log keeps the run before when it grows past its size', () => {
  const dir = tmpdir();
  const file = path.join(dir, 'install.log');
  fs.writeFileSync(file, 'x'.repeat(MAX_BYTES + 1));
  const l = new InstallLog(file);
  l.write('new run\n');
  assert.equal(fs.readFileSync(file, 'utf8'), 'new run\n');
  assert.equal(fs.statSync(`${file}.old`).size, MAX_BYTES + 1);
});

test('the first start of the VM says it takes a minute or two; a later start does not', async () => {
  const { m, run } = linux({ qemu: true });
  const first = await run();
  assert.equal(first.code, 0, first.text + first.err);
  assert.match(first.text, /Starting the VM \(the first start takes a minute or two\)/);
  assert.equal((await run(['stop'])).code, 0);
  assert.equal(m.instance.status, 'Stopped');
  const later = await run(['start']);
  assert.equal(later.code, 0, later.text + later.err);
  assert.match(later.text, /Starting the VM\n/);
  assert.doesNotMatch(later.text, /first start/);
});

test('the Safari note is said on macOS only', async () => {
  const ctx = (platform) => ({
    platform, env: {}, out: captureOutput(), runner: fakeRunner(),
    net: { probe: async () => ({ status: 200, location: '' }) },
    driver: { guest: async () => ({ code: 0, stdout: 'CSERIES SETUP CODE: 0a1b2c-3d4e5f-6a7b8c\n', stderr: '' }) },
  });
  for (const [platform, safari] of [['linux', false], ['win32', false], ['darwin', true]]) {
    const c = ctx(platform);
    await setup.showSetup(c, { open: false });
    assert.match(c.out.text(), /http:\/\/127\.0\.0\.1:8800 {3}\(when the browser cannot open the name/);
    assert.equal(/Safari/.test(c.out.text()), safari, `${platform}: ${c.out.text()}`);
  }
});

test('uninstall --delete-data names the authenticator entry that no longer works; the re-enrolment says to delete it before scanning', async () => {
  const { m, run } = linux({ qemu: true });
  assert.equal((await run()).code, 0);
  const u = await run(['uninstall', '--delete-data', '--yes']);
  assert.equal(u.code, 0, u.err);
  assert.match(u.text, /entry "Privatae NAC: test-machine for host-a" was for this Host\nand no longer works: delete it from the app/);
  // The new Host has a new CA; the bastion still holds the old pairing.
  m.hostCa = '-----BEGIN CERTIFICATE-----\nMIIBexample1\n-----END CERTIFICATE-----';
  const again = await run();
  assert.match(again.text, /already has an entry "Privatae NAC: test-machine for host-a" from before\./);
  assert.match(again.text, /app before you scan the new code: the new entry has the same name/);
  const later = await run();
  assert.doesNotMatch(later.text, /from before/, 'said once, for the enrolment that replaces it');
});

test('on Windows too: the stale entry is named at uninstall and before the new QR code', async () => {
  const p = testPaths();
  const m = fakeMachine(p, {});
  const rel = validRelease();
  rel.guest['wsl-x64'].sha256 = m.rootfsSha256;
  const run = async (argv = []) => {
    const out = captureOutput();
    const code = await main(argv, {
      platform: 'win32', arch: 'x64', env: {}, paths: p, out, runner: m.runner, net: m.net,
      sleep: async () => {}, prompt: async () => false, portState: async () => 'free', freeBytes: () => 64 * 1024 ** 3,
      osRelease: '10.0.22631', releaseInfo: { release: rel, placeholders: [], file: 'test' }, bastionKeys: m.bastionKeys, hostname: 'TEST-PC',
    });
    return { code, text: out.text(), err: out.errText() };
  };
  assert.equal((await run()).code, 0);
  const u = await run(['uninstall', '--delete-data', '--yes']);
  assert.match(u.text, /"Privatae NAC: TEST-PC for host-a"/);
  m.hostCa = '-----BEGIN CERTIFICATE-----\nMIIBexample1\n-----END CERTIFICATE-----';
  const again = await run();
  assert.equal(again.code, 0, again.err);
  assert.match(again.text, /administrator window[\s\S]*already has an entry "Privatae NAC: TEST-PC for host-a"/);
});
