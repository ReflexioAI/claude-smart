"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const cache = require("../plugin/scripts/dashboard-cache");
const { reusePreparedRuntime } = require("../bin/claude-smart");

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "runtime-reuse-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  for (const name of ["old", "new"]) {
    const dashboard = path.join(root, name, "dashboard");
    fs.mkdirSync(dashboard, { recursive: true });
    fs.writeFileSync(path.join(dashboard, "package.json"), "{}");
    fs.writeFileSync(path.join(dashboard, "page.tsx"), "original");
  }
  const dashboard = path.join(root, "old", "dashboard");
  fs.mkdirSync(path.join(dashboard, "node_modules", "next", "dist", "bin"), { recursive: true });
  fs.writeFileSync(path.join(dashboard, "node_modules", "next", "dist", "bin", "next"), "next");
  fs.writeFileSync(path.join(dashboard, "node_modules", "next", "package.json"), "{}");
  fs.mkdirSync(path.join(dashboard, ".next"));
  fs.writeFileSync(path.join(dashboard, ".next", "BUILD_ID"), "built");
  fs.writeFileSync(path.join(dashboard, ".next", "required-server-files.json"), "{}");
  cache.record(dashboard);
  return root;
}

test("compatible dashboard reuse preserves an independent rollback copy", t => {
  const root = fixture(t);
  reusePreparedRuntime(path.join(root, "old"), path.join(root, "new"));
  const current = path.join(root, "new", "dashboard");
  assert.ok(cache.reusable(current));
  fs.writeFileSync(path.join(current, ".next", "BUILD_ID"), "changed");
  assert.equal(fs.readFileSync(path.join(root, "old", "dashboard", ".next", "BUILD_ID"), "utf8"), "built");
});

test("source, runtime, public inputs and missing artifacts invalidate the build", t => {
  const root = fixture(t), dashboard = path.join(root, "old", "dashboard");
  assert.equal(cache.reusable(dashboard, "another-node"), false);
  const original = process.env.NEXT_PUBLIC_REUSE_TEST;
  try {
    process.env.NEXT_PUBLIC_REUSE_TEST = "changed";
    assert.equal(cache.reusable(dashboard), false);
  } finally {
    if (original === undefined) delete process.env.NEXT_PUBLIC_REUSE_TEST;
    else process.env.NEXT_PUBLIC_REUSE_TEST = original;
  }
  fs.writeFileSync(path.join(dashboard, "page.tsx"), "new code");
  assert.equal(cache.reusable(dashboard), false);
  reusePreparedRuntime(path.join(root, "old"), path.join(root, "new"));
  assert.equal(fs.existsSync(path.join(root, "new", "dashboard", ".next")), false);
  cache.record(dashboard);
  fs.unlinkSync(path.join(dashboard, ".next", "BUILD_ID"));
  assert.equal(cache.reusable(dashboard), false);
});

test("Python console script relocation modifies only the independent clone", {skip:process.platform === "win32"}, t => {
  const root = fixture(t), old = path.join(root, "old", ".venv");
  fs.mkdirSync(path.join(old, "bin"), { recursive:true });
  fs.writeFileSync(path.join(old, "bin", "tool"), `#!${old}/bin/python\n`);
  reusePreparedRuntime(path.join(root, "old"), path.join(root, "new"));
  const target = path.join(root, "new", ".venv");
  assert.equal(fs.readFileSync(path.join(target, "bin", "tool"), "utf8"), `#!${target}/bin/python\n`);
  assert.equal(fs.readFileSync(path.join(old, "bin", "tool"), "utf8"), `#!${old}/bin/python\n`);
});

test("a cloned Python environment runs from its new prefix", {skip:process.platform === "win32"}, t => {
  const {spawnSync} = require("node:child_process");
  const root = fixture(t), previous = path.join(root, "old", ".venv");
  const made = spawnSync("python3", ["-m", "venv", "--without-pip", previous], {encoding:"utf8"});
  assert.equal(made.status, 0, made.stderr);
  reusePreparedRuntime(path.join(root, "old"), path.join(root, "new"));
  const target = path.join(root, "new", ".venv");
  fs.rmSync(previous, {recursive:true, force:true});
  const checked = spawnSync(path.join(target,"bin","python3"), ["-c", "import os,sys; print(os.path.realpath(sys.prefix))"], {encoding:"utf8"});
  assert.equal(checked.status, 0, checked.stderr);
  assert.equal(checked.stdout.trim(), fs.realpathSync(target));
});

test("npx and private Node versions may differ without defeating artifact reuse", t => {
  const root = fixture(t), old = path.join(root, "old", "dashboard");
  cache.record(old, "private-node-version");
  reusePreparedRuntime(path.join(root, "old"), path.join(root, "new"));
  const current = path.join(root, "new", "dashboard");
  assert.equal(cache.reusable(current, "private-node-version"), true);
  assert.equal(cache.reusable(current, "another-node-version"), false);
});

test("Next-generated root files do not prevent reuse from a packaged reinstall", t => {
  const root = fixture(t), old = path.join(root, "old", "dashboard");
  fs.writeFileSync(path.join(old, "next-env.d.ts"), "generated next types");
  fs.writeFileSync(path.join(old, "tsconfig.tsbuildinfo"), "generated build state");
  cache.record(old);
  reusePreparedRuntime(path.join(root, "old"), path.join(root, "new"));
  assert.ok(cache.reusable(path.join(root, "new", "dashboard")));
});

test("relative executables survive removal of the rollback source", {skip:process.platform === "win32"}, t => {
  const root = fixture(t), previous = path.join(root, "old");
  fs.mkdirSync(path.join(previous, "dashboard", "node_modules", ".bin"));
  fs.symlinkSync("../next/dist/bin/next", path.join(previous, "dashboard", "node_modules", ".bin", "next"));
  fs.mkdirSync(path.join(previous, ".venv", "bin"), {recursive:true});
  fs.writeFileSync(path.join(previous, ".venv", "bin", "python"), "interpreter");
  fs.symlinkSync("python", path.join(previous, ".venv", "bin", "python3"));
  reusePreparedRuntime(previous, path.join(root, "new"));
  fs.rmSync(previous, {recursive:true, force:true});
  assert.equal(fs.readFileSync(path.join(root, "new", "dashboard", "node_modules", ".bin", "next"), "utf8"), "next");
  assert.equal(fs.readFileSync(path.join(root, "new", ".venv", "bin", "python3"), "utf8"), "interpreter");
});
