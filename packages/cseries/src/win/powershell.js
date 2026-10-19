'use strict';

// Running PowerShell from Node without a quoting problem: the script is
// passed as -EncodedCommand (base64 of its UTF-16LE text), so no character in
// it is ever parsed by cmd.exe or by Windows' argument splitting. Values put
// into a script go through psQuote, PowerShell's single-quoted string.
//
// PowerShell's tokenizer takes FIVE characters as a single quote: ' and the
// typographic U+2018, U+2019, U+201A and U+201B (the language specification's
// single-quote-character). Any of them ends a single-quoted string, so each is
// doubled, as PowerShell's own CodeGeneration.EscapeSingleQuotedStringContent
// does. Escaping only ' would let a value holding ’ close the string: values
// here include a pairing token and an enroll address the Host hands over, and
// some scripts run elevated (elevated.js).
const PS_SINGLE_QUOTES = /['‘’‚‛]/g;

function psQuote(s) {
  return `'${String(s).replace(PS_SINGLE_QUOTES, '$&$&')}'`;
}

function encode(script) {
  return Buffer.from(script, 'utf16le').toString('base64');
}

const PRELUDE = "$ErrorActionPreference = 'Stop'; $ProgressPreference = 'SilentlyContinue'\n";

function args(script) {
  return ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-OutputFormat', 'Text',
    '-EncodedCommand', encode(PRELUDE + script)];
}

// PowerShell started with -EncodedCommand can write its errors to stderr as
// CLIXML ("#< CLIXML" and <S S="Error"> elements) rather than as text. This
// turns either form back into the plain message.
function cleanError(text) {
  const t = String(text || '');
  if (!t.startsWith('#< CLIXML')) return t.trim();
  const parts = [];
  const re = /<S S="Error">([\s\S]*?)<\/S>/g;
  let m;
  while ((m = re.exec(t))) parts.push(m[1]);
  return parts.join('')
    .replace(/_x000D_/g, '')
    .replace(/_x000A_/g, '\n')
    .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&apos;/g, "'").replace(/&amp;/g, '&')
    .trim();
}

// -> {code, stdout, stderr} as text, stderr cleaned.
async function powershell(runner, script, opts = {}) {
  const r = await runner.run('powershell.exe', args(script), opts);
  return {
    code: r.code,
    notFound: r.notFound,
    stdout: r.stdout.toString('utf8'),
    stderr: cleanError(r.stderr.toString('utf8')),
  };
}

module.exports = { powershell, psQuote, encode, args, cleanError };
