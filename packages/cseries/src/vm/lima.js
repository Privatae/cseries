'use strict';

// The macOS and Linux driver: Lima (lima-vm.io, Apache-2.0), on Apple's
// Virtualization.framework (vz) on macOS and on QEMU with KVM on Linux, with
// an instance of our own named cseries.
//
//   - Lima itself: the release pinned below, from Lima's GitHub release,
//     accepted only with the sha256 pinned here, unpacked into this command's
//     folder (paths.tools) and run from there. LIMA_HOME is this command's
//     folder too (paths.lima), so the person's own Lima instances and Lima
//     settings neither see nor change this one. No prompt.
//   - Linux also needs QEMU: when a piece of it is missing, ONE sudo run
//     installs it from the distribution's packages (machine.js), and adds the
//     person to /dev/kvm's group when the device is not theirs to open; this
//     session then starts Lima through `sg <group>`.
//   - The guest: the release's qcow2 (vm/lima/build-image.sh, the WSL guest's
//     packages, units and cseries-guest plus a kernel and what Lima needs),
//     checked against its sha256 and created from the template in
//     lima/template.js: nothing of the machine mounted, only 127.0.0.1:8800,
//     8810 and 8811 forwarded, the size the person chose (size.js), which
//     `npx cseries resize` changes later with `limactl edit`, the disk kept.
//   - Kept running: a Lima VM runs until it is stopped. It is started at login
//     by a LaunchAgent (macOS) or a systemd user unit (Linux) of ours, which
//     carry LIMA_HOME; `limactl start-at-login` would write ones that do not.
//   - Commands in the guest: `limactl shell --workdir / cseries sudo
//     /usr/local/bin/cseries-guest <command>`; Lima's user in the guest has
//     sudo without a password, as Lima sets it up.
//
// Installed for the whole machine on Linux (scope.js; ctx.paths.vmUser is
// set): this command runs as root, Lima and QEMU as the system account
// ctx.paths.vmUser (created here, in /dev/kvm's group), every limactl through
// `runuser -u <it> --`, LIMA_HOME and the tools in /var/lib/cseries, and the
// VM is started and kept running by the system unit cseries.service (User=
// that account, WantedBy=multi-user.target) rather than by a login item.

const fs = require('node:fs');
const path = require('node:path');
const { CliError } = require('../errors');
const size = require('../size');
const { limaTemplate } = require('./lima/template');
const machine = require('./lima/machine');
const { NO_LOG } = require('../log');

const INSTANCE = 'cseries';
const GUEST_BIN = '/usr/local/bin/cseries-guest';
const MIN_MACOS = [13, 5]; // Lima runs vz from macOS 13.5

// Lima 2.2.1. The sha256 values are the release's SHA256SUMS, whose signature
// (SHA256SUMS.asc) verified against Lima's release key
// C020EA876CE4E06C7AB95AEF49524C6F9F638F1A when they were pinned.
const LIMA = {
  version: '2.2.1',
  base: 'https://github.com/lima-vm/lima/releases/download/v2.2.1/',
  archives: {
    'darwin-arm64': { file: 'lima-2.2.1-Darwin-arm64.tar.gz', sha256: '9e9eacce88f37e185c346bad73aa6136f738d8cdf8c3bb23cd42b071824bc66e' },
    'darwin-x64': { file: 'lima-2.2.1-Darwin-x86_64.tar.gz', sha256: '6b776e4bc41f358e6ee01fefcc78e2eb26f11635a0fcff632234af77a566c6c4' },
    'linux-arm64': { file: 'lima-2.2.1-Linux-aarch64.tar.gz', sha256: 'fbcf4d127936666aa2cf6013b663c250431f2a59394757b404070f8d6a160e10' },
    'linux-x64': { file: 'lima-2.2.1-Linux-x86_64.tar.gz', sha256: 'b391ac7fcac2b2a5a1628107756463188ded4d18d3104a40a66a8bb6fec01bcb' },
  },
};

const OS_NAME = { darwin: 'macOS', linux: 'Linux' };

function limaDir(ctx) {
  return ctx.paths.join(ctx.paths.tools, `lima-${LIMA.version}`);
}

function limactlPath(ctx) {
  return ctx.paths.join(limaDir(ctx), 'bin', 'limactl');
}

// The whole machine's install (platform.js machinePaths).
function isMachine(ctx) {
  return Boolean(ctx.paths && ctx.paths.vmUser);
}

function env(ctx) {
  return isMachine(ctx) ? { LIMA_HOME: ctx.paths.lima, HOME: ctx.paths.vmHome } : { LIMA_HOME: ctx.paths.lima };
}

const RUNUSER_PATHS = ['/usr/sbin/runuser', '/sbin/runuser', '/usr/bin/runuser', '/bin/runuser'];

function findRunuser(ctx) {
  const list = ctx.runuserPaths || RUNUSER_PATHS;
  const found = list.find((p) => {
    try {
      fs.accessSync(p, fs.constants.X_OK);
      return true;
    } catch {
      return false;
    }
  });
  if (!found) {
    throw new CliError('runuser (util-linux) is not on this machine, and the whole-machine install runs Lima through it as the cseries account.',
      'Install util-linux, or install C-Series for your account only: npx cseries --just-me');
  }
  return found;
}

// [cmd, args] for running a program of Lima's: as the VM's own account on a
// whole-machine install (runuser keeps the environment the runner adds, and
// sets the account's groups, /dev/kvm's among them, from the group database),
// as this account otherwise. runuser runs the program itself, not a shell.
function asVmUser(ctx, cmd, args) {
  if (!isMachine(ctx)) return [cmd, args];
  return [findRunuser(ctx), ['-u', ctx.paths.vmUser, '--', cmd, ...args]];
}

function text(b) {
  return Buffer.isBuffer(b) ? b.toString('utf8') : String(b || '');
}

async function limactl(ctx, args, opts = {}) {
  const [cmd, argv] = asVmUser(ctx, limactlPath(ctx), ['--tty=false', ...args]);
  const r = await ctx.runner.run(cmd, argv, { ...opts, env: env(ctx) });
  return { code: r.code, notFound: r.notFound, stdout: text(r.stdout), stderr: text(r.stderr) };
}

function tail(s, n = 12) {
  return String(s).trim().split('\n').slice(-n).join('\n');
}

// The facts machine.js gathers, once per run, again after an install.
async function facts(ctx, fresh = false) {
  if (!ctx.limaFacts || fresh) ctx.limaFacts = await machine.probe(ctx, isMachine(ctx) ? ctx.paths.vmUser : null);
  return ctx.limaFacts;
}

async function installedLimaVersion(ctx) {
  if (!fs.existsSync(limactlPath(ctx))) return null;
  const r = await limactl(ctx, ['--version'], { timeoutMs: 15000 });
  if (r.code !== 0) return null;
  const m = /limactl version (\S+)/.exec(r.stdout);
  return m ? m[1] : null;
}

// -> [{name, status}] for the instances under our LIMA_HOME.
async function instances(ctx) {
  if (!fs.existsSync(limactlPath(ctx))) return [];
  const r = await limactl(ctx, ['list', '--format', '{{.Name}} {{.Status}}']);
  if (r.code !== 0) return [];
  return r.stdout.split('\n').map((l) => l.trim().split(/\s+/)).filter((p) => p[0]).map(([name, status]) => ({ name, status: status || '' }));
}

async function instance(ctx) {
  return (await instances(ctx)).find((i) => i.name === INSTANCE) || null;
}

function archiveKey(ctx) {
  return `${ctx.platform}-${ctx.arch}`;
}

async function installLima(ctx) {
  const a = LIMA.archives[archiveKey(ctx)];
  if (!a) throw new CliError(`Lima has no build for this machine (${archiveKey(ctx)}).`, '');
  const file = ctx.paths.join(ctx.paths.downloads, a.file);
  await ctx.net.download(`${LIMA.base}${a.file}`, file, { sha256: a.sha256, out: ctx.out, label: `Lima ${LIMA.version}` });
  const dest = limaDir(ctx);
  const part = `${dest}.part`;
  fs.rmSync(part, { recursive: true, force: true });
  fs.mkdirSync(part, { recursive: true });
  const r = await ctx.runner.run('tar', ['-xzf', file, '-C', part]);
  if (r.code !== 0) {
    fs.rmSync(part, { recursive: true, force: true });
    throw new CliError(`Lima ${LIMA.version} could not be unpacked: ${text(r.stderr).trim() || `tar exited with ${r.code}`}`,
      'Run npx cseries again.');
  }
  fs.rmSync(dest, { recursive: true, force: true });
  fs.renameSync(part, dest);
  fs.rmSync(file, { force: true });
  const v = await installedLimaVersion(ctx);
  if (v !== LIMA.version) {
    throw new CliError(`Lima ${LIMA.version} was unpacked into ${dest}, but it does not run (it answered ${v || 'nothing'}).`,
      'Run npx cseries again. If it fails the same way, please report it with the lines above.');
  }
  ctx.out.detail(`Lima ${LIMA.version} is installed in ${dest}.`);
}

function describeLinuxInstall(out, packages, f, join) {
  out.detail('C-Series runs the Host in a Linux virtual machine, with Lima and QEMU. QEMU comes');
  out.detail(f.uid === '0' ? 'from this Linux\'s own packages, so this command, as root:' : 'from this Linux\'s own packages, so sudo asks for your password once, to:');
  if (packages.length) out.detail(`  - install ${packages.join(' ')} (${f.pm})`);
  if (join) out.detail(`  - add you to the ${f.kvm_group} group, so the VM can use this machine's virtualization (/dev/kvm)`);
}

// state.json "packagesAdded": {pm, packages}, what this command installed
// from the distribution's packages, and "kvmJoined": {group, user}, the
// account it added to /dev/kvm's group, so uninstall can say what to remove
// (and remove it, when the person says yes) without guessing.
function recordAdded(ctx, f, packages, join) {
  if (!ctx.state) return;
  if (packages.length) {
    const was = ctx.state.get('packagesAdded');
    const had = was && was.pm === f.pm && Array.isArray(was.packages) ? was.packages : [];
    ctx.state.set('packagesAdded', { pm: f.pm, packages: [...new Set([...had, ...packages])] });
  }
  if (join) ctx.state.set('kvmJoined', { group: f.kvm_group, user: f.user });
}

async function installLinuxPieces(ctx) {
  const f = await facts(ctx);
  const pieces = machine.missingPieces(f);
  const kvm = machine.kvmAccess(f);
  // The whole machine's VM runs as its own account, which ensureVmUser puts
  // in /dev/kvm's group; nobody's own account is added.
  const join = kvm === 'join' && !isMachine(ctx);
  if (!pieces.length && !join) return;
  const packages = pieces.length ? machine.packagesFor(f, ctx.arch, pieces) : [];
  if (packages === null) {
    throw new CliError(`This Linux has no package manager this command knows (apt-get, dnf, zypper or pacman), and Lima needs QEMU: ${pieces.join(', ')} ${pieces.length === 1 ? 'is' : 'are'} missing.`,
      `Install QEMU for ${machine.QEMU[ctx.arch]} with its UEFI firmware and qemu-img, and an SSH client, then run npx cseries again.`);
  }
  if (!f.sudo && f.uid !== '0') {
    throw new CliError('Lima needs QEMU from this Linux\'s packages, and sudo, which installs it, is not here.',
      `Install ${packages.join(' ')} as root, then run npx cseries again.`);
  }
  describeLinuxInstall(ctx.out, packages, f, join);
  const script = machine.installScript(f, ctx.arch, { packages, join });
  // sudo asks for the password on the terminal itself (not on its output,
  // which is captured), so the status line starts only once the script
  // prints: before that, sudo's prompt may be on the line.
  const doing = packages.length ? `Installing ${packages.join(' ')} with ${f.pm}` : `Adding you to the ${f.kvm_group} group`;
  let status = null;
  const log = ctx.log || NO_LOG;
  log.section(`sudo: ${doing}`);
  // As root (the whole machine's install) the script runs as it is.
  const [cmd, argv] = f.uid === '0' ? ['/bin/sh', ['-c', script]] : [f.sudo, ['-p', '[cseries] sudo password for %u: ', '/bin/sh', '-c', script]];
  const r = await ctx.runner.run(cmd, argv, {
    stdin: 'inherit',
    onOutput: (c) => {
      log.write(c);
      if (!status) status = ctx.out.status(doing);
    },
  });
  if (r.code !== 0) {
    if (status) status.end();
    const why = tail(`${text(r.stderr)}\n${text(r.stdout)}`, 6);
    throw new CliError(`Installing QEMU did not finish (sudo ended with exit code ${r.code}). Nothing else was changed.${why ? `\n${why}` : ''}`,
      `Run npx cseries again. The whole output is in ${ctx.paths.log}. To do it by hand: sudo ${f.pm} install ${packages.join(' ')}${join ? `, then sudo usermod -a -G ${f.kvm_group} ${f.user}` : ''}.`);
  }
  if (status) status.end(packages.length ? `Installed ${packages.join(' ')}.` : `You are in the ${f.kvm_group} group.`);
  recordAdded(ctx, f, packages, join);
  const after = await facts(ctx, true);
  const still = machine.missingPieces(after);
  if (still.length) {
    throw new CliError(`QEMU was installed, but ${still.join(', ')} ${still.length === 1 ? 'is' : 'are'} still not where Lima looks.`,
      'Please report it with this Linux\'s name and version.');
  }
}

// Lima's `start`, through `sg` when the person is in /dev/kvm's group and
// this session is not yet.
async function limaStart(ctx) {
  const args = ['--tty=false', 'start', '--timeout=10m', INSTANCE];
  let cmd = limactlPath(ctx);
  let argv = args;
  if (ctx.platform === 'linux') {
    const f = await facts(ctx);
    if (machine.kvmAccess(f) === 'sg') {
      cmd = f.sg;
      argv = [f.kvm_group, '-c', machine.sgCommand(limactlPath(ctx), args)];
    }
  }
  const log = ctx.log || NO_LOG;
  log.section('limactl start');
  const r = await ctx.runner.run(cmd, argv, { env: env(ctx), onOutput: (c) => log.write(c) });
  return { code: r.code, stdout: text(r.stdout), stderr: text(r.stderr) };
}

// The whole machine's VM account: a system account of its own with no
// login, in /dev/kvm's group, owning LIMA_HOME and its home, both 700. An
// account by that name that this command did not make (another home) is
// refused, never taken over. state.json "vmUserCreated": this command made
// it, so uninstall --delete-data removes it.
function vmUserScript(ctx, f) {
  const p = ctx.paths;
  const g = f.kvm_group && f.kvm_group !== 'root' && f.kvm_group !== 'UNKNOWN' ? f.kvm_group : '';
  return [
    'set -eu',
    'umask 022',
    'PATH=/usr/sbin:/usr/bin:/sbin:/bin',
    'export PATH',
    `u=${machine.shq(p.vmUser)}`,
    `home=${machine.shq(p.vmHome)}`,
    `install -d -o root -g root -m 755 ${machine.shq(p.base)}`,
    'if ! getent passwd "$u" >/dev/null; then',
    '  nl=$(command -v nologin || echo /bin/false)',
    '  useradd --system --user-group --home-dir "$home" --no-create-home --shell "$nl" --comment "C-Series Host VM (npx cseries)" "$u"',
    '  echo created=yes',
    'fi',
    'h=$(getent passwd "$u" | cut -d: -f6)',
    '[ "$h" = "$home" ] || { echo "an account named $u is already here, with the home $h, and this command did not make it" >&2; exit 3; }',
    g ? `usermod -a -G ${machine.shq(g)} "$u"` : ':',
    `install -d -o "$u" -g "$u" -m 700 ${machine.shq(p.lima)} "$home"`,
    'echo ready=yes',
  ].join('\n');
}

async function ensureVmUser(ctx) {
  const f = await facts(ctx);
  const r = await ctx.runner.run('/bin/sh', ['-c', vmUserScript(ctx, f)], { timeoutMs: 60000 });
  const out = text(r.stdout);
  if (r.code !== 0 || !/^ready=yes$/m.test(out)) {
    throw new CliError(`The system account ${ctx.paths.vmUser}, which the whole machine's VM runs as, could not be set up: ${tail(`${text(r.stderr)}\n${out}`, 4) || `exit ${r.code}`}`,
      'Run npx cseries again. To install C-Series for your account only instead: npx cseries --just-me');
  }
  if (/^created=yes$/m.test(out) && ctx.state) ctx.state.set('vmUserCreated', true);
  await facts(ctx, true);
  ctx.out.detail(`The VM runs as the system account ${ctx.paths.vmUser}, which no one signs in as.`);
}

// The account is there with this command's home, and in /dev/kvm's group
// when the device is not open to everyone, and LIMA_HOME, which is the
// account's, is here: uninstall --delete-data that keeps the bastion removes
// LIMA_HOME and leaves the account, and the next install makes it again.
function vmUserReady(ctx, f) {
  if (f.vm_user_home !== ctx.paths.vmHome) return false;
  if (!fs.existsSync(ctx.paths.lima)) return false;
  if (f.kvm_world === 'yes') return true;
  return String(f.vm_user_groups || '').split(/\s+/).includes(f.kvm_group);
}

function systemUnit(ctx) {
  const exe = sdq(limactlPath(ctx));
  return [
    '[Unit]',
    'Description=C-Series: the Host\'s Linux VM (Lima), for the whole machine',
    'After=network-online.target',
    'Wants=network-online.target',
    // Three failed starts in five minutes and the unit reads as failed (what
    // startMachineGuest stops on, with journalctl's hint), instead of
    // restarting every RestartSec for good: systemd's own limit, five in ten
    // seconds, a 10-second RestartSec never reaches.
    'StartLimitIntervalSec=300',
    'StartLimitBurst=3',
    '',
    '[Service]',
    'Type=simple',
    `User=${ctx.paths.vmUser}`,
    `Group=${ctx.paths.vmUser}`,
    `Environment=${sdq(`LIMA_HOME=${ctx.paths.lima}`)} ${sdq(`HOME=${ctx.paths.vmHome}`)}`,
    `WorkingDirectory=${ctx.paths.vmHome}`,
    `ExecStart=${exe} --tty=false start --foreground ${INSTANCE}`,
    `ExecStop=${exe} --tty=false stop ${INSTANCE}`,
    'Restart=on-failure',
    'RestartSec=10',
    'TimeoutStopSec=120',
    '',
    '[Install]',
    'WantedBy=multi-user.target',
    '',
  ].join('\n');
}

async function systemctl(ctx, args) {
  const r = await ctx.runner.run('systemctl', args, { timeoutMs: 60000 });
  return { code: r.code, out: text(r.stdout).trim(), why: `${text(r.stderr)}${text(r.stdout)}`.trim() || `exit ${r.code}` };
}

// The unit in place (rewritten when this release's differs) and systemd
// told about it.
async function writeSystemUnit(ctx) {
  const file = ctx.paths.autostart;
  const want = systemUnit(ctx);
  let have = null;
  try {
    have = fs.readFileSync(file, 'utf8');
  } catch { /* none yet */ }
  if (have === want) return;
  fs.mkdirSync(path.posix.dirname(file), { recursive: true });
  fs.writeFileSync(`${file}.new`, want, { mode: 0o644 });
  fs.renameSync(`${file}.new`, file);
  const r = await systemctl(ctx, ['daemon-reload']);
  if (r.code !== 0) throw new CliError(`systemd did not take the unit ${file}: ${r.why}`, 'Run npx cseries again.');
}

// Starts the whole machine's VM through its unit, then waits for Lima to
// say it runs and for the guest to answer: the unit's limactl stays in the
// foreground, so systemctl start returns before the VM has booted.
async function startMachineGuest(ctx, drv, first) {
  await writeSystemUnit(ctx);
  const status = ctx.out.status(first ? 'Starting the VM (the first start takes a minute or two)' : 'Starting the VM');
  const s = await systemctl(ctx, ['start', 'cseries.service']);
  if (s.code !== 0) {
    status.end();
    throw new CliError(`systemd could not start cseries.service: ${s.why}`, 'Run npx cseries again. "journalctl -u cseries.service" shows why.');
  }
  const deadline = Date.now() + (ctx.vmStartTimeoutMs || 10 * 60 * 1000);
  for (;;) {
    const i = await instance(ctx);
    if (i && i.status === 'Running') {
      const w = await drv.guest(ctx, ['wait-boot']);
      if (w.code === 0) break;
    }
    const a = await systemctl(ctx, ['is-active', 'cseries.service']);
    if (a.out === 'failed' || Date.now() >= deadline) {
      status.end();
      throw new CliError(`The C-Series Linux system did not start${a.out === 'failed' ? ' (cseries.service failed)' : ' within 10 minutes'}.`,
        `Run npx cseries again. "journalctl -u cseries.service" and Lima's log ${ctx.paths.join(ctx.paths.lima, INSTANCE, 'ha.stderr.log')} show why.`);
    }
    await ctx.sleep(3000);
  }
  status.end('The VM is started.');
}

// state.json "limaFirstStart": set when the VM is created and cleared once it
// has started, so only that first start, which takes longest, says so.
const FIRST_START = 'limaFirstStart';

function launchAgentPlist(ctx) {
  const x = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  const log = ctx.paths.join(ctx.paths.lima, 'autostart.log');
  return `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>ai.privatae.cseries</string>
	<key>ProgramArguments</key>
	<array>
		<string>${x(limactlPath(ctx))}</string>
		<string>--tty=false</string>
		<string>start</string>
		<string>--foreground</string>
		<string>${INSTANCE}</string>
	</array>
	<key>EnvironmentVariables</key>
	<dict>
		<key>LIMA_HOME</key>
		<string>${x(ctx.paths.lima)}</string>
	</dict>
	<key>RunAtLoad</key>
	<true/>
	<key>StandardOutPath</key>
	<string>${x(log)}</string>
	<key>StandardErrorPath</key>
	<string>${x(log)}</string>
</dict>
</plist>
`;
}

// systemd's quoting for one word of ExecStart.
function sdq(s) {
  return `"${String(s).replace(/\\/g, '\\\\').replace(/"/g, '\\"').replace(/%/g, '%%')}"`;
}

function userUnit(ctx) {
  const exe = sdq(limactlPath(ctx));
  return [
    '[Unit]',
    'Description=C-Series: the Host\'s Linux VM (Lima)',
    '',
    '[Service]',
    'Type=simple',
    `Environment=${sdq(`LIMA_HOME=${ctx.paths.lima}`)}`,
    `ExecStart=${exe} --tty=false start --foreground ${INSTANCE}`,
    `ExecStop=${exe} --tty=false stop ${INSTANCE}`,
    'TimeoutStopSec=120',
    '',
    '[Install]',
    'WantedBy=default.target',
    '',
  ].join('\n');
}

async function systemctlUser(ctx, args, env) {
  const r = await ctx.runner.run('systemctl', ['--user', ...args], { timeoutMs: 30000, env });
  return { code: r.code, why: `${text(r.stderr)}${text(r.stdout)}`.trim() || `exit ${r.code}` };
}

// daemon-reload and enable through the person's systemd user manager. A
// session with no XDG_RUNTIME_DIR (su -, sudo -iu, some ssh setups) cannot
// find a manager that is running (another login, or linger): its bus is
// asked for by its path, /run/user/<uid>/bus, once.
async function enableUserUnit(ctx, uid) {
  const go = async (env) => {
    const reload = await systemctlUser(ctx, ['daemon-reload'], env);
    return reload.code === 0 ? systemctlUser(ctx, ['enable', 'cseries.service'], env) : reload;
  };
  const first = await go(undefined);
  if (first.code === 0) return first;
  const run = path.posix.join(ctx.userRunDir || '/run/user', String(uid));
  if (!/^\d+$/.test(String(uid)) || !fs.existsSync(path.posix.join(run, 'bus'))) return first;
  const again = await go({ XDG_RUNTIME_DIR: run, DBUS_SESSION_BUS_ADDRESS: `unix:path=${run}/bus` });
  return again.code === 0 ? again : first;
}

// What `systemctl --user enable` writes for WantedBy=default.target: a link
// in the unit's own folder, default.target.wants/<unit>, to the unit's path.
// Written by hand where no user manager answers, so the next one that starts
// for this person (their next login, or at boot with linger) starts the VM.
function wantsLink(file) {
  return path.posix.join(path.posix.dirname(file), 'default.target.wants', path.posix.basename(file));
}

function linkWants(file) {
  const link = wantsLink(file);
  fs.mkdirSync(path.posix.dirname(link), { recursive: true });
  try {
    if (fs.readlinkSync(link) === file) return;
  } catch { /* none yet */ }
  fs.rmSync(link, { force: true });
  fs.symlinkSync(file, link);
}

function versionAtLeast(v, min) {
  const p = String(v).split('.').map((n) => Number(n) || 0);
  for (let i = 0; i < min.length; i++) {
    if ((p[i] || 0) !== min[i]) return (p[i] || 0) > min[i];
  }
  return true;
}

function driver(platform) {
  const osName = OS_NAME[platform] || platform;
  return {
    name: 'lima',
    label: 'Lima',
    supported: true,
    instance: INSTANCE,

    async preflight(ctx) {
      if (ctx.arch !== 'x64' && ctx.arch !== 'arm64') {
        throw new CliError(`C-Series runs on x64 and Arm64 machines; this one is ${ctx.arch}.`, '');
      }
      const f = await facts(ctx);
      if (f.uid === '0' && !isMachine(ctx)) {
        throw new CliError('Run npx cseries as yourself, not as root or with sudo, to install C-Series for your account only.',
          'Lima refuses to run as root. Without --just-me, npx cseries installs C-Series for the whole machine and asks for your password itself, once.');
      }
      if (isMachine(ctx) && f.uid !== '0') {
        throw new CliError('The whole-machine install runs as root, and this run is not.', 'Run npx cseries again; it asks sudo for your password itself.');
      }
      if (platform === 'darwin') {
        if (f.macos && !versionAtLeast(f.macos, MIN_MACOS)) {
          throw new CliError(`C-Series needs macOS ${MIN_MACOS.join('.')} or later; this Mac runs macOS ${f.macos}.`,
            'Update macOS from System Settings, then run npx cseries again.');
        }
        if (f.hv === '0') {
          throw new CliError('This Mac cannot run virtual machines (kern.hv_support is 0).',
            'On a Mac that is itself a virtual machine, turn on nested virtualization in its host; otherwise this Mac is not supported.');
        }
      } else if (f.kvm === 'missing') {
        throw new CliError('This Linux has no /dev/kvm, and the VM the Host runs in needs it.',
          'Turn on virtualization (Intel VT-x or AMD-V) in the firmware settings and make sure the kvm module is loaded (lsmod | grep kvm). Inside a VM, turn on nested virtualization in its host.');
      } else if (isMachine(ctx) && f.kvm_world !== 'yes' && (!f.kvm_group || f.kvm_group === 'root' || f.kvm_group === 'UNKNOWN')) {
        throw new CliError(`/dev/kvm belongs to the group ${f.kvm_group || 'unknown'}, so the account the whole machine's VM runs as cannot be given access to it.`,
          'Give /dev/kvm a group of its own (usually kvm, as the distribution\'s udev rules do), then run npx cseries again.');
      } else if (!isMachine(ctx) && machine.kvmAccess(f) === 'refuse') {
        throw new CliError(`/dev/kvm is not yours to open (its group is ${f.kvm_group || 'unknown'}), and this command cannot change that safely.`,
          'Ask whoever administers this machine to give your account access to /dev/kvm (usually the kvm group), then run npx cseries again.');
      }
    },

    async runtimeReady(ctx) {
      const v = await installedLimaVersion(ctx);
      let ready = v === LIMA.version;
      if (ready) ctx.out.detail(`Lima ${v} is installed.`);
      if (platform === 'linux') {
        const f = await facts(ctx);
        const pieces = machine.missingPieces(f);
        if (pieces.length) ready = false;
        else ctx.out.detail(`QEMU is installed (${f.qemu}).`);
        if (isMachine(ctx)) {
          if (!vmUserReady(ctx, f)) ready = false;
          else ctx.out.detail(`The VM runs as the system account ${ctx.paths.vmUser}.`);
        } else if (machine.kvmAccess(f) === 'join') ready = false;
      }
      return ready;
    },

    async installRuntime(ctx) {
      // The account first: on a whole-machine install every limactl, the
      // version check after unpacking included, runs as it.
      if (isMachine(ctx) && !vmUserReady(ctx, await facts(ctx))) await ensureVmUser(ctx);
      if ((await installedLimaVersion(ctx)) !== LIMA.version) await installLima(ctx);
      if (platform === 'linux') await installLinuxPieces(ctx);
    },

    async guestExists(ctx) {
      return Boolean(await instance(ctx));
    },

    async guestRunning(ctx) {
      const i = await instance(ctx);
      return Boolean(i && i.status === 'Running');
    },

    async createGuest(ctx) {
      const key = `lima-${ctx.arch}`;
      const g = ctx.releaseInfo.release.guest[key];
      if (!g) throw new CliError(`This release has no Lima image for ${ctx.arch} machines.`, 'Try npx cseries@latest.');
      const file = ctx.paths.join(ctx.paths.downloads, path.posix.basename(new URL(g.url).pathname));
      await ctx.net.download(g.url, file, { sha256: g.sha256, out: ctx.out, label: 'the C-Series Linux image' });
      // The size chosen (size.step, before this one); one a test or an older
      // state left out is the suggested one.
      const res = size.recorded(ctx) || size.suggested(size.machineOf(ctx.machine.totalMem, ctx.machine.cpus));
      const yaml = ctx.paths.join(ctx.paths.downloads, 'cseries-lima.yaml');
      fs.writeFileSync(yaml, limaTemplate({
        vmType: platform === 'darwin' ? 'vz' : 'qemu',
        arch: ctx.arch,
        image: file,
        sha256: g.sha256,
        cpus: res.cpus,
        memoryGiB: res.memoryGiB,
      }));
      fs.mkdirSync(ctx.paths.lima, { recursive: true });
      ctx.out.detail(`Creating the VM (${size.sizeName(res)}: ${size.describe(res)}; nothing of this machine shared with it)...`);
      const r = await limactl(ctx, ['create', `--name=${INSTANCE}`, yaml]);
      fs.rmSync(yaml, { force: true });
      if (r.code !== 0 && !/already exists/i.test(r.stderr)) {
        throw new CliError(`Lima could not create the C-Series Linux system:\n${tail(r.stderr || r.stdout)}`,
          'Run npx cseries again; the image is already downloaded.');
      }
      if (r.code === 0 && ctx.state) ctx.state.set(FIRST_START, true);
      // Lima keeps its own copy of the image in the instance.
      fs.rmSync(file, { force: true });
    },

    // The size an install from before sizes were chosen has: Lima's own
    // record of the instance (Memory in bytes).
    async currentSize(ctx) {
      if (!fs.existsSync(limactlPath(ctx))) return null;
      const r = await limactl(ctx, ['list', '--format', '{{.Name}} {{.CPUs}} {{.Memory}}']);
      if (r.code !== 0) return null;
      const line = r.stdout.split('\n').map((l) => l.trim().split(/\s+/)).find((p) => p[0] === INSTANCE);
      if (!line || !/^[1-9][0-9]*$/.test(line[1] || '') || !/^[1-9][0-9]*$/.test(line[2] || '')) return null;
      return { cpus: Number(line[1]), memoryGiB: Math.max(1, Math.round(Number(line[2]) / size.GiB)) };
    },

    // A new size for the VM: stopped (through its unit, on a whole-machine
    // install), given the size by `limactl edit`, which changes only the
    // instance's cpus and memory (its disk, with the Host's data, is the
    // same file); the caller (cli.js resize) records the size and starts it
    // again, so a start that fails leaves the record saying the size the VM
    // has. A size Lima refuses leaves the VM as it was, started again at its
    // old size.
    async resize(ctx, s) {
      if (await this.guestRunning(ctx)) {
        const status = ctx.out.status('Stopping the VM');
        await this.stopGuest(ctx);
        status.end('The VM is stopped.');
      }
      const r = await limactl(ctx, ['edit', INSTANCE, `--cpus=${s.cpus}`, `--memory=${s.memoryGiB}`]);
      if (r.code !== 0) {
        await this.startGuest(ctx).catch(() => {});
        throw new CliError(`Lima did not change the VM's size:\n${tail(r.stderr || r.stdout)}`,
          'The VM keeps the size it had and is started again. Run npx cseries resize again; npx cseries status shows its size.');
      }
      ctx.out.detail(`The VM is ${size.sizeName(s)} now: ${size.describe(s)}. Its disk, with all the Host's data, is the same.`);
    },

    async startGuest(ctx) {
      const i = await instance(ctx);
      if (isMachine(ctx)) {
        const first = Boolean(ctx.state && ctx.state.get(FIRST_START));
        if (!i || i.status !== 'Running') await startMachineGuest(ctx, this, first);
        if (first) ctx.state.set(FIRST_START, undefined);
      } else if (!i || i.status !== 'Running') {
        const first = Boolean(ctx.state && ctx.state.get(FIRST_START));
        const status = ctx.out.status(first ? 'Starting the VM (the first start takes a minute or two)' : 'Starting the VM');
        const r = await limaStart(ctx);
        status.end(r.code === 0 ? 'The VM is started.' : undefined);
        if (r.code === 0 && first) ctx.state.set(FIRST_START, undefined);
        if (r.code !== 0) {
          throw new CliError(`Lima could not start the C-Series Linux system:\n${tail(r.stderr || r.stdout)}`,
            `Run npx cseries again. Lima's own log is ${ctx.paths.join(ctx.paths.lima, INSTANCE, 'ha.stderr.log')}.`);
        }
      }
      const w = await this.guest(ctx, ['wait-boot']);
      if (w.code !== 0) {
        const state = /system=(\S+)/.exec(w.stdout);
        throw new CliError(`The C-Series Linux system did not finish starting${state ? ` (systemd says: ${state[1]})` : `: ${tail(w.stderr, 3)}`}.`,
          'Run npx cseries again. If it keeps failing, "npx cseries logs" shows the Host\'s log.');
      }
    },

    async guest(ctx, args, opts = {}) {
      const [cmd, argv] = asVmUser(ctx, limactlPath(ctx), ['--tty=false', 'shell', '--workdir', '/', INSTANCE, 'sudo', GUEST_BIN, ...args]);
      if (opts.inherit) {
        const r = await ctx.runner.inherit(cmd, argv, { env: env(ctx) });
        return { code: r.code, stdout: '', stderr: '' };
      }
      const r = await ctx.runner.run(cmd, argv, { input: opts.input, env: env(ctx), onOutput: opts.onOutput });
      return { code: r.code, stdout: text(r.stdout), stderr: text(r.stderr) };
    },

    // A shell as root in the guest, the script on its standard input, for
    // what a guest older than this command cannot do with its own
    // cseries-guest (machine-firewall.js).
    async guestShell(ctx, script) {
      const [cmd, argv] = asVmUser(ctx, limactlPath(ctx), ['--tty=false', 'shell', '--workdir', '/', INSTANCE, 'sudo', '/bin/sh', '-s']);
      const r = await ctx.runner.run(cmd, argv, { input: script, env: env(ctx) });
      return { code: r.code, stdout: text(r.stdout), stderr: text(r.stderr) };
    },

    async stopGuest(ctx) {
      if (!(await this.guestRunning(ctx))) return;
      await this.guest(ctx, ['stop']);
      // The unit's own ExecStop is limactl stop; a stop outside it would
      // read as a failure, and Restart=on-failure would start the VM again.
      if (isMachine(ctx)) await systemctl(ctx, ['stop', 'cseries.service']);
      else await limactl(ctx, ['stop', INSTANCE]);
    },

    async removeGuest(ctx, { deleteData }) {
      await this.setAutostart(ctx, false);
      if (!deleteData) {
        await this.stopGuest(ctx);
        return;
      }
      const r = await limactl(ctx, ['delete', '--force', INSTANCE]);
      if (r.code !== 0 && (await instance(ctx))) {
        throw new CliError(`Lima could not delete the C-Series Linux system: ${tail(r.stderr, 3)}`, 'Run npx cseries uninstall --delete-data again.');
      }
      fs.rmSync(ctx.paths.lima, { recursive: true, force: true });
    },

    // A login item of ours. A failure here is said and the run goes on: the
    // Host runs now either way, and npx cseries start brings it back later.
    async setAutostart(ctx, on) {
      const file = ctx.paths.autostart;
      if (isMachine(ctx)) {
        if (on) {
          await writeSystemUnit(ctx);
          const en = await systemctl(ctx, ['enable', 'cseries.service']);
          if (en.code !== 0) ctx.out.warn(`C-Series will not start by itself at boot: systemctl enable cseries.service failed (${en.why}).`);
          return;
        }
        if (fs.existsSync(file)) {
          // --now: the VM stops with it, through the unit's ExecStop.
          await systemctl(ctx, ['disable', '--now', 'cseries.service']);
          fs.rmSync(file, { force: true });
          await systemctl(ctx, ['daemon-reload']);
        }
        return;
      }
      if (platform === 'darwin') {
        if (on) {
          fs.mkdirSync(path.posix.dirname(file), { recursive: true });
          fs.writeFileSync(file, launchAgentPlist(ctx));
        } else {
          fs.rmSync(file, { force: true });
        }
        return;
      }
      if (on) {
        fs.mkdirSync(path.posix.dirname(file), { recursive: true });
        fs.writeFileSync(file, userUnit(ctx));
        const f = await facts(ctx);
        const en = await enableUserUnit(ctx, f.uid);
        if (en.code !== 0) {
          // No user manager answers this session. The unit is enabled the way
          // systemctl would have, so it starts with the next one; without
          // this, a later enable-linger started nothing.
          let linked = true;
          try {
            linkWants(file);
          } catch {
            linked = false;
          }
          ctx.out.warn(`systemd's user manager did not answer (${en.why}).`);
          if (linked) {
            ctx.out.warn('C-Series is set to start when it next starts for you: at your next login.');
            ctx.out.warn(`To have it start at boot, with no login: sudo loginctl enable-linger ${f.user || '"$USER"'}`);
          } else {
            ctx.out.warn('C-Series will not start by itself. Start it with: npx cseries start');
          }
        }
        return;
      }
      if (fs.existsSync(file) || fs.existsSync(wantsLink(file))) {
        const f = await facts(ctx);
        const dis = await systemctlUser(ctx, ['disable', 'cseries.service']);
        if (dis.code !== 0 && /^\d+$/.test(String(f.uid))) {
          const run = path.posix.join(ctx.userRunDir || '/run/user', String(f.uid));
          if (fs.existsSync(path.posix.join(run, 'bus'))) {
            await systemctlUser(ctx, ['disable', 'cseries.service'], { XDG_RUNTIME_DIR: run, DBUS_SESSION_BUS_ADDRESS: `unix:path=${run}/bus` });
          }
        }
        fs.rmSync(wantsLink(file), { force: true });
        fs.rmSync(file, { force: true });
        await systemctlUser(ctx, ['daemon-reload']);
      }
    },

    unreachableHint(ctx) {
      return `Lima is not forwarding 127.0.0.1:8800 from the VM to this machine. Its log is ${ctx.paths.join(ctx.paths.lima, INSTANCE, 'ha.stderr.log')}; "npx cseries stop" and then "npx cseries" restarts it.`;
    },

    osName,
  };
}

module.exports = {
  driver, LIMA, INSTANCE, GUEST_BIN, launchAgentPlist, userUnit, systemUnit, vmUserScript, limactlPath, versionAtLeast, isMachine, findRunuser, RUNUSER_PATHS,
};
