"""Regression tests for dashboard dependencies needed during install-time build."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_ROOT = REPO_ROOT / "plugin" / "dashboard"
DASHBOARD_PACKAGE = DASHBOARD_ROOT / "package.json"


@pytest.mark.parametrize(
    ("package_name", "reference_path", "reference_text"),
    [
        ("@tailwindcss/postcss", "postcss.config.mjs", '"@tailwindcss/postcss"'),
        ("shadcn", "app/globals.css", '@import "shadcn/tailwind.css"'),
    ],
)
def test_dashboard_build_packages_are_production_dependencies(
    package_name: str, reference_path: str, reference_text: str
) -> None:
    package = json.loads(DASHBOARD_PACKAGE.read_text())

    assert reference_text in (DASHBOARD_ROOT / reference_path).read_text()
    assert package_name in package.get("dependencies", {})
    assert package_name not in package.get("devDependencies", {})


def test_dashboard_build_starts_the_dashboard_when_a_fresh_build_completes(
    tmp_path: Path,
) -> None:
    # dashboard-service.sh start only spawns this build when .next is
    # missing and then returns, so nothing else starts the dashboard; the
    # installer tells the user it will serve once the build finishes.
    plugin = tmp_path / "plugin"
    scripts = plugin / "scripts"
    scripts.mkdir(parents=True)
    for name in ("dashboard-build.sh", "_lib.sh"):
        shutil.copy2(REPO_ROOT / "plugin" / "scripts" / name, scripts / name)
    (scripts / "dashboard-service.sh").write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$HOME/dashboard-service.log"\n'
    )
    dashboard = plugin / "dashboard"
    dashboard.mkdir()
    (dashboard / "package.json").write_text("{}\n")
    time.sleep(0.01)
    (dashboard / "node_modules").mkdir()
    # Fake npm on the private-Node path, which the build script prepends.
    npm_dir = tmp_path / ".claude-smart" / "node" / "current" / "bin"
    npm_dir.mkdir(parents=True)
    npm = npm_dir / "npm"
    npm.write_text(
        '#!/bin/sh\n[ "$1" = --version ] && { echo 10.0.0; exit 0; }\n'
        '[ "$1 $2" = "run build" ] && mkdir -p .next\nexit 0\n'
    )
    npm.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_SMART_")}
    env.update({"HOME": str(tmp_path), "SHELL": "/bin/sh"})

    result = subprocess.run(
        ["/bin/bash", str(scripts / "dashboard-build.sh")],
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert (dashboard / ".next").is_dir()
    log = tmp_path / "dashboard-service.log"
    assert log.is_file() and log.read_text().splitlines() == ["start"]
