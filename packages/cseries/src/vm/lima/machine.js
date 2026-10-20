'use strict';

// What this machine has for Lima, and on Linux what one sudo run installs.
//
// The facts come from one small shell script run through the runner (so a
// test answers it), as key=value lines:
//
//   both     uid, user
//   macOS    macos (sw_vers -productVersion), hv (sysctl kern.hv_support)
//   Linux    qemu, qemu_img, ssh, sg: their paths, or empty
//            firmware: the first UEFI image Lima would find (the same places
//              Lima 2.2's QEMU driver looks, getFirmware in qemu.go)
//            kvm: ok | denied | missing; kvm_group: the group /dev/kvm
//              belongs to; kvm_member: whether the person is in it according
//              to the group database, which a usermod changes at once while
//              this session's own groups do not
//            pm: the package manager; systemd: whether systemd runs the
//              machine; sudo: sudo by absolute path
//
// QEMU is GPL and is never bundled: it comes from the distribution's own
// packages. sudo is run by its absolute path, never found through PATH, since
// a folder early in the person's PATH could hold another program by that name
// that would be handed their password.

const { CliError } = require('../../errors');

const QEMU = { x64: 'qemu-system-x86_64', arm64: 'qemu-system-aarch64' };
const EDK2 = { x64: 'x86_64', arm64: 'aarch64' };

const FIRMWARE = {
  x64: [
    '/usr/share/edk2/x64/OVMF_CODE.4m.fd',
    '/usr/share/OVMF/OVMF_CODE.fd',
    '/usr/share/OVMF/OVMF_CODE_4M.fd',
    '/usr/share/edk2/ovmf/OVMF_CODE.fd',
    '/usr/share/qemu/ovmf-x86_64.bin',
    '/usr/share/qemu/ovmf-x86_64-4m.bin',
  ],
  arm64: [
    '/usr/share/edk2/aarch64/QEMU_CODE.fd',
    '/usr/share/AAVMF/AAVMF_CODE.fd',
    '/usr/share/qemu-efi-aarch64/QEMU_EFI.fd',
  ],
};

const SUDO = ['/usr/bin/sudo', '/bin/sudo', '/run/wrappers/bin/sudo'];

// Per package manager: how it installs, and the package for each missing
// piece. Names as each distribution published them in October 2026.
const PACKAGES = {
  'apt-get': {
    install: (p) => `apt-get update -q </dev/null && DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends ${p} </dev/null`,
    qemu: { x64: 'qemu-system-x86', arm64: 'qemu-system-arm' },
    qemu_img: 'qemu-utils',
    firmware: { x64: 'ovmf', arm64: 'qemu-efi-aarch64' },
    ssh: 'openssh-client',
  },
  dnf: {
    install: (p) => `dnf install -y ${p} </dev/null`,
    qemu: { x64: 'qemu-system-x86-core', arm64: 'qemu-system-aarch64-core' },
    qemu_img: 'qemu-img',
    firmware: { x64: 'edk2-ovmf', arm64: 'edk2-aarch64' },
    ssh: 'openssh-clients',
  },
  zypper: {
    install: (p) => `zypper --non-interactive install --no-recommends ${p} </dev/null`,
    qemu: { x64: 'qemu-x86', arm64: 'qemu-arm' },
    qemu_img: 'qemu-tools',
    firmware: { x64: 'qemu-ovmf-x86_64', arm64: 'qemu-uefi-aarch64' },
    ssh: 'openssh-clients',
  },
  pacman: {
    install: (p) => `pacman -S --needed --noconfirm ${p} </dev/null`,
    qemu: { x64: 'qemu-system-x86', arm64: 'qemu-system-aarch64' },
    qemu_img: 'qemu-img',
    firmware: { x64: 'edk2-ovmf', arm64: 'edk2-aarch64' },
    ssh: 'openssh',
  },
};

function shq(s) {
  return `'${String(s).replace(/'/g, `'\\''`)}'`;
}

function probeScript(platform, arch) {
  const lines = ['echo "uid=$(id -u)"', 'echo "user=$(id -un)"'];
  if (platform === 'darwin') {
    lines.push(
      'echo "macos=$(/usr/bin/sw_vers -productVersion 2>/dev/null)"',
      'echo "hv=$(/usr/sbin/sysctl -n kern.hv_support 2>/dev/null)"',
    );
    return lines.join('\n');
  }
  const qemu = QEMU[arch];
  const edk2 = `share/qemu/edk2-${EDK2[arch]}-code.fd`;
  lines.push(
    'w() { command -v "$1" 2>/dev/null || true; }',
    `q=$(w ${qemu})`,
    'echo "qemu=$q"',
    'echo "qemu_img=$(w qemu-img)"',
    'echo "ssh=$(w ssh)"',
    'echo "sg=$(w sg)"',
    'fw=""',
    `for f in "$HOME/.local/${edk2}" \${q:+"$(dirname "$(dirname "$q")")/${edk2}"} ${FIRMWARE[arch].map(shq).join(' ')}; do`,
    '  if [ -e "$f" ]; then fw=$f; break; fi',
    'done',
    'echo "firmware=$fw"',
    'if [ -e /dev/kvm ]; then',
    '  if [ -r /dev/kvm ] && [ -w /dev/kvm ]; then echo kvm=ok; else echo kvm=denied; fi',
    'else echo kvm=missing; fi',
    'g=$(stat -c %G /dev/kvm 2>/dev/null || true)',
    'echo "kvm_group=$g"',
    'if [ -n "$g" ] && id -nG "$(id -un)" | tr " " "\\n" | grep -qx "$g"; then echo kvm_member=yes; else echo kvm_member=no; fi',
    'for pm in apt-get dnf zypper pacman; do if command -v "$pm" >/dev/null 2>&1; then echo "pm=$pm"; break; fi; done',
    'if [ -d /run/systemd/system ]; then echo systemd=yes; else echo systemd=no; fi',
    `for s in ${SUDO.join(' ')}; do if [ -x "$s" ]; then echo "sudo=$s"; break; fi; done`,
  );
  return lines.join('\n');
}

function parseFacts(text) {
  const f = {};
  for (const line of String(text).split('\n')) {
    const i = line.indexOf('=');
    if (i > 0) f[line.slice(0, i).trim()] = line.slice(i + 1).trim();
  }
  return f;
}

async function probe(ctx) {
  const r = await ctx.runner.run('/bin/sh', ['-c', probeScript(ctx.platform, ctx.arch)], { timeoutMs: 20000 });
  if (r.code !== 0) {
    throw new CliError(`Could not look at what this machine has for its Linux VM: ${r.stderr.toString('utf8').trim() || `exit ${r.code}`}`, '');
  }
  return parseFacts(r.stdout.toString('utf8'));
}

// The pieces of QEMU Lima needs that are not here (Linux).
function missingPieces(f) {
  const out = [];
  if (!f.qemu) out.push('qemu');
  if (!f.qemu_img) out.push('qemu_img');
  if (!f.firmware) out.push('firmware');
  if (!f.ssh) out.push('ssh');
  return out;
}

// What /dev/kvm needs: 'ok', 'sg' (the person is in its group, this session
// is not yet: Lima is started through `sg <group>`), 'join' (sudo adds the
// person to the group), or 'missing' / 'refuse' with nothing this command
// can do about it.
function kvmAccess(f) {
  if (f.kvm === 'ok') return 'ok';
  if (f.kvm !== 'denied') return 'missing';
  if (f.kvm_member === 'yes') return f.sg ? 'sg' : 'refuse';
  if (!f.kvm_group || f.kvm_group === 'root' || f.kvm_group === 'UNKNOWN') return 'refuse';
  return 'join';
}

function packagesFor(f, arch, pieces) {
  const pm = PACKAGES[f.pm];
  if (!pm) return null;
  return pieces.map((p) => {
    const v = pm[p];
    return typeof v === 'string' ? v : v[arch];
  });
}

// The one sudo run on Linux: QEMU's missing pieces from the package manager,
// and the person into /dev/kvm's group when the device is not theirs to open.
function installScript(f, arch, { packages, join }) {
  const lines = ['set -eu', 'PATH=/usr/sbin:/usr/bin:/sbin:/bin', 'export PATH'];
  if (packages && packages.length) lines.push(PACKAGES[f.pm].install(packages.map(shq).join(' ')));
  if (join) lines.push(`usermod -a -G ${shq(f.kvm_group)} ${shq(f.user)}`);
  return lines.join('\n');
}

// A command line for `sg <group> -c`, every argument single-quoted.
function sgCommand(cmd, args) {
  return [cmd, ...args].map(shq).join(' ');
}

module.exports = {
  probe, probeScript, parseFacts, missingPieces, kvmAccess, packagesFor, installScript, sgCommand, shq,
  QEMU, FIRMWARE, PACKAGES, SUDO,
};
