import { NextResponse } from "next/server";
import { selectedConnection } from "@/lib/data-source";

export const dynamic = "force-dynamic";

async function proxy(
  req: Request,
  context: { params: Promise<{ path: string[] }> },
): Promise<Response> {
  const { path } = await context.params;
  const targetPath = path.join("/");
  const url = new URL(req.url);
  let base: string;
  let apiKey: string;
  try {
    const connection = await selectedConnection(req);
    base = connection.url;
    apiKey = connection.apiKey;
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Invalid data source" },
      { status: 409 },
    );
  }
  const target = `${base}/${targetPath}${url.search}`;
  const endpoint = new URL(target).pathname.split("/").at(-1) || "";
  const reads =
    req.method === "GET" ||
    req.method === "HEAD" ||
    (req.method === "POST" &&
      (endpoint.startsWith("get_") ||
        ["page", "summary", "transcript"].includes(endpoint)));
  if (!reads && !req.headers.get("x-claude-smart-connection")) {
    return NextResponse.json(
      { error: "Reload the dashboard before modifying this backend" },
      { status: 409 },
    );
  }

  const headers = new Headers(req.headers);
  headers.delete("host");
  headers.delete("connection");
  headers.delete("authorization");
  headers.delete("cookie");
  headers.delete("x-claude-smart-source");
  headers.delete("x-claude-smart-connection");
  headers.set("user-agent", "claude-smart");
  if (apiKey) headers.set("authorization", `Bearer ${apiKey}`);

  const init: RequestInit = {
    method: req.method,
    headers,
    cache: "no-store",
    signal: AbortSignal.timeout(20000),
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
