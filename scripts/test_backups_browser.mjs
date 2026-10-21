// The Backups app's browser modules (system-apps/backups/recovery-key.js and
// restore-file.js), run under node with a recording fetch and a captured
// console. Driven by scripts/test_backups_browser.py, which builds the
// fixtures (a real backup and key file, sealed by the Host's own code) and
// checks what this prints against the `age` tools and the Host's Python.
//
// THE RULE: the recovery key's private half never leaves the page. Every
// request these modules make, and every line they log, is recorded here and
// searched for it.

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { createHash } from 'node:crypto';
import { Decrypter, armor, generateIdentity, identityToRecipient }
    from '../system-apps/backups/vendor/age-encryption.js';
import * as K from '../system-apps/backups/recovery-key.js';
import * as F from '../system-apps/backups/restore-file.js';

const FIX = process.env.FIXTURES;
const STAMP = process.env.STAMP;
const REC_IDENTITY = process.env.REC_IDENTITY;

let failed = 0;
const realLog = console.log.bind(console);
const logged = [];
const capture = (name) => (...a) => { logged.push(name + ': ' + a.map(String).join(' ')); };
for (const m of ['log', 'info', 'warn', 'error', 'debug', 'trace']) console[m] = capture(m);
const check = (label, ok, detail) => {
    realLog((ok ? '  ok   ' : '  FAIL ') + label + (ok ? '' : '  [' + String(detail).slice(0, 300) + ']'));
    if (!ok) failed++;
};

// The page loads vendor/qrcode.js as a plain script: so does this.
vm.runInThisContext(fs.readFileSync(new URL('../system-apps/backups/vendor/qrcode.js', import.meta.url), 'utf8'));

realLog('== a recovery key made in the browser');
const key = await K.makeRecoveryKey();
check('an age identity and recipient, in age\'s own encoding',
    /^AGE-SECRET-KEY-1[023456789ACDEFGHJKLMNPQRSTUVWXYZ]{58}$/.test(key.identity)
    && /^age1[qpzry9x8gf2tvdw0s3jn54khce6mua7l]{58}$/.test(key.recipient), key.recipient);
check('the two halves belong together (age\'s own library derives the same public half)',
    (await identityToRecipient(key.identity)) === key.recipient);
const raw = K.ageRecipientBytes(key.recipient);
check('its fingerprint is sha256 of the raw public key, 16 hex characters',
    key.fingerprint === createHash('sha256').update(raw).digest('hex').slice(0, 16), key.fingerprint);
const label = K.recoveryLabel(new Date('2026-10-19T12:00:00Z'));
check('it is labelled as made on this Host, with the day', label === 'Recovery key made on this Host, 2026-10-19', label);
const file = K.keyFileText(label, key);
const lines = file.split('\n');
check('the key file is age-keygen\'s layout: comments, then the key on a line of its own',
    lines[0] === '# ' + label && lines.includes('# public key: ' + key.recipient)
    && lines.includes('# fingerprint: ' + key.fingerprint)
    && lines.filter((l) => !l.startsWith('#') && l).length === 1 && lines.includes(key.identity), file);
check('its file name carries the fingerprint', K.keyFileName(key) === 'cseries-recovery-key-' + key.fingerprint + '.txt');

realLog('== the owner says they saved it, as on the account page');
check('nothing asks them to type the key back: they tick that they saved it, then give their code',
    K.confirmsKey === undefined && K.CONFIRM_CHARS === undefined);
const other = await K.makeRecoveryKey();

realLog('== a key the owner already has');
const had = await K.keyFromIdentity('# my key from my account page\n' + other.identity.toLowerCase() + '\n');
check('its public half and fingerprint are worked out in the page',
    had.recipient === other.recipient && had.fingerprint === other.fingerprint);
let typo = null;
try { await K.keyFromIdentity(other.identity.replace(/.$/, (c) => (c === 'Q' ? 'P' : 'Q'))); } catch (e) { typo = e; }
check('a key with a typing mistake is refused, without repeating it',
    typo && !String(typo.message).toUpperCase().includes('AGE-SECRET-KEY-1'), typo && typo.message);

realLog('== the QR code');
const svg = K.qrSvg(key.identity);
const m = K.qrMatrix(key.identity);
check('the key is drawn as a QR code (alphanumeric, medium correction, as the account page prints it)',
    svg.startsWith('<svg') && svg.includes('<path d="M') && m.length >= 21 && m.length % 4 === 1 && m.length === m[0].length, m.length);
const qr = globalThis.qrcode(0, 'M');
qr.addData(key.identity, 'Alphanumeric');
qr.make();
check('  module for module the account page\'s matrix for the same key',
    qr.getModuleCount() === m.length && m.every((row, r) => row.every((d, c) => d === qr.isDark(r, c))));

realLog('== using it: only the public half is sent');
const sent = [];
const fetchJSON = async (url, opts) => {
    sent.push({ url, method: (opts && opts.method) || 'GET', body: (opts && opts.body) || '' });
    return { ok: true, fingerprint: key.fingerprint };
};
await K.useKey({ fetchJSON, recipient: key.recipient, fingerprint: key.fingerprint, label, stepupCode: '123456' });
const body = JSON.parse(sent[0].body);
check('one request: the public half, its fingerprint, the label and the code',
    sent.length === 1 && sent[0].url === '/api/backup/key/recovery/own' && sent[0].method === 'POST'
    && body.recipient === key.recipient && body.fingerprint === key.fingerprint && body.label === label
    && body.stepup_code === '123456' && body.replace === false, sent[0].body);
check('THE RULE: no request carries the private half',
    !JSON.stringify(sent).toUpperCase().includes('AGE-SECRET-KEY'));

realLog('== a backup downloaded as one file');
const bundlePath = path.join(FIX, 'cseries-backup-' + STAMP + '.tar');
const bundleBytes = fs.readFileSync(bundlePath);
const bundle = new File([bundleBytes], path.basename(bundlePath));
const dataBytes = fs.readFileSync(path.join(FIX, STAMP + '.tar.zst.age'));
const keyFileText = fs.readFileSync(path.join(FIX, STAMP + '.key.age'), 'utf8');
const picked = await F.pickBackup([bundle]);
check('the key file is read from the start of it, and the backup found by where it sits',
    picked.stamp === STAMP && picked.keyFile === keyFileText && picked.data.size === dataBytes.length
    && Buffer.from(await picked.data.arrayBuffer()).equals(dataBytes), picked.stamp);
const pair = await F.pickBackup([new File([dataBytes], STAMP + '.tar.zst.age'), new File([keyFileText], STAMP + '.key.age')]);
check('a backup and its key file chosen together are read the same',
    pair.stamp === STAMP && pair.keyFile === keyFileText && pair.data.size === dataBytes.length);
let refused = null;
try { await F.pickBackup([new File([dataBytes], STAMP + '.tar.zst.age'), new File([keyFileText], '20260101T000000Z.key.age')]); } catch (e) { refused = e; }
check('a key file of another backup is not taken as this one\'s', refused instanceof F.RestoreFileError);
const big = fs.readFileSync(path.join(FIX, 'big-header.tar'));
let cut = null;
try { await F.readBundle(new File([big], 'cseries-backup-big.tar')); } catch (e) { cut = e; }
check('a backup over 8 GiB is found by its PAX size (here cut short, and said so)',
    cut instanceof F.RestoreFileError && /cut short/.test(cut.message), cut && cut.message);

realLog('== its key, sealed to this Host in the page');
const host = await generateIdentity();
const hostRec = await identityToRecipient(host);
const resealed = await F.resealKeyFile({ identity: '# saved\n' + REC_IDENTITY + '\n', keyFile: picked.keyFile,
    stamp: STAMP, entityId: 1, hostRecipient: hostRec });
const d = new Decrypter();
d.addIdentity(host);
const doc = JSON.parse(await d.decrypt(armor.decode(resealed), 'text'));
check('the Host opens the resealed key file with its own key; it names this backup',
    doc.stamp === STAMP && doc.kind === 'privatae-backup-key' && doc.identity.startsWith('AGE-SECRET-KEY-1'));
let wrong = null;
try {
    await F.resealKeyFile({ identity: other.identity, keyFile: picked.keyFile, stamp: STAMP, entityId: 1, hostRecipient: hostRec });
} catch (e) { wrong = e; }
check('another recovery key does not open it, and is not repeated in the message',
    wrong instanceof F.RestoreFileError && /does not open/.test(wrong.message)
    && !wrong.message.toUpperCase().includes('AGE-SECRET-KEY-1'), wrong && wrong.message);

realLog('== the upload, and the restore');
function hostStub() {
    const state = { key: null, data: Buffer.alloc(0), done: false, total: 0 };
    const calls = [];
    const fetch = async (url, opts = {}) => {
        const u = new URL(url, 'http://host.test');
        const q = Object.fromEntries(u.searchParams);
        const b = opts.body ? Buffer.from(await new Response(opts.body).arrayBuffer()) : Buffer.alloc(0);
        calls.push({ url, method: opts.method || 'GET', body: b });
        const reply = (o, status = 200) => new Response(JSON.stringify(o), { status, headers: { 'content-type': 'application/json' } });
        if (u.pathname === '/api/backup/files/upload' && (opts.method || 'GET') === 'GET') {
            return reply({ stamp: q.stamp, key_file: !!state.key, size: state.data.length, done: state.done });
        }
        if (u.pathname === '/api/backup/files/upload' && opts.method === 'PUT') {
            if (q.part === 'key') { state.key = b.toString(); }
            else {
                if (Number(q.offset) !== state.data.length) return reply({ detail: 'wrong offset' }, 400);
                state.data = Buffer.concat([state.data, b]);
                state.done = q.final === 'true' && state.data.length === Number(q.total);
            }
            return reply({ stamp: q.stamp, key_file: !!state.key, size: state.data.length, done: state.done });
        }
        return reply({ detail: 'no such route' }, 404);
    };
    return { state, calls, fetch };
}
const hs = hostStub();
hs.state.data = dataBytes.subarray(0, 1000);        // an upload that dropped part way
const recorded = [];
const fetchJSON2 = async (url, opts) => {
    recorded.push({ url, method: (opts && opts.method) || 'GET', body: (opts && opts.body) || '' });
    if (url === '/api/backup/files') return { entity_id: 1, host_recipient: hostRec };
    return { started: true, verified: false };
};
const progress = [];
const prepared = await F.prepareFromFile({ fetch: hs.fetch, fetchJSON: fetchJSON2, files: [bundle],
    identity: REC_IDENTITY, onProgress: (a, b) => progress.push([a, b]) });
check('the key file and the backup reach the Host whole', hs.state.key.trim() === keyFileText.trim()
    && hs.state.data.equals(dataBytes) && hs.state.done, hs.state.data.length);
check('  carrying on from what the Host already held', progress[0][0] === 1000
    && hs.calls.filter((c) => c.method === 'PUT' && c.url.includes('part=data'))[0].url.includes('offset=1000'), JSON.stringify(progress));
const small = hostStub();
await F.uploadBackup({ fetch: small.fetch, stamp: STAMP, keyFile: keyFileText, data: new Blob([dataBytes]), chunkBytes: 4096 });
const puts = small.calls.filter((c) => c.method === 'PUT' && c.url.includes('part=data'));
check('  in pieces, each at the offset before it, the last one final',
    puts.length === Math.ceil(dataBytes.length / 4096) && small.state.data.equals(dataBytes)
    && puts.every((c, i) => c.url.includes('offset=' + i * 4096))
    && puts.at(-1).url.includes('final=true') && !puts[0].url.includes('final=true'), puts.length);
await F.restoreUploaded({ fetchJSON: fetchJSON2, stamp: prepared.stamp, resealed: prepared.resealed,
    stepupCode: '654321', unverifiedOk: true });
const last = JSON.parse(recorded.at(-1).body);
check('the restore sends the resealed key file, the code and the owner\'s say-so, and nothing else',
    recorded.at(-1).url === '/api/backup/files/restore' && last.stamp === STAMP && last.stepup_code === '654321'
    && last.unverified_ok === true && last.sidecar.startsWith('-----BEGIN AGE ENCRYPTED FILE-----')
    && Object.keys(last).sort().join() === 'sidecar,stamp,stepup_code,unverified_ok', recorded.at(-1).body);
const everything = (JSON.stringify(recorded) + hs.calls.map((c) => c.url + c.body.toString('latin1')).join('')).toUpperCase();
check('THE RULE: no request carries the recovery key', !everything.includes(REC_IDENTITY.toUpperCase())
    && !everything.includes('AGE-SECRET-KEY-1'));
check('THE RULE: nothing logged carries a key', !logged.join('\n').toUpperCase().includes('AGE-SECRET-KEY'),
    logged.join(' | ').slice(0, 200));

{
realLog('== a download never holds the backup whole in the page');
// A recording fetch and a stand-in document: what the page asks for, and
// what it hands the browser.
function dlStub() {
    const calls = [];
    const clicked = [];
    const doc = {
        createElement: (tag) => ({ tag, href: '', download: '', click() { clicked.push({ href: this.href, download: this.download }); }, remove() {} }),
        body: { appendChild() {} },
    };
    const fetch = async (url, opts) => {
        calls.push({ url, method: (opts && opts.method) || 'GET' });
        if (url.endsWith('/link')) {
            return new Response(JSON.stringify({ url: '/api/backup/files/download/dl_' + 'a'.repeat(32),
                                                 name: 'cseries-backup-' + STAMP + '.tar' }), { status: 200 });
        }
        return new Response(new Uint8Array([1, 2, 3]), { status: 200 });
    };
    return { calls, clicked, doc, fetch };
}
const base = '/api/backup/files/' + STAMP;
let dl = dlStub();
let how = await F.downloadFile({ fetch: dl.fetch, url: base + '/download', linkUrl: base + '/link',
    name: 'x.tar', picker: undefined, doc: dl.doc });
check('without the save-file picker (a framed page, Firefox, Safari): the page asks for a one-time link',
    how === 'started' && dl.calls.length === 1 && dl.calls[0].url === base + '/link' && dl.calls[0].method === 'POST',
    JSON.stringify(dl.calls));
check('  and hands the browser that link to download itself, under the file\'s name; the bytes never pass the page',
    dl.clicked.length === 1 && dl.clicked[0].href === '/api/backup/files/download/dl_' + 'a'.repeat(32)
    && dl.clicked[0].download === 'cseries-backup-' + STAMP + '.tar'
    && !dl.calls.some((c) => c.url === base + '/download'), JSON.stringify(dl.clicked));
dl = dlStub();
how = await F.downloadFile({ fetch: dl.fetch, url: base + '/download', linkUrl: base + '/link', name: 'x.tar',
    picker: async () => { const e = new Error('framed'); e.name = 'SecurityError'; throw e; }, doc: dl.doc });
check('a picker the framed page may not use falls back to the link', how === 'started'
    && dl.calls.map((c) => c.url).join() === base + '/link' && dl.clicked.length === 1, JSON.stringify(dl.calls));
dl = dlStub();
how = await F.downloadFile({ fetch: dl.fetch, url: base + '/download', linkUrl: base + '/link', name: 'x.tar',
    picker: async () => { const e = new Error('no'); e.name = 'AbortError'; throw e; }, doc: dl.doc });
check('the owner cancelling the picker downloads nothing', how === 'cancelled' && dl.calls.length === 0
    && dl.clicked.length === 0);
dl = dlStub();
const written = [];
how = await F.downloadFile({ fetch: dl.fetch, url: base + '/download', linkUrl: base + '/link', name: 'x.tar', doc: dl.doc,
    picker: async () => ({ createWritable: async () => new WritableStream({ write: (c) => { written.push(...c); } }) }) });
check('with the picker, the response streams into the file the owner chose', how === 'saved'
    && dl.calls.map((c) => c.url).join() === base + '/download' && written.join() === '1,2,3' && dl.clicked.length === 0,
    JSON.stringify(dl.calls));
dl = dlStub();
const badFetch = async (url, opts) => { dl.calls.push({ url }); return new Response(JSON.stringify({ url: 'https://elsewhere.example/x' }), { status: 200 }); };
let refused = '';
try {
    await F.downloadFile({ fetch: badFetch, url: base + '/download', linkUrl: base + '/link', name: 'x.tar', picker: undefined, doc: dl.doc });
} catch (e) { refused = e.message; }
check('a link that is not this Host\'s download route is not followed', /download link/.test(refused) && dl.clicked.length === 0, refused);
}

realLog('INTEROP ' + JSON.stringify({ identity: key.identity, recipient: key.recipient, fingerprint: key.fingerprint,
                                      keyfile: file }));
process.exit(failed ? 1 : 0);
