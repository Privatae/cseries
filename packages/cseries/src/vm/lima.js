'use strict';

// macOS and Linux will run the guest in Lima (vz on macOS, QEMU/KVM on Linux),
// with the same cseries-guest inside, through `limactl shell cseries sudo
// cseries-guest ...`. That driver is the next piece of work; until it lands
// this one only says so.

function driver(osName) {
  return {
    name: 'lima',
    label: 'Lima',
    supported: false,
    unsupportedMessage: `npx cseries on ${osName} is coming next: today it installs on Windows only.`,
    unsupportedNext: 'Until then, a C-Series Host runs on this machine with Docker: see https://github.com/Privatae/cseries#readme',
  };
}

module.exports = { driver };
