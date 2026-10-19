#!/usr/bin/env node
'use strict';

// Compares how the guest's units run the two containers with how compose.yaml
// declares them, setting by setting.
//
//   node compare-hardening.js <compose config json> <podman inspect json> <runtime json>
//
// compose config json: `docker compose -f compose.yaml config --format json`
// podman inspect json: `podman inspect cseries-postgres cseries-host`
// runtime json: {"<container>": {"status": "<its /proc/1/status>", ...}}
//
// Prints one line per setting and exits 1 when any differs. The guest also
// publishes 8810 and 8811 (the bastion's enroll and lane listeners, design
// section 3), which compose.yaml does not have yet; those are listed as
// additions, not differences.

const fs = require('node:fs');

const [composeFile, inspectFile, runtimeFile] = process.argv.slice(2);
const compose = JSON.parse(fs.readFileSync(composeFile, 'utf8'));
const inspect = JSON.parse(fs.readFileSync(inspectFile, 'utf8'));
const runtime = JSON.parse(fs.readFileSync(runtimeFile, 'utf8'));

const NAMES = { postgres: 'cseries-postgres', host: 'cseries-host' };
const ADDED_PORTS = new Set(['8810', '8811']);
let bad = 0;

function line(ok, service, setting, want, got) {
  if (!ok) bad++;
  process.stdout.write(`${ok ? 'same' : 'DIFF'}  ${service.padEnd(8)} ${setting.padEnd(18)} compose: ${want}   guest: ${got}\n`);
}

function procField(status, name) {
  const m = new RegExp(`^${name}:\\s*(\\S+)`, 'm').exec(status);
  return m ? m[1] : null;
}

for (const [service, name] of Object.entries(NAMES)) {
  const want = compose.services[service];
  const got = inspect.find((c) => c.Name === name || c.Name === `/${name}`);
  if (!got) {
    line(false, service, 'container', 'present', 'missing');
    continue;
  }
  const h = got.HostConfig;
  const rt = runtime[name] || {};

  line(want.user === got.Config.User, service, 'user', want.user, got.Config.User);
  line(Boolean(want.read_only) === h.ReadonlyRootfs, service, 'read_only', want.read_only, h.ReadonlyRootfs);
  line(want.pids_limit === h.PidsLimit, service, 'pids_limit', want.pids_limit, h.PidsLimit);

  const dropAll = (want.cap_drop || []).map((c) => c.toUpperCase()).includes('ALL');
  const bnd = procField(rt.status || '', 'CapBnd');
  const eff = procField(rt.status || '', 'CapEff');
  line(dropAll && /^0+$/.test(bnd || '') && /^0+$/.test(eff || ''), service, 'cap_drop ALL', (want.cap_drop || []).join(','), `CapBnd=${bnd} CapEff=${eff}`);

  const wantNnp = (want.security_opt || []).some((o) => /^no-new-privileges(:true)?$/.test(o));
  const nnp = procField(rt.status || '', 'NoNewPrivs');
  line(wantNnp && (h.SecurityOpt || []).includes('no-new-privileges') && nnp === '1', service, 'no-new-privileges', wantNnp, `SecurityOpt + NoNewPrivs=${nnp}`);

  const wantSeccomp = (want.security_opt || []).find((o) => o.startsWith('seccomp='));
  const gotSeccomp = (h.SecurityOpt || []).find((o) => o.startsWith('seccomp='));
  const sc = procField(rt.status || '', 'Seccomp');
  if (wantSeccomp) {
    const same = Boolean(gotSeccomp) && rt.seccompSha256 && rt.seccompSha256 === rt.repoSeccompSha256;
    line(Boolean(same) && sc === '2', service, 'seccomp', wantSeccomp, `${gotSeccomp} (same bytes: ${rt.seccompSha256 === rt.repoSeccompSha256}, Seccomp=${sc})`);
  } else {
    // compose names no profile: Docker's default. The guest runs Podman's
    // default, the same kind of filter.
    line(!gotSeccomp && sc === '2', service, 'seccomp', 'engine default', `${gotSeccomp || 'engine default'} (Seccomp=${sc})`);
  }

  const wantTmp = (want.tmpfs || []).slice().sort();
  const gotTmp = Object.keys(h.Tmpfs || {}).filter((p) => p !== '/dev/shm').sort();
  line(JSON.stringify(wantTmp) === JSON.stringify(gotTmp), service, 'tmpfs', wantTmp.join(','), `${gotTmp.join(',')} (+ /dev/shm, Docker's own)`);

  const wantVols = (want.volumes || []).filter((v) => v.type === 'volume').map((v) => `cseries_${v.source}:${v.target}`).sort();
  const gotVols = (got.Mounts || []).filter((m) => m.Type === 'volume').map((m) => `${m.Name}:${m.Destination}`).sort();
  line(JSON.stringify(wantVols) === JSON.stringify(gotVols), service, 'volumes', `${wantVols.length} named`, `${gotVols.length} named${JSON.stringify(wantVols) === JSON.stringify(gotVols) ? ', same names and targets' : `: ${gotVols.join(' ')}`}`);

  const wantPorts = (want.ports || []).map((p) => `${p.host_ip}:${p.published}->${p.target}`).sort();
  const gotPorts = [];
  const added = [];
  for (const [k, binds] of Object.entries(h.PortBindings || {})) {
    for (const b of binds) {
      const s = `${b.HostIp}:${b.HostPort}->${k.split('/')[0]}`;
      if (ADDED_PORTS.has(b.HostPort) && !wantPorts.includes(s)) added.push(s);
      else gotPorts.push(s);
    }
  }
  gotPorts.sort();
  const loopbackOnly = Object.values(h.PortBindings || {}).flat().every((b) => b.HostIp === '127.0.0.1');
  line(JSON.stringify(wantPorts) === JSON.stringify(gotPorts) && loopbackOnly, service, 'ports', wantPorts.join(',') || 'none',
    `${gotPorts.join(',') || 'none'}${added.length ? ` (+ ${added.join(',')}, design section 3)` : ''}`);

  const wantEnv = Object.entries(want.environment || {}).map(([k, v]) => `${k}=${v === null ? '' : v}`).sort();
  const gotEnv = (got.Config.Env || []).filter((e) => wantEnv.some((w) => w.split('=')[0] === e.split('=')[0])).sort();
  line(JSON.stringify(wantEnv) === JSON.stringify(gotEnv), service, 'environment', wantEnv.join(' ') || 'none', gotEnv.join(' ') || 'none');

  const restartOk = want.restart === 'unless-stopped' && rt.unitRestart === 'always';
  line(restartOk, service, 'restart', want.restart, `systemd Restart=${rt.unitRestart}`);
}

process.stdout.write(bad ? `\n${bad} setting(s) differ\n` : '\nevery compose hardening setting is in effect in the guest\n');
process.exit(bad ? 1 : 0);
