'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const setup = require('../src/setup');
const { captureOutput, fakeRunner } = require('./helpers');

test('the newest setup code in the log is the one returned', () => {
  const log = [
    'INFO boot',
    'WARNING CSERIES SETUP CODE: 0a1b2c-3d4e5f-6a7b8c — open /setup in a browser',
    'INFO restart',
    'WARNING CSERIES SETUP CODE: 9f8e7d-6c5b4a-392817 — open /setup in a browser',
    'INFO ready',
  ].join('\n');
  assert.equal(setup.newestSetupCode(log), '9f8e7d-6c5b4a-392817');
  assert.equal(setup.newestSetupCode('no code here'), null);
  assert.equal(setup.newestSetupCode('CSERIES SETUP CODE: 12345-123456-123456'), null, 'a malformed code is not taken');
  assert.equal(setup.newestSetupCode('SETUP CODE: 0a1b2c-3d4e5f-6a7b8c'), null, 'only the CSERIES line counts');
});

test('/setup answering 200 means setup is needed; a redirect to /login means the owner exists', () => {
  assert.equal(setup.setupState({ status: 200, location: '' }), 'needed');
  assert.equal(setup.setupState({ status: 303, location: '/login' }), 'done');
  assert.equal(setup.setupState({ status: 303, location: 'http://home.cseries.localhost:8800/login' }), 'done');
  assert.equal(setup.setupState({ status: 303, location: '/elsewhere' }), 'unknown');
  assert.equal(setup.setupState({ status: 500, location: '' }), 'unknown');
  assert.equal(setup.setupState(null), 'unknown');
});

function ctxWith({ setupProbe, hostLog, platform = 'win32' }) {
  const runner = fakeRunner();
  return {
    platform,
    env: {},
    out: captureOutput(),
    runner,
    net: { probe: async (url) => (url.endsWith('/setup') ? setupProbe : { status: 200, location: '' }) },
    driver: { guest: async () => ({ code: 0, stdout: hostLog, stderr: '' }) },
    sleep: async () => {},
  };
}

test('setup needed: the setup page, the code, both addresses, and the browser opened with cmd /c start', async () => {
  const ctx = ctxWith({ setupProbe: { status: 200, location: '' }, hostLog: 'CSERIES SETUP CODE: 0a1b2c-3d4e5f-6a7b8c x\n' });
  const r = await setup.showSetup(ctx, { open: true });
  assert.deepEqual(r, { state: 'needed', code: '0a1b2c-3d4e5f-6a7b8c', url: 'http://home.cseries.localhost:8800/setup' });
  const text = ctx.out.text();
  assert.match(text, /Open: {8}http:\/\/home\.cseries\.localhost:8800\/setup/);
  assert.match(text, /http:\/\/127\.0\.0\.1:8800 /);
  assert.match(text, /Setup code: {2}0a1b2c-3d4e5f-6a7b8c/);
  const open = ctx.runner.calls.find((c) => c.kind === 'detached');
  assert.equal(open.cmd, 'cmd.exe');
  assert.deepEqual(open.args, ['/c', 'start', '""', '"http://home.cseries.localhost:8800/setup"']);
  assert.equal(open.opts.verbatim, true);
});

test('owner exists: no code is read, and the dashboard opens', async () => {
  const ctx = ctxWith({ setupProbe: { status: 303, location: '/login' }, hostLog: 'CSERIES SETUP CODE: 0a1b2c-3d4e5f-6a7b8c\n' });
  const r = await setup.showSetup(ctx, { open: false });
  assert.deepEqual(r, { state: 'done', code: null, url: 'http://home.cseries.localhost:8800' });
  assert.ok(!/Setup code/.test(ctx.out.text()));
});

test('the browser opens with open on macOS and xdg-open on a Linux desktop', async () => {
  for (const [platform, env, cmd] of [['darwin', {}, 'open'], ['linux', { DISPLAY: ':0' }, 'xdg-open']]) {
    const runner = fakeRunner();
    await setup.openBrowser({ platform, env, runner }, 'http://home.cseries.localhost:8800');
    assert.equal(runner.calls[0].cmd, cmd);
  }
  const runner = fakeRunner();
  await setup.openBrowser({ platform: 'linux', env: {}, runner }, 'http://x');
  assert.equal(runner.calls.length, 0, 'no desktop, no browser');
});

test('a Host that never answers: up inside the VM is a forwarding problem, down is the Host\'s log', async () => {
  const base = {
    out: captureOutput(), sleep: async () => {},
    net: { probe: async () => null },
  };
  const upInside = { ...base, driver: { guest: async (_c, a) => ({ code: a[0] === 'health' ? 0 : 1, stdout: '', stderr: '' }), unreachableHint: () => 'check localhostForwarding' } };
  await assert.rejects(setup.waitHealthy(upInside, { timeoutMs: 0 }), (e) => /cannot reach it on 127\.0\.0\.1:8800/.test(e.message) && /localhostForwarding/.test(e.next));
  const down = { ...base, driver: { guest: async (_c, a) => (a[0] === 'health' ? { code: 1, stdout: '', stderr: '' } : { code: 0, stdout: 'Traceback: boom\n', stderr: '' }) } };
  await assert.rejects(setup.waitHealthy(down, { timeoutMs: 0 }), /did not answer.*\n.*boom/s);
});
