"""The private Node.js major is spelled twice and both spellings must agree.

`plugin/scripts/smart-install.sh` (shell installer) and `bin/claude-smart.js`
(npm CLI) each download a private Node.js into `~/.claude-smart/node` when the
host has none, defaulting `CLAUDE_SMART_NODE_LTS_MAJOR` independently. A bump
applied to one copy would silently give users a different runtime depending on
which entry point installed it.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_SHELL_DEFAULT = re.compile(r'NODE_LTS_MAJOR="\$\{CLAUDE_SMART_NODE_LTS_MAJOR:-(\d+)\}"')
_JS_DEFAULT = re.compile(r'CLAUDE_SMART_NODE_LTS_MAJOR \|\| "(\d+)"')


def _single(pattern: re.Pattern[str], path: Path) -> str:
    matches = pattern.findall(path.read_text(encoding="utf-8"))
    assert len(matches) == 1, f"expected one private Node default in {path}, found {matches}"
    return matches[0]


def test_shell_and_js_private_node_defaults_agree() -> None:
    shell = _single(_SHELL_DEFAULT, ROOT / "plugin" / "scripts" / "smart-install.sh")
    js = _single(_JS_DEFAULT, ROOT / "bin" / "claude-smart.js")
    assert shell == js, f"smart-install.sh defaults to Node {shell}, claude-smart.js to {js}"


def test_documented_default_matches_installers() -> None:
    shell = _single(_SHELL_DEFAULT, ROOT / "plugin" / "scripts" / "smart-install.sh")
    developer = (ROOT / "DEVELOPER.md").read_text(encoding="utf-8")
    assert f"| `CLAUDE_SMART_NODE_LTS_MAJOR` | `{shell}` |" in developer
