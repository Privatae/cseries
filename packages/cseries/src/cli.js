'use strict';

// npx cseries [command]
//
// With no command: install what is missing, start what is stopped, update to
// the release this package pins, and open the browser. Every step checks
// before it acts, so running it again repairs and changes nothing else.

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const readline = require('node:readline');
const { CliError } = require('./errors');
const { Output } = require('./output');
const { systemRunner } = require('./run');
const release = require('./release');
const net = require('./net');
const { State } = require('./state');
const { paths, machineArch } = require('./platform');
const { selectDriver, requireSupported } = require('./vm/driver');
const { runSteps } = require('./steps');
const preflight = require('./preflight');
const bastion = require('./bastion');
const host = require('./host');
const setup = require('./setup');

const HELP = `npx cseries [command]

  (none)       install C-Series if it is not here, otherwise start it and update
               it to this release; then open it in the browser
  start        start the Host
  stop         stop the Host and its Linux system
  status       what is installed and running
  logs [host|postgres] [--follow] [--lines N]
               the Host's (or its database's) log
  update       update to this release without opening the browser
  open         open C-Series in the browser, with the setup code if it needs one
  uninstall [--delete-data]
               stop C-Series and stop starting it at login. The Host's data is
               kept unless --delete-data is given, which deletes it for good
  help, --version
`;

function makePrompt(stdin = process.stdin, stdout = process.stdout) {
  return (question) => new Promise((resolve) => {
    if (!stdin.isTTY) {
      resolve(false);
      return;
    }
    const rl = readline.createInterface({ input: stdin, output: stdout });
    rl.question(question, (answer) => {
      rl.close();
      resolve(/^y(es)?$/i.test(answer.trim()));
    });
  });
}

function makeContext(overrides = {}) {
  const platform = overrides.platform || process.platform;
  const env = overrides.env || process.env;
  const p = overrides.paths || paths(platform, env);
  const ctx = {
    platform,
    env,
    arch: overrides.arch || machineArch(platform, env),
    paths: p,
    out: overrides.out || new Output(),
    runner: overrides.runner || systemRunner(),
    net: overrides.net || net,
    sleep: overrides.sleep || ((ms) => new Promise((r) => setTimeout(r, ms))),
    prompt: overrides.prompt || makePrompt(),
    driver: overrides.driver !== undefined ? overrides.driver : selectDriver(platform),
    binPath: overrides.binPath || path.join(__dirname, '..', 'bin', 'cseries.js'),
    bastionKeys: overrides.bastionKeys,
    portState: overrides.portState,
    osRelease: overrides.osRelease || os.release(),
    // os.availableParallelism is Node 18.14 and later; the package takes any 18.
    machine: overrides.machine || {
      totalMem: os.totalmem(),
      cpus: typeof os.availableParallelism === 'function' ? os.availableParallelism() : os.cpus().length,
    },
  };
  ctx.state = overrides.state || new State(p.state);
  ctx.releaseInfo = overrides.releaseInfo || null;
  return ctx;
}

function loadRelease(ctx) {
  if (!ctx.releaseInfo) ctx.releaseInfo = release.load(ctx.env);
  release.requirePinned(ctx.releaseInfo);
  return ctx.releaseInfo;
}

const preflightStep = {
  title: 'Checking this machine',
  async run(ctx) {
    await ctx.driver.preflight(ctx);
    const installed = await ctx.driver.guestExists(ctx);
    const answer = installed ? await ctx.net.probe(`${setup.BASE}/api/system/health`) : null;
    await preflight.checkDisk(ctx, installed);
    await preflight.checkPorts(ctx, Boolean(answer && answer.status === 200));
  },
};

function upSteps({ open, label = 'WSL' }) {
  return [
    preflightStep,
    bastion.getStep,
    {
      title: `Setting up ${label}`,
      done: (ctx) => ctx.driver.runtimeReady(ctx),
      run: (ctx) => ctx.driver.installRuntime(ctx),
    },
    {
      title: 'Creating the C-Series Linux system',
      doneText: 'It is here.',
      done: (ctx) => ctx.driver.guestExists(ctx),
      run: (ctx) => ctx.driver.createGuest(ctx),
    },
    {
      title: 'Starting the Linux system',
      async run(ctx) {
        await ctx.driver.startGuest(ctx);
        await ctx.driver.setAutostart(ctx, true);
        if (ctx.driver.clearResume) await ctx.driver.clearResume(ctx);
      },
    },
    host.step,
    bastion.installStep,
    { title: 'Opening C-Series', run: (ctx) => setup.showSetup(ctx, { open }) },
  ];
}

async function up(ctx, { open }) {
  requireSupported(ctx.driver, ctx.platform);
  const rel = loadRelease(ctx).release;
  ctx.out.line(`C-Series ${rel.version}`);
  const r = await runSteps(upSteps({ open, label: ctx.driver.label }), ctx);
  if (r.halted) {
    ctx.out.line();
    ctx.out.line(r.message);
    return 0;
  }
  if (r.failures.length) {
    ctx.out.line();
    ctx.out.line(`Finished, except: ${r.failures.map((f) => f.step).join('; ')}. Run npx cseries again to retry.`);
    return 1;
  }
  return 0;
}

async function needInstalled(ctx) {
  requireSupported(ctx.driver, ctx.platform);
  if (!(await ctx.driver.guestExists(ctx))) {
    throw new CliError('C-Series is not installed on this machine.', 'Install it with: npx cseries');
  }
}

async function start(ctx) {
  await needInstalled(ctx);
  await ctx.driver.startGuest(ctx);
  await host.start(ctx, false);
  await setup.waitHealthy(ctx);
  await setup.showSetup(ctx, { open: false });
  return 0;
}

async function stop(ctx) {
  await needInstalled(ctx);
  await ctx.driver.stopGuest(ctx);
  ctx.out.line('C-Series is stopped. It starts again at your next login, or with: npx cseries start');
  return 0;
}

async function status(ctx) {
  requireSupported(ctx.driver, ctx.platform);
  const out = ctx.out;
  const exists = await ctx.driver.guestExists(ctx);
  out.line(`Linux system (${ctx.driver.label}): ${exists ? 'installed' : 'not installed'}`);
  if (exists) {
    const running = await ctx.driver.guestRunning(ctx);
    out.line(`  running:   ${running ? 'yes' : 'no'}`);
    if (running) {
      const s = await host.guestStatus(ctx);
      out.line(`  release:   ${s.release || 'unknown'} (Linux system ${s.guest || 'unknown'})`);
      out.line(`  Host:      ${s.host || 'unknown'}, database: ${s.postgres || 'unknown'}`);
      const p = await ctx.net.probe(`${setup.BASE}/api/system/health`);
      out.line(`  reachable: ${p && p.status === 200 ? `yes, ${setup.NAMED}` : 'no'}`);
    }
  }
  const exe = ctx.paths.serviceExe;
  const b = bastion.bastionState(ctx);
  if (fs.existsSync(exe)) {
    const v = await bastion.installedVersion(ctx, exe);
    out.line(`Bastion: ${v || 'installed'} (${exe})`);
    const svc = await bastion.osOps(ctx).serviceState().catch(() => 'unknown');
    out.line(`  system service: ${svc === 'missing' ? 'not installed' : svc}`);
    out.line(`  paired with this Host: ${b.hostCa ? `yes (${b.host})` : 'no'}`);
    out.line(`  anchored to it: ${b.hostCa && b.anchored ? 'yes' : 'no'}`);
    out.line(`  authenticator for approvals: ${b.hostCa && b.enrolled ? 'enrolled' : 'not enrolled'}`);
  } else {
    out.line('Bastion: not installed');
  }
  return 0;
}

async function logs(ctx, args) {
  await needInstalled(ctx);
  const which = args[0] && !args[0].startsWith('-') ? args.shift() : 'host';
  if (which !== 'host' && which !== 'postgres') throw new CliError(`logs: "${which}" is not host or postgres.`, '');
  const r = await ctx.driver.guest(ctx, ['logs', which, ...args], { inherit: true });
  return r.code === 0 ? 0 : 1;
}

async function open(ctx) {
  await needInstalled(ctx);
  await setup.waitHealthy(ctx, { timeoutMs: 30000 });
  await setup.showSetup(ctx, { open: true });
  return 0;
}

async function uninstall(ctx, args) {
  requireSupported(ctx.driver, ctx.platform);
  const deleteData = args.includes('--delete-data');
  if (deleteData) {
    const yes = args.includes('--yes') || (await ctx.prompt(
      'This deletes the Host, its memory and everything it stored, for good. Type y to go on: '));
    if (!yes) {
      ctx.out.line('Nothing was removed.');
      return 1;
    }
  }
  if (await ctx.driver.guestExists(ctx)) await ctx.driver.removeGuest(ctx, { deleteData });
  else await ctx.driver.setAutostart(ctx, false);
  if (ctx.driver.clearResume) await ctx.driver.clearResume(ctx);
  if (deleteData) {
    // The Host is gone with its data. Its pairing name stays recorded, so the
    // next npx cseries releases the bastion's anchor to it and pairs the new
    // Host (bastion.js planFor).
    const b = bastion.bastionState(ctx);
    if (b.hostCa) {
      const { hostCa, ...rest } = b;
      ctx.state.set('bastion', rest);
    }
    ctx.state.set('pairedHostCa', undefined);
    fs.rmSync(ctx.paths.hostCa, { force: true });
    ctx.out.line('C-Series and its data are removed.');
  } else {
    ctx.out.line('C-Series is stopped and no longer starts at login.');
    ctx.out.line(`Its data is kept in the ${ctx.driver.label} system "cseries"; npx cseries brings it back,`);
    ctx.out.line('and npx cseries uninstall --delete-data deletes it.');
  }
  ctx.out.line('The bastion stays installed, since it can serve other Hosts too.');
  return 0;
}

function packageVersion() {
  try {
    return JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'package.json'), 'utf8')).version;
  } catch {
    return 'unknown';
  }
}

async function main(argv, overrides = {}) {
  const ctx = makeContext(overrides);
  const args = [...argv];
  const cmd = args.shift() || '';
  try {
    switch (cmd) {
      case '': return await up(ctx, { open: true });
      case 'update': return await up(ctx, { open: false });
      case 'start': return await start(ctx);
      case 'stop': return await stop(ctx);
      case 'status': return await status(ctx);
      case 'logs': return await logs(ctx, args);
      case 'open': return await open(ctx);
      case 'uninstall': return await uninstall(ctx, args);
      case 'help': case '--help': case '-h':
        ctx.out.line(HELP);
        return 0;
      case 'version': case '--version': case '-v':
        ctx.out.line(packageVersion());
        return 0;
      default:
        throw new CliError(`"${cmd}" is not a cseries command.`, HELP);
    }
  } catch (err) {
    if (err instanceof CliError) {
      ctx.out.error(err.message, err.next);
      return err.exitCode;
    }
    ctx.out.error(`cseries stopped on an error it did not expect: ${err && err.stack ? err.stack : err}`,
      'Please report it at https://github.com/Privatae/cseries/issues with the lines above.');
    return 1;
  }
}

module.exports = { main, makeContext, upSteps, HELP };
