import { useEffect, useState } from "react";
import { fetchEffortOptions } from "@/lib/api";
import type { EffortOptions } from "@/lib/effort";

/** The reasoning efforts the Host model takes, and its default, read
 * from the server when the project on screen changes. null until it answers,
 * or when it cannot: the pick control then stays hidden and the send route
 * still checks any pick it is sent. */
export function useEffortOptions(projectId: number | null): EffortOptions | null {
  const [options, setOptions] = useState<EffortOptions | null>(null);
  useEffect(() => {
    let cancelled = false;
    void fetchEffortOptions().then((r) => {
      if (!cancelled) setOptions(r.ok ? r.data : null);
    });
    return () => {
      cancelled = true;
    };
  }, [projectId]);
  return options;
}
