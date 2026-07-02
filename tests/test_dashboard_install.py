"""Regression tests for dashboard dependencies needed during install-time build."""

from __future__ import annotations

import json
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
