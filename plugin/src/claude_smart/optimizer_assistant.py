"""Local-script assistant backend for Reflexio playbook optimization.

Reflexio's ``LocalScriptAssistant`` sends one JSON payload on stdin and expects
one JSON object on stdout. This module compiles native Claude/Codex requests
for the SDK to execute, or runs the remaining OpenCode compatibility bridge.
Candidate playbooks are evaluated without re-entering claude-smart hooks.

Host CLI resolution: the saved learning provider selects native Codex when no
custom executable is configured. Explicit custom optimizer executables retain
Claude protocol; the OpenCode bridge still supplies that protocol. The backend
and optimizer both use the user's authenticated CLI rather than a cloud key.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from claude_smart import internal_call, runtime

_INVOCATION_ENV = "_REFLEXIO_ASSISTANT_INVOCATION"
# Mirrors ``reflexio.server.llm.providers.claude_code_provider._ENV_CLI_PATH``
# for explicit custom optimizer executables and the remaining OpenCode bridge.
_ENV_CLI_PATH = "CLAUDE_SMART_CLI_PATH"
_CLAUDE_SMART_COMPAT_BRIDGE_NAMES = frozenset(
    {
        "opencode-claude-compat",
        "opencode-claude-compat.cmd",
        "opencode-claude-compat.js",
    }
)


class OptimizerAssistantError(Exception):
    """Raised for any local assistant protocol or Claude CLI failure."""


def main() -> int:
    """Console-script entrypoint for ``claude-smart-optimizer-assistant``."""
    try:
        deadline = _invocation_deadline()
        payload = _read_payload()
        messages = _validated_list(payload, "messages")
        playbooks = _validated_list(payload, "playbooks")
        prompt, system_prompt = _build_prompt(messages, playbooks)
        result = _prepare_result(
            prompt=prompt, system_prompt=system_prompt, deadline=deadline
        )
    except Exception as exc:  # noqa: BLE001 - script errors become LocalScript failures.
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        return 1

    json.dump(result, sys.stdout, ensure_ascii=False)
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


def _invocation_deadline() -> float:
    """Accept caller-owned IPC facts, never a configurable helper timeout."""
    try:
        invocation = json.loads(os.environ.get(_INVOCATION_ENV, "null"))
        if (
            not isinstance(invocation, dict)
            or invocation.get("protocol") != "native-cli/v1"
        ):
            raise ValueError("unsupported invocation")
        value = invocation["deadline_monotonic"]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("invalid invocation deadline")
        deadline = float(value)
        if not math.isfinite(deadline):
            raise ValueError("invalid invocation deadline")
    except (ValueError, TypeError, KeyError, OverflowError):
        raise OptimizerAssistantError(
            "Upgrade the calling server SDK to native-cli/v1 before using this helper"
        ) from None
    _remaining(deadline)
    return deadline


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise OptimizerAssistantError("assistant invocation deadline expired")
    return remaining


def _prepare_result(
    *, prompt: str, system_prompt: str, deadline: float
) -> dict[str, Any]:
    """Compile Claude/Codex calls; retain the explicitly legacy bridge path."""
    provider = os.environ.get("CLAUDE_SMART_LEARNING_PROVIDER")
    override = os.environ.get(_ENV_CLI_PATH)
    codex = (
        provider == "codex"
        and (not override or _is_claude_smart_compat_bridge(override))
    ) or (not provider and runtime.is_codex())
    model = os.environ.get("CLAUDE_SMART_LEARNING_MODEL")
    if codex:
        protocol = "codex"
        executable = shutil.which("codex") or "codex"
    else:
        executable = _resolve_claude_cli_path()
        if _is_claude_smart_compat_bridge(executable):
            return {
                "content": _run_compat_bridge(
                    cli_path=executable,
                    prompt=prompt,
                    system_prompt=system_prompt,
                    model=model,
                    deadline=deadline,
                )
            }
        protocol = "claude-json"
        if (
            not model
            and not override
            and os.environ.get("CLAUDE_SMART_USE_LOCAL_CLI") != "0"
        ):
            model = "sonnet"
    _remaining(deadline)
    resolved = shutil.which(executable)
    if resolved is None and Path(executable).name == executable:
        raise OptimizerAssistantError("native assistant executable not found on PATH")
    return {
        "native_cli_v1": {
            "protocol": protocol,
            "executable": str(Path(resolved or executable).absolute()),
            "model": model or None,
            "dialogue": prompt,
            "system_prompt": system_prompt,
        }
    }


def _resolve_claude_cli_path() -> str:
    """Return the host CLI to invoke for evaluation rollouts.

    When ``CLAUDE_SMART_CLI_PATH`` is set, prefer it so backend-service.sh can
    route the assistant through the same bridge it uses for the reflexio
    ``claude-code`` provider (e.g. ``opencode-claude-compat`` for the OpenCode
    host). Falls back to ``shutil.which("claude")`` so Claude Code hosts — and
    any environment that never set the override — continue to use the real
    ``claude`` CLI.
    """
    override = os.environ.get(_ENV_CLI_PATH)
    if override:
        return override
    return shutil.which("claude") or "claude"


def _is_claude_smart_compat_bridge(cli_path: str) -> bool:
    return Path(cli_path).name.lower() in _CLAUDE_SMART_COMPAT_BRIDGE_NAMES


def _run_compat_bridge(
    *,
    cli_path: str,
    prompt: str,
    system_prompt: str,
    model: str | None,
    deadline: float,
) -> str:
    """Legacy OpenCode wrapper contract; no native descendant-ownership claim."""
    if os.environ.get("CLAUDE_SMART_OPENCODE_MODEL", "").strip():
        raise OptimizerAssistantError(
            "CLAUDE_SMART_OPENCODE_MODEL has been retired. Use "
            "CLAUDE_SMART_LEARNING_MODEL for the saved OpenCode route and clear "
            "the old setting."
        )
    cmd = [cli_path, "-p", "--output-format", "json"]
    if model:
        cmd.extend(["--model", model])
    if system_prompt:
        cmd.extend(["--append-system-prompt", system_prompt])
    env = os.environ.copy()
    env[runtime.INTERNAL_ENV] = "1"
    env[internal_call._ENTRYPOINT_VAR] = "optimizer"  # noqa: SLF001
    try:
        proc = subprocess.run(  # noqa: S603 — existing explicitly selected compatibility wrapper.
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=_remaining(deadline),
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired:
        raise OptimizerAssistantError("compatibility bridge timed out") from None
    except (OSError, ValueError):
        raise OptimizerAssistantError(
            "compatibility bridge could not execute"
        ) from None
    if proc.returncode != 0:
        raise OptimizerAssistantError("compatibility bridge execution failed")
    try:
        data = json.loads(proc.stdout)
    except (ValueError, TypeError):
        raise OptimizerAssistantError(
            "compatibility bridge returned invalid JSON"
        ) from None
    content = data.get("result") if isinstance(data, dict) else None
    if not isinstance(content, str) and isinstance(data, dict):
        content = data.get("response")
    if not isinstance(content, str):
        raise OptimizerAssistantError("compatibility bridge returned no content")
    _remaining(deadline)
    return content


if __name__ == "__main__":
    raise SystemExit(main())
