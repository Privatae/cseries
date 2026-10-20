'use strict';

// A stand-in for privatae-bastion in the macOS and Linux install tests, run
// by the sudo script the command builds (through a two-line /bin/sh wrapper,
// which is the "downloaded" program the script copies into place). It answers
// the commands that script runs the way the real bastion answers them, keeps
// its state in $HOME/fake-bastion.json, and appends every call, as one JSON
// line, to $FAKE_BASTION_LOG. Everything here is invented test data.

const fs = require('node:fs');
const path = require('node:path');

// node --test runs every file under test/; outside the install tests this
// file does nothing.
if (!process.env.FAKE_BASTION_LOG) return;

const home = process.env.HOME;
const stateFile = path.join(home, 'fake-bastion.json');
const st = fs.existsSync(stateFile)
  ? JSON.parse(fs.readFileSync(stateFile, 'utf8'))
  : { service: false, mode: null, pairings: {}, anchor: null, enrolled: {} };
const args = process.argv.slice(2);
if (process.env.FAKE_BASTION_LOG) {
  fs.appendFileSync(process.env.FAKE_BASTION_LOG, `${JSON.stringify({ args, home, xdgConfigHome: process.env.XDG_CONFIG_HOME || null, stdinTTY: Boolean(process.stdin.isTTY) })}\n`);
}

function done(code, text) {
  fs.writeFileSync(stateFile, JSON.stringify(st, null, 2));
  if (text) process.stdout.write(`${text}\n`);
  process.exit(code);
}

if (process.env.FAKE_BASTION_FAIL === args[0]) done(1, `error: ${args[0]} failed (test)`);

const hostName = process.env.FAKE_HOST_NAME || 'host-a';
switch (args[0]) {
  case 'version':
    done(0, 'privatae-bastion 0.53.2');
    break;
  case 'service':
    st.service = true;
    done(0, 'System service installed: launchd daemon ai.privatae.bastion, running it as root.');
    break;
  case 'set-mode':
    st.mode = args[1];
    done(0, `mode set to ${args[1]}`);
    break;
  case 'release-anchor': {
    const h = args[args.indexOf('--host') + 1];
    const ca = fs.readFileSync(args[args.indexOf('--host-ca') + 1], 'utf8');
    if (st.pairings[h] && st.pairings[h].trim() === ca.trim()) {
      done(1, `error: the CA given with --host-ca is the one ${h} is paired with, so that Host is not gone — nothing was released`);
    }
    st.anchor = null;
    delete st.pairings[h];
    delete st.enrolled[h];
    done(0, `released ${h}`);
    break;
  }
  case 'pair': {
    // As bastion 0.53.2 reads it (pair_stdin.go): `-` in place of the token
    // reads its first line from standard input; no token is the usage error.
    // A token as an argument the real one takes; this one refuses it, since
    // the install must never send one.
    const rest = [];
    for (let i = 1; i < args.length; i++) {
      if (args[i] === '--redeem-url' || args[i] === '--host-ca') i++;
      else if (args[i] !== '--replace') rest.push(args[i]);
    }
    if (rest.length !== 1) done(2, 'usage: privatae-bastion pair [...] <token> | -');
    if (rest[0] !== '-') done(2, 'fake: the token must come on standard input, with -');
    // While pair runs, every process on this machine whose command line holds
    // FAKE_ARGV_CANARY (Linux's /proc only): the script around it included.
    if (process.env.FAKE_ARGV_CANARY && fs.existsSync('/proc/self/cmdline')) {
      st.cmdlineHits = fs.readdirSync('/proc').filter((d) => /^\d+$/.test(d)).filter((d) => {
        try {
          return fs.readFileSync(`/proc/${d}/cmdline`).includes(process.env.FAKE_ARGV_CANARY);
        } catch {
          return false;
        }
      });
      st.cmdlineScanned = true;
    }
    const token = (fs.readFileSync(0, 'utf8').split('\n')[0] || '').trim();
    if (!token) done(2, 'standard input held no pairing token');
    if (st.pairings[hostName]) done(1, `error: already paired with ${hostName}`);
    st.pairings[hostName] = fs.readFileSync(args[args.indexOf('--host-ca') + 1], 'utf8');
    st.token = token;
    st.redeem = args[args.indexOf('--redeem-url') + 1];
    done(0, `Paired with ${hostName} over TCP.\n\n  lanes: 127.0.0.1:8811`);
    break;
  }
  case 'claim-anchor': {
    if (st.anchor) done(1, `error: this box is already anchored to ${st.anchor}`);
    const names = Object.keys(st.pairings);
    if (names.length !== 1) done(1, `error: this box serves ${names.length} hosts`);
    st.anchor = names[0];
    done(0, `anchored to ${names[0]}`);
    break;
  }
  case 'stepup-enroll': {
    // The install sends --console-only on every OS; 0.53.2 takes it as root
    // on Linux too (stepup.go enrolServiceStateRefusal).
    if (!args.includes('--console-only')) done(2, 'not on a console');
    // The "terminal": the test's stand-in file. The code typed back is read
    // from it, and the secret is written to it, never to the captured output.
    const code = fs.readFileSync(0, 'utf8').trim();
    process.stdout.write('Add this second factor to your authenticator app: FAKESECRETNOTREAL\n');
    if (code !== '123456') done(1, 'that code did not match — nothing enrolled');
    st.enrolled[args[args.indexOf('--host') + 1]] = true;
    done(0, '');
    break;
  }
  default:
    done(1, `unknown ${args[0]}`);
}
