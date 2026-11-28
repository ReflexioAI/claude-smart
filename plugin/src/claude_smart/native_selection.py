"""Read and admit one native target, without publishing settings or launching a CLI."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from claude_smart.env_config import (
    NATIVE_MODEL_ENV,
    RETIRED_LEARNING_ENVS,
    native_selection_confirmed,
    parse_env_line,
)

if TYPE_CHECKING:
    from reflexio.server.llm._native_model_ref import NativeModelRef


class LearningSelectionError(ValueError):
    """Value-redacted refusal before settings or running services are replaced."""


_PREPARED_LAUNCHER = "guide-native-launcher"


def _write_prepared_launcher(values: dict[str, str]) -> None:
    venv = Path(sys.prefix)
    if venv.name != ".venv":
        raise LearningSelectionError(
            "A prepared native launcher requires the setup interpreter."
        )
    receipt = venv.parent / _PREPARED_LAUNCHER
    fd = os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(values, stream)


def record_default_launcher(host: str) -> None:
    """Record the SDK's default executable in the runtime that owns this venv."""
    from reflexio.server.llm.providers.native_cli_provider import _resolve_cli_path

    if host not in {"claude-code", "codex"}:
        raise LearningSelectionError(
            "A prepared native launcher requires a known host."
        )
    executable = _resolve_cli_path(host)
    if executable is None:
        return
    _write_prepared_launcher({"host": host, "executable": executable})


def _resolve_opencode(selection: str | None = None) -> str:
    from reflexio.opencode_executable import OpenCodeCLIError, resolve_executable

    choices = (selection,) if selection is not None else ("opencode",)
    if selection is None and os.name == "nt":
        choices = ("opencode.cmd", "opencode.exe", "opencode")
    for choice in choices:
        try:
            executable = resolve_executable(choice)
        except OpenCodeCLIError:
            continue
        if os.name == "nt" and executable.suffix.lower() not in {
            ".exe",
            ".cmd",
            ".bat",
        }:
            raise LearningSelectionError(
                "Select the OpenCode executable in Claude Smart setup."
            )
        return str(executable)
    raise LearningSelectionError(
        "OpenCode could not be selected. Confirm its executable in Claude Smart setup."
    )


def record_adopted_opencode_launcher(
    selection: str, expected_executable: str | None = None
) -> None:
    """Carry the old package transport contract for the exact admitted executable."""
    if not Path(selection).is_absolute():
        raise LearningSelectionError(
            "Confirm the absolute OpenCode executable in Claude Smart setup."
        )
    executable = _resolve_opencode(selection)
    if expected_executable is not None and expected_executable != executable:
        raise LearningSelectionError(
            "The selected OpenCode launcher changed. Confirm the executable in Claude Smart setup."
        )
    _write_prepared_launcher(
        {
            "host": "opencode",
            "selection": selection,
            "executable": executable,
        }
    )


def configure_opencode_launcher(plugin_root: Path) -> None:
    """Use the SDK launcher with the expected wire format for this installation."""
    from reflexio.server.llm._native_model_ref import PLUGIN_OPENCODE_EXECUTABLE_ENV

    os.environ.pop(PLUGIN_OPENCODE_EXECUTABLE_ENV, None)
    selection = os.environ.get("CLAUDE_SMART_CLI_PATH", "")
    receipt = plugin_root / _PREPARED_LAUNCHER
    if selection.strip():
        if (
            not (plugin_root / "guide-prepared-runtime").is_file()
            or not receipt.is_file()
        ):
            return
        try:
            binding = json.loads(receipt.read_text(encoding="utf-8"))
            if (
                binding.get("host") != "opencode"
                or binding.get("selection") != selection
            ):
                return
            executable = _resolve_opencode(selection)
            if binding["executable"] != executable:
                raise ValueError
        except (OSError, AttributeError, KeyError, ValueError) as exc:
            raise LearningSelectionError(
                "The prepared OpenCode executable has changed. Rerun Claude Smart setup."
            ) from exc
    else:
        executable = _resolve_opencode()
    os.environ[PLUGIN_OPENCODE_EXECUTABLE_ENV] = executable
    launcher = executable
    # An opaque Windows batch executable still requires the existing shell-
    # quoting adapter. Known npm forwarding shims resolve directly to .exe.
    if os.name == "nt" and Path(executable).suffix.lower() in {".cmd", ".bat"}:
        launcher = str(plugin_root / "scripts" / "opencode-claude-compat.cmd")
        if not Path(launcher).is_file():
            raise LearningSelectionError(
                "Repair the bundled OpenCode launcher in Claude Smart setup."
            )
    os.environ["CLAUDE_SMART_CLI_PATH"] = launcher


def use_prepared_launcher(plugin_root: Path, host: str) -> None:
    """Reach a setup-resolved default without changing explicit SDK overrides."""
    receipt = plugin_root / _PREPARED_LAUNCHER
    if not (plugin_root / "guide-prepared-runtime").is_file() or not receipt.is_file():
        return

    from reflexio.server.llm.providers.native_cli_provider import (
        _anchored_override,
        _resolve_cli_override_path,
        _resolve_cli_path,
    )

    override = os.environ.get("CLAUDE_SMART_CLI_PATH")
    if (
        override
        and _anchored_override(_resolve_cli_override_path(override)) is not None
    ):
        return

    guidance = "The prepared native launcher is unavailable. Rerun Claude Smart setup."
    try:
        binding = json.loads(receipt.read_text(encoding="utf-8"))
        if binding["host"] != host:
            return
        executable = Path(binding["executable"])
        if not executable.is_absolute():
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        raise LearningSelectionError(guidance) from None

    current = _resolve_cli_path(host)
    if current == str(executable):
        return

    original = os.environ.get("PATH")
    entries = os.get_exec_path()
    position = len(entries)
    if current is not None:
        competing = Path(current).parent
        position = next(
            (
                i
                for i, value in enumerate(entries)
                if value and Path(value).absolute() == competing
            ),
            position,
        )
    # Keep earlier utilities such as the runtime's private Node ahead of the
    # new directory. Only a competing launcher justifies extending precedence.
    entries.insert(position, str(executable.parent))
    os.environ["PATH"] = os.pathsep.join(entries)
    if _resolve_cli_path(host) != str(executable):
        if original is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = original
        raise LearningSelectionError(guidance)


def admit_native_learning_target() -> NativeModelRef | None:
    """Use the SDK for syntax; preserve explicit opt-out and saved model pins."""
    source = os.environ.get("REFLEXIO_ENV_FILE", "")
    saved: dict[str, str] = {}
    nonempty_saved: set[str] = set()
    references: list[str] = []
    content = ""
    if source and Path(source).is_file():
        content = Path(source).read_bytes().decode("utf-8")
        for line in content.split("\n"):
            if (entry := parse_env_line(line)) is not None:
                saved[entry[0]] = entry[1]
                if entry[1].strip():
                    nonempty_saved.add(entry[0])
                if entry[0] == NATIVE_MODEL_ENV:
                    references.append(entry[1])
    if len(set(references)) > 1:
        raise LearningSelectionError(
            "Saved native target assignments are ambiguous. Confirm one target in setup."
        )
    for name in ("CLAUDE_SMART_CLI_MODEL", *sorted(RETIRED_LEARNING_ENVS)):
        if (
            (name in saved or bool(os.environ.get(name, "")))
            if name in RETIRED_LEARNING_ENVS
            else (name in nonempty_saved or bool(os.environ.get(name, "").strip()))
        ):
            guidance = (
                "Record the effective model in REFLEXIO_NATIVE_CLI_MODEL and "
                "remove the obsolete assignment before restarting."
                if name == "CLAUDE_SMART_CLI_MODEL"
                else "Run confirmed Claude Smart setup to convert the saved "
                "learning selection before restarting."
            )
            raise LearningSelectionError(f"{name} has been retired. {guidance}")
    # Only backend children read saved execution intent. An explicitly empty
    # caller value eclipses a saved target, including later worker dotenv loads.
    selected = os.environ.get(NATIVE_MODEL_ENV, saved.get(NATIVE_MODEL_ENV, ""))
    if not selected.strip():
        if NATIVE_MODEL_ENV not in os.environ and (
            NATIVE_MODEL_ENV not in saved or not native_selection_confirmed(content)
        ):
            raise LearningSelectionError(
                "Confirm a native learning target or explicit cloud selection in Claude Smart setup."
            )
        return None  # Cloud-only settings repair needs no SDK/provider imports.
    try:
        from dotenv import dotenv_values
        from reflexio.server.llm._native_model_ref import (
            NativeCLISelectionError,
            parse_native_model_ref,
        )
    except ImportError as exc:
        raise LearningSelectionError(
            "Claude Smart requires its bundled SDK native model interface. "
            "Repair or update the plugin bundle before restarting."
        ) from exc
    sdk_saved = (
        dotenv_values(source, interpolate=False)
        if source and Path(source).is_file()
        else {}
    )
    if references and references[-1] != (sdk_saved.get(NATIVE_MODEL_ENV) or ""):
        raise LearningSelectionError(
            "Saved native target quoting is ambiguous. Confirm one target in setup."
        )
    try:
        target = parse_native_model_ref(selected)
    except NativeCLISelectionError as exc:
        raise LearningSelectionError(
            "Native learning target is invalid; use a local-cli host reference."
        ) from exc
    if (
        target.host == "claude-code"
        and target.model is None
        and _has_saved_sonnet_pin()
    ):
        raise LearningSelectionError(
            "The saved Sonnet model pin requires an explicit "
            "local-cli/claude-code/sonnet target."
        )
    if target.host == "opencode":
        old_path = "CLAUDE_SMART_OPENCODE_PATH"
        if (
            os.environ.get(old_path, "").strip()
            or (sdk_saved.get(old_path) or "").strip()
        ):
            raise LearningSelectionError(
                "CLAUDE_SMART_OPENCODE_PATH has been retired. "
                "Confirm the existing executable in Claude Smart setup before restarting."
            )
        for old_name in (
            "CLAUDE_SMART_OPENCODE_MODEL",
            "CLAUDE_SMART_OPENCODE_VARIANT",
        ):
            if (
                os.environ.get(old_name, "").strip()
                or (sdk_saved.get(old_name) or "").strip()
            ):
                raise LearningSelectionError(
                    f"{old_name} has been retired. "
                    "Save the model/variant in REFLEXIO_NATIVE_CLI_MODEL and remove the old setting."
                )
    return target


def _has_saved_sonnet_pin() -> bool:
    # Read the SDK's historical document without rewriting models or data.
    from reflexio.cli.paths import reflexio_home
    from reflexio.config_env import StoredConfigurationError
    from reflexio.config_file import LocalConfigFile

    config = reflexio_home() / "configs/config_claude-smart.json"
    try:
        document = LocalConfigFile(config).read_document_for_conversion()
    except (FileNotFoundError, StoredConfigurationError):
        return False
    roles = document.get("llm_config")
    return isinstance(roles, dict) and "local-cli/claude-code/sonnet" in roles.values()


def main() -> int:
    try:
        if len(sys.argv) == 3 and sys.argv[1] == "--record-default-launcher":
            record_default_launcher(sys.argv[2])
        elif (
            len(sys.argv) in {3, 4}
            and sys.argv[1] == "--record-adopted-opencode-launcher"
        ):
            record_adopted_opencode_launcher(*sys.argv[2:])
        else:
            admit_native_learning_target()
    except LearningSelectionError as exc:
        print(exc, file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
