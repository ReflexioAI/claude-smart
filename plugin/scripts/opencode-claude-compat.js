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
  fs.writeFileSync(path.join(dir, "opencode.json"), `${JSON.stringify(config, null, 2)}\n`);
  return dir;
}

function runOpenCode({ prompt, model, workDir }) {
  const opencodePath = process.env.CLAUDE_SMART_OPENCODE_PATH || commandPath(opencodeNames());
  if (!opencodePath) {
    throw new Error("opencode CLI not found on PATH");
  }

  const args = [
    "run",
    "--pure",
    "--format",
    "json",
    "--agent",
    AGENT_NAME,
    "--dir",
    workDir,
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
    const stderr = String(proc.stderr || "").trim().slice(0, 500);
    throw new Error(`opencode CLI exited ${proc.status}: ${stderr}`);
  }
  const content = parseOpenCodeJson(proc.stdout);
  if (!content) {
    throw new Error("opencode CLI returned empty output");
  }
  return content;
}

function opencodeModel(modelFromArgs) {
  const explicit = (process.env.CLAUDE_SMART_OPENCODE_MODEL || "").trim();
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
  return chunks.join("").trim();
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
