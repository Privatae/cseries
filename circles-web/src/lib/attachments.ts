// Chat attachments: what the composer takes, and the content block the
// /messages endpoint receives. Pure (no React), so node --test covers it.
import type { FileUIPart } from "ai";
import type { AttachmentBlock } from "./types";

// Media types by extension, for files the browser gives no (or a generic)
// type: the Host picks its reader by media type, and "application/octet-stream"
// has none.
const TYPE_BY_EXT: Record<string, string> = {
  md: "text/markdown", markdown: "text/markdown", txt: "text/plain",
  csv: "text/csv", tsv: "text/tab-separated-values", json: "application/json",
  html: "text/html", htm: "text/html", xml: "application/xml",
  rtf: "application/rtf", eml: "message/rfc822", pdf: "application/pdf",
  docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  pptx: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  odt: "application/vnd.oasis.opendocument.text",
  ods: "application/vnd.oasis.opendocument.spreadsheet",
  odp: "application/vnd.oasis.opendocument.presentation",
};

export function mediaTypeFor(filename: string | undefined, given: string): string {
  if (given && given !== "application/octet-stream") return given;
  const ext = (filename ?? "").toLowerCase().split(".").pop() ?? "";
  return TYPE_BY_EXT[ext] ?? (given || "application/octet-stream");
}

// What the composer takes: every type the Host reads (server/readers) plus
// pictures. Extensions ride along for files the browser leaves untyped.
export const ACCEPT = [
  "image/*", "text/*", "application/pdf", "application/json", "application/rtf",
  "message/rfc822", ...Object.values(TYPE_BY_EXT),
  ...Object.keys(TYPE_BY_EXT).map((e) => `.${e}`),
].filter((v, i, a) => a.indexOf(v) === i).join(",");

// One file's bound is the Host's own (server/routes/context.py
// MAX_FILE_UPLOAD_BYTES, 25 MiB), the number the send route holds it to.
export const MAX_FILE_BYTES = 25 * 1024 * 1024;

/** Whether a file is a type the chat takes (the ACCEPT list, as the rail's
 * prompt-input.tsx matchesAccept read it): ".md" patterns match the name,
 * "image/*" a media-type prefix, the rest the exact type. */
export function acceptsFile(f: { name: string; type: string }, accept: string = ACCEPT): boolean {
  return accept
    .split(",")
    .map((x) => x.trim())
    .filter(Boolean)
    .some((pattern) => {
      if (pattern.startsWith(".")) return f.name.toLowerCase().endsWith(pattern.toLowerCase());
      if (pattern.endsWith("/*")) return f.type.startsWith(pattern.slice(0, -1));
      return f.type === pattern;
    });
}

/** Files offered to the composer, sorted into what it takes and what it
 * names back as refused: a type it does not read, or too large. */
export function sortFiles<F extends { name: string; type: string; size: number }>(
  files: readonly F[],
): { take: F[]; wrongType: F[]; tooBig: F[] } {
  const wrongType = files.filter((f) => !acceptsFile(f));
  const typed = files.filter((f) => acceptsFile(f));
  return {
    take: typed.filter((f) => f.size <= MAX_FILE_BYTES),
    wrongType,
    tooBig: typed.filter((f) => f.size > MAX_FILE_BYTES),
  };
}

// Convert an AI-SDK FileUIPart (post data-URL conversion) into the content
// block /messages accepts, or null when the browser left no data URL.
export function toAttachmentBlock(part: FileUIPart): AttachmentBlock | null {
  const url = part.url ?? "";
  // Expect a base64 data URL: data:<media_type>;base64,<data>
  const m = /^data:([^;]*);base64,(.*)$/s.exec(url);
  if (!m) return null;
  const resolvedMedia = mediaTypeFor(part.filename, part.mediaType || m[1]);
  const data = m[2];
  if (!data) return null;
  const isImage = resolvedMedia.startsWith("image/");
  const type: "image" | "document" = isImage ? "image" : "document";
  return {
    name: part.filename,
    content_block: {
      type,
      source: { type: "base64", media_type: resolvedMedia, data },
    },
  };
}

