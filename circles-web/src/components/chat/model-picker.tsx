import { ChevronsUpDownIcon, StarIcon } from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import {
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import type { CatalogModel, ModelPick } from "@/lib/types";

// Sentinel value meaning "use the host's routed default model".
export const DEFAULT_VALUE = "__default__";

// Encode/decode a CatalogModel <-> the picker's string value so a pick round-trips.
export function encodePick(m: CatalogModel): string {
  return `${m.provider}::${m.id}`;
}
export function decodePick(value: string): ModelPick | null {
  if (value === DEFAULT_VALUE) return null;
  const i = value.indexOf("::");
  if (i < 0) return null;
  const provider = value.slice(0, i);
  const model_id = value.slice(i + 2);
  if (!model_id) return null;
  return { provider, model_id };
}

function labelFor(
  value: string,
  models: CatalogModel[],
  placeholder: string,
): string {
  if (value === DEFAULT_VALUE) return placeholder;
  return models.find((m) => encodePick(m) === value)?.name ?? value;
}

function fmtUsd(n: number): string {
  if (n === 0) return "free";
  if (n < 1) return `$${n.toFixed(2).replace(/0+$/, "").replace(/\.$/, "")}`;
  return `$${Math.round(n * 100) / 100}`;
}
function fmtCtx(n: number): string {
  if (n >= 1_000_000) return `${Math.round(n / 100_000) / 10}M`;
  if (n >= 1000) return `${Math.round(n / 1000)}K`;
  return String(n);
}
// The price/context line under each model — the thing that was missing, so you
// can actually judge what to pick or favorite. Local models are free.
function priceLabel(m: CatalogModel): string {
  const parts: string[] = [];
  if (m.provider === "local") {
    parts.push("local · free");
  } else if (m.inputPerM != null && m.outputPerM != null) {
    parts.push(
      m.inputPerM === 0 && m.outputPerM === 0
        ? "free"
        : `${fmtUsd(m.inputPerM)}/M in · ${fmtUsd(m.outputPerM)}/M out`,
    );
  }
  if (m.contextLength) parts.push(`${fmtCtx(m.contextLength)} ctx`);
  return parts.join(" · ") || "price n/a";
}

function ModelRow({ m, fav }: { m: CatalogModel; fav?: boolean }) {
  return (
    <>
      {fav && <StarIcon className="size-3.5 shrink-0 text-amber-500" />}
      <div className="flex min-w-0 flex-col">
        <span className="truncate">{m.name}</span>
        <span className="text-muted-foreground text-xs">{priceLabel(m)}</span>
      </div>
    </>
  );
}

// A searchable model picker. Click the button → a search dialog opens with the
// host's favorites surfaced first, then the full catalog. Type to filter by name
// or id — the catalog is hundreds of models, so search is the point.
export function ModelPicker({
  models,
  favoriteIds,
  value,
  onChange,
  allowDefault = true,
  placeholder = "Default model",
  size = "sm",
  className,
}: {
  models: CatalogModel[];
  favoriteIds: string[];
  value: string;
  onChange: (v: string) => void;
  allowDefault?: boolean;
  placeholder?: string;
  size?: "xs" | "sm";
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  const favSet = new Set(favoriteIds);
  const favorites = models.filter((m) => favSet.has(m.id));

  const pick = (v: string) => {
    onChange(v);
    setOpen(false);
  };

  return (
    <>
      <Button
        className={className ?? "max-w-56 justify-between gap-2"}
        onClick={() => setOpen(true)}
        size={size}
        type="button"
        variant="outline"
      >
        <span className="truncate">{labelFor(value, models, placeholder)}</span>
        <ChevronsUpDownIcon className="size-3.5 shrink-0 opacity-50" />
      </Button>
      <CommandDialog
        description="Search the model catalog and pick one."
        onOpenChange={setOpen}
        open={open}
        title="Choose a model"
      >
        <CommandInput placeholder="Search models…" />
        <CommandList>
          <CommandEmpty>No models found.</CommandEmpty>
          {allowDefault && (
            <CommandGroup heading="Default">
              <CommandItem
                onSelect={() => pick(DEFAULT_VALUE)}
                value="default host routed model"
              >
                {placeholder}
              </CommandItem>
            </CommandGroup>
          )}
          {favorites.length > 0 && (
            <CommandGroup heading="Favorites">
              {favorites.map((m) => (
                <CommandItem
                  key={`fav-${encodePick(m)}`}
                  onSelect={() => pick(encodePick(m))}
                  value={`favorite ${m.name} ${m.id}`}
                >
                  <ModelRow fav m={m} />
                </CommandItem>
              ))}
            </CommandGroup>
          )}
          <CommandGroup heading="All models">
            {models.map((m) => (
              <CommandItem
                key={encodePick(m)}
                onSelect={() => pick(encodePick(m))}
                value={`${m.name} ${m.id}`}
              >
                <ModelRow m={m} />
              </CommandItem>
            ))}
          </CommandGroup>
        </CommandList>
      </CommandDialog>
    </>
  );
}
