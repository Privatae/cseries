'use strict';

// The Host in the guest: hand the guest the two images this release pins (by
// digest), and the local runtime's images and the coding engines' sources
// when it pins them, let it pull them, and start or restart its units.

const { CliError } = require('./errors');
const { formatBytes } = require('./output');
const { NO_LOG } = require('./log');
const { pinnedImage, hasDoor, runtimeLines } = require('./release');
const machineFirewall = require('./vm/machine-firewall');

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

// The guest knows the remote-access door when its status names it (door=,
// the state of the door's unit); a guest image from before it does not, and
// its configure would refuse the door= key.
function guestHasDoor(st) {
  return Object.prototype.hasOwnProperty.call(st || {}, 'door');
}

function configInput(release, { cpuQuota: quota = null, memoryMax: mem = null, door = false } = {}) {
  let s = `host=${pinnedImage(release, 'host')}\npostgres=${pinnedImage(release, 'postgres')}\nrelease=${release.version}\n`;
  if (quota) s += `cpu_quota=${quota}\n`;
  if (mem) s += `memory_max=${mem}\n`;
  // The door's image, for its door and certificate units: only to a guest
  // that knows the key, and only when this release pins one.
  if (door && hasDoor(release)) s += `door=${pinnedImage(release, 'door')}\n`;
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

// What each pinned image is called on the status line, by its digest: the
// guest pulls by digest, and podman's "Trying to pull <ref>" names it.
const IMAGE_WORDS = {
  host: 'the Host\'s image',
  postgres: 'the database image',
  python: 'the apps\' Python image',
  creative: 'the creative engines\' image',
  helper: 'the helper image',
  browser: 'the browser\'s image',
  door: 'the remote access door\'s image',
};

function imagesByDigest(release) {
  const m = new Map();
  for (const [which, img] of Object.entries(release.images || {})) {
    if (img && img.digest) m.set(img.digest, IMAGE_WORDS[which] || which);
  }
  for (const [which, img] of Object.entries(release.runtime || {})) {
    if (img && img.digest) m.set(img.digest, IMAGE_WORDS[which] || which);
  }
  return m;
}

const SIZE_UNITS = { B: 1, KB: 1e3, MB: 1e6, GB: 1e9, KIB: 1024, MIB: 1024 ** 2, GIB: 1024 ** 3 };

function sizeBytes(n, unit) {
  return Number(n) * (SIZE_UNITS[String(unit).toUpperCase()] || 1);
}

// Reads podman pull's output (its lines, or its progress bars, either way)
// and keeps one status line: which image, how many of its layers are done,
// and the bytes when podman says them. Nothing is shown until podman starts
// pulling something, so a run with every image already here prints nothing.
// Without a terminal podman says only when each layer starts, so until a
// layer is said to be done the line counts the layers started; the config
// comes once every layer is in.
function pullProgress(ctx) {
  const names = imagesByDigest(ctx.releaseInfo.release);
  let status = null;
  let label = null;
  let layers = new Map(); // id -> {done, got, size}
  let pulled = 0;
  let rest = '';
  const text = () => {
    const ls = [...layers.values()];
    let s = `Downloading ${label}`;
    if (ls.length && !ls.some((l) => l.done || l.size)) {
      s += `: ${ls.length} layer${ls.length === 1 ? '' : 's'} so far`;
    } else if (ls.length) s += `: ${ls.filter((l) => l.done).length} of ${ls.length} layers`;
    const size = ls.reduce((a, l) => a + (l.size || 0), 0);
    if (size) s += `, ${formatBytes(ls.reduce((a, l) => a + (l.done ? l.size || 0 : l.got || 0), 0))} of ${formatBytes(size)}`;
    return s;
  };
  // Keyed by the image, so a log gets one line per image pulled.
  const show = () => {
    if (!status) status = ctx.out.status(text(), label);
    else status.update(text(), label);
  };
  const line = (raw) => {
    // eslint-disable-next-line no-control-regex
    const l = raw.replace(/\x1b\[[0-9;?]*[A-Za-z]/g, '').replace(/\0/g, '').trim();
    if (!l) return;
    const pulling = /Trying to pull (\S+?)(?:\.\.\.)?$/.exec(l);
    if (pulling) {
      const d = /sha256:[0-9a-f]{64}/.exec(pulling[1]);
      label = (d && names.get(d[0])) || pulling[1].replace(/@sha256:.*/, '');
      layers = new Map();
      pulled++;
      show();
      return;
    }
    if (!label) return;
    if (/^Copying config\b/.test(l)) {
      for (const layer of layers.values()) layer.done = true;
      if (layers.size) show();
      return;
    }
    const blob = /Copying blob (?:sha256:)?([0-9a-f]{6,64})/.exec(l);
    if (blob) {
      const id = blob[1].slice(0, 12);
      const layer = layers.get(id) || { done: false, got: 0, size: 0 };
      if (/\b(done|skipped)\b|already exists/i.test(l)) layer.done = true;
      const bytes = /([\d.]+)\s*([KMG]i?B|B)\s*\/\s*([\d.]+)\s*([KMG]i?B|B)/i.exec(l);
      if (bytes) {
        layer.got = sizeBytes(bytes[1], bytes[2]);
        layer.size = sizeBytes(bytes[3], bytes[4]);
      }
      layers.set(id, layer);
      show();
    }
  };
  return {
    feed(chunk) {
      const s = rest + chunk.toString('utf8');
      const parts = s.split(/[\r\n]/);
      rest = parts.pop();
      for (const p of parts) line(p);
    },
    end(ok) {
      if (rest) line(rest);
      rest = '';
      if (!status) return;
      status.end(ok ? `Downloaded ${pulled === 1 ? label : `the Host's ${pulled} images`}.` : undefined);
    },
  };
}

function tail(s, n = 12) {
  return String(s).trim().split('\n').slice(-n).join('\n');
}

async function pull(ctx) {
  const progress = pullProgress(ctx);
  const log = ctx.log || NO_LOG;
  log.section('cseries-guest pull');
  const r = await ctx.driver.guest(ctx, ['pull'], {
    onOutput: (c) => {
      log.write(c);
      progress.feed(c);
    },
  });
  progress.end(r.code === 0);
  if (r.code !== 0) {
    const why = tail(`${r.stderr}\n${r.stdout}`, 6);
    throw new CliError(`The Host's images could not be downloaded${why ? `:\n${why}` : '.'}`,
      `Check this machine's internet connection and run npx cseries again; what was already downloaded is kept. The whole output is in ${ctx.paths.log}.`);
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
    // Before the Host (re)starts: a guest from an older release gets the
    // machine wall here, and every guest this machine's current addresses.
    await machineFirewall.apply(ctx);
    ctx.out.detail('Nothing in the Linux system\'s containers can open a connection to this machine.');
    const quota = cpuQuota(st.cpus);
    const runtime = hasRuntime(st);
    const mem = memoryMax(st.mem_total_kb, { runtime });
    const changed = await configure(ctx, { cpuQuota: quota, memoryMax: mem, door: guestHasDoor(st) });
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

module.exports = { step, configure, configInput, cpuQuota, memoryMax, containersMax, hasRuntime, guestHasDoor, systemReserve, pull, pullProgress, start, guestStatus };
