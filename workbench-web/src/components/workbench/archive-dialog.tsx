import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Spinner } from "@/components/ui/spinner";
import { archiveProject, fetchProjectRepository } from "@/lib/api";
import type { Project, ProjectRepository } from "@/lib/types";

/**
 * Archiving a project, and — only if the person asks — its Code Repository
 * with it (deleting a project asks whether to delete the repo as well, with
 * a confirm). The question is asked only where it can be
 * honoured: the Host owns the repository and no other project uses it. It
 * starts unticked; keeping the code is the default.
 */
export function ArchiveDialog({
  project,
  onClose,
  onArchived,
}: {
  project: Project | null;
  onClose: () => void;
  /** Done; `note` says anything the person should know (a deletion refused). */
  onArchived: (id: number, note: string) => void;
}) {
  const [info, setInfo] = useState<ProjectRepository | null>(null);
  const [loading, setLoading] = useState(false);
  const [deleteRepo, setDeleteRepo] = useState(false);
  // Deleting a Code Repository cannot be undone, so it is confirmed by typing
  // its name — the same guard the forges themselves put on it.
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  useEffect(() => {
    setInfo(null);
    setDeleteRepo(false);
    setTyped("");
    setErr("");
    setBusy(false);
    if (!project) return;
    let alive = true;
    setLoading(true);
    (async () => {
      const r = await fetchProjectRepository(project.id);
      if (!alive) return;
      setInfo(r.ok ? r.data : null);
      setLoading(false);
    })();
    return () => {
      alive = false;
    };
  }, [project]);

  if (!project) return null;

  const archive = async () => {
    setBusy(true);
    setErr("");
    const r = await archiveProject(project.id, deleteRepo);
    setBusy(false);
    if (!r.ok) {
      setErr(r.message);
      return;
    }
    const note = r.data.repo_error
      ? r.data.repo_error
      : r.data.repo_deleted
        ? `Archived “${project.name}” and deleted ${r.data.repo}.`
        : "";
    onArchived(project.id, note);
  };

  return (
    <Dialog onOpenChange={(o) => { if (!o && !busy) onClose(); }} open>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Archive “{project.name}”?</DialogTitle>
          <DialogDescription>
            The page leaves the book's tabs for its chapter in the Index. Its conversation and
            plan are kept, and writing on it brings it back.
          </DialogDescription>
        </DialogHeader>
        {loading && <Spinner className="size-4" />}
        {info?.repo && info.can_delete && (
          <label className="flex cursor-pointer items-start gap-2 rounded-md border border-border px-3 py-2 text-sm">
            <input
              checked={deleteRepo}
              className="mt-0.5 size-4 accent-destructive"
              disabled={busy}
              onChange={(e) => {
                setDeleteRepo(e.target.checked);
                setTyped("");
              }}
              type="checkbox"
            />
            <span>
              Also delete its Code Repository, <span className="font-mono">{info.repo}</span>
              <span className="block text-muted-foreground text-xs">
                Every file and every past version in it is gone for good. Leave this off to keep
                the code.
              </span>
            </span>
          </label>
        )}
        {info?.repo && deleteRepo && (
          <label className="block text-sm">
            <span className="text-muted-foreground text-xs">
              To confirm, type <span className="font-mono">{info.repo}</span>
            </span>
            <Input
              aria-label="Type the Code Repository's name to confirm"
              autoFocus
              className="mt-1 font-mono"
              disabled={busy}
              onChange={(e) => setTyped(e.target.value)}
              value={typed}
            />
          </label>
        )}
        {info?.repo && !info.can_delete && info.delete_refusal && (
          <p className="text-muted-foreground text-xs">
            Its Code Repository, <span className="font-mono">{info.repo}</span>, is kept:{" "}
            {info.delete_refusal}
          </p>
        )}
        {err && <p className="text-destructive text-sm">{err}</p>}
        <DialogFooter>
          <Button disabled={busy} onClick={onClose} variant="ghost">
            Cancel
          </Button>
          <Button
            disabled={busy || loading || (deleteRepo && typed.trim() !== info?.repo)}
            onClick={archive}
            variant={deleteRepo ? "destructive" : "default"}
          >
            {busy ? (
              <Spinner className="size-4" />
            ) : deleteRepo ? (
              "Archive and delete the repository"
            ) : (
              "Archive"
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
