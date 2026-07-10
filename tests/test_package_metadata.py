from __future__ import annotations

import importlib
import re
import tomllib
from pathlib import Path

import linear_agent_orchestrator


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


def test_package_version_exists_and_uses_x_y_z_format() -> None:
    version = getattr(linear_agent_orchestrator, "__version__", None)

    assert isinstance(version, str)
    assert SEMVER_RE.fullmatch(version)


def test_package_version_matches_pyproject_version() -> None:
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["project"]["version"] == linear_agent_orchestrator.__version__


def test_cli_entry_point_target_is_importable() -> None:
    module_name, function_name = "linear_agent_orchestrator.cli:main".split(":", maxsplit=1)

    module = importlib.import_module(module_name)

    assert callable(getattr(module, function_name))
