"use strict";

// Dependency-free terminal UI: this file ships in the npm package's bin directory.
const readline = require("readline");
const HOSTS = ["claude-code", "codex", "opencode"];
const LABELS = { "claude-code": "Claude Code", codex: "Codex", opencode: "OpenCode" };
const ACTIVATION = {
  "claude-code": "run /reload-plugins, or use next session",
  codex: "start a new session; review /hooks if needed",
  opencode: "restart OpenCode",
};

function parseOptions(args) {
  const options = {};
  const values = new Set(["--host", "--backend", "--provider", "--model", "--url", "--api-key-env", "--sharing"]);
  const flags = new Set(["--global", "--local", "--read-only", "--learn", "--yes"]);
  for (let i = 0; i < args.length; i++) {
    const arg = args[i];
    if (flags.has(arg)) options[arg.slice(2)] = true;
    else if (values.has(arg)) {
      if (!args[i + 1] || args[i + 1].startsWith("--")) throw new Error(`${arg} requires a value`);
      options[arg.slice(2)] = args[++i];
    } else throw new Error(`unknown argument '${arg}'; run claude-smart --help`);
  }
  if (options.host) {
    options.hosts = [...new Set(options.host.split(","))];
    if (options.hosts.some((host) => !HOSTS.includes(host))) throw new Error(`--host must contain ${HOSTS.join(", ")}`);
  }
  if (options.backend && !["local", "managed"].includes(options.backend)) throw new Error("--backend must be local or managed");
  if (options.provider && !HOSTS.includes(options.provider)) throw new Error(`--provider must be ${HOSTS.join(", ")}`);
  if (options.sharing && !["project", "global"].includes(options.sharing)) throw new Error("--sharing must be project or global");
  if (options.global && options.local) throw new Error("Choose --global or --local, not both");
  if (options.learn) {
    if (options["read-only"]) throw new Error("Choose --learn or --read-only, not both");
    options["read-only"] = false;
  }
  return options;
}

function terminalQuestion(title, choices, { multiple = false, selected = [], secret = false, initial = "", text = false } = {}) {
  const input = process.stdin;
  const output = process.stdout;
  if (!input.isTTY || !output.isTTY) throw new Error("Interactive configuration needs a terminal");
  return new Promise((resolve, reject) => {
    let cursor = Math.max(0, choices.findIndex((choice) => selected.includes(choice.value)));
    const checked = new Set(selected);
    let answer = initial;
    let lines = 0;
    const wasRaw = input.isRaw;
    readline.emitKeypressEvents(input);
    const draw = () => {
      if (lines) output.write(`\x1b[${lines}A\r\x1b[J`);
      const body = text ? [`  ${secret ? "•".repeat(answer.length) : answer}`] : choices.map((choice, index) => {
        const mark = multiple ? `[${checked.has(choice.value) ? "x" : " "}]` : `(${cursor === index ? "●" : " "})`;
        return `${cursor === index ? ">" : " "} ${mark} ${choice.label}${choice.disabled ? " (unavailable)" : ""}`;
      });
      const rendered = [title, ...body, text ? "  Enter: continue · Esc: cancel" : `  ↑/↓: move${multiple ? " · Space: toggle" : ""} · Enter: continue · Esc: cancel`];
      output.write(rendered.join("\n") + "\n");
      lines = rendered.reduce((count, line) => count + Math.max(1, Math.ceil(line.length / (output.columns || 80))), 0);
    };
    const cleanup = () => {
      input.removeListener("keypress", onKey);
      input.removeListener("end", onEnd);
      input.setRawMode(Boolean(wasRaw));
      input.pause();
    };
    const onEnd = () => { cleanup(); reject(new Error("Cancelled; no configuration changes saved")); };
    const onKey = (str, key = {}) => {
      if ((key.ctrl && key.name === "c") || key.name === "escape" || (key.ctrl && key.name === "d")) return onEnd();
      if (key.name === "return") {
        const result = text ? answer : multiple ? choices.filter((c) => checked.has(c.value) && !c.disabled).map((c) => c.value) : choices[cursor]?.value;
        if ((text && !answer.trim()) || (!text && (multiple ? !result.length : choices[cursor]?.disabled))) return;
        cleanup(); output.write("\n"); resolve(result); return;
      }
      if (text) {
        if (key.name === "backspace") answer = answer.slice(0, -1);
        else if (str && !key.ctrl && !key.meta && !/[\x00-\x1f\x7f]/.test(str)) answer += str;
      } else {
        if (key.name === "up") cursor = (cursor + choices.length - 1) % choices.length;
        if (key.name === "down") cursor = (cursor + 1) % choices.length;
        if (multiple && key.name === "space" && !choices[cursor].disabled) {
          const value = choices[cursor].value;
          if (checked.has(value)) checked.delete(value); else checked.add(value);
        }
      }
      draw();
    };
    input.setRawMode(true);
    input.resume();
    input.on("keypress", onKey);
    input.once("end", onEnd);
    draw();
  });
}

function validateManagedUrl(value) {
  let url;
  try { url = new URL(value); } catch { throw new Error("Managed endpoint must be a valid HTTPS URL"); }
  if (url.protocol !== "https:" || url.username || url.password || url.search || url.hash) {
    throw new Error("Managed endpoint must use HTTPS without credentials, query parameters, or a fragment");
  }
  return url.href;
}

function sameOrigin(first, second) {
  try { return new URL(first).origin === new URL(second).origin; } catch { return false; }
}

async function configure(options, inventory, saved, ask = terminalQuestion, interactive = Boolean(process.stdin.isTTY && process.stdout.isTTY)) {
  let hosts = options.hosts;
  if (!hosts) {
    if (!inventory.some((agent) => agent.available)) throw new Error("No supported coding agent found on PATH; install Claude Code, Codex, or OpenCode first");
    if (!interactive) {
      hosts = inventory.filter((agent) => agent.installed).map((agent) => agent.host);
      if (!hosts.length) throw new Error("No saved integrations. Use --host claude-code,codex --backend local --provider codex, or run install in a terminal.");
    } else {
      const installed = inventory.filter((agent) => agent.installed && agent.available);
      hosts = await ask("Which coding agents should use claude-smart?", inventory.map((agent) => ({
        value: agent.host, label: `${LABELS[agent.host]} · ${agent.available ? "detected" : "not found"}${agent.installed ? " · installed" : ""}`, disabled: !agent.available,
      })), { multiple: true, selected: (installed.length ? installed : inventory.filter((agent) => agent.available)).map((agent) => agent.host) });
    }
  }
  if (!inventory.some((agent) => agent.available && hosts.includes(agent.host))) throw new Error("None of the selected coding agents is available on PATH");
  const backend = options.backend || (interactive ? await ask("Where should claude-smart store and process your data?", [
    { value: "local", label: "Local Reflexio — stores data here and starts background services" },
    { value: "managed", label: "Managed Reflexio — connects using your API key; no local backend" },
  ], { selected: [saved.backend || "local"] }) : saved.backend);
  if (!backend) throw new Error("First unattended install requires --backend local or --backend managed; no services were started");
  const config = { hosts, backend, readOnly: options["read-only"] ?? saved.readOnly ?? false, sharing: options.sharing || saved.sharing || "project" };
  config.userId = config.sharing === "global" ? saved.userId || "global_user" : "";
  if (backend === "local") {
    const providers = inventory.filter((agent) => agent.available);
    config.cliPath = !interactive && !options.provider ? process.env.CLAUDE_SMART_CLI_PATH || saved.cliPath || "" : "";
    config.useLocalCli = !interactive && !options.provider ? process.env.CLAUDE_SMART_USE_LOCAL_CLI || saved.useLocalCli || "1" : "1";
    config.provider = options.provider || (interactive ? await ask("Which agent should power background learning?", providers.map((agent) => ({ value: agent.host, label: `${LABELS[agent.host]} CLI` })), { selected: [saved.provider || hosts[0]] }) : saved.provider || (hosts.length === 1 ? hosts[0] : null));
    config.model = options.model || (!saved.provider || config.provider === saved.provider ? process.env.CLAUDE_SMART_LEARNING_MODEL || saved.model : "") || "";
    if (interactive && !options.model && !config.cliPath && config.useLocalCli !== "0") {
      const mode = await ask("Which model should power background learning?", [
        { value: "default", label: "Provider default model" },
        { value: "custom", label: "Choose a model ID" },
      ], { selected: [config.model ? "custom" : "default"] });
      config.model = mode === "custom" ? await ask("Learning model ID supported by the selected agent", [], { text: true, initial: config.model }) : "";
    }
    if (config.model && (!/^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$/.test(config.model))) throw new Error("Learning model must be a valid model ID");
    if (options.model && config.useLocalCli === "0") throw new Error("--model selects a local agent model. Configure cloud-provider models in Reflexio settings instead.");
    if (!config.cliPath && config.useLocalCli !== "0") {
      if (!config.provider) throw new Error("Choose a background learning provider with --provider claude-code, codex, or opencode");
      if (!providers.some((agent) => agent.host === config.provider)) throw new Error(`Background learning provider ${LABELS[config.provider]} is not available`);
    }
    // Preserve explicitly configured custom local Reflexio endpoints and cloud providers.
    const savedLocalUrl = saved.backend === "local" ? saved.url : saved.localUrl;
    config.url = options.url || savedLocalUrl || "";
    if (config.url) {
      let parsed;
      try { parsed = new URL(config.url); } catch { throw new Error("Local endpoint must be a valid URL"); }
      if (parsed.protocol !== "http:" || !["localhost", "127.0.0.1", "0.0.0.0", "[::1]"].includes(parsed.hostname) || parsed.username || parsed.password || parsed.search || parsed.hash) {
        throw new Error("Local endpoint must use an HTTP loopback address without credentials, query parameters, or a fragment");
      }
    }
    config.apiKey = config.url && sameOrigin(config.url, savedLocalUrl) ? (saved.backend === "local" ? saved.apiKey : saved.localApiKey) || "" : "";
  } else {
    if (options.model || options.provider) throw new Error("Managed Reflexio chooses its learning model on the server; --provider and --model apply to local mode");
    const savedManagedUrl = saved.backend === "managed" ? saved.url : saved.managedUrl;
    config.url = validateManagedUrl(options.url || (interactive ? await ask("Managed Reflexio endpoint", [], { text: true, initial: savedManagedUrl || "https://www.reflexio.ai/" }) : savedManagedUrl || "https://www.reflexio.ai/"));
    config.apiKey = options["api-key-env"] ? process.env[options["api-key-env"]] : sameOrigin(savedManagedUrl, config.url) ? (saved.backend === "managed" ? saved.apiKey : saved.managedApiKey) : "";
    if (!config.apiKey && interactive) config.apiKey = await ask("Reflexio API key (hidden)", [], { text: true, secret: true });
    if (!config.apiKey?.trim()) throw new Error("Managed Reflexio requires a saved API key or --api-key-env VARIABLE");
    config.apiKey = config.apiKey.trim();
  }
  if (interactive) {
      if (options["read-only"] === undefined) config.readOnly = (await ask("Allow claude-smart to learn from interactions?", [
        { value: "learn", label: "Learn and retrieve memories" }, { value: "read-only", label: "Read-only — retrieve existing memories" },
      ], { selected: [config.readOnly ? "read-only" : "learn"] })) === "read-only";
      if (!options.sharing) config.sharing = await ask("Memory sharing scope", [
        { value: "project", label: "Project — keep memories scoped to each project" }, { value: "global", label: "Global — share across projects" },
      ], { selected: [config.sharing] });
  }
  return config;
}

function describe(config) {
  return [
    `  Integrations: ${config.hosts.map((host) => LABELS[host]).join(", ")}`,
    `  Backend: ${config.backend === "managed" ? `Managed Reflexio · ${config.url}` : "Local Reflexio"}`,
    `  Background learning: ${config.backend === "managed" ? "Managed Reflexio" : config.useLocalCli === "0" ? "configured cloud LLM" : config.cliPath ? `custom executable (${config.cliPath})` : `${LABELS[config.provider]} CLI`}`,
    `  Learning model: ${config.backend === "managed" ? config.reportedModel || "Managed service default (model not reported)" : config.useLocalCli === "0" ? "Configured cloud provider (model not reported)" : config.model || "Provider default (not explicitly selected)"}`,
    `  Mode: ${config.readOnly ? "read-only" : "learn and retrieve"} · sharing: ${config.sharing}`,
  ].join("\n");
}

async function runGuide(command, args, api) {
  const options = parseOptions(args);
  const interactive = Boolean(process.stdin.isTTY && process.stdout.isTTY) && !options.yes;
  const inventory = api.inventory(options);
  let config;
  if (command === "uninstall") {
    if (["backend", "provider", "model", "url", "api-key-env", "sharing", "read-only"].some((key) => options[key] !== undefined)) throw new Error("Uninstall accepts --host, --global/--local, and --yes; backend settings are kept");
    let hosts = options.hosts;
    if (!hosts) {
      const installed = inventory.filter((agent) => agent.installed);
      if (!installed.length) { process.stdout.write("No claude-smart integrations found for this project/account. Learned data was kept.\n"); return; }
      if (!interactive) throw new Error("Choose integrations to remove with --host claude-code,codex, or run uninstall in a terminal");
      hosts = await terminalQuestion("Which claude-smart integrations should be removed?", installed.map((agent) => ({ value: agent.host, label: LABELS[agent.host] })), { multiple: true, selected: installed.map((agent) => agent.host) });
    }
    config = { hosts };
    process.stdout.write(`\nRemove integrations: ${hosts.map((host) => LABELS[host]).join(", ")}\nLearned data and saved configuration will be kept.\n`);
  } else {
    config = await configure(options, inventory, api.saved(), terminalQuestion, interactive);
    if (config.backend === "managed" && api.learningModel) {
      process.stdout.write("Checking managed learning configuration…\n");
      config.reportedModel = await api.learningModel(config);
    }
    process.stdout.write(`\nConfiguration\n${describe(config)}\n`);
  }
  if (interactive) {
    const answer = await terminalQuestion(command === "uninstall" ? "Remove these integrations?" : "Proceed with installation?", [
      { value: "yes", label: command === "uninstall" ? "Remove integrations; keep learned data" : "Install with these settings" },
      { value: "no", label: "Cancel" },
    ], { selected: ["yes"] });
    if (answer !== "yes") { process.stdout.write("Cancelled; no changes made.\n"); return; }
  }
  if (command !== "uninstall") api.save(config);
  const results = [];
  for (const host of config.hosts) {
    const label = `${command === "uninstall" ? "Removing" : "Installing"} ${LABELS[host]} integration`;
    const result = await api.runHost(command, host, options, label);
    results.push({ host, ...result });
  }
  const serviceSummary = await api.finish(command, config, results) || [];
  process.stdout.write(`\n${results.every((result) => result.code === 0) ? command === "uninstall" ? "Uninstall complete" : "Installation complete" : "Completed with failures"}\n`);
  for (const result of results) {
    process.stdout.write(`  ${LABELS[result.host]}: ${result.code === 0 ? command === "uninstall" ? "removed" : "installed" : "failed"} · ${result.code === 0 ? ACTIVATION[result.host] : `see ${result.log}`}\n`);
  }
  if (command === "uninstall") process.stdout.write("  Learned data: kept in ~/.claude-smart/ and ~/.reflexio/\n");
  else process.stdout.write(`${describe(config)}\n`);
  for (const line of serviceSummary) process.stdout.write(`  ${line}\n`);
  if (results.some((result) => result.code !== 0)) process.exitCode = 1;
}

module.exports = { HOSTS, LABELS, parseOptions, terminalQuestion, configure, describe, validateManagedUrl, runGuide };
