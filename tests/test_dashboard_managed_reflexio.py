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
    assert "readConfig" in route
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


def test_dashboard_model_provenance_reads_local_sqlite_not_reflexio_api() -> None:
    """Model provenance is local filesystem data, not a Reflexio backend API.

    Pattern matches host origin: client hook -> thin local /api/* route ->
    server-side reader of local state (JSONL for origin, SQLite for model).
    """
    reader = (
        REPO_ROOT / "plugin" / "dashboard" / "lib" / "model-lineage.ts"
    ).read_text()
    route = (
        REPO_ROOT
        / "plugin"
        / "dashboard"
        / "app"
        / "api"
        / "model-provenance"
        / "route.ts"
    ).read_text()
    hook = (
        REPO_ROOT / "plugin" / "dashboard" / "lib" / "model-provenance.ts"
    ).read_text()
    view = (
        REPO_ROOT
        / "plugin"
        / "dashboard"
        / "components"
        / "common"
        / "model-provenance.tsx"
    ).read_text()
    preference = (
        REPO_ROOT / "plugin" / "dashboard" / "app" / "preferences" / "[id]" / "page.tsx"
    ).read_text()
    project = (
        REPO_ROOT
        / "plugin"
        / "dashboard"
        / "app"
        / "skills"
        / "project"
        / "[id]"
        / "page.tsx"
    ).read_text()
    shared = (
        REPO_ROOT
        / "plugin"
        / "dashboard"
        / "app"
        / "skills"
        / "shared"
        / "[id]"
        / "page.tsx"
    ).read_text()
    package = (REPO_ROOT / "plugin" / "dashboard" / "package.json").read_text()

    # Direct local SQLite read path.
    assert 'from "node:sqlite"' in reader
    assert 'readOnly: true' in reader
    assert ".reflexio" in reader and "reflexio.db" in reader
    assert "model_name" in reader and "provider" in reader
    assert "requested_model" not in reader
    assert "database unavailable" in reader
    assert "provenance query failed" in reader

    # Local dashboard route, not Reflexio proxy.
    assert 'from "@/lib/model-lineage"' in route
    assert "getLearningModelProvenance" in route
    assert "force-dynamic" in route
    assert "api/reflexio" not in route
    assert "get_learning_provenance" not in route

    # Client hook consumes local route only.
    assert 'fetch(`/api/model-provenance?' in hook
    assert "useLearningModelProvenance" in hook
    assert "api/reflexio" not in hook

    # Detail pages only.
    assert "useLearningModelProvenance" in preference
    assert "useLearningModelProvenance" in project
    assert "useLearningModelProvenance" in shared
    assert "LearningModelProvenanceView" in preference
    assert "LearningModelProvenanceView" in project
    assert "LearningModelProvenanceView" in shared
    assert 'label="Model"' in preference
    assert 'label="Model"' in project
    assert 'label="Model"' in shared
    assert "Not recorded" in view
    assert "Unavailable" in view

    # Runtime floor matches node:sqlite readOnly support.
    assert '"node": ">=22.18.0"' in package
