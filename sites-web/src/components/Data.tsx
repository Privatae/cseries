import { useCallback, useEffect, useState } from "react";
import {
  Archive,
  HardDrive,
  History,
  Loader2,
  RotateCcw,
} from "lucide-react";
import {
  backupVolumes,
  fetchSites,
  fetchVolumes,
  restoreVolume,
  type Site,
  type VolumeBackup,
  type VolumeRestore,
} from "@/lib/api";
import { cn } from "@/lib/utils";

// Data — the volume sibling of the Database tab, for CATALOG apps (one-click
// third-party stacks). Their state lives in data volumes; this is where you
// back those up and put them back. Backups are also taken automatically every
// day; the platform keeps the 7 most recent per volume.

const STATUS_PILL: Record<string, string> = {
  done: "border-success/40 text-success",
  pending: "border-warning/40 text-warning",
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

function fmtWhen(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString();
}

type VolState = {
  volumes: string[];
  backups: VolumeBackup[];
  restores: VolumeRestore[];
};

export function Data() {
  const [sites, setSites] = useState<Site[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [vols, setVols] = useState<Record<string, VolState>>({});
  const [busy, setBusy] = useState<Set<string>>(new Set());
  const [note, setNote] = useState<{ text: string; err?: boolean } | null>(null);
  const [confirmRestore, setConfirmRestore] = useState<{
    slug: string;
    backup: VolumeBackup;
  } | null>(null);

  const startBusy = (s: string) => setBusy((b) => new Set(b).add(s));
  const endBusy = (s: string) =>
    setBusy((b) => {
      const n = new Set(b);
      n.delete(s);
      return n;
    });

  const load = useCallback(async () => {
    const rs = await fetchSites();
    if (!rs.ok) {
      setError(rs.message);
      return;
    }
    const catalog = rs.data.filter(
      (s) => s.kind === "catalog" && s.status !== "retired",
    );
    setSites(catalog);
    const results = await Promise.all(
      catalog.map(async (s) => [s.slug, await fetchVolumes(s.slug)] as const),
    );
    setVols((prev) => {
      const next = { ...prev };
      for (const [slug, r] of results) {
        if (r.ok) next[slug] = r.data;
      }
      return next;
    });
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // Poll while any backup/restore is mid-flight so state changes land live.
  useEffect(() => {
    const transient = Object.values(vols).some(
      (v) =>
        v.backups.some((b) => b.status === "pending") ||
        v.restores.some((r) => r.status === "pending"),
    );
    if (!transient) return;
    const t = setInterval(() => void load(), 4000);
    return () => clearInterval(t);
  }, [vols, load]);

  async function doBackup(slug: string) {
    startBusy(slug);
    setNote(null);
    const r = await backupVolumes(slug);
    endBusy(slug);
    if (!r.ok) {
      setNote({ text: `Backup failed to queue: ${r.message}`, err: true });
      return;
    }
    setNote({
      text: `Backing up ${r.data.volumes.length} volume${
        r.data.volumes.length === 1 ? "" : "s"
      } of ${slug}…`,
    });
    void load();
  }

  async function doRestore() {
    if (!confirmRestore) return;
    const { slug, backup } = confirmRestore;
    setConfirmRestore(null);
    startBusy(slug);
    setNote(null);
    const r = await restoreVolume(slug, backup.backup_id);
    endBusy(slug);
    if (!r.ok) {
      setNote({ text: `Restore failed to queue: ${r.message}`, err: true });
      return;
    }
    setNote({ text: `Restoring ${backup.volume} of ${slug}…` });
    void load();
  }

  if (error) {
    return (
      <p className="text-sm text-destructive-foreground">
        Couldn&apos;t load your apps: {error}
      </p>
    );
  }
  if (sites === null) {
    return (
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <Loader2 className="size-4 animate-spin" /> Loading…
      </div>
    );
  }
  if (sites.length === 0) {
    return (
      <div className="space-y-1 text-sm text-muted-foreground">
        <p>No catalog apps yet.</p>
        <p className="text-xs">
          Apps you deploy from the <b>Catalog</b> tab keep their data in
          volumes — this is where you back those up and restore them. (Sites
          you publish yourself keep data in their <b>Database</b> instead.)
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {note && (
        <p
          className={cn(
            "text-xs",
            note.err ? "text-destructive-foreground" : "text-muted-foreground",
          )}
        >
          {note.text}
        </p>
      )}

      {sites.map((s) => {
        const v = vols[s.slug];
        const isBusy = busy.has(s.slug);
        const pendingWork =
          v &&
          (v.backups.some((b) => b.status === "pending") ||
            v.restores.some((r) => r.status === "pending"));
        return (
          <div key={s.slug} className="rounded-lg border border-border bg-card p-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex min-w-0 items-center gap-2">
                <HardDrive className="size-4 shrink-0 text-primary" />
                <span className="truncate text-sm font-medium">{s.slug}</span>
                {s.template_key && (
                  <span className="rounded-full border border-border px-1.5 py-px text-[10px] text-muted-foreground">
                    {s.template_key}
                  </span>
                )}
                {pendingWork && (
                  <Loader2 className="size-3.5 animate-spin text-warning" />
                )}
              </div>
              <button
                type="button"
                disabled={isBusy || !v}
                onClick={() => void doBackup(s.slug)}
                className="flex items-center gap-1.5 rounded-md bg-secondary px-2.5 py-1 text-xs font-medium text-secondary-foreground hover:opacity-90 disabled:opacity-50"
              >
                {isBusy ? (
                  <Loader2 className="size-3 animate-spin" />
                ) : (
                  <Archive className="size-3" />
                )}
                Back up now
              </button>
            </div>

            {v ? (
              <>
                <p className="mt-2 text-[11px] text-muted-foreground">
                  Volumes: {v.volumes.length ? v.volumes.join(", ") : "none"} ·
                  daily automatic backups · 7 most recent kept per volume
                </p>
                {v.backups.length > 0 && (
                  <table className="mt-2 w-full text-left text-xs">
                    <thead>
                      <tr className="text-[11px] text-muted-foreground">
                        <th className="py-1 pr-2 font-normal">Volume</th>
                        <th className="py-1 pr-2 font-normal">When</th>
                        <th className="py-1 pr-2 font-normal">Size</th>
                        <th className="py-1 pr-2 font-normal">Status</th>
                        <th className="py-1 font-normal" />
                      </tr>
                    </thead>
                    <tbody>
                      {v.backups.slice(0, 8).map((b) => (
                        <tr key={b.backup_id} className="border-t border-border/60">
                          <td className="py-1.5 pr-2">{b.volume}</td>
                          <td className="py-1.5 pr-2 text-muted-foreground">
                            {fmtWhen(b.created_at)}
                            {b.kind === "scheduled" && (
                              <span className="ml-1 text-[10px] opacity-70">
                                (auto)
                              </span>
                            )}
                          </td>
                          <td className="py-1.5 pr-2">{fmtBytes(b.size_bytes)}</td>
                          <td className="py-1.5 pr-2">
                            <span
                              className={cn(
                                "rounded-full border px-1.5 py-px text-[10px]",
                                STATUS_PILL[b.status] ?? "border-border",
                              )}
                              title={b.last_error ?? undefined}
                            >
                              {b.status === "done"
                                ? "saved"
                                : b.status === "pending"
                                  ? "working…"
                                  : "failed"}
                            </span>
                          </td>
                          <td className="py-1.5 text-right">
                            {b.status === "done" && (
                              <button
                                type="button"
                                disabled={isBusy}
                                onClick={() =>
                                  setConfirmRestore({ slug: s.slug, backup: b })
                                }
                                className="inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-[11px] text-muted-foreground hover:bg-accent hover:text-foreground disabled:opacity-50"
                              >
                                <RotateCcw className="size-3" /> Restore
                              </button>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
                {v.restores.some((r) => r.status !== "done") && (
                  <p className="mt-1 flex items-center gap-1 text-[11px] text-muted-foreground">
                    <History className="size-3" />
                    {v.restores.filter((r) => r.status === "pending").length >
                    0
                      ? "Restore in progress — the app pauses briefly while its data is swapped."
                      : v.restores.find((r) => r.status === "error")
                            ?.last_error ?? ""}
                  </p>
                )}
              </>
            ) : (
              <p className="mt-2 text-[11px] text-muted-foreground">
                Couldn&apos;t load this app&apos;s volumes.
              </p>
            )}
          </div>
        );
      })}

      {/* destructive-confirm for restore */}
      {confirmRestore && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
          <div className="w-full max-w-sm rounded-lg border border-border bg-card p-4">
            <h2 className="text-sm font-semibold">
              Restore {confirmRestore.backup.volume}?
            </h2>
            <p className="mt-2 text-xs text-muted-foreground">
              <b>{confirmRestore.slug}</b> will be rolled back to the backup
              from {fmtWhen(confirmRestore.backup.created_at)}. Everything
              saved in this volume since then is <b>replaced</b>. The app
              pauses briefly during the swap.
            </p>
            <div className="mt-4 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setConfirmRestore(null)}
                className="rounded-md px-3 py-1.5 text-xs text-muted-foreground hover:bg-accent"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={() => void doRestore()}
                className="rounded-md bg-destructive px-3 py-1.5 text-xs font-medium text-destructive-foreground hover:opacity-90"
              >
                Restore this backup
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
