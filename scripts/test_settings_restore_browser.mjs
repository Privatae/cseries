// The Settings restore module (system-apps/settings/restore.js), run under node
// with a recording fetch and a captured console. Driven by
// scripts/test_settings_restore_browser.py, which also checks the vendored age
// bundle's hash and its interop with the `age` binary.
//
// THE RULE: the recovery key never leaves the page. Every request this module
// makes, and every line it logs, is recorded here and searched for it.

import { Decrypter, Encrypter, armor, generateIdentity, identityToRecipient }
    from '../system-apps/settings/vendor/age-encryption.js';
import * as R from '../system-apps/settings/restore.js';

let failed = 0;
const check = (label, ok, detail) => {
    console.info = realInfo;
    realLog((ok ? '  ok   ' : '  FAIL ') + label + (ok ? '' : '  [' + String(detail).slice(0, 300) + ']'));
    if (!ok) failed++;
    console.info = capture('info');
};

// Capture every console method the module could use.
const realLog = console.log.bind(console);
const realInfo = console.info;
const logged = [];
const capture = (name) => (...a) => { logged.push(name + ': ' + a.map(String).join(' ')); };
for (const m of ['log', 'info', 'warn', 'error', 'debug', 'trace']) console[m] = capture(m);

const recovery = await generateIdentity();
const recoveryRec = await identityToRecipient(recovery);
const bastion = await generateIdentity();
const host = await generateIdentity();
const hostRec = await identityToRecipient(host);
const stamp = '20260927T020700Z';
const perBackup = await generateIdentity();
const body = JSON.stringify({ v: 1, kind: 'privatae-backup-key', identity: perBackup,
    object: 'b/' + stamp + '.tar.zst.age', entity_id: 42, stamp, made_at: '2026-09-27T02:07:00Z' });

async function seal(text, recipients) {
    const e = new Encrypter();
    for (const r of recipients) e.addRecipient(r);
    return armor.encode(await e.encrypt(text));
}
const sidecar = await seal(body, [recoveryRec, await identityToRecipient(bastion)]);

// A recording fetchJSON standing in for the page's.
function recorder(answer) {
    const calls = [];
    const fetchJSON = async (url, opts) => {
        calls.push({ url, method: (opts && opts.method) || 'GET', body: (opts && opts.body) || '' });
        if (url.startsWith('/api/backup/restore/sidecar')) return answer;
        return { key_ready: true, stamp };
    };
    return { calls, fetchJSON };
}

// ── the whole step ───────────────────────────────────────────────────────────
const rec = recorder({ stamp, entity_id: 42, host_recipient: hostRec, sidecar });
const out = await R.restoreKeyInBrowser({ fetchJSON: rec.fetchJSON, stamp, identity: '# created: now\n' + recovery.toLowerCase() + '\n', stepupCode: '123456' });
check('the Host answers that the key is ready', out && out.key_ready === true, JSON.stringify(out));
check('two requests: fetch the sealed key file, send the resealed one',
    rec.calls.length === 2 && rec.calls[0].method === 'GET' && rec.calls[1].method === 'POST'
    && rec.calls[1].url === '/api/backup/restore/key', JSON.stringify(rec.calls.map((c) => c.url)));
const sent = JSON.parse(rec.calls[1].body);
check('what is sent is the backup id, the second-factor code and an age file',
    sent.stamp === stamp && sent.stepup_code === '123456' && sent.via === 'browser'
    && sent.sidecar.startsWith('-----BEGIN AGE ENCRYPTED FILE-----'), rec.calls[1].body.slice(0, 200));
const d = new Decrypter();
d.addIdentity(host);
check('the Host opens the resealed file with its own key, and it is the same key file',
    (await d.decrypt(armor.decode(sent.sidecar), 'text')) === body);
const dr = new Decrypter();
dr.addIdentity(recovery);
let reopened = true;
try { await dr.decrypt(armor.decode(sent.sidecar), 'text'); } catch (e) { reopened = false; }
check('the resealed file is sealed to the Host only (the recovery key does not open it)', !reopened);
const everything = JSON.stringify(rec.calls).toUpperCase();
check('THE RULE: no request carries the recovery key', !everything.includes(recovery.toUpperCase())
    && !everything.includes('AGE-SECRET-KEY'), everything.slice(0, 200));
check('THE RULE: no request carries the backup\'s own key in clear either', !everything.includes(perBackup.toUpperCase()));

// ── refusals: none of them may echo the key ─────────────────────────────────
async function refused(label, fn, want) {
    try {
        await fn();
        check(label, false, 'it was accepted');
    } catch (e) {
        const msg = String(e && e.message);
        check(label, e instanceof R.RestoreKeyError && (!want || msg.includes(want))
            && !msg.toUpperCase().includes('AGE-SECRET-KEY-1'), msg);
    }
}
await refused('a mistyped recovery key is refused without being repeated',
    () => R.restoreKeyInBrowser({ fetchJSON: recorder({ stamp, entity_id: 42, host_recipient: hostRec, sidecar }).fetchJSON,
        stamp, identity: bastion.replace(/.$/, (c) => (c === 'Q' ? 'P' : 'Q')), stepupCode: '1' }), 'typing mistake');
const other = await generateIdentity();
await refused('another key does not open it',
    () => R.restoreKeyInBrowser({ fetchJSON: recorder({ stamp, entity_id: 42, host_recipient: hostRec, sidecar }).fetchJSON,
        stamp, identity: other, stepupCode: '1' }), 'does not open');
await refused('text that is not a recovery key is refused without being repeated',
    () => R.resealSidecar({ identity: 'hunter2 AGE-SECRET-KEY-1XYZ', sidecar, stamp, entityId: 42, hostRecipient: hostRec }),
    'not a Host recovery key');
await refused('two keys pasted together are refused without being repeated',
    () => R.resealSidecar({ identity: recovery + ' ' + other, sidecar, stamp, entityId: 42, hostRecipient: hostRec }),
    'not a Host recovery key');
const oneLine = await R.resealSidecar({ identity: '# Host 42 recovery key' + recovery, sidecar, stamp,
    entityId: 42, hostRecipient: hostRec }).then(() => true, () => false);
check('a key pasted with its comment line run together (a one-line field) is still read', oneLine);
await refused('a key file of another backup is refused',
    () => R.resealSidecar({ identity: recovery, sidecar, stamp: '20260101T000000Z', entityId: 42, hostRecipient: hostRec }),
    'different backup');
await refused('a key file of another Host is not taken without asking, and names both Hosts',
    () => R.resealSidecar({ identity: recovery, sidecar, stamp, entityId: 43, hostRecipient: hostRec }),
    'from Host #42, and this is Host #43');
await refused('nor when the owner says no',
    () => R.resealSidecar({ identity: recovery, sidecar, stamp, entityId: 43, hostRecipient: hostRec,
        confirmOtherHost: async () => false }), 'from Host #42');
let asked = null;
const moved = await R.resealSidecar({ identity: recovery, sidecar, stamp, entityId: 43, hostRecipient: hostRec,
    confirmOtherHost: async (from, here) => { asked = [from, here]; return true; } });
check('when the owner confirms, a lost Host\'s backup is resealed for this one, and they were shown both numbers',
    moved.startsWith('-----BEGIN AGE') && JSON.stringify(asked) === '[42,43]', JSON.stringify(asked));
const notOurs = await seal(JSON.stringify({ v: 1, kind: 'something-else', stamp }), [recoveryRec]);
await refused('a file that is not a Privatae backup key is refused',
    () => R.resealSidecar({ identity: recovery, sidecar: notOurs, stamp, entityId: 42, hostRecipient: hostRec }),
    'not a Privatae backup key');
await refused('the Host naming a different backup is refused',
    () => R.restoreKeyInBrowser({ fetchJSON: recorder({ stamp: '20200101T000000Z', entity_id: 42, host_recipient: hostRec, sidecar }).fetchJSON,
        stamp, identity: recovery, stepupCode: '1' }), 'different backup');
await refused('a Host that gives no key to seal to is refused',
    () => R.resealSidecar({ identity: recovery, sidecar, stamp, entityId: 42, hostRecipient: 'nope' }), 'did not give a key');

// The links an unverified restore left out, said to the owner.
check('no links left out: nothing is said', R.linksLeftOutSentence([], 0) === '' && R.linksLeftOutSentence(undefined) === '');
check('one link left out is named, with why',
    R.linksLeftOutSentence(['deep/out'], 1) === '1 link in the backup pointed outside your host\'s files, so it was left '
        + 'out: this host could not confirm it made the backup. Left out: deep/out.', R.linksLeftOutSentence(['deep/out'], 1));
const many = Array.from({ length: 50 }, (_, i) => 'l' + i);
const said = R.linksLeftOutSentence(many, 57);
check('many: the count is the whole count, ten are named, and the rest is counted',
    said.startsWith('57 links in the backup') && said.includes('l0, l1') && said.includes('l9 and 47 more.')
        && !said.includes('l10'), said);

check('THE RULE: nothing was logged that carries a key',
    !logged.some((l) => l.toUpperCase().includes('AGE-SECRET-KEY')), logged.join(' | ').slice(0, 300));

// Hand the Python gate what it needs for the interop check with the age binary.
console.info = realInfo;
realLog('INTEROP ' + JSON.stringify({ host, sidecarForHost: sent.sidecar, body }));
realLog(failed ? `FAILED: ${failed}` : 'OK: the recovery key never leaves the page');
process.exit(failed ? 1 : 0);
