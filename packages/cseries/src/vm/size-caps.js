'use strict';

// Windows: the Host's size as a ceiling inside C-Series's own WSL system.
// WSL runs every distribution on the PC in one VM, sized by the person's
// .wslconfig for all of them, so the size (size.js) cannot be the VM's. It is
// set on C-Series's units instead, which caps nothing else on the PC:
//
//   - the Host: its memory ceiling and processor quota from the size, not
//     from the whole VM (host.js memoryMax and cpuQuota, written by
//     `cseries-guest configure` to the Host's 10-memory.conf and 10-cpu.conf);
//   - the database: its own 512 MB (cseries-postgres.container);
//   - the apps, browser and coding engines (cseries.slice, where the local
//     runtime starts them): MemoryMax= what the size leaves them
//     (host.js containersMax), the same figure the runtime admits them into
//     (vm/runtime guard.py reads the slice's ceiling too);
//   - processors: AllowedCPUs= the size's first N processors for the Host,
//     the database and cseries.slice, so together they never use more than
//     N, however busy.
//
// Written by `systemctl set-property`, as root in the guest (driver
// guestShell), which applies it at once and keeps it in
// /etc/systemd/system.control for every boot after. Any guest image has
// systemctl, so a guest from an older release gets the same ceilings.

const { CliError } = require('../errors');

const UNITS = ['cseries-host.service', 'cseries-postgres.service'];
const SLICE = 'cseries.slice';

// The guest script: each unit that is loaded gets its ceilings; one that is
// not (a guest without the local runtime has no cseries.slice) is named.
function script({ cpus, containersMb }) {
  if (!Number.isInteger(cpus) || cpus < 1) throw new Error('cpus must be a whole number of at least 1');
  if (!Number.isInteger(containersMb) || containersMb < 0) throw new Error('containersMb must be a whole number');
  const allowed = cpus === 1 ? '0' : `0-${cpus - 1}`;
  const lines = [
    'PATH=/usr/sbin:/usr/bin:/sbin:/bin',
    'set -u',
    'ok=1',
    'loaded() { [ "$(systemctl show -p LoadState --value "$1" 2>/dev/null)" = loaded ]; }',
  ];
  for (const u of UNITS) {
    lines.push(`if loaded ${u}; then systemctl set-property ${u} AllowedCPUs=${allowed} || ok=0; fi`);
  }
  // At least 1M: MemoryMax=0 is no ceiling of a size, and the runtime admits
  // nothing into 0 MB either way.
  lines.push(`if loaded ${SLICE}; then systemctl set-property ${SLICE} MemoryMax=${Math.max(1, containersMb)}M AllowedCPUs=${allowed} || ok=0; echo slice=set; else echo slice=none; fi`);
  lines.push('[ "$ok" = 1 ] || exit 1');
  return `${lines.join('\n')}\n`;
}

// -> {slice: bool}: whether cseries.slice got its ceiling.
async function apply(ctx, { cpus, containersMb }) {
  const r = await ctx.driver.guestShell(ctx, script({ cpus, containersMb }));
  if (r.code !== 0) {
    throw new CliError(`The Linux system did not take the Host's size: ${`${r.stderr}${r.stdout}`.trim() || `exit ${r.code}`}`,
      'Run npx cseries again. "npx cseries status" shows the size.');
  }
  return { slice: /^slice=set$/m.test(r.stdout) };
}

module.exports = { apply, script, UNITS, SLICE };
