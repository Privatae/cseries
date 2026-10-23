'use strict';

// The machine wall in the guest: no container in the C-Series Linux system
// (the Host, its database, and the apps, browser and coding engines the local
// runtime starts) opens a connection to this machine. Four files in the guest
// image do it (vm/wsl/rootfs, which the Lima image copies too):
//
//   /etc/cseries-machine/machine.nft      the rules, a table of their own
//   /usr/local/sbin/cseries-machine-firewall   loads them with this boot's
//                                          gateways and resolvers
//   cseries-machine-firewall.service      loads them before any container
//   cseries-host.container.d/machine-firewall.conf   the Host requires it
//
// A guest made from this release's image has them from its first boot. A
// guest made from an older one is kept by `npx cseries update`, which updates
// the Host's images and not the guest, so on every run (host.js step, and
// `npx cseries start`) this writes the same four files into the guest, as
// root, from the copies in guest/ (test/machine-firewall.test.js holds them to
// the image's, byte for byte), with /etc/cseries-machine/addresses: the
// addresses this machine has now, and on Lima the user-mode network, where
// 192.168.5.2 is this machine's own loopback. Then it loads the wall and
// checks that it is there; a guest where it cannot be loaded stops the run,
// since the Host's unit requires it, and a Host an older guest started at
// boot, before any wall, is stopped when the run ends with no wall loaded.
//
// What stays open, and why nothing else needs to: see machine.nft's header.

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { CliError } = require('../errors');

const GUEST_FILES = [
  { src: 'machine.nft', dest: '/etc/cseries-machine/machine.nft', mode: '0644' },
  { src: 'cseries-machine-firewall', dest: '/usr/local/sbin/cseries-machine-firewall', mode: '0755' },
  { src: 'cseries-machine-firewall.service', dest: '/etc/systemd/system/cseries-machine-firewall.service', mode: '0644' },
  { src: 'machine-firewall.conf', dest: '/etc/containers/systemd/cseries-host.container.d/machine-firewall.conf', mode: '0644' },
];
const ADDRESSES = '/etc/cseries-machine/addresses';
const UNIT = 'cseries-machine-firewall.service';
const EOF_MARK = 'CSERIES_MACHINE_EOF';

// Lima's user-mode network (QEMU's on Linux, Lima's own on macOS): the guest
// is 192.168.5.15, 192.168.5.2 is this machine's loopback, 192.168.5.3 the
// DNS server Lima answers on this machine. The guest image's own copy of the
// file starts with the same two lines (vm/lima/rootfs/etc/cseries-machine/
// addresses).
const LIMA_LINES = ['block 192.168.5.0/24', 'dns 192.168.5.3'];

const V4 = /^(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}$/;
const V6 = /^[0-9a-f:.]{2,45}$/i;

function guestFiles() {
  return GUEST_FILES.map((f) => ({ ...f, content: fs.readFileSync(path.join(__dirname, 'guest', f.src), 'utf8') }));
}

// The addresses this machine answers on, from os.networkInterfaces(): every
// interface's, a public one included, except those machine.nft walls off
// whatever they are (loopback, link-local). Sorted, each once.
function machineAddresses(ifaces) {
  const out = new Set();
  for (const list of Object.values(ifaces || {})) {
    for (const a of list || []) {
      if (!a || a.internal || typeof a.address !== 'string') continue;
      const family = a.family === 4 || a.family === 'IPv4' ? 4 : a.family === 6 || a.family === 'IPv6' ? 6 : 0;
      const addr = a.address.split('%')[0].toLowerCase();
      if (family === 4 && V4.test(addr)) {
        if (/^(127|0)\./.test(addr) || /^169\.254\./.test(addr)) continue;
        out.add(addr);
      } else if (family === 6 && V6.test(addr) && addr.includes(':')) {
        if (addr === '::1' || addr === '::' || /^fe[89ab]/.test(addr)) continue;
        out.add(addr);
      }
    }
  }
  return [...out].sort();
}

// /etc/cseries-machine/addresses for this driver and these addresses. The
// same machine gives the same file, so a run with nothing new writes the same
// bytes.
function addressesFile(driverName, addrs) {
  const lines = [
    '# The machine, as this Linux system reaches it (read by cseries-machine-firewall).',
    '# Written by the cseries command on the machine, on every run.',
  ];
  if (driverName === 'lima') lines.push(...LIMA_LINES);
  for (const a of addrs) lines.push(`block ${a}`);
  return `${lines.join('\n')}\n`;
}

function heredoc(content) {
  const body = content.endsWith('\n') ? content : `${content}\n`;
  if (body.split('\n').includes(EOF_MARK)) throw new Error(`a guest file holds the line ${EOF_MARK}`);
  return `${body}${EOF_MARK}\n`;
}

// The script the guest runs as root, on its standard input: writes the files
// (each through a .new file and a rename), loads the wall and says
// machine_firewall=loaded once nft lists the table.
function installScript(files, addresses) {
  const lines = [
    'set -eu',
    'umask 022',
    'PATH=/usr/sbin:/usr/bin:/sbin:/bin',
    // As cseries-guest does: a command WSL's exec channel left in the root
    // of the guest's cgroup tree would stop systemd enabling controllers.
    'if [ "$(cat /proc/self/cgroup 2>/dev/null)" = "0::/" ] && [ -d /sys/fs/cgroup/init.scope ]; then',
    '    echo $$ > /sys/fs/cgroup/init.scope/cgroup.procs 2>/dev/null || true',
    'fi',
    'R=${CSERIES_GUEST_ROOT:-}',
    // Fail closed. A guest from an older release started its Host at boot,
    // before any wall: when this run then ends with no wall loaded, for
    // whatever reason, the Host is stopped rather than left running without
    // one. A wall loaded before stays in place when a reload fails, and then
    // the Host is left as it is.
    'wall_or_stop() {',
    '    nft list table inet cseries_machine >/dev/null 2>&1 && return 0',
    '    systemctl stop cseries-host.service >/dev/null 2>&1 || true',
    '    echo "host=stopped"',
    '}',
    'trap \'rc=$?; [ "$rc" -eq 0 ] || wall_or_stop; exit "$rc"\' EXIT',
    'put() {',
    '    mkdir -p "$(dirname "$R$1")"',
    '    cat > "$R$1.new"',
    '    chmod "$2" "$R$1.new"',
    '    mv -f "$R$1.new" "$R$1"',
    '}',
  ];
  let script = `${lines.join('\n')}\n`;
  for (const f of files) script += `put ${f.dest} ${f.mode} <<'${EOF_MARK}'\n${heredoc(f.content)}`;
  script += `put ${ADDRESSES} 0644 <<'${EOF_MARK}'\n${heredoc(addresses)}`;
  script += [
    'systemctl daemon-reload',
    `systemctl enable ${UNIT} >/dev/null 2>&1 || { echo "machine_firewall=not-enabled"; exit 1; }`,
    // Reload, never restart, a running wall: the Host's unit requires it,
    // and systemd restarts a unit whose requirement is restarted. A guest
    // where it is not running yet (an older one) starts it; a reload nft
    // refuses leaves the rules loaded before in place.
    `if ! systemctl reload-or-restart ${UNIT}; then`,
    `    journalctl -u ${UNIT} -n 20 --no-pager -o cat >&2 || true`,
    '    echo "machine_firewall=failed"',
    '    exit 1',
    'fi',
    'nft list table inet cseries_machine >/dev/null || { echo "machine_firewall=missing"; exit 1; }',
    'echo "machine_firewall=loaded"',
    '',
  ].join('\n');
  return script;
}

function tail(s, n = 8) {
  return String(s).trim().split('\n').slice(-n).join('\n');
}

async function apply(ctx) {
  const ifaces = ctx.netInterfaces ? ctx.netInterfaces() : os.networkInterfaces();
  const addrs = machineAddresses(ifaces);
  const script = installScript(guestFiles(), addressesFile(ctx.driver.name, addrs));
  const r = await ctx.driver.guestShell(ctx, script);
  if (r.code !== 0 || !/^machine_firewall=loaded$/m.test(r.stdout)) {
    const stopped = /^host=stopped$/m.test(r.stdout);
    const why = tail(`${r.stderr}\n${r.stdout.replace(/^host=stopped\n?/m, '')}`);
    throw new CliError(`The C-Series Linux system's firewall, which keeps every container in it off this machine, could not be put in place, so the Host is ${stopped ? 'stopped' : 'not started'}${why ? `:\n${why}` : '.'}`,
      'Run npx cseries again. If it fails the same way, please report it with the lines above.');
  }
  return { addresses: addrs };
}

module.exports = { apply, machineAddresses, addressesFile, installScript, guestFiles, GUEST_FILES, ADDRESSES, LIMA_LINES, EOF_MARK, UNIT };
