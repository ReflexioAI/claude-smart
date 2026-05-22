import { NextResponse } from "next/server";
import { originOnly } from "@/lib/reflexio-url";
import { readConfig } from "@/lib/config-file";

export const dynamic = "force-dynamic";

const DEFAULT_URL = "http://localhost:8071";

function isLocalUrl(raw: string | null): boolean {
  if (!raw) return false;
  try {
    const url = new URL(raw);
    return ["localhost", "127.0.0.1", "0.0.0.0", "::1"].includes(url.hostname);
  } catch {
    return false;
  }
}

async function reflexioConfig(req: Request): Promise<{ base: string; apiKey: string }> {
  const config = await readConfig();
  const header = req.headers.get("x-reflexio-url");
  const fromHeader = header ? originOnly(header) : null;
  const apiKey = config.REFLEXIO_API_KEY || process.env.REFLEXIO_API_KEY || "";
  const fromEnv = originOnly(process.env.REFLEXIO_URL ?? "");
  const fromConfig = originOnly(config.REFLEXIO_URL ?? "");
  const configuredBase = fromEnv ?? fromConfig;
  if (
    fromHeader &&
    !(isLocalUrl(fromHeader) && configuredBase && !isLocalUrl(configuredBase))
  ) {
    return {
      base: fromHeader,
      apiKey: fromHeader === configuredBase ? apiKey : "",
    };
  }
  return {
    base: configuredBase ?? DEFAULT_URL,
    apiKey: configuredBase ? apiKey : "",
  };
}

async function proxy(
  req: Request,
  context: { params: Promise<{ path: string[] }> },
): Promise<Response> {
  const { path } = await context.params;
  const targetPath = path.join("/");
  const url = new URL(req.url);
  const { base, apiKey } = await reflexioConfig(req);
  const target = `${base}/${targetPath}${url.search}`;

  const headers = new Headers(req.headers);
  headers.delete("host");
  headers.delete("x-reflexio-url");
  headers.delete("connection");
  headers.delete("authorization");
  headers.set("user-agent", "claude-smart");
  if (apiKey) headers.set("authorization", `Bearer ${apiKey}`);

  const init: RequestInit = {
    method: req.method,
    headers,
    cache: "no-store",
  };

  if (req.method !== "GET" && req.method !== "HEAD") {
    init.body = await req.arrayBuffer();
  }

  try {
    const upstream = await fetch(target, init);
    const buf = await upstream.arrayBuffer();
    return new NextResponse(buf, {
      status: upstream.status,
      headers: {
        "content-type":
          upstream.headers.get("content-type") ?? "application/octet-stream",
      },
    });
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    return NextResponse.json(
      { error: "reflexio unreachable", detail: message, target },
      { status: 502 },
    );
  }
}

export const GET = proxy;
export const POST = proxy;
export const PUT = proxy;
export const PATCH = proxy;
export const DELETE = proxy;
