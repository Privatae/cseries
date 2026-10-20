'use strict';

// The bastion on the machine: the published privatae-bastion, verified, run as
// this machine's system service (so an agent running as the person cannot
// replace or stop it), paired and anchored to the Host in the VM, with the
// person's authenticator enrolled for approvals. Two steps:
//
//   getStep ("Getting the bastion"), early and unelevated, so a bad release
//   stops the run before anything else is installed:
//     1. a PrivataeBastion service this command did not install is refused
//        (a machine has one bastion; one it installed is updated);
//     2. version.json from privatae.ai and its signature, verified against
//        the bastion's pinned release keys (verify.js), and refused when older
//        than this release's bastion.min_version;
//     3. this machine's binary, downloaded into this command's downloads
//        folder and accepted only with the manifest's sha256, unless the
//        installed service program already is it (or is newer and meets the
//        floor: a bastion updates itself).
//
//   installStep ("Installing and pairing the bastion"), after the Host is up:
//     4. a pairing token from the Host (pairing.js), and from state.json, the
//        service's state and step 3, what is missing. Nothing missing: done,
//        no prompt.
//     5. otherwise ONE elevated run (win/elevated.js on Windows, one sudo with
//        posix/elevated.js on macOS and Linux) that does exactly the missing
//        parts: the program into Program Files (macOS: install.sh's root-only
//        folder; Linux: /usr/local/bin), the old per-user install removed
//        (Windows), `service install` (Linux: install.sh's systemd unit),
//        `set-mode networked`, release-anchor when the Host's CA changed,
//        pair (the token on its standard input, `pair -`, never in an
//        argv), claim-anchor, and the authenticator enrolment on that
//        elevated console (macOS and Linux: this terminal, --console-only on
//        every OS);
//     6. its exit code and log, recorded in state.json, so a run that stopped
//        half way is finished by the next one; then the tray for this person
//        (Windows; macOS and Linux have no approvals lane for the person yet,
//        so approvals are answered from an administrator's terminal).
//
// state.json "bastion": {service, owner, host, hostCa, anchored, enrolled}:
// the service is one this command installed; owner, that this account is
// recorded as the one whose tray answers approvals; host is the pairing's
// name; hostCa the sha256 of the Host CA it was paired with.

const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { CliError } = require('./errors');
const { signatureURL, verifySignedManifest, BASTION_RELEASE_KEYS } = require('./verify');
const { compare, isVersion } = require('./version');
const { bastionBinaryKey } = require('./platform');
const { bastionCommands, EXIT, EXIT_WORDS } = require('./bastion-commands');
const pairing = require('./pairing');
const win = require('./win/windows');
const elevated = require('./win/elevated');
const posixElevated = require('./posix/elevated');

const MANIFEST_URL = 'https://privatae.ai/bastion/version.json';
const SUDO_PATHS = ['/usr/bin/sudo', '/bin/sudo', '/run/wrappers/bin/sudo'];
const SUDO_PROMPT = '[cseries] sudo password for %u: ';

async function fetchManifest(ctx) {
  let body;
  let sig;
  try {
    body = await ctx.net.fetchSmall(MANIFEST_URL);
  } catch (err) {
    throw new CliError(`Could not fetch the bastion's release manifest from ${MANIFEST_URL}: ${err.message}`,
      'Check this machine\'s internet connection and run the command again.');
  }
  const sigURL = signatureURL(MANIFEST_URL, body);
  try {
    sig = await ctx.net.fetchSmall(sigURL, { maxBytes: 4096 });
  } catch (err) {
    throw new CliError(`Could not fetch the signature for the bastion's release manifest (${err.message}). Nothing was installed.`,
      'Run the command again in a few minutes: a release that was just published can take a moment to reach every server.');
  }
  try {
    return verifySignedManifest('bastion', body, sig.toString('utf8'), ctx.bastionKeys || BASTION_RELEASE_KEYS);
  } catch (err) {
    throw new CliError(`The bastion's release manifest failed its signature check: ${err.message}. Nothing was installed.`,
      'Run the command again later. If it keeps failing, something between this machine and privatae.ai is changing what it serves.');
  }
}

function checkMinVersion(version, min) {
  if (!isVersion(version)) {
    throw new CliError(`The bastion's release manifest names a version this command cannot read (${version}).`, '');
  }
  if (compare(version, min) < 0) {
    throw new CliError(
      `The published bastion is version ${version}, and this C-Series needs version ${min} or newer. Nothing was installed.`,
      `A new enough bastion has not been published yet. Run npx cseries again once ${MANIFEST_URL} names ${min} or later.`);
  }
}

function chooseBinary(manifest, platform, arch) {
  const key = bastionBinaryKey(platform, arch);
  const b = (manifest.binaries || {})[key];
  if (!b || typeof b.sha256 !== 'string' || !/^[0-9a-f]{64}$/.test(b.sha256) ||
      typeof b.url !== 'string' || !/^https:\/\/\S+$/.test(b.url)) {
    throw new CliError(`The bastion release ${manifest.version} has no build for this machine (${key}).`, '');
  }
  return { key, url: b.url, sha256: b.sha256 };
}

async function installedVersion(ctx, exe = ctx.paths.serviceExe) {
  const r = await ctx.runner.run(exe, ['version'], { timeoutMs: 15000 });
  if (r.code !== 0) return null;
  const m = /privatae-bastion\s+v?(\S+)/.exec(r.stdout.toString('utf8'));
  return m && isVersion(m[1]) ? m[1] : null;
}

function bastionState(ctx) {
  const s = ctx.state.get('bastion');
  return s && typeof s === 'object' ? s : {};
}

// install.sh's systemd unit for a networked bastion (deploy/install.sh,
// the Linux branch): root, HOME=/root, restarted whenever it stops, which is
// also how a self-update takes effect.
function linuxUnit(program) {
  return [
    '[Unit]',
    'Description=Privatae bastion - your secure door into your private mesh (tunnel + lanes)',
    'After=network-online.target',
    'Wants=network-online.target',
    '[Service]',
    'Type=simple',
    'Environment=HOME=/root',
    `ExecStart=${program} run`,
    'Restart=always',
    'RestartSec=2',
    'LimitNOFILE=65536',
    '[Install]',
    'WantedBy=multi-user.target',
    '',
  ].join('\n');
}

// The OS half: the service's state, the one elevated run, and the tray.
function osOps(ctx) {
  if (ctx.platform === 'win32') {
    return {
      async serviceState() {
        try {
          return await win.bastionServiceState(ctx.runner);
        } catch (err) {
          throw new CliError(`Could not ask Windows whether the bastion runs as a service: ${err.message}`,
            'Run npx cseries again.');
        }
      },
      async elevate(plan) {
        // This account's SID: the bastion records it as the one account whose
        // tray answers approvals (approvals-owner), and the old per-user PATH
        // entry is removed through it. Without it nothing is asked for.
        const sid = await win.currentUserSid(ctx.runner);
        if (!sid) {
          throw new CliError('The bastion was not set up: this Windows account\'s SID could not be read, and the bastion records it as the one account that answers approvals. Nothing was changed.',
            'Run npx cseries again.');
        }
        if (plan.hostCa) {
          fs.mkdirSync(path.dirname(ctx.paths.hostCa), { recursive: true });
          fs.writeFileSync(ctx.paths.hostCa, plan.hostCa.pem);
        }
        const nonce = crypto.randomBytes(8).toString('hex');
        fs.mkdirSync(path.dirname(ctx.paths.installLog), { recursive: true });
        fs.rmSync(ctx.paths.installLog, { force: true });
        const script = elevated.bastionInstallScript({
          nonce,
          log: ctx.paths.installLog,
          install: plan.install,
          serviceInstall: plan.serviceInstall,
          old: { exe: ctx.paths.bastionExe, dir: ctx.paths.bastionDir, sid },
          hostCa: plan.hostCa ? { file: ctx.paths.hostCa, sha256: plan.hostCa.sha256 } : null,
          host: plan.host,
          // The owner step runs on every elevated run: the same account
          // again changes nothing, and another account is refused there.
          commands: bastionCommands({ ...plan, owner: sid }, elevated.CA_VAR),
        });
        const r = await win.elevatedScript(ctx.runner, script);
        let text = '';
        try {
          text = fs.readFileSync(ctx.paths.installLog, 'utf8');
        } catch {
          text = '';
        }
        return { ...r, log: elevated.parseInstallLog(text, nonce) };
      },
      startTray() {
        ctx.runner.detached(ctx.paths.serviceExe, ['tray']);
      },
    };
  }
  return posixOps(ctx);
}

function findSudo(ctx) {
  const list = ctx.sudoPaths || SUDO_PATHS;
  return list.find((p) => {
    try {
      fs.accessSync(p, fs.constants.X_OK);
      return true;
    } catch {
      return false;
    }
  }) || null;
}

// macOS and Linux. Approvals have no lane for the signed-in person here yet
// (the bastion's tray and its approvals pipe are Windows only), so there is
// no tray to start and no approvals owner to record: approvals-owner takes a
// Windows SID.
function posixOps(ctx) {
  const darwin = ctx.platform === 'darwin';
  return {
    async serviceState() {
      if (!fs.existsSync(ctx.paths.serviceUnit)) return 'missing';
      if (darwin) {
        const r = await ctx.runner.run('/bin/ps', ['-axo', 'user=,command='], { timeoutMs: 15000 });
        const want = `${ctx.paths.serviceExe} run`;
        const running = r.stdout.toString('utf8').split('\n').some((l) => {
          const t = l.trim();
          return t.startsWith('root ') && t.slice(5).trim() === want;
        });
        return running ? 'running' : 'stopped';
      }
      const r = await ctx.runner.run('systemctl', ['is-active', 'privatae-bastion'], { timeoutMs: 15000 });
      return r.stdout.toString('utf8').trim() === 'active' ? 'running' : 'stopped';
    },
    async elevate(plan) {
      const sudo = findSudo(ctx);
      if (!sudo) {
        throw new CliError('The bastion was not set up: it is installed as this machine\'s system service with sudo, and sudo is not here. Nothing was changed.',
          'Install sudo, or run the bastion\'s own installer as root (https://privatae.ai/bastion/install.sh), then run npx cseries again.');
      }
      if (plan.hostCa) {
        fs.mkdirSync(path.dirname(ctx.paths.hostCa), { recursive: true });
        fs.writeFileSync(ctx.paths.hostCa, plan.hostCa.pem);
      }
      const nonce = crypto.randomBytes(8).toString('hex');
      const script = posixElevated.bastionInstallScript({
        platform: ctx.platform,
        nonce,
        program: ctx.paths.serviceExe,
        unit: linuxUnit(ctx.paths.serviceExe),
        install: plan.install,
        serviceInstall: plan.serviceInstall,
        hostCa: plan.hostCa ? { file: ctx.paths.hostCa, sha256: plan.hostCa.sha256 } : null,
        host: plan.host,
        commands: bastionCommands({ ...plan, owner: undefined }, posixElevated.CA_VAR),
      });
      const r = await ctx.runner.run(sudo, ['-p', SUDO_PROMPT, '/bin/sh', '-s'], { input: script });
      const out = r.stdout.toString('utf8');
      fs.mkdirSync(path.dirname(ctx.paths.installLog), { recursive: true });
      fs.writeFileSync(ctx.paths.installLog, out);
      const log = elevated.parseInstallLog(out, nonce);
      // sudo refused before the script ran: no password, a wrong one three
      // times, no terminal to ask on, or this account may not use sudo.
      return { code: r.code, stderr: r.stderr.toString('utf8').trim(), declined: r.code !== 0 && !log.ours, log };
    },
    startTray() {},
  };
}

function foreignService(ctx) {
  const what = ctx.platform === 'win32' ? 'This PC already runs the bastion as a Windows service' : 'This machine already runs the bastion as a system service';
  return [
    `${what} that this command did not install (a networked bastion an administrator installed), and a machine has one bastion.`,
    'Ask the administrator to pair it with this Host, or remove that service, then run npx cseries again.',
  ];
}

// Step 1-3.
async function prepare(ctx) {
  const out = ctx.out;
  const st = bastionState(ctx);
  ctx.bastionService = await osOps(ctx).serviceState();
  if (ctx.bastionService !== 'missing' && !st.service) throw new CliError(...foreignService(ctx));
  const manifest = await fetchManifest(ctx);
  const min = ctx.releaseInfo.release.bastion.min_version;
  checkMinVersion(manifest.version, min);
  const bin = chooseBinary(manifest, ctx.platform, ctx.arch);
  out.detail(`Published bastion: ${manifest.version} (signature verified).`);

  const exe = ctx.paths.serviceExe;
  let keep = false;
  if (fs.existsSync(exe)) {
    if ((await ctx.net.fileSha256(exe)) === bin.sha256) {
      out.detail(`The bastion ${manifest.version} is installed.`);
      keep = true;
    } else {
      // A bastion updates itself, and may be ahead of the manifest; a newer
      // one that meets this release's floor is left as it is.
      const have = await installedVersion(ctx, exe);
      if (have && compare(have, manifest.version) > 0 && compare(have, min) >= 0) {
        out.detail(`The bastion ${have} is installed, newer than the published one; keeping it.`);
        keep = true;
      }
    }
  }
  let download = null;
  if (!keep) {
    const file = ctx.paths.join(ctx.paths.downloads, `privatae-bastion-${manifest.version}${ctx.platform === 'win32' ? '.exe' : ''}`);
    await ctx.net.download(bin.url, file, { sha256: bin.sha256, out, label: `the bastion ${manifest.version}` });
    download = { file, sha256: bin.sha256, version: manifest.version };
    out.detail(`The bastion ${manifest.version} is downloaded and verified; it is installed once the Host is up.`);
  }
  ctx.bastion = { version: manifest.version, download };
  return { version: manifest.version };
}

// Step 4: what the elevated run has to do. Everything false and null: nothing.
async function planFor(ctx, token) {
  const st = bastionState(ctx);
  const svc = ctx.bastionService || (await osOps(ctx).serviceState());
  const download = (ctx.bastion && ctx.bastion.download) || null;
  const pair = !st.host || st.hostCa !== token.caSha256;
  return {
    install: download,
    serviceInstall: Boolean(download) || svc === 'missing',
    restart: svc !== 'running',
    // Windows only: earlier releases installed a per-user bastion there, and
    // approvals-owner records a Windows account (approvals_owner.go).
    oldInstall: ctx.platform === 'win32' && fs.existsSync(ctx.paths.bastionExe),
    ownerNeeded: ctx.platform === 'win32' && !st.owner,
    // The Host this command paired before is gone (a new CA): its anchor is
    // released first, since 0.53 refuses to re-pair over it.
    release: pair && st.host ? st.host : null,
    pair: pair ? { enroll: token.enroll, token: token.token } : null,
    hostCa: pair ? { pem: token.hostCaPem, sha256: token.caSha256 } : null,
    anchor: pair || !st.anchored,
    enroll: pair || !st.enrolled,
    host: pair ? null : st.host,
  };
}

function planNeeded(p) {
  return Boolean(p.install || p.serviceInstall || p.restart || p.oldInstall || p.ownerNeeded || p.pair || p.anchor || p.enroll);
}

// Step 6: what the log says was done, into state.json, line by line, so a run
// that stopped half way leaves exactly what is left for the next one.
function record(ctx, plan, log) {
  const st = { ...bastionState(ctx) };
  for (const { key, value } of log.entries) {
    switch (key) {
      case 'service':
        st.service = true;
        break;
      case 'owner':
        st.owner = true;
        break;
      case 'released':
        delete st.host;
        delete st.hostCa;
        st.anchored = false;
        st.enrolled = false;
        break;
      case 'paired_host':
        st.host = value;
        st.hostCa = plan.hostCa ? plan.hostCa.sha256 : undefined;
        st.anchored = false;
        st.enrolled = false;
        break;
      case 'anchored':
        st.anchored = true;
        break;
      case 'enrolled':
        st.enrolled = true;
        break;
      default:
    }
  }
  ctx.state.set('bastion', st);
  // The per-user bastion's pairing, from releases before the system service.
  if (st.service && ctx.state.get('pairedHostCa') !== undefined) ctx.state.set('pairedHostCa', undefined);
}

const SERVICE_NAME = {
  win32: 'the system service PrivataeBastion',
  darwin: 'the system service ai.privatae.bastion (launchd)',
  linux: 'the system service privatae-bastion (systemd)',
};

function describePlan(out, p, ctx) {
  const win = ctx.platform === 'win32';
  out.detail('The bastion runs as this machine\'s system service, so nothing running as you can replace or stop it.');
  out.detail(win ? 'Windows asks for an administrator\'s permission once, for all of it:' : 'sudo asks for your password once, for all of it:');
  if (p.install) out.detail(`  - install the bastion ${p.install.version} ${win ? 'under Program Files' : `in ${ctx.paths.serviceDir}`}`);
  if (p.oldInstall) out.detail('  - remove the per-user bastion an earlier release installed');
  if (p.serviceInstall) out.detail(`  - register it as ${SERVICE_NAME[ctx.platform] || 'the system service'}`);
  if (p.ownerNeeded) out.detail('  - record your Windows account as the only one whose tray answers approvals');
  if (p.release) out.detail(`  - release its anchor to ${p.release}, the Host whose data was deleted`);
  if (p.pair) out.detail('  - pair it with this Host and anchor it there');
  else if (p.anchor) out.detail('  - anchor it to this Host');
  if (p.enroll) {
    if (win) {
      out.detail('  - enrol your authenticator app for approvals: the bastion shows a QR code in the');
      out.detail('    administrator window; scan it, then type one code from the app there');
    } else {
      out.detail('  - enrol your authenticator app for approvals: the bastion shows a QR code in');
      out.detail('    this terminal; scan it, then type one code from the app here');
    }
  }
}

// What a failed exit code means on macOS and Linux, where the program goes
// somewhere other than Program Files.
const POSIX_EXIT_WORDS = {
  [EXIT.binaryHash]: 'the bastion copied into place is not the verified download',
  [EXIT.install]: 'the bastion could not be put in place',
};

const installStep = {
  title: 'Installing and pairing the bastion',
  soft: true,
  doneText: 'The bastion runs as this machine\'s service, anchored to this Host, with your authenticator enrolled.',

  async done(ctx) {
    if (!ctx.bastion) await prepare(ctx);
    ctx.pairToken = await pairing.mintToken(ctx);
    ctx.bastionPlan = await planFor(ctx, ctx.pairToken);
    if (planNeeded(ctx.bastionPlan)) return false;
    osOps(ctx).startTray();
    return true;
  },

  async run(ctx) {
    const ops = osOps(ctx);
    const plan = ctx.bastionPlan || (await planFor(ctx, ctx.pairToken || (await pairing.mintToken(ctx))));
    const win = ctx.platform === 'win32';
    describePlan(ctx.out, plan, ctx);
    const r = await ops.elevate(plan);
    record(ctx, plan, r.log);
    if (r.declined) {
      if (win) {
        throw new CliError('The bastion was not set up: the Windows permission prompt was declined. The Host works without it meanwhile.',
          'Run npx cseries again and choose Yes when Windows asks.');
      }
      throw new CliError(`The bastion was not set up: sudo did not run it${r.stderr ? ` (${r.stderr.split('\n').pop()})` : ''}. The Host works without it meanwhile.`,
        'Run npx cseries again in a terminal, as an account that may use sudo, and enter your password when sudo asks.');
    }
    if (r.code !== 0) {
      const err = r.log.entries.filter((e) => e.key === 'error').map((e) => e.value).pop();
      const why = (!win && POSIX_EXIT_WORDS[r.code]) || EXIT_WORDS[r.code] || `the elevated step ended with exit code ${r.code}`;
      const detail = err || r.stderr || (r.log.ours ? '' : 'it left no log');
      throw new CliError(`The bastion is not fully set up: ${why}${detail ? ` (${detail})` : ''}.`,
        `Run npx cseries again: what was done is kept, and only what is left is asked for. The log is ${ctx.paths.installLog}.`);
    }
    const st = bastionState(ctx);
    ctx.out.detail(`The bastion runs as this machine's service, anchored to ${st.host || 'this Host'}.`);
    if (win) {
      ctx.out.detail('Approve what the Host asks for in the bastion\'s icon in the notification area, with a code from your authenticator app.');
    } else {
      const exe = posixElevated.shq(ctx.paths.serviceExe);
      ctx.out.detail('When the Host asks for an approval, answer it from a terminal with a code from your authenticator app:');
      ctx.out.detail(`  sudo -H ${exe} approvals          what is waiting`);
      ctx.out.detail(`  sudo -H ${exe} approve <id>       approve one`);
    }
    ops.startTray();
  },
};

const getStep = {
  title: 'Getting the bastion',
  run: (ctx) => prepare(ctx),
};

module.exports = {
  getStep,
  installStep,
  prepare,
  planFor,
  planNeeded,
  record,
  describePlan,
  linuxUnit,
  findSudo,
  fetchManifest,
  checkMinVersion,
  chooseBinary,
  installedVersion,
  osOps,
  bastionState,
  MANIFEST_URL,
  SUDO_PATHS,
};
