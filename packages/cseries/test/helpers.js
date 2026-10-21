'use strict';

// Test doubles: a runner that answers from a handler instead of starting
// programs, an Output that records what was printed, and the decoder for the
// PowerShell scripts the Windows code passes as -EncodedCommand.

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { Writable } = require('node:stream');
const { Output } = require('../src/output');

function toBuf(v) {
  if (v === undefined || v === null) return Buffer.alloc(0);
  return Buffer.isBuffer(v) ? v : Buffer.from(String(v));
}

// handler(cmd, args, opts) -> {code, stdout, stderr, notFound, chunks} | undefined
// chunks: the output as it would arrive, piece by piece, for opts.onOutput;
// without it onOutput gets stdout and then stderr, as the real runner would.
function fakeRunner(handler = () => ({})) {
  const calls = [];
  return {
    calls,
    async run(cmd, args, opts = {}) {
      calls.push({ kind: 'run', cmd, args, input: opts.input, env: opts.env, stdin: opts.stdin });
      const h = (await handler(cmd, args, opts)) || {};
      if (opts.onOutput) {
        for (const c of h.chunks || [h.stdout, h.stderr]) {
          if (c !== undefined && c !== null && String(c) !== '') opts.onOutput(toBuf(c));
        }
      }
      return { code: h.code === undefined ? 0 : h.code, stdout: toBuf(h.stdout), stderr: toBuf(h.stderr), notFound: Boolean(h.notFound) };
    },
    async inherit(cmd, args, opts = {}) {
      calls.push({ kind: 'inherit', cmd, args, env: opts.env });
      const h = (await handler(cmd, args, { ...opts, inherit: true })) || {};
      return { code: h.code === undefined ? 0 : h.code, notFound: Boolean(h.notFound) };
    },
    detached(cmd, args, opts = {}) {
      calls.push({ kind: 'detached', cmd, args, opts });
    },
  };
}

function captureOutput() {
  const chunks = [];
  const errs = [];
  const stream = new Writable({ write(c, _e, cb) { chunks.push(String(c)); cb(); } });
  const errStream = new Writable({ write(c, _e, cb) { errs.push(String(c)); cb(); } });
  const out = new Output(stream, errStream);
  out.text = () => chunks.join('');
  out.errText = () => errs.join('');
  return out;
}

// The script inside powershell.exe ... -EncodedCommand <b64>.
function psScript(args) {
  const i = args.indexOf('-EncodedCommand');
  if (i < 0) return null;
  return Buffer.from(args[i + 1], 'base64').toString('utf16le');
}

function fixture(name) {
  return fs.readFileSync(path.join(__dirname, 'fixtures', name));
}

function tmpdir(prefix = 'cseries-test-') {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix));
}

// The paths platform.js gives a Windows machine, all under one temp dir so a
// test touches nothing else: base (the person's %LOCALAPPDATA%\cseries),
// the old per-user bastion folder, and the service's Program Files folder.
function testPaths(dir = tmpdir()) {
  return {
    base: dir,
    state: path.join(dir, 'state.json'),
    downloads: path.join(dir, 'downloads'),
    wsl: path.join(dir, 'wsl'),
    wslConfig: path.join(dir, 'home', '.wslconfig'),
    hostCa: path.join(dir, 'host-ca.pem'),
    installLog: path.join(dir, 'bastion-install.log'),
    log: path.join(dir, 'install.log'),
    bastionDir: path.join(dir, 'b'),
    bastionExe: path.join(dir, 'b', 'privatae-bastion.exe'),
    serviceDir: path.join(dir, 'pf', 'Privatae'),
    serviceExe: path.join(dir, 'pf', 'Privatae', 'privatae-bastion.exe'),
    join: path.join,
  };
}

function validRelease(overrides = {}) {
  const hex = (c) => c.repeat(64);
  return {
    version: '1.2.3',
    images: {
      host: { ref: 'ghcr.io/privatae/cseries:1.2.3', digest: `sha256:${hex('a')}` },
      postgres: { ref: 'ghcr.io/privatae/cseries-postgres:1.2.3', digest: `sha256:${hex('b')}` },
    },
    guest: {
      'wsl-x64': { url: 'https://github.com/Privatae/cseries/releases/download/v1.2.3/cseries-wsl-rootfs-1.2.3-x64.tar.gz', sha256: hex('c') },
      'wsl-arm64': { url: 'https://github.com/Privatae/cseries/releases/download/v1.2.3/cseries-wsl-rootfs-1.2.3-arm64.tar.gz', sha256: hex('d') },
      'lima-x64': { url: 'https://github.com/Privatae/cseries/releases/download/v1.2.3/cseries-guest-1.2.3-x64.qcow2', sha256: hex('e') },
      'lima-arm64': { url: 'https://github.com/Privatae/cseries/releases/download/v1.2.3/cseries-guest-1.2.3-arm64.qcow2', sha256: hex('f') },
    },
    bastion: { min_version: '0.53.2' },
    ...overrides,
  };
}

module.exports = { fakeRunner, captureOutput, psScript, fixture, tmpdir, testPaths, validRelease, toBuf };
