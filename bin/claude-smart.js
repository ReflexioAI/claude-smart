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
 * Guided install/setup selects integrations and shared backend settings before
 * invoking native per-agent workers. The saved learning provider is independent
 * of integration identity; workers defer shared service startup to the guide.
 *
 * Keep this file dependency-free — it runs via `npx` with no install step.
 */
"use strict";

const { execSync, spawn, spawnSync } = require("child_process");
const crypto = require("crypto");
const {
  chmodSync,
  closeSync,
  fstatSync,
  openSync,
  readSync,
  constants,
  cpSync,
  existsSync,
  accessSync,
  copyFileSync,
  lstatSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  readlinkSync,
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
const { basename, dirname, extname, isAbsolute, join, relative, resolve, sep } = require("path");
const { fileURLToPath, pathToFileURL } = require("url");
const guide = require("./install-guide");

const PLUGIN_SPEC = "claude-smart@reflexioai";
const CODEX_MARKETPLACE_NAME = "reflexioai";
const CODEX_MARKETPLACE_DISPLAY_NAME = "ReflexioAI";
const CODEX_PLUGIN_ID = `claude-smart@${CODEX_MARKETPLACE_NAME}`;
const OPENCODE_BARE_PLUGIN_SPEC = "claude-smart";
const OPENCODE_CONFIG_NAMES = ["opencode.json", "opencode.jsonc"];
// Pre-#85 installs kept claude-smart's managed settings here. It is also the
// env file of any other Reflexio tool on the machine, so it is only ever read
// for a one-time migration (see captureInstallSnapshot), never written.
const LEGACY_REFLEXIO_ENV_PATH = join(homedir(), ".reflexio", ".env");
const CLAUDE_SMART_ENV_PATH = join(homedir(), ".claude-smart", ".env");
const MANAGED_REFLEXIO_URL = "https://www.reflexio.ai/";
const CLAUDE_SMART_READ_ONLY_ENV = "CLAUDE_SMART_READ_ONLY";
const CLAUDE_SMART_USE_LOCAL_CLI_ENV = "CLAUDE_SMART_USE_LOCAL_CLI";
const CLAUDE_SMART_HOST_ENV = "CLAUDE_SMART_HOST";
const LEARNING_PROVIDER_ENV = "CLAUDE_SMART_LEARNING_PROVIDER";
const CLAUDE_SMART_OPENCODE_PATH_ENV = "CLAUDE_SMART_OPENCODE_PATH";
const REFLEXIO_USER_ID_ENV = "REFLEXIO_USER_ID";
const HOST_CLAUDE_CODE = "claude-code";
const HOST_CODEX = "codex";
const HOST_OPENCODE = "opencode";
const SUPPORTED_HOSTS = [HOST_CLAUDE_CODE, HOST_CODEX, HOST_OPENCODE];
const DEFAULT_CLAUDE_SMART_HOST = HOST_CLAUDE_CODE;
const REFLEXIO_DIR = join(homedir(), ".reflexio");
const CLAUDE_SMART_STATE_DIR = join(homedir(), ".claude-smart");
const GUIDED_RUNTIME_DIR = join(CLAUDE_SMART_STATE_DIR, "guided-runtimes");
const GUIDE_INSTALL_LOCK = join(GUIDED_RUNTIME_DIR, "guide");
const GUIDED_SELECTED_RUNTIME = join(GUIDED_RUNTIME_DIR, "selected");
const GUIDED_PENDING_SHUTDOWN = join(GUIDED_RUNTIME_DIR, "pending-shutdown.json");
const GUIDED_PENDING_STARTUP = join(GUIDED_RUNTIME_DIR, "pending-startup");
const GUIDED_CODEX_HOMES = join(GUIDED_RUNTIME_DIR, "codex-homes");
const GUIDED_OPENCODE_REGISTRATIONS = join(GUIDED_RUNTIME_DIR, "opencode-registrations");
const GUIDED_RUNTIME_BINDING = "guided-runtime-id";
const INSTALL_FAILURE_MARKER = join(CLAUDE_SMART_STATE_DIR, "install-failed");
// Written the first time install considers ~/.reflexio/.env, whatever the
// outcome, so the migration runs once and a later local-mode choice sticks.
// New setup runs record completion in the settings header, in the same commit.
const LEGACY_ENV_MIGRATION_MARKER = join(CLAUDE_SMART_STATE_DIR, "legacy-reflexio-env-checked");
const INSTALL_COMPLETION_COMMENT = "# claude-smart: legacy-env checked";
const OPENCODE_LOCAL_PACKAGE_DIR = join(CLAUDE_SMART_STATE_DIR, "opencode", "claude-smart");
// Claude Code loads plugins from a local-directory marketplace in place
// (<marketplace>/plugin), so this copy IS the runtime root, not a staging area.
const CLAUDE_CODE_LOCAL_PACKAGE_DIR = join(CLAUDE_SMART_STATE_DIR, "claude-code", "claude-smart");
const CLAUDE_SMART_VENVS_DIR = join(CLAUDE_SMART_STATE_DIR, "venvs");
const OPENCODE_PACKAGE_LOCK_TIMEOUT_MS = 120_000;
const OPENCODE_PACKAGE_LOCK_STALE_MS = 10 * 60_000;
const CODEX_HOME = (() => {
  const configured = resolve((process.env.CODEX_HOME || "").trim() || join(homedir(), ".codex"));
  try {
    return realpathSync(configured);
  } catch {
    return configured;
  }
})();
const CODEX_CONFIG_PATH = join(CODEX_HOME, "config.toml");
// Codex validates every entry in a plugin package and rejects dependency
// symlinks. Keep the mutable runtime outside plugins/, scoped to this account.
const CODEX_RUNTIME_PACKAGE_DIR = join(CODEX_HOME, "claude-smart", "runtime");
const CODEX_RUNTIME_PLUGIN_ROOT = join(CODEX_RUNTIME_PACKAGE_DIR, "plugin");
const PACKAGE_ROOT = dirname(dirname(__filename));
// Account-owned staging must never replace Claude's legacy marketplace runtime.
const CODEX_MARKETPLACE_DIR = join(CODEX_HOME, "claude-smart", "marketplace");
const CODEX_MARKETPLACE_PLUGIN_PATH = "plugin";
const CODEX_PLUGIN_CACHE_DIR = join(
  CODEX_HOME,
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
  "plugin/scripts/opencode-claude-compat",
  "plugin/scripts/opencode-claude-compat.cmd",
  "plugin/scripts/opencode-claude-compat.js",
  "plugin/scripts/codex-hook.js",
  "plugin/scripts/_codex_env.sh",
];
const CODEX_CLI_TIMEOUT_MS = 30_000;
const PLUGIN_SERVICE_TIMEOUT_MS = 45_000;
// A publisher phase owns no interactive login; stop a stalled dependency/auth
// helper or retained pipe, and require a fresh preview after an uncertain save.
const SETTINGS_PUBLISHER_TIMEOUT_MS = 120_000;
const COPYTREE_IGNORE_NAMES = new Set([
  "__pycache__",
  ".venv",
  ".pytest_cache",
  ".ruff_cache",
  ".git",
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
  if (process.env.CLAUDE_SMART_INSTALL_CHILD === "1" && spinnerLabel) {
    process.stdout.write(`[phase] ${spinnerLabel}\n`);
  }
  const useSpinner = Boolean(spinnerLabel) && process.stdout.isTTY && !process.env.CI;
  return new Promise((resolve) => {
    // While a package install is uncommitted, run the step in its own
    // process group (as runChecked does) so an interrupt can stop and await
    // every descendant before the rollback. These steps are non-interactive;
    // stdin is not passed through there, since a background group reading
    // the terminal would be stopped.
    const group = !isWindows() && rollbackArmed();
    const stdin = group ? "ignore" : "inherit";
    const child = spawn("claude", args, {
      stdio: useSpinner ? [stdin, "pipe", "pipe"] : [stdin, "inherit", "inherit"],
      detached: group,
    });
    trackChild(child, group);

    if (useSpinner) {
      const frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
      let i = 0;
      let spinTimer = null;
      let rearmTimer = null;
      let exited = false;

      const draw = () => {
        i = (i + 1) % frames.length;
        process.stdout.write(`\r⠿ ${spinnerLabel}`.replace("⠿", frames[i]));
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

function codexHookProcessCleanupError() {
  const release = learningProviderVersion({ provider: HOST_CODEX });
  const version = release?.match(/^codex-cli (\d+)\.(\d+)\.\d+(?:\+\S+)?$/);
  if (!version || (Number(version[1]) === 0 && Number(version[2]) < 148)) {
    return "Codex CLI 0.148 or newer is required for service hook process cleanup. Update Codex, then retry setup.";
  }
  return null;
}

function runCodex(args) {
  return new Promise((resolve) => {
    // Grouped while an install can still roll back (see runChecked).
    const group = !isWindows() && rollbackArmed();
    const child = spawn("codex", args, {
      stdio: [group ? "ignore" : "inherit", "inherit", "inherit"],
      detached: group,
    });
    trackChild(child, group);
    let timedOut = false;
    // On timeout, stop the whole tree (not just the leader) and wait for it
    // before the caller retries or rolls back.
    const timer = setTimeout(async () => {
      timedOut = true;
      process.stderr.write(
        `error: codex ${args.join(" ")} timed out after ${CODEX_CLI_TIMEOUT_MS / 1000}s\n`,
      );
      await terminateChildren([[child, group]]);
      if (!terminatingOnSignal) resolve(124);
    }, CODEX_CLI_TIMEOUT_MS);
    child.on("exit", (code) => {
      if (timedOut || terminatingOnSignal) return;
      clearTimeout(timer);
      resolve(typeof code === "number" ? code : 1);
    });
    child.on("error", () => {
      clearTimeout(timer);
      if (!terminatingOnSignal) resolve(1);
    });
  });
}

function parseEnvLine(line) {
  let trimmed = String(line || "").trim();
  if (!trimmed || trimmed.startsWith("#")) return null;
  trimmed = trimmed.replace(/^export\s+/, "");
  const eq = trimmed.indexOf("=");
  if (eq < 0) return null;
  const key = trimmed.slice(0, eq).trim();
  if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)) return null;
  let value = trimmed.slice(eq + 1).trim();
  // Match the SDK's dotenv treatment of a commented empty retired setting.
  if (key === "CLAUDE_SMART_OPENCODE_VARIANT" &&
      /^(?:"\s*"|'\s*')\s*(?:#.*)?$/.test(value)) value = "";
  if (
    value.length >= 2 &&
    ((value[0] === '"' && value[value.length - 1] === '"') ||
      (value[0] === "'" && value[value.length - 1] === "'"))
  ) {
    value = value.slice(1, -1);
  }
  return { key, value };
}

// The env file holds the managed API key: make it 0600 before any content
// lands in it, so no other account can read it between write and chmod.
function writePrivateFile(path, content) {
  if (existsSync(path)) chmodSync(path, 0o600);
  writeFileSync(path, content, { mode: 0o600 });
  chmodSync(path, 0o600);
}

// Match _lib.sh: caller-first flags retain their first nonempty saved value;
// shared connection/learning fields retain the last assignment, even empty.
const CALLER_FIRST_ENV_KEYS = new Set([
  "CLAUDE_SMART_USE_LOCAL_CLI",
  "CLAUDE_SMART_HOST",
  "CLAUDE_SMART_BACKEND_AUTOSTART",
  "CLAUDE_SMART_DASHBOARD_AUTOSTART",
  "CLAUDE_SMART_CLI_PATH",
  "CLAUDE_SMART_OPENCODE_PATH",
  "CLAUDE_SMART_CLI_TIMEOUT",
  "CLAUDE_SMART_STATE_DIR",
]);

function readEnvSnapshot(path) {
  const exists = existsSync(path);
  const bytes = exists ? readFileSync(path) : Buffer.alloc(0);
  const values = new Map();
  const rows = new Map();
  const lines = new TextDecoder("utf-8", { fatal: true })
    .decode(bytes)
    .split("\n");
  for (const [index, line] of lines.entries()) {
    const parsed = parseEnvLine(line);
    if (
      parsed &&
      (!CALLER_FIRST_ENV_KEYS.has(parsed.key) || !values.get(parsed.key))
    ) {
      values.set(parsed.key, parsed.value);
      rows.set(parsed.key, index);
    }
  }
  return { exists, bytes, values, rows, lines };
}

function readEnvFile(path) {
  return readEnvSnapshot(path).values;
}

function captureInstallSnapshot() {
  const runtime = readEnvSnapshot(CLAUDE_SMART_ENV_PATH);
  const marker = existsSync(LEGACY_ENV_MIGRATION_MARKER);
  const checked =
    marker ||
    runtime.lines[0].replace(/\r$/, "") === INSTALL_COMPLETION_COMMENT;
  return {
    runtime,
    marker,
    checked,
    legacy: checked ? null : readEnvSnapshot(LEGACY_REFLEXIO_ENV_PATH),
  };
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

// An endpoint and its credential are one fallback decision. Even an empty
// runtime assignment is deliberate; never fill its other half from another file.
function legacyManagedConnection(runtime, legacy) {
  if (runtime.has("REFLEXIO_URL") || runtime.has("REFLEXIO_API_KEY")) return new Map();
  const apiKey = (legacy.get("REFLEXIO_API_KEY") || "").trim();
  const url = (legacy.get("REFLEXIO_URL") || "").trim() || MANAGED_REFLEXIO_URL;
  if (!apiKey || isLoopbackUrl(url)) return new Map();
  const connection = new Map([
    ["REFLEXIO_URL", url],
    ["REFLEXIO_API_KEY", apiKey],
  ]);
  if (!runtime.has(REFLEXIO_USER_ID_ENV) && legacy.has(REFLEXIO_USER_ID_ENV))
    connection.set(REFLEXIO_USER_ID_ENV, legacy.get(REFLEXIO_USER_ID_ENV));
  return connection;
}

// Workers consume confirmed policy. Missing defaults belong to the prepared
// Python repair owner, never to another integration's installation decision.
function readInstallPolicy() {
  const values = readEnvFile(CLAUDE_SMART_ENV_PATH);
  const url = values.get("REFLEXIO_URL");
  const apiKey = values.get("REFLEXIO_API_KEY");
  const invalid = () =>
    new Error(
      "Saved installation policy is incomplete or invalid; run npx claude-smart setup first.",
    );
  if (!url || apiKey === undefined) throw invalid();
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    throw invalid();
  }
  if (!["http:", "https:"].includes(parsed.protocol)) throw invalid();
  const managed = isRemoteReflexioUrl(url);
  if (managed && !apiKey.trim()) throw invalid();
  for (const key of [
    "REFLEXIO_URL",
    "REFLEXIO_API_KEY",
    "REFLEXIO_USER_ID",
    "CLAUDE_SMART_READ_ONLY",
    LEARNING_PROVIDER_ENV,
    "CLAUDE_SMART_LEARNING_MODEL",
  ]) {
    if (values.has(key)) process.env[key] = values.get(key);
  }
  // Same nonempty-caller precedence as the runtime. Reading never publishes.
  for (const key of CALLER_FIRST_ENV_KEYS) {
    if (!process.env[key] && values.has(key)) process.env[key] = values.get(key);
  }
  const readOnly = ["1", "true", "yes", "on"].includes(
    (values.get(CLAUDE_SMART_READ_ONLY_ENV) || "").trim().toLowerCase(),
  );
  return {
    managed,
    url,
    readOnly,
    provider: values.get(LEARNING_PROVIDER_ENV) || values.get(CLAUDE_SMART_HOST_ENV),
  };
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
        while (index + 1 < text.length && !(text[index] === "*" && text[index + 1] === "/"))
          index += 1;
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

function parseJsoncObject(text, path) {
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

// Every config file OpenCode reads for the selected scope, existing or not.
// OpenCode merges all of them rather than picking one, so a claude-smart entry
// in any of them is loaded. Mirrors the Python CLI's _opencode_scope_config_paths.
function opencodeScopeConfigPaths(args = [], cwd = process.cwd()) {
  const roots = parseGlobalFlag(args) ? [opencodeGlobalConfigDir()] : [cwd, join(cwd, ".opencode")];
  return roots.flatMap((root) => OPENCODE_CONFIG_NAMES.map((name) => join(root, name)));
}

function opencodePluginSpec(entry) {
  if (typeof entry === "string") return entry;
  if (Array.isArray(entry) && typeof entry[0] === "string") return entry[0];
  // OpenCode also accepts the object form, { package, options }.
  if (entry && typeof entry === "object" && typeof entry.package === "string") return entry.package;
  return null;
}

function opencodeLocalPluginSpec(packageRoot = OPENCODE_LOCAL_PACKAGE_DIR) {
  return pathToFileURL(packageRoot).href;
}

const OPENCODE_V2_PLUGIN_MARKER = "claude-smart OpenCode V2 loader";

// OpenCode scans `plugin`/`plugins` in every config root it discovers: the user
// config directory always, and each project's `.opencode` directory. A project
// install therefore writes its loader into the project so the default install
// cannot capture prompts from unrelated projects.
function opencodeV2PluginFile(args = [], cwd = process.cwd()) {
  return parseGlobalFlag(args)
    ? join(opencodeGlobalConfigDir(), "plugins", "claude-smart.js")
    : join(cwd, ".opencode", "plugins", "claude-smart.js");
}

function opencodeV2ServerURL(packageRoot = OPENCODE_LOCAL_PACKAGE_DIR) {
  return pathToFileURL(join(packageRoot, "plugin", "opencode", "dist", "server.mjs")).href;
}

function opencodeV2PluginBody(packageRoot = OPENCODE_LOCAL_PACKAGE_DIR) {
  const entry = opencodeV2ServerURL(packageRoot);
  return [
    `// ${OPENCODE_V2_PLUGIN_MARKER}`,
    "// OpenCode V2 auto-loads this directory. Reinstall claude-smart to refresh.",
    `export { default } from ${JSON.stringify(entry)};`,
    "",
  ].join("\n");
}

function isGeneratedOpenCodeV2Loader(text) {
  const lines = String(text).split("\n");
  if (lines.length !== 4 || lines[3] !== "") return false;
  if (lines[0] !== `// ${OPENCODE_V2_PLUGIN_MARKER}`) return false;
  if (lines[1] !== "// OpenCode V2 auto-loads this directory. Reinstall claude-smart to refresh.")
    return false;
  return /^export \{ default \} from "file:\/\/.+";$/.test(lines[2]);
}

function openCodeV2LoaderConflicts(args = [], cwd = process.cwd()) {
  const path = opencodeV2PluginFile(args, cwd);
  try {
    const [loader] = snapshotOpenCodeScope([path]);
    return loader.contents !== null && !isGeneratedOpenCodeV2Loader(loader.contents)
      ? "It is not a claude-smart loader" : null;
  } catch (error) {
    if (/utf-8.*codec/i.test(error.message)) return "It is not a claude-smart loader";
    return `It cannot be read as a file (${error.message})`;
  }
}

function writeOpenCodeV2Plugin(args = [], packageRoot = OPENCODE_LOCAL_PACKAGE_DIR,
  cwd = process.cwd(), beforePublish = () => {}) {
  const path = opencodeV2PluginFile(args, cwd);
  try {
    const [loader] = snapshotOpenCodeScope([path]);
    if (loader.contents !== null && !isGeneratedOpenCodeV2Loader(loader.contents))
      return { path, status: "conflict" };
    const replacement = opencodeV2PluginBody(packageRoot);
    const edits = [{ path, ...loader, replacement }];
    const python = requireOpenCodeScopeInterpreter();
    runScopedOpenCode(null, "check", edits, python);
    beforePublish();
    runScopedOpenCode(null, "publish", edits, python);
    return { path, status: replacement === loader.contents ? "unchanged" : "wrote" };
  } catch {
    return { path, status: "conflict" };
  }
}

function removeOpenCodeV2Plugin(args = [], cwd = process.cwd()) {
  const path = opencodeV2PluginFile(args, cwd);
  try {
    const [loader] = snapshotOpenCodeScope([path]);
    if (!isGeneratedOpenCodeV2Loader(loader.contents)) return false;
    runScopedOpenCode(null, "publish", [{ path, ...loader, replacement: null }], requireOpenCodeScopeInterpreter());
    return true;
  } catch {
    return false;
  }
}

function scopeHasClaudeSmart(args = [], cwd = process.cwd()) {
  let unresolved = false;
  try {
    const [loader] = snapshotOpenCodeScope([opencodeV2PluginFile(args, cwd)]);
    if (isGeneratedOpenCodeV2Loader(loader.contents)) return true;
  } catch {
    unresolved = true;
  }
  try {
    const configs = snapshotOpenCodeScope(opencodeScopeConfigPaths(args, cwd));
    for (const { contents } of configs) {
      if (!contents) continue;
      try {
        const data = JSON.parse(stripJsonc(contents));
        if (["plugin", "plugins"].some((key) => Array.isArray(data[key]) &&
          data[key].some((entry) => isOpenCodeClaudeSmartSpec(opencodePluginSpec(entry)))))
          return true;
      } catch {
        unresolved = true;
      }
    }
    return unresolved ? null : false;
  } catch {
    return null;
  }
}

function isOpenCodeClaudeSmartSpec(spec) {
  if (!spec) return false;
  if (spec === OPENCODE_BARE_PLUGIN_SPEC || spec.startsWith(`${OPENCODE_BARE_PLUGIN_SPEC}@`)) {
    return true;
  }
  if (!spec.startsWith("file://")) return false;
  // Legacy installer-owned entries identified the stable account package.
  // A project config does not authorize inspecting arbitrary plugin manifests.
  const packagePath = localOpenCodeFilePath(spec);
  return packagePath !== null && resolve(packagePath) === resolve(OPENCODE_LOCAL_PACKAGE_DIR);
}

function openCodeScopeInterpreter() {
  for (const pluginRoot of [activePluginRoot(), join(OPENCODE_LOCAL_PACKAGE_DIR, "plugin")]) {
    if (!pluginRoot) continue;
    const python = pluginPythonPath(pluginRoot);
    if (isExecutableFile(python)) return python;
  }
  return resolveCommand(isWindows() ? ["python.exe", "python3.exe"] : ["python3", "python"]);
}

function requireOpenCodeScopeInterpreter() {
  const python = openCodeScopeInterpreter();
  if (!python) throw new Error("A prepared runtime or Python is required for the OpenCode scope");
  return python;
}

function snapshotOpenCodeScope(paths) {
  return runScopedOpenCode(null, "snapshot", paths, requireOpenCodeScopeInterpreter());
}

function runScopedOpenCode(pluginRoot, action, request, python) {
  const ownerRoot = pluginRoot || join(PACKAGE_ROOT, "plugin");
  const owner = join(ownerRoot, "src", "claude_smart", "opencode_scope.py");
  if (!existsSync(owner)) throw new Error("could not prepare claude-smart OpenCode package: missing scope owner");
  const args = ["-I", owner, action];
  const result = spawnSync(
    python || pluginPythonPath(pluginRoot), args,
    {
      cwd: pluginRoot || PACKAGE_ROOT,
      env: runtimeEnv(),
      input: Buffer.from(JSON.stringify(request), "utf8"),
      encoding: "utf8",
      timeout: 20000,
      maxBuffer: 32 * 1024 * 1024,
      windowsHide: true,
    },
  );
  if (result.status !== 0)
    throw new Error(`Could not ${action} the OpenCode scope: ${result.stderr?.trim() || result.error?.message || "owner failed"}`);
  return JSON.parse(result.stdout);
}

function readScopedOpenCodeInstallation(args, pluginRoot, python, removing = false) {
  const path = opencodeV2PluginFile(args);
  const configs = opencodeScopeConfigPaths(args);
  const [loader, ...sources] = runScopedOpenCode(pluginRoot, "snapshot", [path, ...configs], python);
  if (!removing && loader.contents !== null && !isGeneratedOpenCodeV2Loader(loader.contents))
    throw new Error(`It is not a claude-smart loader at ${path}; move it aside and retry.`);
  return { path, configs, loader, sources };
}

function planScopedOpenCode(args, packageRoot, python) {
  const removing = packageRoot === null;
  const pluginRoot = removing ? null : join(packageRoot, "plugin");
  const { path, configs, loader, sources } = readScopedOpenCodeInstallation(args, pluginRoot, python, removing);
  const { edits, results } = prepareOpenCodeConfigEdits(
    configs, sources,
    (paths) => runScopedOpenCode(pluginRoot, "snapshot", paths, python),
    { install: false },
  );
  if (!removing || isGeneratedOpenCodeV2Loader(loader.contents))
    edits.push({ path, ...loader, replacement: removing ? null : opencodeV2PluginBody(packageRoot) });
  runScopedOpenCode(pluginRoot, "check", edits, python);
  return { loader: { path, ...loader }, edits, results };
}

function parseOpenCodePluginConfig(configPath, source) {
  const data = parseJsoncObject(source, configPath);
  for (const field of ["plugins", "plugin"]) {
    if (data[field] !== undefined && !Array.isArray(data[field])) {
      throw new Error(`OpenCode config ${configPath} field "${field}" must be a JSON array`);
    }
  }
  return data;
}

function planOpenCodePluginConfig(configPath, source, { install, pluginSpec = null }) {
  const data = parseOpenCodePluginConfig(configPath, source);
  const current = [...(data.plugin ?? []), ...(data.plugins ?? [])];
  const kept = current.filter((entry) => !isOpenCodeClaudeSmartSpec(opencodePluginSpec(entry)));
  const next = install ? [...kept, pluginSpec || opencodeLocalPluginSpec()] : kept;
  const changed =
    data.plugins !== undefined ||
    (Array.isArray(data.plugin)
      ? next.length !== data.plugin.length ||
        next.some((entry, index) => entry !== data.plugin[index])
      : install && next.length > 0);
  if (!changed) return null;
  data.plugin = next;
  delete data.plugins;
  return {
    configPath,
    contents: JSON.stringify(data, null, 2) + "\n",
    backup: source?.trim() && source !== stripJsonc(source) ? source : null,
  };
}

function prepareOpenCodeConfigEdits(configs, sources, snapshot, options) {
  const edits = [];
  const results = [];
  for (const [index, configPath] of configs.entries()) {
    const source = sources[index];
    const plan = planOpenCodePluginConfig(configPath, source.contents, options);
    if (!plan) {
      edits.push({ path: configPath, ...source, replacement: source.contents });
      continue;
    }
    const backupPath = plan.backup === null ? null : `${configPath}.bak`;
    if (backupPath) {
      const [backup] = snapshot([backupPath]);
      edits.push({ path: backupPath, ...backup, replacement: plan.backup });
    }
    edits.push({ path: configPath, ...source, replacement: plan.contents });
    results.push({ changed: true, configPath, backupPath });
  }
  return { edits, results };
}

function editOpenCodeConfigs(configs, options) {
  const { edits, results } = prepareOpenCodeConfigEdits(
    configs, snapshotOpenCodeScope(configs), snapshotOpenCodeScope, options,
  );
  runScopedOpenCode(null, "publish", edits, requireOpenCodeScopeInterpreter());
  return results;
}

function patchOpenCodePluginConfig(configPath, options) {
  return editOpenCodeConfigs([configPath], options)[0] || { changed: false, configPath };
}

function removeOpenCodeLegacyEntries(args = [], cwd = process.cwd()) {
  return editOpenCodeConfigs(opencodeScopeConfigPaths(args, cwd), { install: false });
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
  const explicitPath = process.env[CLAUDE_SMART_OPENCODE_PATH_ENV] || "";
  // Windows normalizes trailing spaces on ordinary batch paths. Keep other
  // spellings if they identify an executable, including leading spaces.
  const batchPath = explicitPath.trimEnd();
  const exactPath = isWindows() && /\.(?:cmd|bat)$/i.test(batchPath) ? batchPath : explicitPath;
  const opencodePath = isExecutableFile(exactPath) ? exactPath : explicitPath.trim();
  if (opencodePath) return isExecutableFile(opencodePath) ? opencodePath : null;
  if (isWindows()) return resolveCommand(["opencode.cmd", "opencode.exe", "opencode"]);
  return resolvePosixOpenCodeCommand("opencode");
}

function resolvePosixOpenCodeCommand(name) {
  // The path spelling reaches exec intact. Resolving '..' lexically would
  // change the target when a parent component is a symlink.
  for (const directory of (process.env.PATH ?? "/bin:/usr/bin").split(":")) {
    const candidate = isAbsolute(directory)
      ? `${directory}/${name}`
      : `${process.cwd()}/${directory || "."}/${name}`;
    if (isExecutableFile(candidate)) return candidate;
  }
  return null;
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

// Guided installs own their selection. Already-loaded legacy integrations can
// still rewrite ~/.reflexio/plugin-root without changing the guided selection.
function activePluginRoot() {
  let selected;
  try {
    selected = lstatSync(GUIDED_SELECTED_RUNTIME);
  } catch (error) {
    if (error?.code !== "ENOENT") return null;
  }
  if (selected) {
    try {
      if (!selected.isFile()) return null;
      const { pluginRoot } = readGuidedRuntime(readFileSync(GUIDED_SELECTED_RUNTIME, "utf8"));
      return existsSync(join(pluginRoot, "scripts", "backend-service.sh")) ? pluginRoot : null;
    } catch {
      return null;
    }
  }
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
function installedPluginRoots(
  codexSurvivors = registeredCodexPluginRoots(),
  claudePlugins = claudeInstallations(),
) {
  // The host owns its effective load source. A local marketplace can report
  // readFromFolder alongside an older installPath; prefer its declared source.
  // Discovery is read-only and does not establish ownership for deletion.
  const roots = [];
  for (const plugin of claudePlugins) {
    if (
      plugin.errors !== undefined &&
      (!Array.isArray(plugin.errors) || plugin.errors.length)
    ) continue;
    const root = Object.hasOwn(plugin, "readFromFolder")
      ? plugin.readFromFolder
      : plugin.installPath;
    if (typeof root === "string" && isAbsolute(root)) roots.push(root);
  }
  roots.push(join(CLAUDE_CODE_LOCAL_PACKAGE_DIR, "plugin"));
  roots.push(...codexSurvivors);
  roots.push(...openCodeGuidedPluginRoots());
  roots.push(join(OPENCODE_LOCAL_PACKAGE_DIR, "plugin"), CODEX_RUNTIME_PLUGIN_ROOT);
  try {
    const legacy = readdirSync(CODEX_PLUGIN_CACHE_DIR)
      .map((name) => join(CODEX_PLUGIN_CACHE_DIR, name))
      .filter((root) => !pathEntryExists(join(root, GUIDED_RUNTIME_BINDING)))
      .sort((a, b) => compareSemverLikePathNames(b, a));
    roots.push(...legacy);
  } catch {
    // No standalone Codex cache.
  }
  return roots.flatMap((root) => {
    const prepared = boundGuidedPluginRoot(root);
    if (prepared) return [prepared];
    return !pathEntryExists(join(root, GUIDED_RUNTIME_BINDING)) &&
      existsSync(join(root, "scripts", "backend-service.sh")) ? [root] : [];
  });
}

function pluginRootIsBroken() {
  if (pathEntryExists(GUIDED_SELECTED_RUNTIME)) return !activePluginRoot();
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
function clearPluginRoot() {
  rmSync(GUIDED_SELECTED_RUNTIME, { force: true });
  rmSync(join(REFLEXIO_DIR, "plugin-root"), { force: true });
  rmSync(join(REFLEXIO_DIR, "plugin-root.txt"), { force: true });
}

function isGuidedPluginRoot(root) {
  try {
    return readGuidedRuntime(basename(dirname(root))).pluginRoot === root;
  } catch {
    return false;
  }
}

function repairPluginRoot() {
  if (pluginRootIsBroken()) {
    const next = installedPluginRoots()[0];
    if (next) {
      forcePluginRoot(next);
      process.stdout.write(`Repointed ${join(REFLEXIO_DIR, "plugin-root")} to ${next}.\n`);
    } else {
      clearPluginRoot();
    }
  }
}

function hostForPluginRoot(root) {
  const real = (path) => {
    try {
      return realpathSync(path);
    } catch {
      return path;
    }
  };
  const target = real(root);
  const under = (dir) => target === real(dir) || target.startsWith(`${real(dir)}${sep}`);
  if (under(OPENCODE_LOCAL_PACKAGE_DIR)) return HOST_OPENCODE;
  if (
    under(CODEX_RUNTIME_PACKAGE_DIR) ||
    under(CODEX_PLUGIN_CACHE_DIR) ||
    under(CODEX_MARKETPLACE_DIR)
  )
    return HOST_CODEX;
  return HOST_CLAUDE_CODE;
}

function forcePluginRoot(pluginRoot) {
  forceLegacyPluginRoot(pluginRoot);
  recordGuidedSelection(pluginRoot);
}

function recordGuidedSelection(pluginRoot) {
  if (isGuidedPluginRoot(pluginRoot)) {
    const launcher = readFileSync(join(pluginRoot, "scripts", "guided-command.sh"));
    writeGuidedRecord(join(GUIDED_RUNTIME_DIR, "guided-command.sh"), launcher);
    writeGuidedRecord(GUIDED_SELECTED_RUNTIME, basename(dirname(pluginRoot)));
  } else {
    rmSync(GUIDED_SELECTED_RUNTIME, { force: true });
  }
}

function forceLegacyPluginRoot(pluginRoot) {
  mkdirSync(REFLEXIO_DIR, { recursive: true });
  const link = join(REFLEXIO_DIR, "plugin-root");
  try {
    const existing = lstatSync(link);
    if (!existing.isSymbolicLink() && !existing.isFile())
      throw new Error(`refusing to replace non-symlink plugin-root at ${link}`);
  } catch (error) {
    if (error?.code !== "ENOENT") throw error;
  }
  const stagedLink = uniquePackagePath(link, ".plugin-root");
  const text = join(REFLEXIO_DIR, "plugin-root.txt");
  const stagedText = uniquePackagePath(text, ".plugin-root-text");
  try {
    // Junction replacement may require unlinking the old entry. Make the
    // compatibility file valid before crossing any point without a link.
    writeFileSync(stagedText, `${pluginRoot}\n`, { flag: "wx", mode: 0o600 });
    renameSync(stagedText, text);
    try {
      symlinkSync(pluginRoot, stagedLink, isWindows() ? "junction" : "dir");
      renameSync(stagedLink, link);
      return;
    } catch (error) {
      if (!["EACCES", "EPERM", "ENOTSUP", "EEXIST", "ENOTEMPTY"].includes(error?.code))
        throw error;
    }
    rmSync(link, { force: true });
    if (pathEntryExists(stagedLink)) {
      try {
        renameSync(stagedLink, link);
      } catch (error) {
        if (!["EACCES", "EPERM", "ENOTSUP"].includes(error?.code)) throw error;
      }
    }
  } finally {
    rmSync(stagedLink, { force: true });
    rmSync(stagedText, { force: true });
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
// Invocation-owned resources only; never an account/runtime selection.
const settingsRuntimes = new Set();
const privateNodeStages = new Map();

function cleanupPrivateNodeStage(directory, stage) {
  const { child, group } = stage;
  if (child?.pid && (
    (child.exitCode === null && child.signalCode === null) ||
    (group && processGroupAlive(child.pid))
  )) return;
  try {
    rmSync(directory, { recursive: true, force: true });
    privateNodeStages.delete(directory);
  } catch (error) {
    process.stderr.write(`Could not clean temporary Node staging at ${directory}: ${error.message}\n`);
  }
}

function cleanupPrivateNodeStages() {
  for (const [directory, stage] of privateNodeStages)
    cleanupPrivateNodeStage(directory, stage);
}

function cleanupSettingsRuntimes() {
  for (const runtime of settingsRuntimes) {
    if (
      runtime.children.some(
        (entry) =>
          !entry.hasClosed ||
          (entry.group && processGroupAlive(entry.child.pid)),
      )
    )
      continue;
    try {
      if (runtime.temporary) rmSync(runtime.directory, { recursive: true, force: true });
      settingsRuntimes.delete(runtime);
    } catch {
      process.stderr.write(
        "Temporary settings publisher cleanup failed: " +
          runtime.directory +
          "\n",
      );
    }
  }
}

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
  process.once("exit", cleanupSettingsRuntimes);
  process.once("exit", cleanupPrivateNodeStages);
  for (const [signal, code] of [
    ["SIGINT", 130],
    ["SIGTERM", 143],
    ["SIGHUP", 129],
  ]) {
    process.once(signal, async () => {
      terminatingOnSignal = true;
      if ([...settingsRuntimes].some((runtime) => runtime.publishing))
        process.stderr.write(
          "Settings save was interrupted and may have completed; reload before retrying.\n",
        );
      // Stop and await children first: an orphaned smart-install or Claude
      // CLI step must not keep writing after the rollback restores the
      // previous package.
      await terminateActiveChildren();
      // close follows pipe/handle release. Never delete under a child whose
      // termination cannot be confirmed; this directory holds no credentials.
      let timeout;
      await Promise.race([
        Promise.all(
          [...settingsRuntimes].flatMap((runtime) =>
            runtime.children.map((entry) => entry.closed),
          ),
        ),
        new Promise((resolve) => {
          timeout = setTimeout(resolve, 5000);
        }),
      ]);
      clearTimeout(timeout);
      cleanupSettingsRuntimes();
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

// Capture marker/root recovery and prior service context before preparation.
// Workers no longer rewrite saved HOST, so configuration needs no compensation.
// A leftover failure marker would suppress the restored working hooks.
let installStateSnapshot = null;

function snapshotInstallState({ preserveFailureMarker = false } = {}) {
  const read = (path) => (existsSync(path) ? readFileSync(path) : null);
  const host = readEnvFile(CLAUDE_SMART_ENV_PATH).get(CLAUDE_SMART_HOST_ENV) ?? null;
  installStateSnapshot = {
    failureMarker: preserveFailureMarker ? undefined : read(INSTALL_FAILURE_MARKER),
    // Bootstraps repoint the shared plugin-root before they finish.
    pluginRoot: activePluginRoot(),
    pluginRootBroken: pluginRootIsBroken(),
    restartHost: process.env.CLAUDE_SMART_HOST || host || HOST_CLAUDE_CODE,
  };
  ensureRollbackOnExit();
}

// True while an install can still roll back: children then run in their own
// process group so an interrupt can stop the whole tree first.
function rollbackArmed() {
  return heldPackageLocks.size > 0 || installStateSnapshot !== null;
}

function commitInstallState() {
  installStateSnapshot = null;
}

function restoreInstallState() {
  const snapshot = installStateSnapshot;
  installStateSnapshot = null;
  if (!snapshot) return true;
  try {
    if (
      snapshot.pluginRoot &&
      existsSync(join(snapshot.pluginRoot, "scripts", "backend-service.sh"))
    ) {
      forcePluginRoot(snapshot.pluginRoot);
    } else if (snapshot.pluginRootBroken || snapshot.pluginRoot) {
      repairPluginRoot();
    } else {
      // A failed first install must not leave its newly published root selected.
      clearPluginRoot();
    }
    if (snapshot.failureMarker === null) rmSync(INSTALL_FAILURE_MARKER, { force: true });
    else if (snapshot.failureMarker !== undefined)
      writeFileSync(INSTALL_FAILURE_MARKER, snapshot.failureMarker);
  } catch (err) {
    process.stderr.write(
      `warning: could not restore claude-smart install state: ${err && err.message ? err.message : err}\n`,
    );
    return false;
  }
  return true;
}

function rollbackLocalPluginPackages() {
  let restored = true;
  for (const [packageRoot, { backupPackage, installedIno }] of pendingPreviousPackages) {
    pendingPreviousPackages.delete(packageRoot);
    try {
      if (!pathEntryExists(packageRoot) || lstatSync(packageRoot).ino !== installedIno) {
        restored = false;
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
      // Older envs can still be referenced by cached hooks. Only the failed
      // install's env is disposable during rollback.
      if (packageRoot === CLAUDE_CODE_LOCAL_PACKAGE_DIR) {
        removeClaudeCodeVenv(join(packageRoot, "plugin", ".venv"));
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
      }
    } catch (err) {
      restored = false;
      process.stderr.write(
        `warning: could not restore the previous claude-smart package from ${backupPackage}: ` +
          `${err && err.message ? err.message : err}\n`,
      );
    }
  }
  restored = restoreInstallState() && restored;
  for (const packageRoot of [...heldPackageLocks.keys()]) releasePackageInstallLock(packageRoot);
  if (restored) restartStoppedServices();
  else {
    stoppedServices = null;
    process.stderr.write(
      "warning: package recovery was incomplete; services were not restarted automatically.\n",
    );
  }
}

// Services an install stopped before replacing their root. If the install
// does not commit, they are started again from the (restored) previous root
// so a failed update does not leave a working setup offline.
let stoppedServices = null;

function pluginServiceStatus(root, scriptName) {
  const script = join(root, "scripts", scriptName);
  const bash = resolveUsableBash();
  if (!existsSync(script) || !bash) return "";
  const result = spawnSync(bash, [script, "status"], {
    cwd: root,
    env: runtimeEnv(),
    encoding: "utf8",
    stdio: ["ignore", "pipe", "ignore"],
    windowsHide: true,
    timeout: PLUGIN_SERVICE_TIMEOUT_MS,
  });
  if (result.error || result.signal || result.status !== 0) return "";
  return String(result.stdout || "").trim().split(/\r?\n/).pop() || "";
}

// A failed HTTP health check can print "not running" while the old
// listener is still bound. Corroborate it against the observed loopback
// port without relying on HTTP, a PID file, or shell-specific networking.
function servicePortRefusesConnections(status) {
  const port = Number(status.match(/ on http:\/\/localhost:(\d+)(?=\s|$)/)?.[1]);
  if (!Number.isInteger(port) || port < 1 || port > 65535) return false;
  const connect = `
    const socket = require("node:net").connect(Number(process.argv[1]), "127.0.0.1");
    socket.once("connect", () => process.exit(1));
    socket.once("error", error => process.exit(error.code === "ECONNREFUSED" ? 0 : 1));
    socket.setTimeout(1000, () => process.exit(1));
  `;
  const probe = spawnSync(process.execPath, ["-e", connect, String(port)], {
    // A terminal interrupt must not kill the observation needed for rollback.
    detached: true,
    env: { ...process.env, NODE_OPTIONS: "" },
    stdio: "ignore",
    windowsHide: true,
    timeout: 3000,
    killSignal: "SIGKILL",
  });
  return !probe.error && !probe.signal && probe.status === 0;
}

async function stopServicesForInstall(installedRoot, host, previousStatus = null) {
  // Shared services may belong to another integration. Never use a candidate
  // npm source as the recovery runtime for a first installation.
  const root = installStateSnapshot?.pluginRoot || installedRoot;
  if (!root || !existsSync(join(root, "scripts", "backend-service.sh"))) return;
  // Guided publication has already committed settings. Its cutover recovers
  // forward, since restarting an old service would read the new settings.
  if (!previousStatus) stoppedServices = { root, host, backend: false, dashboard: false };
  ensureRollbackOnExit();
  const failed = [];
  const reportsActiveService = /^(running on |stale claude-smart backend on )/;
  for (const kind of ["dashboard", "backend"]) {
    const scriptName = `${kind}-service.sh`;
    if (!existsSync(join(root, "scripts", scriptName))) continue;
    const before = previousStatus ? previousStatus[kind] : pluginServiceStatus(root, scriptName);
    if (!before) {
      // An interrupted or failed initial status gives no recovery baseline.
      failed.push(kind);
      break;
    }
    const observed = reportsActiveService.test(before);
    const stopped = runPluginService(root, scriptName, "stop");
    const after = pluginServiceStatus(root, scriptName);
    const stillActive = reportsActiveService.test(after);
    const portClosed = observed && !stillActive && servicePortRefusesConnections(before);
    // Record recovery before yielding for a pending signal. Even a failed
    // status command can follow a successful stop of a previously live service.
    if (!previousStatus) stoppedServices[kind] = portClosed;
    const switchedEndpoint = previousStatus && kind === "backend" &&
      /^(remote configured at |custom local server configured at )/.test(after);
    if (!stopped || !after || stillActive ||
        (observed && (!portClosed || (after !== "not running" && !switchedEndpoint)))) {
      failed.push(kind);
    }
  }
  // A refusal has no package or selector changes to roll back.
  if (failed.length && !previousStatus) commitInstallState();
  // Let a queued interruption restore the old services before any package write.
  await new Promise(setImmediate);
  if (terminatingOnSignal) await new Promise(() => {});
  if (failed.length)
    throw new Error(
      `Could not confirm shutdown of the previous ${failed.join(" and ")} service; ` +
        (previousStatus
          ? "settings and runtime selection are saved. Resolve shutdown and rerun setup to start shared services."
          : "the runtime was not replaced. Resolve the service shutdown failure and retry installation."),
    );
}

function restartStoppedServices() {
  const stopped = stoppedServices;
  stoppedServices = null;
  if (!stopped || !existsSync(join(stopped.root, "scripts", "backend-service.sh"))) return;
  if (!stopped.backend && !stopped.dashboard) return;
  if (stopped.backend) startBackendService(stopped.root, stopped.host);
  if (stopped.dashboard) runPluginService(stopped.root, "dashboard-service.sh", "start");
  process.stderr.write(`Restarted claude-smart services from ${stopped.root}.\n`);
}

function commitLocalPluginPackages(packageRoots) {
  // Commit the complete package set before best-effort backup cleanup. Codex's
  // cache and runtime belong to the same install; neither clears ownership early.
  const committed = packageRoots.map((root) => [root, pendingPreviousPackages.get(root)]);
  for (const root of packageRoots) pendingPreviousPackages.delete(root);
  stoppedServices = null;
  commitInstallState();
  for (const [packageRoot, pending] of committed) {
    try {
      if (pending && pending.backupPackage) {
        rmSync(pending.backupPackage, { recursive: true, force: true });
      }
    } catch (err) {
      process.stderr.write(
        `warning: could not remove the previous package at ${pending.backupPackage}: ` +
          `${err && err.message ? err.message : err}\n`,
      );
    }
    // Keep older Python envs until explicit uninstall: cached hooks may use them.
    try {
      releasePackageInstallLock(packageRoot);
    } catch {
      // A leftover lock dir is reclaimed as stale by the next install.
    }
  }
}

// Claude Code caches can reference an external Python env after an update.
// Successful installs retain old envs; rollback removes only its new env.
// Explicit uninstall removes every claude-code env, preserving other hosts.
function removeClaudeCodeVenv(link) {
  try {
    const target = realpathSync(link);
    if (dirname(target) !== realpathSync(CLAUDE_SMART_VENVS_DIR)) return;
    if (!target.split(sep).pop().startsWith("claude-code-")) return;
    rmSync(target, { recursive: true, force: true });
  } catch {
    // An install may fail before creating its env.
  }
}

function removeClaudeCodeVenvs() {
  let entries;
  try {
    entries = readdirSync(CLAUDE_SMART_VENVS_DIR);
  } catch {
    return;
  }
  for (const name of entries) {
    if (!name.startsWith("claude-code-")) continue;
    const candidate = join(CLAUDE_SMART_VENVS_DIR, name);
    try {
      rmSync(candidate, { recursive: true, force: true });
    } catch (err) {
      process.stderr.write(
        `warning: could not remove unused Python env ${candidate}: ${err && err.message ? err.message : err}\n`,
      );
    }
  }
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

// Clone prepared artifacts rather than moving them: the old install and its
// external Claude Code venv must remain usable if preparation rolls back.
function reusePreparedRuntime(previous, current, externalVenv = false) {
  if (!previous || !existsSync(previous)) return;
  const clone = (from, to) =>
    cpSync(from, to, {
      recursive: true,
      mode: constants.COPYFILE_FICLONE,
      preserveTimestamps: true,
      verbatimSymlinks: true,
    });
  const previousVenv = join(previous, ".venv");
  if (!isWindows() && existsSync(previousVenv)) {
    let target = join(current, ".venv");
    if (externalVenv) {
      mkdirSync(CLAUDE_SMART_VENVS_DIR, { recursive: true });
      target = join(CLAUDE_SMART_VENVS_DIR, `claude-code-${Date.now()}-${process.pid}`);
    }
    try {
      const previousRoot = realpathSync(previousVenv);
      clone(previousRoot, target);
      // Console scripts and activation files contain the venv's absolute path.
      // Rewrite only regular text files in the clone, never interpreter symlinks.
      for (const name of readdirSync(join(target, "bin"))) {
        const file = join(target, "bin", name);
        if (!lstatSync(file).isFile()) continue;
        const bytes = readFileSync(file);
        if (!bytes.includes(0)) {
          const text = bytes.toString("utf8");
          const relocated = text.replaceAll(previousRoot, target).replaceAll(previousVenv, target);
          if (relocated !== text) writeFileSync(file, relocated);
        }
      }
      if (externalVenv)
        symlinkSync(target, join(current, ".venv"), isWindows() ? "junction" : "dir");
    } catch {
      rmSync(target, { recursive: true, force: true });
    }
  }
  const oldDashboard = join(previous, "dashboard");
  const dashboard = join(current, "dashboard");
  const helper = join(PACKAGE_ROOT, "plugin", "scripts", "dashboard-cache.js");
  if (!existsSync(helper)) return;
  const cache = require(helper);
  // npx may run under a different Node than the private build runtime.
  // Clone against the recorded runtime; bootstrap revalidates the actual one.
  const builtWith = cache.recordedVersion(oldDashboard);
  if (
    builtWith &&
    existsSync(dashboard) &&
    cache.reusable(oldDashboard, builtWith) &&
    cache.buildKey(oldDashboard, builtWith) === cache.buildKey(dashboard, builtWith)
  ) {
    try {
      for (const name of ["node_modules", ".next"])
        clone(join(oldDashboard, name), join(dashboard, name));
    } catch {
      for (const name of ["node_modules", ".next"])
        rmSync(join(dashboard, name), { recursive: true, force: true });
    }
  }
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
    const previous = pendingPreviousPackages.get(packageRoot)?.backupPackage;
    reusePreparedRuntime(
      previous ? join(previous, "plugin") : null,
      join(packageRoot, "plugin"),
      packageRoot === CLAUDE_CODE_LOCAL_PACKAGE_DIR,
    );
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
  process.stdout.write("[phase] Preparing Python and runtime dependencies…\n");
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
  if (process.env.CLAUDE_SMART_DEFER_SHARED_SERVICES === "1" ||
      pathEntryExists(join(pluginRoot, "guide-prepared-runtime"))) return;
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
  // Cancellation may start while the stage still waits on a download.
  if (options.stage && terminatingOnSignal) return new Promise(() => {});
  // While a package install is uncommitted, run children in their own
  // process group so an interrupt can stop the whole tree (uv, npm, ...)
  // before rolling back; they are non-interactive there.
  const group = !isWindows() && (rollbackArmed() || Boolean(options.stage));
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
    if (options.stage) Object.assign(options.stage, { child, group });
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

function trackChild(child, group, untilReleased = false) {
  activeChildren.set(child, group);
  const forget = () => activeChildren.delete(child);
  if (!untilReleased) {
    child.on("exit", forget);
    child.on("error", forget);
  }
  return forget;
}

function processGroupAlive(pgid) {
  try {
    process.kill(-pgid, 0);
    return true;
  } catch (err) {
    return Boolean(err && err.code === "EPERM");
  }
}

async function terminateActiveChildren(timeoutMs = 5000) {
  await terminateChildren([...activeChildren.entries()], timeoutMs);
}

// Stops each [child, group] entry, waiting until every process group is
// empty (SIGKILL after the grace period).
async function terminateChildren(children, timeoutMs = 5000) {
  // A process group is waited on until it is empty, not just its leader: a
  // descendant (uv, npm) that ignores SIGTERM must not outlive the rollback.
  const alive = ([child, group]) =>
    group && !isWindows()
      ? processGroupAlive(child.pid)
      : child.exitCode === null && child.signalCode === null;
  const signalAll = (signal) => {
    for (const entry of children) {
      if (!alive(entry)) continue;
      const [child, group] = entry;
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
  const waitAll = async (ms) => {
    const deadline = Date.now() + ms;
    while (children.some(alive) && Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    return !children.some(alive);
  };
  signalAll("SIGTERM");
  if (!(await waitAll(timeoutMs))) {
    signalAll("SIGKILL");
    // A killed process may still be exiting; do not roll back under it.
    await waitAll(timeoutMs);
  }
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

// Use async children during service startup so elapsed progress remains visible.
async function runServiceWithProgress(pluginRoot, scriptName, subcommand, envOverrides = {}) {
  const script = join(pluginRoot, "scripts", scriptName);
  const bash = resolveUsableBash();
  if (!bash || !existsSync(script)) return false;
  const started = Date.now();
  const timer = setInterval(
    () =>
      process.stdout.write(
        `  Still waiting for ${scriptName} ${subcommand}… ${Math.floor((Date.now() - started) / 1000)}s\n`,
      ),
    5000,
  );
  try {
    return await new Promise((resolve) => {
      const child = spawn(bash, [script, subcommand], {
        cwd: pluginRoot,
        env: { ...runtimeEnv(), ...envOverrides },
        stdio: "ignore",
        windowsHide: true,
        detached: !isWindows(),
      });
      trackChild(child, !isWindows());
      const timeout = setTimeout(async () => {
        process.stderr.write(
          `warning: ${scriptName} ${subcommand} timed out; inspect ~/.claude-smart service logs.\n`,
        );
        await terminateChildren([[child, !isWindows()]]);
      }, PLUGIN_SERVICE_TIMEOUT_MS);
      child.once("error", () => {
        clearTimeout(timeout);
        resolve(false);
      });
      child.once("exit", (code) => {
        clearTimeout(timeout);
        if (!terminatingOnSignal) resolve(code === 0);
      });
    });
  } finally {
    clearInterval(timer);
  }
}

function installInventory(options = {}) {
  const args = options.global ? ["--global"] : options.local ? ["--local"] : [];
  // Loader file or a config reference: installs since the plugins-directory
  // loader write no config entry, so the config alone no longer proves anything.
  let openCodeInstalled = false;
  try {
    openCodeInstalled = scopeHasClaudeSmart(args) === true;
  } catch {
    /* A missing config is normal. Installation reports malformed configs. */
  }
  return [
    {
      host: HOST_CLAUDE_CODE,
      available: hasClaudeCli(),
      installed:
        existsSync(join(CLAUDE_CODE_LOCAL_PACKAGE_DIR, "plugin")) ||
        (options.claudePlugins ?? claudeInstallations()).length > 0,
    },
    { host: HOST_CODEX, available: hasCli("codex"), installed: Boolean(findCodexPluginRoot()) },
    { host: HOST_OPENCODE, available: hasOpenCodeCli(), installed: openCodeInstalled },
  ];
}

function claudeInstallations() {
  return readClaudeInstallations() ?? [];
}

function readClaudeInstallations() {
  const executable = resolveCommand(
    isWindows() ? ["claude.exe", "claude.cmd", "claude.bat"] : ["claude"],
  );
  if (!executable) return undefined;
  const shell = isWindows() && /\.(?:cmd|bat)$/i.test(executable);
  const result = spawnSync(shell ? `"${executable}"` : executable, ["plugin", "list", "--json"], {
    encoding: "utf8",
    stdio: ["ignore", "pipe", "ignore"],
    timeout: 5000,
    windowsHide: true,
    shell,
  });
  if (result.error || result.status !== 0) return null;
  try {
    const data = JSON.parse(result.stdout);
    return Array.isArray(data)
      ? data.filter((plugin) => plugin && typeof plugin === "object" &&
          !Array.isArray(plugin) && (plugin.id || plugin.pluginId) === PLUGIN_SPEC)
      : null;
  } catch {
    return null;
  }
}

function installEnvironment(captured) {
  const env = new Map(captured.runtime.values);
  const legacy = captured.legacy?.values || new Map();
  for (const [key, value] of legacy) {
    if (
      key.startsWith("CLAUDE_SMART_") &&
      key !== "CLAUDE_SMART_USE_LOCAL_EMBEDDING" &&
      !env.has(key)
    )
      env.set(key, value);
  }
  for (const [key, value] of legacyManagedConnection(env, legacy))
    env.set(key, value);
  return env;
}

function savedInstallConfiguration(captured) {
  // The same immutable bytes drive preview, discovery and confirmed publication.
  const env = installEnvironment(captured);
  const url =
    env.get("REFLEXIO_URL") ||
    (env.get("REFLEXIO_API_KEY")?.trim()
      ? process.env.REFLEXIO_URL || MANAGED_REFLEXIO_URL
      : "");
  const apiKey = env.get("REFLEXIO_API_KEY") ?? "";
  const userId = env.get(REFLEXIO_USER_ID_ENV) ?? "";
  return {
    backend: env.size ? (isRemoteReflexioUrl(url) ? "managed" : "local") : null,
    url,
    apiKey,
    provider: env.get(LEARNING_PROVIDER_ENV) || env.get(CLAUDE_SMART_HOST_ENV),
    model: env.get("CLAUDE_SMART_LEARNING_MODEL") || "",
    retiredOpenCodeVariant: Boolean(env.get("CLAUDE_SMART_OPENCODE_VARIANT")?.trim()),
    managedUrl: env.get("CLAUDE_SMART_MANAGED_URL") || "",
    managedApiKey: env.get("CLAUDE_SMART_MANAGED_API_KEY") || "",
    localUrl: env.get("CLAUDE_SMART_LOCAL_URL") || "",
    localApiKey: env.get("CLAUDE_SMART_LOCAL_API_KEY") || "",
    cliPath: env.get("CLAUDE_SMART_CLI_PATH") || "",
    opencodePath: env.get(CLAUDE_SMART_OPENCODE_PATH_ENV),
    useLocalCli: env.get(CLAUDE_SMART_USE_LOCAL_CLI_ENV),
    readOnly: ["1", "true", "yes", "on"].includes(
      (env.get(CLAUDE_SMART_READ_ONLY_ENV) || "").toLowerCase(),
    ),
    sharing: userId ? "global" : "project",
    userId,
  };
}

function planInstallSettings(config, captured) {
  const values = {
    REFLEXIO_URL: config.url || localBackendUrl(),
    REFLEXIO_API_KEY: config.apiKey || "",
    CLAUDE_SMART_READ_ONLY: config.readOnly ? "1" : "0",
    REFLEXIO_USER_ID:
      config.sharing === "global" ? config.userId || "global_user" : "",
  };
  // Keep both dashboard connections when the runtime switches modes. Viewing
  // either source must not rewrite the active hook/learning configuration.
  const previous = installEnvironment(captured);
  const previousProvider =
    previous.get(LEARNING_PROVIDER_ENV) || previous.get(CLAUDE_SMART_HOST_ENV);
  if (previousProvider) values[LEARNING_PROVIDER_ENV] = previousProvider;
  // A qualified process override is already inherited by startup. Persist it
  // only when the account has no executable of its own.
  if (config.validatedOpenCodePath &&
      !previous.get(CLAUDE_SMART_OPENCODE_PATH_ENV)?.trim()) {
    values[CLAUDE_SMART_OPENCODE_PATH_ENV] = config.validatedOpenCodePath;
  } else if (
    (config.hosts.includes(HOST_OPENCODE) ||
      config.provider === HOST_OPENCODE ||
      previousProvider === HOST_OPENCODE) &&
    !previous.has(CLAUDE_SMART_OPENCODE_PATH_ENV)
  ) {
    const resolved = resolveOpenCodePath();
    if (resolved) values[CLAUDE_SMART_OPENCODE_PATH_ENV] = resolved;
  }
  const remember = (url, apiKey) => {
    if (!url) return;
    const prefix = isRemoteReflexioUrl(url) ? "MANAGED" : "LOCAL";
    values[`CLAUDE_SMART_${prefix}_URL`] = url;
    values[`CLAUDE_SMART_${prefix}_API_KEY`] = apiKey || "";
  };
  remember(previous.get("REFLEXIO_URL"), previous.get("REFLEXIO_API_KEY"));
  remember(values.REFLEXIO_URL, values.REFLEXIO_API_KEY);
  if (config.backend === "local") {
    if (config.provider) values[LEARNING_PROVIDER_ENV] = config.provider;
    // Make the explicit CLI choice effective even after a previous cloud setup.
    values[CLAUDE_SMART_USE_LOCAL_CLI_ENV] = config.useLocalCli;
    values.CLAUDE_SMART_LEARNING_MODEL = config.model || "";
    if ((previous.get("CLAUDE_SMART_CLI_PATH") || "") !== config.cliPath) {
      values.CLAUDE_SMART_CLI_PATH = config.cliPath;
    }
  }
  const legacyRows = {};
  for (const [key, index] of captured.legacy?.rows || []) {
    if (
      /^CLAUDE_SMART_[A-Z0-9_]+$/.test(key) &&
      key !== "CLAUDE_SMART_USE_LOCAL_EMBEDDING" &&
      !captured.runtime.values.has(key) &&
      !Object.hasOwn(values, key)
    )
      legacyRows[key] = index;
  }
  const digest = (bytes) =>
    crypto.createHash("sha256").update(bytes).digest("hex");
  const request = {
    updates: values,
    snapshot: {
      exists: captured.runtime.exists,
      sha256: digest(captured.runtime.bytes),
      legacy_marker: captured.marker,
    },
    legacy: captured.legacy?.exists
      ? { sha256: digest(captured.legacy.bytes), rows: legacyRows }
      : null,
  };
  return {
    request,
    cliPath: config.backend === "local" ? config.cliPath : null,
    migrated: Boolean(
      captured.legacy &&
        legacyManagedConnection(captured.runtime.values, captured.legacy.values).size,
    ),
  };
}

function usePublishedSettings({ request, cliPath }) {
  // Subsequent operations must inherit the confirmed choices, including the
  // caller-first learning settings and an explicitly cleared custom CLI path.
  Object.assign(process.env, request.updates);
  if (cliPath !== null) process.env.CLAUDE_SMART_CLI_PATH = cliPath;
}

function acceptGuideSettings(settings) {
  usePublishedSettings(settings);
  if (settings.migrated)
    process.stdout.write("Migrated managed Reflexio settings to claude-smart.\n");
}

function runSettingsProcess(command, args, runtime, environment, input) {
  const group = !isWindows();
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      cwd: runtime.directory,
      env: environment,
      stdio: ["pipe", "pipe", "pipe"],
      detached: group,
      windowsHide: true,
    });
    const release = trackChild(child, group, true);
    let close;
    const entry = {
      child,
      group,
      hasClosed: false,
      closed: new Promise((done) => {
        close = done;
      }),
    };
    runtime.children.push(entry);
    let failed = false;
    let timedOut = false;
    const timeout = setTimeout(async () => {
      timedOut = true;
      try {
        await terminateChildren([[child, group]]);
      } finally {
        child.stdin?.destroy();
        child.stdout?.destroy();
        child.stderr?.destroy();
        child.unref();
        if (!terminatingOnSignal)
          reject(new Error(runtime.publishing
            ? "Settings save timed out and may have completed; reload settings before retrying."
            : "Settings publisher preparation timed out; no settings were saved."));
      }
    }, SETTINGS_PUBLISHER_TIMEOUT_MS);
    let output = "";
    child.on("error", () => {
      failed = true;
    });
    child.stderr?.resume();
    child.stdout?.setEncoding("utf8");
    child.stdout?.on("data", (chunk) => {
      if (!input) return;
      // The owner acknowledges with a tiny fixed JSON object, never values.
      if (output.length + chunk.length > 4096) {
        failed = true;
        void terminateChildren([[child, group]]);
      } else output += chunk;
    });
    child.stdin?.on("error", () => {});
    child.stdin?.end(input);
    child.once("close", async (code) => {
      clearTimeout(timeout);
      entry.hasClosed = true;
      close();
      try {
        await terminateChildren([[child, group]]);
        if (group && processGroupAlive(child.pid))
          throw new Error("Settings publisher process group did not stop");
        release();
        if (!terminatingOnSignal && !timedOut) resolve({ code: failed ? 1 : code, output });
      } catch (error) {
        if (!terminatingOnSignal && !timedOut) reject(error);
      }
    });
  });
}

async function publishInstallSettings(request) {
  ensureRollbackOnExit();
  const plugin = join(PACKAGE_ROOT, "plugin");
  const requirements = join(plugin, "settings-owner.requirements.txt");
  const generated = readFileSync(requirements, "utf8");
  for (const name of ["pyproject.toml", "uv.lock"]) {
    const digest = crypto
      .createHash("sha256")
      .update(readFileSync(join(plugin, name)))
      .digest("hex");
    if (!generated.split("\n").includes("# " + name + " sha256: " + digest))
      throw new Error(
        "Settings publisher bundle is inconsistent; update claude-smart before retrying.",
      );
  }
  const pythonRequirement = generated.match(/^# requires-python: (.+)$/m)?.[1];
  if (!pythonRequirement)
    throw new Error(
      "Settings publisher bundle is incomplete; update claude-smart before retrying.",
    );
  const runtime = {
    directory: mkdtempSync(join(tmpdir(), "claude-smart-settings-")),
    temporary: true,
    children: [],
  };
  settingsRuntimes.add(runtime);
  const environment = {
    ...process.env,
    UV_PROJECT_ENVIRONMENT: runtime.directory,
    // This invocation installs only the locked settings-owner dependency group.
    UV_VENV_SEED: "false",
  };
  delete environment.VIRTUAL_ENV;
  const python = join(
    runtime.directory,
    isWindows() ? "Scripts/python.exe" : "bin/python",
  );
  try {
    const uv = await ensureUv(async (command, args) => {
      const result = await runSettingsProcess(command, args, runtime, environment);
      return result.code;
    });
    // The generated constraint avoids discovering an enclosing enterprise
    // workspace or syncing either the source or caller's active environment.
    const prepared = await runSettingsProcess(
      uv,
      [
        "venv",
        "--no-project",
        "--python",
        pythonRequirement,
        runtime.directory,
      ],
      runtime,
      environment,
    );
    if (prepared.code !== 0)
      throw new Error(
        "Cannot prepare settings publisher; no settings were saved.",
      );
    const inspected = await runSettingsProcess(
      python,
      [
        "-I", "-c",
        "import json,sys,sysconfig;from pathlib import Path;print(json.dumps(str(Path(sysconfig.get_path('purelib')).resolve().relative_to(Path(sys.argv[1]).resolve()))))",
        runtime.directory,
      ],
      runtime, environment, Buffer.alloc(0),
    );
    let destination;
    try {
      if (inspected.code !== 0) throw new Error();
      const relative = JSON.parse(inspected.output);
      if (typeof relative !== "string") throw new Error();
      destination = realpathSync(resolve(runtime.directory, relative));
      if (!destination.startsWith(realpathSync(runtime.directory) + sep)) throw new Error();
    } catch {
      throw new Error("Cannot identify the private settings environment; no settings were saved.");
    }
    const installed = await runSettingsProcess(
      uv,
      [
        "pip",
        "sync",
        "--python",
        python,
        // Bind the actual interpreter's site directory, overriding caller pip
        // target/prefix settings without discarding index/auth configuration.
        "--target",
        destination,
        "--require-hashes",
        "--no-build",
        requirements,
      ],
      runtime,
      environment,
    );
    if (installed.code !== 0)
      throw new Error(
        "Cannot install settings publisher dependencies; no settings were saved.",
      );
    await writeConfirmedSettings(
      python,
      ["-I", join(plugin, "src/claude_smart/env_config.py"), "publish-install"],
      runtime,
      environment,
      request,
    );
  } finally {
    cleanupSettingsRuntimes();
  }
}

async function writeConfirmedSettings(python, args, runtime, environment, request) {
  runtime.publishing = true;
  const saved = await runSettingsProcess(
    python,
    args,
    runtime,
    environment,
    Buffer.from(JSON.stringify(request), "utf8"),
  );
  if (saved.code === 73)
    throw new Error("Settings changed after confirmation; reload and retry.");
  if (saved.code === 75)
    throw new Error(
      "Settings are busy; retry after the other save finishes.",
    );
  if (saved.code === 80 && request.opencode_loader)
    throw new Error(`OpenCode loader changed before settings were saved at ${request.opencode_loader.path}; reload and retry.`);
  if (args.includes("--check-windows-embedding")) {
    if (saved.code === 76)
      throw new Error("Could not determine Windows inference ownership. Check the embedding settings before retrying.");
    if (saved.code === 77)
      throw new Error("Windows local embeddings require onnxruntime. Install the Microsoft Visual C++ x64 Redistributable (https://aka.ms/vs/17/release/vc_redist.x64.exe) and retry.");
    if (saved.code === 78)
      throw new Error("Windows embedding preflight timed out before saving settings.");
    if (saved.code === 79)
      throw new Error("A .env file in the installed plugin runtime shadows the account settings. Move those settings to the account file before retrying.");
  }
  let acknowledged = false;
  try {
    acknowledged = saved.code === 0 && JSON.parse(saved.output).ok === true;
  } catch {}
  if (!acknowledged)
    throw new Error(
      "Settings save could not be confirmed; reload settings before retrying.",
    );
  runtime.publishing = false;
}

async function publishFromPreparedRuntime(pluginRoot, settings, openCodeLoader, { guideWorker = true } = {}) {
  const args = ["-I", "-m", "claude_smart.env_config", "publish-prepared-install"];
  if (isWindows()) {
    requireWindowsEmbeddingBash();
    if (isBundledBackendUrl(settings.request.updates.REFLEXIO_URL))
      args.push("--check-windows-embedding");
  }
  if (guideWorker && !process.connected)
    throw new Error("Installation guide disconnected before settings publication.");
  const runtime = { directory: pluginRoot, children: [] };
  settingsRuntimes.add(runtime);
  try {
    await writeConfirmedSettings(
      pluginPythonPath(pluginRoot),
      args,
      runtime,
      runtimeEnv(),
      openCodeLoader ? { ...settings.request, opencode_loader: openCodeLoader } : settings.request,
    );
    usePublishedSettings(settings);
    // A lost acknowledgement cannot undo the settings owner's committed save.
    if (guideWorker && process.connected) process.send("settings-published", () => {});
  } finally {
    cleanupSettingsRuntimes();
  }
}

function reportRuntimePhase(label, settings) {
  process.stdout.write(`[phase] ${label}\n`);
  if (settings && process.connected) process.send({ phase: label }, () => {});
}

async function runGuidedHost(command, host, options, label, settings, preparedRuntime) {
  const generation = preparedRuntime?.id;
  mkdirSync(join(CLAUDE_SMART_STATE_DIR, "install-logs"), { recursive: true });
  const log = join(
    CLAUDE_SMART_STATE_DIR,
    "install-logs",
    `${command}-${host}-${Date.now()}-${process.pid}.log`,
  );
  const fd = openSync(log, "wx", 0o600);
  const args = [__filename, command === "setup" ? "install" : command, "--host", host];
  if (options.global) args.push("--global");
  if (options.local) args.push("--local");
  process.stdout.write(`\n${label}…\n  Log: ${log}\n`);
  const started = Date.now();
  const timer = setInterval(
    () => process.stdout.write(`  ${label}… ${Math.floor((Date.now() - started) / 1000)}s\n`),
    5000,
  );
  let accepted = false;
  const acceptSettings = () => {
    if (accepted) return;
    accepted = true;
    acceptGuideSettings(settings);
  };
  try {
    const code = await new Promise((resolve) => {
      const child = spawn(process.execPath, args, {
        env: {
          ...runtimeEnv(),
          CLAUDE_SMART_INSTALL_CHILD: "1",
          CLAUDE_SMART_DEFER_SHARED_SERVICES: "1",
        },
        // A committed settings worker and its subprocesses must be able to
        // finish logging and registering even if the guide exits.
        stdio: settings || generation ? ["ignore", fd, fd, "ipc"] : ["ignore", "pipe", fd],
        detached: !isWindows(),
        windowsHide: true,
      });
      trackChild(child, !isWindows());
      if (settings || generation) {
        child.on("message", (message) => {
          if (settings && message === "settings-published") acceptSettings();
          else if (typeof message?.phase === "string") process.stdout.write(`  ${message.phase}\n`);
        });
        const payload = generation
          ? { generation, openCodePlan: host === HOST_OPENCODE ? preparedRuntime.openCode : undefined }
          : { request: settings.request, cliPath: settings.cliPath };
        child.send(payload, (error) => {
          if (error && child.connected) child.disconnect();
        });
      }
      let pending = "";
      child.stdout?.on("data", (chunk) => {
        require("fs").writeSync(fd, chunk);
        pending += chunk.toString();
        const lines = pending.split(/\r?\n/);
        pending = lines.pop().slice(-4096);
        for (const line of lines)
          if (line.startsWith("[phase] ")) process.stdout.write(`  ${line.slice(8)}\n`);
      });
      child.once("error", (err) => {
        process.stderr.write(`  ${label} failed: ${err.message}\n`);
        resolve(1);
      });
      // Wait for the log stream independently of IPC. After an explicit
      // disconnect Node can omit ChildProcess's combined close event.
      const exited = new Promise((done) => child.once("exit", done));
      const logClosed = child.stdout
        ? new Promise((done) => child.stdout.once("close", done))
        : Promise.resolve();
      Promise.all([exited, logClosed]).then(([code]) => {
        if (!terminatingOnSignal) resolve(typeof code === "number" ? code : 1);
      });
    });
    // A successful prepared worker requires a confirmed save, even if the
    // guide disconnected before receiving the acknowledgement.
    if (settings && code === 0) acceptSettings();
    if (code !== 0) {
      if (accepted || generation)
        process.stderr.write("  Account settings were saved. Retry setup to finish registration.\n");
      process.stderr.write(`  ${label} failed. Details: ${log}\n`);
    }
    return { code, log };
  } finally {
    clearInterval(timer);
    closeSync(fd);
  }
}

function learningProviderVersion(config) {
  // Custom executables need not implement --version. Do not run arbitrary probes.
  if (config.cliPath) return undefined;
  const command =
    config.provider === HOST_OPENCODE
      ? resolveOpenCodePath()
      : config.provider === HOST_CODEX
        ? "codex"
        : config.provider === HOST_CLAUDE_CODE
          ? "claude"
          : null;
  const result = probeLearningCli(command, ["--version"], 4096);
  if (!result) return undefined;
  const version = (result.stdout || "").trim();
  return /^[A-Za-z0-9][A-Za-z0-9 ._()+:/-]{0,159}$/.test(version) ? version : undefined;
}

function probeLearningCli(command, args, maxBuffer, { cwd, env, captureStderr = false } = {}) {
  if (!command) return undefined;
  const shell =
    isWindows() && (command === "claude" || command === "codex" || /\.(?:cmd|bat)$/i.test(command));
  // Node joins batch-file arguments through the shell. Reject paths that
  // cannot be quoted literally, and pass only fixed probe arguments.
  if (shell && /[%!^&|<>"\r\n]/.test(command)) return undefined;
  const result = spawnSync(shell ? `"${command}"` : command, args, {
    encoding: "utf8",
    cwd,
    env,
    stdio: ["ignore", "pipe", captureStderr ? "pipe" : "ignore"],
    timeout: 3000,
    maxBuffer,
    windowsHide: true,
    shell,
  });
  return result.status === 0 && !result.error && !result.signal ? result : undefined;
}

function validateGuidedOpenCodeLearning(config, saved, withWorkerPath) {
  if (config.backend !== "local" || config.provider !== HOST_OPENCODE ||
      !/^(?:1|true|yes|on)$/i.test(config.useLocalCli?.trim() || "") ||
      (config.url && !isBundledBackendUrl(config.url))) return;
  if (saved.retiredOpenCodeVariant || process.env.CLAUDE_SMART_OPENCODE_VARIANT?.trim()) {
    throw new Error(
      "CLAUDE_SMART_OPENCODE_VARIANT has been retired. Move the variant to " +
      "CLAUDE_SMART_LEARNING_MODEL=provider/model#variant and remove the old assignment before updating.",
    );
  }
  if (!config.model?.startsWith("#")) return;
  const resolveForLaunch = (lookup) => lookup() ?? withWorkerPath(lookup);
  const selected = config.cliPath;
  let launcher;
  if (selected) {
    const absolute = isAbsolute(selected);
    if (!absolute && basename(selected) !== selected) {
      throw new Error("Use an absolute CLAUDE_SMART_CLI_PATH to select an OpenCode variant.");
    }
    const names = isWindows() && !extname(selected)
      ? [`${selected}.exe`, `${selected}.cmd`, `${selected}.bat`, selected]
      : [selected];
    const match = absolute ? names.find(isExecutableFile)
      : resolveForLaunch(() => isWindows() ? resolveCommand(names) : resolvePosixOpenCodeCommand(selected));
    if (!match) {
      throw new Error("The configured CLAUDE_SMART_CLI_PATH cannot be executed. Repair that path before updating.");
    }
    launcher = isWindows() ? resolve(match) : match;
  }
  const bridge = /^opencode-claude-compat(?:\.cmd|\.js)?$/i.test(basename(launcher || ""));
  if (launcher && isWindows() && !bridge && !/\.exe$/i.test(launcher)) {
    throw new Error("Select a native OpenCode .exe or the supported compatibility bridge on Windows.");
  }
  let command = launcher && !bridge ? launcher : resolveForLaunch(resolveOpenCodePath);
  if (command && !isAbsolute(command)) {
    throw new Error("Use an absolute CLAUDE_SMART_OPENCODE_PATH to select an OpenCode variant.");
  }
  if (command && isWindows()) command = resolve(command);
  const { prepareWorkDir, dispatchEnvironment, declaredRunOptions } =
    require("../plugin/scripts/opencode-claude-compat");
  const cwd = prepareWorkDir();
  let probe;
  try {
    probe = withWorkerPath(() => probeLearningCli(command, ["run", "--help"], 64 * 1024, {
      cwd,
      captureStderr: true,
      env: dispatchEnvironment(cwd),
    }));
  } finally {
    rmSync(cwd, { recursive: true, force: true });
  }
  const runOptions = declaredRunOptions(probe ? `${probe.stdout}\n${probe.stderr}` : "");
  if (!runOptions.has("--variant")) {
    throw new Error(
      "The selected OpenCode CLI does not advertise --variant. Specify provider/model#variant instead of #variant.",
    );
  }
  // The pinned SDK's native OpenCode transport needs these options at dispatch.
  // The compatibility bridge has a separate command contract.
  if (launcher && !bridge &&
      (!["--model", "--agent", "--format", "--title"].every((option) => runOptions.has(option)) ||
       !(runOptions.has("--standalone") || runOptions.has("--attach")))) {
    throw new Error("The selected executable does not support the native OpenCode run options.");
  }
  if (launcher) config.cliPath = launcher;
  if (!launcher || bridge) config.validatedOpenCodePath = command;
}

function readGuidedRuntime(id) {
  if (typeof id !== "string" || !/^runtime-[A-Za-z0-9]{6}$/.test(id))
    throw new Error("Invalid prepared runtime identity");
  const base = realpathSync(GUIDED_RUNTIME_DIR);
  const packageRoot = join(base, id);
  const pluginRoot = join(packageRoot, "plugin");
  if (realpathSync(packageRoot) !== packageRoot || realpathSync(pluginRoot) !== pluginRoot ||
      !isExecutableFile(pluginPythonPath(pluginRoot)))
    throw new Error("Prepared runtime is incomplete");
  return { id, packageRoot, pluginRoot };
}

function boundGuidedPluginRoot(root) {
  const path = join(root, GUIDED_RUNTIME_BINDING);
  try {
    if (lstatSync(path).isFile()) return readGuidedRuntime(readFileSync(path, "utf8")).pluginRoot;
  } catch {
    // An absent or incomplete registration cannot select a runtime.
  }
  return null;
}

function openCodeRegistrationPath(loader) {
  const path = isWindows() ? loader.toLowerCase() : loader;
  const key = crypto.createHash("sha256").update(path).digest("hex");
  return join(GUIDED_OPENCODE_REGISTRATIONS, key);
}

function assertOpenCodeRuntimeEntry(pluginRoot) {
  const opencode = join(pluginRoot, "opencode");
  const dist = join(opencode, "dist");
  const entry = join(dist, "server.mjs");
  if (!lstatSync(opencode).isDirectory() || !lstatSync(dist).isDirectory() ||
      !lstatSync(entry).isFile()) throw new Error(`Prepared OpenCode runtime entry is invalid at ${entry}`);
}

function openCodeRuntimeMatchesLoader(runtime, path, strict = false) {
  try {
    const [loader] = runScopedOpenCode(runtime.pluginRoot, "snapshot", [path]);
    if (!isGeneratedOpenCodeV2Loader(loader.contents)) return false;
    const spec = loader.contents.split("\n")[2].slice("export { default } from ".length, -1);
    return localOpenCodeFilePath(JSON.parse(spec)) === join(runtime.pluginRoot, "opencode", "dist", "server.mjs");
  } catch (error) {
    if (strict) throw error;
    return false;
  }
}

function readOpenCodeBinding(binding, { pending = false, strict = false } = {}) {
  const record = pending ? `${binding}.pending` : binding;
  if (!pathEntryExists(record)) return null;
  try {
    if (!lstatSync(record).isFile()) throw new Error("Invalid OpenCode binding");
    const { path, id } = JSON.parse(readFileSync(record, "utf8"));
    if (openCodeRegistrationPath(path) !== binding) throw new Error("OpenCode binding changed scope");
    const runtime = readGuidedRuntime(id);
    assertOpenCodeRuntimeEntry(runtime.pluginRoot);
    return openCodeRuntimeMatchesLoader(runtime, path, strict) ? { ...runtime, path } : null;
  } catch (error) {
    if (strict) throw error;
    return null;
  }
}

function recoverOpenCodeBinding(binding, strict = false) {
  return readOpenCodeBinding(binding, { pending: true, strict });
}

function codexHomeKey(home) {
  return crypto.createHash("sha256").update(isWindows() ? home.toLowerCase() : home).digest("hex");
}

function rememberCodexHome() {
  // Discovery only. A recorded location still needs the host's enabled config
  // and matching installed cache before it can keep a generation alive.
  writeGuidedRecord(join(GUIDED_CODEX_HOMES, codexHomeKey(CODEX_HOME)), CODEX_HOME);
}

function registeredCodexPluginRoots() {
  // The historical standalone installer did not record the default home.
  const homes = new Set([join(homedir(), ".codex")]);
  try {
    for (const name of readdirSync(GUIDED_CODEX_HOMES)) {
      if (!/^[0-9a-f]{64}$/.test(name)) continue;
      try {
        const record = join(GUIDED_CODEX_HOMES, name);
        if (!lstatSync(record).isFile()) continue;
        const home = readFileSync(record, "utf8");
        if (isAbsolute(home) && resolve(home) === home && codexHomeKey(home) === name)
          homes.add(home);
      } catch {
        // An unreadable discovery record cannot preserve any services.
      }
    }
  } catch {
    // There might be a default-home installation without a registry.
  }
  return Array.from(homes).flatMap((candidate) => {
    try {
      const home = realpathSync(candidate);
      return installedCodexRuntime(home);
    } catch {
      return [];
    }
  });
}

function installedCodexRuntime(home) {
  // The installed cache remains authoritative when updating the marketplace
  // adapter and the installed plugin is interrupted between the two writes.
  const cache = join(home, "plugins", "cache", CODEX_MARKETPLACE_NAME, "claude-smart");
  const versions = readdirSync(cache, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry) => entry.name)
    .sort((a, b) => compareSemverLikePathNames(b, a));
  for (const version of versions) {
    const entry = join(cache, version);
    if (codexPluginVersion(entry) !== version) continue;
    const root = boundGuidedPluginRoot(entry);
    if (root && preparedCodexRegistration(basename(dirname(root)), home))
      return [root];
    const standalone = join(home, "claude-smart", "runtime", "plugin");
    if (codexPluginVersion(standalone) === version && standaloneCodexRegistration(standalone, home))
      return [standalone];
  }
  return [];
}

function preparedOpenCodeRegistration(runtime) {
  if (!runtime?.openCode) return false;
  const loader = runtime.openCode.loader.path;
  const binding = openCodeRegistrationPath(loader);
  if (recoverOpenCodeBinding(binding)?.id === runtime.id) return true;
  return readOpenCodeBinding(binding)?.id === runtime.id;
}

function openCodeGuidedPluginRoots() {
  try {
    const names = readdirSync(GUIDED_OPENCODE_REGISTRATIONS)
      .filter((name) => /^[0-9a-f]{64}(?:\.pending)?$/.test(name))
      .map((name) => name.replace(/\.pending$/, ""));
    return Array.from(new Set(names), (name) => {
      const binding = join(GUIDED_OPENCODE_REGISTRATIONS, name);
      return (recoverOpenCodeBinding(binding) || readOpenCodeBinding(binding))?.pluginRoot;
    }).filter(Boolean);
  } catch {
    return [];
  }
}

function stageOpenCodeRegistration(loader, runtime) {
  if (!runtime) return;
  const binding = openCodeRegistrationPath(loader);
  const predecessor = recoverOpenCodeBinding(binding, true);
  if (predecessor) writeGuidedRecord(binding, JSON.stringify({ path: predecessor.path, id: predecessor.id }));
  writeGuidedRecord(`${binding}.pending`, JSON.stringify({ path: loader, id: runtime.id }));
}

function writeGuidedRecord(binding, contents) {
  mkdirSync(dirname(binding), { recursive: true, mode: 0o700 });
  const candidate = uniquePackagePath(binding, ".record");
  try {
    writeFileSync(candidate, contents, { flag: "wx", mode: 0o600 });
    renameSync(candidate, binding);
  } finally {
    rmSync(candidate, { force: true });
  }
}

function publishOpenCodeRegistration(loader, runtime) {
  const binding = openCodeRegistrationPath(loader);
  if (runtime) writeGuidedRecord(binding, JSON.stringify({ path: loader, id: runtime.id }));
  else rmSync(binding, { force: true });
  rmSync(`${binding}.pending`, { force: true });
}

function guidedAdapterStage(target, id) {
  return join(dirname(target), `.adapter-${id}`);
}

function prepareGuidedPackageAdapter(target, id, prepare) {
  const stage = guidedAdapterStage(target, id);
  mkdirSync(dirname(stage), { recursive: true });
  mkdirSync(stage, { mode: 0o700 });
  const discard = () => rmSync(stage, { recursive: true, force: true });
  process.once("exit", discard);
  try {
    prepare(stage);
  } catch (error) {
    process.removeListener("exit", discard);
    discard();
    throw error;
  }
  return () => {
    process.removeListener("exit", discard);
    discard();
  };
}

function publishGuidedPackageAdapter(target, id, adapterPath, label) {
  holdPackageInstallLock(target, label);
  const stage = guidedAdapterStage(target, id);
  if (readFileSync(join(adapterPath(stage), GUIDED_RUNTIME_BINDING), "utf8") !== id)
    throw new Error(`Prepared ${label} changed before registration`);
  replaceLocalPluginPackage(stage, target);
  return target;
}

function observePreviousServices() {
  const root = activePluginRoot();
  if (!root) return { root, status: {} };
  const status = {};
  for (const kind of ["dashboard", "backend"]) {
    const script = `${kind}-service.sh`;
    if (!existsSync(join(root, "scripts", script))) continue;
    status[kind] = pluginServiceStatus(root, script);
    if (!status[kind]) throw new Error(`Could not observe the previous ${kind} before saving settings`);
  }
  return { root, status };
}

function recordGuidedShutdown(selection, previous) {
  const status = {};
  for (const [kind, value] of Object.entries(previous.status)) {
    const observed = value.match(/^(?:running on |stale claude-smart backend on )(http:\/\/(?:localhost|127\.0\.0\.1):[0-9]+)(?:\s|$)/);
    status[kind] = observed ? `running on ${observed[1]}` : "not running";
  }
  const draft = uniquePackagePath(GUIDED_PENDING_SHUTDOWN, ".shutdown");
  try {
    writeFileSync(draft, JSON.stringify({ ...selection, root: previous.root, status }), {
      flag: "wx", mode: 0o600,
    });
    renameSync(draft, GUIDED_PENDING_SHUTDOWN);
  } finally {
    rmSync(draft, { force: true });
  }
}

function readGuidedShutdown() {
  try {
    if (!lstatSync(GUIDED_PENDING_SHUTDOWN).isFile())
      throw new Error("The pending shared service shutdown record is not a regular file");
    return JSON.parse(readFileSync(GUIDED_PENDING_SHUTDOWN, "utf8"));
  } catch (error) {
    if (error?.code === "ENOENT") return;
    throw error;
  }
}

function pendingStartupId() {
  try {
    if (!lstatSync(GUIDED_PENDING_STARTUP).isFile())
      throw new Error("The pending shared service startup record is not a regular file");
    const id = readFileSync(GUIDED_PENDING_STARTUP, "utf8");
    if (!/^runtime-[A-Za-z0-9]{6}$/.test(id))
      throw new Error("The pending shared service startup record is invalid");
    return id;
  } catch (error) {
    if (error?.code === "ENOENT") return null;
    throw error;
  }
}

async function resumeGuidedShutdown({ retainBaseline = true } = {}) {
  const pending = readGuidedShutdown();
  const startup = pendingStartupId();
  if (!pending && !startup) return {};
  let current = activePluginRoot();
  if (pending) {
    if (typeof pending.id !== "string" || !/^runtime-[A-Za-z0-9]{6}$/.test(pending.id) ||
        (pending.root !== null &&
          (typeof pending.root !== "string" || !isAbsolute(pending.root))) ||
        (!pending.status || typeof pending.status !== "object"))
      throw new Error("The pending shared service shutdown record is invalid");
    const target = join(realpathSync(GUIDED_RUNTIME_DIR), pending.id, "plugin");
    if (current === target) {
      if (!pathEntryExists(GUIDED_SELECTED_RUNTIME)) recordGuidedSelection(target);
      if (pending.root && startup !== pending.id && pending.predecessorStopped !== true) {
        if (!existsSync(join(pending.root, "scripts", "backend-service.sh")))
          throw new Error("The previous shared service runtime is unavailable; resolve shutdown before retrying setup");
        await stopServicesForInstall(pending.root, null, pending.status);
      }
      // Publish forward recovery before discarding the predecessor record.
      writeGuidedRecord(GUIDED_PENDING_STARTUP, pending.id);
      rmSync(GUIDED_PENDING_SHUTDOWN);
      return recoverSelectedStartup(target);
    }
    if (current !== pending.root) {
      if (!pending.root || current !== null || pathEntryExists(join(REFLEXIO_DIR, "plugin-root")) ||
          !existsSync(join(pending.root, "scripts", "backend-service.sh")))
        throw new Error("The shared runtime selection changed during shutdown; resolve it before retrying setup");
      forcePluginRoot(pending.root);
      current = pending.root;
    }
  }

  // Candidate preparation and the predecessor observation can fail while the
  // last selected generation is still starting. Retry that generation first.
  if (startup && current && isGuidedPluginRoot(current) &&
      basename(dirname(current)) === startup) {
    const recovered = await recoverSelectedStartup(current);
    if (pending && !retainBaseline) rmSync(GUIDED_PENDING_SHUTDOWN);
    return {
      ...recovered,
      ...(pending && retainBaseline ? { baseline: observePreviousServices() } : {}),
    };
  }

  if (pending && retainBaseline && pending.root)
    return { baseline: { root: pending.root, status: pending.status } };
  if (pending) rmSync(GUIDED_PENDING_SHUTDOWN);
  return {};
}

async function recoverSelectedStartup(root) {
  const { ready, summary } = await refreshSelectedServices(root, readInstallPolicy(), {
    restart: false,
  });
  if (ready) rmSync(GUIDED_PENDING_STARTUP, { force: true });
  return { completed: true, summary };
}

async function refreshSelectedServices(root, setup, { restart = true } = {}) {
  const host = setup.provider || hostForPluginRoot(root);
  startDeferredDashboardBuild(root);
  return startAndReportServices(root, host, setup, { summaryOnly: true, restart });
}

async function prepareGuidedRuntime(config, options, settings, baseline) {
  assertSupportedRuntimePlatform();
  const openCodeArgs = options.global ? ["--global"] : [];
  if (config.hosts.includes(HOST_CODEX)) validateCodexPackage();

  mkdirSync(GUIDED_RUNTIME_DIR, { recursive: true, mode: 0o700 });
  const directory = mkdtempSync(join(realpathSync(GUIDED_RUNTIME_DIR), "runtime-"));
  const pluginRoot = join(directory, "plugin");
  const id = basename(directory);
  const discardAdapters = [];
  // The interpreter embeds its prefix, so build at the final physical path.
  let published = false;
  const discard = () => {
    if (!published) {
      for (const discardAdapter of discardAdapters) discardAdapter();
      rmSync(directory, { recursive: true, force: true });
    }
  };
  process.once("exit", discard);
  try {
    cpSync(PACKAGE_ROOT, directory, { recursive: true, filter: shouldCopyPath });
    verifyLocalPluginPackage(directory, "guided runtime");
    if (!existsSync(join(pluginRoot, "scripts", "guided-command.sh")))
      throw new Error("The guided runtime command launcher is missing");
    const tools = await bootstrapPluginRuntime(pluginRoot, { settings });
    // Only the guide may replace the dependencies of a prepared generation.
    writeFileSync(join(pluginRoot, "guide-prepared-runtime"), id, { flag: "wx", mode: 0o600 });
    const readOnly = settings.request.updates[CLAUDE_SMART_READ_ONLY_ENV] === "1";
    if (config.hosts.includes(HOST_CLAUDE_CODE)) {
      const hooks = renderSharedClaudeHooks(join(pluginRoot, "hooks", "hooks.json"), pluginRoot, readOnly);
      discardAdapters.push(prepareGuidedPackageAdapter(CLAUDE_CODE_LOCAL_PACKAGE_DIR, id, (stage) => {
        cpSync(PACKAGE_ROOT, stage, { recursive: true, filter: shouldCopyPath });
        verifyLocalPluginPackage(stage, "Claude Code adapter");
        const adapter = join(stage, "plugin");
        writeFileSync(join(adapter, "hooks", "hooks.json"), hooks);
        writeFileSync(join(adapter, GUIDED_RUNTIME_BINDING), id);
      }));
    }
    if (config.hosts.includes(HOST_CODEX)) {
      rememberCodexHome();
      const hooks = renderPreparedCodexHooks(pluginRoot, pinHookNode(tools.node), readOnly, true);
      discardAdapters.push(prepareGuidedPackageAdapter(CODEX_MARKETPLACE_DIR, id, (stage) => {
        copyCodexMarketplace(stage, { fresh: true });
        const adapter = codexMarketplacePluginRoot(stage);
        requireCodexPluginVersion(adapter);
        writeFileSync(join(adapter, "hooks", "codex-hooks.json"), hooks);
        writeFileSync(join(adapter, GUIDED_RUNTIME_BINDING), id);
      }));
    }
    let openCode;
    if (config.hosts.includes(HOST_OPENCODE)) {
      assertOpenCodeRuntimeEntry(pluginRoot);
      openCode = planScopedOpenCode(openCodeArgs, directory);
    }
    const previous = baseline || observePreviousServices();
    recordGuidedShutdown({ id }, previous);
    await publishFromPreparedRuntime(pluginRoot, settings, openCode?.loader, { guideWorker: false });
    published = true;
    acceptGuideSettings(settings);
    return { id, previous, openCode };
  } finally {
    process.removeListener("exit", discard);
    discard();
  }
}

async function runGuidedCommand(command, args) {
  if (args.includes("--help") || args.includes("-h")) {
    printHelp();
    return;
  }
  const captured = command === "uninstall" ? null : captureInstallSnapshot();
  const saved = captured ? savedInstallConfiguration(captured) : {};
  if (command !== "uninstall" && !process.env[CLAUDE_SMART_OPENCODE_PATH_ENV]) {
    const savedPath = saved.opencodePath;
    if (savedPath) process.env[CLAUDE_SMART_OPENCODE_PATH_ENV] = savedPath;
  }
  ensureRollbackOnExit();
  process.stdout.write(
    `claude-smart ${JSON.parse(readFileSync(join(PACKAGE_ROOT, "package.json"), "utf8")).version} · ${command}\n`,
  );
  let deferredSettings;
  let preparedRuntime;
  let codexPreflightFailure;
  // Use worker PATH for host discovery without affecting caller-first learning settings.
  const withWorkerPath = (inspect) => {
    const callerPath = process.env.PATH;
    process.env.PATH = runtimeEnv().PATH;
    try {
      return inspect();
    } finally {
      if (callerPath === undefined) delete process.env.PATH;
      else process.env.PATH = callerPath;
    }
  };
  await guide
    .runGuide(command, args, {
      inventory: (options) => withWorkerPath(() => installInventory(options)),
      saved: () => saved,
      save: async (config, options) => {
        validateGuidedOpenCodeLearning(config, saved, withWorkerPath);
        const multipleHosts = config.hosts.length > 1;
        if (config.hosts.includes(HOST_CODEX)) {
          withWorkerPath(() => {
            codexPreflightFailure = codexHookProcessCleanupError();
            if (codexPreflightFailure) {
              if (!multipleHosts) throw new Error(codexPreflightFailure);
              config = { ...config, hosts: config.hosts.filter((host) => host !== HOST_CODEX) };
              const claudeAvailable = config.hosts.includes(HOST_CLAUDE_CODE) && hasClaudeCli();
              const openCodeError = config.hosts.includes(HOST_OPENCODE) ? opencodePrerequisiteError() : null;
              if (!claudeAvailable && (!config.hosts.includes(HOST_OPENCODE) || openCodeError))
                throw new Error([codexPreflightFailure, openCodeError || "Claude Code is not available on PATH."].join(" ").trim());
            }
          });
        }
        holdPackageInstallLock(GUIDE_INSTALL_LOCK, "installation guide");
        const { baseline } = await resumeGuidedShutdown();
        const settings = planInstallSettings(config, captured);
        // Recovery keeps the previous generation intact until registration is
        // complete, even when the user retries with just one host.
        if (multipleHosts || baseline || pathEntryExists(GUIDED_SELECTED_RUNTIME)) {
          preparedRuntime = await prepareGuidedRuntime(config, options, settings, baseline);
          return;
        }
        if (config.hosts.length === 1 && [HOST_CODEX, HOST_OPENCODE].includes(config.hosts[0])) {
          deferredSettings = settings;
          return;
        }
        await publishInstallSettings(settings.request);
        acceptGuideSettings(settings);
      },
      version: JSON.parse(
        readFileSync(join(PACKAGE_ROOT, "package.json"), "utf8"),
      ).version,
      providerVersion: learningProviderVersion,
      learningModel: async (config) => {
        try {
          const response = await fetch(new URL("/api/get_config", config.url), {
            headers: { authorization: `Bearer ${config.apiKey}` },
            signal: AbortSignal.timeout(5000),
          });
          if (!response.ok) return;
          const data = await response.json();
          const model = data.llm_config?.generation_model_name;
          return typeof model === "string" &&
            /^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$/.test(model)
            ? `${model} (managed configuration)`
            : undefined;
        } catch {
          return;
        }
      },
      runHost: async (action, host, options, label) => {
        if (action === "uninstall" && !heldPackageLocks.has(GUIDE_INSTALL_LOCK)) {
          holdPackageInstallLock(GUIDE_INSTALL_LOCK, "installation guide");
          await resumeGuidedShutdown({ retainBaseline: false });
        }
        if (host === HOST_CODEX && codexPreflightFailure)
          return { code: 1, reason: codexPreflightFailure };
        return runGuidedHost(action, host, options, label, deferredSettings, preparedRuntime);
      },
      finish: async (action, config, results) => {
        if (action !== "uninstall" &&
            !results.some((result) => result.code === 0) &&
            !preparedOpenCodeRegistration(preparedRuntime) &&
            !(preparedRuntime && config.hosts.includes(HOST_CODEX) &&
              preparedCodexRegistration(preparedRuntime.id))) return;
        let root = preparedRuntime ? readGuidedRuntime(preparedRuntime.id).pluginRoot : activePluginRoot();
        if (!root || !existsSync(join(root, "scripts", "backend-service.sh")))
          return;
        if (action === "uninstall") {
          const codexSurvivors = registeredCodexPluginRoots();
          const workerPlugins = withWorkerPath(readClaudeInstallations);
          const claudePlugins = workerPlugins === null
            ? (readClaudeInstallations() ?? null)
            : (workerPlugins ?? []);
          if (claudePlugins === null)
            return ["Shared services kept running because Claude Code integrations could not be verified."];
          if (isGuidedPluginRoot(root)) {
            const survivors = installedPluginRoots(codexSurvivors, claudePlugins);
            if (!survivors.includes(root)) {
              const { status } = observePreviousServices();
              await stopServicesForInstall(root, null, status);
              root = survivors[0];
              if (root) forcePluginRoot(root);
              else {
                clearPluginRoot();
                return ["Shared services stopped; no integrations remain."];
              }
            }
          }
          const [current, global] = withWorkerPath(() => [
            installInventory({ claudePlugins }),
            installInventory({ global: true, claudePlugins }),
          ]);
          if (![...current, ...global].some((agent) => agent.installed) &&
              codexSurvivors.length === 0 && openCodeGuidedPluginRoots().length === 0) {
            stopClaudeSmartServices(root);
            return [
              "Shared services stopped; no integrations remain in this project/account or global OpenCode config.",
            ];
          }
          // Removal stops only owned services. Repointing lets remaining integrations
          // serve their hooks again without selecting a different learning provider.
          process.stdout.write(
            "\nRefreshing shared services for remaining integrations…\n",
          );
        } else if (preparedRuntime) {
          const { root: previousRoot } = preparedRuntime.previous;
          if (activePluginRoot() !== previousRoot)
            throw new Error("Account settings and registrations were saved, but the selected runtime changed. Rerun setup to refresh shared services.");
          snapshotInstallState({ preserveFailureMarker: true });
          try {
            // A refused selection must leave the previous services running.
            forcePluginRoot(root);
          } catch (error) {
            rollbackLocalPluginPackages();
            throw new Error(`Account settings and registrations were saved, but runtime selection failed: ${error.message}`, { cause: error });
          }
          commitInstallState();
          const cutover = await resumeGuidedShutdown();
          if (cutover.completed) return cutover.summary;
        }
        let setup;
        try {
          setup = readInstallPolicy();
        } catch (err) {
          if (action !== "uninstall") throw err;
          return [
            "Removal completed; repair saved settings with npx claude-smart setup before refreshing shared services.",
          ];
        }
        return (await refreshSelectedServices(root, setup)).summary;
      },
    })
    .finally(() => {
      releasePackageInstallLock(GUIDE_INSTALL_LOCK);
      // Keypress listeners can keep the TTY alive after the last prompt. Release
      // it only when the whole guide finishes; later prompts still need stdin.
      if (process.stdin.isTTY) process.stdin.destroy();
    });
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
    // Complete an in-flight shutdown before the parent handles Ctrl-C.
    detached: subcommand === "stop",
    cwd: pluginRoot,
    env: { ...runtimeEnv(), ...envOverrides },
    stdio: "ignore",
    windowsHide: true,
    timeout: PLUGIN_SERVICE_TIMEOUT_MS,
    killSignal: "SIGTERM",
  });
  if (result.error || result.signal) {
    const reason =
      result.error && result.error.code === "ETIMEDOUT"
        ? `timed out after ${PLUGIN_SERVICE_TIMEOUT_MS / 1000}s`
        : result.error
          ? result.error.message
          : `terminated by ${result.signal}`;
    process.stderr.write(`warning: ${scriptName} ${subcommand} ${reason}; continuing.\n`);
    return false;
  }
  return result.status === 0;
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
  return (
    String(result.stdout || "")
      .trim()
      .split(/\r?\n/)
      .pop() || ""
  );
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
  // Match the shell's literal authority for configured port spellings such as
  // 08073. A URL object normalizes the digits before exposing its port.
  const literal = value.match(/^http:\/\/(?:localhost|127\.0\.0\.1)(?::([0-9]+))?(?=[/?#]|$)/);
  if (literal) return (literal[1] || "80") === port;
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

async function startAndReportServices(
  pluginRoot, host, setup, { summaryOnly = false, restart = true } = {},
) {
  // A stored learning target wins over the integration that supplied the runtime.
  const learningHost = setup.provider || host;
  if (process.env.CLAUDE_SMART_DEFER_SHARED_SERVICES === "1") return { ready: true, summary: [] };
  const { managed, url: hooksUrl } = setup;
  const summary = [];
  let backendReady = true;
  const report = (message) => {
    summary.push(message.trim());
    if (!summaryOnly) process.stdout.write(message);
  };
  if (managed || (hooksUrl && !isBundledBackendUrl(hooksUrl))) {
    // Hooks will not call the bundled backend, so stop one left running from
    // an earlier local setup (stop only reaps claude-smart's own listener).
    runPluginService(pluginRoot, "backend-service.sh", "stop");
  }
  if (managed) {
    report(`Memory storage and background learning run on Reflexio cloud at ${hooksUrl}.\n`);
    report(`Cloud dashboard: ${new URL(hooksUrl).origin}/dashboard\n`);
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
    report(
      `Hooks use the Reflexio server at ${hooksUrl}, which claude-smart does not start; ` +
        `it is ${answering ? "answering" : "not answering"} ${base}health.\n`,
    );
  } else if (autostartDisabled("CLAUDE_SMART_BACKEND_AUTOSTART")) {
    report("Backend autostart is disabled (CLAUDE_SMART_BACKEND_AUTOSTART=0).\n");
  } else {
    // A same-root backend can be healthy but still use the previous payload's
    // bind address or extraction bridge. Preparation succeeded before this
    // point; restart only owned services to apply the installed configuration.
    process.stdout.write(`\nStarting local Reflexio at ${localBackendUrl()}…\n`);
    if (restart) await runServiceWithProgress(pluginRoot, "backend-service.sh", "stop");
    await runServiceWithProgress(pluginRoot, "backend-service.sh", "start", {
      CLAUDE_SMART_HOST: learningHost,
    });
    const url = localBackendUrl();
    backendReady = false;
    if (await waitForHttp(`${url}health`, 5, ({ status }) => status === 200)) {
      const status = backendServiceStatus(pluginRoot);
      if (status.startsWith("running on")) {
        backendReady = true;
        report(`Backend healthy at ${url}.\n`);
      } else {
        report(
          `Something answers ${url}health, but it is not a claude-smart backend this ` +
            `install can use (backend-service.sh status: ${status || "unknown"}).\n`,
        );
      }
    } else {
      report(
        `Backend is still starting (log: ${join(CLAUDE_SMART_STATE_DIR, "backend.log")}); ` +
          "it is started again at the next session start if needed.\n",
      );
    }
  }
  if (autostartDisabled("CLAUDE_SMART_DASHBOARD_AUTOSTART")) {
    report("Dashboard autostart is disabled (CLAUDE_SMART_DASHBOARD_AUTOSTART=0).\n");
    return { ready: backendReady, summary };
  }
  process.stdout.write("Starting local dashboard (first build may take 1–2 minutes)…\n");
  if (restart) await runServiceWithProgress(pluginRoot, "dashboard-service.sh", "stop");
  await runServiceWithProgress(pluginRoot, "dashboard-service.sh", "start", {
    CLAUDE_SMART_HOST: learningHost,
  });
  const port = (process.env.DASHBOARD_PORT || "").trim() || "3001";
  const url = `http://localhost:${port}/`;
  // Same marker dashboard-service.sh trusts: a foreign app on the port answers
  // the root page too, but only the claude-smart dashboard sends this header.
  const isDashboard = ({ status, headers }) =>
    status === 200 && headers["x-claude-smart-dashboard"] !== undefined;
  const dashboardReady = await waitForHttp(`${url}api/health`, 3, isDashboard);
  if (dashboardReady) {
    report(
      `Local dashboard: ${url} · default data source: ${managed ? "Managed Reflexio" : "Local Reflexio"}.\n`,
    );
  } else if ((await httpProbe(`${url}api/health`)).status !== null) {
    report(
      `Another app answers ${url} without the claude-smart dashboard marker; ` +
        "the dashboard is not running there. Set DASHBOARD_PORT to a free port.\n",
    );
  } else {
    report(
      `Dashboard is building in the background (first build takes 1-2 minutes); it will serve ${url}.\n`,
    );
  }
  return { ready: backendReady && dashboardReady, summary };
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
        downloadFile(new URL(response.headers.location, url).toString(), dest).then(
          resolve,
          reject,
        );
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
      if (isExecutableFile(candidate)) return candidate;
    }
  }
  return null;
}

function privateNodeRoot() {
  return join(homedir(), ".claude-smart", "node", "current");
}

function privateNodeBinDirs(root = privateNodeRoot()) {
  return [join(root, "bin"), root];
}

function resolvePrivateCommand(names, root) {
  for (const dir of privateNodeBinDirs(root)) {
    for (const name of names) {
      const candidate = join(dir, name);
      if (isExecutableFile(candidate)) return candidate;
    }
  }
  return null;
}

function privateNodeTools(root = privateNodeRoot()) {
  const node = resolvePrivateCommand(isWindows() ? ["node.exe", "node"] : ["node"], root);
  const npm = resolvePrivateCommand(isWindows() ? ["npm.cmd", "npm.exe"] : ["npm"], root);
  return node && npm ? { node, npm } : null;
}

function isInsideDirectory(relativePath) {
  return relativePath && !isAbsolute(relativePath) &&
    relativePath !== ".." && !relativePath.startsWith(`..${sep}`);
}

function hookNodeDistributionHash(root) {
  const hash = crypto.createHash("sha256");
  const visit = (directory) => {
    for (const name of readdirSync(directory).sort()) {
      const path = join(directory, name);
      const entry = lstatSync(path);
      const key = relative(root, path);
      const mode = process.platform === "win32" ? 0 : entry.mode & 0o777;
      if (entry.isSymbolicLink()) {
        const target = readlinkSync(path);
        // Native resolution follows symlink components before interpreting ..,
        // just as the OS does when it opens the executable or its libraries.
        const resolved = relative(root, realpathSync.native(path));
        if (isAbsolute(target) || !isInsideDirectory(resolved))
          throw new Error("The private Node distribution contains a link outside its install directory.");
        hash.update(JSON.stringify([key, "link", target]));
      } else if (entry.isDirectory()) {
        hash.update(JSON.stringify([key, "directory", mode]));
        visit(path);
      } else if (entry.isFile()) {
        hash.update(JSON.stringify([key, "file", mode, fileSha256(path)]));
      } else {
        throw new Error("The private Node distribution contains an unsupported file type.");
      }
    }
  };
  visit(root);
  return hash.digest("hex");
}

// Published hooks can outlive node/current. Pin the complete distribution so
// an executable that uses relative libraries keeps the same layout.
function pinHookNode(source) {
  const current = privateNodeRoot();
  const root = realpathSync.native(current);
  const nodePath = relative(current, source);
  if (!isInsideDirectory(nodePath))
    throw new Error("The prepared hook executable is outside the private Node distribution.");
  const digest = hookNodeDistributionHash(root);
  const directory = join(dirname(current), "launchers");
  mkdirSync(directory, { recursive: true, mode: 0o700 });
  if (!lstatSync(directory).isDirectory())
    throw new Error("The Codex hook launcher directory must be a regular directory.");
  const owner = realpathSync.native(directory);
  const destination = join(owner, digest);
  const executable = join(destination, nodePath);
  const verify = () => {
    if (
      !lstatSync(destination).isDirectory() ||
      !isExecutableFile(executable) ||
      hookNodeDistributionHash(destination) !== digest
    ) {
      throw new Error("The installed Codex hook launcher does not match the prepared Node distribution.");
    }
    return executable;
  };
  if (pathEntryExists(destination)) return verify();

  const staging = mkdtempSync(join(owner, ".prepare-"));
  try {
    cpSync(root, staging, {
      recursive: true,
      mode: constants.COPYFILE_FICLONE,
      verbatimSymlinks: true,
    });
    if (hookNodeDistributionHash(staging) !== digest)
      throw new Error("Private Node changed while preparing the Codex hook launcher; retry setup.");
    const probe = spawnSync(join(staging, nodePath), ["--version"], {
      stdio: "ignore",
      timeout: 10000,
      windowsHide: true,
    });
    if (probe.status !== 0)
      throw new Error("The prepared Codex hook launcher could not run Node.");
    try {
      renameSync(staging, destination);
    } catch (error) {
      // Another installer can publish the same distribution concurrently.
      if (!pathEntryExists(destination)) throw error;
    }
    return verify();
  } finally {
    rmSync(staging, { recursive: true, force: true });
  }
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

async function preparePrivateNodeArchive(temp, stage) {
  const major = process.env.CLAUDE_SMART_NODE_LTS_MAJOR || "26";
  const { nodeOs, nodeArch, archiveExt } = nodeArchiveSpec();
  const baseUrl =
    process.env.CLAUDE_SMART_NODE_BASE_URL || `https://nodejs.org/dist/latest-v${major}.x`;

  const sumsPath = join(temp, "SHASUMS256.txt");
  await downloadFile(`${baseUrl}/SHASUMS256.txt`, sumsPath);
  const sums = readFileSync(sumsPath, "utf8");
  const match = sums
    .split(/\r?\n/)
    .map((line) => line.trim().split(/\s+/))
    .find(
      (parts) =>
        parts[1] &&
        new RegExp(
          `^node-v[^ ]+-${nodeOs}-${nodeArch}\\.${archiveExt.replace(/\./g, "\\.")}$`,
        ).test(parts[1]),
    );
  if (!match)
    throw new Error(`could not resolve Node.js ${nodeOs}-${nodeArch} archive from ${baseUrl}`);
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
    const powershell = resolveCommand(["powershell.exe", "powershell", "pwsh.exe", "pwsh"]);
    if (!powershell)
      throw new Error("PowerShell is required to extract private Node.js on Windows");
    code = await runChecked(
      powershell,
      [
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        // Use the two-argument .NET API on both PowerShell editions. A
        // PSModulePath inherited from pwsh can select the wrong Archive module.
        [
          "$ErrorActionPreference = 'Stop'",
          "if (-not ('System.IO.Compression.ZipFile' -as [type])) { Add-Type -AssemblyName System.IO.Compression.FileSystem }",
          "[System.IO.Compression.ZipFile]::ExtractToDirectory($env:ARCHIVE_PATH, $env:DEST_DIR)",
        ].join("; "),
      ],
      { env: { ...process.env, ARCHIVE_PATH: archivePath, DEST_DIR: extractDir }, stage },
    );
  } else {
    const tar = resolveCommand(["tar"]);
    if (!tar) throw new Error("tar is required to extract private Node.js on macOS");
    code = await runChecked(tar, ["-xzf", archivePath, "-C", extractDir], { stage });
  }
  if (code !== 0) throw new Error(`Node.js archive extraction failed for ${archiveName}`);
  const extracted = join(extractDir, archiveName.replace(/\.zip$/, "").replace(/\.tar\.gz$/, ""));
  if (!privateNodeTools(extracted))
    throw new Error(`verified Node.js archive ${archiveName} is missing executable node or npm`);
  return extracted;
}

async function ensurePrivateNode() {
  const existing = privateNodeTools();
  if (existing) return existing;

  assertSupportedRuntimePlatform();
  const current = privateNodeRoot();
  const nodeRoot = dirname(current);
  mkdirSync(nodeRoot, { recursive: true });
  // Stage on the destination filesystem so activation is one rename after
  // backing up the old path. Preparation never touches the current runtime.
  const temp = mkdtempSync(join(nodeRoot, ".prepare-"));
  const stage = {};
  privateNodeStages.set(temp, stage);
  ensureRollbackOnExit();
  try {
    const extracted = await preparePrivateNodeArchive(temp, stage);
    const lock = acquirePackageInstallLock(current, "private Node");
    try {
      const concurrent = privateNodeTools();
      if (concurrent) return concurrent;

      const backup = uniquePackagePath(current, ".previous");
      const hadCurrent = pathEntryExists(current);
      if (hadCurrent) renameSync(current, backup);
      try {
        renameSync(extracted, current);
      } catch (error) {
        if (hadCurrent) renameSync(backup, current);
        throw error;
      }
      const installed = privateNodeTools();
      if (!installed) {
        // Put the invalid candidate back in the private work directory so the
        // same rollback works even when its paths include symlinks.
        renameSync(current, extracted);
        if (hadCurrent) renameSync(backup, current);
        throw new Error("private Node.js install completed but node/npm are not usable");
      }
      if (hadCurrent) rmSync(backup, { recursive: true, force: true });
      return installed;
    } finally {
      rmSync(lock, { recursive: true, force: true });
    }
  } finally {
    cleanupPrivateNodeStage(temp, stage);
  }
}

function resolveUv() {
  return resolveCommand(isWindows() ? ["uv.exe", "uv"] : ["uv"], [
    join(homedir(), ".local", "bin"),
    join(homedir(), ".cargo", "bin"),
  ]);
}

async function ensureUv(run = runChecked) {
  let uv = resolveUv();
  if (uv) return uv;
  assertSupportedRuntimePlatform();
  let code = 0;
  if (isWindows()) {
    const powershell = resolveCommand(["powershell.exe", "powershell", "pwsh"]);
    if (!powershell) throw new Error("PowerShell is required to install uv on Windows");
    code = await run(powershell, [
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
    code = await run(sh, [installer]);
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

function removePublishHooks(parsed) {
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
    removePublishHooks(parsed);
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

function renderCodexHooksForNode(parsed, pluginRoot, nodePath, readOnly, { shared = false } = {}) {
  if (readOnly) removePublishHooks(parsed);
  const runner = join(pluginRoot, "scripts", "codex-hook.js");
  const command = (...args) =>
    [nodePath, runner, ...args, "--runtime-root", pluginRoot].map(quoteCommandPart).join(" ");
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
      if (shared && Array.isArray(block.hooks)) {
        block.hooks = block.hooks.filter((hook) => !hook.command?.includes("ensure-plugin-root.sh"));
      }
      for (const hook of block.hooks || []) {
        hook.command = patchOne(hook.command);
      }
    }
  }
  return JSON.stringify(parsed, null, 2) + "\n";
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

function requireWindowsEmbeddingBash() {
  const bash = resolveUsableBash();
  if (!bash) {
    throw new Error(
      "Git Bash is required for claude-smart dependency checks on Windows. Install Git for Windows and ensure bash.exe is on PATH, or run from WSL.",
    );
  }
  return bash;
}

function verifyWindowsLocalEmbeddingRuntime(pluginRoot, env) {
  if (!isWindows()) return;
  const script = join(pluginRoot, "scripts", "smart-install.sh");
  const result = spawnSync(requireWindowsEmbeddingBash(), [script, "verify-windows-embedding"], {
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
    [
      "pip",
      "install",
      "--project",
      pluginRoot,
      "--python",
      pythonPath,
      "--quiet",
      "--reinstall",
      "--no-deps",
      vendorRoot,
    ],
    { cwd: pluginRoot, env },
  );
  if (code !== 0) {
    process.stderr.write(
      `warning: quiet vendored Reflexio install failed in ${pluginRoot}; retrying with full output.\n`,
    );
    code = await runChecked(
      uv,
      [
        "pip",
        "install",
        "--project",
        pluginRoot,
        "--python",
        pythonPath,
        "--reinstall",
        "--no-deps",
        vendorRoot,
      ],
      { cwd: pluginRoot, env },
    );
  }
  if (code !== 0) throw new Error(`vendored Reflexio install failed in ${pluginRoot}`);
  verifyVendoredReflexioRequirements(pluginRoot, uv, pythonPath, env);
  verifyVendoredReflexioIdentity(pluginRoot, pythonPath, vendorRoot, env);
}

// The installed hook imports Reflexio from Python's own environment. Check
// the bundled Python sources using that interpreter and isolated resolution.
function verifyVendoredReflexioIdentity(pluginRoot, pythonPath, vendorRoot, env) {
  const script = `
import filecmp
from importlib.util import find_spec
import os
from pathlib import Path
import sys

def sources(root):
    if root.is_symlink() or root.is_junction():
        raise SystemExit("Reflexio source directory is linked")
    files = set()
    for directory, children, entries in os.walk(root):
        folder = Path(directory)
        subdirs = (folder / child for child in children)
        if any(child.is_symlink() or child.is_junction() for child in subdirs):
            raise SystemExit("Reflexio source directory contains a linked subdirectory")
        files.update((folder / item).relative_to(root) for item in entries if item.endswith(".py"))
    return files

vendor = Path(sys.argv[1]) / "reflexio"
prefix = Path(sys.prefix)
spec = find_spec("reflexio")
roots = list(spec.submodule_search_locations or ()) if spec else []
if prefix.resolve() != Path(sys.argv[2]).resolve() or len(roots) != 1:
    raise SystemExit("the candidate interpreter does not resolve a single Reflexio package")
installed = Path(roots[0])
if (not installed.is_relative_to(prefix) or not installed.resolve().is_relative_to(prefix.resolve())
        or not spec.origin or Path(spec.origin) != installed / "__init__.py"):
    raise SystemExit("the candidate resolves Reflexio outside its environment")
expected = sources(vendor)
if not expected or expected != sources(installed):
    raise SystemExit("installed Python source paths differ from the bundle")
for source in sorted(expected):
    if not filecmp.cmp(vendor / source, installed / source, shallow=False):
        raise SystemExit(f"installed Python source differs from the bundle: {source}")
`;
  const result = spawnSync(pythonPath, ["-I", "-X", "utf8", "-c", script, vendorRoot, join(pluginRoot, ".venv")], {
    cwd: pluginRoot,
    env,
    encoding: "utf8",
    timeout: 15_000,
    killSignal: "SIGKILL",
    windowsHide: true,
  });
  if (result.status !== 0 || result.error) {
    const detail = result.error?.message || result.stderr?.trim() || "Python verification failed";
    throw new Error(`bundled Reflexio identity check failed in ${pluginRoot}: ${detail}`);
  }
}

// --no-deps skips the bundled Reflexio's own requirements, so a requirement
// plugin/uv.lock does not install would only surface later as an import error
// in the backend. Abort the install instead, naming the missing package.
function verifyVendoredReflexioRequirements(pluginRoot, uv, pythonPath, env) {
  const result = spawnSync(uv, ["pip", "check", "--python", pythonPath], {
    cwd: pluginRoot,
    env,
    encoding: "utf8",
    shell: isWindows() && /\.(?:cmd|bat)$/i.test(uv),
    windowsHide: true,
  });
  if (!result.error && !result.signal && result.status === 0) return;
  const report = `${result.stdout || ""}${result.stderr || ""}`.trim();
  if (report) process.stderr.write(`${report}\n`);
  const problems = report
    .split(/\r?\n/)
    .filter((line) => line.startsWith("The package"))
    .join("; ");
  const detail = problems || (result.error ? result.error.message : "uv pip check failed");
  throw new Error(
    `bundled Reflexio dependency check failed in ${pluginRoot}: ${detail}. ` +
      "This claude-smart package is missing a dependency of its bundled Reflexio; " +
      "update with 'npx claude-smart update' or report it at https://github.com/ReflexioAI/claude-smart/issues",
  );
}

async function syncPluginPythonEnv(pluginRoot, uv, env) {
  let code = await runChecked(uv, ["sync", "--locked", "--python", "3.12", "--quiet"], {
    cwd: pluginRoot,
    env,
  });
  if (code === 0) return;

  const lockIsFresh =
    runSilentStatus(uv, ["lock", "--check", "--python", "3.12"], { cwd: pluginRoot, env }) === 0;
  if (lockIsFresh) {
    process.stderr.write(
      `warning: quiet uv sync failed in ${pluginRoot}; retrying with full output.\n`,
    );
    code = await runChecked(uv, ["sync", "--locked", "--python", "3.12"], { cwd: pluginRoot, env });
    if (code !== 0) throw new Error(`uv sync failed in ${pluginRoot}`);
    return;
  }

  process.stderr.write(
    "warning: plugin/uv.lock is out of sync; refreshing local lockfile and retrying uv sync.\n",
  );
  code = await runChecked(uv, ["lock", "--python", "3.12"], { cwd: pluginRoot, env });
  if (code !== 0) throw new Error(`uv lock failed in ${pluginRoot}`);
  code = await runChecked(uv, ["sync", "--python", "3.12", "--quiet"], { cwd: pluginRoot, env });
  if (code !== 0) {
    process.stderr.write(
      `warning: quiet uv sync failed in ${pluginRoot} after refreshing plugin/uv.lock; retrying with full output.\n`,
    );
    code = await runChecked(uv, ["sync", "--python", "3.12"], { cwd: pluginRoot, env });
  }
  if (code !== 0) throw new Error(`uv sync failed in ${pluginRoot}`);
}

async function preparePluginPython(pluginRoot, uv, inherited) {
  const project = resolve(pluginRoot);
  // uv lets an invoking shell redirect both project discovery and the venv.
  // Preparation owns the candidate package and its Python environment.
  const env = {
    ...inherited,
    UV_WORKING_DIR: project,
    UV_PROJECT: project,
    UV_PROJECT_ENVIRONMENT: join(project, ".venv"),
  };
  delete env.VIRTUAL_ENV;
  const pyprojectPath = join(pluginRoot, "pyproject.toml");
  const pyproject = existsSync(pyprojectPath) ? readFileSync(pyprojectPath, "utf8") : "";
  if (/^\s*\[tool\.uv\.sources\]\s*$/m.test(pyproject)) {
    const lockCode = await runChecked(uv, ["lock", "--quiet"], { cwd: pluginRoot, env });
    if (lockCode !== 0) throw new Error(`uv lock failed in ${pluginRoot}`);
  }
  await syncPluginPythonEnv(pluginRoot, uv, env);
  await installVendoredReflexio(pluginRoot, uv, env);
}

async function preparePluginDashboard(pluginRoot, nodeRuntime, env, settings) {
  const dashboardDir = join(pluginRoot, "dashboard");
  if (existsSync(dashboardDir)) {
    reportRuntimePhase("Preparing dashboard dependencies and build…", settings);
    const helper = join(PACKAGE_ROOT, "plugin", "scripts", "dashboard-cache.js");
    const cache = existsSync(helper) ? require(helper) : null;
    const nodeVersion = spawnSync(nodeRuntime.node, ["--version"], {
      encoding: "utf8",
    }).stdout?.trim();
    if (nodeVersion && cache?.reusable(dashboardDir, nodeVersion)) {
      process.stdout.write("Dashboard build inputs unchanged; reusing prepared build.\n");
      return;
    }
    // Building needs only dependencies; devDependencies (eslint) are developer tools.
    let code = await runChecked(nodeRuntime.npm, ["ci", "--omit=dev"], { cwd: dashboardDir, env });
    if (code !== 0) throw new Error(`npm ci --omit=dev failed in ${dashboardDir}`);
    code = await runChecked(nodeRuntime.npm, ["run", "build"], { cwd: dashboardDir, env });
    if (code !== 0) throw new Error(`npm run build failed in ${dashboardDir}`);
    if (cache && nodeVersion && existsSync(join(dashboardDir, ".next", "BUILD_ID")))
      cache.record(dashboardDir, nodeVersion);
  }
}

async function bootstrapPluginRuntime(pluginRoot, options = {}) {
  assertSupportedRuntimePlatform();
  reportRuntimePhase("Preparing Python and runtime dependencies…", options.settings);
  if (!options.settings) rmSync(INSTALL_FAILURE_MARKER, { force: true });
  const nodeRuntime = options.nodeRuntime ?? (await ensurePrivateNode());
  if (options.readOnly) prunePublishHooksForReadOnly(pluginRoot);
  const uv = await ensureUv();
  const toolPaths = [dirname(uv), ...privateNodeBinDirs()];
  const env = runtimeEnv(toolPaths);
  await preparePluginPython(pluginRoot, uv, env);
  if (options.settings) {
    await preparePluginDashboard(pluginRoot, nodeRuntime, env, options.settings);
    return nodeRuntime;
  }
  const repairCode = await runChecked(
    pluginPythonPath(pluginRoot),
    ["-I", "-m", "claude_smart.env_config", "bootstrap-repair"],
    { cwd: pluginRoot, env },
  );
  if (repairCode !== 0)
    throw new Error(
      "Could not repair missing installation defaults; resolve the settings error above and retry setup.",
    );
  verifyWindowsLocalEmbeddingRuntime(pluginRoot, env);
  await preparePluginDashboard(pluginRoot, nodeRuntime, env);
  return nodeRuntime;
}

function printHelp() {
  process.stdout.write(
    [
      "claude-smart — install helper for Claude Code, Codex, and OpenCode",
      "",
      "Usage:",
      "  npx claude-smart install                       Guided installation for one or more agents",
      "  npx claude-smart install --host claude-code,codex --backend local --provider codex",
      "  npx claude-smart install --host codex --backend managed --api-key-env REFLEXIO_API_KEY",
      "  --host <comma-separated agents>                claude-code, codex, opencode",
      "  --backend local|managed                        Required for an unattended first install",
      "  --provider claude-code|codex|opencode           Local background learning provider",
      "  --model <model-id>                             Local learning model (default: provider default)",
      "  --url <endpoint> --api-key-env <variable>       Managed endpoint and secret source",
      "  --read-only|--learn --sharing project|global   Memory access and sharing settings",
      "  --yes                                          Use explicit/saved settings without prompts",
      "  npx claude-smart install --host codex          Register the plugin marketplace for Codex",
      "  npx claude-smart install --host opencode       Add claude-smart to OpenCode config",
      "  npx claude-smart setup                         Guided configuration and installation",
      "  npx claude-smart status [--host codex]        Show installed and recently observed runtime versions",
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
      "  1. Prepares private Node/npm, uv, Python deps, and dashboard deps as needed",
      `  2. Copies the bundled marketplace to ${CODEX_MARKETPLACE_DIR}`,
      "  3. codex plugin marketplace add <copied marketplace>",
      "  4. codex features enable hooks && codex features enable plugin_hooks",
      "  5. Installs claude-smart into Codex's plugin cache and enables it",
      "  6. Trusts and enables claude-smart hook entries in CODEX_HOME/config.toml (default ~/.codex)",
      "  7. Start a new Codex session to load the plugin.",
      "",
      "OpenCode install:",
      `  1. Copies this package to ${OPENCODE_LOCAL_PACKAGE_DIR}`,
      "  2. Writes a loader into the selected scope's OpenCode plugins directory; both OpenCode lines load the plugin from there, and any legacy opencode.json entry is removed",
      "  3. Prepares local services now from the copied plugin runtime",
      "  4. Restart OpenCode.",
      "",
      "Update:",
      "  npx claude-smart update                        Guided update for selected/saved integrations",
      "  npx claude-smart update --host codex           Reinstall Codex support from this package",
      "  npx claude-smart update --host opencode        Reinstall OpenCode support from this package",
      "  npx claude-smart setup                         Guided configuration and installation",
      "",
      "Uninstall:",
      "  npx claude-smart uninstall                     Choose integrations to remove",
      "  npx claude-smart uninstall --host claude-code,codex  Remove selected integrations; keep data",
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

function validateCodexPackage() {
  for (const rel of CODEX_REQUIRED_FILES) {
    const path = join(PACKAGE_ROOT, rel);
    if (!existsSync(path)) {
      process.stderr.write(
        `error: published package is missing ${rel}; reinstall claude-smart or use a newer release\n`,
      );
      process.exit(1);
    }
  }
}

function copyCodexMarketplace(directory = CODEX_MARKETPLACE_DIR, { fresh = false } = {}) {
  validateCodexPackage();
  if (!fresh) rmSync(directory, { recursive: true, force: true });
  mkdirSync(join(directory, ".agents", "plugins"), { recursive: true });
  mkdirSync(join(directory, "plugins"), { recursive: true });

  writeFileSync(
    join(directory, ".agents", "plugins", "marketplace.json"),
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

  cpSync(join(PACKAGE_ROOT, "plugin"), join(directory, CODEX_MARKETPLACE_PLUGIN_PATH), {
    recursive: true,
    force: true,
    verbatimSymlinks: false,
    filter: shouldCopyPath,
  });

  for (const rel of ["README.md", "LICENSE", "package.json"]) {
    const src = join(PACKAGE_ROOT, rel);
    if (existsSync(src)) {
      cpSync(src, join(directory, rel), {
        recursive: true,
        force: true,
        verbatimSymlinks: false,
      });
    }
  }
  return directory;
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
  rmSync(CODEX_RUNTIME_PACKAGE_DIR, { recursive: true, force: true });
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
  const existing = existsSync(CODEX_CONFIG_PATH) ? readFileSync(CODEX_CONFIG_PATH, "utf8") : "";
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
  const text = existsSync(CODEX_CONFIG_PATH) ? readFileSync(CODEX_CONFIG_PATH, "utf8") : "";
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
      if (featureRe.test(out[i])) {
        hasFeature = true;
        break;
      }
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
  const existing = existsSync(CODEX_CONFIG_PATH) ? readFileSync(CODEX_CONFIG_PATH, "utf8") : "";
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
    while (true) {
      const newline = buffer.indexOf("\n");
      if (newline < 0) break;
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
    const response = await client.request(2, "hooks/list", { cwds: [cwd] }, CODEX_CLI_TIMEOUT_MS);
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
    return typeof manifest.version === "string" && manifest.version ? manifest.version : null;
  } catch {
    return null;
  }
}

function requireCodexPluginVersion(pluginRoot) {
  const version = codexPluginVersion(pluginRoot);
  if (!version) {
    throw new Error(`missing version in ${join(pluginRoot, ".codex-plugin", "plugin.json")}`);
  }
  return version;
}

function installCodexPluginCache(pluginRoot) {
  const cacheDir = join(CODEX_PLUGIN_CACHE_DIR, requireCodexPluginVersion(pluginRoot));
  holdPackageInstallLock(cacheDir, "Codex");
  const staged = uniquePackagePath(cacheDir, ".claude-smart-copy");
  try {
    cpSync(pluginRoot, staged, {
      recursive: true,
      force: true,
      verbatimSymlinks: false,
      filter: shouldCopyPath,
    });
    replaceLocalPluginPackage(staged, cacheDir);
    setCodexPluginEnabled();
    return cacheDir;
  } catch (err) {
    if (!pendingPreviousPackages.has(cacheDir)) releasePackageInstallLock(cacheDir);
    throw err;
  } finally {
    rmSync(staged, { recursive: true, force: true });
  }
}

function codexRegistrationCache(pluginRoot, home) {
  const version = codexPluginVersion(pluginRoot);
  if (!version || basename(version) !== version || version === "." || version === "..")
    return null;
  return join(home, "plugins", "cache", CODEX_MARKETPLACE_NAME, "claude-smart", version);
}

function preparedCodexRegistration(id, home = CODEX_HOME) {
  const { pluginRoot } = readGuidedRuntime(id);
  const cache = codexRegistrationCache(pluginRoot, home);
  return !!cache && boundGuidedPluginRoot(cache) === pluginRoot &&
    codexRegistrationEnabled(pluginRoot, home);
}

function standaloneCodexRegistration(pluginRoot, home) {
  const cache = codexRegistrationCache(pluginRoot, home);
  return !!cache && !pathEntryExists(join(cache, GUIDED_RUNTIME_BINDING)) &&
    codexPluginVersion(cache) === codexPluginVersion(pluginRoot) &&
    existsSync(join(cache, "hooks", "codex-hooks.json")) &&
    isExecutableFile(pluginPythonPath(pluginRoot)) &&
    codexRegistrationEnabled(pluginRoot, home);
}

function codexRegistrationEnabled(pluginRoot, home) {
  const config = join(home, "config.toml");
  if (!existsSync(config)) return false;
  // Check the host's actual configuration, including TOML quoting and boolean
  // types, without printing any values. The completed cache already binds the
  // installed hook manifest to this exact prepared generation.
  const enabled = `
import sys, tomllib
try:
    with open(sys.argv[1], "rb") as stream:
        plugin = tomllib.load(stream)["plugins"][sys.argv[2]]
    sys.exit(0 if plugin.get("enabled") is True else 1)
except (OSError, ValueError, KeyError, AttributeError, TypeError):
    sys.exit(1)
`;
  const result = spawnSync(pluginPythonPath(pluginRoot), [
    "-I", "-X", "utf8", "-c", enabled, config, CODEX_PLUGIN_ID,
  ], { cwd: pluginRoot, stdio: "ignore", timeout: 5000, windowsHide: true });
  return result.status === 0;
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
  process.stdout.write("Updating claude-smart by reinstalling from this package...\n");
  await runInstall(args, { retryInstallAfterUninstall: true });
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
  const marketplaceCode = await runClaude([
    "plugin",
    "marketplace",
    "remove",
    CODEX_MARKETPLACE_NAME,
  ]);
  if (marketplaceCode !== 0) {
    process.stderr.write(
      `warning: could not remove the ${CODEX_MARKETPLACE_NAME} marketplace; remove it with: ` +
        `claude plugin marketplace remove ${CODEX_MARKETPLACE_NAME}\n`,
    );
  }
  rmSync(CLAUDE_CODE_LOCAL_PACKAGE_DIR, { recursive: true, force: true });
  removeClaudeCodeVenvs();
  repairPluginRoot();

  process.stdout.write(
    [
      "",
      "claude-smart uninstalled. Run /reload-plugins in Claude Code, or apply next session.",
      ...LOCAL_DATA_NOTICE,
      "",
    ].join("\n"),
  );
}

function receivePreparedInstallSettings() {
  // A closed IPC channel still identifies a guide worker. Refuse to install
  // against legacy settings if the guide disconnected before sending them.
  if (typeof process.send !== "function") return null;
  return new Promise((resolve, reject) => {
    const disconnected = () => reject(new Error("Installation guide disconnected before passing settings."));
    if (!process.connected) return disconnected();
    process.once("disconnect", disconnected);
    process.once("message", (message) => {
      process.removeListener("disconnect", disconnected);
      resolve(message);
    });
  });
}

function renderSharedClaudeHooks(path, pluginRoot, readOnly) {
  const manifest = JSON.parse(readFileSync(path, "utf8"));
  if (readOnly) removePublishHooks(manifest);
  // Setup was the standalone dependency installer. These hooks only launch
  // the prepared runtime and do not republish account selection.
  delete manifest.hooks.Setup;
  for (const blocks of Object.values(manifest.hooks)) {
    for (const block of blocks) {
      block.hooks = block.hooks.flatMap((hook) => {
        if (hook.command?.includes("ensure-plugin-root.sh")) return [];
        const match = hook.command?.match(/bash "\$_R\/scripts\/([a-z_-]+\.sh)"(.*)$/);
        if (!match) throw new Error("Unrecognized bundled Claude hook command");
        const [, script, args] = match;
        const file = join(pluginRoot, "scripts", script);
        const target = (isWindows() ? file.replaceAll("\\", "/") : file).replaceAll("'", "'\\''");
        return [{ ...hook, command: `bash '${target}'${args}` }];
      });
    }
  }
  return JSON.stringify(manifest, null, 2) + "\n";
}

async function runInstall(args, options = {}) {
  const host = parseHost(args);
  const message = await receivePreparedInstallSettings();
  const runtime = message && Object.hasOwn(message, "generation")
    ? readGuidedRuntime(message.generation) : null;
  const settings = runtime ? null : message;
  if (runtime) {
    verifyLocalPluginPackage(runtime.packageRoot, "guided runtime");
    if (process.connected) process.disconnect();
  }
  if (host === HOST_CODEX) {
    await runInstallCodex(settings, runtime);
    return;
  }
  if (host === HOST_OPENCODE) {
    await runInstallOpenCode(args, settings, runtime, message?.openCodePlan);
    return;
  }

  if (!hasClaudeCli()) {
    process.stderr.write(
      "error: 'claude' CLI not found on PATH. " +
        "Install Claude Code first: https://claude.com/claude-code\n",
    );
    process.exit(1);
  }

  assertSupportedRuntimePlatform();
  if (!runtime) snapshotInstallState();
  const setup = readInstallPolicy();
  const readOnly = setup.readOnly;
  // Stop services running from the previous root (e.g. a pruned npx dir or an
  // older copy) before the copy under them is replaced.
  const previousRoot = activePluginRoot();
  // Restart with the host that root was running for (e.g. Codex's bridge).
  if (!runtime && previousRoot) await stopServicesForInstall(previousRoot, installStateSnapshot.restartHost);
  let source;
  try {
    source = runtime
      ? publishGuidedPackageAdapter(
        CLAUDE_CODE_LOCAL_PACKAGE_DIR, runtime.id, (stage) => join(stage, "plugin"), "Claude Code adapter",
      )
      : installLocalPluginPackage(CLAUDE_CODE_LOCAL_PACKAGE_DIR, "Claude Code");
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
    if (runtime) {
      pluginRoot = runtime.pluginRoot;

    } else {
      pluginRoot = await bootstrapClaudeCodeInstall(join(source, "plugin"));
      restorePublishHooksFromSource(pluginRoot);
      if (readOnly && prunePublishHooksForReadOnly(pluginRoot)) {
        process.stdout.write(
          "Installed read-only hook manifest; publish interactions hooks are disabled.\n",
        );
      }
      forcePluginRoot(pluginRoot);
    }
    process.stdout.write(`Prepared claude-smart runtime at ${pluginRoot}.\n`);
    markLocalPluginPackagePrepared(CLAUDE_CODE_LOCAL_PACKAGE_DIR);
  } catch (err) {
    // The exit rollback restores the previous plugin-root (install snapshot).
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
      process.stderr.write(`error: \`claude ${step.args.join(" ")}\` failed (exit ${code})\n`);
      process.exit(code);
    }
  }

  // `plugin install` is a no-op for an already-installed plugin: Claude Code
  // keeps its recorded version (shown by `claude plugin list` and /plugin)
  // even though the plugin loads in place from the updated copy. `plugin
  // update` re-records it from the local marketplace -- no network, and a
  // no-op when already current. Non-fatal: the runtime is already correct.
  const recordCode = await runClaude(["plugin", "update", PLUGIN_SPEC], {
    spinnerLabel: "Recording claude-smart version…",
  });
  if (recordCode !== 0) {
    process.stderr.write(
      `warning: \`claude plugin update ${PLUGIN_SPEC}\` failed (exit ${recordCode}); ` +
        "claude-smart is installed, but Claude Code may still list the previous version. " +
        `Run \`claude plugin update ${PLUGIN_SPEC}\` to refresh it.\n`,
    );
  }

  commitLocalPluginPackages([CLAUDE_CODE_LOCAL_PACKAGE_DIR]);
  startDeferredDashboardBuild(pluginRoot);
  await startAndReportServices(pluginRoot, HOST_CLAUDE_CODE, setup);

  process.stdout.write(
    [
      "",
      "claude-smart installed and dependencies are prepared.",
      "Run /reload-plugins in your existing Claude Code session, or activate the update next session.",
      "Background services are refreshed separately; plugin activation does not require restarting Claude Code.",
      // Only claim a local backend where one will actually run.
      setup.managed || (setup.url && !isBundledBackendUrl(setup.url))
        ? "The dashboard auto-starts on session start; no bundled backend runs in this setup."
        : "The reflexio backend and dashboard auto-start on session start.",
      "Opt out with CLAUDE_SMART_BACKEND_AUTOSTART=0 or CLAUDE_SMART_DASHBOARD_AUTOSTART=0.",
      "",
    ].join("\n"),
  );
}

function validateGuidedCodexHookManifest(manifest) {
  for (const event of ["SessionStart", "UserPromptSubmit"]) {
    const groups = manifest?.hooks?.[event];
    if (!Array.isArray(groups) || !groups.some((group) =>
      Array.isArray(group?.hooks) && group.hooks.some((hook) =>
        hook?.type === "command" && typeof hook.command === "string" && hook.command.trim(),
      ),
    )) throw new Error(`Codex hook manifest is missing a command for ${event}`);
  }
}

function renderPreparedCodexHooks(pluginRoot, launcher, readOnly, shared = false) {
  const bundled = join(PACKAGE_ROOT, "plugin", "hooks", "codex-hooks.json");
  const manifest = JSON.parse(readFileSync(bundled, "utf8"));
  const hooks = renderCodexHooksForNode(manifest, pluginRoot, launcher, readOnly, { shared });
  if (shared) validateGuidedCodexHookManifest(JSON.parse(hooks));
  return hooks;
}

async function runInstallCodex(settings, runtime) {
  if (!hasCli("codex")) {
    process.stderr.write("error: 'codex' CLI not found on PATH. Install Codex first.\n");
    process.exit(1);
  }
  assertSupportedRuntimePlatform();
  const codexError = codexHookProcessCleanupError();
  if (codexError) throw new Error(codexError);
  if (!runtime) snapshotInstallState({ preserveFailureMarker: Boolean(settings) });
  let setup = settings ? null : readInstallPolicy();
  const pluginRoot = runtime?.pluginRoot ?? CODEX_RUNTIME_PLUGIN_ROOT;

  validateCodexPackage();
  if (!runtime) rememberCodexHome();
  const previousRoot = existsSync(CODEX_RUNTIME_PLUGIN_ROOT)
    ? CODEX_RUNTIME_PLUGIN_ROOT
    : findCodexPluginRoot();
  if (!runtime) await stopServicesForInstall(previousRoot, installStateSnapshot.restartHost);
  let renderedHooks;
  try {
    if (!runtime) {
      installLocalPluginPackage(CODEX_RUNTIME_PACKAGE_DIR, "Codex runtime");
      const nodeRuntime = await ensurePrivateNode();
      const launcher = pinHookNode(nodeRuntime.node);
      await bootstrapPluginRuntime(pluginRoot, { settings, nodeRuntime });
      const readOnly = settings
        ? settings.request.updates[CLAUDE_SMART_READ_ONLY_ENV] === "1"
        : setup.readOnly;
      renderedHooks = renderPreparedCodexHooks(pluginRoot, launcher, readOnly);
      if (settings) {
        validateGuidedCodexHookManifest(JSON.parse(renderedHooks));
        await publishFromPreparedRuntime(pluginRoot, settings);
        setup = readInstallPolicy();
      }
    }
  } catch (err) {
    process.stderr.write(
      `error: could not prepare claude-smart Codex runtime: ${err && err.message ? err.message : err}\n`,
    );
    process.exit(1);
  }
  const readOnly = setup.readOnly;
  const marketplaceRoot = CODEX_MARKETPLACE_DIR;
  if (runtime) {
    publishGuidedPackageAdapter(marketplaceRoot, runtime.id, codexMarketplacePluginRoot, "Codex marketplace");
  } else {
    copyCodexMarketplace();
    if (readOnly) prunePublishHooksForReadOnly(codexMarketplacePluginRoot(marketplaceRoot));
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
    if (!runtime) {
      writeFileSync(
        join(codexMarketplacePluginRoot(marketplaceRoot), "hooks", "codex-hooks.json"),
        renderedHooks,
      );
    }
    cacheDir = installCodexPluginCache(codexMarketplacePluginRoot(marketplaceRoot));
    if (!runtime) ensurePluginRoot(pluginRoot);
    process.stdout.write(`Installed Codex plugin cache at ${cacheDir}.\n`);
    if (readOnly) {
      process.stdout.write(
        "Installed read-only hook manifest; publish interactions hooks are disabled.\n",
      );
    }
    // Preparation and publication succeeded; discard the rollback cache only now.
    commitLocalPluginPackages([cacheDir, runtime ? marketplaceRoot : CODEX_RUNTIME_PACKAGE_DIR]);
    await startAndReportServices(pluginRoot, HOST_CODEX, setup);
  } catch (err) {
    process.stderr.write(
      `error: automatic Codex plugin install failed: ${err && err.message ? err.message : err}\n`,
    );
    process.stderr.write(
      `In Codex, run /plugins, install claude-smart from the ${CODEX_MARKETPLACE_DISPLAY_NAME} marketplace, review /hooks, then start a new session.\n`,
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
      `Run /hooks in Codex to review and trust the claude-smart hooks, then start a new session.\n`,
    );
    process.exit(1);
  } else {
    process.stdout.write(`Trusted and enabled ${trustedHookCount} claude-smart Codex hooks.\n`);
  }

  process.stdout.write(
    [
      "",
      "claude-smart Codex support is installed.",
      `Start a new Codex session to load the installed plugin. You can finish your current session first.`,
      `/plugins should show claude-smart as installed from the ${CODEX_MARKETPLACE_DISPLAY_NAME} marketplace.`,
      "Run npx claude-smart status --host codex to inspect versions observed in recent session hooks.",
      "Local data is shared with Claude Code under ~/.reflexio/ and ~/.claude-smart/.",
      "",
    ].join("\n"),
  );
}

function assertOpenCodeInstallConfiguration(args) {
  const python = openCodeScopeInterpreter();
  if (!python) return; // A fresh bootstrap supplies the interpreter for the scoped plan.
  const { configs, sources } = readScopedOpenCodeInstallation(args, null, python);
  for (const [index, file] of configs.entries())
    parseOpenCodePluginConfig(file, sources[index].contents);
}

function readInstallFailureFingerprint() {
  try {
    const stat = lstatSync(INSTALL_FAILURE_MARKER, { bigint: true });
    if (!stat.isFile()) return null;
    const text = readFileSync(INSTALL_FAILURE_MARKER, "utf8");
    const fingerprint = text.match(/^fingerprint=([0-9a-f]{64})\r?$/m)?.[1];
    return fingerprint ? { stat, text, fingerprint } : null;
  } catch {
    return null;
  }
}

function clearRecoveredOpenCodeFailure(pluginRoot, previous) {
  if (!previous) return;
  const bash = resolveUsableBash();
  if (!bash) return;
  // Use the same fingerprint implementation as the hook and shell installer.
  const probe = spawnSync(bash, [
    "-c",
    '. ./scripts/_lib.sh && root="$(pwd -P)" && claude_smart_install_fingerprint_hash "$root" "$root/scripts"',
  ], {
    cwd: pluginRoot, env: runtimeEnv(), encoding: "utf8", timeout: 5000, windowsHide: true,
  });
  if (probe.status !== 0 || probe.stdout.trim() !== previous.fingerprint) return;
  const current = readInstallFailureFingerprint();
  if (current && current.text === previous.text &&
      current.stat.ino === previous.stat.ino && current.stat.dev === previous.stat.dev &&
      current.stat.ctimeNs === previous.stat.ctimeNs) {
    try {
      rmSync(INSTALL_FAILURE_MARKER, { force: true });
    } catch {
      process.stderr.write("warning: could not clear the previous OpenCode install failure.\n");
    }
  }
}

async function runInstallOpenCode(args, settings, runtime, scopedPlan) {
  let setup = settings ? null : readInstallPolicy();
  const prerequisiteError = opencodePrerequisiteError();
  if (prerequisiteError) {
    process.stderr.write(prerequisiteError);
    process.exit(1);
  }
  assertSupportedRuntimePlatform();
  if (!runtime) snapshotInstallState({ preserveFailureMarker: Boolean(settings) });
  const previousFailure = settings ? readInstallFailureFingerprint() : null;
  const readOnly = settings
    ? settings.request.updates[CLAUDE_SMART_READ_ONLY_ENV] === "1"
    : setup.readOnly;
  if (!hasExtractionProvider()) {
    process.stderr.write(extractionProviderError());
    process.exit(1);
  }

  if (!runtime) assertOpenCodeInstallConfiguration(args);
  const installedRoot = join(OPENCODE_LOCAL_PACKAGE_DIR, "plugin");
  const previousRoot = existsSync(installedRoot) ? installedRoot : null;
  if (!runtime) await stopServicesForInstall(previousRoot, installStateSnapshot.restartHost);
  let packageRoot;
  try {
    packageRoot = runtime?.packageRoot ?? installOpenCodePluginPackage();
  } catch (err) {
    process.stderr.write(
      `error: could not prepare claude-smart OpenCode package: ${err && err.message ? err.message : err}\n`,
    );
    process.exit(1);
  }
  const pluginRoot = join(packageRoot, "plugin");
  if (!runtime) await bootstrapPluginRuntime(pluginRoot, { readOnly, settings });
  const plan = runtime ? scopedPlan : planScopedOpenCode(args, packageRoot);
  if (plan?.loader?.path !== opencodeV2PluginFile(args)) throw new Error("OpenCode scope changed");
  if (settings) {
    await publishFromPreparedRuntime(pluginRoot, settings, plan.loader);
    setup = readInstallPolicy();
  }
  if (!runtime) ensurePluginRoot(pluginRoot);
  stageOpenCodeRegistration(plan.loader.path, runtime);
  runScopedOpenCode(pluginRoot, "publish", plan.edits);
  // Keep the prepared runtime once a loader can refer to it.
  commitLocalPluginPackages([packageRoot]);
  publishOpenCodeRegistration(plan.loader.path, runtime);
  const results = plan.results;
  if (readOnly) {
    process.stdout.write(
      "Installed read-only hook manifest; publish interactions hooks are disabled.\n",
    );
  }
  clearRecoveredOpenCodeFailure(pluginRoot, previousFailure);
  await startAndReportServices(pluginRoot, HOST_OPENCODE, setup);
  reportOpenCodeConfigBackups(results);
  process.stdout.write(
    [
      "",
      ...results.map(
        (result) =>
          `Removed the legacy claude-smart plugin entry from ${result.configPath}; the loader replaces it.`,
      ),
      `Prepared claude-smart OpenCode package at ${packageRoot}.`,
      `Wrote the OpenCode loader at ${plan.loader.path}; OpenCode 1.x and V2 both load the plugin from there.`,
      "claude-smart OpenCode support is installed.",
      "Restart OpenCode in your project so it loads the plugin.",
      "",
    ].join("\n"),
  );
}

async function runUninstallCodex() {
  stopClaudeSmartServices(CODEX_RUNTIME_PLUGIN_ROOT);
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
      "claude-smart Codex plugin and marketplace state removed. Start a new Codex session to apply.",
      "Codex's global hook feature flags were left in place.",
      ...LOCAL_DATA_NOTICE,
      "",
    ].join("\n"),
  );
}

function reportOpenCodeConfigBackups(results) {
  for (const result of results) {
    if (result.backupPath) {
      process.stdout.write(
        `Saved a comment-preserving backup of your previous config at ${result.backupPath}.\n`,
      );
    }
  }
}

async function runUninstallOpenCode(args) {
  const python = requireOpenCodeScopeInterpreter();
  const plan = planScopedOpenCode(args, null, python);
  runScopedOpenCode(null, "publish", plan.edits, python);
  stopClaudeSmartServices(join(PACKAGE_ROOT, "plugin"));
  stopClaudeSmartServices(join(OPENCODE_LOCAL_PACKAGE_DIR, "plugin"));
  const results = plan.results;
  reportOpenCodeConfigBackups(results);
  // Both scopes share one package copy and every loader re-exports its
  // server.mjs, so keep it while any other scope still references it: the global
  // scope for a project uninstall, and the current project for a --global one.
  // shortcut: loaders in other projects are not enumerable, so a --global
  // uninstall can still strand them; upgrade when installs are tracked machine-wide.
  publishOpenCodeRegistration(opencodeV2PluginFile(args), null);
  const otherScopeInstalled = scopeHasClaudeSmart(parseGlobalFlag(args) ? [] : ["--global"]);
  if (otherScopeInstalled === false) {
    rmSync(OPENCODE_LOCAL_PACKAGE_DIR, { recursive: true, force: true });
    repairPluginRoot();
    try {
      rmdirSync(dirname(OPENCODE_LOCAL_PACKAGE_DIR));
    } catch {
      // Keep the parent when it still contains future OpenCode state.
    }
  }
  process.stdout.write(
    [
      "",
      ...(results.length
        ? results.map(
            (result) => `Removed claude-smart OpenCode plugin entries from ${result.configPath}.`,
          )
        : ["OpenCode config did not include claude-smart."]),
      "Restart OpenCode to apply.",
      ...LOCAL_DATA_NOTICE,
      "",
    ].join("\n"),
  );
}

// Read-only: lists claude-smart hook keys that Codex records as disabled in
// config.toml (`[hooks.state."<key>"]` with `enabled = false`, or the inline
// `"<key>" = { enabled = false }` form under `[hooks.state]`). A disabled hook
// silently stops capture or injection, and nothing else reports it.
function disabledCodexHooks(configPath = CODEX_CONFIG_PATH) {
  let text;
  try {
    text = readFileSync(configPath, "utf8");
  } catch {
    return [];
  }
  const prefix = `${CODEX_PLUGIN_ID}:`;
  const unquote = (value) => value.replace(/\\(["\\])/g, "$1");
  const disabled = new Set();
  let section = null;
  for (const rawLine of text.split(/\r?\n/)) {
    const line = rawLine.trim();
    const header = line.match(/^\[\s*hooks\.state\.\s*"((?:[^"\\]|\\.)*)"\s*\]\s*(?:#.*)?$/);
    if (header) {
      section = unquote(header[1]);
      continue;
    }
    if (/^\[/.test(line)) {
      section = /^\[\s*hooks\.state\s*\]/.test(line) ? "" : null;
      continue;
    }
    if (section === null) continue;
    if (section) {
      if (section.startsWith(prefix) && /^enabled\s*=\s*false\b/.test(line)) disabled.add(section);
      continue;
    }
    const inline = line.match(/^"((?:[^"\\]|\\.)*)"\s*=\s*\{(.*)\}\s*(?:#.*)?$/);
    if (
      inline &&
      unquote(inline[1]).startsWith(prefix) &&
      /(?:^|[,{\s])enabled\s*=\s*false\b/.test(inline[2])
    ) {
      disabled.add(unquote(inline[1]));
    }
  }
  return [...disabled].sort();
}

function writeDisabledCodexHookWarnings(configPath = CODEX_CONFIG_PATH) {
  const disabled = disabledCodexHooks(configPath);
  if (!disabled.length) return;
  process.stdout.write(
    `Warning: ${disabled.length} claude-smart Codex hook${disabled.length === 1 ? " is" : "s are"} ` +
      `disabled in ${configPath}; learning capture or context injection is skipped for ${disabled.length === 1 ? "it" : "them"}:\n`,
  );
  for (const key of disabled) {
    const event = key.slice(CODEX_PLUGIN_ID.length + 1).split(":")[1] || "unknown event";
    process.stdout.write(`  ${key} (${event})\n`);
  }
  process.stdout.write(
    "  Re-enable in Codex with /hooks, or set `enabled = true` in that hook's " +
      '[hooks.state."<key>"] section, then start a new Codex session. ' +
      "Rerunning `npx claude-smart install --host codex` also re-enables them.\n",
  );
}

function localOpenCodeFilePath(spec) {
  try {
    const url = new URL(spec);
    if (url.protocol !== "file:" || url.host || url.pathname.startsWith("//")) return null;
    return fileURLToPath(url);
  } catch {
    return null;
  }
}

function installedOpenCodeRuntime(args) {
  const path = opencodeV2PluginFile(args);
  const binding = openCodeRegistrationPath(path);
  const recovered = recoverOpenCodeBinding(binding);
  if (recovered) return recovered.pluginRoot;
  return readOpenCodeBinding(binding)?.pluginRoot || null;
}

function openCodeInstalledVersion(args) {
  const root = installedOpenCodeRuntime(args);
  if (!root) return codexPluginVersion(join(OPENCODE_LOCAL_PACKAGE_DIR, "plugin"));
  try {
    const directory = join(root, ".codex-plugin");
    if (!lstatSync(directory).isDirectory() ||
        !lstatSync(join(directory, "plugin.json")).isFile()) return null;
    return codexPluginVersion(root);
  } catch {
    return null;
  }
}

function runStatus(args) {
  const host = parseHost(args);
  let installed = null;
  let claudeInstallations = [];
  let inventoryAvailable = false;
  if (host === HOST_OPENCODE) {
    const version = openCodeInstalledVersion(args);
    if (version) installed = { version };
    inventoryAvailable = true;
  } else {
    const cli = host === HOST_CODEX ? "codex" : "claude";
    const command = ["plugin", "list", "--json"];
    if (host === HOST_CODEX) command.push("--marketplace", CODEX_MARKETPLACE_NAME);
    const result = spawnSync(cli, command, {
      encoding: "utf8",
      timeout: CODEX_CLI_TIMEOUT_MS,
      windowsHide: true,
      stdio: ["ignore", "pipe", "ignore"],
    });
    if (result.status === 0) {
      try {
        const data = JSON.parse(result.stdout);
        const plugins = host === HOST_CODEX ? data.installed : data;
        if (Array.isArray(plugins)) {
          const matches = plugins.filter(
            (plugin) => (plugin.pluginId || plugin.id) === PLUGIN_SPEC,
          );
          if (host === HOST_CLAUDE_CODE) claudeInstallations = matches;
          else installed = matches[0];
          inventoryAvailable = true;
        }
      } catch {
        // Unknown host inventory formats must not be mistaken for an uninstall.
      }
    }
  }
  process.stdout.write(`Host: ${host}\n`);
  if (host === HOST_CODEX) process.stdout.write(`Codex home: ${CODEX_HOME}\n`);
  const versionLabel = host === HOST_OPENCODE ? "Prepared runtime version" : "Installed version";
  const version = installed
    ? installed.version || "unknown (host reported no version)"
    : inventoryAvailable
      ? "not installed"
      : "unknown (host inventory unavailable)";
  if (claudeInstallations.length) {
    process.stdout.write("Claude Code registrations (session activation is unknown):\n");
    for (const plugin of claudeInstallations) {
      const scope = plugin.scope || "unknown";
      const project = plugin.projectPath ? `, project: ${plugin.projectPath}` : "";
      const enabled = typeof plugin.enabled === "boolean" ? String(plugin.enabled) : "unknown";
      const projectEnabled =
        typeof plugin.projectEnabled === "boolean"
          ? `, project enabled: ${plugin.projectEnabled}`
          : "";
      process.stdout.write(
        `  Installed version: ${plugin.version || "unknown (host reported no version)"} ` +
          `(scope: ${scope}${project}, enabled: ${enabled}${projectEnabled})\n`,
      );
    }
  } else {
    process.stdout.write(`${versionLabel}: ${version}\n`);
    if (installed?.enabled === false) process.stdout.write("Plugin is disabled in the host.\n");
  }
  if (host === HOST_CODEX) writeDisabledCodexHookWarnings();

  // Match only this host and account. The shared log is bounded by hook_log;
  // read its last 5 MiB even if an older writer left a larger file behind.
  const logOverride = (process.env.CLAUDE_SMART_HOOK_LOG || "").trim();
  const logPath =
    logOverride && !["1", "true", "yes", "on"].includes(logOverride.toLowerCase())
      ? logOverride
      : join(CLAUDE_SMART_STATE_DIR, "hook.log");
  const sessions = new Map();
  try {
    const fd = openSync(logPath, constants.O_RDONLY | constants.O_NONBLOCK);
    try {
      const stat = fstatSync(fd);
      if (!stat.isFile()) throw new Error("Hook history is not a regular file");
      const size = stat.size;
      const length = Math.min(size, 5 * 1024 * 1024);
      const buffer = Buffer.alloc(length);
      const count = readSync(fd, buffer, 0, length, size - length);
      const lines = buffer.subarray(0, count).toString("utf8").split(/\r?\n/);
      if (size > length) lines.shift();
      for (let i = lines.length - 1; i >= 0 && sessions.size < 5; i--) {
        let entry;
        try {
          entry = JSON.parse(lines[i]);
        } catch {
          continue;
        }
        if (!entry || entry.host !== host || entry.internal_skipped || !entry.session_id) continue;
        if (host === HOST_CODEX && entry.codex_home !== CODEX_HOME) continue;
        if (entry.event === "publish-result") continue;
        if (!sessions.has(entry.session_id)) sessions.set(entry.session_id, entry);
      }
    } finally {
      closeSync(fd);
    }
  } catch {
    // Missing history is normal on first install.
  }
  process.stdout.write("Recent session hook observations (sessions may have ended):\n");
  if (!sessions.size)
    process.stdout.write("  None recorded for this host/account; active version is unknown.\n");
  for (const [session, entry] of sessions) {
    process.stdout.write(
      `  ${session}: ${entry.plugin_version || "unknown"} (${entry.event}, ts=${entry.ts})\n`,
    );
  }
  if (host === HOST_CLAUDE_CODE)
    process.stdout.write(
      "Activate updates with /reload-plugins in Claude Code, or next session.\n",
    );
  if (host === HOST_CODEX)
    process.stdout.write(
      "Activate updates in a new Codex session; review changed hooks in /hooks.\n",
    );
  if (host === HOST_OPENCODE) process.stdout.write("Restart OpenCode to activate updates.\n");
}

async function main() {
  const args = process.argv.slice(2);
  const cmd = args[0] || "install";
  if (cmd === "setup" && process.env.CLAUDE_SMART_INSTALL_CHILD === "1") {
    throw new Error(
      "Setup uses the public guide; unset the internal CLAUDE_SMART_INSTALL_CHILD marker and run npx claude-smart setup.",
    );
  }

  if (cmd === "help" || cmd === "--help" || cmd === "-h") {
    printHelp();
    return;
  }

  if (cmd === "status") {
    runStatus(args.slice(1));
    return;
  }

  if (cmd === "install") {
    if (process.env.CLAUDE_SMART_INSTALL_CHILD === "1") await runInstall(args.slice(1));
    else await runGuidedCommand(cmd, args.slice(1));
    return;
  }

  if (cmd === "update") {
    if (process.env.CLAUDE_SMART_INSTALL_CHILD === "1") await runUpdate(args.slice(1));
    else await runGuidedCommand(cmd, args.slice(1));
    return;
  }

  if (cmd === "setup") {
    await runGuidedCommand(cmd, args.slice(1));
    return;
  }

  if (cmd === "uninstall") {
    if (process.env.CLAUDE_SMART_INSTALL_CHILD === "1") await runUninstall(args.slice(1));
    else await runGuidedCommand(cmd, args.slice(1));
    return;
  }

  process.stderr.write(`claude-smart: unknown command '${cmd}'. Try 'npx claude-smart --help'.\n`);
  process.exit(1);
}

if (require.main === module) {
  main().catch((err) => {
    process.stderr.write(`claude-smart: ${err && err.message ? err.message : err}\n`);
    process.exit(1);
  });
}

module.exports = {
  installedPluginRoots,
  publishInstallSettings,
  reusePreparedRuntime,
  pinHookNode,
  learningProviderVersion,
  assertSupportedRuntimePlatform,
  bootstrapPluginRuntime,
  installVendoredReflexio,
  codexMarketplacePluginRoot,
  copyCodexMarketplace,
  disabledCodexHooks,
  ensurePrivateNode,
  ensureUv,
  renderCodexHooksForNode,
  opencodeScopeConfigPaths,
  removeOpenCodeLegacyEntries,
  openCodeV2LoaderConflicts,
  opencodeLocalPluginSpec,
  writeOpenCodeV2Plugin,
  removeOpenCodeV2Plugin,
  installOpenCodePluginPackage,
  patchOpenCodePluginConfig,
  parseHost,
  hasExtractionProvider,
  hasOpenCodeCli,
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
