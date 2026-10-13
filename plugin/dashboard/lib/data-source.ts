import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { createHash } from "node:crypto";
import {
  managedReflexioSettings,
  isRemoteReflexioUrl,
  parseLine,
  deriveFromBackendPort,
} from "./config-file";

export type DataSource = "local" | "managed";
export function connectionIdentity(connection: {
  url: string;
  apiKey: string;
}): string {
  return createHash("sha256")
    .update(`${connection.url}\0${connection.apiKey}`)
    .digest("hex");
}

export async function dataSources() {
  const settings = await managedReflexioSettings();
  const active = {
    apiKey: settings.url ? settings.apiKey : "",
    url:
      settings.url || `http://localhost:${process.env.BACKEND_PORT || "8071"}/`,
  };
  const defaultSource: DataSource = isRemoteReflexioUrl(active.url)
    ? "managed"
    : "local";
  const saved: Record<string, string> = {};
  try {
    const content = await fs.readFile(
      path.join(os.homedir(), ".claude-smart", ".env"),
      "utf8",
    );
    for (const line of content.split("\n")) {
      const pair = parseLine(line);
      const match = pair?.key.match(
        /^CLAUDE_SMART_(LOCAL|MANAGED)_(URL|API_KEY)$/,
      );
      if (pair && match) saved[`${match[1]}_${match[2]}`] = pair.value;
    }
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
  }
  const local =
    defaultSource === "local"
      ? active
      : {
          url: deriveFromBackendPort(
            saved.LOCAL_URL ||
              `http://localhost:${process.env.BACKEND_PORT || "8071"}/`,
          ),
          apiKey: saved.LOCAL_API_KEY || "",
        };
  const managed =
    defaultSource === "managed"
      ? active
      : {
          url: saved.MANAGED_URL || "",
          apiKey: saved.MANAGED_API_KEY || "",
        };
  return { defaultSource, local, managed };
}

export async function selectedConnection(request: Request) {
  const sources = await dataSources();
  const selection =
    request.headers.get("x-claude-smart-source") || sources.defaultSource;
  if (selection !== "local" && selection !== "managed")
    throw new Error("Unknown data source");
  const connection = sources[selection];
  const identity = request.headers.get("x-claude-smart-connection");
  if (identity && identity !== connectionIdentity(connection))
    throw new Error(
      "This backend connection changed. Reload the dashboard before continuing.",
    );
  if (!connection.url || (selection === "managed" && !connection.apiKey)) {
    throw new Error(
      "Managed Reflexio is not configured. Configure a managed connection first.",
    );
  }
  const url = new URL(connection.url);
  if (url.username || url.password || url.search || url.hash)
    throw new Error("Invalid backend endpoint");
  if (selection === "local" && isRemoteReflexioUrl(connection.url))
    throw new Error("Local Reflexio requires a loopback endpoint");
  if (selection === "managed" && url.protocol !== "https:")
    throw new Error("Managed Reflexio requires HTTPS");
  return { source: selection, url: url.origin, apiKey: connection.apiKey };
}
