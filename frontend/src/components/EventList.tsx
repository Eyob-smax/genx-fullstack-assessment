"use client";

import React, { memo } from "react";
import type { SystemEvent } from "@/lib/mockData";
import { useSelectEvent, useSelectedEvent } from "@/components/SelectionContext";
import { counterStyle, useCommitCounter } from "@/lib/useCommitCounter";

const severityColor: Record<SystemEvent["severity"], string> = {
  critical: "#7f1d1d",
  warning: "#78350f",
  info: "#1e3a8a",
};

/**
 * Re-renders only when the server sends a new `events` array (filter change) or the
 * selected id changes. It never sees summary text. Rows are memoized, so a selection
 * change re-renders just the two rows whose `selected` flag flipped.
 */
export function EventList({ events }: { events: SystemEvent[] }) {
  const selectedId = useSelectedEvent()?.id ?? null;
  const onSelect = useSelectEvent(); // stable setState identity
  const counterRef = useCommitCounter("Event list");

  return (
    <>
      <div
        style={{
          padding: "6px 16px",
          borderBottom: "1px solid #1e293b",
          display: "flex",
          justifyContent: "space-between",
          fontSize: "12px",
          color: "#64748b",
        }}
      >
        <span>{events.length} events</span>
        <span ref={counterRef} data-testid="eventlist-renders" style={counterStyle} />
      </div>
      <div role="listbox" aria-label="Events" style={{ overflowY: "auto", flex: 1 }}>
        {events.length === 0 && <div style={{ padding: "12px", color: "#94a3b8" }}>No events match.</div>}
        {events.map((evt) => (
          <EventRow key={evt.id} event={evt} selected={evt.id === selectedId} onSelect={onSelect} />
        ))}
      </div>
    </>
  );
}

const EventRow = memo(function EventRow({
  event,
  selected,
  onSelect,
}: {
  event: SystemEvent;
  selected: boolean;
  onSelect: (evt: SystemEvent) => void;
}) {
  return (
    <div
      role="option"
      aria-selected={selected}
      tabIndex={0}
      data-event-id={event.id}
      onClick={() => onSelect(event)}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onSelect(event);
        }
      }}
      style={{
        padding: "12px 16px",
        borderBottom: "1px solid #1e293b",
        cursor: "pointer",
        background: selected ? "#1e293b" : "transparent",
        display: "flex",
        justifyContent: "space-between",
        alignItems: "center",
      }}
    >
      <div>
        <div style={{ fontSize: "14px", fontWeight: 500 }}>{event.title}</div>
        <div style={{ fontSize: "12px", color: "#64748b" }}>
          {event.id} · {event.timestamp}
        </div>
      </div>
      <span
        style={{
          fontSize: "11px",
          padding: "2px 8px",
          borderRadius: "999px",
          background: severityColor[event.severity],
          color: "#f8fafc",
        }}
      >
        {event.severity}
      </span>
    </div>
  );
});
