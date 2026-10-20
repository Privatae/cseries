'use strict';

// The limits on the WSL 2 virtual machine, set in the person's own
// %UserProfile%\.wslconfig [wsl2] section. WSL reads that file for every
// distribution on the PC, so it is theirs, not ours: a key they have set is
// never changed, whatever its value; we only add the keys they have not.
//
// The values are the ones WSL and Docker Desktop already use:
//   - Microsoft's WSL documentation (learn.microsoft.com/windows/wsl/wsl-config,
//     [wsl2] settings): memory defaults to 50% of the PC's memory, processors
//     to as many logical processors as the PC has.
//   - Docker Desktop's settings documentation (docs.docker.com/desktop/
//     settings-and-maintenance/settings): the memory limit "defaults to 50%
//     of your host's memory", and with the WSL 2 backend its limits are the
//     WSL 2 VM's, set in .wslconfig.
// Writing them down, instead of leaving WSL to apply them silently, pins what
// the Host was sized against, and a person who wants less edits two lines.
// (Rancher Desktop and Podman Desktop set no memory or processors on Windows
// beyond WSL's own: their documentation names none, so none is cited.)
// swap is left alone: WSL's disk-backed swap is not ours to size, and the
// containers inside forbid themselves swap (cseries-host.container).

const GiB = 1024 ** 3;

// -> {memory: '8GB', processors: '8'} for a PC with this much memory (bytes)
// and this many logical processors.
function vmLimits(totalMemBytes, logicalProcessors) {
  const gb = Math.max(2, Math.round(totalMemBytes / GiB / 2));
  const cpus = Math.max(1, Math.floor(logicalProcessors));
  return { memory: `${gb}GB`, processors: String(cpus) };
}

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
  if (!added.length) return { text, added };
  const add = added.map((k) => `${k}=${want[k]}`);
  if (start === -1) {
    if (lines.length && lines[lines.length - 1].trim() !== '') lines.push('');
    lines.push('[wsl2]', ...add);
  } else {
    let at = end;
    while (at > start + 1 && lines[at - 1].trim() === '') at--;
    lines.splice(at, 0, ...add);
  }
  return { text: lines.join(eol) + eol, added };
}

module.exports = { vmLimits, decodeWslConfig, mergeWslConfig };
