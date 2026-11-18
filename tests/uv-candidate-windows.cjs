
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const Module = require("node:module");
const {execFileSync} = require("node:child_process");
assert.equal(process.platform, "win32");
const root = fs.mkdtempSync(path.join(os.tmpdir(), "owned uv λ "));
const candidate = path.join(root, "candidate");
const previous = path.join(root, "previous");
const uv = process.argv[2];
const entry = path.resolve("bin/claude-smart.js");
const wrapper = new Module(entry, module);
wrapper.filename = entry;
wrapper.paths = Module._nodeModulePaths(path.dirname(entry));
wrapper._compile(fs.readFileSync(entry, "utf8") + "\nmodule.exports = {preparePluginPython};", entry);
async function main() {
  try {
    for (const [dir, name] of [[candidate, "candidate"], [previous, "previous"]]) {
      fs.mkdirSync(dir);
      fs.writeFileSync(path.join(dir, "pyproject.toml"),
        '[project]\nname = "' + name + '"\nversion = "0.1.0"\nrequires-python = ">=3.12"\n');
    }
    const lock = path.join(candidate, "uv.lock");
    const prior = fs.readFileSync(path.join(previous, "pyproject.toml"));
    const env = { ...process.env, UV_OFFLINE: "1", UV_CACHE_DIR: path.join(root,"cache") };
    execFileSync(uv, ["lock", "--offline", "--python", "3.12"], { cwd: candidate, env, stdio:"inherit" });
    const lockedBytes = fs.readFileSync(lock);
    await wrapper.exports.preparePluginPython(candidate, uv, {
      ...env, uv_project: previous, Uv_Working_Dir: previous,
      uv_project_environment: previous, VIRTUAL_ENV: previous,
    });
    assert.deepEqual(fs.readFileSync(lock), lockedBytes);
    assert.deepEqual(fs.readFileSync(path.join(previous,"pyproject.toml")), prior);
    assert.deepEqual(fs.readdirSync(previous), ["pyproject.toml"]);
    const python = path.join(candidate, ".venv", "Scripts", "python.exe");
    const prefix = execFileSync(python, ["-I", "-c", "import sys;print(sys.prefix)"], {encoding:"utf8"}).trim();
    assert.equal(fs.realpathSync(prefix), fs.realpathSync(path.join(candidate, ".venv")));
    console.log("UV_NATIVE_OWNERSHIP " + JSON.stringify({bound:true, previousPreserved:true, mixedCase:true, lockUnchanged:true}));
  } finally { fs.rmSync(root, {recursive:true,force:true}); }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
