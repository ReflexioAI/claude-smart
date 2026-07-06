"""Tests for the user-facing ``claude-smart show`` command."""

from __future__ import annotations

import argparse
import io
import sys

from claude_smart import cli


def test_show_reports_reflexio_read_errors(monkeypatch, capsys) -> None:
    class BrokenAdapter:
        url = "https://www.reflexio.ai/"
        read_errors = ["fetch_project_profiles: 503 Service Temporarily Unavailable"]

        def fetch_all(self, **_kwargs):
            return [], [], []

    monkeypatch.setattr(cli.ids, "resolve_user_id", lambda: "claude-smart")
    monkeypatch.setattr(cli, "Adapter", BrokenAdapter)

    code = cli.cmd_show(argparse.Namespace(project=None))

    out = capsys.readouterr().out
    assert code == 1
    assert "Could not read skills or preferences from Reflexio" in out
    assert "project `claude-smart`" in out
    assert "https://www.reflexio.ai/" in out
    assert "503 Service Temporarily Unavailable" in out
    assert "_No skills or preferences yet" not in out
    # No promo footer on the read-error path.
    assert "star it on GitHub" not in out


def test_show_reports_empty_when_reflexio_read_succeeds(monkeypatch) -> None:
    class EmptyAdapter:
        url = "https://www.reflexio.ai/"
        read_errors: list[str] = []

        def fetch_all(self, **_kwargs):
            return [], [], []

    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(cli.ids, "resolve_user_id", lambda: "claude-smart")
    monkeypatch.setattr(cli, "Adapter", EmptyAdapter)

    code = cli.cmd_show(argparse.Namespace(project=None))

    stream.flush()
    out = raw.getvalue().decode("cp1252")
    assert code == 0
    assert "_No skills or preferences yet for project `claude-smart`._" in out
    # The reflexio attribution footer rides along even when empty.
    assert "powered by [reflexio](https://github.com/ReflexioAI/reflexio)" in out
    assert "star it on GitHub" in out


def test_show_appends_reflexio_footer_when_populated(monkeypatch, capsys) -> None:
    class PopulatedAdapter:
        url = "https://www.reflexio.ai/"
        read_errors: list[str] = []

        def fetch_all(self, **_kwargs):
            return [{"content": "use pathlib", "user_playbook_id": 7}], [], []

    monkeypatch.setattr(cli.ids, "resolve_user_id", lambda: "claude-smart")
    monkeypatch.setattr(cli, "Adapter", PopulatedAdapter)

    code = cli.cmd_show(argparse.Namespace(project=None))

    out = capsys.readouterr().out
    assert code == 0
    assert "use pathlib" in out
    # Footer follows the rendered skills/preferences.
    assert out.index("use pathlib") < out.index("star it on GitHub")
    assert "powered by [reflexio](https://github.com/ReflexioAI/reflexio)" in out
