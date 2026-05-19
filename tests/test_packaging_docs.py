from __future__ import annotations

import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_pyproject_defines_cli_and_build_dev_dependency() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["project"]["scripts"]["dbcli"] == "dbcli.cli:app"
    assert "build>=1,<2" in pyproject["project"]["optional-dependencies"]["dev"]


def test_ci_workflow_runs_tests_and_builds_distribution() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert 'python -m pip install -e ".[dev]"' in workflow
    assert "python -m pytest" in workflow
    assert "python -m build" in workflow
    assert '"3.11"' in workflow
    assert '"3.12"' in workflow


def test_manifest_includes_readme_demo_assets() -> None:
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")

    assert "include README.md" in manifest
    assert "recursive-include docs *.svg" in manifest


def test_readme_documents_operator_workflow() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    for expected in [
        "![dbcli quickstart demo](docs/assets/dbcli-quickstart.svg)",
        "Why dbcli",
        "Quickstart",
        "dbcli init",
        "dbcli profile add",
        "dbcli validate",
        "dbcli load",
        "append",
        "replace",
        ".dbcli/runs.jsonl",
        "Exit Codes",
    ]:
        assert expected in readme


def test_readme_demo_asset_is_valid_svg() -> None:
    asset = ROOT / "docs/assets/dbcli-quickstart.svg"

    assert asset.exists()
    root = ET.parse(asset).getroot()
    assert root.tag.endswith("svg")
