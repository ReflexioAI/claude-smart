# claude-smart

Self-improving [Claude Code](https://claude.com/claude-code) and Codex plugin — turns your corrections into durable skills that future sessions follow, via [reflexio](https://github.com/ReflexioAI/reflexio).

This directory is the published Python package (`claude-smart` on PyPI) and the Claude Code/Codex plugin payload shipped through the marketplace. For the project overview, install instructions, benchmarks, and feature walkthrough, see the [top-level README](https://github.com/ReflexioAI/claude-smart#readme).

## Install

```bash
claude plugin marketplace add ReflexioAI/claude-smart
claude plugin install claude-smart@reflexioai
```

The Setup hook bootstraps `uv`, Python 3.12, and a private Node.js/npm runtime
under `~/.claude-smart/` when they are missing. If Node.js is already installed,
`npx claude-smart install` is equivalent; if uv is already installed,
`uvx claude-smart install` is equivalent.

Add `--read-only` to either install command to skip hooks that publish
interactions for learning.

Supported vanilla native targets are Apple Silicon macOS 14+ and Windows x64.
Intel Mac, macOS 13 or older, and Windows ARM fail early because the local
embedding/ML dependency stack does not provide a complete native wheel set.

Then restart Claude Code.

## Uninstall

```bash
claude plugin uninstall claude-smart@reflexioai
```

Or, if Node.js or uv is already installed:

```bash
npx claude-smart uninstall   # or: uvx claude-smart uninstall
```

Local data under `~/.reflexio/` and `~/.claude-smart/` is left in place — remove manually if desired.

## License

Apache 2.0 — see [LICENSE](LICENSE).
