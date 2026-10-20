#!/usr/bin/env node
'use strict';

// Runs a command in a pseudo-terminal (util-linux `script`), passes its output
// through, and plays the person at the terminal for the one thing the install
// asks of them: when the bastion shows the authenticator key and asks for a
// code, it types the current TOTP code (RFC 6238: SHA-1, 6 digits, 30 s) for
// that key. Exits with the command's exit code.
//
//   node tty-driver.js <command> [args...]

const crypto = require('node:crypto');
const { spawn } = require('node:child_process');

function shq(s) {
  return `'${String(s).replace(/'/g, `'\\''`)}'`;
}

function base32(s) {
  const abc = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
  let bits = '';
  for (const c of s.replace(/=+$/, '')) bits += abc.indexOf(c).toString(2).padStart(5, '0');
  const out = [];
  for (let i = 0; i + 8 <= bits.length; i += 8) out.push(parseInt(bits.slice(i, i + 8), 2));
  return Buffer.from(out);
}

function totp(secret, now = Date.now()) {
  const counter = Buffer.alloc(8);
  counter.writeBigUInt64BE(BigInt(Math.floor(now / 1000 / 30)));
  const h = crypto.createHmac('sha1', base32(secret)).update(counter).digest();
  const o = h[h.length - 1] & 0xf;
  const n = (h.readUInt32BE(o) & 0x7fffffff) % 1000000;
  return String(n).padStart(6, '0');
}

const cmd = process.argv.slice(2).map(shq).join(' ');
const child = spawn('script', ['-q', '-e', '-c', cmd, '/dev/null'], { stdio: ['pipe', 'pipe', 'inherit'] });
let seen = '';
child.stdout.on('data', (d) => {
  process.stdout.write(d);
  seen += d.toString('utf8');
  const key = /enter the key by hand:\s+([A-Z2-7]+)/.exec(seen);
  if (key && /Enter a code from the app to confirm:/.test(seen)) {
    child.stdin.write(`${totp(key[1])}\r`);
    seen = '';
  }
});
child.on('close', (code) => process.exit(code === null ? 1 : code));
