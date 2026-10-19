# Personal Computer — Manual Test Checklist

Tests that require a running container, browser, and/or relay infrastructure.
Run after automated tests pass (`python3 tests/test_automated.py`).

---

## Phase A: Container Build & Start

### A1. Docker Build
```bash
cd personal-computer
docker build -t personal-computer:test .
```
- [ ] Build completes without errors
- [ ] Image size is under 500MB (Alpine + minimal deps)
- [ ] No warnings about deprecated instructions

### A2. Generate Test Certs
```bash
# Self-signed test certs (not for production)
mkdir -p /tmp/pc-test-certs && cd /tmp/pc-test-certs
openssl ecparam -name prime256v1 -genkey -noout -out ca.key
openssl req -new -x509 -key ca.key -out ca.crt -days 1 -subj "/CN=CEIGAS-TEST-CA"
openssl ecparam -name prime256v1 -genkey -noout -out relay.key
openssl req -new -key relay.key -out relay.csr -subj "/CN=entity-77"
openssl x509 -req -in relay.csr -CA ca.crt -CAkey ca.key -CAcreateserial -out relay.crt -days 1
echo '{"entity_id": 77, "tunnel_url": "wss://tunnel.example.test:8700/ws"}' > relay.json
```

### A3. Container Start
```bash
cd personal-computer
ENTITY_ID=77 \
RELAY_CONFIG=$(base64 -w0 /tmp/pc-test-certs/relay.json) \
RELAY_CERT=$(base64 -w0 /tmp/pc-test-certs/relay.crt) \
RELAY_KEY=$(base64 -w0 /tmp/pc-test-certs/relay.key) \
CA_CERT=$(base64 -w0 /tmp/pc-test-certs/ca.crt) \
docker compose up -d
```
- [ ] Container starts without crashing
- [ ] `docker logs privatae-pc-77` shows "Personal computer starting for entity 77"
- [ ] No errors in logs about cert validation or config

### A4. Health Check
```bash
curl -s http://127.0.0.1:8800/api/system/health | python3 -m json.tool
```
- [ ] Returns `{"status": "ok", "uptime_s": ...}`
- [ ] Response within 1 second

### A5. Status Endpoint
```bash
curl -s http://127.0.0.1:8800/api/system/status | python3 -m json.tool
```
- [ ] Returns entity_id: 77
- [ ] Returns dashboard_port: 8800
- [ ] Has timestamp field

---

## Phase B: Dashboard Visual

### B1. Page Load
Open `http://127.0.0.1:8800` in browser.
- [ ] Page loads without console errors
- [ ] Dark theme renders (near-black background #0a0a0a)
- [ ] Taskbar visible at bottom
- [ ] Desktop icons visible (Files, Apps)
- [ ] Clock shows current time in taskbar
- [ ] Entity status shows "connecting..." (expected — no relay backend)

### B2. Desktop Icons
- [ ] Files icon visible with folder emoji
- [ ] Apps icon visible with package emoji
- [ ] Hover state shows subtle highlight
- [ ] Double-click Files opens file explorer window
- [ ] Double-click Apps opens app launcher window

### B3. Window Manager
Open the Files window:
- [ ] Window appears centered-ish on screen
- [ ] Title bar shows "Files" with folder icon
- [ ] Minimize, maximize, close buttons visible
- [ ] Dragging title bar moves window
- [ ] Double-click title bar toggles maximize
- [ ] Minimize hides window, taskbar tab dims
- [ ] Click taskbar tab restores minimized window
- [ ] Close button removes window and taskbar tab
- [ ] Resize handles work (drag corners/edges)
- [ ] Window has subtle shadow, focused state has stronger shadow

### B4. Multiple Windows
Open both Files and Apps:
- [ ] Both windows render
- [ ] Both have taskbar tabs
- [ ] Clicking a window brings it to front (z-index)
- [ ] Active taskbar tab highlights correctly
- [ ] Closing one doesn't affect the other

---

## Phase C: File Explorer

### C1. Directory Listing
```bash
# Create test files in workspace volume
docker exec privatae-pc-77 sh -c "echo 'hello' > /app/workspace/test.txt"
docker exec privatae-pc-77 sh -c "mkdir -p /app/workspace/subdir"
docker exec privatae-pc-77 sh -c "echo 'nested' > /app/workspace/subdir/nested.txt"
```
- [ ] File explorer shows test.txt and subdir/
- [ ] Directories shown with folder icon
- [ ] Files shown with appropriate type icon
- [ ] Statusbar shows item count

### C2. Navigation
- [ ] Double-click subdir/ navigates into it
- [ ] Breadcrumb shows "Workspace / subdir"
- [ ] Back button returns to workspace root
- [ ] Up button navigates to parent
- [ ] Breadcrumb clicks navigate correctly

### C3. View Toggle
- [ ] Grid view shows icon + name tiles
- [ ] List view shows name, size, date columns
- [ ] Toggle button highlights active view

### C4. API Direct
```bash
curl -s "http://127.0.0.1:8800/api/files/list?path=" | python3 -m json.tool
curl -s "http://127.0.0.1:8800/api/files/read?path=test.txt" | python3 -m json.tool
curl -s "http://127.0.0.1:8800/api/files/index" | python3 -m json.tool
```
- [ ] list returns entries array with test.txt and subdir
- [ ] read returns content "hello\n"
- [ ] index returns recursive file listing

### C5. Path Traversal Blocked
```bash
curl -s "http://127.0.0.1:8800/api/files/read?path=../etc/passwd"
curl -s "http://127.0.0.1:8800/api/files/read?path=/etc/passwd"
```
- [ ] Both return 403 with "path traversal blocked" or "resolves outside sandbox"

---

## Phase D: WebSocket

### D1. Connection
Open browser console on dashboard:
```javascript
// Should already be connected by ws-client.js
// Check status dot color
```
- [ ] WebSocket connection established (check Network tab)
- [ ] Status dot is green when connected
- [ ] No rapid reconnect loops in console

### D2. Message Echo
```javascript
// In browser console:
WSClient.send({type: "test", module: "debug", data: {hello: "world"}});
```
- [ ] No errors in console
- [ ] Server logs show WS message received

---

## Phase E: App Launcher

### E1. Empty State
- [ ] App launcher window opens
- [ ] Shows "No apps installed yet"
- [ ] Shows "Your entity will build apps here"

### E2. API
```bash
curl -s http://127.0.0.1:8800/api/apps/list | python3 -m json.tool
```
- [ ] Returns `{"apps": [], "total": 0}`

---

## Phase F: Security Verification

### F1. Localhost Only
```bash
# From another machine on the LAN (or different IP):
curl -s http://<server-ip>:8800/api/system/health
```
- [ ] Connection refused (port bound to 127.0.0.1 only)

### F2. Read-Only Filesystem
```bash
docker exec privatae-pc-77 sh -c "touch /test-readonly" 2>&1
```
- [ ] Fails with "Read-only file system"

### F3. No Root Access
```bash
docker exec privatae-pc-77 whoami
docker exec privatae-pc-77 id
```
- [ ] Shows user "privatae" with uid=1000
- [ ] NOT root

### F4. Capabilities Dropped
```bash
docker exec privatae-pc-77 sh -c "cat /proc/1/status | grep Cap"
```
- [ ] CapEff shows 0000000000000000 (no capabilities)

### F5. Secrets Cleared
```bash
docker exec privatae-pc-77 sh -c "env | grep -i 'relay\|ca_cert\|claude_auth'"
```
- [ ] No output (env vars were unset by entrypoint)

### F6. Write to Workspace Only
```bash
curl -s -X POST http://127.0.0.1:8800/api/files/write \
  -H "Content-Type: application/json" \
  -d '{"path": "../etc/test", "content": "hack"}'
```
- [ ] Returns 403

---

## Phase G: Overlay Filesystem

### G1. Base Dashboard Served
- [ ] Default theme renders (dark, #0a0a0a background)
- [ ] All CSS/JS files load (check Network tab, no 404s)

### G2. Custom Override
```bash
# Write a custom theme override
docker exec privatae-pc-77 sh -c "mkdir -p /app/dashboard/custom/css"
docker exec privatae-pc-77 sh -c "echo ':root { --accent-primary: #ff4444; }' > /app/dashboard/custom/css/theme.css"
```
Reload the dashboard page:
- [ ] Accent color changes to red (#ff4444)
- [ ] Other theme values still come from base (not broken)

### G3. Custom Removal
```bash
docker exec privatae-pc-77 sh -c "rm /app/dashboard/custom/css/theme.css"
```
Reload:
- [ ] Accent color reverts to blue (#4a9eff)

---

## Phase H: Cleanup

```bash
docker compose -f personal-computer/docker-compose.yml down -v
rm -rf /tmp/pc-test-certs
```
- [ ] Container stopped and removed
- [ ] Volumes cleaned up
- [ ] Test certs deleted

---

## Results

| Phase | Tests | Pass | Fail | Notes |
|-------|-------|------|------|-------|
| A: Build & Start | 5 | | | |
| B: Dashboard Visual | 4 | | | |
| C: File Explorer | 5 | | | |
| D: WebSocket | 2 | | | |
| E: App Launcher | 2 | | | |
| F: Security | 6 | | | |
| G: Overlay | 3 | | | |
| H: Cleanup | 1 | | | |
| **Total** | **28** | | | |
