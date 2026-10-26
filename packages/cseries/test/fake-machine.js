'use strict';

// A simulated Windows machine for the flow tests: wsl.exe (answering in
// UTF-16LE, as it does), the guest's cseries-guest behind it, PowerShell
// (registry, services, UAC), the bastion's elevated install script (decoded
// from the stdin of the PowerShell that asks for UAC, and acted out step by
// step, writing the log the real one writes), the bastion binary and the
// network. It keeps state, so a second run sees what the first one did.
//
// Everything here is invented test data.

const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { CliError } = require('../src/errors');
const { fakeRunner, psScript, fixture, guestShell } = require('./helpers');
const { statement, sha256Hex } = require('../src/verify');

const u16 = (s) => Buffer.from(s, 'utf16le');

function signer() {
  const { publicKey, privateKey } = crypto.generateKeyPairSync('ed25519');
  const raw = publicKey.export({ format: 'der', type: 'spki' }).subarray(12).toString('base64');
  return {
    key: raw,
    sign(kind, body) {
      const m = JSON.parse(body.toString('utf8'));
      return crypto.sign(null, statement(kind, m.version, sha256Hex(body)), privateKey).toString('base64');
    },
  };
}

// opts: {wsl: bool, uac: 'yes'|'no', distro: bool, bastionVersion,
//        hostCa, foreignService, failAt: '<bastion command>',
//        virt: {hypervisor, firmware},
//        cpus: the guest's processor count (default 8; null is a guest image
//        from before `status` reported it, whose configure refuses the key),
//        memKb: the guest's MemTotal in kB (default 8 GiB; null as for cpus,
//        and the default when cpus is null),
//        runtime: the guest carries the local runtime, so its status names it
//        (default false: a guest image from before it did),
//        door: the guest knows the remote-access door, so its status names it
//        and its configure takes door= (default false: a guest image from
//        before it did, whose configure refuses the key),
//        tokenPrefix (the pairing tokens minted are <it>-1, <it>-2, ...)}
function fakeMachine(paths, opts = {}) {
  const m = {
    // The guest's root shell: the machine wall's install script runs here.
    guestShell: guestShell(paths.base, { restartFails: opts.wallFails }),
    cpus: opts.cpus === undefined ? 8 : opts.cpus,
    memKb: opts.memKb !== undefined ? opts.memKb : (opts.cpus === null ? null : 8 * 1024 * 1024),
    runtime: opts.runtime === true,
    door: opts.door === true,
    wsl: opts.wsl !== false,
    distros: opts.distro ? ['cseries'] : [],
    running: false,
    images: null,
    hostStarted: false,
    hostCa: opts.hostCa || '-----BEGIN CERTIFICATE-----\nMIIBexample0\n-----END CERTIFICATE-----',
    tokens: 0,
    registry: {},
    tasks: new Set(),
    pairCalls: [],
    // What pair read on its standard input.
    pairTokens: [],
    // The bastion as the elevated script leaves it.
    service: opts.foreignService ? { running: true } : null,
    serviceInstalls: 0,
    serviceRestarts: 0,
    bastion: { mode: null, pairings: {}, anchor: null, enrolled: {}, owner: opts.bastionOwner || null },
    bastionScripts: [],
    bastionCommands: [],
    releases: [],
    enrolments: 0,
    hostName: opts.hostName || 'host-a',
    failAt: opts.failAt || null,
    paths,
    sid: 'S-1-5-21-1111111111-2222222222-3333333333-1001',
    imports: [],
    elevated: [],
    shutdowns: 0,
    bastionVersion: opts.bastionVersion || '0.53.2',
    pulls: 0,
    restarts: 0,
  };
  const s = signer();
  const binary = Buffer.from(`fake bastion ${m.bastionVersion}\n`);
  const manifest = Buffer.from(JSON.stringify({
    version: m.bastionVersion,
    binaries: { 'windows-amd64.exe': { url: 'https://dl.downloads.example/privatae-bastion-windows-amd64.exe', sha256: sha256Hex(binary) } },
  }, null, 2));
  const rootfs = Buffer.from('fake rootfs tarball');
  m.rootfsSha256 = sha256Hex(rootfs);
  m.bastionKeys = [s.key];

  const files = {
    'https://privatae.ai/bastion/version.json': manifest,
    [`https://privatae.ai/bastion/version.json.${sha256Hex(manifest).slice(0, 16)}.sig`]: Buffer.from(s.sign('bastion', manifest)),
    'https://dl.downloads.example/privatae-bastion-windows-amd64.exe': binary,
  };
  m.serve = (url, bytes) => { files[url] = bytes; };
  m.rootfs = rootfs;

  function guest(sub, input) {
    if (!m.distros.includes('cseries')) return { code: 1, stderr: u16('There is no distribution with the supplied name.\r\nError code: Wsl/Service/WSL_E_DISTRO_NOT_FOUND\r\n') };
    m.running = true;
    switch (sub[0]) {
      case 'wait-boot': return { stdout: 'system=running\n' };
      case 'configure': {
        if (m.cpus === null && /^cpu_quota=/m.test(input)) return { code: 1, stderr: 'configure: unknown key cpu_quota\n' };
        if (m.memKb === null && /^memory_max=/m.test(input)) return { code: 1, stderr: 'configure: unknown key memory_max\n' };
        if (!m.door && /^door=/m.test(input)) return { code: 1, stderr: 'configure: unknown key door\n' };
        const changed = input !== m.images;
        m.images = input;
        return { stdout: `changed=${changed ? 1 : 0}\n` };
      }
      case 'pull': m.pulls++; return {};
      case 'start': m.hostStarted = true; return {};
      case 'restart': m.hostStarted = true; m.restarts++; return {};
      case 'stop': m.hostStarted = false; return {};
      case 'health': return { code: m.hostStarted ? 0 : 1 };
      case 'status': return { stdout: `guest=1.2.3\nrelease=1.2.3\nhost=${m.hostStarted ? 'active' : 'inactive'}\npostgres=active\nhealth=ok\n${m.cpus === null ? '' : `cpus=${m.cpus}\n`}${m.memKb === null ? '' : `mem_total_kb=${m.memKb}\n`}${m.runtime ? 'runtime=active\n' : ''}${m.door ? 'door=inactive\nremote_cert=inactive\n' : ''}` };
      case 'host-log': return { stdout: 'boot\nCSERIES SETUP CODE: 111111-222222-333333 old\nCSERIES SETUP CODE: abc123-def456-0789ab — open /setup\n' };
      case 'pair-token':
        m.tokens++;
        return { stdout: JSON.stringify({ token: `${opts.tokenPrefix || 'tok'}-${m.tokens}`, host_ca_pem: m.hostCa, enroll: 'https://127.0.0.1:8810/v1/local/redeem' }) };
      case 'logs': return { stdout: 'log line\n' };
      default: return { code: 1, stderr: `unknown ${sub[0]}` };
    }
  }

  function wslExe(args, o) {
    if (!m.wsl) {
      return { code: 1, stdout: fixture('wsl-not-installed.utf16le') };
    }
    const a = args.join(' ');
    if (a === '--version') return { stdout: fixture('wsl-version.utf16le') };
    if (a === '--list --quiet') {
      if (!m.distros.length) return { code: -1, stdout: fixture('wsl-list-empty.utf16le') };
      return { stdout: u16(`${m.distros.join('\r\n')}\r\n`) };
    }
    if (a === '--list --running --quiet') return { stdout: u16(m.running ? 'cseries\r\n' : '') };
    if (args[0] === '--import') {
      m.imports.push(args);
      if (!fs.existsSync(args[3])) return { code: 1, stdout: u16('The system cannot find the file specified.\r\n') };
      m.distros.push(args[1]);
      return { stdout: u16('The operation completed successfully.\r\n') };
    }
    if (args[0] === '--terminate') { m.running = false; return {}; }
    if (args[0] === '--unregister') { m.distros = m.distros.filter((d) => d !== args[1]); return {}; }
    if (args[0] === '-d' && args[1] === 'cseries' && args[4] === '--exec' && args[5] === '/usr/local/bin/cseries-guest') {
      return guest(args.slice(6), o.input);
    }
    if (a === '-d cseries -u root --exec /bin/sh -s') {
      if (!m.distros.includes('cseries')) return { code: 1, stderr: u16('There is no distribution with the supplied name.\r\nError code: Wsl/Service/WSL_E_DISTRO_NOT_FOUND\r\n') };
      m.running = true;
      return m.guestShell.run(o.input);
    }
    return { code: 1, stderr: u16(`unexpected wsl ${a}`) };
  }

  const unq = (v) => v.replace(/''/g, "'");
  const sha = (file) => (fs.existsSync(file) ? sha256Hex(fs.readFileSync(file)) : null);

  // One bastion command, as the service's bastion answers it.
  function bastionCmd(args, stdin) {
    m.bastionCommands.push(args);
    if (m.failAt && args[0] === m.failAt) return { code: 1, out: `error: ${args[0]} failed (test)` };
    const b = m.bastion;
    switch (args[0]) {
      case 'service':
        m.service = { running: true };
        m.serviceInstalls++;
        m.registry['HKLM:\\Software\\Microsoft\\Windows\\CurrentVersion\\Run\\PrivataeBastionTray'] = `"${paths.serviceExe}" tray`;
        return { code: 0, out: 'System service installed: PrivataeBastion' };
      case 'set-mode':
        b.mode = args[1];
        return { code: 0, out: `mode set to ${args[1]}` };
      case 'approvals-owner': {
        // As approvals_owner.go: the same SID changes nothing; another is
        // refused and the first stays; --replace only at a terminal.
        const sid = args[args.indexOf('--sid') + 1];
        if (args.includes('--replace')) return { code: 1, out: 'error: approvals-owner --replace is taken only in an Administrator terminal' };
        if (b.owner && b.owner !== sid) {
          return { code: 1, out: `error: this machine's approvals belong to ${b.owner}, and stay with it — nothing was changed.` };
        }
        b.owner = sid;
        return { code: 0, out: `approvals on this machine belong to ${sid}` };
      }
      case 'release-anchor': {
        // As anchor_release.go: --force only at a terminal; with something to
        // release, --host-ca must be a CA other than the one pinned.
        const h = args[2];
        if (args.includes('--force')) return { code: 1, out: 'error: release-anchor --force is taken only in an Administrator terminal' };
        if (b.anchor && b.anchor !== h) return { code: 1, out: `error: this box is anchored to ${b.anchor}, not ${h}` };
        if (b.anchor === h || b.pairings[h] !== undefined) {
          const at = args.indexOf('--host-ca');
          if (at < 0) return { code: 1, out: 'error: name the new Host\'s CA with --host-ca <pem file> — nothing was released' };
          const pinned = b.pairings[h];
          if (!pinned) return { code: 1, out: `error: this box pinned no Host CA for ${h} — nothing was released` };
          if (pinned.trim() === fs.readFileSync(args[at + 1], 'utf8').trim()) {
            return { code: 1, out: `error: the CA given with --host-ca is the one ${h} is paired with, so that Host is not gone — nothing was released` };
          }
        }
        b.anchor = null;
        delete b.pairings[h];
        delete b.enrolled[h];
        m.releases.push(h);
        return { code: 0, out: `released ${h}` };
      }
      case 'pair': {
        // As bastion 0.53.2 (pair_stdin.go): `-` reads the token's first line
        // from standard input. A token as an argument the real one takes;
        // this one refuses it, since the install must never send one.
        m.pairCalls.push(args);
        if (args[args.length - 1] !== '-') return { code: 2, out: 'fake: the token must come on standard input, with -' };
        const token = String(stdin === undefined ? '' : stdin).split('\n')[0].trim();
        if (!token) return { code: 2, out: 'standard input held no pairing token: give it as the first line, or as the argument; nothing was paired' };
        m.pairTokens.push(token);
        if (b.pairings[m.hostName]) {
          return { code: 1, out: `error: already paired with ${m.hostName} — run \`privatae-bastion pair --replace <token>\` to rotate its credentials` };
        }
        b.pairings[m.hostName] = fs.readFileSync(args[args.indexOf('--host-ca') + 1], 'utf8');
        return { code: 0, out: `Paired with ${m.hostName} over TCP.\n\n  lanes: 127.0.0.1:8811` };
      }
      case 'claim-anchor': {
        if (b.anchor) return { code: 1, out: `error: this box is already anchored to ${b.anchor}` };
        const names = Object.keys(b.pairings);
        if (names.length !== 1) return { code: 1, out: `error: this box serves ${names.length} hosts` };
        b.anchor = names[0];
        return { code: 0, out: `anchored to ${names[0]} (owner "x")` };
      }
      case 'stepup-enroll': {
        if (!args.includes('--console-only')) return { code: 2, out: 'not on a console' };
        b.enrolled[args[args.indexOf('--host') + 1]] = true;
        m.enrolments++;
        return { code: 0, out: '' };
      }
      default:
        return { code: 1, out: `unknown ${args[0]}` };
    }
  }

  // The elevated script, acted out in the order its steps appear.
  function runBastionScript(script) {
    const logPath = unq(/^\$log = '((?:[^']|'')*)'$/m.exec(script)[1]);
    const nonce = /run=([0-9a-f]+)/.exec(script)[1];
    let hostName = unq(/^\$hostName = '((?:[^']|'')*)'$/m.exec(script)[1]);
    const caCopy = path.join(paths.serviceDir, 'host-ca.pem');
    const log = [];
    const say = (l) => log.push(l);
    const finish = (code) => {
      fs.mkdirSync(path.dirname(logPath), { recursive: true });
      fs.writeFileSync(logPath, `\uFEFF${[`run=${nonce}`, ...log].join('\r\n')}\r\n`);
      return { code };
    };
    const quit = (code, why) => { say(`error=${why}`); say(`exit=${code}`); return finish(code); };
    const items = (list) => {
      const out = [];
      const re = /'((?:[^']|'')*)'|\$ca\b|\$hostName\b/g;
      let x;
      while ((x = re.exec(list))) out.push(x[1] !== undefined ? unq(x[1]) : (x[0] === '$ca' ? caCopy : hostName));
      return out;
    };
    const re = /Emit 'step=install'|Emit 'step=old-install'|Bastion (\d+) '([^']*)' @\(([^\n]*?)\)(?: ('(?:[^']|'')*'))?|Emit 'step=host-ca'|& \$exe ('stepup-enroll'[^\n]*)|Restart-Service/g;
    let x;
    while ((x = re.exec(script))) {
      const t = x[0];
      if (t === "Emit 'step=install'") {
        const file = unq(/Copy-Item -LiteralPath '((?:[^']|'')*)' -Destination \$new/.exec(script)[1]);
        const want = /if \(\$h -ne '([0-9a-f]{64})'\) \{ Remove-Item -LiteralPath \$new/.exec(script)[1];
        if (sha(file) !== want) return quit(10, 'the copied program has another sha256');
        fs.mkdirSync(paths.serviceDir, { recursive: true });
        fs.copyFileSync(file, paths.serviceExe);
        say(`installed=${paths.serviceExe}`);
      } else if (t === "Emit 'step=old-install'") {
        m.tasks.delete('PrivataeBastion');
        fs.rmSync(paths.bastionExe, { force: true });
        const k = `HKU\\${m.sid}\\Environment\\Path`;
        if (script.includes(`OpenSubKey('${m.sid}\\Environment', $true)`) && m.registry[k] !== undefined) {
          m.registry[k] = m.registry[k].split(';').filter((e) => e && e.replace(/\\$/, '').toLowerCase() !== paths.bastionDir.toLowerCase()).join(';');
        }
        say('old_install=removed');
      } else if (t === "Emit 'step=host-ca'") {
        const file = unq(/Copy-Item -LiteralPath '((?:[^']|'')*)' -Destination \$ca/.exec(script)[1]);
        const want = /if \(\$h -ne '([0-9a-f]{64})'\) \{ Remove-Item -LiteralPath \$ca/.exec(script)[1];
        if (sha(file) !== want) return quit(14, 'the Host CA copy has another sha256');
        fs.mkdirSync(paths.serviceDir, { recursive: true });
        fs.copyFileSync(file, caCopy);
      } else if (t === 'Restart-Service') {
        m.serviceRestarts++;
        m.service = { running: true };
        say('result=ok');
        return finish(0);
      } else if (x[1]) {
        const code = Number(x[1]);
        const args = items(x[3]);
        say(`step=${x[2]}`);
        // PowerShell pipes the string to the program and ends it with a newline.
        const r = bastionCmd(args, x[4] === undefined ? undefined : `${unq(x[4].slice(1, -1))}\r\n`);
        if (r.out) say(r.out);
        if (r.code !== 0) return quit(code, `privatae-bastion ${x[2]} exited with ${r.code}`);
        if (args[0] === 'service') say('service=yes');
        if (args[0] === 'approvals-owner') say('owner=yes');
        if (args[0] === 'release-anchor') say(`released=${args[2]}`);
        if (args[0] === 'claim-anchor') say('anchored=yes');
        if (args[0] === 'pair') {
          fs.rmSync(caCopy, { force: true });
          hostName = /Paired with (\S+)/.exec(r.out)[1].replace(/\.$/, '');
          say(`paired_host=${hostName}`);
        }
      } else if (x[5] !== undefined) {
        const args = items(x[5]);
        say('step=stepup-enroll');
        if (!hostName) return quit(18, 'no pairing to enrol an authenticator for');
        const r = bastionCmd(args);
        if (r.code !== 0) return quit(18, `privatae-bastion stepup-enroll exited with ${r.code}`);
        say('enrolled=yes');
      }
    }
    return quit(1, 'the script ended without restarting the service');
  }

  function ps(script, input) {
    if (/\[Console\]::In\.ReadToEnd\(\)/.test(script) && /-Verb RunAs/.test(script)) {
      m.elevated.push(script);
      if (opts.uac === 'no') {
        return { code: 1, stderr: '#< CLIXML\r\n<Objs Version="1.1.0.1"><S S="Error">Start-Process : This operation was canceled by the user._x000D__x000A_</S></Objs>' };
      }
      const inner = Buffer.from(String(input).trim(), 'base64').toString('utf16le');
      m.bastionScripts.push(inner);
      return runBastionScript(inner);
    }
    if (/Start-Process -FilePath 'wsl\.exe'.*-Verb RunAs/.test(script)) {
      m.elevated.push(script);
      if (opts.uac === 'no') {
        return { code: 1, stderr: '#< CLIXML\r\n<Objs Version="1.1.0.1"><S S="Error">Start-Process : This operation was canceled by the user._x000D__x000A_</S></Objs>' };
      }
      m.wsl = true;
      return {};
    }
    if (/Win32_ComputerSystem/.test(script)) {
      const v = opts.virt || { hypervisor: false, firmware: true };
      return { stdout: `hypervisor=${v.hypervisor ? 'True' : 'False'}\r\nfirmware=${v.firmware ? 'True' : 'False'}\r\n` };
    }
    if (/Set-ItemProperty/.test(script)) {
      const key = /\$k = '([^']*)'/.exec(script)[1];
      const name = /-Name '([^']*)'/.exec(script)[1];
      const value = /-Value '((?:[^']|'')*)'/.exec(script)[1].replace(/''/g, "'");
      m.registry[`${key}\\${name}`] = value;
      return {};
    }
    if (/Remove-ItemProperty/.test(script)) {
      const key = /-Path '([^']*)'/.exec(script)[1];
      const name = /-Name '([^']*)'/.exec(script)[1];
      delete m.registry[`${key}\\${name}`];
      return {};
    }
    if (/Get-Service/.test(script)) return { stdout: m.service ? (m.service.running ? 'Running' : 'Stopped') : 'missing' };
    if (/WindowsIdentity\]::GetCurrent\(\)\.User\.Value/.test(script)) return { stdout: m.sid };
    return { code: 1, stderr: `unexpected script: ${script.slice(0, 80)}` };
  }

  function bastionExe(args) {
    if (args[0] === 'version') return { stdout: `privatae-bastion ${m.bastionVersion}\n` };
    return { code: 1 };
  }

  m.runner = fakeRunner((cmd, args, o) => {
    if (cmd === 'wsl.exe') return wslExe(args, o);
    if (cmd === 'powershell.exe') return ps(psScript(args), o.input);
    if (cmd === paths.serviceExe) return bastionExe(args);
    if (cmd === 'shutdown.exe') { m.shutdowns++; return {}; }
    if (cmd === 'clip.exe') return {};
    return { code: 1, notFound: true };
  });

  m.net = {
    async fetchSmall(url) {
      if (!files[url]) throw new Error(`${url} answered 404`);
      return files[url];
    },
    async download(url, dest, { sha256 }) {
      const body = files[url] || (url.endsWith('.tar.gz') ? rootfs : null);
      if (!body) throw new CliError(`Could not download: ${url} answered 404.`, '');
      if (sha256Hex(body) !== sha256) throw new CliError(`The downloaded file is not the file this release pins (sha256 ${sha256Hex(body)}, expected ${sha256}).`, '');
      fs.mkdirSync(path.dirname(dest), { recursive: true });
      fs.writeFileSync(dest, body);
      m.downloads = (m.downloads || 0) + 1;
      return { downloaded: true };
    },
    fileSha256: require('../src/net').fileSha256,
    async probe(url) {
      if (!m.hostStarted) return null;
      if (url.endsWith('/api/system/health')) return { status: 200, location: '' };
      if (url.endsWith('/setup')) return opts.ownerExists ? { status: 303, location: '/login' } : { status: 200, location: '' };
      return { status: 404, location: '' };
    },
  };
  return m;
}

module.exports = { fakeMachine, signer };
