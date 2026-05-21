import { readFileSync, realpathSync } from "node:fs";
import path from "node:path";

export const dynamic = "force-dynamic";

function realpathOrSelf(value: string): string {
  try {
    return realpathSync(value);
  } catch {
    return value;
  }
}

function pluginVersion(pluginRoot: string): string {
  try {
    const manifest = JSON.parse(
      readFileSync(path.join(pluginRoot, ".codex-plugin", "plugin.json"), "utf8"),
    );
    return typeof manifest.version === "string" && manifest.version
      ? manifest.version
      : "unknown";
  } catch {
    return "unknown";
  }
}

export function GET() {
  const dashboardDir = realpathOrSelf(process.cwd());
  const pluginRoot = realpathOrSelf(path.dirname(dashboardDir));
  const version = pluginVersion(pluginRoot);

  return new Response(
    JSON.stringify({
      service: "claude-smart-dashboard",
      pluginRoot,
      dashboardDir,
      version,
    }),
    {
      headers: {
        "content-type": "application/json",
        "x-claude-smart-dashboard": "1",
        "x-claude-smart-plugin-root": pluginRoot,
        "x-claude-smart-dashboard-dir": dashboardDir,
        "x-claude-smart-version": version,
      },
    },
  );
}
