#!/usr/bin/env node
// Generate .claude-plugin/marketplace.json from the committed template.
//
// The manifest is deliberately NOT committed. It is the file that makes a
// directory installable as a Claude Code marketplace, and the plugin payload it
// advertises is incomplete in git: plugin/vendor/reflexio is generated at pack
// time and gitignored. Committing the manifest would let
// `claude plugin marketplace add ReflexioAI/claude-smart` install a plugin with
// no Reflexio runtime. With the manifest absent from the repo, that command
// fails immediately ("Marketplace file not found") instead of producing a
// plugin that cannot start.
//
// npm's prepack hook runs this, so every `npm pack` / `npm publish` — and the
// Makefile targets that wrap them — ships a manifest whose version matches
// package.json by construction.

const { readFileSync, writeFileSync } = require("node:fs");
const { join, dirname } = require("node:path");

const REPO_ROOT = dirname(__dirname);
const TEMPLATE = join(REPO_ROOT, ".claude-plugin", "marketplace.template.json");
const OUTPUT = join(REPO_ROOT, ".claude-plugin", "marketplace.json");

function main() {
  const { version } = JSON.parse(readFileSync(join(REPO_ROOT, "package.json"), "utf8"));
  if (!version) {
    throw new Error("package.json has no version");
  }

  const manifest = JSON.parse(readFileSync(TEMPLATE, "utf8"));
  if (!Array.isArray(manifest.plugins) || manifest.plugins.length === 0) {
    throw new Error(`${TEMPLATE} declares no plugins`);
  }
  // package.json is the single source of version truth; `make bump` no longer
  // rewrites this manifest because it does not exist until pack time.
  for (const plugin of manifest.plugins) {
    plugin.version = version;
  }

  writeFileSync(OUTPUT, `${JSON.stringify(manifest, null, 2)}\n`);
  // stderr, not stdout: this runs as npm's prepack hook, and `npm pack` prints
  // the tarball path on stdout. Callers read that path (`npm pack | tail -1` in
  // the Makefile), so anything else on stdout corrupts it.
  process.stderr.write(`generated .claude-plugin/marketplace.json (version ${version})\n`);
}

main();
