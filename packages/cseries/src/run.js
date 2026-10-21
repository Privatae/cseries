'use strict';

// Running other programs. Every command this package runs goes through a
// runner with these three methods, so tests replace it with one that answers
// from recorded output, and nothing here goes through a shell.
//
//   run(cmd, args, opts)     -> {code, stdout: Buffer, stderr: Buffer, notFound}
//   inherit(cmd, args, opts) -> {code, notFound}; output goes to this terminal
//   detached(cmd, args, {verbatim}) -> starts it in the background, returns at once
//
// opts: {input: string|Buffer, env: {...}, timeoutMs, onOutput, stdin}.
// onOutput(chunk) is handed each piece of stdout and stderr as it arrives
// (for the install log and a step's status line); stdin: 'inherit' gives the
// program this terminal's input, for sudo, which asks for the password on the
// terminal itself while its output is captured. verbatim passes the
// arguments to a Windows program as written, unquoted (for cmd.exe's start).

const { spawn } = require('node:child_process');

function systemRunner() {
  return {
    run(cmd, args, opts = {}) {
      return new Promise((resolve) => {
        let child;
        try {
          child = spawn(cmd, args, {
            env: opts.env ? { ...process.env, ...opts.env } : process.env,
            windowsHide: true,
            stdio: [opts.stdin === 'inherit' ? 'inherit' : 'pipe', 'pipe', 'pipe'],
          });
        } catch (err) {
          resolve({ code: -1, stdout: Buffer.alloc(0), stderr: Buffer.from(String(err)), notFound: err.code === 'ENOENT' });
          return;
        }
        const out = [];
        const errs = [];
        let timer = null;
        if (opts.timeoutMs) {
          timer = setTimeout(() => child.kill(), opts.timeoutMs);
        }
        const seen = (d) => {
          if (opts.onOutput) {
            try { opts.onOutput(d); } catch { /* a status line never stops the program */ }
          }
        };
        child.stdout.on('data', (d) => { out.push(d); seen(d); });
        child.stderr.on('data', (d) => { errs.push(d); seen(d); });
        child.on('error', (err) => {
          if (timer) clearTimeout(timer);
          resolve({ code: -1, stdout: Buffer.concat(out), stderr: Buffer.from(String(err.message)), notFound: err.code === 'ENOENT' });
        });
        child.on('close', (code) => {
          if (timer) clearTimeout(timer);
          resolve({ code: code === null ? -1 : code, stdout: Buffer.concat(out), stderr: Buffer.concat(errs), notFound: false });
        });
        if (child.stdin) {
          child.stdin.on('error', () => {});
          if (opts.input !== undefined) child.stdin.end(opts.input);
          else child.stdin.end();
        }
      });
    },

    inherit(cmd, args, opts = {}) {
      return new Promise((resolve) => {
        const child = spawn(cmd, args, {
          env: opts.env ? { ...process.env, ...opts.env } : process.env,
          stdio: ['inherit', 'inherit', 'inherit'],
        });
        child.on('error', (err) => resolve({ code: -1, notFound: err.code === 'ENOENT' }));
        child.on('close', (code) => resolve({ code: code === null ? -1 : code, notFound: false }));
      });
    },

    detached(cmd, args, opts = {}) {
      const child = spawn(cmd, args, {
        detached: true,
        windowsHide: true,
        stdio: 'ignore',
        windowsVerbatimArguments: Boolean(opts.verbatim),
      });
      child.on('error', () => {});
      child.unref();
    },
  };
}

module.exports = { systemRunner };
