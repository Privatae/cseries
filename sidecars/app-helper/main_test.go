// The helper's wall, cache, and honesty — tested against real HTTP servers
// (httptest), no network. What these pin, mapped to the design (§4) and the
// campaign's T-matrix:
//
//	T6 (egress wall)      — undeclared host 403 {"error":"undeclared_host"};
//	                        a redirect OFF the list (or down to http) is the
//	                        SAME 403 even from a declared host (the
//	                        open-redirect and downgrade holes); runaway
//	                        chains die at the hop cap.
//	cache contract        — miss -> hit -> (expiry) -> stale-while-error,
//	                        each state named in X-Helper-Cache.
//	GET-only              — POST 405.
//	size cap              — an oversized upstream is a 502, never cached.
//	concurrency cap       — parallel misses hold at most maxInFlight
//	                        upstream fetches (the pod-budget bound).
package main

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync"
	"testing"
	"time"
)

// testServer wires a helper whose "https" upstream is actually the given
// httptest server: the client's transport rewrites every dial to it, which
// keeps the ALLOWLIST logic (checked on the URL, pre-rewrite) fully real.
func testServer(t *testing.T, hosts []string, upstream *httptest.Server) *server {
	t.Helper()
	s := newServer(config{AppID: "t", Hosts: hosts,
		TTLs: map[string]int{"slow.example": 3600}})
	base := s.client.CheckRedirect
	s.client = &http.Client{
		Timeout:       caps.upstreamTimeout,
		CheckRedirect: base,
		Transport: &rewriteTransport{inner: http.DefaultTransport,
			target: upstream.Listener.Addr().String()},
	}
	return s
}

type rewriteTransport struct {
	inner  http.RoundTripper
	target string
}

func (rt *rewriteTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	u := *req.URL
	u.Scheme = "http"
	u.Host = rt.target
	r2 := req.Clone(req.Context())
	r2.URL = &u
	return rt.inner.RoundTrip(r2)
}

func get(t *testing.T, s *server, path string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(http.MethodGet, path, nil)
	w := httptest.NewRecorder()
	s.handleFetch(w, req)
	return w
}

func TestUndeclaredHostIs403(t *testing.T) {
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		io.WriteString(w, "data")
	}))
	defer up.Close()
	s := testServer(t, []string{"api.weather.gov"}, up)

	w := get(t, s, "/fetch/evil.example/anything")
	if w.Code != http.StatusForbidden {
		t.Fatalf("want 403, got %d", w.Code)
	}
	var body map[string]string
	_ = json.Unmarshal(w.Body.Bytes(), &body)
	if body["error"] != "undeclared_host" {
		t.Fatalf("refusal must name its class: %v", body)
	}
}

func TestDeclaredHostFetchesAndCaches(t *testing.T) {
	calls := 0
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		calls++
		w.Header().Set("Content-Type", "application/json")
		io.WriteString(w, `{"n":1}`)
	}))
	defer up.Close()
	s := testServer(t, []string{"api.weather.gov"}, up)

	w1 := get(t, s, "/fetch/api.weather.gov/points/1,2?units=si")
	w2 := get(t, s, "/fetch/api.weather.gov/points/1,2?units=si")
	if w1.Code != 200 || w2.Code != 200 {
		t.Fatalf("want 200/200, got %d/%d", w1.Code, w2.Code)
	}
	if got := w1.Header().Get("X-Helper-Cache"); got != "miss" {
		t.Fatalf("first fetch must be a miss, got %q", got)
	}
	if got := w2.Header().Get("X-Helper-Cache"); got != "hit" {
		t.Fatalf("second fetch must be a hit, got %q", got)
	}
	if calls != 1 {
		t.Fatalf("upstream must be called once, got %d", calls)
	}
	if ct := w2.Header().Get("Content-Type"); ct != "application/json" {
		t.Fatalf("content type must pass through, got %q", ct)
	}
}

func TestStaleWhileError(t *testing.T) {
	healthy := true
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		if !healthy {
			w.WriteHeader(500)
			return
		}
		io.WriteString(w, "good data")
	}))
	defer up.Close()
	s := testServer(t, []string{"api.weather.gov"}, up)

	if w := get(t, s, "/fetch/api.weather.gov/x"); w.Code != 200 {
		t.Fatalf("prime failed: %d", w.Code)
	}
	// expire the entry, then break the upstream
	s.mu.Lock()
	for _, e := range s.cache {
		e.storedAt = time.Now().Add(-2 * caps.defaultTTL)
	}
	s.mu.Unlock()
	healthy = false

	w := get(t, s, "/fetch/api.weather.gov/x")
	if w.Code != 200 || w.Body.String() != "good data" {
		t.Fatalf("stale-while-error must serve the last good payload: %d %q",
			w.Code, w.Body.String())
	}
	if got := w.Header().Get("X-Helper-Cache"); got != "stale" {
		t.Fatalf("staleness must be named, got %q", got)
	}
}

func TestNeverCachedUpstreamFailureIsHonest502(t *testing.T) {
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(500)
	}))
	defer up.Close()
	s := testServer(t, []string{"api.weather.gov"}, up)
	if w := get(t, s, "/fetch/api.weather.gov/x"); w.Code != http.StatusBadGateway {
		t.Fatalf("want 502, got %d", w.Code)
	}
}

func TestRedirectOffTheListIsRefused(t *testing.T) {
	// A DECLARED host answers with a redirect to an UNDECLARED one — the
	// open-redirect exfil lane. The hop check must refuse it.
	var up *httptest.Server
	up = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasPrefix(r.URL.Path, "/bounce") {
			w.Header().Set("Location", "https://evil.example/collect?x=secret")
			w.WriteHeader(302)
			return
		}
		io.WriteString(w, "ok")
	}))
	defer up.Close()
	s := testServer(t, []string{"api.weather.gov"}, up)

	w := get(t, s, "/fetch/api.weather.gov/bounce")
	if w.Code != http.StatusForbidden {
		t.Fatalf("off-list redirect must be the same 403 as a direct undeclared host, got %d", w.Code)
	}
	var body map[string]string
	_ = json.Unmarshal(w.Body.Bytes(), &body)
	if body["error"] != "undeclared_host" || body["via"] != "redirect" {
		t.Fatalf("refusal must name its class and lane: %v", body)
	}
}

func TestRedirectSchemeDowngradeIsRefused(t *testing.T) {
	// A DECLARED host redirects to ITSELF over plain http — the downgrade
	// lane. The per-hop check must refuse it even though the host is fine.
	var up *httptest.Server
	up = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasPrefix(r.URL.Path, "/insecure") {
			w.Header().Set("Location", "http://api.weather.gov/plain")
			w.WriteHeader(302)
			return
		}
		io.WriteString(w, "ok")
	}))
	defer up.Close()
	s := testServer(t, []string{"api.weather.gov"}, up)

	w := get(t, s, "/fetch/api.weather.gov/insecure")
	if w.Code != http.StatusForbidden {
		t.Fatalf("scheme downgrade must be refused with 403, got %d", w.Code)
	}
}

func TestRedirectHopCap(t *testing.T) {
	// An endless on-list redirect chain must die at the hop cap, and the
	// failure is a plain upstream failure (502) — nothing was cached.
	var up *httptest.Server
	up = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Location", "https://api.weather.gov"+r.URL.Path+"x")
		w.WriteHeader(302)
	}))
	defer up.Close()
	s := testServer(t, []string{"api.weather.gov"}, up)

	w := get(t, s, "/fetch/api.weather.gov/loop")
	if w.Code != http.StatusBadGateway {
		t.Fatalf("runaway redirect chain must 502 at the hop cap, got %d", w.Code)
	}
	s.mu.Lock()
	n := len(s.cache)
	s.mu.Unlock()
	if n != 0 {
		t.Fatalf("a refused chain must never be cached, cache has %d entries", n)
	}
}

func TestRedirectWithinTheListIsFollowed(t *testing.T) {
	var up *httptest.Server
	up = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasPrefix(r.URL.Path, "/old") {
			w.Header().Set("Location", "https://api.weather.gov/new")
			w.WriteHeader(302)
			return
		}
		io.WriteString(w, "moved data")
	}))
	defer up.Close()
	s := testServer(t, []string{"api.weather.gov"}, up)

	w := get(t, s, "/fetch/api.weather.gov/old")
	if w.Code != 200 || w.Body.String() != "moved data" {
		t.Fatalf("on-list redirect must be followed: %d %q", w.Code, w.Body.String())
	}
}

func TestGetOnly(t *testing.T) {
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		io.WriteString(w, "x")
	}))
	defer up.Close()
	s := testServer(t, []string{"api.weather.gov"}, up)
	req := httptest.NewRequest(http.MethodPost, "/fetch/api.weather.gov/x",
		strings.NewReader("payload"))
	w := httptest.NewRecorder()
	s.handleFetch(w, req)
	if w.Code != http.StatusMethodNotAllowed {
		t.Fatalf("POST must 405, got %d", w.Code)
	}
}

func TestOversizedUpstreamIs502AndUncached(t *testing.T) {
	big := strings.Repeat("a", int(caps.maxBodyBytes)+100)
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		io.WriteString(w, big)
	}))
	defer up.Close()
	s := testServer(t, []string{"api.weather.gov"}, up)
	if w := get(t, s, "/fetch/api.weather.gov/big"); w.Code != http.StatusBadGateway {
		t.Fatalf("oversize must 502, got %d", w.Code)
	}
	s.mu.Lock()
	n := len(s.cache)
	s.mu.Unlock()
	if n != 0 {
		t.Fatalf("oversize must never be cached, cache has %d entries", n)
	}
}

func TestEvictionKeepsTheCacheUnderTheCeiling(t *testing.T) {
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		io.WriteString(w, strings.Repeat("b", 1<<20)) // 1MiB per entry
	}))
	defer up.Close()
	s := testServer(t, []string{"api.weather.gov"}, up)
	old := caps.maxCacheBytes
	caps.maxCacheBytes = 3 << 20
	defer func() { caps.maxCacheBytes = old }()

	for i := 0; i < 6; i++ {
		get(t, s, "/fetch/api.weather.gov/item?"+url.Values{"i": {string(rune('a' + i))}}.Encode())
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.cacheBytes > caps.maxCacheBytes {
		t.Fatalf("cache %d bytes exceeds ceiling %d", s.cacheBytes, caps.maxCacheBytes)
	}
}

func TestPerHostTTLOverride(t *testing.T) {
	s := newServer(config{Hosts: []string{"slow.example"},
		TTLs: map[string]int{"slow.example": 3600}})
	if got := s.ttlFor("slow.example"); got != time.Hour {
		t.Fatalf("per-host TTL override: %v", got)
	}
	if got := s.ttlFor("other.example"); got != caps.defaultTTL {
		t.Fatalf("default TTL: %v", got)
	}
}

func TestTTLKeysNormalizedLikeTheAllowlist(t *testing.T) {
	// A mixed-case host in the descriptor's ttls must still override —
	// the allowlist lowercases, so the TTL map must too.
	s := newServer(config{Hosts: []string{"Slow.Example"},
		TTLs: map[string]int{" Slow.Example ": 3600}})
	if got := s.ttlFor("slow.example"); got != time.Hour {
		t.Fatalf("mixed-case TTL key must be normalized, got %v", got)
	}
}

func TestUpstreamConcurrencyIsBounded(t *testing.T) {
	// The maxInFlight bound: N slow misses in parallel may only hold
	// maxInFlight upstream fetches (and so maxInFlight body buffers) at
	// once — the reviewed OOMKill lane for a community app's parallel JS.
	oldCap := caps.maxInFlight
	caps.maxInFlight = 2
	defer func() { caps.maxInFlight = oldCap }()

	var mu sync.Mutex
	inFlight, peak := 0, 0
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		mu.Lock()
		inFlight++
		if inFlight > peak {
			peak = inFlight
		}
		mu.Unlock()
		time.Sleep(50 * time.Millisecond)
		mu.Lock()
		inFlight--
		mu.Unlock()
		io.WriteString(w, "slow")
	}))
	defer up.Close()
	s := testServer(t, []string{"api.weather.gov"}, up)

	var wg sync.WaitGroup
	for i := 0; i < 6; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			w := get(t, s, "/fetch/api.weather.gov/slow?i="+string(rune('a'+i)))
			if w.Code != 200 {
				t.Errorf("bounded fetch %d: want 200, got %d", i, w.Code)
			}
		}(i)
	}
	wg.Wait()
	if peak > 2 {
		t.Fatalf("upstream concurrency must be capped at 2, saw %d", peak)
	}
}
