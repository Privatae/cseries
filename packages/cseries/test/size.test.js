'use strict';

// How big the Host is (src/size.js): the sizes offered, the one suggested,
// the question at the terminal, the flags, and `npx cseries resize` on Lima,
// for an account's install and the whole machine's, with the Host's data
// kept. Windows' side (the ceilings inside its WSL system) is in
// limits.test.js.

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const size = require('../src/size');
const { main } = require('../src/cli');
const { CliError } = require('../src/errors');
const { captureOutput, tmpdir, validRelease } = require('./helpers');
const { fakeLima, limaPaths, machineLimaPaths } = require('./fake-lima');

const GiB = 1024 ** 3;
const names = (m, limit) => size.choices(size.machineOf(m[0] * GiB, m[1]), limit).map((s) => s.name + (s.most ? '*' : ''));

test('the sizes offered leave the machine 2 processors and 4 GB, or a quarter when that is more, and end with most of it', () => {
  // A "16 GB" PC (the OS sees 15.6 GiB) with 8 processors keeps 4 GB and 2: up to C6-12.
  assert.deepEqual(names([15.6, 8]), ['C2-4', 'C4-8', 'C6-12*']);
  // A "32 GB" one with 12 keeps 8 GB and 3.
  assert.deepEqual(names([31.3, 12]), ['C2-4', 'C4-8', 'C4-16', 'C6-12', 'C8-24', 'C9-24*']);
  // 64 GB and 16: keeps 16 GB and 4.
  assert.deepEqual(names([64, 16]), ['C2-4', 'C4-8', 'C4-16', 'C6-12', 'C8-24', 'C8-32', 'C12-48*']);
  // 128 GB and 32: every size, then most of it.
  assert.deepEqual(names([128, 32]), ['C2-4', 'C4-8', 'C4-16', 'C6-12', 'C8-24', 'C8-32', 'C12-48', 'C16-64', 'C24-96*']);
  // 8 GB and 4: only the smallest, which is all it can spare.
  assert.deepEqual(names([8, 4]), ['C2-4*']);
  // Too small to keep its reserve: the smallest Host that boots, said short.
  const tiny = size.machineOf(4 * GiB, 2);
  assert.deepEqual(size.most(tiny), { cpus: 1, memoryGiB: 2, short: true });
  assert.deepEqual(names([4, 2]), ['C1-2*']);
});

test('the size suggested holds the Host, a coding engine and the browser, and never takes over half the memory unless chosen', () => {
  const sug = (gib, cpus, limit) => size.sizeName(size.suggested(size.machineOf(gib * GiB, cpus), limit));
  assert.equal(size.NEED_GIB, 5);
  assert.equal(sug(15.6, 8), 'C4-8', 'a 16 GB PC: half its memory, as sold, is 8');
  assert.equal(sug(15.6, 6), 'C4-8');
  assert.equal(sug(64, 16), 'C4-8', 'a big machine is not taken over by default');
  assert.equal(sug(7.7, 4), 'C2-4', 'an 8 GB PC: C4-8 would leave it nothing');
  assert.equal(sug(12, 8), 'C2-4', 'C4-8 fits a 12 GB PC with 8 processors, but is more than half its memory');
  assert.equal(sug(4, 2), 'C1-2');
  // Windows: what WSL gives stands too.
  assert.equal(sug(64, 16, { cpus: 16, memoryGiB: 6 }), 'C2-4');
  for (const [gib, cpus] of [[15.6, 8], [31.3, 16], [64, 16], [128, 32], [7.7, 4], [12, 4]]) {
    const s = size.suggested(size.machineOf(gib * GiB, cpus));
    assert.ok(s.memoryGiB <= Math.ceil(gib) / 2, `${gib} GiB: ${size.sizeName(s)}`);
  }
});

test('sizes are read like the Cloud\'s names, and the flags take a size or exact values, never both', () => {
  assert.deepEqual(size.parseSize('C4-16'), { cpus: 4, memoryGiB: 16 });
  assert.deepEqual(size.parseSize(' c8-32 '), { cpus: 8, memoryGiB: 32 });
  for (const bad of ['C0-4', 'C4-0', '4-16', 'C4_16', 'C4-16GB', 'C-4', '']) assert.equal(size.parseSize(bad), null, bad);
  assert.deepEqual(size.fromArgs(['--size', 'C4-16']), { cpus: 4, memoryGiB: 16 });
  assert.deepEqual(size.fromArgs(['--size=most']), { most: true });
  assert.deepEqual(size.fromArgs(['--cpus', '6', '--memory', '12GB']), { cpus: 6, memoryGiB: 12 });
  assert.deepEqual(size.fromArgs(['C8-32'], { positional: true }), { cpus: 8, memoryGiB: 32 });
  assert.equal(size.fromArgs(['C8-32']), null, 'an install takes no bare size');
  assert.equal(size.fromArgs(['--just-me']), null);
  for (const bad of [['--size', 'C4-16', '--cpus', '4'], ['--cpus', '4'], ['--memory', '8'], ['--size'], ['--size', 'big'], ['--cpus', 'x', '--memory', '8'], ['--cpus', '4', '--memory', '8TB']]) {
    assert.throws(() => size.fromArgs(bad), CliError, bad.join(' '));
  }
});

test('a size that does not fit is refused with what does', () => {
  const m = size.machineOf(15.6 * GiB, 8);
  assert.deepEqual(size.resolve({ cpus: 6, memoryGiB: 12 }, m), { cpus: 6, memoryGiB: 12 });
  assert.deepEqual(size.resolve({ most: true }, m), { cpus: 6, memoryGiB: 12 });
  assert.throws(() => size.resolve({ cpus: 4, memoryGiB: 16 }, m),
    (e) => /C4-16 does not fit here: this machine keeps 2 processors and 4 GB for itself, so the biggest the Host can be is C6-12\./.test(e.message) && /C2-4, C4-8, C6-12/.test(e.next));
  assert.throws(() => size.resolve({ cpus: 7, memoryGiB: 8 }, m), /C7-8 does not fit/);
  assert.throws(() => size.resolve({ cpus: 1, memoryGiB: 1 }, m), /too small/);
});

// The Lima machine (fake-lima.js), as lima.test.js drives it.
function releaseFor(m) {
  const r = validRelease();
  for (const k of ['lima-x64', 'lima-arm64']) r.guest[k] = { ...r.guest[k], sha256: m.qcow2Sha256 };
  return r;
}

function limaMachine(opts = {}) {
  const dir = tmpdir();
  const p = (opts.whole ? machineLimaPaths : limaPaths)(dir, 'linux');
  const m = fakeLima(p, { platform: 'linux', qemu: true, uid: opts.whole ? 0 : 1000, root: path.join(dir, 'machine'), ...opts.fake });
  const run = async (argv = [], extra = {}) => {
    const out = captureOutput();
    const code = await main(argv, {
      platform: 'linux', arch: 'x64', env: {}, paths: p, out, runner: m.runner, net: m.net,
      sleep: async () => {}, prompt: async () => false, portState: async () => 'free', freeBytes: () => 64 * GiB,
      releaseInfo: { release: releaseFor(m), placeholders: [], file: 'test' }, bastionKeys: m.bastionKeys, sudoPaths: m.sudoPaths,
      machine: { totalMem: 31.3 * GiB, cpus: 12 },
      runuserPaths: m.runuserPaths, hostname: 'test-machine', uid: opts.whole ? 0 : 1000,
      ...extra,
    });
    return { code, text: out.text(), err: out.errText() };
  };
  return { m, p, run };
}

const yamlSize = (m, p) => {
  const y = fs.readFileSync(path.join(p.lima, 'cseries', 'lima.yaml'), 'utf8');
  return `C${/^cpus: (\d+)$/m.exec(y)[1]}-${/^memory: "(\d+)GiB"$/m.exec(y)[1]}`;
};

test('a new install at a terminal shows the sizes that fit, the suggested one marked, and takes the one typed', async () => {
  const { m, p, run } = limaMachine();
  const asked = [];
  const answers = ['huge', 'C12-48', '3'];
  const r = await run([], { ask: async (q) => { asked.push(q); return answers.shift(); } });
  assert.equal(r.code, 0, r.err);
  assert.match(r.text, /How big should the Host be\?/);
  assert.match(r.text, / {3}1\) C2-4 {3}2 processors and 4 GB of memory\n/);
  assert.match(r.text, / {3}2\) C4-8 {3}4 processors and 8 GB of memory {3}\(suggested\)\n/);
  assert.match(r.text, / {3}6\) C9-24 {2}9 processors and 24 GB of memory, most of this machine\n/);
  assert.match(r.text, /"huge" is not one of them\./);
  assert.match(r.text, /C12-48 does not fit here/);
  assert.equal(asked.length, 3);
  assert.match(asked[0], /press Enter for C4-8/);
  assert.match(m.template, /^cpus: 4$/m);
  assert.match(m.template, /^memory: "16GiB"$/m);
  assert.deepEqual(JSON.parse(fs.readFileSync(p.state, 'utf8')).size, { cpus: 4, memoryGiB: 16 });
  // Enter takes the suggested one.
  const x = limaMachine();
  assert.equal((await x.run([], { ask: async () => '' })).code, 0);
  assert.match(x.m.template, /^cpus: 4$/m);
  assert.match(x.m.template, /^memory: "8GiB"$/m);
});

test('--size and --cpus/--memory answer for a scripted install; a size that does not fit stops it before anything is made', async () => {
  let x = limaMachine();
  let r = await x.run(['--size', 'C6-12']);
  assert.equal(r.code, 0, r.err);
  assert.equal(yamlSize(x.m, x.p), 'C6-12');
  assert.doesNotMatch(r.text, /How big should/);
  x = limaMachine();
  r = await x.run(['--cpus', '3', '--memory', '10']);
  assert.equal(r.code, 0, r.err);
  assert.equal(yamlSize(x.m, x.p), 'C3-10');
  x = limaMachine();
  r = await x.run(['--size', 'C16-64']);
  assert.equal(r.code, 1);
  assert.match(r.err, /C16-64 does not fit here/);
  assert.equal(x.m.template, null, 'no VM was made');
  x = limaMachine();
  r = await x.run(['--size', 'most']);
  assert.equal(r.code, 0, r.err);
  assert.equal(yamlSize(x.m, x.p), 'C9-24');
});

test('a second run keeps the size, and an install\'s --size never changes an existing Host: resize does', async () => {
  const { run } = limaMachine();
  assert.equal((await run(['--size', 'C2-4'])).code, 0);
  let r = await run();
  assert.equal(r.code, 0, r.err);
  assert.match(r.text, /It is C2-4: 2 processors and 4 GB of memory\. To change it, keeping everything: npx cseries resize/);
  r = await run(['--size', 'C4-8']);
  assert.equal(r.code, 1);
  assert.match(r.err, /This Host is C2-4 already/);
  assert.match(r.err, /npx cseries resize C4-8/);
});

test('an install from before sizes were chosen keeps its VM\'s size and records it', async () => {
  const { m, p, run } = limaMachine();
  assert.equal((await run(['--size', 'C8-15'])).code, 0);
  // As an earlier release left it: no size in the record.
  const st = JSON.parse(fs.readFileSync(p.state, 'utf8'));
  delete st.size;
  fs.writeFileSync(p.state, JSON.stringify(st));
  const r = await run();
  assert.equal(r.code, 0, r.err);
  assert.match(r.text, /It is C8-15, 8 processors and 15 GB of memory, as an earlier release made it\./);
  assert.deepEqual(JSON.parse(fs.readFileSync(p.state, 'utf8')).size, { cpus: 8, memoryGiB: 15 });
  assert.equal(m.edits || 0, 0, 'nothing resized');
});

for (const whole of [false, true]) {
  test(`resize on Lima (${whole ? 'the whole machine' : 'an account'}): the VM is stopped, given the size and started again, its disk and the Host's data kept`, async () => {
    const { m, p, run } = limaMachine({ whole });
    let r = await run(['--size', 'C2-4']);
    assert.equal(r.code, 0, r.err);
    const disk = path.join(p.lima, 'cseries', 'disk');
    const data = fs.readFileSync(disk, 'utf8');
    r = await run(['resize', 'C4-8']);
    assert.equal(r.code, 0, r.err);
    assert.match(r.text, /The Host is C2-4 now/);
    assert.match(r.text, /The VM is C4-8 now: 4 processors and 8 GB of memory\. Its disk, with all the Host's data, is the same\./);
    assert.match(r.text, /The Host is C4-8 and running again\./);
    assert.equal(yamlSize(m, p), 'C4-8');
    assert.equal(fs.readFileSync(disk, 'utf8'), data, 'the same disk');
    assert.equal(m.edits, 1);
    assert.deepEqual(JSON.parse(fs.readFileSync(p.state, 'utf8')).size, { cpus: 4, memoryGiB: 8 });
    r = await run(['status']);
    assert.match(r.text, /size: {6}C4-8, 4 processors and 8 GB of memory/);
    // Most of it, and the same size again changes nothing.
    r = await run(['resize', 'most']);
    assert.equal(r.code, 0, r.err);
    assert.equal(yamlSize(m, p), 'C9-24');
    r = await run(['resize', '--size', 'C9-24']);
    assert.match(r.text, /It is C9-24 already; nothing was changed\./);
    assert.equal(m.edits, 2);
  });
}

test('resize: a size that does not fit, no size and no terminal, or Lima refusing, each leave the VM as it was', async () => {
  const { m, p, run } = limaMachine({ fake: {} });
  assert.equal((await run(['--size', 'C2-4'])).code, 0);
  let r = await run(['resize', 'C16-64']);
  assert.equal(r.code, 1);
  assert.match(r.err, /C16-64 does not fit here/);
  r = await run(['resize']);
  assert.equal(r.code, 1);
  assert.match(r.err, /Which size\? There is no terminal here to ask at\./);
  assert.match(r.err, /The sizes that fit: C2-4, C4-8, C4-16, C6-12, C8-24, C9-24/);
  assert.equal(yamlSize(m, p), 'C2-4');
  // At a terminal, resize asks.
  r = await run(['resize'], { ask: async () => '2' });
  assert.equal(r.code, 0, r.err);
  assert.equal(yamlSize(m, p), 'C4-8');
  // Lima refuses: started again at the size it had, nothing recorded.
  const y = limaMachine({ fake: { editFail: 'level=fatal msg="invented refusal"' } });
  assert.equal((await y.run(['--size', 'C2-4'])).code, 0);
  r = await y.run(['resize', 'C4-8']);
  assert.equal(r.code, 1);
  assert.match(r.err, /Lima did not change the VM's size:\ninvented refusal|Lima did not change the VM's size:\nlevel=fatal msg="invented refusal"/);
  assert.equal(yamlSize(y.m, y.p), 'C2-4');
  assert.deepEqual(JSON.parse(fs.readFileSync(y.p.state, 'utf8')).size, { cpus: 2, memoryGiB: 4 });
  assert.equal(y.m.hostStarted, true, 'running again');
});

test('a resize that stopped after the VM was resized is finished by the same resize, never "nothing was changed"', async () => {
  const { m, p, run } = limaMachine();
  assert.equal((await run(['--size', 'C2-4'])).code, 0);
  const orig = m.runner.run.bind(m.runner);
  let failRestart = true;
  m.runner.run = async (cmd, args, o = {}) => {
    if (failRestart && m.edits === 1 && args.includes('sudo') && args[args.length - 1] === 'restart') {
      failRestart = false;
      return { code: 1, stdout: '', stderr: 'invented restart failure' };
    }
    return orig(cmd, args, o);
  };
  let r = await run(['resize', 'C4-8']);
  assert.equal(r.code, 1);
  assert.match(r.err, /invented restart failure/);
  assert.equal(yamlSize(m, p), 'C4-8', 'the VM was resized');
  // The same resize again starts the VM and applies the Host's ceilings.
  m.hostStarted = false;
  r = await run(['resize', 'C4-8']);
  assert.equal(r.code, 0, r.err);
  assert.doesNotMatch(r.text, /nothing was changed/);
  assert.match(r.text, /The Host is C4-8 and running again\./);
  assert.equal(m.hostStarted, true);
  assert.equal(JSON.parse(fs.readFileSync(p.state, 'utf8')).resizing, undefined, 'the resize is finished');
  r = await run(['resize', 'C4-8']);
  assert.match(r.text, /It is C4-8 already; nothing was changed\./);
});

test('a VM resized and then not started again is recorded at its new size', async () => {
  const { m, p, run } = limaMachine();
  assert.equal((await run(['--size', 'C2-4'])).code, 0);
  const orig = m.runner.run.bind(m.runner);
  m.runner.run = async (cmd, args, o = {}) => {
    if (m.edits === 1 && args.includes('start') && args.includes('cseries') && !args.includes('shell')) {
      return { code: 1, stdout: '', stderr: 'level=fatal msg="invented start failure"' };
    }
    return orig(cmd, args, o);
  };
  const r = await run(['resize', 'C4-8']);
  assert.equal(r.code, 1);
  assert.match(r.err, /invented start failure/);
  assert.equal(yamlSize(m, p), 'C4-8');
  assert.deepEqual(JSON.parse(fs.readFileSync(p.state, 'utf8')).size, { cpus: 4, memoryGiB: 8 }, 'state.json says the size the VM has');
});

test('resize of a stopped VM refuses a Host of another release once it is started, before its ceilings are sent', async () => {
  const { m, run } = limaMachine();
  assert.equal((await run(['--size', 'C2-4'])).code, 0);
  assert.equal((await run(['stop'])).code, 0);
  const orig = m.runner.run.bind(m.runner);
  const configured = [];
  m.runner.run = async (cmd, args, o = {}) => {
    if (args.includes('configure')) configured.push(args);
    const res = await orig(cmd, args, o);
    if (args.includes('status') && args.includes('sudo')) return { ...res, stdout: Buffer.from(res.stdout.toString().replace('release=1.2.3', 'release=1.2.0')) };
    return res;
  };
  const r = await run(['resize', 'C4-8']);
  assert.equal(r.code, 1);
  assert.match(r.err, /This Host runs release 1\.2\.0, and this command is release 1\.2\.3\./);
  assert.deepEqual(configured, [], 'nothing of this release was sent to it');
});

test('resize needs an install, and refuses one that runs another release', async () => {
  const { m, run } = limaMachine();
  let r = await run(['resize', 'C4-8']);
  assert.equal(r.code, 1);
  assert.match(r.err, /C-Series is not installed on this machine\./);
  assert.equal((await run(['--size', 'C2-4'])).code, 0);
  const orig = m.runner.run.bind(m.runner);
  m.runner.run = async (cmd, args, o = {}) => {
    const res = await orig(cmd, args, o);
    if (args.includes('status') && args.includes('sudo')) return { ...res, stdout: Buffer.from(res.stdout.toString().replace('release=1.2.3', 'release=1.2.0')) };
    return res;
  };
  r = await run(['resize', 'C4-8']);
  assert.equal(r.code, 1);
  assert.match(r.err, /This Host runs release 1\.2\.0, and this command is release 1\.2\.3\./);
  assert.match(r.err, /Update it first, then resize it: npx cseries@1\.2\.3 update, then npx cseries@1\.2\.3 resize C4-8/);
  assert.doesNotMatch(r.err, /cseries@1\.2\.0 resize/, 'an older release has no resize to run');
  // A newer Host: its own release's command resizes it.
  m.runner.run = async (cmd, args, o = {}) => {
    const res = await orig(cmd, args, o);
    if (args.includes('status') && args.includes('sudo')) return { ...res, stdout: Buffer.from(res.stdout.toString().replace('release=1.2.3', 'release=1.4.0')) };
    return res;
  };
  r = await run(['resize', 'C4-8']);
  assert.equal(r.code, 1);
  assert.match(r.err, /Resize it with its own release: npx cseries@1\.4\.0 resize C4-8/);
  assert.equal(m.edits || 0, 0);
});
