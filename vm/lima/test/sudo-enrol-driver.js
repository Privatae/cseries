#!/usr/bin/env node
'use strict';

// Plays the person at the terminal for vm/lima/test/sudo-enrol.sh: runs the
// installer's bastion step (bastion.js osOps(ctx).elevate) in a pseudo-terminal
// (util-linux `script`), types the account's password when sudo asks for it and
// the code when the bastion asks for it, counts the password prompts, and gives
// up after a minute, which is a hang. Prints one line, RESULT {...}.
//
//   node sudo-enrol-driver.js <packages/cseries> <bastion program> <password> <code>
//   node sudo-enrol-driver.js --inner <packages/cseries> <bastion program>
//
// Every value here is invented.

const path = require('node:path');
const { spawn } = require('node:child_process');

if (process.argv[2] === '--inner') {
  const [pkg, program] = process.argv.slice(3);
  const { makeContext } = require(path.join(pkg, 'src/cli'));
  const bastion = require(path.join(pkg, 'src/bastion'));
  const dir = process.env.HOME;
  const ctx = makeContext({
    platform: 'linux',
    env: {},
    paths: {
      base: dir, state: path.join(dir, 'state.json'), hostCa: path.join(dir, 'host-ca.pem'),
      installLog: path.join(dir, 'install.log'), log: path.join(dir, 'cseries.log'),
      serviceExe: program, serviceUnit: path.join(dir, 'unit'), join: path.join,
    },
  });
  const plan = { install: null, serviceInstall: false, restart: true, pair: null, hostCa: null, anchor: false, enroll: true, host: 'host-a' };
  bastion.osOps(ctx).elevate(plan).then((r) => {
    const e = r.log.entries;
    process.stdout.write(`INNER ${JSON.stringify({ code: r.code, enrolled: e.some((x) => x.key === 'enrolled'), errors: e.filter((x) => x.key === 'error').map((x) => x.value) })}\n`);
    process.exit(0);
  });
  return;
}

const [pkg, program, password, code] = process.argv.slice(2);
const inner = [process.execPath, __filename, '--inner', pkg, program].map((a) => `'${a.replace(/'/g, `'\\''`)}'`).join(' ');
const child = spawn('script', ['-q', '-e', '-c', inner, '/dev/null'], { stdio: ['pipe', 'pipe', 'inherit'] });
let seen = '';
let since = 0;
let prompts = 0;
let typedCode = false;
const timer = setTimeout(() => child.kill('SIGKILL'), 60000);
child.stdout.on('data', (d) => {
  seen += d.toString('utf8');
  const fresh = seen.slice(since);
  if (/\[cseries\] sudo password for \S+:/.test(fresh)) {
    prompts++;
    since = seen.length;
    child.stdin.write(`${password}\r`);
  } else if (!typedCode && /Enter a code from the app to confirm:/.test(fresh)) {
    typedCode = true;
    since = seen.length;
    child.stdin.write(`${code}\r`);
  }
});
child.on('close', () => {
  clearTimeout(timer);
  const m = /INNER (\{.*\})/.exec(seen);
  process.stdout.write(`${seen.replace(/INNER \{.*\}\r?\n?/, '')}\n`);
  process.stdout.write(`RESULT ${JSON.stringify({ hung: !m, prompts, typedCode, ...(m ? JSON.parse(m[1]) : {}) })}\n`);
});
