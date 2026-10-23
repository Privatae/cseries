'use strict';

// On Ubuntu 26.04 (sudo-rs, `Defaults use_pty`) the install hung at
// "Enter a code from the app to confirm": the code typed never reached the
// bastion. sudo ran the install script on a terminal of its own and passed the
// person's keystrokes on only when sudo's own standard input was their
// terminal; the script was on its standard input, a pipe.
//
// This runs the real elevate path (bastion.js osOps(ctx).elevate) in a real
// pseudo-terminal (util-linux `script`), against a stand-in sudo that keeps
// that rule: a command whose sudo was handed a pipe runs with no way to the
// person's terminal (a session of its own, setsid), and one whose sudo was
// handed the terminal gets it. The stand-in bastion's stepup-enroll refuses
// anything but a terminal and waits for a code, which this test types once the
// prompt shows. The old install (enrolment inside the piped script) fails here;
// the enrolment must reach the terminal and finish within the time limit.
// Everything here is invented test data.

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawn, spawnSync } = require('node:child_process');
const { tmpdir } = require('./helpers');

const PKG = path.resolve(__dirname, '..');
const have = (cmd) => spawnSync('/bin/sh', ['-c', `command -v ${cmd}`]).status === 0;
const usable = process.platform === 'linux' && have('script') && have('setsid')
  && /util-linux/.test(spawnSync('script', ['--version']).stdout.toString());

function setup(dir, { cache = true } = {}) {
  const stubs = path.join(dir, 'stubs');
  fs.mkdirSync(stubs);
  fs.writeFileSync(path.join(stubs, 'systemctl'), '#!/bin/sh\nexit 0\n', { mode: 0o755 });
  const sudo = path.join(dir, 'sudo');
  fs.writeFileSync(sudo, `#!/bin/sh
# A stand-in for sudo-rs with Defaults use_pty (Ubuntu 26.04), never for real.
if [ "$1" = -n ]; then
  ${cache ? 'shift; exec "$@"' : 'echo "sudo: a password is required" >&2; exit 1'}
fi
[ "$1" = -p ] && shift 2
if [ "$1" = /bin/sh ] && [ "$2" = -s ]; then
  # The script on sudo's standard input; this test's stand-ins first on its PATH.
  sed "s#^PATH=/usr/sbin:/usr/bin:/sbin:/bin\\$#PATH=${stubs}:/usr/sbin:/usr/bin:/sbin:/bin#" > ${JSON.stringify(path.join(dir, 'script.sh'))}
  set -- /bin/sh ${JSON.stringify(path.join(dir, 'script.sh'))}
  exec </dev/null
fi
if [ -t 0 ]; then exec "$@"; fi
# sudo's own standard input is not the terminal: the command gets none of it.
exec setsid -w "$@"
`, { mode: 0o755 });
  const bastion = path.join(dir, 'privatae-bastion');
  fs.writeFileSync(bastion, `#!/bin/sh
echo "$*" >> ${JSON.stringify(path.join(dir, 'calls'))}
case "$1" in
  set-mode) echo "mode set to $2" ;;
  stepup-enroll)
    [ -t 0 ] && [ -t 1 ] || { echo "stepup-enroll --console-only: not a console" >&2; exit 2; }
    printf 'Enter a code from the app to confirm: '
    read -r code
    [ "$code" = 246810 ] || { echo "that code did not match"; exit 1; }
    echo enrolled > ${JSON.stringify(path.join(dir, 'enrolled'))} ;;
  *) echo "unknown $1" >&2; exit 1 ;;
esac
`, { mode: 0o755 });
  const driver = path.join(dir, 'driver.js');
  fs.writeFileSync(driver, `'use strict';
const path = require('node:path');
const { makeContext } = require(${JSON.stringify(path.join(PKG, 'src/cli'))});
const bastion = require(${JSON.stringify(path.join(PKG, 'src/bastion'))});
const dir = ${JSON.stringify(dir)};
const ctx = makeContext({
  platform: 'linux',
  env: {},
  paths: {
    base: dir, state: path.join(dir, 'state.json'), hostCa: path.join(dir, 'host-ca.pem'),
    installLog: path.join(dir, 'install.log'), log: path.join(dir, 'cseries.log'), serviceExe: ${JSON.stringify(bastion)},
    serviceUnit: path.join(dir, 'unit'), join: path.join,
  },
});
ctx.sudoPaths = [${JSON.stringify(sudo)}];
const plan = { install: null, serviceInstall: false, restart: true, pair: null, hostCa: null, anchor: false, enroll: true, host: 'host-a' };
bastion.osOps(ctx).elevate(plan).then((r) => {
  process.stdout.write('RESULT ' + JSON.stringify({ code: r.code, declined: r.declined, enrolled: r.log.entries.some((e) => e.key === 'enrolled'), errors: r.log.entries.filter((e) => e.key === 'error').map((e) => e.value) }) + '\\n');
  process.exit(0);
});
`);
  return { driver, calls: path.join(dir, 'calls'), enrolled: path.join(dir, 'enrolled') };
}

// Runs the driver in a pseudo-terminal, types the code when the bastion asks
// for it, and gives up after limitMs (a hang).
function inTerminal(driver, limitMs = 20000) {
  return new Promise((resolve) => {
    const cmd = `${JSON.stringify(process.execPath)} ${JSON.stringify(driver)}`;
    const child = spawn('script', ['-q', '-e', '-c', cmd, '/dev/null'], { stdio: ['pipe', 'pipe', 'pipe'] });
    let seen = '';
    let typed = false;
    const timer = setTimeout(() => { child.kill('SIGKILL'); }, limitMs);
    child.stdout.on('data', (d) => {
      seen += d.toString('utf8');
      if (!typed && /Enter a code from the app to confirm:/.test(seen)) {
        typed = true;
        child.stdin.write('246810\r');
      }
    });
    child.on('close', () => {
      clearTimeout(timer);
      const m = /RESULT (\{.*\})/.exec(seen);
      resolve({ seen, result: m ? JSON.parse(m[1]) : null, typed });
    });
  });
}

test('the authenticator\'s code reaches the bastion under a use_pty sudo that keeps a piped command off the terminal', { skip: !usable && 'needs Linux with util-linux script and setsid' }, async () => {
  const dir = tmpdir();
  const s = setup(dir);
  const { seen, result, typed } = await inTerminal(s.driver);
  assert.ok(result, `the install finished (no hang):\n${seen}`);
  assert.ok(typed, 'the bastion asked for the code on the terminal');
  assert.deepEqual(result, { code: 0, declined: false, enrolled: true, errors: [] }, seen);
  assert.equal(fs.readFileSync(s.enrolled, 'utf8').trim(), 'enrolled');
  assert.deepEqual(fs.readFileSync(s.calls, 'utf8').trim().split('\n'),
    ['set-mode networked', 'stepup-enroll --console-only --host host-a']);
  assert.doesNotMatch(seen, /sudo asks for your password once more/, 'the cached credentials carry the second sudo');
});

test('where sudo\'s cache does not carry the second run, the person is told before sudo asks again', { skip: !usable && 'needs Linux with util-linux script and setsid' }, async () => {
  const dir = tmpdir();
  const s = setup(dir, { cache: false });
  const { seen, result } = await inTerminal(s.driver);
  assert.ok(result, `the install finished (no hang):\n${seen}`);
  assert.equal(result.enrolled, true, seen);
  assert.match(seen, /sudo asks for your password once more, for this step\./);
});
