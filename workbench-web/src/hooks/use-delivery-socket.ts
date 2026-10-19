import { useEffect, useRef } from "react";

import { onSessionBack, sessionEnded } from "@/lib/session";

/**
 * Live delivery for the Workbench (2026-08-19). A finished background job —
 * a dispatched agent or a pool build — is written into the thread as an
 * assistant turn AND pushed over the kernel's existing `/ws` as a
 * `chat_message` (data.agent set) plus a `notification` frame (the toast).
 * Circles has subscribed to that socket since 2026-07; the Workbench never
 * did — it learned of a delivery only from the bench's 3 s poll, which stops
 * while the tab is hidden, so a build could finish and nothing in the UI
 * moved ("says completed, doesn't follow up ever"). This hook is the same
 * subscription: on a delivery push it fires `onDelivered` (the chat pane
 * refetches its thread — server is the source of truth, no synthetic
 * bubble); on a notification it fires `onNotice` for the in-app notice
 * stack. Reconnects on close; refires once on (re)open in case a delivery
 * landed while the socket was down.
 *
 * NOT WHILE THE HOST SESSION HAS ENDED (2026-10-07). The reconnect used to
 * run every 5 s for as long as the Workbench stayed open: about 720 refused
 * attempts an hour from a desktop whose session had lapsed. On a close it now
 * asks whether the session ended (lib/session.ts, through the desktop's
 * keeper, which answers at once when it already knows), and if so waits for
 * the session to be back (onSessionBack: only the desktop's own
 * announcement, or a tab-wake that finds it alive) instead of a timer.
 */
export type DeliveryNotice = {
  title?: string;
  message: string;
  level: "success" | "error" | "info";
  task_id?: number | string | null;
};

export function useDeliverySocket(
  onDelivered: (info: {
    projectId: number | null;
    taskId?: number | string | null;
    /** entity_memory id of the delivery turn the kernel just wrote. Null on
     * a socket reopen (nothing specific to wait for) or an older kernel. */
    rowId?: number | null;
    /** The frame's ORIGIN surface (`module`) — a Circles-origin delivery is
     * not the Workbench's to wait for. Null on a socket reopen. */
    appId?: string | null;
  }) => void,
  onNotice: (n: DeliveryNotice) => void,
) {
  const deliveredRef = useRef(onDelivered);
  deliveredRef.current = onDelivered;
  const noticeRef = useRef(onNotice);
  noticeRef.current = onNotice;

  useEffect(() => {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    let ws: WebSocket | null = null;
    let retry: ReturnType<typeof setTimeout> | null = null;
    let unwait: (() => void) | null = null;
    let alive = true;

    const onMessage = (ev: MessageEvent) => {
      let msg: {
        type?: string;
        /** The delivery's ORIGIN surface (session.py: origin.app_id, falling
         * back to "circles"). Not every frame is the Workbench's. */
        module?: string | null;
        data?: {
          project_id?: number | null;
          turn_row_id?: number | null;
          agent?: { task_id?: number | string | null; status?: string };
          title?: string;
          message?: string;
          level?: string;
          task_id?: number | string | null;
        };
      } | null = null;
      try {
        msg = JSON.parse(ev.data as string);
      } catch {
        return;
      }
      if (!msg) return;
      if (msg.type === "chat_message" && msg.data?.agent) {
        deliveredRef.current({
          projectId: msg.data.project_id ?? null,
          taskId: msg.data.agent.task_id ?? null,
          rowId: typeof msg.data.turn_row_id === "number" ? msg.data.turn_row_id : null,
          appId: typeof msg.module === "string" ? msg.module : null,
        });
        return;
      }
      if (msg.type === "notification" && msg.data?.message) {
        const lvl = msg.data.level === "error" ? "error" : msg.data.level === "success" ? "success" : "info";
        noticeRef.current({
          title: msg.data.title,
          message: msg.data.message,
          level: lvl,
          task_id: msg.data.task_id ?? null,
        });
      }
    };

    const connect = () => {
      if (!alive) return;
      try {
        ws = new WebSocket(`${proto}//${location.host}/ws`);
      } catch {
        retry = setTimeout(connect, 5000);
        return;
      }
      ws.addEventListener("message", onMessage);
      ws.addEventListener("open", () => {
        // A delivery may have landed while the socket was down.
        if (alive) deliveredRef.current({ projectId: null, taskId: null });
      });
      ws.addEventListener("close", () => {
        if (!alive) return;
        void sessionEnded().then((ended) => {
          if (!alive) return;
          if (!ended) {
            retry = setTimeout(connect, 5000);
          } else if (!unwait) {
            unwait = onSessionBack(() => {
              unwait = null;
              connect();
            });
          }
        });
      });
    };
    connect();
    return () => {
      alive = false;
      if (retry) clearTimeout(retry);
      if (unwait) unwait();
      try {
        ws?.removeEventListener("message", onMessage);
        ws?.close();
      } catch {
        /* noop */
      }
    };
  }, []);
}
