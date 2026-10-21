'use strict';

// A simulated Mac or Linux machine for the Lima driver's tests: the probe
// script, tar, limactl (and `sg <group> -c limactl ...`), the guest's
// cseries-guest behind `limactl shell`, systemctl --user, ps, and sudo.
//
// sudo is half real: the QEMU install script is checked and acted out, and
// the bastion's install script is RUN, by /bin/sh, after these test-only
// substitutions, so its quoting, here-document and sed are exercised:
//   PATH gets a folder of stubs in front (install, chown, systemctl,
//   launchctl, shasum); HOME is a temp folder; the systemd unit, the
//   /run/systemd/system check, /usr/local/bin and /dev/tty point into the
//   temp folder. The bastion it runs is fake-bastion.js.
//
// Everything here is invented test data.

const childProcess = require('node:child_process');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { CliError } = require('../src/errors');
const { fakeRunner } = require('./helpers');
const { statement, sha256Hex } = require('../src/verify');
const lima = require('../src/vm/lima');

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

// The words of a command line sgCommand built ('a' 'b' ...).
function unquoteWords(line) {
  const out = [];
  const re = /'((?:[^']|'\\'')*)'/g;
  let x;
  while ((x = re.exec(line))) out.push(x[1].replace(/'\\''/g, "'"));
  return out;
}

// opts: {platform, qemu: bool, kvm: 'ok'|'denied'|'missing', kvmMember,
//        pm, sudo: 'yes'|'no', uid, macos, hv, systemd, failAt, hostName,
//        userManager: bool, cpus, memKb (what the guest's status reports),
//        sudoEnv (variables sudo passes on to the bastion script),
//        tokenPrefix (the pairing tokens minted are <it>-1, <it>-2, ...),
//        pullFail / qemuFail (the stderr of a pull, or a QEMU install, that fails)}
function fakeLima(paths, opts = {}) {
  const platform = opts.platform || 'linux';
  const root = path.join(paths.base, 'machine');
  fs.mkdirSync(root, { recursive: true });
  const m = {
    platform,
    paths,
    qemu: Boolean(opts.qemu),
    kvm: opts.kvm || 'ok',
    kvmMember: Boolean(opts.kvmMember),
    instance: null,
    template: null,
    images: null,
    hostStarted: false,
    starts: [],
    pulls: 0,
    tokens: 0,
    hostCa: opts.hostCa || '-----BEGIN CERTIFICATE-----\nMIIBexample0\n-----END CERTIFICATE-----',
    sudoRuns: [],
    userUnits: [],
    calls: [],
    systemd: { enabled: false, active: false, unitWritten: false },
    root,
  };

  // The program the sudo script installs: a wrapper around fake-bastion.js.
  const program = Buffer.from(`#!/bin/sh\nexec ${JSON.stringify(process.execPath)} ${JSON.stringify(path.join(__dirname, 'fake-bastion.js'))} "$@"\n`);
  const s = signer();
  const key = `${platform}-${opts.arch === 'arm64' ? 'arm64' : 'amd64'}`;
  const manifest = Buffer.from(JSON.stringify({
    version: '0.53.2',
    binaries: { [key]: { url: `https://dl.downloads.example/privatae-bastion-${key}`, sha256: sha256Hex(program) } },
  }, null, 2));
  m.bastionKeys = [s.key];
  m.program = program;
  const qcow2 = Buffer.from('fake qcow2 disk');
  m.qcow2Sha256 = sha256Hex(qcow2);
  const files = {
    'https://privatae.ai/bastion/version.json': manifest,
    [`https://privatae.ai/bastion/version.json.${sha256Hex(manifest).slice(0, 16)}.sig`]: Buffer.from(s.sign('bastion', manifest)),
    [`https://dl.downloads.example/privatae-bastion-${key}`]: program,
  };

  // Folders the real script would touch, inside the temp folder.
  const home = path.join(root, 'root-home');
  const stubs = path.join(root, 'stubs');
  const sd = path.join(root, 'run-systemd-system');
  const unit = path.join(root, 'privatae-bastion.service');
  const linkBin = path.join(root, 'usr-local-bin');
  const ttyIn = path.join(root, 'tty-in');
  const ttyOut = path.join(root, 'tty-out');
  const svcLog = path.join(root, 'service.log');
  m.bastionLog = path.join(root, 'bastion-calls.log');
  for (const d of [home, stubs, linkBin]) fs.mkdirSync(d, { recursive: true });
  if (opts.systemd !== false) fs.mkdirSync(sd, { recursive: true });
  fs.writeFileSync(ttyIn, `${opts.code || '123456'}\n`);
  const stub = (name, body) => fs.writeFileSync(path.join(stubs, name), `#!/bin/sh\n${body}\n`, { mode: 0o755 });
  // install -o U -g G -m M SRC DST
  const modes = path.join(root, 'install-modes');
  stub('install', `src=$7; dst=$8; echo "$6" >> ${JSON.stringify(modes)}; cp "$src" "$dst" && chmod "$6" "$dst"`);
  m.installModes = () => (fs.existsSync(modes) ? fs.readFileSync(modes, 'utf8').trim().split('\n') : []);
  stub('chown', 'exit 0');
  stub('shasum', 'sha256sum "$3"');
  stub('systemctl', `echo "systemctl $*" >> ${JSON.stringify(svcLog)}`);
  stub('launchctl', `echo "launchctl $*" >> ${JSON.stringify(svcLog)}`);
  m.serviceLog = () => (fs.existsSync(svcLog) ? fs.readFileSync(svcLog, 'utf8') : '');
  m.ttyOut = () => (fs.existsSync(ttyOut) ? fs.readFileSync(ttyOut, 'utf8') : '');
  // The stand-in for root's HOME (/root, /var/root) the script runs under.
  m.bastionHome = () => home;
  m.bastionCalls = () => (fs.existsSync(m.bastionLog) ? fs.readFileSync(m.bastionLog, 'utf8').trim().split('\n').filter(Boolean).map((l) => JSON.parse(l)) : []);
  m.bastionState = () => {
    const f = path.join(home, 'fake-bastion.json');
    return fs.existsSync(f) ? JSON.parse(fs.readFileSync(f, 'utf8')) : null;
  };

  function testScript(script) {
    return script
      .replace('PATH=/usr/sbin:/usr/bin:/sbin:/bin', `PATH=${stubs}:/usr/sbin:/usr/bin:/sbin:/bin`)
      .replace(/^HOME=\/(var\/)?root$/m, `HOME=${home}`)
      .split('/etc/systemd/system/privatae-bastion.service').join(unit)
      .split('/run/systemd/system').join(sd)
      .split('/usr/local/bin').join(linkBin)
      .split('</dev/tty').join(`<${ttyIn}`)
      .split('>/dev/tty').join(`>>${ttyOut}`);
  }

  function runBastionScript(script) {
    m.sudoRuns.push({ kind: 'bastion', script });
    if (m.beforeSudo) m.beforeSudo(script);
    const r = childProcess.spawnSync('/bin/sh', ['-s'], {
      input: testScript(script),
      env: { ...process.env, ...(opts.sudoEnv || {}), FAKE_BASTION_LOG: m.bastionLog, FAKE_BASTION_FAIL: opts.failAt || '', FAKE_HOST_NAME: opts.hostName || 'host-a' },
    });
    if (fs.existsSync(unit)) m.systemd.unitWritten = true;
    const log = m.serviceLog();
    if (/systemctl enable privatae-bastion/.test(log)) m.systemd.enabled = true;
    if (/systemctl restart privatae-bastion|launchctl kickstart -k system\/ai\.privatae\.bastion/.test(log)) m.systemd.active = true;
    // What the real service leaves for serviceState() to find.
    if (m.systemd.unitWritten || (m.bastionState() || {}).service) fs.writeFileSync(paths.serviceUnit, 'unit');
    return { code: r.status, stdout: r.stdout, stderr: r.stderr };
  }

  function probe() {
    const f = { uid: String(opts.uid === undefined ? 1000 : opts.uid), user: 'someone' };
    if (platform === 'darwin') {
      f.macos = opts.macos || '15.1';
      f.hv = opts.hv || '1';
    } else {
      Object.assign(f, {
        qemu: m.qemu ? '/usr/bin/qemu-system-x86_64' : '',
        qemu_img: m.qemu ? '/usr/bin/qemu-img' : '',
        ssh: '/usr/bin/ssh',
        sg: '/usr/bin/sg',
        firmware: m.qemu ? '/usr/share/OVMF/OVMF_CODE_4M.fd' : '',
        kvm: m.kvm,
        kvm_group: 'kvm',
        kvm_member: m.kvmMember ? 'yes' : 'no',
        pm: opts.pm || 'apt-get',
        systemd: 'yes',
        sudo: '/usr/bin/sudo',
      });
    }
    return { stdout: Object.entries(f).map(([k, v]) => `${k}=${v}`).join('\n') };
  }

  function guest(sub, input) {
    if (!m.instance || m.instance.status !== 'Running') return { code: 1, stderr: 'instance "cseries" is not running' };
    switch (sub[0]) {
      case 'wait-boot': return { stdout: 'system=running\n' };
      case 'configure': {
        const changed = input !== m.images;
        m.images = input;
        return { stdout: `changed=${changed ? 1 : 0}\n` };
      }
      case 'pull': {
        m.pulls++;
        if (opts.pullFail) return { code: 125, stderr: opts.pullFail };
        if (m.imagesHere) return {};
        m.imagesHere = true;
        // What podman prints for a pull through limactl shell, invented ids:
        // "Trying to pull" and the layers on stderr, the image id on stdout.
        const chunks = [];
        for (const which of ['postgres', 'host']) {
          const ref = new RegExp(`^${which}=(.*)$`, 'm').exec(m.images || '');
          if (!ref) continue;
          chunks.push(`Trying to pull ${ref[1]}...\nGetting image source signatures\n`,
            'Copying blob 0a1b2c3d4e5f [=====>-------] 12.0MiB / 48.0MiB | 3.1 MiB/s\r',
            'Copying blob 0a1b2c3d4e5f done   | \nCopying blob 9f8e7d6c5b4a done   | \n',
            'Copying config 1234abcd5678 done   | \nWriting manifest to image destination\n',
            `${which === 'host' ? '6966a79c67b4' : '5a4b3c2d1e0f'}0123456789abcdef0123456789abcdef0123456789abcdef0123\n`);
        }
        return { chunks, stdout: '6966a79c67b40123456789abcdef0123456789abcdef0123456789abcdef0123\n' };
      }
      case 'start': case 'restart': m.hostStarted = true; return {};
      case 'stop': m.hostStarted = false; return {};
      case 'health': return { code: m.hostStarted ? 0 : 1 };
      case 'status': return { stdout: `guest=1.2.3\nrelease=1.2.3\nhost=${m.hostStarted ? 'active' : 'inactive'}\npostgres=active\nhealth=ok\ncpus=${opts.cpus || 4}\nmem_total_kb=${opts.memKb || 8126464}\n` };
      case 'host-log': return { stdout: 'CSERIES SETUP CODE: abc123-def456-0789ab\n' };
      case 'pair-token':
        m.tokens++;
        return { stdout: JSON.stringify({ token: `${opts.tokenPrefix || 'tok'}-${m.tokens}`, host_ca_pem: m.hostCa, enroll: 'https://127.0.0.1:8810/v1/local/redeem' }) };
      default: return { code: 1, stderr: `unknown ${sub[0]}` };
    }
  }

  function limactl(args, o, viaSg) {
    if (args[0] !== '--tty=false') return { code: 2, stderr: 'expected --tty=false first' };
    if (o.env && o.env.LIMA_HOME !== paths.lima) return { code: 2, stderr: `LIMA_HOME is ${o.env && o.env.LIMA_HOME}` };
    const a = args.slice(1);
    switch (a[0]) {
      case '--version': return { stdout: 'limactl version 2.2.1\n' };
      case 'list': return { stdout: m.instance ? `cseries ${m.instance.status}\n` : '' };
      case 'create': {
        const yaml = fs.readFileSync(a[2], 'utf8');
        m.template = yaml;
        const image = JSON.parse(/location: (".*")/.exec(yaml)[1]);
        if (!fs.existsSync(image)) return { code: 1, stderr: `image ${image} not found` };
        m.instance = { status: 'Stopped' };
        return { stderr: 'level=info msg="Run `limactl start cseries` to start the instance."' };
      }
      case 'start':
        m.starts.push({ viaSg });
        if (platform === 'linux' && m.kvm !== 'ok' && !viaSg) {
          return { code: 1, stderr: 'level=fatal msg="Could not access KVM kernel module: Permission denied"' };
        }
        m.instance.status = 'Running';
        if (m.images) m.hostStarted = true;
        return {};
      case 'shell': {
        const at = a.indexOf('sudo');
        if (a[1] !== '--workdir' || a[2] !== '/' || a[3] !== 'cseries' || a[at + 1] !== lima.GUEST_BIN) return { code: 2, stderr: 'unexpected shell' };
        return guest(a.slice(at + 2), o.input);
      }
      case 'stop': if (m.instance) m.instance.status = 'Stopped'; m.hostStarted = false; return {};
      case 'delete': m.instance = null; m.imagesHere = false; return {};
      default: return { code: 1, stderr: `unknown limactl ${a[0]}` };
    }
  }

  m.runner = fakeRunner((cmd, args, o) => {
    m.calls.push({ cmd, args, input: o.input });
    if (cmd === '/bin/sh' && args[0] === '-c' && /echo "uid=/.test(args[1])) return probe();
    if (cmd === 'tar') {
      const dir = args[args.indexOf('-C') + 1];
      fs.mkdirSync(path.join(dir, 'bin'), { recursive: true });
      fs.writeFileSync(path.join(dir, 'bin', 'limactl'), 'fake limactl');
      return {};
    }
    if (cmd === lima.limactlPath({ paths })) return limactl(args, o, false);
    if (cmd === '/usr/bin/sg') {
      if (args[0] !== 'kvm' || args[1] !== '-c') return { code: 2 };
      const [c, ...rest] = unquoteWords(args[2]);
      if (c !== lima.limactlPath({ paths })) return { code: 2 };
      return limactl(rest, o, true);
    }
    if (/sudo$/.test(cmd)) {
      if (opts.sudo === 'no') return { code: 1, stderr: 'sudo: 3 incorrect password attempts' };
      const sh = args.indexOf('/bin/sh');
      if (args[sh + 1] === '-c') {
        const script = args[sh + 2];
        m.sudoRuns.push({ kind: 'qemu', script });
        if (opts.qemuFail) return { code: 100, stdout: 'Reading package lists...\n', stderr: opts.qemuFail };
        if (/apt-get install/.test(script) && /qemu-system-x86/.test(script)) m.qemu = true;
        if (/usermod -a -G 'kvm' 'someone'/.test(script)) m.kvmMember = true;
        // apt-get's own lines, as many as it prints for QEMU's packages.
        const apt = [];
        for (let i = 0; i < 40; i++) apt.push(`Unpacking libexample${i}:amd64 (1.0-${i}) ...`, `Setting up libexample${i}:amd64 (1.0-${i}) ...`);
        return { stdout: `${apt.join('\n')}\n` };
      }
      if (args[sh + 1] === '-s') return runBastionScript(o.input);
      return { code: 2 };
    }
    if (cmd === 'systemctl' && args[0] === '--user') {
      m.userUnits.push(args.slice(1).join(' '));
      return opts.userManager === false ? { code: 1, stderr: 'Failed to connect to bus: No medium found' } : {};
    }
    if (cmd === 'systemctl' && args[0] === 'is-active') return { stdout: m.systemd.active ? 'active\n' : 'inactive\n' };
    if (cmd === '/bin/ps') return { stdout: m.systemd.active ? `root ${paths.serviceExe} run\nsomeone /bin/zsh\n` : 'someone /bin/zsh\n' };
    if (cmd === paths.serviceExe) return { stdout: 'privatae-bastion 0.53.2\n' };
    if (cmd === 'open' || cmd === 'xdg-open') return {};
    return { code: 1, notFound: true };
  });

  m.net = {
    async fetchSmall(url) {
      if (!files[url]) throw new Error(`${url} answered 404`);
      return files[url];
    },
    async download(url, dest, { sha256 }) {
      let body = files[url];
      if (!body && url.endsWith('.qcow2')) body = qcow2;
      if (!body && url.startsWith(lima.LIMA.base)) body = Buffer.from('fake lima tarball');
      if (!body) throw new CliError(`Could not download: ${url} answered 404.`, '');
      if (!url.startsWith(lima.LIMA.base) && sha256Hex(body) !== sha256) throw new CliError('sha256 mismatch', '');
      fs.mkdirSync(path.dirname(dest), { recursive: true });
      fs.writeFileSync(dest, body);
      return { downloaded: true };
    },
    fileSha256: require('../src/net').fileSha256,
    async probe(url) {
      if (!m.hostStarted) return null;
      if (url.endsWith('/api/system/health')) return { status: 200, location: '' };
      if (url.endsWith('/setup')) return { status: 200, location: '' };
      return { status: 404, location: '' };
    },
  };
  return m;
}

// The paths platform.js gives a Mac or a Linux machine, all under one temp
// folder; on macOS the program folder has a space in it, as the real one does.
function limaPaths(dir, platform = 'linux') {
  const j = path.join;
  const serviceDir = platform === 'darwin' ? j(dir, 'Library', 'Application Support', 'privatae', 'bin') : j(dir, 'usr-local', 'bin');
  return {
    base: dir,
    state: j(dir, 'state.json'),
    downloads: j(dir, 'downloads'),
    hostCa: j(dir, 'host-ca.pem'),
    installLog: j(dir, 'bastion-install.log'),
    log: j(dir, 'install.log'),
    bastionDir: j(dir, 'home-bin'),
    bastionExe: j(dir, 'home-bin', 'privatae-bastion'),
    serviceDir,
    serviceExe: j(serviceDir, 'privatae-bastion'),
    serviceUnit: j(dir, 'service-unit'),
    lima: j(dir, 'lima'),
    tools: j(dir, 'tools'),
    autostart: platform === 'darwin' ? j(dir, 'LaunchAgents', 'ai.privatae.cseries.plist') : j(dir, 'systemd-user', 'cseries.service'),
    join: path.join,
  };
}

module.exports = { fakeLima, limaPaths, unquoteWords };
