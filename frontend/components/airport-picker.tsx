"use client";

import { MapPin, Plus, X } from "lucide-react";
import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { useAirports } from "@/hooks/queries";

/** Multi-select airport combobox backed by /api/airports search. */
export function AirportPicker({
  value,
  onChange,
  max = 5,
  placeholder = "Search city or IATA code (e.g. SFO)",
  id,
}: {
  value: string[];
  onChange: (codes: string[]) => void;
  max?: number;
  placeholder?: string;
  id?: string;
}) {
  const [q, setQ] = useState("");
  const { data: results = [], isFetching } = useAirports(q);
  const options = results.filter((a) => !value.includes(a.iata));

  function add(code: string) {
    if (value.length >= max) return;
    onChange([...value, code]);
    setQ("");
  }

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-2">
        {value.map((code) => (
          <Badge key={code} variant="secondary" className="gap-1.5 py-1 pr-1 pl-2.5 text-sm">
            <MapPin className="size-3.5" /> {code}
            <button
              type="button"
              className="rounded-full p-0.5 hover:bg-black/10"
              onClick={() => onChange(value.filter((c) => c !== code))}
              aria-label={`Remove ${code}`}
            >
              <X className="size-3" />
            </button>
          </Badge>
        ))}
      </div>
      {value.length < max && (
        <div className="relative">
          <Input
            id={id}
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder={placeholder}
            autoComplete="off"
            role="combobox"
            aria-expanded={q.trim().length >= 2}
            aria-controls={id ? `${id}-options` : undefined}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                const exact = options.find((a) => a.iata === q.trim().toUpperCase()) ?? options[0];
                if (exact) add(exact.iata);
              }
            }}
          />
          {q.trim().length >= 2 && (
            <ul
              id={id ? `${id}-options` : undefined}
              role="listbox"
              className="absolute z-30 mt-1 max-h-64 w-full overflow-auto rounded-xl border bg-popover p-1 shadow-lg"
            >
              {options.length === 0 && (
                <li className="px-3 py-2 text-sm text-muted-foreground">{isFetching ? "Searching…" : "No airports found"}</li>
              )}
              {options.map((a) => (
                <li key={a.iata} role="option" aria-selected={false}>
                  <button
                    type="button"
                    onClick={() => add(a.iata)}
                    className="flex w-full items-center gap-3 rounded-md px-3 py-2 text-left text-sm hover:bg-accent"
                  >
                    <span className="w-10 font-mono font-semibold text-ocean">{a.iata}</span>
                    <span className="min-w-0 flex-1 truncate">
                      {a.city} · <span className="text-muted-foreground">{a.name}</span>
                    </span>
                    <Plus className="size-3.5 text-muted-foreground" />
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
