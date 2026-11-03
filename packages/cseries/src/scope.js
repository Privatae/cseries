'use strict';

// Who a Linux install belongs to: the whole machine, or one account.
//
// WHOLE MACHINE (the default for a new install on Linux). The Host is the
// machine's, as the bastion beside it is (it runs as the system service
// privatae-bastion): its VM runs as the system account "cseries" from a
// system unit (cseries.service, started at boot with nobody signed in), and
// everything of it is in /var/lib/cseries (platform.js machinePaths). No
// account's home holds any of it, so removing an account leaves the Host and
// the bastion working together, and uninstalling from any administrator's
// account removes all of it. This command then runs as root: the person runs
// npx cseries as themselves, and it hands itself to sudo once, at the start
// (reexecArgs), so sudo asks for the password once for the whole run.
//
// JUST ME (--just-me, and every install made before whole-machine ones). The
// VM is under the account's own folders and starts at that account's login,
// as earlier releases did. An existing one keeps working as it is: a run that
// finds one uses it.
//
// macOS and Windows keep their per-account VM: there scope is always 'user'.

const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { MACHINE_BASE, MACHINE_UNIT } = require('./platform');

const JUST_ME = '--just-me';

// Commands that never touch an install and never need sudo.
const LOCAL_COMMANDS = new Set(['help', '--help', '-h', 'version', '--version', '-v']);

function exists(p, fsx = fs) {
  try {
    return fsx.existsSync(p);
  } catch {
    return false;
  }
}

// state.json "unfinished": a whole-machine install that started and has not
// finished. The root run's up (cli.js) writes it first, before anything else
// of the machine's is made, and clears it once the install has finished; a
// run that stops in between (an error, ^C, the power) leaves it. Such a
// record is no install: what it left (the unit, lima/, the system account)
// is not a Host that runs. Every reader says so and offers to finish it
// (npx cseries) or to remove what it left (uninstall --delete-data).
const UNFINISHED = 'unfinished';

function readMachineState(fsx) {
  try {
    const st = JSON.parse(fsx.readFileSync(path.posix.join(MACHINE_BASE, 'state.json'), 'utf8'));
    return st && typeof st === 'object' ? st : null;
  } catch {
    return null;
  }
}

// The whole machine's record says its install did not finish.
function machineUnfinished(fsx = fs) {
  const st = readMachineState(fsx);
  return Boolean(st && st[UNFINISHED]);
}

// A record that holds only the bastion's: uninstall --delete-data removed
// the Host and its VM (the unit and lima/ are gone) and kept this command's
// record of the bastion it installed, for the next install (--keep-bastion,
// or a bastion it could not remove; cli.js uninstall). Such a record is no
// install: nothing of a Host is here. The next install reads it, so the
// bastion is reused, not reinstalled: the machine's own (the root run reads
// its state.json) or an account's (--just-me, cli.js adoptKeptBastion).
// st: the record; unit and lima: where the machine's are (platform.js
// machinePaths, a test's own folders in a test). -> the bastion's record, or
// null.
function keptBastionRecord(st, { unit = MACHINE_UNIT, lima = path.posix.join(MACHINE_BASE, 'lima') } = {}, fsx = fs) {
  if (!st || typeof st !== 'object' || st[UNFINISHED]) return null;
  const b = st.bastion;
  if (!b || typeof b !== 'object' || !b.service) return null;
  if (exists(unit, fsx) || exists(lima, fsx)) return null;
  return b;
}

// The whole machine's record is only the bastion's (keptBastionRecord).
// -> the bastion's record, or null.
function machineKeptBastion(fsx = fs) {
  return keptBastionRecord(readMachineState(fsx), {}, fsx);
}

// The whole machine's record, when it holds only the bastion's
// (keptBastionRecord), for an account's install that takes that bastion over
// (cli.js adoptKeptBastion): -> {bastion, vmUserCreated, sha256} or null.
// sha256 is of the file as read here, so the root step that clears it
// (posix/elevated.js machineRecordBlock) clears this record and no other.
function machineKeptRecord(fsx = fs) {
  let raw;
  let st;
  try {
    raw = fsx.readFileSync(path.posix.join(MACHINE_BASE, 'state.json'));
    st = JSON.parse(raw.toString('utf8'));
  } catch {
    return null;
  }
  const bastion = keptBastionRecord(st, {}, fsx);
  if (!bastion) return null;
  return { bastion, vmUserCreated: Boolean(st.vmUserCreated), sha256: crypto.createHash('sha256').update(raw).digest('hex') };
}

// A whole-machine install is here: its unit, or its folder with a VM or a
// record in it, and the record does not say it did not finish, nor hold only
// the bastion's (keptBastionRecord).
function machineInstalled(fsx = fs) {
  if (machineUnfinished(fsx)) return false;
  if (exists(MACHINE_UNIT, fsx) || exists(path.posix.join(MACHINE_BASE, 'lima'), fsx)) return true;
  return exists(path.posix.join(MACHINE_BASE, 'state.json'), fsx) && !machineKeptBastion(fsx);
}

// This account's own install (paths: platform.js paths for the account).
function userInstalled(p, fsx = fs) {
  return exists(path.posix.join(p.lima, 'cseries'), fsx) || exists(p.autostart, fsx);
}

// -> 'machine' | 'user'. A run as root is always the machine's (Lima will not
// run as root for an account). Otherwise: the install that is here; with none,
// the whole machine for a command that installs, unless --just-me; and the
// account's own for every other command (status says nothing is installed;
// uninstall still finds the bastion, which is the machine's either way).
function chooseScope({ platform, uid, cmd, args = [], machine, user }) {
  if (platform !== 'linux') return 'user';
  if (uid === 0) return 'machine';
  if (LOCAL_COMMANDS.has(cmd)) return 'user';
  if (machine) return 'machine';
  if (user) return 'user';
  if (args.includes(JUST_ME)) return 'user';
  return cmd === '' || cmd === 'update' ? 'machine' : 'user';
}

// The variables the run as root keeps: sudo drops the environment, and these
// are this command's own settings (CSERIES_RELEASE_JSON for a local test).
function keptEnv(env) {
  return Object.keys(env).filter((k) => /^CSERIES_[A-Z0-9_]+$/.test(k)).sort().map((k) => `${k}=${env[k]}`);
}

// sudo's argument list for this same command as root: /usr/bin/env sets the
// kept variables (sudo's own VAR=value needs the policy's setenv), then this
// Node and this package's bin run the same arguments.
function reexecArgs({ prompt, env, node, bin, argv }) {
  return ['-p', prompt, '--', '/usr/bin/env', ...keptEnv(env), node, bin, ...argv];
}

module.exports = { chooseScope, machineInstalled, machineUnfinished, machineKeptBastion, machineKeptRecord, keptBastionRecord, userInstalled, UNFINISHED, reexecArgs, keptEnv, JUST_ME, LOCAL_COMMANDS };
