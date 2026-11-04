#!/usr/bin/env node
'use strict';

// npm test: the installer's tests (node --test), run with a temp directory of
// their own, and then a check that they left nothing in it. test/helpers.js
// removes every directory its tmpdir() made when a test file's process exits;
// a run that leaves a cseries-test-* directory behind fails here, so the next
// helper that forgets to clean up is caught by the run that adds it, not by a
// full temp directory weeks later.
//
// Not in test/ and not named like a test file, so node --test never runs it.

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const LEFTOVER = /^cseries-test-/;

function main() {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'cseries-run-'));
  // TMPDIR for POSIX, TMP and TEMP for Windows: os.tmpdir() reads them.
  const env = { ...process.env, TMPDIR: tmp, TMP: tmp, TEMP: tmp };
  const res = spawnSync(process.execPath, ['--test', ...process.argv.slice(2)],
    { cwd: __dirname, env, stdio: 'inherit' });
  let code = res.status === null ? 1 : res.status;
  if (res.error) {
    console.error(`run-tests: node --test did not run: ${res.error.message}`);
    code = 1;
  }
  let left = [];
  try {
    left = fs.readdirSync(tmp).filter((n) => LEFTOVER.test(n));
  } catch (e) {
    console.error(`run-tests: could not read ${tmp}: ${e.message}`);
    code = code || 1;
  }
  if (left.length) {
    console.error(`run-tests: the tests left ${left.length} cseries-test-* director${left.length === 1 ? 'y' : 'ies'} ` +
      `in the temp directory (${left.slice(0, 5).join(', ')}${left.length > 5 ? ', ...' : ''}). ` +
      'Make them with tmpdir() from test/helpers.js, which removes them when the test file ends.');
    code = code || 1;
  }
  try {
    fs.rmSync(tmp, { recursive: true, force: true });
  } catch (e) {
    console.error(`run-tests: could not remove ${tmp}: ${e.message}`);
  }
  process.exit(code);
}

main();
