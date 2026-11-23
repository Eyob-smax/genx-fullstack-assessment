import { NextRequest, NextResponse } from "next/server";
import { parseFilters } from "@/lib/filters";
import { queryEvents } from "@/lib/events";

/**
 * Public JSON endpoint (kept for external consumers). The dashboard page itself no
 * longer calls this: it reads events directly in a Server Component, which avoids a
 * redundant server -> own-API -> server hop.
 */
export async function GET(request: NextRequest) {
  const filters = parseFilters(request.nextUrl.searchParams);

  // Simulate realistic network jitter (150ms - 550ms) so out-of-order responses happen naturally
  const delay = 150 + Math.floor(Math.random() * 400);
  await new Promise((r) => setTimeout(r, delay));

  const filtered = queryEvents(filters);
  return NextResponse.json({ events: filtered, total: filtered.length });
}
