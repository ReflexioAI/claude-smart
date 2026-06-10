#!/usr/bin/env node
/**
 * npx claude-smart install — thin wrapper around the native host plugin
 * CLIs. Both Claude Code and Codex install from the bundled marketplace in
 * this npm package: Claude Code registers the package root as a local
 * marketplace, and Codex copies the bundled plugin into its own marketplace
 * wrapper. Both paths seed ~/.reflexio/.env with the two local-provider flags
 * so reflexio can route generation through local tools with no API key.
 * Managed/read-only/global setup is handled by `npx claude-smart setup`,
 * which writes ~/.reflexio/.env before running this installer.
 *
 * Keep this file dependency-free — it runs via `npx` with no install step.
 */
"use strict";

const { execSync, spawn, spawnSync } = require("child_process");
const crypto = require("crypto");
const {
  chmodSync,
  cpSync,
  existsSync,
  lstatSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  renameSync,
  rmSync,
  statSync,
  symlinkSync,
  writeFileSync,
} = require("fs");
const https = require("https");
const { arch, homedir, platform, release, tmpdir } = require("os");
const { dirname, join } = require("path");

const PLUGIN_SPEC = "claude-smart@reflexioai";
const CODEX_MARKETPLACE_NAME = "reflexioai";
const CODEX_MARKETPLACE_DISPLAY_NAME = "ReflexioAI";
const CODEX_PLUGIN_ID = `claude-smart@${CODEX_MARKETPLACE_NAME}`;
const REFLEXIO_ENV_PATH = join(homedir(), ".reflexio", ".env");
const MANAGED_REFLEXIO_URL = "https://www.reflexio.ai/";
const MANAGED_SETUP_ENV = "CLAUDE_SMART_MANAGED_SETUP";
const CLAUDE_SMART_READ_ONLY_ENV = "CLAUDE_SMART_READ_ONLY";
const CLAUDE_SMART_USE_LOCAL_CLI_ENV = "CLAUDE_SMART_USE_LOCAL_CLI";
const CLAUDE_SMART_USE_LOCAL_EMBEDDING_ENV = "CLAUDE_SMART_USE_LOCAL_EMBEDDING";
const REFLEXIO_USER_ID_ENV = "REFLEXIO_USER_ID";
const REFLEXIO_DIR = join(homedir(), ".reflexio");
const CLAUDE_SMART_STATE_DIR = join(homedir(), ".claude-smart");
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
  "plugin/scripts/codex-hook.js",
  "plugin/scripts/_codex_env.sh",
];
const CODEX_CLI_TIMEOUT_MS = 30_000;
const PLUGIN_SERVICE_TIMEOUT_MS = 15_000;
const COPYTREE_IGNORE_NAMES = new Set([
  "__pycache__",
  ".venv",
  ".pytest_cache",
  ".ruff_cache",
  "node_modules",
  ".next",
]);
const LOCAL_DEFAULT_ENV_ENTRIES = [
  [
    "# Route reflexio generation through the local Claude Code CLI",
    CLAUDE_SMART_USE_LOCAL_CLI_ENV,
    "1",
  ],
  [
    "# Use the in-process ONNX embedder (chromadb) - no API key for semantic search",
    CLAUDE_SMART_USE_LOCAL_EMBEDDING_ENV,
    "1",
  ],
  [null, CLAUDE_SMART_READ_ONLY_ENV, "0"],
];
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

function ensureLocalReflexioEnv() {
  mkdirSync(dirname(REFLEXIO_ENV_PATH), { recursive: true });
  const existing = existsSync(REFLEXIO_ENV_PATH)
    ? readFileSync(REFLEXIO_ENV_PATH, "utf8")
    : "";
  const present = new Set();
  const keptLines = [];
  let pruned = false;
  for (const line of existing.split(/\r?\n/)) {
    const parsed = parseEnvLine(line);
    if (parsed) {
      if (LOCAL_MODE_PRUNE_KEYS.has(parsed.key)) {
        pruned = true;
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
    if (comment) additions.push(comment);
    if (key === CLAUDE_SMART_READ_ONLY_ENV) {
      additions.push(`${key}="${escapeEnvValue(value)}"`);
    } else {
      additions.push(`${key}=${escapeEnvValue(value)}`);
    }
    added.push(key);
  }

  if (additions.length > 0 || pruned) {
    let content = keptLines.join("\n").replace(/\n*$/, "");
    if (additions.length > 0) {
      const prefix = content ? "\n" : "";
      content = content + prefix + additions.join("\n");
    }
    writeFileSync(REFLEXIO_ENV_PATH, content ? `${content}\n` : "");
  } else if (!existsSync(REFLEXIO_ENV_PATH)) {
    writeFileSync(REFLEXIO_ENV_PATH, "");
  }
  chmodSync(REFLEXIO_ENV_PATH, 0o600);
  return added;
}

function maskSecret(value) {
  if (!value) return "";
  if (value.length <= 8) return "*".repeat(value.length);
  const prefix = value.slice(0, 8).includes("-") ? value.slice(0, 5) : value.slice(0, 4);
  return `${prefix}****${value.slice(-4)}`;
}

function loadReflexioSetupEnv() {
  let readOnlyValue = "";
  let fileApiKey = "";
  let fileUrl = "";
  if (existsSync(REFLEXIO_ENV_PATH)) {
    const text = readFileSync(REFLEXIO_ENV_PATH, "utf8");
    for (const line of text.split(/\r?\n/)) {
      const parsed = parseEnvLine(line);
      if (!parsed) continue;
      if (parsed.key === "REFLEXIO_API_KEY") {
        fileApiKey = parsed.value;
      } else if (parsed.key === "REFLEXIO_URL") {
        fileUrl = parsed.value;
      } else if (parsed.key === REFLEXIO_USER_ID_ENV) {
        process.env[parsed.key] = parsed.value;
      } else if (parsed.key === CLAUDE_SMART_READ_ONLY_ENV) {
        readOnlyValue = parsed.value;
      }
    }
  }
  const apiKey = (fileApiKey || process.env.REFLEXIO_API_KEY || "").trim();
  if (apiKey) {
    process.env.REFLEXIO_API_KEY = apiKey;
    process.env.REFLEXIO_URL = (fileUrl || process.env.REFLEXIO_URL || MANAGED_REFLEXIO_URL).trim();
    process.env[MANAGED_SETUP_ENV] = "1";
    process.stdout.write(
      `Using managed Reflexio at ${process.env.REFLEXIO_URL} (API key ${maskSecret(apiKey)}).\n`,
    );
  } else {
    delete process.env.REFLEXIO_URL;
    delete process.env.REFLEXIO_API_KEY;
    delete process.env[REFLEXIO_USER_ID_ENV];
    delete process.env[MANAGED_SETUP_ENV];
    const added = ensureLocalReflexioEnv();
    if (added.length > 0) {
      process.stdout.write(`Seeded ${REFLEXIO_ENV_PATH} with ${added.join(", ")}.\n`);
    }
  }
  const readOnly = ["1", "true", "yes", "on"].includes(
    String(readOnlyValue).trim().toLowerCase(),
  );
  return { readOnly };
}

function configureReflexioSetup() {
  return loadReflexioSetupEnv();
}

function findClaudeCodePluginRoot() {
  const cacheRoot = join(homedir(), ".claude", "plugins", "cache", CODEX_MARKETPLACE_NAME, "claude-smart");
  const candidates = [];
  try {
    for (const entry of readdirSync(cacheRoot, { withFileTypes: true })) {
      if (!entry.isDirectory()) continue;
      const candidate = join(cacheRoot, entry.name);
      if (
        existsSync(join(candidate, "pyproject.toml")) &&
        existsSync(join(candidate, "scripts", "smart-install.sh"))
      ) {
        candidates.push(candidate);
      }
    }
  } catch {
    // Fall through to marketplace fallback.
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
  // PACKAGE_ROOT/plugin is intentionally NOT a fallback: under `npx` it
  // points at /root/.npm/_npx/<hash>/.../plugin which npm may prune between
  // invocations, breaking the editable claude_smart install (.pth dangles,
  // venv survives) and confusing every later `uv sync`. Require `claude
  // plugin install` to have populated the cache dir instead, and fail loud.
  const fallbacks = [
    join(homedir(), ".claude", "plugins", "marketplaces", CODEX_MARKETPLACE_NAME, "plugin"),
  ];
  for (const candidate of [...candidates, ...fallbacks]) {
    if (
      existsSync(join(candidate, "pyproject.toml")) &&
      existsSync(join(candidate, "scripts", "smart-install.sh"))
    ) {
      return candidate;
    }
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

async function bootstrapClaudeCodeInstall() {
  const pluginRoot = findClaudeCodePluginRoot();
  if (!pluginRoot) {
    const cacheRoot = join(
      homedir(),
      ".claude",
      "plugins",
      "cache",
      CODEX_MARKETPLACE_NAME,
      "claude-smart",
    );
    throw new Error(
      `claude plugin install did not populate ${cacheRoot}. ` +
        "Open Claude Code, run /plugins, verify claude-smart is installed " +
        "from the ReflexioAI marketplace, then rerun `npx claude-smart install`.",
    );
  }
  forcePluginRoot(pluginRoot);
  const bash = resolveCommand(isWindows() ? ["bash.exe", "bash"] : ["bash"]);
  if (!bash) {
    throw new Error("bash is required to bootstrap claude-smart dependencies");
  }
  const code = await runChecked(bash, [join(pluginRoot, "scripts", "smart-install.sh")], {
    cwd: pluginRoot,
  });
  if (code !== 0) {
    throw new Error(`smart-install.sh failed in ${pluginRoot}`);
  }
  const failureMarker = join(CLAUDE_SMART_STATE_DIR, "install-failed");
  if (existsSync(failureMarker)) {
    const reason = readFileSync(failureMarker, "utf8").trim() || "unknown error";
    throw new Error(reason);
  }
  return pluginRoot;
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
  return new Promise((resolve) => {
    const child = spawn(command, args, {
      cwd: options.cwd,
      env: options.env || process.env,
      shell: isWindows() && /\.(?:cmd|bat)$/i.test(command),
      stdio: "inherit",
      windowsHide: true,
    });
    child.on("exit", (code) => resolve(typeof code === "number" ? code : 1));
    child.on("error", () => resolve(1));
  });
}

function runPluginService(pluginRoot, scriptName, subcommand, envOverrides = {}) {
  const script = join(pluginRoot, "scripts", scriptName);
  if (!existsSync(script)) return false;
  const bash = resolveCommand(isWindows() ? ["bash.exe", "bash"] : ["bash"]);
  if (!bash) return false;
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

function prunePublishHooksForReadOnly(pluginRoot) {
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
  const reflexioDir = dirname(REFLEXIO_ENV_PATH);
  const link = join(reflexioDir, "plugin-root");
  mkdirSync(reflexioDir, { recursive: true });
  rmSync(link, { recursive: true, force: true });
  try {
    require("fs").symlinkSync(pluginRoot, link, isWindows() ? "junction" : "dir");
  } catch {
    writeFileSync(join(reflexioDir, "plugin-root.txt"), `${pluginRoot}\n`);
  }
}

async function installVendoredReflexio(pluginRoot, uv, env) {
  const vendorRoot = join(pluginRoot, "vendor", "reflexio");
  if (!existsSync(join(vendorRoot, "pyproject.toml"))) return;

  const pythonPath = isWindows()
    ? join(pluginRoot, ".venv", "Scripts", "python.exe")
    : join(pluginRoot, ".venv", "bin", "python");
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

async function bootstrapPluginRuntime(pluginRoot, options = {}) {
  assertSupportedRuntimePlatform();
  process.stdout.write("Preparing claude-smart runtime for hooks...\n");
  const nodeRuntime = await ensurePrivateNode();
  patchCodexHooksForNode(pluginRoot, nodeRuntime.node);
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
  let code = await runChecked(
    uv,
    ["sync", "--locked", "--python", "3.12", "--quiet"],
    { cwd: pluginRoot, env },
  );
  if (code !== 0) {
    process.stderr.write(
      `warning: quiet uv sync failed in ${pluginRoot}; retrying with full output.\n`,
    );
    code = await runChecked(
      uv,
      ["sync", "--locked", "--python", "3.12"],
      { cwd: pluginRoot, env },
    );
  }
  if (code !== 0) throw new Error(`uv sync failed in ${pluginRoot}`);
  await installVendoredReflexio(pluginRoot, uv, env);

  const dashboardDir = join(pluginRoot, "dashboard");
  if (existsSync(dashboardDir)) {
    code = await runChecked(nodeRuntime.npm, ["ci"], { cwd: dashboardDir, env });
    if (code !== 0) throw new Error(`npm ci failed in ${dashboardDir}`);
    code = await runChecked(nodeRuntime.npm, ["run", "build"], { cwd: dashboardDir, env });
    if (code !== 0) throw new Error(`npm run build failed in ${dashboardDir}`);
  }
}

function printHelp() {
  process.stdout.write(
    [
      "claude-smart — install helper for Claude Code and Codex",
      "",
      "Usage:",
      "  npx claude-smart install                       Install the plugin into Claude Code",
      "  npx claude-smart install --host codex          Register the plugin marketplace for Codex",
      "  npx claude-smart setup                         Configure managed/read-only/global setup",
      "  npx claude-smart uninstall --host codex        Remove the Codex marketplace registration",
      "  npx claude-smart --help                        Show this help",
      "",
      "Claude Code install:",
      "  1. claude plugin marketplace add <this package>",
      `  2. claude plugin install ${PLUGIN_SPEC}`,
      "  3. Reads ~/.reflexio/.env when managed/read-only setup was configured.",
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
      "Update:",
      "  npx claude-smart update                        Reinstall Claude Code support from this package",
      "  npx claude-smart update --host codex           Reinstall Codex support from this package",
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
  if (parseHost(args) === "codex") {
    await runUpdateCodex(args);
    return;
  }

  const pluginRoot = findClaudeCodePluginRoot();
  if (pluginRoot) {
    stopClaudeSmartServices(pluginRoot);
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
  stopClaudeSmartServices(join(PACKAGE_ROOT, "plugin"));

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
  if (parseHost(args) === "codex") {
    await runInstallCodex(args);
    return;
  }

  if (!hasClaudeCli()) {
    process.stderr.write(
      "error: 'claude' CLI not found on PATH. " +
        "Install Claude Code first: https://claude.com/claude-code\n",
    );
    process.exit(1);
  }

  const source = PACKAGE_ROOT;
  const setup = configureReflexioSetup();
  const readOnly = setup.readOnly;

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

  try {
    const pluginRoot = await bootstrapClaudeCodeInstall();
    restorePublishHooksFromSource(pluginRoot);
    if (readOnly) {
      prunePublishHooksForReadOnly(pluginRoot);
      process.stdout.write("Installed read-only hook manifest; publish interactions hooks are disabled.\n");
    }
    process.stdout.write(`Prepared claude-smart runtime at ${pluginRoot}.\n`);
    if (startBackendService(pluginRoot, "claude-code")) {
      process.stdout.write("Started claude-smart backend service.\n");
    }
    if (refreshDashboardService(pluginRoot)) {
      process.stdout.write("Refreshed claude-smart dashboard service.\n");
    }
  } catch (err) {
    process.stderr.write(
      `error: claude-smart installed, but dependency bootstrap failed: ${err && err.message ? err.message : err}\n`,
    );
    process.stderr.write(
      "Fix the issue above, then run /claude-smart:restart or restart Claude Code to retry.\n",
    );
    process.exit(1);
  }

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
  const setup = configureReflexioSetup();
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
    if (startBackendService(cacheDir, "codex")) {
      process.stdout.write("Started claude-smart backend service.\n");
    }
    if (refreshDashboardService(cacheDir)) {
      process.stdout.write("Refreshed claude-smart dashboard service.\n");
    }
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
  platformSupportError,
  prunePublishHooksForReadOnly,
  restorePublishHooksFromSource,
};
