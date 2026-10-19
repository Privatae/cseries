# cseries-app-helper

The platform's Form-1 app runtime:
a static, stdlib-only Go fetch+cache proxy that is the ONLY server-side
code in a store app's pod. Configured entirely from the app's signed
manifest `network[]` (via the operator's descriptor ConfigMap); refuses
undeclared hosts, GET-only, HTTPS-only upstream, per-hop redirect
re-checks (max 3), 8MiB response cap, TTL cache with stale-while-error,
content-blind. No shell in the image — the kubelet probe is
`/app-helper -healthcheck`.

Build: `docker build -t cseries-app-helper:vX.Y.Z .`
Test:  `go test ./...` (14 tests, httptest-only, no network)
Measured at v0.1.0 build time: image 9.73MB; live e2e: /health ok, undeclared host 403, POST 405, real
api.weather.gov fetch 200 miss->hit, -healthcheck exit 0.
