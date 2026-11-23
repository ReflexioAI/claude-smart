"""Read and admit one native target, without publishing settings or launching a CLI."""

from __future__ import annotations

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
        admit_native_learning_target()
    except LearningSelectionError as exc:
        print(exc, file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
