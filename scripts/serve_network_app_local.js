#!/usr/bin/env node
/*
 * serve_network_app_local.js — serve the Network system app from the WORKING
 * TREE to a real browser, with the /api/network/* responses stubbed.
 *
 * Why this exists: scripts/test_network_views.js proves the renderers are pure
 * graph → markup functions, but a string containing "<svg" is not a drawing.
 * Only a browser engine proves the markup parses and paints. This puts the real
 * files (real index.html, real app.js, real style.css, real shell CSS) behind a
 * real HTTP server so the app can be exercised from where the user stands.
 *
 * It is a DEV HARNESS: read-only, fixtures inline. It serves the source tree,
 * so what the browser runs is exactly what was edited.
 *
 * BIND: 127.0.0.1, deliberately and permanently. Nothing off this box may reach
 * it — the only client is a browser running on the same machine as the checkout
 * it serves, and it serves repo files with no auth. A wider bind would publish
 * an unauthenticated read of the source tree onto the network. This is NOT the
 * cseries_sidecar class of loopback bug (a service something else must dial); it
 * is same-box tooling and must stay that way.
 *
 * Run: node scripts/serve_network_app_local.js [port]
 */
'use strict';

const http = require('http');
const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..');
const PORT = Number(process.argv[2] || 8791);

const TYPES = {
    '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml', '.png': 'image/png',
    '.json': 'application/json',
};

// ── fixtures: one live machine, one that has never reported, one peer ────
const NOW = new Date().toISOString();
const FIXTURES = {
    '/api/network/overview': {
        identity: { handle: 'acme', entity_id: 42 },
        bridge: { connected: true },
        counts: { bastions: 3, bastions_online: 2, secured_channels: 1, contacts: 1, unrequested_agents: 0 },
        peer: { trust_ready: true },
    },
    '/api/network/bastions': {
        bastions: [
            {
                id: 7, name: 'desk', hostname: 'desktop-01', platform: 'linux',
                online: true, last_seen_at: NOW, reported_ip: '2001:db8::a:7',
                capabilities: ['embed'], models: ['mxbai-embed-large'],
                tunnel_status: 'up', bastion_version: '0.13.12', scopes: [],
                enrollment: {
                    wg_bastion_pubkey: 'PUB1', wg_endpoint: 'edge-a.example.test:51820',
                    wg_host_ip: '2001:db8::e:42', wg_bastion_ip: '2001:db8::a:7',
                },
                // This box also serves two other Hosts, and someone with root
                // on it said who owns it.
                peers: [
                    { host_name: 'beta.zone.example.test', handle: 'beta', since: '2026-09-01T00:00:00Z' },
                    { host_name: 'gamma.zone.example.test', handle: 'gamma', since: '2026-09-05T00:00:00Z' },
                ],
                owner_claim: { owner: 'Acme IT', claimed_at: '2026-09-07T12:00:00Z',
                               public_key: 'PUBKEY', signature: 'SIG' },
            },
            {
                id: 9, name: 'shed', hostname: 'rack-nas-02', platform: 'darwin',
                online: true, last_seen_at: NOW, reported_ip: '2001:db8::a:9',
                capabilities: [], models: [], tunnel_status: 'up', scopes: [],
                enrollment: {
                    wg_bastion_pubkey: 'PUB2', wg_endpoint: 'edge-b.example.test:51820',
                    wg_host_ip: '2001:db8::e:42', wg_bastion_ip: '2001:db8::a:9',
                },
                peers: [{ host_name: 'beta.zone.example.test', handle: 'beta', since: '' }],
            },
            {   // never reported: must still be drawn, as down
                id: 8, name: 'nas', hostname: null, platform: null,
                online: false, last_seen_at: null, reported_ip: null,
                capabilities: [], models: [], scopes: [], enrollment: null,
            },
        ],
    },
    '/api/network/peers': {
        secured_channels: [{ peer_entity_id: 99 }],
        contacts: [{ id: 5, kind: 'host', status: 'active', display_name: 'Beta Host' }],
    },
    // A host-authored map, as the appearance pointer would serve it. Reads the
    // graph off the bridge — it cannot type its own nodes and pass the floor.
    '/api/appearance/network/meta': { artifact_id: 3, entry: 'index.html', updated_at: '2026-09-07T21:00:00Z' },
    '/api/embedding/current': { provider: 'bastion', model: 'mxbai-embed-large', dim: 1024 },
    '/api/domain-policy': { posture: 'contacts' },
};

// Map a URL path onto the source tree, exactly as the pod serves it.
function resolve(urlPath) {
    if (urlPath === '/' || urlPath === '') urlPath = '/system-apps/network/index.html';
    if (urlPath.startsWith('/system-apps/')) return path.join(ROOT, urlPath.replace(/^\//, ''));
    if (urlPath.startsWith('/css/')) return path.join(ROOT, 'dashboard', 'base', 'css', urlPath.slice(5));
    if (urlPath.startsWith('/js/')) return path.join(ROOT, 'dashboard', 'base', 'js', urlPath.slice(4));
    // The shell CSS pulls its own webfonts. Without this the page still renders
    // (fallback fonts) but throws 404s that would mask a real console error.
    if (urlPath.startsWith('/fonts/')) return path.join(ROOT, 'dashboard', 'base', 'fonts', urlPath.slice(7));
    return null;
}

const server = http.createServer((req, res) => {
    const urlPath = decodeURIComponent((req.url || '/').split('?')[0]);

    if (urlPath === '/api/appearance/network') {
        res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
        return res.end(`<!DOCTYPE html><html><head>
<meta name="cseries-network" content="map">
<style>body{margin:0;font:12px system-ui;color:#cfd6d3;background:transparent}
h1{font-size:13px;margin:14px 16px 8px;color:#e8efe9}
.cseries-node{padding:5px 16px;border-left:2px solid #4a7c59;margin:3px 0;cursor:pointer}</style>
</head><body>
<h1 id="t">my network</h1>
<div data-cseries-nodes></div>
<script>
  CSeriesNetwork.on(function (s) {
    document.getElementById('t').textContent =
      (s.host ? s.host.label : 'my network') + ' — ' + s.nodes.length + ' things';
  });
</script>
</body></html>`);
    }

    for (const [route, body] of Object.entries(FIXTURES)) {
        if (urlPath === route) {
            res.writeHead(200, { 'Content-Type': 'application/json' });
            return res.end(JSON.stringify(body));
        }
    }
    if (urlPath.startsWith('/api/') || urlPath.startsWith('/nac/')) {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        return res.end('{}');
    }

    const file = resolve(urlPath);
    if (!file) { res.writeHead(404); return res.end('no route: ' + urlPath); }
    // Never serve outside the repo.
    if (!path.resolve(file).startsWith(path.resolve(ROOT))) { res.writeHead(403); return res.end('nope'); }
    fs.readFile(file, (err, buf) => {
        if (err) { res.writeHead(404); return res.end('no such file: ' + urlPath); }
        res.writeHead(200, { 'Content-Type': TYPES[path.extname(file)] || 'application/octet-stream' });
        res.end(buf);
    });
});

server.listen(PORT, '127.0.0.1', () => {
    console.log(`network app (working tree) on http://127.0.0.1:${PORT}/`);
});
