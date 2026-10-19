import { useEffect, useId, useRef, useState } from "react";
import { PaperclipIcon, SquareIcon, XIcon } from "lucide-react";
import { composerHint } from "@kit/book/hold-to-think";
import { PressButton, type PressHandle } from "@kit/book/press-button";
import { storeVaultSecret } from "@/lib/api";
import { onCompose } from "@/lib/compose-inbox";
import { ACCEPT, MAX_FILE_BYTES, sortFiles, toAttachmentBlock } from "@/lib/attachments";
import {
  applyMemory,
  describeFinding,
  hardFindings,
  keysHint,
  redactSecrets,
  rememberDecision,
  replaceWithKeysRefs,
  scanSecrets,
  secretValue,
  softFindings,
  type Finding,
} from "@/lib/secret-shapes";
import type { AttachmentBlock } from "@/lib/types";

function readFile(file: File): Promise<AttachmentBlock | null> {
  return new Promise((resolve) => {
    const r = new FileReader();
    r.onload = () =>
      resolve(
        toAttachmentBlock({ type: "file", url: String(r.result ?? ""), mediaType: file.type, filename: file.name }),
      );
    r.onerror = () => resolve(null);
    r.readAsDataURL(file);
  });
}

/** Words held back because they look like a secret (the rail composer's
 * rule, lib/secret-shapes.ts: hard shapes are cut by the pod anyway, so the
 * offer is to send them cut, to seal them into Keys, or to edit; a soft one
 * may go as written, and is then not asked about again). */
interface Held {
  text: string;
  findings: Finding[];
  files: AttachmentBlock[];
  reached: string | null;
  pills: string[];
}

/** What a send carries beside its words: the files, the effort a hold
 * reached, and the app pills riding it (the rail's composer.tsx: a pill is
 * folded into the words and also named on the turn's frame). */
export type HostSend = (text: string, files: AttachmentBlock[], reached: string | null, pills: string[]) => void;

/**
 * The box on your Host's back (plan, slice 10; mismatch 9's default): Send
 * is the book's press, a tap at the page's effort and a hold to think harder
 * (app-web-kit/book/press-button.tsx, hold-to-think.ts); while a turn runs,
 * Send is Nudge, Stop sits beside it, and Enter nudges. Files ride a send:
 * picked, pasted or dropped, of the types the Host reads and up to 25 MB
 * each, with what is refused named. What an app hands the chat
 * (`cseries:compose`, the Artifacts window and the Orchestrator) arrives here:
 * a pill rides the next send, a "send" goes at once as your own words.
 */
export function HostComposer({
  placeholder,
  busy,
  nudging,
  levels,
  level,
  climb,
  onSend,
  onNudge,
  onStop,
  error,
}: {
  placeholder: string;
  /** A turn is running on this page. */
  busy: boolean;
  /** Words typed now go into the running turn. */
  nudging: boolean;
  levels: readonly string[];
  level: string | null;
  climb: readonly string[];
  onSend: HostSend;
  onNudge: (text: string) => void;
  onStop: () => void;
  error?: string | null;
}) {
  const [draft, setDraft] = useState("");
  const [files, setFiles] = useState<AttachmentBlock[]>([]);
  const [held, setHeld] = useState<Held | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [pills, setPills] = useState<string[]>([]);
  const [sealing, setSealing] = useState<{ busy: boolean; error: string | null }>({ busy: false, error: null });
  const [dragging, setDragging] = useState(false);
  const pressRef = useRef<PressHandle | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const hintId = useId();

  // A quiet note clears itself (the rail's composer did the same).
  useEffect(() => {
    if (!note) return;
    const t = window.setTimeout(() => setNote(null), 8000);
    return () => window.clearTimeout(t);
  }, [note]);

  // What an app hands the chat (the shell relays it, same origin only; the
  // words are capped and never read as markup): mode 'pill' rides the next
  // send, mode 'send' goes now as the person's own message. The listener is
  // the page's (lib/compose-inbox.ts), so one that arrived while this box was
  // not drawn is held for it.
  const onSendRef = useRef(onSend);
  onSendRef.current = onSend;
  useEffect(
    () =>
      onCompose((m) => {
        if (m.mode === "send") onSendRef.current(m.text, [], null, []);
        else setPills((p) => [...p.slice(-4), m.text]);
      }),
    [],
  );

  const addFiles = async (list: File[]) => {
    if (!list.length) return;
    const { take, wrongType, tooBig } = sortFiles(list);
    const said: string[] = [];
    if (wrongType.length) said.push(`Not a type this chat takes: ${wrongType.map((f) => f.name).join(", ")}.`);
    if (tooBig.length)
      said.push(
        `Too large to attach (over ${Math.round(MAX_FILE_BYTES / (1024 * 1024))} MB each): ${tooBig.map((f) => f.name).join(", ")}.`,
      );
    const read = await Promise.all(take.map(readFile));
    const bad = take.filter((_, i) => !read[i]).map((f) => f.name);
    if (bad.length) said.push(`Could not attach ${bad.join(", ")}. Try adding it again.`);
    if (said.length) setNote(said.join(" "));
    setFiles((cur) => [...cur, ...(read.filter(Boolean) as AttachmentBlock[])]);
  };

  const submit = async (reached: string | null = null) => {
    const text = draft.trim();
    if (nudging) {
      if (!text) return;
      setDraft("");
      // A nudge goes straight into the turn: hard shapes are cut first, as
      // the rail's Nudge does (the pod cuts them again, session.push_nudge).
      onNudge(redactSecrets(text).text);
      return;
    }
    if (!text && files.length === 0 && pills.length === 0) return;
    const sending = files;
    const sentPills = [...pills];
    const withPills = sentPills.length ? [...sentPills.map((p) => `[${p}]`), text].filter(Boolean).join("\n") : text;
    setDraft("");
    setFiles([]);
    setPills([]);
    const findings = scanSecrets(withPills);
    if (findings.length) {
      // Memory first: a key already in Keys becomes its reference, a soft
      // value already allowed is not asked about again.
      const m = await applyMemory(withPills, findings);
      if (m.notes.length) setNote(m.notes.join(" "));
      if (m.remaining.length) {
        setSealing({ busy: false, error: null });
        setHeld({ text: m.text, findings: m.remaining, files: sending, reached, pills: sentPills });
        return;
      }
      onSend(m.text, sending, reached, sentPills);
      return;
    }
    onSend(withPills, sending, reached, sentPills);
  };

  // "Store in Keys": seal every hard hit into the vault (POST
  // /api/vault/secrets, write:vault), then send with (stored in Keys as "…")
  // where each was. One failure stops it: nothing is sent and the strip says
  // why.
  const storeHeld = async (h: Held) => {
    setSealing({ busy: true, error: null });
    const stored: Array<{ f: Finding; name: string }> = [];
    for (const f of hardFindings(h.findings)) {
      const hint = keysHint(f);
      if (!hint) continue;
      const r = await storeVaultSecret(hint.name, hint.cred_type, secretValue(h.text, f));
      if (!r.ok) {
        setSealing({ busy: false, error: `Could not store the ${f.label}: ${r.message}` });
        return;
      }
      stored.push({ f, name: r.data.name });
      await rememberDecision(secretValue(h.text, f), { kind: "stored", name: r.data.name });
    }
    setSealing({ busy: false, error: null });
    setHeld(null);
    onSend(replaceWithKeysRefs(h.text, stored), h.files, h.reached, h.pills);
  };

  // "Send as written" on a soft hit: remembered, so the same value is not
  // held again in this browser.
  const allowAndSend = async (h: Held) => {
    for (const f of softFindings(h.findings)) await rememberDecision(secretValue(h.text, f), { kind: "allowed" });
    setHeld(null);
    onSend(h.text, h.files, h.reached, h.pills);
  };

  const hard = held ? hardFindings(held.findings) : [];
  const soft = held ? softFindings(held.findings) : [];
  const storeNames = hard.map((f) => keysHint(f)?.name).filter((n): n is string => !!n);
  return (
    <div
      className="composer-wrap"
      data-book-nudges
      data-dragging={dragging || undefined}
      onDragLeave={() => setDragging(false)}
      onDragOver={(e) => {
        if (nudging || !e.dataTransfer?.types?.includes("Files")) return;
        e.preventDefault();
        setDragging(true);
      }}
      onDrop={(e) => {
        if (nudging || !e.dataTransfer?.files?.length) return;
        e.preventDefault();
        setDragging(false);
        void addFiles(Array.from(e.dataTransfer.files));
      }}
    >
      {held && (
        <div className="held" data-secret-hold role="alert">
          <span>
            {hard.length
              ? `This looks like a credential: ${hard.map(describeFinding).join(", ")}. Your Host would only see [REDACTED] in its place; keys belong in the Keys app, not in chat.`
              : `This might be a secret: ${soft.map(describeFinding).join(", ")}.`}
            {hard.length > 0 && soft.length > 0 && (
              <span data-secret-soft-too>
                {" "}
                Also possibly sensitive: {soft.map(describeFinding).join(", ")}. Cut too if you send redacted.
              </span>
            )}
            {storeNames.length > 0 && (
              <span> Or seal it into Keys here; the message then says where it went.</span>
            )}
            {sealing.error && (
              <span className="err" role="alert">
                {" "}
                {sealing.error}
              </span>
            )}
          </span>
          <span style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
            <button
              className="btn quiet"
              disabled={sealing.busy}
              onClick={() => {
                setDraft(held.text);
                setFiles(held.files);
                setHeld(null);
                inputRef.current?.focus();
              }}
              type="button"
            >
              Edit message
            </button>
            {storeNames.length > 0 && (
              <button
                className="btn"
                data-secret-store
                disabled={sealing.busy}
                onClick={() => void storeHeld(held)}
                type="button"
              >
                {sealing.busy
                  ? "Sealing…"
                  : storeNames.length === 1
                    ? `Store in Keys as "${storeNames[0]}"`
                    : `Store ${storeNames.length} in Keys`}
              </button>
            )}
            {hard.length === 0 && (
              <button className="btn" data-secret-as-written onClick={() => void allowAndSend(held)} type="button">
                Send as written
              </button>
            )}
            <button
              className="btn primary"
              disabled={sealing.busy}
              onClick={() => {
                const h = held;
                setHeld(null);
                onSend(redactSecrets(h.text, ["hard", "soft"]).text, h.files, h.reached, h.pills);
              }}
              type="button"
            >
              Send redacted
            </button>
          </span>
        </div>
      )}
      {pills.length > 0 && (
        <div className="files" data-pills>
          {pills.map((p, i) => (
            <span data-pill key={`${i}-${p.slice(0, 24)}`} title={p}>
              {p.length > 60 ? `${p.slice(0, 58)}…` : p}{" "}
              <button
                aria-label="Remove this from the message"
                onClick={() => setPills((cur) => cur.filter((_, j) => j !== i))}
                type="button"
              >
                <XIcon size={11} />
              </button>
            </span>
          ))}
        </div>
      )}
      {files.length > 0 && (
        <div className="files">
          {files.map((f, i) => (
            <span data-file key={`${i}-${f.name}`}>
              {f.name || "a file"}{" "}
              <button
                aria-label={`Remove ${f.name || "the file"}`}
                onClick={() => setFiles((x) => x.filter((_, j) => j !== i))}
                type="button"
              >
                <XIcon size={11} />
              </button>
            </span>
          ))}
        </div>
      )}
      <div className="composer">
        <input
          accept={ACCEPT}
          data-file-input
          hidden
          multiple
          onChange={(e) => {
            const list = Array.from(e.target.files ?? []);
            e.target.value = "";
            void addFiles(list);
          }}
          ref={fileRef}
          type="file"
        />
        {!nudging && (
          <button
            aria-label="Add files"
            className="tool"
            data-attach
            onClick={() => fileRef.current?.click()}
            title="Add files"
            type="button"
          >
            <PaperclipIcon size={16} />
          </button>
        )}
        <textarea
          aria-describedby={hintId}
          aria-label={placeholder}
          onBlur={() => pressRef.current?.cancel()}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            // Backspace in an empty box takes off the last file (the rail's
            // prompt-input.tsx did the same).
            if (e.key === "Backspace" && !draft && files.length && !nudging) {
              e.preventDefault();
              setFiles((x) => x.slice(0, -1));
              return;
            }
            if (e.key !== "Enter" || e.shiftKey) return;
            // The Enter that ends an IME composition commits the words.
            if (e.nativeEvent.isComposing || e.nativeEvent.keyCode === 229) return;
            e.preventDefault();
            if (e.repeat) return;
            if (!nudging && climb.length && draft.trim()) pressRef.current?.start();
            else void submit();
          }}
          onKeyUp={(e) => {
            if (e.key === "Enter") pressRef.current?.release();
          }}
          onPaste={(e) => {
            if (nudging) return;
            const pasted = Array.from(e.clipboardData?.items ?? [])
              .filter((it) => it.kind === "file")
              .map((it) => it.getAsFile())
              .filter((f): f is File => !!f);
            if (!pasted.length) return;
            e.preventDefault();
            void addFiles(pasted);
          }}
          placeholder={nudging ? "Nudge your Host while it works…" : placeholder}
          ref={inputRef}
          rows={1}
          value={draft}
        />
        {busy && (
          <button
            aria-label="Stop the turn"
            className="tool"
            data-stop
            onClick={onStop}
            title="Stop the turn"
            type="button"
          >
            <SquareIcon size={15} />
          </button>
        )}
        {nudging ? (
          <button
            className="nudge"
            data-nudge-btn
            disabled={!draft.trim()}
            onClick={() => void submit()}
            title="Your words reach your Host before its next step, without stopping it"
            type="button"
          >
            Nudge
          </button>
        ) : (
          <PressButton
            climb={climb}
            describedBy={hintId}
            disabled={!draft.trim() && files.length === 0 && pills.length === 0}
            level={level}
            levels={levels}
            onSend={(reached) => void submit(reached)}
            ref={pressRef}
          />
        )}
      </div>
      <p className="hint" data-composer-hint id={hintId}>
        {error || note || composerHint({ nudging, page: level, climb })}
      </p>
    </div>
  );
}

/** The box on a person's front, or a room's back: plain words to people or
 * Hosts, no model turn. */
export function PlainComposer({
  placeholder,
  disabled,
  why,
  onSend,
  round,
}: {
  placeholder: string;
  disabled?: boolean;
  /** Why the box is closed, said in its place. */
  why?: string | null;
  onSend: (text: string) => Promise<string | null>;
  round?: boolean;
}) {
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const go = async () => {
    const text = draft.trim();
    if (!text || busy || disabled) return;
    setBusy(true);
    setErr(null);
    const e = await onSend(text);
    setBusy(false);
    if (e) setErr(e);
    else setDraft("");
  };
  return (
    <div className="composer-wrap">
      {(err || why) && (
        <p className="hint" role={err ? "alert" : undefined} style={{ margin: "0 26px 8px" }}>
          {err || why}
        </p>
      )}
      <div className="composer">
        <textarea
          aria-label={placeholder}
          disabled={disabled}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key !== "Enter" || e.shiftKey) return;
            if (e.nativeEvent.isComposing || e.nativeEvent.keyCode === 229) return;
            e.preventDefault();
            void go();
          }}
          placeholder={placeholder}
          rows={1}
          value={draft}
        />
        <button
          aria-label="Send"
          className="send"
          data-send
          disabled={disabled || busy || !draft.trim()}
          onClick={() => void go()}
          type="button"
        >
          {round ? "↑" : "↵"}
        </button>
      </div>
    </div>
  );
}
