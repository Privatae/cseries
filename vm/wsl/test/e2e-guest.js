#!/usr/bin/env node
'use strict';

// Runs the cseries command's guest steps inside a booted guest, as root, for
// real: the release's images handed to cseries-guest, pulled (here: found in
// the local store), started, the Host's health and /setup probed on the
// guest's 127.0.0.1:8800 (where WSL's forwarding connects), the setup code read
// from the Host's log, and the bastion's install-and-pair step tried (its
// pairing token minted in the guest; installing the bastion as a Linux
// service is not built yet, so it ends there, softly). Then the same steps again,
// which must change nothing.
//
//   node e2e-guest.js <packages/cseries dir> <host image@digest> <postgres image@digest>
//
// Only the VM driver is replaced: guest() runs cseries-guest directly, since
// this already runs inside the guest.

const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');

const [pkg, hostImage, pgImage] = process.argv.slice(2);
const { makeContext } = require(`${pkg}/src/cli`);
const host = require(`${pkg}/src/host`);
const bastion = require(`${pkg}/src/bastion`);
const setup = require(`${pkg}/src/setup`);
const { runSteps } = require(`${pkg}/src/steps`);

function split(ref) {
  const i = ref.indexOf('@');
  return { ref: ref.slice(0, i), digest: ref.slice(i + 1) };
}

const inside = {
  name: 'inside',
  label: 'test guest',
  supported: true,
  async guest(ctx, args, opts = {}) {
    if (opts.inherit) {
      const r = await ctx.runner.inherit('cseries-guest', args);
      return { code: r.code, stdout: '', stderr: '' };
    }
    const r = await ctx.runner.run('cseries-guest', args, opts);
    return { code: r.code, stdout: r.stdout.toString('utf8'), stderr: r.stderr.toString('utf8') };
  },
  unreachableHint: () => 'not applicable inside the guest',
};

const release = {
  version: '0.0.0-dev',
  images: { host: split(hostImage), postgres: split(pgImage) },
  guest: {},
  bastion: { min_version: '0.53.0' },
};

function invocation(unit) {
  return execFileSync('systemctl', ['show', unit, '-p', 'InvocationID', '--value']).toString().trim();
}

async function pass(n) {
  const ctx = makeContext({
    platform: 'linux',
    env: {},
    driver: inside,
    paths: {
      base: '/tmp/e2e', state: '/tmp/e2e/state.json', hostCa: '/tmp/e2e/host-ca.pem', installLog: '/tmp/e2e/bastion-install.log',
      bastionExe: '/nonexistent/privatae-bastion', serviceExe: '/nonexistent/privatae-bastion', join: require('node:path').join,
    },
    releaseInfo: { release, placeholders: [], file: 'e2e' },
    prompt: async () => false,
  });
  // The release check and download (bastion.getStep) are not this test's: it
  // starts from "nothing to download", so the step goes straight to the token.
  ctx.bastion = { version: release.bastion.min_version, download: null };
  const result = {};
  const steps = [
    host.step,
    bastion.installStep,
    { title: 'Opening C-Series', run: async (c) => { result.setup = await setup.showSetup(c, { open: false }); } },
  ];
  process.stdout.write(`\n===== pass ${n}\n`);
  const r = await runSteps(steps, ctx);
  return { r, result };
}

(async () => {
  const one = await pass(1);
  const hostRun = invocation('cseries-host.service');
  const pgRun = invocation('cseries-postgres.service');
  assert.equal(one.r.halted, false);
  assert.equal(one.result.setup.state, 'needed', 'a fresh Host needs setup');
  assert.match(one.result.setup.code || '', /^[0-9a-f]{6}-[0-9a-f]{6}-[0-9a-f]{6}$/, 'the setup code was read from the log');
  const all = execFileSync('sh', ['-c', 'podman logs cseries-host 2>&1']).toString();
  assert.equal(setup.newestSetupCode(all), one.result.setup.code, 'it is the newest code in the log');
  const cpus = Number(execFileSync('getconf', ['_NPROCESSORS_ONLN']).toString());
  const dropIn = require('node:fs').readFileSync('/etc/containers/systemd/cseries-host.container.d/10-cpu.conf', 'utf8');
  assert.match(dropIn, new RegExp(`^CPUQuota=${Math.max(1, cpus - 1) * 100}%$`, 'm'), 'configure wrote the processor ceiling from the guest\'s count');
  const memKb = /^MemTotal:\s*(\d+) kB$/m.exec(require('node:fs').readFileSync('/proc/meminfo', 'utf8'))[1];
  const memDropIn = require('node:fs').readFileSync('/etc/containers/systemd/cseries-host.container.d/10-memory.conf', 'utf8');
  assert.match(memDropIn, new RegExp(`^MemoryMax=${host.memoryMax(memKb)}$`, 'm'), 'configure wrote the memory ceiling from the guest\'s MemTotal');

  const two = await pass(2);
  assert.equal(two.result.setup.code, one.result.setup.code, 'same Host run, same code');
  assert.equal(invocation('cseries-host.service'), hostRun, 'the second pass did not restart the Host');
  assert.equal(invocation('cseries-postgres.service'), pgRun, 'the second pass did not restart the database');

  const failures = one.r.failures.map((f) => `${f.step}: ${f.error.message}`);
  process.stdout.write(`\ne2e: setup code ${one.result.setup.code}; second pass changed nothing; soft failures: ${failures.length ? failures.join(' | ') : 'none'}\n`);
})().catch((err) => {
  process.stderr.write(`e2e FAILED: ${err.stack || err}\n`);
  process.exit(1);
});
