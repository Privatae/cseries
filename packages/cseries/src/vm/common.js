'use strict';

// What every VM driver shares, in a module of its own so the drivers can
// require it without requiring driver.js (which requires them).
//
// PORTS: the guest publishes these on its own 127.0.0.1, and the driver makes
// them, and only them, reachable on the machine's 127.0.0.1: the dashboard,
// and the bastion's enroll and lane listeners.
//
// The VM's size is the person's choice (size.js): on macOS and Linux it is
// the Lima VM's own (lima.js), on Windows the ceiling on C-Series's units
// inside its WSL system (size-caps.js). What keeps the Host from taking all
// of it is inside the guest, the same units on every OS: the Host's memory
// ceiling (host.js memoryMax, written to 10-memory.conf), every processor but
// one (host.js cpuQuota, 10-cpu.conf), and the database's 512 MB.

const PORTS = [8800, 8810, 8811];

module.exports = { PORTS };
