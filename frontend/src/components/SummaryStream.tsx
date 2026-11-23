"use client";

import React, { useEffect, useState } from "react";
import { counterStyle, useCommitCounter } from "@/lib/useCommitCounter";

type Status = "connecting" | "streaming" | "done" | "error";

/**
 * Leaf component that owns the streamed text. It is the ONLY component whose state
 * changes per token, so each chunk re-renders just this subtree.
 *
 * Cancellation model:
 *  - The parent renders it with `key={eventId}`, so every selection gets a fresh
 *    instance with empty state. Text can't carry over between events.
 *  - Each effect run owns its own AbortController. Cleanup (selection change,
 *    filter change, unmount, StrictMode re-run) aborts that run's fetch/reader, and
 *    every state update is guarded by *that run's* `signal.aborted`. A stale stream
 *    therefore can never write, even for A -> B -> A, where the stale run and the
 *    new run share the same eventId.
 *  - AbortError is expected and swallowed; only real failures surface as an error.
 */
export function SummaryStream({ eventId }: { eventId: string }) {
  const [text, setText] = useState("");
  const [status, setStatus] = useState<Status>("connecting");
  const counterRef = useCommitCounter("Summary");

  useEffect(() => {
    const controller = new AbortController();
    const { signal } = controller;

    async function run() {
      const res = await fetch(`/api/stream-summary?eventId=${encodeURIComponent(eventId)}`, {
        signal,
        cache: "no-store",
      });
      if (!res.ok || !res.body) throw new Error(`Summary request failed (${res.status})`);
      if (signal.aborted) return;
      setStatus("streaming");

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      try {
        while (true) {
          const { done, value } = await reader.read();
          if (signal.aborted) return;
          if (done) break;
          const chunk = decoder.decode(value, { stream: true });
          if (chunk) setText((prev) => prev + chunk);
        }
        const tail = decoder.decode();
        if (tail) setText((prev) => prev + tail);
        setStatus("done");
      } finally {
        reader.releaseLock();
      }
    }

    run().catch((err: unknown) => {
      if (signal.aborted) return; // expected: the user moved on
      console.error(err);
      setStatus("error");
    });

    return () => controller.abort();
  }, [eventId]);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "6px" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", minHeight: "16px" }}>
        <span data-testid="stream-status" data-status={status} style={{ fontSize: "12px", color: "#38bdf8" }}>
          {status === "connecting" && "Generating…"}
          {status === "streaming" && "Streaming tokens..."}
          {status === "error" && <span style={{ color: "#f87171" }}>Summary failed. Reselect to retry.</span>}
        </span>
        <span ref={counterRef} data-testid="summary-renders" style={counterStyle} />
      </div>
      <div
        data-testid="summary-text"
        data-event-id={eventId}
        aria-live="polite"
        aria-busy={status === "connecting" || status === "streaming"}
        style={{
          background: "#1e293b",
          padding: "14px",
          borderRadius: "8px",
          border: "1px solid #334155",
          whiteSpace: "pre-wrap",
          lineHeight: 1.55,
          minHeight: "180px",
        }}
      >
        {text}
      </div>
    </div>
  );
}
