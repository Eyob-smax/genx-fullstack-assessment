"use client";

import React, { memo, useEffect, useState, useTransition } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import {
  SEARCH_DEBOUNCE_MS,
  SEVERITY_FILTERS,
  parseFilters,
  toQueryString,
  type EventFilters,
  type SeverityFilter,
} from "@/lib/filters";
import { counterStyle, useCommitCounter } from "@/lib/useCommitCounter";

const inputStyle: React.CSSProperties = {
  padding: "8px 12px",
  borderRadius: "6px",
  border: "1px solid #334155",
  background: "#1e293b",
  color: "#f8fafc",
};

function sameFilters(a: EventFilters, b: EventFilters) {
  return a.search === b.search && a.severity === b.severity;
}

/**
 * The URL is the single source of truth for *committed* filters. The inputs keep
 * local state only so typing stays instant while a navigation is debounced or in
 * flight.
 *
 * Data flow (no feedback loop):
 *   input -> URL : one debounced effect (search) / the onChange handler (severity),
 *                  each a single router.push inside startTransition.
 *   URL -> input : adopted *during render* when the URL changes for a reason we
 *                  didn't initiate (Back/Forward, links). It never writes the URL.
 * The server page re-renders from the new searchParams. There is no client-side
 * fetch, so there is nothing to duplicate or race.
 */
export const FilterBar = memo(function FilterBar() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const url = parseFilters(searchParams);

  const [isPending, startTransition] = useTransition();
  const [searchInput, setSearchInput] = useState(url.search);
  const [severityInput, setSeverityInput] = useState<SeverityFilter>(url.severity);
  // The filters we've asked the router for but which may not have committed yet.
  const [requested, setRequested] = useState<EventFilters | null>(null);
  // The URL state the inputs were last reconciled with.
  const [reconciled, setReconciled] = useState<EventFilters>(url);

  if (!sameFilters(url, reconciled)) {
    setReconciled(url);
    const isOurCommit = requested !== null && sameFilters(url, requested);
    if (!isOurCommit) {
      // External navigation (Back/Forward): mirror the URL into the inputs.
      setSearchInput(url.search);
      setSeverityInput(url.severity);
    }
    // Our own commit: keep the inputs, the user may already have typed further.
    setRequested(null);
  }

  function navigate(next: EventFilters) {
    setRequested(next);
    const qs = toQueryString(next, new URLSearchParams(searchParams.toString()));
    startTransition(() => {
      // push (not replace): each committed filter state is a history entry, so
      // Back/Forward walk through them. scroll:false keeps the list position.
      router.push(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
    });
  }

  // Debounced search commit. Compares against what the URL is (or is about to be),
  // so it only pushes on real divergence and goes quiet once they match.
  const target = requested ?? url;
  useEffect(() => {
    if (searchInput === target.search) return;
    const timer = setTimeout(
      () => navigate({ search: searchInput, severity: severityInput }),
      SEARCH_DEBOUNCE_MS,
    );
    return () => clearTimeout(timer);
    // navigate is recreated each render; the values it reads are listed below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchInput, severityInput, target.search]);

  function onSeverityChange(value: SeverityFilter) {
    setSeverityInput(value);
    // Discrete change: commit immediately, together with whatever is typed in search,
    // and supersede any pending debounced search commit.
    navigate({ search: searchInput, severity: value });
  }

  const counterRef = useCommitCounter("Filter bar");

  return (
    <div
      style={{
        padding: "16px",
        borderBottom: "1px solid #1e293b",
        display: "flex",
        gap: "12px",
        alignItems: "center",
      }}
    >
      <input
        type="search"
        aria-label="Filter events"
        value={searchInput}
        onChange={(e) => setSearchInput(e.target.value)}
        placeholder="Filter by service, title, or ID..."
        style={{ ...inputStyle, flex: 1 }}
      />
      <select
        aria-label="Severity"
        value={severityInput}
        onChange={(e) => onSeverityChange(e.target.value as SeverityFilter)}
        style={inputStyle}
      >
        {SEVERITY_FILTERS.map((s) => (
          <option key={s} value={s}>
            {s === "all" ? "All Severities" : s[0].toUpperCase() + s.slice(1)}
          </option>
        ))}
      </select>
      <span aria-live="polite" style={{ fontSize: "12px", color: "#38bdf8", minWidth: "60px" }}>
        {isPending ? "Updating…" : ""}
      </span>
      <span ref={counterRef} data-testid="filterbar-renders" style={counterStyle} />
    </div>
  );
});
