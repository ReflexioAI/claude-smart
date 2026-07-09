# claude-smart

Self-improving [Claude Code](https://claude.com/claude-code), Codex, and OpenCode plugin — turns corrections and successful workflows into durable Preferences, Project-specific skills, and Shared skills that future sessions follow, via [reflexio](https://github.com/ReflexioAI/reflexio).

This directory is the Claude Code/Codex/OpenCode plugin payload shipped through the marketplace and the `claude-smart` npm package. For the project overview, install instructions, benchmarks, and feature walkthrough, see the [top-level README](https://github.com/ReflexioAI/claude-smart#readme).

Claude Code and Codex also get a read-only `search_learnings` MCP tool. Agents
use it before non-trivial work with a rewritten, task-specific query and the
active repo/workspace absolute path as `cwd`, so claude-smart searches the right
project memories instead of blindly reusing the raw user prompt.

## Install

### Claude Code

```bash
claude plugin marketplace add ReflexioAI/claude-smart
claude plugin install claude-smart@reflexioai
```

The Setup hook bootstraps `uv`, Python 3.12, and a private Node.js/npm runtime
under `~/.claude-smart/` when they are missing. If Node.js is already installed,
`npx claude-smart install` is equivalent.

Add `--read-only` to either install command to skip hooks that publish
interactions for learning.

Supported vanilla native targets are Apple Silicon macOS 14+ and Windows x64.
Intel Mac, macOS 13 or older, and Windows ARM fail early because the local
embedding/ML dependency stack does not provide a complete native wheel set.

Then restart Claude Code.

### Codex

```bash
npx claude-smart install --host codex
```

Then fully quit and reopen Codex so hooks reload. Codex installs reuse the same
local Preferences, Project-specific skills, and Shared skills as Claude Code.

### OpenCode

```bash
npx claude-smart install --host opencode
```

Then restart OpenCode in your project so it loads the plugin from `opencode.json`
or your existing `.opencode/opencode.json*` config. The installer copies the
active npm package to `~/.claude-smart/opencode/claude-smart` and registers that
stable local package with `file://`. OpenCode installs reuse the same local
Preferences, Project-specific skills, and Shared skills as Claude Code and Codex.

## Uninstall

### Claude Code

```bash
claude plugin uninstall claude-smart@reflexioai
```

Or, if Node.js is already installed:

```bash
npx claude-smart uninstall
```

### Codex

```bash
npx claude-smart uninstall --host codex
```

Restart Codex after uninstalling. Local data under `~/.reflexio/` and
`~/.claude-smart/` is left in place for both hosts — remove manually if desired.

### OpenCode

```bash
npx claude-smart uninstall --host opencode
```

Restart OpenCode after uninstalling. The copied OpenCode package is removed, but
local learning data under `~/.reflexio/` and `~/.claude-smart/` is preserved and
shared across supported hosts.

## License

Apache 2.0 — see [LICENSE](LICENSE).
