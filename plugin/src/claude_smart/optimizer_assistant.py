"""Local-script assistant backend for Reflexio playbook optimization.

Reflexio's ``LocalScriptAssistant`` sends one JSON payload on stdin and expects
one JSON object on stdout. This module bridges that protocol to a guarded
local CLI subprocess so candidate playbooks can be evaluated against the active
host without re-entering claude-smart/reflexio hooks.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any

from claude_smart import internal_call, runtime

_CLI_TIMEOUT_SECONDS = 300
_READ_ONLY_TOOLS = "Read,Grep,Glob,LS"
_MUTATING_TOOLS = "Bash,Edit,Write,MultiEdit,NotebookEdit"


class OptimizerAssistantError(Exception):
    """Raised for any local assistant protocol or Claude CLI failure."""


def main() -> int:
    """Console-script entrypoint for ``claude-smart-optimizer-assistant``."""
    try:
        payload = _read_payload()
        messages = _validated_list(payload, "messages")
        playbooks = _validated_list(payload, "playbooks")
        prompt, system_prompt = _build_prompt(messages, playbooks)
        content = _run_local_cli(prompt=prompt, system_prompt=system_prompt)
    except Exception as exc:  # noqa: BLE001 - script errors become LocalScript failures.
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        return 1

    json.dump({"content": content}, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


def _read_payload() -> dict[str, Any]:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise OptimizerAssistantError("stdin must be a JSON object") from exc
    if not isinstance(payload, dict):
        raise OptimizerAssistantError("stdin must be a JSON object")
    return payload


def _validated_list(payload: dict[str, Any], field: str) -> list[Any]:
    value = payload.get(field)
    if not isinstance(value, list):
        raise OptimizerAssistantError(f"payload.{field} must be a list")
    return value


def _build_prompt(messages: list[Any], playbooks: list[Any]) -> tuple[str, str]:
    normalized = [_normalize_message(message) for message in messages]
    normalized = [message for message in normalized if message["content"]]
    if not normalized:
        raise OptimizerAssistantError("payload.messages must contain content")

    final_message = normalized[-1]
    prior_messages = normalized[:-1]

    system_sections = [_render_playbooks(playbooks)]
    existing_system = [
        message["content"] for message in normalized if message["role"] == "system"
    ]
    if existing_system:
        system_sections.append(
            "## Existing system context\n" + "\n\n".join(existing_system)
        )

    prior_dialogue = [
        message
        for message in prior_messages
        if message["role"] in {"user", "assistant"}
    ]
    if prior_dialogue:
        system_sections.append(
            "## Conversation so far\n" + _render_transcript(prior_dialogue)
        )

    prompt = final_message["content"]
    if final_message["role"] != "user":
        prompt = _render_transcript([final_message])
    system_prompt = "\n\n".join(section for section in system_sections if section)
    return prompt, system_prompt


def _normalize_message(message: Any) -> dict[str, str]:
    if not isinstance(message, dict):
        raise OptimizerAssistantError("each message must be an object")
    role = str(message.get("role") or "user").strip().lower()
    if role not in {"user", "assistant", "system"}:
        role = "user"
    content = message.get("content")
    if not isinstance(content, str):
        raise OptimizerAssistantError("each message.content must be a string")
    return {"role": role, "content": content.strip()}


def _render_playbooks(playbooks: list[Any]) -> str:
    if not playbooks:
        return ""
    lines = ["## Candidate playbook rules"]
    for index, playbook in enumerate(playbooks, start=1):
        if not isinstance(playbook, dict):
            raise OptimizerAssistantError("each playbook must be an object")
        content = playbook.get("content")
        if not isinstance(content, str) or not content.strip():
            raise OptimizerAssistantError("each playbook.content must be a string")
        trigger = playbook.get("trigger")
        suffix = ""
        if isinstance(trigger, str) and trigger.strip():
            suffix = f" (when: {trigger.strip()})"
        lines.append(f"{index}. {content.strip()}{suffix}")
    return "\n".join(lines)


def _render_transcript(messages: list[dict[str, str]]) -> str:
    labels = {"user": "User", "assistant": "Assistant", "system": "System"}
    return "\n\n".join(
        f"{labels.get(message['role'], 'User')}: {message['content']}"
        for message in messages
    )


def _run_local_cli(*, prompt: str, system_prompt: str) -> str:
    if runtime.is_codex():
        return _run_codex(prompt=prompt, system_prompt=system_prompt)
    return _run_claude(prompt=prompt, system_prompt=system_prompt)


def _run_claude(*, prompt: str, system_prompt: str) -> str:
    cli_path = shutil.which("claude") or "claude"
    # This is an evaluation rollout, not a real user session: allow local
    # inspection, but prevent filesystem, shell, MCP, and session mutations.
    cmd = [
        cli_path,
        "-p",
        "--output-format",
        "json",
        "--permission-mode",
        "plan",
        "--tools",
        _READ_ONLY_TOOLS,
        "--disallowedTools",
        _MUTATING_TOOLS,
        "--no-session-persistence",
        "--mcp-config",
        '{"mcpServers": {}}',
        "--strict-mcp-config",
    ]
    if system_prompt:
        cmd.extend(["--append-system-prompt", system_prompt])

    env = os.environ.copy()
    env[runtime.INTERNAL_ENV] = "1"
    env[internal_call._ENTRYPOINT_VAR] = "optimizer"  # noqa: SLF001

    try:
        proc = subprocess.run(  # noqa: S603 - command is fixed plus resolved executable.
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=_CLI_TIMEOUT_SECONDS,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise OptimizerAssistantError(
            f"claude CLI timed out after {_CLI_TIMEOUT_SECONDS}s"
        ) from exc
    except FileNotFoundError as exc:
        raise OptimizerAssistantError("claude CLI not found on PATH") from exc

    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        raise OptimizerAssistantError(
            f"claude CLI exited {proc.returncode}: {stderr[:500]}"
        )

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise OptimizerAssistantError("claude CLI returned non-JSON output") from exc
    if not isinstance(data, dict):
        raise OptimizerAssistantError("claude CLI JSON output must be an object")

    content = data.get("result")
    if not isinstance(content, str):
        content = data.get("response")
    if not isinstance(content, str):
        raise OptimizerAssistantError("claude CLI JSON output missing result/response")
    return content


def _run_codex(*, prompt: str, system_prompt: str) -> str:
    cli_path = shutil.which("codex") or "codex"
    output_path = _temporary_output_path()
    cmd = [
        cli_path,
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
    env[runtime.HOST_ENV] = runtime.HOST_CODEX
    env[runtime.INTERNAL_ENV] = "1"
    env[internal_call._ENTRYPOINT_VAR] = "optimizer"  # noqa: SLF001

    try:
        proc = subprocess.run(  # noqa: S603 - command is fixed plus resolved executable.
            cmd,
            input=_codex_prompt(prompt=prompt, system_prompt=system_prompt),
            capture_output=True,
            text=True,
            timeout=_CLI_TIMEOUT_SECONDS,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise OptimizerAssistantError(
            f"codex CLI timed out after {_CLI_TIMEOUT_SECONDS}s"
        ) from exc
    except FileNotFoundError as exc:
        raise OptimizerAssistantError("codex CLI not found on PATH") from exc

    try:
        content = output_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise OptimizerAssistantError("codex CLI did not write output") from exc
    finally:
        try:
            output_path.unlink()
        except OSError:
            pass

    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        raise OptimizerAssistantError(
            f"codex CLI exited {proc.returncode}: {stderr[:500]}"
        )
    if not content:
        raise OptimizerAssistantError("codex CLI returned empty output")
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
