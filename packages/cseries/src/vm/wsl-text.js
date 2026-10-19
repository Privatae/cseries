'use strict';

// wsl.exe's own messages and lists are UTF-16LE (with or without a byte
// order mark, CRLF line ends, sometimes a stray NUL), unless WSL_UTF8=1 is
// honoured, when they are UTF-8. What a Linux program inside the distribution
// prints passes through unchanged, as UTF-8. So every buffer from wsl.exe is
// decoded by looking at it, never by assuming.

function looksUtf16le(buf) {
  if (buf.length >= 2 && buf[0] === 0xff && buf[1] === 0xfe) return true;
  if (buf.length < 2) return false;
  // ASCII text in UTF-16LE has a zero in every odd byte. Count over the first
  // few hundred units; mostly-zero odd bytes and few zero even bytes is UTF-16.
  const n = Math.min(buf.length - (buf.length % 2), 512);
  let oddZero = 0;
  let evenZero = 0;
  for (let i = 0; i < n; i += 2) {
    if (buf[i] === 0) evenZero++;
    if (buf[i + 1] === 0) oddZero++;
  }
  const units = n / 2;
  return oddZero / units > 0.6 && evenZero / units < 0.2;
}

function decodeWslOutput(buf) {
  if (!buf || !buf.length) return '';
  if (!Buffer.isBuffer(buf)) buf = Buffer.from(buf);
  let text;
  if (looksUtf16le(buf)) {
    let start = buf[0] === 0xff && buf[1] === 0xfe ? 2 : 0;
    const end = buf.length - ((buf.length - start) % 2);
    text = buf.subarray(start, end).toString('utf16le');
  } else {
    text = buf.toString('utf8');
    if (text.charCodeAt(0) === 0xfeff) text = text.slice(1);
  }
  return text.replace(/\u0000/g, '').replace(/\r\n?/g, '\n');
}

// `wsl.exe --list --quiet`: one distribution name per line.
function parseDistroList(text) {
  return text
    .split('\n')
    .map((l) => l.trim())
    .filter((l) => l && !/\s/.test(l));
}

// `wsl.exe --version`: the first line is "WSL version: 2.3.26.0" in English
// and the same number after a translated label elsewhere. Returns the WSL
// version and, when present, the kernel version.
function parseWslVersion(text) {
  const lines = text.split('\n').map((l) => l.trim()).filter(Boolean);
  const num = /(\d+\.\d+\.\d+(?:\.\d+)?)/;
  if (!lines.length) return null;
  const first = num.exec(lines[0]);
  if (!first) return null;
  const kernelLine = lines.find((l) => /kernel/i.test(l));
  const kernel = kernelLine ? num.exec(kernelLine) : null;
  return { wsl: first[1], kernel: kernel ? kernel[1] : null };
}

// Windows' build number from os.release() ("10.0.22631").
function windowsBuild(release) {
  const m = /^\d+\.\d+\.(\d+)/.exec(String(release));
  return m ? Number(m[1]) : null;
}

// What a failed wsl.exe call's message means, for the few failures a person
// can do something about. Returns {message, next} or null. The codes are the
// HRESULTs wsl.exe prints beside its message.
function explainWslError(text) {
  const t = String(text);
  if (/0x80370102|HCS_E_HYPERV_NOT_INSTALLED/i.test(t)) {
    return {
      message: 'Windows cannot start the virtual machine WSL needs: virtualization is turned off.',
      next: 'Turn on virtualization (Intel VT-x or AMD-V, sometimes called SVM) in this PC\'s firmware (BIOS/UEFI) settings, restart, and run npx cseries again. If you just installed WSL, restarting Windows may be all it needs.',
    };
  }
  if (/0x8004032d|WSL_E_WSL_OPTIONAL_COMPONENT_REQUIRED|Virtual Machine Platform/i.test(t)) {
    return {
      message: 'WSL needs the Virtual Machine Platform, which is not switched on yet.',
      next: 'Restart Windows to finish installing WSL, then run npx cseries again.',
    };
  }
  if (/0x80070422/i.test(t)) {
    return {
      message: 'A Windows service WSL needs is disabled.',
      next: 'Run "wsl.exe --update" in a terminal, restart Windows, and run npx cseries again.',
    };
  }
  if (/ERROR_ALREADY_EXISTS|already exists/i.test(t)) {
    return { message: 'already exists', next: '', alreadyExists: true };
  }
  if (/WSL_E_DISTRO_NOT_FOUND|no distribution with the supplied name/i.test(t)) {
    return { message: 'not found', next: '', notFound: true };
  }
  return null;
}

module.exports = { decodeWslOutput, looksUtf16le, parseDistroList, parseWslVersion, windowsBuild, explainWslError };
