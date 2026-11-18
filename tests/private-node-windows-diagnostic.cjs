"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const Module = require("node:module");
const path = require("node:path");
const crypto = require("node:crypto");
const { spawnSync } = require("node:child_process");
const argv = process.argv.slice(2);
const [entry, home] = argv;
assert.equal(home, require("node:os").homedir());
const installer = new Module(entry, module);
installer.filename = entry;
installer.paths = Module._nodeModulePaths(path.dirname(entry));
installer._compile(fs.readFileSync(entry, "utf8")
  + "\nmodule.exports = { preparePrivateNodeArchive, ensurePrivateNode, privateNodeTools };", entry);

function invoke(command, args, extra = {}) {
  const target = /\.cmd$/i.test(command) ? `"${command}"` : command;
  const result = spawnSync(target, args, {
    encoding: "utf8", shell: /\.cmd$/i.test(command), env: { ...process.env, ...extra },
  });
  assert.equal(result.status, 0, result.stderr || result.error);
  return result.stdout.trim();
}

async function qualify() {
  const state = path.join(home, ".claude-smart");
  const nodeRoot = path.join(state, "node");
  const current = path.join(nodeRoot, "current");
  const previous = path.join(current, "predecessor");
  fs.mkdirSync(current, { recursive: true });
  fs.writeFileSync(previous, "predecessor");
  const stage = fs.mkdtempSync(path.join(nodeRoot, ".prepare-"));
  try {
    const extracted = await installer.exports.preparePrivateNodeArchive(stage);
    assert.equal(fs.readFileSync(previous, "utf8"), "predecessor");
    const versionRoot = path.join(nodeRoot, "versions");
    fs.mkdirSync(versionRoot);
    const archived = fs.readdirSync(stage).find(name => /(?:\.zip|\.tar\.gz)$/.test(name));
    const digest = crypto.createHash("sha256")
      .update(fs.readFileSync(path.join(stage, archived))).digest("hex");
    const immutable = path.join(versionRoot, digest);
    fs.renameSync(extracted, immutable);
    const stable = installer.exports.privateNodeTools(immutable);
    const stableVersion = invoke(stable.node, ["--version"]);
    const stableNpm = invoke(stable.npm, ["--version"],
      { PATH: path.dirname(stable.node) + path.delimiter + process.env.PATH });
    const active = await installer.exports.ensurePrivateNode();
    assert.equal(fs.existsSync(previous), false);
    assert.equal(invoke(active.node, ["--version"]), stableVersion);
    assert.equal(invoke(stable.node, ["--version"]), stableVersion);
    assert.equal(invoke(stable.npm, ["--version"],
      { PATH: path.dirname(stable.node) + path.delimiter + process.env.PATH }), stableNpm);
    assert.equal((await installer.exports.ensurePrivateNode()).node, active.node);
    process.stdout.write(JSON.stringify({
      archive: archived, sha256: digest, node: stableVersion, npm: stableNpm,
      priorPathUnchangedDuringPreparation: true,
      stableToolsSurvivedCurrentReplacement: true,
      scope: "exact installer archive preparation and activation; versioned destination in disposable harness",
    }, null, 2) + "\n");
  } finally {
    fs.rmSync(stage, { recursive: true, force: true });
  }
  assert.deepEqual(fs.readdirSync(nodeRoot).sort(), ["current", "versions"]);
}
qualify().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
