import { CheckIcon, CodeIcon, CopyIcon, DownloadIcon, EyeIcon, Maximize2Icon, SaveIcon } from "lucide-react";
import { createContext, useContext, useState } from "react";
import { Button } from "../ui/button";
import { Dialog, DialogContent, DialogTitle } from "../ui/dialog";
import { saveHtmlAsArtifact } from "./api";
import { openArtifactWindow, SealedFrame } from "./sealed-frame";
import { datedName, fileNameFor, nameFor, VISUAL_LANG } from "./visual";

/** Whether a reply's visuals RUN in this bubble. True only for the user's
 * own Host. In Circles a Peers or People thread carries someone else's words,
 * and their visuals are shown as code — the sandbox would hold it, but nobody
 * should be able to run a page in your window just by messaging you. Default
 * false: a surface that never says is showing someone else's words. */
export const VisualsAllowed = createContext(false);

type SaveState =
  | { s: "idle" }
  | { s: "saving" }
  | { s: "saved"; id: number }
  | { s: "error"; message: string };

function Code({ code }: { code: string }) {
  return (
    <pre className="max-h-96 overflow-auto whitespace-pre-wrap break-words rounded-md bg-muted/40 px-3 py-2 font-mono text-xs">
      {code}
    </pre>
  );
}

/**
 * A ```visual fence in a Host reply, drawn as the page it describes.
 * While the reply streams it is a placeholder (a half-written page would
 * restart its scripts on every chunk); once the fence closes it runs in the
 * sealed frame. Nothing is kept unless the user asks: Save as artifact,
 * Download, Copy, Full size (the usual save/export options, nothing automatic).
 */
export function VisualBlock({
  code,
  isIncomplete,
  meta,
}: {
  code: string;
  isIncomplete: boolean;
  language: string;
  meta?: string;
}) {
  const allowed = useContext(VisualsAllowed);
  const [view, setView] = useState<"page" | "code">("page");
  const [full, setFull] = useState(false);
  const [copied, setCopied] = useState(false);
  const [blocked, setBlocked] = useState<string[]>([]);
  const [save, setSave] = useState<SaveState>({ s: "idle" });
  const name = nameFor(code, meta);

  if (isIncomplete) {
    const bytes = new Blob([code]).size;
    return (
      <div className="my-2 flex items-center gap-2 rounded-md border border-border border-dashed px-3 py-3 text-muted-foreground text-xs">
        <span aria-hidden className="size-1.5 animate-pulse rounded-full bg-current" />
        Drawing the visual{name ? ` “${name}”` : ""}… {bytes < 1024 ? `${bytes} B` : `${(bytes / 1024).toFixed(1)} KB`}
      </div>
    );
  }
  if (!allowed) {
    return (
      <div className="my-2">
        <div className="mb-1 text-muted-foreground text-xs">
          A visual, shown as code — visuals run only in your own Host's replies.
        </div>
        <Code code={code} />
      </div>
    );
  }

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      setView("code"); // the source is then on screen to select by hand
    }
  };
  const download = () => {
    // octet-stream, never text/html: the blob: URL has THIS app's origin, so
    // if a browser ever navigated to it instead of saving it, an HTML type
    // would run the Host's page unsealed with the app's session. Bytes are
    // only ever saved. The file still ends in .html.
    const url = URL.createObjectURL(new Blob([code], { type: "application/octet-stream" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = fileNameFor(name || "visual");
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  const keep = async () => {
    setSave({ s: "saving" });
    const r = await saveHtmlAsArtifact(name || datedName(new Date()), code);
    setSave(r.ok ? { s: "saved", id: r.data.artifact_id } : { s: "error", message: r.message });
  };

  return (
    <div className="my-2 overflow-hidden rounded-md border border-border" data-lang={VISUAL_LANG}>
      <div className="flex flex-wrap items-center gap-1 border-border border-b bg-muted/30 px-2 py-1">
        <span className="mr-auto truncate text-muted-foreground text-xs">{name || "Visual"}</span>
        <Button onClick={() => setView(view === "page" ? "code" : "page")} size="xs" variant="ghost">
          {view === "page" ? <CodeIcon /> : <EyeIcon />} {view === "page" ? "Code" : "Visual"}
        </Button>
        <Button onClick={() => void copy()} size="xs" variant="ghost">
          {copied ? <CheckIcon /> : <CopyIcon />} {copied ? "Copied" : "Copy"}
        </Button>
        <Button onClick={download} size="xs" variant="ghost">
          <DownloadIcon /> Download
        </Button>
        <Button onClick={() => setFull(true)} size="xs" variant="ghost">
          <Maximize2Icon /> Full size
        </Button>
        {save.s === "saved" ? (
          <Button onClick={() => openArtifactWindow(save.id)} size="xs" variant="outline">
            <CheckIcon /> Saved — open
          </Button>
        ) : (
          <Button disabled={save.s === "saving"} onClick={() => void keep()} size="xs" variant="ghost">
            <SaveIcon /> {save.s === "saving" ? "Saving…" : "Save as artifact"}
          </Button>
        )}
      </div>
      {save.s === "error" && (
        <div className="border-border border-b px-3 py-1.5 text-destructive text-xs">
          Couldn't save it: {save.message}
        </div>
      )}
      {blocked.length > 0 && view === "page" && (
        <div className="border-border border-b px-3 py-1.5 text-muted-foreground text-xs">
          Not loaded — visuals run sealed, with no outside requests: {blocked.join(", ")}
        </div>
      )}
      {view === "page" ? (
        <SealedFrame html={code} mode="fit" onBlocked={setBlocked} title={name || "Visual"} />
      ) : (
        <Code code={code} />
      )}
      <Dialog onOpenChange={setFull} open={full}>
        <DialogContent className="flex h-[88vh] max-w-[94vw] flex-col gap-2 p-3 sm:max-w-[94vw]">
          <DialogTitle className="pr-8 text-sm">{name || "Visual"}</DialogTitle>
          <div className="min-h-0 flex-1 overflow-hidden rounded-md border border-border">
            {full && <SealedFrame html={code} mode="fill" title={name || "Visual"} />}
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
