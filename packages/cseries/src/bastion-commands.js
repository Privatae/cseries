'use strict';

// The bastion commands the one elevated step runs, in order. The same list on
// every OS: Windows renders it into its elevated PowerShell script
// (win/elevated.js), and macOS and Linux into the script they run under sudo
// (bastion.js). Each entry has the exit code the elevated step ends with when
// that command fails.
//
//   approvals-owner --sid <SID> Windows: the account that ran this command is
//                               the only one whose tray answers approvals.
//                               First, so a run from another account stops
//                               here, before anything is paired or enrolled:
//                               the bastion refuses a second account and the
//                               first keeps its approvals. Never --replace.
//   set-mode networked          the service's state, once (idempotent)
//   release-anchor --host <h> --host-ca <copy>
//                               only when the Host this command paired before
//                               has a new CA (its data was deleted): drops
//                               the dead anchor, unpairs it and sets its key
//                               pin and factor aside, so the new Host pairs
//                               fresh. The bastion itself refuses when the CA
//                               given is the one it pinned (the same Host,
//                               alive), so a wrong state.json cannot drop a
//                               live anchor. Never --force. 0.53 refuses
//                               `pair --replace` across a CA change, so a
//                               rotation is no longer used.
//   pair --redeem-url --host-ca <copy> -
//                               the Host CA from the copy only an
//                               administrator can write; the token on pair's
//                               standard input (`-`, bastion 0.53.1 and
//                               later), never in its arguments, where any
//                               account could read it from the process list
//   claim-anchor                no token: the box has exactly one pairing
//   stepup-enroll --console-only --host <h>
//                               the person's authenticator, enrolled on the
//                               elevated console and nowhere else

const HOST = '{host}';

// In place of pair's token: read it from standard input.
const PAIR_STDIN = '-';

const EXIT = {
  binaryHash: 10,
  install: 11,
  serviceInstall: 12,
  setMode: 13,
  hostCa: 14,
  release: 15,
  pair: 16,
  anchor: 17,
  enroll: 18,
  restart: 19,
  owner: 20,
};

// What each exit code means, for the person reading the failure.
const EXIT_WORDS = {
  1: 'the elevated step stopped on an error it did not expect',
  [EXIT.binaryHash]: 'the bastion copied into Program Files is not the verified download',
  [EXIT.install]: 'the bastion could not be put in place, or the earlier per-user install not removed',
  [EXIT.serviceInstall]: 'the bastion could not be registered as the system service',
  [EXIT.setMode]: 'the bastion could not be set to networked mode',
  [EXIT.hostCa]: 'the Host CA copied for the pairing is not the one the Host handed over',
  [EXIT.release]: 'the bastion could not release the anchor of the Host it was paired with before',
  [EXIT.pair]: 'the bastion could not pair with the Host',
  [EXIT.anchor]: 'the bastion could not anchor itself to the Host',
  [EXIT.enroll]: 'the authenticator was not enrolled',
  [EXIT.restart]: 'the bastion service could not be restarted',
  [EXIT.owner]: 'this machine\'s approvals belong to another Windows account, which keeps them (an administrator hands them over with privatae-bastion approvals-owner --replace)',
};

// plan: {owner: SID|undefined, release: host|null, pair: {enroll, token}|null,
// anchor, enroll}. caFile is where the elevated step keeps its copy of the
// Host CA. An entry's args are its command line; stdin, when present, is what
// the elevated step writes to the command's standard input, and the only
// place the pairing token goes.
function bastionCommands(plan, caFile) {
  const out = [];
  if (plan.owner) out.push({ code: EXIT.owner, what: 'approvals-owner', args: ['approvals-owner', '--sid', plan.owner] });
  out.push({ code: EXIT.setMode, what: 'set-mode networked', args: ['set-mode', 'networked'] });
  if (plan.release) {
    out.push({ code: EXIT.release, what: 'release-anchor', args: ['release-anchor', '--host', plan.release, '--host-ca', caFile] });
  }
  if (plan.pair) {
    out.push({ code: EXIT.pair, what: 'pair', args: ['pair', '--redeem-url', plan.pair.enroll, '--host-ca', caFile, PAIR_STDIN], stdin: plan.pair.token, pair: true });
  }
  if (plan.anchor) out.push({ code: EXIT.anchor, what: 'claim-anchor', args: ['claim-anchor'] });
  if (plan.enroll) {
    out.push({ code: EXIT.enroll, what: 'stepup-enroll', args: ['stepup-enroll', '--console-only', '--host', HOST], console: true });
  }
  return out;
}

module.exports = { bastionCommands, EXIT, EXIT_WORDS, HOST, PAIR_STDIN };
