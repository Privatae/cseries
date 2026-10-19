import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowRight,
  Check,
  ExternalLink,
  Loader2,
  Package,
  Rocket,
  Search,
  X,
} from "lucide-react";
import {
  deployCatalogApp,
  fetchCatalog,
  fetchHandle,
  fetchSites,
  type CatalogTemplate,
  type Site,
} from "@/lib/api";
import { cn } from "@/lib/utils";

// Service catalog — one-click third-party apps (blog, analytics, automation,
// …) deployed as managed sites. Browse the admitted templates, pick a name,
// fill the optional settings, deploy — then watch it come up LIVE (no reload,
// no raw status strings). Every deployed app is a normal site afterward:
// born PRIVATE (only you, signed in), flipped public in Visibility.
//
// No vendor logos here on purpose (we don't fabricate brand marks): tiles are
// initial-letter + category.

const SLUG_RE = /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/;

// Deploy progress, derived from the site row the poller sees. The steps are
// coarse on purpose — real, observable states, not theater.
type Phase = "queued" | "starting" | "live" | "error";

function phaseOf(site: Site | undefined): Phase {
  if (!site) return "queued";
  if (site.status === "live") return "live";
  if (site.last_error) return "error";
  return "starting";
}

const CATEGORY_TONE: Record<string, string> = {
  cms: "border-primary/40 text-primary",
  analytics: "border-success/40 text-success",
  automation: "border-warning/40 text-warning",
  monitoring: "border-success/40 text-success",
};

export function Catalog() {
  const [templates, setTemplates] = useState<CatalogTemplate[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState("");
  const [category, setCategory] = useState<string | null>(null);

  // Where a new app lands: <name>.<suffix>. Always the bare zone -- the
  // controller returns it from GET handle and there is no per-owner label.
  // undefined = still loading. NEVER null: the claim dialog that used to
  // handle null is gone, so a null would only disable Deploy for good.
  const [suffix, setSuffix] = useState<string | undefined>(undefined);

  // deploy dialog
  const [deploying, setDeploying] = useState<CatalogTemplate | null>(null);
  const [slug, setSlug] = useState("");
  const [slugTouched, setSlugTouched] = useState(false);
  const [settings, setSettings] = useState<Record<string, string>>({});
  const [showSettings, setShowSettings] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [deployError, setDeployError] = useState<string | null>(null);

  // live progress (after a successful deploy request)
  const [watch, setWatch] = useState<{ slug: string; hostname: string } | null>(
    null,
  );
  const [watchSite, setWatchSite] = useState<Site | undefined>(undefined);
  const pollRef = useRef<number | null>(null);

  useEffect(() => {
    void (async () => {
      const [rc, rh] = await Promise.all([fetchCatalog(), fetchHandle()]);
      if (rc.ok) setTemplates(rc.data);
      else setError(rc.message);
      // Never null. Deploy is disabled while suffix is falsy, so a host
      // with no sites zone configured (the lookup fails or returns no zone)
      // keeps Deploy off rather than guessing an address.
      setSuffix((rh.ok && (rh.data.suffix || rh.data.zone)) || "");
    })();
  }, []);


  // Poll the sites list while a deploy is in flight so every state change is
  // visible the moment the platform sees it (standing rule: live progress).
  const poll = useCallback(async () => {
    if (!watch) return;
    const r = await fetchSites();
    if (!r.ok) return;
    const s = r.data.find((x) => x.slug === watch.slug);
    setWatchSite(s);
  }, [watch]);

  useEffect(() => {
    if (!watch) return;
    void poll();
    pollRef.current = window.setInterval(() => void poll(), 3000);
    return () => {
      if (pollRef.current != null) window.clearInterval(pollRef.current);
    };
  }, [watch, poll]);

  const phase = phaseOf(watchSite);
  useEffect(() => {
    // Stop polling once terminal; keep the card on screen.
    if ((phase === "live" || phase === "error") && pollRef.current != null) {
      window.clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, [phase]);

  const categories = useMemo(() => {
    const c = new Set<string>();
    for (const t of templates ?? []) {
      if (t.header.category) c.add(t.header.category);
    }
    return [...c].sort();
  }, [templates]);

  const shown = useMemo(() => {
    const q = filter.trim().toLowerCase();
    return (templates ?? []).filter((t) => {
      if (category && t.header.category !== category) return false;
      if (!q) return true;
      const hay = `${t.display_name} ${t.header.slogan ?? ""} ${(
        t.header.tags ?? []
      ).join(" ")}`.toLowerCase();
      return hay.includes(q);
    });
  }, [templates, filter, category]);

  function openDeploy(t: CatalogTemplate) {
    setDeploying(t);
    setSlug(t.template_key.replace(/[^a-z0-9-]/g, "-").slice(0, 40));
    setSlugTouched(false);
    setSettings({});
    setShowSettings(false);
    setDeployError(null);
  }

  async function submitDeploy() {
    if (!deploying) return;
    const s = slug.trim().toLowerCase();
    if (!SLUG_RE.test(s)) {
      setDeployError("Name must be lowercase letters, numbers and hyphens.");
      return;
    }
    setSubmitting(true);
    setDeployError(null);
    // Only send settings the user actually changed — defaults live server-side.
    const changed: Record<string, string> = {};
    for (const [k, v] of Object.entries(settings)) {
      if (v !== (deploying.settings[k] ?? "")) changed[k] = v;
    }
    const r = await deployCatalogApp(s, deploying.template_key, changed);
    setSubmitting(false);
    if (!r.ok) {
      setDeployError(r.message);
      return;
    }
    setWatch({ slug: s, hostname: r.data.hostname });
    setWatchSite(undefined);
    setDeploying(null);
  }

  if (error) {
    return (
      <p className="text-sm text-destructive-foreground">
        Couldn&apos;t load the catalog: {error}
      </p>
    );
  }
  if (templates === null) {
    return (
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <Loader2 className="size-4 animate-spin" /> Loading catalog…
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {/* live deploy progress card — stays until dismissed */}
      {watch && (
        <div className="rounded-lg border border-border bg-card p-4">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0 space-y-2">
              <div className="flex items-center gap-2 text-sm font-medium">
                {phase === "live" ? (
                  <Check className="size-4 text-success" />
                ) : phase === "error" ? (
                  <X className="size-4 text-destructive-foreground" />
                ) : (
                  <Loader2 className="size-4 animate-spin text-primary" />
                )}
                <span className="truncate">{watch.hostname}</span>
              </div>
              <ol className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
                {(
                  [
                    ["queued", "Requested"],
                    ["starting", "Starting up"],
                    ["live", "Live"],
                  ] as const
                ).map(([p, label], i) => {
                  const reached =
                    p === "queued" ||
                    (p === "starting" && phase !== "queued") ||
                    (p === "live" && phase === "live");
                  const current = phase === p && phase !== "live";
                  return (
                    <li key={p} className="flex items-center gap-1.5">
                      {i > 0 && <ArrowRight className="size-3 opacity-50" />}
                      <span
                        className={cn(
                          "rounded-full border px-2 py-0.5",
                          phase === "error" && p === "live"
                            ? "border-destructive/40 text-destructive-foreground"
                            : reached
                              ? "border-success/40 text-success"
                              : current
                                ? "border-primary/40 text-primary"
                                : "border-border",
                        )}
                      >
                        {phase === "error" && p === "live" ? "Problem" : label}
                      </span>
                    </li>
                  );
                })}
              </ol>
              {phase === "starting" && (
                <p className="text-xs text-muted-foreground">
                  Pulling the app and starting it up — usually under two
                  minutes. You can leave this page; the app keeps deploying.
                </p>
              )}
              {phase === "live" && (
                <p className="text-xs text-muted-foreground">
                  Your app is running, and it&apos;s <b>private</b> — only you,
                  signed in. Open it to finish its own setup, then flip it
                  public in <b>Visibility</b> when ready.
                </p>
              )}
              {phase === "error" && (
                <p className="text-xs text-destructive-foreground">
                  {watchSite?.last_error ??
                    "Something went wrong bringing the app up."}
                </p>
              )}
            </div>
            <div className="flex shrink-0 items-center gap-2">
              {phase === "live" && (
                <a
                  href={`https://${watch.hostname}`}
                  target="_blank"
                  rel="noreferrer"
                  className="flex items-center gap-1.5 rounded-md bg-primary px-2.5 py-1 text-xs font-medium text-primary-foreground hover:opacity-90"
                >
                  Open <ExternalLink className="size-3" />
                </a>
              )}
              <button
                type="button"
                onClick={() => setWatch(null)}
                className="rounded-md px-2 py-1 text-xs text-muted-foreground hover:bg-accent"
              >
                Dismiss
              </button>
            </div>
          </div>
        </div>
      )}

      {/* search + category filter */}
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative">
          <Search className="pointer-events-none absolute left-2 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
          <input
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="Search apps…"
            className="w-56 rounded-md border border-border bg-background py-1.5 pl-7 pr-2 text-xs outline-none focus:border-primary"
          />
        </div>
        <button
          type="button"
          onClick={() => setCategory(null)}
          className={cn(
            "rounded-full border px-2.5 py-1 text-xs",
            category === null
              ? "border-primary/40 text-primary"
              : "border-border text-muted-foreground hover:text-foreground",
          )}
        >
          All
        </button>
        {categories.map((c) => (
          <button
            key={c}
            type="button"
            onClick={() => setCategory(category === c ? null : c)}
            className={cn(
              "rounded-full border px-2.5 py-1 text-xs capitalize",
              category === c
                ? "border-primary/40 text-primary"
                : "border-border text-muted-foreground hover:text-foreground",
            )}
          >
            {c}
          </button>
        ))}
      </div>

      {/* browse grid */}
      {shown.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          {templates.length === 0
            ? "No apps in the catalog yet."
            : "Nothing matches that search."}
        </p>
      ) : (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {shown.map((t) => (
            <div
              key={t.template_key}
              className="flex flex-col rounded-lg border border-border bg-card p-4"
            >
              <div className="flex items-center gap-3">
                <div className="flex size-9 shrink-0 items-center justify-center rounded-md bg-secondary text-sm font-semibold text-secondary-foreground">
                  {t.display_name.charAt(0).toUpperCase()}
                </div>
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium">
                    {t.display_name}
                  </p>
                  {t.header.category && (
                    <span
                      className={cn(
                        "inline-block rounded-full border px-1.5 py-px text-[10px] capitalize",
                        CATEGORY_TONE[t.header.category] ??
                          "border-border text-muted-foreground",
                      )}
                    >
                      {t.header.category}
                    </span>
                  )}
                </div>
              </div>
              <p className="mt-2 line-clamp-2 flex-1 text-xs text-muted-foreground">
                {t.header.slogan ?? ""}
              </p>
              <div className="mt-3 flex items-center justify-between">
                {t.header.documentation ? (
                  <a
                    href={t.header.documentation}
                    target="_blank"
                    rel="noreferrer"
                    className="text-[11px] text-muted-foreground underline-offset-2 hover:underline"
                  >
                    About
                  </a>
                ) : (
                  <span />
                )}
                <button
                  type="button"
                  onClick={() => openDeploy(t)}
                  className="flex items-center gap-1.5 rounded-md bg-primary px-2.5 py-1 text-xs font-medium text-primary-foreground hover:opacity-90"
                >
                  <Rocket className="size-3" /> Deploy
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* deploy dialog */}
      {deploying && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
          <div className="w-full max-w-md rounded-lg border border-border bg-card p-4">
            <div className="flex items-center justify-between">
              <h2 className="flex items-center gap-2 text-sm font-semibold">
                <Package className="size-4 text-primary" />
                Deploy {deploying.display_name}
              </h2>
              <button
                type="button"
                onClick={() => setDeploying(null)}
                className="rounded p-1 text-muted-foreground hover:bg-accent"
              >
                <X className="size-4" />
              </button>
            </div>

                <label className="mt-4 block text-xs text-muted-foreground">
                  Name (becomes the web address)
                </label>
                <input
                  value={slug}
                  onChange={(e) => {
                    setSlug(e.target.value.toLowerCase());
                    setSlugTouched(true);
                  }}
                  className="mt-1 w-full rounded-md border border-border bg-background px-2 py-1.5 text-sm outline-none focus:border-primary"
                />
                <p className="mt-1 text-[11px] text-muted-foreground">
                  {SLUG_RE.test(slug.trim())
                    ? suffix
                      ? `→ https://${slug.trim()}.${suffix}`
                      : ""
                    : slugTouched
                      ? "Lowercase letters, numbers and hyphens only."
                      : ""}
                </p>

            {Object.keys(deploying.settings).length > 0 && (
              <div className="mt-3">
                <button
                  type="button"
                  onClick={() => setShowSettings((v) => !v)}
                  className="text-xs text-muted-foreground underline-offset-2 hover:underline"
                >
                  {showSettings ? "Hide" : "Show"} optional settings (
                  {Object.keys(deploying.settings).length})
                </button>
                {showSettings && (
                  <div className="mt-2 max-h-48 space-y-2 overflow-y-auto pr-1">
                    {Object.entries(deploying.settings).map(([k, dflt]) => (
                      <div key={k}>
                        <label className="block break-all text-[11px] text-muted-foreground">
                          {k}
                        </label>
                        <input
                          value={settings[k] ?? dflt}
                          placeholder={dflt || "(blank)"}
                          onChange={(e) =>
                            setSettings((s) => ({ ...s, [k]: e.target.value }))
                          }
                          className="mt-0.5 w-full rounded-md border border-border bg-background px-2 py-1 text-xs outline-none focus:border-primary"
                        />
                      </div>
                    ))}
                    <p className="text-[11px] text-muted-foreground">
                      Every setting has a working default — most deploys need
                      none of these.
                    </p>
                  </div>
                )}
              </div>
            )}

            {deployError && (
              <p className="mt-3 text-xs text-destructive-foreground">
                {deployError}
              </p>
            )}

            <div className="mt-4 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setDeploying(null)}
                className="rounded-md px-3 py-1.5 text-xs text-muted-foreground hover:bg-accent"
              >
                Cancel
              </button>
              <button
                type="button"
                disabled={submitting || !suffix || !SLUG_RE.test(slug.trim())}
                onClick={() => void submitDeploy()}
                className="flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:opacity-90 disabled:opacity-50"
              >
                {submitting ? (
                  <Loader2 className="size-3 animate-spin" />
                ) : (
                  <Rocket className="size-3" />
                )}
                Deploy
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
