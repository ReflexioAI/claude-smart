"""Tests for the reflexio self-feedback guard.

``is_internal_invocation`` is the gate that stops the Stop hook from
publishing reflexio's own extractor prompts back into reflexio. A silent
regression here causes the backend to train on its own internals, so
each detection path has an explicit test.
"""

from __future__ import annotations

import pytest

from claude_smart import internal_call, runtime
from claude_smart.internal_call import is_internal_invocation


@pytest.fixture(autouse=True)
def _clear_entrypoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip ``CLAUDE_CODE_ENTRYPOINT`` so individual tests opt in explicitly."""
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)


def test_returns_true_when_env_marker_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_SMART_INTERNAL", "1")
    assert is_internal_invocation({}) is True


def test_env_marker_wins_over_cwd(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_SMART_INTERNAL", "1")
    assert is_internal_invocation({"cwd": "/tmp"}) is True


def test_returns_true_when_cwd_inside_reflexio(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.delenv("CLAUDE_SMART_INTERNAL", raising=False)
    fake_reflexio = tmp_path / "reflexio"
    (fake_reflexio / "server").mkdir(parents=True)
    monkeypatch.setattr(internal_call, "_REFLEXIO_DIR", fake_reflexio)
    assert is_internal_invocation({"cwd": str(fake_reflexio / "server")}) is True


def test_returns_false_for_external_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.delenv("CLAUDE_SMART_INTERNAL", raising=False)
    fake_reflexio = tmp_path / "reflexio"
    fake_reflexio.mkdir()
    monkeypatch.setattr(internal_call, "_REFLEXIO_DIR", fake_reflexio)
    assert is_internal_invocation({"cwd": str(tmp_path)}) is False


def test_returns_false_when_cwd_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_SMART_INTERNAL", raising=False)
    assert is_internal_invocation({}) is False


def test_returns_false_when_cwd_empty_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CLAUDE_SMART_INTERNAL", raising=False)
    assert is_internal_invocation({"cwd": ""}) is False


@pytest.mark.parametrize("bad_cwd", [None, 123, ["/tmp"], {"path": "/tmp"}])
def test_returns_false_when_cwd_wrong_type(
    monkeypatch: pytest.MonkeyPatch, bad_cwd: object
) -> None:
    monkeypatch.delenv("CLAUDE_SMART_INTERNAL", raising=False)
    assert is_internal_invocation({"cwd": bad_cwd}) is False


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


def test_returns_false_for_desktop_entrypoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CLAUDE_SMART_INTERNAL", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "claude-desktop")
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
