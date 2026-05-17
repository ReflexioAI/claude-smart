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

function emitOk() {
  process.stdout.write('{"continue":true,"suppressOutput":true}\n');
}

function ensureDir(dir) {
  fs.mkdirSync(dir, { recursive: true });
}

function appendLog(name, line) {
  ensureDir(STATE_DIR);
  fs.appendFileSync(path.join(STATE_DIR, name), `${line}\n`);
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
  return path.join(root, "scripts", "codex-claude-compat.py");
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
    appendLog("backend.log", "[claude-smart] backend: uv not on PATH; skipping");
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
    uv,
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
    appendLog("dashboard.log", "[claude-smart] dashboard: npm not on PATH; skipping");
    emitOk();
    return;
  }
  if (!fs.existsSync(path.join(dashboard, ".next"))) {
    const buildPidFile = path.join(STATE_DIR, "dashboard-build.pid");
    if (!pidAlive(readPid(buildPidFile))) {
      const pid = detached(npm, ["run", "build"], { cwd: dashboard });
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
  const pid = detached(npm, ["run", "start"], { cwd: dashboard, env });
  writePid(pidFile, pid);
  await waitForHealth(DASHBOARD_PORT, "/api/health", "x-claude-smart-dashboard", 5);
  emitOk();
}

function runHook(root, event) {
  const uv = uvPath();
  if (!uv) {
    appendLog("backend.log", "[claude-smart] hook: uv not on PATH; skipping");
    emitOk();
    return 0;
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
      },
      input,
      stdio: ["pipe", "inherit", "inherit"],
      windowsHide: true,
    },
  );
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
