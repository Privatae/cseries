'use strict';

// Moving an account's own install on Linux (--just-me, and every install from
// before whole-machine ones) to the whole machine (scope.js).
//
// WHO ASKS. `npx cseries` run as the account that has one, with no
// whole-machine install here, offers it once, in a terminal (offer). The
// answer is recorded in that install's state.json ("machineOffer"), so it is
// never asked again; `npx cseries --whole-machine` moves it later without a
// question. A no, or no terminal to ask on, leaves the account's install
// working exactly as it was.
//
// HOW IT MOVES. On a yes the same command runs again as root through one sudo
// (cli.js asMachine), told where the account's install is (MOVE_FROM: its
// folder and its login unit, which the account's own XDG settings decide and
// sudo drops). The root run checks those against the account sudo ran it for
// (fromArgs), then runs the whole machine's install steps (cli.js upSteps)
// with three of its own:
//   carryStep   first: this command's record of the account's install (the
//               bastion it installed and paired, the packages it added, the
//               Host CA) into the machine's state.json, so the bastion step
//               sees the bastion as its own and the pairing as current, and
//               the Host's identity as it is now (its CA, from the running
//               Host itself);
//   diskStep    once the VM account and Lima are ready: the account's VM
//               stopped, then the Lima instance (its disk, with the Host's
//               database, apps, keys and pairings in it) and Lima's own keys
//               copied into /var/lib/cseries/lima, given to the VM account,
//               and put in place (copyScript); the account's copy untouched;
//   finishStep  once the moved Host answers: its CA checked against the one
//               it had (the same Host, so the bastion's pairing and anchor,
//               pinned to that CA and reached on the same 127.0.0.1 ports,
//               hold as they were), then the account's login unit and its
//               copy removed.
// Anything that stops the run before finishStep is done undoes the move
// (rollback): the machine's VM and unit, /var/lib/cseries and the system
// account (which did not exist before), and the account's VM started again
// through its own login unit and user manager (startAccountVm).

const fs = require('node:fs');
const path = require('node:path');
const { CliError } = require('./errors');
const { paths: accountPaths, MACHINE_BASE } = require('./platform');
const lima = require('./vm/lima');
const machine = require('./vm/lima/machine');
const pairing = require('./pairing');
const { State } = require('./state');
const scope = require('./scope');

const MOVE_FROM = '--move-from';
const WHOLE_MACHINE = '--whole-machine';
const OFFER = 'machineOffer';

function text(b) {
  return Buffer.isBuffer(b) ? b.toString('utf8') : String(b || '');
}

// The unelevated side. -> true to move now; false to go on with the account's
// install as it is. Asked only on a plain `npx cseries` with a terminal, once.
async function offer(ctx, args) {
  if (args.includes(WHOLE_MACHINE)) return true;
  if (args.includes('--just-me') || ctx.state.get(OFFER)) return false;
  if (!ctx.isTTY) return false;
  const out = ctx.out;
  out.line(`C-Series is installed for your account only: its Host runs in ${ctx.paths.base}`);
  out.line('and starts when you sign in. It can move to the whole machine instead: it then runs');
  out.line('as a system service of its own, starts when the computer starts with nobody signed in,');
  out.line('and removing your account never removes it. Everything moves with it: its data, apps,');
  out.line('pairings and keys, and the bastion stays connected to the same Host. sudo asks for');
  out.line('your password once. If the move does not finish, it is undone.');
  const yes = await ctx.prompt('Move it to the whole machine? Type y to move it, anything else keeps it as it is: ');
  ctx.state.set(OFFER, yes ? 'accepted' : 'declined');
  if (!yes) {
    out.line(`It stays for your account only. To move it later: npx cseries ${WHOLE_MACHINE}`);
    out.line();
  }
  return yes;
}

// The arguments the root run is given: where the account's install is.
function moveArgs(p) {
  return [MOVE_FROM, p.base, p.autostart];
}

// The root run: the account's install, from MOVE_FROM's two paths, checked
// against the account sudo ran this for (SUDO_USER) and its home from the
// account database. -> null when no move was asked for; throws when the
// paths are not that account's own install.
async function fromArgs(ctx, args) {
  const at = args.indexOf(MOVE_FROM);
  if (at < 0) return null;
  const [base, unit] = [args[at + 1], args[at + 2]];
  const account = ctx.env.SUDO_USER;
  const refuse = (why) => new CliError(`The move to the whole machine was not started: ${why}. Nothing was changed.`,
    `Run npx cseries ${WHOLE_MACHINE} again as the account whose install it is.`);
  if (!account || account === 'root' || !/^[A-Za-z0-9._][A-Za-z0-9._-]{0,63}$/.test(account)) throw refuse('it is moved from the account that runs npx cseries through sudo, and sudo named none');
  if (!base || !unit) throw refuse('the account\'s install was not named');
  const r = await ctx.runner.run('getent', ['passwd', account], { timeoutMs: 15000 });
  const f = text(r.stdout).trim().split(':');
  if (r.code !== 0 || f.length < 7 || f[0] !== account) throw refuse(`the account ${account} is not in this machine's account database`);
  const [uid, gid, home] = [f[2], f[3], f[5]];
  if (!/^\d+$/.test(uid) || uid === '0' || !path.posix.isAbsolute(home) || home === '/') throw refuse(`${account} is not an account an install can belong to`);
  // The two paths are what platform.js gives this account with the XDG
  // folders they imply, and under its home.
  if (path.posix.basename(base) !== 'cseries' || !/\/systemd\/user\/cseries\.service$/.test(unit)) throw refuse('those are not the folders of an account\'s install');
  const src = accountPaths('linux', {
    XDG_DATA_HOME: path.posix.dirname(base),
    XDG_CONFIG_HOME: path.posix.dirname(path.posix.dirname(path.posix.dirname(unit))),
  }, home);
  const inHome = (p) => path.posix.normalize(p) === p && p.startsWith(`${home}/`);
  if (src.base !== base || src.autostart !== unit || !inHome(base) || !inHome(unit)) throw refuse(`those are not folders in ${account}'s home`);
  let real;
  try {
    real = fs.realpathSync(base);
  } catch {
    throw refuse(`${base} is not here`);
  }
  if (!real.startsWith(`${fs.realpathSync(home)}/`)) throw refuse(`${base} leads outside ${account}'s home`);
  if (!fs.existsSync(path.posix.join(src.lima, lima.INSTANCE))) throw refuse(`${account} has no C-Series VM in ${src.lima}`);
  if (fs.existsSync(path.posix.join(ctx.paths.lima, lima.INSTANCE))) throw refuse(`the whole machine already has its own C-Series in ${ctx.paths.base}`);
  // A record of the machine's own (an install of it that did not finish,
  // scope.js UNFINISHED) is never mixed with the account's, nor removed by
  // an undone move: what it left is removed first. One that holds only the
  // bastion's, kept by an uninstall (scope.js keptBastionRecord), is no
  // install: the move goes on, and an undone move puts it back as it was.
  const kept = fs.existsSync(ctx.paths.state) &&
    scope.keptBastionRecord(ctx.state.data, { unit: ctx.paths.autostart, lima: ctx.paths.lima }) ? fs.readFileSync(ctx.paths.state) : null;
  if (fs.existsSync(ctx.paths.state) && !kept) {
    throw new CliError(`The move to the whole machine was not started: an install of C-Series for the whole machine started here and did not finish (${ctx.paths.state}). Nothing was changed.`,
      `Remove what it left (npx cseries uninstall --delete-data ${WHOLE_MACHINE}), then run npx cseries ${WHOLE_MACHINE} again as ${account}.`);
  }
  // What of the machine's folder is here already, so an undone move leaves
  // exactly that: state.json and lima/ are what make a later run take this
  // machine as installed for the whole machine (scope.js machineInstalled).
  const had = (p) => Boolean(p) && fs.existsSync(p);
  return {
    account, uid, gid, home, paths: src,
    baseExisted: had(ctx.paths.base),
    kept,
    had: { state: had(ctx.paths.state), lima: had(ctx.paths.lima), hostCa: had(ctx.paths.hostCa), vmHome: had(ctx.paths.vmHome) },
  };
}

// limactl for the account's VM, as that account (Lima refuses root), with its
// own LIMA_HOME and HOME.
async function accountLimactl(ctx, mv, args, opts = {}) {
  const runuser = lima.findRunuser(ctx);
  const r = await ctx.runner.run(runuser, ['-u', mv.account, '--', lima.limactlPath({ paths: mv.paths }), '--tty=false', ...args],
    { ...opts, env: { LIMA_HOME: mv.paths.lima, HOME: mv.home } });
  return { code: r.code, stdout: text(r.stdout), stderr: text(r.stderr) };
}

async function accountStatus(ctx, mv) {
  const r = await accountLimactl(ctx, mv, ['list', '--format', '{{.Name}} {{.Status}}']);
  const line = r.stdout.split('\n').map((l) => l.trim().split(/\s+/)).find((p) => p[0] === lima.INSTANCE);
  return line ? (line[1] || '') : null;
}

// The Host's CA, from the Host itself (a pairing token's CA): the account's
// running VM before the move, the machine's after it. null when it cannot
// say.
async function accountHostCa(ctx, mv) {
  const r = await accountLimactl(ctx, mv, ['shell', '--workdir', '/', lima.INSTANCE, 'sudo', lima.GUEST_BIN, 'pair-token']);
  if (r.code !== 0) return null;
  try {
    return pairing.parsePairToken(r.stdout).caSha256;
  } catch {
    return null;
  }
}

// The keys of the account's state.json the machine's install carries on.
const CARRIED = ['bastion', 'packagesAdded', 'kvmJoined', 'pairedHostCa', 'size'];

const carryStep = {
  title: 'Moving C-Series to the whole machine',
  async run(ctx) {
    const mv = ctx.move;
    const out = ctx.out;
    const status = await accountStatus(ctx, mv);
    if (status === null) {
      throw new CliError(`Lima does not list the C-Series VM of ${mv.account} in ${mv.paths.lima}. Nothing was moved.`,
        `Run npx cseries as ${mv.account} to repair that install first.`);
    }
    // The Host as it is now, to check the moved one against.
    const src = new State(mv.paths.state);
    const recorded = (src.get('bastion') || {}).hostCa || src.get('pairedHostCa') || null;
    mv.hostCa = (status === 'Running' && (await accountHostCa(ctx, mv))) || recorded;
    // Without it the moved Host could not be checked, and the account's copy
    // is removed only after that check: so no move, before anything changes.
    if (!mv.hostCa) {
      throw new CliError(`The Host of ${mv.account} did not say which Host it is (its CA), and nothing records it, so the moved Host could not be checked against it. Nothing was moved.`,
        `Start it as ${mv.account} (npx cseries start), then run npx cseries ${WHOLE_MACHINE} again.`);
    }
    for (const k of CARRIED) if (src.get(k) !== undefined) ctx.state.set(k, src.get(k));
    // Written anew, so the copy is root's like the rest of the folder, not
    // the account's (a copy keeps the source's owner on some systems).
    if (fs.existsSync(mv.paths.hostCa)) {
      fs.mkdirSync(path.posix.dirname(ctx.paths.hostCa), { recursive: true });
      fs.rmSync(ctx.paths.hostCa, { force: true });
      fs.writeFileSync(ctx.paths.hostCa, fs.readFileSync(mv.paths.hostCa), { mode: 0o644 });
    }
    ctx.state.set('movedFrom', { account: mv.account, base: mv.paths.base });
    out.detail(`The Host of ${mv.account} (in ${mv.paths.base}) moves to ${ctx.paths.base}.`);
    if (mv.hostCa) out.detail(`Its identity, checked again once it runs here: Host CA sha256 ${mv.hostCa.slice(0, 16)}...`);
  },
};

// The account's Lima instance and Lima's keys into the machine's LIMA_HOME,
// as root: copied (sparse, so the disk takes what it uses) into a folder of
// its own beside the destination, checked, given to the VM account, then put
// in place by rename. The account's own copy is only read. Sockets and pid
// files are Lima's of the stopped VM, not copied. A disk that names a file
// in the account's folder as its backing file would stop working when that
// copy is removed: such a move is refused before anything is copied.
function copyScript(mv, ctx) {
  const q = machine.shq;
  const dst = ctx.paths.lima;
  return [
    'set -eu',
    'umask 077',
    'PATH=/usr/sbin:/usr/bin:/sbin:/bin',
    'export PATH',
    'echo step=move-copy',
    `src=${q(mv.paths.lima)}`,
    `dst=${q(dst)}`,
    `u=${q(ctx.paths.vmUser)}`,
    'stage="$dst/.move"',
    '[ -d "$src/cseries" ] || { echo "error=there is no VM in $src"; exit 3; }',
    'if command -v qemu-img >/dev/null 2>&1; then',
    '  for d in "$src"/cseries/*disk*; do',
    '    [ -f "$d" ] || continue',
    '    if qemu-img info -U "$d" 2>/dev/null | grep -F "backing file: $src/" >/dev/null; then echo "error=the VM\'s disk $d is backed by a file in $src"; exit 3; fi',
    '  done',
    'fi',
    'need=$(du -sk "$src/cseries" "$src/_config" 2>/dev/null | awk \'{s+=$1} END {print s+0}\')',
    'have=$(df -Pk "$dst" | awk \'NR==2 {print $4}\')',
    // What the copy takes, and 1 GiB more for the Host to keep writing.
    'if [ "$have" -lt $((need + 1048576)) ]; then echo "error=the copy needs $(((need + 1048576) / 1048576)) GB free in $dst, and it has $((have / 1048576)) GB"; exit 4; fi',
    'rm -rf "$stage"',
    'mkdir -m 700 "$stage"',
    'cp -a --sparse=always "$src/cseries" "$stage/cseries"',
    '[ ! -d "$src/_config" ] || cp -a "$src/_config" "$stage/_config"',
    'find "$stage" \\( -type s -o -name "*.pid" \\) -exec rm -f {} +',
    'chown -R "$u:$u" "$stage"',
    'rm -rf "$dst/_config"',
    '[ ! -d "$stage/_config" ] || mv "$stage/_config" "$dst/_config"',
    'mv "$stage/cseries" "$dst/cseries"',
    'rmdir "$stage"',
    'echo "copied=$need"',
    '',
  ].join('\n');
}

const diskStep = {
  title: 'Moving the Host\'s VM',
  doneText: 'It is here.',
  done: (ctx) => ctx.driver.guestExists(ctx),
  async run(ctx) {
    const mv = ctx.move;
    const out = ctx.out;
    const status = out.status(`Stopping the VM of ${mv.account}`);
    const st = await accountLimactl(ctx, mv, ['stop', lima.INSTANCE]);
    const now = await accountStatus(ctx, mv);
    if (st.code !== 0 && now !== 'Stopped') {
      status.end();
      throw new CliError(`The VM of ${mv.account} did not stop: ${st.stderr.trim().split('\n').pop() || `exit ${st.code}`}`, 'Run npx cseries again.');
    }
    mv.stopped = true;
    status.end(`The VM of ${mv.account} is stopped.`);
    const copying = out.status('Copying the VM, with the Host\'s data');
    const r = await ctx.runner.run('/bin/sh', ['-s'], { input: copyScript(mv, ctx), timeoutMs: 60 * 60 * 1000 });
    const said = text(r.stdout);
    if (ctx.log) {
      ctx.log.section('move: copy');
      ctx.log.write(r.stdout);
      ctx.log.write(r.stderr);
    }
    if (r.code !== 0 || !/^copied=\d+$/m.test(said)) {
      copying.end();
      const why = (/^error=(.*)$/m.exec(said) || [])[1] || text(r.stderr).trim().split('\n').pop() || `exit ${r.code}`;
      throw new CliError(`The VM could not be copied: ${why}.`, 'Run npx cseries again once that is fixed.');
    }
    mv.copied = true;
    copying.end(`Copied into ${ctx.paths.lima}.`);
  },
};

// The account's own install, now that the moved Host runs: its login unit
// (and the link systemctl --user enable made), then its folders, each by name
// (cli.js removeUserInstall). Its systemd user manager forgets the unit at
// its next reload; told now when it answers.
async function removeAccountInstall(ctx, mv, removeUserInstall) {
  const unit = mv.paths.autostart;
  const wants = path.posix.join(path.posix.dirname(unit), 'default.target.wants', path.posix.basename(unit));
  fs.rmSync(wants, { force: true });
  fs.rmSync(unit, { force: true });
  try {
    fs.rmdirSync(path.posix.dirname(wants));
  } catch { /* other units are wanted there: left as it is */ }
  await ctx.runner.run('systemctl', ['--user', `--machine=${mv.account}@`, 'daemon-reload'], { timeoutMs: 15000 });
  removeUserInstall({ platform: 'linux', paths: mv.paths });
}

function finishStep(removeUserInstall) {
  return {
    title: 'Checking the moved Host and removing the copy in your account',
    async run(ctx) {
      const mv = ctx.move;
      const now = (await pairing.mintToken(ctx)).caSha256;
      if (mv.hostCa && now !== mv.hostCa) {
        throw new CliError(`The moved Host is not the same Host: its CA is ${now.slice(0, 16)}..., and it was ${mv.hostCa.slice(0, 16)}....`, 'Run npx cseries again.');
      }
      ctx.out.detail(mv.hostCa ? 'It is the same Host: the same CA, so its pairings and the bastion\'s anchor hold.' : 'The moved Host answers.');
      mv.done = true;
      try {
        await removeAccountInstall(ctx, mv, removeUserInstall);
        ctx.state.set('movedFrom', undefined);
        ctx.out.detail(`Removed the copy in ${mv.paths.base} and the login unit of ${mv.account}.`);
      } catch (err) {
        ctx.out.warn(`The Host runs for the whole machine, but the copy in ${mv.paths.base} could not all be removed (${err.message}). It is not used; remove it yourself.`);
      }
    },
  };
}

// A move that did not finish, undone: the machine's VM and unit, everything
// in /var/lib/cseries and the system account (none of it was here before the
// move), and the account's VM started again as it ran (startAccountVm).
async function rollback(ctx, removeMachineInstall) {
  const mv = ctx.move;
  const out = ctx.out;
  out.line();
  out.line('The move did not finish, so it is undone.');
  try {
    if (await ctx.driver.guestExists(ctx)) await ctx.driver.setAutostart(ctx, false);
    else if (ctx.paths.autostart && fs.existsSync(ctx.paths.autostart)) await ctx.driver.setAutostart(ctx, false);
  } catch { /* the folder below goes either way */ }
  // The system account stays when the kept record says it was made before
  // the move (an earlier whole-machine install made it).
  const keptRecord = mv.kept ? JSON.parse(mv.kept.toString('utf8')) : {};
  const vmUserCreated = Boolean(ctx.state.get('vmUserCreated')) && !keptRecord.vmUserCreated;
  if (!mv.baseExisted) {
    await removeMachineInstall(ctx, vmUserCreated);
  } else {
    // The folder was here (a log of an earlier run, say): what the move added
    // to it goes, and what was here stays. A state.json or lima/ left behind
    // would make every later npx cseries take the machine as installed.
    const had = mv.had || {};
    for (const d of ['cseries', '_config', '.move']) fs.rmSync(path.posix.join(ctx.paths.lima, d), { recursive: true, force: true });
    if (!had.lima) fs.rmSync(ctx.paths.lima, { recursive: true, force: true });
    if (!had.state) for (const f of [ctx.paths.state, `${ctx.paths.state}.tmp`]) fs.rmSync(f, { force: true });
    if (!had.hostCa) fs.rmSync(ctx.paths.hostCa, { force: true });
    if (!had.vmHome && ctx.paths.vmHome) fs.rmSync(ctx.paths.vmHome, { recursive: true, force: true });
    await removeMachineInstall(ctx, vmUserCreated, { keepBase: true });
    // The bastion's record kept by an uninstall, as it was before the move.
    if (mv.kept) {
      fs.writeFileSync(`${ctx.paths.state}.tmp`, mv.kept);
      fs.renameSync(`${ctx.paths.state}.tmp`, ctx.paths.state);
    }
  }
  if (mv.stopped && !(await startAccountVm(ctx, mv))) return;
  out.line(`C-Series runs for ${mv.account} only, as it did before: nothing of it was changed.`);
  out.line(`To try the move again: npx cseries ${WHOLE_MACHINE}`);
}

// systemctl --user as the account, run by root: runuser runs it as that
// account, and its own systemd user manager is found by its bus in its
// runtime folder (/run/user/<uid>), which sudo's environment does not name.
async function accountSystemctl(ctx, mv, run, args) {
  const r = await ctx.runner.run(lima.findRunuser(ctx), ['-u', mv.account, '--', 'systemctl', '--user', ...args], {
    timeoutMs: 30000,
    env: { HOME: mv.home, XDG_RUNTIME_DIR: run, DBUS_SESSION_BUS_ADDRESS: `unix:path=${run}/bus` },
  });
  return { code: r.code, out: text(r.stdout).trim(), why: `${text(r.stderr)}${text(r.stdout)}`.trim() || `exit ${r.code}` };
}

// The account's VM started again as it runs between its logins: by its own
// login unit (vm/lima.js userUnit, still in place, since the account's copy
// goes only once the move is done), through the account's systemd user
// manager, so that manager supervises it and stops it at logout or shutdown
// as before. The unit's limactl stays in the foreground, so its start returns
// before the VM runs: Lima is asked until it says Running. With no user
// manager running for the account (no session and no linger), or a unit that
// does not start, Lima starts it as the account, as the install does.
// -> whether it runs.
async function startAccountVm(ctx, mv) {
  const out = ctx.out;
  const status = out.status(`Starting the VM of ${mv.account} again`);
  const run = path.posix.join(ctx.userRunDir || '/run/user', String(mv.uid));
  let why = `no systemd user manager runs for ${mv.account}`;
  if (fs.existsSync(mv.paths.autostart) && fs.existsSync(path.posix.join(run, 'bus'))) {
    const s = await accountSystemctl(ctx, mv, run, ['start', 'cseries.service']);
    if (s.code === 0) {
      const deadline = Date.now() + (ctx.vmStartTimeoutMs || 10 * 60 * 1000);
      for (;;) {
        if ((await accountStatus(ctx, mv)) === 'Running') {
          status.end(`The VM of ${mv.account} runs again, from its login unit.`);
          return true;
        }
        const a = await accountSystemctl(ctx, mv, run, ['is-active', 'cseries.service']);
        if (a.out === 'failed' || Date.now() >= deadline) {
          status.end();
          out.warn(`The VM of ${mv.account} did not start again from its login unit${a.out === 'failed' ? ' (cseries.service failed)' : ' within 10 minutes'}. Run npx cseries as ${mv.account} to start it; "journalctl --user -u cseries.service", run as ${mv.account}, shows why.`);
          return false;
        }
        await ctx.sleep(3000);
      }
    }
    why = `its login unit did not start (${s.why.split('\n').pop()})`;
  } else if (!fs.existsSync(mv.paths.autostart)) {
    why = 'it has no login unit';
  }
  const r = await accountLimactl(ctx, mv, ['start', '--timeout=10m', lima.INSTANCE]);
  if (r.code !== 0) {
    status.end();
    out.warn(`The VM of ${mv.account} did not start again: ${r.stderr.trim().split('\n').pop()}. Run npx cseries as ${mv.account} to start it.`);
    return false;
  }
  status.end(`The VM of ${mv.account} runs again, started by Lima directly: ${why}.`);
  return true;
}

module.exports = { offer, moveArgs, fromArgs, copyScript, carryStep, diskStep, finishStep, rollback, accountLimactl, startAccountVm, MOVE_FROM, WHOLE_MACHINE, OFFER, CARRIED, MACHINE_BASE };
