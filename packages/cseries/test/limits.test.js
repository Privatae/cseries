'use strict';

// How much of the machine the Host may take. Memory: the Linux system's
// memory less its reserve and the database's 512 MB (cseries-postgres.
// container), which the cseries command sends to the guest's configure from
// the MemTotal the guest's status reports; the base unit's share stands only
// without it. Processors: every one the Linux system has but one, sent the
// same way from the count status reports.

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const host = require('../src/host');
const { main } = require('../src/cli');
const { captureOutput, testPaths, validRelease } = require('./helpers');
const { fakeMachine } = require('./fake-machine');

const GiB = 1024 ** 3;
const UNITS = path.join(__dirname, '..', '..', '..', 'vm', 'wsl', 'rootfs', 'etc', 'containers', 'systemd');

function machine(opts = {}) {
  const p = testPaths();
  const m = fakeMachine(p, opts);
  const rel = validRelease();
  rel.guest['wsl-x64'].sha256 = m.rootfsSha256;
  const run = async (argv = [], extra = {}) => {
    const out = captureOutput();
    const code = await main(argv, {
      platform: 'win32', arch: 'x64', env: {}, paths: p, out, runner: m.runner, net: m.net,
      sleep: async () => {}, prompt: async () => false, portState: async () => 'free', freeBytes: () => 64 * 1024 ** 3, osRelease: '10.0.22631',
      releaseInfo: { release: rel, placeholders: [], file: 'test' }, bastionKeys: m.bastionKeys,
      machine: { totalMem: 32 * GiB, cpus: 16 }, ...extra,
    });
    return { code, text: out.text(), err: out.errText() };
  };
  return { m, run, p };
}

test('the Host may use every processor but one, and all of a machine that has only one', () => {
  assert.equal(host.cpuQuota(8), '700%');
  assert.equal(host.cpuQuota('16'), '1500%');
  assert.equal(host.cpuQuota(2), '100%');
  assert.equal(host.cpuQuota(1), '100%');
});

test('no processor ceiling is sent without a count the guest reported', () => {
  for (const v of [undefined, null, '', '0', '-2', '8.5', 'abc', 'x8', '08']) {
    assert.equal(host.cpuQuota(v), null, `count ${JSON.stringify(v)}`);
  }
});

test('configure carries the ceiling only when there is one', () => {
  const rel = validRelease();
  assert.match(host.configInput(rel, { cpuQuota: '700%' }), /\nrelease=1\.2\.3\ncpu_quota=700%\n$/);
  assert.doesNotMatch(host.configInput(rel), /cpu_quota/);
  assert.doesNotMatch(host.configInput(rel, { cpuQuota: null }), /cpu_quota/);
});

test('an install hands the guest its ceiling from the guest\'s own count, and says so', async () => {
  const { m, run } = machine({ distro: true, cpus: 12 });
  const r = await run();
  assert.equal(r.code, 0, r.err);
  assert.match(m.images, /^cpu_quota=1100%$/m);
  assert.match(r.text, /The Host may use 11 of this Linux system's 12 processors\./);
});

test('a guest image from before the count was reported is configured as before', async () => {
  const { m, run } = machine({ distro: true, cpus: null });
  const r = await run();
  assert.equal(r.code, 0, r.err);
  assert.doesNotMatch(m.images, /cpu_quota/);
  assert.doesNotMatch(r.text, /of this Linux system's/);
});

test('the same count changes nothing; a resized Linux system restarts the Host once, on the new ceiling', async () => {
  const { m, run } = machine({ distro: true, cpus: 8 });
  await run();
  assert.equal(m.restarts, 1, 'the first configure starts the Host on its images');
  await run();
  assert.equal(m.restarts, 1, 'nothing changed, nothing restarted');
  m.cpus = 4;
  await run();
  assert.equal(m.restarts, 2, 'restarted once, for the new ceiling');
  assert.match(m.images, /^cpu_quota=300%$/m);
});

// The guest's unit files, as systemd reads them: [Section] then key=value,
// a later assignment of a key replacing an earlier one.
function unit(name) {
  const out = {};
  let section = null;
  for (const raw of fs.readFileSync(path.join(UNITS, name), 'utf8').split('\n')) {
    const line = raw.trim();
    if (!line || line.startsWith('#')) continue;
    const s = /^\[(.+)\]$/.exec(line);
    if (s) { section = s[1]; out[section] = out[section] || {}; continue; }
    const i = line.indexOf('=');
    out[section][line.slice(0, i)] = line.slice(i + 1);
  }
  return out;
}


function bytes(value, systemBytes) {
  const pct = /^(\d+(?:\.\d+)?)%$/.exec(value);
  if (pct) return systemBytes * Number(pct[1]) / 100;
  const m = /^(\d+)([KMGT]?)$/.exec(value);
  assert.ok(m, `not a systemd size: ${value}`);
  return Number(m[1]) * 1024 ** ' KMGT'.indexOf(m[2] || ' ');
}

// GKE's reservation for a node's own system, which its containers may not
// use: 25% of the first 4 GiB, 20% of the next 4, 10% of the next 8, 6% of
// the next 112, 2% above that, and 100 MiB for eviction.
function systemReserve(systemBytes) {
  const tiers = [[4, 0.25], [4, 0.20], [8, 0.10], [112, 0.06], [Infinity, 0.02]];
  let left = systemBytes / GiB;
  let reserve = 0;
  for (const [size, share] of tiers) {
    const take = Math.min(left, size);
    reserve += take * share;
    left -= take;
  }
  return reserve * GiB + 100 * 1024 ** 2;
}

test('both containers have a memory ceiling, and neither can go past it into swap', () => {
  for (const name of ['cseries-host.container', 'cseries-postgres.container']) {
    const s = unit(name).Service;
    assert.ok(s.MemoryMax, `${name} has a MemoryMax`);
    assert.equal(s.MemorySwapMax, '0', `${name} may not swap`);
    // Past a MemoryHigh the kernel throttles every allocation in the unit,
    // so one runaway process would stall the whole Host.
    assert.equal(s.MemoryHigh, undefined, `${name} has no MemoryHigh`);
  }
});

// The Host's ceiling an install leaves in force on a Linux system of this
// size: the memory_max the cseries command handed configure (10-memory.conf
// sorts after the base unit, so it wins), or else the base unit's own.
async function effectiveHostMax(sysBytes) {
  const { m, run } = machine({ distro: true, memKb: sysBytes / 1024 });
  const r = await run();
  assert.equal(r.code, 0, r.err);
  const sent = /^memory_max=(.+)$/m.exec(m.images || '');
  return { value: sent ? sent[1] : unit('cseries-host.container').Service.MemoryMax, text: r.text };
}

for (const size of [2, 3, 4, 8]) {
  test(`a ${size} GiB Linux system: the Host's ceiling plus the database's leaves the system its reserve`, async () => {
    const sys = size * GiB;
    const { value } = await effectiveHostMax(sys);
    const pg = unit('cseries-postgres.container').Service.MemoryMax;
    const used = bytes(value, sys) + bytes(pg, sys);
    assert.ok(used + systemReserve(sys) <= sys,
      `${size} GiB: Host ${value} + database ${pg} leaves ${((sys - used) / GiB).toFixed(2)} GiB, under the ${(systemReserve(sys) / GiB).toFixed(2)} GiB reserve`);
    // And no tighter than that: the Host gets the rest, to the MiB.
    assert.ok(sys - used - systemReserve(sys) < 1024 ** 2, `${size} GiB: Host ${value} leaves memory unused`);
  });
}

// With the local runtime on the guest, the Host and the runtime's containers
// share one budget: the runtime admits containers only into what the
// reserve, the database's ceiling and the Host's ceiling leave
// (vm/runtime guard.py; scripts/test_runtime_guard.py checks the two agree).
async function effectiveWithRuntime(sysBytes) {
  const { m, run } = machine({ distro: true, memKb: sysBytes / 1024, runtime: true });
  const r = await run();
  assert.equal(r.code, 0, r.err);
  const sent = /^memory_max=(.+)$/m.exec(m.images || '');
  assert.ok(sent, 'a guest with the runtime is sent a memory ceiling');
  return { value: sent[1], text: r.text };
}

for (const size of [2, 3, 4, 8, 16, 32]) {
  test(`a ${size} GiB Linux system with the local runtime: the Host, the database, the containers and the system's reserve fit in it together`, async () => {
    const sys = size * GiB;
    const { value, text } = await effectiveWithRuntime(sys);
    const pg = bytes(unit('cseries-postgres.container').Service.MemoryMax, sys);
    const hostB = bytes(value, sys);
    const apps = host.containersMax(sys / 1024) * 1024 ** 2;
    assert.ok(hostB >= 512 * 1024 ** 2 && hostB <= 2 * GiB, `${size} GiB: the Host's ${value} is within 512 MB and 2 GiB`);
    assert.ok(hostB + pg + apps + systemReserve(sys) <= sys,
      `${size} GiB: Host ${value} + database + containers ${apps / 1024 ** 2} MB is more than the system has past its reserve`);
    // And nothing is left over: what the Host does not get, the containers do.
    if (size >= 3) assert.ok(sys - hostB - pg - apps - systemReserve(sys) < 2 * 1024 ** 2, `${size} GiB: memory left unused`);
    assert.match(text, new RegExp(`The Host may use up to ${parseInt(value, 10)} MB of this Linux system's ${size * 1024} MB of memory\\.`));
    assert.match(text, new RegExp(`Its apps, browser and coding engines may use up to ${host.containersMax(sys / 1024)} MB beside it\\.`));
  });
}

test('with the local runtime the Host gets a share of the room, not all of it, and the containers room for the browser from 4 GiB', () => {
  for (const size of [4, 8]) {
    const kb = size * 1024 * 1024;
    assert.ok(parseInt(host.memoryMax(kb, { runtime: true }), 10) < parseInt(host.memoryMax(kb), 10), `${size} GiB`);
    assert.ok(host.containersMax(kb) >= 1024, `${size} GiB: the browser's 1 GiB fits`);
  }
  assert.ok(host.containersMax(8 * 1024 * 1024) >= 2048, '8 GiB: the browser and a coding engine fit together');
  assert.equal(host.memoryMax(1024 * 1024, { runtime: true }), '512M', 'the floor holds on a tiny system');
  assert.equal(host.containersMax(1024 * 1024), 0, 'and leaves the containers nothing, not a negative number');
  assert.equal(host.memoryMax(64 * 1024 * 1024, { runtime: true }), '2048M', 'the cap holds on a big system');
  assert.equal(host.hasRuntime({ runtime: 'active' }), true);
  assert.equal(host.hasRuntime({ runtime: 'inactive' }), true, 'installed but stopped is still the runtime\'s share');
  assert.equal(host.hasRuntime({ cpus: '8' }), false);
});

test('with the local runtime the Host\'s ceiling holds the busiest measured Host from a 3 GiB system up', () => {
  // The busiest live Host's largest working set over 7 days was 901 MiB
  // (host.js HOST_NEED's derivation); every other one stayed under 200 MiB.
  const BUSIEST_MI = 901;
  for (const size of [3, 4, 6, 8, 16, 64]) {
    const kb = size * 1024 * 1024;
    const hostMi = parseInt(host.memoryMax(kb, { runtime: true }), 10);
    assert.ok(hostMi >= BUSIEST_MI, `${size} GiB: the Host's ${hostMi} MB holds ${BUSIEST_MI} MB`);
    assert.ok(hostMi <= 2048, `${size} GiB: never over the cap`);
  }
  // Below that the Host takes all the room and the containers none, never
  // more than the room unless that is under the floor.
  assert.equal(host.memoryMax(2 * 1024 * 1024, { runtime: true }), '924M');
  assert.equal(host.containersMax(2 * 1024 * 1024), 0);
  assert.equal(host.memoryMax(4 * 1024 * 1024, { runtime: true }), '1024M');
  assert.equal(host.containersMax(4 * 1024 * 1024), 1436, '4 GiB: the browser or a coding engine beside it');
});

test('a guest without the local runtime still gives the Host all the room, and says nothing of containers', async () => {
  const { m, run } = machine({ distro: true, memKb: 4 * 1024 * 1024 });
  const r = await run();
  assert.equal(r.code, 0, r.err);
  assert.match(m.images, new RegExp(`^memory_max=${host.memoryMax(4 * 1024 * 1024)}$`, 'm'));
  assert.doesNotMatch(r.text, /coding engines may use/);
});

test('a Linux system too small for the reserve still gives the Host its floor, and says how much it may use', async () => {
  const { value, text } = await effectiveHostMax(1 * GiB);
  assert.equal(value, '512M');
  const eight = await effectiveHostMax(8 * GiB);
  assert.match(eight.text, /The Host may use up to [0-9]+ MB of this Linux system's 8192 MB of memory\./);
});

test('a guest image from before it reported its memory gets no memory_max, and keeps the base unit\'s share', async () => {
  const { m, run } = machine({ distro: true, memKb: null });
  const r = await run();
  assert.equal(r.code, 0, r.err);
  assert.doesNotMatch(m.images, /memory_max/);
});

test('a resized Linux system restarts the Host once, on the new memory ceiling', async () => {
  const { m, run } = machine({ distro: true, memKb: 8 * 1024 * 1024 });
  await run();
  await run();
  assert.equal(m.restarts, 1);
  m.memKb = 4 * 1024 * 1024;
  await run();
  assert.equal(m.restarts, 2);
});

test('the Host\'s memory ceiling follows the system it runs in, and its processor ceiling comes from configure', () => {
  const h = unit('cseries-host.container').Service;
  assert.match(h.MemoryMax, /%$/, 'a share of the Linux system\'s memory, not a fixed size');
  assert.equal(h.CPUQuota, undefined, 'the base unit names no processor count: configure writes 10-cpu.conf from the guest\'s');
  assert.equal(unit('cseries-postgres.container').Service.CPUQuota, undefined, 'the database has no processor ceiling');
});

// Windows: the size is a ceiling inside C-Series's own WSL system; the
// person's .wslconfig, which caps every WSL system, is never written.
const { State } = require('../src/state');
const { asOldRelease } = require('./old-wslconfig');
const sizeCaps = require('../src/vm/size-caps');

test('a new install on Windows writes no .wslconfig, and the person\'s own stays byte for byte', async () => {
  for (const before of [null, '﻿[wsl2]\nmemory=12GB\nlocalhostForwarding=true\n', Buffer.concat([Buffer.from([0xff, 0xfe]), Buffer.from('[wsl2]\r\nswap=0\r\n', 'utf16le')])]) {
    const { run, p } = machine({ distro: false });
    if (before !== null) {
      fs.mkdirSync(path.dirname(p.wslConfig), { recursive: true });
      fs.writeFileSync(p.wslConfig, before);
    }
    const r = await run();
    assert.equal(r.code, 0, r.err);
    if (before === null) assert.ok(!fs.existsSync(p.wslConfig), 'no file made');
    else assert.ok(fs.readFileSync(p.wslConfig).equals(Buffer.from(before)), 'the file is exactly as it was');
    assert.doesNotMatch(r.text, /Added |\.wslconfig.*add/);
  }
});

test('Windows: the Host\'s ceilings come from the size, not from WSL\'s VM, and cseries.slice gets the rest', async () => {
  // WSL's VM: 16 processors and 16 GiB (half of a 32 GB PC); the size, C4-8.
  const { m, run } = machine({ distro: false, cpus: 16, memKb: 16 * 1024 * 1024, runtime: true });
  const r = await run(['--size', 'C4-8']);
  assert.equal(r.code, 0, r.err);
  const kb = String(8 * 1024 * 1024);
  assert.match(m.images, /^cpu_quota=300%$/m);
  assert.match(m.images, new RegExp(`^memory_max=${host.memoryMax(kb, { runtime: true })}$`, 'm'));
  const log = m.guestShell.systemctl();
  assert.match(log, new RegExp(`^systemctl set-property cseries\\.slice MemoryMax=${host.containersMax(kb)}M AllowedCPUs=0-3$`, 'm'));
  assert.match(log, /^systemctl set-property cseries-host\.service AllowedCPUs=0-3$/m);
  assert.match(log, /^systemctl set-property cseries-postgres\.service AllowedCPUs=0-3$/m);
  assert.match(r.text, /The Host may use 3 of the 4 processors and 8192 MB of memory C4-8 gives C-Series\./);
  assert.match(r.text, /nothing else on this PC is limited by it/);
});

test('the guest script sets each loaded unit, names a missing slice, and refuses nonsense', () => {
  const s = sizeCaps.script({ cpus: 1, containersMb: 0 });
  assert.match(s, /cseries\.slice MemoryMax=1M AllowedCPUs=0 /, 'never MemoryMax=0, and one processor is "0"');
  assert.throws(() => sizeCaps.script({ cpus: 0, containersMb: 10 }), /cpus/);
  assert.throws(() => sizeCaps.script({ cpus: 2, containersMb: -1 }), /containersMb/);
});

test('Windows: no size above what WSL gives is offered, and the refusal names the person\'s own line', async () => {
  const { run, p } = machine({ distro: false });
  fs.mkdirSync(path.dirname(p.wslConfig), { recursive: true });
  fs.writeFileSync(p.wslConfig, '[wsl2]\nmemory=6GB\n');
  let r = await run(['--size', 'C4-8']);
  assert.equal(r.code, 1);
  assert.match(r.err, /C4-8 does not fit here: WSL gives its Linux systems 6 GB of memory and 16 processors at most \(as your .*\.wslconfig sets\), so the biggest the Host can be is C12-6\./);
  r = await run(['--size', 'C2-4']);
  assert.equal(r.code, 0, r.err);
  assert.equal(fs.readFileSync(p.wslConfig, 'utf8'), '[wsl2]\nmemory=6GB\n');
});

test('the lines an earlier release added to .wslconfig are taken out at the next run; a value the person changed stays', async () => {
  const { run, p } = machine({ distro: true });
  fs.mkdirSync(path.dirname(p.wslConfig), { recursive: true });
  fs.writeFileSync(p.wslConfig, '[wsl2]\nswap=0\n');
  asOldRelease(p, new State(p.state), { memory: '16GB', processors: '16' });
  fs.writeFileSync(p.wslConfig, fs.readFileSync(p.wslConfig, 'utf8').replace('processors=16', 'processors=6'));
  const r = await run();
  assert.equal(r.code, 0, r.err);
  assert.equal(fs.readFileSync(p.wslConfig, 'utf8'), '[wsl2]\nswap=0\nprocessors=6\n');
  assert.match(r.text, /Took memory=16GB out of .*\.wslconfig: an earlier release of C-Series added them/);
  assert.equal(JSON.parse(fs.readFileSync(p.state, 'utf8')).wslConfigAdded, undefined, 'settled: not touched again');
  // A file it made itself, untouched since: the file goes.
  const x = machine({ distro: true });
  asOldRelease(x.p, new State(x.p.state));
  assert.equal((await x.run()).code, 0);
  assert.ok(!fs.existsSync(x.p.wslConfig));
});

test('an earlier release\'s lines in a file that cannot be written, or is not UTF-8, stay with their record, and the run goes on', async () => {
  const { run, p } = machine({ distro: true });
  fs.mkdirSync(path.dirname(p.wslConfig), { recursive: true });
  fs.writeFileSync(p.wslConfig, '[wsl2]\nswap=0\n');
  asOldRelease(p, new State(p.state));
  const real = fs.writeFileSync;
  fs.writeFileSync = function (file, ...rest) {
    if (String(file) === p.wslConfig) throw Object.assign(new Error(`EPERM: operation not permitted, open '${file}'`), { code: 'EPERM' });
    return real.call(this, file, ...rest);
  };
  let r;
  try { r = await run(); } finally { fs.writeFileSync = real; }
  assert.equal(r.code, 0, r.err);
  assert.equal(fs.readFileSync(p.wslConfig, 'utf8'), '[wsl2]\nswap=0\nmemory=8GB\nprocessors=8\n');
  assert.match(r.text, /could not be changed: EPERM/);
  assert.ok(JSON.parse(fs.readFileSync(p.state, 'utf8')).wslConfigAdded, 'kept: uninstall offers them again');
  const x = machine({ distro: true });
  asOldRelease(x.p, new State(x.p.state));
  const utf16 = Buffer.concat([Buffer.from([0xff, 0xfe]), Buffer.from('[wsl2]\r\nmemory=8GB\r\n', 'utf16le')]);
  fs.writeFileSync(x.p.wslConfig, utf16);
  r = await x.run();
  assert.equal(r.code, 0, r.err);
  assert.ok(fs.readFileSync(x.p.wslConfig).equals(utf16));
  assert.match(r.text, /not saved as UTF-8/);
});

test('Windows: resize changes the ceilings inside the guest and restarts the Host; WSL\'s VM and the distribution stay', async () => {
  const { m, run, p } = machine({ distro: false, cpus: 16, memKb: 16 * 1024 * 1024, runtime: true });
  assert.equal((await run(['--size', 'C2-4'])).code, 0);
  const restarts = m.restarts;
  const callsBefore = m.runner.calls.length;
  const r = await run(['resize', 'C6-12']);
  assert.equal(r.code, 0, r.err);
  assert.match(r.text, /Changing the Host to C6-12: 6 processors and 12 GB of memory\. Its data stays as it is\./);
  const kb = String(12 * 1024 * 1024);
  assert.match(m.images, /^cpu_quota=500%$/m);
  assert.match(m.images, new RegExp(`^memory_max=${host.memoryMax(kb, { runtime: true })}$`, 'm'));
  assert.match(m.guestShell.systemctl(), new RegExp(`cseries\\.slice MemoryMax=${host.containersMax(kb)}M AllowedCPUs=0-5$`, 'm'));
  assert.equal(m.restarts, restarts + 1, 'the Host restarted once, on its new ceilings');
  assert.deepEqual(JSON.parse(fs.readFileSync(p.state, 'utf8')).size, { cpus: 6, memoryGiB: 12 });
  assert.ok(m.distros.includes('cseries'), 'the distribution, with the Host\'s data, is the same');
  assert.ok(!m.runner.calls.slice(callsBefore).some((c) => c.cmd === 'wsl.exe' && /--shutdown|--unregister|--import/.test(c.args.join(' '))), 'WSL is not restarted');
  const again = await run(['resize', 'C6-12']);
  assert.match(again.text, /It is C6-12 already; nothing was changed\./);
  assert.equal(m.restarts, restarts + 1);
});

test('the machine is measured on any Node 18 the package accepts (availableParallelism is 18.14 and later)', () => {
  const os = require('node:os');
  const { makeContext } = require('../src/cli');
  const had = Object.getOwnPropertyDescriptor(os, 'availableParallelism');
  delete os.availableParallelism;
  try {
    const ctx = makeContext({ platform: 'win32', arch: 'x64', env: {}, paths: testPaths(), out: captureOutput(), driver: null });
    assert.equal(ctx.machine.cpus, os.cpus().length);
    assert.ok(ctx.machine.totalMem > 0);
  } finally {
    if (had) Object.defineProperty(os, 'availableParallelism', had);
  }
});

// The remote-access door's image goes to configure only when the guest's
// status names the door (a guest image from before it would refuse door=).
test('an install hands a guest that knows the door the door\'s image, and an older guest none', async () => {
  const door = { ref: 'ghcr.io/privatae/cseries-remote-door:1.2.3', digest: `sha256:${'9'.repeat(64)}` };
  for (const knows of [true, false]) {
    const r = await (async () => {
      // validRelease() has no door: give this run's release one.
      const p = testPaths();
      const mm = fakeMachine(p, { distro: true, door: knows });
      const rel = validRelease({ images: { ...validRelease().images, door } });
      rel.guest['wsl-x64'].sha256 = mm.rootfsSha256;
      const out = captureOutput();
      const code = await main([], {
        platform: 'win32', arch: 'x64', env: {}, paths: p, out, runner: mm.runner, net: mm.net,
        sleep: async () => {}, prompt: async () => false, portState: async () => 'free', freeBytes: () => 64 * 1024 ** 3, osRelease: '10.0.22631',
        releaseInfo: { release: rel, placeholders: [], file: 'test' }, bastionKeys: mm.bastionKeys,
      });
      return { code, err: out.errText(), images: mm.images };
    })();
    assert.equal(r.code, 0, r.err);
    if (knows) assert.match(r.images, new RegExp(`^door=ghcr\\.io/privatae/cseries-remote-door:1\\.2\\.3@sha256:${'9'.repeat(64)}$`, 'm'));
    else assert.doesNotMatch(r.images, /door=/);
  }
});
