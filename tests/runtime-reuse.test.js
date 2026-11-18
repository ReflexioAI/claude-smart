"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const cache = require("../plugin/scripts/dashboard-cache");
const { reusePreparedRuntime } = require("../bin/claude-smart");

test("probe runtime selection replacement on the native runner filesystem", t => {
  const { spawnSync } = require("node:child_process");
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "runtime-selector λ "));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const prior = path.join(root, "prior runtime");
  const next = path.join(root, "next runtime");
  fs.mkdirSync(prior);
  fs.mkdirSync(next);
  const junction = path.join(root, "plugin-root");
  const candidate = path.join(root, "candidate");
  const linkType = process.platform === "win32" ? "junction" : "dir";
  fs.symlinkSync(prior, junction, linkType);
  fs.symlinkSync(next, candidate, linkType);
  let junctionReplace = "success";
  try {
    fs.renameSync(candidate, junction);
  } catch (error) {
    assert.ok(error.code);
    junctionReplace = error.code;
  }
  assert.equal(fs.realpathSync(junction), fs.realpathSync(
    junctionReplace === "success" ? next : prior,
  ));
  assert.ok(fs.statSync(prior).isDirectory());
  assert.ok(fs.statSync(next).isDirectory());

  const record = path.join(root, "plugin-root.txt");
  fs.writeFileSync(record, prior);
  const stageAndTerminate = `
const fs = require("node:fs");
const [record, next, boundary] = process.argv.slice(1);
fs.writeFileSync(record + ".tmp", next, { flag: "wx" });
if (boundary === "after") fs.renameSync(record + ".tmp", record);
process.exit(87);
`;
  const textSelection = {};
  for (const boundary of ["before", "after"]) {
    const result = spawnSync(process.execPath, ["-e", stageAndTerminate, record, next, boundary], {
      encoding: "utf8", timeout: 10000,
    });
    assert.equal(result.status, 87, result.stderr || String(result.error || result.signal));
    const expected = boundary === "before" ? prior : next;
    assert.equal(fs.readFileSync(record, "utf8"), expected);
    textSelection[boundary] = boundary === "before" ? "prior" : "next";
    if (boundary === "before") fs.unlinkSync(record + ".tmp");
  }
  const held = fs.openSync(record, "r");
  const tempRecord = record + ".tmp";
  fs.writeFileSync(tempRecord, prior, { flag: "wx" });
  let heldReaderReplace = "success";
  try {
    try {
      fs.renameSync(tempRecord, record);
    } catch (error) {
      assert.ok(error.code);
      heldReaderReplace = error.code;
    }
    assert.equal(fs.readFileSync(held, "utf8"), next);
    assert.equal(fs.readFileSync(record, "utf8"), heldReaderReplace === "success" ? prior : next);
  } finally {
    fs.closeSync(held);
  }
  if (heldReaderReplace !== "success") fs.renameSync(tempRecord, record);
  assert.equal(fs.readFileSync(record, "utf8"), prior);
  t.diagnostic("RUNTIME_SELECTOR_PROBE " + JSON.stringify({
    platform: process.platform, node: process.version, junctionReplace, textSelection, heldReaderReplace,
  }));
});

test("registered Claude runtime discovery respects source metadata and native shims", t => {
  const {spawnSync} = require("node:child_process");
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "registered runtime λ "));
  t.after(() => fs.rmSync(root, {recursive:true, force:true}));
  const bin = path.join(root, "tools with spaces");
  fs.mkdirSync(bin);
  const windows = process.platform === "win32";
  const relay = "if(JSON.stringify(process.argv.slice(1))!==JSON.stringify(['plugin','list','--json']))process.exit(91);const fs=require('node:fs');process.stdout.write(fs.readFileSync(process.env.USERPROFILE+'/inventory.json'));process.exit(fs.existsSync(process.env.USERPROFILE+'/exit-nonzero')?1:0)";
  fs.writeFileSync(path.join(bin, windows ? "claude.cmd" : "claude"), windows
    ? `@echo off\r\n"${process.execPath}" -e "${relay}" %*\r\n`
    : '#!/bin/sh\n[ "$#" -eq 3 ] && [ "$1" = plugin ] && [ "$2" = list ] && [ "$3" = --json ] || exit 91\ncat "$HOME/inventory.json"\n[ ! -f "$HOME/exit-nonzero" ]\n', {mode:0o700});
  fs.mkdirSync(path.join(root,"shadow directory",windows ? "claude.exe" : "claude"), {recursive:true});
  fs.mkdirSync(path.join(root,"shadow file"));
  fs.writeFileSync(path.join(root,"shadow file","claude"), "not executable", {mode:0o600});
  const cacheRoot = path.join(root, "registered cache");
  const folder = path.join(root, "source folder λ");
  for (const source of [cacheRoot, folder]) {
    fs.mkdirSync(path.join(source, "scripts"), {recursive:true});
    fs.writeFileSync(path.join(source, "scripts/backend-service.sh"), "owned sentinel");
  }
  const record = {id:"claude-smart@reflexioai", installPath:cacheRoot};
  const cases = [
    {data:[record], expected:[cacheRoot]},
    {data:[{...record, readFromFolder:folder, errors:[]}], expected:[folder]},
    {data:[{...record, readFromFolder:path.join(root, "missing")}], expected:[]},
    {raw:"{bad JSON", expected:[]},
    {data:[null, 42, "invalid", record], expected:[cacheRoot]},
    {data:[{...record,id:"another@marketplace"}], expected:[]},
    {data:[{...record,errors:["unavailable"]}], expected:[]},
    {data:[record], nonzero:true, expected:[]},
    {data:[{...record,installPath:"relative/source"}], expected:[]},
    {data:[{...record,errors:{message:"invalid"}}], expected:[]},
    {data:[{...record,readFromFolder:null}], expected:[]},
    {data:[record], shadow:"shadow directory", expected:[cacheRoot]},
    {data:[record],shadow:"shadow file",expected:[cacheRoot]},
  ];
  fs.writeFileSync(path.join(root, "cases.json"), JSON.stringify(cases));
  const script = `
const fs=require('node:fs'),path=require('node:path');
const root=process.env.HOME,owner=require(process.argv[1]);
const basePath=process.env.PATH;
const result=[];
for(const item of JSON.parse(fs.readFileSync(path.join(root,'cases.json'),'utf8'))) {
  process.env.PATH=item.shadow ? path.join(root,item.shadow)+path.delimiter+basePath : basePath;
  fs.writeFileSync(path.join(root,'inventory.json'),item.raw ?? JSON.stringify(item.data));
  const exit=path.join(root,'exit-nonzero');
  if(item.nonzero) fs.writeFileSync(exit,''); else fs.rmSync(exit,{force:true});
  result.push(owner.installedPluginRoots());
}
process.stdout.write(JSON.stringify(result));`;
  const env = {...process.env, HOME:root, USERPROFILE:root,
    CLAUDE_CONFIG_DIR:path.join(root,".claude"), CODEX_HOME:path.join(root,".codex"),
    XDG_CONFIG_HOME:path.join(root,"xdg"), PATH:bin+path.delimiter+process.env.PATH};
  delete env.NODE_OPTIONS;
  const result = spawnSync(process.execPath, ["-e", script, path.resolve(__dirname,"../bin/claude-smart.js")],
    {env,encoding:"utf8",timeout:20000});
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(JSON.parse(result.stdout), cases.map(item => item.expected));
  for (const source of [cacheRoot, folder])
    assert.equal(fs.readFileSync(path.join(source,"scripts/backend-service.sh"),"utf8"),"owned sentinel");
});

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
