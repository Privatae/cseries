// A backup the owner holds as a file: downloaded from this page, or an export
// from another C-Series Host, brought back to restore this Host from it.
//
// THE RULE: the platform holds a backup OR a key, never both, and this Host
// never holds the owner's recovery key. A backup's own key sits in its key
// file, sealed to the owner's recovery key. To restore one from a file, this
// page:
//   1. reads the key file from the file the owner chose (the start of a
//      downloaded backup, or the .key.age file beside it), without reading
//      the rest into memory;
//   2. opens the key file with the recovery key the owner pasted, scanned or
//      chose, IN THIS PAGE, checks it names that backup, and seals the same
//      contents again to this Host's own key (so a wrong recovery key is
//      found before anything large is sent);
//   3. uploads the key file and the backup to this Host, in pieces, each
//      sent at the offset the Host already holds, so a dropped connection
//      picks up where it stopped (PUT /api/backup/files/upload);
//   4. with the owner's agreement and second-factor code, sends only the
//      resealed key file and starts the restore (POST /api/backup/files/restore).
// The recovery key is not in any request and not in any log line, and the
// page drops it once step 2 is done. scripts/test_backups_browser.mjs runs
// this module under node with a recording fetch and console to hold that.
//
// age is age's own JavaScript implementation (vendor/age-encryption.js,
// pinned by sha256 in vendor/VERSIONS). No custom crypto here.

import { Decrypter, Encrypter, armor } from './vendor/age-encryption.js';

const IDENTITY = /^AGE-SECRET-KEY-1[023456789ACDEFGHJKLMNPQRSTUVWXYZ]{58}$/;
const IDENTITY_IN_TEXT = /AGE-SECRET-KEY-1[0-9A-Z]+/gi;
const STAMP = /^[0-9]{8}T[0-9]{6}Z$/;
const DATA_NAME = /([0-9]{8}T[0-9]{6}Z)\.tar\.zst\.age$/;
const KEY_NAME = /([0-9]{8}T[0-9]{6}Z)\.key\.age$/;
const KEY_FILE_MAX = 1 << 20;
const BLOCK = 512;
export const CHUNK_BYTES = 32 * 1024 * 1024;

export class RestoreFileError extends Error {}

// ── reading the file the owner chose ─────────────────────────────────────────

async function bytesOf(blob, start, length) {
    return new Uint8Array(await blob.slice(start, start + length).arrayBuffer());
}

function text(bytes) {
    return new TextDecoder().decode(bytes);
}

function cString(bytes) {
    const end = bytes.indexOf(0);
    return text(end < 0 ? bytes : bytes.subarray(0, end));
}

// A tar header's number: octal digits, or (GNU) base-256 when the first byte
// has its high bit set.
function tarNumber(field) {
    if (field[0] & 0x80) {
        let n = field[0] & 0x7f;
        for (let i = 1; i < field.length; i++) n = n * 256 + field[i];
        return n;
    }
    const s = cString(field).trim();
    return s ? parseInt(s, 8) : 0;
}

// "<len> <key>=<value>\n" records of a PAX header.
function paxRecords(bytes) {
    const out = {};
    let i = 0;
    while (i < bytes.length) {
        let sp = i;
        while (sp < bytes.length && bytes[sp] !== 0x20) sp++;
        const len = parseInt(text(bytes.subarray(i, sp)), 10);
        if (!(len > 0)) break;
        const rec = text(bytes.subarray(sp + 1, i + len - 1));
        const eq = rec.indexOf('=');
        if (eq > 0) out[rec.slice(0, eq)] = rec.slice(eq + 1);
        i += len;
    }
    return out;
}

// The members of a downloaded backup (a plain tar: README, key file,
// backup), read header by header: the key file's text, and where the backup's
// bytes are. Nothing past the key file is read.
export async function readBundle(file) {
    let at = 0;
    let pax = {};
    let keyFile = null;
    for (let n = 0; n < 16 && at + BLOCK <= file.size; n++) {
        const h = await bytesOf(file, at, BLOCK);
        if (h.every((b) => b === 0)) break;
        const type = String.fromCharCode(h[156] || 0x30);
        let name = cString(h.subarray(0, 100));
        const prefix = cString(h.subarray(345, 500));
        if (prefix) name = prefix + '/' + name;
        let size = tarNumber(h.subarray(124, 136));
        const body = at + BLOCK;
        if (type === 'x') {
            if (size > 65536) throw new RestoreFileError('That file is not a backup downloaded from a C-Series Host.');
            pax = paxRecords(await bytesOf(file, body, size));
            at = body + Math.ceil(size / BLOCK) * BLOCK;
            continue;
        }
        if (pax.path) name = pax.path;
        if (pax.size) size = parseInt(pax.size, 10);
        pax = {};
        const end = body + Math.ceil(size / BLOCK) * BLOCK;
        if (type === '0') {
            const k = KEY_NAME.exec(name);
            const d = DATA_NAME.exec(name);
            if (k) {
                if (size > KEY_FILE_MAX) throw new RestoreFileError('That key file is larger than any key file.');
                keyFile = { stamp: k[1], text: text(await bytesOf(file, body, size)) };
            } else if (d) {
                if (!keyFile || keyFile.stamp !== d[1]) {
                    throw new RestoreFileError('That file holds a backup without its key file.');
                }
                if (body + size > file.size) throw new RestoreFileError('That file is cut short: download it again.');
                return { stamp: d[1], keyFile: keyFile.text, data: file.slice(body, body + size) };
            }
        }
        at = end;
    }
    throw new RestoreFileError('That file holds no backup. Choose the file the Backups app downloaded '
        + '(cseries-backup-....tar), or a backup and its key file together.');
}

// The backup in what the owner chose: one downloaded file, or a backup
// (<stamp>.tar.zst.age) and its key file (<stamp>.key.age) together.
export async function pickBackup(files) {
    const list = Array.from(files || []);
    if (list.length === 1) return readBundle(list[0]);
    if (list.length === 2) {
        const data = list.find((f) => DATA_NAME.test(f.name));
        const key = list.find((f) => KEY_NAME.test(f.name));
        if (data && key && DATA_NAME.exec(data.name)[1] === KEY_NAME.exec(key.name)[1]) {
            if (key.size > KEY_FILE_MAX) throw new RestoreFileError('That key file is larger than any key file.');
            return { stamp: DATA_NAME.exec(data.name)[1], keyFile: await key.text(), data };
        }
    }
    throw new RestoreFileError('Choose the one file the Backups app downloaded, or a backup and its key '
        + 'file together (their names start with the same time).');
}

// ── the key file, sealed again to this Host ──────────────────────────────────

// The recovery key as typed, pasted, scanned or read from its file. Nothing
// about the key goes into an error message.
export function readIdentity(t) {
    const found = String(t || '').match(IDENTITY_IN_TEXT) || [];
    const id = found.length === 1 ? found[0].toUpperCase() : '';
    if (!IDENTITY.test(id)) {
        throw new RestoreFileError('That is not a Host recovery key. It is the one long line in the key '
            + 'file you saved, under its QR code on the printout.');
    }
    return id;
}

export async function resealKeyFile({ identity, keyFile, stamp, entityId, hostRecipient, confirmOtherHost }) {
    if (!STAMP.test(stamp || '')) throw new RestoreFileError('That backup has no time stamp.');
    if (!/^age1[02-9ac-hj-np-z]{58}$/.test(hostRecipient || '')) {
        throw new RestoreFileError('This Host did not give a key to seal to.');
    }
    const d = new Decrypter();
    try {
        d.addIdentity(readIdentity(identity));
    } catch (e) {
        if (e instanceof RestoreFileError) throw e;
        throw new RestoreFileError('That recovery key has a typing mistake in it. Paste it again, '
            + 'or choose the key file.');
    }
    let body;
    try {
        body = await d.decrypt(armor.decode(String(keyFile || '').trim() + '\n'), 'text');
    } catch (e) {
        throw new RestoreFileError('This recovery key does not open that backup. It opens only the '
            + 'backups taken while it was the Host\'s recovery key.');
    }
    let doc;
    try {
        doc = JSON.parse(body);
    } catch (e) {
        throw new RestoreFileError('That backup\'s key file is not readable.');
    }
    if (!doc || doc.kind !== 'privatae-backup-key' || doc.v !== 1) {
        throw new RestoreFileError('That is not a backup\'s key file.');
    }
    if (doc.stamp !== stamp || !String(doc.object || '').endsWith(stamp + '.tar.zst.age')) {
        throw new RestoreFileError('That key file belongs to a different backup.');
    }
    if (entityId !== undefined && entityId !== null && doc.entity_id !== entityId) {
        const ok = typeof confirmOtherHost === 'function' && await confirmOtherHost(doc.entity_id, entityId);
        if (!ok) {
            throw new RestoreFileError('Not restored: that backup is from Host #' + doc.entity_id
                + ', and this is Host #' + entityId + '.');
        }
    }
    const e = new Encrypter();
    e.addRecipient(hostRecipient);
    return armor.encode(await e.encrypt(body));
}

// ── the upload ───────────────────────────────────────────────────────────────

async function okJSON(r) {
    if (!r.ok) {
        let detail = '';
        try { detail = (await r.json()).detail || ''; } catch (e) { /* not JSON */ }
        throw new RestoreFileError(String(detail || ('the Host answered ' + r.status)));
    }
    return r.json();
}

// Send the key file, then the backup, in pieces. Carries on from what the
// Host already holds. onProgress(sent, total) after each piece.
export async function uploadBackup({ fetch, stamp, keyFile, data, chunkBytes = CHUNK_BYTES, onProgress }) {
    const q = (o) => '/api/backup/files/upload?' + new URLSearchParams(o).toString();
    let st = await okJSON(await fetch(q({ stamp })));
    if (!st.key_file) {
        const body = new TextEncoder().encode(String(keyFile).trim() + '\n');
        st = await okJSON(await fetch(q({ stamp, part: 'key', offset: 0, total: body.length, final: 'true' }),
            { method: 'PUT', headers: { 'Content-Type': 'application/octet-stream' }, body }));
    }
    let offset = st.done ? data.size : (st.size || 0);
    if (onProgress) onProgress(offset, data.size);
    while (!st.done) {
        const end = Math.min(data.size, offset + chunkBytes);
        st = await okJSON(await fetch(q({ stamp, part: 'data', offset, total: data.size, final: String(end === data.size) }),
            { method: 'PUT', headers: { 'Content-Type': 'application/octet-stream' }, body: data.slice(offset, end) }));
        if (st.size !== end && !st.done) throw new RestoreFileError('The Host did not take that piece whole; try again.');
        offset = end;
        if (onProgress) onProgress(offset, data.size);
    }
    return st;
}

// Steps 1 to 3: read the file, check the recovery key opens it and seal its
// key to this Host, then upload it. Resolves to what step 4 sends.
export async function prepareFromFile({ fetch, fetchJSON, files, identity, confirmOtherHost, onProgress }) {
    const picked = await pickBackup(files);
    const here = await fetchJSON('/api/backup/files');
    const resealed = await resealKeyFile({ identity, keyFile: picked.keyFile, stamp: picked.stamp,
        entityId: here.entity_id, hostRecipient: here.host_recipient, confirmOtherHost });
    await uploadBackup({ fetch, stamp: picked.stamp, keyFile: picked.keyFile, data: picked.data, onProgress });
    return { stamp: picked.stamp, resealed, bytes: picked.data.size };
}

// Step 4: restore it. The owner agreed, and gave their code.
export async function restoreUploaded({ fetchJSON, stamp, resealed, stepupCode, unverifiedOk }) {
    return fetchJSON('/api/backup/files/restore', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ stamp, sidecar: resealed, stepup_code: stepupCode, unverified_ok: !!unverifiedOk }),
    });
}

// ── a download ───────────────────────────────────────────────────────────────

// Save a backup from this Host as one file, never holding it whole in this
// page. With the save-file picker (a top-level page in Chromium) the response
// streams into the file the owner chose; the picker is asked first, while the
// click that started this still counts as one. Without it (this page framed
// in the desktop, Firefox, Safari) the page asks the Host for a one-time link
// to the file (POST .../link, with this window's token) and the browser
// downloads it like any other file, writing it to disk as it arrives
// (Content-Disposition: attachment). A Blob of the response, the way this
// used to save, held the whole backup in memory first.
const LINK = /^\/api\/backup\/files\/download\/dl_[A-Za-z0-9_-]+$/;

export async function downloadFile({ fetch, url, linkUrl, name,
                                     picker = globalThis.showSaveFilePicker, doc = globalThis.document }) {
    if (typeof picker === 'function') {
        let handle = null;
        try {
            handle = await picker({ suggestedName: name });
        } catch (e) {
            if (e && e.name === 'AbortError') return 'cancelled';
            // Refused (a framed page): the link below.
        }
        if (handle) {
            const r = await fetch(url);
            if (!r.ok) await okJSON(r);
            await r.body.pipeTo(await handle.createWritable());
            return 'saved';
        }
    }
    const link = await okJSON(await fetch(linkUrl, { method: 'POST' }));
    if (!LINK.test(String(link.url || ''))) throw new RestoreFileError('The Host did not give a download link.');
    const a = doc.createElement('a');
    a.href = link.url;
    a.download = link.name || name;
    doc.body.appendChild(a);
    a.click();
    a.remove();
    return 'started';
}

if (typeof window !== 'undefined') {
    window.BackupsRestore = { prepareFromFile, restoreUploaded, downloadFile, readIdentity, RestoreFileError };
}
