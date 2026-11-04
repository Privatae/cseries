'use strict';

// What releases up to 0.1.16 did to the person's .wslconfig, kept here so the
// tests can put a PC in the state such a release left it in: mergeWslConfig
// as it was (src/vm/wslconfig.js before 0.1.17), and the record its
// wsl.js limitVm wrote (state.json wslConfigAdded). The release that reads
// the record takes the lines out again (wsl.js releaseWslConfig, cli.js
// windowsExtras).

const fs = require('node:fs');
const path = require('node:path');

// Merge `want` ({key: value}) into the [wsl2] section of .wslconfig text.
// -> {text, added: [keys]}. Keys compare without regard to case, as WSL's do;
// a commented-out key is not a key, and a key in any [wsl2] section (a file
// can have the header twice) is theirs. A header may carry a comment after
// it. Added keys go at the end of the first [wsl2] section. The file's own
// line endings are kept.
function mergeWslConfig(text, want) {
  const eol = /\r\n/.test(text) ? '\r\n' : '\n';
  const lines = text === '' ? [] : text.split(/\r?\n/);
  if (lines.length && lines[lines.length - 1] === '') lines.pop();
  let start = -1;
  let end = lines.length;
  let inWsl2 = false;
  const have = new Set();
  for (let i = 0; i < lines.length; i++) {
    const s = /^\s*\[([^\]]*)\]\s*(?:[#;].*)?$/.exec(lines[i]);
    if (s) {
      if (inWsl2 && end === lines.length && start !== -1 && i > start) end = i;
      inWsl2 = s[1].trim().toLowerCase() === 'wsl2';
      if (inWsl2 && start === -1) start = i;
      continue;
    }
    if (!inWsl2) continue;
    const k = /^\s*([^#;=\s][^=]*?)\s*=/.exec(lines[i]);
    if (k) have.add(k[1].toLowerCase());
  }
  const added = Object.keys(want).filter((k) => !have.has(k.toLowerCase()));
  if (!added.length) return { text, added, section: false };
  const add = added.map((k) => `${k}=${want[k]}`);
  if (start === -1) {
    if (lines.length && lines[lines.length - 1].trim() !== '') lines.push('');
    lines.push('[wsl2]', ...add);
  } else {
    let at = end;
    while (at > start + 1 && lines[at - 1].trim() === '') at--;
    lines.splice(at, 0, ...add);
  }
  return { text: lines.join(eol) + eol, added, section: start === -1 };
}

// The PC as such a release left it, for a PC of `want` ({memory, processors},
// its defaults then: half the memory, every processor): the file merged and
// written, the record set. -> the lines it added.
function asOldRelease(p, state, want = { memory: '8GB', processors: '8' }) {
  const file = p.wslConfig;
  const existed = fs.existsSync(file);
  const before = existed ? fs.readFileSync(file, 'utf8') : '';
  const { text, added, section } = mergeWslConfig(before, want);
  if (!added.length) return [];
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, text);
  const lines = added.map((k) => `${k}=${want[k]}`);
  state.set('wslConfigAdded', { file, lines, section, created: !existed });
  return lines;
}

module.exports = { mergeWslConfig, asOldRelease };
