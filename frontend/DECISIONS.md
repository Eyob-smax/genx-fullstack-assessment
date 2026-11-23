# Frontend Architecture & Debugging Decisions

## 1. Root-Cause Diagnosis

- **Stream cross-contamination:**
  `handleSelectEvent` started a `fetch` + `reader.read()` loop with no
  `AbortController`, and every chunk did `setSummaryText(prev => prev + chunk)` into
  one shared piece of state. Clicking B only reset the text to `""`. A's loop kept
  running, so its remaining chunks were appended to B's text. Nothing tied a chunk
  to the request that produced it, the stale loop also ran `setIsStreaming(false)`
  when *it* finished (flipping the indicator off while B was still streaming), and
  network or read errors were unhandled promise rejections.

- **Duplicate fetches & broken history navigation:**
  Filter state existed twice (local `useState` and the URL), and two effects synced
  them in opposite directions:
  - Effect 1 ran `router.push` on every `search`/`severity` change, including the
    initial mount, which pushed a redundant `/?` entry.
  - Effect 2 copied `searchParams` back into state.
  - Effect 3 fetched on every keystroke, with no debounce and no cancellation.

  Each keystroke created a history entry. Going Back changed `searchParams`, Effect 2
  wrote state, and Effect 1 immediately pushed again, so Back bounced forward and
  history filled with one entry per character. The fetch effect also ran on mount
  and again after the URL round-trip (and twice under StrictMode). Responses have
  150–550 ms of random jitter and nothing ignored stale ones, so an older response
  could overwrite a newer one.

- **Render cascade during streaming:**
  `summaryText` and `isStreaming` lived in the top-level page component, so each of
  the ~60 tokens re-rendered the whole page. That meant the filter bar, all 500 rows
  (not memoized, with new inline `onClick` closures each time), and
  `HeavyPayloadFormatter`, which burns ~12 ms per render and wasn't memoized: about
  60 × (12 ms + 500-row reconcile) of main-thread work while the user types or
  scrolls. Separately, the formatter rendered a timing-dependent `data-checksum`,
  which produced a hydration-mismatch warning on every load.

## 2. Server vs. Client Component Boundaries

| Component | Kind | Why |
|---|---|---|
| `app/page.tsx` | **Server** | Awaits `searchParams`, parses/validates filters, runs `queryEvents()` directly on the server (no fetch to its own API route), renders the layout shell (`<main>` grid, panes). Each filter change is an RSC navigation that re-runs it. |
| `app/layout.tsx` | **Server** | Document shell and metadata (unchanged). |
| `lib/events.ts` | server-only module | Imports the 500-event dataset. Only the page and `/api/events` import it, so it's never bundled into the client: the route's client JS is ~3 kB. |
| `lib/filters.ts` | shared, pure | Filter parsing and query-string building, used by both server and client. |
| `SelectionProvider` | Client | Holds the selected event (needs `useState`). Split into value and setter contexts. |
| `FilterBar` | Client | Controlled inputs, debounce, `useRouter`/`useSearchParams`, `useTransition`. |
| `EventList` / `EventRow` | Client | Click/keyboard selection. Rows are `memo`ized. |
| `SummaryPanel` | Client | Reads the selection and chooses what to show. |
| `SummaryStream` | Client (leaf) | Owns the stream, the `AbortController` and the streamed text. |
| `HeavyPayloadFormatter` | Client, `memo` | Only receives `rawPayload`. |

**Props crossing the boundary** (all plain, serializable JSON):
`EventList` receives `events: SystemEvent[]` (strings only: id, timestamp, service,
severity, title, rawPayload). `SelectionProvider` receives `children` (server-rendered
elements). No functions, Dates or class instances are passed from server to client.
`FilterBar` reads the URL itself through `useSearchParams`, so it needs no props.

## 3. Stream Cancellation & Race-Condition Strategy

1. **Streamed text lives only in the leaf** (`<SummaryStream key={event.id} eventId=…>`).
   The `key` gives every selection a fresh component instance with empty state, so
   text can't carry over from one selection to the next.
2. **One `AbortController` per effect run.** `useEffect(…, [eventId])` creates a
   controller, and its cleanup calls `controller.abort()`. The cleanup runs on every
   path that ends a stream: selecting another event (which unmounts via `key`), a
   filter change (the provider clears the selection, unmounting the stream),
   navigating away, and StrictMode's dev double-mount. Aborting cancels the `fetch`,
   errors out the pending `reader.read()`, and propagates to the route handler
   (`request.signal` plus `ReadableStream.cancel()`), so the server stops generating
   tokens as well.
3. **Each state write is guarded by its own run's `signal.aborted`**, checked after
   every `await`. Even a chunk already in flight when the user clicks is dropped.
   The guard uses the signal, not the `eventId`, which is what makes **A → B → A**
   safe: the first A stream, the B stream and the second A stream are three separate
   effect runs. The first two are aborted and their writes are ignored even though
   the first A has the same id as the active one. The second A starts from empty
   text and streams cleanly from its first token.
4. **No unhandled `AbortError`.** The whole read loop is a single promise with a
   `.catch` that returns silently when `signal.aborted`; any other error shows a
   "Summary failed" message instead of failing silently. `reader.releaseLock()`
   runs in `finally`.

Verified by `npm run verify`, which clicks A → B → C → A within ~900 ms. It then
asserts the panel contains exactly one report, for A, with no B/C text, that the
report is complete, and that the console shows no errors.

## 4. State Synchronization & Render Performance

- **Syncing filters with the URL without a `useEffect` loop:**
  The URL holds the *committed* filters, and the server renders the list from it.
  The inputs keep local state only so typing is instant. The two directions are
  handled differently, so they can't feed each other:
  - **Input → URL:** severity changes call `router.push` directly in `onChange`.
    Search uses a single 300 ms debounced effect that pushes only if the input
    differs from the URL (or from the navigation already in flight), so it goes
    quiet as soon as they match. Pushes are wrapped in `startTransition`: the old
    list stays visible with an "Updating…" hint, and the router keeps only the
    latest navigation, so out-of-order responses can't commit.
  - **URL → input:** handled *during render*, using React's "adjust state when a
    prop changes" pattern (`if (url !== reconciled) { setReconciled(url); … }`). If
    the new URL is one we asked for, the inputs are left alone, since the user may
    have typed further while it was in flight. Otherwise (Back/Forward) the URL
    values are copied into the inputs. This direction never writes the URL.
  - `push` is used rather than `replace`, so each committed filter state is a
    history entry: one per typing pause, not one per keystroke. `scroll: false`
    keeps the list position.
  - **Fetch counts:** **0** client data fetches on mount (the list is in the server
    HTML), exactly **1** RSC request per committed change, and no `/api/events` calls.
    `verify` checks each of these, plus Back/Forward restoring both inputs and no
    stray pushes afterwards.

- **Verifying token updates don't re-render the event table:**
  - **Built-in counters:** each component (filter bar, event list, payload
    formatter, summary) shows a render counter. It counts *commits* in an effect and
    writes the number into a `<span>` through a ref, so the counter itself never
    triggers a render. While a full summary streams, the summary counter rises by
    ~60 and the list, filter bar and formatter counters stay the same. `npm run verify`
    asserts exactly this (sample: `list 6→6, filter bar 1→1, formatter 6→6, summary 1→61`).
  - **React DevTools Profiler:** while streaming, the only committed component is
    `SummaryStream`.
  - **Why it holds:** the streamed state's owner has no children other than its own
    text. `SummaryPanel` and `EventList` subscribe only to the selected-event
    context, which changes on click, not per token. `FilterBar` subscribes to no
    selection state. `onSelect` is a stable `setState`, so memoized rows only
    re-render when their `selected` flag flips (two rows per click).
