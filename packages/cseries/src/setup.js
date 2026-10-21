'use strict';

// The last step: wait until the Host answers, find out whether the owner
// account exists yet, and if not, read the setup code from the Host's log.
//
//   GET /api/system/health   200 when the Host is up
//   GET /setup               200 is the setup page (no owner yet);
//                            a redirect to /login means the owner exists
//   CSERIES SETUP CODE: ...  printed at every boot until the owner exists;
//                            the newest one in the log is the live one

const { CliError } = require('./errors');

const BASE = 'http://127.0.0.1:8800';
const NAMED = 'http://home.cseries.localhost:8800';
const CODE = /CSERIES SETUP CODE: ([0-9a-f]{6}-[0-9a-f]{6}-[0-9a-f]{6})/g;

function newestSetupCode(text) {
  let last = null;
  let m;
  CODE.lastIndex = 0;
  while ((m = CODE.exec(String(text)))) last = m[1];
  return last;
}

// -> 'needed' | 'done' | 'unknown'
function setupState(probe) {
  if (!probe) return 'unknown';
  if (probe.status === 200) return 'needed';
  if (probe.status >= 300 && probe.status < 400 && /(^|\/)login(\?|$)/.test(probe.location)) return 'done';
  return 'unknown';
}

async function waitHealthy(ctx, { timeoutMs = 5 * 60 * 1000, intervalMs = 3000 } = {}) {
  const deadline = Date.now() + timeoutMs;
  let waited = false;
  for (;;) {
    const p = await ctx.net.probe(`${BASE}/api/system/health`);
    if (p && p.status === 200) {
      if (waited) ctx.out.line();
      return;
    }
    if (Date.now() >= deadline) break;
    if (!waited) ctx.out.stream.write('      Waiting for the Host to answer');
    waited = true;
    ctx.out.dot();
    await ctx.sleep(intervalMs);
  }
  // Nothing on this machine's 127.0.0.1:8800. Ask inside the VM whether the
  // Host is up there, which tells a stopped Host from a broken forward.
  const inside = await ctx.driver.guest(ctx, ['health']);
  if (inside.code === 0) {
    throw new CliError('The Host is running inside the VM, but this machine cannot reach it on 127.0.0.1:8800.',
      ctx.driver.unreachableHint(ctx));
  }
  const logs = await ctx.driver.guest(ctx, ['logs', 'host', '--lines', '40']);
  throw new CliError(
    `The Host did not answer within ${Math.round(timeoutMs / 60000)} minutes. The end of its log:\n${logs.stdout.trimEnd()}`,
    'Run npx cseries again. If it fails the same way, "npx cseries logs host" shows the whole log.');
}

async function openBrowser(ctx, url) {
  if (ctx.platform === 'win32') {
    ctx.runner.detached('cmd.exe', ['/c', 'start', '""', `"${url}"`], { verbatim: true });
  } else if (ctx.platform === 'darwin') {
    ctx.runner.detached('open', [url]);
  } else if (ctx.env.DISPLAY || ctx.env.WAYLAND_DISPLAY) {
    ctx.runner.detached('xdg-open', [url]);
  }
}

async function copyToClipboard(ctx, text) {
  if (ctx.platform !== 'win32') return false;
  const r = await ctx.runner.run('clip.exe', [], { input: text, timeoutMs: 5000 });
  return r.code === 0;
}

async function showSetup(ctx, { open = true } = {}) {
  const out = ctx.out;
  const state = setupState(await ctx.net.probe(`${BASE}/setup`));
  let code = null;
  if (state === 'needed') {
    const r = await ctx.driver.guest(ctx, ['host-log']);
    code = newestSetupCode(`${r.stdout}\n${r.stderr}`);
  }
  const url = state === 'done' ? NAMED : `${NAMED}/setup`;
  out.line();
  out.line('C-Series is running.');
  out.line(`  Open:        ${url}`);
  // The name is one some Safari versions cannot open; elsewhere the address
  // is only the fallback.
  const why = ctx.platform === 'darwin' ? 'when the browser cannot open the name; some Safari versions' : 'when the browser cannot open the name';
  out.line(`  or           ${BASE}   (${why})`);
  if (code) {
    out.line(`  Setup code:  ${code}`);
    if (await copyToClipboard(ctx, code)) out.line('               (copied to the clipboard)');
    out.line();
    out.line('Enter the code on that page to create the owner account.');
  } else if (state === 'needed') {
    out.line();
    out.line('The setup code is in the Host\'s log: npx cseries logs host');
  }
  if (open) await openBrowser(ctx, url);
  return { state, code, url };
}

module.exports = { waitHealthy, showSetup, newestSetupCode, setupState, openBrowser, BASE, NAMED };
