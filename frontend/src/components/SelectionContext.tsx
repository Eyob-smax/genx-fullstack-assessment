"use client";

import React, { createContext, useContext, useState } from "react";
import { useSearchParams } from "next/navigation";
import type { SystemEvent } from "@/lib/mockData";

/**
 * Selection state, split into two contexts:
 *  - SelectedEventContext: the value (changes on click) -> only the list highlight
 *    and the summary panel subscribe.
 *  - SelectEventContext: the setter (referentially stable forever) -> consumers that
 *    only need to *change* the selection never re-render because of it.
 *
 * Streamed summary text is deliberately NOT stored here; it lives in the
 * <SummaryStream> leaf so token updates can't propagate upward.
 */
const SelectedEventContext = createContext<SystemEvent | null>(null);
const SelectEventContext = createContext<(evt: SystemEvent | null) => void>(() => {});

export function SelectionProvider({ children }: { children: React.ReactNode }) {
  const searchParams = useSearchParams();
  const filterKey = `${searchParams.get("search") ?? ""}\u0000${searchParams.get("severity") ?? "all"}`;

  const [selected, setSelected] = useState<SystemEvent | null>(null);
  const [selectionFilterKey, setSelectionFilterKey] = useState(filterKey);

  // When the committed filters change, drop the selection. This unmounts the active
  // <SummaryStream>, whose effect cleanup aborts the in-flight stream. Done during
  // render ("adjusting state when a prop changes"), not in an effect, so there is no
  // extra commit showing a stale selection.
  if (filterKey !== selectionFilterKey) {
    setSelectionFilterKey(filterKey);
    setSelected(null);
  }

  return (
    <SelectEventContext.Provider value={setSelected}>
      <SelectedEventContext.Provider value={selected}>{children}</SelectedEventContext.Provider>
    </SelectEventContext.Provider>
  );
}

export function useSelectedEvent() {
  return useContext(SelectedEventContext);
}

export function useSelectEvent() {
  return useContext(SelectEventContext);
}
