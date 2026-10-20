'use strict';

// What every VM driver shares, in a module of its own so the drivers can
// require it without requiring driver.js (which requires them).
//
// PORTS: the guest publishes these on its own 127.0.0.1, and the driver makes
// them, and only them, reachable on the machine's 127.0.0.1: the dashboard,
// and the bastion's enroll and lane listeners.
//
// guestResources: the VM's size, the same on every OS. On Windows this
// command writes WSL's limits into .wslconfig (wslconfig.js vmLimits: half the
// PC's memory to the nearest GB, at least 2, and every logical processor), so
// a Mac or a Linux machine gives its VM exactly that. What keeps the Host from
// taking the machine is inside the guest, the same units on every OS: the
// Host's memory ceiling from the VM's own memory (host.js memoryMax, written
// to 10-memory.conf), every processor but one (host.js cpuQuota, 10-cpu.conf),
// and the database's 512 MB.

const os = require('node:os');
const { vmLimits } = require('./wslconfig');

const PORTS = [8800, 8810, 8811];

const GiB = 1024 ** 3;
// Below this the Host and its database do not fit with room to work.
const HOST_NEEDS_GIB = 4;

function cpuCount() {
  return typeof os.availableParallelism === 'function' ? os.availableParallelism() : os.cpus().length;
}

// -> {memoryGiB, cpus, totalGiB}
function guestResources(totalBytes = os.totalmem(), cpus = cpuCount()) {
  const totalGiB = Math.floor(totalBytes / GiB);
  const want = vmLimits(totalBytes, Math.floor(cpus) || 1);
  // vmLimits' floor of 2 GB never asks for more than the machine has.
  const memoryGiB = Math.max(1, Math.min(parseInt(want.memory, 10), totalGiB));
  return { memoryGiB, cpus: Number(want.processors), totalGiB };
}

// The same for this run's machine (cli.js makeContext's ctx.machine).
function guestResourcesFor(ctx) {
  const m = ctx && ctx.machine;
  return m ? guestResources(m.totalMem, m.cpus) : guestResources();
}

module.exports = { PORTS, guestResources, guestResourcesFor, HOST_NEEDS_GIB };
