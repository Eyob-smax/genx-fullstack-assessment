# Frontend Technical Assessment — Event Log & Live AI Summary Workspace

## Context
You are taking over an internal operations dashboard built with **Next.js (App Router)**, **React**, and **TypeScript**. The workspace displays a filterable list of 500 system events on the left and a live streaming AI incident-summary drawer on the right.

Users are reporting three major issues in production:
1. **Stream Cross-Contamination:** Clicking Event A starts a streaming summary; clicking Event B before Event A finishes causes leftover chunks from Event A to bleed into Event B's summary panel.
2. **Sluggish Typing & Broken Back Button:** Typing in the search box or toggling severity filters triggers duplicate API calls and breaks browser Back/Forward navigation.
3. **UI Jank During Streaming:** While a summary is streaming tokens, typing or scrolling feels sluggish because the entire 500-row table and heavy syntax formatter re-render on every single streamed chunk.

## Setup
```bash
npm install
npm run dev
```
Open `http://localhost:3000` in your browser.

## Acceptance Criteria
1. **Zero Stream Bleed:** Rapidly switching between events (or changing filters while a stream is active) must cleanly abort in-flight streams and never append stale chunks to the newly selected event.
2. **URL-Driven Filter State:** `search` and `severity` filters must stay synchronized with URL query parameters (`?search=...&severity=...`) with **no duplicate fetches** on mount/change, proper debouncing on search input, and working browser Back/Forward navigation.
3. **Render Isolation:** Streaming token updates must **only** re-render the leaf summary output component—never the 500-row event list or filter bar (verifiable via React DevTools Profiler or the built-in render counters).
4. **Server/Client Boundary Hygiene:** Move initial data loading and shell layout to React Server Components where appropriate instead of making the entire page `'use client'`.

## Constraints
- **No external state or data-fetching libraries:** Do not install Zustand, Redux, Jotai, TanStack Query, or SWR. Solve state and cancellation using native React and Next.js App Router primitives.
- Complete `DECISIONS.md` before submitting.

---

## Solution overview

See **[DECISIONS.md](DECISIONS.md)** for the diagnosis and design rationale.

```
src/app/page.tsx                    Server Component: parses ?search/&severity, queries events, renders shell
src/lib/filters.ts                  shared pure filter parsing / query-string building
src/lib/events.ts                   server-only event query (shared with /api/events)
src/components/SelectionContext.tsx selected-event value + stable setter (split contexts)
src/components/FilterBar.tsx        URL-driven filters: debounced search, router.push in a transition
src/components/EventList.tsx        memoized rows
src/components/SummaryPanel.tsx     selection → <SummaryStream key=id> + memoized payload formatter
src/components/SummaryStream.tsx    leaf that owns the stream, AbortController per effect run
src/lib/useCommitCounter.ts         built-in render counters (DOM-only, never cause renders)
scripts/verify.cjs                  Playwright check of all four acceptance criteria
```

### Verifying

```bash
npm run build && npm start          # or npm run dev
# in another terminal:
npx playwright install chromium     # once (or set CHROME_PATH=/path/to/chrome)
npm run verify                      # BASE=http://localhost:3000 by default
```

The render counters are visible in the UI. Select an event and watch them: only the
"Summary renders" counter moves while tokens stream.
