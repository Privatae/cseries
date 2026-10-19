'use strict';

// The Host in the guest: hand the guest the two images this release pins (by
// digest), let it pull them, and start or restart its units.

const { CliError } = require('./errors');
const { pinnedImage } = require('./release');

function configInput(release) {
  return `host=${pinnedImage(release, 'host')}\npostgres=${pinnedImage(release, 'postgres')}\nrelease=${release.version}\n`;
}

// -> true when the guest's images changed (a new release).
async function configure(ctx) {
  const r = await ctx.driver.guest(ctx, ['configure'], { input: configInput(ctx.releaseInfo.release) });
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
    const changed = await configure(ctx);
    await pull(ctx);
    await start(ctx, changed);
    if (changed) ctx.out.detail(`The Host runs release ${rel.version}.`);
    await require('./setup').waitHealthy(ctx);
  },
};

module.exports = { step, configure, configInput, pull, start, guestStatus };
