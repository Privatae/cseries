#!/usr/bin/env node
'use strict';

// Runs `npx cseries` on a Linux machine for real, through the Lima driver:
// Lima downloaded from its release and checked against the pinned sha256,
// QEMU installed with sudo, the guest image made into a Lima VM under KVM, the
// Host started from its units, the bastion installed as the systemd service,
// paired over 127.0.0.1:8810 and anchored, and the authenticator enrolled.
// Then the same again, which must ask for nothing and change nothing.
//
//   node e2e-lima.js <packages/cseries> <guest.qcow2> <bastion binary> \
//        <images.tar> <host image> <postgres image>
//
// Three things are local because nothing is published yet, and only these:
// the guest image and the bastion are served from files (each still checked
// against its sha256), the bastion's manifest is signed with a key made for
// this run, and the two images are loaded into the guest from a `docker save`
// tarball before the Host step and pinned by the digests podman gives them
// there, so the Host step finds them and pulls nothing.
// Everything else is the command's own code, on this machine.

const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

const [pkg, qcow2, bastionBin, imagesTar, hostImage, pgImage] = process.argv.slice(2);
if (!pgImage) {
  process.stderr.write('usage: e2e-lima.js <packages/cseries> <guest.qcow2> <bastion> <images.tar> <host image> <postgres image>\n');
  process.exit(2);
}
const { makeContext, upSteps } = require(`${pkg}/src/cli`);
const { runSteps } = require(`${pkg}/src/steps`);
const realNet = require(`${pkg}/src/net`);
const { statement, sha256Hex } = require(`${pkg}/src/verify`);
const lima = require(`${pkg}/src/vm/lima`);

const GUEST_URL = 'https://downloads.invalid/cseries-guest-e2e-x64.qcow2';
const BASTION_URL = 'https://downloads.invalid/privatae-bastion-linux-amd64';

const guestSha = sha256Hex(fs.readFileSync(qcow2));
const bastionBytes = fs.readFileSync(bastionBin);
// The manifest names the binary's own version, and the run asks for the
// release's own floor (release.json): a hand-written version here let a
// bastion older than the floor pass the install and fail at pairing.
const bastionVersion = (/^privatae-bastion ([0-9]+\.[0-9]+\.[0-9]+)\s*$/.exec(
  require('node:child_process').execFileSync(bastionBin, ['version'], { encoding: 'utf8' })) || [])[1];
if (!bastionVersion) throw new Error(`${bastionBin} version: not a privatae-bastion X.Y.Z`);
const minVersion = JSON.parse(fs.readFileSync(`${pkg}/release.json`, 'utf8')).bastion.min_version;
const { publicKey, privateKey } = crypto.generateKeyPairSync('ed25519');
const manifest = Buffer.from(JSON.stringify({
  version: bastionVersion,
  binaries: { 'linux-amd64': { url: BASTION_URL, sha256: sha256Hex(bastionBytes) } },
}, null, 2));
const sig = crypto.sign(null, statement('bastion', bastionVersion, sha256Hex(manifest)), privateKey).toString('base64');
const bastionKeys = [publicKey.export({ format: 'der', type: 'spki' }).subarray(12).toString('base64')];

const local = { [GUEST_URL]: qcow2, [BASTION_URL]: bastionBin };
const net = {
  ...realNet,
  async fetchSmall(url, opts) {
    if (url === 'https://privatae.ai/bastion/version.json') return manifest;
    if (url.startsWith('https://privatae.ai/bastion/version.json.')) return Buffer.from(sig);
    return realNet.fetchSmall(url, opts);
  },
  async download(url, dest, opts) {
    if (!local[url]) return realNet.download(url, dest, opts);
    fs.mkdirSync(path.dirname(dest), { recursive: true });
    fs.copyFileSync(local[url], `${dest}.part`);
    if ((await realNet.fileSha256(`${dest}.part`)) !== opts.sha256) throw new Error(`${url}: sha256 mismatch`);
    fs.renameSync(`${dest}.part`, dest);
    return { downloaded: true };
  },
};

const release = {
  version: '0.0.0-e2e',
  // The digests are podman's, read once the images are loaded.
  images: { host: { ref: hostImage.replace(/:[^:/]+$/, ''), digest: null }, postgres: { ref: pgImage.replace(/:[^:/]+$/, ''), digest: null } },
  guest: { 'lima-x64': { url: GUEST_URL, sha256: guestSha } },
  bastion: { min_version: minVersion },
};

const loadImages = {
  title: 'Loading the test images into the guest (nothing is published yet)',
  async run(ctx) {
    const r = await ctx.runner.run(lima.limactlPath(ctx), ['--tty=false', 'shell', '--workdir', '/', 'cseries', 'sudo', 'podman', 'load', '-q'],
      { input: fs.readFileSync(imagesTar), env: { LIMA_HOME: ctx.paths.lima } });
    if (r.code !== 0) throw new Error(`podman load: ${r.stderr}`);
    ctx.out.detail(r.stdout.toString().trim());
    for (const [which, name] of [['host', hostImage], ['postgres', pgImage]]) {
      const d = await ctx.runner.run(lima.limactlPath(ctx), ['--tty=false', 'shell', '--workdir', '/', 'cseries', 'sudo', 'podman', 'image', 'inspect', '--format', '{{.Digest}}', name],
        { env: { LIMA_HOME: ctx.paths.lima } });
      release.images[which].digest = d.stdout.toString().trim();
      ctx.out.detail(`${which}: ${release.images[which].ref}@${release.images[which].digest}`);
    }
  },
};

async function pass(n) {
  process.stdout.write(`\n===== pass ${n}\n`);
  const ctx = makeContext({ platform: 'linux', arch: 'x64', net, bastionKeys, releaseInfo: { release, placeholders: [], file: 'e2e' }, prompt: async () => false });
  const steps = upSteps({ open: false, label: 'Lima' });
  const at = steps.findIndex((s) => s.title === 'Starting the Linux system');
  if (n === 1) steps.splice(at + 1, 0, loadImages);
  const r = await runSteps(steps, ctx);
  return { r, ctx };
}

(async () => {
  const one = await pass(1);
  if (one.r.failures.length) throw new Error(`pass 1: ${one.r.failures.map((f) => `${f.step}: ${f.error.message}`).join(' | ')}`);
  const two = await pass(2);
  if (two.r.failures.length) throw new Error(`pass 2: ${two.r.failures.map((f) => `${f.step}: ${f.error.message}`).join(' | ')}`);
  process.stdout.write(`\ne2e: state ${JSON.stringify(two.ctx.state.get('bastion'))}\n`);
})().catch((err) => {
  process.stderr.write(`e2e FAILED: ${err.stack || err}\n`);
  process.exit(1);
});
