'use strict';

// What this command remembers between runs, in one small JSON file: what the
// bastion's elevated step did (bastion.js: the service it installed, the Host
// it paired with and that Host's CA, whether it anchored and enrolled).
// Everything else is read from the machine each time, so a lost or stale
// state file costs at most one repeated step, never a wrong answer.

const fs = require('node:fs');
const path = require('node:path');

class State {
  constructor(file) {
    this.file = file;
    this.data = {};
    try {
      const parsed = JSON.parse(fs.readFileSync(file, 'utf8'));
      if (parsed && typeof parsed === 'object') this.data = parsed;
    } catch {
      this.data = {};
    }
  }

  get(key) {
    return this.data[key];
  }

  set(key, value) {
    if (value === undefined) delete this.data[key];
    else this.data[key] = value;
    fs.mkdirSync(path.dirname(this.file), { recursive: true });
    const tmp = `${this.file}.tmp`;
    fs.writeFileSync(tmp, `${JSON.stringify(this.data, null, 2)}\n`);
    fs.renameSync(tmp, this.file);
  }
}

module.exports = { State };
