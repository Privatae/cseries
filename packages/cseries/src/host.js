'use strict';

// The Host in the guest: hand the guest the two images this release pins (by
// digest), and the local runtime's images and the coding engines' sources
// when it pins them, let it pull them, and start or restart its units.

const { CliError } = require('./errors');
const { pinnedImage, runtimeLines } = require('./release');

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
// The least the Host is given, even on a system too small for the reserve:
// about four times the 119 MiB a live idle Host was measured at, room for a
// few turns of 11.5 MiB each (brain/mastercode/runtime/host_memory.py).
const HOST_FLOOR = 512 * MiB;
// Where the guest runs the local runtime (cseries-runtime: the Host's apps,
// its browser and its coding engines, each in a container beside it), the
// Host and those containers share one budget: the runtime admits containers
// only into what the system's reserve, the database's ceiling and the Host's
// ceiling leave (vm/runtime guard.py, held_mb). At its ceiling the kernel
// kills inside the Host (cseries-host.container: no MemoryHigh, no swap), so
// the ceiling is set from what live Hosts were measured to use, and the
// containers, which the runtime refuses with a reason when there is no room,
// get the rest.
//
// Measured on the live Hosts (2026-10-20; cAdvisor's
// container_memory_working_set_bytes of each Host's container, its
// Kubernetes memory limit 2 to 16 GiB, no restarts in 30 days): ten Hosts
// stayed under 200 MiB over 30 days (7-day P95 112 to 159 MiB); the busiest
// one, used every day for months, had a 7-day P95 of 777 MiB, a P99 of
// 779 MiB, a max of 901 MiB (30-day max the same) and an RSS max of 633 MiB.
// So:
//   - HOST_NEED first: 1 GiB holds that busiest Host's largest working set
//     with 123 MiB to spare and its RSS with 391 MiB, so the Host is never
//     killed for the use a live Host has. On a 4 GiB system (WSL's default
//     for an 8 GB PC) that leaves 1436 MB for the containers: the browser
//     (1 GiB) or a coding engine (1 GiB and 576 MiB for each run slot past
//     the first, cseries_runtime/common.py) with small apps beside it. On a
//     system whose room is under 1 GiB (under 3 GiB, below the 4 GB the
//     installer asks for) the Host takes all of it and the containers none;
//   - a quarter of the room once that is more: from about 6 GiB the
//     containers grow faster than the Host, whose own memory is a small
//     process plus 11.5 MB for each turn it runs at once (measured by
//     scripts/rig_parallel_threads.py, on a 122 MB baseline); on 8 GiB that is
//     1434 MB for the Host and 4302 MB for the containers, room for the
//     browser and a coding engine together;
//   - never under HOST_FLOOR, which stands only where the room is smaller;
//   - never over HOST_CAP: 2 GiB is 2.3 times the busiest Host's largest
//     working set and holds the baseline and over 160 turns at once, more
//     than a person's provider rate limits let run, so from a 16 GiB system
//     up every further MiB goes to the containers. No live Host came near it,
//     so it stays.
const HOST_NEED = 1 * GiB;
const HOST_CAP = 2 * GiB;
const HOST_SHARE = 1 / 4;

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
// MemTotal (kB) the guest's status reports. The room is the system's memory
// less the system's reserve and the database's ceiling. Without the local
// runtime the Host gets all of it; with it (runtime: true, a guest whose
// status names the runtime) HOST_NEED or the whole room when that is less,
// or HOST_SHARE of it when that is more, up to HOST_CAP (above).
// Never below HOST_FLOOR. null when the guest did not report its memory (a
// guest image from before it did, which would also refuse the key); the base
// unit's own share then stands.
function memoryMax(memTotalKb, { runtime = false } = {}) {
  const s = String(memTotalKb === undefined || memTotalKb === null ? '' : memTotalKb).trim();
  if (!/^[1-9][0-9]*$/.test(s)) return null;
  const total = Number(s) * 1024;
  const room = total - systemReserve(total) - DATABASE_MAX;
  const host = runtime
    ? Math.min(HOST_CAP, Math.max(HOST_FLOOR, Math.min(HOST_NEED, room), room * HOST_SHARE))
    : Math.max(HOST_FLOOR, room);
  return `${Math.floor(host / MiB)}M`;
}

// What that leaves the runtime's containers, in MB (0 when nothing).
function containersMax(memTotalKb) {
  const mem = memoryMax(memTotalKb, { runtime: true });
  if (mem === null) return null;
  const total = Number(String(memTotalKb).trim()) * 1024;
  const room = total - systemReserve(total) - DATABASE_MAX;
  return Math.max(0, Math.floor(room / MiB) - parseInt(mem, 10));
}

// The local runtime is on the guest when its status names it (runtime=, the
// state of cseries-runtime.socket); a guest image from before it does not.
function hasRuntime(st) {
  return Object.prototype.hasOwnProperty.call(st || {}, 'runtime');
}

function configInput(release, { cpuQuota: quota = null, memoryMax: mem = null } = {}) {
  let s = `host=${pinnedImage(release, 'host')}\npostgres=${pinnedImage(release, 'postgres')}\nrelease=${release.version}\n`;
  if (quota) s += `cpu_quota=${quota}\n`;
  if (mem) s += `memory_max=${mem}\n`;
  return s + runtimeLines(release);
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
    const runtime = hasRuntime(st);
    const mem = memoryMax(st.mem_total_kb, { runtime });
    const changed = await configure(ctx, { cpuQuota: quota, memoryMax: mem });
    if (quota) {
      ctx.out.detail(`The Host may use ${Math.max(1, Number(st.cpus) - 1)} of this Linux system's ${st.cpus} processors.`);
    }
    if (mem) {
      ctx.out.detail(`The Host may use up to ${parseInt(mem, 10)} MB of this Linux system's ${Math.floor(Number(st.mem_total_kb) / 1024)} MB of memory.`);
      if (runtime) {
        ctx.out.detail(`Its apps, browser and coding engines may use up to ${containersMax(st.mem_total_kb)} MB beside it.`);
      }
    }
    await pull(ctx);
    await start(ctx, changed);
    if (changed) ctx.out.detail(`The Host runs release ${rel.version}.`);
    await require('./setup').waitHealthy(ctx);
  },
};

module.exports = { step, configure, configInput, cpuQuota, memoryMax, containersMax, hasRuntime, systemReserve, pull, start, guestStatus };
