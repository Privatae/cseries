import { test } from "node:test";
import assert from "node:assert/strict";
import {
  PROOF_PREFIX,
  b64uToBytes,
  bytesToB64u,
  passkeyErrorText,
  passkeyMode,
  proofFromAssertion,
  requestOptions,
} from "../passkey.ts";

// The passkey half of an approval card (app-web-kit/passkey.ts): which way
// the card asks, and the proof it sends in the code's slot, shaped as the
// server's webauthn library reads it (server/approval_passkeys.py).

test("the card asks for a passkey only where one approves and the browser can use it", () => {
  const st = { passkey: true, code: true, setup_url: "http://home.cseries.localhost:8800/passkey" };
  assert.equal(passkeyMode(st, true), "approve");
  assert.equal(passkeyMode({ ...st, passkey: false }, true), "setup");
  assert.equal(passkeyMode(st, false), "unsupported");
  assert.equal(passkeyMode(null, true), "unknown");
});

test("base64url round-trips without padding", () => {
  const bytes = new Uint8Array([0, 1, 250, 251, 252, 253, 254, 255]);
  const s = bytesToB64u(bytes);
  assert.ok(!/[+/=]/.test(s));
  assert.deepEqual([...b64uToBytes(s)], [...bytes]);
});

test("the server's options become the browser's, ids and challenge as bytes", () => {
  const o = requestOptions({
    challenge: bytesToB64u(new Uint8Array([1, 2, 3])),
    rpId: "home.cseries.localhost",
    allowCredentials: [{ id: bytesToB64u(new Uint8Array([9, 9])), type: "public-key", transports: ["internal"] }],
    userVerification: "required",
  });
  assert.deepEqual([...(o.challenge as Uint8Array)], [1, 2, 3]);
  assert.deepEqual([...(o.allowCredentials![0].id as Uint8Array)], [9, 9]);
  assert.equal(o.userVerification, "required");
});

test("the proof is the assertion as JSON behind passkey:", () => {
  const buf = (n: number) => new Uint8Array([n, n]).buffer;
  const proof = proofFromAssertion({
    id: "AQE",
    rawId: buf(1),
    type: "public-key",
    response: { clientDataJSON: buf(2), authenticatorData: buf(3), signature: buf(4), userHandle: buf(5) },
    getClientExtensionResults: () => ({}),
  });
  assert.ok(proof.startsWith(PROOF_PREFIX));
  const j = JSON.parse(proof.slice(PROOF_PREFIX.length));
  assert.equal(j.type, "public-key");
  assert.equal(j.rawId, bytesToB64u(new Uint8Array([1, 1])));
  for (const k of ["clientDataJSON", "authenticatorData", "signature", "userHandle"]) {
    assert.equal(typeof j.response[k], "string", k);
  }
});

test("a prompt the person closed says so and offers the code", () => {
  assert.match(passkeyErrorText({ name: "NotAllowedError" }), /not used.*code instead/);
  assert.match(passkeyErrorText({ name: "SecurityError" }), /can't be used on this address/);
});
