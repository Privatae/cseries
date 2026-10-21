'use strict';

// The one elevated step that makes the bastion this PC's system service, paired
// and anchored to the Host in the VM, with the person's authenticator enrolled.
//
// HOW IT IS RUN. elevatedScript starts an unelevated PowerShell whose own
// script is fixed and short, and hands it the elevated script's
// -EncodedCommand text on STDIN. That PowerShell passes it to
// `Start-Process powershell.exe -EncodedCommand <it> -Verb RunAs`, the UAC
// prompt. So the elevated script travels only in the elevated process's own
// command line: it is never a file the user's account could swap between the
// prompt and the run, and it is not in the unelevated process's command line
// either. Encoding it twice (the outer script carrying the inner one) would
// pass Windows' 32767-character command-line limit.
//
// WHAT IT CAN AND CANNOT SEE. Under over-the-shoulder UAC the elevated
// process is ANOTHER account (the administrator who typed their password), so
// its $env:LOCALAPPDATA and HKCU are that account's. Everything belonging to
// the person (the download, the Host CA file, the log, the old per-user
// install, their user PATH) is named by an explicit path or SID built here.
//
// WHAT IT REPORTS. Its output cannot be captured, so it reports through its
// exit code (bastion-commands.js EXIT) and a log at the person's install-log
// path: `run=<nonce>` first, then `step=<name>` before each step and these
// results as they happen:
//
//   installed=<exe>        the verified program is in Program Files
//   old_install=removed    the per-user logon task, program and PATH entry
//   service=yes            `service install` succeeded
//   owner=yes              approvals-owner recorded this person's account
//   released=<host>        release-anchor succeeded
//   paired_host=<host>     pair succeeded, as that pairing name
//   anchored=yes           claim-anchor succeeded
//   enrolled=yes           stepup-enroll succeeded
//   result=ok              everything, and the service was restarted
//   error=<words>, exit=<code>  on a failure
//
// The pairing token is in the script (the elevated command line) and on
// `pair -`'s standard input, piped from the script, never in pair's argv and
// never in the log: a bastion command is logged as its name and its output
// only. stepup-enroll is run attached to this elevated console
// with nothing redirected, so the TOTP secret it shows reaches that window and
// nothing else: no file, no log, no command line.

const { psQuote } = require('./powershell');
const { EXIT, HOST } = require('../bastion-commands');

// Stands for the elevated step's own copy of the Host CA in a command's args.
const CA_VAR = '\u0000ca';

const SERVICE = 'PrivataeBastion';

function arg(a) {
  if (a === CA_VAR) return '$ca';
  if (a === HOST) return '$hostName';
  return psQuote(a);
}

function args(list) {
  return `@(${list.map(arg).join(',')})`;
}

function preamble(p) {
  return [
    "$ErrorActionPreference = 'Stop'",
    "$ProgressPreference = 'SilentlyContinue'",
    `$log = ${psQuote(p.log)}`,
    '$code = 1',
    'function Emit([string]$line) { Add-Content -LiteralPath $log -Value $line -Encoding UTF8 }',
    "function Quit([int]$c, [string]$why) { Emit ('error=' + ($why -replace '[\\r\\n]+', ' ')); Emit \"exit=$c\"; exit $c }",
    // A bastion command: its name and its output go to the log, never its
    // arguments. PowerShell 5.1 turns a native program's stderr into errors
    // under 2>&1, which 'Stop' would throw on; the exit code decides instead.
    // With $stdin given, it is piped to the command's standard input (pair's
    // token), as UTF-8 without a byte order mark, and never in its args.
    'function Bastion([int]$c, [string]$what, [string[]]$a, [string]$stdin) {',
    '  Emit "step=$what"',
    '  Emit "> privatae-bastion $what"',
    "  $ErrorActionPreference = 'Continue'",
    "  if ($PSBoundParameters.ContainsKey('stdin')) {",
    '    $OutputEncoding = New-Object System.Text.UTF8Encoding $false',
    '    $text = ($stdin | & $exe @a 2>&1 | ForEach-Object { "$_" }) -join "`n"',
    '  } else {',
    '    $text = (& $exe @a 2>&1 | ForEach-Object { "$_" }) -join "`n"',
    '  }',
    '  $rc = $LASTEXITCODE',
    "  $ErrorActionPreference = 'Stop'",
    '  if ($text) { Emit $text }',
    '  if ($rc -ne 0) { Quit $c "privatae-bastion $what exited with $rc" }',
    '  return $text',
    '}',
    'New-Item -ItemType Directory -Force -Path (Split-Path -Parent $log) | Out-Null',
    `Set-Content -LiteralPath $log -Value ${psQuote(`run=${p.nonce}`)} -Encoding UTF8`,
    // Program Files from HKLM, which only an administrator can change, and
    // not from $env:ProgramFiles: an elevated process's environment is built
    // with the elevating account's own environment variables, which that
    // account can set unelevated. The folder that holds the service's program
    // must not be one the person's processes can name.
    "$pf = [string](Get-ItemProperty -LiteralPath 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion' -Name 'ProgramFilesDir').ProgramFilesDir",
    "if (-not $pf) { Quit 1 'cannot read where Program Files is' }",
    "$dir = Join-Path $pf 'Privatae'",
    "$exe = Join-Path $dir 'privatae-bastion.exe'",
    `$hostName = ${psQuote(p.host || '')}`,
  ];
}

// The verified download, copied into Program Files and checked THERE: the
// copy is a new file with the folder's permissions, and the check after the
// copy is what defeats a swap of the user-writable download in between.
function installBlock(p) {
  return [
    `$code = ${EXIT.install}`,
    "Emit 'step=install'",
    'New-Item -ItemType Directory -Force -Path $dir | Out-Null',
    '$new = "$exe.new"',
    `Copy-Item -LiteralPath ${psQuote(p.install.file)} -Destination $new -Force`,
    '$h = (Get-FileHash -LiteralPath $new -Algorithm SHA256).Hash.ToLower()',
    `if ($h -ne ${psQuote(p.install.sha256)}) { Remove-Item -LiteralPath $new -Force; Quit ${EXIT.binaryHash} "the program copied into $dir has sha256 $h, not ${p.install.sha256}" }`,
    `$svc = Get-Service -Name ${psQuote(SERVICE)} -ErrorAction SilentlyContinue`,
    "if ($svc -and $svc.Status -ne 'Stopped') {",
    `  Stop-Service -Name ${psQuote(SERVICE)} -Force`,
    "  $svc.WaitForStatus('Stopped', [TimeSpan]::FromSeconds(30))",
    '}',
    'Move-Item -LiteralPath $new -Destination $exe -Force',
    'Emit "installed=$exe"',
  ];
}

// The per-user install earlier releases of this command made: its logon task,
// any copy running from another path (it would hold the port the service's
// first pairing is given), its program, and its folder on the PERSON's user
// PATH, read and written as stored (REG_EXPAND_SZ stays REG_EXPAND_SZ).
function oldInstallBlock(p) {
  const lines = [
    `$code = ${EXIT.install}`,
    "Emit 'step=old-install'",
    `if (Get-ScheduledTask -TaskName ${psQuote(SERVICE)} -ErrorAction SilentlyContinue) { Unregister-ScheduledTask -TaskName ${psQuote(SERVICE)} -Confirm:$false }`,
    "Get-Process -Name 'privatae-bastion' -ErrorAction SilentlyContinue | Where-Object { $_.Path -and ($_.Path -ne $exe) } | ForEach-Object { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue }",
    `if (Test-Path -LiteralPath ${psQuote(p.old.exe)}) { Start-Sleep -Milliseconds 500; Remove-Item -LiteralPath ${psQuote(p.old.exe)} -Force -ErrorAction SilentlyContinue }`,
  ];
  if (p.old.sid) {
    lines.push(
      `$userEnv = [Microsoft.Win32.Registry]::Users.OpenSubKey(${psQuote(`${p.old.sid}\\Environment`)}, $true)`,
      'if ($userEnv) {',
      '  try {',
      "    $raw = $userEnv.GetValue('Path', $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)",
      '    if ($raw) {',
      "      $kind = $userEnv.GetValueKind('Path')",
      "      $all = @(([string]$raw).Split(';') | Where-Object { $_ })",
      `      $kept = @($all | Where-Object { $_.TrimEnd('\\') -ne ${psQuote(p.old.dir)} })`,
      "      if ($kept.Count -ne $all.Count) { $userEnv.SetValue('Path', ($kept -join ';'), $kind) }",
      '    }',
      '  } finally { $userEnv.Close() }',
      '}',
    );
  }
  lines.push("Emit 'old_install=removed'");
  return lines;
}

// `service install`, then the program's folder on the MACHINE path, read and
// written as stored, as the bastion's install.ps1 -Networked does.
function serviceBlock() {
  return [
    `$code = ${EXIT.serviceInstall}`,
    `Bastion ${EXIT.serviceInstall} 'service install' @('service','install') | Out-Null`,
    "Emit 'service=yes'",
    "$envKey = [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey('SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Environment', $true)",
    'try {',
    "  $raw = [string]$envKey.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)",
    "  if (($raw -split ';') -notcontains $dir) { $envKey.SetValue('Path', ($raw.TrimEnd(';') + ';' + $dir), [Microsoft.Win32.RegistryValueKind]::ExpandString) }",
    '} finally { $envKey.Close() }',
  ];
}

// The Host CA, copied to where only an administrator can write and checked
// against the sha256 of the CA the Host handed over, before `pair` reads it.
function hostCaBlock(p) {
  return [
    `$code = ${EXIT.hostCa}`,
    "Emit 'step=host-ca'",
    'New-Item -ItemType Directory -Force -Path $dir | Out-Null',
    "$ca = Join-Path $dir 'host-ca.pem'",
    `Copy-Item -LiteralPath ${psQuote(p.hostCa.file)} -Destination $ca -Force`,
    '$h = (Get-FileHash -LiteralPath $ca -Algorithm SHA256).Hash.ToLower()',
    `if ($h -ne ${psQuote(p.hostCa.sha256)}) { Remove-Item -LiteralPath $ca -Force; Quit ${EXIT.hostCa} "the Host CA copied into $dir has sha256 $h, not ${p.hostCa.sha256}" }`,
  ];
}

function commandBlock(c) {
  if (c.console) {
    // Attached to this elevated console, nothing redirected: the secret is
    // shown here and typed back here. Only the exit code is logged.
    return [
      `$code = ${c.code}`,
      `Emit 'step=${c.what}'`,
      `if (-not $hostName) { Quit ${c.code} 'no pairing to enrol an authenticator for' }`,
      "Write-Host ''",
      "Write-Host 'Approvals: the bastion shows a code for your authenticator app next. It is shown only in this window.'",
      "$ErrorActionPreference = 'Continue'",
      `& $exe ${c.args.map(arg).join(' ')}`,
      '$rc = $LASTEXITCODE',
      "$ErrorActionPreference = 'Stop'",
      `if ($rc -ne 0) { Quit ${c.code} "privatae-bastion ${c.what} exited with $rc" }`,
      "Emit 'enrolled=yes'",
    ];
  }
  const stdin = c.stdin === undefined ? '' : ` ${psQuote(c.stdin)}`;
  const lines = [`$code = ${c.code}`, `$text = Bastion ${c.code} ${psQuote(c.what)} ${args(c.args)}${stdin}`];
  if (c.pair) {
    lines.push(
      'Remove-Item -LiteralPath $ca -Force -ErrorAction SilentlyContinue',
      "$m = [regex]::Match([string]$text, 'Paired with (\\S+)')",
      `if (-not $m.Success) { Quit ${c.code} 'pair did not say which Host it paired with' }`,
      "$hostName = $m.Groups[1].Value.TrimEnd('.')",
      'Emit "paired_host=$hostName"',
    );
  } else if (c.args[0] === 'approvals-owner') {
    lines.push("Emit 'owner=yes'");
  } else if (c.args[0] === 'release-anchor') {
    lines.push(`Emit ${psQuote(`released=${c.args[2]}`)}`);
  } else if (c.args[0] === 'claim-anchor') {
    lines.push("Emit 'anchored=yes'");
  }
  return lines;
}

// p: {nonce, log, install: {file, sha256}|null, serviceInstall: bool,
//     old: {exe, dir, sid|null}, hostCa: {file, sha256}|null, host: name|null,
//     commands: bastionCommands(plan, CA_VAR)}
function bastionInstallScript(p) {
  const body = [];
  if (p.install) body.push(...installBlock(p));
  body.push(...oldInstallBlock(p));
  if (p.serviceInstall) body.push(...serviceBlock());
  if (p.hostCa) body.push(...hostCaBlock(p));
  for (const c of p.commands) body.push(...commandBlock(c));
  body.push(
    `$code = ${EXIT.restart}`,
    "Emit 'step=restart'",
    `Restart-Service -Name ${psQuote(SERVICE)} -Force`,
    "Emit 'result=ok'",
    'exit 0',
  );
  return [
    ...preamble(p),
    'try {',
    ...body.map((l) => `  ${l}`),
    '} catch {',
    '  Quit $code $_.Exception.Message',
    '}',
  ].join('\n');
}

// The log the script wrote for THIS run (its first line names the nonce): the
// key=value lines, in order. A log from another run, or none, is empty.
function parseInstallLog(text, nonce) {
  const lines = String(text || '').replace(/^﻿/, '').split(/\r?\n/);
  if (lines[0] !== `run=${nonce}`) return { ours: false, entries: [] };
  const entries = [];
  for (const l of lines.slice(1)) {
    const m = /^(installed|old_install|service|owner|released|paired_host|anchored|enrolled|result|error|exit|step)=(.*)$/.exec(l);
    if (m) entries.push({ key: m[1], value: m[2].trim() });
  }
  return { ours: true, entries };
}

module.exports = { bastionInstallScript, parseInstallLog, CA_VAR, SERVICE };
