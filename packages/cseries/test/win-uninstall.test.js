'use strict';

// uninstall --delete-data on Windows (cli.js uninstall, bastion.js
// removeOnWindows, win/elevated.js bastionUninstallScript): the bastion this
// command installed goes through the same single UAC prompt the install uses,
// its anchor to this Host included; what else the command added (WSL, its
// .wslconfig lines) is named and removed only on a y; a bastion it did not
// install stays. On the fake PC of fake-machine.js. Everything here is
// invented test data.

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { main } = require('../src/cli');
const elevated = require('../src/win/elevated');
const { removeWslConfigLines } = require('../src/vm/wslconfig');
const { mergeWslConfig, asOldRelease } = require('./old-wslconfig');
const { State } = require('../src/state');
const { captureOutput, testPaths, validRelease } = require('./helpers');
const { fakeMachine } = require('./fake-machine');

const TRAY = 'HKLM:\\Software\\Microsoft\\Windows\\CurrentVersion\\Run\\PrivataeBastionTray';

function machine(opts = {}) {
  const p = testPaths();
  const m = fakeMachine(p, opts);
  const rel = validRelease();
  rel.guest['wsl-x64'].sha256 = m.rootfsSha256;
  const run = async (argv = [], extra = {}) => {
    const out = captureOutput();
    const code = await main(argv, {
      platform: 'win32', arch: 'x64', env: {}, paths: p, out, runner: m.runner, net: m.net,
      sleep: async () => {}, prompt: async () => false, portState: async () => 'free', freeBytes: () => 64 * 1024 ** 3, osRelease: '10.0.22631',
      releaseInfo: { release: rel, placeholders: [], file: 'test' }, bastionKeys: m.bastionKeys,
      machine: { totalMem: 16 * 1024 ** 3, cpus: 8 },
      ...extra,
    });
    return { code, text: out.text(), err: out.errText() };
  };
  return { m, p, run };
}

// A PC where the run installed WSL first (the restart, then the resumed run).
async function installed(opts = {}) {
  const x = machine({ wsl: false, ...opts });
  assert.equal((await x.run()).code, 0);
  const r = await x.run();
  assert.equal(r.code, 0, r.text + r.err);
  assert.equal(x.m.bastion.anchor, 'host-a', 'the bastion is anchored to this Host');
  return x;
}

test('Windows, uninstall --delete-data: the bastion it installed goes, anchor and all, through one UAC prompt', async () => {
  const { m, p, run } = await installed({ doorRules: ['7878'] });
  const prompts = m.elevated.length;
  assert.ok(fs.existsSync(m.stateDir) && fs.existsSync(path.join(m.stateDir, 'anchor.json')));
  assert.ok(m.registry[TRAY]);
  const r = await run(['uninstall', '--delete-data', '--yes']);
  assert.equal(r.code, 0, r.text + r.err);
  assert.equal(m.elevated.length, prompts + 1, 'one more prompt, for all of it');
  assert.equal(m.uninstallScripts.length, 1);
  // The bastion's own uninstall, never asked to ask again, never unpairing.
  assert.ok(m.bastionCommands.some((a) => a.length === 1 && a[0] === 'uninstall'));
  assert.ok(!m.bastionCommands.some((a) => /unpair|release-anchor|--delete-data/.test(a.join(' '))));
  // Everything of it is gone.
  assert.equal(m.service, null, 'the service');
  assert.ok(!fs.existsSync(p.serviceExe), 'the program in Program Files');
  assert.ok(!fs.existsSync(p.serviceDir), 'its folder');
  assert.ok(!fs.existsSync(m.stateDir), 'its state, pairings and keys under LocalSystem\'s profile');
  assert.equal(m.registry[TRAY], undefined, 'the tray\'s logon entry');
  assert.deepEqual(m.firewallRules, [], 'its door\'s firewall rule');
  assert.equal(m.eventSource, false);
  assert.match(r.text, /The bastion is removed: its service, program, tray, firewall rules, pairings, keys and audit log\./);
  assert.doesNotMatch(r.text, /The bastion stays installed/);
  // And this command's own folder.
  assert.equal(m.distros.length, 0);
  assert.ok(!fs.existsSync(p.state));
  assert.ok(!fs.existsSync(p.installLog));
  assert.match(r.text, /C-Series is removed from this machine\./);
});

test('Windows: door firewall rules an older bastion\'s own uninstall does not know are removed by name', async () => {
  const { m, run } = await installed({ doorRules: ['7878', '7879'], oldUninstall: true });
  const r = await run(['uninstall', '--delete-data', '--yes']);
  assert.equal(r.code, 0, r.text + r.err);
  assert.deepEqual(m.firewallRules, []);
});

test('Windows: the tray holds the program open, so it is stopped before the program goes', async () => {
  const { m, p, run } = await installed();
  assert.ok(m.runner.calls.some((c) => c.kind === 'detached' && c.cmd === p.serviceExe && c.args[0] === 'tray'));
  const r = await run(['uninstall', '--delete-data', '--yes']);
  assert.equal(r.code, 0, r.text + r.err);
  assert.ok(!fs.existsSync(p.serviceExe));
});

test('Windows: the prompt declined keeps the bastion and its record, says so, and fails', async () => {
  const { m, p, run } = await installed();
  // From now on the person answers No to Windows.
  const ps = m.runner;
  const orig = ps.run.bind(ps);
  ps.run = async (cmd, args, o = {}) => {
    if (cmd === 'powershell.exe' && /-Verb RunAs/.test(require('./helpers').psScript(args) || '')) {
      return { code: 1, stdout: Buffer.alloc(0), stderr: Buffer.from('Start-Process : This operation was canceled by the user.'), notFound: false };
    }
    return orig(cmd, args, o);
  };
  const r = await run(['uninstall', '--delete-data', '--yes']);
  assert.equal(r.code, 1);
  assert.match(r.err, /The bastion was not removed: the Windows permission prompt was declined\./);
  assert.match(r.err, /privatae-bastion\.exe" uninstall --delete-data {3}\(from an Administrator terminal\)/);
  assert.ok(m.service, 'the service stays');
  assert.ok(fs.existsSync(p.serviceExe));
  assert.equal(JSON.parse(fs.readFileSync(p.state, 'utf8')).bastion.service, true, 'its record is kept for the next run');
});

test('Windows: a bastion this command did not install stays, and is said to stay', async () => {
  const { m, p, run } = await installed();
  const st = JSON.parse(fs.readFileSync(p.state, 'utf8'));
  delete st.bastion;
  fs.writeFileSync(p.state, JSON.stringify(st));
  const prompts = m.elevated.length;
  const r = await run(['uninstall', '--delete-data', '--yes']);
  assert.equal(r.code, 0, r.text + r.err);
  assert.equal(m.elevated.length, prompts, 'no prompt: nothing of it is touched');
  assert.ok(m.service);
  assert.ok(fs.existsSync(p.serviceExe));
  assert.match(r.text, /was not installed by npx cseries from this install, so it stays as it is/);
  assert.match(r.text, /privatae-bastion\.exe" uninstall --delete-data/);
});

test('Windows: WSL it installed is named and removed in the same prompt only on a y; kept on a no or with --yes', async () => {
  // No: kept, and the command named.
  let x = await installed();
  let r = await x.run(['uninstall', '--delete-data'], { prompt: async (q) => /Type y to go on/.test(q) });
  assert.equal(r.code, 0, r.text + r.err);
  assert.match(r.text, /C-Series installed WSL on this PC for its Linux system, and no other Linux system uses it\./);
  assert.match(r.text, /WSL is kept\. To remove it: wsl --uninstall/);
  assert.equal(x.m.wslUninstalls, 0);
  assert.ok(x.m.wsl);

  // --yes is not a y to this.
  x = await installed();
  r = await x.run(['uninstall', '--delete-data', '--yes']);
  assert.equal(x.m.wslUninstalls, 0);

  // Yes: in the bastion's own elevated run, one prompt for both.
  x = await installed();
  const prompts = x.m.elevated.length;
  r = await x.run(['uninstall', '--delete-data'], { prompt: async () => true });
  assert.equal(r.code, 0, r.text + r.err);
  assert.equal(x.m.elevated.length, prompts + 1, 'one prompt for the bastion and WSL');
  assert.equal(x.m.wslUninstalls, 1);
  assert.equal(x.m.wsl, false);
  assert.match(r.text, /WSL is removed\./);
  assert.equal(x.m.service, null);
});

test('Windows: WSL and the .wslconfig lines, once removed, are not asked about again by a later uninstall; WSL in a run that did not finish is said not removed', async () => {
  // --keep-bastion keeps this command's record for the next install; what it
  // already removed is no longer in it.
  const x = await installed();
  let r = await x.run(['uninstall', '--delete-data', '--keep-bastion'], { prompt: async () => true });
  assert.equal(r.code, 0, r.text + r.err);
  assert.equal(x.m.wslUninstalls, 1);
  assert.ok(!fs.existsSync(x.p.wslConfig));
  const st = JSON.parse(fs.readFileSync(x.p.state, 'utf8'));
  assert.equal(st.wslInstalled, undefined, 'WSL is gone: not offered again');
  assert.equal(st.wslConfigAdded, undefined, 'the lines are out: not offered again');
  const asked = [];
  r = await x.run(['uninstall', '--delete-data', '--keep-bastion'], { prompt: async (q) => { asked.push(q); return true; } });
  assert.ok(!asked.some((q) => /Remove WSL|Take them out/.test(q)), asked.join('\n'));
  assert.equal(x.m.wslUninstalls, 1);

  // The administrator prompt declined: neither the bastion nor WSL went, and both are said.
  const y = await installed();
  const orig = y.m.runner.run.bind(y.m.runner);
  y.m.runner.run = async (cmd, args, o = {}) => {
    if (cmd === 'powershell.exe' && /-Verb RunAs/.test(require('./helpers').psScript(args) || '')) {
      return { code: 1, stdout: Buffer.alloc(0), stderr: Buffer.from('Start-Process : This operation was canceled by the user.'), notFound: false };
    }
    return orig(cmd, args, o);
  };
  r = await y.run(['uninstall', '--delete-data'], { prompt: async () => true });
  assert.equal(r.code, 1);
  assert.match(r.err, /WSL was not removed: it goes in the same administrator run as the bastion, which did not finish\./);
  assert.ok(y.m.wsl);
  assert.equal(JSON.parse(fs.readFileSync(y.p.state, 'utf8')).wslInstalled, true, 'still recorded: the next uninstall offers it again');
});

test('Windows: WSL this command did not install is never offered; one another Linux system uses is kept', async () => {
  // Installed before C-Series (no record): not a word about removing it.
  let x = machine();
  assert.equal((await x.run()).code, 0);
  let r = await x.run(['uninstall', '--delete-data'], { prompt: async () => true });
  assert.equal(r.code, 0, r.text + r.err);
  assert.doesNotMatch(r.text, /installed WSL/);
  assert.equal(x.m.wslUninstalls, 0);

  // Installed by it, then used by another distribution too.
  x = await installed();
  x.m.distros.push('Ubuntu');
  r = await x.run(['uninstall', '--delete-data'], { prompt: async () => true });
  assert.equal(r.code, 0, r.text + r.err);
  assert.match(r.text, /C-Series installed WSL on this PC\. It stays: Ubuntu runs in it too\./);
  assert.equal(x.m.wslUninstalls, 0);
});

test('Windows: the .wslconfig lines an earlier release added are named and taken out only on a y, and only as it wrote them', async () => {
  // Installed now, then the PC as a release up to 0.1.16 left it (the lines
  // and their record), as an uninstall straight after updating finds it.
  let x = machine();
  assert.equal((await x.run()).code, 0);
  assert.ok(!fs.existsSync(x.p.wslConfig), 'this release writes no .wslconfig');
  const file = x.p.wslConfig;
  asOldRelease(x.p, new State(x.p.state));
  assert.equal(fs.readFileSync(file, 'utf8'), '[wsl2]\nmemory=8GB\nprocessors=8\n');
  let r = await x.run(['uninstall', '--delete-data'], { prompt: async (q) => /Type y to go on/.test(q) });
  assert.match(r.text, /C-Series added memory=8GB and processors=8 to .*\.wslconfig, the limits on WSL's virtual machine\./);
  assert.match(r.text, /They are kept\./);
  assert.equal(fs.readFileSync(file, 'utf8'), '[wsl2]\nmemory=8GB\nprocessors=8\n');

  // A file of the person's own, one of the lines changed since: only the other goes.
  x = machine();
  assert.equal((await x.run()).code, 0);
  fs.mkdirSync(path.dirname(x.p.wslConfig), { recursive: true });
  fs.writeFileSync(x.p.wslConfig, '[wsl2]\nswap=0\n');
  asOldRelease(x.p, new State(x.p.state));
  fs.writeFileSync(x.p.wslConfig, fs.readFileSync(x.p.wslConfig, 'utf8').replace('processors=8', 'processors=4'));
  r = await x.run(['uninstall', '--delete-data'], { prompt: async () => true });
  assert.equal(r.code, 0, r.text + r.err);
  assert.equal(fs.readFileSync(x.p.wslConfig, 'utf8'), '[wsl2]\nswap=0\nprocessors=4\n');
  assert.match(r.text, /Taken out of .*: memory=8GB\./);
  assert.match(r.text, /processors=8 is not in .* as C-Series wrote it .*so it stays\./);

  // A file it made itself, all its lines unchanged: the file goes.
  x = machine();
  assert.equal((await x.run()).code, 0);
  asOldRelease(x.p, new State(x.p.state));
  r = await x.run(['uninstall', '--delete-data'], { prompt: async () => true });
  assert.equal(r.code, 0, r.text + r.err);
  assert.ok(!fs.existsSync(x.p.wslConfig));
});

test('removeWslConfigLines undoes what an earlier release merged exactly, and leaves what the person wrote', () => {
  const cases = ['', '[wsl2]\nswap=0\n', '[experimental]\r\nsparseVhd=true\r\n', '[wsl2]  # mine\nmemory=3GB\n\n[boot]\nsystemd=true\n'];
  for (const before of cases) {
    const m = mergeWslConfig(before, { memory: '8GB', processors: '8' });
    const lines = m.added.map((k) => `${k}=${{ memory: '8GB', processors: '8' }[k]}`);
    const r = removeWslConfigLines(m.text, lines, m.section);
    assert.equal(r.text, before, JSON.stringify(before));
  }
  // Outside [wsl2], the same text is the person's.
  assert.equal(removeWslConfigLines('[boot]\nmemory=8GB\n', ['memory=8GB'], false).text, '[boot]\nmemory=8GB\n');
});

test('the Windows removal script: the bastion\'s own uninstall, then what it leaves, all named from Windows\' own folders', () => {
  const s = elevated.bastionUninstallScript({ nonce: '00aa', log: 'C:\\Users\\someone\\AppData\\Local\\cseries\\bastion-install.log', bastion: true, wsl: false });
  const at = (t) => {
    const i = s.indexOf(t);
    assert.ok(i >= 0, `${t} is not in the script`);
    return i;
  };
  assert.match(s, /^\$pf = \[string\]\(Get-ItemProperty -LiteralPath 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion' -Name 'ProgramFilesDir'\)\.ProgramFilesDir$/m, 'Program Files from HKLM');
  const own = at("& $exe 'uninstall'");
  assert.doesNotMatch(s, /'uninstall','--delete-data'|uninstall --delete-data/, 'the person said yes already');
  const firewall = at("Get-NetFirewallRule -DisplayName 'Privatae bastion: your Host connects (TCP *)'");
  const tray = at("Get-Process -Name 'privatae-bastion'");
  const program = at("foreach ($f in @($exe, \"$exe.new\", (Join-Path $dir 'host-ca.pem')))");
  const state = at("$state = Join-Path ([Environment]::SystemDirectory) 'config\\systemprofile\\AppData\\Roaming\\privatae-bastion'");
  const check = at("Emit 'step=check'");
  assert.ok(own < firewall && firewall < tray && tray < program && program < state && state < check, 'its own uninstall first, the check last');
  assert.ok(at("Remove-ItemProperty -LiteralPath 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Run' -Name 'PrivataeBastionTray'") > own);
  assert.ok(at("[System.Diagnostics.EventLog]::DeleteEventSource('PrivataeBastionAudit')") > own);
  assert.ok(at("& (Join-Path ([Environment]::SystemDirectory) 'sc.exe') delete 'PrivataeBastion'") > own);
  assert.ok(at("$pd = [Environment]::GetFolderPath('CommonApplicationData')") > own);
  assert.doesNotMatch(s, /\$env:/, 'no folder from an environment variable');
  assert.doesNotMatch(s, /wsl\.exe/, 'WSL only on a yes');
  const w = elevated.bastionUninstallScript({ nonce: '00aa', log: 'C:\\x.log', bastion: false, wsl: true });
  assert.match(w, /& \(Join-Path \(\[Environment\]::SystemDirectory\) 'wsl\.exe'\) '--uninstall'/);
  assert.doesNotMatch(w, /'uninstall'\)|step=uninstall/, 'the bastion only when it is ours');
  // Its log is read back for this run only.
  const log = elevated.parseInstallLog('\uFEFFrun=00aa\r\nremoved=C:\\x\r\nrunning=no\r\nleft=C:\\y\r\nwsl_exit=0\r\nresult=ok\r\n', '00aa');
  assert.deepEqual(log.entries.map((e) => e.key), ['removed', 'running', 'left', 'wsl_exit', 'result']);
});
