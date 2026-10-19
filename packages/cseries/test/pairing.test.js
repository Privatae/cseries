'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const pairing = require('../src/pairing');
const bastion = require('../src/bastion');
const { makeContext } = require('../src/cli');
const { captureOutput, testPaths, validRelease } = require('./helpers');
const { fakeMachine } = require('./fake-machine');
const wsl = require('../src/vm/wsl');

const CA = '-----BEGIN CERTIFICATE-----\nMIIBexample0\n-----END CERTIFICATE-----';

test('a pairing token is accepted only with a CA and an https enroll address on loopback', () => {
  const ok = pairing.parsePairToken(JSON.stringify({ token: 'tok-1', host_ca_pem: CA, enroll: 'https://127.0.0.1:8810/v1/local/redeem' }));
  assert.equal(ok.enroll, 'https://127.0.0.1:8810/v1/local/redeem');
  assert.equal(ok.hostCaPem, `${CA}\n`);
  const bad = [
    { token: 'tok-1', host_ca_pem: CA, enroll: 'http://127.0.0.1:8810/v1/local/redeem' },
    { token: 'tok-1', host_ca_pem: CA, enroll: 'https://host.example:8810/v1/local/redeem' },
    { token: 'tok 1', host_ca_pem: CA, enroll: 'https://127.0.0.1:8810/v1/local/redeem' },
    { token: 'tok-1', host_ca_pem: 'not a cert', enroll: 'https://127.0.0.1:8810/v1/local/redeem' },
    { token: 'tok-1', host_ca_pem: CA },
  ];
  for (const b of bad) assert.throws(() => pairing.parsePairToken(JSON.stringify(b)));
  assert.throws(() => pairing.parsePairToken('Traceback (most recent call last):'), /not answer with JSON/);
});

function ctxFor(opts = {}) {
  const p = testPaths();
  const m = fakeMachine(p, { distro: true, ...opts });
  m.hostStarted = true;
  const ctx = makeContext({
    platform: 'win32', arch: 'x64', env: {}, paths: p, out: captureOutput(), runner: m.runner, net: m.net,
    sleep: async () => {}, prompt: async () => false, driver: wsl.driver,
    releaseInfo: { release: validRelease(), placeholders: [], file: 'test' }, bastionKeys: m.bastionKeys,
  });
  return { ctx, m, p };
}

test('the token is minted in the guest, and each mint is a new token', async () => {
  const { ctx } = ctxFor();
  const a = await pairing.mintToken(ctx);
  const b = await pairing.mintToken(ctx);
  assert.equal(a.token, 'tok-1');
  assert.equal(b.token, 'tok-2');
  assert.equal(a.caSha256, b.caSha256);
});

test('a Host CA that changed (the Host\'s data was deleted) is not rotated with --replace: the old anchor is released', async () => {
  const { ctx, m } = ctxFor();
  ctx.state.set('bastion', { service: true, host: 'host-a', hostCa: 'f'.repeat(64), anchored: true, enrolled: true });
  ctx.bastion = { version: '0.53.0', download: null };
  ctx.bastionService = 'running';
  const t = await pairing.mintToken(ctx);
  const plan = await bastion.planFor(ctx, t);
  assert.equal(plan.release, 'host-a');
  assert.deepEqual(plan.pair, { enroll: 'https://127.0.0.1:8810/v1/local/redeem', token: 'tok-1' });
  assert.equal(plan.anchor, true);
  assert.equal(plan.enroll, true);
  // The same CA as recorded: nothing to pair, nothing released.
  ctx.state.set('bastion', { service: true, owner: true, host: 'host-a', hostCa: t.caSha256, anchored: true, enrolled: true });
  const same = await bastion.planFor(ctx, t);
  assert.equal(same.release, null);
  assert.equal(same.pair, null);
  assert.equal(bastion.planNeeded(same), false);
  assert.equal(m.elevated.length, 0);
});

test('a Host without pair-token (an older image) is a soft, plain failure', async () => {
  const { ctx, m } = ctxFor();
  const run = m.runner.run.bind(m.runner);
  m.runner.run = async (cmd, args, o) => {
    if (cmd === 'wsl.exe' && args.includes('pair-token')) {
      return { code: 2, stdout: Buffer.alloc(0), stderr: Buffer.from('/usr/bin/python3: No module named cli.cseries\n') };
    }
    return run(cmd, args, o);
  };
  assert.equal(bastion.installStep.soft, true);
  ctx.bastion = { version: '0.53.0', download: null };
  await assert.rejects(bastion.installStep.done(ctx), /could not make a pairing token: .*No module named/);
});
