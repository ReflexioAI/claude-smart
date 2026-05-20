import { NextResponse } from "next/server";
import { listAppliedRules } from "@/lib/session-reader";

export const dynamic = "force-dynamic";

const MAX_DAYS_BACK = 365;
const MAX_LIMIT = 500;

function positiveInt(value: string | null, fallback: number): number {
  if (!value) return fallback;
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

export async function GET(req: Request) {
  const url = new URL(req.url);
  const daysBack = Math.min(
    positiveInt(url.searchParams.get("daysBack"), 30),
    MAX_DAYS_BACK,
  );
  const limit = Math.min(
    positiveInt(url.searchParams.get("limit"), 20),
    MAX_LIMIT,
  );
  const stats = await listAppliedRules({ daysBack, limit });
  return NextResponse.json({ success: true, stats });
}
