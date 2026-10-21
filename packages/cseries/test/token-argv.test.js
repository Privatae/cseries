'use strict';

// The pairing token is a whole enrolment until it is redeemed, and a process's
// command line is readable by every account on the machine. So no argv this
// command builds may hold it, on any OS: not the commands it runs itself, not
// the elevated step's, not the bastion's. It goes only to `pair -`'s standard
// input. Each run below mints a token with a prefix found nowhere else, then
// looks for that prefix in every argv, and checks the token did reach pair, so
// a run that never paired cannot pass.

const test = require('node:test');
const assert = require('node:assert/strict');
const bastion = require('../src/bastion');
const { bastionCommands } = require('../src/bastion-commands');
const elevated = require('../src/win/elevated');
const posixElevated = require('../src/posix/elevated');
const { main, makeContext } = require('../src/cli');
const { captureOutput, testPaths, tmpdir, validRelease } = require('./helpers');
const { fakeMachine } = require('./fake-machine');
const { fakeLima, limaPaths } = require('./fake-lima');
const wsl = require('../src/vm/wsl');

const CANARY = 'skbArgvCanary7Q';

function argvHolds(cmd, args) {
  return [cmd, ...(args || [])].some((a) => String(a).includes(CANARY));
}

function envHolds(env) {
  return Object.values(env || {}).some((v) => String(v).includes(CANARY));
}

test('bastionCommands puts the token in no command\'s args, only in pair\'s stdin', () => {
  for (const release of [null, 'host-a']) {
    const plan = { release, pair: { enroll: 'https://127.0.0.1:8810/v1/local/redeem', token: `${CANARY}-1` }, anchor: true, enroll: true, owner: 'S-1-5-21-1-2-3-1001' };
    for (const ca of [elevated.CA_VAR, posixElevated.CA_VAR]) {
      const cmds = bastionCommands(plan, ca);
      for (const c of cmds) assert.ok(!argvHolds(c.args[0], c.args.slice(1)), `${c.what}: ${c.args.join(' ')}`);
      const pair = cmds.find((c) => c.what === 'pair');
      assert.equal(pair.args.at(-1), '-');
      assert.equal(pair.stdin, `${CANARY}-1`);
      assert.deepEqual(cmds.filter((c) => c.stdin !== undefined).map((c) => c.what), ['pair']);
    }
  }
});

test('Windows: no argv the install builds holds the token, and pair read it on stdin', async () => {
  const p = testPaths();
  const m = fakeMachine(p, { distro: true, tokenPrefix: CANARY });
  m.hostStarted = true;
  const ctx = makeContext({
    platform: 'win32', arch: 'x64', env: {}, paths: p, out: captureOutput(), runner: m.runner, net: m.net,
    sleep: async () => {}, prompt: async () => false, driver: wsl.driver,
    releaseInfo: { release: validRelease(), placeholders: [], file: 'test' }, bastionKeys: m.bastionKeys,
  });
  await bastion.getStep.run(ctx);
  assert.equal(await bastion.installStep.done(ctx), false);
  await bastion.installStep.run(ctx);

  assert.deepEqual(m.pairTokens, [`${CANARY}-1`], 'the token reached pair, on its standard input');
  for (const c of m.runner.calls) {
    assert.ok(!argvHolds(c.cmd, c.args), `${c.cmd} ${(c.args || []).join(' ')}`);
    assert.ok(!envHolds(c.env), `${c.cmd}'s environment`);
  }
  // Every command line the elevated script started the bastion with.
  assert.ok(m.bastionCommands.length >= 4);
  for (const a of m.bastionCommands) assert.ok(!argvHolds('privatae-bastion', a), a.join(' '));
  // In the elevated script, the token is only the string piped to pair.
  const script = m.bastionScripts.at(-1) || m.elevated.at(-1);
  const lines = script.split('\n').filter((l) => l.includes(CANARY));
  assert.equal(lines.length, 1);
  assert.match(lines[0], new RegExp(`^\\s*\\$text = Bastion 16 'pair' @\\([^)]*,'-'\\) '${CANARY}-1'$`));
});

function limaRun(platform) {
  const p = limaPaths(tmpdir(), platform);
  const m = fakeLima(p, { platform, tokenPrefix: CANARY, sudoEnv: { FAKE_ARGV_CANARY: CANARY } });
  const r = validRelease();
  for (const k of ['lima-x64', 'lima-arm64']) r.guest[k] = { ...r.guest[k], sha256: m.qcow2Sha256 };
  const out = captureOutput();
  return main([], {
    platform, arch: 'x64', env: {}, paths: p, out, runner: m.runner, net: m.net,
    sleep: async () => {}, prompt: async () => false, portState: async () => 'free', freeBytes: () => 64 * 1024 ** 3,
    releaseInfo: { release: r, placeholders: [], file: 'test' }, bastionKeys: m.bastionKeys,
  }).then((code) => ({ code, out, m }));
}

for (const platform of ['linux', 'darwin']) {
  test(`${platform}: no argv the install builds holds the token, and pair read it on stdin`, async () => {
    const { code, out, m } = await limaRun(platform);
    assert.equal(code, 0, out.text() + out.errText());
    const st = m.bastionState();
    assert.equal(st.token, `${CANARY}-1`, 'the token reached pair, on its standard input');

    // What this command ran: sudo's argv included, its script on stdin.
    for (const c of m.calls) assert.ok(!argvHolds(c.cmd, c.args), `${c.cmd} ${(c.args || []).join(' ')}`);
    const sudo = m.calls.filter((c) => /sudo$/.test(c.cmd) && c.args.includes('-s'));
    assert.equal(sudo.length, 1);
    assert.ok(sudo[0].input.includes(CANARY), 'the token travels in the script, on sudo\'s standard input');

    // What the script started the bastion with, as that process saw its argv.
    const calls = m.bastionCalls();
    assert.deepEqual(calls.map((c) => c.args[0]), platform === 'darwin'
      ? ['service', 'set-mode', 'pair', 'claim-anchor', 'stepup-enroll']
      : ['set-mode', 'pair', 'claim-anchor', 'stepup-enroll']);
    for (const c of calls) assert.ok(!argvHolds('privatae-bastion', c.args), c.args.join(' '));
    assert.equal(calls.find((c) => c.args[0] === 'pair').args.at(-1), '-');

    // On Linux, the command line of every process alive while pair ran, from
    // /proc. A program that ran and exited before (an exec'd printf) is
    // not seen here; the script checks below cover that.
    // Where this test runs has /proc, so the scan ran: a fake that skipped it
    // would make the check below pass on nothing.
    if (require('node:fs').existsSync('/proc/self/cmdline')) assert.equal(st.cmdlineScanned, true, 'the /proc scan did not run');
    if (st.cmdlineScanned) assert.deepEqual(st.cmdlineHits, [], 'a process had the token in its command line');

    // In the script, the token is only the stdin argument of bastion_in, a
    // shell function, which pipes it with the builtin printf after checking
    // that printf is one.
    const script = sudo[0].input;
    const lines = script.split('\n').filter((l) => l.includes(CANARY));
    assert.equal(lines.length, 1);
    assert.match(lines[0], new RegExp(`^bastion_in 16 'pair' '${CANARY}-1' 'pair' .* '-'$`));
    assert.match(script, /^ {2}case \$\(command -V printf 2>\/dev\/null\) in \*builtin\*\) ;; \*\) quit /m);
    assert.match(script, /^ {2}text=\$\(printf '%s\\n' "\$input" \| "\$exe" "\$@" 2>&1\); rc=\$\?$/m);
  });
}

test('the sudo script stops before pair when this /bin/sh has no builtin printf', () => {
  const plan = { release: null, pair: { enroll: 'https://127.0.0.1:8810/v1/local/redeem', token: `${CANARY}-1` }, anchor: false, enroll: false };
  const script = posixElevated.bastionInstallScript({
    platform: 'linux', nonce: 'abcd', program: '/usr/local/bin/privatae-bastion', unit: '', install: null, serviceInstall: false,
    hostCa: null, host: null, commands: bastionCommands(plan, posixElevated.CA_VAR),
  });
  // `command` answering as if printf were a program on PATH.
  const fake = script.replace('set -u', 'set -u\ncommand() { echo "printf is /usr/bin/printf"; }');
  const r = require('node:child_process').spawnSync('/bin/sh', ['-s'], { input: fake });
  const text = r.stdout.toString();
  assert.equal(r.status, 16, text);
  assert.match(text, /^error=this \/bin\/sh has no builtin printf/m);
  assert.ok(!text.includes(CANARY));
});
