import { NextResponse } from "next/server";
import {
  readClaudeCodeHookConfig,
  writeClaudeCodeHookConfig,
} from "@/lib/claude-settings-file";

export const dynamic = "force-dynamic";

export async function GET() {
  const config = await readClaudeCodeHookConfig();
  return NextResponse.json(config);
}

export async function PUT(req: Request) {
  const body = await req.json();
  await writeClaudeCodeHookConfig(body);
  const config = await readClaudeCodeHookConfig();
  return NextResponse.json(config);
}
