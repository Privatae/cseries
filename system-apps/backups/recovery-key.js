// The Host recovery key, made in the owner's browser, on their own Host.
//
// Every backup this Host takes is encrypted to its recovery key, and only the
// owner holds the key's private half. This page makes the key pair, shows the
// private half ONCE (as text and as a QR code, with a file to save and a page
// to print), has the owner tick that they saved it, as the account page does,
// and only then sends the PUBLIC half, with its fingerprint and their
// second-factor code
// (POST /api/backup/key/recovery/own). The private half is not in any
// request, not in any log line, and not kept once it has been confirmed.
// scripts/test_backups_browser.mjs runs this module under node with a
// recording fetch and console to hold that.
//
// THE SAME KEY AS THE ACCOUNT PAGE MAKES. A key is an age X25519 key pair
// from the browser's own WebCrypto, written in age's own encoding: bech32 of
// the raw 32 bytes, "age1..." for the public half and "AGE-SECRET-KEY-1..."
// for the private half; its fingerprint is the first 16 hex characters of
// the sha256 of the raw public key (server/backup_key.py fingerprint). The
// key file is age-keygen's layout: comment lines, then the key, which age
// reads as it is. So a key made here or on the account page opens backups
// either way, and the restore pages read either.

import { identityToRecipient } from './vendor/age-encryption.js';

const CHARSET = 'qpzry9x8gf2tvdw0s3jn54khce6mua7l';
const GEN = [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3];

function polymod(values) {
    let chk = 1;
    for (const v of values) {
        const b = chk >>> 25;
        chk = ((chk & 0x1ffffff) << 5) ^ v;
        for (let i = 0; i < 5; i++) if ((b >>> i) & 1) chk ^= GEN[i];
    }
    return chk >>> 0;
}

function hrpExpand(hrp) {
    const out = [];
    for (const c of hrp) out.push(c.charCodeAt(0) >>> 5);
    out.push(0);
    for (const c of hrp) out.push(c.charCodeAt(0) & 31);
    return out;
}

function toWords(data) {
    const out = [];
    let acc = 0;
    let bits = 0;
    for (const value of data) {
        acc = ((acc << 8) | value) & 0xffff;
        bits += 8;
        while (bits >= 5) {
            bits -= 5;
            out.push((acc >>> bits) & 31);
        }
    }
    if (bits > 0) out.push((acc << (5 - bits)) & 31);
    return out;
}

export function bech32Encode(hrp, data) {
    const words = toWords(data);
    const poly = polymod([...hrpExpand(hrp), ...words, 0, 0, 0, 0, 0, 0]) ^ 1;
    const checksum = [];
    for (let i = 0; i < 6; i++) checksum.push((poly >>> (5 * (5 - i))) & 31);
    return hrp + '1' + [...words, ...checksum].map((d) => CHARSET[d]).join('');
}

function b64urlToBytes(s) {
    const b64 = s.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - (s.length % 4)) % 4);
    const bin = atob(b64);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
}

// A fresh X25519 key pair as an age recipient and identity, from WebCrypto.
// Throws when the browser has no X25519 (Chrome 133, Firefox 130, Safari 17
// and later have it); the page then says to update the browser.
export async function mintAgeKeypair() {
    const pair = await globalThis.crypto.subtle.generateKey({ name: 'X25519' }, true, ['deriveBits']);
    const pubRaw = new Uint8Array(await globalThis.crypto.subtle.exportKey('raw', pair.publicKey));
    const jwk = await globalThis.crypto.subtle.exportKey('jwk', pair.privateKey);
    if (!jwk.d) throw new Error('x25519 keygen returned no private key');
    const privRaw = b64urlToBytes(jwk.d);
    if (pubRaw.length !== 32 || privRaw.length !== 32) throw new Error('x25519 raw key is not 32 bytes');
    const identity = bech32Encode('age-secret-key-', privRaw).toUpperCase();
    privRaw.fill(0);
    return { recipient: bech32Encode('age', pubRaw), identity };
}

// A valid age X25519 recipient only: "age1", 58 characters, checksum right.
export function isAgeRecipient(s) {
    if (typeof s !== 'string' || !/^age1[qpzry9x8gf2tvdw0s3jn54khce6mua7l]{58}$/.test(s)) return false;
    const data = [...s.slice(4)].map((c) => CHARSET.indexOf(c));
    if (polymod([...hrpExpand('age'), ...data]) !== 1) return false;
    return (data[51] & 0xf) === 0;
}

export function ageRecipientBytes(s) {
    if (!isAgeRecipient(s)) return null;
    const words = [...s.slice(4, 4 + 52)].map((c) => CHARSET.indexOf(c));
    const out = new Uint8Array(32);
    let acc = 0;
    let bits = 0;
    let i = 0;
    for (const w of words) {
        acc = ((acc << 5) | w) & 0xffff;
        bits += 5;
        if (bits >= 8) {
            bits -= 8;
            if (i < 32) out[i++] = (acc >>> bits) & 0xff;
        }
    }
    return i === 32 ? out : null;
}

// sha256 of the raw key, first 16 hex characters: how the Host, the account
// page and the owner's key file all name a recovery key.
export async function ageFingerprint(recipient) {
    const raw = ageRecipientBytes(recipient);
    if (!raw) throw new Error('not an age recipient');
    const digest = new Uint8Array(await globalThis.crypto.subtle.digest('SHA-256', new Uint8Array(raw)));
    return [...digest.slice(0, 8)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

export async function makeRecoveryKey() {
    const k = await mintAgeKeypair();
    return { ...k, fingerprint: await ageFingerprint(k.recipient) };
}

// The label the key is known by, on this Host and at the top of its file.
export function recoveryLabel(now = new Date()) {
    return 'Recovery key made on this Host, ' + now.toISOString().slice(0, 10);
}

// The file the owner saves, in age-keygen's layout, with the public key and
// fingerprint so the owner holds a record of which key this is.
export function keyFileText(label, key) {
    return [
        `# ${label}`,
        `# public key: ${key.recipient}`,
        `# fingerprint: ${key.fingerprint}`,
        "# This opens this Host's backups. Keep it somewhere safe, away from the Host.",
        '# It was made in your browser and never sent anywhere. Lose it (with no bastion key)',
        '# and nobody can open these backups.',
        '# To open a backup by hand: age -d -i this-file backup.tar.zst.age | zstd -d | tar -x',
        key.identity,
        '',
    ].join('\n');
}

export function keyFileName(key) {
    return 'cseries-recovery-key-' + key.fingerprint + '.txt';
}

const IDENTITY = /^AGE-SECRET-KEY-1[023456789ACDEFGHJKLMNPQRSTUVWXYZ]{58}$/;
const IDENTITY_IN_TEXT = /AGE-SECRET-KEY-1[0-9A-Z]+/gi;

// The one recovery key in a text (a pasted key, a key file), upper case; ''
// when there is none, or more than one.
export function identityIn(text) {
    const found = String(text || '').match(IDENTITY_IN_TEXT) || [];
    const id = found.length === 1 ? found[0].toUpperCase() : '';
    return IDENTITY.test(id) ? id : '';
}

// A recovery key the owner already has (made on the account page, or on
// another Host): its public half, worked out here from the private half,
// which stays in this page.
export async function keyFromIdentity(text) {
    const identity = identityIn(text);
    if (!identity) throw new Error('That is not a recovery key. It is the one long line in the key file, under its comments.');
    let recipient;
    try {
        recipient = await identityToRecipient(identity);
    } catch (e) {
        throw new Error('That recovery key has a typing mistake in it. Paste it again, or choose the key file.');
    }
    return { identity, recipient, fingerprint: await ageFingerprint(recipient) };
}

// The key as a QR code, as the account page's printout draws it: rows of
// dark (true) and light modules, alphanumeric mode, medium error correction.
export function qrMatrix(text, qrcode = globalThis.qrcode) {
    if (typeof qrcode !== 'function') throw new Error('the QR library is not loaded');
    const qr = qrcode(0, 'M');
    qr.addData(text, /^[0-9A-Z $%*+\-./:]*$/.test(text) ? 'Alphanumeric' : 'Byte');
    qr.make();
    const n = qr.getModuleCount();
    return Array.from({ length: n }, (_, r) => Array.from({ length: n }, (_, c) => qr.isDark(r, c)));
}

export function qrSvg(text, size = 220, qrcode = globalThis.qrcode) {
    const matrix = qrMatrix(text, qrcode);
    const n = matrix.length + 8;
    const parts = [];
    matrix.forEach((row, r) => row.forEach((dark, c) => {
        if (dark) parts.push(`M${c + 4} ${r + 4}h1v1h-1z`);
    }));
    return `<svg viewBox="0 0 ${n} ${n}" width="${size}" height="${size}" role="img" `
        + `aria-label="Recovery key as a QR code" shape-rendering="crispEdges">`
        + `<rect width="${n}" height="${n}" fill="#ffffff"/><path d="${parts.join('')}" fill="#000000"/></svg>`;
}

// Use the key: send its public half, its fingerprint, its label and the
// owner's code. Never the private half: it is not even passed in.
export async function useKey({ fetchJSON, recipient, fingerprint, label, stepupCode, replace }) {
    if (!isAgeRecipient(recipient)) throw new Error('That is not a recovery key\'s public half.');
    return fetchJSON('/api/backup/key/recovery/own', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ recipient, fingerprint, label, replace: !!replace, stepup_code: stepupCode }),
    });
}

if (typeof window !== 'undefined') {
    window.BackupsKey = { makeRecoveryKey, keyFileText, keyFileName, recoveryLabel,
                          keyFromIdentity, identityIn, qrSvg, useKey };
}
