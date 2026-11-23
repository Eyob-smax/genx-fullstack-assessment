"use client";

import React from "react";
import { useSelectedEvent } from "@/components/SelectionContext";
import { SummaryStream } from "@/components/SummaryStream";
import { HeavyPayloadFormatter } from "@/components/HeavyPayloadFormatter";

/**
 * Right-hand drawer. Re-renders only when the selection changes; streamed tokens
 * stay inside <SummaryStream>.
 */
export function SummaryPanel() {
  const selected = useSelectedEvent();

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "12px" }}>
      <h2 style={{ margin: 0, fontSize: "18px" }}>
        {selected ? `Analysis: ${selected.id}` : "Incident Summary"}
      </h2>

      {selected ? (
        // key => a fresh stream instance (and fresh state) per selected event
        <SummaryStream key={selected.id} eventId={selected.id} />
      ) : (
        <div
          style={{
            background: "#1e293b",
            padding: "14px",
            borderRadius: "8px",
            border: "1px solid #334155",
            minHeight: "180px",
            color: "#94a3b8",
          }}
        >
          Select an event to generate an AI incident analysis...
        </div>
      )}

      <HeavyPayloadFormatter rawPayload={selected?.rawPayload ?? "{}"} />
    </div>
  );
}
