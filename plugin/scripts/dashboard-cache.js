/* A build is reusable only for the same sources, runtime and public build inputs. */
"use strict";
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");

function buildKey(root, nodeVersion = process.version) {
  const hash = crypto.createHash("sha256");
  hash.update(JSON.stringify([process.platform, process.arch, nodeVersion,
    Object.entries(process.env).filter(([name]) => name.startsWith("NEXT_PUBLIC_")).sort()]));
  function visit(directory) {
    for (const name of fs.readdirSync(directory).sort()) {
      if (["node_modules", ".next", ".git", "next-env.d.ts", ".DS_Store"].includes(name) || name.endsWith(".tsbuildinfo")) continue;
      const file = path.join(directory, name);
      const stat = fs.statSync(file);
      if (stat.isDirectory()) visit(file);
      else if (stat.isFile()) {
        hash.update(JSON.stringify(path.relative(root, file)));
        hash.update(crypto.createHash("sha256").update(fs.readFileSync(file)).digest());
      }
    }
  }
  visit(root);
  return hash.digest("hex");
}
function reusable(root, nodeVersion = process.version) {
  try {
    return fs.existsSync(path.join(root, "node_modules", "next", "dist", "bin", "next")) &&
      fs.existsSync(path.join(root, ".next", "required-server-files.json")) &&
      fs.existsSync(path.join(root, ".next", "BUILD_ID")) &&
      JSON.parse(fs.readFileSync(path.join(root, ".next", ".claude-smart-inputs"), "utf8")).key === buildKey(root, nodeVersion);
  } catch { return false; }
}
function record(root, nodeVersion = process.version) {
  fs.writeFileSync(path.join(root, ".next", ".claude-smart-inputs"), JSON.stringify({key: buildKey(root, nodeVersion), nodeVersion}));
}
function recordedVersion(root) {
  try { return JSON.parse(fs.readFileSync(path.join(root, ".next", ".claude-smart-inputs"), "utf8")).nodeVersion; }
  catch { return null; }
}
module.exports = { buildKey, reusable, record, recordedVersion };
if (require.main === module) {
  const [action, root] = process.argv.slice(2);
  if (action === "record") record(root);
  else process.exitCode = reusable(root) ? 0 : 1;
}
