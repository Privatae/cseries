'use strict';

// The machine wall in the guest (src/vm/machine-firewall.js, the files in
// src/vm/guest/): the copies the command writes are the image's, every run
// writes them and this machine's addresses into the guest and loads them
// before the Host starts, an older guest that `update` keeps gets them, and a
// wall that does not load stops the run before the Host starts. The rules
// themselves are checked with real packets in scripts/test_vm_machine_firewall.py.
// Every address here is invented (documentation ranges and private ones).

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const childProcess = require('node:child_process');
const { main } = require('../src/cli');
const mf = require('../src/vm/machine-firewall');
const lima = require('../src/vm/lima');
const wsl = require('../src/vm/wsl');
const { captureOutput, tmpdir, validRelease, testPaths, guestShell, fakeRunner } = require('./helpers');
const { fakeLima, limaPaths } = require('./fake-lima');
const { fakeMachine } = require('./fake-machine');

const REPO = path.resolve(__dirname, '..', '..', '..');
const IMAGE = path.join(REPO, 'vm', 'wsl', 'rootfs');

// An invented machine: a LAN address, a public one, a VPN's CGNAT one, a
// global IPv6 one, and what the wall holds already (loopback, link-local).
const IFACES = {
  lo: [{ address: '127.0.0.1', family: 'IPv4', internal: true }, { address: '::1', family: 'IPv6', internal: true }],
  eth0: [
    { address: '192.168.1.20', family: 'IPv4', internal: false },
    { address: '2001:db8:1::20', family: 'IPv6', internal: false },
    { address: 'fe80::1c2b:3aff:fe4d:5e6f', family: 'IPv6', internal: false, scopeid: 2 },
  ],
  wan0: [{ address: '203.0.113.7', family: 4, internal: false }],
  tailscale0: [{ address: '100.101.102.103', family: 'IPv4', internal: false }],
  apipa: [{ address: '169.254.10.20', family: 'IPv4', internal: false }],
  dup: [{ address: '192.168.1.20', family: 'IPv4', internal: false }],
};

test('the copies the command writes into a guest are the guest image\'s own files, byte for byte', { skip: !fs.existsSync(IMAGE) && 'not in the repository' }, () => {
  for (const f of mf.guestFiles()) {
    const image = fs.readFileSync(path.join(IMAGE, f.dest));
    assert.ok(image.equals(Buffer.from(f.content, 'utf8')), `${f.dest}: src/vm/guest/${f.src} differs from vm/wsl/rootfs${f.dest}`);
  }
  // The loader is executable in the image and written 0755 by the command.
  assert.equal(mf.GUEST_FILES.find((f) => f.src === 'cseries-machine-firewall').mode, '0755');
  assert.ok(fs.statSync(path.join(IMAGE, 'usr/local/sbin/cseries-machine-firewall')).mode & 0o111, 'the image\'s loader is executable');
  // The Lima image starts with the same user-mode network lines the command writes.
  const limaFile = fs.readFileSync(path.join(REPO, 'vm/lima/rootfs/etc/cseries-machine/addresses'), 'utf8');
  assert.deepEqual(limaFile.split('\n').filter((l) => l && !l.startsWith('#')), mf.LIMA_LINES);
});

test('the machine\'s own addresses: every interface\'s, public and VPN ones included, not what the wall holds already', () => {
  assert.deepEqual(mf.machineAddresses(IFACES), ['100.101.102.103', '192.168.1.20', '2001:db8:1::20', '203.0.113.7']);
  assert.deepEqual(mf.machineAddresses({}), []);
  assert.deepEqual(mf.machineAddresses({ x: [{ address: '1.2.3.4; reboot', family: 'IPv4' }, { address: '::', family: 'IPv6' }] }), [], 'nothing but an address');
});

test('the addresses file: Lima\'s user-mode network on Lima, then the machine\'s addresses; WSL has no fixed network', () => {
  const l = mf.addressesFile('lima', ['192.168.1.20', '203.0.113.7']).split('\n').filter((x) => x && !x.startsWith('#'));
  assert.deepEqual(l, ['block 192.168.5.0/24', 'dns 192.168.5.3', 'block 192.168.1.20', 'block 203.0.113.7']);
  const w = mf.addressesFile('wsl', ['192.168.1.20']).split('\n').filter((x) => x && !x.startsWith('#'));
  assert.deepEqual(w, ['block 192.168.1.20']);
  assert.equal(mf.addressesFile('wsl', ['192.168.1.20']), mf.addressesFile('wsl', ['192.168.1.20']), 'the same machine writes the same bytes');
});

test('the install script, run by /bin/sh: the four files with their modes, the addresses, the unit enabled and (re)loaded', () => {
  const dir = tmpdir();
  const g = guestShell(dir);
  const r = g.run(mf.installScript(mf.guestFiles(), mf.addressesFile('lima', ['192.168.1.20'])));
  assert.equal(r.code, 0, r.stderr);
  assert.match(r.stdout, /^machine_firewall=loaded$/m);
  for (const f of mf.guestFiles()) {
    const got = g.file(f.dest);
    assert.equal(fs.readFileSync(got, 'utf8'), f.content, f.dest);
    assert.equal((fs.statSync(got).mode & 0o777).toString(8), f.mode.slice(1), `${f.dest} mode`);
    assert.ok(!fs.existsSync(`${got}.new`));
  }
  assert.match(fs.readFileSync(g.file(mf.ADDRESSES), 'utf8'), /^block 192\.168\.5\.0\/24\ndns 192\.168\.5\.3\nblock 192\.168\.1\.20\n$/m);
  assert.deepEqual(g.systemctl().trim().split('\n'),
    ['systemctl daemon-reload', 'systemctl enable cseries-machine-firewall.service', 'systemctl reload-or-restart cseries-machine-firewall.service']);
  // A heredoc can only carry a file that does not hold its end marker.
  assert.throws(() => mf.installScript([{ dest: '/etc/x', mode: '0644', content: `a\n${mf.EOF_MARK}\nb\n` }], ''), /holds the line/);
});

test('the loader, as written into a guest, reads the addresses file and skips what is not an address', { skip: process.platform === 'win32' && 'POSIX sh' }, () => {
  const dir = tmpdir();
  const g = guestShell(dir);
  g.run(mf.installScript(mf.guestFiles(), `${mf.addressesFile('lima', ['192.168.1.20', '2001:db8:1::20'])}block not;an address\n`));
  fs.mkdirSync(g.file('/etc'), { recursive: true });
  fs.writeFileSync(g.file('/etc/resolv.conf'), 'nameserver 192.168.5.3\n');
  const r = childProcess.spawnSync('/bin/sh', [g.file('/usr/local/sbin/cseries-machine-firewall'), 'print'], {
    env: { ...process.env, CSERIES_GUEST_ROOT: g.root, CSERIES_MACHINE_NO_WAIT: '1' },
  });
  const out = r.stdout.toString();
  assert.equal(r.status, 0, r.stderr.toString());
  assert.match(out, /^table inet cseries_machine$/m);
  assert.match(out, /^add element inet cseries_machine machine_v4 \{ [^}]*192\.168\.1\.20[^}]*192\.168\.5\.0\/24[^}]* \}$/m);
  assert.match(out, /^add element inet cseries_machine machine_v6 \{ 2001:db8:1::20 \}$/m);
  assert.match(out, /^add element inet cseries_machine dns_v4 \{ 192\.168\.5\.3 \}$/m);
  assert.match(r.stderr.toString(), /skipped a line/);
});

function limaRun(argv, m, p) {
  const out = captureOutput();
  const rel = validRelease();
  for (const k of ['lima-x64', 'lima-arm64']) rel.guest[k] = { ...rel.guest[k], sha256: m.qcow2Sha256 };
  return main(argv, {
    platform: m.platform, arch: 'x64', env: {}, paths: p, out, runner: m.runner, net: m.net,
    sleep: async () => {}, prompt: async () => false, portState: async () => 'free', freeBytes: () => 64 * 1024 ** 3,
    releaseInfo: { release: rel, placeholders: [], file: 'test' }, bastionKeys: m.bastionKeys,
    netInterfaces: () => IFACES,
  }).then((code) => ({ code, out }));
}

const guestCalls = (m) => m.calls.filter((c) => /limactl$/.test(c.cmd) && c.args.includes('shell'))
  .map((c) => (c.args.includes('/bin/sh') ? 'wall' : c.args[c.args.indexOf(lima.GUEST_BIN) + 1]));

test('Lima: every run loads the wall with this machine\'s addresses before the Host is configured and started', async () => {
  const p = limaPaths(tmpdir(), 'linux');
  const m = fakeLima(p, { platform: 'linux', qemu: true });
  const { code, out } = await limaRun([], m, p);
  assert.equal(code, 0, out.text() + out.errText());
  const order = guestCalls(m);
  assert.ok(order.indexOf('wall') >= 0 && order.indexOf('wall') < order.indexOf('configure'), order.join(' '));
  const hostStart = order.findIndex((x) => x === 'start' || x === 'restart');
  assert.ok(hostStart > 0 && order.indexOf('wall') < hostStart, order.join(' '));
  const shellCall = m.calls.find((c) => c.args.includes('/bin/sh'));
  assert.deepEqual(shellCall.args.slice(-3), ['sudo', '/bin/sh', '-s'], 'as root, the script on standard input');
  const addrs = fs.readFileSync(m.guestShell.file(mf.ADDRESSES), 'utf8');
  assert.match(addrs, /^block 192\.168\.5\.0\/24$/m);
  for (const a of ['192.168.1.20', '203.0.113.7', '100.101.102.103', '2001:db8:1::20']) assert.match(addrs, new RegExp(`^block ${a.replace(/\./g, '\\.')}$`, 'm'));
  assert.match(out.text(), /Nothing in the Linux system's containers can open a connection to this machine\./);
});

test('Lima: an older guest that update keeps gets the wall, and start brings in an address the machine gained', async () => {
  const p = limaPaths(tmpdir(), 'linux');
  const m = fakeLima(p, { platform: 'linux', qemu: true });
  assert.equal((await limaRun([], m, p)).code, 0);
  // The guest as an older release made it: none of the wall's files.
  fs.rmSync(m.guestShell.root, { recursive: true, force: true });
  fs.mkdirSync(m.guestShell.root, { recursive: true });
  const r = await limaRun(['update'], m, p);
  assert.equal(r.code, 0, r.out.text() + r.out.errText());
  assert.equal(m.calls.filter((c) => /limactl$/.test(c.cmd) && c.args[1] === 'create').length, 1, 'update keeps the guest it found');
  for (const f of mf.GUEST_FILES) assert.ok(fs.existsSync(m.guestShell.file(f.dest)), `${f.dest} is in the guest after update`);
  assert.match(m.guestShell.systemctl(), /systemctl reload-or-restart cseries-machine-firewall\.service/);
  // An address the machine took since: `npx cseries start` writes it before the Host starts.
  IFACES.wifi0 = [{ address: '10.20.30.40', family: 'IPv4', internal: false }];
  try {
    assert.equal((await limaRun(['stop'], m, p)).code, 0);
    const s = await limaRun(['start'], m, p);
    assert.equal(s.code, 0, s.out.text() + s.out.errText());
    assert.match(fs.readFileSync(m.guestShell.file(mf.ADDRESSES), 'utf8'), /^block 10\.20\.30\.40$/m);
  } finally {
    delete IFACES.wifi0;
  }
});

test('Lima: a wall that does not load stops the run before the Host is configured or started, and says why', async () => {
  const p = limaPaths(tmpdir(), 'linux');
  const m = fakeLima(p, { platform: 'linux', qemu: true, wallFails: true });
  const { code, out } = await limaRun([], m, p);
  assert.notEqual(code, 0);
  assert.match(out.errText(), /firewall, which keeps every container in it off this machine, could not be put in place/);
  assert.match(out.errText(), /syntax error \(invented for the test\)/, 'the unit\'s journal lines are shown');
  assert.ok(!guestCalls(m).includes('configure') && !guestCalls(m).includes('start'));
});

test('the install script fails closed: with no wall loaded the Host is stopped, and a wall loaded before is left to hold', { skip: process.platform === 'win32' && 'POSIX sh' }, () => {
  // An older guest's Host was started at boot, before any wall: when the wall
  // then cannot be loaded, the Host must not go on running without one.
  const none = guestShell(tmpdir(), { restartFails: true, noTable: true });
  const r = none.run(mf.installScript(mf.guestFiles(), mf.addressesFile('lima', [])));
  assert.notEqual(r.code, 0);
  assert.match(r.stdout, /^machine_firewall=failed$/m);
  assert.match(r.stdout, /^host=stopped$/m);
  assert.match(none.systemctl(), /^systemctl stop cseries-host\.service$/m);
  // A failure before the reload (a file that cannot be written) fails closed too.
  const early = guestShell(tmpdir(), { noTable: true });
  fs.writeFileSync(early.file('/etc'), 'a file where a folder must be');
  const e = early.run(mf.installScript(mf.guestFiles(), mf.addressesFile('lima', [])));
  assert.notEqual(e.code, 0);
  assert.match(e.stdout, /^host=stopped$/m);
  // A reload nft refuses with the earlier wall still loaded: that wall holds,
  // and the Host is left as it is.
  const kept = guestShell(tmpdir(), { restartFails: true });
  const k = kept.run(mf.installScript(mf.guestFiles(), mf.addressesFile('lima', [])));
  assert.notEqual(k.code, 0);
  assert.doesNotMatch(k.stdout, /host=stopped/);
  assert.doesNotMatch(kept.systemctl(), /stop cseries-host/);
  // A run that succeeds stops nothing.
  const ok = guestShell(tmpdir());
  assert.equal(ok.run(mf.installScript(mf.guestFiles(), mf.addressesFile('lima', []))).code, 0);
  assert.doesNotMatch(ok.systemctl(), /stop cseries-host/);
});

test('Lima: an older guest\'s Host, started at boot before any wall, is stopped when the wall then cannot be loaded', async () => {
  const p = limaPaths(tmpdir(), 'linux');
  const m = fakeLima(p, { platform: 'linux', qemu: true });
  assert.equal((await limaRun([], m, p)).code, 0);
  assert.equal((await limaRun(['stop'], m, p)).code, 0);
  // The guest as an older release left it: no wall, and one that will not load.
  fs.rmSync(m.guestShell.root, { recursive: true, force: true });
  m.guestShell = guestShell(path.dirname(m.guestShell.root), { restartFails: true, noTable: true });
  const s = await limaRun(['start'], m, p);
  assert.notEqual(s.code, 0);
  assert.equal(m.hostStarted, false, 'the Host the boot started is stopped, not left running without the wall');
  assert.match(s.out.errText(), /so the Host is stopped/);
});

test('WSL: the wall goes in through wsl.exe as root, before the Host is configured, on every run', async () => {
  const p = testPaths();
  const m = fakeMachine(p, { distro: true });
  const rel = validRelease();
  rel.guest['wsl-x64'].sha256 = m.rootfsSha256;
  const out = captureOutput();
  const code = await main([], {
    platform: 'win32', arch: 'x64', env: {}, paths: p, out, runner: m.runner, net: m.net,
    sleep: async () => {}, prompt: async () => false, portState: async () => 'free', freeBytes: () => 64 * 1024 ** 3, osRelease: '10.0.22631',
    releaseInfo: { release: rel, placeholders: [], file: 'test' }, bastionKeys: m.bastionKeys, netInterfaces: () => IFACES,
  });
  assert.equal(code, 0, out.text() + out.errText());
  const calls = m.runner.calls.filter((c) => c.cmd === 'wsl.exe' && c.args[0] === '-d').map((c) => c.args.slice(5).join(' '));
  const wall = calls.indexOf('/bin/sh -s');
  assert.ok(wall >= 0 && wall < calls.indexOf('/usr/local/bin/cseries-guest configure'), calls.join(' | '));
  const addrs = fs.readFileSync(m.guestShell.file(mf.ADDRESSES), 'utf8');
  assert.doesNotMatch(addrs, /192\.168\.5\./, 'no Lima network on WSL');
  assert.match(addrs, /^block 192\.168\.1\.20$/m);
});

test('the drivers\' root shell: Lima through sudo in limactl shell, WSL through wsl.exe -u root --exec', async () => {
  const r = fakeRunner(() => ({ stdout: 'machine_firewall=loaded\n' }));
  const ctxL = { runner: r, paths: limaPaths(tmpdir(), 'linux') };
  await lima.driver('linux').guestShell(ctxL, 'echo hi\n');
  assert.deepEqual(r.calls[0].args, ['--tty=false', 'shell', '--workdir', '/', 'cseries', 'sudo', '/bin/sh', '-s']);
  assert.equal(r.calls[0].input, 'echo hi\n');
  await wsl.driver.guestShell({ runner: r }, 'echo hi\n');
  assert.deepEqual(r.calls[1].args, ['-d', 'cseries', '-u', 'root', '--exec', '/bin/sh', '-s']);
  assert.equal(r.calls[1].input, 'echo hi\n');
});
