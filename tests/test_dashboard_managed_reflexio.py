"""Static checks for dashboard managed Reflexio support."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_env_bootstrap import owner_python as owner_python

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_dashboard_config_knows_reflexio_api_key() -> None:
    config = (REPO_ROOT / "plugin" / "dashboard" / "lib" / "config-file.ts").read_text()
    types = (REPO_ROOT / "plugin" / "dashboard" / "lib" / "types.ts").read_text()
    page = (
        REPO_ROOT / "plugin" / "dashboard" / "app" / "configure" / "env" / "page.tsx"
    ).read_text()

    assert '"REFLEXIO_API_KEY"' in config
    assert "function defaultReflexioUrl()" in config
    assert 'process.env.BACKEND_PORT || "8071"' in config
    assert "REFLEXIO_API_KEY: string;" in types
    assert "REFLEXIO_API_KEY_SET?: boolean;" in types
    assert "<Label>REFLEXIO_API_KEY</Label>" in page
    assert 'type="password"' in page
    assert "apiKeyDirty" in page
    assert "delete envUpdate.REFLEXIO_API_KEY" in page
    assert "leave blank to keep existing key" in page


def test_dashboard_config_can_toggle_read_only_mode() -> None:
    config = (REPO_ROOT / "plugin" / "dashboard" / "lib" / "config-file.ts").read_text()
    types = (REPO_ROOT / "plugin" / "dashboard" / "lib" / "types.ts").read_text()
    page = (
        REPO_ROOT / "plugin" / "dashboard" / "app" / "configure" / "env" / "page.tsx"
    ).read_text()

    assert '"CLAUDE_SMART_READ_ONLY"' in config
    assert "CLAUDE_SMART_READ_ONLY: boolean;" in types
    assert '<Label htmlFor="read-only-mode">CLAUDE_SMART_READ_ONLY</Label>' in page
    assert "checked={!!config.CLAUDE_SMART_READ_ONLY}" in page
    assert 'update("CLAUDE_SMART_READ_ONLY", v)' in page


def test_dashboard_config_endpoint_masks_reflexio_api_key() -> None:
    route = (
        REPO_ROOT / "plugin" / "dashboard" / "app" / "api" / "config" / "route.ts"
    ).read_text()

    assert "function publicConfig" in route
    assert 'REFLEXIO_API_KEY: ""' in route
    assert "REFLEXIO_API_KEY_SET: Boolean(config.REFLEXIO_API_KEY)" in route
    assert "return NextResponse.json(publicConfig(config))" in route


def test_dashboard_proxy_forwards_bearer_auth_without_client_auth() -> None:
    route = (
        REPO_ROOT
        / "plugin"
        / "dashboard"
        / "app"
        / "api"
        / "reflexio"
        / "[...path]"
        / "route.ts"
    ).read_text()

    assert 'headers.delete("authorization")' in route
    assert "selectedConnection(req)" in route
    assert 'headers.delete("cookie")' in route
    assert 'headers.set("user-agent", "claude-smart")' in route
    assert 'headers.set("authorization", `Bearer ${apiKey}`)' in route
    assert "fromHeader" not in route
    assert "x-reflexio-url" not in route


def test_dashboard_settings_read_configured_reflexio_url_only() -> None:
    settings = (
        REPO_ROOT / "plugin" / "dashboard" / "hooks" / "use-settings.tsx"
    ).read_text()
    page = (
        REPO_ROOT / "plugin" / "dashboard" / "app" / "configure" / "env" / "page.tsx"
    ).read_text()

    assert 'fetch("/api/data-sources",' in settings
    assert "data.defaultSource" in settings
    assert "selectDataSource(selected, data[selected].id)" in settings
    assert "localStorage" not in settings
    assert "setReflexioUrl" not in settings
    assert "claude-smart-dashboard-settings" not in settings
    assert 'SETTINGS_CHANGED_EVENT = "claude-smart-settings-changed"' in settings
    assert "window.dispatchEvent(new Event(SETTINGS_CHANGED_EVENT))" in page
    assert "Stored in browser localStorage" not in page
    assert "Reflexio endpoint (dashboard)" not in page


@pytest.fixture(autouse=True)
def _prepared_dashboard_owner(tmp_path, owner_python):
    # Match the service's actual cwd/venv relationship with an owned installed
    # publisher, rather than falling back to ambient PATH/PYTHONPATH.
    (tmp_path / "dashboard").mkdir(exist_ok=True)
    (tmp_path / ".venv").symlink_to(
        owner_python.parent.parent, target_is_directory=True
    )


def _run_config_module(
    tmp_path: Path,
    script: str,
    env_extra: dict[str, str],
    module_name: str = "config-file",
) -> str:
    """Run a dashboard library module with Node type stripping and HOME=tmp_path."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required")
    probe = subprocess.run(  # noqa: S603 — repository-controlled test command.
        [node, "-p", "process.features.typescript"], capture_output=True, text=True
    )
    if probe.stdout.strip() in {"", "false", "undefined"}:
        pytest.skip("this node cannot strip TypeScript types")
    env = {k: v for k, v in os.environ.items() if not k.startswith("REFLEXIO_")}
    env["HOME"] = str(tmp_path)
    env.update(env_extra)
    module = REPO_ROOT / "plugin" / "dashboard" / "lib" / f"{module_name}.ts"
    result = subprocess.run(  # noqa: S603 — repository-controlled test command.
        [
            node,
            "--no-warnings",
            "--input-type=module",
            "-e",
            f"const m = await import({json.dumps(str(module))});\n{script}",
        ],
        env=env,
        cwd=tmp_path / "dashboard",
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_dashboard_proxy_prefers_the_saved_file_over_its_launch_env(
    tmp_path: Path,
) -> None:
    # dashboard-service.sh exports the file before launching Next, so the
    # inherited env goes stale after a Configure save. File wins, like hooks.
    env_file = tmp_path / ".claude-smart" / ".env"
    env_file.parent.mkdir()
    env_file.write_text('REFLEXIO_URL="https://b.example/"\nREFLEXIO_API_KEY="kb"\n')
    out = _run_config_module(
        tmp_path,
        "process.stdout.write(JSON.stringify(await m.managedReflexioSettings()));",
        {"REFLEXIO_URL": "https://a.example/", "REFLEXIO_API_KEY": "ka"},
    )
    assert json.loads(out) == {"url": "https://b.example/", "apiKey": "kb"}

    # `export KEY=value` is valid in the file (the shell and Python loaders
    # strip the prefix); it must not fall through to the launch env.
    env_file.write_text(
        'export REFLEXIO_URL="https://c.example/"\nexport REFLEXIO_API_KEY="kc"\n'
    )
    out = _run_config_module(
        tmp_path,
        "process.stdout.write(JSON.stringify(await m.managedReflexioSettings()));",
        {"REFLEXIO_URL": "https://a.example/", "REFLEXIO_API_KEY": "ka"},
    )
    assert json.loads(out) == {"url": "https://c.example/", "apiKey": "kc"}

    env_file.write_text("CLAUDE_SMART_HOST=claude-code\n")
    out = _run_config_module(
        tmp_path,
        "process.stdout.write(JSON.stringify(await m.managedReflexioSettings()));",
        {"REFLEXIO_URL": "https://a.example/", "REFLEXIO_API_KEY": "ka"},
    )
    assert json.loads(out) == {"url": "https://a.example/", "apiKey": "ka"}


def test_dashboard_unrelated_save_does_not_materialize_a_timeout(tmp_path: Path) -> None:
    out = _run_config_module(
        tmp_path,
        "const saved = await m.readConfig(); "
        "await m.writeConfig({...saved, CLAUDE_SMART_READ_ONLY:true}); "
        "process.stdout.write(JSON.stringify(await m.readConfig()));",
        {},
    )
    assert "CLAUDE_SMART_CLI_TIMEOUT" not in json.loads(out)
    text = (tmp_path / ".claude-smart/.env").read_text()
    assert "CLAUDE_SMART_READ_ONLY" in text and "CLAUDE_SMART_CLI_TIMEOUT" not in text


def test_dashboard_timeout_save_and_clear_preserve_presence(tmp_path: Path) -> None:
    out = _run_config_module(
        tmp_path,
        "await m.writeConfig({CLAUDE_SMART_CLI_TIMEOUT:'75'}); "
        "const saved = await m.readConfig(); "
        "await m.writeConfig({...saved, CLAUDE_SMART_READ_ONLY:true}); "
        "const kept = await m.readConfig(); "
        "await m.writeConfig({CLAUDE_SMART_CLI_TIMEOUT:''}); "
        "const cleared = await m.readConfig(); "
        "process.stdout.write(JSON.stringify({kept, cleared}));",
        {},
    )
    states = json.loads(out)
    assert states["kept"]["CLAUDE_SMART_CLI_TIMEOUT"] == "75"
    assert "CLAUDE_SMART_CLI_TIMEOUT" not in states["cleared"]
    assert "CLAUDE_SMART_CLI_TIMEOUT" not in (tmp_path / ".claude-smart/.env").read_text()


def test_dashboard_save_does_not_turn_off_absent_local_providers(
    tmp_path: Path,
) -> None:
    # A managed file has no local-provider flags. Saving the Configure page
    # must not write them as 0, or a later switch to local mode keeps them off.
    env_file = tmp_path / ".claude-smart" / ".env"
    env_file.parent.mkdir()
    env_file.write_text(
        'REFLEXIO_URL="https://www.reflexio.ai/"\nREFLEXIO_API_KEY="k"\n'
    )
    _run_config_module(tmp_path, "await m.writeConfig(await m.readConfig());", {})
    text = env_file.read_text()
    assert "CLAUDE_SMART_USE_LOCAL_CLI=0" not in text
    assert "CLAUDE_SMART_USE_LOCAL_EMBEDDING" not in text


def test_dashboard_proxy_follows_the_backend_port_for_default_spellings(
    tmp_path: Path,
) -> None:
    # The hooks rewrite the default 8071 spellings to BACKEND_PORT; the
    # dashboard must call the same port, but keep genuinely custom URLs.
    env_file = tmp_path / ".claude-smart" / ".env"
    env_file.parent.mkdir()
    script = "process.stdout.write(JSON.stringify(await m.managedReflexioSettings()));"
    env_file.write_text('REFLEXIO_URL="http://localhost:8071"\n')
    out = _run_config_module(tmp_path, script, {"BACKEND_PORT": "9123"})
    assert json.loads(out)["url"] == "http://localhost:9123/"
    env_file.write_text('REFLEXIO_URL="http://localhost:7000/"\n')
    out = _run_config_module(tmp_path, script, {"BACKEND_PORT": "9123"})
    assert json.loads(out)["url"] == "http://localhost:7000/"


def test_dashboard_clearing_the_key_drops_a_remote_url(tmp_path: Path) -> None:
    # An empty key is local mode; a remote URL left behind would keep hooks
    # and services remote with no credentials.
    env_file = tmp_path / ".claude-smart" / ".env"
    env_file.parent.mkdir()
    env_file.write_text(
        'REFLEXIO_URL="https://www.reflexio.ai/"\nREFLEXIO_API_KEY="k"\n'
    )
    _run_config_module(
        tmp_path,
        'await m.writeConfig({ REFLEXIO_URL: "https://www.reflexio.ai/", '
        'REFLEXIO_API_KEY: "" });',
        {},
    )
    text = env_file.read_text()
    assert "REFLEXIO_URL" not in text
    # A local URL is kept.
    env_file.write_text('REFLEXIO_URL="http://localhost:8071/"\nREFLEXIO_API_KEY="k"\n')
    _run_config_module(tmp_path, 'await m.writeConfig({ REFLEXIO_API_KEY: "" });', {})
    assert "REFLEXIO_URL=" in env_file.read_text()


@pytest.mark.parametrize(
    "invalid_array", [None, "not-an-array", {"request_id": "ignored"}]
)
def test_dashboard_session_reader_retains_all_chunk_ids_and_legacy_lineage(
    tmp_path, invalid_array
):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    records = [
        {"role": "User", "content": "question", "host": "codex"},
        {
            "published_up_to": 1,
            "request_id": "chunk-2",
            "request_ids": ["chunk-1", "chunk-2", "chunk-1", 3, None, "", "  ", {}, []],
        },
        {"published_up_to": 1, "request_id": "legacy-id"},
        {"published_up_to": 1, "request_ids": invalid_array},
        {"published_up_to": 1, "request_id": "  "},
    ]
    (sessions / "session.jsonl").write_text(
        "\n".join(json.dumps(record) for record in records) + "\n"
    )
    out = _run_config_module(
        tmp_path,
        "process.stdout.write(JSON.stringify(await m.listSessions()));",
        {"CLAUDE_SMART_STATE_DIR": str(sessions)},
        module_name="session-reader",
    )
    summaries = json.loads(out)
    assert len(summaries) == 1
    assert summaries[0]["host"] == "codex"
    assert summaries[0]["request_ids"] == ["chunk-1", "chunk-2", "legacy-id"]


def test_changing_runtime_mode_keeps_connections_but_never_reuses_cloud_key_locally(
    tmp_path: Path,
) -> None:
    config = tmp_path / ".claude-smart" / ".env"
    config.parent.mkdir()
    config.write_text(
        'REFLEXIO_URL="https://managed.example/"\nREFLEXIO_API_KEY="cloud-secret"\n'
    )
    _run_config_module(
        tmp_path,
        'await m.writeConfig({REFLEXIO_URL:"http://localhost:9090/"}); process.stdout.write(JSON.stringify(await m.managedReflexioSettings()));',
        {},
    )
    text = config.read_text()
    assert "CLAUDE_SMART_MANAGED_API_KEY=cloud-secret" in text
    assert "CLAUDE_SMART_LOCAL_API_KEY=cloud-secret" not in text
    assert json.loads(
        _run_config_module(
            tmp_path,
            "process.stdout.write(JSON.stringify(await m.managedReflexioSettings()));",
            {},
        )
    ) == {"url": "http://localhost:9090/", "apiKey": ""}


def test_explicitly_clearing_managed_key_also_forgets_saved_dashboard_key(
    tmp_path: Path,
) -> None:
    config = tmp_path / ".claude-smart" / ".env"
    config.parent.mkdir()
    config.write_text(
        'REFLEXIO_URL="https://managed.example/"\nREFLEXIO_API_KEY="cloud-secret"\nCLAUDE_SMART_MANAGED_URL="https://managed.example/"\nCLAUDE_SMART_MANAGED_API_KEY="cloud-secret"\n'
    )
    _run_config_module(
        tmp_path,
        'await m.writeConfig({REFLEXIO_API_KEY:""}); process.stdout.write("true");',
        {},
    )
    assert "cloud-secret" not in config.read_text()


@pytest.mark.parametrize("existing", [False, True])
def test_dashboard_does_not_advertise_or_write_retired_embedding_setting(
    tmp_path, existing
):
    env_file = tmp_path / ".claude-smart" / ".env"
    env_file.parent.mkdir()
    original = "# Preserve unrelated SDK policy\nREFLEXIO_EMBEDDING_PROVIDER=off\n"
    if existing:
        original += "CLAUDE_SMART_USE_LOCAL_EMBEDDING=0\n"
    env_file.write_text(original)
    output = _run_config_module(
        tmp_path,
        "const before=await m.readConfig(); "
        "await m.writeConfig({CLAUDE_SMART_USE_LOCAL_EMBEDDING:true, CLAUDE_SMART_READ_ONLY:true}); "
        "process.stdout.write(JSON.stringify({before, keys:m.KNOWN_KEYS}));",
        {},
    )
    result = json.loads(output)
    assert "CLAUDE_SMART_USE_LOCAL_EMBEDDING" not in result["before"]
    assert "CLAUDE_SMART_USE_LOCAL_EMBEDDING" not in result["keys"]
    saved = env_file.read_text()
    assert original.strip() in saved
    assert "CLAUDE_SMART_READ_ONLY=1" in saved
    assert saved.count("CLAUDE_SMART_USE_LOCAL_EMBEDDING") == int(existing)


def test_dashboard_explicit_empty_key_overrides_stale_launch_credentials(
    tmp_path: Path,
) -> None:
    state = tmp_path / ".claude-smart"
    state.mkdir()
    (state / ".env").write_text("REFLEXIO_URL=http://localhost:8071/\n")
    output = _run_config_module(
        tmp_path,
        'await m.writeConfig({REFLEXIO_API_KEY:""}); process.stdout.write(JSON.stringify(await m.managedReflexioSettings()));',
        {"REFLEXIO_API_KEY": "stale-owned-key"},
    )
    assert json.loads(output)["apiKey"] == ""
    assert "REFLEXIO_API_KEY=\n" in (state / ".env").read_text()


def test_dashboard_rejects_concurrent_endpoint_credentials_after_snapshot(
    tmp_path: Path,
) -> None:
    state = tmp_path / ".claude-smart"
    state.mkdir()
    selected = state / ".env"
    selected.write_text(
        "REFLEXIO_URL=http://localhost:8071/\nREFLEXIO_API_KEY=old-owned-key\n"
    )
    script = r"""
      const fs = (await import('node:fs/promises')).default;
      const originalRead = fs.readFile.bind(fs);
      let raced = false;
      fs.readFile = async (...args) => {
        const result = await originalRead(...args);
        if (!raced && String(args[0]).endsWith('/.claude-smart/.env')) {
          raced = true;
          await fs.writeFile(args[0], 'REFLEXIO_URL=http://localhost:8071/\nREFLEXIO_API_KEY=new-owned-key\n');
        }
        return result;
      };
      let error = '';
      try { await m.writeConfig({CLAUDE_SMART_READ_ONLY:true}); }
      catch (failure) { error = failure.message; }
      process.stdout.write(JSON.stringify({raced,error}));
    """
    output = json.loads(_run_config_module(tmp_path, script, {}))
    assert output["raced"] is True, "The real file must change after the snapshot read"
    assert "changed after confirmation" in output["error"]
    assert (
        selected.read_text()
        == "REFLEXIO_URL=http://localhost:8071/\nREFLEXIO_API_KEY=new-owned-key\n"
    )


def test_dashboard_save_respects_the_live_python_publisher_lock(tmp_path: Path) -> None:
    from tests.test_env_transactions import _paused_writer

    state = tmp_path / ".claude-smart"
    state.mkdir()
    selected = state / ".env"
    original = "REFLEXIO_URL=http://localhost:8071/\nREFLEXIO_API_KEY=owned-key\n"
    selected.write_text(original)
    with _paused_writer(selected):
        output = _run_config_module(
            tmp_path,
            "try { await m.writeConfig({CLAUDE_SMART_READ_ONLY:true}); } catch(error) { process.stdout.write(error.message); }",
            {},
        )
        assert "busy" in output
        assert selected.read_text() == original
    _run_config_module(
        tmp_path, "await m.writeConfig({CLAUDE_SMART_READ_ONLY:true});", {}
    )
    assert "CLAUDE_SMART_READ_ONLY=1\n" in selected.read_text()


@pytest.mark.parametrize("first", ["REFLEXIO_URL", "CLAUDE_SMART_READ_ONLY", "OTHER"])
@pytest.mark.parametrize("export", ["", "export\t"])
def test_dashboard_accepts_bom_prefixed_first_assignment(
    tmp_path: Path, first: str, export: str
) -> None:
    state = tmp_path / ".claude-smart"
    state.mkdir()
    selected = state / ".env"
    values = {
        "REFLEXIO_URL": "http://localhost:8071/",
        "CLAUDE_SMART_READ_ONLY": "0",
        "OTHER": "keep",
    }
    content = "\ufeff" + export + first + "=" + values[first] + "\n"
    content += "".join(
        key + "=" + value + "\n" for key, value in values.items() if key != first
    )
    selected.write_bytes(content.encode())
    output = _run_config_module(
        tmp_path,
        "await m.writeConfig({CLAUDE_SMART_READ_ONLY:true}); process.stdout.write(JSON.stringify(await m.readConfig()));",
        {},
    )
    assert json.loads(output)["CLAUDE_SMART_READ_ONLY"] is True
    if first != "CLAUDE_SMART_READ_ONLY":
        assert selected.read_bytes().startswith(
            ("\ufeff" + export + first + "=" + values[first] + "\n").encode()
        )
