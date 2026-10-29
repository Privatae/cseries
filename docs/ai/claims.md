# C-Series security claims and the tests behind them

Each row is a claim C-Series makes about itself, the test in this tree that
fails if the claim stops being true, and what that test checks. A test is
written `path::name`: a test function, or the label of a check inside a gate
script. Where there is no test, the row names the code that enforces the
claim. [`scripts/test_ai_claims.py`](../../scripts/test_ai_claims.py) fails if
a cited file or name no longer exists.

Where they run: `scripts/*.py` gates run in `scripts/pre_merge_gates.sh`
unless marked (smoke), which runs in `scripts/smoke_test.sh` against a built
image, or (store), which runs on the store-app lane
(`scripts/store_app_gates.sh`). `tests/` runs under pytest in
`scripts/pre_merge_gates.sh`; `packages/cseries/test/` under `npm test`.

The bastion's own claims and their tests are in its repository:
[github.com/Privatae/bastion#how-each-claim-is-tested](https://github.com/Privatae/bastion#how-each-claim-is-tested).

## The virtual machine and the containers

| Claim | Test | What the test checks |
|---|---|---|
| On macOS and Linux the VM forwards only ports 8800, 8810 and 8811, from its loopback to the machine's loopback, and shares no folder. | `packages/cseries/test/lima.test.js::the template forwards 8800, 8810 and 8811 on loopback, ignores every other port, and shares nothing` | The Lima template: three forwards, 127.0.0.1 to 127.0.0.1, every other port ignored, `mounts: []`. |
| No container in the virtual machine can open a connection to the machine under it: its loopback as the VM reaches it, its LAN address, its other addresses; DNS and other computers on the network still work, and the bastion still reaches the Host. | `scripts/test_vm_machine_firewall.py::every port measured open on the machine's loopback (192.168.5.2) is dropped from the container`, `scripts/test_vm_machine_firewall.py::the machine's LAN address :22 is dropped`, `scripts/test_vm_machine_firewall.py::a model server on the LAN and the internet still answer`, `scripts/test_vm_machine_firewall.py::the machine still reaches a forwarded port in the container (the bastion's lane)` | The guest's real rules and loader in network namespaces playing Lima's network, a podman bridge and a container: 192.168.5.2:22/7877/7878/7978/8800 and the machine's LAN address dropped, DNS on 192.168.5.3 answered, a LAN model server, the internet and an inbound forwarded port still reach. Before each tag, the full gate in docs/RELEASING.md runs `vm/wsl/test/boot-test.sh`, which checks in a running WSL guest that neither the Host's container nor a new one reaches the machine at the guest's gateway, and that the Host's container still resolves names and reaches the internet. Nothing in the release boots the Lima guest; `vm/lima/test/e2e.sh` checks the wall in one, run on its own. |
| A guest from an older release gets the machine wall when `npx cseries` updates the Host on it, before the Host is configured or restarted on the new release. Its Host, which that guest started at boot before any wall, is stopped if the run ends with no wall loaded. | `packages/cseries/test/machine-firewall.test.js::Lima: an older guest that update keeps gets the wall`, `packages/cseries/test/machine-firewall.test.js::a wall that does not load stops the run before the Host is configured or started`, `packages/cseries/test/machine-firewall.test.js::the install script fails closed`, `packages/cseries/test/machine-firewall.test.js::Lima: an older guest's Host, started at boot before any wall, is stopped when the wall then cannot be loaded` | The install script, run by /bin/sh into a guest root with none of the wall's files, writes them and loads the unit; a load that fails stops the run before configure and start, and with no wall loaded it stops the Host the boot started. |
| On Windows the WSL guest mounts no Windows drive and cannot start Windows programs. | no test; enforced by the code at `vm/wsl/rootfs/etc/wsl.conf` | A setting inside the guest, not a boundary enforced from outside it. |
| Both containers run as UID 1000 with a read-only root filesystem, no capabilities, no new privileges, and the Host under a seccomp profile. | `tests/test_automated.py::test_docker_compose` | `compose.yaml` carries each setting (a text check). Before each tag, the maintainer's full gate in docs/RELEASING.md runs `vm/wsl/test/boot-test.sh`, which compares the containers running in a WSL guest with it; the release workflow does not run it. |
| The Host's seccomp profile refuses ptrace, memfd_create, unshare and mount. | `tests/test_seccomp_browser_profile.py::test_the_rest_of_the_hardening_is_untouched`, `tests/test_seccomp_browser_profile.py::test_the_allowlist_was_not_quietly_expanded` | ptrace and memfd_create return an error; unshare and mount are among the calls only the browser's profile adds. |
| The database listens on no network port. | no test; enforced by the code at `entrypoint-postgres.sh` | Writes `listen_addresses = ''`. |
| The database's data directory is never mounted into the Host, and the Host's /tmp is in memory. | no test; enforced by the code at `compose.yaml` and `vm/wsl/rootfs/etc/containers/systemd/cseries-host.container` | |

## The dashboard

| Claim | Test | What the test checks |
|---|---|---|
| Without the owner's session, only loopback reaches the dashboard, apart from routes that check their own credential: the few the local runtime's containers call from their own network, and an outside agent's `/mcp` and OAuth paths. | `scripts/test_dashboard_dualstack.py::public v4 still refused`, `scripts/test_dashboard_dualstack.py::v4-mapped docker bridge gw refused without a session`, `scripts/test_localhost_gate_runtime_net.py::from outside a page request without a session is still sent to sign in`, `scripts/test_agent_access_door.py::gate: POST /mcp from the door reaches its handler`, `scripts/test_agent_access_door.py::is not an agent's request and is refused without a session` | The real middleware refuses public addresses and the container bridge's gateway without a session, and sends a page request from outside to sign in. From the runtime's network only those few routes pass. From the door's address the agent paths reach their handlers, and the door refuses any other method on them without a session. |
| Behind a proxy, the Host answers the proxy's door question only with the door key, and answers nobody without the key file. | `scripts/test_host_door_unit.py::the trusted proxy's peer without the door key: 401 for a public path`, `scripts/test_host_door_unit.py::no CSERIES_DOOR_SECRET_FILE: the door answers nobody` | 401 for a missing, wrong or short key, and for every question with no file. |
| Once a passkey exists the password no longer signs in, and the first passkey ends every other session. | `scripts/test_owner_passkeys_unit.py::once a passkey exists the password is refused`, `scripts/test_owner_passkeys_unit.py::the first passkey ends every other session, the app origin joined from it too, though both were checked a moment ago` | Password sign-in raises; two other sessions are deleted, the current one stays. |
| Until the owner exists, the setup code changes at every start unless the installer fixed it in a file (C-Series Cloud does), and the password must be at least 12 characters. | `scripts/test_owner_login_unit.py::and stays the same across restarts` | A code in `CSERIES_SETUP_CODE_FILE` is taken and kept across restarts. The fresh code at each start and the 12 characters: no test; enforced by the code at `server/owner_login.py`. |
| One app's page cannot reach another app's. | `scripts/test_app_origin.py::WebSocket to another app's path on Settings' origin is closed`, `scripts/test_app_origin.py::a header token on the wrong origin is still a 403` | The real origin guard and token middleware. |
| Every response from the Host's routes says nosniff and Referrer-Policy no-referrer, and the desktop shell's page carries its own Content-Security-Policy. | `scripts/test_security_headers.py::every response says nosniff`, `scripts/test_security_headers.py::every response says Referrer-Policy no-referrer`, `scripts/test_security_headers.py::carries the shell policy` | The middleware's own source sets both headers; run on its own, it puts the shell's policy on the shell's page on each desktop name. |
| While the kill switch is set, mapped API routes are refused except stops and governance. | `scripts/test_ceigas_gate_routes.py::kill_switch_problems` | Through the real app: writes refused 403, stops reach their handlers. |

## Outside agents over MCP

An agent such as Claude Code connects to the Host's `/mcp` by OAuth, with the
Host as the authorization server (`server/agent_access/`).

| Claim | Test | What the test checks |
|---|---|---|
| `/mcp` answers nothing without a token, from loopback too, and a disconnected agent's token stops at once. | `scripts/test_agent_access_door.py::no token is 401 naming the sign-in`, `scripts/test_agent_access_pg.py::and its token stops at once` | The real handler behind the real gates answers 401 to a call with no token from loopback and from the door; after the person disconnects the agent, its token gets 401. |
| An agent's change reaches a machine only after the person approves it on the Host's page, and "just this once" covers only those arguments, once. | `scripts/test_agent_access_pg.py::a change answers -32042 with the approval link`, `scripts/test_agent_access_pg.py::and nothing reached the machine`, `scripts/test_agent_access_pg.py::'once' bound the exact arguments: another argument asks again`, `scripts/test_agent_access_pg.py::and 'once' is spent: the same call asks again` | Real routes on the kernel's schema, the machine side stubbed: nothing reaches it before the answer; another argument, or the same call again, asks again. |
| An agent on this computer signs in on `http://127.0.0.1:<port>/mcp`, the address Settings shows on an installed Host, and approves on the desktop's own connect page; its token is taken only on that address. | `scripts/test_agent_access_pg.py::and the connect page is the desktop's own, where the person is signed in`, `scripts/test_agent_access_pg.py::the code goes back with iss = the issuer the client found (127.0.0.1's, RFC 9207)`, `scripts/test_agent_access_pg.py::the approval link a 127.0.0.1 agent gets is the desktop's own page`, `scripts/test_agent_access_pg.py::a token for 127.0.0.1 is not taken on another address of this Host (its audience)`, `scripts/test_agent_access_pg.py::the desktop's /mcp page and Settings name 127.0.0.1's address for agents`, `scripts/test_agent_access_pg.py::and the whole sign-in runs there: iss is its own, its token works on it` | Real routes on the kernel's schema: on 127.0.0.1 the metadata keeps everything there but the connect page, which is the desktop's; the code comes back with 127.0.0.1's issuer and its approval links are the desktop's; the token works on 127.0.0.1 and gets 401 elsewhere; the https remote address signs in on its own as before. |
| An op the Host itself may not run is never offered to the person, and never spends "just this once". | `scripts/test_agent_access_pg.py::an op the Host's own gate refuses: an error, no ask and no link for the person, nothing run, recorded as refused by the Host's own permissions`, `scripts/test_agent_access_pg.py::and an agent the person never let run that op is not told why: the person's own words in their grant (or a quarantine) stay on the Host`, `scripts/test_agent_access_pg.py::the Host's gate refusing at the run: an error with its words, the 'once' given back, never recorded as ran`, `scripts/test_agent_access_pg.py::the approval page of an op the Host may no longer run offers no approval`, `scripts/test_agent_access_pg.py::the real pod gate's refusal is marked refused_by the Host, and gate_op gives its reason` | The pod gate (stubbed for the flow, real for its marker) refuses: no ask is filed, the agent gets an error saying the Host's own permissions refuse it (with the reason only when it holds the op), a refusal at the run gives the "once" back, and an older ask's page offers no approval. |
| The connect and approval pages answer only the signed-in owner, with a passkey check from this session or a code from the authenticator. | `scripts/test_agent_access_door.py::door: the approval page without a session goes to sign in`, `scripts/test_agent_access_pg.py::the connect page needs the person's sign-in`, `scripts/test_agent_access_pg.py::no passkey check and no code: nothing is connected`, `scripts/test_agent_access_pg.py::another session's passkey check confirms nothing here`, `scripts/test_agent_access_pg.py::a wrong code allows nothing` | Without a session the pages send to sign in or refuse; a yes with no fresh passkey check and no right code changes nothing. |

## Agents the Host dispatches

In `scripts/test_agent_grant_wall.py` (smoke, and in `scripts/pre_merge_gates.sh`), except where a row names another file.

| Claim | Test | What the test checks |
|---|---|---|
| An agent starts with no capabilities. | `scripts/test_agent_grant_wall.py::floor is zero — an agent holds nothing until elevated` | The floor is 0 bits; an empty grant is 0. |
| A grant naming a write capability, administering or a shell is refused at dispatch. | `scripts/test_agent_grant_wall.py::door refuses a grant holding`, `scripts/test_agent_grant_wall.py::a dispatch granting admin is refused at the door` | Each write name, and admin, is refused before the agent exists. |
| A misspelt capability name is refused, never matched to a close one. | `scripts/test_agent_grant_wall.py::unknown capability names are surfaced, never silently dropped`, `scripts/test_agent_grant_wall.py::door pre-check surfaces unknown capability names` | `reed_memory` comes back as unknown, and the door's pre-check refuses a dispatch that names a capability that does not exist. |
| An agent's capabilities are the Host's own and the grant's together, never more. | `scripts/test_agent_grant_wall.py::grant can never widen past the entity`, `scripts/test_agent_grant_wall.py::read-only grant masks WRITE_MEMORY off the effective bits` | Bitwise AND of the two. |
| The check is per operation; an unclassified operation needs the tool's full requirement. | `scripts/test_agent_grant_wall.py::a read op authorizes as a READ`, `scripts/test_agent_grant_wall.py::an absent/unknown op falls back to the union (never looser)` | A search needs the read bit; a missing or unknown op needs read and write. |
| A tool the agent was not given is refused when called, and the refusal is shown. | `scripts/test_agent_grant_wall.py::out-of-grant tool never reaches the executor`, `scripts/test_agent_grant_wall.py::refusal is VISIBLE — denied tool card emitted` | The executor never runs; one denied card is emitted. |
| A dispatch whose tools need a capability it was not granted is refused before it starts. | `scripts/test_agent_grant_wall.py::incoherent dispatch refused before the agent runs`, `scripts/test_agent_grant_wall.py::refusal names the tool and the missing capability` | No round runs; the error names the tool and the capability. |
| Nothing an agent runs can execute code. | `scripts/test_agent_grant_wall.py::code-exec never reaches the executor`, `scripts/test_operate_sweep.py::no code-exec name is in DIRECT_OP_TOOLS` | Code-running tools are stripped and refused; the known-operation path holds none. |

## Approvals and the person's limits

| Claim | Test | What the test checks |
|---|---|---|
| A network rule that widens reach commits only with the person's authenticator code; tightening needs none. | `scripts/test_nac_allow_permits.py::an allow with a lane approves, with the step-up checked`, `scripts/test_nac_allow_permits.py::revoking an approved quarantine WITHOUT a code is refused 403`, `scripts/test_nac_allow_permits.py::revoking an approved allow that admits needs no code (a tighten)` | The code is checked on widening; a tighten passes without one. |
| With no authenticator enrolled nothing widens, and each code works once. | `scripts/check_nac_user_gate.py::db_tests` | (smoke) No factor: a valid code is refused. A counter is accepted once. |
| Wrong codes make the person wait longer each time, up to an hour, and inside the wait even the right code is refused. | `scripts/test_stepup_attempt_limit.py::each wrong code after the free ones doubles the wait, up to an hour`, `scripts/test_stepup_attempt_limit.py::inside the wait even the right code is not accepted (429)` | Waits of 60 s doubling to 3600 s; 429 inside the wait. |
| On ingress, while any approved rule fails its check (its signed terms were changed, it carries no signature, its match cannot be evaluated, or the Host cannot read its signing key), the gate admits no machine. | `scripts/check_nac_engine.py::pure_tests`, `scripts/check_nac_engine.py::lock_tests`, `scripts/test_nac_cards_match_engine.py::...and the gate's own evaluate() refuses the machine the rule would admit`, `scripts/test_nac_gate_fails_closed.py::the engine locked on the rule rather than raising`, `scripts/test_nac_gate_fails_closed.py::no lock, and every reported machine gets the answer the old matcher gave`, `scripts/test_nac_gate_fails_closed.py::still locks as an unreadable match` | (engine: smoke) Tampered terms and an unsigned rule deny every device; with the key unreadable a signed allow is not enforced. A list or a string as an approved rule's whole match, a wrongly typed condition, or a condition written as text that holds a known value without being one locks the engine; one known value written as text where a list belongs is enforced as that value and does not lock. |
| On ingress, a machine the gate cannot decide for is refused: the engine raised, or no engine loaded at start. | `scripts/test_nac_gate_fails_closed.py::the gate refuses the machine when the engine raises`, `scripts/test_nac_gate_fails_closed.py::no engine loaded: a governed machine is refused, not admitted`, `scripts/check_mcp_nac_gate.py::a governed device must be refused when no network engine is loaded` | (MCP gate: smoke) The real gate refuses; a token with no device fingerprint and the in-pod loopback are not asked about. |
| An unreadable signing key is read again on each reload, and the lock lifts once it reads. | `scripts/check_nac_engine.py::key: the reload did not re-arm once the key became readable` | (smoke) The next reload re-arms and admits the healthy machine again. |
| Ingress rules apply in the order of their signed priority, not the stored priority column. | `scripts/check_nac_engine.py::priority: swapping the priority COLUMNS reordered enforcement (allow won)` | (smoke) Swapped columns leave the signed order in force. |
| Each time the gate locks, the person is told once, as a notice in the Host's inbox. | `scripts/check_nac_engine.py::lock_tests`, `scripts/test_nac_lock_notice_episodes.py::the lock's return is a second notice the inbox keeps (a new id)` | (smoke) One notice over four reloads; a restart in the same lock reuses its id; a new breakage is told again, and so is a lock that lifted and came back. |
| Each start where no engine loads tells the person, as a notice in the Host's inbox. | `scripts/test_nac_gate_fails_closed.py::it goes to the Host's inbox through handle_inbox_append, as one system_notification`, `scripts/test_nac_gate_fails_closed.py::each start's notice has its own id, so the inbox keeps a later one`, `scripts/test_nac_gate_fails_closed.py::main.py: the NAC init error handler tells the inbox when no engine was built` | The notice through a stand-in inbox, a new id each start, and the start's error handler awaiting it. |
| Revealing a saved key goes through the step-up check. | `scripts/test_stepup_attempt_limit.py::vault reveal: locked` | With the limit reached, reveal answers 429. |
| The Host has no route to reveal a key or restore a backup. | `scripts/test_host_restore.py::is walled from the Host: the user's act alone`, `scripts/test_backup_files_routes.py::is walled, with a reason that says whose act it is` | (restore: smoke) Each restore route is walled from the Host. For reveal: no test; enforced by the code at `brain/mastercode/runtime/kernel_ops.py`. |
| A recurring task that runs a model starts only after the person approves its card. | `scripts/test_scheduled_spend_pg.py::a recurring prompt task files one card locked to the person`, `scripts/test_scheduled_spend_pg.py::the Host cannot let its own recurring task run` | The card is the person's alone; the Host gets 403. |
| Each limit that can stop work has a coded default, and only the person changes it. | `scripts/test_user_limits.py::unit_checks`, `scripts/test_user_limits.py::the Host's limits tool has no path that writes a limit`, `scripts/test_user_limits.py::the Host cannot decide its own card` | Defaults match the enforcing code; the Host's tool writes nothing. |
| The Host cannot answer a connector's pairing card. | `scripts/test_connector_grants.py::the Host cannot answer the approval ticket` | 403 for the Host. |

## The Host's own data (`world`)

All in `scripts/test_host_world_pg.py`, against a real PostgreSQL.

| Claim | Test | What the test checks |
|---|---|---|
| The `world` role writes nowhere else. | `scripts/test_host_world_pg.py::X no write to a kernel table (INSERT entity_memory)`, `scripts/test_host_world_pg.py::X no write to any table outside public and world (AGE graph included)` | Insufficient privilege. |
| It cannot change the person's network rules or the tool log. | `scripts/test_host_world_pg.py::X no UPDATE of the person's network rules (nac_policy)`, `scripts/test_host_world_pg.py::X no DELETE from the tool log (tool_call_log)` | Insufficient privilege. |
| It reads no table holding secrets. | `scripts/test_host_world_pg.py::L host_world reads no SECRET or CLOSED table` | Its grants list no such table. |
| It is no superuser, belongs to no role and can create no extension. | `scripts/test_host_world_pg.py::R no superuser, no RLS bypass, no CREATEDB/CREATEROLE/REPLICATION`, `scripts/test_host_world_pg.py::X no extension` | The role's attributes. |
| A change that could reshape data takes a snapshot first. | `scripts/test_host_world_pg.py::C a change that can reshape data takes a snapshot first` | |

## Folders, machines and the bastion lane

| Claim | Test | What the test checks |
|---|---|---|
| There is no delete operation for a connected folder, and a grant cannot include one. | `scripts/check_dcn_cn1.py::pure_tests` | (smoke) A mask with delete is refused; no delete op exists. |
| The Host cannot draft a shell grant for a machine. | `scripts/test_grant_onboarding.py::the machinery, the shell and the DCN lane are not grantable` | The shell ops are not grantable. |
| The bastion lane accepts only a certificate this Host issued at pairing and has not revoked, and a revoke closes open connections. | `scripts/test_host_owns_bastion.py::a certificate under the Host CA that no redeem issued: refused, nothing reaches the kernel`, `scripts/test_host_owns_bastion.py::the redeemed certificate once its bastion is revoked: refused, nothing reaches the kernel`, `scripts/test_host_owns_bastion.py::a lane connection open when its bastion is revoked is closed by the revoke` | Real TLS connections. |
| A pairing token works once, for 15 minutes. | `tests/test_bastion_redeem_token_burn.py::test_an_already_used_token_is_still_refused` | A second redeem gets 401. The 15 minutes: no test; enforced by the code at `server/network/bastion_enroll.py`. |

## Memory and skills

| Claim | Test | What the test checks |
|---|---|---|
| A newer note on a topic becomes the live one and the older is kept. | `scripts/test_memory_slots_pg.py::S1 A is retired, its pointer on B, both in the slot` | The older row stays, retired, pointing at the newer. |
| A skill the Host writes is checked against a standard before it is saved, and every version is kept. | `scripts/test_host_skills_pg.py::W a text the standard refuses is refused, every broken rule named, nothing written`, `scripts/test_host_skills_pg.py::R history lists every version newest first, author and status each` | |
| A skill written in a turn that read untrusted content is marked untrusted until the person saves it. | `scripts/test_host_skills_pg.py::U a context write in a marked turn stores the mark through the real sandbox`, `scripts/test_host_skills_pg.py::U the person's save of the same text is version 2 with no mark` | |

## Without Privatae, and the network

| Claim | Test | What the test checks |
|---|---|---|
| A Host with no membership lists and installs from the free public App Store catalog. | `scripts/test_public_store.py::a standalone Host's list is the public catalog`, `scripts/test_public_store.py::a standalone Host is entitled to a public app` | |
| A Host with no membership backs up to a local folder. | `scripts/test_standalone_backups.py::a standalone Host that chose nothing: a folder on this machine` | The default destination. |
| Nothing the network sends can run on a member Host. | `tests/test_member_client.py::test_17_register_handler_keeps_nothing_and_inbound_requests_are_unsupported` | A pushed request is answered "unsupported". |
| Through remote access, a browser reaches a member Host only from the door in the guest, never as loopback, and without a session it is sent to sign in. | `scripts/test_remote_door_loopback.py::the door and its certificate job publish no port`, `scripts/test_remote_door_loopback.py::every request the Host received came from the door's address`, `scripts/test_remote_door_loopback.py::without a session a page is sent to /login` | End to end in a private network namespace: browser, reference relay, the Host's tunnel, the real door and the Host's real gates; no request reached the Host from loopback. |
| Only the signed-in owner joins or leaves the network. | `tests/test_member_client.py::test_join_and_leave_are_the_owners_alone` | 403 for every token but a live owner session. |

## Releases

| Claim | Test | What the test checks |
|---|---|---|
| `npx cseries` installs the bastion only when its manifest's signature verifies against a pinned key and the binary matches the manifest's sha256. | `packages/cseries/test/bastion.test.js::a manifest whose signature does not verify downloads and installs nothing`, `packages/cseries/test/bastion.test.js::a binary whose sha256 is not the manifest's is not downloaded into place`, `packages/cseries/test/verify.test.js::a signature by a key that is not pinned is refused, and one that is pinned passes` | |
| The installer refuses a guest image or other download whose sha256 is not the one the release pins, and keeps nothing of it. | `packages/cseries/test/net.test.js::a download with the wrong sha256 is refused and leaves no file behind` | |
| A Host refuses an App Store catalog not signed by a pinned key, and a bundle that does not match the catalog or its signer. | `scripts/test_public_store.py::the test key is not one of the pinned keys`, `scripts/test_public_store.py::a bundle that does not match the signed sha256 is refused`, `scripts/test_public_store.py::a bundle the trust store's keys did not sign is still refused at install` | |
| A catalog more than 45 days old shows a notice. | `scripts/test_public_store.py::one issued 46 days ago still lists, with a notice saying how old it is and what that can mean` | |
| A C-Series Cloud server refuses a release manifest that is unsigned, signed by another key, or signed for another kind. | `scripts/test_cloud_box_release.py::an unsigned manifest is refused`, `scripts/test_cloud_box_release.py::a manifest signed by a key that is not pinned is refused`, `scripts/test_cloud_box_release.py::a manifest signed for the bastion kind is refused` | |
| A new release is created as a pre-release, and the step that makes it GitHub's latest release (the one a C-Series Cloud server and a Host's App Store read) refuses until a signature for its cloud manifest and one for its App Store catalog are on it. | `scripts/test_release_latest_after_signing.py::the release is created as a pre-release, which GitHub never offers as the latest`, `scripts/test_release_latest_after_signing.py::no signature beside cloud-release.json: nothing is changed`, `scripts/test_release_latest_after_signing.py::no signature beside catalog.json: nothing is changed`, `scripts/test_release_latest_after_signing.py::the two signatures swapped, each under the other manifest's name, are refused`, `scripts/test_release_latest_after_signing.py::a signature by a key that is not pinned is refused` | The release workflow creates the release as a pre-release and nothing in it marks it the latest; the script that does, run against a stand-in for gh, changes nothing while either signature file is missing, named for another file, or not a valid signature of that manifest's kind, version and sha256 by a pinned release key. |

## Messaging connectors (store)

| Claim | Test | What the test checks |
|---|---|---|
| A connector's container never holds the bot token; it stays in the Host's Keys. | `scripts/test_connector_discord_example.py::C the app sent Identify with the placeholder, never the token`, `scripts/test_connector_telegram_example.py::L the token is in no log line, no state file, no request to the Host`, `scripts/test_connector_slack_example.py::L no token in any log line, the state file, or a request to the Host` | |

## Claims with no test yet

- That new tools shaped like code runners are caught by name pattern:
  enforced by `brain/mastercode/code_exec_guard.py`, untested.
- That turning on the live copy of memory takes the person's code:
  enforced by `server/network/mirror.py`; only turning it off is tested.
- That functions in `world` cannot reach files or the network: only the
  role's attributes above are tested.
- How traffic leaving through the gate treats an unsigned or changed rule.
- That a member Host opens no listener for the network: tested only
  indirectly, by the Host dialling out.
- The path checks in `server/routes/review.py` and `server/routes/codespace.py`.
