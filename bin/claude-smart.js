#!/usr/bin/env node
/**
 * npx claude-smart install — thin wrapper around the native host plugin
 * CLIs. Both Claude Code and Codex install from the bundled marketplace in
 * this npm package: Claude Code registers a stable copy of the package
 * (~/.claude-smart/claude-code/claude-smart) as a local marketplace and runs
 * the plugin in place from it, and Codex copies the bundled plugin into its
 * own marketplace wrapper. Config lives in ~/.claude-smart/.env — the file the
 * hooks and backend read — seeded with local-provider defaults so reflexio can
 * route generation through local tools with no API key.
 * Managed/read-only/global setup is handled by `npx claude-smart setup`,
 * which writes ~/.claude-smart/.env before running this installer.
 *
 * Keep this file dependency-free — it runs via `npx` with no install step.
 */
"use strict";

const { execSync, spawn, spawnSync } = require("child_process");
const crypto = require("crypto");
const {
  chmodSync,
  constants,
  cpSync,
  existsSync,
  accessSync,
  lstatSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  realpathSync,
  renameSync,
  rmdirSync,
  rmSync,
  statSync,
  symlinkSync,
  utimesSync,
  writeFileSync,
} = require("fs");
const http = require("http");
const https = require("https");
const { arch, homedir, platform, release, tmpdir } = require("os");
const { dirname, join, resolve } = require("path");
const { fileURLToPath, pathToFileURL } = require("url");

const PLUGIN_SPEC = "claude-smart@reflexioai";
const CODEX_MARKETPLACE_NAME = "reflexioai";
const CODEX_MARKETPLACE_DISPLAY_NAME = "ReflexioAI";
const CODEX_PLUGIN_ID = `claude-smart@${CODEX_MARKETPLACE_NAME}`;
const OPENCODE_BARE_PLUGIN_SPEC = "claude-smart";
const OPENCODE_CONFIG_NAMES = ["opencode.json", "opencode.jsonc"];
// Pre-#85 installs kept claude-smart's managed settings here. It is also the
// env file of any other Reflexio tool on the machine, so it is only ever read
// for a one-time migration (see migrateLegacyManagedEnv), never written.
const LEGACY_REFLEXIO_ENV_PATH = join(homedir(), ".reflexio", ".env");
const CLAUDE_SMART_ENV_PATH = join(homedir(), ".claude-smart", ".env");
const MANAGED_REFLEXIO_URL = "https://www.reflexio.ai/";
const MANAGED_SETUP_ENV = "CLAUDE_SMART_MANAGED_SETUP";
const CLAUDE_SMART_READ_ONLY_ENV = "CLAUDE_SMART_READ_ONLY";
const CLAUDE_SMART_USE_LOCAL_CLI_ENV = "CLAUDE_SMART_USE_LOCAL_CLI";
const CLAUDE_SMART_USE_LOCAL_EMBEDDING_ENV = "CLAUDE_SMART_USE_LOCAL_EMBEDDING";
const CLAUDE_SMART_HOST_ENV = "CLAUDE_SMART_HOST";
const CLAUDE_SMART_OPENCODE_PATH_ENV = "CLAUDE_SMART_OPENCODE_PATH";
const REFLEXIO_USER_ID_ENV = "REFLEXIO_USER_ID";
const HOST_CLAUDE_CODE = "claude-code";
const HOST_CODEX = "codex";
const HOST_OPENCODE = "opencode";
const SUPPORTED_HOSTS = [HOST_CLAUDE_CODE, HOST_CODEX, HOST_OPENCODE];
const DEFAULT_CLAUDE_SMART_HOST = HOST_CLAUDE_CODE;
const REFLEXIO_DIR = join(homedir(), ".reflexio");
const CLAUDE_SMART_STATE_DIR = join(homedir(), ".claude-smart");
const INSTALL_FAILURE_MARKER = join(CLAUDE_SMART_STATE_DIR, "install-failed");
// Written the first time install considers ~/.reflexio/.env, whatever the
// outcome, so the migration runs once and a later local-mode choice sticks.
// scripts/setup-claude-smart.sh writes the same path before a local install.
const LEGACY_ENV_MIGRATION_MARKER = join(CLAUDE_SMART_STATE_DIR, "legacy-reflexio-env-checked");
const OPENCODE_LOCAL_PACKAGE_DIR = join(CLAUDE_SMART_STATE_DIR, "opencode", "claude-smart");
// Claude Code loads plugins from a local-directory marketplace in place
// (<marketplace>/plugin), so this copy IS the runtime root, not a staging area.
const CLAUDE_CODE_LOCAL_PACKAGE_DIR = join(CLAUDE_SMART_STATE_DIR, "claude-code", "claude-smart");
const OPENCODE_PACKAGE_LOCK_TIMEOUT_MS = 120_000;
const OPENCODE_PACKAGE_LOCK_STALE_MS = 10 * 60_000;
const CODEX_CONFIG_PATH = join(homedir(), ".codex", "config.toml");
const PACKAGE_ROOT = dirname(dirname(__filename));
const CODEX_MARKETPLACE_DIR = join(
  homedir(),
  ".claude",
  "plugins",
  "marketplaces",
  CODEX_MARKETPLACE_NAME,
);
const CODEX_MARKETPLACE_PLUGIN_PATH = "plugin";
const CODEX_PLUGIN_CACHE_DIR = join(
  homedir(),
  ".codex",
  "plugins",
  "cache",
  CODEX_MARKETPLACE_NAME,
  "claude-smart",
);
const LOCAL_DATA_NOTICE = [
  "Local data was kept so reinstalling claude-smart can reuse your learned rules, sessions, logs, and local Reflexio data.",
  "Kept folders:",
  "  ~/.claude-smart",
  "  ~/.reflexio",
  "Delete them only if you want a full reset or need to remove local claude-smart data from this machine:",
  "  rm -rf ~/.claude-smart ~/.reflexio",
];
const CODEX_REQUIRED_FILES = [
  ".agents/plugins/marketplace.json",
  "plugin/.codex-plugin/plugin.json",
  "plugin/hooks/codex-hooks.json",
  "plugin/scripts/codex-claude-compat",
  "plugin/scripts/codex-claude-compat.cmd",
  "plugin/scripts/codex-claude-compat.js",
  "plugin/scripts/opencode-claude-compat",
  "plugin/scripts/opencode-claude-compat.cmd",
  "plugin/scripts/opencode-claude-compat.js",
  "plugin/scripts/codex-hook.js",
  "plugin/scripts/_codex_env.sh",
];
const CODEX_CLI_TIMEOUT_MS = 30_000;
const PLUGIN_SERVICE_TIMEOUT_MS = 45_000;
const COPYTREE_IGNORE_NAMES = new Set([
  "__pycache__",
  ".venv",
  ".pytest_cache",
  ".ruff_cache",
  ".git",
  "node_modules",
  ".next",
]);
const LOCAL_DEFAULT_ENV_ENTRIES = [
  [
    "# Route reflexio generation through the configured local host CLI",
    CLAUDE_SMART_USE_LOCAL_CLI_ENV,
    "1",
  ],
  [
    "# Use the in-process ONNX embedder (ONNX Runtime) - no API key for semantic search",
    CLAUDE_SMART_USE_LOCAL_EMBEDDING_ENV,
    "1",
  ],
  [null, CLAUDE_SMART_READ_ONLY_ENV, "0"],
  [null, CLAUDE_SMART_HOST_ENV, DEFAULT_CLAUDE_SMART_HOST],
];
const ENV_OVERRIDABLE_LOCAL_DEFAULT_KEYS = new Set([
  CLAUDE_SMART_USE_LOCAL_CLI_ENV,
  CLAUDE_SMART_USE_LOCAL_EMBEDDING_ENV,
]);
const LOCAL_MODE_PRUNE_KEYS = new Set([
  "REFLEXIO_URL",
  "REFLEXIO_API_KEY",
  REFLEXIO_USER_ID_ENV,
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
    trackChild(child, false);

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

    child.on("exit", (code) => terminatingOnSignal || resolve(typeof code === "number" ? code : 1));
    child.on("error", () => terminatingOnSignal || resolve(1));
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

function parseEnvLine(line) {
  let trimmed = String(line || "").trim();
  if (!trimmed || trimmed.startsWith("#")) return null;
  if (trimmed.startsWith("export ")) trimmed = trimmed.slice("export ".length).trimStart();
  const eq = trimmed.indexOf("=");
  if (eq < 0) return null;
  const key = trimmed.slice(0, eq).trim();
  if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)) return null;
  let value = trimmed.slice(eq + 1).trim();
  if (
    value.length >= 2 &&
    ((value[0] === '"' && value[value.length - 1] === '"') ||
      (value[0] === "'" && value[value.length - 1] === "'"))
  ) {
    value = value.slice(1, -1);
  }
  return { key, value };
}

function escapeEnvValue(value) {
  return String(value).replace(/\\/g, "\\\\").replace(/"/g, '\\"');
}

function resolveLocalEnvDefault(key, fallback, installHost) {
  if (key === CLAUDE_SMART_HOST_ENV) return installHost;
  if (ENV_OVERRIDABLE_LOCAL_DEFAULT_KEYS.has(key)) {
    const explicit = (process.env[key] || "").trim();
    if (explicit) return explicit;
  }
  return fallback;
}

// prune=false keeps REFLEXIO_URL / REFLEXIO_API_KEY: a keyed loopback URL is
// a local setup whose URL the runtime still honors.
function ensureLocalEnvFile(path, installHost, { prune = true } = {}) {
  mkdirSync(dirname(path), { recursive: true });
  const existing = existsSync(path)
    ? readFileSync(path, "utf8")
    : "";
  const present = new Set();
  const keptLines = [];
  let pruned = false;
  let changed = false;
  let hostWritten = false;
  for (const line of existing.split(/\r?\n/)) {
    const parsed = parseEnvLine(line);
    if (parsed) {
      if (prune && LOCAL_MODE_PRUNE_KEYS.has(parsed.key)) {
        pruned = true;
        continue;
      }
      if (parsed.key === CLAUDE_SMART_HOST_ENV) {
        present.add(parsed.key);
        if (!hostWritten) {
          const replacement = `${CLAUDE_SMART_HOST_ENV}=${escapeEnvValue(installHost)}`;
          keptLines.push(replacement);
          hostWritten = true;
          if (line !== replacement || parsed.value !== installHost) changed = true;
        } else {
          changed = true;
        }
        continue;
      }
      present.add(parsed.key);
    }
    keptLines.push(line);
  }

  const additions = [];
  const added = [];
  for (const [comment, key, value] of LOCAL_DEFAULT_ENV_ENTRIES) {
    if (present.has(key)) continue;
    const effectiveValue = resolveLocalEnvDefault(key, value, installHost);
    if (comment) additions.push(comment);
    if (key === CLAUDE_SMART_READ_ONLY_ENV) {
      additions.push(`${key}="${escapeEnvValue(effectiveValue)}"`);
    } else {
      additions.push(`${key}=${escapeEnvValue(effectiveValue)}`);
    }
    added.push(key);
  }

  if (additions.length > 0 || pruned || changed) {
    let content = keptLines.join("\n").replace(/\n*$/, "");
    if (additions.length > 0) {
      const prefix = content ? "\n" : "";
      content = content + prefix + additions.join("\n");
    }
    writePrivateFile(path, content ? `${content}\n` : "");
  } else if (!existsSync(path)) {
    writePrivateFile(path, "");
  }
  chmodSync(path, 0o600);
  return added;
}

// The env file holds the managed API key: make it 0600 before any content
// lands in it, so no other account can read it between write and chmod.
function writePrivateFile(path, content) {
  if (existsSync(path)) chmodSync(path, 0o600);
  writeFileSync(path, content, { mode: 0o600 });
  chmodSync(path, 0o600);
}

function setEnvVars(path, values) {
  mkdirSync(dirname(path), { recursive: true });
  const existing = existsSync(path) ? readFileSync(path, "utf8") : "";
  const seen = new Set();
  const out = [];
  for (const line of existing ? existing.split(/\r?\n/) : []) {
    const parsed = parseEnvLine(line);
    if (!parsed || !(parsed.key in values)) {
      out.push(line);
      continue;
    }
    out.push(`${parsed.key}="${escapeEnvValue(values[parsed.key])}"`);
    seen.add(parsed.key);
  }
  const added = [];
  for (const [key, value] of Object.entries(values)) {
    if (seen.has(key)) continue;
    out.push(`${key}="${escapeEnvValue(value)}"`);
    added.push(key);
  }
  const content = out.join("\n").replace(/\n*$/, "");
  writePrivateFile(path, content ? `${content}\n` : "");
  return added;
}

function maskSecret(value) {
  if (!value) return "";
  if (value.length <= 8) return "*".repeat(value.length);
  const prefix = value.slice(0, 8).includes("-") ? value.slice(0, 5) : value.slice(0, 4);
  return `${prefix}****${value.slice(-4)}`;
}

function readEnvFile(path) {
  const values = new Map();
  if (!existsSync(path)) return values;
  for (const line of readFileSync(path, "utf8").split(/\r?\n/)) {
    const parsed = parseEnvLine(line);
    if (parsed) values.set(parsed.key, parsed.value);
  }
  return values;
}

// Mirrors claude_smart_reflexio_url_is_remote in plugin/scripts/_lib.sh, which
// decides whether the runtime starts a local backend. Keep the two in sync.
function isRemoteReflexioUrl(url) {
  const value = String(url || "");
  if (!value) return false;
  return !/^http:\/\/(localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])(\/?|:.*)$/.test(value);
}

function isLoopbackUrl(url) {
  try {
    const host = new URL(String(url)).hostname;
    return ["localhost", "127.0.0.1", "0.0.0.0", "[::1]", "::1"].includes(host);
  } catch {
    return false;
  }
}

function localBackendUrl() {
  return `http://localhost:${(process.env.BACKEND_PORT || "").trim() || "8071"}/`;
}

// Carry managed settings written by pre-#85 `claude-smart setup` over to the
// runtime env file. A loopback URL there belongs to some other local Reflexio
// server (e.g. a dev backend), not to claude-smart, so it is left alone.
function migrateLegacyManagedEnv() {
  if (existsSync(LEGACY_ENV_MIGRATION_MARKER)) return;
  migrateLegacyManagedEnvOnce();
  // Recorded only once the check has completed: a read or write error above
  // aborts the install and the next run tries again.
  mkdirSync(CLAUDE_SMART_STATE_DIR, { recursive: true });
  writeFileSync(LEGACY_ENV_MIGRATION_MARKER, "");
}

function migrateLegacyManagedEnvOnce() {
  if ((readEnvFile(CLAUDE_SMART_ENV_PATH).get("REFLEXIO_API_KEY") || "").trim()) return;
  const legacy = readEnvFile(LEGACY_REFLEXIO_ENV_PATH);
  const apiKey = (legacy.get("REFLEXIO_API_KEY") || "").trim();
  // A key with no URL meant the managed service to older installers (and to
  // the Reflexio client, whose default URL it is), so it migrates as such.
  const url = (legacy.get("REFLEXIO_URL") || "").trim() || MANAGED_REFLEXIO_URL;
  if (!apiKey || isLoopbackUrl(url)) return;
  const values = {};
  for (const key of ["REFLEXIO_API_KEY", REFLEXIO_USER_ID_ENV, CLAUDE_SMART_READ_ONLY_ENV]) {
    if (legacy.has(key)) values[key] = legacy.get(key);
  }
  values.REFLEXIO_URL = url;
  setEnvVars(CLAUDE_SMART_ENV_PATH, values);
  process.stdout.write(
    `Migrated managed Reflexio settings from ${LEGACY_REFLEXIO_ENV_PATH} to ${CLAUDE_SMART_ENV_PATH}.\n`,
  );
}

// Reads ~/.claude-smart/.env — the file the hooks and backend read — so the
// mode reported here is the mode the runtime will actually use.
function loadReflexioSetupEnv(installHost = DEFAULT_CLAUDE_SMART_HOST) {
  migrateLegacyManagedEnv();
  const fileEnv = readEnvFile(CLAUDE_SMART_ENV_PATH);
  if (fileEnv.has(REFLEXIO_USER_ID_ENV)) {
    process.env[REFLEXIO_USER_ID_ENV] = fileEnv.get(REFLEXIO_USER_ID_ENV);
  }
  // Same precedence as claude_smart_source_reflexio_env: a key present in the
  // file wins (even when empty), otherwise the inherited environment.
  const resolved = (key) => (fileEnv.has(key) ? fileEnv.get(key) : process.env[key] || "");
  const apiKey = resolved("REFLEXIO_API_KEY").trim();
  // Policy: only a key saved in the file makes install managed. A key that is
  // only exported in this shell is gone in later sessions, and install never
  // copies a secret from the environment into a file.
  const shellOnlyKey = Boolean(apiKey) && !fileEnv.has("REFLEXIO_API_KEY");
  let url = resolved("REFLEXIO_URL");
  const updates = {};
  if (apiKey && !shellOnlyKey && !(fileEnv.get("REFLEXIO_URL") || "").trim()) {
    // The runtime treats a file key with no file URL as local mode, so the URL
    // chosen here (an exported one, else the managed default) must be written
    // where the runtime will read it once that export is gone.
    if (!url.trim()) url = MANAGED_REFLEXIO_URL;
    updates.REFLEXIO_URL = url;
  }
  if (apiKey && !shellOnlyKey && url.trim()) {
    process.env.REFLEXIO_API_KEY = apiKey;
    process.env.REFLEXIO_URL = url;
    process.env[MANAGED_SETUP_ENV] = "1";
    // The host follows the install in both modes (ensureLocalEnvFile does it
    // for local mode).
    updates[CLAUDE_SMART_HOST_ENV] = installHost;
    setEnvVars(CLAUDE_SMART_ENV_PATH, updates);
    if (!isRemoteReflexioUrl(url)) {
      // A key next to a plain http loopback URL is still local mode at
      // runtime, and the local backend needs the local provider defaults.
      const added = ensureLocalEnvFile(CLAUDE_SMART_ENV_PATH, installHost, { prune: false });
      if (added.length > 0) {
        process.stdout.write(`Seeded ${CLAUDE_SMART_ENV_PATH} with ${added.join(", ")}.\n`);
      }
    }
  } else {
    const exportedUrl = process.env.REFLEXIO_URL || "";
    delete process.env.REFLEXIO_URL;
    delete process.env.REFLEXIO_API_KEY;
    delete process.env[REFLEXIO_USER_ID_ENV];
    delete process.env[MANAGED_SETUP_ENV];
    url = "";
    const added = ensureLocalEnvFile(CLAUDE_SMART_ENV_PATH, installHost);
    if (added.length > 0) {
      process.stdout.write(`Seeded ${CLAUDE_SMART_ENV_PATH} with ${added.join(", ")}.\n`);
    }
    if (shellOnlyKey) {
      process.stderr.write(
        "warning: REFLEXIO_API_KEY is only exported in this shell, so install uses local " +
          "mode and saves no key. Run `npx claude-smart setup` to save managed settings.\n",
      );
    }
    // Hooks in a Claude Code started from this shell inherit the export: a
    // remote URL, or a loopback URL on another port, sends them somewhere
    // other than the bundled backend this install starts.
    if (exportedUrl && !isBundledBackendUrl(exportedUrl)) {
      process.stderr.write(
        `warning: REFLEXIO_URL=${exportedUrl} is exported in this shell. ` +
          "Install ignores it, but claude-smart hooks in a Claude Code started from this " +
          `shell inherit it and will not use the local backend at ${localBackendUrl()}. ` +
          "Unset it, or run `npx claude-smart setup` for managed mode.\n",
      );
    }
  }
  const managed = isRemoteReflexioUrl(url);
  if (managed) {
    process.stdout.write(
      `Using managed Reflexio at ${url} (API key ${maskSecret(apiKey)}).\n`,
    );
    if (isLoopbackUrl(url)) {
      process.stderr.write(
        `warning: REFLEXIO_URL ${url} points at this machine but is not a plain ` +
          "http://localhost URL, so claude-smart treats it as remote and will not start " +
          "its local backend.\n",
      );
    }
  } else {
    process.stdout.write(`Using local Reflexio backend at ${url || localBackendUrl()}.\n`);
  }
  const readOnly = ["1", "true", "yes", "on"].includes(
    String(fileEnv.get(CLAUDE_SMART_READ_ONLY_ENV) || "").trim().toLowerCase(),
  );
  return { readOnly, managed, url };
}

function configureReflexioSetup(installHost = DEFAULT_CLAUDE_SMART_HOST) {
  return loadReflexioSetupEnv(installHost);
}

function stripJsonc(text) {
  let out = "";
  let inString = false;
  let escaped = false;
  const skipTrivia = (index) => {
    while (index < text.length) {
      while (index < text.length && /\s/.test(text[index])) index += 1;
      if (text[index] === "/" && text[index + 1] === "/") {
        index += 2;
        while (index < text.length && !"\r\n".includes(text[index])) index += 1;
        continue;
      }
      if (text[index] === "/" && text[index + 1] === "*") {
        index += 2;
        while (index + 1 < text.length && !(text[index] === "*" && text[index + 1] === "/")) index += 1;
        index = Math.min(index + 2, text.length);
        continue;
      }
      break;
    }
    return index;
  };
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i];
    const next = text[i + 1] || "";
    if (inString) {
      out += ch;
      if (escaped) escaped = false;
      else if (ch === "\\") escaped = true;
      else if (ch === '"') inString = false;
      continue;
    }
    if (ch === '"') {
      inString = true;
      out += ch;
      continue;
    }
    if (ch === "/" && next === "/") {
      while (i < text.length && !"\r\n".includes(text[i])) i += 1;
      i -= 1;
      continue;
    }
    if (ch === "/" && next === "*") {
      i += 2;
      while (i + 1 < text.length && !(text[i] === "*" && text[i + 1] === "/")) i += 1;
      i += 1;
      continue;
    }
    if (ch === ",") {
      const j = skipTrivia(i + 1);
      if (text[j] === "}" || text[j] === "]") continue;
    }
    out += ch;
  }
  return out;
}

function readJsoncObject(path) {
  const text = existsSync(path) ? readFileSync(path, "utf8") : "{}";
  const parsed = JSON.parse(stripJsonc(text || "{}"));
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error(`OpenCode config ${path} must be a JSON object`);
  }
  return parsed;
}

function parseGlobalFlag(args) {
  return args.includes("--global");
}

function opencodeGlobalConfigDir() {
  const xdg = (process.env.XDG_CONFIG_HOME || "").trim();
  const base = xdg ? xdg : join(homedir(), ".config");
  return join(base, "opencode");
}

function opencodeConfigPath(args = [], cwd = process.cwd()) {
  if (parseGlobalFlag(args)) {
    const configDir = opencodeGlobalConfigDir();
    for (const name of OPENCODE_CONFIG_NAMES) {
      const candidate = join(configDir, name);
      if (existsSync(candidate)) return candidate;
    }
    return join(configDir, "opencode.json");
  }
  const candidates = [
    ...OPENCODE_CONFIG_NAMES.map((name) => join(cwd, name)),
    ...OPENCODE_CONFIG_NAMES.map((name) => join(cwd, ".opencode", name)),
  ];
  for (const candidate of candidates) {
    if (existsSync(candidate)) return candidate;
  }
  return join(cwd, "opencode.json");
}

function opencodePluginSpec(entry) {
  if (typeof entry === "string") return entry;
  if (Array.isArray(entry) && typeof entry[0] === "string") return entry[0];
  return null;
}

function opencodeLocalPluginSpec(packageRoot = OPENCODE_LOCAL_PACKAGE_DIR) {
  return pathToFileURL(packageRoot).href;
}

function isOpenCodeLocalPackagePath(packagePath) {
  return (
    sameRealPath(packagePath, OPENCODE_LOCAL_PACKAGE_DIR) ||
    resolve(packagePath) === resolve(OPENCODE_LOCAL_PACKAGE_DIR)
  );
}

function isOpenCodeClaudeSmartSpec(spec) {
  if (!spec) return false;
  if (spec === OPENCODE_BARE_PLUGIN_SPEC || spec.startsWith(`${OPENCODE_BARE_PLUGIN_SPEC}@`)) {
    return true;
  }
  if (!spec.startsWith("file://")) return false;
  let packagePath = null;
  try {
    packagePath = fileURLToPath(spec);
  } catch {
    return false;
  }
  if (isOpenCodeLocalPackagePath(packagePath)) return true;
  try {
    const manifest = JSON.parse(readFileSync(join(packagePath, "package.json"), "utf8"));
    if (manifest && manifest.name === OPENCODE_BARE_PLUGIN_SPEC) return true;
  } catch {
    // Missing or malformed manifests are not enough to identify arbitrary file specs.
  }
  return false;
}

function patchOpenCodePluginConfig(configPath, { install, pluginSpec = null }) {
  const resolvedPluginSpec = install ? (pluginSpec || opencodeLocalPluginSpec()) : pluginSpec;
  const data = readJsoncObject(configPath);
  for (const field of ["plugins", "plugin"]) {
    if (data[field] !== undefined && !Array.isArray(data[field])) {
      throw new Error(`OpenCode config ${configPath} field "${field}" must be a JSON array`);
    }
  }
  const current = [
    ...(Array.isArray(data.plugin) ? data.plugin : []),
    ...(Array.isArray(data.plugins) ? data.plugins : []),
  ];
  const kept = current.filter((entry) => !isOpenCodeClaudeSmartSpec(opencodePluginSpec(entry)));
  const next = install ? [...kept, resolvedPluginSpec] : kept;
  const changed =
    data.plugins !== undefined ||
    (Array.isArray(data.plugin)
      ? next.length !== data.plugin.length || next.some((entry, index) => entry !== data.plugin[index])
      : install && next.length > 0);
  if (!changed) return { changed: false, configPath };
  let backupPath = null;
  if (existsSync(configPath)) {
    const original = readFileSync(configPath, "utf8");
    if (original.trim() && original !== stripJsonc(original)) {
      backupPath = `${configPath}.bak`;
      writeFileSync(backupPath, original);
    }
  }
  data.plugin = next;
  delete data.plugins;
  mkdirSync(dirname(configPath), { recursive: true });
  writeFileSync(configPath, JSON.stringify(data, null, 2) + "\n");
  return { changed: true, configPath, backupPath };
}

function hasExtractionProvider() {
  if ((process.env.REFLEXIO_API_KEY || "").trim()) return true;
  const cliPath = (process.env.CLAUDE_SMART_CLI_PATH || "").trim();
  if (cliPath && isExecutableFile(cliPath)) return true;
  return hasCli("claude") || hasCli("codex") || Boolean(resolveOpenCodePath());
}

function isExecutableFile(path) {
  try {
    if (!statSync(path).isFile()) return false;
    accessSync(path, constants.X_OK);
    return true;
  } catch {
    return false;
  }
}

function extractionProviderError() {
  return (
    "error: OpenCode support needs a working learning/extraction provider.\n" +
    "Run `npx claude-smart setup` to configure Reflexio, or install " +
    "OpenCode, Claude Code, or Codex so local extraction can use a supported CLI.\n"
  );
}

function hasOpenCodeCli() {
  return Boolean(resolveOpenCodePath());
}

function resolveOpenCodePath() {
  const opencodePath = (process.env[CLAUDE_SMART_OPENCODE_PATH_ENV] || "").trim();
  if (opencodePath && isExecutableFile(opencodePath)) return opencodePath;
  return resolveCommand(isWindows() ? ["opencode.cmd", "opencode.exe", "opencode"] : ["opencode"]);
}

function persistOpenCodePath() {
  const resolved = resolveOpenCodePath();
  if (!resolved) return [];
  process.env[CLAUDE_SMART_OPENCODE_PATH_ENV] = resolved;
  return setEnvVars(CLAUDE_SMART_ENV_PATH, { [CLAUDE_SMART_OPENCODE_PATH_ENV]: resolved });
}

const WINDOWS_SYSTEM_BASH_SUFFIXES = [
  "\\windows\\system32\\bash.exe",
  "\\windows\\sysnative\\bash.exe",
  "\\windows\\syswow64\\bash.exe",
];

function windowsPathText(path) {
  return path.replace(/\//g, "\\").toLowerCase();
}

function isWindowsSystemBash(path) {
  const normalized = windowsPathText(path);
  return WINDOWS_SYSTEM_BASH_SUFFIXES.some((suffix) => normalized.endsWith(suffix));
}

function pathCommandCandidates(names) {
  // Return every PATH match so Windows can skip System32 bash and still find Git Bash.
  const delimiter = isWindows() ? ";" : ":";
  const pathParts = (process.env.PATH || "").split(delimiter).filter(Boolean);
  const candidates = [];
  for (const dir of pathParts) {
    for (const name of names) {
      const candidate = join(dir, name);
      if (existsSync(candidate)) candidates.push(candidate);
    }
  }
  return candidates;
}

function firstUsableBash(candidates) {
  for (const candidate of candidates) {
    const resolved = existsSync(candidate) ? candidate : resolveCommand([candidate]);
    if (resolved && !isWindowsSystemBash(resolved)) return resolved;
  }
  return null;
}

function resolveUsableBash() {
  if (!isWindows()) return resolveCommand(["bash"]);
  const sources = [];
  const bashEnv = (process.env.BASH || "").trim();
  if (bashEnv) sources.push([bashEnv]);
  sources.push([
    "C:\\Program Files\\Git\\bin\\bash.exe",
    "C:\\Program Files (x86)\\Git\\bin\\bash.exe",
  ]);
  sources.push(pathCommandCandidates(["bash.exe", "bash"]));
  sources.push(["bash.exe", "bash"]);
  for (const source of sources) {
    const resolved = firstUsableBash(source);
    if (resolved) return resolved;
  }
  return null;
}

function opencodePrerequisiteError() {
  if (!hasOpenCodeCli()) {
    return (
      "error: OpenCode CLI not found on PATH. Install OpenCode first, " +
      "or set CLAUDE_SMART_OPENCODE_PATH to the OpenCode executable.\n"
    );
  }
  if (isWindows() && !resolveUsableBash()) {
    return (
      "error: Git Bash is required for claude-smart OpenCode support on Windows. " +
      "Install Git for Windows and ensure bash.exe is on PATH, or run OpenCode from WSL.\n"
    );
  }
  return null;
}

function semverLikePathName(path) {
  const base = String(path).split(/[\\/]/).pop() || "";
  const match = base.match(/^(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$/);
  if (!match) return null;
  return match.slice(1).map((part) => Number.parseInt(part, 10));
}

function compareSemverLikePathNames(a, b) {
  const av = semverLikePathName(a);
  const bv = semverLikePathName(b);
  if (av && bv) {
    for (let i = 0; i < 3; i += 1) {
      if (av[i] !== bv[i]) return av[i] - bv[i];
    }
    return 0;
  }
  if (av) return 1;
  if (bv) return -1;
  return 0;
}

// The root ~/.reflexio/plugin-root currently points at, if it is still a
// usable plugin dir (npm may have pruned an npx target).
function activePluginRoot() {
  let root = null;
  try {
    root = realpathSync(join(REFLEXIO_DIR, "plugin-root"));
  } catch {
    // forcePluginRoot falls back to plugin-root.txt where symlinks fail.
    try {
      root = readFileSync(join(REFLEXIO_DIR, "plugin-root.txt"), "utf8").trim() || null;
    } catch {
      return null;
    }
  }
  return root && existsSync(join(root, "scripts", "backend-service.sh")) ? root : null;
}

// Installed runtime roots other integrations may still use, newest host
// first. Codex keeps one cache dir per version.
function installedPluginRoots() {
  const roots = [
    join(CLAUDE_CODE_LOCAL_PACKAGE_DIR, "plugin"),
    join(OPENCODE_LOCAL_PACKAGE_DIR, "plugin"),
  ];
  try {
    const versions = readdirSync(CODEX_PLUGIN_CACHE_DIR).map((name) => join(CODEX_PLUGIN_CACHE_DIR, name));
    // Same order as findCodexPluginRoot: newest version first, not lexical.
    versions.sort((a, b) => compareSemverLikePathNames(b, a));
    roots.push(...versions);
  } catch {
    // No Codex install.
  }
  return roots.filter((root) => existsSync(join(root, "scripts", "backend-service.sh")));
}

function pluginRootIsBroken() {
  const hasRuntime = (root) => existsSync(join(root, "scripts", "backend-service.sh"));
  const link = join(REFLEXIO_DIR, "plugin-root");
  try {
    if (lstatSync(link).isSymbolicLink()) {
      try {
        return !hasRuntime(realpathSync(link));
      } catch {
        return true;
      }
    }
  } catch {
    // No link; forcePluginRoot may have used plugin-root.txt instead.
  }
  try {
    const root = readFileSync(join(REFLEXIO_DIR, "plugin-root.txt"), "utf8").trim();
    return Boolean(root) && !hasRuntime(root);
  } catch {
    return false;
  }
}

// Call after deleting an integration's files (or after `claude plugin
// uninstall`, which can remove a pre-stable-copy target). The shared
// ~/.reflexio/plugin-root is used by every installed host's commands, so a
// link left pointing at deleted files is repointed at a remaining install,
// or removed when none is left.
function repairPluginRoot() {
  if (!pluginRootIsBroken()) return;
  const next = installedPluginRoots()[0];
  if (next) {
    forcePluginRoot(next);
    process.stdout.write(`Repointed ${join(REFLEXIO_DIR, "plugin-root")} to ${next}.\n`);
  } else {
    rmSync(join(REFLEXIO_DIR, "plugin-root"), { force: true });
    rmSync(join(REFLEXIO_DIR, "plugin-root.txt"), { force: true });
  }
}

function forcePluginRoot(pluginRoot) {
  mkdirSync(REFLEXIO_DIR, { recursive: true });
  const link = join(REFLEXIO_DIR, "plugin-root");
  try {
    const existing = lstatSync(link);
    if (existing.isSymbolicLink() || existing.isFile()) {
      rmSync(link, { force: true });
    } else {
      throw new Error(`refusing to replace non-symlink plugin-root at ${link}`);
    }
  } catch (err) {
    if (err && err.code !== "ENOENT") throw err;
  }
  try {
    // Use a symlink when possible so slash commands follow the active plugin root.
    symlinkSync(pluginRoot, link, isWindows() ? "junction" : "dir");
  } catch {
    writeFileSync(join(REFLEXIO_DIR, "plugin-root.txt"), `${pluginRoot}\n`);
  }
}

function sameRealPath(left, right) {
  try {
    return realpathSync(left) === realpathSync(right);
  } catch {
    return false;
  }
}

function pathEntryExists(path) {
  try {
    lstatSync(path);
    return true;
  } catch {
    return false;
  }
}

function fileSha256(path) {
  return crypto.createHash("sha256").update(readFileSync(path)).digest("hex");
}

function verifyLocalPluginPackage(packageRoot, label) {
  const sourceScript = join(PACKAGE_ROOT, "plugin", "scripts", "smart-install.sh");
  const copiedScript = join(packageRoot, "plugin", "scripts", "smart-install.sh");
  for (const file of [join(packageRoot, "package.json"), copiedScript]) {
    if (!existsSync(file)) {
      throw new Error(`${label} local plugin package is missing ${file}`);
    }
  }
  if (fileSha256(sourceScript) !== fileSha256(copiedScript)) {
    throw new Error(
      `${label} local plugin package does not match the installed claude-smart package`,
    );
  }
}

function sleepSync(ms) {
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
}

function acquirePackageInstallLock(packageRoot, label) {
  const lockDir = join(dirname(packageRoot), ".install.lock");
  mkdirSync(dirname(packageRoot), { recursive: true });
  const deadline = Date.now() + OPENCODE_PACKAGE_LOCK_TIMEOUT_MS;
  while (true) {
    try {
      mkdirSync(lockDir);
      break;
    } catch (err) {
      if (!err || err.code !== "EEXIST") throw err;
      try {
        if (Date.now() - statSync(lockDir).mtimeMs > OPENCODE_PACKAGE_LOCK_STALE_MS) {
          rmSync(lockDir, { recursive: true, force: true });
          continue;
        }
      } catch (statErr) {
        if (statErr && statErr.code !== "ENOENT") throw statErr;
      }
      if (Date.now() >= deadline) {
        throw new Error(`timed out waiting for ${label} package install lock at ${lockDir}`);
      }
      sleepSync(100);
    }
  }
  return lockDir;
}

// Locks held from the package copy until the install commits or rolls back,
// so a concurrent install of the same host waits instead of stacking its
// backup on top of an uncommitted package. The lock's mtime is refreshed
// while held, so a long dependency bootstrap is never mistaken for stale.
const heldPackageLocks = new Map();

function holdPackageInstallLock(packageRoot, label) {
  const lockDir = acquirePackageInstallLock(packageRoot, label);
  const timer = setInterval(() => {
    try {
      const now = new Date();
      utimesSync(lockDir, now, now);
    } catch {
      // Lock dir gone; nothing to refresh.
    }
  }, 30_000);
  timer.unref();
  ensureRollbackOnExit();
  heldPackageLocks.set(packageRoot, { lockDir, timer });
}

// Node does not emit "exit" when SIGINT/SIGTERM/SIGHUP terminate the process
// by default, so an interrupted update would otherwise leave the uncommitted
// copy in place and the lock held. Roll back, then exit with the signal's
// conventional status.
let rollbackOnExitRegistered = false;
function ensureRollbackOnExit() {
  if (rollbackOnExitRegistered) return;
  rollbackOnExitRegistered = true;
  process.once("exit", rollbackLocalPluginPackages);
  for (const [signal, code] of [["SIGINT", 130], ["SIGTERM", 143], ["SIGHUP", 129]]) {
    process.once(signal, async () => {
      terminatingOnSignal = true;
      // Stop and await children first: an orphaned smart-install or Claude
      // CLI step must not keep writing after the rollback restores the
      // previous package.
      await terminateActiveChildren();
      rollbackLocalPluginPackages();
      process.exit(code);
    });
  }
}

function releasePackageInstallLock(packageRoot) {
  const held = heldPackageLocks.get(packageRoot);
  if (!held) return;
  heldPackageLocks.delete(packageRoot);
  clearInterval(held.timer);
  rmSync(held.lockDir, { recursive: true, force: true });
}

function uniquePackagePath(packageRoot, prefix) {
  return join(
    dirname(packageRoot),
    `${prefix}-${process.pid}-${Date.now()}-${crypto.randomBytes(4).toString("hex")}`,
  );
}

// A replaced package (with its prepared .venv and dashboard build) is kept
// aside until the install that replaced it has succeeded, so a failed update
// or reinstall puts the working runtime back instead of leaving the host
// pointed at an unprepared copy. Keyed by package root; the value holds the
// backup path and the inode of the package this process installed. The
// package lock is held until commit or rollback, so no concurrent install
// can replace the package meanwhile; the inode check is a last guard that
// never deletes a backup. Any exit before commitLocalPluginPackage rolls
// back.
const pendingPreviousPackages = new Map();

function rollbackLocalPluginPackages() {
  for (const [packageRoot, { backupPackage, installedIno }] of pendingPreviousPackages) {
    pendingPreviousPackages.delete(packageRoot);
    try {
      if (!pathEntryExists(packageRoot) || lstatSync(packageRoot).ino !== installedIno) {
        // Someone replaced this package despite the lock. Leave both alone;
        // the backup may be the only copy of a working runtime.
        if (backupPackage) {
          process.stderr.write(
            `warning: ${packageRoot} changed during this install; the previous package ` +
              `was kept at ${backupPackage}.\n`,
          );
        }
        continue;
      }
      rmSync(packageRoot, { recursive: true, force: true });
      if (backupPackage) {
        renameSync(backupPackage, packageRoot);
        process.stderr.write(
          `Install did not complete; restored the previous claude-smart package at ${packageRoot}.\n`,
        );
      } else {
        process.stderr.write(
          `Install did not complete; removed the unprepared claude-smart package at ${packageRoot}.\n`,
        );
        repairPluginRoot();
      }
    } catch (err) {
      process.stderr.write(
        `warning: could not restore the previous claude-smart package from ${backupPackage}: ` +
          `${err && err.message ? err.message : err}\n`,
      );
    }
  }
  for (const packageRoot of [...heldPackageLocks.keys()]) releasePackageInstallLock(packageRoot);
  restartStoppedServices();
}

// Services an install stopped before replacing their root. If the install
// does not commit, they are started again from the (restored) previous root
// so a failed update does not leave a working setup offline.
let stoppedServices = null;

function stopServicesForInstall(root, host) {
  stopClaudeSmartServices(root);
  stoppedServices = { root, host };
  ensureRollbackOnExit();
}

function restartStoppedServices() {
  const stopped = stoppedServices;
  stoppedServices = null;
  if (!stopped || !existsSync(join(stopped.root, "scripts", "backend-service.sh"))) return;
  startBackendService(stopped.root, stopped.host);
  runPluginService(stopped.root, "dashboard-service.sh", "start");
  process.stderr.write(`Restarted claude-smart services from ${stopped.root}.\n`);
}

function commitLocalPluginPackage(packageRoot) {
  stoppedServices = null;
  const pending = pendingPreviousPackages.get(packageRoot);
  pendingPreviousPackages.delete(packageRoot);
  if (pending && pending.backupPackage) {
    rmSync(pending.backupPackage, { recursive: true, force: true });
  }
  releasePackageInstallLock(packageRoot);
}

// A newly created package whose runtime is now prepared is worth keeping
// even if a later Claude CLI step fails: the marketplace may already point
// at it. A replaced package keeps its backup until commit.
function markLocalPluginPackagePrepared(packageRoot) {
  const pending = pendingPreviousPackages.get(packageRoot);
  if (pending && !pending.backupPackage) pendingPreviousPackages.delete(packageRoot);
}

function replaceLocalPluginPackage(stagedPackage, packageRoot) {
  const backupPackage = uniquePackagePath(packageRoot, ".claude-smart-previous");
  let backupCreated = false;
  try {
    if (pathEntryExists(packageRoot)) {
      renameSync(packageRoot, backupPackage);
      backupCreated = true;
    }
    renameSync(stagedPackage, packageRoot);
  } catch (err) {
    if (backupCreated && !existsSync(packageRoot) && existsSync(backupPackage)) {
      renameSync(backupPackage, packageRoot);
    }
    throw err;
  }
  // A package with no predecessor is tracked too (backupPackage null): if
  // the install fails before its runtime is prepared, rollback removes it
  // rather than leaving an unprepared copy for the next attempt to treat as
  // the previous working package.
  ensureRollbackOnExit();
  pendingPreviousPackages.set(packageRoot, {
    backupPackage: backupCreated ? backupPackage : null,
    installedIno: lstatSync(packageRoot).ino,
  });
}

// Copy this npm package to a stable per-host dir. The npx cache this runs from
// may be pruned by npm between invocations, so no host may load from it.
function installLocalPluginPackage(packageRoot, label) {
  if (sameRealPath(PACKAGE_ROOT, packageRoot)) {
    verifyLocalPluginPackage(packageRoot, label);
    return packageRoot;
  }
  // Held until commitLocalPluginPackage or the exit rollback releases it.
  holdPackageInstallLock(packageRoot, label);
  const stagedPackage = uniquePackagePath(packageRoot, ".claude-smart-copy");
  rmSync(stagedPackage, { recursive: true, force: true });
  let replaced = false;
  try {
    cpSync(PACKAGE_ROOT, stagedPackage, {
      recursive: true,
      force: true,
      verbatimSymlinks: false,
      filter: shouldCopyPath,
    });
    verifyLocalPluginPackage(stagedPackage, label);
    replaceLocalPluginPackage(stagedPackage, packageRoot);
    replaced = true;
    verifyLocalPluginPackage(packageRoot, label);
    return packageRoot;
  } catch (err) {
    // Nothing replaced: nothing to roll back, so do not keep others waiting.
    if (!replaced) releasePackageInstallLock(packageRoot);
    throw err;
  } finally {
    rmSync(stagedPackage, { recursive: true, force: true });
  }
}

function installOpenCodePluginPackage() {
  return installLocalPluginPackage(OPENCODE_LOCAL_PACKAGE_DIR, "OpenCode");
}

async function bootstrapClaudeCodeInstall(pluginRoot) {
  forcePluginRoot(pluginRoot);
  const bash = resolveCommand(isWindows() ? ["bash.exe", "bash"] : ["bash"]);
  if (!bash) {
    throw new Error("bash is required to bootstrap claude-smart dependencies");
  }
  // Services and the dashboard build start only after the install commits
  // (startCommittedClaudeCodeServices), so a rollback never leaves processes
  // running from, or writing into, a package it has replaced.
  const code = await runChecked(bash, [join(pluginRoot, "scripts", "smart-install.sh")], {
    cwd: pluginRoot,
    env: { ...process.env, CLAUDE_SMART_DEFER_SERVICES: "1" },
  });
  if (code !== 0) {
    throw new Error(`smart-install.sh failed in ${pluginRoot}`);
  }
  throwIfInstallFailureMarker();
  return pluginRoot;
}

// What smart-install.sh defers under CLAUDE_SMART_DEFER_SERVICES: the
// dashboard's first build (detached, as smart-install does); the backend and
// dashboard are then started and reported by startAndReportServices.
function startDeferredDashboardBuild(pluginRoot) {
  const dashboardDir = join(pluginRoot, "dashboard");
  const bash = resolveUsableBash();
  if (!bash || !existsSync(dashboardDir) || existsSync(join(dashboardDir, ".next"))) return;
  const child = spawn(bash, [join(pluginRoot, "scripts", "dashboard-build.sh")], {
    cwd: pluginRoot,
    env: runtimeEnv(),
    detached: true,
    stdio: "ignore",
    windowsHide: true,
  });
  child.on("error", () => {});
  child.unref();
}

function isWindows() {
  return currentPlatform() === "win32";
}

function currentPlatform() {
  return process.env.CLAUDE_SMART_TEST_PLATFORM || platform();
}

function currentArch() {
  return process.env.CLAUDE_SMART_TEST_ARCH || arch();
}

function currentRelease() {
  return process.env.CLAUDE_SMART_TEST_RELEASE || release();
}

function platformSupportError() {
  const os = currentPlatform();
  const cpu = currentArch();
  if (os === "darwin") {
    if (cpu !== "arm64") {
      return "claude-smart currently supports Apple Silicon macOS 14+ only; Intel Mac is not supported because native ML wheels are unavailable.";
    }
    const darwinMajor = Number.parseInt(currentRelease().split(".")[0] || "0", 10);
    if (!Number.isFinite(darwinMajor) || darwinMajor < 23) {
      return "claude-smart currently supports macOS 14+ on Apple Silicon; macOS 13 and older are not supported because native ML wheels are unavailable.";
    }
    return null;
  }
  if (os === "win32") {
    if (cpu !== "x64") {
      return "claude-smart currently supports Windows x64 only; Windows ARM is not supported because native ML wheels are unavailable.";
    }
    return null;
  }
  if (os === "linux") return null;
  return "claude-smart currently supports Apple Silicon macOS 14+, Windows x64, and Linux for vanilla installs.";
}

function assertSupportedRuntimePlatform() {
  const message = platformSupportError();
  if (message) throw new Error(message);
}

function runChecked(command, args, options = {}) {
  // While a package install is uncommitted, run children in their own
  // process group so an interrupt can stop the whole tree (uv, npm, ...)
  // before rolling back; they are non-interactive there.
  const group = !isWindows() && heldPackageLocks.size > 0;
  return new Promise((resolve) => {
    const child = spawn(command, args, {
      cwd: options.cwd,
      env: options.env || process.env,
      shell: isWindows() && /\.(?:cmd|bat)$/i.test(command),
      stdio: "inherit",
      windowsHide: true,
      detached: group,
    });
    trackChild(child, group);
    // While a signal handler is rolling back, the caller must not see the
    // terminated child as a failure and exit first.
    child.on("exit", (code) => terminatingOnSignal || resolve(typeof code === "number" ? code : 1));
    child.on("error", () => terminatingOnSignal || resolve(1));
  });
}

// Children still running, so a signal handler can stop them before rolling
// back a package they may be writing into.
const activeChildren = new Map();
let terminatingOnSignal = false;

function trackChild(child, group) {
  activeChildren.set(child, group);
  const forget = () => activeChildren.delete(child);
  child.on("exit", forget);
  child.on("error", forget);
}

async function terminateActiveChildren(timeoutMs = 5000) {
  const children = [...activeChildren.entries()];
  const signalAll = (signal) => {
    for (const [child, group] of children) {
      if (child.exitCode !== null || child.signalCode !== null) continue;
      try {
        if (isWindows()) {
          // No process groups on Windows: taskkill /T ends the whole tree
          // (uv, npm, ...) rather than only the direct child.
          spawnSync("taskkill", ["/pid", String(child.pid), "/T", "/F"], {
            stdio: "ignore",
            windowsHide: true,
          });
        } else if (group) {
          process.kill(-child.pid, signal);
        } else {
          child.kill(signal);
        }
      } catch {
        // Already gone.
      }
    }
  };
  const exited = () =>
    Promise.all(
      children.map(([child]) =>
        child.exitCode !== null || child.signalCode !== null
          ? null
          : new Promise((resolve) => child.once("exit", resolve)),
      ),
    );
  signalAll("SIGTERM");
  const timedOut = await Promise.race([
    exited().then(() => false),
    new Promise((resolve) => setTimeout(() => resolve(true), timeoutMs).unref()),
  ]);
  if (timedOut) signalAll("SIGKILL");
}

function runSilentStatus(command, args, options = {}) {
  const result = spawnSync(command, args, {
    cwd: options.cwd,
    env: options.env || process.env,
    shell: isWindows() && /\.(?:cmd|bat)$/i.test(command),
    stdio: "ignore",
    windowsHide: true,
  });
  if (result.error || result.signal) return 1;
  return typeof result.status === "number" ? result.status : 1;
}

function runPluginService(pluginRoot, scriptName, subcommand, envOverrides = {}) {
  const script = join(pluginRoot, "scripts", scriptName);
  if (!existsSync(script)) return false;
  const bash = resolveUsableBash();
  if (!bash) {
    const reason = isWindows()
      ? "Git Bash is required for claude-smart services on Windows. Install Git for Windows and ensure bash.exe is on PATH, or run from WSL"
      : "bash is required but was not found on PATH";
    process.stderr.write(`warning: ${scriptName} ${subcommand} ${reason}; continuing.\n`);
    return false;
  }
  const result = spawnSync(bash, [script, subcommand], {
    cwd: pluginRoot,
    env: { ...runtimeEnv(), ...envOverrides },
    stdio: "ignore",
    windowsHide: true,
    timeout: PLUGIN_SERVICE_TIMEOUT_MS,
    killSignal: "SIGTERM",
  });
  if (result.error || result.signal) {
    const reason = result.error && result.error.code === "ETIMEDOUT"
      ? `timed out after ${PLUGIN_SERVICE_TIMEOUT_MS / 1000}s`
      : result.error
        ? result.error.message
        : `terminated by ${result.signal}`;
    process.stderr.write(
      `warning: ${scriptName} ${subcommand} ${reason}; continuing.\n`,
    );
    return false;
  }
  return result.status === 0;
}

function refreshDashboardService(pluginRoot) {
  // dashboard-service.sh is marker-gated: stop only reaps a listener that
  // identifies as claude-smart, so foreign apps on 3001 are left alone.
  runPluginService(pluginRoot, "dashboard-service.sh", "stop");
  return runPluginService(pluginRoot, "dashboard-service.sh", "start");
}

function startBackendService(pluginRoot, host) {
  return runPluginService(pluginRoot, "backend-service.sh", "start", {
    CLAUDE_SMART_HOST: host,
  });
}

function httpProbe(url, timeoutMs = 1000) {
  return new Promise((resolve) => {
    let request;
    try {
      request = http.get(url, (response) => {
        response.resume();
        resolve({ status: response.statusCode || null, headers: response.headers });
      });
    } catch {
      // e.g. a non-numeric BACKEND_PORT / DASHBOARD_PORT: an unsuccessful
      // probe, not an install failure.
      resolve({ status: null, headers: {} });
      return;
    }
    request.on("error", () => resolve({ status: null, headers: {} }));
    request.setTimeout(timeoutMs, () => request.destroy());
  });
}

async function waitForHttp(url, attempts, isReady) {
  for (let i = 0; i < attempts; i += 1) {
    if (isReady(await httpProbe(url))) return true;
    if (i + 1 < attempts) await new Promise((r) => setTimeout(r, 1000));
  }
  return false;
}

// `backend-service.sh status` checks listener identity (a claude-smart backend
// process, current or compatible version), which /health alone cannot: any
// service on the port may answer it.
function backendServiceStatus(pluginRoot) {
  const script = join(pluginRoot, "scripts", "backend-service.sh");
  const bash = resolveUsableBash();
  if (!existsSync(script) || !bash) return "";
  const result = spawnSync(bash, [script, "status"], {
    cwd: pluginRoot,
    env: runtimeEnv(),
    encoding: "utf8",
    stdio: ["ignore", "pipe", "ignore"],
    windowsHide: true,
    timeout: PLUGIN_SERVICE_TIMEOUT_MS,
    killSignal: "SIGTERM",
  });
  return String(result.stdout || "").trim().split(/\r?\n/).pop() || "";
}

// Same precedence as _lib.sh: an exported value wins over ~/.claude-smart/.env.
function autostartDisabled(key) {
  const value = process.env[key] || readEnvFile(CLAUDE_SMART_ENV_PATH).get(key);
  return String(value || "").trim() === "0";
}

// backend-service.sh / dashboard-service.sh always exit 0 (they double as
// hooks), so their status says nothing about whether a service is serving.
// Report only what an HTTP probe observes.
// True when hooks calling `url` reach the bundled backend install starts.
// The Reflexio client joins absolute /api/... paths onto REFLEXIO_URL
// (urljoin), so only the origin matters: http://localhost or
// http://127.0.0.1 on BACKEND_PORT. The exact 8071 spellings also count,
// because claude_smart_derive_reflexio_url_from_backend_port (_lib.sh)
// rewrites them to BACKEND_PORT. Mirrors claude_smart_reflexio_url_is_custom_local.
function isBundledBackendUrl(url) {
  const port = (process.env.BACKEND_PORT || "").trim() || "8071";
  const value = String(url || "").trim();
  const rewritten = ["localhost", "127.0.0.1"].flatMap((host) => [
    `http://${host}:8071`,
    `http://${host}:8071/`,
  ]);
  if (rewritten.includes(value)) return true;
  try {
    const parsed = new URL(value);
    return (
      parsed.protocol === "http:" &&
      ["localhost", "127.0.0.1"].includes(parsed.hostname) &&
      // WHATWG URL drops the scheme's default port ("" for :80).
      (parsed.port || "80") === port
    );
  } catch {
    return false;
  }
}

async function startAndReportServices(pluginRoot, host, setup) {
  const { managed, url: hooksUrl } = setup;
  if (managed) {
    process.stdout.write("Managed mode: no local backend is started.\n");
  } else if (hooksUrl && !isBundledBackendUrl(hooksUrl)) {
    // A kept loopback URL other than the bundled endpoint is the user's own
    // local Reflexio server: the hooks call it, not the bundled backend, so
    // report on it and start nothing.
    // Hooks call the origin (see isBundledBackendUrl), so probe it there.
    let base = hooksUrl;
    try {
      base = `${new URL(hooksUrl).origin}/`;
    } catch {
      // Unparseable: the probe below simply fails.
    }
    const answering = await waitForHttp(`${base}health`, 3, ({ status }) => status === 200);
    process.stdout.write(
      `Hooks use the Reflexio server at ${hooksUrl}, which claude-smart does not start; ` +
        `it is ${answering ? "answering" : "not answering"} ${base}health.\n`,
    );
  } else if (autostartDisabled("CLAUDE_SMART_BACKEND_AUTOSTART")) {
    process.stdout.write("Backend autostart is disabled (CLAUDE_SMART_BACKEND_AUTOSTART=0).\n");
  } else {
    startBackendService(pluginRoot, host);
    const url = localBackendUrl();
    if (await waitForHttp(`${url}health`, 5, ({ status }) => status === 200)) {
      const status = backendServiceStatus(pluginRoot);
      if (status.startsWith("running on")) {
        process.stdout.write(`Backend healthy at ${url}.\n`);
      } else {
        process.stdout.write(
          `Something answers ${url}health, but it is not a claude-smart backend this ` +
            `install can use (backend-service.sh status: ${status || "unknown"}).\n`,
        );
      }
    } else {
      process.stdout.write(
        `Backend is still starting (log: ${join(CLAUDE_SMART_STATE_DIR, "backend.log")}); ` +
          "it is started again at the next session start if needed.\n",
      );
    }
  }
  if (autostartDisabled("CLAUDE_SMART_DASHBOARD_AUTOSTART")) {
    process.stdout.write("Dashboard autostart is disabled (CLAUDE_SMART_DASHBOARD_AUTOSTART=0).\n");
    return;
  }
  refreshDashboardService(pluginRoot);
  const port = (process.env.DASHBOARD_PORT || "").trim() || "3001";
  const url = `http://localhost:${port}/`;
  // Same marker dashboard-service.sh trusts: a foreign app on the port answers
  // the root page too, but only the claude-smart dashboard sends this header.
  const isDashboard = ({ status, headers }) =>
    status === 200 && headers["x-claude-smart-dashboard"] !== undefined;
  if (await waitForHttp(`${url}api/health`, 3, isDashboard)) {
    process.stdout.write(`Dashboard running at ${url}.\n`);
  } else if ((await httpProbe(`${url}api/health`)).status !== null) {
    process.stdout.write(
      `Another app answers ${url} without the claude-smart dashboard marker; ` +
        "the dashboard is not running there. Set DASHBOARD_PORT to a free port.\n",
    );
  } else {
    process.stdout.write(
      `Dashboard is building in the background (first build takes 1-2 minutes); it will serve ${url}.\n`,
    );
  }
}

function stopClaudeSmartServices(pluginRoot) {
  runPluginService(pluginRoot, "dashboard-service.sh", "stop");
  runPluginService(pluginRoot, "backend-service.sh", "stop");
}

function downloadFile(url, dest) {
  return new Promise((resolve, reject) => {
    const request = https.get(url, (response) => {
      if (
        response.statusCode &&
        response.statusCode >= 300 &&
        response.statusCode < 400 &&
        response.headers.location
      ) {
        downloadFile(new URL(response.headers.location, url).toString(), dest)
          .then(resolve, reject);
        response.resume();
        return;
      }
      if (response.statusCode !== 200) {
        response.resume();
        reject(new Error(`download failed (${response.statusCode}) for ${url}`));
        return;
      }
      const chunks = [];
      response.on("data", (chunk) => chunks.push(chunk));
      response.on("end", () => {
        writeFileSync(dest, Buffer.concat(chunks));
        resolve();
      });
    });
    request.on("error", reject);
    request.setTimeout(120_000, () => request.destroy(new Error(`download timed out for ${url}`)));
  });
}

function resolveCommand(names, extraDirs = []) {
  const pathParts = [
    ...extraDirs,
    ...(process.env.PATH || "").split(isWindows() ? ";" : ":"),
  ].filter(Boolean);
  for (const dir of pathParts) {
    for (const name of names) {
      const candidate = join(dir, name);
      if (existsSync(candidate)) return candidate;
    }
  }
  return null;
}

function privateNodeRoot() {
  return join(homedir(), ".claude-smart", "node", "current");
}

function privateNodeBinDirs() {
  const root = privateNodeRoot();
  return [join(root, "bin"), root];
}

function resolvePrivateCommand(names) {
  for (const dir of privateNodeBinDirs()) {
    for (const name of names) {
      const candidate = join(dir, name);
      if (existsSync(candidate)) return candidate;
    }
  }
  return null;
}

function resolvePrivateNode() {
  return resolvePrivateCommand(isWindows() ? ["node.exe", "node"] : ["node"]);
}

function resolvePrivateNpm() {
  return resolvePrivateCommand(isWindows() ? ["npm.cmd", "npm.exe", "npm"] : ["npm"]);
}

function runtimeEnv(extraDirs = []) {
  const delimiter = isWindows() ? ";" : ":";
  const dirs = [
    ...extraDirs,
    ...privateNodeBinDirs(),
    join(homedir(), ".local", "bin"),
    join(homedir(), ".cargo", "bin"),
  ];
  return {
    ...process.env,
    PATH: `${dirs.join(delimiter)}${delimiter}${process.env.PATH || ""}`,
  };
}

function nodeArchiveSpec() {
  const os = currentPlatform();
  const cpu = currentArch();
  let nodeOs = null;
  let archiveExt = null;
  if (os === "darwin") {
    nodeOs = "darwin";
    archiveExt = "tar.gz";
  } else if (os === "win32") {
    nodeOs = "win";
    archiveExt = "zip";
  } else if (os === "linux") {
    nodeOs = "linux";
    archiveExt = "tar.gz";
  } else {
    throw new Error(`unsupported OS for private Node.js install: ${os}`);
  }
  const nodeArch = cpu === "arm64" ? "arm64" : "x64";
  return { nodeOs, nodeArch, archiveExt };
}

async function ensurePrivateNode() {
  const existing = resolvePrivateNode();
  const existingNpm = resolvePrivateNpm();
  if (existing && existingNpm) return { node: existing, npm: existingNpm };

  assertSupportedRuntimePlatform();
  const major = process.env.CLAUDE_SMART_NODE_LTS_MAJOR || "22";
  const { nodeOs, nodeArch, archiveExt } = nodeArchiveSpec();
  const baseUrl = process.env.CLAUDE_SMART_NODE_BASE_URL || `https://nodejs.org/dist/latest-v${major}.x`;
  const nodeRoot = join(homedir(), ".claude-smart", "node");
  const temp = join(tmpdir(), `claude-smart-node-${process.pid}`);
  mkdirSync(nodeRoot, { recursive: true });
  rmSync(temp, { recursive: true, force: true });
  mkdirSync(temp, { recursive: true });

  const sumsPath = join(temp, "SHASUMS256.txt");
  await downloadFile(`${baseUrl}/SHASUMS256.txt`, sumsPath);
  const sums = readFileSync(sumsPath, "utf8");
  const match = sums
    .split(/\r?\n/)
    .map((line) => line.trim().split(/\s+/))
    .find((parts) => parts[1] && new RegExp(`^node-v[^ ]+-${nodeOs}-${nodeArch}\\.${archiveExt.replace(/\./g, "\\.")}$`).test(parts[1]));
  if (!match) throw new Error(`could not resolve Node.js ${nodeOs}-${nodeArch} archive from ${baseUrl}`);
  const [expectedHash, archiveName] = match;
  const archivePath = join(temp, archiveName);
  await downloadFile(`${baseUrl}/${archiveName}`, archivePath);
  const actualHash = crypto.createHash("sha256").update(readFileSync(archivePath)).digest("hex");
  if (actualHash !== expectedHash) {
    throw new Error(`Node.js checksum verification failed for ${archiveName}`);
  }

  const extractDir = join(temp, "extract");
  mkdirSync(extractDir, { recursive: true });
  let code = 0;
  if (archiveExt === "zip") {
    const powershell = resolveCommand(["powershell.exe", "powershell", "pwsh"]);
    if (!powershell) throw new Error("PowerShell is required to extract private Node.js on Windows");
    code = await runChecked(
      powershell,
      [
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        "$ProgressPreference='SilentlyContinue'; Expand-Archive -LiteralPath $env:ARCHIVE_PATH -DestinationPath $env:DEST_DIR -Force",
      ],
      { env: { ...process.env, ARCHIVE_PATH: archivePath, DEST_DIR: extractDir } },
    );
  } else {
    const tar = resolveCommand(["tar"]);
    if (!tar) throw new Error("tar is required to extract private Node.js on macOS");
    code = await runChecked(tar, ["-xzf", archivePath, "-C", extractDir]);
  }
  if (code !== 0) throw new Error(`Node.js archive extraction failed for ${archiveName}`);
  const extracted = join(extractDir, archiveName.replace(/\.zip$/, "").replace(/\.tar\.gz$/, ""));
  const current = privateNodeRoot();
  // Atomic swap with rollback: move existing `current` to a backup first
  // so a non-EXDEV failure (EACCES, EBUSY) does not leave the user with no
  // private node at all. EXDEV (cross-device) falls back to cpSync.
  const backup = `${current}.prev.${process.pid}`;
  rmSync(backup, { recursive: true, force: true });
  const hadCurrent = existsSync(current);
  if (hadCurrent) renameSync(current, backup);
  try {
    try {
      renameSync(extracted, current);
    } catch (err) {
      if (!err || err.code !== "EXDEV") throw err;
      cpSync(extracted, current, {
        recursive: true,
        force: true,
        verbatimSymlinks: true,
      });
    }
  } catch (err) {
    if (hadCurrent) {
      try { renameSync(backup, current); } catch { /* leave backup for manual recovery */ }
    }
    throw err;
  }
  rmSync(backup, { recursive: true, force: true });
  rmSync(temp, { recursive: true, force: true });

  const node = resolvePrivateNode();
  const npm = resolvePrivateNpm();
  if (!node || !npm) throw new Error("private Node.js install completed but node/npm are not usable");
  return { node, npm };
}

function resolveUv() {
  return resolveCommand(isWindows() ? ["uv.exe", "uv"] : ["uv"], [
    join(homedir(), ".local", "bin"),
    join(homedir(), ".cargo", "bin"),
  ]);
}

async function ensureUv() {
  let uv = resolveUv();
  if (uv) return uv;
  assertSupportedRuntimePlatform();
  let code = 0;
  if (isWindows()) {
    const powershell = resolveCommand(["powershell.exe", "powershell", "pwsh"]);
    if (!powershell) throw new Error("PowerShell is required to install uv on Windows");
    code = await runChecked(powershell, [
      "-NoProfile",
      "-ExecutionPolicy",
      "Bypass",
      "-Command",
      "irm https://astral.sh/uv/install.ps1 | iex",
    ]);
    if (code !== 0) throw new Error("uv install via PowerShell failed");
  } else {
    const installer = join(homedir(), ".claude-smart", "uv-install.sh");
    mkdirSync(dirname(installer), { recursive: true });
    await downloadFile("https://astral.sh/uv/install.sh", installer);
    const sh = resolveCommand(["sh"]);
    if (!sh) throw new Error("sh is required to install uv on macOS");
    code = await runChecked(sh, [installer]);
    if (code !== 0) throw new Error("uv install failed");
  }
  uv = resolveUv();
  if (!uv) throw new Error("uv install reported success but uv was not found");
  return uv;
}

function quoteCommandPart(part) {
  return `"${String(part).replace(/"/g, '\\"')}"`;
}

function commandIsPublishHook(command) {
  if (typeof command !== "string") return false;
  return (
    /hook_entry\.sh\b[\s"']+(?:codex|claude-code)[\s"']+(?:stop|session-end)\b/.test(command) ||
    /codex-hook\.js"?(?:\s+"?hook"?){1}\s+"?(?:stop|session-end)"?/.test(command)
  );
}

// Returns false when pluginRoot is this package's own plugin dir: its
// manifests are the pristine source restorePublishHooksFromSource copies
// from, so pruning them would make read-only mode permanent. Publishing is
// still skipped there, because the stop and session-end hooks honor
// CLAUDE_SMART_READ_ONLY themselves.
function prunePublishHooksForReadOnly(pluginRoot) {
  if (sameRealPath(pluginRoot, join(PACKAGE_ROOT, "plugin"))) {
    process.stdout.write(
      "Read-only mode: publishing is skipped via CLAUDE_SMART_READ_ONLY; " +
        "the source hook manifests are left unchanged.\n",
    );
    return false;
  }
  for (const hookFile of ["hooks.json", "codex-hooks.json"]) {
    const hookPath = join(pluginRoot, "hooks", hookFile);
    if (!existsSync(hookPath)) continue;
    const parsed = JSON.parse(readFileSync(hookPath, "utf8"));
    const hooksByEvent = parsed.hooks || {};
    for (const event of Object.keys(hooksByEvent)) {
      const blocks = [];
      for (const block of hooksByEvent[event] || []) {
        const keptHooks = (block.hooks || []).filter(
          (hook) => !commandIsPublishHook(hook && hook.command),
        );
        if (keptHooks.length > 0) blocks.push({ ...block, hooks: keptHooks });
      }
      if (blocks.length > 0) {
        hooksByEvent[event] = blocks;
      } else {
        delete hooksByEvent[event];
      }
    }
    writeFileSync(hookPath, JSON.stringify(parsed, null, 2) + "\n");
  }
  return true;
}

function restorePublishHooksFromSource(pluginRoot) {
  const sourceHooksDir = join(PACKAGE_ROOT, "plugin", "hooks");
  const targetHooksDir = join(pluginRoot, "hooks");
  for (const hookFile of ["hooks.json", "codex-hooks.json"]) {
    const sourcePath = join(sourceHooksDir, hookFile);
    const targetPath = join(targetHooksDir, hookFile);
    if (!existsSync(sourcePath) || !existsSync(targetPath)) continue;
    if (sourcePath === targetPath) continue;
    cpSync(sourcePath, targetPath, { force: true });
  }
}

function patchCodexHooksForNode(pluginRoot, nodePath) {
  const hookPath = join(pluginRoot, "hooks", "codex-hooks.json");
  const parsed = JSON.parse(readFileSync(hookPath, "utf8"));
  const runner = join(pluginRoot, "scripts", "codex-hook.js");
  const command = (...args) => [nodePath, runner, ...args].map(quoteCommandPart).join(" ");
  // Dispatch by command content rather than index — entries can be added or
  // reordered without breaking the patch.
  const patchOne = (original) => {
    if (typeof original !== "string") return original;
    if (original.includes("smart-install.sh")) return original;
    if (original.includes("ensure-plugin-root.sh")) return command("ensure-root");
    if (original.includes("backend-service.sh")) return command("backend");
    if (original.includes("dashboard-service.sh")) return command("dashboard");
    // Match `hook_entry.sh" codex session-start` and similar — between
    // the script name, the host token, and the subcommand there may be
    // closing quotes plus whitespace, so allow both as separators.
    const hookMatch = original.match(/hook_entry\.sh\b[\s"']+(?:codex|claude-code)[\s"']+([\w-]+)/);
    if (hookMatch) return command("hook", hookMatch[1]);
    return original;
  };
  for (const event of Object.keys(parsed.hooks || {})) {
    for (const block of parsed.hooks[event] || []) {
      for (const hook of block.hooks || []) {
        hook.command = patchOne(hook.command);
      }
    }
  }
  writeFileSync(hookPath, JSON.stringify(parsed, null, 2) + "\n");
}

function ensurePluginRoot(pluginRoot) {
  const reflexioDir = REFLEXIO_DIR;
  const pluginRootLink = join(reflexioDir, "plugin-root");
  mkdirSync(reflexioDir, { recursive: true });
  let pathNotReplaceable = false;
  try {
    const existing = lstatSync(pluginRootLink);
    if (existing.isSymbolicLink() || existing.isFile()) {
      rmSync(pluginRootLink, { recursive: true, force: true });
    } else {
      pathNotReplaceable = true;
    }
  } catch (err) {
    if (!err || err.code !== "ENOENT") pathNotReplaceable = true;
  }
  if (pathNotReplaceable) {
    writeFileSync(join(reflexioDir, "plugin-root.txt"), `${pluginRoot}\n`);
    return;
  }
  try {
    symlinkSync(pluginRoot, pluginRootLink, isWindows() ? "junction" : "dir");
    writeFileSync(join(reflexioDir, "plugin-root.txt"), `${pluginRoot}\n`);
  } catch {
    writeFileSync(join(reflexioDir, "plugin-root.txt"), `${pluginRoot}\n`);
  }
}

function pluginPythonPath(pluginRoot) {
  // Python venvs created by uv use Scripts/python.exe on Windows across x64/arm64.
  return isWindows()
    ? join(pluginRoot, ".venv", "Scripts", "python.exe")
    : join(pluginRoot, ".venv", "bin", "python");
}

function installFailureReason() {
  // smart-install.sh owns the install-failed marker format; Node surfaces the
  // first line as the actionable reason and leaves fingerprint handling to shell.
  const text = readFileSync(INSTALL_FAILURE_MARKER, "utf8");
  const first = text.split(/\r?\n/, 1)[0].trim();
  return first || "unknown error";
}

function throwIfInstallFailureMarker() {
  if (existsSync(INSTALL_FAILURE_MARKER)) throw new Error(installFailureReason());
}

function verifyWindowsLocalEmbeddingRuntime(pluginRoot, env) {
  if (!isWindows()) return;
  // Reflexio owns local embeddings, but this installer is the first place a
  // user sees whether the prepared Reflexio runtime can import onnxruntime.
  const script = join(pluginRoot, "scripts", "smart-install.sh");
  const bash = resolveUsableBash();
  if (!bash) {
    throw new Error(
      "Git Bash is required for claude-smart dependency checks on Windows. Install Git for Windows and ensure bash.exe is on PATH, or run from WSL.",
    );
  }
  const result = spawnSync(bash, [script, "verify-windows-embedding"], {
    cwd: pluginRoot,
    env,
    encoding: "utf8",
    windowsHide: true,
  });
  if (result.error || result.signal || result.status !== 0) {
    const detail = result.error
      ? result.error.message
      : result.signal
        ? `terminated by ${result.signal}`
        : (result.stderr || "").trim() || `exited with status ${result.status}`;
    throw new Error(`Windows local embedding preflight failed: ${detail}`);
  }
  throwIfInstallFailureMarker();
}

async function installVendoredReflexio(pluginRoot, uv, env) {
  const vendorRoot = join(pluginRoot, "vendor", "reflexio");
  if (!existsSync(join(vendorRoot, "pyproject.toml"))) return;

  const pythonPath = pluginPythonPath(pluginRoot);
  if (!existsSync(pythonPath)) {
    throw new Error(`plugin Python was not created by uv sync: ${pythonPath}`);
  }

  process.stdout.write(`Installing bundled Reflexio source from ${vendorRoot}...\n`);
  let code = await runChecked(
    uv,
    ["pip", "install", "--project", pluginRoot, "--python", pythonPath, "--quiet", "--reinstall", "--no-deps", vendorRoot],
    { cwd: pluginRoot, env },
  );
  if (code !== 0) {
    process.stderr.write(
      `warning: quiet vendored Reflexio install failed in ${pluginRoot}; retrying with full output.\n`,
    );
    code = await runChecked(
      uv,
      ["pip", "install", "--project", pluginRoot, "--python", pythonPath, "--reinstall", "--no-deps", vendorRoot],
      { cwd: pluginRoot, env },
    );
  }
  if (code !== 0) throw new Error(`vendored Reflexio install failed in ${pluginRoot}`);
}

async function syncPluginPythonEnv(pluginRoot, uv, env) {
  let code = await runChecked(
    uv,
    ["sync", "--locked", "--python", "3.12", "--quiet"],
    { cwd: pluginRoot, env },
  );
  if (code === 0) return;

  const lockIsFresh = runSilentStatus(
    uv,
    ["lock", "--check", "--python", "3.12"],
    { cwd: pluginRoot, env },
  ) === 0;
  if (lockIsFresh) {
    process.stderr.write(
      `warning: quiet uv sync failed in ${pluginRoot}; retrying with full output.\n`,
    );
    code = await runChecked(
      uv,
      ["sync", "--locked", "--python", "3.12"],
      { cwd: pluginRoot, env },
    );
    if (code !== 0) throw new Error(`uv sync failed in ${pluginRoot}`);
    return;
  }

  process.stderr.write(
    "warning: plugin/uv.lock is out of sync; refreshing local lockfile and retrying uv sync.\n",
  );
  code = await runChecked(
    uv,
    ["lock", "--python", "3.12"],
    { cwd: pluginRoot, env },
  );
  if (code !== 0) throw new Error(`uv lock failed in ${pluginRoot}`);
  code = await runChecked(
    uv,
    ["sync", "--python", "3.12", "--quiet"],
    { cwd: pluginRoot, env },
  );
  if (code !== 0) {
    process.stderr.write(
      `warning: quiet uv sync failed in ${pluginRoot} after refreshing plugin/uv.lock; retrying with full output.\n`,
    );
    code = await runChecked(
      uv,
      ["sync", "--python", "3.12"],
      { cwd: pluginRoot, env },
    );
  }
  if (code !== 0) throw new Error(`uv sync failed in ${pluginRoot}`);
}

async function bootstrapPluginRuntime(pluginRoot, options = {}) {
  assertSupportedRuntimePlatform();
  process.stdout.write("Preparing claude-smart runtime for hooks...\n");
  rmSync(INSTALL_FAILURE_MARKER, { force: true });
  const nodeRuntime = await ensurePrivateNode();
  if (options.patchCodexHooks !== false) {
    patchCodexHooksForNode(pluginRoot, nodeRuntime.node);
  }
  if (options.readOnly) prunePublishHooksForReadOnly(pluginRoot);
  ensurePluginRoot(pluginRoot);
  const uv = await ensureUv();
  const env = runtimeEnv([dirname(uv), ...privateNodeBinDirs()]);
  const pyprojectPath = join(pluginRoot, "pyproject.toml");
  const pyproject = existsSync(pyprojectPath) ? readFileSync(pyprojectPath, "utf8") : "";
  if (/^\s*\[tool\.uv\.sources\]\s*$/m.test(pyproject)) {
    const lockCode = await runChecked(
      uv,
      ["lock", "--quiet"],
      { cwd: pluginRoot, env },
    );
    if (lockCode !== 0) throw new Error(`uv lock failed in ${pluginRoot}`);
  }
  await syncPluginPythonEnv(pluginRoot, uv, env);
  await installVendoredReflexio(pluginRoot, uv, env);
  verifyWindowsLocalEmbeddingRuntime(pluginRoot, env);

  const dashboardDir = join(pluginRoot, "dashboard");
  if (existsSync(dashboardDir)) {
    let code = await runChecked(nodeRuntime.npm, ["ci"], { cwd: dashboardDir, env });
    if (code !== 0) throw new Error(`npm ci failed in ${dashboardDir}`);
    code = await runChecked(nodeRuntime.npm, ["run", "build"], { cwd: dashboardDir, env });
    if (code !== 0) throw new Error(`npm run build failed in ${dashboardDir}`);
  }
}

function printHelp() {
  process.stdout.write(
    [
      "claude-smart — install helper for Claude Code, Codex, and OpenCode",
      "",
      "Usage:",
      "  npx claude-smart install                       Install the plugin into Claude Code",
      "  npx claude-smart install --host codex          Register the plugin marketplace for Codex",
      "  npx claude-smart install --host opencode       Add claude-smart to OpenCode config",
      "  npx claude-smart setup                         Configure managed/read-only/global setup",
      "  npx claude-smart uninstall --host codex        Remove the Codex marketplace registration",
      "  npx claude-smart uninstall --host opencode     Remove claude-smart from OpenCode config",
      "  npx claude-smart --help                        Show this help",
      "",
      "Claude Code install:",
      "  1. claude plugin marketplace add <this package>",
      `  2. claude plugin install ${PLUGIN_SPEC}`,
      "  3. Reads setup/bootstrap config when managed/read-only setup was configured.",
      "",
      "Codex install:",
      `  1. Copies the bundled marketplace to ${CODEX_MARKETPLACE_DIR}`,
      "  2. codex plugin marketplace add <copied marketplace>",
      "  3. codex features enable hooks && codex features enable plugin_hooks",
      "  4. Installs private Node/npm, uv, Python deps, and dashboard deps as needed",
      "  5. Installs claude-smart into Codex's plugin cache and enables it",
      "  6. Trusts and enables claude-smart hook entries in ~/.codex/config.toml",
      "  7. Restart Codex.",
      "",
      "OpenCode install:",
      `  1. Copies this package to ${OPENCODE_LOCAL_PACKAGE_DIR}`,
      "  2. Adds that local file:// package to OpenCode's plugin list in opencode.json",
      "  3. Prepares local services now from the copied plugin runtime",
      "  4. Restart OpenCode.",
      "",
      "Update:",
      "  npx claude-smart update                        Reinstall Claude Code support from this package",
      "  npx claude-smart update --host codex           Reinstall Codex support from this package",
      "  npx claude-smart update --host opencode        Reinstall OpenCode support from this package",
      "  npx claude-smart setup                         Configure managed/read-only/global setup",
      "",
      "Uninstall:",
      "  npx claude-smart uninstall                     Remove the plugin from Claude Code",
      "",
    ].join("\n"),
  );
}

function parseHost(args) {
  const idx = args.indexOf("--host");
  if (idx === -1) return DEFAULT_CLAUDE_SMART_HOST;
  const value = args[idx + 1];
  if (!value) {
    process.stderr.write(`error: --host requires a value: ${SUPPORTED_HOSTS.join(", ")}\n`);
    process.exit(1);
  }
  if (!SUPPORTED_HOSTS.includes(value)) {
    process.stderr.write(`error: --host must be ${SUPPORTED_HOSTS.join(", ")}\n`);
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

function codexMarketplacePluginRoot(marketplaceRoot) {
  const manifestPath = join(marketplaceRoot, ".agents", "plugins", "marketplace.json");
  const fallback = join(marketplaceRoot, CODEX_MARKETPLACE_PLUGIN_PATH);
  try {
    const manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
    const entry = (manifest.plugins || []).find((plugin) => plugin.name === "claude-smart");
    const rawPath = entry && entry.source && entry.source.path;
    if (typeof rawPath !== "string" || !rawPath) return fallback;
    const relPath = rawPath.replace(/^\.\//, "");
    return join(marketplaceRoot, relPath);
  } catch {
    return fallback;
  }
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
  repairPluginRoot();
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

function tomlDottedQuoted(name) {
  return `"${name.replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"`;
}

function setTomlFeature(feature, value) {
  // Minimal port of `_set_toml_feature` in plugin/src/claude_smart/cli.py:
  // ensures `[features]\n<feature> = <bool>\n` is present in
  // ~/.codex/config.toml, replacing any prior value for the same key.
  const desired = `${feature} = ${value ? "true" : "false"}`;
  const sectionRe = /^\s*\[([^\]]+)\]\s*(?:#.*)?$/;
  const featureRe = new RegExp(`^\\s*${feature.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\s*=`);
  const text = existsSync(CODEX_CONFIG_PATH)
    ? readFileSync(CODEX_CONFIG_PATH, "utf8")
    : "";
  const lines = text.split("\n");
  let inFeatures = false;
  let featuresIdx = null;
  let insertIdx = null;
  let changed = false;
  const out = [];
  for (const line of lines) {
    const sectionMatch = line.match(sectionRe);
    if (sectionMatch) {
      if (inFeatures && insertIdx === null) insertIdx = out.length;
      inFeatures = sectionMatch[1].trim() === "features";
      if (inFeatures) featuresIdx = out.length;
      out.push(line);
      continue;
    }
    if (inFeatures && featureRe.test(line)) {
      out.push(desired);
      changed = changed || line !== desired;
      continue;
    }
    out.push(line);
  }
  if (featuresIdx === null) {
    if (out.length && out[out.length - 1].trim()) out.push("");
    out.push("[features]", desired);
    changed = true;
  } else {
    const sectionEnd = insertIdx !== null ? insertIdx : out.length;
    let hasFeature = false;
    for (let i = featuresIdx + 1; i < sectionEnd; i++) {
      if (featureRe.test(out[i])) { hasFeature = true; break; }
    }
    if (!hasFeature) {
      const idx = insertIdx !== null ? insertIdx : out.length;
      out.splice(idx, 0, desired);
      changed = true;
    }
  }
  if (!changed && text.endsWith("\n")) return true;
  mkdirSync(dirname(CODEX_CONFIG_PATH), { recursive: true });
  let payload = out.join("\n");
  if (!payload.endsWith("\n")) payload += "\n";
  writeFileSync(CODEX_CONFIG_PATH, payload);
  return true;
}

function setCodexHookStates(states) {
  const entries = Object.entries(states);
  if (entries.length === 0) return false;
  removeTomlSections(CODEX_CONFIG_PATH, {
    exact: new Set(),
    prefixes: [`hooks.state."${CODEX_PLUGIN_ID}:`],
  });
  const existing = existsSync(CODEX_CONFIG_PATH)
    ? readFileSync(CODEX_CONFIG_PATH, "utf8")
    : "";
  let next = existing;
  if (next && !next.endsWith("\n")) next += "\n";
  if (!next.includes("[hooks.state]")) {
    if (next.trim()) next += "\n";
    next += "[hooks.state]\n";
  }
  if (next.trim()) next += "\n";
  for (const [key, currentHash] of entries.sort(([a], [b]) => a.localeCompare(b))) {
    next += `[hooks.state.${tomlDottedQuoted(key)}]\n`;
    next += "enabled = true\n";
    next += `trusted_hash = "${currentHash}"\n\n`;
  }
  mkdirSync(dirname(CODEX_CONFIG_PATH), { recursive: true });
  writeFileSync(CODEX_CONFIG_PATH, next.trimEnd() + "\n");
  return true;
}

function createCodexAppServerClient(child) {
  // A single long-lived stdout listener that demultiplexes JSON-RPC responses
  // by id. Avoids losing messages between sequential requests.
  const pending = new Map();
  let buffer = "";
  let exited = false;

  const onData = (chunk) => {
    buffer += chunk.toString();
    let newline;
    while ((newline = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, newline);
      buffer = buffer.slice(newline + 1);
      if (!line.trim()) continue;
      let message;
      try {
        message = JSON.parse(line);
      } catch {
        continue;
      }
      const entry = pending.get(message.id);
      if (!entry) continue;
      pending.delete(message.id);
      clearTimeout(entry.timer);
      if (message.error) {
        entry.reject(new Error(JSON.stringify(message.error)));
      } else {
        entry.resolve(message);
      }
    }
  };
  const onExit = () => {
    exited = true;
    for (const entry of pending.values()) {
      clearTimeout(entry.timer);
      entry.reject(new Error("Codex app-server exited before responding"));
    }
    pending.clear();
  };
  child.stdout.on("data", onData);
  child.on("exit", onExit);

  return {
    request(id, method, params, timeoutMs) {
      return new Promise((resolve, reject) => {
        if (exited) {
          reject(new Error("Codex app-server exited before responding"));
          return;
        }
        const timer = setTimeout(() => {
          pending.delete(id);
          reject(new Error(`Codex app-server ${method} timed out`));
        }, timeoutMs);
        pending.set(id, { resolve, reject, timer });
        child.stdin.write(JSON.stringify({ id, method, params }) + "\n");
      });
    },
    notify(method, params) {
      if (exited) return;
      child.stdin.write(JSON.stringify({ method, params }) + "\n");
    },
    close() {
      child.stdout.off("data", onData);
      child.off("exit", onExit);
    },
  };
}

async function listCodexPluginHooks(cwd) {
  const child = spawn("codex", ["app-server", "--listen", "stdio://"], {
    stdio: ["pipe", "pipe", "ignore"],
  });
  const client = createCodexAppServerClient(child);
  try {
    await client.request(
      1,
      "initialize",
      {
        clientInfo: {
          name: "claude_smart_installer",
          title: "claude-smart installer",
          version: "0.0.0",
        },
        capabilities: { experimentalApi: true },
      },
      CODEX_CLI_TIMEOUT_MS,
    );
    client.notify("initialized", {});
    const response = await client.request(
      2,
      "hooks/list",
      { cwds: [cwd] },
      CODEX_CLI_TIMEOUT_MS,
    );
    const hooks = response.result?.data?.[0]?.hooks;
    if (!Array.isArray(hooks)) {
      throw new Error("Codex app-server hook metadata was malformed");
    }
    return hooks.filter(
      (hook) =>
        hook &&
        (hook.pluginId === CODEX_PLUGIN_ID ||
          String(hook.key || "").startsWith(`${CODEX_PLUGIN_ID}:`)),
    );
  } finally {
    client.close();
    child.stdin.destroy();
    child.stdout.destroy();
    child.kill("SIGTERM");
    child.unref();
  }
}

async function trustCodexPluginHooks(cwd) {
  const hooks = await listCodexPluginHooks(cwd);
  const states = {};
  for (const hook of hooks) {
    if (
      typeof hook.key === "string" &&
      hook.key.startsWith(`${CODEX_PLUGIN_ID}:`) &&
      typeof hook.currentHash === "string"
    ) {
      states[hook.key] = hook.currentHash;
    }
  }
  if (Object.keys(states).length === 0) {
    throw new Error("Codex did not report trust hashes for claude-smart hooks");
  }
  if (!setCodexHookStates(states)) {
    throw new Error(`could not write claude-smart hook trust state to ${CODEX_CONFIG_PATH}`);
  }
  return Object.keys(states).length;
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

function findCodexPluginRoot() {
  const candidates = [];
  try {
    for (const entry of readdirSync(CODEX_PLUGIN_CACHE_DIR, { withFileTypes: true })) {
      if (!entry.isDirectory()) continue;
      const candidate = join(CODEX_PLUGIN_CACHE_DIR, entry.name);
      if (
        existsSync(join(candidate, "pyproject.toml")) &&
        existsSync(join(candidate, "scripts", "smart-install.sh"))
      ) {
        candidates.push(candidate);
      }
    }
  } catch {
    // No Codex cache yet.
  }
  candidates.sort((a, b) => {
    const versionCompare = compareSemverLikePathNames(b, a);
    if (versionCompare !== 0) return versionCompare;
    try {
      return statSync(b).mtimeMs - statSync(a).mtimeMs;
    } catch {
      return 0;
    }
  });
  return candidates[0] || null;
}

async function runUpdate(args) {
  if (parseHost(args) === HOST_CODEX) {
    await runUpdateCodex(args);
    return;
  }
  if (parseHost(args) === HOST_OPENCODE) {
    await runUpdateOpenCode(args);
    return;
  }

  process.stdout.write("Updating claude-smart by reinstalling from this package...\n");
  await runInstall(args, { retryInstallAfterUninstall: true });
}

async function runUpdateCodex(args) {
  const pluginRoot = findCodexPluginRoot() || join(PACKAGE_ROOT, "plugin");
  stopClaudeSmartServices(pluginRoot);
  process.stdout.write("Updating claude-smart Codex support by reinstalling from this package...\n");
  await runInstallCodex(args);
}

async function runUpdateOpenCode(args) {
  const pluginRoot = join(PACKAGE_ROOT, "plugin");
  stopClaudeSmartServices(pluginRoot);
  process.stdout.write("Updating claude-smart OpenCode support by reinstalling from this package...\n");
  await runInstallOpenCode(args);
}

async function runUninstall(args) {
  if (parseHost(args) === HOST_CODEX) {
    await runUninstallCodex();
    return;
  }
  if (parseHost(args) === HOST_OPENCODE) {
    await runUninstallOpenCode(args);
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
  stopClaudeSmartServices(join(PACKAGE_ROOT, "plugin"));
  stopClaudeSmartServices(join(CLAUDE_CODE_LOCAL_PACKAGE_DIR, "plugin"));
  // The marketplace points at the copy removed below; drop it too so Claude
  // Code is not left with an entry for a missing directory.
  const marketplaceCode = await runClaude(["plugin", "marketplace", "remove", CODEX_MARKETPLACE_NAME]);
  if (marketplaceCode !== 0) {
    process.stderr.write(
      `warning: could not remove the ${CODEX_MARKETPLACE_NAME} marketplace; remove it with: ` +
        `claude plugin marketplace remove ${CODEX_MARKETPLACE_NAME}\n`,
    );
  }
  rmSync(CLAUDE_CODE_LOCAL_PACKAGE_DIR, { recursive: true, force: true });
  repairPluginRoot();

  process.stdout.write(
    [
      "",
      "claude-smart uninstalled. Restart Claude Code to apply.",
      ...LOCAL_DATA_NOTICE,
      "",
    ].join("\n"),
  );
}

async function runSetup(args) {
  const bash = resolveCommand(isWindows() ? ["bash.exe", "bash"] : ["bash"]);
  if (!bash) {
    process.stderr.write("error: bash is required to run claude-smart setup.\n");
    process.exit(1);
  }
  const script = join(PACKAGE_ROOT, "scripts", "setup-claude-smart.sh");
  if (!existsSync(script)) {
    process.stderr.write(`error: setup script not found at ${script}\n`);
    process.exit(1);
  }
  const code = await runChecked(bash, [script, ...args], { cwd: PACKAGE_ROOT });
  if (code !== 0) process.exit(code);
}

async function runInstall(args, options = {}) {
  if (parseHost(args) === HOST_CODEX) {
    await runInstallCodex(args);
    return;
  }
  if (parseHost(args) === HOST_OPENCODE) {
    await runInstallOpenCode(args);
    return;
  }

  if (!hasClaudeCli()) {
    process.stderr.write(
      "error: 'claude' CLI not found on PATH. " +
        "Install Claude Code first: https://claude.com/claude-code\n",
    );
    process.exit(1);
  }

  const setup = configureReflexioSetup(HOST_CLAUDE_CODE);
  const readOnly = setup.readOnly;
  // Stop services running from the previous root (e.g. a pruned npx dir or an
  // older copy) before the copy under them is replaced.
  const previousRoot = activePluginRoot();
  if (previousRoot) stopServicesForInstall(previousRoot, HOST_CLAUDE_CODE);
  let source;
  try {
    source = installLocalPluginPackage(CLAUDE_CODE_LOCAL_PACKAGE_DIR, "Claude Code");
  } catch (err) {
    process.stderr.write(
      `error: could not prepare claude-smart Claude Code package: ${err && err.message ? err.message : err}\n`,
    );
    process.exit(1);
  }
  // Prepare the copy BEFORE registering it: re-adding the marketplace with
  // a new path re-points an existing "reflexioai" entry (verified on Claude
  // Code 2.1.280), and on a first migration from the npx dir there is no
  // previous stable copy to roll back to. So Claude Code is only moved once
  // the runtime it will load is ready; a failed bootstrap leaves the old
  // registration (and ~/.reflexio/plugin-root) as they were.
  let pluginRoot;
  try {
    // Claude Code runs a local-directory marketplace plugin in place, so the
    // copy's plugin dir is the one runtime root to bootstrap and report.
    pluginRoot = await bootstrapClaudeCodeInstall(join(source, "plugin"));
    restorePublishHooksFromSource(pluginRoot);
    if (readOnly && prunePublishHooksForReadOnly(pluginRoot)) {
      process.stdout.write("Installed read-only hook manifest; publish interactions hooks are disabled.\n");
    }
    process.stdout.write(`Prepared claude-smart runtime at ${pluginRoot}.\n`);
    markLocalPluginPackagePrepared(CLAUDE_CODE_LOCAL_PACKAGE_DIR);
  } catch (err) {
    if (previousRoot && existsSync(join(previousRoot, "scripts", "backend-service.sh"))) {
      forcePluginRoot(previousRoot);
    }
    process.stderr.write(
      `error: claude-smart dependency bootstrap failed: ${err && err.message ? err.message : err}\n`,
    );
    process.stderr.write(
      "Claude Code was left on its previous claude-smart registration. Fix the issue above, " +
        "then run `npx claude-smart install` again.\n",
    );
    process.exit(1);
  }

  const steps = [
    { args: ["plugin", "marketplace", "add", source], label: "Adding marketplace…" },
    { args: ["plugin", "install", PLUGIN_SPEC], label: "Installing claude-smart…" },
  ];

  for (const step of steps) {
    let code = await runClaude(step.args, { spinnerLabel: step.label });
    if (
      code !== 0 &&
      options.retryInstallAfterUninstall &&
      step.args[0] === "plugin" &&
      step.args[1] === "install"
    ) {
      process.stderr.write(
        `warning: \`claude ${step.args.join(" ")}\` failed (exit ${code}); retrying after uninstalling ${PLUGIN_SPEC}.\n`,
      );
      await runClaude(["plugin", "uninstall", PLUGIN_SPEC], {
        spinnerLabel: "Removing existing claude-smart install…",
      });
      code = await runClaude(step.args, { spinnerLabel: step.label });
    }
    if (code !== 0) {
      process.stderr.write(
        `error: \`claude ${step.args.join(" ")}\` failed (exit ${code})\n`,
      );
      process.exit(code);
    }
  }

  commitLocalPluginPackage(CLAUDE_CODE_LOCAL_PACKAGE_DIR);
  startDeferredDashboardBuild(pluginRoot);
  await startAndReportServices(pluginRoot, HOST_CLAUDE_CODE, setup);

  process.stdout.write(
    [
      "",
      "claude-smart installed and dependencies are prepared. Restart Claude Code in your project.",
      "The reflexio backend and dashboard auto-start on session start.",
      "Opt out with CLAUDE_SMART_BACKEND_AUTOSTART=0 or CLAUDE_SMART_DASHBOARD_AUTOSTART=0.",
      "",
    ].join("\n"),
  );
}

async function runInstallCodex(args) {
  if (!hasCli("codex")) {
    process.stderr.write("error: 'codex' CLI not found on PATH. Install Codex first.\n");
    process.exit(1);
  }
  const setup = configureReflexioSetup(HOST_CODEX);
  const readOnly = setup.readOnly;

  const marketplaceRoot = copyCodexMarketplace();
  if (readOnly) {
    prunePublishHooksForReadOnly(codexMarketplacePluginRoot(marketplaceRoot));
  }
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

  for (const feature of ["hooks", "plugin_hooks"]) {
    code = await runCodex(["features", "enable", feature]);
    if (code !== 0) {
      // Older Codex builds may not recognize the `hooks` feature name; fall
      // through to writing the flag directly under [features] in config.toml.
      try {
        setTomlFeature(feature, true);
        process.stdout.write(`Enabled Codex ${feature} via ${CODEX_CONFIG_PATH}.\n`);
      } catch (err) {
        process.stderr.write(
          `error: could not enable Codex ${feature} feature: ${err && err.message ? err.message : err}\n`,
        );
        process.exit(code);
      }
    }
  }

  let cacheDir = null;
  let trustedHookCount = 0;
  let trustError = null;
  try {
    cacheDir = installCodexPluginCache(codexMarketplacePluginRoot(marketplaceRoot));
    process.stdout.write(`Installed Codex plugin cache at ${cacheDir}.\n`);
    await bootstrapPluginRuntime(cacheDir, { readOnly });
    if (readOnly) {
      process.stdout.write("Installed read-only hook manifest; publish interactions hooks are disabled.\n");
    }
    await startAndReportServices(cacheDir, HOST_CODEX, setup);
  } catch (err) {
    process.stderr.write(
      `error: automatic Codex plugin install failed: ${err && err.message ? err.message : err}\n`,
    );
    process.stderr.write(
      `Open Codex, run /plugins, install claude-smart from the ${CODEX_MARKETPLACE_DISPLAY_NAME} marketplace, and restart Codex.\n`,
    );
    process.exit(1);
  }

  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      trustedHookCount = await trustCodexPluginHooks(process.cwd());
      trustError = null;
      break;
    } catch (err) {
      trustError = err;
      if (attempt === 0) await new Promise((r) => setTimeout(r, 500));
    }
  }
  if (trustError) {
    process.stderr.write(
      `warning: ${trustError && trustError.message ? trustError.message : trustError}\n`,
    );
    process.stderr.write(
      `Fully quit and reopen Codex in this repo, run /hooks, trust the claude-smart hooks, and restart Codex.\n`,
    );
    process.exit(1);
  } else {
    process.stdout.write(`Trusted and enabled ${trustedHookCount} claude-smart Codex hooks.\n`);
  }

  process.stdout.write(
    [
      "",
      "claude-smart Codex support is installed.",
      `Restart Codex so the installed plugin and trusted hooks reload. /plugins should show claude-smart as installed from the ${CODEX_MARKETPLACE_DISPLAY_NAME} marketplace.`,
      "Local data is shared with Claude Code under ~/.reflexio/ and ~/.claude-smart/.",
      "",
    ].join("\n"),
  );
}

async function runInstallOpenCode(args) {
  const prerequisiteError = opencodePrerequisiteError();
  if (prerequisiteError) {
    process.stderr.write(prerequisiteError);
    process.exit(1);
  }
  const setup = configureReflexioSetup(HOST_OPENCODE);
  const readOnly = setup.readOnly;
  persistOpenCodePath();
  if (!hasExtractionProvider()) {
    process.stderr.write(extractionProviderError());
    process.exit(1);
  }

  let packageRoot;
  try {
    packageRoot = installOpenCodePluginPackage();
  } catch (err) {
    process.stderr.write(
      `error: could not prepare claude-smart OpenCode package: ${err && err.message ? err.message : err}\n`,
    );
    process.exit(1);
  }
  const pluginRoot = join(packageRoot, "plugin");
  const pluginSpec = opencodeLocalPluginSpec(packageRoot);
  let result;
  try {
    await bootstrapPluginRuntime(pluginRoot, { readOnly, patchCodexHooks: false });
  } catch (err) {
    process.stderr.write(
      `error: claude-smart OpenCode setup failed during dependency bootstrap: ${err && err.message ? err.message : err}\n`,
    );
    process.exit(1);
  }
  try {
    result = patchOpenCodePluginConfig(opencodeConfigPath(args), {
      install: true,
      pluginSpec,
    });
  } catch (err) {
    process.stderr.write(`error: could not update OpenCode config: ${err && err.message ? err.message : err}\n`);
    stopClaudeSmartServices(pluginRoot);
    process.exit(1);
  }
  commitLocalPluginPackage(packageRoot);
  if (readOnly) {
    process.stdout.write("Installed read-only hook manifest; publish interactions hooks are disabled.\n");
  }
  await startAndReportServices(pluginRoot, HOST_OPENCODE, setup);
  if (result.backupPath) {
    process.stdout.write(`Saved a comment-preserving backup of your previous config at ${result.backupPath}.\n`);
  }
  process.stdout.write(
    [
      "",
      `${result.changed ? "Updated" : "OpenCode config already includes"} "${pluginSpec}" in ${result.configPath}.`,
      `Prepared claude-smart OpenCode package at ${packageRoot}.`,
      "claude-smart OpenCode support is installed.",
      "Restart OpenCode in your project so it loads the plugin.",
      "",
    ].join("\n"),
  );
}

async function runUninstallCodex() {
  stopClaudeSmartServices(join(PACKAGE_ROOT, "plugin"));
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
      "Codex's global hook feature flags were left in place.",
      ...LOCAL_DATA_NOTICE,
      "",
    ].join("\n"),
  );
}

async function runUninstallOpenCode(args) {
  stopClaudeSmartServices(join(PACKAGE_ROOT, "plugin"));
  stopClaudeSmartServices(join(OPENCODE_LOCAL_PACKAGE_DIR, "plugin"));
  let result;
  try {
    result = patchOpenCodePluginConfig(opencodeConfigPath(args), { install: false });
  } catch (err) {
    process.stderr.write(`error: could not update OpenCode config: ${err && err.message ? err.message : err}\n`);
    process.exit(1);
  }
  if (result.backupPath) {
    process.stdout.write(`Saved a comment-preserving backup of your previous config at ${result.backupPath}.\n`);
  }
  rmSync(OPENCODE_LOCAL_PACKAGE_DIR, { recursive: true, force: true });
  repairPluginRoot();
  try {
    rmdirSync(dirname(OPENCODE_LOCAL_PACKAGE_DIR));
  } catch {
    // Keep the parent when it still contains future OpenCode state.
  }
  process.stdout.write(
    [
      "",
      result.changed
        ? `Removed claude-smart OpenCode plugin entries from ${result.configPath}.`
        : "OpenCode config did not include claude-smart.",
      "Restart OpenCode to apply.",
      ...LOCAL_DATA_NOTICE,
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

  if (cmd === "install" || cmd === "update" || cmd === "setup") {
    // Service scripts run from the plugin dir; the dashboard they start must
    // still edit the project install was run from (dashboard-service.sh).
    if (!process.env.CLAUDE_SMART_DASHBOARD_WORKSPACE) {
      process.env.CLAUDE_SMART_DASHBOARD_WORKSPACE = process.cwd();
    }
  }

  if (cmd === "install") {
    await runInstall(args.slice(1));
    return;
  }

  if (cmd === "update") {
    await runUpdate(args.slice(1));
    return;
  }

  if (cmd === "setup") {
    await runSetup(args.slice(1));
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

if (require.main === module) {
  main().catch((err) => {
    process.stderr.write(`claude-smart: ${err && err.message ? err.message : err}\n`);
    process.exit(1);
  });
}

module.exports = {
  assertSupportedRuntimePlatform,
  bootstrapPluginRuntime,
  codexMarketplacePluginRoot,
  copyCodexMarketplace,
  ensurePrivateNode,
  ensureUv,
  configureReflexioSetup,
  patchCodexHooksForNode,
  opencodeConfigPath,
  opencodeLocalPluginSpec,
  installOpenCodePluginPackage,
  patchOpenCodePluginConfig,
  parseHost,
  hasExtractionProvider,
  hasOpenCodeCli,
  persistOpenCodePath,
  resolveOpenCodePath,
  opencodePrerequisiteError,
  platformSupportError,
  prunePublishHooksForReadOnly,
  restorePublishHooksFromSource,
  stripJsonc,
  terminateActiveChildren,
  trackChild,
  isBundledBackendUrl,
};
