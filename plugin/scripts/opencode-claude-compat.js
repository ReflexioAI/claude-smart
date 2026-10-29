#!/usr/bin/env node
"use strict";

/*
 * Translate Reflexio's Claude CLI provider contract to `opencode run`.
 *
 * Reflexio shells out to CLAUDE_SMART_CLI_PATH as if it were Claude Code:
 *
 *   <path> -p --output-format stream-json --model <model> ...
 *
 * Under OpenCode, this bridge preserves that executable contract while routing
 * generation through the user's authenticated OpenCode CLI/provider setup.
 */

const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const DEFAULT_TIMEOUT_MS = 120_000;
const AGENT_NAME = "claude-smart-extractor";

function main(argv) {
  let workDir = null;
  try {
    const { outputFormat, systemPrompt, model } = parseSupportedArgs(argv);
    const prompt = fs.readFileSync(0, "utf8");
    workDir = prepareWorkDir();
    const content = runOpenCode({
      prompt: combinedPrompt({ prompt, systemPrompt }),
      model,
      workDir,
    });
    const payload =
      outputFormat === "stream-json"
        ? { type: "result", subtype: "success", result: content }
        : { result: content };
    process.stdout.write(`${JSON.stringify(payload)}\n`);
    return 0;
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    process.stderr.write(`opencode-claude-compat: ${message}\n`);
    return 1;
  } finally {
    if (workDir) {
      try {
        fs.rmSync(workDir, { recursive: true, force: true });
      } catch {
        // Best effort cleanup only.
      }
    }
  }
}

function parseSupportedArgs(argv) {
  let outputFormat = "json";
  let systemPrompt = "";
  let model = "";
  let idx = 0;
  while (idx < argv.length) {
    const arg = argv[idx];
    if (arg === "-p") {
      idx += 1;
    } else if (arg === "--output-format") {
      outputFormat = requireValue(argv, idx, arg);
      idx += 2;
    } else if (arg === "--model") {
      model = requireValue(argv, idx, arg);
      idx += 2;
    } else if (arg === "--verbose" || arg === "--include-partial-messages") {
      idx += 1;
    } else if (arg === "--append-system-prompt") {
      systemPrompt = requireValue(argv, idx, arg);
      idx += 2;
    } else {
      throw new Error(`unsupported Claude CLI argument: ${arg}`);
    }
  }
  if (outputFormat !== "json" && outputFormat !== "stream-json") {
    throw new Error(`unsupported --output-format: ${outputFormat}`);
  }
  return { outputFormat, systemPrompt, model };
}

function requireValue(argv, idx, name) {
  if (idx + 1 >= argv.length) {
    throw new Error(`${name} requires a value`);
  }
  return argv[idx + 1];
}

function prepareWorkDir() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "claude-smart-opencode-"));
  const config = {
    agent: {
      [AGENT_NAME]: {
        description: "Internal claude-smart learning extraction",
        prompt:
          "You are running as an internal claude-smart extraction subprocess. " +
          "Return only the requested text or JSON. Do not use tools, edit files, " +
          "search the workspace, or ask follow-up questions.",
        steps: 2,
        permission: "deny",
      },
    },
  };
  const opencodeDir = path.join(dir, ".opencode");
  fs.mkdirSync(opencodeDir, { recursive: true });
  fs.writeFileSync(path.join(opencodeDir, "opencode.json"), `${JSON.stringify(config, null, 2)}\n`);
  return dir;
}

function runOpenCode({ prompt, model, workDir }) {
  const opencodePath = process.env.CLAUDE_SMART_OPENCODE_PATH || commandPath(opencodeNames());
  if (!opencodePath) {
    throw new Error("opencode CLI not found on PATH");
  }

  // No `--pure` / `--dir`: V2's `opencode run` rejects both. OPENCODE_PURE=1
  // is 1.x's --pure in env form (skip external plugins); V2 has no pure mode
  // and ignores it, so on V2 the extraction run loads the user's global
  // plugins. Project isolation comes from
  // `cwd: workDir` below, which carries the temp `.opencode/opencode.json`
  // that defines AGENT_NAME.
  const args = [
    "run",
    "--format",
    "json",
    "--agent",
    AGENT_NAME,
  ];
  const selectedModel = opencodeModel(model);
  if (selectedModel) {
    args.push("--model", selectedModel);
  }
  const variant = (process.env.CLAUDE_SMART_OPENCODE_VARIANT || "").trim();
  if (variant) {
    args.push("--variant", variant);
  }

  const proc = spawnSync(opencodePath, args, {
    input: prompt,
    cwd: workDir,
    encoding: "utf8",
    env: {
      ...process.env,
      OPENCODE_PURE: "1",
      // opencode resolves the project root from $PWD, not the spawn cwd.
      // A stale inherited PWD points at the caller's directory, so the temp
      // `.opencode/opencode.json` defining AGENT_NAME is never read.
      PWD: workDir,
      CLAUDE_SMART_HOST: "opencode",
      CLAUDE_SMART_INTERNAL: "1",
      CLAUDE_CODE_ENTRYPOINT: "optimizer",
    },
    timeout: timeoutMs(),
    windowsHide: true,
    shell: process.platform === "win32" && /\.(?:cmd|bat)$/i.test(opencodePath),
  });
  if (proc.error) {
    if (proc.error.code === "ETIMEDOUT") {
      throw new Error(`opencode CLI timed out after ${timeoutMs() / 1000}s`);
    }
    throw proc.error;
  }
  if (proc.status !== 0) {
    // opencode reports CLI errors as JSON events on stdout and can emit
    // step_start events before the error, so a fixed-size slice of stdout — or
    // a nonempty warning-only stderr — can hide it. Read the error event first.
    const eventMessage = errorEventMessage(proc.stdout);
    const stderr = String(proc.stderr || "").trim().slice(0, 300);
    const stdout = stdoutDiagnostic(proc.stdout);
    throw new Error(
      `opencode CLI exited ${proc.status}: ${eventMessage || stderr || stdout}`,
    );
  }
  const content = parseOpenCodeJson(proc.stdout);
  if (!content) {
    throw new Error("opencode CLI returned empty output");
  }
  return content;
}

function opencodeModel(modelFromArgs) {
  const explicit = (process.env.CLAUDE_SMART_LEARNING_MODEL || process.env.CLAUDE_SMART_OPENCODE_MODEL || "").trim();
  if (explicit) return explicit;
  const candidate = (modelFromArgs || "").trim();
  return candidate.includes("/") ? candidate : "";
}

function parseOpenCodeJson(stdout) {
  const chunks = [];
  for (const line of String(stdout || "").split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    let event;
    try {
      event = JSON.parse(trimmed);
    } catch {
      continue;
    }
    const part = event && typeof event === "object" ? event.part : null;
    if (part && part.type === "text" && typeof part.text === "string") {
      chunks.push(part.text);
    } else if (event.type === "text" && typeof event.text === "string") {
      chunks.push(event.text);
    } else if (event.type === "result" && typeof event.result === "string") {
      chunks.push(event.result);
    }
  }
  return stripOuterMarkdownFence(chunks.join("").trim());
}

// Extract the message from any `error` events in an `opencode run --format json`
// stdout stream. Handles the 2.x shape ({type:"error", error:{message}}) and the
// 1.x NamedError shape ({type:"error", error:{name, data:{message}}}).
function errorEventMessage(stdout) {
  const messages = [];
  for (const line of String(stdout || "").split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    let event;
    try {
      event = JSON.parse(trimmed);
    } catch {
      continue;
    }
    if (!event || event.type !== "error") continue;
    const err = event.error;
    const message =
      (err && typeof err.message === "string" && err.message) ||
      (err && err.data && typeof err.data.message === "string" && err.data.message) ||
      (err && typeof err.name === "string" && err.name) ||
      "";
    if (message) messages.push(message);
  }
  return messages.join("; ").slice(0, 300);
}

// The stdout fallback is diagnostics only: drop text events (model output)
// and lines that start like a JSON event but do not parse (a text event cut
// off by the exit). Plain lines, including "[ERROR] ..." prefixes, go ahead of
// other events so a long step_start cannot push them past the length limit.
function stdoutDiagnostic(stdout) {
  const plain = [];
  const events = [];
  for (const line of String(stdout || "").split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    let event;
    try {
      event = JSON.parse(trimmed);
    } catch {
      if (!/^(\{"|\[[{["])/.test(trimmed)) plain.push(trimmed);
      continue;
    }
    if (!event || typeof event !== "object") {
      plain.push(trimmed);
      continue;
    }
    const part = event.part;
    if (event.type === "text" || (part && part.type === "text")) continue;
    events.push(trimmed);
  }
  return [...plain, ...events].join("\n").slice(0, 300);
}

function stripOuterMarkdownFence(content) {
  // OpenCode can wrap one-shot JSON in a whole-response markdown fence.
  // Strip only that outer fence so streamed and one-shot output normalize alike.
  const match = String(content || "").match(/^```[^\r\n]*\r?\n([\s\S]*?)\r?\n```[ \t]*$/);
  return match ? match[1].trim() : content;
}

function timeoutMs() {
  const raw = Number(process.env.CLAUDE_SMART_CLI_TIMEOUT || "");
  if (Number.isFinite(raw) && raw > 0) return raw * 1000;
  return DEFAULT_TIMEOUT_MS;
}

function opencodeNames() {
  return process.platform === "win32" ? ["opencode.cmd", "opencode.exe", "opencode"] : ["opencode"];
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

function combinedPrompt({ prompt, systemPrompt }) {
  if (!systemPrompt) return prompt;
  return `${systemPrompt}\n\n## Task\n${prompt}`;
}

process.exitCode = main(process.argv.slice(2));
