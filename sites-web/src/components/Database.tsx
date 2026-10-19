import { useCallback, useEffect, useState } from "react";
import {
  Check,
  Copy,
  Database as DbIcon,
  History,
  KeyRound,
  Loader2,
  RotateCcw,
  Save,
  Trash2,
} from "lucide-react";
import {
  attachDatabase,
  backupDatabase,
  detachDatabase,
  fetchBackups,
  fetchDatabases,
  fetchSites,
  restoreDatabase,
  revealDatabase,
  saveDatabaseToKeys,
  type DbBackup,
  type DbRestore,
  type Site,
  type SiteDatabase,
} from "@/lib/api";
import { cn } from "@/lib/utils";

// Managed Postgres (SDK stations 4-5) — the Database tab. One managed Postgres
// DB per site, provisioned on the zone. The owner attaches, backs up /
// restores, and can tear it down. Drives /sites/db* -> bridge webapp.db.* ->
// controller; the zone agent does the real CREATE/DROP + pg_dump/pg_restore.
// The DB + its data are the owner's — we never read it.
//
// CREDENTIAL RULE: the DATABASE_URL is never rendered in
// plaintext here. "Save to Keys" deposits it into the vault server-side (the
// URL never reaches this page); on-screen reveal is the Keys app's TOTP-gated
// job. "Copy URL" goes straight to the clipboard without touching the DOM —
// the escape hatch for psql/external tools. The published app itself needs
// none of this: DATABASE_URL is injected into its pod automatically.

const STATUS_PILL: Record<string, string> = {
  active: "border-success/40 text-success",
  done: "border-success/40 text-success",
  pending: "border-warning/40 text-warning",
  deleting: "border-warning/40 text-warning",
  error: "border-destructive/40 text-destructive-foreground",
};

function fmtBytes(n?: number | null): string {
  if (n == null) return "—";
  if (n < 1024) return `${n} B`;
  const u = ["KB", "MB", "GB", "TB"];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < u.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(v < 10 ? 1 : 0)} ${u[i]}`;
}

interface Row {
  slug: string;
  retired: boolean; // stranded DB whose site is unpublished (NB4)
  db?: SiteDatabase;
}

export function Database() {
  const [sites, setSites] = useState<Site[] | null>(null);
  const [dbs, setDbs] = useState<SiteDatabase[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<{ text: string; err?: boolean } | null>(null);
  const [busy, setBusy] = useState<Set<string>>(new Set());
  const [saved, setSaved] = useState<
    Record<string, { name: string; masked_url: string | null }>
  >({});
  const [copied, setCopied] = useState<string | null>(null);
  const [confirmDetach, setConfirmDetach] = useState<string | null>(null);
  const [confirmRestore, setConfirmRestore] = useState<{
    slug: string;
    backup_id: number;
  } | null>(null);
  const [openHistory, setOpenHistory] = useState<Set<string>>(new Set());
  const [history, setHistory] = useState<
    Record<string, { backups: DbBackup[]; restores: DbRestore[] }>
  >({});

  const startBusy = (s: string) => setBusy((b) => new Set(b).add(s));
  const endBusy = (s: string) =>
    setBusy((b) => {
      const n = new Set(b);
      n.delete(s);
      return n;
    });

  const load = useCallback(async () => {
    const [rs, rd] = await Promise.all([fetchSites(), fetchDatabases()]);
    if (rs.ok) setSites(rs.data);
    else setError(rs.message);
    // NB5: surface a databases-fetch failure instead of silently showing stale.
    if (rd.ok) setDbs(rd.data);
    else if (rs.ok)
      setNote({ text: `Couldn't load database status: ${rd.message}`, err: true });
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // Poll while any DB is mid-transition (agent provisioning/dropping).
  useEffect(() => {
    const transient = dbs.some(
      (d) => d.status === "pending" || d.status === "deleting",
    );
    if (!transient) return;
    const t = setInterval(() => void load(), 4000);
    return () => clearInterval(t);
  }, [dbs, load]);

  const loadHistory = useCallback(async (slug: string) => {
    const r = await fetchBackups(slug);
    if (r.ok) setHistory((h) => ({ ...h, [slug]: r.data }));
  }, []);

  // Poll open history panels while a backup/restore is in flight.
  useEffect(() => {
    const open = [...openHistory];
    if (open.length === 0) return;
    const anyPending = open.some((slug) =>
      [
        ...(history[slug]?.backups ?? []),
        ...(history[slug]?.restores ?? []),
      ].some((x) => x.status === "pending"),
    );
    if (!anyPending) return;
    const t = setInterval(() => {
      for (const slug of open) void loadHistory(slug);
    }, 4000);
    return () => clearInterval(t);
  }, [openHistory, history, loadHistory]);

  const dbBySlug = new Map(dbs.map((d) => [d.slug, d]));

  async function act(
    slug: string,
    fn: () => Promise<{ ok: boolean; message?: string }>,
    after?: () => void,
  ) {
    if (busy.has(slug)) return;
    startBusy(slug);
    setNote(null);
    const r = await fn();
    endBusy(slug);
    if (r.ok) after?.();
    else setNote({ text: r.message ?? "failed", err: true });
  }

  const attach = (slug: string) =>
    act(slug, () => attachDatabase(slug), () => void load());

  const saveToKeys = (slug: string) =>
    act(
      slug,
      async () => {
        const r = await saveDatabaseToKeys(slug);
        if (r.ok)
          setSaved((m) => ({
            ...m,
            [slug]: { name: r.data.name, masked_url: r.data.masked_url },
          }));
        return r;
      },
    );

  // Clipboard-only: the URL lives in a local variable for the duration of the
  // write and is never put in state or the DOM.
  const copyUrl = (slug: string) =>
    act(slug, async () => {
      const r = await revealDatabase(slug);
      if (!r.ok) return r;
      try {
        await navigator.clipboard.writeText(r.data.conn_url);
      } catch {
        return {
          ok: false as const,
          message:
            "Clipboard unavailable — use Save to Keys and reveal it in the Keys app.",
        };
      }
      setCopied(slug);
      setTimeout(() => setCopied(null), 1500);
      return r;
    });

  const detach = (slug: string) => {
    setConfirmDetach(null);
    return act(
      slug,
      () => detachDatabase(slug),
      () => {
        setSaved((m) => {
          const n = { ...m };
          delete n[slug];
          return n;
        });
        void load();
      },
    );
  };

  const backup = (slug: string) =>
    act(
      slug,
      () => backupDatabase(slug),
      () => {
        setOpenHistory((s) => new Set(s).add(slug));
        void loadHistory(slug);
      },
    );

  const restore = (slug: string, backup_id: number) => {
    setConfirmRestore(null);
    return act(slug, () => restoreDatabase(slug, backup_id), () =>
      void loadHistory(slug),
    );
  };

  function toggleHistory(slug: string) {
    setOpenHistory((s) => {
      const n = new Set(s);
      if (n.has(slug)) n.delete(slug);
      else {
        n.add(slug);
        void loadHistory(slug);
      }
      return n;
    });
  }

  if (error) {
    return (
      <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive-foreground">
        Couldn't load databases: {error}
      </div>
    );
  }
  if (sites === null) {
    return (
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <Loader2 className="size-4 animate-spin" /> Loading…
      </div>
    );
  }

  // Catalog apps bring their own database — offering attach here misleads
  // (the tab offered attach on a catalog site, prompting a needless
  // attach + restart cycle). Mirror of Data.tsx's kind filter, inverted.
  const shownSites = sites.filter(
    (s) => s.status !== "retired" && s.kind !== "catalog",
  );
  const shownSlugs = new Set(shownSites.map((s) => s.slug));
  const rows: Row[] = [
    ...shownSites.map((s) => ({
      slug: s.slug,
      retired: false,
      db: dbBySlug.get(s.slug),
    })),
    // NB4: a managed DB whose site is unpublished still needs reveal/delete/backup.
    ...dbs
      .filter((d) => !shownSlugs.has(d.slug))
      .map((d) => ({ slug: d.slug, retired: true, db: d })),
  ];

  return (
    <div className="space-y-4">
      <div>
        <h2 className="flex items-center gap-1.5 text-sm font-semibold">
          <DbIcon className="size-4 text-primary" /> Databases
        </h2>
        <p className="mt-0.5 text-xs text-muted-foreground">
          Give a site its own managed Postgres. Your published app gets{" "}
          <code className="text-foreground">DATABASE_URL</code> automatically —
          for external tools, save the credential to Keys (reveal there, with
          your second factor) or copy it straight to the clipboard. Your data,
          on your own zone — we never read it. Catalog apps aren&apos;t listed
          here — they include their own database.
        </p>
      </div>

      {note && (
        <div
          className={cn(
            "rounded-md px-3 py-1.5 text-xs",
            note.err
              ? "border border-destructive/40 bg-destructive/10 text-destructive-foreground"
              : "border border-border bg-card text-muted-foreground",
          )}
        >
          {note.text}
        </div>
      )}

      {rows.length === 0 ? (
        <p className="text-xs text-muted-foreground">
          Publish a site first, then add a database to it here.
        </p>
      ) : (
        <ul className="divide-y divide-border overflow-hidden rounded-md border border-border">
          {rows.map(({ slug, retired, db }) => {
            const acting = busy.has(slug);
            const sv = saved[slug];
            const hist = history[slug];
            const showHistory = openHistory.has(slug);
            return (
              <li key={slug} className="bg-card px-3 py-2.5">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="min-w-0 flex-1 truncate text-sm text-foreground">
                    {slug}
                    {retired && (
                      <span className="ml-1.5 text-[11px] text-muted-foreground">
                        (unpublished)
                      </span>
                    )}
                  </span>
                  {db && (
                    <span
                      className={cn(
                        "rounded-full border px-2 py-0.5 text-[11px]",
                        STATUS_PILL[db.status] ??
                          "border-border text-muted-foreground",
                      )}
                    >
                      {db.status}
                    </span>
                  )}
                  {acting && (
                    <Loader2 className="size-3.5 animate-spin text-muted-foreground" />
                  )}
                  {!db && !retired && (
                    <button
                      type="button"
                      disabled={acting}
                      onClick={() => attach(slug)}
                      className="rounded-md border border-primary bg-primary px-2.5 py-1 text-[11px] text-primary-foreground hover:opacity-90 disabled:opacity-50"
                    >
                      Add database
                    </button>
                  )}
                  {db?.status === "active" && (
                    <>
                      <button
                        type="button"
                        disabled={acting}
                        onClick={() => saveToKeys(slug)}
                        className="flex items-center gap-1 rounded-md border border-primary bg-primary px-2 py-1 text-[11px] text-primary-foreground hover:opacity-90 disabled:opacity-50"
                      >
                        <KeyRound className="size-3" /> Save to Keys
                      </button>
                      <button
                        type="button"
                        disabled={acting}
                        onClick={() => copyUrl(slug)}
                        className="flex items-center gap-1 rounded-md border border-border px-2 py-1 text-[11px] text-foreground hover:bg-accent disabled:opacity-50"
                      >
                        {copied === slug ? (
                          <Check className="size-3 text-success" />
                        ) : (
                          <Copy className="size-3" />
                        )}
                        {copied === slug ? "Copied" : "Copy URL"}
                      </button>
                      <button
                        type="button"
                        disabled={acting}
                        onClick={() => backup(slug)}
                        className="flex items-center gap-1 rounded-md border border-border px-2 py-1 text-[11px] text-foreground hover:bg-accent disabled:opacity-50"
                      >
                        <Save className="size-3" /> Back up
                      </button>
                      <button
                        type="button"
                        onClick={() => toggleHistory(slug)}
                        className="flex items-center gap-1 rounded-md border border-border px-2 py-1 text-[11px] text-foreground hover:bg-accent"
                      >
                        <History className="size-3" /> History
                      </button>
                      <button
                        type="button"
                        disabled={acting}
                        onClick={() => setConfirmDetach(slug)}
                        className="flex items-center gap-1 rounded-md border border-border px-2 py-1 text-[11px] text-destructive-foreground hover:bg-destructive/10 disabled:opacity-50"
                      >
                        <Trash2 className="size-3" /> Delete
                      </button>
                    </>
                  )}
                  {db?.status === "error" && !retired && (
                    <button
                      type="button"
                      disabled={acting}
                      onClick={() => attach(slug)}
                      className="rounded-md border border-border px-2 py-1 text-[11px] text-foreground hover:bg-accent disabled:opacity-50"
                    >
                      Retry
                    </button>
                  )}
                </div>

                {db?.status === "active" && (
                  <div className="mt-1 text-[11px] text-muted-foreground">
                    Database <code className="text-foreground">{db.db_name}</code>
                    {" · "}
                    {fmtBytes(db.size_bytes)}
                  </div>
                )}
                {db?.status === "error" && db.last_error && (
                  <div className="mt-1 text-[11px] text-destructive-foreground">
                    {db.last_error}
                  </div>
                )}

                {sv && (
                  <div className="mt-2">
                    <div className="text-[11px] text-muted-foreground">
                      Saved to Keys as{" "}
                      <code className="text-foreground">{sv.name}</code> — open
                      the Keys app to reveal it (needs your second factor).
                    </div>
                    {sv.masked_url && (
                      <code className="mt-1 block min-w-0 truncate rounded-md border border-border bg-background px-2 py-1 text-xs text-foreground">
                        {sv.masked_url}
                      </code>
                    )}
                  </div>
                )}

                {showHistory && (
                  <div className="mt-2 rounded-md border border-border bg-background p-2">
                    {!hist ? (
                      <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
                        <Loader2 className="size-3 animate-spin" /> Loading
                        backups…
                      </div>
                    ) : hist.backups.length === 0 ? (
                      <div className="text-[11px] text-muted-foreground">
                        No backups yet. “Back up” makes one now; a daily backup
                        runs automatically.
                      </div>
                    ) : (
                      <ul className="space-y-1">
                        {hist.backups.map((b) => (
                          <li
                            key={b.backup_id}
                            className="flex flex-wrap items-center gap-2 text-[11px]"
                          >
                            <span className="text-muted-foreground">
                              {b.created_at
                                ? b.created_at.slice(0, 16).replace("T", " ")
                                : "—"}
                            </span>
                            <span className="text-muted-foreground">
                              {b.kind}
                            </span>
                            <span
                              className={cn(
                                "rounded-full border px-1.5 py-px",
                                STATUS_PILL[b.status] ??
                                  "border-border text-muted-foreground",
                              )}
                            >
                              {b.status}
                            </span>
                            {b.status === "done" && (
                              <span className="text-muted-foreground">
                                {fmtBytes(b.size_bytes)}
                              </span>
                            )}
                            {b.status === "done" && (
                              <button
                                type="button"
                                disabled={acting}
                                onClick={() =>
                                  setConfirmRestore({
                                    slug,
                                    backup_id: b.backup_id,
                                  })
                                }
                                className="ml-auto flex items-center gap-1 rounded-md border border-border px-1.5 py-0.5 text-foreground hover:bg-accent disabled:opacity-50"
                              >
                                <RotateCcw className="size-3" /> Restore
                              </button>
                            )}
                            {b.status === "error" && b.last_error && (
                              <span className="text-destructive-foreground">
                                {b.last_error}
                              </span>
                            )}
                          </li>
                        ))}
                      </ul>
                    )}
                    {hist?.restores?.some((r) => r.status !== "done") && (
                      <div className="mt-1 border-t border-border pt-1 text-[11px] text-warning">
                        Restore in progress…
                      </div>
                    )}
                  </div>
                )}

                {confirmDetach === slug && (
                  <div className="mt-2 rounded-md border border-destructive/40 bg-destructive/10 p-2">
                    <div className="text-[11px] text-destructive-foreground">
                      Delete this database and everything in it? This can't be
                      undone.
                    </div>
                    <div className="mt-1.5 flex justify-end gap-2">
                      <button
                        type="button"
                        onClick={() => setConfirmDetach(null)}
                        className="rounded-md border border-border px-2 py-1 text-[11px] text-foreground hover:bg-accent"
                      >
                        Cancel
                      </button>
                      <button
                        type="button"
                        disabled={acting}
                        onClick={() => detach(slug)}
                        className="rounded-md border border-destructive bg-destructive px-2 py-1 text-[11px] text-destructive-foreground hover:opacity-90 disabled:opacity-50"
                      >
                        Delete database
                      </button>
                    </div>
                  </div>
                )}

                {confirmRestore?.slug === slug && (
                  <div className="mt-2 rounded-md border border-warning/40 bg-warning/10 p-2">
                    <div className="text-[11px] text-foreground">
                      Restore this backup? It overwrites the database's current
                      contents.
                    </div>
                    <div className="mt-1.5 flex justify-end gap-2">
                      <button
                        type="button"
                        onClick={() => setConfirmRestore(null)}
                        className="rounded-md border border-border px-2 py-1 text-[11px] text-foreground hover:bg-accent"
                      >
                        Cancel
                      </button>
                      <button
                        type="button"
                        disabled={acting}
                        onClick={() => restore(slug, confirmRestore.backup_id)}
                        className="rounded-md border border-primary bg-primary px-2 py-1 text-[11px] text-primary-foreground hover:opacity-90 disabled:opacity-50"
                      >
                        Restore
                      </button>
                    </div>
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
