'use strict';

// Downloads and probes against a real HTTP server on loopback.

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const crypto = require('node:crypto');
const net = require('../src/net');
const { CliError } = require('../src/errors');
const { captureOutput, tmpdir } = require('./helpers');

const payload = crypto.randomBytes(300 * 1024);
const sha = crypto.createHash('sha256').update(payload).digest('hex');

function serve(t) {
  const hits = { file: 0 };
  const srv = http.createServer((req, res) => {
    if (req.url === '/file') {
      hits.file++;
      res.writeHead(200, { 'content-length': payload.length });
      res.end(payload);
    } else if (req.url === '/moved') {
      res.writeHead(302, { location: '/file' });
      res.end();
    } else if (req.url === '/big') {
      res.writeHead(200);
      res.end(Buffer.alloc(2 << 20));
    } else if (req.url === '/setup') {
      res.writeHead(303, { location: '/login' });
      res.end();
    } else {
      res.writeHead(404);
      res.end();
    }
  });
  return new Promise((resolve) => {
    srv.listen(0, '127.0.0.1', () => {
      t.after(() => srv.close());
      resolve({ base: `http://127.0.0.1:${srv.address().port}`, hits });
    });
  });
}

test('a download whose sha256 matches lands at dest, with byte progress', async (t) => {
  const { base } = await serve(t);
  const dest = path.join(tmpdir(), 'x.bin');
  const out = captureOutput();
  const r = await net.download(`${base}/file`, dest, { sha256: sha, out, label: 'the test file' });
  assert.equal(r.downloaded, true);
  assert.ok(fs.readFileSync(dest).equals(payload));
  assert.match(out.text(), /the test file: 300\.0 KB of 300\.0 KB \(100%\)/);
  assert.equal(fs.existsSync(`${dest}.part`), false);
});

test('a download with the wrong sha256 is refused and leaves no file behind', async (t) => {
  const { base } = await serve(t);
  const dest = path.join(tmpdir(), 'x.bin');
  await assert.rejects(net.download(`${base}/file`, dest, { sha256: 'f'.repeat(64) }),
    (e) => e instanceof CliError && /not the file this release pins/.test(e.message) && /Nothing was installed/.test(e.message));
  assert.equal(fs.existsSync(dest), false);
  assert.equal(fs.existsSync(`${dest}.part`), false);
});

test('a file already holding the right bytes is not downloaded again', async (t) => {
  const { base, hits } = await serve(t);
  const dest = path.join(tmpdir(), 'x.bin');
  await net.download(`${base}/file`, dest, { sha256: sha });
  const r = await net.download(`${base}/file`, dest, { sha256: sha });
  assert.equal(r.downloaded, false);
  assert.equal(hits.file, 1);
});

test('a file holding the wrong bytes is replaced', async (t) => {
  const { base, hits } = await serve(t);
  const dest = path.join(tmpdir(), 'x.bin');
  fs.writeFileSync(dest, 'stale');
  await net.download(`${base}/file`, dest, { sha256: sha });
  assert.equal(hits.file, 1);
  assert.ok(fs.readFileSync(dest).equals(payload));
});

test('redirects on the same scheme are followed', async (t) => {
  const { base } = await serve(t);
  const dest = path.join(tmpdir(), 'x.bin');
  await net.download(`${base}/moved`, dest, { sha256: sha });
  assert.ok(fs.readFileSync(dest).equals(payload));
});

test('a 404 is a plain error', async (t) => {
  const { base } = await serve(t);
  await assert.rejects(net.download(`${base}/gone`, path.join(tmpdir(), 'x'), { sha256: sha }), /answered 404/);
});

test('fetchSmall refuses what is larger than a manifest can be', async (t) => {
  const { base } = await serve(t);
  await assert.rejects(net.fetchSmall(`${base}/big`), /larger than/);
  assert.equal((await net.fetchSmall(`${base}/file`)).length, payload.length);
});

test('probe reports a redirect without following it, and null when nothing answers', async (t) => {
  const { base } = await serve(t);
  assert.deepEqual(await net.probe(`${base}/setup`), { status: 303, location: '/login' });
  assert.equal(await net.probe('http://127.0.0.1:1/'), null);
});
