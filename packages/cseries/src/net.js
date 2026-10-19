'use strict';

// HTTP for the few things this command fetches: small manifests, large
// downloads checked against a sha256, and local probes of the Host. Plain
// node:http/https, no dependencies.

const fs = require('node:fs');
const http = require('node:http');
const https = require('node:https');
const path = require('node:path');
const crypto = require('node:crypto');
const { CliError } = require('./errors');

const USER_AGENT = `cseries-cli node/${process.versions.node}`;
const MAX_REDIRECTS = 5;

function moduleFor(u) {
  if (u.protocol === 'https:') return https;
  if (u.protocol === 'http:') return http;
  throw new Error(`unsupported URL scheme: ${u.protocol}`);
}

// One GET, following redirects that stay on the same scheme. Resolves the
// final response; the caller reads or discards its body.
function get(url, { idleTimeoutMs = 60000, redirects = MAX_REDIRECTS } = {}) {
  return new Promise((resolve, reject) => {
    let u;
    try {
      u = new URL(url);
    } catch (err) {
      reject(err);
      return;
    }
    const req = moduleFor(u).get(u, { headers: { 'user-agent': USER_AGENT } }, (res) => {
      const loc = res.headers.location;
      if (res.statusCode >= 300 && res.statusCode < 400 && loc) {
        res.resume();
        if (redirects <= 0) {
          reject(new Error(`too many redirects fetching ${url}`));
          return;
        }
        const next = new URL(loc, u);
        if (next.protocol !== u.protocol) {
          reject(new Error(`refusing a redirect from ${u.protocol} to ${next.protocol}`));
          return;
        }
        get(next.toString(), { idleTimeoutMs, redirects: redirects - 1 }).then(resolve, reject);
        return;
      }
      resolve(res);
    });
    req.setTimeout(idleTimeoutMs, () => req.destroy(new Error(`no answer from ${u.host} for ${idleTimeoutMs / 1000}s`)));
    req.on('error', reject);
  });
}

// A small document (a manifest or a signature), read whole. Refuses anything
// larger than maxBytes.
async function fetchSmall(url, { maxBytes = 1 << 20, timeoutMs = 20000 } = {}) {
  const res = await get(url, { idleTimeoutMs: timeoutMs });
  if (res.statusCode !== 200) {
    res.resume();
    throw new Error(`${url} answered ${res.statusCode}`);
  }
  const chunks = [];
  let n = 0;
  for await (const chunk of res) {
    n += chunk.length;
    if (n > maxBytes) {
      res.destroy();
      throw new Error(`${url} is larger than ${maxBytes} bytes`);
    }
    chunks.push(chunk);
  }
  return Buffer.concat(chunks);
}

function fileSha256(file) {
  return new Promise((resolve) => {
    const h = crypto.createHash('sha256');
    fs.createReadStream(file)
      .on('data', (d) => h.update(d))
      .on('error', () => resolve(null))
      .on('end', () => resolve(h.digest('hex')));
  });
}

// Downloads url to dest and checks its sha256 before dest exists: the bytes go
// to dest.part, and only a file whose digest matches is renamed into place.
// A dest already holding the right bytes is not downloaded again.
async function download(url, dest, { sha256, out, label = path.basename(dest) }) {
  if (!/^[0-9a-f]{64}$/.test(sha256 || '')) throw new Error('download needs the expected sha256');
  if (fs.existsSync(dest) && (await fileSha256(dest)) === sha256) {
    return { downloaded: false };
  }
  fs.mkdirSync(path.dirname(dest), { recursive: true });
  const part = `${dest}.part`;
  let res;
  try {
    res = await get(url);
  } catch (err) {
    throw new CliError(`Could not download ${label}: ${err.message}`,
      'Check this machine\'s internet connection and run the command again.');
  }
  if (res.statusCode !== 200) {
    res.resume();
    throw new CliError(`Could not download ${label}: ${url} answered ${res.statusCode}.`,
      'Run the command again in a few minutes. If it keeps failing, this release may have been withdrawn: try npx cseries@latest.');
  }
  const total = Number(res.headers['content-length']) || 0;
  const hash = crypto.createHash('sha256');
  let done = 0;
  await new Promise((resolve, reject) => {
    const file = fs.createWriteStream(part);
    res.on('data', (chunk) => {
      hash.update(chunk);
      done += chunk.length;
      if (out) out.bytes(label, done, total);
    });
    res.on('error', reject);
    file.on('error', reject);
    file.on('finish', resolve);
    res.pipe(file);
  }).catch((err) => {
    fs.rmSync(part, { force: true });
    throw new CliError(`The download of ${label} stopped: ${err.message}`, 'Run the command again; it starts the download over.');
  });
  if (out) out.line();
  const actual = hash.digest('hex');
  if (actual !== sha256) {
    fs.rmSync(part, { force: true });
    throw new CliError(
      `The downloaded ${label} is not the file this release pins (sha256 ${actual}, expected ${sha256}). Nothing was installed from it.`,
      'Run the command again. If it fails the same way, something between this machine and the download site is changing the file.');
  }
  fs.renameSync(part, dest);
  return { downloaded: true, bytes: done };
}

// A local probe that does not follow redirects. Resolves {status, location}
// or null when nothing answers.
function probe(url, { timeoutMs = 3000 } = {}) {
  return new Promise((resolve) => {
    const u = new URL(url);
    const req = moduleFor(u).get(u, { headers: { 'user-agent': USER_AGENT } }, (res) => {
      res.resume();
      resolve({ status: res.statusCode, location: res.headers.location || '' });
    });
    req.setTimeout(timeoutMs, () => req.destroy());
    req.on('error', () => resolve(null));
  });
}

module.exports = { get, fetchSmall, download, probe, fileSha256 };
