import { useEffect, useState } from "react";
import { fetchEffortOptions } from "@/lib/api";
import { keepPick, type EffortOptions } from "@/lib/effort";
import type { ModelPick } from "@/lib/types";

/** The per-thread reasoning-effort pick, held beside the model pick
 * and sent with each turn the same way: never stored, reset when the thread
 * changes, and dropped back to the default when the model changes to one that
 * does not take it. The options come from the server for the model the turn
 * runs on (the pick, else the Host model route). `enabled` false (a thread
 * with no Host model, e.g. a person) asks the server nothing. */
export function useEffortPick(model: ModelPick | null, threadKey: string, enabled: boolean) {
  const [options, setOptions] = useState<EffortOptions | null>(null);
  const [effort, setEffort] = useState<string>("");

  useEffect(() => {
    setEffort("");
  }, [threadKey]);

  const provider = model?.provider ?? "";
  const modelId = model?.model_id ?? "";
  useEffect(() => {
    if (!enabled) {
      setOptions(null);
      return;
    }
    let cancelled = false;
    void fetchEffortOptions(provider && modelId ? { provider, model_id: modelId } : null).then((r) => {
      if (cancelled) return;
      const next = r.ok ? r.data : null;
      setOptions(next);
      setEffort((cur) => keepPick(cur, next));
    });
    return () => {
      cancelled = true;
    };
  }, [enabled, provider, modelId]);

  return { options, effort, setEffort };
}
