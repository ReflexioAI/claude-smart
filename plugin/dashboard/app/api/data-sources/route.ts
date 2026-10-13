import { NextResponse } from "next/server";
import { connectionIdentity, dataSources } from "@/lib/data-source";
import { readConfig } from "@/lib/config-file";

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    const sources = await dataSources();
    const config = await readConfig();
    return NextResponse.json({
      defaultSource: sources.defaultSource,
      local: {
        id: connectionIdentity(sources.local),
        url: sources.local.url,
        configured: Boolean(sources.local.url),
        learningModel: String(config.CLAUDE_SMART_LEARNING_MODEL || ""),
      },
      managed: {
        id: connectionIdentity(sources.managed),
        url: sources.managed.url,
        configured: Boolean(sources.managed.url && sources.managed.apiKey),
      },
    });
  } catch {
    return NextResponse.json(
      { error: "Unable to read dashboard connections" },
      { status: 500 },
    );
  }
}
