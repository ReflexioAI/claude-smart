"""Tests for the reflexio self-feedback guard.

``is_internal_invocation`` is the gate that stops the Stop hook from
publishing reflexio's own extractor prompts back into reflexio. A silent
regression here causes the backend to train on its own internals, so
each detection path has an explicit test.
"""

from __future__ import annotations

import importlib
import json
from unittest.mock import Mock

import pytest

from claude_smart import internal_call, runtime
from claude_smart.internal_call import is_internal_invocation


@pytest.fixture(autouse=True)
def _clear_entrypoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests opt into process provenance explicitly, never inherit a caller."""
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.delenv("CLAUDE_SMART_INTERNAL", raising=False)


def test_returns_true_when_env_marker_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_SMART_INTERNAL", "1")
    assert is_internal_invocation({}) is True


def test_env_marker_wins_over_cwd(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_SMART_INTERNAL", "1")
    assert is_internal_invocation({"cwd": "/tmp"}) is True


@pytest.mark.parametrize("relative", ["reflexio", "reflexio/server", "project"])
@pytest.mark.parametrize(
    "entrypoint", [None, "cli", "claude-desktop", "claude-vscode", "claude-jetbrains"]
)
def test_interactive_work_is_not_internal_by_directory(
    monkeypatch, tmp_path, relative, entrypoint
):
    if entrypoint is not None:
        monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", entrypoint)
    # An old exported override cannot turn a user's checkout into provenance.
    monkeypatch.setenv("CLAUDE_SMART_REFLEXIO_DIR", str(tmp_path / "reflexio"))
    try:
        reloaded = importlib.reload(internal_call)
        assert (
            reloaded.is_internal_invocation(
                {"cwd": str(tmp_path / relative), "prompt": "Human coding request"}
            )
            is False
        )
    finally:
        monkeypatch.delenv("CLAUDE_SMART_REFLEXIO_DIR", raising=False)
        importlib.reload(internal_call)


@pytest.mark.parametrize(
    "provenance",
    [
        {"CLAUDE_SMART_INTERNAL": "1"},
        {"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"},
        {"CLAUDE_CODE_ENTRYPOINT": "optimizer"},
    ],
)
def test_internal_dispatch_never_loads_handlers(
    monkeypatch, tmp_path, capsys, provenance
):
    from claude_smart import hook

    for key, value in provenance.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        hook,
        "_read_stdin_json",
        lambda: {"session_id": "owned-internal", "cwd": str(tmp_path)},
    )
    monkeypatch.setattr(hook.env_config, "load_reflexio_env", lambda: None)
    loader = Mock(
        side_effect=AssertionError(
            "Internal invocations must not reach handlers or API clients"
        )
    )
    record = Mock()
    monkeypatch.setattr(hook, "_load_handlers", loader)
    monkeypatch.setattr(hook.hook_log, "log_event", record)
    assert hook.main(["session-start"]) == 0
    loader.assert_not_called()
    assert record.call_args.kwargs["internal_skipped"] is True
    assert json.loads(capsys.readouterr().out) == {
        "continue": True,
        "suppressOutput": True,
    }


def test_env_marker_other_values_do_not_trigger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDE_SMART_INTERNAL", "0")
    assert is_internal_invocation({}) is False
    monkeypatch.setenv("CLAUDE_SMART_INTERNAL", "true")
    assert is_internal_invocation({}) is False


def test_returns_true_for_headless_entrypoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CLAUDE_SMART_INTERNAL", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "sdk-cli")
    assert is_internal_invocation({}) is True


def test_returns_false_for_interactive_entrypoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CLAUDE_SMART_INTERNAL", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    assert is_internal_invocation({}) is False


@pytest.mark.parametrize(
    "entrypoint", ["claude-desktop", "claude-vscode", "claude-jetbrains"]
)
def test_returns_false_for_interactive_entrypoints(
    monkeypatch: pytest.MonkeyPatch, entrypoint: str
) -> None:
    monkeypatch.delenv("CLAUDE_SMART_INTERNAL", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", entrypoint)
    assert is_internal_invocation({}) is False


def test_returns_true_for_codex_title_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_SMART_INTERNAL", raising=False)
    runtime.set_host(runtime.HOST_CODEX)

    assert (
        is_internal_invocation(
            {
                "prompt": (
                    "You are a helpful assistant. You will be presented with a user "
                    "prompt, and your job is to provide a short title for a task "
                    "that will be created from that prompt."
                )
            }
        )
        is True
    )


@pytest.mark.parametrize(
    "content",
    [
        '{"title":"Find claude-smart install type"}',
        '{ "title": "Install local claude-smart plugin" }',
    ],
)
def test_detects_codex_title_response(content: str) -> None:
    assert internal_call.is_codex_title_response(content) is True


@pytest.mark.parametrize(
    "content",
    [
        "",
        "not json",
        '{"title": ""}',
        '{"title": "real", "body": "extra"}',
        '["title", "real"]',
        {"title": "real"},
    ],
)
def test_rejects_non_title_responses(content: object) -> None:
    assert internal_call.is_codex_title_response(content) is False


def test_detects_codex_suggestions_response() -> None:
    content = (
        '{"suggestions":[{"title":"Prepare patch","description":"Fix the issue.",'
        '"prompt":"Make the change.","appId":""}]}'
    )

    assert internal_call.is_codex_suggestions_response(content) is True


@pytest.mark.parametrize(
    "content",
    [
        "",
        "not json",
        '{"suggestions": "nope"}',
        '{"suggestions": [{"title": "x"}]}',
        (
            '{"suggestions": [{"title": "x", "description": "d", '
            '"prompt": "p", "appId": "", "extra": "e"}]}'
        ),
        (
            '{"suggestions": [{"title": "x", "description": "d", '
            '"prompt": "p", "appId": 1}]}'
        ),
        '{"suggestions": [], "body": "extra"}',
        {"suggestions": []},
    ],
)
def test_rejects_non_suggestions_responses(content: object) -> None:
    assert internal_call.is_codex_suggestions_response(content) is False


def test_returns_true_for_codex_suggestions_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CLAUDE_SMART_INTERNAL", raising=False)
    runtime.set_host(runtime.HOST_CODEX)

    assert (
        is_internal_invocation(
            {
                "prompt": (
                    "# Overview\n\nGenerate 0 to 3 hyperpersonalized suggestions "
                    "for what this user can do with Codex in this local project: "
                    "/tmp/repo\n\nGet an understanding of the user's intent and "
                    "goals by deeply viewing their connected apps."
                )
            }
        )
        is True
    )


def test_codex_prompt_fingerprints_do_not_apply_to_claude_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CLAUDE_SMART_INTERNAL", raising=False)
    runtime.set_host(runtime.HOST_CLAUDE_CODE)

    assert (
        is_internal_invocation(
            {
                "prompt": (
                    "You are a helpful assistant. You will be presented with a user "
                    "prompt, and your job is to provide a short title for a task."
                )
            }
        )
        is False
    )
