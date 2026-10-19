import { defaultLabel, hasChoices, type EffortOptions } from "@/lib/effort";

// The reasoning-effort pick beside the model picker: a plain select, so the
// coming composer redesign can move or restyle it without touching the logic
// (hooks/use-effort-pick.ts, lib/effort.ts). Renders nothing for a model that
// takes no effort.
export function EffortPicker({
  options,
  value,
  onChange,
}: {
  options: EffortOptions | null;
  value: string;
  onChange: (v: string) => void;
}) {
  if (!hasChoices(options)) return null;
  return (
    <label
      className="flex items-center gap-1 text-muted-foreground text-xs"
      title="How hard the Host thinks on this thread's turns. Default is the model's own level."
    >
      Effort
      <select
        aria-label="Reasoning effort"
        className="h-8 rounded-md border border-input bg-background px-2 text-foreground text-xs"
        onChange={(e) => onChange(e.target.value)}
        value={value}
      >
        <option value="">{defaultLabel(options)}</option>
        {options?.efforts.map((lv) => (
          <option key={lv} value={lv}>
            {lv}
          </option>
        ))}
      </select>
    </label>
  );
}
