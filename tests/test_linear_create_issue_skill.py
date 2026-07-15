from __future__ import annotations

import importlib.util
from pathlib import Path
import stat
import sys
from types import SimpleNamespace

import pytest


SCRIPT_PATH = (
    Path(__file__).parents[1]
    / "skills"
    / "linear-create-issue"
    / "scripts"
    / "create_linear_issue.py"
)
SPEC = importlib.util.spec_from_file_location("linear_create_issue_skill_script", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
skill_script = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = skill_script
SPEC.loader.exec_module(skill_script)


def _fake_lao_root(tmp_path: Path) -> Path:
    root = tmp_path / "linear-agent-orchestrator"
    module = root / "src" / "linear_agent_orchestrator" / "cli.py"
    module.parent.mkdir(parents=True)
    module.write_text("# test fixture\n", encoding="utf-8")
    return root


def _secure_env(path: Path, content: str = "LINEAR_API_KEY=secret\n") -> Path:
    path.write_text(content, encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return path


def _description(path: Path) -> Path:
    path.write_text(
        "## Task\nDo the work.\n\n"
        "## Acceptance Criteria\n- It works.\n\n"
        "## Validation\n- Run tests.\n",
        encoding="utf-8",
    )
    return path


def test_main_invokes_lao_from_unrelated_directory_and_scrubs_session_linear_env(
    tmp_path,
    monkeypatch,
):
    root = _fake_lao_root(tmp_path)
    env_file = _secure_env(root / ".env")
    description = _description(tmp_path / "issue.md")
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)
    monkeypatch.setenv("LINEAR_API_KEY", "wrong-session-secret")
    calls = []

    def fake_run(command, *, cwd, env, check):
        calls.append((command, cwd, env, check))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(skill_script.subprocess, "run", fake_run)

    result = skill_script.main(
        [
            "--lao-root",
            str(root),
            "--env-file",
            str(env_file),
            "--title",
            "Create guarded issue",
            "--description-file",
            str(description),
            "--dry-run",
            "--json",
        ]
    )

    assert result == 0
    assert len(calls) == 1
    command, cwd, child_env, check = calls[0]
    assert cwd == root.resolve()
    assert check is False
    assert "LINEAR_API_KEY" not in child_env
    assert child_env["PYTHONPATH"].split(":")[0] == str(root.resolve() / "src")
    assert command[1:4] == ["-m", "linear_agent_orchestrator.cli", "--env-file"]
    assert command[4] == str(env_file.resolve())
    assert command[-2:] == ["--dry-run", "--json"]


def test_insecure_env_file_fails_before_subprocess(tmp_path, monkeypatch, capsys):
    root = _fake_lao_root(tmp_path)
    env_file = root / ".env"
    env_file.write_text("LINEAR_API_KEY=secret\n", encoding="utf-8")
    env_file.chmod(0o644)
    description = _description(tmp_path / "issue.md")

    def must_not_run(*args, **kwargs):
        raise AssertionError("subprocess must not run for insecure credentials")

    monkeypatch.setattr(skill_script.subprocess, "run", must_not_run)

    result = skill_script.main(
        [
            "--lao-root",
            str(root),
            "--env-file",
            str(env_file),
            "--title",
            "Blocked issue",
            "--description-file",
            str(description),
        ]
    )

    assert result == 2
    assert "expected 0600 or stricter" in capsys.readouterr().err


def test_launcher_does_not_discover_arbitrary_cwd_dotenv(tmp_path, monkeypatch, capsys):
    root = _fake_lao_root(tmp_path)
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    _secure_env(unrelated / ".env")
    description = _description(tmp_path / "issue.md")
    empty_home = tmp_path / "home"
    empty_home.mkdir()
    monkeypatch.setenv("HOME", str(empty_home))
    monkeypatch.delenv("LINEAR_CREATE_ISSUE_ENV_FILE", raising=False)
    monkeypatch.chdir(unrelated)

    result = skill_script.main(
        [
            "--lao-root",
            str(root),
            "--title",
            "No implicit env",
            "--description-file",
            str(description),
            "--dry-run",
        ]
    )

    assert result == 2
    error = capsys.readouterr().err
    assert str(root.resolve() / ".env") in error
    assert str(unrelated / ".env") not in error


def test_explicit_paths_override_environment_pointers(tmp_path, monkeypatch):
    root = _fake_lao_root(tmp_path)
    env_file = _secure_env(root / ".env")
    other_root = _fake_lao_root(tmp_path / "other")
    other_env = _secure_env(other_root / ".env")
    monkeypatch.setenv("LINEAR_CREATE_ISSUE_LAO_ROOT", str(other_root))
    monkeypatch.setenv("LINEAR_CREATE_ISSUE_ENV_FILE", str(other_env))
    args = skill_script.build_parser().parse_args(
        [
            "--lao-root",
            str(root),
            "--env-file",
            str(env_file),
            "--title",
            "Explicit",
            "--description-file",
            str(tmp_path / "body.md"),
        ]
    )

    selected_env = skill_script._explicit_env_file(args)
    selected_root = skill_script._resolve_lao_root(args, selected_env)

    assert selected_env == env_file.resolve()
    assert selected_root == root.resolve()


def test_root_discovery_ignores_code_in_arbitrary_working_directory(tmp_path, monkeypatch):
    untrusted_root = _fake_lao_root(tmp_path / "untrusted")
    empty_home = tmp_path / "empty-home"
    empty_home.mkdir()
    monkeypatch.chdir(untrusted_root)
    monkeypatch.setenv("HOME", str(empty_home))
    monkeypatch.delenv("LINEAR_CREATE_ISSUE_LAO_ROOT", raising=False)
    args = skill_script.build_parser().parse_args(
        [
            "--title",
            "Do not execute local code",
            "--description-file",
            str(tmp_path / "body.md"),
        ]
    )

    with pytest.raises(skill_script.LauncherError, match="Could not locate"):
        skill_script._resolve_lao_root(args, None)
