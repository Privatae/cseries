// Chat attachments leave the composer typed for the Host's readers, and
// every readable kind of file is accepted (drag and drop files, pictures,
// anything).
import { test } from "node:test";
import assert from "node:assert/strict";
import { ACCEPT, mediaTypeFor, toAttachmentBlock } from "../src/lib/attachments.ts";

test("an untyped file gets its type from its name", () => {
  assert.equal(mediaTypeFor("notes.md", ""), "text/markdown");
  assert.equal(mediaTypeFor("plan.DOCX", "application/octet-stream"),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document");
  assert.equal(mediaTypeFor("x.pdf", "application/pdf"), "application/pdf");
  assert.equal(mediaTypeFor("blob.bin", ""), "application/octet-stream");
});

test("the composer accepts Office, text, email, pdf and pictures", () => {
  for (const t of ["image/*", "application/pdf", ".docx", ".xlsx", ".pptx", ".odt",
                   ".rtf", ".csv", ".md", ".eml", ".html", "text/*"]) {
    assert.ok(ACCEPT.split(",").includes(t), t);
  }
});

test("a picture is an image block, anything else a document block", () => {
  const img = toAttachmentBlock({ type: "file", mediaType: "image/png", filename: "a.png",
    url: "data:image/png;base64,iVBOR" });
  assert.equal(img?.content_block.type, "image");
  const doc = toAttachmentBlock({ type: "file", mediaType: "", filename: "r.md",
    url: "data:;base64,SGVsbG8=" });
  assert.equal(doc?.content_block.type, "document");
  assert.equal(doc?.content_block.source.media_type, "text/markdown");
  assert.equal(doc?.name, "r.md");
});

test("no data URL means no block (the composer then says so)", () => {
  assert.equal(toAttachmentBlock({ type: "file", mediaType: "image/png", url: "blob:x" }), null);
});
