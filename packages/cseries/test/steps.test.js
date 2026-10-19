'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { runSteps } = require('../src/steps');
const { CliError } = require('../src/errors');
const { captureOutput } = require('./helpers');

// A step that is done once it has run, like every real step.
function idempotent(name, log) {
  let did = false;
  return {
    title: name,
    done: async () => did,
    run: async () => { log.push(name); did = true; },
  };
}

test('every step runs once; a second run skips them all', async () => {
  const log = [];
  const steps = [idempotent('a', log), idempotent('b', log), idempotent('c', log)];
  const out = captureOutput();
  let r = await runSteps(steps, { out });
  assert.deepEqual(log, ['a', 'b', 'c']);
  assert.deepEqual(r, { halted: false, failures: [] });
  r = await runSteps(steps, { out });
  assert.deepEqual(log, ['a', 'b', 'c'], 'nothing ran the second time');
  assert.equal(out.text().match(/Already done\./g).length, 3);
  assert.match(out.text(), /\[1\/3\] a\n.*\n\[2\/3\] b/);
});

test('a second run repairs only what is missing', async () => {
  const log = [];
  const a = idempotent('a', log);
  const b = idempotent('b', log);
  await runSteps([a, b], { out: captureOutput() });
  let broken = true;
  const c = { title: 'c', done: async () => !broken, run: async () => { log.push('c'); broken = false; } };
  await runSteps([a, b, c], { out: captureOutput() });
  assert.deepEqual(log, ['a', 'b', 'c']);
});

test('a failing step stops the run, and the steps after it do not run', async () => {
  const log = [];
  const steps = [
    idempotent('a', log),
    { title: 'b', run: async () => { throw new CliError('b broke', 'fix b'); } },
    idempotent('c', log),
  ];
  await assert.rejects(runSteps(steps, { out: captureOutput() }), /b broke/);
  assert.deepEqual(log, ['a']);
});

test('a soft step\'s failure is a warning; the run goes on and reports it', async () => {
  const log = [];
  const out = captureOutput();
  const steps = [
    { title: 'soft', soft: true, run: async () => { throw new CliError('soft broke', 'try later'); } },
    idempotent('after', log),
  ];
  const r = await runSteps(steps, { out });
  assert.deepEqual(log, ['after']);
  assert.equal(r.failures.length, 1);
  assert.match(out.errText(), /Warning: soft broke/);
  assert.match(out.errText(), /try later/);
});

test('a soft step\'s failure in its done check is a warning too', async () => {
  const r = await runSteps([{ title: 's', soft: true, done: async () => { throw new CliError('no answer', ''); }, run: async () => {} }], { out: captureOutput() });
  assert.equal(r.failures.length, 1);
});

test('a bug (not a CliError) in a soft step still stops the run', async () => {
  await assert.rejects(runSteps([{ title: 's', soft: true, run: async () => { throw new TypeError('bug'); } }], { out: captureOutput() }), TypeError);
});

test('a step can halt the run on purpose, and nothing after it runs', async () => {
  const log = [];
  const r = await runSteps([{ title: 'restart', run: async () => ({ halt: 'Restart Windows.' }) }, idempotent('after', log)], { out: captureOutput() });
  assert.deepEqual(r, { halted: true, message: 'Restart Windows.', failures: [] });
  assert.deepEqual(log, []);
});
