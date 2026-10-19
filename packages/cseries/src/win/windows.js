'use strict';

// The Windows pieces the command needs outside WSL itself: per-user registry
// entries (HKCU Run and RunOnce), the elevated runs (installing WSL; the
// bastion's system service, elevated.js), the bastion service's state, and a
// few facts about the machine. Nothing here needs administrator rights except
// elevatedRun and elevatedScript, which ask through UAC.

const { powershell, psQuote, encode } = require('./powershell');

const RUN_KEY = 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Run';
const RUNONCE_KEY = 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\RunOnce';
const BASTION_SERVICE = 'PrivataeBastion';

// Sets one value under an HKCU key. The key is created only when missing:
// New-Item -Force on a registry key that exists would empty it.
function setValueScript(key, name, value) {
  return [
    `$k = ${psQuote(key)}`,
    'if (-not (Test-Path $k)) { New-Item -Path $k | Out-Null }',
    `Set-ItemProperty -Path $k -Name ${psQuote(name)} -Value ${psQuote(value)} -Type String`,
  ].join('\n');
}

function removeValueScript(key, name) {
  return `Remove-ItemProperty -Path ${psQuote(key)} -Name ${psQuote(name)} -ErrorAction SilentlyContinue`;
}

function getValueScript(key, name) {
  return `$v = Get-ItemProperty -Path ${psQuote(key)} -Name ${psQuote(name)} -ErrorAction SilentlyContinue\nif ($v) { [Console]::Out.Write($v.(${psQuote(name)})) }`;
}

async function setRegistryValue(runner, key, name, value) {
  const r = await powershell(runner, setValueScript(key, name, value));
  if (r.code !== 0) throw new Error(`could not write ${key}\\${name}: ${r.stderr.trim()}`);
}

async function removeRegistryValue(runner, key, name) {
  await powershell(runner, removeValueScript(key, name));
}

async function getRegistryValue(runner, key, name) {
  const r = await powershell(runner, getValueScript(key, name));
  return r.code === 0 ? r.stdout : '';
}

// Runs file with argList elevated, through the UAC prompt, and waits for it.
// -> {code, declined, stderr}
async function elevatedRun(runner, file, argList) {
  const script = [
    `$p = Start-Process -FilePath ${psQuote(file)} -ArgumentList ${argList.map(psQuote).join(',')} -Verb RunAs -Wait -PassThru`,
    'exit $p.ExitCode',
  ].join('\n');
  const r = await powershell(runner, script);
  const declined = r.code !== 0 && /cancel/i.test(r.stderr);
  return { code: r.code, declined, stderr: r.stderr.trim() };
}

// Whether the CPU's virtualization is usable: a running hypervisor (Hyper-V
// or WSL already up) hides the firmware flag, so either one is enough.
// -> {hypervisor: bool|null, firmware: bool|null}
async function virtualization(runner) {
  const r = await powershell(runner, [
    '$cs = Get-CimInstance Win32_ComputerSystem',
    '$p = Get-CimInstance Win32_Processor | Select-Object -First 1',
    '"hypervisor=$($cs.HypervisorPresent)"',
    '"firmware=$($p.VirtualizationFirmwareEnabled)"',
  ].join('\n'));
  const pick = (name) => {
    const m = new RegExp(`^${name}=(True|False)\\s*$`, 'mi').exec(r.stdout);
    return m ? m[1] === 'True' : null;
  };
  return { hypervisor: pick('hypervisor'), firmware: pick('firmware') };
}

// The bastion's system service: 'missing', 'running' (or starting),
// 'stopped', or 'other' (stopping, pausing). Get-Service needs no
// administrator.
function serviceStateScript() {
  return [
    `$s = Get-Service -Name ${psQuote(BASTION_SERVICE)} -ErrorAction SilentlyContinue`,
    "if ($s) { [Console]::Out.Write([string]$s.Status) } else { [Console]::Out.Write('missing') }",
  ].join('\n');
}

async function bastionServiceState(runner) {
  const r = await powershell(runner, serviceStateScript());
  if (r.code !== 0) throw new Error(r.stderr.trim() || `exit ${r.code}`);
  const s = r.stdout.trim();
  if (s === 'missing') return 'missing';
  if (s === 'Running' || s === 'StartPending') return 'running';
  if (s === 'Stopped') return 'stopped';
  return 'other';
}

// This account's SID, so the elevated step (which may run as another
// administrator) can reach this person's own registry hive. null when it
// cannot be read.
async function currentUserSid(runner) {
  const r = await powershell(runner, '[Console]::Out.Write([Security.Principal.WindowsIdentity]::GetCurrent().User.Value)');
  const sid = r.code === 0 ? r.stdout.trim() : '';
  return /^S-1-\d+(-\d+)+$/.test(sid) ? sid : null;
}

// Runs a whole script elevated, through one UAC prompt, and waits for it.
// The script goes to the unelevated PowerShell on STDIN as -EncodedCommand
// text and from there only into the elevated process's own command line (see
// elevated.js for why). -> {code, declined, stderr}
// The elevated program is named by its full path under the system directory
// (GetSystemDirectory, not an environment variable), never found by a search:
// what is started with administrator rights is Windows' own PowerShell.
const ELEVATE_FROM_STDIN = [
  '$enc = [Console]::In.ReadToEnd().Trim()',
  "if (-not $enc) { [Console]::Error.Write('no script on standard input'); exit 2 }",
  "$ps = Join-Path ([Environment]::SystemDirectory) 'WindowsPowerShell\\v1.0\\powershell.exe'",
  "$p = Start-Process -FilePath $ps -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-EncodedCommand',$enc -Verb RunAs -Wait -PassThru",
  'exit $p.ExitCode',
].join('\n');

async function elevatedScript(runner, script) {
  const r = await powershell(runner, ELEVATE_FROM_STDIN, { input: encode(script) });
  const declined = r.code !== 0 && /cancel/i.test(r.stderr);
  return { code: r.code, declined, stderr: r.stderr.trim() };
}

module.exports = {
  RUN_KEY,
  RUNONCE_KEY,
  setValueScript,
  removeValueScript,
  setRegistryValue,
  removeRegistryValue,
  getRegistryValue,
  elevatedRun,
  virtualization,
  BASTION_SERVICE,
  serviceStateScript,
  bastionServiceState,
  currentUserSid,
  ELEVATE_FROM_STDIN,
  elevatedScript,
};
