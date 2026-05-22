"""Static checks for dashboard managed Reflexio support."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_dashboard_config_knows_reflexio_api_key() -> None:
    config = (REPO_ROOT / "plugin" / "dashboard" / "lib" / "config-file.ts").read_text()
    types = (REPO_ROOT / "plugin" / "dashboard" / "lib" / "types.ts").read_text()
    page = (
        REPO_ROOT / "plugin" / "dashboard" / "app" / "configure" / "env" / "page.tsx"
    ).read_text()

    assert '"REFLEXIO_API_KEY"' in config
    assert "REFLEXIO_API_KEY: string;" in types
    assert "REFLEXIO_API_KEY_SET?: boolean;" in types
    assert "<Label>REFLEXIO_API_KEY</Label>" in page
    assert 'type="password"' in page
    assert "apiKeyDirty" in page
    assert "delete envUpdate.REFLEXIO_API_KEY" in page
    assert "leave blank to keep existing key" in page


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
    assert 'headers.set("user-agent", "claude-smart")' in route
    assert 'headers.set("authorization", `Bearer ${apiKey}`)' in route
    assert "readConfig" in route
    assert "function isLocalUrl" in route
    assert "configuredBase" in route
    assert 'apiKey: fromHeader === configuredBase ? apiKey : ""' in route
    assert 'apiKey: configuredBase ? apiKey : ""' in route


def test_dashboard_settings_adopt_managed_reflexio_url() -> None:
    settings = (
        REPO_ROOT / "plugin" / "dashboard" / "hooks" / "use-settings.tsx"
    ).read_text()

    assert 'fetch("/api/config", { cache: "no-store" })' in settings
    assert "REFLEXIO_URL?: string" in settings
    assert "function isLocalUrl" in settings
    assert "!isLocalUrl(configuredUrl)" in settings
    assert "isLocalUrl(settings.reflexioUrl)" in settings
    assert "writeStorage({ reflexioUrl: configuredUrl })" in settings
