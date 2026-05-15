#!/usr/bin/env node
/**
 * npx claude-smart install — thin wrapper around the native host plugin
 * CLIs. For Claude Code it registers the GitHub marketplace and installs the
 * plugin. For Codex it copies the bundled local marketplace, registers it,
 * and enables plugin hooks. Both paths seed ~/.reflexio/.env with the two
 * local-provider flags so reflexio can route generation through local tools
 * with no API key.
 *
 * Keep this file dependency-free — it runs via `npx` with no install step.
 */
"use strict";

const { execSync, spawn } = require("child_process");
const {
  appendFileSync,
  cpSync,
  existsSync,
  mkdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} = require("fs");
const { homedir } = require("os");
const { dirname, join } = require("path");

const DEFAULT_MARKETPLACE_SOURCE = "ReflexioAI/claude-smart";
const PLUGIN_SPEC = "claude-smart@reflexioai";
const CODEX_MARKETPLACE_NAME = "reflexioai";
const CODEX_MARKETPLACE_DISPLAY_NAME = "ReflexioAI";
const CODEX_PLUGIN_ID = `claude-smart@${CODEX_MARKETPLACE_NAME}`;
const REFLEXIO_ENV_PATH = join(homedir(), ".reflexio", ".env");
const CODEX_CONFIG_PATH = join(homedir(), ".codex", "config.toml");
const PACKAGE_ROOT = dirname(dirname(__filename));
const CODEX_MARKETPLACE_DIR = join(
  homedir(),
  ".claude",
  "plugins",
  "marketplaces",
  CODEX_MARKETPLACE_NAME,
);
const CODEX_MARKETPLACE_PLUGIN_PATH = join("plugins", "claude-smart");
const CODEX_PLUGIN_CACHE_DIR = join(
  homedir(),
  ".codex",
  "plugins",
  "cache",
  CODEX_MARKETPLACE_NAME,
  "claude-smart",
);
const CODEX_REQUIRED_FILES = [
  ".agents/plugins/marketplace.json",
  "plugin/.codex-plugin/plugin.json",
  "plugin/hooks/codex-hooks.json",
  "plugin/scripts/_codex_env.sh",
];
const CODEX_CLI_TIMEOUT_MS = 30_000;
const COPYTREE_IGNORE_NAMES = new Set([
  "__pycache__",
  ".venv",
  ".pytest_cache",
  ".ruff_cache",
  "node_modules",
  ".next",
]);

function shouldCopyPath(src) {
  const base = src.split(/[\\/]/).pop() || "";
  if (COPYTREE_IGNORE_NAMES.has(base)) return false;
  if (base.endsWith(".pyc") || base.endsWith(".pyo")) return false;
  return true;
}

function runClaude(args, { spinnerLabel } = {}) {
  const useSpinner = Boolean(spinnerLabel) && process.stdout.isTTY && !process.env.CI;
  return new Promise((resolve) => {
    const child = spawn("claude", args, {
      stdio: useSpinner ? ["inherit", "pipe", "pipe"] : "inherit",
    });

    if (useSpinner) {
      const frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
      let i = 0;
      let spinTimer = null;
      let rearmTimer = null;
      let exited = false;

      const draw = () => {
        process.stdout.write(`\r⠿ ${spinnerLabel}`.replace("⠿", frames[i = (i + 1) % frames.length]));
      };
      const clearLine = () => process.stdout.write("\r\x1b[2K");
      const startSpin = () => {
        if (spinTimer || exited) return;
        draw();
        spinTimer = setInterval(draw, 80);
      };
      const stopSpin = () => {
        if (!spinTimer) return;
        clearInterval(spinTimer);
        spinTimer = null;
        clearLine();
      };
      const armRearm = () => {
        if (rearmTimer) clearTimeout(rearmTimer);
        rearmTimer = setTimeout(() => {
          rearmTimer = null;
          startSpin();
        }, 200);
      };

      startSpin();

      const passthrough = (stream) => (chunk) => {
        stopSpin();
        stream.write(chunk);
        armRearm();
      };
      child.stdout.on("data", passthrough(process.stdout));
      child.stderr.on("data", passthrough(process.stderr));
      child.on("exit", () => {
        exited = true;
        if (rearmTimer) {
          clearTimeout(rearmTimer);
          rearmTimer = null;
        }
        stopSpin();
      });
    }

    child.on("exit", (code) => resolve(typeof code === "number" ? code : 1));
    child.on("error", () => resolve(1));
  });
}

function hasClaudeCli() {
  return hasCli("claude");
}

function hasCli(name) {
  const probe = process.platform === "win32" ? `where ${name}` : `command -v ${name}`;
  try {
    execSync(probe, { stdio: "ignore" });
    return true;
  } catch {
    return false;
  }
}

function runCodex(args) {
  return new Promise((resolve) => {
    const child = spawn("codex", args, {
      stdio: "inherit",
      timeout: CODEX_CLI_TIMEOUT_MS,
      killSignal: "SIGTERM",
    });
    let timedOut = false;
    child.on("exit", (code, signal) => {
      if (signal === "SIGTERM" && code === null) {
        timedOut = true;
        process.stderr.write(
          `error: codex ${args.join(" ")} timed out after ${CODEX_CLI_TIMEOUT_MS / 1000}s\n`,
        );
        resolve(124);
        return;
      }
      if (timedOut) return;
      resolve(typeof code === "number" ? code : 1);
    });
    child.on("error", () => resolve(1));
  });
}

function seedReflexioEnv() {
  mkdirSync(dirname(REFLEXIO_ENV_PATH), { recursive: true });
  const existing = existsSync(REFLEXIO_ENV_PATH)
    ? readFileSync(REFLEXIO_ENV_PATH, "utf8")
    : "";
  const flags = ["CLAUDE_SMART_USE_LOCAL_CLI", "CLAUDE_SMART_USE_LOCAL_EMBEDDING"];
  const missing = flags.filter((f) => !new RegExp(`^${f}=`, "m").test(existing));
  if (missing.length === 0) return [];
  const prefix = existing && !existing.endsWith("\n") ? "\n" : "";
  const body = missing.map((f) => `${f}=1`).join("\n") + "\n";
  appendFileSync(REFLEXIO_ENV_PATH, prefix + body);
  return missing;
}

function printHelp() {
  process.stdout.write(
    [
      "claude-smart — install helper for Claude Code and Codex",
      "",
      "Usage:",
      "  npx claude-smart install                       Install the plugin into Claude Code",
      "  npx claude-smart install --host codex          Register the plugin marketplace for Codex",
      "  npx claude-smart install --source <owner/repo> Override the marketplace source",
      "  npx claude-smart uninstall --host codex        Remove the Codex marketplace registration",
      "  npx claude-smart --help                        Show this help",
      "",
      "Claude Code install:",
      "  1. claude plugin marketplace add <source>",
      `  2. claude plugin install ${PLUGIN_SPEC}`,
      "  3. Appends CLAUDE_SMART_USE_LOCAL_CLI=1 and CLAUDE_SMART_USE_LOCAL_EMBEDDING=1",
      "     to ~/.reflexio/.env (idempotent).",
      "",
      "Codex install:",
      `  1. Copies the bundled marketplace to ${CODEX_MARKETPLACE_DIR}`,
      "  2. codex plugin marketplace add <copied marketplace>",
      "  3. codex features enable plugin_hooks",
      "  4. Installs claude-smart into Codex's plugin cache and enables it",
      "  5. Restart Codex.",
      "",
      "Update:",
      "  npx claude-smart update                        Update to the latest version",
      "",
      "Uninstall:",
      "  npx claude-smart uninstall                     Remove the plugin from Claude Code",
      "",
    ].join("\n"),
  );
}

function parseSource(args) {
  const idx = args.indexOf("--source");
  if (idx === -1) return DEFAULT_MARKETPLACE_SOURCE;
  const value = args[idx + 1];
  if (!value) {
    process.stderr.write("error: --source requires a value (e.g. owner/repo)\n");
    process.exit(1);
  }
  return value;
}

function parseHost(args) {
  const idx = args.indexOf("--host");
  if (idx === -1) return "claude-code";
  const value = args[idx + 1];
  if (!value) {
    process.stderr.write("error: --host requires a value: claude-code or codex\n");
    process.exit(1);
  }
  if (value !== "claude-code" && value !== "codex") {
    process.stderr.write("error: --host must be claude-code or codex\n");
    process.exit(1);
  }
  return value;
}

function copyCodexMarketplace() {
  for (const rel of CODEX_REQUIRED_FILES) {
    const path = join(PACKAGE_ROOT, rel);
    if (!existsSync(path)) {
      process.stderr.write(
        `error: published package is missing ${rel}; reinstall claude-smart or use a newer release\n`,
      );
      process.exit(1);
    }
  }

  rmSync(CODEX_MARKETPLACE_DIR, { recursive: true, force: true });
  mkdirSync(join(CODEX_MARKETPLACE_DIR, ".agents", "plugins"), { recursive: true });
  mkdirSync(join(CODEX_MARKETPLACE_DIR, "plugins"), { recursive: true });

  writeFileSync(
    join(CODEX_MARKETPLACE_DIR, ".agents", "plugins", "marketplace.json"),
    JSON.stringify(
      {
        name: CODEX_MARKETPLACE_NAME,
        interface: { displayName: CODEX_MARKETPLACE_DISPLAY_NAME },
        plugins: [
          {
            name: "claude-smart",
            source: {
              source: "local",
              path: `./${CODEX_MARKETPLACE_PLUGIN_PATH}`,
            },
            policy: {
              installation: "AVAILABLE",
              authentication: "ON_INSTALL",
            },
            category: "Productivity",
          },
        ],
      },
      null,
      2,
    ) + "\n",
  );

  cpSync(join(PACKAGE_ROOT, "plugin"), join(CODEX_MARKETPLACE_DIR, CODEX_MARKETPLACE_PLUGIN_PATH), {
    recursive: true,
    force: true,
    verbatimSymlinks: false,
    filter: shouldCopyPath,
  });

  for (const rel of ["README.md", "LICENSE", "package.json"]) {
    const src = join(PACKAGE_ROOT, rel);
    if (existsSync(src)) {
      cpSync(src, join(CODEX_MARKETPLACE_DIR, rel), {
        recursive: true,
        force: true,
        verbatimSymlinks: false,
      });
    }
  }
  return CODEX_MARKETPLACE_DIR;
}

function removeTomlSections(path, { exact, prefixes = [] }) {
  if (!existsSync(path)) return true;
  const text = readFileSync(path, "utf8");
  if (!text) return true;

  let changed = false;
  let dropping = false;
  const lines = text.split(/(?<=\n)/);
  const kept = [];
  for (const line of lines) {
    const match = line.match(/^\s*\[([^\]]+)\]\s*(?:#.*)?$/);
    if (match) {
      const name = match[1].trim();
      dropping = exact.has(name) || prefixes.some((prefix) => name.startsWith(prefix));
      changed = changed || dropping;
    }
    if (!dropping) kept.push(line);
  }
  if (changed) writeFileSync(path, kept.join(""));
  return true;
}

function cleanupCodexInstallState() {
  removeTomlSections(CODEX_CONFIG_PATH, {
    exact: new Set([
      `plugins."${CODEX_PLUGIN_ID}"`,
      `marketplaces.${CODEX_MARKETPLACE_NAME}`,
    ]),
    prefixes: [`hooks.state."${CODEX_PLUGIN_ID}:`],
  });
  rmSync(CODEX_MARKETPLACE_DIR, { recursive: true, force: true });
  rmSync(CODEX_PLUGIN_CACHE_DIR, { recursive: true, force: true });
  try {
    rmSync(dirname(CODEX_PLUGIN_CACHE_DIR), { recursive: false, force: true });
  } catch {
    // Leave the marketplace cache parent if Codex has other entries there.
  }
}

function setCodexPluginEnabled() {
  const sectionName = `plugins."${CODEX_PLUGIN_ID}"`;
  removeTomlSections(CODEX_CONFIG_PATH, { exact: new Set([sectionName]) });
  const existing = existsSync(CODEX_CONFIG_PATH)
    ? readFileSync(CODEX_CONFIG_PATH, "utf8")
    : "";
  let next = existing;
  if (next && !next.endsWith("\n")) next += "\n";
  if (next.trim()) next += "\n";
  next += `[${sectionName}]\nenabled = true\n`;
  mkdirSync(dirname(CODEX_CONFIG_PATH), { recursive: true });
  writeFileSync(CODEX_CONFIG_PATH, next);
}

function codexPluginVersion(pluginRoot) {
  try {
    const manifest = JSON.parse(
      readFileSync(join(pluginRoot, ".codex-plugin", "plugin.json"), "utf8"),
    );
    return typeof manifest.version === "string" && manifest.version
      ? manifest.version
      : null;
  } catch {
    return null;
  }
}

function installCodexPluginCache(pluginRoot) {
  const version = codexPluginVersion(pluginRoot);
  if (!version) {
    throw new Error(`missing version in ${join(pluginRoot, ".codex-plugin", "plugin.json")}`);
  }
  const cacheDir = join(CODEX_PLUGIN_CACHE_DIR, version);
  rmSync(cacheDir, { recursive: true, force: true });
  mkdirSync(dirname(cacheDir), { recursive: true });
  cpSync(pluginRoot, cacheDir, {
    recursive: true,
    force: true,
    verbatimSymlinks: false,
  });
  setCodexPluginEnabled();
  return cacheDir;
}

async function runUpdate() {
  if (!hasClaudeCli()) {
    process.stderr.write(
      "error: 'claude' CLI not found on PATH. " +
        "Install Claude Code first: https://claude.com/claude-code\n",
    );
    process.exit(1);
  }

  const code = await runClaude(["plugin", "update", PLUGIN_SPEC], {
    spinnerLabel: "Checking for claude-smart updates…",
  });
  if (code !== 0) {
    process.stderr.write(`error: \`claude plugin update ${PLUGIN_SPEC}\` failed (exit ${code})\n`);
    process.exit(code);
  }

  process.stdout.write("\nclaude-smart updated. Restart Claude Code to apply.\n");
}

async function runUninstall(args) {
  if (parseHost(args) === "codex") {
    await runUninstallCodex();
    return;
  }

  if (!hasClaudeCli()) {
    process.stderr.write(
      "error: 'claude' CLI not found on PATH. " +
        "Install Claude Code first: https://claude.com/claude-code\n",
    );
    process.exit(1);
  }

  const code = await runClaude(["plugin", "uninstall", PLUGIN_SPEC], {
    spinnerLabel: "Uninstalling claude-smart…",
  });
  if (code !== 0) {
    process.stderr.write(
      `error: \`claude plugin uninstall ${PLUGIN_SPEC}\` failed (exit ${code})\n`,
    );
    process.exit(code);
  }

  process.stdout.write(
    [
      "",
      "claude-smart uninstalled. Restart Claude Code to apply.",
      "Local data in ~/.reflexio/ and ~/.claude-smart/ was left in place — remove manually if desired.",
      "",
    ].join("\n"),
  );
}

async function runInstall(args) {
  if (parseHost(args) === "codex") {
    await runInstallCodex();
    return;
  }

  if (!hasClaudeCli()) {
    process.stderr.write(
      "error: 'claude' CLI not found on PATH. " +
        "Install Claude Code first: https://claude.com/claude-code\n",
    );
    process.exit(1);
  }

  const source = parseSource(args);
  const steps = [
    { args: ["plugin", "marketplace", "add", source], label: "Adding marketplace…" },
    { args: ["plugin", "install", PLUGIN_SPEC], label: "Installing claude-smart…" },
  ];

  for (const step of steps) {
    const code = await runClaude(step.args, { spinnerLabel: step.label });
    if (code !== 0) {
      process.stderr.write(
        `error: \`claude ${step.args.join(" ")}\` failed (exit ${code})\n`,
      );
      process.exit(code);
    }
  }

  const added = seedReflexioEnv();
  if (added.length > 0) {
    process.stdout.write(
      `Seeded ${REFLEXIO_ENV_PATH} with ${added.join(", ")}.\n`,
    );
  }

  process.stdout.write(
    [
      "",
      "claude-smart installed. Restart Claude Code in your project.",
      "The reflexio backend and dashboard auto-start on session start.",
      "Opt out with CLAUDE_SMART_BACKEND_AUTOSTART=0 or CLAUDE_SMART_DASHBOARD_AUTOSTART=0.",
      "",
    ].join("\n"),
  );
}

async function runInstallCodex() {
  if (!hasCli("codex")) {
    process.stderr.write("error: 'codex' CLI not found on PATH. Install Codex first.\n");
    process.exit(1);
  }

  const marketplaceRoot = copyCodexMarketplace();
  process.stdout.write(`Prepared Codex marketplace at ${marketplaceRoot}.\n`);

  let code = await runCodex(["plugin", "marketplace", "add", marketplaceRoot]);
  if (code !== 0) {
    process.stderr.write(
      `warning: \`codex plugin marketplace add ${marketplaceRoot}\` failed; retrying after removing ${CODEX_MARKETPLACE_NAME}.\n`,
    );
    await runCodex(["plugin", "marketplace", "remove", CODEX_MARKETPLACE_NAME]);
    code = await runCodex(["plugin", "marketplace", "add", marketplaceRoot]);
  }
  if (code !== 0) {
    process.stderr.write(
      `error: could not register Codex marketplace. Run manually: codex plugin marketplace add ${marketplaceRoot}\n`,
    );
    process.exit(code);
  }

  code = await runCodex(["features", "enable", "plugin_hooks"]);
  if (code !== 0) {
    process.stderr.write("error: could not enable Codex plugin_hooks feature.\n");
    process.exit(code);
  }

  let cacheDir = null;
  try {
    cacheDir = installCodexPluginCache(join(marketplaceRoot, CODEX_MARKETPLACE_PLUGIN_PATH));
    process.stdout.write(`Installed Codex plugin cache at ${cacheDir}.\n`);
  } catch (err) {
    process.stderr.write(
      `error: automatic Codex plugin install failed: ${err && err.message ? err.message : err}\n`,
    );
    process.stderr.write(
      `Open Codex, run /plugins, and install claude-smart from the ${CODEX_MARKETPLACE_DISPLAY_NAME} marketplace manually.\n`,
    );
    process.exit(1);
  }

  const added = seedReflexioEnv();
  if (added.length > 0) {
    process.stdout.write(`Seeded ${REFLEXIO_ENV_PATH} with ${added.join(", ")}.\n`);
  }

  process.stdout.write(
    [
      "",
      "claude-smart Codex support is installed.",
      `Restart Codex so the installed plugin and hooks reload. /plugins should show claude-smart as installed from the ${CODEX_MARKETPLACE_DISPLAY_NAME} marketplace.`,
      "Local data is shared with Claude Code under ~/.reflexio/ and ~/.claude-smart/.",
      "",
    ].join("\n"),
  );
}

async function runUninstallCodex() {
  if (!hasCli("codex")) {
    process.stdout.write("Codex CLI not found; skipping marketplace removal.\n");
    cleanupCodexInstallState();
    return;
  }

  const code = await runCodex(["plugin", "marketplace", "remove", CODEX_MARKETPLACE_NAME]);
  if (code !== 0) {
    process.stderr.write(
      `warning: Codex marketplace removal failed; remove manually with: codex plugin marketplace remove ${CODEX_MARKETPLACE_NAME}\n`,
    );
  }
  cleanupCodexInstallState();

  process.stdout.write(
    [
      "",
      "claude-smart Codex plugin and marketplace state removed. Restart Codex to apply.",
      "Codex's global plugin_hooks feature and local data under ~/.reflexio/ and ~/.claude-smart/ were left in place.",
      "",
    ].join("\n"),
  );
}

async function main() {
  const args = process.argv.slice(2);
  const cmd = args[0] || "install";

  if (cmd === "help" || cmd === "--help" || cmd === "-h") {
    printHelp();
    return;
  }

  if (cmd === "install") {
    await runInstall(args.slice(1));
    return;
  }

  if (cmd === "update") {
    await runUpdate();
    return;
  }

  if (cmd === "uninstall") {
    await runUninstall(args.slice(1));
    return;
  }

  process.stderr.write(
    `claude-smart: unknown command '${cmd}'. Try 'npx claude-smart --help'.\n`,
  );
  process.exit(1);
}

main().catch((err) => {
  process.stderr.write(`claude-smart: ${err && err.message ? err.message : err}\n`);
  process.exit(1);
});
