'use strict';

// WSL: decoding wsl.exe's output and the driver's calls.
//
// The fixtures are byte files in the encoding wsl.exe uses, UTF-16LE (one
// with a byte order mark, one ending in a NUL), holding the text of WSL's
// documented messages. They were encoded from that text, not captured from a
// Windows machine; a run on Windows is what proves them against the real tool.

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const t = require('../src/vm/wsl-text');
const wsl = require('../src/vm/wsl');
const { makeContext } = require('../src/cli');
const { fixture, captureOutput, psScript, tmpdir, validRelease } = require('./helpers');
const { fakeMachine } = require('./fake-machine');

test('UTF-16LE output is detected and decoded, with or without a BOM, and NULs and CRs go', () => {
  const v = t.decodeWslOutput(fixture('wsl-version.utf16le'));
  assert.match(v, /^WSL version: 2\.3\.26\.0\nKernel version: 5\.15\.167\.4-1\n/);
  assert.ok(!v.includes('\r') && !v.includes('\u0000'));
  const de = t.decodeWslOutput(fixture('wsl-version-de-bom.utf16le'));
  assert.ok(de.startsWith('WSL-Version: 2.4.11.0\n'));
  assert.equal(t.looksUtf16le(fixture('wsl-version.utf16le')), true);
  assert.equal(t.looksUtf16le(fixture('wsl-version.utf8')), false);
});

test('UTF-8 output (WSL_UTF8=1, or a Linux program in the guest) passes through', () => {
  assert.equal(t.decodeWslOutput(fixture('wsl-version.utf8')), 'WSL version: 2.3.26.0\nKernel version: 5.15.167.4-1\n');
  assert.equal(t.decodeWslOutput(Buffer.from('system=running\n')), 'system=running\n');
  assert.equal(t.decodeWslOutput(Buffer.from('changed=1\r\n')), 'changed=1\n');
  assert.equal(t.decodeWslOutput(Buffer.alloc(0)), '');
});

test('the WSL version is read in English and from a translated label', () => {
  assert.deepEqual(t.parseWslVersion(t.decodeWslOutput(fixture('wsl-version.utf16le'))), { wsl: '2.3.26.0', kernel: '5.15.167.4' });
  assert.equal(t.parseWslVersion(t.decodeWslOutput(fixture('wsl-version-de-bom.utf16le'))).wsl, '2.4.11.0');
  assert.equal(t.parseWslVersion(t.decodeWslOutput(fixture('wsl-not-installed.utf16le'))), null);
});

test('the distribution list is one name per line, and the "none installed" message is no names', () => {
  assert.deepEqual(t.parseDistroList(t.decodeWslOutput(fixture('wsl-list-quiet.utf16le'))), ['Ubuntu-24.04', 'cseries', 'docker-desktop']);
  assert.deepEqual(t.parseDistroList(t.decodeWslOutput(fixture('wsl-list-empty.utf16le'))), []);
});

test('wsl.exe failures a person can act on are explained', () => {
  const hcs = t.explainWslError(t.decodeWslOutput(fixture('wsl-import-hcs.utf16le')));
  assert.match(hcs.message, /virtualization/);
  assert.equal(t.explainWslError(t.decodeWslOutput(fixture('wsl-no-distro.utf16le'))).notFound, true);
  assert.equal(t.explainWslError('Wsl/Service/RegisterDistro/ERROR_ALREADY_EXISTS').alreadyExists, true);
  assert.equal(t.explainWslError('something else entirely'), null);
});

test('the Windows build number comes from os.release()', () => {
  assert.equal(t.windowsBuild('10.0.22631'), 22631);
  assert.equal(t.windowsBuild('10.0.19041'), 19041);
  assert.equal(t.windowsBuild('6.1'), null);
});

function winCtx(opts = {}) {
  const dir = tmpdir();
  const p = {
    base: dir,
    state: path.join(dir, 'state.json'),
    downloads: path.join(dir, 'downloads'),
    wsl: path.join(dir, 'wsl'),
    hostCa: path.join(dir, 'host-ca.pem'),
    bastionDir: path.join(dir, 'bastion'),
    bastionExe: path.join(dir, 'bastion', 'privatae-bastion.exe'),
    join: path.join,
  };
  const m = fakeMachine(p, opts);
  const rel = validRelease({ guest: { ...validRelease().guest, 'wsl-x64': { url: 'https://github.com/Privatae/cseries/releases/download/v1.2.3/cseries-wsl-rootfs-1.2.3-x64.tar.gz', sha256: m.rootfsSha256 } } });
  const ctx = makeContext({
    platform: 'win32',
    arch: 'x64',
    env: {},
    paths: p,
    out: captureOutput(),
    runner: m.runner,
    net: m.net,
    sleep: async () => {},
    prompt: async () => opts.answer === true,
    releaseInfo: { release: rel, placeholders: [], file: 'test' },
    bastionKeys: m.bastionKeys,
  });
  return { ctx, m, p };
}

test('every wsl.exe call asks for UTF-8 and every guest command runs as root through --exec', async () => {
  const { ctx, m } = winCtx({ distro: true });
  await wsl.driver.guest(ctx, ['status']);
  const call = m.runner.calls.find((c) => c.cmd === 'wsl.exe');
  assert.deepEqual(call.args, ['-d', 'cseries', '-u', 'root', '--exec', '/usr/local/bin/cseries-guest', 'status']);
  assert.equal(call.env.WSL_UTF8, '1');
});

test('WSL missing: one elevated install, a RunOnce entry to resume, and a restart prompt', async () => {
  const { ctx, m } = winCtx({ wsl: false, answer: true });
  assert.equal(await wsl.driver.runtimeReady(ctx), false);
  const r = await wsl.driver.installRuntime(ctx);
  assert.match(r.halt, /restarts in 10 seconds/);
  assert.equal(m.elevated.length, 1);
  assert.match(m.elevated[0], /Start-Process -FilePath 'wsl\.exe' -ArgumentList '--install','--no-distribution' -Verb RunAs -Wait -PassThru/);
  assert.equal(m.registry['HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\RunOnce\\cseries-resume'], 'cmd.exe /k npx --yes cseries@1.2.3');
  assert.equal(m.shutdowns, 1);
});

test('WSL missing and the restart declined: no restart, and the run ends with what to do', async () => {
  const { ctx, m } = winCtx({ wsl: false, answer: false });
  const r = await wsl.driver.installRuntime(ctx);
  assert.match(r.halt, /^Restart Windows/);
  assert.equal(m.shutdowns, 0);
});

test('the UAC prompt declined is a plain error, and nothing is written to RunOnce', async () => {
  const { ctx, m } = winCtx({ wsl: false, uac: 'no' });
  await assert.rejects(wsl.driver.installRuntime(ctx), /permission prompt was declined/);
  assert.deepEqual(m.registry, {});
});

test('the guest is downloaded, checked and imported with the contract\'s arguments', async () => {
  const { ctx, m, p } = winCtx();
  assert.equal(await wsl.driver.guestExists(ctx), false);
  await wsl.driver.createGuest(ctx);
  assert.equal(m.imports.length, 1);
  assert.deepEqual(m.imports[0], ['--import', 'cseries', p.wsl, path.join(p.downloads, 'cseries-wsl-rootfs-1.2.3-x64.tar.gz'), '--version', '2']);
  assert.equal(await wsl.driver.guestExists(ctx), true);
  assert.equal(fs.existsSync(path.join(p.downloads, 'cseries-wsl-rootfs-1.2.3-x64.tar.gz')), false, 'the tarball is removed after import');
});

test('a guest image with the wrong sha256 is refused and nothing is imported', async () => {
  const { ctx, m } = winCtx();
  ctx.releaseInfo.release.guest['wsl-x64'].sha256 = 'e'.repeat(64);
  await assert.rejects(wsl.driver.createGuest(ctx), /not the file this release pins/);
  assert.equal(m.imports.length, 0);
});

test('a disk left by an earlier install is registered in place, keeping its data', async () => {
  const { ctx, m, p } = winCtx();
  fs.mkdirSync(p.wsl, { recursive: true });
  fs.writeFileSync(path.join(p.wsl, 'ext4.vhdx'), 'disk');
  m.runner.calls.length = 0;
  await wsl.driver.createGuest(ctx).catch(() => {});
  const call = m.runner.calls.find((c) => c.cmd === 'wsl.exe' && c.args[0].startsWith('--import'));
  assert.deepEqual(call.args, ['--import-in-place', 'cseries', path.join(p.wsl, 'ext4.vhdx')]);
});

test('the keep-alive starts now, hidden, and at every logon from HKCU Run', async () => {
  const { ctx, m } = winCtx({ distro: true });
  await wsl.driver.startGuest(ctx);
  const d = m.runner.calls.find((c) => c.kind === 'detached');
  assert.deepEqual(d.args, ['-d', 'cseries', '-u', 'root', '--exec', '/usr/local/bin/cseries-guest', 'keepalive']);
  await wsl.driver.setAutostart(ctx, true);
  const run = m.registry['HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Run\\cseries'];
  assert.equal(run, wsl.keepAliveCommand());
  assert.match(run, /-WindowStyle Hidden -FilePath wsl\.exe -ArgumentList '-d','cseries','-u','root','--exec','\/usr\/local\/bin\/cseries-guest','keepalive'/);
  await wsl.driver.setAutostart(ctx, false);
  assert.equal(m.registry['HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Run\\cseries'], undefined);
});

test('registry writes never use New-Item -Force on an existing key (it would empty it)', () => {
  const win = require('../src/win/windows');
  const s = win.setValueScript(win.RUN_KEY, 'cseries', "a 'quoted' value");
  assert.match(s, /if \(-not \(Test-Path \$k\)\) \{ New-Item -Path \$k \| Out-Null \}/);
  assert.ok(!/New-Item[^\n]*-Force/.test(s));
  assert.match(s, /-Value 'a ''quoted'' value'/);
});

test('PowerShell scripts go as -EncodedCommand, so nothing in them is parsed by a shell', () => {
  const ps = require('../src/win/powershell');
  const a = ps.args("Write-Output 'x\"y'");
  assert.equal(a[a.length - 2], '-EncodedCommand');
  assert.match(psScript(a), /Write-Output 'x"y'$/);
});
