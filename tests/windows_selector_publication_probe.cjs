
const fs = require("node:fs");
const assert = require("node:assert/strict");
const {dirname, join} = require("node:path");
const {tmpdir} = require("node:os");
const { randomBytes } = require("node:crypto");
const { mkdirSync, lstatSync, writeFileSync, renameSync, symlinkSync } = fs;
const root = fs.mkdtempSync(join(tmpdir(), "rx-selector-"));
const REFLEXIO_DIR = join(root, ".reflexio");
const old = join(root, "old");
const next = join(root, "next");
const link = join(REFLEXIO_DIR, "plugin-root");
const text = join(REFLEXIO_DIR, "plugin-root.txt");
let unlinkObserved = false;
const isWindows = () => true;
const pathEntryExists = path => { try { fs.lstatSync(path); return true; } catch { return false; } };
const uniquePackagePath = (path, prefix) => join(dirname(path), prefix + "-" + randomBytes(8).toString("hex"));
function rmSync(path, options) {
  if (path === link) {
    unlinkObserved = true;
    assert.equal(fs.readFileSync(text, "utf8").trim(), next);
  }
  return fs.rmSync(path, options);
}
function forcePluginRoot(pluginRoot) {
  mkdirSync(REFLEXIO_DIR, { recursive: true });
  const link = join(REFLEXIO_DIR, "plugin-root");
  try {
    const existing = lstatSync(link);
    if (!existing.isSymbolicLink() && !existing.isFile())
      throw new Error(`refusing to replace non-symlink plugin-root at ${link}`);
  } catch (error) {
    if (error?.code !== "ENOENT") throw error;
  }
  const stagedLink = uniquePackagePath(link, ".plugin-root");
  const text = join(REFLEXIO_DIR, "plugin-root.txt");
  const stagedText = uniquePackagePath(text, ".plugin-root-text");
  try {
    // Junction replacement may require unlinking the old entry. Make the
    // compatibility file valid before crossing any point without a link.
    writeFileSync(stagedText, `${pluginRoot}\n`, { flag: "wx", mode: 0o600 });
    renameSync(stagedText, text);
    try {
      symlinkSync(pluginRoot, stagedLink, isWindows() ? "junction" : "dir");
      renameSync(stagedLink, link);
      return;
    } catch (error) {
      if (!["EACCES", "EPERM", "ENOTSUP", "EEXIST", "ENOTEMPTY"].includes(error?.code))
        throw error;
    }
    rmSync(link, { force: true });
    if (pathEntryExists(stagedLink)) {
      try {
        renameSync(stagedLink, link);
      } catch (error) {
        if (!["EACCES", "EPERM", "ENOTSUP"].includes(error?.code)) throw error;
      }
    }
  } finally {
    rmSync(stagedLink, { force: true });
    rmSync(stagedText, { force: true });
  }
}

try {
  for (const path of [REFLEXIO_DIR, old, next]) fs.mkdirSync(path);
  fs.symlinkSync(old, link, "junction");
  forcePluginRoot(next);
  const active = fs.existsSync(link) ? fs.realpathSync(link) : fs.readFileSync(text, "utf8").trim();
  assert.equal(active, fs.realpathSync(next));
  console.log(JSON.stringify({ native_selector_switched: true, unlink_observed: unlinkObserved,
    fallback_ready_before_unlink: true }));
} finally {
  fs.rmSync(root, { recursive: true, force: true });
}
