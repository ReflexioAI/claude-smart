"""Tests for ``claude_smart.cli.cmd_clear_user``.

These tests exercise the per-user-scoped clear command in isolation
from any real reflexio server. The companion ``ReflexioClient.clear_user_data``
method ships in a separate reflexio PR, so we inject a fake ``reflexio``
module into ``sys.modules`` to keep the CLI's late import resolvable
while remaining fully under the test's control.
"""

from __future__ import annotations

import argparse
import sys
import types
from typing import Any
from unittest.mock import MagicMock

import pytest

from claude_smart import cli


def _args(*, user_id: str = "alice", yes: bool = True) -> argparse.Namespace:
    """Build a Namespace shaped like ``_build_parser()`` produces for ``clear-user``.

    Args:
        user_id (str): Positional ``user_id`` value.
        yes (bool): Value of the ``--yes`` flag.

    Returns:
        argparse.Namespace: Namespace ready to feed into ``cmd_clear_user``.
    """
    return argparse.Namespace(user_id=user_id, yes=yes)


def _install_fake_reflexio(monkeypatch: pytest.MonkeyPatch, client_factory: Any) -> Any:
    """Insert a synthetic ``reflexio`` module exposing ``ReflexioClient``.

    Mirrors the inline ``from reflexio import ReflexioClient`` import that
    ``cmd_clear_user`` performs. The factory is invoked with the same
    ``url_endpoint=...`` kwarg the CLI passes, so tests can intercept and
    assert on construction arguments if desired.

    Args:
        monkeypatch (pytest.MonkeyPatch): Active monkeypatch fixture.
        client_factory (Any): Callable returning the per-construction
            fake client (typically a ``MagicMock``).

    Returns:
        Any: The factory callable (returned for chaining/assertion).
    """
    module = types.ModuleType("reflexio")
    module.ReflexioClient = client_factory  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "reflexio", module)
    return client_factory


def test_clear_user_invokes_client_method_with_user_id(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``cmd_clear_user`` constructs ``ReflexioClient`` and calls ``clear_user_data``."""
    fake_client = MagicMock()
    fake_client.clear_user_data.return_value = {"deleted_counts": {}}
    factory = MagicMock(return_value=fake_client)
    _install_fake_reflexio(monkeypatch, factory)
    monkeypatch.setenv("REFLEXIO_URL", "http://localhost:8071/")

    rc = cli.cmd_clear_user(_args(user_id="bob", yes=True))

    assert rc == 0
    factory.assert_called_once_with(
        url_endpoint="http://localhost:8071/",
        api_key="",
    )
    fake_client.clear_user_data.assert_called_once_with("bob")
    assert "Cleared user 'bob'" in capsys.readouterr().out


def test_clear_user_requires_yes_flag(capsys: pytest.CaptureFixture[str]) -> None:
    """Omitting ``--yes`` must short-circuit before any client construction.

    Two surfaces are checked:
      1. argparse-level: invoking the full parser without ``--yes`` is
         allowed (``--yes`` is a flag, not required by argparse), so the
         flag is enforced by ``cmd_clear_user`` itself with a clear
         confirmation message and a non-zero exit.
      2. argparse-level: invoking with no ``user_id`` at all *is* an
         argparse error and exits with the expected SystemExit.
    """
    parser = cli._build_parser()

    # No user_id -> argparse error.
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["clear-user"])
    assert excinfo.value.code == 2
    assert "user_id" in capsys.readouterr().err

    # user_id present, --yes absent -> CLI safety guard fires.
    args = parser.parse_args(["clear-user", "alice"])
    assert args.yes is False
    rc = cli.cmd_clear_user(args)
    assert rc == 1
    captured = capsys.readouterr()
    assert "Re-run with --yes" in captured.out


def test_clear_user_propagates_backend_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failing ``clear_user_data`` call exits non-zero with the error on stderr."""
    fake_client = MagicMock()
    fake_client.clear_user_data.side_effect = RuntimeError("boom: reflexio unreachable")
    factory = MagicMock(return_value=fake_client)
    _install_fake_reflexio(monkeypatch, factory)

    rc = cli.cmd_clear_user(_args(user_id="carol", yes=True))

    assert rc == 1
    captured = capsys.readouterr()
    assert "boom: reflexio unreachable" in captured.err
    assert captured.out == ""


def test_clear_user_prints_deleted_counts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The success line surfaces the per-table breakdown from ``deleted_counts``."""
    fake_client = MagicMock()
    fake_client.clear_user_data.return_value = {
        "deleted_counts": {"interactions": 3, "profiles": 2, "user_playbooks": 5},
    }
    factory = MagicMock(return_value=fake_client)
    _install_fake_reflexio(monkeypatch, factory)

    rc = cli.cmd_clear_user(_args(user_id="dave", yes=True))

    assert rc == 0
    out = capsys.readouterr().out
    assert "Cleared user 'dave'" in out
    assert "10 row(s)" in out
    assert "interactions=3" in out
    assert "profiles=2" in out
    assert "user_playbooks=5" in out
