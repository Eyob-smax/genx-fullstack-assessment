/**
 * Filter parsing/serialization shared by the server page, the API route and the
 * client filter bar. Pure and dependency-free (it must NOT import mockData, or the
 * 500-event dataset would be pulled into the client bundle).
 */

export const SEVERITY_FILTERS = ["all", "info", "warning", "critical"] as const;
export type SeverityFilter = (typeof SEVERITY_FILTERS)[number];

export interface EventFilters {
  search: string;
  severity: SeverityFilter;
}

export const SEARCH_DEBOUNCE_MS = 300;

export function parseSeverity(value: string | null | undefined): SeverityFilter {
  return (SEVERITY_FILTERS as readonly string[]).includes(value ?? "") ? (value as SeverityFilter) : "all";
}

type RawParams = Record<string, string | string[] | undefined>;

function first(value: string | string[] | undefined): string {
  return Array.isArray(value) ? (value[0] ?? "") : (value ?? "");
}

/** Parse filters from Next.js `searchParams` (server) or a URLSearchParams (client/API). */
export function parseFilters(params: RawParams | URLSearchParams): EventFilters {
  if (params instanceof URLSearchParams) {
    return { search: params.get("search") ?? "", severity: parseSeverity(params.get("severity")) };
  }
  return { search: first(params.search), severity: parseSeverity(first(params.severity)) };
}

/**
 * Build the query string for a filter state, preserving any unrelated params.
 * Defaults are omitted so the canonical URL for "no filters" is just `/`.
 */
export function toQueryString(filters: EventFilters, base?: URLSearchParams): string {
  const params = new URLSearchParams(base?.toString());
  if (filters.search) params.set("search", filters.search);
  else params.delete("search");
  if (filters.severity !== "all") params.set("severity", filters.severity);
  else params.delete("severity");
  return params.toString();
}
