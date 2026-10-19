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
//     5. otherwise ONE elevated run (win/elevated.js on Windows) that does
//        exactly the missing parts: the program into Program Files, the old
//        per-user install removed, `service install`, `set-mode networked`,
//        release-anchor when the Host's CA changed, pair, claim-anchor, and
//        the authenticator enrolment on that elevated console;
//     6. its exit code and log, recorded in state.json, so a run that stopped
//        half way is finished by the next one; then the tray for this person.
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
const { bastionCommands, EXIT_WORDS } = require('./bastion-commands');
const pairing = require('./pairing');
const win = require('./win/windows');
const elevated = require('./win/elevated');

const MANIFEST_URL = 'https://privatae.ai/bastion/version.json';

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

const NOT_YET = 'Installing the bastion as the system service is built for Windows so far; macOS and Linux come with their VM drivers.';

// The same install on macOS and Linux, as data: the program where the
// bastion's own install.sh puts it for a networked install, then the same
// bastion commands Windows runs (bastion-commands.js). Not run yet.
function posixPlan(platform, paths, plan, download) {
  const program = paths.serviceExe;
  const caFile = path.posix.join(path.posix.dirname(program), 'host-ca.pem');
  const prelude = [];
  let env;
  let restart;
  if (platform === 'darwin') {
    // install.sh:100-124 and :169-175: a root-owned folder, root:wheel 755,
    // and launchd's daemon from `service install`.
    const dir = path.posix.dirname(program);
    prelude.push(['mkdir', '-p', dir], ['chown', 'root:wheel', path.posix.dirname(dir), dir], ['chmod', '755', path.posix.dirname(dir), dir]);
    if (download) prelude.push(['install', '-o', 'root', '-g', 'wheel', '-m', '755', download.file, program]);
    prelude.push([program, 'service', 'install']);
    env = { HOME: '/var/root' };
    restart = ['launchctl', 'kickstart', '-k', 'system/ai.privatae.bastion'];
  } else {
    // install.sh:127-158: `service install` does nothing on Linux, so the
    // unit is install.sh's, root with HOME=/root.
    if (download) prelude.push(['install', '-o', 'root', '-g', 'root', '-m', '755', download.file, program]);
    prelude.push(['write', '/etc/systemd/system/privatae-bastion.service', linuxUnit(program)]);
    prelude.push(['systemctl', 'daemon-reload'], ['systemctl', 'enable', '--now', 'privatae-bastion']);
    env = { HOME: '/root' };
    restart = ['systemctl', 'restart', 'privatae-bastion'];
  }
  return { program, env, prelude, commands: bastionCommands(plan, caFile), restart };
}

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
  const later = async () => {
    throw new CliError(NOT_YET, '');
  };
  return {
    serviceState: async () => 'missing',
    elevate: later,
    startTray() {},
    plan: (plan, download) => posixPlan(ctx.platform, ctx.paths, plan, download),
  };
}

const FOREIGN_SERVICE = [
  'This PC already runs the bastion as a Windows service that this command did not install (a networked bastion an administrator installed), and a machine has one bastion.',
  'Ask the administrator to pair it with this Host, or remove that service, then run npx cseries again.',
];

// Step 1-3.
async function prepare(ctx) {
  const out = ctx.out;
  const st = bastionState(ctx);
  ctx.bastionService = await osOps(ctx).serviceState();
  if (ctx.bastionService !== 'missing' && !st.service) throw new CliError(...FOREIGN_SERVICE);
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
    oldInstall: fs.existsSync(ctx.paths.bastionExe),
    ownerNeeded: !st.owner,
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

function describePlan(out, p) {
  out.detail('The bastion runs as this machine\'s system service, so nothing running as you can replace or stop it.');
  out.detail('Windows asks for an administrator\'s permission once, for all of it:');
  if (p.install) out.detail(`  - install the bastion ${p.install.version} under Program Files`);
  if (p.oldInstall) out.detail('  - remove the per-user bastion an earlier release installed');
  if (p.serviceInstall) out.detail('  - register it as the system service PrivataeBastion');
  if (p.ownerNeeded) out.detail('  - record your Windows account as the only one whose tray answers approvals');
  if (p.release) out.detail(`  - release its anchor to ${p.release}, the Host whose data was deleted`);
  if (p.pair) out.detail('  - pair it with this Host and anchor it there');
  else if (p.anchor) out.detail('  - anchor it to this Host');
  if (p.enroll) {
    out.detail('  - enrol your authenticator app for approvals: the bastion shows a QR code in the');
    out.detail('    administrator window; scan it, then type one code from the app there');
  }
}

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
    if (ctx.platform === 'win32') describePlan(ctx.out, plan);
    const r = await ops.elevate(plan);
    record(ctx, plan, r.log);
    if (r.declined) {
      throw new CliError('The bastion was not set up: the Windows permission prompt was declined. The Host works without it meanwhile.',
        'Run npx cseries again and choose Yes when Windows asks.');
    }
    if (r.code !== 0) {
      const err = r.log.entries.filter((e) => e.key === 'error').map((e) => e.value).pop();
      const why = EXIT_WORDS[r.code] || `the elevated step ended with exit code ${r.code}`;
      const detail = err || r.stderr || (r.log.ours ? '' : 'it left no log');
      throw new CliError(`The bastion is not fully set up: ${why}${detail ? ` (${detail})` : ''}.`,
        `Run npx cseries again: what was done is kept, and only what is left is asked for. The log is ${ctx.paths.installLog}.`);
    }
    const st = bastionState(ctx);
    ctx.out.detail(`The bastion runs as this machine's service, anchored to ${st.host || 'this Host'}.`);
    ctx.out.detail('Approve what the Host asks for in the bastion\'s icon in the notification area, with a code from your authenticator app.');
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
  posixPlan,
  fetchManifest,
  checkMinVersion,
  chooseBinary,
  installedVersion,
  osOps,
  bastionState,
  MANIFEST_URL,
  NOT_YET,
};
