'use strict';

// The person's own %UserProfile%\.wslconfig. WSL reads its [wsl2] section for
// every Linux system on the PC, so it is theirs, not ours, and C-Series no
// longer writes to it: the Host's size is a ceiling on C-Series's own units
// inside its WSL system (size-caps.js), which caps nothing else on the PC.
//
// What is read from it, and what is still done to it:
//   - wslLimit: the memory= and processors= the person set there, if any.
//     They cap every WSL system together, C-Series's included, so no size
//     above them is offered (wsl.js sizeLimit). Unset, WSL's own defaults
//     stand (learn.microsoft.com/windows/wsl/wsl-config, [wsl2] settings):
//     memory 50% of the PC's, processors as many logical processors as it has.
//   - removeWslConfigLines: the lines releases up to 0.1.16 added there
//     (memory= and processors=, at those same defaults) taken out again, each
//     only where it still stands exactly as written (state.json
//     wslConfigAdded), at the next run (wsl.js releaseWslConfig) and on
//     uninstall --delete-data.

const GiB = 1024 ** 3;

// The file's bytes as text we can append to and write back exactly:
// -> {text, bom} for UTF-8 (bom is the byte order mark it started with, or
// ''), or null for anything else: a UTF-16 file (Notepad's "Unicode"), or
// bytes that are not valid UTF-8, such as a path saved in the PC's ANSI code
// page. Decoding those as UTF-8 and writing them back would replace what the
// person wrote, so such a file is never edited.
function decodeWslConfig(buf) {
  if (buf.length >= 2 && ((buf[0] === 0xff && buf[1] === 0xfe) || (buf[0] === 0xfe && buf[1] === 0xff))) return null;
  const bom = buf.length >= 3 && buf[0] === 0xef && buf[1] === 0xbb && buf[2] === 0xbf ? '﻿' : '';
  const text = buf.toString('utf8');
  if (text.includes('\0') || !Buffer.from(text, 'utf8').equals(buf)) return null;
  return { text: bom ? text.slice(1) : text, bom };
}

// The lines an earlier release added, taken out again:
// `lines` as it wrote them ("memory=8GB"), each removed only where it still
// stands exactly so in a [wsl2] section, so a value the person changed since
// is theirs and stays. `section`: that release wrote the [wsl2] header
// too, which goes when nothing but blank lines is left under it.
// -> {text, removed: [lines]}
function removeWslConfigLines(text, added, section) {
  const eol = /\r\n/.test(text) ? '\r\n' : '\n';
  const lines = text === '' ? [] : text.split(/\r?\n/);
  const trailing = lines.length && lines[lines.length - 1] === '';
  if (trailing) lines.pop();
  const want = new Set(added);
  const removed = [];
  let inWsl2 = false;
  const kept = [];
  for (const l of lines) {
    const s = /^\s*\[([^\]]*)\]\s*(?:[#;].*)?$/.exec(l);
    if (s) inWsl2 = s[1].trim().toLowerCase() === 'wsl2';
    if (!s && inWsl2 && want.has(l.trim()) && !removed.includes(l.trim())) {
      removed.push(l.trim());
      continue;
    }
    kept.push(l);
  }
  if (section && removed.length) {
    // The last [wsl2] header with nothing under it but blank lines.
    for (let i = kept.length - 1; i >= 0; i--) {
      if (!/^\s*\[\s*wsl2\s*\]\s*$/i.test(kept[i])) continue;
      let j = i + 1;
      while (j < kept.length && kept[j].trim() === '') j++;
      if (j === kept.length) {
        kept.splice(i);
        while (kept.length && kept[kept.length - 1].trim() === '') kept.pop();
      }
      break;
    }
  }
  return { text: kept.length ? kept.join(eol) + (trailing || removed.length ? eol : '') : '', removed };
}

// A size as .wslconfig takes it ("8GB", "8192MB", "8G", "0.5TB"; WSL reads a
// bare number as bytes) -> bytes, or null for one WSL would not read.
function wslSize(v) {
  const m = /^\s*([0-9]+(?:\.[0-9]+)?)\s*(B|K|KB|M|MB|G|GB|T|TB)?\s*$/i.exec(String(v || ''));
  if (!m) return null;
  const unit = { B: 1, K: 1024, KB: 1024, M: 1024 ** 2, MB: 1024 ** 2, G: GiB, GB: GiB, T: 1024 * GiB, TB: 1024 * GiB }[(m[2] || 'B').toUpperCase()];
  const n = Math.floor(Number(m[1]) * unit);
  return n > 0 ? n : null;
}

// The memory= (bytes) and processors= the [wsl2] section sets: the last
// value of each, as WSL takes it, in any [wsl2] section; a commented-out key
// is not a key. -> {memory, processors}, each null when unset or unreadable.
function wslLimit(text) {
  let inWsl2 = false;
  let memory = null;
  let processors = null;
  for (const l of String(text || '').split(/\r?\n/)) {
    const s = /^\s*\[([^\]]*)\]\s*(?:[#;].*)?$/.exec(l);
    if (s) {
      inWsl2 = s[1].trim().toLowerCase() === 'wsl2';
      continue;
    }
    if (!inWsl2) continue;
    const kv = /^\s*([^#;=\s][^=]*?)\s*=\s*([^#;]*?)\s*(?:[#;].*)?$/.exec(l);
    if (!kv) continue;
    const k = kv[1].toLowerCase();
    if (k === 'memory') memory = wslSize(kv[2]);
    else if (k === 'processors') processors = /^[1-9][0-9]*$/.test(kv[2]) ? Number(kv[2]) : null;
  }
  return { memory, processors };
}

module.exports = { decodeWslConfig, removeWslConfigLines, wslLimit, wslSize };
