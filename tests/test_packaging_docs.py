from __future__ import annotations

import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_pyproject_defines_cli_and_build_dev_dependency() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["project"]["scripts"]["dbcli"] == "dbcli.cli:app"
    assert "build>=1,<2" in pyproject["dependency-groups"]["dev"]
    assert "pytest>=8,<9" in pyproject["dependency-groups"]["dev"]


def test_ci_workflow_runs_tests_and_builds_distribution() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "astral-sh/setup-uv@v5" in workflow
    assert "uv sync --dev" in workflow
    assert "uv run pytest" in workflow
    assert "uv build" in workflow
    assert "python -m pip" not in workflow
    assert "cache: pip" not in workflow
    assert '"3.11"' in workflow
    assert '"3.12"' in workflow


def test_manifest_includes_readme_demo_assets() -> None:
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")

    assert "include README.md" in manifest
    assert "recursive-include docs *.svg" in manifest
    assert "recursive-exclude docs *.md" in manifest
    assert "include SPEC.md" not in manifest
    assert "recursive-include docs *.md" not in manifest


def test_readme_documents_operator_workflow() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    for expected in [
        "![dbcli quickstart demo](docs/assets/dbcli-quickstart.svg)",
        "Why dbcli",
        "Quickstart",
        "dbcli init",
        "macOS Keychain",
        "dbcli profile test",
        "dbcli validate",
        "dbcli load",
        "append",
        "replace",
        ".dbcli/runs.jsonl",
        "Exit Codes",
        "uv sync --dev",
        "uv tool install .",
    ]:
        assert expected in readme


def test_local_planning_docs_are_gitignored() -> None:
    root_markdown = sorted(path.name for path in ROOT.glob("*.md"))
    assert root_markdown == ["README.md"]

    assert not (ROOT / "SPEC.md").exists()
    assert not (ROOT / "BUILD.md").exists()
    assert not (ROOT / "ROADMAP.md").exists()

    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in (
        "SPEC.md",
        "BUILD.md",
        "ROADMAP.md",
        "docs/spec.md",
        "docs/build.md",
        "docs/roadmap.md",
    ):
        assert pattern in gitignore


def test_readme_demo_asset_is_valid_svg() -> None:
    asset = ROOT / "docs/assets/dbcli-quickstart.svg"

    assert asset.exists()
    root = ET.parse(asset).getroot()
    assert root.tag.endswith("svg")
