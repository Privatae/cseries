'use strict';

// Release versions: X.Y.Z with an optional -prerelease, compared the way
// semver orders them (a prerelease sorts before its release). Build metadata
// after + is ignored.

const RE = /^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$/;

function parse(v) {
  const m = RE.exec(String(v).trim());
  if (!m) return null;
  return { major: +m[1], minor: +m[2], patch: +m[3], pre: m[4] ? m[4].split('.') : [] };
}

function comparePre(a, b) {
  if (!a.length && !b.length) return 0;
  if (!a.length) return 1;
  if (!b.length) return -1;
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    if (a[i] === undefined) return -1;
    if (b[i] === undefined) return 1;
    const an = /^\d+$/.test(a[i]);
    const bn = /^\d+$/.test(b[i]);
    if (an && bn) {
      const d = Number(a[i]) - Number(b[i]);
      if (d) return Math.sign(d);
    } else if (an !== bn) {
      return an ? -1 : 1;
    } else if (a[i] !== b[i]) {
      return a[i] < b[i] ? -1 : 1;
    }
  }
  return 0;
}

// compare(a, b) -> -1, 0 or 1. Throws on a string that is not a version.
function compare(a, b) {
  const pa = parse(a);
  const pb = parse(b);
  if (!pa) throw new Error(`not a version: ${a}`);
  if (!pb) throw new Error(`not a version: ${b}`);
  for (const k of ['major', 'minor', 'patch']) {
    if (pa[k] !== pb[k]) return pa[k] < pb[k] ? -1 : 1;
  }
  return comparePre(pa.pre, pb.pre);
}

function isVersion(v) {
  return parse(v) !== null;
}

module.exports = { compare, isVersion, parse };
