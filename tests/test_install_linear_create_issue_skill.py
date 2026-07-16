from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess


REPO_ROOT = Path(__file__).parents[1]
INSTALL_SCRIPT = REPO_ROOT / "scripts" / "install-linear-create-issue-skill.sh"
SOURCE_SKILL = REPO_ROOT / "skills" / "linear-create-issue"


def test_installer_copies_discoverable_skill_and_updates_stale_files(tmp_path):
    codex_home = tmp_path / "codex-home"
    stale = codex_home / "skills" / "linear-create-issue" / "SKILL.md"
    stale.parent.mkdir(parents=True)
    stale.write_text("stale\n", encoding="utf-8")
    env = {**os.environ, "CODEX_HOME": str(codex_home)}

    first = subprocess.run(
        [str(INSTALL_SCRIPT)],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    second = subprocess.run(
        [str(INSTALL_SCRIPT)],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    target = codex_home / "skills" / "linear-create-issue"
    assert first.returncode == 0
    assert second.returncode == 0
    assert (target / "SKILL.md").read_bytes() == (SOURCE_SKILL / "SKILL.md").read_bytes()
    assert (target / "agents" / "openai.yaml").read_bytes() == (
        SOURCE_SKILL / "agents" / "openai.yaml"
    ).read_bytes()
    launcher = target / "scripts" / "create_linear_issue.py"
    assert launcher.read_bytes() == (
        SOURCE_SKILL / "scripts" / "create_linear_issue.py"
    ).read_bytes()
    assert stat.S_IMODE(launcher.stat().st_mode) == 0o755


def test_installer_rejects_relative_codex_home(tmp_path):
    env = {**os.environ, "CODEX_HOME": "relative-codex-home"}

    result = subprocess.run(
        [str(INSTALL_SCRIPT)],
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "must be absolute" in result.stderr
