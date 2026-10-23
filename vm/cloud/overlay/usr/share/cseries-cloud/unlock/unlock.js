/* The unlock page: unlocks this machine's disk with a key only its owner
 * holds, sets those keys up when the machine is claimed, and changes them.
 * The passkey never leaves the browser: the page asks it for its PRF output
 * over the machine's salt and sends the machine only the passphrase derived
 * from that (derive.js). The machine's server is cseries-unlock-agent. */
(function () {
  'use strict';
  const D = window.cseriesDerive;
  const $ = (id) => document.getElementById(id);
  const say = (id, msg) => { const el = $(id); if (el) el.textContent = msg || ''; };
  const fail = (msg) => { say('done', ''); say('err', msg || 'Something went wrong.'); };
  const ok = (msg) => { say('err', ''); say('done', msg); };
  const VIEWS = ['locked', 'enroll', 'recovery-show', 'claiming', 'open', 'absent'];
  const show = (id) => VIEWS.forEach((v) => { $(v).hidden = v !== id; });
  const rnd = (n) => crypto.getRandomValues(new Uint8Array(n));
  let S = null;
  let setupCode = '';
  let polling = null;

  class NoPrf extends Error {}

  async function api(path, body) {
    const opts = body === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json'},
                                             body: JSON.stringify(body)};
    const r = await fetch('/api' + path, Object.assign({credentials: 'omit', cache: 'no-store'}, opts));
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || 'The machine did not answer.');
    return j;
  }

  async function fromPasskey(credIds) {
    if (!window.PublicKeyCredential) throw new NoPrf();
    let cred;
    try {
      cred = await navigator.credentials.get({publicKey: {
        challenge: rnd(32), rpId: S.rp_id, timeout: 120000, userVerification: 'required',
        allowCredentials: credIds.map((id) => ({type: 'public-key', id: D.b64u.dec(id)})),
        extensions: {prf: {eval: {first: D.b64u.dec(S.salt)}}}}});
    } catch (e) {
      throw new Error('The passkey was not used.');
    }
    const r = (cred.getClientExtensionResults() || {}).prf;
    if (!r || !r.results || !r.results.first) throw new NoPrf();
    const cid = D.b64u.enc(cred.rawId);
    return {credential_id: cid,
            passphrase: await D.passphrase(new Uint8Array(r.results.first), S.machine_id, cid)};
  }

  async function newPasskey() {
    if (!window.PublicKeyCredential) throw new NoPrf();
    let cred;
    try {
      cred = await navigator.credentials.create({publicKey: {
        rp: {id: S.rp_id, name: 'C-Series machine'},
        user: {id: rnd(16), name: 'disk@' + S.rp_id, displayName: 'Disk of ' + S.name},
        challenge: rnd(32), timeout: 120000,
        pubKeyCredParams: [{type: 'public-key', alg: -7}, {type: 'public-key', alg: -257}],
        authenticatorSelection: {residentKey: 'preferred', userVerification: 'required'},
        extensions: {prf: {eval: {first: D.b64u.dec(S.salt)}}}}});
    } catch (e) {
      throw new Error('The passkey was not made.');
    }
    const ext = (cred.getClientExtensionResults() || {}).prf || {};
    const cid = D.b64u.enc(cred.rawId);
    if (ext.results && ext.results.first) {
      return {credential_id: cid,
              passphrase: await D.passphrase(new Uint8Array(ext.results.first), S.machine_id, cid)};
    }
    if (ext.enabled === false) throw new NoPrf();
    // Some authenticators give the PRF result only when the passkey is used.
    return fromPasskey([cid]);
  }

  function drawQr(el, text) {
    el.textContent = '';
    if (typeof window.qrcode !== 'function') return;
    const q = window.qrcode(0, 'M');
    q.addData(text);
    q.make();
    const n = q.getModuleCount();
    const ns = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('viewBox', `-2 -2 ${n + 4} ${n + 4}`);
    svg.setAttribute('width', '220');
    svg.setAttribute('height', '220');
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', 'The recovery key as a QR code');
    const bg = document.createElementNS(ns, 'rect');
    bg.setAttribute('x', '-2'); bg.setAttribute('y', '-2');
    bg.setAttribute('width', String(n + 4)); bg.setAttribute('height', String(n + 4));
    bg.setAttribute('fill', '#ffffff');
    svg.appendChild(bg);
    let d = '';
    for (let r = 0; r < n; r++) for (let c = 0; c < n; c++) if (q.isDark(r, c)) d += `M${c} ${r}h1v1h-1z`;
    const path = document.createElementNS(ns, 'path');
    path.setAttribute('d', d);
    path.setAttribute('fill', '#000000');
    svg.appendChild(path);
    el.appendChild(svg);
  }

  function showRecovery(key, withConfirm) {
    say('recovery-text', key);
    drawQr($('recovery-qr'), key);
    $('confirm-form').hidden = !withConfirm;
    show('recovery-show');
  }

  // A key of this machine, to authorise a change: a passkey when one works
  // here, else the recovery key typed into the form.
  function auth() {
    return new Promise((resolve, reject) => {
      const viaRecovery = () => {
        const form = $('auth-recovery-form');
        form.hidden = false;
        $('auth-recovery').focus();
        form.onsubmit = (e) => {
          e.preventDefault();
          form.hidden = true;
          resolve({recovery_key: $('auth-recovery').value});
        };
      };
      if (!S.passkeys.length) { viaRecovery(); return; }
      fromPasskey(S.passkeys.map((k) => k.credential_id)).then(resolve).catch((e) => {
        if (e instanceof NoPrf || e.message === 'The passkey was not used.') {
          say('err', 'Use the recovery key, then.');
          viaRecovery();
        } else { reject(e); }
      });
    });
  }

  function renderKeys() {
    const list = $('key-list');
    list.textContent = '';
    S.passkeys.forEach((k) => {
      const li = document.createElement('li');
      li.textContent = `Passkey ${k.credential_id.slice(0, 8)}… · added ${(k.added_at || '').slice(0, 10) || 'at setup'}`;
      const b = document.createElement('button');
      b.type = 'button';
      b.textContent = 'Remove';
      b.addEventListener('click', async () => {
        try {
          const a = await auth();
          await api('/keys/remove', {auth: a, credential_id: k.credential_id});
          ok('Passkey removed.');
          await refresh();
        } catch (e) { fail(e.message); }
      });
      li.appendChild(document.createElement('br'));
      li.appendChild(b);
      list.appendChild(li);
    });
    const li = document.createElement('li');
    li.textContent = S.recovery ? 'Recovery key · saved at setup or since' : 'No recovery key';
    list.appendChild(li);
  }

  function renderClaim() {
    const c = S.claim || {};
    const pct = c.size ? Math.floor((100 * c.done) / c.size) : 0;
    $('claim-progress').value = pct;
    say('claim-note', c.phase === 'reencrypt' && c.size ? `${pct}% written` : 'Starting…');
  }

  async function refresh() {
    S = await api('/state');
    say('title', S.name ? `Machine ${S.name}` : 'This machine');
    $('host-link').href = S.host_url;
    if (S.state === 'absent') { say('intro', ''); show('absent'); return; }
    if (S.state === 'locked') { say('intro', 'Locked.'); show('locked'); return; }
    if (S.resume && !S.claiming) {
      // A restart cut off setting up the disk after the machine's own key
      // was gone: one of the customer's keys finishes it.
      say('intro', 'A restart cut off setting up the disk. Use one of your keys to finish it.');
      show('locked');
      return;
    }
    if (!S.enrolled) {
      const c = S.claim || {};
      if (c.phase && c.phase !== 'failed' && c.phase !== 'done') {
        say('intro', 'Almost done.');
        renderClaim();
        show('claiming');
        if (!polling) polling = setInterval(() => refresh().catch(() => {}), 2000);
        return;
      }
      if (c.phase === 'failed') fail('Setting up the disk stopped: ' + (c.error || '') + ' Start again below.');
      say('intro', 'One step before you start.');
      if ($('recovery-show').hidden) show('enroll');
      return;
    }
    if (polling) { clearInterval(polling); polling = null; }
    say('intro', '');
    renderKeys();
    show('open');
    const dropped = S.dropped || [];
    if (dropped.includes('recovery')) {
      fail('A restart cut off setting up the disk, so your recovery key does not unlock this machine. '
           + 'Make a new recovery key below now.');
    } else if (dropped.includes('passkey')) {
      fail('A restart cut off setting up the disk, so your passkey does not unlock this machine. '
           + 'Add it again below.');
    }
    if ((S.claim || {}).phase === 'done' && setupCode) {
      $('host-link').href = S.host_url + 'setup';
      $('host-link').textContent = 'Continue: create your owner account';
      ok('The disk is set up. Only your keys unlock it.');
    }
  }

  $('unlock-passkey').addEventListener('click', async () => {
    say('err', '');
    try {
      const k = await fromPasskey(S.passkeys.map((p) => p.credential_id));
      unlocked(await api('/unlock', k));
    } catch (e) {
      if (e instanceof NoPrf) {
        fail('This browser or passkey cannot unlock a disk. Use the recovery key.');
        $('recovery-unlock').open = true;
      } else { fail(e.message); }
    }
  });

  $('recovery-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    say('err', '');
    try {
      unlocked(await api('/unlock', {recovery_key: $('recovery-key').value}));
    } catch (err) { fail(err.message); }
  });

  // After /api/unlock: on to the Host, or, when the unlock finishes setting
  // up the disk (a claim a restart cut off), its progress here.
  function unlocked(r) {
    if (r.claiming) {
      ok('Unlocked. Finishing setting up the disk…');
      refresh().catch(() => {});
      return;
    }
    ok('Unlocked. Starting your Host…');
    setTimeout(() => { location.href = r.next; }, 1500);
  }

  $('enroll-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    say('err', '');
    setupCode = $('setup-code').value;
    let passkey = null;
    try {
      passkey = await newPasskey();
    } catch (err) {
      if (!(err instanceof NoPrf)) { fail(err.message); return; }
      say('err', 'This browser or passkey cannot unlock a disk, so the recovery key alone will. '
                 + 'You will type it after every restart.');
    }
    try {
      const r = await api('/enroll', {setup_code: setupCode, passkey: passkey});
      showRecovery(r.recovery_key, true);
    } catch (err) { fail(err.message); }
  });

  $('confirm-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    say('err', '');
    try {
      await api('/enroll/confirm', {setup_code: setupCode, recovery_key: $('recovery-confirm').value});
      say('recovery-text', '');
      $('recovery-qr').textContent = '';
      show('claiming');
      renderClaim();
      if (!polling) polling = setInterval(() => refresh().catch(() => {}), 2000);
    } catch (err) { fail(err.message); }
  });

  $('add-passkey').addEventListener('click', async () => {
    say('err', '');
    try {
      const a = await auth();
      const k = await newPasskey();
      await api('/keys/add', Object.assign({auth: a}, k));
      ok('Passkey added. It unlocks this machine from now on.');
      await refresh();
    } catch (e) {
      fail(e instanceof NoPrf ? 'This browser or passkey cannot unlock a disk.' : e.message);
    }
  });

  $('new-recovery').addEventListener('click', async () => {
    say('err', '');
    try {
      const a = await auth();
      const r = await api('/keys/recovery', {auth: a});
      showRecovery(r.recovery_key, false);
      ok('A new recovery key. The earlier one no longer unlocks this machine.');
    } catch (e) { fail(e.message); }
  });

  refresh().catch((e) => fail(e.message));
})();
