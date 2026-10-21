# cseries-app-helper

The server side of a helper-form store app: a static, standard-library-only
Go fetch-and-cache proxy, and the only container such an app runs. It fetches
public data from the hosts the app declared, on behalf of the app's browser
code.

- Configured from a JSON file (`CONFIG_PATH`, default `/config/config.json`)
  naming the app, its declared hosts and optional per-host cache TTLs. The
  local runtime writes it from the app's descriptor `network[]`.
- `GET /fetch/<host>/<path>`: a host not on the list is refused with 403;
  any method other than GET (and the CORS preflight) with 405.
- Upstream is HTTPS only. Redirects are followed at most 3 hops, each hop
  re-checked against the list; an off-list hop is the same 403.
- Responses are capped at 8 MiB and cached with a TTL; when the upstream
  fails, the last good response is served marked stale.
- It logs startup and refusals by class only, never a request or a
  destination.
- Outbound traffic goes through `HTTP_PROXY`/`HTTPS_PROXY`; the local runtime
  points both at the Host's egress gate.
- The image has no shell. `/app-helper -healthcheck` checks the running
  instance's `/health` and exits 0 or 1.

Build: `docker build -t cseries-app-helper:dev .`
Test: `go test ./...` (no network needed)
