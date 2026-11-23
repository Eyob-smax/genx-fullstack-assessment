/**
 * Server Component (no 'use client').
 *
 * Reads the filters from the URL, loads the matching events on the server and
 * renders the static shell. Interactivity lives in small client islands:
 *   <FilterBar>      URL-driven filters (debounced search, history-friendly)
 *   <EventList>      clickable rows (memoized)
 *   <SummaryPanel>   selected event + <SummaryStream> leaf (owns streamed tokens)
 * <SelectionProvider> shares the selected event between the list and the panel.
 *
 * A filter change is a client-side navigation (router.push). Next.js re-runs this
 * component with the new searchParams and streams back only the RSC payload, while
 * client state (input focus, selection provider) is preserved. Out-of-order
 * responses are handled by the router: only the latest navigation commits.
 */
import { parseFilters } from "@/lib/filters";
import { queryEvents } from "@/lib/events";
import { SelectionProvider } from "@/components/SelectionContext";
import { FilterBar } from "@/components/FilterBar";
import { EventList } from "@/components/EventList";
import { SummaryPanel } from "@/components/SummaryPanel";

type PageProps = {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
};

export default async function DashboardPage({ searchParams }: PageProps) {
  const filters = parseFilters(await searchParams);
  const events = queryEvents(filters);

  return (
    <SelectionProvider>
      <main style={{ display: "grid", gridTemplateColumns: "1.2fr 1fr", height: "100vh", overflow: "hidden" }}>
        {/* Left Pane: Filters & Event Feed */}
        <section
          aria-label="Event log"
          style={{ borderRight: "1px solid #1e293b", display: "flex", flexDirection: "column", overflow: "hidden" }}
        >
          <FilterBar />
          <EventList events={events} />
        </section>

        {/* Right Pane: Streaming AI Incident Drawer */}
        <aside aria-label="Incident summary" style={{ padding: "20px", overflowY: "auto" }}>
          <SummaryPanel />
        </aside>
      </main>
    </SelectionProvider>
  );
}
