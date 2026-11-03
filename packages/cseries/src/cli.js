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
const { InstallLog } = require('./log');
const { paths, machinePaths, machineArch } = require('./platform');
const scope = require('./scope');
const machine = require('./vm/lima/machine');
const { selectDriver, requireSupported } = require('./vm/driver');
const { runSteps } = require('./steps');
const preflight = require('./preflight');
const bastion = require('./bastion');
const host = require('./host');
const setup = require('./setup');
const machineFirewall = require('./vm/machine-firewall');
const move = require('./move');
const { decodeWslConfig, removeWslConfigLines } = require('./vm/wslconfig');

const HELP = `npx cseries [command]

  (none)       install C-Series if it is not here, otherwise start it and update
               it to this release; then open it in the browser. On Linux it is
               installed for the whole machine, through sudo; --just-me
               installs it for your account only. An install for your account
               only is offered the move to the whole machine once;
               --whole-machine moves it without asking
  start        start the Host
  stop         stop the Host and its Linux system
  status       what is installed and running
  logs [host|postgres] [--follow] [--lines N]
               the Host's (or its database's) log
  update       update to this release without opening the browser
  open         open C-Series in the browser, with the setup code if it needs one
  uninstall [--delete-data [--keep-bastion]]
               stop C-Series and stop starting it. The Host's data is kept
               unless --delete-data is given, which deletes it for good, with
               everything else this command put on the machine: the bastion it
               installed (its pairings, keys and audit log; on Windows also its
               tray and firewall rules, through one administrator prompt;
               --keep-bastion keeps it), its folders, and on Linux the system
               account the VM ran as. What else it installed (Linux: QEMU's
               packages; Windows: WSL, its .wslconfig lines) is named, and
               removed only when its prompt is answered y
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
  const uid = overrides.uid !== undefined ? overrides.uid : (typeof process.getuid === 'function' ? process.getuid() : null);
  // A test that hands its own paths says where everything goes; otherwise a
  // run as root on Linux is the whole machine's install (scope.js).
  const machineScope = overrides.paths ? Boolean(overrides.paths.vmUser) : (platform === 'linux' && uid === 0);
  const p = overrides.paths || (machineScope ? machinePaths() : paths(platform, env));
  if (machineScope && !overrides.paths) {
    // Root's own umask may be 077; the VM's account reads Lima's release and
    // the files handed to limactl create, so what this run makes is 755/644.
    process.umask(0o022);
  }
  const ctx = {
    platform,
    env,
    uid,
    scope: machineScope ? 'machine' : 'user',
    arch: overrides.arch || machineArch(platform, env),
    paths: p,
    out: overrides.out || new Output(),
    runner: overrides.runner || systemRunner(),
    net: overrides.net || net,
    sleep: overrides.sleep || ((ms) => new Promise((r) => setTimeout(r, ms))),
    prompt: overrides.prompt || makePrompt(),
    // Whether a question can be asked here at all (move.js offer).
    isTTY: overrides.isTTY !== undefined ? overrides.isTTY : Boolean(process.stdin.isTTY),
    driver: overrides.driver !== undefined ? overrides.driver : selectDriver(platform),
    binPath: overrides.binPath || path.join(__dirname, '..', 'bin', 'cseries.js'),
    // This Node, which the run as root (scope.js) is started with.
    nodePath: overrides.nodePath || process.execPath,
    bastionKeys: overrides.bastionKeys,
    portState: overrides.portState,
    freeBytes: overrides.freeBytes,
    hostname: overrides.hostname || os.hostname(),
    // This machine's addresses, for the machine wall (vm/machine-firewall.js).
    netInterfaces: overrides.netInterfaces || (() => os.networkInterfaces()),
    // Where systemd keeps each user's runtime folder (vm/lima.js autostart).
    userRunDir: overrides.userRunDir,
    // Where to look for sudo; unset, bastion.js looks in SUDO_PATHS.
    sudoPaths: overrides.sudoPaths,
    // Where to look for runuser (the whole machine's install); unset,
    // vm/lima.js looks in RUNUSER_PATHS.
    runuserPaths: overrides.runuserPaths,
    vmStartTimeoutMs: overrides.vmStartTimeoutMs,
    // This account's name, for the bastion's owner record; unset,
    // bastion.js reads it from the OS.
    accountName: overrides.accountName,
    // Where the whole machine's install is read from by a run as an account
    // (scope.js): this machine's own files; in a test, its stand-ins, and
    // none at all for a test that hands its own paths and no stand-ins.
    machineFs: overrides.machineFs !== undefined ? overrides.machineFs : (overrides.paths ? null : fs),
    osRelease: overrides.osRelease || os.release(),
    // os.availableParallelism is Node 18.14 and later; the package takes any 18.
    machine: overrides.machine || {
      totalMem: os.totalmem(),
      cpus: typeof os.availableParallelism === 'function' ? os.availableParallelism() : os.cpus().length,
    },
  };
  ctx.state = overrides.state || new State(p.state);
  ctx.log = overrides.log || new InstallLog(p.log);
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
    // Moving an account's install (move.js): its Host holds the ports until
    // its VM is stopped, later in this run.
    const answer = installed || ctx.move ? await ctx.net.probe(`${setup.BASE}/api/system/health`) : null;
    // A move's copy checks its own room (move.js copyScript).
    await preflight.checkDisk(ctx, installed || Boolean(ctx.move));
    await preflight.checkPorts(ctx, Boolean(answer && answer.status === 200));
  },
};

function upSteps({ open, label = 'WSL', moving = false }) {
  return [
    preflightStep,
    ...(moving ? [move.carryStep] : []),
    bastion.getStep,
    {
      title: `Setting up ${label}`,
      done: (ctx) => ctx.driver.runtimeReady(ctx),
      run: (ctx) => ctx.driver.installRuntime(ctx),
    },
    ...(moving ? [move.diskStep] : []),
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
    ...(moving ? [move.finishStep(removeUserInstall)] : []),
    bastion.installStep,
    { title: 'Opening C-Series', run: (ctx) => setup.showSetup(ctx, { open }) },
  ];
}

async function up(ctx, { open }) {
  requireSupported(ctx.driver, ctx.platform);
  const rel = loadRelease(ctx).release;
  ctx.out.line(`C-Series ${rel.version}`);
  // The whole machine's install is marked unfinished before anything of it
  // is made, and is one only once this run has finished (scope.js
  // UNFINISHED). An install that is one already (an update) stays one.
  // A record that holds only the bastion's, kept by an uninstall, is no
  // install either: this run is a new one, which reuses that bastion.
  const machineRun = ctx.scope === 'machine';
  if (machineRun && ctx.state.get(scope.UNFINISHED)) {
    ctx.out.line('An install of C-Series for the whole machine started here before and did not finish. This run finishes it.');
  } else if (machineRun && (!fs.existsSync(ctx.paths.state) || keptBastionHere(ctx))) {
    ctx.state.set(scope.UNFINISHED, true);
  }
  adoptKeptBastion(ctx);
  let r;
  try {
    r = await runSteps(upSteps({ open, label: ctx.driver.label, moving: Boolean(ctx.move) }), ctx);
  } catch (err) {
    // A move that stopped before the moved Host was checked is undone.
    if (ctx.move && !ctx.move.done) await move.rollback(ctx, removeMachineInstall);
    throw err;
  }
  // Every step ran: the Host runs from the machine's install. A soft step
  // that failed (the bastion's) is retried by the next run, which takes the
  // machine as installed.
  if (machineRun && ctx.state.get(scope.UNFINISHED)) ctx.state.set(scope.UNFINISHED, undefined);
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

// The whole machine's record holds only the bastion's, kept by its uninstall
// (scope.js keptBastionRecord). -> the bastion's record, or null.
function keptBastionHere(ctx) {
  return scope.keptBastionRecord(ctx.state.data, { unit: ctx.paths.autostart, lima: ctx.paths.lima });
}

// Linux, a new install for this account (--just-me) where this account has
// no record of the bastion: the one the whole machine's install put here and
// its uninstall kept (scope.js keptBastionRecord) is this command's own, so
// it is reused and paired with this Host, not refused as a service an
// administrator installed, and its owner is recorded again, for this
// account. The machine's record must not name it after that: a later
// whole-machine install would pair it with another Host, and the machine's
// uninstall would remove it. So the takeover is the bastion step's one sudo
// run (bastion.js planFor machineRecord): it clears the machine's record
// first, and only a run that cleared it writes the bastion into this
// account's state.json. Until then the record is held here only, so a run
// whose sudo did not clear it takes nothing over.
function adoptKeptBastion(ctx) {
  if (ctx.scope !== 'user' || ctx.platform !== 'linux' || !ctx.machineFs) return;
  if (bastion.bastionState(ctx).service) return;
  const kept = scope.machineKeptRecord(ctx.machineFs);
  if (!kept) return;
  const { owner, hostCa, ...rest } = kept.bastion;
  ctx.keptBastion = { record: rest, sha256: kept.sha256, vmUserCreated: kept.vmUserCreated };
  ctx.out.detail(`The bastion C-Series installed for the whole machine is here, and its record was kept (${machinePaths().state}): this install takes it over and pairs it with its Host.`);
}

// What a whole-machine install that did not finish (scope.js UNFINISHED)
// is said as, and what can be done with it.
const UNFINISHED_TEXT = 'An install of C-Series for the whole machine started on this machine and did not finish, so no Host runs from it.';
const FINISH_TEXT = 'To finish it: npx cseries';
const CLEAN_TEXT = `To remove what it left: npx cseries uninstall --delete-data ${move.WHOLE_MACHINE}`;

function unfinishedHere(ctx) {
  return ctx.scope === 'machine' && Boolean(ctx.state.get(scope.UNFINISHED));
}

async function needInstalled(ctx) {
  requireSupported(ctx.driver, ctx.platform);
  if (unfinishedHere(ctx)) throw new CliError(UNFINISHED_TEXT, `${FINISH_TEXT}\n${CLEAN_TEXT}`);
  if (!(await ctx.driver.guestExists(ctx))) {
    throw new CliError('C-Series is not installed on this machine.', 'Install it with: npx cseries');
  }
}

async function start(ctx) {
  await needInstalled(ctx);
  await ctx.driver.startGuest(ctx);
  // The machine wall with this machine's addresses as they are now (an
  // address it gained since the last run, say), before the Host starts.
  await machineFirewall.apply(ctx);
  await host.start(ctx, false);
  await setup.waitHealthy(ctx);
  await setup.showSetup(ctx, { open: false });
  return 0;
}

async function stop(ctx) {
  await needInstalled(ctx);
  await ctx.driver.stopGuest(ctx);
  ctx.out.line(ctx.scope === 'machine'
    ? 'C-Series is stopped. It starts again when this machine starts, or with: npx cseries start'
    : 'C-Series is stopped. It starts again at your next login, or with: npx cseries start');
  return 0;
}

async function status(ctx) {
  requireSupported(ctx.driver, ctx.platform);
  const out = ctx.out;
  const unfinished = unfinishedHere(ctx);
  const exists = !unfinished && await ctx.driver.guestExists(ctx);
  out.line(`Linux system (${ctx.driver.label}): ${exists ? 'installed' : 'not installed'}`);
  if (unfinished) {
    out.line(`  ${UNFINISHED_TEXT}`);
    out.line(`  ${FINISH_TEXT}`);
    out.line(`  ${CLEAN_TEXT}`);
  }
  if (exists && ctx.platform === 'linux') {
    out.line(ctx.scope === 'machine'
      ? `  for:       the whole machine (it runs as the system account ${ctx.paths.vmUser}, started at boot by cseries.service)`
      : `  for:       your account only (in ${ctx.paths.lima}, started at your login)`);
  }
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
  await setup.showSetup(ctx, { open: browserHere(ctx) });
  return 0;
}

// What uninstall --delete-data says about the system packages QEMU came
// from, and removes only when this command installed them (state.json
// packagesAdded) and the person types y. -> nothing; a failure is said.
async function packagesOnUninstall(ctx, added, joined, interactive) {
  if (ctx.platform !== 'linux') return;
  let f = null;
  try {
    f = await machine.probe(ctx);
  } catch {
    f = null;
  }
  const rm = machine.removalFor(added, f, ctx.arch);
  const sudo = ctx.scope === 'machine' ? '' : 'sudo ';
  if (rm && rm.recorded) {
    ctx.out.line();
    ctx.out.line(`C-Series installed these packages from ${rm.pm} for its VM: ${rm.packages.join(' ')}.`);
    const yes = interactive && await ctx.prompt('Remove them too, if nothing else on this machine needs them? Type y to remove them: ');
    if (yes) {
      const sudoPath = ctx.scope === 'machine' ? null : bastion.findSudo(ctx);
      const [cmd, argv] = sudoPath ? [sudoPath, ['-p', '[cseries] sudo password for %u: ', '/bin/sh', '-c', rm.script]] : ['/bin/sh', ['-c', rm.script]];
      const r = await ctx.runner.run(cmd, argv, { stdin: 'inherit', timeoutMs: 15 * 60 * 1000 });
      if (ctx.log) {
        ctx.log.section(`remove ${rm.packages.join(' ')}`);
        ctx.log.write(r.stdout);
        ctx.log.write(r.stderr);
      }
      if (r.code === 0) ctx.out.line(`Removed ${rm.packages.join(' ')}.`);
      else ctx.out.warn(`${rm.pm} did not remove them (exit ${r.code}). To do it yourself: ${sudo}${rm.command}`);
    } else {
      ctx.out.line(`They are kept. To remove them: ${sudo}${rm.command}`);
    }
  } else if (rm) {
    ctx.out.line();
    ctx.out.line(`C-Series used QEMU from these packages: ${rm.packages.join(' ')}. This command did not record installing`);
    ctx.out.line('them (an install from before it did), so it leaves them. If they were installed for C-Series and');
    ctx.out.line(`nothing else uses them: ${sudo}${rm.command}`);
  }
  if (joined && joined.group && joined.user) {
    ctx.out.line(`You were added to the ${joined.group} group for the VM. To undo it: ${sudo}gpasswd -d ${joined.user} ${joined.group}`);
  }
}

// The whole machine's install, after its VM: the system account it ran as
// (only when this command made it) and its folder, which holds this command's
// state, logs, downloads and Lima's release. keepBase: the account only, the
// folder as it is (a move undone where the folder was here before, move.js).
async function removeMachineInstall(ctx, vmUserCreated, { keepBase = false } = {}) {
  if (vmUserCreated) {
    const r = await ctx.runner.run('/bin/sh', ['-c',
      `PATH=/usr/sbin:/usr/bin:/sbin:/bin; if getent passwd ${machine.shq(ctx.paths.vmUser)} >/dev/null; then userdel ${machine.shq(ctx.paths.vmUser)}; fi`], { timeoutMs: 60000 });
    if (r.code !== 0) ctx.out.warn(`The system account ${ctx.paths.vmUser} could not be removed: ${r.stderr.toString('utf8').trim()}. To do it yourself: userdel ${ctx.paths.vmUser}`);
  }
  if (!keepBase) fs.rmSync(ctx.paths.base, { recursive: true, force: true });
}

// This account's own install, after its VM: what this command keeps in its
// folders (state, logs, downloads, Lima's release and LIMA_HOME; on Windows
// the WSL disk's folder), each by name, then each folder if that left it
// empty. Never a folder as a whole: a path that came out wrong can then never
// take anything else with it.
function removeUserInstall(ctx) {
  const p = ctx.paths;
  const files = [p.state, `${p.state}.tmp`, p.log, `${p.log}.old`, p.installLog, p.hostCa, p.downloads, p.tools, p.lima, p.wsl];
  for (const f of files) if (f) fs.rmSync(f, { recursive: true, force: true });
  // Windows: the per-user bastion's own folder an earlier release made
  // (%LOCALAPPDATA%\Programs\privatae-bastion), once its program is gone.
  const own = ctx.platform === 'win32' ? [p.bastionDir] : [];
  for (const d of new Set([p.base, p.lima && path.dirname(p.lima), p.tools && path.dirname(p.tools), ...own])) {
    if (!d) continue;
    try {
      fs.rmdirSync(d);
    } catch { /* not empty, or already gone: left as it is */ }
  }
}

// Windows, uninstall --delete-data: what this command added besides the Host,
// the bastion and its own folder, each named, and each removed only on a y:
//   - WSL itself, when this command installed it (state.json wslInstalled)
//     and no other Linux system uses it now: removed by `wsl --uninstall` in
//     the same elevated run as the bastion (bastion.removeOnWindows), so
//     Windows asks once;
//   - the lines it added to the person's .wslconfig (state.json
//     wslConfigAdded), taken out here, as the person.
// -> whether WSL is to be removed.
async function windowsExtras(ctx, interactive) {
  const out = ctx.out;
  let wsl = false;
  if (ctx.state.get('wslInstalled')) {
    const others = ctx.driver.otherDistros ? await ctx.driver.otherDistros(ctx) : [];
    out.line();
    if (others.length) {
      out.line(`C-Series installed WSL on this PC. It stays: ${others.join(', ')} ${others.length === 1 ? 'runs' : 'run'} in it too.`);
    } else {
      out.line('C-Series installed WSL on this PC for its Linux system, and no other Linux system uses it.');
      wsl = interactive && await ctx.prompt('Remove WSL too? Type y to remove it: ');
      if (!wsl) out.line('WSL is kept. To remove it: wsl --uninstall   (from an Administrator terminal)');
    }
  }
  const added = ctx.state.get('wslConfigAdded');
  if (added && Array.isArray(added.lines) && added.lines.length && added.file) {
    out.line();
    out.line(`C-Series added ${added.lines.join(' and ')} to ${added.file}, the limits on WSL's virtual machine.`);
    const yes = interactive && await ctx.prompt('Take them out again? Type y to take them out: ');
    if (!yes) {
      out.line('They are kept.');
    } else {
      let r = null;
      try {
        const decoded = decodeWslConfig(fs.readFileSync(added.file));
        if (decoded) {
          r = removeWslConfigLines(decoded.text, added.lines, Boolean(added.section));
          if (!r.text && added.created && !decoded.bom) fs.rmSync(added.file, { force: true });
          else if (r.removed.length) fs.writeFileSync(added.file, decoded.bom + r.text);
        }
      } catch (e) {
        if (!e || !e.code) throw e;
        r = null;
      }
      // Looked at and settled: a later uninstall (the bastion's record kept)
      // does not ask about these lines again.
      if (r) ctx.state.set('wslConfigAdded', undefined);
      const kept = r ? added.lines.filter((l) => !r.removed.includes(l)) : added.lines;
      if (r && r.removed.length) out.line(`Taken out of ${added.file}: ${r.removed.join(' and ')}.`);
      if (kept.length) out.line(`${kept.join(' and ')} ${kept.length === 1 ? 'is' : 'are'} not in ${added.file} as C-Series wrote ${kept.length === 1 ? 'it' : 'them'} (changed since, or the file could not be read), so ${kept.length === 1 ? 'it stays' : 'they stay'}.`);
    }
  }
  return wsl;
}

async function uninstall(ctx, args) {
  requireSupported(ctx.driver, ctx.platform);
  const deleteData = args.includes('--delete-data');
  const keepBastion = args.includes('--keep-bastion');
  const interactive = !args.includes('--yes');
  const win = ctx.platform === 'win32';
  const b = bastion.bastionState(ctx);
  // The bastion goes with the Host when this command installed it.
  const ownBastion = Boolean(b.service) && !keepBastion;
  if (deleteData) {
    ctx.out.line('This deletes, for good:');
    ctx.out.line('  - the Host, its memory and everything it stored');
    if (ownBastion) {
      ctx.out.line(win
        ? '  - the bastion this command installed: its service, its program in Program Files\\Privatae, the tray that starts at sign-in, its firewall rules, and its pairings (every Host it serves), keys and audit log'
        : '  - the bastion this command installed, with its pairings (every Host it serves), keys and audit log');
    }
    ctx.out.line(`  - ${ctx.scope === 'machine' ? `${ctx.paths.base} and the system account ${ctx.paths.vmUser}` : `this command's folders (${ctx.paths.base})`}`);
    if (ownBastion && win) ctx.out.line('Windows asks for an administrator\'s permission once, for the bastion.');
    const yes = !interactive || (await ctx.prompt('Type y to go on: '));
    if (!yes) {
      ctx.out.line('Nothing was removed.');
      return 1;
    }
  }
  if (await ctx.driver.guestExists(ctx)) await ctx.driver.removeGuest(ctx, { deleteData });
  else await ctx.driver.setAutostart(ctx, false);
  if (ctx.driver.clearResume) await ctx.driver.clearResume(ctx);
  if (!deleteData) {
    ctx.out.line(ctx.scope === 'machine' ? 'C-Series is stopped and no longer starts at boot.' : 'C-Series is stopped and no longer starts at login.');
    ctx.out.line(`Its data is kept in the ${ctx.driver.label} system "cseries"; npx cseries brings it back,`);
    ctx.out.line('and npx cseries uninstall --delete-data deletes it with everything else C-Series put here.');
    ctx.out.line('The bastion stays installed, paired with this Host.');
    return 0;
  }
  ctx.out.line('The Host and its data are removed.');
  // Windows asks about what else it added first: WSL goes in the same
  // elevated run as the bastion.
  const wslYes = win ? await windowsExtras(ctx, interactive) : false;
  let failed = false;
  let bastionGone = false;
  let wslExit = null;
  if (ownBastion) {
    const r = await bastion.removeBastion(ctx, { wsl: wslYes });
    wslExit = r.wslExit === undefined ? null : r.wslExit;
    // WSL goes in the same elevated run: one that did not finish did not
    // remove it either.
    if (wslYes && wslExit === null) {
      failed = true;
      ctx.out.warn('WSL was not removed: it goes in the same administrator run as the bastion, which did not finish. To remove it: wsl --uninstall   (from an Administrator terminal)');
    }
    if (r.removed && !r.running && !(r.left && r.left.length)) {
      bastionGone = true;
      ctx.out.line(win
        ? 'The bastion is removed: its service, program, tray, firewall rules, pairings, keys and audit log.'
        : 'The bastion is removed: its service, program, pairings, keys and audit log.');
    } else {
      failed = true;
      if (!r.removed) ctx.out.warn(`The bastion was not removed: ${r.why}.`);
      else if (r.running) ctx.out.warn('The bastion was removed, but its service still runs.');
      else ctx.out.warn(`The bastion was removed, except: ${r.left.join('; ')}.`);
      ctx.out.warn(`Run npx cseries uninstall --delete-data again, or remove it yourself: ${bastion.manualRemoval(ctx)}`);
    }
  } else {
    if (wslYes) {
      const r = await bastion.removeOnWindows(ctx, { bastion: false, wsl: true });
      if (!r.why) wslExit = r.wslExit;
      else ctx.out.warn(`WSL was not removed: ${r.why}.`);
    }
    if (keepBastion && b.service) {
      ctx.out.line('The bastion stays installed, as --keep-bastion asks; the next npx cseries pairs it with the new Host.');
    } else if (!keepBastion && !b.service && await bastion.bastionHere(ctx)) {
      ctx.out.line('This machine\'s bastion was not installed by npx cseries from this install, so it stays as it is.');
      ctx.out.line(`To remove it with its pairings, keys and audit log: ${bastion.manualRemoval(ctx)}`);
    }
  }
  if (wslYes && wslExit !== null) {
    if (wslExit === 0) {
      ctx.out.line('WSL is removed. Windows may finish removing it at the next restart.');
      ctx.state.set('wslInstalled', undefined);
    } else {
      failed = true;
      ctx.out.warn(`wsl --uninstall ended with exit code ${wslExit}. To try again: wsl --uninstall   (from an Administrator terminal)`);
    }
  }
  if (!bastionGone && b.hostCa) {
    // The bastion stays, paired with the Host that is gone. Its pairing name
    // stays recorded, so the next npx cseries releases the bastion's anchor
    // to it and pairs the new Host (bastion.js planFor).
    const { hostCa, ...rest } = b;
    ctx.state.set('bastion', rest);
    ctx.state.set('pairedHostCa', undefined);
    fs.rmSync(ctx.paths.hostCa, { force: true });
  }
  if (b.host && b.enrolled) {
    ctx.out.line(`Your authenticator app's entry "${bastion.authenticatorEntry(ctx, b.host)}" was for this Host`);
    ctx.out.line('and no longer works: delete it from the app.');
    if (!bastionGone) ctx.out.line('If you install C-Series again, it adds a new entry with the same name.');
  }
  await packagesOnUninstall(ctx, ctx.state.get('packagesAdded'), ctx.state.get('kvmJoined'), interactive);
  if (bastionGone || !b.service) {
    // Nothing of this install is left that a later run needs to know about.
    if (ctx.scope === 'machine') await removeMachineInstall(ctx, Boolean(ctx.state.get('vmUserCreated')));
    else removeUserInstall(ctx);
    ctx.out.line(failed ? 'The rest of C-Series is removed.' : 'C-Series is removed from this machine.');
  } else {
    ctx.out.line('C-Series is removed; this command\'s record of the bastion is kept, for the next install.');
  }
  return failed ? 1 : 0;
}

function packageVersion() {
  try {
    return JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'package.json'), 'utf8')).version;
  } catch {
    return 'unknown';
  }
}

// Whether this run opens the browser itself: never as root on Linux (the
// whole machine's install). sudo can keep DISPLAY and XAUTHORITY, and a
// browser started by root in the person's session runs as root; asMachine
// opens it as the account that ran npx cseries once the run as root is done.
function browserHere(ctx) {
  return ctx.scope !== 'machine';
}

// A Linux run as an account that should act on the whole machine's install
// (scope.js chooseScope): the same command again, as root, through one sudo
// on this terminal; then, for npx cseries and npx cseries open, the browser opened here,
// as this account, which root's run cannot do.
async function asMachine(ctx, argv, cmd) {
  const sudo = bastion.findSudo(ctx);
  if (!sudo) {
    throw new CliError('C-Series is installed for the whole machine with sudo, and sudo is not here.',
      'Run npx cseries as root, or install it for your account only: npx cseries --just-me');
  }
  if (cmd === 'move') {
    ctx.out.line('Moving C-Series to the whole machine. sudo asks for your password once, for the whole run.');
  } else if (cmd === '' || cmd === 'update') {
    ctx.out.line('C-Series is installed for the whole machine: the Host runs as a system service, not');
    ctx.out.line('from your account, so removing an account never removes it. sudo asks for your');
    ctx.out.line('password once, for the whole run. (npx cseries --just-me installs it for your account only.)');
  }
  const r = await ctx.runner.inherit(sudo, scope.reexecArgs({
    prompt: '[cseries] sudo password for %u: ', env: ctx.env, node: ctx.nodePath, bin: ctx.binPath, argv,
  }));
  if (r.notFound) throw new CliError('sudo could not be started.', 'Run npx cseries again.');
  const code = r.code < 0 ? 1 : r.code;
  if (code === 0 && (cmd === '' || cmd === 'open' || cmd === 'move')) {
    const state = setup.setupState(await ctx.net.probe(`${setup.BASE}/setup`));
    await setup.openBrowser(ctx, state === 'done' ? setup.NAMED : `${setup.NAMED}/setup`);
  }
  return code;
}

// Linux, run as an account, with a whole-machine install that did not finish
// (scope.js UNFINISHED): uninstall removes what it left (through sudo, as the
// machine's own uninstall); npx cseries finishes it when no install of this
// account's is here; every other command says what it is and how to finish
// or remove it, then goes on as chosen (scope.js). -> an exit code, or null
// to go on.
async function unfinishedMachine(ctx, argv, cmd, args, user) {
  if (cmd === 'uninstall' && (!user || args.includes(move.WHOLE_MACHINE))) {
    return asMachine(ctx, argv.filter((a) => a !== move.WHOLE_MACHINE), cmd);
  }
  const install = cmd === '' || cmd === 'update';
  if (!user && !install && cmd !== 'status') throw new CliError(UNFINISHED_TEXT, `${FINISH_TEXT}\n${CLEAN_TEXT}`);
  // The run as root finishes it, and says so.
  if (!user && install && !args.includes(scope.JUST_ME)) return null;
  const out = ctx.out;
  out.line(UNFINISHED_TEXT);
  if (!user) out.line(FINISH_TEXT);
  out.line(CLEAN_TEXT);
  out.line();
  return null;
}

async function main(argv, overrides = {}) {
  const ctx = makeContext(overrides);
  const args = [...argv];
  const cmd = args[0] && args[0].startsWith('--') && !scope.LOCAL_COMMANDS.has(args[0]) ? '' : (args.shift() || '');
  try {
    // Linux, run as an account: which install this is about (scope.js). A
    // test that hands its own paths has said it already, or says what is
    // installed (overrides.installed).
    if (ctx.platform === 'linux' && ctx.uid !== 0) {
      const mfs = ctx.machineFs;
      const installed = overrides.installed || (mfs && {
        machine: scope.machineInstalled(mfs), user: scope.userInstalled(ctx.paths), unfinished: scope.machineUnfinished(mfs),
        kept: Boolean(scope.machineKeptBastion(mfs)),
      });
      if (installed && installed.unfinished && !installed.machine && !scope.LOCAL_COMMANDS.has(cmd)) {
        const code = await unfinishedMachine(ctx, argv, cmd, args, installed.user);
        if (code !== null) return code;
      }
      // A record that holds only the bastion's (scope.js keptBastionRecord)
      // is no install, and its uninstall is still the machine's: it removes
      // the bastion and what the machine's install left with it.
      if (installed && installed.kept && !installed.machine && cmd === 'uninstall' &&
        (!installed.user || args.includes(move.WHOLE_MACHINE))) {
        return await asMachine(ctx, argv.filter((a) => a !== move.WHOLE_MACHINE), cmd);
      }
      // An account's own install, and none for the whole machine: offered
      // the move once (move.js); not onto a machine install that did not
      // finish, unless asked for (the root run then says what to do first).
      if (installed && installed.user && !installed.machine && cmd === '' && (!installed.unfinished || args.includes(move.WHOLE_MACHINE)) &&
        await move.offer(ctx, args)) {
        const code = await asMachine(ctx, [...argv.filter((a) => a !== move.WHOLE_MACHINE), ...move.moveArgs(ctx.paths)], 'move');
        // The offer is not made again: say how to move it, whatever stopped it.
        if (code !== 0) ctx.out.line(`C-Series was not moved. To try again: npx cseries ${move.WHOLE_MACHINE}`);
        return code;
      }
      if (installed && scope.chooseScope({ platform: ctx.platform, uid: ctx.uid, cmd, args, ...installed }) === 'machine') {
        return await asMachine(ctx, argv.filter((a) => a !== move.WHOLE_MACHINE), cmd);
      }
    }
    if (ctx.scope === 'machine' && cmd === '') ctx.move = await move.fromArgs(ctx, args);
    if (args.includes(scope.JUST_ME) && ctx.scope === 'machine') {
      throw new CliError('--just-me installs C-Series for one account, and this run is root.', 'Run npx cseries --just-me as that account, without sudo.');
    }
    switch (cmd) {
      case '': return await up(ctx, { open: browserHere(ctx) });
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

module.exports = { main, makeContext, upSteps, uninstall, HELP };
