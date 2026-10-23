/* The unlock passkey's keyslot passphrase, from its WebAuthn PRF output: the
 * lowercase hex of HKDF-SHA256 (empty salt) over the 32-byte PRF result, with
 * info "cseries-vault-v1|<machine_id>|<credential_id>". The machine's
 * cseries_vault.passkey_passphrase computes the same, and
 * scripts/test_cloud_box_unlock_page.mjs checks the two agree.
 * Loaded by the unlock page before unlock.js; also by that test under Node. */
(function (root) {
  'use strict';
  const b64u = {
    dec(s) {
      s = String(s).replace(/-/g, '+').replace(/_/g, '/');
      s += '==='.slice((s.length + 3) % 4);
      return Uint8Array.from(atob(s), (c) => c.charCodeAt(0));
    },
    enc(buf) {
      let s = '';
      new Uint8Array(buf).forEach((x) => { s += String.fromCharCode(x); });
      return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
    },
  };
  const hex = (buf) => Array.from(new Uint8Array(buf), (b) => b.toString(16).padStart(2, '0')).join('');

  async function passphrase(prfOutput, machineId, credentialId, subtle) {
    subtle = subtle || root.crypto.subtle;
    const key = await subtle.importKey('raw', prfOutput, 'HKDF', false, ['deriveBits']);
    const info = new TextEncoder().encode(`cseries-vault-v1|${machineId}|${credentialId}`);
    const bits = await subtle.deriveBits({name: 'HKDF', hash: 'SHA-256', salt: new Uint8Array(0), info},
                                         key, 256);
    return hex(bits);
  }

  root.cseriesDerive = {b64u, hex, passphrase};
})(typeof globalThis !== 'undefined' ? globalThis : this);
