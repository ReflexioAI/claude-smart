"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const { configure, parseOptions, describe, validateManagedUrl } = require("../bin/install-guide");
const inventory = [
  { host: "claude-code", available: true, installed: true },
  { host: "codex", available: true, installed: false },
  { host: "opencode", available: false, installed: false },
];
const neverAsk = () => { throw new Error("Unexpected prompt"); };

test("explicit multi-agent install requires a provider and deduplicates agents", async () => {
  const options = parseOptions(["--host", "codex,claude-code,codex", "--backend", "local"]);
  assert.deepEqual(options.hosts, ["codex", "claude-code"]);
  await assert.rejects(configure(options, inventory, {}, neverAsk, false), /--provider/);
  options.provider = "claude-code";
  const config = await configure(options, inventory, {}, neverAsk, false);
  assert.equal(config.provider, "claude-code");
  assert.match(describe(config), /Background learning: Claude Code CLI/);
});

test("first unattended install cannot silently start local services", async () => {
  await assert.rejects(configure(parseOptions(["--host", "codex"]), inventory, {}, neverAsk, false), /--backend/);
});

test("saved configuration survives integration changes and install order", async () => {
  const saved = { backend: "local", provider: "codex", readOnly: true, sharing: "global" };
  for (const host of ["claude-code", "codex"]) {
    const config = await configure(parseOptions(["--host", host]), inventory, saved, neverAsk, false);
    assert.equal(config.provider, "codex");
    assert.equal(config.readOnly, true);
    assert.equal(config.sharing, "global");
  }
});

test("interactive choices use checkboxes for hosts and single selection for provider", async () => {
  const prompts = [];
  const ask = async (title, choices, options) => {
    prompts.push({ title, choices, options });
    if (title.startsWith("Which coding")) return ["claude-code", "codex"];
    if (title.startsWith("Where")) return "local";
    return "claude-code";
  };
  const config = await configure({}, inventory, {}, ask, true);
  assert.equal(config.provider, "claude-code");
  assert.equal(prompts[0].options.multiple, true);
  assert.deepEqual(prompts[0].options.selected, ["claude-code"]);
  assert.equal(prompts[0].choices[2].disabled, true);
  assert.deepEqual(prompts[2].choices.map((choice) => choice.value), ["claude-code", "codex"]);
});

test("managed mode needs a key, validates HTTPS and never asks for a local provider", async () => {
  const prompts = [];
  const ask = async (title) => {
    prompts.push(title);
    if (title.includes("endpoint")) return "https://managed.example/";
    if (title.includes("key")) return "secret-not-in-output";
    if (title.includes("Allow")) return "read-only";
    return "project";
  };
  const config = await configure(parseOptions(["--host", "codex", "--backend", "managed"]), inventory, {}, ask, true);
  assert.equal(config.provider, undefined);
  assert.equal(config.readOnly, true);
  assert.ok(!prompts.some((title) => title.includes("background learning")));
  assert.ok(!describe(config).includes(config.apiKey));
  await assert.rejects(configure(parseOptions(["--host", "codex", "--backend", "managed"]), inventory, {}, neverAsk, false), /API key/);
  for (const url of ["http://example.com", "https://user:pass@example.com", "https://example.com/?key=secret"]) assert.throws(() => validateManagedUrl(url));
});

test("provider can be an available agent without installing its integration", async () => {
  const config = await configure(parseOptions(["--host", "codex", "--backend", "local", "--provider", "claude-code"]), inventory, {}, neverAsk, false);
  assert.deepEqual(config.hosts, ["codex"]);
  assert.equal(config.provider, "claude-code");
  await assert.rejects(configure(parseOptions(["--host", "codex", "--backend", "local", "--provider", "opencode"]), inventory, {}, neverAsk, false), /not available/);
});

test("updates retain custom local settings and cloud-provider opt-out", async () => {
  const saved = { backend: "local", provider: "codex", url: "http://localhost:9090", apiKey: "local-key", cliPath: "/custom/cli", useLocalCli: "0" };
  const config = await configure(parseOptions(["--host", "codex"]), inventory, saved, neverAsk, false);
  assert.equal(config.url, saved.url);
  assert.equal(config.apiKey, saved.apiKey);
  assert.equal(config.cliPath, saved.cliPath);
  assert.match(describe(config), /configured cloud LLM/);
  for (const overrides of [{ useLocalCli: "0" }, { cliPath: "/custom/cli" }]) {
    const config = await configure(parseOptions(["--host", "codex"]), inventory, { backend: "local", provider: "opencode", ...overrides }, neverAsk, false);
    assert.equal(config.provider, "opencode");
  }
  for (const url of ["http://[::1]:8071/", "http://0.0.0.0:8071/"]) {
    const config = await configure(parseOptions(["--host", "codex"]), inventory, { backend: "local", provider: "codex", url }, neverAsk, false);
    assert.equal(config.url, url);
  }
});

test("partial failures continue to the next integration and exit nonzero", () => {
  const script = `
    const {runGuide} = require('./bin/install-guide');
    runGuide('install', ['--host','claude-code,codex','--backend','local','--provider','codex'], {
      inventory: () => ${JSON.stringify(inventory)}, saved: () => ({}), save: () => console.log('SAVED'),
      runHost: async (_cmd, host) => { console.log('ATTEMPT '+host); return {code:host === 'claude-code' ? 1 : 0, log:'/tmp/detail.log'}; },
      finish: async () => console.log('SERVICES ONCE')
    }).catch(e => {console.error(e); process.exitCode=1;});`;
  const result = spawnSync(process.execPath, ["-e", script], { encoding: "utf8" });
  assert.equal(result.status, 1, result.stderr);
  assert.match(result.stdout, /ATTEMPT claude-code[\s\S]*ATTEMPT codex/);
  assert.match(result.stdout, /Claude Code: failed[\s\S]*Codex: installed/);
  assert.equal(result.stdout.match(/SERVICES ONCE/g).length, 1);
});

test("unattended uninstall requires explicit integrations and keeps configuration", () => {
  for (const args of [[], ["--host", "codex"]]) {
    const script = `
      const {runGuide} = require('./bin/install-guide');
      runGuide('uninstall', ${JSON.stringify(args)}, {
        inventory: () => ${JSON.stringify(inventory)}, save: () => {throw Error('must not save')},
        runHost: async () => ({code:0}), finish: async () => {}
      }).catch(e => {console.error(e.message); process.exitCode=1;});`;
    const result = spawnSync(process.execPath, ["-e", script], { encoding: "utf8" });
    assert.equal(result.status, args.length ? 0 : 1, result.stderr);
    if (args.length) assert.match(result.stdout, /Learned data: kept/);
  }
});

test("invalid arguments fail before configuration", () => {
  for (const args of [["--host", "cursor"], ["--backend", "unknown"], ["--provider"], ["--local", "--global"]]) assert.throws(() => parseOptions(args));
});

test("local read-only configuration can resume learning interactively or explicitly", async () => {
  const saved = { backend: "local", provider: "codex", readOnly: true };
  const config = await configure(parseOptions(["--host", "codex", "--learn"]), inventory, saved, neverAsk, false);
  assert.equal(config.readOnly, false);
  assert.throws(() => parseOptions(["--read-only", "--learn"]));
  const ask = async (title) => title.startsWith("Where") ? "local" : title.startsWith("Which agent") ? "codex" : title.startsWith("Allow") ? "learn" : "project";
  assert.equal((await configure(parseOptions(["--host", "codex"]), inventory, saved, ask, true)).readOnly, false);
});

test("interactive local setup offers an explicit model choice", async () => {
  const prompts = [];
  const ask = async (title, choices, options) => {
    prompts.push({ title, choices, options });
    if (title.startsWith("Which agent")) return "codex";
    if (title.startsWith("Which model")) return "custom";
    if (title.startsWith("Learning model ID")) return "test/codex-model";
    if (title.startsWith("Allow")) return "learn";
    return "project";
  };
  const config = await configure(parseOptions(["--host", "codex", "--backend", "local"]), inventory, {}, ask, true);
  assert.equal(config.model, "test/codex-model");
  const picker = prompts.find((prompt) => prompt.title.startsWith("Which model"));
  assert.deepEqual(picker.choices.map((choice) => choice.value), ["default", "custom"]);
  assert.equal(prompts.find((prompt) => prompt.title.startsWith("Learning model ID")).options.text, true);
  assert.match(describe(config), /Learning model: test\/codex-model/);
});

test("interactive provider changes reset the previous agent's model", async () => {
  const ask = async (title, choices, options) => {
    if (title.startsWith("Which agent")) return "claude-code";
    if (title.startsWith("Which model")) {
      assert.deepEqual(options.selected, ["default"]);
      return "default";
    }
    if (title.startsWith("Allow")) return "learn";
    return "project";
  };
  const config = await configure(parseOptions(["--host", "codex", "--backend", "local"]), inventory, {backend:"local", provider:"codex", model:"old-codex-model"}, ask, true);
  assert.equal(config.provider, "claude-code");
  assert.equal(config.model, "");
});
