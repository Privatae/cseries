import { useCallback, useEffect, useState } from "react";
import { ExternalLink, Loader2 } from "lucide-react";
import {
  fetchAnalytics,
  fetchSites,
  siteAction,
  type Analytics,
  type Site,
  type SiteAction,
} from "@/lib/api";
import { cn } from "@/lib/utils";
import { Sparkline } from "@/components/Sparkline";
import { Visibility } from "@/components/Visibility";

const STATUS_LABEL: Record<string, string> = {
  live: "Live",
  pending: "Deploying",
  suspended: "Suspended",
  retired: "Retired",
};

function statusClass(st: string): string {
  if (st === "live") return "border-success/40 text-success";
  if (st === "suspended") return "border-warning/40 text-warning";
  return "border-border text-muted-foreground";
}

export function SitesList() {
  const [sites, setSites] = useState<Site[] | null>(null);
  const [analytics, setAnalytics] = useState<Analytics>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<Record<string, boolean>>({});
  const [note, setNote] = useState<{ text: string; err?: boolean } | null>(null);
  const [confirm, setConfirm] = useState<{ slug: string; host: string } | null>(
    null,
  );

  const load = useCallback(async () => {
    const r = await fetchSites();
    if (r.ok) {
      setSites(r.data);
      setError(null);
    } else {
      setError(r.message);
    }
    const a = await fetchAnalytics(168); // best-effort
    if (a.ok) setAnalytics(a.data);
  }, []);

  useEffect(() => {
    void load();
    // Poll every 20s so a freshly published site's status advances without a
    // manual refresh (parity with the vanilla app's interval).
    const id = setInterval(() => void load(), 20000);
    return () => clearInterval(id);
  }, [load]);

  const run = useCallback(
    async (act: SiteAction, slug: string) => {
      setBusy((b) => ({ ...b, [slug]: true }));
      const r = await siteAction(act, slug);
      setNote(
        r.ok
          ? { text: `${act} ✓ ${slug}` }
          : { text: `${act} failed: ${r.message}`, err: true },
      );
      // let the agent reconcile, then refresh
      setTimeout(() => {
        setBusy((b) => ({ ...b, [slug]: false }));
        void load();
      }, 600);
    },
    [load],
  );

  if (error) {
    return (
      <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive-foreground">
        Couldn't load sites: {error}
      </div>
    );
  }
  if (sites === null) {
    return (
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <Loader2 className="size-4 animate-spin" /> Loading sites…
      </div>
    );
  }
  if (sites.length === 0) {
    return (
      <div className="rounded-md border border-border bg-card px-4 py-6 text-center text-sm text-muted-foreground">
        <div className="font-medium text-foreground">No sites yet</div>
        Publish from Workbench ("Publish as site" on the bench) or ask your host to publish a project or repo — no uploads here.
      </div>
    );
  }

  return (
    <>
      {note && (
        <div
          className={cn(
            "mb-3 rounded-md px-3 py-1.5 text-xs",
            note.err
              ? "border border-destructive/40 bg-destructive/10 text-destructive-foreground"
              : "border border-border bg-card text-muted-foreground",
          )}
        >
          {note.text}
        </div>
      )}
      <ul className="space-y-2">
        {sites.map((s) => {
          const st = s.status || "pending";
          const b = !!busy[s.slug];
          const a = analytics[s.hostname] ?? { series: [], total: 0 };
          const canSuspend = st === "live";
          return (
            <li
              key={s.site_id}
              className="rounded-md border border-border bg-card px-4 py-3"
            >
              <div className="flex flex-wrap items-center gap-4">
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <a
                    href={`https://${s.hostname}/`}
                    target="_blank"
                    rel="noreferrer"
                    className="flex items-center gap-1 truncate text-sm font-medium text-link hover:underline"
                  >
                    {s.hostname}
                    <ExternalLink className="size-3 shrink-0" />
                  </a>
                  <span
                    className={cn(
                      "shrink-0 rounded-full border px-2 py-0.5 text-xs",
                      statusClass(st),
                    )}
                  >
                    {STATUS_LABEL[st] ?? st}
                  </span>
                </div>
                <div className="mt-0.5 text-xs text-muted-foreground">
                  {s.last_error ? (
                    <span className="text-destructive-foreground">
                      {s.last_error}
                    </span>
                  ) : (
                    <>v{s.latest_version_id ?? "—"}</>
                  )}
                </div>
              </div>

              <div className="flex flex-col items-end">
                <div className="text-sm text-foreground">
                  {a.total}
                  <span className="text-xs text-muted-foreground"> reqs / 7d</span>
                </div>
                <Sparkline series={a.series} />
              </div>

              <div className="flex items-center gap-2">
                {st === "suspended" ? (
                  <ActionButton
                    disabled={b}
                    onClick={() => run("resume", s.slug)}
                  >
                    Resume
                  </ActionButton>
                ) : (
                  <ActionButton
                    disabled={b || !canSuspend}
                    onClick={() => run("suspend", s.slug)}
                  >
                    Suspend
                  </ActionButton>
                )}
                <ActionButton
                  disabled={b || st !== "live"}
                  onClick={() => run("restart", s.slug)}
                >
                  Restart
                </ActionButton>
                <ActionButton
                  danger
                  disabled={b}
                  onClick={() => setConfirm({ slug: s.slug, host: s.hostname })}
                >
                  Unpublish
                </ActionButton>
              </div>
              </div>
              <Visibility site={s} onChanged={load} />
            </li>
          );
        })}
      </ul>

      {confirm && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
          onClick={() => setConfirm(null)}
        >
          <div
            className="w-full max-w-sm rounded-lg border border-border bg-popover p-4 text-popover-foreground shadow-lg"
            onClick={(e) => e.stopPropagation()}
          >
            <h2 className="text-sm font-semibold">Unpublish site?</h2>
            <p className="mt-2 text-sm text-muted-foreground">
              Take <span className="text-foreground">{confirm.host}</span>{" "}
              offline. Its content and data are kept; you can republish later.
            </p>
            <div className="mt-4 flex justify-end gap-2">
              <ActionButton onClick={() => setConfirm(null)}>Cancel</ActionButton>
              <ActionButton
                danger
                onClick={() => {
                  const slug = confirm.slug;
                  setConfirm(null);
                  void run("unpublish", slug);
                }}
              >
                Unpublish
              </ActionButton>
            </div>
          </div>
        </div>
      )}
    </>
  );
}

function ActionButton({
  children,
  onClick,
  disabled,
  danger,
}: {
  children: React.ReactNode;
  onClick: () => void;
  disabled?: boolean;
  danger?: boolean;
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      className={cn(
        "rounded-md border px-2.5 py-1 text-xs transition-colors disabled:cursor-not-allowed disabled:opacity-50",
        danger
          ? "border-destructive/40 text-destructive-foreground hover:bg-destructive/10"
          : "border-border text-foreground hover:bg-accent",
      )}
    >
      {children}
    </button>
  );
}
