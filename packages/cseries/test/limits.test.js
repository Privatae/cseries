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

const UNITS = path.join(__dirname, '..', '..', '..', 'vm', 'wsl', 'rootfs', 'etc', 'containers', 'systemd');

function machine(opts = {}) {
  const p = testPaths();
  const m = fakeMachine(p, opts);
  const rel = validRelease();
  rel.guest['wsl-x64'].sha256 = m.rootfsSha256;
  const run = async (argv = []) => {
    const out = captureOutput();
    const code = await main(argv, {
      platform: 'win32', arch: 'x64', env: {}, paths: p, out, runner: m.runner, net: m.net,
      sleep: async () => {}, prompt: async () => false, portState: async () => 'free', freeBytes: () => 64 * 1024 ** 3, osRelease: '10.0.22631',
      releaseInfo: { release: rel, placeholders: [], file: 'test' }, bastionKeys: m.bastionKeys,
    });
    return { code, text: out.text(), err: out.errText() };
  };
  return { m, run };
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

const GiB = 1024 ** 3;

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

// The WSL VM's limits, in the person's own .wslconfig.
const { vmLimits, mergeWslConfig } = require('../src/vm/wslconfig');

test('the VM gets half the PC\'s memory and all its processors, as WSL and Docker Desktop default', () => {
  assert.deepEqual(vmLimits(16 * GiB, 16), { memory: '8GB', processors: '16' });
  assert.deepEqual(vmLimits(15.9 * GiB, 8), { memory: '8GB', processors: '8' });
  assert.deepEqual(vmLimits(8 * GiB, 4), { memory: '4GB', processors: '4' });
  assert.deepEqual(vmLimits(2 * GiB, 1), { memory: '2GB', processors: '1' });
});

test('no .wslconfig yet: the section is written', () => {
  const r = mergeWslConfig('', { memory: '8GB', processors: '8' });
  assert.equal(r.text, '[wsl2]\nmemory=8GB\nprocessors=8\n');
  assert.deepEqual(r.added, ['memory', 'processors']);
});

test('the person\'s own values stay, stricter or looser, in any case, and only the missing key is added', () => {
  for (const mem of ['memory=2GB', 'Memory = 64GB', 'MEMORY=512MB']) {
    const text = `[general]\nx=1\n\n[wsl2]\n${mem}\nswap=0\n\n[experimental]\nsparseVhd=true\n`;
    const r = mergeWslConfig(text, { memory: '8GB', processors: '8' });
    assert.deepEqual(r.added, ['processors']);
    assert.equal(r.text, `[general]\nx=1\n\n[wsl2]\n${mem}\nswap=0\nprocessors=8\n\n[experimental]\nsparseVhd=true\n`);
  }
});

test('a file that already has both is returned untouched; a commented key does not count', () => {
  const both = '[wsl2]\nmemory=3GB\nprocessors=2\n';
  assert.deepEqual(mergeWslConfig(both, { memory: '8GB', processors: '8' }), { text: both, added: [] });
  const r = mergeWslConfig('[wsl2]\n# memory=3GB\n; processors=2\n', { memory: '8GB', processors: '8' });
  assert.deepEqual(r.added, ['memory', 'processors']);
});

test('keys elsewhere do not count as [wsl2] keys, and CRLF files stay CRLF', () => {
  const r = mergeWslConfig('[experimental]\r\nmemory=1GB\r\n', { memory: '8GB' });
  assert.equal(r.text, '[experimental]\r\nmemory=1GB\r\n\r\n[wsl2]\r\nmemory=8GB\r\n');
});

test('an install adds the limits to the person\'s .wslconfig, keeps what they set, and says so', async () => {
  const p = testPaths();
  fs.mkdirSync(path.dirname(p.wslConfig), { recursive: true });
  fs.writeFileSync(p.wslConfig, '﻿[wsl2]\nmemory=3GB\nlocalhostForwarding=true\n');
  const m = fakeMachine(p, { distro: false });
  const rel = validRelease();
  rel.guest['wsl-x64'].sha256 = m.rootfsSha256;
  const out = captureOutput();
  const code = await main([], {
    platform: 'win32', arch: 'x64', env: {}, paths: p, out, runner: m.runner, net: m.net,
    sleep: async () => {}, prompt: async () => false, portState: async () => 'free', freeBytes: () => 64 * 1024 ** 3, osRelease: '10.0.22631',
    releaseInfo: { release: rel, placeholders: [], file: 'test' }, bastionKeys: m.bastionKeys,
    machine: { totalMem: 16 * GiB, cpus: 12 },
  });
  assert.equal(code, 0, out.errText());
  assert.equal(fs.readFileSync(p.wslConfig, 'utf8'), '\uFEFF[wsl2]\nmemory=3GB\nlocalhostForwarding=true\nprocessors=12\n');
  assert.match(out.text(), /Added processors=12 to .*\.wslconfig/);
});

test('a PC with no .wslconfig gets one with both limits, and a second run leaves it alone', async () => {
  const p = testPaths();
  const m = fakeMachine(p, { distro: false });
  const rel = validRelease();
  rel.guest['wsl-x64'].sha256 = m.rootfsSha256;
  const go = () => main([], {
    platform: 'win32', arch: 'x64', env: {}, paths: p, out: captureOutput(), runner: m.runner, net: m.net,
    sleep: async () => {}, prompt: async () => false, portState: async () => 'free', freeBytes: () => 64 * 1024 ** 3, osRelease: '10.0.22631',
    releaseInfo: { release: rel, placeholders: [], file: 'test' }, bastionKeys: m.bastionKeys,
    machine: { totalMem: 32 * GiB, cpus: 16 },
  });
  assert.equal(await go(), 0);
  assert.equal(fs.readFileSync(p.wslConfig, 'utf8'), '[wsl2]\nmemory=16GB\nprocessors=16\n');
  const before = fs.statSync(p.wslConfig).mtimeMs;
  assert.equal(await go(), 0);
  assert.equal(fs.statSync(p.wslConfig).mtimeMs, before);
});

// What the person's own file may look like, and the ways writing it can fail.
// The rule throughout: their file is changed only by appending our lines to
// text we read exactly, and a run never fails because of it.

test('a second [wsl2] section, or a header with a comment after it, still counts', () => {
  const two = '[wsl2]\nswap=0\n\n[experimental]\nx=1\n\n[wsl2]\nmemory=3GB\n';
  const r = mergeWslConfig(two, { memory: '8GB', processors: '8' });
  assert.deepEqual(r.added, ['processors'], 'memory set in the second [wsl2] section is theirs');
  assert.equal(r.text, '[wsl2]\nswap=0\nprocessors=8\n\n[experimental]\nx=1\n\n[wsl2]\nmemory=3GB\n');
  const commented = mergeWslConfig('[wsl2]  # mine\nmemory=3GB\n', { memory: '8GB' });
  assert.deepEqual(commented.added, [], 'a [wsl2] header followed by a comment is still the section');
});

function installWith(p, opts = {}) {
  const m = fakeMachine(p, { distro: false });
  const rel = validRelease();
  rel.guest['wsl-x64'].sha256 = m.rootfsSha256;
  const out = captureOutput();
  return main([], {
    platform: 'win32', arch: 'x64', env: {}, paths: p, out, runner: m.runner, net: m.net,
    sleep: async () => {}, prompt: async () => false, portState: async () => 'free', freeBytes: () => 64 * 1024 ** 3, osRelease: '10.0.22631',
    releaseInfo: { release: rel, placeholders: [], file: 'test' }, bastionKeys: m.bastionKeys,
    machine: { totalMem: 16 * GiB, cpus: 8 }, ...opts,
  }).then((code) => ({ code, text: out.text(), err: out.errText() }));
}

test('a .wslconfig that is not plain UTF-8 is left byte for byte, and the run says what to add', async () => {
  const utf16 = Buffer.concat([Buffer.from([0xff, 0xfe]), Buffer.from('[wsl2]\r\nswap=0\r\n', 'utf16le')]);
  const cp1252 = Buffer.from('[wsl2]\nswapFile=C:\\\\Users\\\\J\xfcrgen\\\\swap.vhdx\n', 'latin1');
  for (const bytes of [utf16, cp1252]) {
    const p = testPaths();
    fs.mkdirSync(path.dirname(p.wslConfig), { recursive: true });
    fs.writeFileSync(p.wslConfig, bytes);
    const r = await installWith(p);
    assert.equal(r.code, 0, r.err);
    assert.ok(fs.readFileSync(p.wslConfig).equals(bytes), 'the file is exactly as it was');
    assert.match(r.text, /memory=8GB/);
    assert.match(r.text, /processors=8/);
    assert.doesNotMatch(r.text, /^.*Added /m);
  }
});

test('a UTF-8 byte order mark stays where it was', async () => {
  const p = testPaths();
  fs.mkdirSync(path.dirname(p.wslConfig), { recursive: true });
  fs.writeFileSync(p.wslConfig, '\uFEFF[wsl2]\nmemory=3GB\n');
  const r = await installWith(p);
  assert.equal(r.code, 0, r.err);
  assert.equal(fs.readFileSync(p.wslConfig, 'utf8'), '\uFEFF[wsl2]\nmemory=3GB\nprocessors=8\n');
});

test('a .wslconfig that cannot be written (read-only, or open elsewhere) leaves the run going and says what to add', async () => {
  const p = testPaths();
  fs.mkdirSync(path.dirname(p.wslConfig), { recursive: true });
  fs.writeFileSync(p.wslConfig, '[wsl2]\nswap=0\n');
  const real = fs.writeFileSync;
  fs.writeFileSync = function (file, ...rest) {
    if (String(file) === p.wslConfig) {
      throw Object.assign(new Error(`EPERM: operation not permitted, open '${file}'`), { code: 'EPERM' });
    }
    return real.call(this, file, ...rest);
  };
  let r;
  try { r = await installWith(p); } finally { fs.writeFileSync = real; }
  assert.equal(r.code, 0, r.err);
  assert.equal(fs.readFileSync(p.wslConfig, 'utf8'), '[wsl2]\nswap=0\n');
  assert.match(r.text, /EPERM/);
  assert.match(r.text, /memory=8GB/);
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
