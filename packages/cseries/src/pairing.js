'use strict';

// The pairing token that carries the bastion on the machine to the Host in the
// VM, as the design's section 3 sets it out:
//
//   1. `cseries-guest pair-token` runs `python3 -m cli.cseries bastion
//      pair-token` in the Host's container, through the VM's exec channel (no
//      network route can mint a token), and prints
//      {"token", "host_ca_pem", "enroll"};
//   2. the elevated step (bastion.js installStep) copies the Host CA to where
//      only an administrator can write, checks it, and runs
//      `pair --redeem-url <enroll> --host-ca <copy> <token>`, which redeems
//      the token over TLS checked against that CA alone.
//
// A Host whose data was deleted has a new CA. It is not rotated with
// `pair --replace` (the bastion refuses that across a CA change); the old
// anchor is released and the new Host paired fresh.

const { CliError } = require('./errors');
const { sha256Hex } = require('./verify');

const LOOPBACK = new Set(['127.0.0.1', 'localhost', '[::1]']);

function parsePairToken(text) {
  let j;
  try {
    j = JSON.parse(String(text).trim());
  } catch {
    throw new Error('the Host did not answer with JSON');
  }
  const token = j && j.token;
  const ca = j && j.host_ca_pem;
  if (typeof token !== 'string' || !token || /\s/.test(token)) throw new Error('the answer holds no token');
  if (typeof ca !== 'string' || !/^-----BEGIN CERTIFICATE-----[\s\S]+-----END CERTIFICATE-----$/.test(ca.trim())) {
    throw new Error('the answer holds no Host CA certificate');
  }
  let u;
  try {
    u = new URL(j.enroll);
  } catch {
    throw new Error('the answer holds no enroll address');
  }
  // The token goes only to this machine's own loopback, over TLS.
  if (u.protocol !== 'https:' || !LOOPBACK.has(u.hostname)) {
    throw new Error(`the enroll address ${j.enroll} is not https on this machine's loopback`);
  }
  const pem = `${ca.trim()}\n`;
  return { token, hostCaPem: pem, enroll: u.toString(), caSha256: sha256Hex(Buffer.from(pem)) };
}

async function mintToken(ctx) {
  const r = await ctx.driver.guest(ctx, ['pair-token']);
  if (r.code !== 0) {
    const why = `${r.stderr}\n${r.stdout}`.trim().split('\n').slice(-3).join(' ');
    throw new CliError(`The Host could not make a pairing token: ${why || `exit ${r.code}`}`,
      'The Host works without the bastion meanwhile. Run npx cseries again; if it fails the same way, this Host release cannot pair a bastion on this machine yet.');
  }
  try {
    return parsePairToken(r.stdout);
  } catch (err) {
    throw new CliError(`The Host's pairing token could not be read: ${err.message}.`, '');
  }
}

module.exports = { parsePairToken, mintToken };
