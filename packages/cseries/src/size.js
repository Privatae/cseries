'use strict';

// How big the Host is: how many of this machine's processors and how much of
// its memory the C-Series Linux system may use. The person picks it, by the
// names the Cloud machines have ("C4-16" is 4 processors and 16 GB of
// memory), when C-Series is installed, and changes it later with
// `npx cseries resize`, which keeps every bit of the Host's data.
//
//   - The sizes offered are the ones that fit this machine while leaving it
//     at least MIN_LEFT (2 processors and 4 GB), or a quarter of each when
//     that is more, plus "most of this machine": everything but that.
//   - The size suggested, and taken when nobody is at a terminal to ask, is
//     the smallest that holds what the Host needs (NEED_GIB, below), never
//     more than half of this machine's memory; a bigger one
//     only when the person chooses it.
//   - On Windows WSL gives all its Linux systems together at most what the
//     person's own .wslconfig says, or WSL's default (half the PC's memory,
//     every processor). That file is the person's, shared by every WSL
//     system, so no size above it is offered; the run says which line raises
//     it (wsl.js sizeLimit).
//
// The size is recorded in state.json ("size": {cpus, memoryGiB}). On macOS
// and Linux it is the Lima VM's own size (lima.js); on Windows it is the
// ceiling on C-Series's units inside its own WSL system (size-caps.js), so
// the size never caps any other WSL system on the PC.

const { CliError } = require('./errors');

const GiB = 1024 ** 3;

// The sizes, by the Cloud's names (README.md: C4-8, C6-12, C8-24) and the
// steps between them.
const SIZES = [[2, 4], [4, 8], [4, 16], [6, 12], [8, 24], [8, 32], [12, 48], [16, 64]]
  .map(([cpus, memoryGiB]) => ({ cpus, memoryGiB }));

// What the machine keeps for itself whatever size is chosen: these, or
// LEFT_SHARE of what it has when that is more.
const MIN_LEFT = { cpus: 2, memoryGiB: 4 };
const LEFT_SHARE = 0.25;

// The least a size may be given as exact values (--cpus/--memory): the
// smallest VM Lima and WSL boot the guest in. Under HOST_NEEDS_GIB the run
// says the Host is short.
const MIN_SIZE = { cpus: 1, memoryGiB: 2 };

// What the Host needs, measured (host.js, the Host's memory ceiling and why):
//   - HOST_NEEDS_GIB, 4 GB: the Host and its database with the Linux
//     system's own reserve. Inside a 4 GB system that is about 1.1 GB for the
//     system (GKE's reserve), 512 MB for the database and 1 GB for the Host
//     (the busiest live Host's largest working set was 901 MB), leaving
//     1436 MB: one coding engine (1 GB, cseries_runtime/common.py
//     HARNESS_MEM_MI) or the browser (1 GB, BROWSER_MEM_MI), not both.
//   - NEED_GIB, 5 GB: that and one coding engine beside the browser it
//     drives, so the size suggested is the smallest at or above it: C4-8,
//     where the containers get 4302 MB (an engine, the browser and the
//     apps' connectors together), and the Cloud's smallest machine too.
const HOST_NEEDS_GIB = 4;
const NEED_GIB = HOST_NEEDS_GIB + 1;

function sizeName(s) {
  return `C${s.cpus}-${s.memoryGiB}`;
}

function describe(s) {
  return `${s.cpus} processor${s.cpus === 1 ? '' : 's'} and ${s.memoryGiB} GB of memory`;
}

// "C4-16" (any case) -> {cpus, memoryGiB}, or null.
function parseSize(text) {
  const m = /^c([1-9][0-9]{0,2})-([1-9][0-9]{0,4})$/i.exec(String(text || '').trim());
  return m ? { cpus: Number(m[1]), memoryGiB: Number(m[2]) } : null;
}

// This machine: its memory as it is sold, in GB, and its logical
// processors. The OS sees a little less than what is installed (the firmware,
// the graphics and the kernel keep some: a "16 GB" PC shows about 15.6 GiB),
// so the whole GB above what it sees is what the machine has: the reserve and
// the sizes are reckoned from that, as a person reckons them.
function machineOf(totalBytes, cpus) {
  return { memoryGiB: Math.max(1, Math.ceil(totalBytes / GiB - 0.01)), cpus: Math.max(1, Math.floor(cpus) || 1) };
}

function reserveOf(m) {
  return {
    cpus: Math.max(MIN_LEFT.cpus, Math.ceil(m.cpus * LEFT_SHARE)),
    memoryGiB: Math.max(MIN_LEFT.memoryGiB, m.memoryGiB * LEFT_SHARE),
  };
}

// The biggest size this machine gives the Host ("most of this machine"),
// within `limit` ({cpus, memoryGiB}: what WSL gives its systems) when there
// is one. Never under MIN_SIZE: a machine too small to leave its reserve
// still gets the smallest Host that boots, and the run says so (short).
function most(m, limit = null) {
  const r = reserveOf(m);
  let cpus = m.cpus - r.cpus;
  let memoryGiB = Math.floor(m.memoryGiB - r.memoryGiB);
  if (limit) {
    cpus = Math.min(cpus, limit.cpus);
    memoryGiB = Math.min(memoryGiB, Math.floor(limit.memoryGiB));
  }
  const short = cpus < MIN_SIZE.cpus || memoryGiB < HOST_NEEDS_GIB;
  return {
    cpus: Math.max(MIN_SIZE.cpus, cpus),
    memoryGiB: Math.max(MIN_SIZE.memoryGiB, memoryGiB),
    short,
  };
}

function fits(s, top) {
  return s.cpus <= top.cpus && s.memoryGiB <= top.memoryGiB;
}

// -> [{cpus, memoryGiB, name, most?}]: the named sizes that fit, then "most
// of this machine" (marked most), unless it is one of them already, which is
// then marked instead.
function choices(m, limit = null) {
  const top = most(m, limit);
  const list = SIZES.filter((s) => fits(s, top)).map((s) => ({ ...s, name: sizeName(s) }));
  const same = list.find((s) => s.cpus === top.cpus && s.memoryGiB === top.memoryGiB);
  if (same) same.most = true;
  else list.push({ cpus: top.cpus, memoryGiB: top.memoryGiB, name: sizeName(top), most: true });
  return list;
}

// The size suggested: the smallest named size that fits, holds NEED_GIB and
// takes no more than half of this machine's memory; else the
// biggest that fits within that half; else the smallest that fits at all;
// else most of this machine.
function suggested(m, limit = null) {
  const list = choices(m, limit);
  const named = list.filter((s) => SIZES.some((n) => n.cpus === s.cpus && n.memoryGiB === s.memoryGiB));
  // Half the memory: what is left to the machine is what its owner feels.
  // Processors are shared by time, so only the reserve bounds them.
  const half = named.filter((s) => s.memoryGiB <= m.memoryGiB / 2);
  const pick = half.find((s) => s.memoryGiB >= NEED_GIB) || half[half.length - 1] || named[0] || list[list.length - 1];
  return { cpus: pick.cpus, memoryGiB: pick.memoryGiB };
}

// The size the arguments ask for, or null when they name none:
//   --size C4-16 | --size=C4-16 | --size most
//   --cpus N --memory G   (G in GB: 16 or 16GB)
//   a bare size or "most" (resize C8-32): positional
function fromArgs(args, { positional = false } = {}) {
  const value = (flag) => {
    const eq = args.find((a) => a.startsWith(`${flag}=`));
    if (eq) return eq.slice(flag.length + 1);
    const i = args.indexOf(flag);
    if (i < 0) return undefined;
    const v = args[i + 1];
    if (v === undefined || v.startsWith('--')) throw new CliError(`${flag} needs a value.`, USAGE);
    return v;
  };
  const size = value('--size');
  const cpus = value('--cpus');
  const memory = value('--memory');
  let bare;
  if (positional) {
    const taken = new Set();
    for (const f of ['--size', '--cpus', '--memory']) {
      const i = args.indexOf(f);
      if (i >= 0) taken.add(i + 1);
    }
    bare = args.find((a, i) => !a.startsWith('--') && !taken.has(i));
  }
  const named = size !== undefined ? size : bare;
  if (named !== undefined && (cpus !== undefined || memory !== undefined)) {
    throw new CliError('Give either a size (C4-16) or --cpus and --memory, not both.', USAGE);
  }
  if (named !== undefined) {
    if (/^most$/i.test(named.trim())) return { most: true };
    const s = parseSize(named);
    if (!s) throw new CliError(`"${named}" is not a size. A size is written like C4-16: 4 processors and 16 GB of memory.`, USAGE);
    return s;
  }
  if (cpus === undefined && memory === undefined) return null;
  if (cpus === undefined || memory === undefined) {
    throw new CliError('--cpus and --memory go together: how many processors, and how many GB of memory.', USAGE);
  }
  if (!/^[1-9][0-9]{0,2}$/.test(cpus.trim())) throw new CliError(`--cpus ${cpus} is not a number of processors.`, USAGE);
  const mem = /^([1-9][0-9]{0,4})\s*(?:G|GB|GiB)?$/i.exec(memory.trim());
  if (!mem) throw new CliError(`--memory ${memory} is not a number of GB.`, USAGE);
  return { cpus: Number(cpus), memoryGiB: Number(mem[1]) };
}

const USAGE = 'For example: npx cseries --size C4-8, npx cseries resize C8-32, or --cpus 6 --memory 12.';

// What the sizes that fit look like, one line each, the suggested one marked.
function menu(m, limit, pick) {
  const list = choices(m, limit);
  const width = Math.max(...list.map((s) => s.name.length));
  return list.map((s, i) => {
    let line = `  ${String(i + 1).padStart(2)}) ${s.name.padEnd(width)}  ${describe(s)}`;
    if (s.most) line += ', most of this machine';
    if (s.cpus === pick.cpus && s.memoryGiB === pick.memoryGiB) line += '   (suggested)';
    return line;
  });
}

// The size `want` (fromArgs) resolves to on this machine: "most" made exact,
// and one that does not fit refused, with the sizes that do.
function resolve(want, m, limit = null, { limitWhy = '' } = {}) {
  const top = most(m, limit);
  if (want.most) return { cpus: top.cpus, memoryGiB: top.memoryGiB };
  if (want.cpus < MIN_SIZE.cpus || want.memoryGiB < MIN_SIZE.memoryGiB) {
    throw new CliError(`${sizeName(want)} is too small: the Host needs at least ${MIN_SIZE.memoryGiB} GB of memory.`, sizesText(m, limit));
  }
  if (!fits(want, top)) {
    const r = reserveOf(m);
    const keep = `this machine keeps ${r.cpus} processors and ${Math.ceil(r.memoryGiB)} GB for itself`;
    // Within this machine's own reserve, but not within what WSL gives: say
    // that, since it is what raising the person's own limit would change.
    const why = limit && limitWhy && fits(want, most(m)) ? limitWhy : keep;
    throw new CliError(`${sizeName(want)} does not fit here: ${why}, so the biggest the Host can be is ${sizeName(top)}.`, sizesText(m, limit));
  }
  return { cpus: want.cpus, memoryGiB: want.memoryGiB };
}

function sizesText(m, limit) {
  return `The sizes that fit: ${choices(m, limit).map((s) => s.name).join(', ')} (or "most" for ${sizeName(most(m, limit))}).`;
}

// Asks at the terminal (ctx.ask: the line typed, or null with no terminal)
// which size, the suggested one taken on Enter. -> {cpus, memoryGiB}, or
// null when there is no terminal to ask at.
async function ask(ctx, m, limit, pick, { intro } = {}) {
  if (!ctx.ask) return null;
  const out = ctx.out;
  const list = choices(m, limit);
  let shown = false;
  for (let tries = 0; tries < 5; tries++) {
    if (!shown) {
      out.line();
      for (const l of intro) out.line(l);
      for (const l of menu(m, limit, pick)) out.line(l);
      shown = true;
    }
    const answer = await ctx.ask(`Type its number or a size like C4-8, or press Enter for ${sizeName(pick)}: `);
    if (answer === null || answer === undefined) return tries === 0 ? null : pick;
    const a = String(answer).trim();
    if (a === '') return pick;
    if (/^[0-9]+$/.test(a) && Number(a) >= 1 && Number(a) <= list.length) {
      const s = list[Number(a) - 1];
      return { cpus: s.cpus, memoryGiB: s.memoryGiB };
    }
    if (/^most$/i.test(a)) return resolve({ most: true }, m, limit);
    const s = parseSize(a);
    if (s) {
      try {
        return resolve(s, m, limit);
      } catch (e) {
        if (!(e instanceof CliError)) throw e;
        out.line(e.message);
        continue;
      }
    }
    out.line(`"${a}" is not one of them. ${USAGE}`);
  }
  return pick;
}

// The words the run says about a size that leaves the Host short.
function warnings(s) {
  const w = [];
  if (s.memoryGiB < HOST_NEEDS_GIB) {
    w.push(`${sizeName(s)} gives the Host ${s.memoryGiB} GB of memory; it works best with ${HOST_NEEDS_GIB} GB or more, and with ${NEED_GIB} GB or more a coding engine and the browser fit beside it.`);
  }
  return w;
}

// state.json "size": {cpus, memoryGiB}, the size chosen.
const KEY = 'size';

function recorded(ctx) {
  const s = ctx.state && ctx.state.get(KEY);
  if (!s || !Number.isInteger(s.cpus) || !Number.isInteger(s.memoryGiB) || s.cpus < 1 || s.memoryGiB < 1) return null;
  return { cpus: s.cpus, memoryGiB: s.memoryGiB };
}

function record(ctx, s) {
  ctx.state.set(KEY, { cpus: s.cpus, memoryGiB: s.memoryGiB });
}

function same(a, b) {
  return Boolean(a && b) && a.cpus === b.cpus && a.memoryGiB === b.memoryGiB;
}

// This run's machine (cli.js makeContext ctx.machine) and what its VM
// software lets C-Series have (wsl.js sizeLimit; none for Lima).
// -> {m, limit, limitWhy}
async function where(ctx) {
  const m = machineOf(ctx.machine.totalMem, ctx.machine.cpus);
  const l = ctx.driver && ctx.driver.sizeLimit ? await ctx.driver.sizeLimit(ctx) : null;
  return { m, limit: l ? { cpus: l.cpus, memoryGiB: l.memoryGiB } : null, limitWhy: l ? l.why : '', limitNote: l ? l.note : '' };
}

const INTRO = [
  'How big should the Host be? It runs in a Linux system of its own, which may use',
  'this many of this machine\'s processors and this much of its memory; the rest stays',
  'this machine\'s. It can be changed later, keeping everything: npx cseries resize',
];

// The size for a new install, or a resize: the one the arguments name, or
// the one chosen at the terminal, or with no terminal the suggested one.
// -> {size, asked}
async function choose(ctx, args, { positional = false } = {}) {
  const want = fromArgs(args, { positional });
  const { m, limit, limitWhy, limitNote } = await where(ctx);
  const pick = suggested(m, limit);
  if (want) return { size: resolve(want, m, limit, { limitWhy }), asked: false };
  const intro = limitNote ? [...INTRO, limitNote] : INTRO;
  const s = await ask(ctx, m, limit, pick, { intro });
  if (s) return { size: s, asked: true };
  return { size: pick, asked: false, suggested: true };
}

// The install's step: before the VM is made. A size recorded stands (a
// resize changes it); an install from before sizes were chosen keeps the
// size its VM has, recorded when the driver can read it (lima.js
// currentSize).
const step = {
  title: 'Choosing how big the Host is',
  async run(ctx) {
    const out = ctx.out;
    const args = ctx.args || [];
    const want = fromArgs(args);
    const have = recorded(ctx);
    const resizeText = (s) => `To change it, keeping everything: npx cseries resize${s ? ` ${s}` : ''}`;
    if (have) {
      if (want && !want.most && !same(want, have)) {
        throw new CliError(`This Host is ${sizeName(have)} already, and an install's --size does not change it.`, resizeText(sizeName(want)));
      }
      out.detail(`It is ${sizeName(have)}: ${describe(have)}. ${resizeText()}`);
      return;
    }
    if (ctx.move || await ctx.driver.guestExists(ctx)) {
      if (want) throw new CliError('This Host is installed already, and an install\'s --size does not change it.', resizeText(want.most ? 'most' : sizeName(want)));
      const cur = !ctx.move && ctx.driver.currentSize ? await ctx.driver.currentSize(ctx) : null;
      if (cur) {
        record(ctx, cur);
        out.detail(`It is ${sizeName(cur)}, ${describe(cur)}, as an earlier release made it. ${resizeText()}`);
      } else if (ctx.driver.name === 'wsl') {
        out.detail('It may use all the memory and processors WSL gives its Linux systems, as an earlier release set it up.');
        out.detail(`To choose a size for it instead: npx cseries resize`);
      } else {
        out.detail(`It keeps the size its VM has. ${resizeText()}`);
      }
      return;
    }
    const { size: s, asked, suggested: took } = await choose(ctx, args);
    if (took) {
      out.detail(`Nobody is at a terminal to ask, so the Host gets the suggested size, ${sizeName(s)}.`);
      out.detail('An install can name another: npx cseries --size C4-8 (or --cpus and --memory).');
    } else if (asked) {
      out.line();
    }
    record(ctx, s);
    out.detail(`The Host is ${sizeName(s)}: ${describe(s)}. ${resizeText()}`);
    for (const w of warnings(s)) out.warn(w);
  },
};

module.exports = {
  SIZES, MIN_LEFT, LEFT_SHARE, MIN_SIZE, HOST_NEEDS_GIB, NEED_GIB, GiB, USAGE, KEY,
  sizeName, describe, parseSize, machineOf, reserveOf, most, choices, suggested, fromArgs, resolve, menu, ask, warnings, sizesText,
  recorded, record, same, where, choose, step,
};
