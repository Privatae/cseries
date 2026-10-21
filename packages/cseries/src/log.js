'use strict';

// The install log: the whole output of the programs this command runs for a
// step (the package manager, the image pulls, Lima starting the VM), so the
// terminal shows one line per step and a step that fails can name the file
// that says why. Appended to, one section per program run; a log past
// MAX_BYTES is moved to <log>.old when a run first writes to it, so it never
// grows without end and the run before is still there.

const fs = require('node:fs');
const path = require('node:path');

const MAX_BYTES = 4 * 1024 * 1024;

class InstallLog {
  constructor(file) {
    this.file = file || null;
    this._opened = false;
  }

  _open() {
    if (this._opened) return;
    this._opened = true;
    fs.mkdirSync(path.dirname(this.file), { recursive: true });
    try {
      if (fs.statSync(this.file).size > MAX_BYTES) fs.renameSync(this.file, `${this.file}.old`);
    } catch {
      // No log yet.
    }
  }

  // A failure to write the log never stops the step it records.
  write(chunk) {
    if (!this.file) return;
    try {
      this._open();
      fs.appendFileSync(this.file, chunk);
    } catch {
      // Nothing to do: the step goes on without its log.
    }
  }

  section(title) {
    this.write(`\n== ${new Date().toISOString()} ${title}\n`);
  }
}

// For a context built without one (a test that calls a driver directly).
const NO_LOG = { write() {}, section() {} };

module.exports = { InstallLog, MAX_BYTES, NO_LOG };
