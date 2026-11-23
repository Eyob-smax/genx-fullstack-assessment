"use client";

import React, { memo } from "react";
import { counterStyle, useCommitCounter } from "@/lib/useCommitCounter";

/**
 * Simulates an expensive markdown / syntax highlighting component.
 *
 * It no longer receives `summaryText`: it only depends on `rawPayload`, and memo()
 * skips it entirely unless that string changes (i.e. on selection change, never per
 * streamed token).
 */
export const HeavyPayloadFormatter = memo(function HeavyPayloadFormatter({ rawPayload }: { rawPayload: string }) {
  // Simulate ~12ms of synchronous formatting work per render.
  // (The starter rendered this loop's counter as `data-checksum`. That value depends
  // on timing, so server and client HTML never matched -> hydration warning. It is
  // no longer rendered.)
  const start = performance.now();
  while (performance.now() - start < 12) {
    // busy-wait
  }

  const counterRef = useCommitCounter("Payload formatter");

  return (
    <details open>
      <summary style={{ cursor: "pointer", color: "#94a3b8", fontSize: "13px" }}>
        Raw Telemetry Payload <span ref={counterRef} data-testid="formatter-renders" style={counterStyle} />
      </summary>
      <pre
        style={{
          background: "#020617",
          padding: "12px",
          borderRadius: "6px",
          fontSize: "12px",
          overflowX: "auto",
        }}
      >
        {rawPayload}
      </pre>
    </details>
  );
});
