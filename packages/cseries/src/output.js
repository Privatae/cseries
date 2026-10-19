'use strict';

// Everything the command prints goes through here, so tests can capture it and
// progress lines can redraw in place on a terminal and stay quiet in a log.

function formatBytes(n) {
  if (n < 1024) return `${n} B`;
  const units = ['KB', 'MB', 'GB'];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(1)} ${units[i]}`;
}

class Output {
  constructor(stream = process.stdout, errStream = process.stderr) {
    this.stream = stream;
    this.errStream = errStream;
    this.tty = Boolean(stream.isTTY);
    this._progressOpen = false;
    this._lastPlain = -1;
  }

  line(text = '') {
    this._endProgress();
    this.stream.write(`${text}\n`);
  }

  step(index, total, title) {
    this.line(`[${index}/${total}] ${title}`);
  }

  detail(text) {
    this.line(`      ${text}`);
  }

  warn(text) {
    this._endProgress();
    this.errStream.write(`      Warning: ${text}\n`);
  }

  error(message, next) {
    this._endProgress();
    this.errStream.write(`\n${message}\n`);
    if (next) this.errStream.write(`${next}\n`);
  }

  // A byte counter for a download. On a terminal it redraws one line; in a log
  // it prints at most every 10% so the log stays readable.
  bytes(label, done, total) {
    const text = total
      ? `      ${label}: ${formatBytes(done)} of ${formatBytes(total)} (${Math.floor((done / total) * 100)}%)`
      : `      ${label}: ${formatBytes(done)}`;
    if (this.tty) {
      this.stream.write(`\r${text}\x1b[K`);
      this._progressOpen = true;
      return;
    }
    const pct = total ? Math.floor((done / total) * 10) : Math.floor(done / (50 * 1024 * 1024));
    if (pct !== this._lastPlain || done === total) {
      this._lastPlain = pct;
      this.stream.write(`${text}\n`);
    }
  }

  // A line of dots while waiting; one dot per call.
  dot() {
    this.stream.write('.');
    this._progressOpen = true;
  }

  _endProgress() {
    if (this._progressOpen) {
      this.stream.write('\n');
      this._progressOpen = false;
    }
    this._lastPlain = -1;
  }
}

module.exports = { Output, formatBytes };
