'use strict';

// A failure the person running the command can act on. `message` says what
// went wrong in plain words; `next` says what to do about it. Anything that is
// not a CliError is a bug in this command and is printed with its stack.
class CliError extends Error {
  constructor(message, next, opts = {}) {
    super(message);
    this.name = 'CliError';
    this.next = next || '';
    this.exitCode = opts.exitCode || 1;
    if (opts.cause) this.cause = opts.cause;
  }
}

module.exports = { CliError };
