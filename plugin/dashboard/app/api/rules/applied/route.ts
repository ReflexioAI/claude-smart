import { NextResponse } from "next/server";
import { listAppliedRules } from "@/lib/session-reader";

export const dynamic = "force-dynamic";

function positiveInt(value: string | null, fallback: number): number {
  if (!value) return fallback;
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

export async function GET(req: Request) {
  const url = new URL(req.url);
  const daysBack = positiveInt(url.searchParams.get("daysBack"), 30);
  const limit = positiveInt(url.searchParams.get("limit"), 20);
  const stats = await listAppliedRules({ daysBack, limit });
  return NextResponse.json({ success: true, stats });
}
