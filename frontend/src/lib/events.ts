/**
 * Server-side data access for events. Imported by the Server Component page and by
 * the /api/events route so both share one filtering implementation.
 * Do not import this from a 'use client' module.
 */
import { ALL_EVENTS, type SystemEvent } from "@/lib/mockData";
import type { EventFilters } from "@/lib/filters";

export function queryEvents({ search, severity }: EventFilters): SystemEvent[] {
  const needle = search.toLowerCase().trim();
  return ALL_EVENTS.filter((evt) => {
    const matchesSeverity = severity === "all" || evt.severity === severity;
    const matchesSearch =
      !needle ||
      evt.title.toLowerCase().includes(needle) ||
      evt.service.toLowerCase().includes(needle) ||
      evt.id.toLowerCase().includes(needle);
    return matchesSeverity && matchesSearch;
  });
}
