// The Backup tab's restore, the part that runs in the owner's browser.
//
// THE RULE: the platform holds a backup OR a key, never
// both. A backup's own key sits in its sidecar, sealed to the owner's Host
// recovery key (and to any bastion keys). To restore, this page:
//   1. asks the Host for that backup's sealed key file and the Host's own
//      recipient (GET /api/backup/restore/sidecar: ciphertext only);
//   2. opens the file with the recovery key the owner pasted or scanned, IN
//      THIS PAGE, and checks it is the key of that backup of this Host;
//   3. seals the same contents again to the Host's recipient;
//   4. sends only that resealed file (POST /api/backup/restore/key).
// The recovery key never leaves the page: it is not in any request, not in
// any log line, not kept after the call. scripts/test_settings_restore_browser.py
// runs this module under node with a recording fetch and console to hold that.
//
// age is age's own JavaScript implementation (vendor/age-encryption.js, pinned
// by sha256 in vendor/VERSIONS). No custom crypto here.

import { Decrypter, Encrypter, armor } from './vendor/age-encryption.js';

const IDENTITY = /^AGE-SECRET-KEY-1[023456789ACDEFGHJKLMNPQRSTUVWXYZ]{58}$/;
const STAMP = /^[0-9]{8}T[0-9]{6}Z$/;

export class RestoreKeyError extends Error {}

// The recovery key as typed, pasted or scanned. A pasted key file often
// brings its comment line ("# Host 42 recovery key", "# created: ...") with
// it, and a one-line password field joins the lines together, so the key is
// found wherever it sits in the text; there must be exactly one. Nothing about
// the key goes into an error message.
const IDENTITY_IN_TEXT = /AGE-SECRET-KEY-1[0-9A-Z]+/gi;

export function readIdentity(text) {
    const found = String(text || '').match(IDENTITY_IN_TEXT) || [];
    const id = found.length === 1 ? found[0].toUpperCase() : '';
    if (!IDENTITY.test(id)) {
        throw new RestoreKeyError('That is not a Host recovery key. It is the one long line on the '
            + 'recovery key you saved, under its QR code.');
    }
    return id;
}

// Open a sidecar with the recovery key, check it, seal the same contents to
// the Host. Returns the armored resealed file.
export async function resealSidecar({ identity, sidecar, stamp, entityId, hostRecipient, confirmOtherHost }) {
    if (!STAMP.test(stamp || '')) throw new RestoreKeyError('Choose a backup from the list.');
    if (!/^age1[02-9ac-hj-np-z]{58}$/.test(hostRecipient || '')) {
        throw new RestoreKeyError('The Host did not give a key to seal to.');
    }
    const d = new Decrypter();
    try {
        d.addIdentity(readIdentity(identity));
    } catch (e) {
        // The library's own message repeats the key it could not read.
        if (e instanceof RestoreKeyError) throw e;
        throw new RestoreKeyError('That recovery key has a typing mistake in it. Paste it again, '
            + 'or scan its QR code.');
    }
    let text;
    try {
        text = await d.decrypt(armor.decode(sidecar), 'text');
    } catch (e) {
        throw new RestoreKeyError('This recovery key does not open that backup. It opens only the backups '
            + 'taken while it was your Host\'s recovery key.');
    }
    let body;
    try {
        body = JSON.parse(text);
    } catch (e) {
        throw new RestoreKeyError('That backup\'s key file is not readable.');
    }
    if (!body || body.kind !== 'privatae-backup-key' || body.v !== 1) {
        throw new RestoreKeyError('That is not a Privatae backup key file.');
    }
    if (body.stamp !== stamp || !String(body.object || '').endsWith(stamp + '.tar.zst.age')) {
        throw new RestoreKeyError('That key file belongs to a different backup.');
    }
    if (entityId !== undefined && entityId !== null && body.entity_id !== entityId) {
        // A Host that replaced a lost one may have a new number. Never silent:
        // the owner says so, seeing both numbers.
        const ok = typeof confirmOtherHost === 'function'
            && await confirmOtherHost(body.entity_id, entityId);
        if (!ok) {
            throw new RestoreKeyError('Not restored: that backup is from Host #' + body.entity_id
                + ', and this is Host #' + entityId + '.');
        }
    }
    const e = new Encrypter();
    e.addRecipient(hostRecipient);
    return armor.encode(await e.encrypt(text));
}

// The whole step, with the page's own fetch helper (fetchJSON(url, opts)).
// Resolves to the Host's answer ({key_ready, stamp}).
export async function restoreKeyInBrowser({ fetchJSON, stamp, identity, stepupCode, confirmOtherHost }) {
    const got = await fetchJSON('/api/backup/restore/sidecar?stamp=' + encodeURIComponent(stamp));
    if (!got || got.stamp !== stamp) throw new RestoreKeyError('The Host sent the key file of a different backup.');
    const resealed = await resealSidecar({
        identity, sidecar: got.sidecar, stamp, entityId: got.entity_id, hostRecipient: got.host_recipient,
        confirmOtherHost,
    });
    return fetchJSON('/api/backup/restore/key', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ stepup_code: stepupCode, stamp, via: 'browser', sidecar: resealed }),
    });
}

// Read the recovery key's QR code with the camera, where the browser can
// (BarcodeDetector). Resolves to the key text, or null when it cannot scan.
export function canScan() {
    return typeof globalThis.BarcodeDetector === 'function'
        && !!(globalThis.navigator && navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
}

export async function scanQr(video, { timeoutMs = 60000 } = {}) {
    if (!canScan()) return null;
    const detector = new globalThis.BarcodeDetector({ formats: ['qr_code'] });
    const stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' } });
    video.srcObject = stream;
    video.hidden = false;
    await video.play();
    const stop = () => {
        stream.getTracks().forEach((t) => t.stop());
        video.srcObject = null;
        video.hidden = true;
    };
    const until = Date.now() + timeoutMs;
    try {
        while (Date.now() < until) {
            const found = await detector.detect(video);
            const hit = found.find((c) => /^\s*AGE-SECRET-KEY-1/i.test(c.rawValue || ''));
            if (hit) return hit.rawValue.trim();
            await new Promise((r) => setTimeout(r, 250));
        }
        return null;
    } finally {
        stop();
    }
}

// The links a restore left out, said to the owner (server/host_restore.py:
// a link in a backup this host could not confirm it made, which leads
// outside the restored files once live, is not made). A replacement host's
// restore is always such a backup. `links` holds at most 50 names; `count`
// is how many there were.
export function linksLeftOutSentence(links, count) {
    const names = Array.isArray(links) ? links.filter((x) => typeof x === 'string' && x) : [];
    const n = Math.max(names.length, Number(count) || 0);
    if (!n) return '';
    const shown = names.slice(0, 10);
    const rest = n - shown.length;
    return (n === 1 ? '1 link' : n + ' links') + ' in the backup pointed outside your host\'s files, so '
        + (n === 1 ? 'it was' : 'they were') + ' left out: this host could not confirm it made the backup.'
        + (shown.length ? ' Left out: ' + shown.join(', ') + (rest > 0 ? ' and ' + rest + ' more' : '') + '.' : '');
}

if (typeof window !== 'undefined') {
    window.SettingsRestore = { restoreKeyInBrowser, scanQr, canScan, readIdentity, RestoreKeyError,
                               linksLeftOutSentence };
}
