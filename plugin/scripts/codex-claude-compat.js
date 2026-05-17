#!/usr/bin/env node
"use strict";

/*
 * Translate Reflexio's Claude CLI provider contract to `codex exec`.
 *
 * Reflexio shells out to CLAUDE_SMART_CLI_PATH as if it were Claude Code:
 *
 *   <path> -p --output-format stream-json --model <model> ...
 *
 * Under Codex, this small bridge preserves that executable contract while
 * routing the actual generation through the authenticated Codex CLI.
 */

const { spawnSync } = require("node:child_process");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const TIMEOUT_MS = 120_000;

function main(argv) {
  try {
    const { outputFormat, systemPrompt } = parseSupportedArgs(argv);
    const content = runCodex({
      prompt: fs.readFileSync(0, "utf8"),
      systemPrompt,
    });
    const payload =
      outputFormat === "stream-json"
        ? { type: "result", subtype: "success", result: content }
        : { result: content };
    process.stdout.write(`${JSON.stringify(payload)}\n`);
    return 0;
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    process.stderr.write(`codex-claude-compat: ${message}\n`);
    return 1;
  }
}

function parseSupportedArgs(argv) {
  let outputFormat = "json";
  let systemPrompt = "";
  let idx = 0;
  while (idx < argv.length) {
    const arg = argv[idx];
    if (arg === "-p") {
      idx += 1;
    } else if (arg === "--output-format") {
      outputFormat = requireValue(argv, idx, arg);
      idx += 2;
    } else if (arg === "--model") {
      requireValue(argv, idx, arg);
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
  return { outputFormat, systemPrompt };
}

function requireValue(argv, idx, name) {
  if (idx + 1 >= argv.length) {
    throw new Error(`${name} requires a value`);
  }
  return argv[idx + 1];
}

function runCodex({ prompt, systemPrompt }) {
  const codexPath = process.env.CLAUDE_SMART_CODEX_PATH || commandPath(codexNames());
  if (!codexPath) {
    throw new Error("codex CLI not found on PATH");
  }

  const outputPath = temporaryOutputPath();
  const args = [
    "exec",
    "--sandbox",
    "read-only",
    "--skip-git-repo-check",
    "--ephemeral",
    "--ignore-rules",
    "--output-last-message",
    outputPath,
    "-",
  ];
  const env = {
    ...process.env,
    CLAUDE_SMART_HOST: "codex",
    CLAUDE_SMART_INTERNAL: "1",
    CLAUDE_CODE_ENTRYPOINT: "optimizer",
  };

  try {
    const proc = spawnSync(codexPath, args, {
      input: codexPrompt({ prompt, systemPrompt }),
      encoding: "utf8",
      env,
      timeout: TIMEOUT_MS,
      windowsHide: true,
      shell: process.platform === "win32" && /\.(?:cmd|bat)$/i.test(codexPath),
    });
    if (proc.error) {
      if (proc.error.code === "ETIMEDOUT") {
        throw new Error(`codex CLI timed out after ${TIMEOUT_MS / 1000}s`);
      }
      throw proc.error;
    }
    if (proc.status !== 0) {
      const stderr = String(proc.stderr || "").trim().slice(0, 500);
      throw new Error(`codex CLI exited ${proc.status}: ${stderr}`);
    }
    const content = fs.readFileSync(outputPath, "utf8").trim();
    if (!content) {
      throw new Error("codex CLI returned empty output");
    }
    return content;
  } finally {
    try {
      fs.unlinkSync(outputPath);
    } catch {
      // Best effort cleanup only.
    }
  }
}

function codexNames() {
  return process.platform === "win32" ? ["codex.cmd", "codex.exe", "codex"] : ["codex"];
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

function temporaryOutputPath() {
  const suffix = crypto.randomBytes(8).toString("hex");
  return path.join(os.tmpdir(), `claude-smart-codex-${process.pid}-${Date.now()}-${suffix}`);
}

function codexPrompt({ prompt, systemPrompt }) {
  if (!systemPrompt) return prompt;
  return `${systemPrompt}\n\n## Task\n${prompt}`;
}

process.exitCode = main(process.argv.slice(2));
