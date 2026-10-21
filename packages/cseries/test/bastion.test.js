'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const bastion = require('../src/bastion');
const { bastionCommands } = require('../src/bastion-commands');
const elevated = require('../src/win/elevated');
const posixElevated = require('../src/posix/elevated');
const { makeContext } = require('../src/cli');
const { CliError } = require('../src/errors');
const { sha256Hex } = require('../src/verify');
const { paths } = require('../src/platform');
const { captureOutput, fixture, testPaths, validRelease } = require('./helpers');
const { fakeMachine } = require('./fake-machine');
const wsl = require('../src/vm/wsl');

const CA = '-----BEGIN CERTIFICATE-----\nMIIBexample0\n-----END CERTIFICATE-----';

// A context on a fake machine; given {m, p}, a new run on that same machine
// (a fresh process: only state.json carries over).
function ctxFor(opts = {}, same = null) {
  const p = same ? same.p : testPaths();
  const m = same ? same.m : fakeMachine(p, { distro: true, ...opts });
  m.hostStarted = true;
  const ctx = makeContext({
    platform: 'win32', arch: 'x64', env: {}, paths: p, out: captureOutput(), runner: m.runner, net: m.net,
    sleep: async () => {}, prompt: async () => false, driver: wsl.driver,
    releaseInfo: { release: validRelease(), placeholders: [], file: 'test' }, bastionKeys: m.bastionKeys,
  });
  return { ctx, m, p };
}

// Both bastion steps, as the run does them.
async function both(ctx) {
  await bastion.getStep.run(ctx);
  if (await bastion.installStep.done(ctx)) return 'done';
  await bastion.installStep.run(ctx);
  return 'ran';
}

test('the published 0.52.0 manifest is verified, then refused as older than min_version 0.53.0', async () => {
  const realNet = {
    async fetchSmall(url) {
      if (url === 'https://privatae.ai/bastion/version.json') return fixture('bastion-version.json');
      if (url === 'https://privatae.ai/bastion/version.json.9fee703d23448dbe.sig') return fixture('bastion-version.json.9fee703d23448dbe.sig');
      throw new Error('404');
    },
  };
  const ctx = { net: realNet };
  const manifest = await bastion.fetchManifest(ctx);
  assert.equal(manifest.version, '0.52.0');
  assert.throws(() => bastion.checkMinVersion(manifest.version, '0.53.0'),
    (e) => e instanceof CliError && /published bastion is version 0\.52\.0/.test(e.message) && /needs version 0\.53\.0 or newer/.test(e.message));
  bastion.checkMinVersion(manifest.version, '0.52.0');
});

test('a manifest whose signature does not verify downloads and installs nothing', async () => {
  const { ctx, m, p } = ctxFor();
  ctx.bastionKeys = ['tWOyZm38DwTwbawSnvExQG/CQsVJBKYWc46arCBZj9E='];
  await assert.rejects(bastion.getStep.run(ctx), /failed its signature check/);
  assert.equal(m.downloads, undefined);
  assert.equal(fs.existsSync(p.serviceExe), false);
});

test('a missing signature installs nothing', async () => {
  const { ctx, m } = ctxFor();
  const orig = ctx.net.fetchSmall;
  ctx.net.fetchSmall = async (url) => { if (url.endsWith('.sig')) throw new Error('404'); return orig(url); };
  await assert.rejects(bastion.getStep.run(ctx), /signature/);
  assert.equal(m.downloads, undefined);
});

test('a binary whose sha256 is not the manifest\'s is not downloaded into place', async () => {
  const { ctx, m, p } = ctxFor();
  m.serve('https://dl.downloads.example/privatae-bastion-windows-amd64.exe', Buffer.from('something else'));
  await assert.rejects(bastion.getStep.run(ctx), /not the file this release pins/);
  assert.equal(fs.existsSync(p.serviceExe), false);
  assert.equal(m.elevated.length, 0);
});

test('getting the bastion is unelevated: verified into downloads, no prompt, nothing in Program Files', async () => {
  const { ctx, m, p } = ctxFor();
  await bastion.getStep.run(ctx);
  assert.equal(m.elevated.length, 0);
  assert.equal(fs.existsSync(p.serviceExe), false);
  assert.ok(ctx.bastion.download.file.startsWith(p.downloads));
  assert.equal(sha256Hex(fs.readFileSync(ctx.bastion.download.file)), ctx.bastion.download.sha256);
});

test('one prompt installs the service, pairs, anchors and enrols; a second run asks nothing', async () => {
  const { ctx, m, p } = ctxFor();
  assert.equal(await both(ctx), 'ran');
  assert.equal(m.elevated.length, 1);
  assert.equal(m.serviceInstalls, 1);
  assert.equal(fs.readFileSync(p.serviceExe, 'utf8'), 'fake bastion 0.53.2\n');
  assert.equal(m.bastion.mode, 'networked');
  assert.equal(m.bastion.anchor, 'host-a');
  assert.equal(m.bastion.enrolled['host-a'], true);
  assert.deepEqual(m.pairCalls[0].slice(0, 4), ['pair', '--redeem-url', 'https://127.0.0.1:8810/v1/local/redeem', '--host-ca']);
  assert.equal(m.pairCalls[0][4], `${p.serviceDir}/host-ca.pem`, 'pair reads the copy in Program Files, not the user\'s file');
  assert.deepEqual(m.pairCalls[0].slice(5), ['-'], 'the token is read from standard input, not an argument');
  assert.deepEqual(m.pairTokens, ['tok-1'], 'pair read the Host\'s token on its standard input');
  assert.equal(m.serviceRestarts, 1);
  assert.deepEqual(ctx.state.get('bastion'),
    { service: true, owner: true, host: 'host-a', hostCa: sha256Hex(Buffer.from(`${CA}\n`)), anchored: true, enrolled: true });
  assert.equal(m.bastion.owner, m.sid, 'the account that ran it owns the approvals');
  const tray = m.runner.calls.filter((c) => c.kind === 'detached' && c.cmd === p.serviceExe);
  assert.deepEqual(tray.map((c) => c.args), [['tray']], 'the tray is started for this person');
  assert.equal(m.registry['HKLM:\\Software\\Microsoft\\Windows\\CurrentVersion\\Run\\PrivataeBastionTray'], `"${p.serviceExe}" tray`);

  const { ctx: ctx2 } = ctxFor({}, { m, p });
  assert.equal(await both(ctx2), 'done');
  assert.equal(m.elevated.length, 1, 'no second prompt');
  assert.equal(m.downloads, 1, 'not downloaded again');
  assert.match(ctx2.out.text(), /The bastion 0\.53\.2 is installed\./);
});

test('a PrivataeBastion service this command did not install is refused, with what to do', async () => {
  const { ctx, m } = ctxFor({ foreignService: true });
  await assert.rejects(bastion.getStep.run(ctx), /already runs the bastion as a Windows service that this command did not install/);
  assert.equal(m.elevated.length, 0);
});

test('the old per-user install is removed: logon task, program, and its folder on the person\'s PATH', async () => {
  const { ctx, m, p } = ctxFor();
  fs.mkdirSync(p.bastionDir, { recursive: true });
  fs.writeFileSync(p.bastionExe, 'old per-user bastion');
  m.tasks.add('PrivataeBastion');
  const k = `HKU\\${m.sid}\\Environment\\Path`;
  m.registry[k] = `%USERPROFILE%\\bin;${p.bastionDir};C:\\Tools`;
  await both(ctx);
  assert.equal(fs.existsSync(p.bastionExe), false);
  assert.equal(m.tasks.has('PrivataeBastion'), false);
  assert.equal(m.registry[k], '%USERPROFILE%\\bin;C:\\Tools');
});

test('UAC declined: a soft failure that says what to do, and nothing recorded', async () => {
  const { ctx, m } = ctxFor({ uac: 'no' });
  await bastion.getStep.run(ctx);
  assert.equal(await bastion.installStep.done(ctx), false);
  assert.equal(bastion.installStep.soft, true);
  await assert.rejects(bastion.installStep.run(ctx),
    (e) => e instanceof CliError && /permission prompt was declined/.test(e.message) && /choose Yes/.test(e.next));
  assert.equal(m.elevated.length, 1);
  assert.deepEqual(ctx.state.get('bastion'), {});
});

test('a run that stopped at claim-anchor is finished by the next one, which only anchors and enrols', async () => {
  const { ctx, m, p } = ctxFor({ failAt: 'claim-anchor' });
  await bastion.getStep.run(ctx);
  await bastion.installStep.done(ctx);
  await assert.rejects(bastion.installStep.run(ctx), /could not anchor itself to the Host .*claim-anchor exited with 1/);
  const st = ctx.state.get('bastion');
  assert.equal(st.service, true, 'the service it installed is recorded as its own');
  assert.equal(st.host, 'host-a');
  assert.equal(st.anchored, false);
  m.failAt = null;
  const { ctx: c2 } = ctxFor({}, { m, p });
  assert.equal(await both(c2), 'ran');
  const script = m.bastionScripts.at(-1);
  assert.ok(!/Emit 'step=install'/.test(script), 'the program is not installed again');
  assert.ok(!/@\('pair'/.test(script), 'not paired again');
  assert.match(script, /@\('claim-anchor'\)/);
  assert.match(script, /& \$exe 'stepup-enroll' '--console-only' '--host' \$hostName/);
  assert.match(script, /^\$hostName = 'host-a'$/m);
  assert.equal(m.pairCalls.length, 1);
  assert.equal(m.bastion.anchor, 'host-a');
  assert.deepEqual(c2.state.get('bastion'), { ...st, anchored: true, enrolled: true });
});

test('the bastion command sequence is the same on macOS and Linux as in the Windows script', () => {
  const plan = { release: 'host-a', pair: { enroll: 'https://127.0.0.1:8810/v1/local/redeem', token: 'tok-9' }, anchor: true, enroll: true };
  const winCmds = bastionCommands(plan, elevated.CA_VAR).map((c) => c.args[0]);
  assert.deepEqual(winCmds, ['set-mode', 'release-anchor', 'pair', 'claim-anchor', 'stepup-enroll']);
  for (const platform of ['darwin', 'linux']) {
    const pp = paths(platform, {}, '/home/someone');
    const script = posixElevated.bastionInstallScript({
      platform, nonce: 'abcd', program: pp.serviceExe, unit: bastion.linuxUnit(pp.serviceExe),
      install: { file: '/tmp/dl/privatae-bastion-0.53.0', sha256: 'e'.repeat(64) }, serviceInstall: true,
      hostCa: { file: '/tmp/ca.pem', sha256: 'f'.repeat(64) }, host: null,
      commands: bastionCommands(plan, posixElevated.CA_VAR),
    });
    const order = [...script.matchAll(/^bastion(?:_in)? \d+ '([^']+)'|^"\$exe" '(stepup-enroll)'/gm)].map((x) => (x[1] || x[2]).split(' ')[0]);
    assert.deepEqual(order.filter((c) => c !== 'service'), winCmds, platform);
    assert.match(script, /^bastion_in 16 'pair' 'tok-9' 'pair' '--redeem-url' 'https:\/\/127\.0\.0\.1:8810\/v1\/local\/redeem' '--host-ca' "\$ca" '-'$/m);
    assert.ok(script.includes(`"$exe" 'stepup-enroll' '--console-only' '--host' "$hostName" </dev/tty >/dev/tty 2>&1`), platform);
    if (platform === 'darwin') {
      assert.equal(pp.serviceExe, '/Library/Application Support/privatae/bin/privatae-bastion');
      assert.match(script, /^HOME=\/var\/root$/m);
      assert.match(script, /^exe='\/Library\/Application Support\/privatae\/bin\/privatae-bastion'$/m);
      assert.match(script, /chown root:wheel "\$\(dirname "\$dir"\)" "\$dir"/);
      assert.match(script, /^bastion 12 'service install' service install$/m);
      assert.match(script, /launchctl kickstart -k system\/ai\.privatae\.bastion/);
    } else {
      assert.equal(pp.serviceExe, '/usr/local/bin/privatae-bastion');
      assert.match(script, /^HOME=\/root$/m);
      assert.match(script, /^ExecStart=\/usr\/local\/bin\/privatae-bastion run$/m);
      assert.match(script, /^Environment=HOME=\/root$/m);
      assert.match(script, /systemctl restart privatae-bastion/);
      assert.ok(!/service install/.test(script.replace("emit 'step=service install'", '')), 'Linux writes the unit; `service install` does nothing there');
    }
  }
});

test('on macOS and Linux a bastion service this command did not install is refused, as on Windows', async () => {
  for (const platform of ['darwin', 'linux']) {
    const dir = require('./helpers').tmpdir();
    const pp = { ...paths(platform, {}, dir), serviceUnit: `${dir}/unit`, state: `${dir}/state.json` };
    fs.writeFileSync(pp.serviceUnit, 'someone else\'s');
    const ctx = makeContext({ platform, arch: 'x64', env: {}, paths: pp, out: captureOutput(),
      runner: { run: async () => ({ code: 0, stdout: Buffer.from('active\n'), stderr: Buffer.alloc(0) }) } });
    await assert.rejects(bastion.prepare(ctx), /already runs the bastion as a system service that this command did not install/);
    fs.rmSync(pp.serviceUnit);
    assert.equal(await bastion.osOps(ctx).serviceState(), 'missing');
  }
});

test('the binary is the one for this machine\'s CPU', () => {
  const manifest = JSON.parse(fixture('bastion-version.json'));
  assert.equal(bastion.chooseBinary(manifest, 'win32', 'x64').key, 'windows-amd64.exe');
  assert.equal(bastion.chooseBinary(manifest, 'win32', 'arm64').key, 'windows-arm64.exe');
  assert.equal(bastion.chooseBinary(manifest, 'darwin', 'arm64').key, 'darwin-arm64');
  assert.throws(() => bastion.chooseBinary({ version: '1.0.0', binaries: { 'windows-amd64.exe': { url: 'http://x.example/b', sha256: sha256Hex('x') } } }, 'win32', 'x64'), /no build for this machine/);
});

test('another account\'s run stops at the owner step: nothing is paired or enrolled, and the first account keeps its approvals', async () => {
  const other = 'S-1-5-21-1111111111-2222222222-3333333333-1002';
  const { ctx, m } = ctxFor({ bastionOwner: other });
  await assert.rejects(() => both(ctx), /belong to another Windows account/);
  assert.equal(m.bastion.owner, other, 'the recorded owner is unchanged');
  assert.equal(m.pairCalls.length, 0, 'nothing was paired');
  assert.equal(m.enrolments, 0, 'no authenticator was enrolled');
  assert.equal(m.bastion.anchor, null, 'nothing was anchored');
  assert.notEqual(ctx.state.get('bastion').owner, true);
});

test('with no SID to record as the approvals\' owner, nothing is asked for', async () => {
  const { ctx, m } = ctxFor();
  m.sid = '';
  await assert.rejects(() => both(ctx), /SID could not be read/);
  assert.equal(m.elevated.length, 0, 'no UAC prompt');
});

test('a state.json that says the Host CA changed cannot drop the live anchor: the bastion is shown the CA and refuses', async () => {
  const first = ctxFor();
  const { m } = first;
  assert.equal(await both(first.ctx), 'ran');
  // Something edits this account's state.json: the same Host, a "changed" CA.
  first.ctx.state.set('bastion', { ...first.ctx.state.get('bastion'), hostCa: 'f'.repeat(64) });
  const again = ctxFor({}, first);
  await assert.rejects(() => both(again.ctx), /could not release the anchor/);
  const release = m.bastionCommands.find((a) => a[0] === 'release-anchor');
  assert.ok(release, 'the plan asked for a release');
  assert.equal(release[release.indexOf('--host-ca') + 1], `${first.p.serviceDir}/host-ca.pem`,
    'release-anchor is shown the checked copy of the CA the Host has now');
  assert.ok(!release.includes('--force'));
  assert.deepEqual(m.releases, [], 'nothing was released');
  assert.equal(m.bastion.anchor, 'host-a', 'the live anchor stands');
  assert.equal(m.bastion.enrolled['host-a'], true, 'its factor stands');
});
