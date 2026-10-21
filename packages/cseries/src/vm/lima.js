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
//     8810 and 8811 forwarded, sized as WSL sizes its VM (common.js).
//   - Kept running: a Lima VM runs until it is stopped. It is started at login
//     by a LaunchAgent (macOS) or a systemd user unit (Linux) of ours, which
//     carry LIMA_HOME; `limactl start-at-login` would write ones that do not.
//   - Commands in the guest: `limactl shell --workdir / cseries sudo
//     /usr/local/bin/cseries-guest <command>`; Lima's user in the guest has
//     sudo without a password, as Lima sets it up.

const fs = require('node:fs');
const path = require('node:path');
const { CliError } = require('../errors');
const { guestResourcesFor, HOST_NEEDS_GIB } = require('./common');
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

function env(ctx) {
  return { LIMA_HOME: ctx.paths.lima };
}

function text(b) {
  return Buffer.isBuffer(b) ? b.toString('utf8') : String(b || '');
}

async function limactl(ctx, args, opts = {}) {
  const r = await ctx.runner.run(limactlPath(ctx), ['--tty=false', ...args], { ...opts, env: env(ctx) });
  return { code: r.code, notFound: r.notFound, stdout: text(r.stdout), stderr: text(r.stderr) };
}

function tail(s, n = 12) {
  return String(s).trim().split('\n').slice(-n).join('\n');
}

// The facts machine.js gathers, once per run, again after an install.
async function facts(ctx, fresh = false) {
  if (!ctx.limaFacts || fresh) ctx.limaFacts = await machine.probe(ctx);
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
  out.detail('from this Linux\'s own packages, so sudo asks for your password once, to:');
  if (packages.length) out.detail(`  - install ${packages.join(' ')} (${f.pm})`);
  if (join) out.detail(`  - add you to the ${f.kvm_group} group, so the VM can use this machine's virtualization (/dev/kvm)`);
}

async function installLinuxPieces(ctx) {
  const f = await facts(ctx);
  const pieces = machine.missingPieces(f);
  const kvm = machine.kvmAccess(f);
  const join = kvm === 'join';
  if (!pieces.length && !join) return;
  const packages = pieces.length ? machine.packagesFor(f, ctx.arch, pieces) : [];
  if (packages === null) {
    throw new CliError(`This Linux has no package manager this command knows (apt-get, dnf, zypper or pacman), and Lima needs QEMU: ${pieces.join(', ')} ${pieces.length === 1 ? 'is' : 'are'} missing.`,
      `Install QEMU for ${machine.QEMU[ctx.arch]} with its UEFI firmware and qemu-img, and an SSH client, then run npx cseries again.`);
  }
  if (!f.sudo) {
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
  const r = await ctx.runner.run(f.sudo, ['-p', '[cseries] sudo password for %u: ', '/bin/sh', '-c', script], {
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

async function systemctlUser(ctx, args) {
  const r = await ctx.runner.run('systemctl', ['--user', ...args], { timeoutMs: 30000 });
  return { code: r.code, why: `${text(r.stderr)}${text(r.stdout)}`.trim() || `exit ${r.code}` };
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
      if (f.uid === '0') {
        throw new CliError('Run npx cseries as yourself, not as root or with sudo.',
          'Lima refuses to run as root, and npx cseries asks for your password itself, once, when it needs it.');
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
      } else if (machine.kvmAccess(f) === 'refuse') {
        throw new CliError(`/dev/kvm is not yours to open (its group is ${f.kvm_group || 'unknown'}), and this command cannot change that safely.`,
          'Ask whoever administers this machine to give your account access to /dev/kvm (usually the kvm group), then run npx cseries again.');
      }
      const res = guestResourcesFor(ctx);
      if (res.memoryGiB < HOST_NEEDS_GIB) {
        ctx.out.warn(`This machine has ${res.totalGiB} GB of memory; the VM gets ${res.memoryGiB} GB of it, and the Host works best with ${HOST_NEEDS_GIB} GB or more.`);
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
        if (machine.kvmAccess(f) === 'join') ready = false;
      }
      return ready;
    },

    async installRuntime(ctx) {
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
      const res = guestResourcesFor(ctx);
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
      ctx.out.detail(`Creating the VM (${res.memoryGiB} GB of memory and ${res.cpus} processors at most; nothing of this machine shared with it)...`);
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

    async startGuest(ctx) {
      const i = await instance(ctx);
      if (!i || i.status !== 'Running') {
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
      const argv = ['--tty=false', 'shell', '--workdir', '/', INSTANCE, 'sudo', GUEST_BIN, ...args];
      if (opts.inherit) {
        const r = await ctx.runner.inherit(limactlPath(ctx), argv, { env: env(ctx) });
        return { code: r.code, stdout: '', stderr: '' };
      }
      const r = await ctx.runner.run(limactlPath(ctx), argv, { input: opts.input, env: env(ctx), onOutput: opts.onOutput });
      return { code: r.code, stdout: text(r.stdout), stderr: text(r.stderr) };
    },

    async stopGuest(ctx) {
      if (!(await this.guestRunning(ctx))) return;
      await this.guest(ctx, ['stop']);
      await limactl(ctx, ['stop', INSTANCE]);
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
        const reload = await systemctlUser(ctx, ['daemon-reload']);
        const en = reload.code === 0 ? await systemctlUser(ctx, ['enable', 'cseries.service']) : reload;
        if (en.code !== 0) {
          ctx.out.warn(`C-Series will not start by itself when you log in: systemd's user manager said: ${en.why}`);
          ctx.out.warn('Start it with: npx cseries start');
        }
        return;
      }
      if (fs.existsSync(file)) {
        await systemctlUser(ctx, ['disable', 'cseries.service']);
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

module.exports = { driver, LIMA, INSTANCE, GUEST_BIN, launchAgentPlist, userUnit, limactlPath, versionAtLeast };
