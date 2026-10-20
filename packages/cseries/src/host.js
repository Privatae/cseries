'use strict';

// The Host in the guest: hand the guest the two images this release pins (by
// digest), let it pull them, and start or restart its units.

const { CliError } = require('./errors');
const { pinnedImage } = require('./release');

// The Host's processor ceiling, as systemd's CPUQuota= takes it (100% is one
// processor): every processor the Linux system has but one, so whatever the
// Host runs leaves one for the system and the machine under it. A machine
// with one processor has none to spare, and the Host may use it. null when
// the guest did not report a count: a guest image from before it did, which
// would also refuse the key.
function cpuQuota(cpus) {
  const s = String(cpus === undefined || cpus === null ? '' : cpus).trim();
  if (!/^[1-9][0-9]*$/.test(s)) return null;
  return `${Math.max(1, Number(s) - 1) * 100}%`;
}

const MiB = 1024 ** 2;
const GiB = 1024 ** 3;
// cseries-postgres.container's MemoryMax.
const DATABASE_MAX = 512 * MiB;
// The least the Host is given on a system too small for the reserve: about
// four times the 119 MiB a live idle Host was measured at, room for a few
// turns of 11.5 MiB each (brain/mastercode/runtime/host_memory.py).
const HOST_FLOOR = 512 * MiB;

// What a Linux system of this many bytes keeps for itself, by GKE's rule for
// a node's own reservation: 25% of the first 4 GiB, 20% of the next 4, 10%
// of the next 8, 6% of the next 112, 2% above that, and 100 MiB for eviction.
function systemReserve(bytes) {
  const tiers = [[4, 0.25], [4, 0.20], [8, 0.10], [112, 0.06], [Infinity, 0.02]];
  let left = bytes / GiB;
  let reserve = 0;
  for (const [size, share] of tiers) {
    const take = Math.min(left, size);
    reserve += take * share;
    left -= take;
  }
  return reserve * GiB + 100 * MiB;
}

// The Host's memory ceiling, as systemd's MemoryMax= takes it, from the
// MemTotal (kB) the guest's status reports: the system's memory less the
// system's reserve and the database's ceiling, never below HOST_FLOOR. null
// when the guest did not report it (a guest image from before it did, which
// would also refuse the key); the base unit's own share then stands.
function memoryMax(memTotalKb) {
  const s = String(memTotalKb === undefined || memTotalKb === null ? '' : memTotalKb).trim();
  if (!/^[1-9][0-9]*$/.test(s)) return null;
  const total = Number(s) * 1024;
  const room = total - systemReserve(total) - DATABASE_MAX;
  return `${Math.floor(Math.max(HOST_FLOOR, room) / MiB)}M`;
}

function configInput(release, { cpuQuota: quota = null, memoryMax: mem = null } = {}) {
  let s = `host=${pinnedImage(release, 'host')}\npostgres=${pinnedImage(release, 'postgres')}\nrelease=${release.version}\n`;
  if (quota) s += `cpu_quota=${quota}\n`;
  if (mem) s += `memory_max=${mem}\n`;
  return s;
}

// -> true when the guest's images (or the Host's processor or memory ceiling) changed.
async function configure(ctx, limits = {}) {
  const r = await ctx.driver.guest(ctx, ['configure'], { input: configInput(ctx.releaseInfo.release, limits) });
  if (r.code !== 0) {
    throw new CliError(`The C-Series Linux system refused this release's images: ${`${r.stderr}${r.stdout}`.trim()}`,
      'Reinstall this package (npx cseries@latest) and run it again.');
  }
  return /changed=1/.test(r.stdout);
}

async function pull(ctx) {
  ctx.out.detail('Downloading the Host\'s images if they are not here yet (about 2 GB the first time)...');
  const r = await ctx.driver.guest(ctx, ['pull'], { inherit: true });
  if (r.code !== 0) {
    throw new CliError('The Host\'s images could not be downloaded.',
      'Check this machine\'s internet connection and run npx cseries again; what was already downloaded is kept.');
  }
}

async function start(ctx, restart) {
  const r = await ctx.driver.guest(ctx, [restart ? 'restart' : 'start']);
  if (r.code !== 0) {
    throw new CliError(`The Host could not be started: ${`${r.stderr}${r.stdout}`.trim()}`,
      'Run npx cseries again. "npx cseries logs host" shows the Host\'s log.');
  }
}

async function guestStatus(ctx) {
  const r = await ctx.driver.guest(ctx, ['status']);
  const s = {};
  if (r.code !== 0) return s;
  for (const line of r.stdout.split('\n')) {
    const i = line.indexOf('=');
    if (i > 0) s[line.slice(0, i).trim()] = line.slice(i + 1).trim();
  }
  return s;
}

const step = {
  title: 'Starting the Host',
  async run(ctx) {
    const st = await guestStatus(ctx);
    const rel = ctx.releaseInfo.release;
    if (st.guest && st.guest !== 'unknown' && st.guest !== rel.version) {
      ctx.out.detail(`The Linux system is from release ${st.guest}; the Host is updated to ${rel.version} on it.`);
    }
    const quota = cpuQuota(st.cpus);
    const mem = memoryMax(st.mem_total_kb);
    const changed = await configure(ctx, { cpuQuota: quota, memoryMax: mem });
    if (quota) {
      ctx.out.detail(`The Host may use ${Math.max(1, Number(st.cpus) - 1)} of this Linux system's ${st.cpus} processors.`);
    }
    if (mem) {
      ctx.out.detail(`The Host may use up to ${parseInt(mem, 10)} MB of this Linux system's ${Math.floor(Number(st.mem_total_kb) / 1024)} MB of memory.`);
    }
    await pull(ctx);
    await start(ctx, changed);
    if (changed) ctx.out.detail(`The Host runs release ${rel.version}.`);
    await require('./setup').waitHealthy(ctx);
  },
};

module.exports = { step, configure, configInput, cpuQuota, memoryMax, pull, start, guestStatus };
