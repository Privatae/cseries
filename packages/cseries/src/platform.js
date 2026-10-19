'use strict';

// Where things go on each OS, and this machine's own CPU architecture.
// Windows paths are built with path.win32 so they come out the same whether
// this runs on Windows or in a test on Linux.

const os = require('node:os');
const path = require('node:path');

// The CPU the machine has, not the one this Node was built for: an x64 Node
// on an Arm64 Windows runs under emulation, and the VM must be Arm64.
function machineArch(platform = process.platform, env = process.env, arch = process.arch) {
  if (platform === 'win32') {
    const a = (env.PROCESSOR_ARCHITEW6432 || env.PROCESSOR_ARCHITECTURE || '').toUpperCase();
    if (a === 'ARM64') return 'arm64';
    if (a === 'AMD64' || a === 'X64') return 'x64';
  }
  if (arch === 'x64' || arch === 'arm64') return arch;
  return arch;
}

// serviceDir/serviceExe: where the bastion runs from as the machine's system
// service, a folder only an administrator can change (on Windows Program
// Files, as the bastion's own install.ps1 -Networked puts it; on macOS the
// root-owned folder install.sh uses; on Linux install.sh's /usr/local/bin).
// bastionDir/bastionExe: the per-user install earlier releases of this
// command made, kept so the system-service install can remove it.
// installLog: what the elevated step wrote, read back by this command.
function paths(platform = process.platform, env = process.env, home = os.homedir()) {
  if (platform === 'win32') {
    const p = path.win32;
    const local = env.LOCALAPPDATA || p.join(home, 'AppData', 'Local');
    const base = p.join(local, 'cseries');
    const bastionDir = p.join(local, 'Programs', 'privatae-bastion');
    const serviceDir = p.join(env.ProgramFiles || 'C:\\Program Files', 'Privatae');
    return {
      base,
      state: p.join(base, 'state.json'),
      downloads: p.join(base, 'downloads'),
      wsl: p.join(base, 'wsl'),
      hostCa: p.join(base, 'host-ca.pem'),
      installLog: p.join(base, 'bastion-install.log'),
      bastionDir,
      bastionExe: p.join(bastionDir, 'privatae-bastion.exe'),
      serviceDir,
      serviceExe: p.join(serviceDir, 'privatae-bastion.exe'),
      join: p.join,
    };
  }
  const p = path.posix;
  const base = platform === 'darwin'
    ? p.join(home, 'Library', 'Application Support', 'cseries')
    : p.join(env.XDG_DATA_HOME || p.join(home, '.local', 'share'), 'cseries');
  const bastionDir = p.join(home, '.local', 'bin');
  const serviceDir = platform === 'darwin' ? '/Library/Application Support/privatae/bin' : '/usr/local/bin';
  return {
    base,
    state: p.join(base, 'state.json'),
    downloads: p.join(base, 'downloads'),
    hostCa: p.join(base, 'host-ca.pem'),
    installLog: p.join(base, 'bastion-install.log'),
    bastionDir,
    bastionExe: p.join(bastionDir, 'privatae-bastion'),
    serviceDir,
    serviceExe: p.join(serviceDir, 'privatae-bastion'),
    join: p.join,
  };
}

// The bastion manifest's key for this machine's binary.
function bastionBinaryKey(platform, arch) {
  const cpu = arch === 'arm64' ? 'arm64' : 'amd64';
  if (platform === 'win32') return `windows-${cpu}.exe`;
  if (platform === 'darwin') return `darwin-${cpu}`;
  return `linux-${cpu}`;
}

module.exports = { machineArch, paths, bastionBinaryKey };
