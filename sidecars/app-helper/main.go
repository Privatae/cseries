// cseries-app-helper — the platform's Form-1 app runtime.
//
// One job: fetch public data from the hosts an app DECLARED, on behalf of
// that app's browser code, with a short cache. It is the server side of a
// store app — and the reason it can be trusted on anyone's machine is that
// it is THIS code, not the app author's: the only thing in the pod that
// dials out, and it refuses any host not on the declared list.
//
// Deliberate properties, each one load-bearing:
//
//   - ALLOWLIST WALL: /fetch/<host>/... 403s any host absent from the
//     config's list. The list is written by the operator from the app's
//     signed manifest network[] — the same list that pins the browser CSP,
//     so the client side and server side agree by construction.
//   - GET-ONLY: a helper that can POST is an exfiltration pump. 405.
//   - NO REDIRECT-FOLLOWING past the wall: Go's default client chases
//     redirects, so a declared host with an open redirect would forward
//     the fetch (query string included) anywhere. Every hop re-checks the
//     allowlist; max 3 hops; an off-list hop is the same 403 as a direct
//     undeclared host.
//   - HTTPS-ONLY upstream: a bare-hostname allowlist must not become a
//     downgrade lane.
//   - STALE-WHILE-ERROR: a dead upstream serves the last good payload,
//     marked stale — the same contract the weather app's own backend had
//     (its serve() semantics), because that is the shape this replaces.
//   - CONTENT-BLIND: no per-request logging, no destination logging —
//     platform egress doctrine. Startup and refusals-by-CLASS only.
//   - It holds NO credential and reads NO header from the caller beyond
//     the request line: reached only through the kernel proxy, which owns
//     authentication (token app_id == path app_id).
//
// Egress rides HTTP_PROXY/HTTPS_PROXY (the node squid) via Go's default
// ProxyFromEnvironment — the platform chokepoint underneath the wall.
package main

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"sort"
	"strings"
	"sync"
	"time"
)

// caps — every tunable in one place, a comment per choice.
var caps = struct {
	defaultTTL      time.Duration // per-source override via config ttls
	maxBodyBytes    int64         // upstream cap; bigger is a 502, never cached
	upstreamTimeout time.Duration // one fetch, end to end
	maxRedirects    int           // hops, each re-checked against the wall
	maxCacheBytes   int64         // in-RAM cache ceiling — the pod's budget
	// is the cgroup limit; the cache must stay well under it
	maxInFlight int // concurrent UPSTREAM fetches (cache hits don't count):
	// each miss buffers up to maxBodyBytes, so this bounds transient memory
	// at maxInFlight*maxBodyBytes = 64MiB — which, plus the 32MiB cache and
	// the Go runtime, stays under the 128Mi community pod budget. Excess
	// requests queue (a goroutine each, ~KB) rather than 429: the only
	// caller is the same entity's kernel proxy, whose own client timeout
	// bounds the wait.
}{
	defaultTTL:      300 * time.Second,
	maxBodyBytes:    8 << 20,
	upstreamTimeout: 30 * time.Second,
	maxRedirects:    3,
	maxCacheBytes:   32 << 20,
	maxInFlight:     8,
}

type config struct {
	AppID string         `json:"app_id"`
	Port  int            `json:"port"`
	Hosts []string       `json:"hosts"`
	TTLs  map[string]int `json:"ttls"`
}

type entry struct {
	body        []byte
	contentType string
	storedAt    time.Time
	ttl         time.Duration
}

func (e *entry) expired() bool { return time.Since(e.storedAt) > e.ttl }

type server struct {
	cfg     config
	allowed map[string]bool
	ttls    map[string]time.Duration // normalized like allowed, or the
	// TTL for a mixed-case descriptor host would silently fall to default
	client *http.Client
	sem    chan struct{} // the maxInFlight bound on upstream fetches

	mu         sync.Mutex
	cache      map[string]*entry
	cacheBytes int64
}

var errOffList = errors.New("redirect target not on the declared host list")

func newServer(cfg config) *server {
	allowed := make(map[string]bool, len(cfg.Hosts))
	for _, h := range cfg.Hosts {
		allowed[strings.ToLower(strings.TrimSpace(h))] = true
	}
	ttls := make(map[string]time.Duration, len(cfg.TTLs))
	for h, sec := range cfg.TTLs {
		if sec > 0 {
			ttls[strings.ToLower(strings.TrimSpace(h))] = time.Duration(sec) * time.Second
		}
	}
	s := &server{cfg: cfg, allowed: allowed, ttls: ttls,
		cache: map[string]*entry{},
		sem:   make(chan struct{}, caps.maxInFlight)}
	s.client = &http.Client{
		Timeout: caps.upstreamTimeout,
		// Transport default = ProxyFromEnvironment: the node squid.
		CheckRedirect: func(req *http.Request, via []*http.Request) error {
			if len(via) >= caps.maxRedirects {
				return fmt.Errorf("too many redirects (max %d)", caps.maxRedirects)
			}
			if req.URL.Scheme != "https" || !s.allowed[strings.ToLower(req.URL.Hostname())] {
				return errOffList
			}
			return nil
		},
	}
	return s
}

func (s *server) ttlFor(host string) time.Duration {
	if ttl, ok := s.ttls[host]; ok {
		return ttl
	}
	return caps.defaultTTL
}

// evict expired entries, then oldest, until under the ceiling. Caller holds mu.
func (s *server) evictLocked() {
	for k, e := range s.cache {
		if e.expired() {
			s.cacheBytes -= int64(len(e.body))
			delete(s.cache, k)
		}
	}
	for s.cacheBytes > caps.maxCacheBytes {
		oldestK, oldestT := "", time.Now()
		for k, e := range s.cache {
			if e.storedAt.Before(oldestT) {
				oldestK, oldestT = k, e.storedAt
			}
		}
		if oldestK == "" {
			return
		}
		s.cacheBytes -= int64(len(s.cache[oldestK].body))
		delete(s.cache, oldestK)
	}
}

func cors(w http.ResponseWriter) {
	w.Header().Set("Access-Control-Allow-Origin", "*")
	w.Header().Set("Access-Control-Allow-Methods", "GET, OPTIONS")
}

func (s *server) handleHealth(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(map[string]any{
		"ok": true, "app_id": s.cfg.AppID, "hosts": len(s.allowed),
	})
}

func (s *server) handleFetch(w http.ResponseWriter, r *http.Request) {
	cors(w)
	if r.Method == http.MethodOptions {
		w.WriteHeader(http.StatusNoContent)
		return
	}
	if r.Method != http.MethodGet {
		http.Error(w, `{"error":"helper is GET-only"}`, http.StatusMethodNotAllowed)
		return
	}
	// /fetch/<host>/<path...>
	rest := strings.TrimPrefix(r.URL.Path, "/fetch/")
	host, path, _ := strings.Cut(rest, "/")
	host = strings.ToLower(host)
	if host == "" || !s.allowed[host] {
		// The wall. The class is logged (T6's assertion), never the payload.
		log.Printf("refused: undeclared host class (app %s)", s.cfg.AppID)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusForbidden)
		_ = json.NewEncoder(w).Encode(map[string]string{
			"error": "undeclared_host", "host": host,
		})
		return
	}

	// "\n" separators: a literal "?" decoded into the path can't alias a
	// path+query pair into the same key (newlines survive in neither part).
	key := host + "\n" + path + "\n" + r.URL.RawQuery

	s.mu.Lock()
	cached, ok := s.cache[key]
	s.mu.Unlock()
	if ok && !cached.expired() {
		serveEntry(w, cached, "hit")
		return
	}

	// The maxInFlight bound: only requests that actually dial upstream (and
	// so may buffer up to maxBodyBytes each) take a slot; hits served above
	// never wait here.
	s.sem <- struct{}{}
	defer func() { <-s.sem }()

	upstream := &url.URL{Scheme: "https", Host: host, Path: "/" + path,
		RawQuery: r.URL.RawQuery}
	resp, err := s.client.Get(upstream.String())
	if err == nil && resp.StatusCode >= 400 {
		resp.Body.Close()
		err = fmt.Errorf("upstream status %d", resp.StatusCode)
		resp = nil
	}
	if err != nil {
		// An off-list redirect hop is the same wall as a direct undeclared
		// host — same 403, same error class (the design's and this file's
		// header's promise) — and it is never papered over with stale data.
		if errors.Is(err, errOffList) || strings.Contains(err.Error(), errOffList.Error()) {
			log.Printf("refused: undeclared redirect-target class (app %s)", s.cfg.AppID)
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusForbidden)
			_ = json.NewEncoder(w).Encode(map[string]string{
				"error": "undeclared_host",
				"via":   "redirect",
			})
			return
		}
		if ok { // stale-while-error: the last good payload, marked honestly
			serveEntry(w, cached, "stale")
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusBadGateway)
		_ = json.NewEncoder(w).Encode(map[string]string{
			"error": "upstream fetch failed and no cached data is available"})
		return
	}
	defer resp.Body.Close()
	body, rerr := io.ReadAll(io.LimitReader(resp.Body, caps.maxBodyBytes+1))
	if rerr != nil || int64(len(body)) > caps.maxBodyBytes {
		if ok {
			serveEntry(w, cached, "stale")
			return
		}
		http.Error(w, `{"error":"upstream response too large or unreadable"}`,
			http.StatusBadGateway)
		return
	}

	e := &entry{body: body, contentType: resp.Header.Get("Content-Type"),
		storedAt: time.Now(), ttl: s.ttlFor(host)}
	s.mu.Lock()
	if old, existed := s.cache[key]; existed {
		s.cacheBytes -= int64(len(old.body))
	}
	s.cache[key] = e
	s.cacheBytes += int64(len(body))
	s.evictLocked()
	s.mu.Unlock()
	serveEntry(w, e, "miss")
}

func serveEntry(w http.ResponseWriter, e *entry, status string) {
	if e.contentType != "" {
		w.Header().Set("Content-Type", e.contentType)
	}
	w.Header().Set("X-Helper-Cache", status)
	w.Header().Set("X-Helper-Cached-At", e.storedAt.UTC().Format(time.RFC3339))
	_, _ = w.Write(e.body)
}

func loadConfig() (config, error) {
	path := os.Getenv("CONFIG_PATH")
	if path == "" {
		path = "/config/config.json"
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return config{}, err
	}
	var cfg config
	if err := json.Unmarshal(raw, &cfg); err != nil {
		return config{}, err
	}
	if len(cfg.Hosts) == 0 {
		return config{}, errors.New("config has no hosts — a helper with no allowlist is nothing")
	}
	return cfg, nil
}

func main() {
	healthcheck := flag.Bool("healthcheck", false,
		"probe the running instance's /health and exit (the exec probe)")
	flag.Parse()

	port := os.Getenv("PORT")
	if port == "" {
		port = "8850"
	}
	if *healthcheck {
		c := http.Client{Timeout: 3 * time.Second}
		resp, err := c.Get("http://localhost:" + port + "/health")
		if err != nil || resp.StatusCode != 200 {
			os.Exit(1)
		}
		os.Exit(0)
	}

	cfg, err := loadConfig()
	if err != nil {
		log.Fatalf("config: %v", err)
	}
	// The operator sets PORT env and writes the same port into the config;
	// the config is the fallback so neither copy is a dead field.
	if os.Getenv("PORT") == "" && cfg.Port > 0 {
		port = fmt.Sprintf("%d", cfg.Port)
	}
	s := newServer(cfg)

	mux := http.NewServeMux()
	mux.HandleFunc("/health", s.handleHealth)
	mux.HandleFunc("/fetch/", s.handleFetch)

	bind := os.Getenv("BIND")
	if bind == "" {
		bind = "::" // dual-stack: the kernel proxy dials the pod's IPv6 IP
	}
	addr := net.JoinHostPort(bind, port)
	hosts := append([]string(nil), cfg.Hosts...)
	sort.Strings(hosts)
	log.Printf("cseries-app-helper: app=%s addr=%s hosts=%d ttl_default=%s cap=%dMiB inflight=%d",
		cfg.AppID, addr, len(hosts), caps.defaultTTL, caps.maxBodyBytes>>20, caps.maxInFlight)
	srv := &http.Server{
		Addr:    addr,
		Handler: mux,
		// A hung same-entity caller must not pin a goroutine forever.
		ReadHeaderTimeout: 10 * time.Second,
		IdleTimeout:       120 * time.Second,
	}
	log.Fatal(srv.ListenAndServe())
}
