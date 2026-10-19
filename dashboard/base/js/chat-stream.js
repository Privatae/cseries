/**
 * chat-stream.js — shared AI SDK Data Stream Protocol client.
 *
 * The browser-side parser for what brain_service.send_message emits:
 *
 *   f:{"messageId":"<uuid>"}                       — start step
 *   2:[]                                           — heartbeat / no-op data array
 *   9:{"toolCallId","toolName","args"}             — tool call dispatched
 *   a:{"toolCallId","result"}                      — tool result
 *   0:"text content"                               — text delta (concatenable)
 *   d:{"finishReason","usage"}                     — finish step
 *   e:{"finishReason","usage","isContinued":bool}  — finish message (terminal)
 *   3:"error message"                              — error (terminal)
 *
 * Used by Workbench (system-apps/workbench/app.js) and Circles
 * (system-apps/circles/app.js) — was duplicated across the two until the
 * Phase 4 follow-up DRY pass put it here. Served from the kernel image
 * at /js/chat-stream.js via the dashboard overlay mount.
 *
 * Usage:
 *   const ctrl = new AbortController();
 *   const res = await fetch('/api/brain/messages', {
 *     method: 'POST',
 *     headers: {
 *       'Content-Type': 'application/json',
 *       'Accept': 'text/x-vercel-ai-data-stream',
 *     },
 *     body: JSON.stringify({ message, project_id }),
 *     signal: ctrl.signal,
 *   });
 *   await ChatStream.consume(res, {
 *     onText:   (delta, fullSoFar) => { ... },
 *     onTool:   ({toolCallId, toolName, args}) => { ... },
 *     onResult: ({toolCallId, result, status}) => { ... },
 *     onData:   (item) => { ... },           // each item of a non-empty 2: array
 *     onError:  (message) => { ... },
 *     onDone:   ({fullText}) => { ... },
 *   });
 *
 * All handlers are optional. `consume` resolves when the stream
 * terminates (clean d:/e: or 3: error frame, or stream close).
 * Throws AbortError when the caller's signal aborts.
 */

(function (root) {
    const ChatStream = {};

    /**
     * Pull a one-line summary out of a tool's args dict — used by the
     * shared chip rendering so the user sees what the tool is doing
     * without having to expand the JSON detail.
     */
    ChatStream.summarizeToolArgs = function (args) {
        if (!args || typeof args !== "object") return "";
        const keys = [
            "intent", "message", "query", "pattern", "name",
            "session_id", "key", "search",
        ];
        for (const k of keys) {
            const v = args[k];
            if (typeof v === "string" && v) {
                return v.length > 90 ? v.slice(0, 87) + "…" : v;
            }
        }
        return "";
    };

    /**
     * Status glyph + class for a tool chip given the result envelope.
     * { ok | error | running } — running until a result arrives.
     */
    ChatStream.toolStatus = function (result) {
        if (!result) return { kind: "running", glyph: "…" };
        const s = (result && result.status) || null;
        if (s === "error" || s === "denied" || s === "blocked") {
            return { kind: "error", glyph: "✗" };
        }
        return { kind: "ok", glyph: "✓" };
    };

    /**
     * Consume an SSE Response body, decoding the AI SDK Data Stream
     * Protocol and dispatching to caller-supplied handlers.
     *
     * Errors during parse are swallowed (a malformed frame is dropped,
     * the next one tries). Network drops / fetch aborts propagate via
     * the AbortController signal on the original fetch.
     */
    ChatStream.consume = async function (response, handlers) {
        const h = handlers || {};
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        let fullText = "";
        let fullReasoning = "";
        let terminalEmitted = false;
        try {
            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split("\n");
                buffer = lines.pop() || "";
                for (const line of lines) {
                    if (!line || line.startsWith(":")) continue;
                    const colon = line.indexOf(":");
                    if (colon < 0) continue;
                    const prefix = line.slice(0, colon);
                    const rest = line.slice(colon + 1);
                    let payload = null;
                    try { payload = JSON.parse(rest); }
                    catch { continue; }

                    if (prefix === "0") {
                        const t = typeof payload === "string" ? payload : "";
                        if (t) {
                            fullText += t;
                            if (h.onText) h.onText(t, fullText);
                        }
                    } else if (prefix === "g") {
                        // Live reasoning delta (2026-07-23) — the host's
                        // thinking streamed while it works. Optional handler;
                        // clients without one simply don't render it.
                        const t = typeof payload === "string" ? payload : "";
                        if (t) {
                            fullReasoning += t;
                            if (h.onReasoning) h.onReasoning(t, fullReasoning);
                        }
                    } else if (prefix === "9") {
                        const toolName = payload.toolName || "tool";
                        // Host-driven app open (host-environment awareness):
                        // the host's `open_app` tool CALL is the real-time
                        // signal. Relay it to the dashboard shell (window.parent),
                        // which runs AppRunner.launch under the user's session.
                        // Same-origin target only — first-party chat surface to
                        // its first-party shell.
                        if (toolName === "open_app" && payload.args && payload.args.app_id) {
                            try {
                                const openMsg = { type: "cseries:open-app", app_id: payload.args.app_id };
                                if (typeof payload.args.view === "string" && payload.args.view) {
                                    openMsg.view = payload.args.view;
                                }
                                (window.parent || window).postMessage(
                                    openMsg, window.location.origin);
                            } catch (_) { /* no shell / detached — host still reports it */ }
                        }
                        if (h.onTool) {
                            h.onTool({
                                toolCallId: payload.toolCallId,
                                toolName: toolName,
                                args: payload.args || {},
                            });
                        }
                    } else if (prefix === "a") {
                        if (h.onResult) {
                            h.onResult({
                                toolCallId: payload.toolCallId,
                                result: payload.result,
                                status: ChatStream.toolStatus(payload.result).kind,
                            });
                        }
                    } else if (prefix === "2") {
                        // Data frames. 2:[] is the heartbeat (silent);
                        // non-empty arrays carry typed items — e.g.
                        // {type:"session", session_id} so the client can
                        // target the live tool loop (Nudge).
                        if (h.onData && Array.isArray(payload)) {
                            for (const item of payload) {
                                if (item) h.onData(item);
                            }
                        }
                    } else if (prefix === "3") {
                        terminalEmitted = true;
                        const m = typeof payload === "string" ? payload : "(error)";
                        if (h.onError) h.onError(m);
                    }
                    // f: / d: / e: — start, finish-step, finish-message.
                    // No UI dispatch; the stream itself ends.
                }
            }
        } finally {
            try { reader.releaseLock(); } catch (_) {}
        }
        if (!terminalEmitted && h.onDone) {
            h.onDone({ fullText });
        }
        return { fullText };
    };

    root.ChatStream = ChatStream;
})(window);
