'use strict';

// Checks before anything is installed: the OS and virtualization (the
// driver's), free disk, and the three ports on 127.0.0.1.

const fs = require('node:fs');
const net = require('node:net');
const path = require('node:path');
const { CliError } = require('./errors');
const { PORTS } = require('./vm/driver');
const { formatBytes } = require('./output');

const NEED_BYTES = 10 * 1024 ** 3;

// -> 'free' | 'busy' | 'reserved'
function portState(port, host = '127.0.0.1') {
  return new Promise((resolve) => {
    const srv = net.createServer();
    srv.once('error', (err) => resolve(err.code === 'EACCES' ? 'reserved' : 'busy'));
    srv.listen({ port, host, exclusive: true }, () => srv.close(() => resolve('free')));
  });
}

function freeBytes(dir) {
  // fs.statfs is in Node 18.15 and later; an older 18 skips the check.
  if (typeof fs.statfsSync !== 'function') return null;
  let d = dir;
  while (d && !fs.existsSync(d)) {
    const up = path.dirname(d);
    if (up === d) break;
    d = up;
  }
  try {
    const s = fs.statfsSync(d);
    return Number(s.bavail) * Number(s.bsize);
  } catch {
    return null;
  }
}

async function checkDisk(ctx, installed) {
  const free = freeBytes(ctx.paths.base);
  if (free === null) return;
  if (!installed && free < NEED_BYTES) {
    throw new CliError(
      `C-Series needs about 10 GB of free disk and the drive with ${ctx.paths.base} has ${formatBytes(free)}.`,
      'Free some space on that drive and run npx cseries again.');
  }
  ctx.out.detail(`Free disk: ${formatBytes(free)}.`);
}

// A busy port is fine when it is this Host answering; anything else holding
// it would stand between the browser or the bastion and the Host.
async function checkPorts(ctx, ours) {
  for (const port of PORTS) {
    const s = await (ctx.portState || portState)(port);
    if (s === 'free') continue;
    if (ours) continue;
    if (s === 'reserved') {
      throw new CliError(
        `Windows has reserved port ${port} on 127.0.0.1 for itself, and C-Series needs it.`,
        `"netsh interface ipv4 show excludedportrange protocol=tcp" lists the reserved ranges. Restarting Windows usually moves them; then run npx cseries again.`);
    }
    throw new CliError(
      `Another program on this machine is using port ${port} on 127.0.0.1, and C-Series needs it.`,
      `Stop that program (on Windows, "netstat -ano | findstr :${port}" shows its process id) and run npx cseries again.`);
  }
}

module.exports = { portState, freeBytes, checkDisk, checkPorts, NEED_BYTES };
