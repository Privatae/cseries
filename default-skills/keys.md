---
name: keys
description: How the host handles credentials without ever holding one — the sealed vault and its broker, form autofill under the user's standing per-site grant, and bring-your-own compute connections whose keys are sealed and confined to non-prompt roles.
allowed-tools: keys_manage, autofill, connect_compute, credential_wiring, turn_type
---

# Keys SOP

## keys_manage — guidance

You never receive raw secret bytes, and you call `keys_manage` yourself
(`list` through `read`, every other op through `operate`); never dispatch an
agent with a credential. `op='use'` is refused on a turn that read untrusted
content. `op='list'` returns credential
METADATA only (name, type, created) — no values. `op='add'` seals a NEW
credential: name, cred_type (e.g. 'api_key'|'token'|'ssh_key'|'totp_secret';
default 'opaque'), and the secret as value (text) OR value_b64 (base64
bytes). `op='revoke'` supersedes a credential by name. `op='use'` brokers an
action with a credential SERVER-SIDE so the secret never leaves the broker —
name + action ('http_call' makes an authenticated request with the key
injected as Bearer/header/basic and returns only the response: request is
{url, method?, headers?, json?, auth_scheme?: bearer|header|basic,
auth_header?}; 'sign' HMAC-SHA256s a challenge_b64: request is
{challenge_b64, alg?: hmac-sha256}). There is intentionally NO reveal op:
revealing a raw key requires the user's TOTP step-up and happens only in the
Keys app UI.

SOVEREIGN user keys (opt-in): `op='seal_for_user'` seals a value TO a user
(user_id + name + cred_type + value|value_b64) so only the user's private
key — held in their mesh — can open it; the host/platform can NEVER reopen
it. The user's public key must already be registered BY THE USER (POST
/api/vault/user-keys with their authenticator code, or the backup key wizard
in Settings) — there is intentionally NO register op here: the registered key
is who every later seal goes to, and a host must never be able to choose it.

## autofill — guidance

`op='saved'` lists what is stored for a host — keys and whether each is a
password, never the values. `op='list'` shows which sites carry a STANDING
GRANT (unattended filling allowed) and when each was last used. You CANNOT
create a grant — it is the user's own act, made in the Keys app beside the
entries it governs, so that a page cannot talk YOU into widening your own
permission (there is deliberately no 'grant' op). When a fill is refused for
want of one, ask the user to allow that site in Keys. `op='revoke'`
withdraws a grant, which only TIGHTENS and so is yours to do: the saved
entries are kept and only unattended use stops. `op='fill'` types the saved
entries into a browser SESSION you have already created and navigated (the
automation lane, NOT the shared page the user drives in the Browser app):
pass session + mapping [{ref, key}], where refs come from a snapshot of the
page and keys come from op='saved'. Filling is REFUSED without a live grant,
and the refusal names the host so you can ask the user for one. Each field
goes through the credential broker separately: the page's live URL is
re-checked against the host, a value saved as a password can only ever land
in a real password box, and every field writes its own audit row. A field
that fails does not abandon the rest — you get back filled[] and skipped[]
with a reason each. You never see a stored value, here or anywhere.

## connect_compute — guidance

Any token/api-key/SSH key is SEALED IN THE VAULT (you never hold raw
bytes; runpod is dispatched through the broker). `kind='local'` is the host's
OWN local inference endpoint — Ollama/vLLM/llama.cpp (base_url required,
optional token sealed in the vault); `kind='runpod'` is a BYO RunPod
serverless endpoint for BATCH only (endpoint_id + api_key, api_key sealed).
There is no SSH kind here: connect_compute registers inference endpoints only.
To run a command on one of the user's machines, use `machine_exec` from your
root thread (`operate(tool='turn_type', args={op:'root', request:…})`), one command at a time
under that machine's standing grant. `op='assign_role'` points a NON-PROMPT
role (embedding/image) at a registered local/runpod connection so the
inference dispatch reads its base_url (for embedding, optional model and dim
— one of 768/1024/2048/2560/4096). LEAK PROTECTION: a BYO/base_url endpoint
is confined to non-prompt roles (embedding/image) — it can NEVER serve
chat/plan/review/etc., which carry the system prompt. BYO billing is
egress/throughput only (no platform inference debit).


## Parameter notes (full text of shortened argument descriptions)

- **keys_manage** `op`: list = metadata (default); add = seal a new credential; revoke = supersede one; use = broker an action; seal_for_user = seal a value to the user's own key.
- **keys_manage** `request`: use: the action args — http_call: {url, method?, headers?, json?, auth_scheme?, auth_header?}; sign: {challenge_b64, alg?}.
- **connect_compute** `dim`: assign_role role='embedding' (optional): the output dim — one of 768/1024/2048/2560/4096. Defaults to the model's known dim.
- **autofill** `op`: Which operation to run (default 'saved'). There is deliberately no 'grant' — creating one is the user's act, in Keys.
- **autofill** `mapping`: For fill: [{ref, key}] pairs. `ref` is an element from a page snapshot; `key` is one of the keys op='saved' returned.
- **keys_manage** `cred_type`: For add/seal_for_user: kind of credential, e.g. 'api_key', 'token', 'ssh_key', 'totp_secret' (default 'opaque').
- **keys_manage** `action`: For use: 'http_call' = authenticated outbound request (key injected server-side); 'sign' = HMAC-SHA256 a challenge.
