// Tailwind's scanner honors ancestor .gitignore files even in packaged installs.
// Override a home dotfiles repo's ignore-all rule locally, retaining exclusions.
const fs = require("node:fs");
const path = require("node:path");

const ignorePath = path.join(__dirname, "..", ".gitignore");
const existing = fs.existsSync(ignorePath)
  ? fs.readFileSync(ignorePath, "utf8")
  : "/node_modules\n/.next/\n";
const override = "# Keep dashboard sources visible to Tailwind inside a home Git repo.\n!**\n";
if (!existing.startsWith(override)) {
  fs.writeFileSync(ignorePath, override + existing);
}
