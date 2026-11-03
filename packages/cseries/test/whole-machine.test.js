'use strict';

// The whole-machine install on Linux (scope.js, vm/lima.js on a context
// whose paths name a VM account) and the uninstall that leaves nothing
// behind (cli.js uninstall, posix/elevated.js bastionUninstallScript), on the
// fake machine of fake-lima.js. Everything here is invented test data.

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { main } = require('../src/cli');
const scope = require('../src/scope');
const lima = require('../src/vm/lima');
const machine = require('../src/vm/lima/machine');
const posixElevated = require('../src/posix/elevated');
const { machinePaths } = require('../src/platform');
const { captureOutput, tmpdir, validRelease, fakeRunner } = require('./helpers');
const { fakeLima, limaPaths, machineLimaPaths } = require('./fake-lima');

function setup(opts = {}, pathsFor = machineLimaPaths) {
  const dir = tmpdir();
  const p = pathsFor(dir);
  const m = fakeLima(p, { platform: 'linux', uid: pathsFor === machineLimaPaths ? 0 : 1000, root: path.join(dir, 'machine'), ...opts });
  const rel = validRelease();
  for (const k of ['lima-x64', 'lima-arm64']) rel.guest[k] = { ...rel.guest[k], sha256: m.qcow2Sha256 };
  const run = async (argv = [], extra = {}) => {
    const out = captureOutput();
    const code = await main(argv, {
      platform: 'linux', arch: 'x64', env: {}, paths: p, out, runner: m.runner, net: m.net,
      sleep: async () => {}, prompt: async () => false, portState: async () => 'free', freeBytes: () => 64 * 1024 ** 3,
      releaseInfo: { release: rel, placeholders: [], file: 'test' }, bastionKeys: m.bastionKeys, sudoPaths: m.sudoPaths,
      runuserPaths: m.runuserPaths, hostname: 'test-machine', uid: pathsFor === machineLimaPaths ? 0 : 1000,
      ...extra,
    });
    return { code, text: out.text(), err: out.errText() };
  };
  return { m, p, run };
}

test('which install a Linux run is about: the machine\'s by default, an account\'s own when it is there or --just-me', () => {
  const c = (cmd, o = {}) => scope.chooseScope({ platform: 'linux', uid: 1000, cmd, args: [], machine: false, user: false, ...o });
  assert.equal(c(''), 'machine', 'a new install is the whole machine\'s');
  assert.equal(c('update'), 'machine');
  assert.equal(c('', { args: ['--just-me'] }), 'user');
  assert.equal(c('', { user: true }), 'user', 'an install from before keeps working where it is');
  assert.equal(c('status', { user: true }), 'user');
  assert.equal(c('status', { machine: true }), 'machine');
  assert.equal(c('uninstall', { machine: true }), 'machine');
  assert.equal(c('', { machine: true, user: true }), 'machine', 'the machine\'s install wins when both are here');
  assert.equal(c('status'), 'user', 'nothing installed: nothing to ask sudo for');
  assert.equal(c('help', { machine: true }), 'user');
  assert.equal(c('--version', { machine: true }), 'user');
  assert.equal(c('', { uid: 0, user: true }), 'machine', 'root is always the machine');
  for (const platform of ['darwin', 'win32']) {
    assert.equal(scope.chooseScope({ platform, uid: 501, cmd: '', args: [], machine: false, user: false }), 'user');
  }
});

test('the whole machine\'s paths are in /var/lib/cseries, its unit a system one, its VM an account of its own', () => {
  const p = machinePaths();
  assert.equal(p.base, '/var/lib/cseries');
  assert.equal(p.lima, '/var/lib/cseries/lima');
  assert.equal(p.vmUser, 'cseries');
  assert.equal(p.autostart, '/etc/systemd/system/cseries.service');
  for (const v of Object.values(p)) if (typeof v === 'string') assert.doesNotMatch(v, /^\/home\//, 'nothing in any account\'s home');
});

test('run as an account, a new install hands itself to sudo once, keeps only its own variables, then opens the browser as that account', async () => {
  const runner = fakeRunner((cmd, args, o) => (o.inherit ? { code: 0 } : { code: 1, notFound: true }));
  const sudoDir = tmpdir();
  const sudo = path.join(sudoDir, 'sudo');
  fs.writeFileSync(sudo, '#!/bin/sh\nexit 2\n', { mode: 0o755 });
  const out = captureOutput();
  const code = await main([], {
    platform: 'linux', arch: 'x64', uid: 1000, out, runner,
    env: { CSERIES_RELEASE_JSON: '/tmp/r.json', DISPLAY: ':0', HOME: '/home/someone', AWS_SECRET: 'not-passed' },
    paths: limaPaths(tmpdir()), installed: { machine: false, user: false }, sudoPaths: [sudo],
    net: { probe: async () => ({ status: 200, location: '' }) }, binPath: '/pkg/bin/cseries.js', nodePath: '/usr/bin/node',
    releaseInfo: { release: validRelease(), placeholders: [], file: 'test' },
  });
  assert.equal(code, 0, out.text() + out.errText());
  const call = runner.calls.find((c) => c.kind === 'inherit');
  assert.equal(call.cmd, sudo);
  assert.deepEqual(call.args, ['-p', '[cseries] sudo password for %u: ', '--', '/usr/bin/env', 'CSERIES_RELEASE_JSON=/tmp/r.json', '/usr/bin/node', '/pkg/bin/cseries.js']);
  assert.match(out.text(), /installed for the whole machine/);
  assert.match(out.text(), /--just-me installs it for your account only/);
  const browser = runner.calls.find((c) => c.kind === 'detached');
  assert.deepEqual([browser.cmd, browser.args], ['xdg-open', ['http://home.cseries.localhost:8800/setup']], 'opened here, not by root');
});

test('run as an account with its own install from before, nothing is handed to sudo', async () => {
  const runner = fakeRunner(() => ({ code: 1, notFound: true }));
  const out = captureOutput();
  await main(['status'], { platform: 'linux', arch: 'x64', uid: 1000, out, runner, env: {}, paths: limaPaths(tmpdir()), installed: { machine: false, user: true } });
  assert.equal(runner.calls.filter((c) => c.kind === 'inherit').length, 0);
});

test('whole machine, first run as root: the VM\'s own account, Lima run as it, a system unit started at boot, no login item, and what it installed recorded', async () => {
  const { m, p, run } = setup({ qemu: false });
  const r = await run();
  assert.equal(r.code, 0, r.text + r.err);
  // The account, made once, in /dev/kvm's group.
  const made = m.sudoRuns.filter((s) => s.kind === 'vm-user');
  assert.equal(made.length, 1);
  assert.equal(made[0].via, 'root', 'already root: no sudo');
  assert.match(made[0].script, /useradd --system --user-group --home-dir "\$home" --no-create-home --shell "\$nl"/);
  assert.deepEqual(m.vmUser.groups, ['cseries', 'kvm']);
  // QEMU from the packages, as root, and recorded for uninstall.
  const q = m.sudoRuns.filter((s) => s.kind === 'qemu');
  assert.equal(q.length, 1);
  assert.equal(q[0].via, 'root');
  assert.doesNotMatch(q[0].script, /usermod -a -G 'kvm' 'someone'/, 'nobody\'s own account is added to kvm');
  const st = JSON.parse(fs.readFileSync(p.state, 'utf8'));
  assert.deepEqual(st.packagesAdded, { pm: 'apt-get', packages: ['qemu-system-x86', 'qemu-utils', 'ovmf'] });
  assert.equal(st.vmUserCreated, true);
  assert.equal(st.kvmJoined, undefined);
  // Every limactl ran as the account (the fake refuses root's), with its HOME.
  assert.ok(m.asVmUser > 5);
  assert.equal(m.calls.filter((c) => c.cmd === lima.limactlPath({ paths: p })).length, 0, 'never limactl as root');
  // The system unit: as the account, at boot, LIMA_HOME and HOME set.
  const unit = fs.readFileSync(p.autostart, 'utf8');
  assert.match(unit, /^User=cseries$/m);
  assert.match(unit, /^Group=cseries$/m);
  assert.match(unit, /^WantedBy=multi-user\.target$/m);
  assert.match(unit, /^StartLimitIntervalSec=300\nStartLimitBurst=3$/m, 'a unit that keeps failing reads as failed');
  assert.match(unit, /^Restart=on-failure\nRestartSec=10$/m);
  assert.match(unit, new RegExp(`^Environment="LIMA_HOME=${p.lima}" "HOME=${p.vmHome}"$`, 'm'));
  assert.match(unit, /^ExecStart=".*limactl" --tty=false start --foreground cseries$/m);
  assert.ok(m.systemUnits.includes('start cseries.service'));
  assert.ok(m.systemUnits.includes('enable cseries.service'));
  assert.equal(m.userUnits.length, 0, 'no systemd --user unit');
  assert.ok(m.starts.every((s) => s.viaUnit), 'the VM is started by its unit, never by a limactl outside it');
  // The bastion, the machine's as before, paired with this Host.
  assert.equal(st.bastion.service, true);
  assert.equal(st.bastion.anchored, true);
  assert.match(r.text, /This command, as root, does all of it:/);
  // A second run changes nothing.
  const again = await run();
  assert.equal(again.code, 0, again.text + again.err);
  assert.equal(m.sudoRuns.filter((s) => s.kind === 'vm-user' || s.kind === 'qemu').length, 2, 'no account or package step again');
  assert.equal(m.starts.length, 1, 'a running VM is not started again');
  const status = await run(['status']);
  assert.match(status.text, /for: {7}the whole machine \(it runs as the system account cseries, started at boot by cseries\.service\)/);
});

test('whole machine: stop and start go through the unit', async () => {
  const { m, run } = setup({ qemu: true });
  assert.equal((await run()).code, 0);
  let r = await run(['stop']);
  assert.equal(r.code, 0, r.err);
  assert.ok(m.systemUnits.includes('stop cseries.service'));
  assert.equal(m.instance.status, 'Stopped');
  assert.match(r.text, /starts again when this machine starts/);
  r = await run(['start']);
  assert.equal(r.code, 0, r.err);
  assert.equal(m.instance.status, 'Running');
  assert.equal(m.starts.length, 2);
});

test('whole machine, uninstall --delete-data: the VM, its unit and account, the bastion with its anchor, pairings and state, and the folder; the packages named, not removed', async () => {
  const { m, p, run } = setup({ qemu: false });
  assert.equal((await run()).code, 0);
  const bastionState = m.stateDirs['/root/.config/privatae-bastion'];
  assert.ok(fs.existsSync(path.join(bastionState, 'anchor.json')), 'the bastion is anchored to this Host');
  fs.mkdirSync(path.join(m.stateDirs['/var/lib/privatae'], 'freeagent'), { recursive: true });
  // The bastion's marker of where its state is (singleton.go markerDirLinux).
  fs.mkdirSync(path.join(m.stateDirs['/var/lib/privatae'], 'bastion'), { recursive: true });
  fs.writeFileSync(path.join(m.stateDirs['/var/lib/privatae'], 'bastion', 'machine.json'), '{}');
  // The folder of its ask and context sockets (contextsock_unix.go), which a
  // stopped bastion leaves behind with both sockets in it.
  const sockDir = m.stateDirs['/run/privatae-bastion'];
  fs.mkdirSync(sockDir, { recursive: true });
  for (const f of ['ask.sock', 'context.sock']) fs.writeFileSync(path.join(sockDir, f), '');
  const r = await run(['uninstall', '--delete-data', '--yes']);
  assert.equal(r.code, 0, r.text + r.err);
  // The Host.
  assert.equal(m.instance, null);
  assert.ok(m.systemUnits.includes('disable --now cseries.service'));
  assert.ok(!fs.existsSync(p.autostart));
  // The bastion: its own uninstall, then its state, anchor included.
  const calls = m.bastionCalls().map((c) => c.args.join(' '));
  assert.ok(calls.includes('uninstall'), `${calls.join('; ')}\n${r.text}${r.err}`);
  assert.ok(!calls.some((c) => /unpair|release-anchor|claim-anchor --rotate|--delete-data/.test(c)), 'no anchor dance, no second question');
  assert.ok(!fs.existsSync(bastionState));
  assert.ok(!fs.existsSync(m.stateDirs['/var/lib/privatae']));
  assert.ok(!fs.existsSync(sockDir), 'the sockets folder is gone');
  assert.ok(!fs.existsSync(p.serviceUnit));
  assert.match(m.serviceLog(), /systemctl disable --now privatae-bastion\.service/);
  assert.match(r.text, /The bastion is removed: its service, program, pairings, keys and audit log\./);
  // The account and the folder.
  assert.equal(m.sudoRuns.filter((s) => s.kind === 'userdel').length, 1);
  assert.equal(m.vmUser, null);
  assert.ok(!fs.existsSync(p.base), `${p.base} is removed`);
  // The packages: named with the command, kept without a yes.
  assert.match(r.text, /C-Series installed these packages from apt-get for its VM: qemu-system-x86 qemu-utils ovmf\./);
  assert.match(r.text, /They are kept\. To remove them: apt-get purge --autoremove qemu-system-x86 qemu-utils ovmf/);
  assert.equal(m.sudoRuns.filter((s) => s.kind === 'remove-packages').length, 0);
  assert.match(r.text, /C-Series is removed from this machine\./);
});

test('uninstall --delete-data removes the packages it installed only on a typed y; an install that recorded none only names them', async () => {
  let { m, run } = setup({ qemu: false });
  assert.equal((await run()).code, 0);
  let r = await run(['uninstall', '--delete-data'], { prompt: async () => true });
  assert.equal(r.code, 0, r.text + r.err);
  const rm = m.sudoRuns.filter((s) => s.kind === 'remove-packages');
  assert.equal(rm.length, 1);
  assert.match(rm[0].script, /apt-get purge -y -q --autoremove 'qemu-system-x86' 'qemu-utils' 'ovmf'/);
  assert.match(r.text, /Removed qemu-system-x86 qemu-utils ovmf\./);

  ({ m, run } = setup({ qemu: true }));
  assert.equal((await run()).code, 0);
  r = await run(['uninstall', '--delete-data'], { prompt: async () => true });
  assert.equal(r.code, 0, r.text + r.err);
  assert.equal(m.sudoRuns.filter((s) => s.kind === 'remove-packages').length, 0, 'never what it did not install');
  assert.match(r.text, /did not record installing/);
  assert.match(r.text, /apt-get purge --autoremove qemu-system-x86 qemu-utils ovmf/);
});

test('uninstall --delete-data asks first, and a no removes nothing', async () => {
  const { m, p, run } = setup({ qemu: true });
  assert.equal((await run()).code, 0);
  const r = await run(['uninstall', '--delete-data']);
  assert.equal(r.code, 1);
  assert.match(r.text, /the bastion this command installed, with its pairings \(every Host it serves\), keys and audit log/);
  assert.match(r.text, /Nothing was removed\./);
  assert.ok(m.instance);
  assert.ok(fs.existsSync(p.serviceUnit));
});

test('an account\'s own install: uninstall --delete-data removes the bastion it installed through one sudo; --keep-bastion keeps it for the next Host', async () => {
  let { m, p, run } = setup({ qemu: false, kvm: 'denied' }, limaPaths);
  assert.equal((await run()).code, 0);
  let r = await run(['uninstall', '--delete-data', '--yes']);
  assert.equal(r.code, 0, r.text + r.err);
  const removal = m.calls.filter((c) => /sudo$/.test(c.cmd) && c.args.includes('-s') && /step=uninstall/.test(c.input || ''));
  assert.equal(removal.length, 1, 'through sudo');
  assert.ok(!fs.existsSync(p.serviceUnit));
  assert.ok(!fs.existsSync(p.state));
  assert.match(r.text, /To remove them: sudo apt-get purge --autoremove qemu-system-x86 qemu-utils ovmf/);
  assert.match(r.text, /You were added to the kvm group for the VM\. To undo it: sudo gpasswd -d someone kvm/);

  ({ m, p, run } = setup({ qemu: true }, limaPaths));
  assert.equal((await run()).code, 0);
  r = await run(['uninstall', '--delete-data', '--keep-bastion', '--yes']);
  assert.equal(r.code, 0, r.text + r.err);
  assert.ok(fs.existsSync(p.serviceUnit), 'the bastion stays');
  assert.equal(m.bastionCalls().filter((c) => c.args[0] === 'uninstall').length, 0);
  assert.ok(JSON.parse(fs.readFileSync(p.state, 'utf8')).bastion.host, 'its pairing is remembered for the next Host');
});

test('a bastion this command did not install stays, and the command that removes it is named', async () => {
  const { m, p, run } = setup({ qemu: true }, limaPaths);
  assert.equal((await run()).code, 0);
  const st = JSON.parse(fs.readFileSync(p.state, 'utf8'));
  delete st.bastion;
  fs.writeFileSync(p.state, JSON.stringify(st));
  const r = await run(['uninstall', '--delete-data', '--yes']);
  assert.equal(r.code, 0, r.text + r.err);
  assert.ok(fs.existsSync(p.serviceUnit));
  assert.equal(m.bastionCalls().filter((c) => c.args[0] === 'uninstall').length, 0);
  assert.match(r.text, /was not installed by npx cseries from this install, so it stays as it is/);
  assert.match(r.text, /sudo -H '.*privatae-bastion' uninstall --delete-data/);
});

test('a bastion that will not go is said, with the command to finish it, and the run fails', async () => {
  const { m, run } = setup({ qemu: true }, limaPaths);
  assert.equal((await run()).code, 0);
  m.beforeSudo = () => {};
  const r = await run(['uninstall', '--delete-data', '--yes'], { sudoPaths: [] });
  assert.equal(r.code, 1);
  assert.match(r.err, /The bastion was not removed: sudo is not here/);
  assert.match(r.err, /uninstall --delete-data/);
});

test('the bastion removal script: its own uninstall, then its state, as root with root\'s HOME, never its argv holding anything secret', () => {
  const s = posixElevated.bastionUninstallScript({ platform: 'linux', nonce: 'n1', program: '/usr/local/bin/privatae-bastion' });
  assert.match(s, /^HOME=\/root$/m);
  assert.match(s, /unset XDG_CONFIG_HOME/);
  assert.match(s, /text=\$\("\$exe" uninstall <\/dev\/null 2>&1\)/);
  assert.doesNotMatch(s, /uninstall --delete-data/, 'the person said yes already; the bastion is not asked to ask again');
  for (const d of ['/root/.config/privatae-bastion', '/var/lib/privatae/freeagent', '/var/lib/privatae/bastion', '/run/privatae',
    '/run/privatae-bastion']) assert.ok(s.includes(`rm -rf '${d}'`), d);
  assert.match(s, /^rmdir \/root\/\.config 2>\/dev\/null \|\| true$/m, 'root\'s .config only when the bastion\'s state was all of it');
  assert.match(s, /systemctl disable --now privatae-bastion\.service/);
  assert.match(s, /emit running=no/);
  const mac = posixElevated.bastionUninstallScript({ platform: 'darwin', nonce: 'n1', program: '/Library/Application Support/privatae/bin/privatae-bastion' });
  assert.ok(mac.includes("rm -rf '/var/root/Library/Application Support/privatae-bastion'"));
  assert.ok(mac.includes("rm -rf '/Library/Application Support/privatae/bastion'"));
  assert.match(mac, /launchctl bootout system\/ai\.privatae\.bastion/);
});

test('the VM account script refuses an account by that name it did not make', () => {
  const p = machinePaths();
  const s = lima.vmUserScript({ paths: p }, { kvm_group: 'kvm' });
  assert.match(s, /\[ "\$h" = "\$home" \] \|\| \{ echo "an account named \$u is already here/);
  assert.match(s, /usermod -a -G 'kvm' "\$u"/);
  assert.match(lima.vmUserScript({ paths: p }, { kvm_group: 'root' }), /^:$/m, 'never into root\'s group');
});

test('package removal commands, per package manager', () => {
  const f = { pm: 'dnf', qemu: '/usr/bin/qemu-system-x86_64', qemu_img: '/usr/bin/qemu-img', firmware: '/usr/share/edk2/ovmf/OVMF_CODE.fd' };
  assert.equal(machine.removalFor(null, f, 'x64').command, 'dnf remove qemu-system-x86-core qemu-img edk2-ovmf');
  assert.equal(machine.removalFor({ pm: 'pacman', packages: ['qemu-system-x86'] }, f, 'x64').command, 'pacman -Rns qemu-system-x86');
  assert.equal(machine.removalFor({ pm: 'zypper', packages: ['qemu-x86'] }, f, 'x64').command, 'zypper remove --clean-deps qemu-x86');
  assert.equal(machine.removalFor(null, { pm: 'apt-get' }, 'x64'), null, 'nothing of QEMU here: nothing said');
});

test('--just-me as root is refused before anything is installed', async () => {
  const { m, run } = setup({ qemu: true });
  const r = await run(['--just-me']);
  assert.equal(r.code, 1);
  assert.match(r.err, /--just-me installs C-Series for one account, and this run is root/);
  assert.equal(m.sudoRuns.length, 0);
});

test('the root run never opens a browser; npx cseries open on the whole machine\'s install opens it as the account', async () => {
  // As root, with the DISPLAY a sudo may keep: the URL is shown, no browser.
  const { m, run } = setup({ qemu: true });
  const first = await run([], { env: { DISPLAY: ':0', XAUTHORITY: '/home/someone/.Xauthority' } });
  assert.equal(first.code, 0, first.text + first.err);
  const opened = await run(['open'], { env: { DISPLAY: ':0' } });
  assert.equal(opened.code, 0, opened.text + opened.err);
  assert.equal(m.runner.calls.filter((c) => c.kind === 'detached').length, 0, 'no browser started by root');
  // As the account: open hands itself to sudo, then opens the browser here.
  const runner = fakeRunner((cmd, args, o) => (o.inherit ? { code: 0 } : { code: 1, notFound: true }));
  const sudoDir = tmpdir();
  const sudo = path.join(sudoDir, 'sudo');
  fs.writeFileSync(sudo, '#!/bin/sh\nexit 2\n', { mode: 0o755 });
  const code = await main(['open'], {
    platform: 'linux', arch: 'x64', uid: 1000, out: captureOutput(), runner, env: { DISPLAY: ':0' },
    paths: limaPaths(tmpdir()), installed: { machine: true, user: false }, sudoPaths: [sudo],
    net: { probe: async () => ({ status: 200, location: '' }) }, binPath: '/pkg/bin/cseries.js', nodePath: '/usr/bin/node',
  });
  assert.equal(code, 0);
  assert.deepEqual(runner.calls.find((c) => c.kind === 'inherit').args.slice(-2), ['/pkg/bin/cseries.js', 'open']);
  const browser = runner.calls.find((c) => c.kind === 'detached');
  assert.ok(browser, 'the account opens the browser');
  assert.equal(browser.cmd, 'xdg-open');
});

test('the fake machine never runs a script that could reach this computer\'s own root folders or tools', async () => {
  const { m } = setup({ qemu: true });
  const sh = (input) => m.runner.run('/bin/sh', ['-s'], { input });
  const PATHLINE = 'PATH=/usr/sbin:/usr/bin:/sbin:/bin';
  await assert.rejects(sh(`${PATHLINE}\nHOME=/root\nHOME=/root\necho "$HOME"\n`), /still names \/root/, 'a second HOME=/root');
  await assert.rejects(sh(`${PATHLINE}\nHOME=/root\ncd /root && pwd\n`), /still names \/root/, 'root\'s home, as a whole word');
  await assert.rejects(sh('HOME=/root\necho no PATH line\n'), /sets no PATH/, 'without its PATH line it would find the real systemctl');
  await assert.rejects(sh(`${PATHLINE}\nHOME=/root\n/bin/systemctl --version\n`), /absolute path/, 'systemctl by its absolute path');
  // The fake bastion writes only under the HOME its test made for it.
  const dir = tmpdir();
  const r = require('node:child_process').spawnSync(process.execPath, [path.join(__dirname, 'fake-bastion.js'), 'uninstall'], {
    env: { ...process.env, HOME: dir, FAKE_BASTION_LOG: path.join(dir, 'log'), FAKE_BASTION_HOME: '' },
  });
  assert.equal(r.status, 97, r.stderr.toString());
  assert.deepEqual(fs.readdirSync(dir), [], 'nothing written');
});

test('the account sudo ran the whole machine\'s install for is recorded as the bastion\'s owner, so a model server it runs is trusted', async () => {
  let { m, p, run } = setup({ qemu: true, bastionVersion: '0.62.0' });
  let r = await run([], { env: { SUDO_USER: 'someone' } });
  assert.equal(r.code, 0, r.text + r.err);
  const script = m.sudoRuns.filter((s) => s.kind === 'bastion').map((s) => s.script).join('\n');
  assert.match(script, /bastion 20 'approvals-owner' 'approvals-owner' '--account' 'someone'/);
  assert.equal(m.bastionState().owner, 'someone');
  assert.equal(JSON.parse(fs.readFileSync(p.state, 'utf8')).bastion.owner, true);
  assert.match(r.text, /record someone as this machine's owner/);
  // Recorded once: the next run has nothing to do for it.
  const runs = m.sudoRuns.length;
  r = await run([], { env: { SUDO_USER: 'someone' } });
  assert.equal(r.code, 0, r.text + r.err);
  assert.equal(m.sudoRuns.length, runs, 'no elevated run again');

  // A bastion older than 0.62 has no --account; root with no SUDO_USER has
  // no one to record.
  for (const [opts, env] of [[{ bastionVersion: '0.53.2' }, { SUDO_USER: 'someone' }], [{ bastionVersion: '0.62.0' }, {}], [{ bastionVersion: '0.62.0' }, { SUDO_USER: 'root' }]]) {
    ({ m, run } = setup({ qemu: true, ...opts }));
    r = await run([], { env });
    assert.equal(r.code, 0, r.text + r.err);
    assert.doesNotMatch(m.sudoRuns.map((s) => s.script).join('\n'), /approvals-owner/, JSON.stringify([opts, env]));
  }
});

test('an account\'s own install records that account as the bastion\'s owner', async () => {
  const { m, run } = setup({ qemu: true, bastionVersion: '0.62.0' }, limaPaths);
  const r = await run([], { accountName: 'someone' });
  assert.equal(r.code, 0, r.text + r.err);
  assert.equal(m.bastionState().owner, 'someone');
});

// scope.js reads the real /var/lib/cseries and /etc/systemd/system: here the
// fake machine's folders stand in for them.
function machineFs(p) {
  const at = (f) => f.replace(platformConsts.MACHINE_UNIT, p.autostart).replace(platformConsts.MACHINE_BASE, p.base);
  return { existsSync: (f) => fs.existsSync(at(f)), readFileSync: (f, e) => fs.readFileSync(at(f), e) };
}
const platformConsts = require('../src/platform');

// The download of the guest's image fails once: after the VM's account,
// Lima and the record of both are made, before the VM is.
function failingNet(m) {
  let failed = false;
  return {
    ...m.net,
    async download(url, dest, o) {
      if (!failed && url.endsWith('.qcow2')) {
        failed = true;
        const { CliError } = require('../src/errors');
        throw new CliError('Could not download: the network went away (test).', '');
      }
      return m.net.download(url, dest, o);
    },
  };
}

test('a whole-machine install that stops after its record is written is no install: every reader says so, and npx cseries finishes it', async () => {
  const { m, p, run } = setup({ qemu: true });
  const r = await run([], { net: failingNet(m) });
  assert.equal(r.code, 1, r.text + r.err);
  const st = JSON.parse(fs.readFileSync(p.state, 'utf8'));
  assert.equal(st.unfinished, true, 'marked unfinished');
  assert.equal(st.vmUserCreated, true, 'what it made is still recorded, so its removal finds it');
  assert.ok(fs.existsSync(p.lima), 'lima/ is here, and is not an install');
  // The account's side (scope.js): not installed, and unfinished.
  assert.equal(scope.machineInstalled(machineFs(p)), false);
  assert.equal(scope.machineUnfinished(machineFs(p)), true);
  // The root run's own readers.
  const s = await run(['status']);
  assert.equal(s.code, 0);
  assert.match(s.text, /Linux system \(Lima\): not installed\n {2}An install of C-Series for the whole machine started on this machine and did not finish, so no Host runs from it\.\n {2}To finish it: npx cseries\n {2}To remove what it left: npx cseries uninstall --delete-data --whole-machine/);
  for (const cmd of ['start', 'stop', 'open', 'logs']) {
    const x = await run([cmd]);
    assert.equal(x.code, 1, cmd);
    assert.match(x.err, /did not finish, so no Host runs from it\./, cmd);
  }
  // npx cseries again finishes it, and then it is an install.
  const again = await run();
  assert.equal(again.code, 0, again.text + again.err);
  assert.match(again.text, /An install of C-Series for the whole machine started here before and did not finish\. This run finishes it\./);
  assert.equal(JSON.parse(fs.readFileSync(p.state, 'utf8')).unfinished, undefined);
  assert.equal(scope.machineInstalled(machineFs(p)), true);
  assert.equal(scope.machineUnfinished(machineFs(p)), false);
  const s2 = await run(['status']);
  assert.match(s2.text, /Linux system \(Lima\): installed/);
});

test('an unfinished whole-machine install is removed by uninstall --delete-data; a finished one that fails an update stays an install', async () => {
  const { m, p, run } = setup({ qemu: true });
  assert.equal((await run([], { net: failingNet(m) })).code, 1);
  const u = await run(['uninstall', '--delete-data', '--yes', '--whole-machine']);
  assert.equal(u.code, 0, u.text + u.err);
  assert.ok(!fs.existsSync(p.base), 'nothing of it left');
  assert.equal(m.vmUser, null, 'the account it made is removed');
  assert.equal(scope.machineInstalled(machineFs(p)), false);
  assert.equal(scope.machineUnfinished(machineFs(p)), false);
  // Stopped once its VM was made: the VM is here, and it is still no install.
  const z = setup({ qemu: true });
  const noStart = fakeRunner((cmd, args, o) => (cmd === 'systemctl' && args.join(' ') === 'start cseries.service'
    ? { code: 1, stderr: 'Job for cseries.service failed (test).' } : z.m.runner.run(cmd, args, o)));
  assert.equal((await z.run([], { runner: noStart })).code, 1);
  assert.ok(z.m.instance, 'the VM was made');
  assert.equal(scope.machineInstalled(machineFs(z.p)), false);
  const zs = await z.run(['status']);
  assert.match(zs.text, /Linux system \(Lima\): not installed\n {2}An install of C-Series for the whole machine started on this machine and did not finish/);
  assert.doesNotMatch(zs.text, /running:/);
  // A finished install whose update stops is still one.
  const y = setup({ qemu: true });
  assert.equal((await y.run()).code, 0);
  y.m.instance.status = 'Stopped';
  const failStart = fakeRunner((cmd, args, o) => (cmd === 'systemctl' && args.join(' ') === 'start cseries.service'
    ? { code: 1, stderr: 'Job for cseries.service failed (test).' } : y.m.runner.run(cmd, args, o)));
  const up = await y.run(['update'], { runner: failStart });
  assert.equal(up.code, 1, up.text + up.err);
  assert.equal(JSON.parse(fs.readFileSync(y.p.state, 'utf8')).unfinished, undefined);
  assert.equal(scope.machineInstalled(machineFs(y.p)), true);
});

test('run as an account, an unfinished whole-machine install: uninstall goes to sudo, npx cseries finishes it, --just-me and the account\'s own install go on, nothing else uses it', async () => {
  const sudoDir = tmpdir();
  const sudo = path.join(sudoDir, 'sudo');
  fs.writeFileSync(sudo, '#!/bin/sh\nexit 2\n', { mode: 0o755 });
  const go = async (argv, installed, extra = {}) => {
    const runner = fakeRunner((cmd, args, o) => (o.inherit ? { code: 0 } : { code: 1, notFound: true }));
    const out = captureOutput();
    const asked = [];
    const code = await main(argv, {
      platform: 'linux', arch: 'x64', uid: 1000, out, runner, env: {}, paths: limaPaths(tmpdir()), installed, sudoPaths: [sudo],
      net: { probe: async () => null }, binPath: '/pkg/bin/cseries.js', nodePath: '/usr/bin/node', isTTY: true,
      prompt: async (q) => { asked.push(q); return true; }, releaseInfo: { release: validRelease(), placeholders: [], file: 'test' }, ...extra,
    });
    const sudoArgs = runner.calls.filter((c) => c.kind === 'inherit').map((c) => c.args.slice(c.args.indexOf('/pkg/bin/cseries.js') + 1));
    return { code, text: out.text(), err: out.errText(), sudoArgs, asked };
  };
  const only = { machine: false, user: false, unfinished: true };
  const both = { machine: false, user: true, unfinished: true };
  // Uninstall: the machine's, through sudo.
  let r = await go(['uninstall', '--delete-data'], only);
  assert.deepEqual(r.sudoArgs, [['uninstall', '--delete-data']]);
  r = await go(['uninstall', '--delete-data', '--whole-machine'], both);
  assert.deepEqual(r.sudoArgs, [['uninstall', '--delete-data']], 'with the account\'s own here, when asked for');
  r = await go(['uninstall', '--delete-data'], both);
  assert.deepEqual(r.sudoArgs, [], 'the account\'s own otherwise');
  // npx cseries, nothing of the account's: the run as root finishes it.
  r = await go([], only);
  assert.deepEqual(r.sudoArgs, [[]]);
  // status says what it is, without sudo; start and the rest refuse.
  r = await go(['status'], only);
  assert.equal(r.code, 0);
  assert.deepEqual(r.sudoArgs, []);
  assert.match(r.text, /did not finish, so no Host runs from it\.\nTo finish it: npx cseries\nTo remove what it left: npx cseries uninstall --delete-data --whole-machine/);
  r = await go(['start'], only);
  assert.equal(r.code, 1);
  assert.deepEqual(r.sudoArgs, []);
  assert.match(r.err, /did not finish/);
  // --just-me: the account's own install, told what is left and how to remove it.
  r = await go(['--just-me'], only);
  assert.deepEqual(r.sudoArgs, []);
  assert.match(r.text, /did not finish, so no Host runs from it\.\nTo finish it: npx cseries\nTo remove what it left: npx cseries uninstall --delete-data --whole-machine/);
  // The account's own install: it goes on, the move is not offered onto it.
  r = await go([], both);
  assert.deepEqual(r.sudoArgs, []);
  assert.deepEqual(r.asked, [], 'no move offered');
  assert.match(r.text, /did not finish, so no Host runs from it\.\nTo remove what it left: npx cseries uninstall --delete-data --whole-machine/);
  // Before: a finished whole-machine install is still the machine's.
  r = await go(['status'], { machine: true, user: false, unfinished: false });
  assert.deepEqual(r.sudoArgs, [['status']]);
});
