"""Static checks for dashboard managed Reflexio support."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


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
    assert 'checked={!!config.CLAUDE_SMART_READ_ONLY}' in page
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
    assert "function defaultUrl()" in route
    assert 'process.env.BACKEND_PORT || "8071"' in route
    assert 'headers.set("user-agent", "claude-smart")' in route
    assert 'headers.set("authorization", `Bearer ${apiKey}`)' in route
    assert "managedReflexioSettings" in route
    assert "configuredBase" in route
    assert 'apiKey: configuredBase ? apiKey : ""' in route
    assert "fromHeader" not in route
    assert "x-reflexio-url" not in route


def test_dashboard_settings_read_configured_reflexio_url_only() -> None:
    settings = (
        REPO_ROOT / "plugin" / "dashboard" / "hooks" / "use-settings.tsx"
    ).read_text()
    page = (
        REPO_ROOT / "plugin" / "dashboard" / "app" / "configure" / "env" / "page.tsx"
    ).read_text()

    assert 'fetch("/api/config", { cache: "no-store" })' in settings
    assert "REFLEXIO_URL?: string" in settings
    assert "localStorage" not in settings
    assert "setReflexioUrl" not in settings
    assert "claude-smart-dashboard-settings" not in settings
    assert 'SETTINGS_CHANGED_EVENT = "claude-smart-settings-changed"' in settings
    assert "window.dispatchEvent(new Event(SETTINGS_CHANGED_EVENT))" in page
    assert "Stored in browser localStorage" not in page
    assert "Reflexio endpoint (dashboard)" not in page


def _run_config_module(tmp_path: Path, script: str, env_extra: dict[str, str]) -> str:
    """Run config-file.ts under Node's type stripping with HOME=tmp_path."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required")
    probe = subprocess.run(
        [node, "-p", "process.features.typescript"], capture_output=True, text=True
    )
    if probe.stdout.strip() in {"", "false", "undefined"}:
        pytest.skip("this node cannot strip TypeScript types")
    env = {k: v for k, v in os.environ.items() if not k.startswith("REFLEXIO_")}
    env["HOME"] = str(tmp_path)
    env.update(env_extra)
    module = REPO_ROOT / "plugin" / "dashboard" / "lib" / "config-file.ts"
    result = subprocess.run(
        [
            node,
            "--no-warnings",
            "--input-type=module",
            "-e",
            f"const m = await import({json.dumps(str(module))});\n{script}",
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_dashboard_proxy_prefers_the_saved_file_over_its_launch_env(tmp_path: Path) -> None:
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
    env_file.write_text('export REFLEXIO_URL="https://c.example/"\nexport REFLEXIO_API_KEY="kc"\n')
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


def test_dashboard_save_does_not_turn_off_absent_local_providers(tmp_path: Path) -> None:
    # A managed file has no local-provider flags. Saving the Configure page
    # must not write them as 0, or a later switch to local mode keeps them off.
    env_file = tmp_path / ".claude-smart" / ".env"
    env_file.parent.mkdir()
    env_file.write_text('REFLEXIO_URL="https://www.reflexio.ai/"\nREFLEXIO_API_KEY="k"\n')
    _run_config_module(tmp_path, "await m.writeConfig(await m.readConfig());", {})
    text = env_file.read_text()
    assert "CLAUDE_SMART_USE_LOCAL_CLI=0" not in text
    assert "CLAUDE_SMART_USE_LOCAL_EMBEDDING=0" not in text
