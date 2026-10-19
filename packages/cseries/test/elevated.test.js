'use strict';

// The elevated script that makes the bastion this PC's system service. It is
// the one thing in this package that runs as an administrator, so what it
// does, in what order, and what it never carries are checked line by line.

const test = require('node:test');
const assert = require('node:assert/strict');
const elevated = require('../src/win/elevated');
const win = require('../src/win/windows');
const { bastionCommands } = require('../src/bastion-commands');
const { fakeRunner, psScript } = require('./helpers');
const { psQuote } = require('../src/win/powershell');

const SID = 'S-1-5-21-1111111111-2222222222-3333333333-1001';
const TOKEN = 'tok-invented-0f3c9a';
const SHA = 'a'.repeat(64);
const CA_SHA = 'c'.repeat(64);

function plan(over = {}) {
  const p = { release: null, pair: { enroll: 'https://127.0.0.1:8810/v1/local/redeem', token: TOKEN }, anchor: true, enroll: true, ...over };
  return {
    nonce: '0011223344556677',
    log: 'C:\\Users\\someone\\AppData\\Local\\cseries\\bastion-install.log',
    install: { file: 'C:\\Users\\someone\\AppData\\Local\\cseries\\downloads\\privatae-bastion-0.53.0.exe', sha256: SHA },
    serviceInstall: true,
    old: { exe: 'C:\\Users\\someone\\AppData\\Local\\Programs\\privatae-bastion\\privatae-bastion.exe', dir: 'C:\\Users\\someone\\AppData\\Local\\Programs\\privatae-bastion', sid: SID },
    hostCa: p.pair ? { file: 'C:\\Users\\someone\\AppData\\Local\\cseries\\host-ca.pem', sha256: CA_SHA } : null,
    host: p.pair ? null : 'host-a',
    commands: bastionCommands(p, elevated.CA_VAR),
  };
}

const pos = (s, re) => {
  const i = s.search(re);
  assert.ok(i >= 0, `${re} is not in the script`);
  return i;
};

test('the program is copied into Program Files and its sha256 checked THERE, before it replaces the service\'s', () => {
  const s = elevated.bastionInstallScript(plan());
  assert.match(s, /^\$pf = \[string\]\(Get-ItemProperty -LiteralPath 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion' -Name 'ProgramFilesDir'\)\.ProgramFilesDir$/m);
  assert.match(s, /^\$dir = Join-Path \$pf 'Privatae'$/m);
  assert.match(s, /^\$exe = Join-Path \$dir 'privatae-bastion\.exe'$/m);
  const copy = pos(s, /Copy-Item -LiteralPath 'C:\\Users\\someone\\AppData\\Local\\cseries\\downloads\\privatae-bastion-0\.53\.0\.exe' -Destination \$new/);
  const hash = pos(s, /\(Get-FileHash -LiteralPath \$new -Algorithm SHA256\)/);
  const check = pos(s, new RegExp(`if \\(\\$h -ne '${SHA}'\\) \\{ Remove-Item -LiteralPath \\$new -Force; Quit 10 `));
  const stop = pos(s, /Stop-Service -Name 'PrivataeBastion' -Force/);
  const move = pos(s, /Move-Item -LiteralPath \$new -Destination \$exe -Force/);
  assert.ok(copy < hash && hash < check && check < stop && stop < move);
  assert.match(s, /\$new = "\$exe\.new"/, 'the copy is made inside Program Files, so it gets that folder\'s permissions');
  assert.ok(pos(s, /Bastion 12 'service install' @\('service','install'\)/) > move);
});

test('the Host CA is copied to where only an administrator writes, and checked, before pair reads it', () => {
  const s = elevated.bastionInstallScript(plan());
  const copy = pos(s, /\$ca = Join-Path \$dir 'host-ca\.pem'/);
  const from = pos(s, /Copy-Item -LiteralPath 'C:\\Users\\someone\\AppData\\Local\\cseries\\host-ca\.pem' -Destination \$ca -Force/);
  const check = pos(s, new RegExp(`if \\(\\$h -ne '${CA_SHA}'\\) \\{ Remove-Item -LiteralPath \\$ca -Force; Quit 14 `));
  const pair = pos(s, /Bastion 16 'pair' @\('pair','--redeem-url','https:\/\/127\.0\.0\.1:8810\/v1\/local\/redeem','--host-ca',\$ca,'tok-invented-0f3c9a'\)/);
  assert.ok(copy < from && from < check && check < pair);
  assert.ok(!/--replace/.test(s), 'a CA change is a release and a fresh pair, never a rotation');
});

test('the order is service, mode, pair, anchor, enrol, restart; release-anchor only when asked, before pair', () => {
  const s = elevated.bastionInstallScript(plan());
  assert.ok(!/release-anchor/.test(s));
  const order = [/'service','install'/, /'set-mode','networked'/, /@\('pair'/, /@\('claim-anchor'\)/, /& \$exe 'stepup-enroll'/, /Restart-Service -Name 'PrivataeBastion' -Force/]
    .map((re) => pos(s, re));
  assert.deepEqual([...order].sort((a, b) => a - b), order);
  const r = elevated.bastionInstallScript(plan({ release: 'host-a' }));
  assert.ok(pos(r, /Bastion 15 'release-anchor' @\('release-anchor','--host','host-a','--host-ca',\$ca\)/) < pos(r, /@\('pair'/));
  assert.ok(pos(r, /@\('set-mode','networked'\)/) < pos(r, /release-anchor/));
  assert.ok(pos(r, /\$ca = Join-Path \$dir 'host-ca\.pem'/) < pos(r, /release-anchor/), 'the checked CA copy exists before release-anchor reads it');
});

test('enrolment runs on the elevated console: nothing redirected, nothing of it logged, no secret anywhere', () => {
  const s = elevated.bastionInstallScript(plan());
  const line = s.split('\n').find((l) => /stepup-enroll'/.test(l) && /& \$exe/.test(l));
  assert.equal(line.trim(), "& $exe 'stepup-enroll' '--console-only' '--host' $hostName");
  assert.ok(!/[|>]|Tee|Out-File|Out-String|Add-Content|Say/.test(line), `redirected or logged: ${line}`);
  assert.ok(!/Bastion \d+ 'stepup-enroll'/.test(s), 'not through the logging wrapper');
  // A TOTP secret is base32: no such run of characters, and no otpauth URI.
  assert.ok(!/[A-Z2-7]{16,}/.test(s));
  assert.ok(!/otpauth|secret/i.test(s));
});

test('the pairing token is in pair\'s arguments only: never in a logged line', () => {
  const s = elevated.bastionInstallScript(plan());
  const withToken = s.split('\n').filter((l) => l.includes(TOKEN));
  assert.equal(withToken.length, 1);
  assert.match(withToken[0], /^\s*\$text = Bastion 16 'pair' @\(/);
  // The wrapper logs the command's name and output, not its arguments.
  assert.match(s, /Say "> privatae-bastion \$what"/);
  assert.ok(!/Say[^\n]*\$a\b/.test(s));
});

test('the old per-user install goes: its logon task, any copy from another path, its program, its PATH entry via HKEY_USERS', () => {
  const s = elevated.bastionInstallScript(plan());
  assert.match(s, /Unregister-ScheduledTask -TaskName 'PrivataeBastion' -Confirm:\$false/);
  assert.match(s, /Get-Process -Name 'privatae-bastion'[^\n]*\$_\.Path -ne \$exe[^\n]*Stop-Process/);
  assert.match(s, /Remove-Item -LiteralPath 'C:\\Users\\someone\\AppData\\Local\\Programs\\privatae-bastion\\privatae-bastion\.exe' -Force/);
  assert.match(s, new RegExp(`\\[Microsoft\\.Win32\\.Registry\\]::Users\\.OpenSubKey\\('${SID}\\\\Environment', \\$true\\)`));
  assert.match(s, /GetValue\('Path', \$null, \[Microsoft\.Win32\.RegistryValueOptions\]::DoNotExpandEnvironmentNames\)/);
  assert.match(s, /\$kind = \$userEnv\.GetValueKind\('Path'\)/);
  assert.match(s, /\$userEnv\.SetValue\('Path', \(\$kept -join ';'\), \$kind\)/);
  assert.ok(!/HKCU|\$env:LOCALAPPDATA|\$env:APPDATA/.test(s), 'nothing of the person\'s is found through the elevated account\'s profile');
});

test('the machine PATH is read and written as stored, as install.ps1 -Networked does', () => {
  const s = elevated.bastionInstallScript(plan());
  assert.match(s, /LocalMachine\.OpenSubKey\('SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Environment', \$true\)/);
  assert.match(s, /SetValue\('Path', \(\$raw\.TrimEnd\(';'\) \+ ';' \+ \$dir\), \[Microsoft\.Win32\.RegistryValueKind\]::ExpandString\)/);
});

test('the outer, unelevated PowerShell carries neither the token nor the script: both go on stdin', async () => {
  const s = elevated.bastionInstallScript(plan());
  const runner = fakeRunner(() => ({ code: 0 }));
  await win.elevatedScript(runner, s);
  const call = runner.calls[0];
  assert.equal(call.cmd, 'powershell.exe');
  const argv = call.args.join(' ');
  const outer = psScript(call.args);
  assert.ok(!argv.includes(TOKEN) && !outer.includes(TOKEN));
  assert.ok(!outer.includes('Copy-Item'), 'the elevated script is not in the outer command line');
  assert.match(outer, /\$enc = \[Console\]::In\.ReadToEnd\(\)\.Trim\(\)/);
  assert.match(outer, /Start-Process -FilePath \$ps -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-EncodedCommand',\$enc -Verb RunAs -Wait -PassThru/);
  assert.equal(Buffer.from(call.input, 'base64').toString('utf16le'), s);
});

// PowerShell's single-quoted string as its language specification scans it:
// five characters are a single quote (' and U+2018, U+2019, U+201A, U+201B),
// and two in a row stand for one. -> the value, and where the literal ended.
const PS_SINGLE_QUOTES = new Set(["'", '‘', '’', '‚', '‛']);
function scanSingleQuoted(src, at) {
  assert.ok(PS_SINGLE_QUOTES.has(src[at]), `a single-quoted string starts at ${at}`);
  let value = '';
  for (let i = at + 1; i < src.length; i += 1) {
    if (PS_SINGLE_QUOTES.has(src[i])) {
      if (PS_SINGLE_QUOTES.has(src[i + 1])) {
        value += src[i];
        i += 1;
        continue;
      }
      return { value, end: i };
    }
    value += src[i];
  }
  throw new Error('the single-quoted string never ends');
}

test('psQuote keeps a value holding ANY of PowerShell\'s five single quotes inside one string', () => {
  for (const q of PS_SINGLE_QUOTES) {
    const value = `a${q}b${q}${q}c`;
    const quoted = psQuote(value);
    const r = scanSingleQuoted(quoted, 0);
    assert.equal(r.end, quoted.length - 1, `U+${q.codePointAt(0).toString(16)} ended the string early`);
    assert.equal(r.value, value);
  }
});

test('a pairing token the Host hands over stays one argument of pair in the elevated script', () => {
  const token = 'tok’invented‘';
  const p = { release: null, pair: { enroll: 'https://127.0.0.1:8810/v1/local/redeem', token }, anchor: true, enroll: false };
  const s = elevated.bastionInstallScript({ ...plan(), commands: bastionCommands(p, elevated.CA_VAR) });
  const at = s.indexOf(",$ca,") + ',$ca,'.length;
  const r = scanSingleQuoted(s, at);
  assert.equal(r.value, token);
  assert.equal(s[r.end + 1], ')', 'the argument list ends right after the token');
});

test('the elevated program is Windows\' own PowerShell, by its full path, never found by a search', () => {
  assert.match(win.ELEVATE_FROM_STDIN, /^\$ps = Join-Path \(\[Environment\]::SystemDirectory\) 'WindowsPowerShell\\v1\.0\\powershell\.exe'$/m);
  assert.ok(!/-FilePath 'powershell\.exe'/.test(win.ELEVATE_FROM_STDIN));
});

test('nothing in the elevated script is placed by $env:ProgramFiles, which the elevating account can set', () => {
  assert.ok(!/\$env:ProgramFiles/i.test(elevated.bastionInstallScript(plan())));
});

test('a declined prompt is told apart from a failed step', async () => {
  const declined = fakeRunner(() => ({ code: 1, stderr: 'Start-Process : This operation was canceled by the user.' }));
  assert.equal((await win.elevatedScript(declined, 'x')).declined, true);
  const failed = fakeRunner(() => ({ code: 17 }));
  const r = await win.elevatedScript(failed, 'x');
  assert.deepEqual([r.declined, r.code], [false, 17]);
});

test('only this run\'s log is read, line by line', () => {
  const text = '\uFEFFrun=0011223344556677\r\nstep=pair\r\n> privatae-bastion pair\r\nPaired with host-a over TCP.\r\npaired_host=host-a\r\nexit=17\r\n';
  const l = elevated.parseInstallLog(text, '0011223344556677');
  assert.equal(l.ours, true);
  assert.deepEqual(l.entries.map((e) => `${e.key}=${e.value}`), ['step=pair', 'paired_host=host-a', 'exit=17']);
  assert.deepEqual(elevated.parseInstallLog(text, 'ffffffffffffffff'), { ours: false, entries: [] });
  assert.deepEqual(elevated.parseInstallLog('', 'x'), { ours: false, entries: [] });
});

test('the approvals owner is recorded first, as this person\'s SID; the script never hands approvals over or forces a release', () => {
  const p = plan({ release: 'host-a' });
  const s = elevated.bastionInstallScript({ ...p, commands: bastionCommands({ release: 'host-a', pair: { enroll: 'https://127.0.0.1:8810/v1/local/redeem', token: TOKEN }, anchor: true, enroll: true, owner: SID }, elevated.CA_VAR) });
  const owner = pos(s, new RegExp(`Bastion 20 'approvals-owner' @\\('approvals-owner','--sid','${SID}'\\)`));
  assert.ok(pos(s, /'service','install'/) < owner, 'the service state exists first');
  assert.ok(owner < pos(s, /'set-mode','networked'/), 'before anything else changes');
  assert.ok(owner < pos(s, /release-anchor/) && owner < pos(s, /@\('pair'/) && owner < pos(s, /& \$exe 'stepup-enroll'/));
  assert.match(s, /Say 'owner=yes'/);
  assert.ok(!/--replace/.test(s) && !/--force/.test(s));
});
