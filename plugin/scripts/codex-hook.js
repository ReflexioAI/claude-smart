#!/usr/bin/env node
"use strict";

const { spawn, spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const HOME = os.homedir();
const STATE_DIR = path.join(HOME, ".claude-smart");
const REFLEXIO_DIR = path.join(HOME, ".reflexio");
const SERVICE_SCRIPT_TIMEOUT_MS = parsePositiveInteger(
  process.env.CLAUDE_SMART_SERVICE_SCRIPT_TIMEOUT_MS,
  45_000,
);
const DEFAULT_BACKEND_PORT = parsePort(process.env.BACKEND_PORT, 8071);
const DEFAULT_EMBEDDING_PORT = parsePort(process.env.EMBEDDING_PORT, 8072);
const DASHBOARD_PORT = parsePort(process.env.DASHBOARD_PORT, parsePort(process.env.PORT, 3001));
const LOG_MAX_BYTES = 10000000;

function parsePort(value, fallback) {
  const port = Number(value);
  return Number.isInteger(port) && port > 0 && port <= 65535 ? port : fallback;
}

function parsePositiveInteger(value, fallback) {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : fallback;
}

function emitOk() {
  process.stdout.write('{"continue":true}\n');
}

function emitHookOk() {
  process.stdout.write('{"continue":true}\n');
}

function emitNormalizedHookOutput(stdout) {
  const lines = String(stdout || "")
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
  if (lines.length === 0) {
    emitHookOk();
    return;
  }

  const merged = {};
  for (const line of lines) {
    let parsed;
    try {
      parsed = JSON.parse(line);
    } catch {
      appendLog("backend.log", `[claude-smart] codex hook emitted non-JSON stdout: ${line.slice(0, 500)}`);
      emitHookOk();
      return;
    }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      appendLog("backend.log", `[claude-smart] codex hook emitted non-object JSON stdout: ${line.slice(0, 500)}`);
      emitHookOk();
      return;
    }
    Object.assign(merged, parsed);
  }

  if (!Object.prototype.hasOwnProperty.call(merged, "continue")) {
    merged.continue = true;
  }
  delete merged.suppressOutput;
  process.stdout.write(`${JSON.stringify(merged)}\n`);
}

function ensureDir(dir) {
  fs.mkdirSync(dir, { recursive: true });
}

function trimLog(file) {
  if (!Number.isFinite(LOG_MAX_BYTES) || LOG_MAX_BYTES < 1) return;
  let stat;
  try {
    stat = fs.statSync(file);
  } catch {
    return;
  }
  if (!stat.isFile() || stat.size <= LOG_MAX_BYTES) return;
  const fd = fs.openSync(file, "r");
  try {
    const buffer = Buffer.alloc(LOG_MAX_BYTES);
    fs.readSync(fd, buffer, 0, LOG_MAX_BYTES, stat.size - LOG_MAX_BYTES);
    fs.writeFileSync(file, buffer);
  } finally {
    fs.closeSync(fd);
  }
}

function appendLog(name, line) {
  ensureDir(STATE_DIR);
  const file = path.join(STATE_DIR, name);
  trimLog(file);
  fs.appendFileSync(file, `${line}\n`);
  trimLog(file);
}

function realDir(dir) {
  try {
    if (!fs.statSync(dir).isDirectory()) return null;
    return fs.realpathSync(dir);
  } catch {
    return null;
  }
}

function isInsideDir(parent, child) {
  const rel = path.relative(parent, child);
  return rel && !rel.startsWith("..") && !path.isAbsolute(rel);
}

function isPluginLikeRoot(root) {
  return fs.existsSync(path.join(root, "pyproject.toml")) && fs.existsSync(path.join(root, "scripts"));
}

function isReflexioStrayPluginCopy(root) {
  if (!isPluginLikeRoot(root)) return false;
  const rootReal = realDir(root);
  const reflexioReal = realDir(REFLEXIO_DIR);
  if (!rootReal || !reflexioReal) return false;
  return isInsideDir(reflexioReal, rootReal);
}

function stablePluginRootForStrayCopy(root) {
  if (!isReflexioStrayPluginCopy(root)) return null;
  const rootReal = realDir(root);
  const candidates = [
    path.join(REFLEXIO_DIR, "plugin-root"),
    path.join(HOME, ".claude", "plugins", "marketplaces", "reflexioai", "plugin"),
    path.join(HOME, ".codex", "plugins", "cache", "reflexioai", "claude-smart", "current"),
  ];
  for (const cacheRoot of [
    path.join(HOME, ".claude", "plugins", "cache", "reflexioai", "claude-smart"),
    path.join(HOME, ".codex", "plugins", "cache", "reflexioai", "claude-smart"),
  ]) {
    try {
      const versions = fs
        .readdirSync(cacheRoot, { withFileTypes: true })
        .filter((entry) => entry.isDirectory())
        .map((entry) => path.join(cacheRoot, entry.name))
        .sort((a, b) => fs.statSync(b).mtimeMs - fs.statSync(a).mtimeMs);
      candidates.push(...versions);
    } catch {
      // Missing cache roots are fine; try the next candidate family.
    }
  }
  for (const candidate of candidates) {
    if (!isPluginLikeRoot(candidate)) continue;
    const candidateReal = realDir(candidate);
    if (!candidateReal || candidateReal === rootReal) continue;
    if (isReflexioStrayPluginCopy(candidateReal)) continue;
    return candidateReal;
  }
  return null;
}

function stablePluginRoot(root) {
  const stable = stablePluginRootForStrayCopy(root);
  if (stable) {
    appendLog("backend.log", `[claude-smart] redirecting stray plugin copy under ~/.reflexio (${root}) to stable root ${stable}`);
    return stable;
  }
  return root;
}

function pluginRoot() {
  for (const value of [process.env.CLAUDE_PLUGIN_ROOT, process.env.PLUGIN_ROOT]) {
    if (value && fs.existsSync(path.join(value, "pyproject.toml"))) {
      return stablePluginRoot(path.resolve(value));
    }
  }
  const fromScript = path.resolve(__dirname, "..");
  if (fs.existsSync(path.join(fromScript, "pyproject.toml"))) return stablePluginRoot(fromScript);
  const cacheRoot = path.join(HOME, ".codex", "plugins", "cache", "reflexioai", "claude-smart");
  try {
    const versions = fs
      .readdirSync(cacheRoot, { withFileTypes: true })
      .filter((entry) => entry.isDirectory())
      .map((entry) => path.join(cacheRoot, entry.name))
      .sort((a, b) => fs.statSync(b).mtimeMs - fs.statSync(a).mtimeMs);
    for (const candidate of versions) {
      if (fs.existsSync(path.join(candidate, "pyproject.toml"))) return stablePluginRoot(candidate);
    }
  } catch {
    // Fall through to the stable plugin-root link.
  }
  return stablePluginRoot(path.join(REFLEXIO_DIR, "plugin-root"));
}

function prependRuntimePath() {
  const privateNode = path.join(STATE_DIR, "node", "current");
  const parts = [
    path.join(privateNode, "bin"),
    privateNode,
    path.join(HOME, ".local", "bin"),
    path.join(HOME, ".cargo", "bin"),
  ];
  process.env.PATH = `${parts.join(path.delimiter)}${path.delimiter}${process.env.PATH || ""}`;
}

function commandPath(names) {
  const pathParts = (process.env.PATH || "").split(path.delimiter).filter(Boolean);
  for (const dir of pathParts) {
    for (const name of names) {
      const candidate = path.join(dir, name);
      if (fs.existsSync(candidate)) return candidate;
    }
  }
  return null;
}

function uvPath() {
  return commandPath(process.platform === "win32" ? ["uv.exe", "uv"] : ["uv"]);
}

function bashPath() {
  return commandPath(process.platform === "win32" ? ["bash.exe", "bash"] : ["bash"]);
}

function unquoteEnvValue(value) {
  const trimmed = String(value || "").trim();
  if (trimmed.length >= 2) {
    const first = trimmed[0];
    const last = trimmed[trimmed.length - 1];
    if ((first === '"' && last === '"') || (first === "'" && last === "'")) {
      return trimmed.slice(1, -1);
    }
  }
  return trimmed;
}

function loadReflexioEnv() {
  const file = path.join(REFLEXIO_DIR, ".env");
  let text;
  try {
    text = fs.readFileSync(file, "utf8");
  } catch {
    return;
  }
  const managedReflexioKeys = new Set([
    "REFLEXIO_URL",
    "REFLEXIO_API_KEY",
    "REFLEXIO_USER_ID",
    "CLAUDE_SMART_READ_ONLY",
  ]);
  const localConfigKeys = new Set([
    "CLAUDE_SMART_USE_LOCAL_CLI",
    "CLAUDE_SMART_USE_LOCAL_EMBEDDING",
    "CLAUDE_SMART_BACKEND_AUTOSTART",
    "CLAUDE_SMART_DASHBOARD_AUTOSTART",
    "CLAUDE_SMART_CLI_PATH",
    "CLAUDE_SMART_CLI_TIMEOUT",
    "CLAUDE_SMART_STATE_DIR",
    "CLAUDE_SMART_ENABLE_OPTIMIZER",
  ]);
  for (const rawLine of text.split(/\r?\n/)) {
    let line = rawLine.trim();
    if (!line || line.startsWith("#")) continue;
    if (line.startsWith("export ")) line = line.slice("export ".length).trimStart();
    const eq = line.indexOf("=");
    if (eq < 0) continue;
    const key = line.slice(0, eq).trim();
    if (managedReflexioKeys.has(key)) {
      process.env[key] = unquoteEnvValue(line.slice(eq + 1));
    } else if (localConfigKeys.has(key) && !process.env[key]) {
      process.env[key] = unquoteEnvValue(line.slice(eq + 1));
    }
  }
}

function codexCompatPath(root) {
  const filename = process.platform === "win32"
    ? "codex-claude-compat.cmd"
    : "codex-claude-compat";
  return path.join(root, "scripts", filename);
}

function readBackendUrl() {
  if (process.env.REFLEXIO_URL) return process.env.REFLEXIO_URL;
  return `http://localhost:${DEFAULT_BACKEND_PORT}/`;
}

function detached(command, args, options = {}) {
  const child = spawn(command, args, {
    cwd: options.cwd,
    env: options.env || process.env,
    detached: true,
    shell: process.platform === "win32" && /\.(?:cmd|bat)$/i.test(command),
    stdio: "ignore",
    windowsHide: true,
  });
  child.unref();
  return child.pid;
}

function startInstallerDetached(root, reason) {
  if (process.env.CLAUDE_SMART_BOOTSTRAPPING === "1") return false;
  const script = path.join(root, "scripts", "smart-install.sh");
  const bash = bashPath();
  if (!bash || !fs.existsSync(script)) return false;
  appendLog("backend.log", `[claude-smart] ${reason}; starting installer in background`);
  detached(bash, [script], {
    cwd: root,
    env: {
      ...process.env,
      CLAUDE_SMART_BOOTSTRAPPING: "1",
    },
  });
  return true;
}

function runServiceScript(root, scriptName, action, logName) {
  trimLog(path.join(STATE_DIR, logName));
  const bash = bashPath();
  const script = path.join(root, "scripts", scriptName);
  if (!bash || !fs.existsSync(script)) {
    appendLog(
      logName,
      `[claude-smart] codex hook: cannot run ${scriptName}; bash or script missing`,
    );
    emitOk();
    return 0;
  }
  const result = spawnSync(bash, [script, action], {
    cwd: root,
    env: {
      ...process.env,
      PLUGIN_ROOT: root,
      CLAUDE_PLUGIN_ROOT: root,
      CLAUDE_SMART_HOST: "codex",
      CLAUDE_SMART_CLI_PATH: process.env.CLAUDE_SMART_CLI_PATH || codexCompatPath(root),
      BACKEND_PORT: String(DEFAULT_BACKEND_PORT),
      EMBEDDING_PORT: String(DEFAULT_EMBEDDING_PORT),
      DASHBOARD_PORT: String(DASHBOARD_PORT),
      PORT: String(DASHBOARD_PORT),
      REFLEXIO_URL: readBackendUrl(),
    },
    stdio: ["ignore", "pipe", "pipe"],
    timeout: SERVICE_SCRIPT_TIMEOUT_MS,
    windowsHide: true,
  });
  const stderr = String(result.stderr || "").trim();
  if (stderr) appendLog(logName, stderr);
  if (result.error) {
    appendLog(
      logName,
      `[claude-smart] codex hook: ${scriptName} ${action} failed: ${result.error.message}`,
    );
  }
  if (result.signal) {
    appendLog(
      logName,
      `[claude-smart] codex hook: ${scriptName} ${action} terminated by ${result.signal}`,
    );
  }
  emitNormalizedHookOutput(result.stdout);
  return typeof result.status === "number" ? result.status : 0;
}

function ensurePluginRoot(root) {
  ensureDir(REFLEXIO_DIR);
  const link = path.join(REFLEXIO_DIR, "plugin-root");
  const metadata = path.join(REFLEXIO_DIR, "plugin-root.txt");
  let blocked = false;
  try {
    const existing = fs.lstatSync(link);
    if (existing.isSymbolicLink() || existing.isFile()) {
      fs.rmSync(link, { recursive: true, force: true });
    } else {
      blocked = true;
    }
  } catch (err) {
    if (!err || err.code !== "ENOENT") blocked = true;
  }
  if (blocked) {
    fs.writeFileSync(metadata, `${root}\n`);
    return;
  }
  try {
    fs.symlinkSync(root, link, process.platform === "win32" ? "junction" : "dir");
    fs.writeFileSync(metadata, `${root}\n`);
  } catch {
    fs.writeFileSync(metadata, `${root}\n`);
  }
}

async function startBackend(root) {
  runServiceScript(root, "backend-service.sh", "start", "backend.log");
}

async function startDashboard(root) {
  runServiceScript(root, "dashboard-service.sh", "start", "dashboard.log");
}

function runHook(root, event) {
  trimLog(path.join(STATE_DIR, "backend.log"));
  let uv = uvPath();
  if (!uv) {
    startInstallerDetached(root, "hook: uv not on PATH");
    uv = uvPath();
    if (!uv) {
      appendLog("backend.log", "[claude-smart] hook: uv not on PATH; installer recovery scheduled; skipping");
      emitHookOk();
      return 0;
    }
  }
  const input = fs.readFileSync(0);
  const result = spawnSync(
    uv,
    ["run", "--project", root, "--quiet", "python", "-m", "claude_smart.hook", "codex", event],
    {
      cwd: root,
      env: {
        ...process.env,
        REFLEXIO_URL: readBackendUrl(),
        CLAUDE_SMART_HOST: "codex",
        CLAUDE_SMART_CLI_PATH: process.env.CLAUDE_SMART_CLI_PATH || codexCompatPath(root),
        CLAUDE_SMART_CITATION_LINK_STYLE: process.env.CLAUDE_SMART_CITATION_LINK_STYLE || "markdown",
      },
      input,
      stdio: ["pipe", "pipe", "inherit"],
      windowsHide: true,
    },
  );
  emitNormalizedHookOutput(result.stdout);
  return typeof result.status === "number" ? result.status : 1;
}

async function main() {
  prependRuntimePath();
  loadReflexioEnv();
  const root = pluginRoot();
  process.env.PLUGIN_ROOT = root;
  process.env.CLAUDE_PLUGIN_ROOT = root;
  const action = process.argv[2] || "hook";
  if (action === "ensure-root") {
    ensurePluginRoot(root);
    emitOk();
    return 0;
  }
  if (action === "backend") {
    await startBackend(root);
    return 0;
  }
  if (action === "dashboard") {
    await startDashboard(root);
    return 0;
  }
  if (action === "hook") {
    return runHook(root, process.argv[3] || "session-start");
  }
  emitOk();
  return 0;
}

main()
  .then((code) => process.exit(code))
  .catch((err) => {
    appendLog("backend.log", `[claude-smart] codex hook failed: ${err && err.stack ? err.stack : err}`);
    emitOk();
  });
