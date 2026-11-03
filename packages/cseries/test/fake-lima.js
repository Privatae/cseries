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
const { fakeRunner, guestShell } = require('./helpers');
const { statement, sha256Hex } = require('../src/verify');
const lima = require('../src/vm/lima');
const { MACHINE_BASE, MACHINE_UNIT } = require('../src/platform');

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
  // opts.root: the fake machine's own folder, outside paths.base for a test
  // that checks uninstall removed all of paths.base.
  const root = opts.root || path.join(paths.base, 'machine');
  fs.mkdirSync(root, { recursive: true });
  const m = {
    platform,
    paths,
    qemu: Boolean(opts.qemu),
    kvm: opts.kvm || 'ok',
    kvmMember: Boolean(opts.kvmMember),
    // Lima's instances by LIMA_HOME (inst): the whole machine's or the
    // account's own (paths.lima), and with opts.account, that account's.
    instances: {},
    template: null,
    images: null,
    hostStarted: false,
    starts: [],
    pulls: 0,
    tokens: 0,
    hostCa: opts.hostCa || '-----BEGIN CERTIFICATE-----\nMIIBexample0\n-----END CERTIFICATE-----',
    sudoRuns: [],
    userUnits: [],
    systemUnits: [],
    calls: [],
    vmUser: null,
    // The whole machine's install runs limactl through this runuser.
    runuser: paths.vmUser ? path.join(root, 'runuser-bin', 'runuser') : null,
    systemd: { enabled: false, active: false, unitWritten: false },
    root,
    // The guest's root shell: the machine wall's install script runs here.
    guestShell: guestShell(root, { restartFails: opts.wallFails, noTable: opts.wallMissing }),
    uid: opts.uid === undefined ? 1000 : opts.uid,
    moveRuns: [],
  };
  // An instance whose files are on the disk (one copied in by a move) is
  // there for Lima, stopped, as limactl list finds one.
  function inst(home) {
    if (m.instances[home]) return m.instances[home];
    if (fs.existsSync(path.join(home, 'cseries', 'disk'))) m.instances[home] = { status: 'Stopped' };
    return m.instances[home] || null;
  }
  Object.defineProperty(m, 'instance', {
    get: () => inst(paths.lima),
    set: (v) => { m.instances[paths.lima] = v; },
  });
  // opts.account: {name, home, paths}, an account's own install on this same
  // machine (move.js), its Lima run as that account through runuser.
  const account = opts.account || null;
  m.accountInstance = () => (account ? inst(account.paths.lima) : null);

  // The machine's own sudo, for the context's sudoPaths. bastion.js looks for
  // sudo on the disk before it runs it, and this runner answers every sudo,
  // so without it a test would pass or fail on whether this computer has a
  // sudo its account can see (a CI account may not). It is never run.
  m.sudoPaths = [path.join(root, 'sudo-bin', 'sudo')];
  fs.mkdirSync(path.dirname(m.sudoPaths[0]), { recursive: true });
  fs.writeFileSync(m.sudoPaths[0], '#!/bin/sh\necho "the fake machine answers sudo; this file is never run" >&2\nexit 2\n', { mode: 0o755 });

  // runuser, for the context's runuserPaths (lima.js looks for it on the
  // disk); this runner answers it, so it is never run either.
  if (m.runuser) {
    m.runuserPaths = [m.runuser];
    fs.mkdirSync(path.dirname(m.runuser), { recursive: true });
    fs.writeFileSync(m.runuser, '#!/bin/sh\necho "the fake machine answers runuser; this file is never run" >&2\nexit 2\n', { mode: 0o755 });
  }

  // The program the sudo script installs: a wrapper around fake-bastion.js.
  const program = Buffer.from(`#!/bin/sh\nexec ${JSON.stringify(process.execPath)} ${JSON.stringify(path.join(__dirname, 'fake-bastion.js'))} "$@"\n`);
  const s = signer();
  const key = `${platform}-${opts.arch === 'arm64' ? 'arm64' : 'amd64'}`;
  const manifest = Buffer.from(JSON.stringify({
    version: opts.bastionVersion || '0.53.2',
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
  const chowns = path.join(root, 'chowns');
  stub('chown', `echo "$*" >> ${JSON.stringify(chowns)}`);
  m.chowns = () => (fs.existsSync(chowns) ? fs.readFileSync(chowns, 'utf8').trim().split('\n') : []);
  // qemu-img info: opts.backedBy names the disk's backing file.
  stub('qemu-img', opts.backedBy ? `echo "file format: qcow2"; echo "backing file: ${opts.backedBy}"` : 'echo "file format: qcow2"');
  if (opts.diskFull) stub('df', 'echo "Filesystem 1024-blocks Used Available Capacity Mounted on"; echo "fake 1000 999 1 100% /"');
  stub('shasum', 'sha256sum "$3"');
  // systemctl: logged, and is-active answers from what was restarted and
  // not disabled since.
  const active = path.join(root, 'service-active');
  stub('systemctl', `echo "systemctl $*" >> ${JSON.stringify(svcLog)}
case "$1" in
  restart) touch ${JSON.stringify(active)} ;;
  disable) rm -f ${JSON.stringify(active)} ;;
  is-active) [ -f ${JSON.stringify(active)} ] || exit 3 ;;
esac`);
  stub('launchctl', `echo "launchctl $*" >> ${JSON.stringify(svcLog)}`);
  // The whole machine's VM account, as the bastion script sees it (getent,
  // userdel): there while m.vmUser is, written to vmUserMark before each run.
  const vmUserMark = path.join(root, 'vm-user-here');
  const userdels = path.join(root, 'userdels');
  stub('getent', `[ "$1" = passwd ] && [ "$2" = ${JSON.stringify(paths.vmUser || '')} ] && [ -f ${JSON.stringify(vmUserMark)} ] && echo "$2:x:990:990::/nonexistent:/bin/false"`);
  // m.rmFails: rm refuses the whole machine's folder, leaving all of it
  // ('all') or all but its state.json ('partial'); other paths it removes.
  const rmFails = path.join(root, 'rm-fails');
  stub('rm', `for a in "$@"; do
  if [ "$a" = ${JSON.stringify(paths.base)} ] && [ -f ${JSON.stringify(rmFails)} ]; then
    [ "$(cat ${JSON.stringify(rmFails)})" != partial ] || /bin/rm -f ${JSON.stringify(path.join(paths.base, 'state.json'))}
    echo "rm: cannot remove '$a': Operation not permitted" >&2; exit 1
  fi
done
exec /bin/rm "$@"`);
  // m.userdelFails: userdel refuses (the account is in use, say).
  stub('userdel', `echo "$*" >> ${JSON.stringify(userdels)}; [ ! -f ${JSON.stringify(path.join(root, 'userdel-fails'))} ] || exit 8; rm -f ${JSON.stringify(vmUserMark)}`);
  m.userdels = () => (fs.existsSync(userdels) ? fs.readFileSync(userdels, 'utf8').trim().split('\n').filter(Boolean) : []);
  m.serviceLog = () => (fs.existsSync(svcLog) ? fs.readFileSync(svcLog, 'utf8') : '');
  m.ttyOut = () => (fs.existsSync(ttyOut) ? fs.readFileSync(ttyOut, 'utf8') : '');
  // The stand-in for root's HOME (/root, /var/root) the script runs under.
  m.bastionHome = () => home;
  m.bastionCalls = () => (fs.existsSync(m.bastionLog) ? fs.readFileSync(m.bastionLog, 'utf8').trim().split('\n').filter(Boolean).map((l) => JSON.parse(l)) : []);
  m.bastionState = () => {
    const f = path.join(home, 'fake-bastion.json');
    return fs.existsSync(f) ? JSON.parse(fs.readFileSync(f, 'utf8')) : null;
  };

  // The bastion's state folders as the uninstall script names them, each in
  // the temp folder (posix/elevated.js BASTION_STATE).
  m.stateDirs = {
    // The sockets' folder before /run/privatae, which is a prefix of it, so
    // neither name is ever rewritten into the other's fake.
    '/var/run/privatae-bastion': path.join(root, 'run-privatae-bastion'),
    '/run/privatae-bastion': path.join(root, 'run-privatae-bastion'),
    '/root/.config/privatae-bastion': path.join(home, '.config', 'privatae-bastion'),
    '/var/root/Library/Application Support/privatae-bastion': path.join(home, 'Library', 'Application Support', 'privatae-bastion'),
    '/var/lib/privatae': path.join(root, 'var-lib-privatae'),
    '/run/privatae': path.join(root, 'run-privatae'),
    '/Library/LaunchDaemons': path.join(root, 'LaunchDaemons'),
    '/Library/Application Support/privatae/bastion': path.join(root, 'library-privatae-bastion'),
    // After the bastion's own folder in it, which is replaced first.
    '/root/.config': path.join(home, '.config'),
  };
  function testScript(script) {
    let t = script
      .replace('PATH=/usr/sbin:/usr/bin:/sbin:/bin', `PATH=${stubs}:/usr/sbin:/usr/bin:/sbin:/bin`)
      .replace(/^HOME=\/(var\/)?root$/m, `HOME=${home}`)
      .split('/etc/systemd/system/privatae-bastion.service').join(unit)
      .split('/run/systemd/system').join(sd)
      .split('/usr/local/bin').join(linkBin)
      .split('</dev/tty').join(`<${ttyIn}`)
      .split('>/dev/tty').join(`>>${ttyOut}`);
    for (const [real, fake] of Object.entries(m.stateDirs)) t = t.split(real).join(fake);
    // The whole machine's folder and unit (bastion.js machineRecordFor): this
    // fake machine's own.
    if (paths.vmUser) t = t.split(MACHINE_UNIT).join(paths.autostart).split(MACHINE_BASE).join(paths.base);
    // These scripts run for real: one that still names a folder of this
    // computer's own is a bug in the test, and is never run.
    // A path that starts there (after a space, a quote, = or a parenthesis),
    // not one inside the test's own folder that merely contains the name.
    // /root and /var/root as a whole word too (HOME=/root a second time,
    // cd /root), not only as the start of a longer path.
    // The one place root's home may stay: the text of the unit the install
    // script writes (to this fake's own unit file), which names the HOME the
    // service runs with and is never run here.
    const checked = t.split(/^Environment=HOME=\/root$/m).join('');
    for (const real of ['/root', '/var/root', '/var/lib/', '/run/privatae', '/var/run/', '/etc/', '/Library/']) {
      const at = new RegExp(`(^|[\\s'"=(:])${real.replace(/[/]/g, '\\/')}`, 'm').exec(checked);
      if (at) throw new Error(`fake-lima: the script still names ${real} (${checked.slice(Math.max(0, at.index - 40), at.index + 40).replace(/\n/g, '\\n')}); it is not run`);
    }
    // The script finds systemctl, launchctl and the account tools through
    // its own PATH line, which points at this fake's stubs: without that
    // line, or with one of them by its absolute path, it would reach this
    // computer's real ones.
    if (!t.includes(`PATH=${stubs}:`)) throw new Error('fake-lima: the script sets no PATH this fake can point at its stubs; it is not run');
    if (/(^|[\s'"=(:;&|])\/(usr\/)?s?bin\/(systemctl|launchctl|userdel|useradd|usermod)\b/m.test(t)) throw new Error('fake-lima: the script runs a service or account tool by its absolute path; it is not run');
    return t;
  }

  function runBastionScript(script) {
    m.sudoRuns.push({ kind: 'bastion', script });
    if (m.beforeSudo) m.beforeSudo(script);
    if (m.rmFails) fs.writeFileSync(rmFails, m.rmFails);
    else fs.rmSync(rmFails, { force: true });
    if (m.userdelFails) fs.writeFileSync(path.join(root, 'userdel-fails'), '');
    else fs.rmSync(path.join(root, 'userdel-fails'), { force: true });
    if (m.vmUser) fs.writeFileSync(vmUserMark, '');
    else fs.rmSync(vmUserMark, { force: true });
    const r = childProcess.spawnSync('/bin/sh', ['-s'], {
      input: testScript(script),
      env: { ...process.env, ...(opts.sudoEnv || {}), FAKE_BASTION_LOG: m.bastionLog, FAKE_BASTION_HOME: home, FAKE_BASTION_FAIL: opts.failAt || '', FAKE_HOST_NAME: opts.hostName || 'host-a' },
    });
    if (m.vmUser && !fs.existsSync(vmUserMark)) m.vmUser = null;
    if (fs.existsSync(unit)) m.systemd.unitWritten = true;
    const log = m.serviceLog();
    if (/systemctl enable privatae-bastion/.test(log)) m.systemd.enabled = true;
    if (/systemctl restart privatae-bastion|launchctl kickstart -k system\/ai\.privatae\.bastion/.test(log)) m.systemd.active = true;
    if (/systemctl disable --now privatae-bastion|launchctl bootout system\/ai\.privatae\.bastion/.test(script)) {
      m.systemd.active = fs.existsSync(active);
      m.systemd.enabled = false;
    }
    // What the real service leaves for serviceState() to find.
    if (fs.existsSync(unit) || (m.bastionState() || {}).service) fs.writeFileSync(paths.serviceUnit, 'unit');
    else fs.rmSync(paths.serviceUnit, { force: true });
    return { code: r.status, stdout: r.stdout, stderr: r.stderr };
  }

  // The move's copy (move.js copyScript), as root: RUN by /bin/sh with the
  // same substitutions, so its cp, find, du and df are the real ones on the
  // test's own folders.
  function runMoveScript(script) {
    m.moveRuns.push(script);
    if (m.beforeMove) m.beforeMove(script);
    const r = childProcess.spawnSync('/bin/sh', ['-s'], { input: testScript(script), env: { ...process.env } });
    if (m.afterMove) m.afterMove();
    return { code: r.status, stdout: r.stdout, stderr: r.stderr };
  }

  // The authenticator's own sudo run (bastion.js enrolOnTerminal): RUN by
  // /bin/sh with the same substitutions, the "terminal" (tty-in, tty-out) as
  // its three streams. opts.noTerminal: there is no terminal to open.
  function runEnrol(script, rest, o) {
    m.sudoRuns.push({ kind: 'enrol', script, args: rest, tty: Boolean(o.tty) });
    if (opts.noTerminal) return { code: -1, noTerminal: true };
    const inFd = fs.openSync(ttyIn, 'r');
    const outFd = fs.openSync(ttyOut, 'a');
    try {
      const r = childProcess.spawnSync('/bin/sh', ['-c', testScript(script), ...rest], {
        stdio: [inFd, outFd, outFd],
        env: { ...process.env, ...(opts.sudoEnv || {}), FAKE_BASTION_LOG: m.bastionLog, FAKE_BASTION_HOME: home, FAKE_BASTION_FAIL: opts.failAt || '', FAKE_HOST_NAME: opts.hostName || 'host-a' },
      });
      return { code: r.status };
    } finally {
      fs.closeSync(inFd);
      fs.closeSync(outFd);
    }
  }

  function probe() {
    const f = { uid: String(m.uid), user: 'someone' };
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
        kvm_world: 'no',
        vm_user_home: m.vmUser ? m.vmUser.home : '',
        vm_user_groups: m.vmUser ? m.vmUser.groups.join(' ') : '',
      });
    }
    return { stdout: Object.entries(f).map(([k, v]) => `${k}=${v}`).join('\n') };
  }

  function guest(sub, input, home = paths.lima) {
    const i = inst(home);
    if (!i || i.status !== 'Running') return { code: 1, stderr: 'instance "cseries" is not running' };
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
      case 'pair-token': {
        m.tokens++;
        // The Host's CA is on its disk: a moved disk is the same Host.
        const disk = path.join(home, 'cseries', 'disk');
        const ca = fs.existsSync(disk) ? JSON.parse(fs.readFileSync(disk, 'utf8')).hostCa : m.hostCa;
        return { stdout: JSON.stringify({ token: `${opts.tokenPrefix || 'tok'}-${m.tokens}`, host_ca_pem: ca, enroll: 'https://127.0.0.1:8810/v1/local/redeem' }) };
      }
      default: return { code: 1, stderr: `unknown ${sub[0]}` };
    }
  }

  function limactl(args, o, viaSg, asVmUser = false) {
    if (args[0] !== '--tty=false') return { code: 2, stderr: 'expected --tty=false first' };
    const home = o.env && o.env.LIMA_HOME;
    if (home !== paths.lima && !(account && home === account.paths.lima)) return { code: 2, stderr: `LIMA_HOME is ${home}` };
    // Lima refuses root; the whole machine's install runs it as its account.
    if (paths.vmUser && home === paths.lima && !asVmUser) return { code: 1, stderr: 'level=fatal msg="must not run as the root user"' };
    if (account && home === account.paths.lima && m.uid === 0 && asVmUser !== account.name) return { code: 1, stderr: 'level=fatal msg="must not run as the root user"' };
    const a = args.slice(1);
    const i = inst(home);
    switch (a[0]) {
      case '--version': return { stdout: 'limactl version 2.2.1\n' };
      case 'list':
        // Started by a unit: Lima says Running once it has booted, a
        // question later.
        if (i && i.booting !== undefined && i.booting-- <= 0) {
          i.status = 'Running';
          delete i.booting;
        }
        return { stdout: i ? `cseries ${i.status}\n` : '' };
      case 'create': {
        const yaml = fs.readFileSync(a[2], 'utf8');
        m.template = yaml;
        const image = JSON.parse(/location: (".*")/.exec(yaml)[1]);
        if (!fs.existsSync(image)) return { code: 1, stderr: `image ${image} not found` };
        m.instances[home] = { status: 'Stopped' };
        // What Lima keeps: the instance's disk (here, the Host's identity,
        // its CA, stands for all the data on it), its settings, a pid file
        // of a run, and Lima's own SSH key.
        fs.mkdirSync(path.join(home, 'cseries'), { recursive: true });
        fs.mkdirSync(path.join(home, '_config'), { recursive: true });
        fs.writeFileSync(path.join(home, 'cseries', 'disk'), JSON.stringify({ hostCa: m.hostCa, data: 'invented Host data' }));
        fs.writeFileSync(path.join(home, 'cseries', 'lima.yaml'), yaml);
        fs.writeFileSync(path.join(home, '_config', 'user'), 'invented ssh key');
        return { stderr: 'level=info msg="Run `limactl start cseries` to start the instance."' };
      }
      case 'start':
        if (account && home === account.paths.lima && m.uid === 0) (m.accountStarts = m.accountStarts || []).push({ as: asVmUser });
        else m.starts.push({ viaSg });
        if (platform === 'linux' && m.kvm !== 'ok' && !viaSg) {
          return { code: 1, stderr: 'level=fatal msg="Could not access KVM kernel module: Permission denied"' };
        }
        i.status = 'Running';
        fs.writeFileSync(path.join(home, 'cseries', 'ha.pid'), '4242');
        if (m.images) m.hostStarted = true;
        return {};
      case 'shell': {
        const at = a.indexOf('sudo');
        if (a[1] !== '--workdir' || a[2] !== '/' || a[3] !== 'cseries') return { code: 2, stderr: 'unexpected shell' };
        if (a[at + 1] === '/bin/sh' && a[at + 2] === '-s' && a.length === at + 3) {
          if (!i || i.status !== 'Running') return { code: 1, stderr: 'instance "cseries" is not running' };
          const r = m.guestShell.run(o.input);
          // The script's own `systemctl stop cseries-host.service` (a stub
          // here) stops the fake's Host too.
          if (/^host=stopped$/m.test(r.stdout)) m.hostStarted = false;
          return r;
        }
        if (a[at + 1] !== lima.GUEST_BIN) return { code: 2, stderr: 'unexpected shell' };
        return guest(a.slice(at + 2), o.input, home);
      }
      case 'stop':
        if (i) i.status = 'Stopped';
        m.hostStarted = false;
        fs.rmSync(path.join(home, 'cseries', 'ha.pid'), { force: true });
        return {};
      case 'delete':
        m.instances[home] = null;
        fs.rmSync(path.join(home, 'cseries'), { recursive: true, force: true });
        m.imagesHere = false;
        return {};
      default: return { code: 1, stderr: `unknown limactl ${a[0]}` };
    }
  }

  // The account's own systemd user manager (opts.accountRunDir: its runtime
  // folder; it runs when a bus is in it), as runuser reaches it for root:
  // start runs its login unit's limactl start --foreground, as that account.
  // m.accountUnit: what it was asked.
  function accountUserManager(args, env) {
    const run = opts.accountRunDir;
    (m.accountUnit = m.accountUnit || []).push(args.join(' '));
    if (!run || env.XDG_RUNTIME_DIR !== run || env.DBUS_SESSION_BUS_ADDRESS !== `unix:path=${run}/bus` || !fs.existsSync(path.join(run, 'bus'))) {
      return { code: 1, stderr: 'Failed to connect to user scope bus via local transport: No such file or directory' };
    }
    const unit = account.paths.autostart;
    const i = inst(account.paths.lima);
    switch (args[0]) {
      case 'start': {
        if (!fs.existsSync(unit)) return { code: 5, stderr: 'Failed to start cseries.service: Unit cseries.service not found.' };
        const u = fs.readFileSync(unit, 'utf8');
        if (!/ start --foreground cseries$/m.test(u) || !u.includes(`LIMA_HOME=${account.paths.lima}`)) return { code: 1, stderr: 'not the login unit lima.js writes' };
        if (opts.accountUnitFails) return {};
        if (i.status !== 'Running') i.booting = 1;
        return {};
      }
      case 'is-active':
        if (opts.accountUnitFails) return { code: 3, stdout: 'failed\n' };
        return i.status === 'Running' || i.booting !== undefined ? { stdout: 'active\n' } : { code: 3, stdout: 'inactive\n' };
      default: return { code: 1, stderr: `unexpected systemctl --user ${args.join(' ')}` };
    }
  }

  // A script for root (sh -c): QEMU's install, the packages' removal, the
  // whole machine's VM account and its removal. Acted out, never run.
  function rootScript(script, via) {
    if (/useradd --system/.test(script)) {
      m.sudoRuns.push({ kind: 'vm-user', script, via });
      const created = !m.vmUser;
      const home = /^home='([^']*)'$/m.exec(script)[1];
      m.vmUser = m.vmUser || { home, groups: ['cseries'] };
      // install -d ... LIMA_HOME and the account's home.
      fs.mkdirSync(paths.lima, { recursive: true });
      fs.mkdirSync(home, { recursive: true });
      if (/usermod -a -G 'kvm' "\$u"/.test(script) && !m.vmUser.groups.includes('kvm')) m.vmUser.groups.push('kvm');
      return { stdout: `${created ? 'created=yes\n' : ''}ready=yes\n` };
    }
    if (/userdel/.test(script)) {
      m.sudoRuns.push({ kind: 'userdel', script, via });
      m.vmUser = null;
      return {};
    }
    if (/apt-get (remove|purge)|dnf remove|zypper .*remove|pacman -Rns/.test(script)) {
      m.sudoRuns.push({ kind: 'remove-packages', script, via });
      m.qemu = false;
      return {};
    }
    m.sudoRuns.push({ kind: 'qemu', script, via });
    if (opts.qemuFail) return { code: 100, stdout: 'Reading package lists...\n', stderr: opts.qemuFail };
    if (/apt-get install/.test(script) && /qemu-system-x86/.test(script)) m.qemu = true;
    if (/usermod -a -G 'kvm' 'someone'/.test(script)) m.kvmMember = true;
    // apt-get's own lines, as many as it prints for QEMU's packages.
    const apt = [];
    for (let i = 0; i < 40; i++) apt.push(`Unpacking libexample${i}:amd64 (1.0-${i}) ...`, `Setting up libexample${i}:amd64 (1.0-${i}) ...`);
    return { stdout: `${apt.join('\n')}\n` };
  }

  // systemd's own systemctl for cseries.service, the whole machine's VM
  // (lima.js systemUnit): start runs the unit's limactl start --foreground.
  function systemVm(args) {
    m.systemUnits.push(args.join(' '));
    const unitHere = fs.existsSync(paths.autostart);
    switch (args[0]) {
      case 'daemon-reload': return {};
      case 'enable': return unitHere ? {} : { code: 1, stderr: 'Unit file cseries.service does not exist.' };
      case 'start':
        if (!unitHere) return { code: 5, stderr: 'Unit cseries.service not found.' };
        if (!/^User=cseries$/m.test(fs.readFileSync(paths.autostart, 'utf8'))) return { code: 1, stderr: 'expected User=cseries' };
        m.starts.push({ viaUnit: true });
        if (m.instance) m.instance.status = 'Running';
        if (m.images) m.hostStarted = true;
        return {};
      case 'stop': case 'disable':
        if (args[0] === 'disable' && args[1] !== '--now') return {};
        if (m.instance) m.instance.status = 'Stopped';
        m.hostStarted = false;
        return {};
      case 'is-active': return { stdout: m.instance && m.instance.status === 'Running' ? 'active\n' : 'inactive\n' };
      default: return { code: 1, stderr: `unexpected systemctl ${args.join(' ')}` };
    }
  }

  m.runner = fakeRunner((cmd, args, o) => {
    m.calls.push({ cmd, args, input: o.input });
    if (cmd === '/bin/sh' && args[0] === '-c' && /echo "uid=/.test(args[1])) return probe();
    // As root (the whole machine's install): scripts run as they are.
    if (cmd === '/bin/sh' && args[0] === '-c') return rootScript(args[1], 'root');
    if (cmd === '/bin/sh' && args[0] === '-s' && args.length === 1 && /^echo step=move-copy$/m.test(o.input || '')) return runMoveScript(o.input);
    if (cmd === '/bin/sh' && args[0] === '-s' && args.length === 1) return runBastionScript(o.input);
    if (cmd === 'getent' && args[0] === 'passwd') {
      if (account && args[1] === account.name) return { stdout: `${account.name}:x:1000:1000:Someone:${account.home}:/bin/bash\n` };
      return { code: 2 };
    }
    if (m.runuser && cmd === m.runuser && account && args[1] === account.name) {
      if (args[0] !== '-u' || args[2] !== '--') return { code: 2, stderr: 'unexpected runuser' };
      if (!o.env || o.env.HOME !== account.home) return { code: 2, stderr: `HOME is ${o.env && o.env.HOME}` };
      if (args[3] === 'systemctl' && args[4] === '--user') return accountUserManager(args.slice(5), o.env);
      if (args[3] !== lima.limactlPath({ paths: account.paths })) return { code: 2, stderr: 'runuser runs limactl only' };
      return limactl(args.slice(4), o, false, account.name);
    }
    if (account && cmd === lima.limactlPath({ paths: account.paths })) return limactl(args, o, false);
    if (m.runuser && cmd === m.runuser) {
      if (args[0] !== '-u' || args[1] !== 'cseries' || args[2] !== '--') return { code: 2, stderr: 'unexpected runuser' };
      if (!m.vmUser) return { code: 1, stderr: 'runuser: user cseries does not exist' };
      if (!o.env || o.env.HOME !== paths.vmHome) return { code: 2, stderr: `HOME is ${o.env && o.env.HOME}` };
      if (args[3] !== lima.limactlPath({ paths })) return { code: 2, stderr: 'runuser runs limactl only' };
      m.asVmUser = (m.asVmUser || 0) + 1;
      return limactl(args.slice(4), o, false, true);
    }
    if (cmd === 'systemctl' && args[0] !== '--user' && (args[args.length - 1] === 'cseries.service' || args[0] === 'daemon-reload')) return systemVm(args);
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
      // `sudo -n true`: whether sudo's credential cache spares a second
      // password (opts.sudoCache false: it does not).
      if (args.length === 2 && args[0] === '-n' && args[1] === 'true') {
        m.sudoCacheChecks = (m.sudoCacheChecks || 0) + 1;
        return opts.sudoCache === false ? { code: 1, stderr: 'sudo: a password is required' } : {};
      }
      const sh = args.indexOf('/bin/sh');
      if (o.inherit) {
        if (args[sh + 1] !== '-c') return { code: 2 };
        return runEnrol(args[sh + 2], args.slice(sh + 3), o);
      }
      if (args[sh + 1] === '-c') return rootScript(args[sh + 2], 'sudo');
      if (args[sh + 1] === '-s') return runBastionScript(o.input);
      return { code: 2 };
    }
    if (cmd === 'systemctl' && args[0] === '--user') {
      m.userUnits.push(args.slice(1).join(' '));
      // userManager: false, none answers; 'bus-only', one runs but only its
      // bus path finds it (a session with no XDG_RUNTIME_DIR, as su - gives).
      if (opts.userManager === 'bus-only' && o.env && o.env.XDG_RUNTIME_DIR) return {};
      return opts.userManager === false || opts.userManager === 'bus-only'
        ? { code: 1, stderr: 'Failed to connect to user scope bus via local transport: No such file or directory' } : {};
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

// The same for a whole-machine install on Linux (platform.js machinePaths).
// Its own folder (base, as /var/lib/cseries) holds the state, logs,
// downloads, Lima and LIMA_HOME; the bastion's and systemd's are beside it.
function machineLimaPaths(dir) {
  const p = limaPaths(dir, 'linux');
  const base = path.join(dir, 'var-lib-cseries');
  const j = (f) => path.join(base, f);
  return {
    ...p, base, state: j('state.json'), downloads: j('downloads'), hostCa: j('host-ca.pem'), installLog: j('bastion-install.log'),
    log: j('install.log'), lima: j('lima'), tools: j('tools'), vmHome: j('home'),
    vmUser: 'cseries', autostart: path.join(dir, 'systemd-system', 'cseries.service'),
  };
}

module.exports = { fakeLima, limaPaths, machineLimaPaths, unquoteWords };
