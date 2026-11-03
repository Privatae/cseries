'use strict';

// Moving an account's own install on Linux to the whole machine (move.js,
// cli.js): offered once, moved with everything on a yes, untouched on a no,
// undone when it does not finish. On the fake machine of fake-lima.js, with
// one account's install and the whole machine's beside it; the copy script
// runs for real on the test's own folders. Everything here is invented test
// data.

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { main } = require('../src/cli');
const move = require('../src/move');
const platform = require('../src/platform');
const { captureOutput, tmpdir, validRelease, fakeRunner } = require('./helpers');
const { fakeLima, machineLimaPaths } = require('./fake-lima');

// One machine: the whole machine's paths (as /var/lib/cseries) and the
// account "someone" with its own install where platform.js puts one, under
// its home; the bastion's program and unit are the test's, never this
// computer's.
function setup(opts = {}) {
  const dir = tmpdir();
  const mp = machineLimaPaths(dir);
  const home = path.join(dir, 'home', 'someone');
  fs.mkdirSync(home, { recursive: true });
  const ap = {
    ...platform.paths('linux', {}, home),
    serviceDir: mp.serviceDir, serviceExe: mp.serviceExe, serviceUnit: mp.serviceUnit, bastionDir: mp.bastionDir, bastionExe: mp.bastionExe,
  };
  const account = { name: 'someone', home, paths: ap };
  const backed = opts.backedIntoAccount ? { backedBy: path.join(ap.lima, 'cseries', 'basedisk') } : {};
  // The account's systemd user manager, signed in: its bus in its runtime
  // folder (opts.noUserManager: none runs for it).
  const accountRunDir = path.join(dir, 'run-user', '1000');
  fs.mkdirSync(accountRunDir, { recursive: true });
  if (!opts.noUserManager) fs.writeFileSync(path.join(accountRunDir, 'bus'), '');
  const m = fakeLima(mp, { platform: 'linux', root: path.join(dir, 'machine'), qemu: true, account, bastionVersion: '0.62.0', accountRunDir, ...backed, ...opts });
  const rel = validRelease();
  for (const k of ['lima-x64', 'lima-arm64']) rel.guest[k] = { ...rel.guest[k], sha256: m.qcow2Sha256 };
  const base = {
    platform: 'linux', arch: 'x64', runner: m.runner, net: m.net, sleep: async () => {}, portState: async () => 'free',
    freeBytes: () => 64 * 1024 ** 3, releaseInfo: { release: rel, placeholders: [], file: 'test' }, bastionKeys: m.bastionKeys,
    sudoPaths: m.sudoPaths, runuserPaths: m.runuserPaths, hostname: 'test-machine', accountName: 'someone',
    userRunDir: path.join(dir, 'run-user'),
  };
  // As the account, on its own install.
  const asAccount = async (argv = [], extra = {}) => {
    m.uid = 1000;
    const out = captureOutput();
    const code = await main(argv, { ...base, uid: 1000, env: {}, paths: ap, out, prompt: async () => false, isTTY: false, installed: { machine: false, user: true }, ...extra });
    return { code, text: out.text(), err: out.errText() };
  };
  // The run as root sudo starts for it, with the arguments asMachine gives.
  const asRoot = async (argv = [], extra = {}) => {
    m.uid = 0;
    const out = captureOutput();
    const code = await main(argv, { ...base, uid: 0, env: { SUDO_USER: 'someone' }, paths: mp, out, prompt: async () => false, ...extra });
    return { code, text: out.text(), err: out.errText() };
  };
  return { m, mp, ap, home, account, asAccount, asRoot };
}

// The account's install, paired with the bastion, anchored and enrolled.
async function accountInstall(opts) {
  const x = setup(opts);
  const r = await x.asAccount(['--just-me']);
  assert.equal(r.code, 0, r.text + r.err);
  assert.equal(x.m.accountInstance().status, 'Running');
  const st = JSON.parse(fs.readFileSync(x.ap.state, 'utf8'));
  assert.equal(st.bastion.anchored, true);
  x.before = st;
  return x;
}

test('the offer: once, in a terminal, on a plain npx cseries; a no keeps the account\'s install and is never asked again', async () => {
  const x = await accountInstall();
  const asked = [];
  const sudoRuns = [];
  const runner = fakeRunner((cmd, args, o) => {
    if (o.inherit) sudoRuns.push(args);
    return x.m.runner.run(cmd, args, o).then((r) => ({ code: r.code, stdout: r.stdout, stderr: r.stderr, notFound: r.notFound }));
  });
  // No terminal: not asked, not recorded.
  let r = await x.asAccount([], { isTTY: false, prompt: async (q) => { asked.push(q); return true; } });
  assert.equal(r.code, 0, r.err);
  assert.equal(asked.length, 0);
  assert.equal(JSON.parse(fs.readFileSync(x.ap.state, 'utf8')).machineOffer, undefined);
  // A terminal, answered no: the account's install goes on as it is.
  r = await x.asAccount([], { isTTY: true, runner, prompt: async (q) => { asked.push(q); return false; } });
  assert.equal(r.code, 0, r.err);
  assert.equal(asked.length, 1);
  assert.match(asked[0], /^Move it to the whole machine\? Type y to move it/);
  assert.match(r.text, /It can move to the whole machine instead: it then runs\nas a system service of its own, starts when the computer starts with nobody signed in/);
  assert.match(r.text, /It stays for your account only\. To move it later: npx cseries --whole-machine/);
  assert.equal(sudoRuns.length, 0, 'nothing handed to sudo');
  assert.equal(x.m.accountInstance().status, 'Running');
  assert.equal(JSON.parse(fs.readFileSync(x.ap.state, 'utf8')).machineOffer, 'declined');
  // Asked once.
  r = await x.asAccount([], { isTTY: true, runner, prompt: async (q) => { asked.push(q); return true; } });
  assert.equal(asked.length, 1, 'never asked again');
  assert.equal(sudoRuns.length, 0);
  // Not for other commands, nor with --just-me.
  const y = await accountInstall();
  for (const argv of [['status'], ['update'], ['--just-me']]) {
    await y.asAccount(argv, { isTTY: true, prompt: async (q) => { asked.push(q); return true; } });
  }
  assert.equal(asked.length, 1);
});

test('a yes hands the move to sudo once, with where the account\'s install is, and opens the browser as the account', async () => {
  const x = await accountInstall();
  const runner = fakeRunner((cmd, args, o) => (o.inherit ? { code: 0 } : { code: 1, notFound: true }));
  const r = await x.asAccount([], {
    isTTY: true, prompt: async () => true, runner, env: { CSERIES_RELEASE_JSON: '/tmp/r.json', DISPLAY: ':0' },
    net: { probe: async () => ({ status: 200, location: '' }) }, binPath: '/pkg/bin/cseries.js', nodePath: '/usr/bin/node',
  });
  assert.equal(r.code, 0, r.text + r.err);
  const call = runner.calls.find((c) => c.kind === 'inherit');
  assert.deepEqual(call.args.slice(-4), ['/pkg/bin/cseries.js', '--move-from', x.ap.base, x.ap.autostart]);
  assert.match(r.text, /Moving C-Series to the whole machine\. sudo asks for your password once/);
  assert.equal(JSON.parse(fs.readFileSync(x.ap.state, 'utf8')).machineOffer, 'accepted');
  assert.ok(runner.calls.some((c) => c.kind === 'detached' && c.cmd === 'xdg-open'));
  // sudo refused (a wrong password three times): how to move it later is said.
  const z = await accountInstall();
  const no = fakeRunner((cmd, args, o) => (o.inherit ? { code: 1 } : { code: 1, notFound: true }));
  const rz = await z.asAccount([], { isTTY: true, prompt: async () => true, runner: no });
  assert.equal(rz.code, 1);
  assert.match(rz.text, /C-Series was not moved\. To try again: npx cseries --whole-machine/);
  // --whole-machine asks nothing, and is not passed on.
  const y = await accountInstall();
  const r2 = fakeRunner((cmd, args, o) => (o.inherit ? { code: 0 } : { code: 1, notFound: true }));
  await y.asAccount(['--whole-machine'], { runner: r2, prompt: async () => { throw new Error('asked'); }, net: { probe: async () => ({ status: 200, location: '' }) } });
  const c2 = r2.calls.find((c) => c.kind === 'inherit');
  assert.ok(c2.args.includes('--move-from'));
  assert.ok(!c2.args.includes('--whole-machine'));
});

test('the move: the same Host, its data, pairing and anchor, runs for the whole machine; the account\'s copy goes only after', async () => {
  const x = await accountInstall();
  const { m, mp, ap } = x;
  const disk = fs.readFileSync(path.join(ap.lima, 'cseries', 'disk'), 'utf8');
  const bastionCalls = m.bastionCalls().length;
  const r = await x.asRoot(['--move-from', ap.base, ap.autostart], {});
  assert.equal(r.code, 0, r.text + r.err);
  // The moved VM: the same disk, Lima's key, no pid of the old run, given to the VM's account.
  assert.equal(fs.readFileSync(path.join(mp.lima, 'cseries', 'disk'), 'utf8'), disk, 'the Host\'s disk, byte for byte');
  assert.equal(fs.readFileSync(path.join(mp.lima, '_config', 'user'), 'utf8'), 'invented ssh key', 'Lima\'s own key, which the guest trusts');
  assert.ok(!fs.existsSync(path.join(mp.lima, 'cseries', 'ha.pid')) || m.instance.status === 'Running');
  assert.ok(m.chowns().some((c) => /^-R cseries:cseries .*\.move$/.test(c)), m.chowns().join('\n'));
  assert.ok(!fs.existsSync(path.join(mp.lima, '.move')));
  // It runs, from the system unit, as the VM's account.
  assert.equal(m.instance.status, 'Running');
  assert.ok(m.systemUnits.includes('start cseries.service'));
  assert.ok(m.systemUnits.includes('enable cseries.service'));
  assert.match(r.text, /It is the same Host: the same CA, so its pairings and the bastion's anchor hold\./);
  // The bastion: no new pairing, no anchor released, nothing asked for.
  const st = JSON.parse(fs.readFileSync(mp.state, 'utf8'));
  assert.deepEqual(st.bastion, x.before.bastion, 'the record of the bastion carried as it was');
  assert.deepEqual(st.packagesAdded, x.before.packagesAdded);
  assert.ok(!m.bastionCalls().slice(bastionCalls).some((c) => /^(pair|release-anchor|claim-anchor|unpair)/.test(c.args.join(' '))), 'the pairing and anchor are untouched');
  assert.equal(m.bastionState().anchor || 'host-a', 'host-a');
  // The account's copy and login unit are gone; nothing of it runs.
  assert.equal(m.accountInstance().status, 'Stopped');
  assert.ok(!fs.existsSync(ap.base), `${ap.base} is removed`);
  assert.ok(!fs.existsSync(ap.autostart), 'its login unit');
  assert.ok(!fs.existsSync(path.join(path.dirname(ap.autostart), 'default.target.wants', 'cseries.service')));
  assert.equal(st.movedFrom, undefined);
  assert.match(r.text, /Removed the copy in .* and the login unit of someone\./);
  // And a run as root afterwards is a plain whole-machine run.
  const again = await x.asRoot([]);
  assert.equal(again.code, 0, again.text + again.err);
});

test('the move stops the account\'s VM before it copies, and checks the moved Host before it removes anything', async () => {
  const x = await accountInstall();
  const seen = {};
  x.m.beforeMove = () => {
    seen.accountAtCopy = x.m.accountInstance().status;
    // A pid file a run that crashed left: the stopped VM's, never copied.
    fs.writeFileSync(path.join(x.ap.lima, 'cseries', 'qemu.pid'), '999');
  };
  x.m.afterMove = () => { seen.accountFilesAfterCopy = fs.existsSync(path.join(x.ap.lima, 'cseries', 'disk')); };
  const r = await x.asRoot(['--move-from', x.ap.base, x.ap.autostart]);
  assert.equal(r.code, 0, r.text + r.err);
  assert.equal(seen.accountAtCopy, 'Stopped', 'never copied while it runs');
  assert.equal(seen.accountFilesAfterCopy, true, 'copied, not moved');
  assert.ok(!fs.existsSync(path.join(x.mp.lima, 'cseries', 'qemu.pid')), 'no pid file of the account\'s VM in the copy');
  const order = ['Moving the Host\'s VM', 'Starting the Linux system', 'Checking the moved Host and removing the copy in your account', 'Installing and pairing the bastion'];
  const at = order.map((t) => r.text.indexOf(t));
  assert.ok(at.every((i, n) => i > 0 && (n === 0 || i > at[n - 1])), at.join(','));
});

test('a moved Host that is not the same Host is undone: the account\'s install runs again, as it was, and nothing of the machine\'s is left', async () => {
  const x = await accountInstall();
  const { m, mp, ap } = x;
  const stateBefore = fs.readFileSync(ap.state, 'utf8');
  // The copy lands, but its disk is not the account's Host.
  m.afterMove = () => {
    fs.writeFileSync(path.join(mp.lima, 'cseries', 'disk'), JSON.stringify({ hostCa: '-----BEGIN CERTIFICATE-----\nMIIBother\n-----END CERTIFICATE-----' }));
  };
  const r = await x.asRoot(['--move-from', ap.base, ap.autostart]);
  assert.equal(r.code, 1);
  assert.match(r.err, /The moved Host is not the same Host/);
  assert.match(r.text, /The move did not finish, so it is undone\./);
  assert.match(r.text, /C-Series runs for someone only, as it did before/);
  // The account's install: started again, its files and record as they were.
  assert.equal(m.accountInstance().status, 'Running');
  // Through its own login unit, by its own user manager (so supervised as
  // before the move), and Lima asked until it runs: not by a limactl of root's.
  assert.deepEqual(m.accountUnit, ['start cseries.service', 'is-active cseries.service']);
  assert.equal(m.accountStarts, undefined, 'no limactl start outside the unit');
  assert.match(r.text, /The VM of someone runs again, from its login unit\./);
  assert.ok(fs.existsSync(path.join(ap.lima, 'cseries', 'disk')));
  assert.ok(fs.existsSync(ap.autostart), 'its login unit stays');
  assert.equal(fs.readFileSync(ap.state, 'utf8'), stateBefore);
  // Nothing of the whole machine's.
  assert.ok(!fs.existsSync(mp.base), `${mp.base} is removed`);
  assert.ok(!fs.existsSync(mp.autostart), 'no system unit');
  assert.equal(m.vmUser, null, 'the system account it made is removed');
  assert.ok(m.systemUnits.includes('disable --now cseries.service'));
});

test('an undone move with no user manager running for the account starts its VM through Lima, as the account, and says so; a unit that fails is said', async () => {
  const x = await accountInstall({ noUserManager: true });
  x.m.afterMove = () => {
    fs.writeFileSync(path.join(x.mp.lima, 'cseries', 'disk'), JSON.stringify({ hostCa: '-----BEGIN CERTIFICATE-----\nMIIBother\n-----END CERTIFICATE-----' }));
  };
  const r = await x.asRoot(['--move-from', x.ap.base, x.ap.autostart]);
  assert.equal(r.code, 1);
  assert.equal(x.m.accountInstance().status, 'Running');
  assert.equal(x.m.accountUnit, undefined, 'no manager to ask');
  assert.deepEqual(x.m.accountStarts.map((s) => s.as), ['someone'], 'started as the account, through runuser');
  assert.match(r.text, /The VM of someone runs again, started by Lima directly: no systemd user manager runs for someone\./);
  assert.match(r.text, /C-Series runs for someone only, as it did before/);
  // The unit there, but it fails: said, with where to look, and not said to run.
  const y = await accountInstall({ accountUnitFails: true });
  y.m.afterMove = () => {
    fs.writeFileSync(path.join(y.mp.lima, 'cseries', 'disk'), JSON.stringify({ hostCa: '-----BEGIN CERTIFICATE-----\nMIIBother\n-----END CERTIFICATE-----' }));
  };
  const ry = await y.asRoot(['--move-from', y.ap.base, y.ap.autostart]);
  assert.equal(ry.code, 1);
  assert.match(ry.err, /The VM of someone did not start again from its login unit \(cseries\.service failed\)\. Run npx cseries as someone to start it/);
  assert.doesNotMatch(ry.text, /C-Series runs for someone only, as it did before/);
});

test('a move undone where the machine\'s folder was already there (a log of an earlier run) leaves no state.json, lima/ or account behind', async () => {
  const x = await accountInstall();
  const { m, mp, ap } = x;
  // An earlier sudo npx cseries that stopped before it installed anything
  // left only its log: no state.json, no lima/, no unit, so this is not an
  // install for the whole machine, and the move is offered.
  fs.mkdirSync(mp.base, { recursive: true });
  fs.writeFileSync(mp.log, 'an earlier run\n');
  m.afterMove = () => {
    fs.writeFileSync(path.join(mp.lima, 'cseries', 'disk'), JSON.stringify({ hostCa: '-----BEGIN CERTIFICATE-----\nMIIBother\n-----END CERTIFICATE-----' }));
  };
  const r = await x.asRoot(['--move-from', ap.base, ap.autostart]);
  assert.equal(r.code, 1);
  assert.match(r.text, /The move did not finish, so it is undone\./);
  assert.equal(m.accountInstance().status, 'Running');
  // What scope.js machineInstalled looks at: none of it is left.
  assert.ok(!fs.existsSync(mp.state), 'no state.json: a later run would take the machine as installed');
  assert.ok(!fs.existsSync(mp.lima), 'no lima/: the same');
  assert.ok(!fs.existsSync(mp.hostCa));
  assert.ok(!fs.existsSync(mp.vmHome));
  assert.equal(m.vmUser, null, 'the system account the move made is removed');
  // What was there before stays.
  assert.ok(fs.existsSync(mp.log));
});

test('a Host whose CA cannot be known (its VM stopped, no record of it) is not moved, so its copy is never removed unchecked', async () => {
  const x = await accountInstall();
  const { m, mp, ap } = x;
  const st = JSON.parse(fs.readFileSync(ap.state, 'utf8'));
  delete st.bastion.hostCa;
  delete st.pairedHostCa;
  fs.writeFileSync(ap.state, JSON.stringify(st));
  m.accountInstance().status = 'Stopped';
  const r = await x.asRoot(['--move-from', ap.base, ap.autostart]);
  assert.equal(r.code, 1, r.text + r.err);
  assert.match(r.err, /did not say which Host it is \(its CA\).*Nothing was moved\./);
  assert.ok(fs.existsSync(path.join(ap.lima, 'cseries', 'disk')), 'the account\'s copy stays');
  assert.ok(fs.existsSync(ap.autostart), 'its login unit stays');
  assert.ok(!fs.existsSync(mp.base));
});

test('a copy that cannot be made (no room) is undone before anything of the account\'s is lost', async () => {
  const x = await accountInstall({ diskFull: true });
  const r = await x.asRoot(['--move-from', x.ap.base, x.ap.autostart]);
  assert.equal(r.code, 1);
  assert.match(r.err, /The VM could not be copied: the copy needs \d+ GB free/);
  assert.equal(x.m.accountInstance().status, 'Running');
  assert.ok(!fs.existsSync(x.mp.base));
  assert.ok(fs.existsSync(path.join(x.ap.lima, 'cseries', 'disk')));
});

test('a disk backed by a file in the account\'s folder is refused before it is copied, and the account\'s VM runs again', async () => {
  const x = await accountInstall({ backedIntoAccount: true });
  const r = await x.asRoot(['--move-from', x.ap.base, x.ap.autostart]);
  assert.equal(r.code, 1);
  assert.match(r.err, /The VM could not be copied: the VM's disk .* is backed by a file in /);
  assert.ok(!fs.existsSync(path.join(x.mp.lima, 'cseries')) && !fs.existsSync(x.mp.base));
  assert.equal(x.m.accountInstance().status, 'Running');
  const s = move.copyScript({ paths: { lima: '/home/someone/.local/share/cseries/lima' } }, { paths: { lima: '/var/lib/cseries/lima', vmUser: 'cseries' } });
  assert.ok(s.indexOf('backing file') < s.indexOf('cp -a'), 'checked before anything is copied');
});

test('the root run takes only the account sudo ran it for, and only that account\'s own install', async () => {
  const x = await accountInstall();
  const tries = [
    [{ env: {} }, [x.ap.base, x.ap.autostart], /sudo named none/],
    [{ env: { SUDO_USER: 'root' } }, [x.ap.base, x.ap.autostart], /sudo named none/],
    [{ env: { SUDO_USER: 'nobody-here' } }, [x.ap.base, x.ap.autostart], /not in this machine's account database/],
    [{}, ['/etc', x.ap.autostart], /not the folders of an account's install/],
    [{}, [path.join(x.home, '..', 'other', '.local', 'share', 'cseries'), x.ap.autostart], /not folders in someone's home/],
  ];
  for (const [extra, where, re] of tries) {
    const r = await x.asRoot(['--move-from', ...where], extra);
    assert.equal(r.code, 1, JSON.stringify(where));
    assert.match(r.err, re);
    assert.match(r.err, /Nothing was changed\./);
  }
  assert.ok(!fs.existsSync(x.mp.base), 'nothing made');
  assert.equal(x.m.accountInstance().status, 'Running');
  // A whole-machine install that did not finish is here: its record is never
  // mixed with the account's, nor removed by an undone move.
  fs.mkdirSync(x.mp.base, { recursive: true });
  const unfinished = `${JSON.stringify({ unfinished: true, vmUserCreated: true })}\n`;
  fs.writeFileSync(x.mp.state, unfinished);
  const r = await x.asRoot(['--move-from', x.ap.base, x.ap.autostart]);
  assert.equal(r.code, 1);
  assert.match(r.err, /an install of C-Series for the whole machine started here and did not finish .* Nothing was changed\./);
  assert.match(r.err, /Remove what it left \(npx cseries uninstall --delete-data --whole-machine\), then run npx cseries --whole-machine again as someone\./);
  assert.equal(fs.readFileSync(x.mp.state, 'utf8'), unfinished);
  assert.equal(x.m.accountInstance().status, 'Running', 'the account\'s VM never stopped');
});

test('the copy script: as root, sparse, sockets and pid files left behind, the VM account\'s, put in place by rename', () => {
  const s = move.copyScript(
    { paths: { lima: '/home/someone/.local/share/cseries/lima' } },
    { paths: { lima: '/var/lib/cseries/lima', vmUser: 'cseries' } },
  );
  assert.match(s, /^PATH=\/usr\/sbin:\/usr\/bin:\/sbin:\/bin$/m);
  assert.match(s, /^cp -a --sparse=always "\$src\/cseries" "\$stage\/cseries"$/m);
  assert.match(s, /find "\$stage" \\\( -type s -o -name "\*\.pid" \\\) -exec rm -f \{\} \+/);
  const chown = s.indexOf('chown -R "$u:$u" "$stage"');
  const into = s.indexOf('mv "$stage/cseries" "$dst/cseries"');
  assert.ok(chown > 0 && into > chown, 'given to the account before it is in place');
  assert.doesNotMatch(s, /rm -rf "\$src|rm -rf '\/home/, 'the account\'s copy is only read');
});

// scope.js reads the real /var/lib/cseries and /etc/systemd/system: here the
// fake machine's folders stand in for them.
function machineFs(mp) {
  const at = (f) => f.replace(platform.MACHINE_UNIT, mp.autostart).replace(platform.MACHINE_BASE, mp.base);
  return { existsSync: (f) => fs.existsSync(at(f)), readFileSync: (f, e) => fs.readFileSync(at(f), e) };
}

// A whole-machine install, then uninstall --delete-data --keep-bastion: what
// is left is the record of the bastion only.
async function keptBastion(opts) {
  const x = setup(opts);
  let r = await x.asRoot([]);
  assert.equal(r.code, 0, r.text + r.err);
  r = await x.asRoot(['uninstall', '--delete-data', '--keep-bastion', '--yes']);
  assert.equal(r.code, 0, r.text + r.err);
  assert.match(r.text, /this command's record of the bastion is kept, for the next install/);
  const st = JSON.parse(fs.readFileSync(x.mp.state, 'utf8'));
  assert.equal(st.bastion.service, true);
  assert.ok(!fs.existsSync(x.mp.lima) && !fs.existsSync(x.mp.autostart), 'no VM, no unit');
  // The next Host is a new one, with a CA of its own.
  x.m.hostCa = '-----BEGIN CERTIFICATE-----\nMIIBexample1\n-----END CERTIFICATE-----';
  x.mfs = machineFs(x.mp);
  x.kept = fs.readFileSync(x.mp.state, 'utf8');
  return x;
}

const elevatedArgs = (m, from) => m.bastionCalls().slice(from).map((c) => c.args[0]);

test('a record that holds only the kept bastion is no install: every reader says so, and --just-me installs for the account and reuses that bastion', async () => {
  const x = await keptBastion();
  const { m, mp, ap, mfs } = x;
  const scope = require('../src/scope');
  assert.equal(scope.machineInstalled(mfs), false, 'no Host, no VM: not installed');
  assert.equal(scope.machineUnfinished(mfs), false);
  assert.equal(scope.machineKeptBastion(mfs).service, true);
  // The root run's own readers.
  const s = await x.asRoot(['status']);
  assert.equal(s.code, 0);
  assert.match(s.text, /Linux system \(Lima\): not installed/);
  assert.match(s.text, /paired with this Host: no/);
  for (const cmd of ['start', 'stop', 'open', 'logs']) {
    const c = await x.asRoot([cmd]);
    assert.equal(c.code, 1, cmd);
    assert.match(c.err, /C-Series is not installed on this machine\./, cmd);
  }
  // As the account, what is installed read from the machine: --just-me is
  // the account's own install, not handed to the run as root.
  const bastionProgram = fs.readFileSync(mp.serviceExe);
  const from = m.bastionCalls().length;
  const r = await x.asAccount(['--just-me'], { installed: undefined, machineFs: mfs });
  assert.equal(r.code, 0, r.text + r.err);
  assert.equal(m.accountInstance().status, 'Running');
  const st = JSON.parse(fs.readFileSync(ap.state, 'utf8'));
  assert.equal(st.bastion.service, true, 'the kept bastion is this command\'s own');
  assert.ok(st.bastion.hostCa && st.bastion.anchored, 'paired with the account\'s Host and anchored to it');
  // Reused: its program and service as they were; paired with the new Host.
  assert.ok(!/install -o /.test(m.sudoRuns.filter((s) => s.kind === 'bastion').at(-1).script), 'the program is not installed again');
  assert.deepEqual(fs.readFileSync(mp.serviceExe), bastionProgram);
  assert.ok(elevatedArgs(m, from).includes('pair'));
  assert.ok(!elevatedArgs(m, from).includes('service'), 'no service install');
  assert.ok(elevatedArgs(m, from).includes('approvals-owner'), 'this account recorded as its owner');
  // The account's record is its own from now on: a later run leaves it as it is.
  const again = m.bastionCalls().length;
  const r2 = await x.asAccount(['update'], { installed: undefined, machineFs: mfs });
  assert.equal(r2.code, 0, r2.text + r2.err);
  assert.deepEqual(elevatedArgs(m, again), [], 'nothing paired again');
  assert.deepEqual(JSON.parse(fs.readFileSync(ap.state, 'utf8')).bastion, st.bastion);
  // Taken over: the machine's record is cleared in that same sudo run, before
  // anything is paired, with what its uninstall kept only for it.
  const takeover = m.sudoRuns.filter((s) => s.kind === 'bastion');
  assert.equal(takeover.filter((s) => /step=machine-record/.test(s.script)).length, 1, 'one sudo run, the bastion\'s');
  const t = takeover.find((s) => /step=machine-record/.test(s.script)).script;
  const at = (re) => t.search(re);
  assert.ok(at(/step=machine-record/) > 0 && at(/step=machine-record/) < at(/'approvals-owner' '--account'/) && at(/'approvals-owner' '--account'/) < at(/'pair' '--redeem-url'/), 'cleared before anything is recorded or paired');
  assert.ok(!fs.existsSync(mp.state) && !fs.existsSync(mp.base), 'the machine\'s record and folder are gone');
  assert.equal(m.vmUser, null, 'the system account the machine\'s install made is removed');
  assert.deepEqual(m.userdels(), ['cseries']);
  assert.equal(scope.machineKeptBastion(mfs), null);
  assert.equal(scope.machineInstalled(mfs), false);
  // The whole machine's uninstall leaves the account's bastion as it is, and
  // its next install does not pair it with a Host of its own.
  const paired = m.bastionState();
  const u = await x.asRoot(['uninstall', '--delete-data', '--yes']);
  assert.equal(u.code, 0, u.text + u.err);
  assert.match(u.text, /This machine's bastion was not installed by npx cseries from this install, so it stays as it is\./);
  assert.deepEqual(m.bastionState(), paired, 'not removed, not unpaired');
  const before = m.bastionCalls().length;
  const w = await x.asRoot([]);
  assert.equal(w.code, 1, w.text + w.err);
  assert.match(w.err, /already runs the bastion as a system service that this command did not install/);
  assert.deepEqual(elevatedArgs(m, before), [], 'not paired with the whole machine\'s Host');
  assert.deepEqual(m.bastionState(), paired);
  // uninstall, as an account with nothing of its own: the machine's, through sudo.
  const y = await keptBastion();
  const runner = fakeRunner((cmd, args, o) => (o.inherit ? { code: 0 } : { code: 1, notFound: true }));
  await y.asAccount(['uninstall', '--delete-data'], { installed: undefined, machineFs: y.mfs, runner, binPath: '/pkg/bin/cseries.js', nodePath: '/usr/bin/node' });
  const calls = runner.calls.filter((c) => c.kind === 'inherit').map((c) => c.args.slice(c.args.indexOf('/pkg/bin/cseries.js') + 1));
  assert.deepEqual(calls, [['uninstall', '--delete-data']]);
  // With an install of the account's own: the account's, unless --whole-machine.
  const sudoFor = async (argv) => {
    const rn = fakeRunner((cmd, args, o) => (o.inherit ? { code: 0 } : { code: 1, notFound: true }));
    await y.asAccount(argv, { installed: { machine: false, user: true, unfinished: false, kept: true }, runner: rn, prompt: async () => false, binPath: '/pkg/bin/cseries.js', nodePath: '/usr/bin/node' });
    return rn.calls.filter((c) => c.kind === 'inherit').map((c) => c.args.slice(c.args.indexOf('/pkg/bin/cseries.js') + 1));
  };
  assert.deepEqual(await sudoFor(['uninstall', '--delete-data', '--whole-machine']), [['uninstall', '--delete-data']]);
  assert.deepEqual(await sudoFor(['uninstall']), []);
});

test('which records hold only the kept bastion\'s', () => {
  const scope = require('../src/scope');
  const dir = tmpdir();
  const where = { unit: path.join(dir, 'cseries.service'), lima: path.join(dir, 'lima') };
  const b = { service: true, host: 'host-a' };
  assert.deepEqual(scope.keptBastionRecord({ bastion: b, vmUserCreated: true }, where), b);
  assert.equal(scope.keptBastionRecord({ bastion: b, unfinished: true }, where), null, 'an install that did not finish');
  assert.equal(scope.keptBastionRecord({ bastion: { host: 'host-a' } }, where), null, 'a bastion this command did not install');
  assert.equal(scope.keptBastionRecord({ packagesAdded: {} }, where), null);
  assert.equal(scope.keptBastionRecord(null, where), null);
  fs.writeFileSync(where.unit, 'unit');
  assert.equal(scope.keptBastionRecord({ bastion: b }, where), null, 'its unit is here');
  fs.rmSync(where.unit);
  fs.mkdirSync(where.lima);
  assert.equal(scope.keptBastionRecord({ bastion: b }, where), null, 'its VM\'s folder is here');
});

test('the whole machine\'s next install over the kept bastion\'s record: marked unfinished until it finishes, and the bastion reused', async () => {
  const x = await keptBastion();
  const { m, mp, mfs } = x;
  const scope = require('../src/scope');
  // The system account stays; its LIMA_HOME, which the uninstall removed, is made again for it.
  assert.ok(m.vmUser);
  const vmUserRuns = m.sudoRuns.filter((s) => s.kind === 'vm-user').length;
  // Stopped once its VM is made: no install, and said to be unfinished.
  const noStart = fakeRunner((cmd, args, o) => (cmd === 'systemctl' && args.join(' ') === 'start cseries.service'
    ? { code: 1, stderr: 'Job for cseries.service failed (test).' } : m.runner.run(cmd, args, o)));
  let r = await x.asRoot([], { runner: noStart });
  assert.equal(r.code, 1, r.text + r.err);
  assert.equal(m.sudoRuns.filter((s) => s.kind === 'vm-user').length, vmUserRuns + 1, 'the account\'s folders set up again');
  assert.equal(JSON.parse(fs.readFileSync(mp.state, 'utf8')).unfinished, true);
  assert.equal(scope.machineInstalled(mfs), false);
  assert.equal(scope.machineUnfinished(mfs), true);
  // Finished: an install, the bastion paired with the new Host, not reinstalled.
  const from = m.bastionCalls().length;
  r = await x.asRoot([]);
  assert.equal(r.code, 0, r.text + r.err);
  const st = JSON.parse(fs.readFileSync(mp.state, 'utf8'));
  assert.equal(st.unfinished, undefined);
  assert.ok(st.bastion.hostCa);
  assert.equal(scope.machineInstalled(mfs), true);
  assert.ok(elevatedArgs(m, from).includes('pair'));
  assert.ok(!elevatedArgs(m, from).includes('service'));
});

// The bastion step's sudo run, refused (a wrong password three times); every
// other command as the fake machine answers it.
function bastionSudoDeclined(m) {
  return fakeRunner((cmd, args, o) => (/sudo$/.test(cmd) && args.includes('-s')
    ? { code: 1, stderr: 'sudo: 3 incorrect password attempts' } : m.runner.run(cmd, args, o)));
}

test('a takeover of the kept bastion whose sudo did not run takes nothing over, says so, and the next run takes it over', async () => {
  const x = await keptBastion();
  const { m, mp, ap, mfs } = x;
  const scope = require('../src/scope');
  const from = m.bastionCalls().length;
  let r = await x.asAccount(['--just-me'], { installed: undefined, machineFs: mfs, runner: bastionSudoDeclined(m) });
  assert.equal(r.code, 1, r.text + r.err);
  assert.equal(m.accountInstance().status, 'Running', 'the Host is installed and runs');
  assert.match(r.err, /The bastion the whole machine's install left here was not taken over for this account: sudo did not run the step that takes it over \(sudo: 3 incorrect password attempts\)\. It stays the whole machine's/);
  assert.match(r.err, /Run npx cseries again in a terminal, as an account that may use sudo/);
  assert.deepEqual(elevatedArgs(m, from), [], 'nothing paired');
  assert.equal(JSON.parse(fs.readFileSync(ap.state, 'utf8')).bastion, undefined, 'the account\'s record does not name it');
  assert.equal(fs.readFileSync(mp.state, 'utf8'), x.kept, 'the machine\'s record, as it was');
  assert.ok(m.vmUser);
  assert.equal(scope.machineKeptBastion(mfs).service, true);
  // The next run, as the account (its own install now): taken over.
  r = await x.asAccount([], { installed: undefined, machineFs: mfs });
  assert.equal(r.code, 0, r.text + r.err);
  assert.ok(elevatedArgs(m, from).includes('pair'));
  assert.ok(JSON.parse(fs.readFileSync(ap.state, 'utf8')).bastion.hostCa);
  assert.ok(!fs.existsSync(mp.state) && m.vmUser === null);
});

test('a takeover of the kept bastion stops before anything is paired when the machine\'s record changed or its install is back', async () => {
  const scope = require('../src/scope');
  // Changed after the account read it: a whole-machine install started meanwhile.
  const x = await keptBastion();
  const from = x.m.bastionCalls().length;
  x.m.beforeSudo = () => {
    const st = JSON.parse(fs.readFileSync(x.mp.state, 'utf8'));
    fs.writeFileSync(x.mp.state, JSON.stringify({ ...st, note: 'changed' }));
  };
  let r = await x.asAccount(['--just-me'], { installed: undefined, machineFs: x.mfs });
  assert.equal(r.code, 1, r.text + r.err);
  assert.match(r.err, /was not taken over for this account: the whole machine's record \(\S+state\.json\) changed after this command read it\. It stays the whole machine's/);
  assert.deepEqual(elevatedArgs(x.m, from), [], 'nothing recorded or paired');
  assert.equal(JSON.parse(fs.readFileSync(x.mp.state, 'utf8')).note, 'changed', 'that record is left as it is');
  assert.ok(x.m.vmUser && fs.existsSync(x.mp.base));
  assert.equal(JSON.parse(fs.readFileSync(x.ap.state, 'utf8')).bastion, undefined);
  // The machine's unit is here again.
  const y = await keptBastion();
  const yFrom = y.m.bastionCalls().length;
  y.m.beforeSudo = () => {
    fs.mkdirSync(path.dirname(y.mp.autostart), { recursive: true });
    fs.writeFileSync(y.mp.autostart, 'unit');
  };
  r = await y.asAccount(['--just-me'], { installed: undefined, machineFs: y.mfs });
  assert.equal(r.code, 1, r.text + r.err);
  assert.match(r.err, /an install of C-Series for the whole machine is here again, and the bastion stays its own/);
  assert.deepEqual(elevatedArgs(y.m, yFrom), []);
  assert.equal(fs.readFileSync(y.mp.state, 'utf8'), y.kept);
  assert.equal(JSON.parse(fs.readFileSync(y.ap.state, 'utf8')).bastion, undefined);
  assert.ok(scope.machineInstalled(y.mfs));
});

test('a takeover of the kept bastion: stopped when the machine\'s record cannot be removed; standing, and saying how, when what was kept with it cannot', async () => {
  // Nothing of the folder could be removed: the record stays, so nothing is taken over.
  const z = await keptBastion();
  const zFrom = z.m.bastionCalls().length;
  z.m.rmFails = 'all';
  let rz = await z.asAccount(['--just-me'], { installed: undefined, machineFs: z.mfs });
  assert.equal(rz.code, 1, rz.text + rz.err);
  assert.match(rz.err, /was not taken over for this account: cannot remove \S+state\.json\. It stays the whole machine's/);
  assert.deepEqual(elevatedArgs(z.m, zFrom), []);
  assert.equal(fs.readFileSync(z.mp.state, 'utf8'), z.kept);
  assert.ok(z.m.vmUser, 'its system account stays with it');
  assert.equal(JSON.parse(fs.readFileSync(z.ap.state, 'utf8')).bastion, undefined);
  // The record went, the rest of the folder did not: taken over, and said.
  z.m.rmFails = 'partial';
  rz = await z.asAccount([], { installed: undefined, machineFs: z.mfs });
  assert.equal(rz.code, 0, rz.text + rz.err);
  assert.match(rz.err, new RegExp(`To remove it yourself: sudo rm -rf ${z.mp.base.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}$`, 'm'));
  assert.ok(!fs.existsSync(z.mp.state) && fs.existsSync(z.mp.base));
  assert.ok(JSON.parse(fs.readFileSync(z.ap.state, 'utf8')).bastion.hostCa);
  // The system account could not be removed.
  const x = await keptBastion();
  x.m.userdelFails = true;
  const r = await x.asAccount(['--just-me'], { installed: undefined, machineFs: x.mfs });
  assert.equal(r.code, 0, r.text + r.err);
  assert.match(r.err, /not all that the whole machine's uninstall kept for it could be removed\. To remove it yourself: sudo userdel cseries$/m);
  assert.ok(x.m.vmUser, 'still here');
  assert.ok(!fs.existsSync(x.mp.state) && !fs.existsSync(x.mp.base));
  assert.ok(JSON.parse(fs.readFileSync(x.ap.state, 'utf8')).bastion.hostCa, 'taken over and paired');
});

test('the move is offered over the kept bastion\'s record, goes on over it, and an undone move puts it back as it was', async () => {
  const x = await keptBastion();
  const { m, mp, ap, mfs } = x;
  // An account's install that has not taken the bastion over (its sudo did
  // not run): the machine's record still names it.
  let r = await x.asAccount(['--just-me'], { installed: undefined, machineFs: mfs, runner: bastionSudoDeclined(m) });
  assert.equal(r.code, 1, r.text + r.err);
  assert.equal(fs.readFileSync(mp.state, 'utf8'), x.kept);
  // The offer: made, since the machine has no install.
  const asked = [];
  const runner = fakeRunner((cmd, args, o) => (o.inherit ? { code: 0 } : { code: 1, notFound: true }));
  r = await x.asAccount([], {
    installed: undefined, machineFs: mfs, isTTY: true, runner, prompt: async (q) => { asked.push(q); return true; },
    net: { probe: async () => ({ status: 200, location: '' }) }, binPath: '/pkg/bin/cseries.js', nodePath: '/usr/bin/node',
  });
  assert.equal(asked.length, 1, 'offered');
  assert.deepEqual(runner.calls.find((c) => c.kind === 'inherit').args.slice(-3), ['--move-from', ap.base, ap.autostart]);
  // The root side: a move that does not finish is undone, the record put back.
  m.afterMove = () => {
    fs.writeFileSync(path.join(mp.lima, 'cseries', 'disk'), JSON.stringify({ hostCa: '-----BEGIN CERTIFICATE-----\nMIIBother\n-----END CERTIFICATE-----' }));
  };
  r = await x.asRoot(['--move-from', ap.base, ap.autostart]);
  assert.equal(r.code, 1, r.text + r.err);
  assert.match(r.text, /The move did not finish, so it is undone\./);
  assert.equal(fs.readFileSync(mp.state, 'utf8'), x.kept, 'the kept record, as it was');
  assert.ok(m.vmUser, 'the system account made before the move stays');
  assert.equal(m.accountInstance().status, 'Running');
  // Then it moves.
  m.afterMove = null;
  const from = m.bastionCalls().length;
  r = await x.asRoot(['--move-from', ap.base, ap.autostart]);
  assert.equal(r.code, 0, r.text + r.err);
  const st = JSON.parse(fs.readFileSync(mp.state, 'utf8'));
  assert.ok(st.bastion.service && st.bastion.hostCa, 'the kept bastion, paired with the moved Host');
  assert.ok(elevatedArgs(m, from).includes('pair') && !elevatedArgs(m, from).includes('service'), 'reused, not installed again');
  assert.equal(st.unfinished, undefined);
  assert.equal(st.vmUserCreated, true);
  assert.equal(require('../src/scope').machineInstalled(mfs), true);
});
