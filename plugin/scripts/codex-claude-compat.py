#!/usr/bin/env python3
"""Translate Reflexio's Claude CLI provider contract to ``codex exec``.

Reflexio's local provider currently shells out to ``CLAUDE_SMART_CLI_PATH`` as
if it were the Claude Code CLI:

    <path> -p --output-format json --model <model> [--append-system-prompt ...]

When claude-smart runs under Codex, this executable preserves that narrow
contract while routing the actual model call through ``codex exec``. The
stdout shape intentionally matches Claude Code's JSON output enough for
Reflexio's provider to read ``result``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


_TIMEOUT_SECONDS = 120


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        system_prompt = _parse_supported_args(argv)
        content = _run_codex(
            prompt=sys.stdin.read(),
            system_prompt=system_prompt,
        )
    except Exception as exc:  # noqa: BLE001 - CLI bridge errors go to stderr.
        print(f"codex-claude-compat: {exc}", file=sys.stderr)
        return 1

    json.dump({"result": content}, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


def _parse_supported_args(argv: list[str]) -> str:
    system_prompt = ""
    idx = 0
    while idx < len(argv):
        arg = argv[idx]
        if arg == "-p":
            idx += 1
        elif arg == "--output-format":
            idx += 2
        elif arg == "--model":
            idx += 2
        elif arg == "--append-system-prompt":
            if idx + 1 >= len(argv):
                raise ValueError("--append-system-prompt requires a value")
            system_prompt = argv[idx + 1]
            idx += 2
        else:
            raise ValueError(f"unsupported Claude CLI argument: {arg}")
    return system_prompt


def _run_codex(*, prompt: str, system_prompt: str) -> str:
    codex_path = os.environ.get("CLAUDE_SMART_CODEX_PATH") or shutil.which("codex")
    if not codex_path:
        raise FileNotFoundError("codex CLI not found on PATH")

    output_path = _temporary_output_path()
    cmd = [
        codex_path,
        "exec",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--ephemeral",
        "--ignore-rules",
        "--output-last-message",
        str(output_path),
        "-",
    ]

    env = os.environ.copy()
    env["CLAUDE_SMART_HOST"] = "codex"
    env["CLAUDE_SMART_INTERNAL"] = "1"
    env["CLAUDE_CODE_ENTRYPOINT"] = "optimizer"

    try:
        proc = subprocess.run(  # noqa: S603 - fixed command plus resolved executable.
            cmd,
            input=_codex_prompt(prompt=prompt, system_prompt=system_prompt),
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
            check=False,
            env=env,
        )
        if proc.returncode != 0:
            stderr = proc.stderr.strip()
            raise RuntimeError(f"codex CLI exited {proc.returncode}: {stderr[:500]}")
        content = output_path.read_text(encoding="utf-8").strip()
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"codex CLI timed out after {_TIMEOUT_SECONDS}s") from exc
    finally:
        try:
            output_path.unlink()
        except OSError:
            pass

    if not content:
        raise RuntimeError("codex CLI returned empty output")
    return content


def _temporary_output_path() -> Path:
    handle = tempfile.NamedTemporaryFile(prefix="claude-smart-codex-", delete=False)
    try:
        return Path(handle.name)
    finally:
        handle.close()


def _codex_prompt(*, prompt: str, system_prompt: str) -> str:
    if not system_prompt:
        return prompt
    return f"{system_prompt}\n\n## Task\n{prompt}"


if __name__ == "__main__":
    raise SystemExit(main())
