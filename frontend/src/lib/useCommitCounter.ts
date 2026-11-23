"use client";

import { useEffect, useRef } from "react";

/**
 * Built-in render counter: counts *commits* of the calling component and writes the
 * number straight into a <span> via a ref. Updating the DOM directly (instead of via
 * state) means the counter itself never causes an extra render.
 *
 * Note: in `next dev` React StrictMode double-invokes effects on mount, so each
 * counter starts at 2 in development. What matters is whether it moves while a
 * summary is streaming.
 */
export function useCommitCounter(label: string) {
  const count = useRef(0);
  const el = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    count.current += 1;
    if (el.current) el.current.textContent = `${label} renders: ${count.current}`;
  });
  return el;
}

export const counterStyle: React.CSSProperties = {
  fontSize: "11px",
  color: "#64748b",
  fontVariantNumeric: "tabular-nums",
  whiteSpace: "nowrap",
};
