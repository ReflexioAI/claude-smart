#!/usr/bin/env node
"use strict";

const { spawn, spawnSync } = require("node:child_process");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");

const HOME = os.homedir();
const STATE_DIR = path.join(HOME, ".claude-smart");
const REFLEXIO_DIR = path.join(HOME, ".reflexio");
const DEFAULT_BACKEND_PORT = 8071;
const FALLBACK_BACKEND_PORT = 8072;
const DASHBOARD_PORT = 3001;
const LOG_MAX_BYTES = 10000000;

function emitOk() {
  process.stdout.write('{"continue":true}\n');
}

function emitNormalizedHookOutput(stdout) {
  const lines = String(stdout || "")
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
  if (lines.length === 0) {
    emitOk();
    return;
  }

  const merged = {};
  for (const line of lines) {
    let parsed;
    try {
      parsed = JSON.parse(line);
    } catch {
      appendLog("backend.log", `[claude-smart] codex hook emitted non-JSON stdout: ${line.slice(0, 500)}`);
      emitOk();
      return;
    }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      appendLog("backend.log", `[claude-smart] codex hook emitted non-object JSON stdout: ${line.slice(0, 500)}`);
      emitOk();
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

function pluginRoot() {
  for (const value of [process.env.CLAUDE_PLUGIN_ROOT, process.env.PLUGIN_ROOT]) {
    if (value && fs.existsSync(path.join(value, "pyproject.toml"))) {
      return path.resolve(value);
    }
  }
  const fromScript = path.resolve(__dirname, "..");
  if (fs.existsSync(path.join(fromScript, "pyproject.toml"))) return fromScript;
  const cacheRoot = path.join(HOME, ".codex", "plugins", "cache", "reflexioai", "claude-smart");
  try {
    const versions = fs
      .readdirSync(cacheRoot, { withFileTypes: true })
      .filter((entry) => entry.isDirectory())
      .map((entry) => path.join(cacheRoot, entry.name))
      .sort((a, b) => fs.statSync(b).mtimeMs - fs.statSync(a).mtimeMs);
    for (const candidate of versions) {
      if (fs.existsSync(path.join(candidate, "pyproject.toml"))) return candidate;
    }
  } catch {
    // Fall through to the stable plugin-root link.
  }
  return path.join(REFLEXIO_DIR, "plugin-root");
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

function npmPath() {
  return commandPath(process.platform === "win32" ? ["npm.cmd", "npm.exe", "npm"] : ["npm"]);
}

function bashPath() {
  return commandPath(process.platform === "win32" ? ["bash.exe", "bash"] : ["bash"]);
}

function stateFile(name) {
  return path.join(STATE_DIR, name);
}

function backendUrlFile() {
  return stateFile("backend-url");
}

function writeBackendUrl(port) {
  ensureDir(STATE_DIR);
  fs.writeFileSync(backendUrlFile(), `http://localhost:${port}/\n`);
}

function codexCompatPath(root) {
  const filename = process.platform === "win32"
    ? "codex-claude-compat.cmd"
    : "codex-claude-compat";
  return path.join(root, "scripts", filename);
}

function readBackendUrl() {
  if (process.env.REFLEXIO_URL) return process.env.REFLEXIO_URL;
  try {
    const value = fs.readFileSync(backendUrlFile(), "utf8").trim();
    if (value) return value;
  } catch {
    // Fall through to default.
  }
  return `http://localhost:${DEFAULT_BACKEND_PORT}/`;
}

function healthOk(port, pathname, markerHeader) {
  return new Promise((resolve) => {
    const req = http.request(
      {
        host: "127.0.0.1",
        port,
        path: pathname,
        method: "GET",
        timeout: 1200,
      },
      (res) => {
        const ok = res.statusCode && res.statusCode >= 200 && res.statusCode < 400;
        const markerOk = markerHeader ? Boolean(res.headers[markerHeader]) : true;
        res.resume();
        resolve(Boolean(ok && markerOk));
      },
    );
    req.on("timeout", () => req.destroy());
    req.on("error", () => resolve(false));
    req.end();
  });
}

function portOccupied(port) {
  return new Promise((resolve) => {
    const req = http.request(
      {
        host: "127.0.0.1",
        port,
        path: "/",
        method: "GET",
        timeout: 900,
      },
      (res) => {
        res.resume();
        resolve(true);
      },
    );
    req.on("timeout", () => req.destroy());
    req.on("error", (err) => {
      resolve(err && err.code !== "ECONNREFUSED");
    });
    req.end();
  });
}

async function waitForHealth(port, pathname, markerHeader, attempts) {
  for (let i = 0; i < attempts; i += 1) {
    if (await healthOk(port, pathname, markerHeader)) return true;
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  return false;
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

function runInstaller(root, reason) {
  if (process.env.CLAUDE_SMART_BOOTSTRAPPING === "1") return false;
  const script = path.join(root, "scripts", "smart-install.sh");
  if (!fs.existsSync(script)) return false;
  const bash = bashPath();
  if (!bash) return false;
  appendLog("backend.log", `[claude-smart] ${reason}; running installer`);
  const result = spawnSync(bash, [script], {
    cwd: root,
    env: {
      ...process.env,
      CLAUDE_SMART_BOOTSTRAPPING: "1",
    },
    encoding: "utf8",
    maxBuffer: 20 * 1024 * 1024,
    windowsHide: true,
  });
  const output = `${result.stdout || ""}${result.stderr || ""}`.trim();
  if (output) {
    ensureDir(STATE_DIR);
    fs.appendFileSync(path.join(STATE_DIR, "install.log"), `${output}\n`);
    trimLog(path.join(STATE_DIR, "install.log"));
  }
  prependRuntimePath();
  return result.status === 0;
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

function readPid(file) {
  try {
    const value = fs.readFileSync(file, "utf8").trim();
    return value ? Number(value) : null;
  } catch {
    return null;
  }
}

function pidAlive(pid) {
  if (!pid || Number.isNaN(pid)) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

function writePid(file, pid) {
  ensureDir(path.dirname(file));
  fs.writeFileSync(file, `${pid}\n`);
}

function ensurePluginRoot(root) {
  ensureDir(REFLEXIO_DIR);
  const link = path.join(REFLEXIO_DIR, "plugin-root");
  try {
    fs.rmSync(link, { recursive: true, force: true });
  } catch {
    // Ignore and try to recreate below.
  }
  try {
    fs.symlinkSync(root, link, process.platform === "win32" ? "junction" : "dir");
  } catch {
    fs.writeFileSync(path.join(REFLEXIO_DIR, "plugin-root.txt"), `${root}\n`);
  }
}

async function startBackend(root) {
  trimLog(path.join(STATE_DIR, "backend.log"));
  if (process.env.CLAUDE_SMART_BACKEND_AUTOSTART === "0") {
    emitOk();
    return;
  }
  const pidFile = path.join(STATE_DIR, "backend.pid");
  for (const port of [DEFAULT_BACKEND_PORT, FALLBACK_BACKEND_PORT]) {
    if (pidAlive(readPid(pidFile)) && await healthOk(port, "/health")) {
      writeBackendUrl(port);
      emitOk();
      return;
    }
    if (await healthOk(port, "/health")) {
      writeBackendUrl(port);
      emitOk();
      return;
    }
  }
  const uv = uvPath();
  if (!uv) {
    runInstaller(root, "backend: uv not on PATH");
  }
  const readyUv = uvPath();
  if (!readyUv) {
    appendLog("backend.log", "[claude-smart] backend: uv not on PATH after installer; skipping");
    emitOk();
    return;
  }
  let selectedPort = DEFAULT_BACKEND_PORT;
  if (await portOccupied(DEFAULT_BACKEND_PORT)) {
    appendLog("backend.log", "[claude-smart] backend: port 8071 occupied; trying 8072");
    selectedPort = FALLBACK_BACKEND_PORT;
  }
  const backendUrl = `http://localhost:${selectedPort}/`;
  const env = {
    ...process.env,
    BACKEND_PORT: String(selectedPort),
    REFLEXIO_URL: backendUrl,
    CLAUDE_SMART_USE_LOCAL_CLI: process.env.CLAUDE_SMART_USE_LOCAL_CLI || "1",
    CLAUDE_SMART_USE_LOCAL_EMBEDDING: process.env.CLAUDE_SMART_USE_LOCAL_EMBEDDING || "1",
    CLAUDE_SMART_HOST: "codex",
    CLAUDE_SMART_CLI_PATH: process.env.CLAUDE_SMART_CLI_PATH || codexCompatPath(root),
    INTERACTION_CLEANUP_THRESHOLD: process.env.INTERACTION_CLEANUP_THRESHOLD || "500",
    INTERACTION_CLEANUP_DELETE_COUNT: process.env.INTERACTION_CLEANUP_DELETE_COUNT || "200",
  };
  const pid = detached(
    readyUv,
    [
      "run",
      "--project",
      root,
      "--quiet",
      "reflexio",
      "services",
      "start",
      "--only",
      "backend",
      "--no-reload",
    ],
    { cwd: root, env },
  );
  writePid(pidFile, pid);
  if (await waitForHealth(selectedPort, "/health", null, 10)) {
    writeBackendUrl(selectedPort);
  }
  emitOk();
}

async function startDashboard(root) {
  if (process.env.CLAUDE_SMART_DASHBOARD_AUTOSTART === "0") {
    emitOk();
    return;
  }
  const dashboard = path.join(root, "dashboard");
  if (!fs.existsSync(dashboard)) {
    emitOk();
    return;
  }
  const pidFile = path.join(STATE_DIR, "dashboard.pid");
  if (
    pidAlive(readPid(pidFile)) &&
    await healthOk(DASHBOARD_PORT, "/api/health", "x-claude-smart-dashboard")
  ) {
    emitOk();
    return;
  }
  const npm = npmPath();
  if (!npm) {
    runInstaller(root, "dashboard: npm not on PATH");
  }
  const readyNpm = npmPath();
  if (!readyNpm) {
    appendLog("dashboard.log", "[claude-smart] dashboard: npm not on PATH after installer; skipping");
    emitOk();
    return;
  }
  if (!fs.existsSync(path.join(dashboard, ".next"))) {
    const buildPidFile = path.join(STATE_DIR, "dashboard-build.pid");
    if (!pidAlive(readPid(buildPidFile))) {
      const pid = detached(readyNpm, ["run", "build"], { cwd: dashboard });
      writePid(buildPidFile, pid);
      appendLog("dashboard.log", "[claude-smart] dashboard: .next missing; started background build");
    }
    emitOk();
    return;
  }
  const env = {
    ...process.env,
    PORT: String(DASHBOARD_PORT),
    REFLEXIO_URL: readBackendUrl(),
    CLAUDE_SMART_DASHBOARD_WORKSPACE: process.cwd(),
  };
  const pid = detached(readyNpm, ["run", "start"], { cwd: dashboard, env });
  writePid(pidFile, pid);
  await waitForHealth(DASHBOARD_PORT, "/api/health", "x-claude-smart-dashboard", 5);
  emitOk();
}

function runHook(root, event) {
  trimLog(path.join(STATE_DIR, "backend.log"));
  let uv = uvPath();
  if (!uv) {
    if (event === "session-start") {
      runInstaller(root, "hook: uv not on PATH");
      uv = uvPath();
    } else {
      startInstallerDetached(root, "hook: uv not on PATH");
    }
    if (!uv) {
      appendLog("backend.log", "[claude-smart] hook: uv not on PATH after installer; skipping");
      emitOk();
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
        CLAUDE_SMART_CITATION_LINK_STYLE: process.env.CLAUDE_SMART_CITATION_LINK_STYLE || "osc8",
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
