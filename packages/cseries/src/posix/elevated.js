'use strict';

// The one sudo run that makes the bastion this Mac's or this Linux machine's
// system service, paired and anchored to the Host in the VM, with the
// person's authenticator enrolled: the macOS and Linux counterpart of
// win/elevated.js, doing the same steps in the same order with the same exit
// codes (bastion-commands.js EXIT) and the same log lines.
//
// HOW IT IS RUN. `sudo -p <prompt> /bin/sh -s`, with this script on sudo's
// standard input. So the script, and the pairing token in it, is in no
// process's command line and in no file the person's account could change
// between the password and the run. sudo is run by its absolute path
// (machine.js SUDO), and the script sets PATH and HOME itself.
//
// THE PAIRING TOKEN goes on to `privatae-bastion pair -` the same way: the
// shell's own printf, a builtin, writes it into a pipe that is pair's
// standard input. A builtin runs inside the shell, so the token is in no
// process's argv, and a pipe is not a file anyone can open. The script
// checks that printf IS a builtin here before it pairs, and stops if not.
//
// WHAT IT REPORTS. Its standard output is captured: `run=<nonce>` first, then
// the same key=value lines the Windows script writes to its log, which
// win/elevated.js parseInstallLog reads. A bastion command's own output is
// passed on with "> " in front, so no line of it can be taken for a result.
//
// THE AUTHENTICATOR is enrolled by a second sudo run of its own
// (enrolScript), after this one, with the person's terminal as its standard
// input, output and errors, never inside this script. With `Defaults use_pty`
// (the stock sudoers of Debian and Ubuntu, and of sudo-rs, Ubuntu's sudo from
// 25.10) sudo runs its command on a pseudo-terminal of its own, and sudo-rs
// passes the person's keystrokes on to it only when sudo's own standard input
// is their terminal. This script is on sudo's standard input, a pipe, so a
// stepup-enroll attached to /dev/tty in here waited on a terminal nobody could
// type into: the install hung at "Enter a code from the app to confirm"
// (Ubuntu 26.04, sudo-rs 0.2.13, 2026-10-22). stepup-enroll runs with
// --console-only on every OS, so the bastion itself refuses to show the
// secret anywhere but a terminal, and its output is never captured.
//
// WHERE THINGS GO, as the bastion's own install.sh puts them:
//   macOS  the program in /Library/Application Support/privatae/bin, that
//          folder and its parent root:wheel 755, `service install` (launchd's
//          daemon ai.privatae.bastion), state under HOME=/var/root, and the
//          privatae-bastion link in /usr/local/bin install.sh makes;
//   Linux  the program in /usr/local/bin and install.sh's systemd unit,
//          since `service install` does nothing on Linux, state under
//          HOME=/root.
// The Host CA and the downloaded program are copied into folders only root
// can write and checked there, after the copy, against the sha256 the
// unelevated side recorded.

const { EXIT, HOST } = require('../bastion-commands');

const CA_VAR = '\u0000ca';
const LAUNCHD_LABEL = 'ai.privatae.bastion';
const UNIT = '/etc/systemd/system/privatae-bastion.service';

function shq(s) {
  return `'${String(s).replace(/'/g, `'\\''`)}'`;
}

function arg(a) {
  if (a === CA_VAR) return '"$ca"';
  if (a === HOST) return '"$hostName"';
  return shq(a);
}

function preamble(p) {
  const darwin = p.platform === 'darwin';
  return [
    'set -u',
    'umask 022',
    'PATH=/usr/sbin:/usr/bin:/sbin:/bin',
    `HOME=${darwin ? '/var/root' : '/root'}`,
    'export PATH HOME',
    // The bastion finds its state with os.UserConfigDir, which on Linux reads
    // XDG_CONFIG_HOME before HOME. sudo drops it by default, but a sudoers
    // env_keep or root's pam_env can pass it on, and then the pairing and the
    // authenticator's secret would go wherever it points instead of the state
    // the service reads (HOME only, as its unit sets). On Linux the
    // bastion's --console-only check asks only whether it runs as root, so
    // it does not catch this itself.
    'unset XDG_CONFIG_HOME XDG_DATA_HOME XDG_STATE_HOME XDG_CACHE_HOME TMPDIR',
    `exe=${shq(p.program)}`,
    'dir=$(dirname "$exe")',
    `hostName=${shq(p.host || '')}`,
    'ca=""',
    'tmp=""',
    'text=""',
    'emit() { printf \'%s\\n\' "$1"; }',
    'quit() { emit "error=$(printf \'%s\' "$2" | tr \'\\n\' \' \')"; emit "exit=$1"; [ -z "$tmp" ] || rm -rf "$tmp"; exit "$1"; }',
    darwin ? 'sha() { shasum -a 256 "$1" | cut -d" " -f1; }' : 'sha() { sha256sum "$1" | cut -d" " -f1; }',
    // A bastion command: its name, then its output marked, never its args.
    'bastion() {',
    '  c=$1; w=$2; shift 2',
    '  emit "step=$w"',
    '  emit "> privatae-bastion $w"',
    '  text=$("$exe" "$@" </dev/null 2>&1); rc=$?',
    '  [ -z "$text" ] || printf \'%s\\n\' "$text" | sed \'s/^/> /\'',
    '  [ "$rc" -eq 0 ] || quit "$c" "privatae-bastion $w exited with $rc"',
    '}',
    // The same, with $3 written to the command's standard input by the
    // builtin printf (see THE PAIRING TOKEN above), and never in its args.
    'bastion_in() {',
    '  c=$1; w=$2; input=$3; shift 3',
    '  emit "step=$w"',
    '  emit "> privatae-bastion $w"',
    '  case $(command -V printf 2>/dev/null) in *builtin*) ;; *) quit "$c" "this /bin/sh has no builtin printf, so the pairing token cannot reach privatae-bastion $w without a command line" ;; esac',
    '  text=$(printf \'%s\\n\' "$input" | "$exe" "$@" 2>&1); rc=$?',
    '  input=""',
    '  [ -z "$text" ] || printf \'%s\\n\' "$text" | sed \'s/^/> /\'',
    '  [ "$rc" -eq 0 ] || quit "$c" "privatae-bastion $w exited with $rc"',
    '}',
    `emit ${shq(`run=${p.nonce}`)}`,
  ];
}

function installBlock(p) {
  const darwin = p.platform === 'darwin';
  const owner = darwin ? 'root:wheel' : 'root:root';
  const lines = ["emit 'step=install'"];
  if (darwin) {
    lines.push(
      `mkdir -p "$dir" || quit ${EXIT.install} "cannot make $dir"`,
      `chown ${owner} "$(dirname "$dir")" "$dir" && chmod 755 "$(dirname "$dir")" "$dir" || quit ${EXIT.install} "cannot make $dir root's alone"`,
    );
  } else {
    lines.push(`mkdir -p "$dir" || quit ${EXIT.install} "cannot make $dir"`);
  }
  lines.push(
    // Root's copy is root's alone (700) until its hash is checked: the source
    // is in the person's downloads folder, and could have been swapped for a
    // link to any file root can read while sudo waited for the password. Only
    // the verified program is made readable and runnable by all.
    `install -o ${owner.split(':')[0]} -g ${owner.split(':')[1]} -m 700 ${shq(p.install.file)} "$exe.new" || quit ${EXIT.install} "cannot copy the bastion into $dir"`,
    'h=$(sha "$exe.new")',
    `[ "$h" = ${shq(p.install.sha256)} ] || { rm -f "$exe.new"; quit ${EXIT.binaryHash} "the program copied into $dir has sha256 $h, not ${p.install.sha256}"; }`,
    `chmod 755 "$exe.new" || { rm -f "$exe.new"; quit ${EXIT.install} "cannot make the bastion runnable in $dir"; }`,
    `mv -f "$exe.new" "$exe" || quit ${EXIT.install} "cannot put the bastion in place in $dir"`,
  );
  if (darwin) {
    // install.sh's link for the name people type; -n replaces a link rather
    // than following it into a folder.
    lines.push('mkdir -p /usr/local/bin && ln -sfn "$exe" /usr/local/bin/privatae-bastion || true');
  }
  lines.push('emit "installed=$exe"');
  return lines;
}

function serviceBlock(p) {
  if (p.platform === 'darwin') {
    return [`bastion ${EXIT.serviceInstall} 'service install' service install`, "emit 'service=yes'"];
  }
  return [
    "emit 'step=service install'",
    `[ -d /run/systemd/system ] || quit ${EXIT.serviceInstall} "this Linux does not run systemd, which the bastion's service needs"`,
    `cat > ${UNIT}.new <<'CSERIES_UNIT'`,
    p.unit.replace(/\n$/, ''),
    'CSERIES_UNIT',
    `chmod 644 ${UNIT}.new && mv -f ${UNIT}.new ${UNIT} || quit ${EXIT.serviceInstall} "cannot write ${UNIT}"`,
    `systemctl daemon-reload || quit ${EXIT.serviceInstall} "systemctl daemon-reload failed"`,
    `systemctl enable privatae-bastion.service >/dev/null 2>&1 || quit ${EXIT.serviceInstall} "systemctl enable privatae-bastion.service failed"`,
    "emit 'service=yes'",
  ];
}

function hostCaBlock(p) {
  return [
    "emit 'step=host-ca'",
    `tmp=$(mktemp -d "$HOME/cseries-ca.XXXXXX") || quit ${EXIT.hostCa} "cannot make a folder for the Host CA"`,
    'ca="$tmp/host-ca.pem"',
    `cp ${shq(p.hostCa.file)} "$ca" || quit ${EXIT.hostCa} "cannot copy the Host CA"`,
    'h=$(sha "$ca")',
    `[ "$h" = ${shq(p.hostCa.sha256)} ] || quit ${EXIT.hostCa} "the Host CA copied for the pairing has sha256 $h, not ${p.hostCa.sha256}"`,
  ];
}

function commandBlock(c) {
  // The authenticator's enrolment is not in this script: enrolScript.
  if (c.console) return [];
  const lines = c.stdin === undefined
    ? [`bastion ${c.code} ${shq(c.what)} ${c.args.map(arg).join(' ')}`]
    : [`bastion_in ${c.code} ${shq(c.what)} ${shq(c.stdin)} ${c.args.map(arg).join(' ')}`];
  if (c.pair) {
    lines.push(
      'rm -f "$ca"',
      'hostName=$(printf \'%s\\n\' "$text" | sed -n \'s/.*Paired with \\([^ ]*\\).*/\\1/p\' | head -n 1)',
      'hostName=${hostName%.}',
      `[ -n "$hostName" ] || quit ${c.code} 'pair did not say which Host it paired with'`,
      'emit "paired_host=$hostName"',
    );
  } else if (c.args[0] === 'release-anchor') {
    lines.push(`emit ${shq(`released=${c.args[2]}`)}`);
  } else if (c.args[0] === 'claim-anchor') {
    lines.push("emit 'anchored=yes'");
  }
  return lines;
}

function restartBlock(p) {
  const cmd = p.platform === 'darwin'
    ? `launchctl kickstart -k system/${LAUNCHD_LABEL}`
    : 'systemctl restart privatae-bastion';
  return [
    "emit 'step=restart'",
    `${cmd} || quit ${EXIT.restart} "${cmd} failed"`,
    '[ -z "$tmp" ] || rm -rf "$tmp"',
    "emit 'result=ok'",
    'exit 0',
  ];
}

// p: {platform, nonce, program, unit (Linux), install: {file, sha256}|null,
//     serviceInstall, hostCa: {file, sha256}|null, host,
//     commands: bastionCommands(plan, CA_VAR)}
// The service is restarted at the end, before the authenticator's own run:
// the bastion reads its second factor from its state file on every check
// (client-go stepup.go stepupEnrolled, stepupLoad), so the running service
// sees a factor enrolled after it started.
function bastionInstallScript(p) {
  const body = [...preamble(p)];
  if (p.install) body.push(...installBlock(p));
  if (p.serviceInstall) body.push(...serviceBlock(p));
  if (p.hostCa) body.push(...hostCaBlock(p));
  for (const c of p.commands) body.push(...commandBlock(c));
  body.push(...restartBlock(p));
  return `${body.join('\n')}\n`;
}

// The authenticator's enrolment, its own sudo run:
//   sudo -p <prompt> /bin/sh -c <this> cseries-enrol <program> <host> <args>...
// with the person's terminal as all three of its streams (bastion.js). The
// program and the Host's name come as arguments, never inside the script, so
// nothing in them is read by the shell. The same root environment as the
// install script: HOME as the service's unit sets it, and no XDG_* variable
// that would move the bastion's state elsewhere.
function enrolScript(p) {
  return [
    'set -u',
    'umask 022',
    'PATH=/usr/sbin:/usr/bin:/sbin:/bin',
    `HOME=${p.platform === 'darwin' ? '/var/root' : '/root'}`,
    'export PATH HOME',
    'unset XDG_CONFIG_HOME XDG_DATA_HOME XDG_STATE_HOME XDG_CACHE_HOME TMPDIR',
    'exe=$1',
    'shift',
    'exec "$exe" "$@"',
    '',
  ].join('\n');
}

// enrolScript's argument list after the script: $0, the program, then the
// command's own arguments with the Host's name in place of HOST.
function enrolArgs(program, command, host) {
  return ['cseries-enrol', program, ...command.args.map((a) => (a === HOST ? host : a))];
}

module.exports = { bastionInstallScript, enrolScript, enrolArgs, CA_VAR, LAUNCHD_LABEL, UNIT, shq };
