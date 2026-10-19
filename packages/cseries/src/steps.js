'use strict';

// The step runner. A step is
//
//   {title, done?(ctx) -> bool, run(ctx) -> {halt?: string} | void, soft?: bool}
//
// Every run walks every step: one that is already done says so and is skipped,
// so a second run repairs what is missing and changes nothing else. A step
// that throws a CliError stops the run, unless it is soft: then the warning is
// printed, the run goes on, and the command exits non-zero at the end. A step
// may halt the run on purpose (WSL installed, Windows must restart) by
// returning {halt: '<what to tell the person>'}.

const { CliError } = require('./errors');

async function runSteps(steps, ctx) {
  const out = ctx.out;
  const total = steps.length;
  const failures = [];
  for (let i = 0; i < total; i++) {
    const s = steps[i];
    out.step(i + 1, total, s.title);
    try {
      if (s.done && (await s.done(ctx))) {
        out.detail(s.doneText || 'Already done.');
        continue;
      }
      const r = await s.run(ctx);
      if (r && r.halt) {
        return { halted: true, message: r.halt, failures };
      }
    } catch (err) {
      if (s.soft && err instanceof CliError) {
        out.warn(err.message);
        if (err.next) out.warn(err.next);
        failures.push({ step: s.title, error: err });
        continue;
      }
      throw err;
    }
  }
  return { halted: false, failures };
}

module.exports = { runSteps };
