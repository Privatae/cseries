// Composer half of the secret-shapes gate. Mirrors scripts/test_secret_shapes.py
// case for case, because the point of the shared rules file is that the JS
// engine and Python `re` agree on every one of these.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  applyMemory,
  describeFinding,
  fingerprint,
  forgetAllDecisions,
  hardFindings,
  keysHint,
  rememberDecision,
  redactSecrets,
  replaceWithKeysRefs,
  RULES_VERSION,
  scanSecrets,
  secretValue,
  shannonBitsPerChar,
  softFindings,
} from "../src/lib/secret-shapes.ts";

const HARD: Record<string, string> = {
  "anthropic-key": "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcdefghij",
  "openai-key": "sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
  "xai-key": "xai-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
  "google-api-key": "AIzaSyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q",
  "github-token": "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
  "gitlab-token": "glpat-AbCdEfGhIjKlMnOpQrSt",
  "aws-access-key": "AKIAIOSFODNN7EXAMPLE",
  "slack-token": "xoxb-1234567890-abcdefghijkl",
  // Synthetic: the shapes only, built so no real token is in the tree.
  "slack-app-token": "xapp-1-A0FAKE0000-0000000000000-" + "f".repeat(24),
  "telegram-bot-token": "1234567890:" + "AA" + "Fake".repeat(8) + "x",
  "stripe-key": "sk_live_AbCdEfGhIjKlMnOpQrStUvWx",
  jwt: "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
};

test("rules file loaded", () => {
  assert.equal(RULES_VERSION, 1);
});

test("every hard vendor shape is found, labelled, and redacted with prose kept", () => {
  for (const [id, value] of Object.entries(HARD)) {
    const text = `here is my key: ${value} please use it`;
    const { text: out, findings } = redactSecrets(text);
    assert.ok(findings.some((f) => f.id === id), `${id} found`);
    assert.ok(!out.includes(value), `${id} value gone`);
    assert.ok(out.includes(`[REDACTED:${id}]`), `${id} marker`);
    assert.ok(out.startsWith("here is my key: ") && out.endsWith(" please use it"), `${id} prose`);
  }
});

test("PEM block is redacted whole", () => {
  const pem =
    "-----BEGIN OPENSSH PRIVATE KEY-----\n" +
    "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW\n" +
    "QyNTUxOQAAACBsZ2VhZ2VhZ2VhZ2VhZ2VhZ2VhZ2VhZ2VhZ2VhZ2VhZ2VhZ2VhZ2VhZ2Vh\n" +
    "-----END OPENSSH PRIVATE KEY-----";
  const { text } = redactSecrets(`my ssh key:\n${pem}\nthanks`);
  assert.equal(text, "my ssh key:\n[REDACTED:private-key]\nthanks");
});

test("messaging bot tokens: Telegram (bare and in the API URL) and Slack app-level", () => {
  const tg = HARD["telegram-bot-token"];
  assert.equal(tg.split(":")[1].length, 35);
  assert.equal(
    redactSecrets(`curl https://api.telegram.org/bot${tg}/getMe`).text,
    "curl https://api.telegram.org/bot[REDACTED:telegram-bot-token]/getMe",
  );
  assert.equal(
    redactSecrets(`token ${"987654321:" + "B".repeat(34) + "-"} ok`).text,
    "token [REDACTED:telegram-bot-token] ok",
  );
  assert.equal(scanSecrets("at 1696500000:3f786850e387550fdab836ed7e6dc881de23001b it broke").length, 0);
  assert.ok(!scanSecrets("1234567890:" + "C".repeat(36)).some((f) => f.id === "telegram-bot-token"));
  assert.equal(
    redactSecrets(`rotating xoxe.xapp-1-${"Z".repeat(30)} here`).text,
    "rotating [REDACTED:slack-app-token] here",
  );
  const tgHit = hardFindings(scanSecrets(tg))[0];
  assert.deepEqual(keysHint(tgHit), { name: "telegram-bot", cred_type: "opaque" });
  const slHit = hardFindings(scanSecrets(HARD["slack-app-token"]))[0];
  assert.deepEqual(keysHint(slHit), { name: "slack-app-token", cred_type: "opaque" });
});

test("group redaction keeps the surroundings", () => {
  assert.equal(
    redactSecrets("connect with postgres://app:S3cretPassw0rd@db.internal:5432/prod").text,
    "connect with postgres://app:[REDACTED:url-credential]@db.internal:5432/prod",
  );
  assert.equal(
    redactSecrets("Authorization: Bearer AbCdEfGhIjKlMnOpQrStUvWxYz012345").text,
    "Authorization: Bearer [REDACTED:bearer-token]",
  );
});

test("soft heuristics are reported, never redacted by default", () => {
  const soft = "password = hunter2hunter2 and the blob is kJ8f2Lm9Qw3Rt5Yu7Io1Pa4Sd6Fg8Hj0Kl2Zx4Cv6Bn";
  const { text, findings } = redactSecrets(soft);
  const ids = softFindings(findings).map((f) => f.id);
  assert.ok(ids.includes("assigned-secret"));
  assert.ok(ids.includes("high-entropy-token"));
  assert.equal(hardFindings(findings).length, 0);
  assert.equal(text, soft);
  const both = redactSecrets(soft, ["hard", "soft"]).text;
  assert.ok(!both.includes("hunter2hunter2") && !both.includes("kJ8f2Lm9"));
});

test("entropy floor: sha and uuid pass, random base64 trips", () => {
  const sha = "3f786850e387550fdab836ed7e6dc881de23001b";
  assert.equal(scanSecrets(`commit ${sha} fixed it`).length, 0);
  assert.equal(scanSecrets("row 0e7c9a4e-2b1f-4b7e-9c1d-2f3a4b5c6d7e is missing").length, 0);
  assert.ok(scanSecrets("Zx9Qw2Er4Ty6Ui8Op0As1Df3Gh5Jk7Lz9Xc2Vb4Nm6Qw8Er0").some((f) => f.id === "high-entropy-token"));
  assert.ok(shannonBitsPerChar(sha) < 4.3);
});

test("\\b parity with Python (re.ASCII): non-ASCII adjacency is still a hard hit", () => {
  assert.deepEqual(scanSecrets(`café${HARD["anthropic-key"]}`).map((f) => f.id), ["anthropic-key"]);
});

test("precedence: sk-ant- is anthropic, not openai", () => {
  assert.deepEqual(scanSecrets(HARD["anthropic-key"]).map((f) => f.id), ["anthropic-key"]);
});

test("benign chat is untouched", () => {
  for (const b of [
    "can you look at /usr/local/lib/python3.12/site-packages/requests/adapters.py",
    "my password manager is fine, I use a passphrase of four words",
    "the sk- prefix is what openai uses, mine starts with sk-",
    "see https://example.com/docs/getting-started#authentication for the flow",
    "ThisIsAVeryLongVariableNameForTestingPurposesOnly",
    "1234567890123456789012345678901234567890",
    "",
  ]) {
    const { text, findings } = redactSecrets(b);
    assert.equal(text, b);
    assert.equal(findings.length, 0, `${b}: ${findings.map((f) => f.id)}`);
  }
});

test("Store in Keys: hard hits carry a hint, vendor keys on the canonical provider name", () => {
  const text = `use ${HARD["anthropic-key"]} and password = hunter2hunter2`;
  const fs = scanSecrets(text);
  const hard = hardFindings(fs)[0];
  assert.deepEqual(keysHint(hard), { name: "anthropic", cred_type: "api_key" });
  assert.equal(secretValue(text, hard), HARD["anthropic-key"]);
  assert.equal(keysHint(softFindings(fs)[0]), null);
  assert.equal(
    replaceWithKeysRefs(text, [{ f: hard, name: "anthropic" }]),
    'use (stored in Keys as "anthropic") and password = hunter2hunter2',
  );
});

test("fingerprint memory: a stored key is swapped on sight, an allowed soft hit is not held again", async () => {
  forgetAllDecisions();
  const key = HARD["openai-key"];
  const soft = "kJ8f2Lm9Qw3Rt5Yu7Io1Pa4Sd6Fg8Hj0Kl2Zx4Cv6Bn";
  const text = `key ${key} blob ${soft}`;
  // Nothing remembered: everything still needs the user.
  const fresh = await applyMemory(text, scanSecrets(text));
  assert.equal(fresh.text, text);
  assert.equal(fresh.remaining.length, 2);
  assert.equal(fresh.notes.length, 0);
  // Remember both decisions.
  await rememberDecision(key, { kind: "stored", name: "openai" });
  await rememberDecision(soft, { kind: "allowed" });
  const again = await applyMemory(text, scanSecrets(text));
  assert.equal(again.text, `key (stored in Keys as "openai") blob ${soft}`);
  assert.equal(again.remaining.length, 0);
  assert.equal(again.notes.length, 1);
  assert.match(again.notes[0], /already stored in Keys as "openai"/);
  // A different key is still new.
  const other = `key ${HARD["anthropic-key"]}`;
  assert.equal((await applyMemory(other, scanSecrets(other))).remaining.length, 1);
  // The fingerprint is not the value.
  const fp = await fingerprint(key);
  assert.equal(fp.length, 16);
  assert.ok(!key.includes(fp));
  forgetAllDecisions();
});

// Section 13 of scripts/test_secret_shapes.py (section 14 there runs every one
// of its cases through this scanner and the pod's). A span one rule claims,
// and a marker an earlier cut wrote, read as one opaque run to the rules after
// it, so the composer warns of what the pod cuts.
test("one pass cuts a key in a link's user name and the link's password; cut text finds nothing", () => {
  const pem =
    "-----BEGIN OPENSSH PRIVATE KEY-----\n" +
    "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW\n" +
    "QyNTUxOQAAACBsZ2VhZ2VhZ2VhZ2VhZ2VhZ2VhZ2VhZ2VhZ2VhZ2VhZ2VhZ2VhZ2VhZ2Vh\n" +
    "-----END OPENSSH PRIVATE KEY-----";
  for (const [id, value] of Object.entries({ ...HARD, "private-key": pem })) {
    const { text, findings } = redactSecrets(`git clone https://${value}:x-oauth-basic@github.com/x/y.git`);
    assert.equal(text, `git clone https://[REDACTED:${id}]:[REDACTED:url-credential]@github.com/x/y.git`, id);
    assert.deepEqual(hardFindings(findings).map((f) => f.id), [id, "url-credential"], id);
    assert.deepEqual(scanSecrets(text), [], `${id}: the cut text finds nothing`);
  }
  for (const [id, value] of Object.entries(HARD)) {
    for (const x of [
      `git clone https://alex:${value}@github.com/x/y.git`,
      `git clone https://${value}@github.com/x/y.git`,
      `api_key = ${value}`,
    ]) {
      const once = redactSecrets(x).text;
      assert.notEqual(once, x, `${id}: the first pass cut it`);
      assert.deepEqual(redactSecrets(once), { text: once, findings: [] }, `${id}: ${once}`);
    }
  }
  // Only a marker this rules file could have written is claimed.
  assert.deepEqual(scanSecrets(`[REDACTED:${HARD["anthropic-key"]}]`).map((f) => f.id), ["anthropic-key"]);
  const { text, findings } = redactSecrets("https://[REDACTED:github-token]:S3cretPassw0rd@db.internal/prod");
  assert.equal(text, "https://[REDACTED:github-token]:[REDACTED:url-credential]@db.internal/prod");
  assert.deepEqual(hardFindings(findings).map((f) => f.id), ["url-credential"]);
});

test("previews never carry the value", () => {
  for (const f of scanSecrets(HARD["openai-key"])) {
    assert.notEqual(f.preview, HARD["openai-key"]);
    assert.match(f.preview, /^.{4}….{4}$/);
    assert.match(describeFinding(f), /^OpenAI .*\(.{4}….{4}\)$/);
  }
});
