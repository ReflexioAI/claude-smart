
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const Module = require("node:module");
const {execFileSync} = require("node:child_process");
assert.equal(process.platform, "win32");
const root = fs.mkdtempSync(path.join(os.tmpdir(), "SDK package λ "));
const entry = path.resolve("bin/claude-smart.js");
const wrapper = new Module(entry, module);
wrapper.filename = entry;
wrapper.paths = Module._nodeModulePaths(path.dirname(entry));
wrapper._compile(fs.readFileSync(entry, "utf8") + "\nmodule.exports = {verifyVendoredReflexioIdentity};", entry);
try {
  const plugin = path.join(root,"candidate");
  const vendor = path.join(plugin,"vendor","reflexio");
  fs.mkdirSync(path.join(vendor,"reflexio","helpers"), {recursive:true});
  fs.writeFileSync(path.join(vendor,"reflexio","__init__.py"), "");
  fs.writeFileSync(path.join(vendor,"reflexio","helpers","example.py"), "marker = True\n");
  const venv = path.join(plugin,".venv");
  execFileSync("python",["-m","venv","--without-pip",venv],{stdio:"inherit"});
  const python = path.join(venv,"Scripts","python.exe");
  const site = execFileSync(python,["-I","-X","utf8","-c","import sysconfig;print(sysconfig.get_path('purelib'))"], {encoding:"utf8"}).trim();
  const installed = path.join(site,"reflexio");
  fs.cpSync(path.join(vendor,"reflexio"),installed,{recursive:true});
  const verify = () => wrapper.exports.verifyVendoredReflexioIdentity(plugin, python, vendor, process.env);
  verify();
  const foreign = path.join(root,"external identical helpers");
  fs.renameSync(path.join(installed,"helpers"), foreign);
  fs.symlinkSync(foreign,path.join(installed,"helpers"),"junction");
  assert.throws(verify, /linked subdirectory/);
  assert.equal(fs.readFileSync(path.join(foreign,"example.py"),"utf8"), "marker = True\n");
  console.log("SDK_WINDOWS_JUNCTION {validPackageAccepted:true,identicalExternalJunctionRejected:true}");
} finally {
  fs.rmSync(root,{recursive:true,force:true});
}
