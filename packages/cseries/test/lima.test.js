'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { main } = require('../src/cli');
const lima = require('../src/vm/lima');
const machine = require('../src/vm/lima/machine');
const { limaTemplate } = require('../src/vm/lima/template');
const { guestResources } = require('../src/vm/common');
const { vmLimits } = require('../src/vm/wslconfig');
const host = require('../src/host');
const { selectDriver } = require('../src/vm/driver');
const { paths, machineArch } = require('../src/platform');
const { captureOutput, tmpdir, validRelease } = require('./helpers');
const { fakeLima, limaPaths } = require('./fake-lima');

const GiB = 1024 ** 3;

function releaseFor(m) {
  const r = validRelease();
  for (const k of ['lima-x64', 'lima-arm64']) r.guest[k] = { ...r.guest[k], sha256: m.qcow2Sha256 };
  return r;
}

function run(argv, m, p, extra = {}) {
  const out = captureOutput();
  const code = main(argv, {
    platform: m.platform,
    arch: 'x64',
    env: {},
    paths: p,
    out,
    runner: m.runner,
    net: m.net,
    sleep: async () => {},
    prompt: async () => false,
    portState: async () => 'free', freeBytes: () => 64 * 1024 ** 3,
    releaseInfo: { release: releaseFor(m), placeholders: [], file: 'test' },
    bastionKeys: m.bastionKeys,
    sudoPaths: m.sudoPaths,
    ...extra,
  });
  return code.then((c) => ({ code: c, out }));
}

function p0() {
  return tmpdir();
}

function machineFor(platform, opts = {}) {
  const p = limaPaths(tmpdir(), platform);
  const m = fakeLima(p, { platform, ...opts });
  return { m, p };
}

test('macOS and Linux get the Lima driver, built and supported', () => {
  for (const platform of ['darwin', 'linux']) {
    const d = selectDriver(platform);
    assert.equal(d.name, 'lima');
    assert.equal(d.supported, true);
  }
});

test('the template forwards 8800, 8810 and 8811 on loopback, ignores every other port, and shares nothing', () => {
  const y = limaTemplate({ vmType: 'vz', arch: 'arm64', image: '/Users/some one/x "y".qcow2', sha256: 'a'.repeat(64), cpus: 4, memoryGiB: 8 });
  assert.match(y, /^vmType: "vz"$/m);
  assert.match(y, /^arch: "aarch64"$/m);
  assert.match(y, /^- location: "\/Users\/some one\/x \\"y\\"\.qcow2"$/m);
  assert.match(y, /^ {2}digest: "sha256:a{64}"$/m);
  assert.match(y, /^cpus: 4$/m);
  assert.match(y, /^memory: "8GiB"$/m);
  assert.match(y, /^mounts: \[\]$/m);
  assert.match(y, /^ {2}forwardAgent: false$/m);
  const rules = y.split('portForwards:\n')[1].split('\nuser:')[0];
  for (const port of [8800, 8810, 8811]) {
    assert.match(rules, new RegExp(`- guestIP: "127\\.0\\.0\\.1"\\n {2}guestPort: ${port}\\n {2}hostIP: "127\\.0\\.0\\.1"\\n {2}hostPort: ${port}`));
  }
  // The catch-all comes last (Lima takes the first rule that matches), and
  // matches every address the guest binds, 127.0.0.1 included.
  assert.match(rules, /- guestIP: "0\.0\.0\.0"\n {2}guestIPMustBeZero: false\n {2}guestPortRange: \[1, 65535\]\n {2}proto: "any"\n {2}ignore: true$/);
  assert.equal((rules.match(/hostIP/g) || []).length, 3, 'nothing else is forwarded');
  assert.match(y, /^ {2}uid: 2000$/m, 'Lima\'s user is not uid 1000, which the containers run as');
  assert.throws(() => limaTemplate({ vmType: 'qemu', arch: 'ia32', image: '/x', sha256: 'a'.repeat(64), cpus: 1, memoryGiB: 1 }), /no Lima architecture/);
  assert.throws(() => limaTemplate({ vmType: 'qemu', arch: 'x64', image: '/x', sha256: 'abc', cpus: 1, memoryGiB: 1 }), /sha256/);
});

test('the VM is sized as WSL sizes it on Windows: half the memory, every processor', () => {
  assert.deepEqual(guestResources(16 * GiB, 8), { memoryGiB: 8, cpus: 8, totalGiB: 16 });
  assert.deepEqual(guestResources(64 * GiB, 16), { memoryGiB: 32, cpus: 16, totalGiB: 64 });
  // A Linux machine's MemTotal sits under its nominal size: 15.6 GiB is half
  // to the nearest GB, 8, as .wslconfig gets it on a PC of the same memory.
  assert.deepEqual(guestResources(15.6 * GiB, 12), { memoryGiB: 8, cpus: 12, totalGiB: 15 });
  assert.deepEqual(guestResources(6 * GiB, 2), { memoryGiB: 3, cpus: 2, totalGiB: 6 });
  assert.deepEqual(guestResources(1 * GiB, 0), { memoryGiB: 1, cpus: 1, totalGiB: 1 }, 'never more than the machine has');
  for (const [bytes, cpus] of [[15.6 * GiB, 12], [7.7 * GiB, 4], [31.3 * GiB, 16], [3 * GiB, 2], [128 * GiB, 64]]) {
    const w = vmLimits(bytes, cpus);
    const l = guestResources(bytes, cpus);
    assert.equal(`${l.memoryGiB}GB`, w.memory, `${bytes / GiB} GiB`);
    assert.equal(String(l.cpus), w.processors);
  }
});

test('the Lima VM is made from this run\'s machine, as WSL\'s limits are', async () => {
  const { m, p } = machineFor('linux', { qemu: true });
  const { code, out } = await run([], m, p, { machine: { totalMem: 15.6 * GiB, cpus: 6 } });
  assert.equal(code, 0, out.text() + out.errText());
  assert.match(m.template, /^cpus: 6$/m);
  assert.match(m.template, /^memory: "8GiB"$/m);
});

test('the Lima guest gets the Host\'s processor and memory ceilings from its own status, through limactl shell', async () => {
  // Invented figures: a VM of 6 processors and 7.75 GiB.
  const { m, p } = machineFor('linux', { qemu: true, cpus: 6, memKb: 8126464 });
  const { code, out } = await run([], m, p);
  assert.equal(code, 0, out.text() + out.errText());
  assert.match(m.images, /^cpu_quota=500%$/m);
  assert.match(m.images, new RegExp(`^memory_max=${host.memoryMax('8126464')}$`, 'm'));
  assert.match(m.images, /^memory_max=[1-9][0-9]*M$/m);
  assert.match(out.text(), /The Host may use 5 of this Linux system's 6 processors/);
});

test('a sudo that passes XDG_CONFIG_HOME on does not move the bastion\'s state, or the authenticator\'s secret, out of root\'s', async () => {
  const { m, p } = machineFor('linux', { qemu: true, sudoEnv: { XDG_CONFIG_HOME: path.join(p0(), 'someone-config') } });
  const { code, out } = await run([], m, p);
  assert.equal(code, 0, out.text() + out.errText());
  const calls = m.bastionCalls();
  assert.deepEqual(calls.map((c) => c.args[0]), ['set-mode', 'pair', 'claim-anchor', 'stepup-enroll']);
  for (const c of calls) {
    assert.equal(c.xdgConfigHome, null, `${c.args[0]} ran with XDG_CONFIG_HOME set`);
    assert.equal(c.home, m.bastionHome());
  }
});

test('a download swapped for a link while sudo waits is never readable where root copied it, and is refused', async () => {
  const { m, p } = machineFor('linux', { qemu: true });
  const secret = path.join(p.base, 'root-only-file');
  fs.writeFileSync(secret, 'INVENTED-ROOT-ONLY-CONTENT\n');
  m.beforeSudo = (script) => {
    const src = /install -o \S+ -g \S+ -m \d+ '([^']+)'/.exec(script)[1];
    fs.rmSync(src);
    fs.symlinkSync(secret, src);
  };
  const { code, out } = await run([], m, p);
  assert.equal(code, 1);
  assert.match(out.errText(), /not the verified download/);
  assert.deepEqual(m.installModes(), ['700'], 'root\'s copy is root\'s alone until its hash is checked');
  assert.ok(!fs.existsSync(p.serviceExe));
  assert.ok(!fs.existsSync(`${p.serviceExe}.new`));
  assert.deepEqual(m.bastionCalls(), [], 'nothing ran');
});

test('on an Apple silicon Mac an x64 Node under Rosetta still gets the arm64 VM', () => {
  assert.equal(machineArch('darwin', {}, 'x64', () => true), 'arm64');
  assert.equal(machineArch('darwin', {}, 'x64', () => false), 'x64');
  assert.equal(machineArch('darwin', {}, 'arm64', () => { throw new Error('not asked'); }), 'arm64');
  assert.equal(machineArch('linux', {}, 'x64', () => { throw new Error('not asked'); }), 'x64');
});

test('macOS paths keep Lima out of Application Support, under the socket length limit', () => {
  const mac = paths('darwin', {}, '/Users/someone');
  assert.equal(mac.lima, '/Users/someone/.cseries/lima');
  assert.equal(mac.autostart, '/Users/someone/Library/LaunchAgents/ai.privatae.cseries.plist');
  assert.equal(mac.serviceUnit, '/Library/LaunchDaemons/ai.privatae.bastion.plist');
  assert.ok(`${mac.lima}/cseries/ssh.sock`.length < 104);
  const lin = paths('linux', { XDG_CONFIG_HOME: '/home/someone/.cfg' }, '/home/someone');
  assert.equal(lin.lima, '/home/someone/.local/share/cseries/lima');
  assert.equal(lin.autostart, '/home/someone/.cfg/systemd/user/cseries.service');
  assert.equal(lin.serviceUnit, '/etc/systemd/system/privatae-bastion.service');
});

test('the probe script runs under /bin/sh here and answers every key', async () => {
  const { systemRunner } = require('../src/run');
  const f = await machine.probe({ runner: systemRunner(), platform: 'linux', arch: 'x64' });
  for (const k of ['uid', 'user', 'qemu', 'qemu_img', 'ssh', 'sg', 'firmware', 'kvm', 'kvm_group', 'kvm_member', 'systemd']) {
    assert.ok(k in f, `${k} is answered`);
  }
  assert.match(f.kvm, /^(ok|denied|missing)$/);
  assert.match(f.uid, /^\d+$/);
});

test('/dev/kvm: open, reachable through sg, joined with sudo, or refused', () => {
  assert.equal(machine.kvmAccess({ kvm: 'ok' }), 'ok');
  assert.equal(machine.kvmAccess({ kvm: 'missing' }), 'missing');
  assert.equal(machine.kvmAccess({ kvm: 'denied', kvm_member: 'yes', sg: '/usr/bin/sg' }), 'sg');
  assert.equal(machine.kvmAccess({ kvm: 'denied', kvm_member: 'no', kvm_group: 'kvm' }), 'join');
  assert.equal(machine.kvmAccess({ kvm: 'denied', kvm_member: 'no', kvm_group: 'root' }), 'refuse', 'never adds anyone to root');
});

test('QEMU\'s packages per distribution, and the one script sudo runs', () => {
  const f = { pm: 'apt-get', kvm_group: 'kvm', user: 'some one' };
  assert.deepEqual(machine.packagesFor(f, 'x64', ['qemu', 'qemu_img', 'firmware', 'ssh']), ['qemu-system-x86', 'qemu-utils', 'ovmf', 'openssh-client']);
  assert.deepEqual(machine.packagesFor({ pm: 'dnf' }, 'arm64', ['qemu', 'firmware']), ['qemu-system-aarch64-core', 'edk2-aarch64']);
  assert.equal(machine.packagesFor({ pm: 'apk' }, 'x64', ['qemu']), null);
  const s = machine.installScript(f, 'x64', { packages: ['qemu-system-x86', 'ovmf'], join: true });
  assert.match(s, /^PATH=\/usr\/sbin:\/usr\/bin:\/sbin:\/bin$/m);
  assert.match(s, /apt-get install -y -q --no-install-recommends 'qemu-system-x86' 'ovmf' <\/dev\/null/);
  assert.match(s, /^usermod -a -G 'kvm' 'some one'$/m);
});

test('Linux, first run: one sudo for QEMU and kvm, Lima unpacked, the VM made and started, the Host up, one sudo for the bastion', async () => {
  const { m, p } = machineFor('linux', { qemu: false, kvm: 'denied', kvmMember: false });
  const { code, out } = await run([], m, p);
  assert.equal(code, 0, out.text() + out.errText());

  const qemu = m.sudoRuns.filter((r) => r.kind === 'qemu');
  assert.equal(qemu.length, 1);
  assert.match(qemu[0].script, /'qemu-system-x86' 'qemu-utils' 'ovmf'/);
  assert.match(qemu[0].script, /usermod -a -G 'kvm' 'someone'/);
  assert.ok(fs.existsSync(lima.limactlPath({ paths: p })));
  assert.match(m.template, /^vmType: "qemu"$/m);
  assert.ok(!fs.existsSync(path.join(p.downloads, 'cseries-guest-1.2.3-x64.qcow2')), 'Lima keeps its own copy; the download is removed');
  assert.deepEqual(m.starts, [{ viaSg: true }], 'started once, through sg kvm, since this session predates the group');
  assert.match(m.images, /^host=ghcr\.io\/privatae\/cseries:1\.2\.3@sha256:a{64}$/m);
  assert.equal(m.pulls, 1);

  // The bastion: one sudo, its script on stdin, the token nowhere in argv.
  const sudo = m.calls.filter((c) => /sudo$/.test(c.cmd) && c.args.includes('-s'));
  assert.equal(sudo.length, 1);
  assert.ok(!sudo[0].args.join(' ').includes('tok-'), 'the pairing token is not in sudo\'s command line');
  assert.deepEqual(m.bastionCalls().map((c) => c.args[0]), ['set-mode', 'pair', 'claim-anchor', 'stepup-enroll']);
  assert.ok(m.systemd.unitWritten && m.systemd.enabled && m.systemd.active, 'the systemd unit is written, enabled and (re)started');
  const st = m.bastionState();
  assert.equal(st.mode, 'networked');
  assert.equal(st.anchor, 'host-a');
  assert.equal(st.redeem, 'https://127.0.0.1:8810/v1/local/redeem');
  assert.ok(st.enrolled['host-a']);
  const enrol = m.bastionCalls().find((c) => c.args[0] === 'stepup-enroll');
  // --console-only as on the other OSes: 0.53.2 takes it as root on Linux.
  assert.deepEqual(enrol.args, ['stepup-enroll', '--console-only', '--host', 'host-a']);
  const pair = m.bastionCalls().find((c) => c.args[0] === 'pair');
  assert.equal(pair.args.at(-1), '-', 'pair is told to read the token from standard input');
  assert.equal(pair.stdinTTY, false);
  assert.equal(st.token, 'tok-1', 'pair read the Host\'s token on its standard input');
  assert.equal(enrol.home, m.bastionHome(), 'enrolled into the state the service reads');
  assert.deepEqual(m.installModes(), ['700']);
  assert.equal(fs.statSync(p.serviceExe).mode & 0o777, 0o755, 'the verified program is made runnable by all');
  // The secret went to the terminal, not to the captured output or the log.
  assert.match(m.ttyOut(), /FAKESECRETNOTREAL/);
  assert.ok(!fs.readFileSync(p.installLog, 'utf8').includes('FAKESECRETNOTREAL'));
  assert.ok(!out.text().includes('FAKESECRETNOTREAL'));

  const state = JSON.parse(fs.readFileSync(p.state, 'utf8'));
  assert.deepEqual(Object.keys(state.bastion).sort(), ['anchored', 'enrolled', 'host', 'hostCa', 'service']);
  assert.equal(state.bastion.owner, undefined, 'no approvals owner off Windows');
  assert.match(out.text(), /sudo -H '.*privatae-bastion' approvals/);
  assert.deepEqual(m.userUnits, ['daemon-reload', 'enable cseries.service']);
  assert.match(fs.readFileSync(p.autostart, 'utf8'), new RegExp(`Environment="LIMA_HOME=${p.lima}"`));

  // The second run asks for nothing and changes nothing.
  const sudoBefore = m.calls.filter((c) => /sudo$/.test(c.cmd)).length;
  const again = await run([], m, p);
  assert.equal(again.code, 0, again.out.text() + again.out.errText());
  assert.equal(m.calls.filter((c) => /sudo$/.test(c.cmd)).length, sudoBefore, 'no sudo on a second run');
  assert.equal(m.starts.length, 1, 'a running VM is not started again');
  assert.equal(m.bastionCalls().length, 4);
  assert.match(again.out.text(), /Lima 2\.2\.1 is installed/);
});

test('macOS, first run: no QEMU, vz, the bastion as launchd\'s daemon, and a LaunchAgent that carries LIMA_HOME', async () => {
  const { m, p } = machineFor('darwin');
  const { code, out } = await run([], m, p);
  assert.equal(code, 0, out.text() + out.errText());
  assert.equal(m.sudoRuns.filter((r) => r.kind === 'qemu').length, 0);
  assert.match(m.template, /^vmType: "vz"$/m);
  assert.deepEqual(m.starts, [{ viaSg: false }]);
  assert.deepEqual(m.bastionCalls().map((c) => c.args[0]), ['service', 'set-mode', 'pair', 'claim-anchor', 'stepup-enroll']);
  assert.match(m.serviceLog(), /launchctl kickstart -k system\/ai\.privatae\.bastion/);
  assert.deepEqual(m.bastionCalls().find((c) => c.args[0] === 'stepup-enroll').args, ['stepup-enroll', '--console-only', '--host', 'host-a']);
  assert.ok(fs.existsSync(p.serviceExe), 'the program is in its folder, with a space in the path');
  assert.equal(fs.readFileSync(p.serviceExe, 'utf8'), m.program.toString());
  const plist = fs.readFileSync(p.autostart, 'utf8');
  assert.match(plist, /<key>LIMA_HOME<\/key>\s*<string>[^<]*\/lima<\/string>/);
  assert.match(plist, /<string>--foreground<\/string>/);
  // One password: the install script's sudo asks; `sudo -n true` asks
  // nothing, and says the credential cache lets the enrolment's own sudo run
  // through without asking again. A second run calls no sudo at all.
  const kinds = () => m.calls.filter((c) => /sudo$/.test(c.cmd))
    .map((c) => (c.args[0] === '-n' ? 'cache-check' : c.args.includes('-s') ? 'script' : 'enrol'));
  assert.deepEqual(kinds(), ['script', 'cache-check', 'enrol']);
  assert.doesNotMatch(out.text(), /sudo asks for your password once more/);
  const again = await run([], m, p);
  assert.equal(again.code, 0, again.out.text() + again.out.errText());
  assert.deepEqual(kinds(), ['script', 'cache-check', 'enrol'], 'no sudo on the second run');
});

test('on macOS and Linux the line that makes the VM names no WSL or Windows', async () => {
  for (const platform of ['linux', 'darwin']) {
    const { m, p } = machineFor(platform, platform === 'linux' ? { qemu: false, kvm: 'denied', kvmMember: false } : {});
    const { code, out } = await run([], m, p);
    assert.equal(code, 0, out.text() + out.errText());
    const line = out.text().split('\n').find((l) => l.includes('Creating the VM'));
    assert.ok(line, `${platform}: the VM's sizing is said`);
    assert.match(line, /\d+ GB of memory and \d+ processors at most/);
    assert.doesNotMatch(line, /WSL|Windows/, `${platform}: ${line.trim()}`);
  }
});

test('sudo refused: the Host still runs, the bastion step fails softly and says how to finish', async () => {
  const { m, p } = machineFor('darwin', { sudo: 'no' });
  const { code, out } = await run([], m, p);
  assert.equal(code, 1);
  assert.match(out.errText(), /sudo did not run it \(sudo: 3 incorrect password attempts\)/);
  assert.match(out.text(), /C-Series is running/);
  assert.deepEqual(JSON.parse(fs.readFileSync(p.state, 'utf8')).bastion, {}, 'nothing recorded as done');
  assert.equal(m.bastionCalls().length, 0);
});

test('a run that stops half way is finished by the next, asking only for what is left', async () => {
  const { m, p } = machineFor('linux', { qemu: true, failAt: 'claim-anchor' });
  const first = await run([], m, p);
  assert.equal(first.code, 1);
  assert.match(first.out.errText(), /could not anchor itself to the Host/);
  let st = JSON.parse(fs.readFileSync(p.state, 'utf8')).bastion;
  assert.equal(st.host, 'host-a');
  assert.equal(st.anchored, false);
  // The next run, with the fault gone, on the same machine: the bastion's
  // state is where the first run left it, and its service is stopped.
  const m2 = fakeLima(p, { platform: 'linux', qemu: true });
  Object.assign(m2, { instance: m.instance, images: m.images, hostStarted: true });
  fs.rmSync(m.bastionLog);
  const second = await run([], m2, p);
  assert.equal(second.code, 0, second.out.text() + second.out.errText());
  assert.deepEqual(m2.bastionCalls().map((c) => c.args[0]), ['set-mode', 'claim-anchor', 'stepup-enroll'], 'not installed or paired again');
  assert.match(m2.serviceLog(), /systemctl restart privatae-bastion/);
  st = JSON.parse(fs.readFileSync(p.state, 'utf8')).bastion;
  assert.equal(st.anchored, true);
  assert.equal(st.enrolled, true);
});

test('root is refused before anything is installed', async () => {
  const { m, p } = machineFor('linux', { uid: 0 });
  const { code, out } = await run([], m, p);
  assert.equal(code, 1);
  assert.match(out.errText(), /as yourself, not as root or with sudo/);
  assert.equal(m.calls.filter((c) => c.cmd === 'tar' || /sudo$/.test(c.cmd)).length, 0);
});

test('an old macOS and a Linux without /dev/kvm are told why', async () => {
  let { m, p } = machineFor('darwin', { macos: '12.7.4' });
  let r = await run([], m, p);
  assert.match(r.out.errText(), /needs macOS 13\.5 or later; this Mac runs macOS 12\.7\.4/);
  ({ m, p } = machineFor('linux', { kvm: 'missing' }));
  r = await run([], m, p);
  assert.match(r.out.errText(), /no \/dev\/kvm/);
});

test('without a systemd user manager the Host still runs, and the unit is enabled for the next one', async () => {
  const { m, p } = machineFor('linux', { qemu: true, userManager: false });
  const { code, out } = await run([], m, p, { userRunDir: tmpdir() });
  assert.equal(code, 0, out.errText());
  // What `systemctl --user enable` writes for WantedBy=default.target, so the
  // next user manager starts the VM; enable-linger then makes that boot.
  const link = path.join(path.dirname(p.autostart), 'default.target.wants', 'cseries.service');
  assert.equal(fs.readlinkSync(link), p.autostart);
  assert.match(out.errText(), /systemd's user manager did not answer \(Failed to connect to user scope bus/);
  assert.match(out.errText(), /set to start when it next starts for you: at your next login/);
  assert.match(out.errText(), /sudo loginctl enable-linger someone/);
  // A second run, still with no manager, leaves the one link as it is.
  assert.equal((await run([], m, p, { userRunDir: tmpdir() })).code, 0);
  assert.equal(fs.readlinkSync(link), p.autostart);
  // uninstall takes the link away with the unit.
  assert.equal((await run(['uninstall'], m, p, { userRunDir: tmpdir() })).code, 0);
  assert.ok(!fs.existsSync(link) && !fs.existsSync(p.autostart));
});

test('with no terminal to show the authenticator\'s code on, the bastion is still paired and anchored, and the step says why', async () => {
  const { m, p } = machineFor('linux', { qemu: true, noTerminal: true });
  const { code, out } = await run([], m, p);
  assert.equal(code, 1, 'a soft failure: the Host runs');
  assert.match(out.errText(), /the authenticator was not enrolled \(no terminal to show the authenticator code on: run npx cseries in a terminal window\)/);
  const st = JSON.parse(fs.readFileSync(p.state, 'utf8')).bastion;
  assert.equal(st.anchored, true);
  assert.ok(!st.enrolled);
  assert.match(fs.readFileSync(p.installLog, 'utf8'), /^exit=18$/m, 'the install log says where it stopped');
});

test('a session with no XDG_RUNTIME_DIR finds a running user manager by its bus path, and enables the unit there', async () => {
  const { m, p } = machineFor('linux', { qemu: true, userManager: 'bus-only' });
  const runDir = tmpdir();
  fs.mkdirSync(path.join(runDir, '1000'));
  fs.writeFileSync(path.join(runDir, '1000', 'bus'), '');
  const { code, out } = await run([], m, p, { userRunDir: runDir });
  assert.equal(code, 0, out.errText());
  assert.doesNotMatch(out.errText(), /did not answer/);
  const viaBus = m.runner.calls.filter((c) => c.cmd === 'systemctl' && c.env && c.env.XDG_RUNTIME_DIR === path.join(runDir, '1000'));
  assert.deepEqual(viaBus.map((c) => c.args.slice(1).join(' ')), ['daemon-reload', 'enable cseries.service']);
  assert.equal(viaBus[0].env.DBUS_SESSION_BUS_ADDRESS, `unix:path=${path.join(runDir, '1000')}/bus`);
  assert.ok(!fs.existsSync(path.join(path.dirname(p.autostart), 'default.target.wants')), 'systemctl itself enabled it');
});

test('stop, start, status, and uninstall --delete-data', async () => {
  const { m, p } = machineFor('linux', { qemu: true });
  assert.equal((await run([], m, p)).code, 0);
  let r = await run(['status'], m, p);
  assert.match(r.out.text(), /Linux system \(Lima\): installed\n {2}running: {3}yes/);
  assert.match(r.out.text(), /system service: running/);
  r = await run(['stop'], m, p);
  assert.equal(r.code, 0);
  assert.equal(m.instance.status, 'Stopped');
  r = await run(['start'], m, p);
  assert.equal(r.code, 0, r.out.errText());
  assert.equal(m.instance.status, 'Running');
  r = await run(['uninstall', '--delete-data', '--yes'], m, p);
  assert.equal(r.code, 0, r.out.errText());
  assert.equal(m.instance, null);
  assert.ok(!fs.existsSync(p.lima));
  assert.ok(!fs.existsSync(p.autostart));
  assert.ok(m.userUnits.includes('disable cseries.service'));
});
