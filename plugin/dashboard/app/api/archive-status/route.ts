import { NextResponse } from "next/server";
import { readArchiveStatus } from "@/lib/archive-status";

export const dynamic = "force-dynamic";

export async function GET() {
  return NextResponse.json(await readArchiveStatus());
}
