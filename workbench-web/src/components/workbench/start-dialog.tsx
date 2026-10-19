import { useEffect, useRef, useState } from "react";
import { ArrowLeftIcon, FolderGitIcon, PlusIcon, SearchIcon } from "lucide-react";
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { Spinner } from "@/components/ui/spinner";
import { adoptGeneralInto, createProject, fetchCodeRepositories, searchProjects } from "@/lib/api";
import { GROUP_LABEL, groupOfProject, suggestTitle } from "@/lib/project-groups";
import type { CodeRepository, SearchHit } from "@/lib/types";
import { cn } from "@/lib/utils";

/** The Select value for "a new repository" (a Select item cannot be ""). */
const NEW_REPO = "__new__";

/** How long the box waits after the last keystroke before asking the index.
 * 200 ms — long enough to skip the intermediate letters of a word, short
 * enough that the list feels attached to the typing. */
const DEBOUNCE_MS = 200;
/** Matches shown under the box. Five: enough to disambiguate similar
 * projects, few enough to scan without reading. */
const MAX_HITS = 5;

/**
 * "What would you like to work on?" — the one door into the Workbench.
 *
 * One text box. As the user types, the project index ranks their existing
 * projects underneath; picking one opens that project's thread with the
 * typed text already in the composer, so nothing typed is thrown away. If
 * nothing fits, the same text becomes a NEW project's description and the
 * user is asked only for a title — suggested from the text, one keystroke
 * from being theirs. The description is never the thing they have to invent.
 *
 * Naming a new project also chooses its Code Repository: a new one — the default — or one the
 * Host can already reach. A repository that already has a live project, or
 * that the Host can only read, is offered as a fork instead, so two projects
 * never share one.
 *
 * `pendingGeneral` is the one-time migration after General was retired: the
 * projectless thread still holds turns, so whichever project this dialog
 * opens — picked or created — adopts them (retag, never delete; the same
 * path Circles→project uses).
 */
export function StartDialog({
  open,
  onOpenChange,
  pendingGeneral = null,
  onOpened,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Turns sitting in the retired General thread: how many user messages and
   * the text to pre-fill the box with. Null = nothing to migrate. */
  pendingGeneral?: { userTurns: number; seedText: string } | null;
  /** The chosen or created project is open; `text` goes into the composer.
   * `created` is true when this call made the project. */
  onOpened: (p: {
    id: number;
    name: string;
    text: string;
    created: boolean;
    adopted?: boolean;
    /** Something to say about the project's Code Repository (reading a
     * mounted repository's code in, or why it could not be made). */
    note?: string | null;
  }) => void;
}) {
  const [text, setText] = useState("");
  const [hits, setHits] = useState<SearchHit[]>([]);
  const [semantic, setSemantic] = useState(false);
  const [searching, setSearching] = useState(false);
  const [mode, setMode] = useState<"search" | "title">("search");
  const [title, setTitle] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  // The Code Repository the new project mounts. NEW = a fresh one (the
  // default, as it always was); a Select item cannot carry "".
  const [repos, setRepos] = useState<CodeRepository[] | null>(null);
  const [repo, setRepo] = useState<string>(NEW_REPO);
  const [fork, setFork] = useState(false);
  const boxRef = useRef<HTMLTextAreaElement | null>(null);
  const titleRef = useRef<HTMLInputElement | null>(null);
  // Only the newest request may write the list: a slow early response must
  // not overwrite the ranking for what the user has typed since.
  const seqRef = useRef(0);

  // Fresh state on every open; the General seed lands in the box.
  useEffect(() => {
    if (!open) return;
    setText(pendingGeneral?.seedText ?? "");
    setHits([]);
    setSemantic(false);
    setMode("search");
    setTitle("");
    setErr("");
    setBusy(false);
    setRepos(null);
    setRepo(NEW_REPO);
    setFork(false);
    setTimeout(() => boxRef.current?.focus(), 0);
  }, [open, pendingGeneral]);

  // The repositories are read when a new project is being named — the only
  // time they are offered. A failure leaves "a new repository", which is what
  // every project got before.
  useEffect(() => {
    if (!open || mode !== "title" || repos != null) return;
    (async () => {
      const r = await fetchCodeRepositories();
      setRepos(r.ok && r.data.connected ? r.data.repositories : []);
    })();
  }, [open, mode, repos]);

  const chosen = repo === NEW_REPO ? null : (repos ?? []).find((x) => x.full_name === repo) ?? null;
  // A fork is needed where the chosen repository already has a live project,
  // or the Host can only read it.
  const needsFork = !!chosen && (!!chosen.project || !chosen.can_write);

  // Debounced ranking. An empty box lists the most recently active projects
  // (the index's empty-query state), so the user sees where they were.
  useEffect(() => {
    if (!open || mode !== "search") return;
    const q = text.trim();
    const seq = ++seqRef.current;
    setSearching(true);
    const t = setTimeout(async () => {
      const r = await searchProjects(q, MAX_HITS);
      if (seq !== seqRef.current) return;
      setSearching(false);
      if (r.ok) {
        setHits(r.data.projects.filter((p) => groupOfProject(p) !== "archived" || q.length > 0));
        setSemantic(r.data.semantic);
      } else {
        setHits([]);
      }
    }, DEBOUNCE_MS);
    return () => clearTimeout(t);
  }, [open, mode, text]);

  useEffect(() => {
    if (mode === "title") setTimeout(() => titleRef.current?.select(), 0);
  }, [mode]);

  const typed = text.trim();
  const migrating = !!pendingGeneral && pendingGeneral.userTurns > 0;

  const pick = async (p: SearchHit) => {
    if (busy) return;
    setBusy(true);
    setErr("");
    let adopted = false;
    if (migrating) {
      const r = await adoptGeneralInto(p.id);
      // Fail-soft: the project still opens; the turns stay where they were
      // and the migration offers again next open.
      adopted = r.ok && !r.data.error;
    }
    setBusy(false);
    onOpenChange(false);
    onOpened({ id: p.id, name: p.name, text: typed, created: false, adopted });
  };

  const toTitle = () => {
    if (!typed) return;
    setTitle(suggestTitle(typed));
    setMode("title");
  };

  const create = async () => {
    const name = title.trim();
    if (!name || busy || (needsFork && !fork)) return;
    setBusy(true);
    setErr("");
    const r = await createProject(
      name,
      typed,
      migrating ? { kind: "app", ref: "workbench" } : undefined,
      chosen ? { repo: chosen.full_name, fork: needsFork } : undefined,
    );
    setBusy(false);
    if (!r.ok) {
      setErr(r.message);
      return;
    }
    onOpenChange(false);
    onOpened({
      id: r.data.id,
      name,
      text: typed,
      created: true,
      adopted: migrating && !!r.data.adopted && !r.data.adopted.error,
      note: r.data.repo_note ?? null,
    });
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-xl">
        {mode === "search" ? (
          <>
            <DialogHeader>
              <DialogTitle>What would you like to work on?</DialogTitle>
              <DialogDescription>
                Describe it. Matching projects appear below — pick one, or make
                it a new project.
              </DialogDescription>
            </DialogHeader>
            {migrating && (
              <p className="rounded-md border border-border bg-muted/40 px-3 py-2 text-xs">
                {pendingGeneral!.userTurns === 1
                  ? "One message"
                  : `${pendingGeneral!.userTurns} messages`}{" "}
                from your earlier open conversation will move into the project
                you open here — organized, never deleted.
              </p>
            )}
            <Textarea
              autoFocus
              className="min-h-[72px]"
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  // Enter takes the top match when the text matches something;
                  // otherwise it goes straight to naming a new project.
                  if (typed && hits.length > 0) void pick(hits[0]);
                  else toTitle();
                }
              }}
              placeholder="What would you like to work on?"
              ref={boxRef}
              rows={3}
              value={text}
            />
            <div className="flex min-h-[2rem] flex-col gap-1">
              {hits.length > 0 && (
                <p className="flex items-center gap-1.5 text-muted-foreground text-xs">
                  <SearchIcon className="size-3" />
                  {typed ? "Matching projects" : "Recent projects"}
                  {searching && <Spinner className="size-3" />}
                  {typed && semantic && <span>· ranked with your embedding model</span>}
                </p>
              )}
              {hits.map((p, i) => (
                <button
                  className={cn(
                    "flex w-full items-start gap-2 rounded-md border border-border px-3 py-2 text-left hover:bg-accent/60",
                    i === 0 && typed && "border-primary/50",
                  )}
                  disabled={busy}
                  key={p.id}
                  onClick={() => pick(p)}
                  type="button"
                >
                  <FolderGitIcon className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
                  <span className="min-w-0 flex-1">
                    <span className="flex items-center gap-2">
                      <span className="truncate font-medium text-sm">{p.name}</span>
                      <span className="shrink-0 rounded-full bg-muted px-1.5 text-[10px] text-muted-foreground uppercase">
                        {GROUP_LABEL[groupOfProject(p)]}
                      </span>
                    </span>
                    {p.description ? (
                      <span className="block truncate text-muted-foreground text-xs">{p.description}</span>
                    ) : null}
                  </span>
                </button>
              ))}
              {typed && !searching && hits.length === 0 && (
                <p className="text-muted-foreground text-xs">
                  Nothing matches — this will be a new project.
                </p>
              )}
            </div>
            {err && <p className="text-destructive text-sm">{err}</p>}
            <DialogFooter>
              <Button disabled={!typed || busy} onClick={toTitle} variant={hits.length && typed ? "outline" : "default"}>
                <PlusIcon className="size-4" />
                {hits.length && typed ? "None of these — new project" : "New project"}
              </Button>
            </DialogFooter>
          </>
        ) : (
          <>
            <DialogHeader>
              <DialogTitle>Name the project</DialogTitle>
              <DialogDescription>
                What you typed is the description. The title is suggested from it —
                change it if you like.
              </DialogDescription>
            </DialogHeader>
            <div className="flex flex-col gap-3">
              <Input
                onChange={(e) => setTitle(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    void create();
                  }
                }}
                placeholder="Project title"
                ref={titleRef}
                value={title}
              />
              <Textarea
                className="min-h-[72px]"
                onChange={(e) => setText(e.target.value)}
                placeholder="Description"
                rows={3}
                value={text}
              />
              <label className="flex flex-col gap-1 text-sm">
                <span className="text-muted-foreground text-xs">
                  Code Repository — where this project's code lives
                </span>
                <Select
                  onValueChange={(v) => {
                    setRepo(v);
                    setFork(false);
                  }}
                  value={repo}
                >
                  <SelectTrigger className="w-full">
                    <SelectValue placeholder="A new repository" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value={NEW_REPO}>A new repository, made for this project</SelectItem>
                    {(repos ?? []).map((r) => (
                      <SelectItem key={r.full_name} value={r.full_name}>
                        {r.full_name}
                        {r.project ? ` — has a project` : !r.can_write ? " — read only" : ""}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                {repos == null && <span className="text-muted-foreground text-xs">Loading repositories…</span>}
              </label>
              {chosen && needsFork && (
                <div className="rounded-md border border-border bg-muted/40 px-3 py-2 text-xs">
                  <p>
                    {chosen.project
                      ? `This code already has a project, “${chosen.project.name}”. `
                      : "The Host can read this code but can't change it. "}
                    Fork it into this new project? That makes a copy, {chosen.name}, that only
                    this project works in.
                  </p>
                  <div className="mt-2 flex flex-wrap gap-2">
                    <Button onClick={() => setFork(true)} size="sm" variant={fork ? "default" : "outline"}>
                      {fork ? "Forking into this project" : "Fork into this project"}
                    </Button>
                    {chosen.project && (
                      <Button
                        onClick={() => {
                          const p = chosen.project!;
                          onOpenChange(false);
                          onOpened({ id: p.id, name: p.name, text: typed, created: false });
                        }}
                        size="sm"
                        variant="ghost"
                      >
                        Open “{chosen.project.name}” instead
                      </Button>
                    )}
                  </div>
                </div>
              )}
              {err && <p className="text-destructive text-sm">{err}</p>}
            </div>
            <DialogFooter className="sm:justify-between">
              <Button disabled={busy} onClick={() => setMode("search")} variant="ghost">
                <ArrowLeftIcon className="size-4" />
                Back
              </Button>
              <Button disabled={!title.trim() || busy || (needsFork && !fork)} onClick={create}>
                {busy ? <Spinner className="size-4" /> : "Create project"}
              </Button>
            </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
