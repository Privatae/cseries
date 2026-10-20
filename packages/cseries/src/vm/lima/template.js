'use strict';

// The Lima template for the instance "cseries", written by createGuest and
// read once by `limactl create`, which keeps its own copy in the instance.
//
// What it holds, and why:
//   - the release's guest image, by path and sha256;
//   - vz on macOS, QEMU on Linux, the machine's own CPU architecture;
//   - the VM's share of the machine (driver.js guestResources);
//   - no folder of the machine mounted, no containerd, no SSH agent or X11
//     forwarded into the guest;
//   - 127.0.0.1:8800, 8810 and 8811 forwarded to the same ports on the
//     machine's 127.0.0.1, then every other port on every guest address
//     ignored (guestIPMustBeZero: false makes 0.0.0.0 match any address the
//     guest binds, not only 0.0.0.0 itself);
//   - Lima's own user in the guest (the one `limactl shell` logs in as, with
//     sudo) is uid 2000: the Host's containers run as uid 1000, so a process
//     that got out of one would not be that user.
//
// Every value goes out JSON-quoted, which YAML reads as a double-quoted
// string, so a path with a space or a quote in it stays one value.

const { PORTS } = require('../common');

const LIMA_ARCH = { x64: 'x86_64', arm64: 'aarch64' };
const GUEST_UID = 2000;
const DISK_GIB = 100;

function q(v) {
  return JSON.stringify(String(v));
}

// opts: {vmType: 'vz'|'qemu', arch: 'x64'|'arm64', image, sha256, cpus, memoryGiB}
function limaTemplate(opts) {
  const arch = LIMA_ARCH[opts.arch];
  if (!arch) throw new Error(`no Lima architecture for ${opts.arch}`);
  if (!/^[0-9a-f]{64}$/.test(opts.sha256 || '')) throw new Error('the guest image needs its sha256');
  for (const k of ['cpus', 'memoryGiB']) {
    if (!Number.isInteger(opts[k]) || opts[k] < 1) throw new Error(`${k} must be a whole number of at least 1`);
  }
  const lines = [
    '# The cseries command\'s Lima instance. Written once, for `limactl create`.',
    'minimumLimaVersion: "2.0.0"',
    `vmType: ${q(opts.vmType)}`,
    'os: "Linux"',
    `arch: ${q(arch)}`,
    'images:',
    `- location: ${q(opts.image)}`,
    `  arch: ${q(arch)}`,
    `  digest: ${q(`sha256:${opts.sha256}`)}`,
    `cpus: ${opts.cpus}`,
    `memory: ${q(`${opts.memoryGiB}GiB`)}`,
    `disk: ${q(`${DISK_GIB}GiB`)}`,
    'mounts: []',
    'containerd:',
    '  system: false',
    '  user: false',
    'ssh:',
    '  loadDotSSHPubKeys: false',
    '  forwardAgent: false',
    '  forwardX11: false',
    'portForwards:',
  ];
  for (const port of PORTS) {
    lines.push(
      '- guestIP: "127.0.0.1"',
      `  guestPort: ${port}`,
      '  hostIP: "127.0.0.1"',
      `  hostPort: ${port}`,
    );
  }
  lines.push(
    '- guestIP: "0.0.0.0"',
    '  guestIPMustBeZero: false',
    '  guestPortRange: [1, 65535]',
    '  proto: "any"',
    '  ignore: true',
    'user:',
    '  name: "cseries"',
    `  uid: ${GUEST_UID}`,
    'video:',
    '  display: "none"',
    'upgradePackages: false',
    '',
  );
  return lines.join('\n');
}

module.exports = { limaTemplate, LIMA_ARCH, GUEST_UID, DISK_GIB };
