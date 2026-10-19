'use strict';

// The Windows driver: WSL2, with a distribution of our own named cseries.
//
//   - WSL missing: the one elevated step (wsl.exe --install --no-distribution,
//     through a UAC prompt), an HKCU RunOnce entry so the command resumes
//     after the restart, and the restart prompt.
//   - The guest: the release's root filesystem, checked against its sha256,
//     imported with `wsl --import cseries %LOCALAPPDATA%\cseries\wsl <rootfs>
//     --version 2`.
//   - Kept running: WSL stops an idle distribution, so a hidden
//     `cseries-guest keepalive` holds it while the person is logged in,
//     started now and at every logon from an HKCU Run entry.
//   - Commands in the guest: wsl.exe -d cseries -u root --exec cseries-guest.
//
// wsl.exe's own output is UTF-16LE; see wsl-text.js.

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { CliError } = require('../errors');
const { decodeWslOutput, parseDistroList, parseWslVersion, windowsBuild, explainWslError } = require('./wsl-text');
const win = require('../win/windows');

const DISTRO = 'cseries';
const GUEST_BIN = '/usr/local/bin/cseries-guest';
const WSL = 'wsl.exe';
const MIN_BUILD = 19041; // Windows 10 version 2004, the first with WSL 2 everywhere
const RUN_VALUE = 'cseries';
const RUNONCE_VALUE = 'cseries-resume';

async function wsl(ctx, args, opts = {}) {
  const r = await ctx.runner.run(WSL, args, { ...opts, env: { WSL_UTF8: '1', ...(opts.env || {}) } });
  return {
    code: r.code,
    notFound: r.notFound,
    stdout: decodeWslOutput(r.stdout),
    stderr: decodeWslOutput(r.stderr),
  };
}

// -> {installed, version, kernel, detail}
async function status(ctx) {
  const v = await wsl(ctx, ['--version']);
  if (v.notFound) return { installed: false, detail: 'wsl.exe is not on this PC' };
  if (v.code === 0) {
    const p = parseWslVersion(v.stdout);
    if (p) return { installed: true, version: p.wsl, kernel: p.kernel };
  }
  return { installed: false, detail: `${v.stdout}${v.stderr}`.trim() };
}

async function distros(ctx) {
  const r = await wsl(ctx, ['--list', '--quiet']);
  if (r.code !== 0) return [];
  return parseDistroList(r.stdout);
}

function guestArgs(args) {
  return ['-d', DISTRO, '-u', 'root', '--exec', GUEST_BIN, ...args];
}

function keepAliveArgs() {
  return guestArgs(['keepalive']);
}

// The Run entry's command line. Start-Process -WindowStyle Hidden starts
// wsl.exe with no window; the PowerShell around it may show for a moment at
// logon.
function keepAliveCommand() {
  const list = keepAliveArgs().map((a) => `'${a}'`).join(',');
  return `powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden -Command "Start-Process -WindowStyle Hidden -FilePath wsl.exe -ArgumentList ${list}"`;
}

// The RunOnce entry's command line: this same release again, in a window that
// stays open so its output can be read.
function resumeCommand(ctx) {
  const { release, placeholders } = ctx.releaseInfo;
  if (!placeholders.length && !ctx.env.CSERIES_RELEASE_JSON) {
    return `cmd.exe /k npx --yes cseries@${release.version}`;
  }
  return `cmd.exe /k ""${process.execPath}" "${path.win32.resolve(ctx.binPath || '')}""`;
}

function failure(r, what) {
  const text = `${r.stdout}\n${r.stderr}`.trim();
  const e = explainWslError(text);
  if (e && !e.alreadyExists && !e.notFound) return new CliError(e.message, e.next);
  return new CliError(`${what} (wsl.exe exited with ${r.code}): ${text || 'no message'}`,
    'Run npx cseries again. If it fails the same way, run "wsl.exe --update" in a terminal, restart Windows, and try once more.');
}

const driver = {
  name: 'wsl',
  label: 'WSL',
  supported: true,
  distro: DISTRO,

  async preflight(ctx) {
    const build = windowsBuild(ctx.osRelease || os.release());
    if (build !== null && build < MIN_BUILD) {
      throw new CliError(
        `C-Series needs Windows 10 version 2004 (build ${MIN_BUILD}) or later, or Windows 11. This PC runs build ${build}.`,
        'Update Windows from Settings, then run npx cseries again.');
    }
    if (ctx.arch !== 'x64' && ctx.arch !== 'arm64') {
      throw new CliError(`C-Series runs on x64 and Arm64 PCs; this one is ${ctx.arch}.`, '');
    }
    // WSL already running a distribution proves virtualization works.
    const st = await status(ctx);
    if (st.installed && (await distros(ctx)).length) return;
    const v = await win.virtualization(ctx.runner);
    if (v.hypervisor === false && v.firmware === false) {
      throw new CliError(
        'Virtualization is turned off on this PC, and WSL needs it.',
        'Turn on virtualization (Intel VT-x or AMD-V, sometimes called SVM) in the firmware (BIOS/UEFI) settings, restart, and run npx cseries again.');
    }
    if (v.hypervisor === null && v.firmware === null) {
      ctx.out.warn('Could not check whether virtualization is on; carrying on.');
    }
  },

  async runtimeReady(ctx) {
    const st = await status(ctx);
    if (st.installed) ctx.out.detail(`WSL ${st.version} is installed.`);
    return st.installed;
  },

  async installRuntime(ctx) {
    const out = ctx.out;
    out.detail('C-Series runs the Host in WSL, the Linux virtual machine built into Windows.');
    out.detail('WSL is not installed yet. Windows will ask for permission to install it: this is');
    out.detail('the only step that needs an administrator. After it, Windows needs a restart.');
    const r = await win.elevatedRun(ctx.runner, WSL, ['--install', '--no-distribution']);
    if (r.declined) {
      throw new CliError('WSL was not installed: the Windows permission prompt was declined.',
        'Run npx cseries again and choose Yes when Windows asks.');
    }
    if (r.code !== 0) {
      throw new CliError(`WSL could not be installed (exit ${r.code}). ${r.stderr}`.trim(),
        'Open a terminal and run "wsl.exe --install --no-distribution" to see why, then run npx cseries again.');
    }
    await win.setRegistryValue(ctx.runner, win.RUNONCE_KEY, RUNONCE_VALUE, resumeCommand(ctx));
    out.detail('WSL is installed.');
    const now = ctx.prompt ? await ctx.prompt('Restart Windows now to finish? [y/N] ') : false;
    if (now) {
      await ctx.runner.run('shutdown.exe', ['/r', '/t', '10', '/c', 'Restarting to finish installing WSL for C-Series.']);
      return { halt: 'Windows restarts in 10 seconds. When you sign in again, C-Series carries on by itself.' };
    }
    return { halt: 'Restart Windows to finish installing WSL. When you sign in again, C-Series carries on by itself.' };
  },

  async guestExists(ctx) {
    return (await distros(ctx)).some((d) => d.toLowerCase() === DISTRO);
  },

  async guestRunning(ctx) {
    const r = await wsl(ctx, ['--list', '--running', '--quiet']);
    return r.code === 0 && parseDistroList(r.stdout).some((d) => d.toLowerCase() === DISTRO);
  },

  async createGuest(ctx) {
    const key = `wsl-${ctx.arch}`;
    const g = ctx.releaseInfo.release.guest[key];
    if (!g) throw new CliError(`This release has no WSL image for ${ctx.arch} PCs.`, 'Try npx cseries@latest.');
    const file = ctx.paths.join(ctx.paths.downloads, path.posix.basename(new URL(g.url).pathname));
    await ctx.net.download(g.url, file, { sha256: g.sha256, out: ctx.out, label: 'the C-Series Linux image' });
    const vhdx = ctx.paths.join(ctx.paths.wsl, 'ext4.vhdx');
    let r;
    if (fs.existsSync(vhdx)) {
      // A disk from an earlier install that WSL no longer lists: register it
      // as it is, so the Host's data on it is kept.
      ctx.out.detail(`Using the existing disk ${vhdx}.`);
      r = await wsl(ctx, ['--import-in-place', DISTRO, vhdx]);
    } else {
      fs.mkdirSync(ctx.paths.wsl, { recursive: true });
      ctx.out.detail('Importing it into WSL...');
      r = await wsl(ctx, ['--import', DISTRO, ctx.paths.wsl, file, '--version', '2']);
    }
    if (r.code !== 0) {
      const e = explainWslError(`${r.stdout}\n${r.stderr}`);
      if (!(e && e.alreadyExists)) throw failure(r, 'WSL could not import the C-Series Linux image');
    }
    fs.rmSync(file, { force: true });
  },

  async startGuest(ctx) {
    const r = await this.guest(ctx, ['wait-boot']);
    if (r.code !== 0) {
      const state = /system=(\S+)/.exec(r.stdout);
      if (state === null) throw failure(r, 'The C-Series Linux system did not start');
      throw new CliError(`The C-Series Linux system did not finish starting (systemd says: ${state[1]}).`,
        'Run npx cseries again. If it keeps failing, "npx cseries logs" shows the Host\'s log.');
    }
    ctx.runner.detached(WSL, keepAliveArgs());
  },

  async guest(ctx, args, opts = {}) {
    if (opts.inherit) {
      const r = await ctx.runner.inherit(WSL, guestArgs(args), { env: { WSL_UTF8: '1' } });
      return { code: r.code, stdout: '', stderr: '' };
    }
    return wsl(ctx, guestArgs(args), opts);
  },

  async stopGuest(ctx) {
    await this.guest(ctx, ['stop']);
    await wsl(ctx, ['--terminate', DISTRO]);
  },

  async removeGuest(ctx, { deleteData }) {
    await this.setAutostart(ctx, false);
    if (!deleteData) {
      await this.stopGuest(ctx);
      return;
    }
    const r = await wsl(ctx, ['--unregister', DISTRO]);
    if (r.code !== 0) {
      const e = explainWslError(`${r.stdout}\n${r.stderr}`);
      if (!(e && e.notFound)) throw failure(r, 'WSL could not remove the cseries distribution');
    }
    fs.rmSync(ctx.paths.wsl, { recursive: true, force: true });
  },

  async setAutostart(ctx, on) {
    if (on) await win.setRegistryValue(ctx.runner, win.RUN_KEY, RUN_VALUE, keepAliveCommand());
    else await win.removeRegistryValue(ctx.runner, win.RUN_KEY, RUN_VALUE);
  },

  async clearResume(ctx) {
    await win.removeRegistryValue(ctx.runner, win.RUNONCE_KEY, RUNONCE_VALUE);
  },

  unreachableHint() {
    return 'WSL is not passing 127.0.0.1:8800 through to Windows. If %USERPROFILE%\\.wslconfig sets localhostForwarding=false, remove that line, run "wsl --shutdown", and run npx cseries again.';
  },
};

module.exports = { driver, keepAliveCommand, keepAliveArgs, resumeCommand, guestArgs, DISTRO };
