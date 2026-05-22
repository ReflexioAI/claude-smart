import { NextResponse } from "next/server";
import { readConfig, writeConfig } from "@/lib/config-file";
import type { ClaudeSmartConfig } from "@/lib/types";

export const dynamic = "force-dynamic";

function publicConfig(config: ClaudeSmartConfig): ClaudeSmartConfig {
  return {
    ...config,
    REFLEXIO_API_KEY: "",
    REFLEXIO_API_KEY_SET: Boolean(config.REFLEXIO_API_KEY),
  };
}

export async function GET() {
  const config = await readConfig();
  return NextResponse.json(publicConfig(config));
}

export async function PUT(req: Request) {
  const body = await req.json();
  await writeConfig(body);
  const config = await readConfig();
  return NextResponse.json(publicConfig(config));
}
