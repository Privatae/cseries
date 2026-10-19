import { ExternalLinkIcon } from "lucide-react";
import { useEffect, useState } from "react";
import { Button } from "../ui/button";
import { fetchArtifactHtml } from "./api";
import { openArtifactWindow, SealedFrame } from "./sealed-frame";
import { nameFor } from "./visual";

/**
 * An artifact this turn made or changed, as the page itself: a live
 * thumbnail (the Artifacts gallery's 1280×800 stage, scaled to the card) that
 * opens the Artifacts window on click. `rev` changes whenever another call in
 * the turn touches the artifact, so a create followed by writes redraws as it
 * goes. An older turn shows the artifact as it is NOW — the History strip in
 * the window holds every earlier revision.
 */
export function ArtifactCard({ id, rev }: { id: number; rev: number }) {
  const [html, setHtml] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    void fetchArtifactHtml(id).then((r) => {
      if (!live) return;
      if (r.ok) {
        setHtml(r.data);
        setError(null);
      } else setError(r.message);
    });
    return () => {
      live = false;
    };
  }, [id, rev]);

  const name = (html && nameFor(html)) || `Artifact ${id}`;
  const open = () => openArtifactWindow(id);
  return (
    <div className="mb-2 w-full max-w-sm overflow-hidden rounded-md border border-border">
      <button
        aria-label={`Open ${name} in Artifacts`}
        className="block w-full cursor-pointer text-left"
        onClick={open}
        type="button"
      >
        {html != null ? (
          <SealedFrame html={html} mode="stage" title={name} />
        ) : (
          <div className="flex aspect-[16/10] w-full items-center justify-center bg-muted/30 text-muted-foreground text-xs">
            {error || "Loading…"}
          </div>
        )}
      </button>
      <div className="flex items-center gap-2 border-border border-t px-2 py-1">
        <span className="min-w-0 flex-1 truncate text-xs">{name}</span>
        <Button onClick={open} size="xs" variant="ghost">
          <ExternalLinkIcon /> Open
        </Button>
      </div>
    </div>
  );
}
