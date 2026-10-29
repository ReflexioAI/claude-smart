"""Codex install helpers exercised against a real fake ``codex`` executable.

The fake speaks the Codex app-server JSON-RPC protocol over stdio (and the
plain ``codex plugin ...`` / ``codex features ...`` subcommands), so these
tests cover the real subprocess, pipe and config-file boundaries instead of
replacing the helpers under test with stubs.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest  # type: ignore[reportMissingImports]
from claude_smart import cli

_PLUGIN = cli._CODEX_PLUGIN_ID

_FAKE_CODEX = r"""
import json, os, sys, time

mode = os.environ.get("FAKE_CODEX_MODE", "ok")
log = os.environ.get("FAKE_CODEX_LOG")


def record(entry):
    if log:
        with open(log, "a") as handle:
            handle.write(json.dumps(entry) + "\n")


def emit(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


args = sys.argv[1:]
record({"argv": args})
if args[:1] != ["app-server"]:
    code = int(os.environ.get("FAKE_CODEX_EXIT", "0"))
    sys.stdout.write(os.environ.get("FAKE_CODEX_STDOUT", ""))
    sys.stderr.write(os.environ.get("FAKE_CODEX_STDERR", ""))
    sys.exit(code)

if mode == "exit":
    sys.exit(3)

for line in sys.stdin:
    request = json.loads(line)
    record({"request": request})
    if mode == "hang":
        time.sleep(30)
    method = request.get("method")
    if method == "initialize":
        if mode == "burst":
            # Noise, a notification and the reply in ONE write: the shape the
            # select()+buffered-readline reader cannot see past (see xfail).
            sys.stdout.write(
                "starting codex app-server\n"
                + json.dumps({"method": "log"}) + "\n"
                + json.dumps({"id": request["id"], "result": {}}) + "\n"
            )
            sys.stdout.flush()
            continue
        if mode == "error":
            emit({"id": request["id"], "error": {"message": "unsupported client"}})
            continue
        emit({"id": request["id"], "result": {"ok": True}})
    elif method == "hooks/list":
        if mode == "nodata":
            emit({"id": request["id"], "result": {"data": []}})
        elif mode == "malformed":
            emit({"id": request["id"], "result": {"data": [{"hooks": "nope"}]}})
        else:
            emit({"id": request["id"], "result": {"data": [{"hooks": json.loads(
                os.environ["FAKE_CODEX_HOOKS"])}]}})
"""


@pytest.fixture
def fake_codex(tmp_path, monkeypatch):
    """Install a fake ``codex`` on PATH; returns a reader for its call log."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "codex"
    script.write_text(f"#!{sys.executable}\n{_FAKE_CODEX}")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "codex-calls.jsonl"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log))
    monkeypatch.setenv("FAKE_CODEX_HOOKS", "[]")

    def calls() -> list[dict]:
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]

    return calls


def _hooks_env(monkeypatch, hooks: list[object]) -> None:
    monkeypatch.setenv("FAKE_CODEX_HOOKS", json.dumps(hooks))


def test_list_codex_plugin_hooks_speaks_json_rpc_and_filters_to_claude_smart(
    fake_codex, monkeypatch, tmp_path
) -> None:
    _hooks_env(
        monkeypatch,
        [
            {"pluginId": _PLUGIN, "key": "legacy-key", "currentHash": "h1"},
            {"key": f"{_PLUGIN}:stop", "currentHash": "h2"},
            {"pluginId": "other@vendor", "key": "other@vendor:stop"},
            "not-a-hook",
        ],
    )

    ok, hooks, message = cli._list_codex_plugin_hooks(tmp_path)

    assert ok, message
    assert message == "found 2 claude-smart hooks"
    assert [hook["currentHash"] for hook in hooks] == ["h1", "h2"]
    requests = [entry["request"] for entry in fake_codex() if "request" in entry]
    assert [req.get("method") for req in requests] == [
        "initialize",
        "initialized",
        "hooks/list",
    ]
    assert requests[0]["id"] == 1
    assert requests[0]["params"]["capabilities"] == {"experimentalApi": True}
    assert requests[2] == {
        "id": 2,
        "method": "hooks/list",
        "params": {"cwds": [str(tmp_path)]},
    }


def test_trust_codex_plugin_hooks_writes_trusted_hashes_from_live_app_server(
    fake_codex, monkeypatch, tmp_path
) -> None:
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text('model = "gpt-5"\n')
    monkeypatch.setattr(cli, "_CODEX_CONFIG_PATH", config)
    _hooks_env(
        monkeypatch,
        [
            {"key": f"{_PLUGIN}:session-start", "currentHash": "sha-a"},
            {"key": f"{_PLUGIN}:stop", "currentHash": "sha-b"},
            # Matched by pluginId but its key cannot be trusted by name.
            {"pluginId": _PLUGIN, "key": "unscoped", "currentHash": "sha-c"},
        ],
    )

    ok, message = cli._trust_codex_plugin_hooks(tmp_path)

    assert ok, message
    assert message == "trusted and enabled 2 claude-smart Codex hooks"
    parsed = tomllib.loads(config.read_text())
    assert parsed["model"] == "gpt-5"
    assert parsed["hooks"]["state"] == {
        f"{_PLUGIN}:session-start": {"enabled": True, "trusted_hash": "sha-a"},
        f"{_PLUGIN}:stop": {"enabled": True, "trusted_hash": "sha-b"},
    }


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("error", "unsupported client"),
        ("nodata", "Codex app-server returned no hook metadata"),
        ("malformed", "Codex app-server hook metadata was malformed"),
    ],
)
def test_list_codex_plugin_hooks_reports_app_server_failures(
    fake_codex, monkeypatch, tmp_path, mode, expected
) -> None:
    monkeypatch.setenv("FAKE_CODEX_MODE", mode)

    ok, hooks, message = cli._list_codex_plugin_hooks(tmp_path)

    assert not ok
    assert hooks == []
    assert expected in message


@pytest.mark.xfail(
    strict=True,
    reason=(
        "known bug: _read_codex_app_server_response select()s the raw fd but "
        "readline()s a buffered TextIOWrapper, so a reply written in the same "
        "burst as earlier output stays in the buffer until the deadline"
    ),
)
def test_list_codex_plugin_hooks_sees_reply_written_with_other_output(
    fake_codex, monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("FAKE_CODEX_MODE", "burst")
    monkeypatch.setattr(cli, "_CODEX_CLI_TIMEOUT_SECONDS", 1)

    ok, _hooks, message = cli._list_codex_plugin_hooks(tmp_path)

    # Initialize must succeed; hooks/list then reports the empty fake list.
    assert message != "Codex app-server did not respond in time"
    assert not ok


@pytest.mark.xfail(
    strict=True,
    reason=(
        "known bug: on child exit EOF keeps select() ready and readline() "
        "returns '', so the loop never reaches proc.poll() and reports a "
        "timeout instead of the exit"
    ),
)
def test_list_codex_plugin_hooks_reports_early_exit_promptly(
    fake_codex, monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("FAKE_CODEX_MODE", "exit")
    monkeypatch.setattr(cli, "_CODEX_CLI_TIMEOUT_SECONDS", 1)

    ok, _hooks, message = cli._list_codex_plugin_hooks(tmp_path)

    assert not ok
    assert message == "Codex app-server exited before responding"


def test_list_codex_plugin_hooks_times_out_on_a_silent_app_server(
    fake_codex, monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("FAKE_CODEX_MODE", "hang")
    monkeypatch.setattr(cli, "_CODEX_CLI_TIMEOUT_SECONDS", 0.5)

    ok, hooks, message = cli._list_codex_plugin_hooks(tmp_path)

    assert (ok, hooks) == (False, [])
    assert message == "Codex app-server did not respond in time"


def test_list_codex_plugin_hooks_reports_missing_codex_binary(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))

    ok, hooks, message = cli._list_codex_plugin_hooks(tmp_path)

    assert (ok, hooks) == (False, [])
    assert message.startswith("could not start Codex app-server:")


def test_app_server_helpers_reject_missing_pipes() -> None:
    no_stdout = SimpleNamespace(stdout=None)
    no_stdin = SimpleNamespace(stdin=None)

    with pytest.raises(RuntimeError, match="stdout pipe is not available"):
        cli._read_codex_app_server_response(no_stdout, 1, 0.0)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="stdin pipe is not available"):
        cli._codex_app_server_request(no_stdin, 1, "x", {}, 0.0)  # type: ignore[arg-type]


def test_trust_codex_plugin_hooks_requires_hashes(
    fake_codex, monkeypatch, tmp_path
) -> None:
    config = tmp_path / "config.toml"
    monkeypatch.setattr(cli, "_CODEX_CONFIG_PATH", config)
    _hooks_env(monkeypatch, [{"key": f"{_PLUGIN}:stop", "currentHash": None}])

    ok, message = cli._trust_codex_plugin_hooks(tmp_path)

    assert not ok
    assert message == "Codex did not report trust hashes for claude-smart hooks"
    assert not config.exists()


def test_trust_codex_plugin_hooks_reports_unwritable_config(
    fake_codex, monkeypatch, tmp_path
) -> None:
    config = tmp_path / "config.toml"
    config.mkdir()  # a directory cannot be read or rewritten as TOML
    monkeypatch.setattr(cli, "_CODEX_CONFIG_PATH", config)
    _hooks_env(monkeypatch, [{"key": f"{_PLUGIN}:stop", "currentHash": "h"}])

    ok, message = cli._trust_codex_plugin_hooks(tmp_path)

    assert not ok
    assert message == f"could not write claude-smart hook trust state to {config}"


def test_enable_codex_plugin_hooks_uses_cli_when_it_succeeds(
    fake_codex, monkeypatch, tmp_path
) -> None:
    config = tmp_path / "config.toml"
    monkeypatch.setattr(cli, "_CODEX_CONFIG_PATH", config)

    ok, message = cli._enable_codex_plugin_hooks()

    assert ok
    assert message == "codex features enable hooks; codex features enable plugin_hooks"
    assert [entry["argv"] for entry in fake_codex()] == [
        ["features", "enable", "hooks"],
        ["features", "enable", "plugin_hooks"],
    ]
    assert not config.exists()


def test_enable_codex_plugin_hooks_falls_back_to_config_toml(
    fake_codex, monkeypatch, tmp_path
) -> None:
    config = tmp_path / "config.toml"
    config.write_text('model = "gpt-5"\n')
    monkeypatch.setattr(cli, "_CODEX_CONFIG_PATH", config)
    monkeypatch.setenv("FAKE_CODEX_EXIT", "2")

    ok, message = cli._enable_codex_plugin_hooks()

    assert ok
    assert message == (
        f"set hooks = true in {config}; set plugin_hooks = true in {config}"
    )
    parsed = tomllib.loads(config.read_text())
    assert parsed == {
        "model": "gpt-5",
        "features": {"hooks": True, "plugin_hooks": True},
    }


def test_enable_codex_plugin_hooks_reports_cli_error_when_config_unwritable(
    fake_codex, monkeypatch, tmp_path
) -> None:
    config = tmp_path / "config.toml"
    config.mkdir()
    monkeypatch.setattr(cli, "_CODEX_CONFIG_PATH", config)
    monkeypatch.setenv("FAKE_CODEX_EXIT", "2")
    monkeypatch.setenv("FAKE_CODEX_STDERR", "unknown feature\n")

    ok, message = cli._enable_codex_plugin_hooks()

    assert not ok
    assert message == "hooks: unknown feature"


def test_register_codex_marketplace_without_cache_refresh(
    fake_codex, monkeypatch, tmp_path
) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str]):
        calls.append(args)
        return argparse.Namespace(
            returncode=0 if args[2] == "add" else 1, stdout="", stderr=""
        )

    monkeypatch.setattr(cli, "_run_codex", fake_run)

    ok, message = cli._register_codex_marketplace(tmp_path)

    assert ok
    assert message == "registered Codex marketplace"
    assert calls[-1] == ["plugin", "marketplace", "upgrade", "reflexioai"]


def test_register_codex_marketplace_surfaces_cli_output_on_failure(
    fake_codex, monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("FAKE_CODEX_EXIT", "1")
    monkeypatch.setenv(
        "FAKE_CODEX_STDOUT", "marketplace exists from a different source"
    )

    ok, message = cli._register_codex_marketplace(tmp_path)

    # Removal also fails (same fake exit), so the original error is reported.
    assert not ok
    assert message == "marketplace exists from a different source"
    assert [entry["argv"][:3] for entry in fake_codex()] == [
        ["plugin", "marketplace", "add"],
        ["plugin", "marketplace", "remove"],
    ]


def test_register_codex_marketplace_explains_silent_failure(
    fake_codex, monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("FAKE_CODEX_EXIT", "1")

    ok, message = cli._register_codex_marketplace(tmp_path)

    assert not ok
    assert message == "Codex CLI does not expose plugin marketplace commands"


# --- config.toml writers -----------------------------------------------------


def test_set_toml_feature_creates_features_table_in_new_file(tmp_path) -> None:
    config = tmp_path / "nested" / "config.toml"

    assert cli._set_toml_feature(config, "hooks", True)

    assert config.read_text() == "[features]\nhooks = true\n"


def test_set_toml_feature_appends_table_after_existing_content(tmp_path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[profile]\nname = "x"')

    assert cli._set_toml_feature(config, "plugin_hooks", False)

    assert config.read_text() == (
        '[profile]\nname = "x"\n\n[features]\nplugin_hooks = false\n'
    )


def test_set_toml_feature_appends_to_features_table_at_end_of_file(tmp_path) -> None:
    config = tmp_path / "config.toml"
    config.write_text("[features]\nother = true\n")

    assert cli._set_toml_feature(config, "hooks", True)

    assert tomllib.loads(config.read_text()) == {
        "features": {"other": True, "hooks": True}
    }


def test_set_toml_feature_leaves_already_correct_file_untouched(tmp_path) -> None:
    config = tmp_path / "config.toml"
    original = "[features]\nhooks = true\n\n[other]\nhooks = false\n"
    config.write_text(original)
    os.utime(config, (1, 1))

    assert cli._set_toml_feature(config, "hooks", True)

    assert config.read_text() == original
    # An identical value is not rewritten, so mtime is preserved.
    assert config.stat().st_mtime == 1


def test_set_toml_feature_reports_read_and_write_failures(tmp_path) -> None:
    unreadable = tmp_path / "dir.toml"
    unreadable.mkdir()
    blocker = tmp_path / "blocker"
    blocker.write_text("")

    assert not cli._set_toml_feature(unreadable, "hooks", True)
    assert not cli._set_toml_feature(blocker / "config.toml", "hooks", True)


def test_remove_toml_sections_reports_read_and_write_failures(tmp_path) -> None:
    unreadable = tmp_path / "dir.toml"
    unreadable.mkdir()
    readonly = tmp_path / "readonly.toml"
    readonly.write_text("[marketplaces.reflexioai]\nsource = 'x'\n")
    readonly.chmod(0o444)
    missing = tmp_path / "missing.toml"

    try:
        assert not cli._remove_toml_sections(unreadable, exact={"x"})
        assert cli._remove_toml_sections(missing, exact={"x"})
        assert not missing.exists()
        if os.access(readonly, os.W_OK):  # running as root ignores file modes
            pytest.skip("file permissions are not enforced for this user")
        assert not cli._remove_toml_sections(
            readonly, exact={"marketplaces.reflexioai"}
        )
    finally:
        readonly.chmod(0o644)


def test_set_codex_plugin_enabled_replaces_existing_section(tmp_path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(f'model = "gpt-5"\n[plugins."{_PLUGIN}"]\nenabled = false')

    assert cli._set_codex_plugin_enabled(config)

    text = config.read_text()
    assert text.count(f'[plugins."{_PLUGIN}"]') == 1
    assert tomllib.loads(text) == {
        "model": "gpt-5",
        "plugins": {_PLUGIN: {"enabled": True}},
    }


def test_set_codex_plugin_enabled_reports_unusable_config(tmp_path) -> None:
    directory = tmp_path / "config.toml"
    directory.mkdir()
    blocker = tmp_path / "blocker"
    blocker.write_text("")

    assert not cli._set_codex_plugin_enabled(directory)
    assert not cli._set_codex_plugin_enabled(blocker / "config.toml")


def test_set_codex_hook_states_rejects_empty_and_escapes_keys(tmp_path) -> None:
    config = tmp_path / "config.toml"
    config.write_text("[hooks.state]\n")
    key = f'{_PLUGIN}:odd"name\\x'

    assert not cli._set_codex_hook_states(config, {})
    assert cli._set_codex_hook_states(config, {key: "abc"})

    parsed = tomllib.loads(config.read_text())
    assert parsed["hooks"]["state"] == {key: {"enabled": True, "trusted_hash": "abc"}}
    assert config.read_text().count("[hooks.state]") == 1


def test_set_codex_hook_states_reports_unusable_config(tmp_path) -> None:
    directory = tmp_path / "config.toml"
    directory.mkdir()
    blocker = tmp_path / "blocker"
    blocker.write_text("")

    assert not cli._set_codex_hook_states(directory, {f"{_PLUGIN}:stop": "h"})
    assert not cli._set_codex_hook_states(
        blocker / "config.toml", {f"{_PLUGIN}:stop": "h"}
    )


# --- plugin cache ------------------------------------------------------------


def _codex_plugin(root: Path, version: object) -> Path:
    manifest = root / ".codex-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"version": version}))
    return root


def test_codex_plugin_version_requires_a_nonempty_string(tmp_path) -> None:
    good = _codex_plugin(tmp_path / "good", "1.2.3")
    numeric = _codex_plugin(tmp_path / "numeric", 3)
    broken = tmp_path / "broken"
    (broken / ".codex-plugin").mkdir(parents=True)
    (broken / ".codex-plugin" / "plugin.json").write_text("{not json")

    assert cli._codex_plugin_version(good) == "1.2.3"
    assert cli._codex_plugin_version(numeric) is None
    assert cli._codex_plugin_version(broken) is None
    assert cli._codex_plugin_version(tmp_path / "absent") is None


def test_install_codex_plugin_cache_requires_manifest_version(
    monkeypatch, tmp_path
) -> None:
    cache = tmp_path / "cache"
    monkeypatch.setattr(cli, "_CODEX_PLUGIN_CACHE_DIR", cache)
    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()

    ok, message = cli._install_codex_plugin_cache(plugin_root)

    assert not ok
    assert message == (
        f"missing version in {plugin_root / '.codex-plugin' / 'plugin.json'}"
    )
    assert not cache.exists()


def test_install_codex_plugin_cache_reports_copy_and_enable_failures(
    monkeypatch, tmp_path
) -> None:
    plugin_root = _codex_plugin(tmp_path / "plugin", "0.4.0")
    blocker = tmp_path / "blocker"
    blocker.write_text("")
    monkeypatch.setattr(cli, "_CODEX_PLUGIN_CACHE_DIR", blocker / "cache")

    ok, message = cli._install_codex_plugin_cache(plugin_root)
    assert not ok
    assert message.startswith("could not write Codex plugin cache:")

    monkeypatch.setattr(cli, "_CODEX_PLUGIN_CACHE_DIR", tmp_path / "cache")
    config = tmp_path / "config.toml"
    config.mkdir()
    monkeypatch.setattr(cli, "_CODEX_CONFIG_PATH", config)

    ok, message = cli._install_codex_plugin_cache(plugin_root)
    assert not ok
    assert message == f"could not enable {_PLUGIN} in {config}"
    # The cache copy itself landed before the config write failed.
    assert (tmp_path / "cache" / "0.4.0" / ".codex-plugin" / "plugin.json").is_file()


def _cached_version(cache: Path, version: str, *, complete: bool = True) -> Path:
    root = cache / version
    (root / "scripts").mkdir(parents=True)
    (root / "pyproject.toml").write_text("")
    if complete:
        (root / "scripts" / "smart-install.sh").write_text("")
    return root


def test_find_codex_plugin_root_prefers_highest_complete_semver(
    monkeypatch, tmp_path
) -> None:
    cache = tmp_path / "cache"
    monkeypatch.setattr(cli, "_CODEX_PLUGIN_CACHE_DIR", cache)
    assert cli._find_codex_plugin_root() is None

    _cached_version(cache, "0.2.9")
    newest = _cached_version(cache, "0.2.10")
    _cached_version(cache, "0.3.0", complete=False)
    _cached_version(cache, "not-a-version")
    (cache / "stray-file").write_text("")

    assert cli._find_codex_plugin_root() == newest


def test_update_codex_stops_services_from_installed_cache_then_reinstalls(
    monkeypatch, tmp_path
) -> None:
    cache = tmp_path / "cache"
    installed = _cached_version(cache, "1.0.0")
    monkeypatch.setattr(cli, "_CODEX_PLUGIN_CACHE_DIR", cache)
    stopped: list[tuple[Path, str]] = []
    installs: list[argparse.Namespace] = []
    monkeypatch.setattr(
        cli, "_run_service", lambda script, sub: stopped.append((script, sub)) or 0
    )
    monkeypatch.setattr(
        cli, "cmd_install_codex", lambda args: installs.append(args) or 7
    )

    assert cli.cmd_update_codex(argparse.Namespace(host="claude-code")) == 7

    assert stopped == [
        (installed / "scripts" / "dashboard-service.sh", "stop"),
        (installed / "scripts" / "backend-service.sh", "stop"),
    ]
    assert installs[0].host == cli._HOST_CODEX


def test_update_codex_without_cache_stops_bundled_services(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(cli, "_CODEX_PLUGIN_CACHE_DIR", tmp_path / "missing")
    stopped: list[Path] = []
    monkeypatch.setattr(
        cli, "_run_service", lambda script, _sub: stopped.append(script) or 0
    )
    monkeypatch.setattr(cli, "cmd_install_codex", lambda _args: 0)

    assert cli.cmd_update_codex(argparse.Namespace()) == 0
    assert stopped == [cli._DASHBOARD_SCRIPT, cli._BACKEND_SCRIPT]


def _isolate_codex_state(monkeypatch, tmp_path: Path) -> Path:
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        f'model = "gpt-5"\n\n[plugins."{_PLUGIN}"]\nenabled = true\n\n'
        "[marketplaces.reflexioai]\nsource = '/x'\n"
    )
    monkeypatch.setattr(cli, "_CODEX_CONFIG_PATH", config)
    monkeypatch.setattr(cli, "_CODEX_LOCAL_MARKETPLACE_ROOT", tmp_path / "market")
    monkeypatch.setattr(
        cli, "_CODEX_PLUGIN_CACHE_DIR", tmp_path / "plugins-cache" / "claude-smart"
    )
    monkeypatch.setattr(cli, "_REFLEXIO_DIR", tmp_path / ".reflexio")
    monkeypatch.setattr(cli, "_STATE_DIR", tmp_path / ".claude-smart")
    monkeypatch.setattr(
        cli, "_CLAUDE_SMART_ENV_PATH", tmp_path / ".claude-smart" / ".env"
    )
    (tmp_path / "market").mkdir()
    _cached_version(tmp_path / "plugins-cache" / "claude-smart", "1.0.0")
    return config


def test_uninstall_codex_without_cli_still_cleans_local_state(
    monkeypatch, tmp_path, capsys
) -> None:
    config = _isolate_codex_state(monkeypatch, tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))

    assert cli.cmd_uninstall_codex(argparse.Namespace()) == 0

    assert (
        "Codex CLI not found; skipping marketplace removal." in capsys.readouterr().out
    )
    assert tomllib.loads(config.read_text()) == {"model": "gpt-5"}
    assert not (tmp_path / "market").exists()
    # The emptied parent cache directory is removed as well.
    assert not (tmp_path / "plugins-cache").exists()


def test_uninstall_codex_warns_about_cli_and_config_failures(
    fake_codex, monkeypatch, tmp_path, capsys
) -> None:
    _isolate_codex_state(monkeypatch, tmp_path)
    unwritable = tmp_path / "config-dir"
    unwritable.mkdir()
    monkeypatch.setattr(cli, "_CODEX_CONFIG_PATH", unwritable)
    monkeypatch.setenv("FAKE_CODEX_EXIT", "1")
    monkeypatch.setenv("FAKE_CODEX_STDERR", "no such marketplace\n")

    assert cli.cmd_uninstall_codex(argparse.Namespace()) == 0

    captured = capsys.readouterr()
    assert "warning: Codex marketplace removal failed: no such marketplace" in (
        captured.err
    )
    assert f"warning: could not update {unwritable}" in captured.err
    assert "claude-smart Codex plugin and marketplace state removed." in captured.out
    assert not (tmp_path / "market").exists()
