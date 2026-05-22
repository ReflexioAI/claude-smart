"""Tests for the user-facing ``claude-smart show`` command."""

from __future__ import annotations

from types import SimpleNamespace

from claude_smart import cli


def test_show_reports_reflexio_read_errors(monkeypatch, capsys) -> None:
    class BrokenAdapter:
        url = "https://www.reflexio.ai/"
        read_errors = ["fetch_project_profiles: 503 Service Temporarily Unavailable"]

        def fetch_all(self, **_kwargs):
            return [], [], []

    monkeypatch.setattr(cli.ids, "resolve_project_id", lambda: "claude-smart")
    monkeypatch.setattr(cli, "Adapter", BrokenAdapter)

    code = cli.cmd_show(SimpleNamespace(project=None))

    out = capsys.readouterr().out
    assert code == 1
    assert "Could not read skills or preferences from Reflexio" in out
    assert "project `claude-smart`" in out
    assert "https://www.reflexio.ai/" in out
    assert "503 Service Temporarily Unavailable" in out
    assert "_No skills or preferences yet" not in out


def test_show_reports_empty_when_reflexio_read_succeeds(monkeypatch, capsys) -> None:
    class EmptyAdapter:
        url = "https://www.reflexio.ai/"
        read_errors: list[str] = []

        def fetch_all(self, **_kwargs):
            return [], [], []

    monkeypatch.setattr(cli.ids, "resolve_project_id", lambda: "claude-smart")
    monkeypatch.setattr(cli, "Adapter", EmptyAdapter)

    code = cli.cmd_show(SimpleNamespace(project=None))

    out = capsys.readouterr().out
    assert code == 0
    assert "_No skills or preferences yet for project `claude-smart`._" in out
