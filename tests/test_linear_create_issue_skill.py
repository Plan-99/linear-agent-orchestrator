from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys


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


def _fake_lao_root(parent: Path) -> Path:
    root = parent / "linear-agent-orchestrator"
    module = root / "src" / "linear_agent_orchestrator" / "cli.py"
    module.parent.mkdir(parents=True)
    module.write_text("# test fixture\n", encoding="utf-8")
    return root


def _secure_env(path: Path, content: str = "LINEAR_API_KEY=secret\n") -> Path:
    path.write_text(content, encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return path


def _description(path: Path, task: str = "Do the work.") -> Path:
    path.write_text(
        f"## Task\n{task}\n\n"
        "## Acceptance Criteria\n- It works.\n\n"
        "## Validation\n- Run tests.\n",
        encoding="utf-8",
    )
    return path


def _completed(
    returncode: int = 0,
    *,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


def _live_args(
    *,
    root: Path,
    env_file: Path,
    description: Path,
    state_db: Path,
    request_sha256: str = "a" * 64,
) -> list[str]:
    return [
        "--lao-root",
        str(root),
        "--env-file",
        str(env_file),
        "--state-db",
        str(state_db),
        "--title",
        "Create guarded issue",
        "--description-file",
        str(description),
        "--expected-request-sha256",
        request_sha256,
        "--json",
    ]


def test_dry_run_uses_isolated_python_and_scrubs_inherited_python_and_linear_env(
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
    monkeypatch.setenv("PYTHONPATH", "/tmp/untrusted")
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return _completed(stdout='{"dry_run": true}\n')

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
    command, options = calls[0]
    assert command[:3] == [sys.executable, "-I", "-c"]
    assert str(root.resolve() / "src") in command[3]
    assert command[4:7] == ["--env-file", str(env_file.resolve()), "create-issue"]
    state_index = command.index("--state-name")
    assert command[state_index + 1] == "Backlog"
    assert command[-2:] == ["--dry-run", "--json"]
    assert options["cwd"] == root.resolve()
    assert options["check"] is False
    assert options["text"] is True
    assert options["capture_output"] is True
    assert "LINEAR_API_KEY" not in options["env"]
    assert "PYTHONPATH" not in options["env"]


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
            "--dry-run",
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
    monkeypatch.chdir(unrelated)
    monkeypatch.delenv("LINEAR_CREATE_ISSUE_ENV_FILE", raising=False)

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


def test_invalid_explicit_root_fails_without_falling_back(tmp_path, monkeypatch):
    home = tmp_path / "home"
    fallback = _fake_lao_root(home / "Project")
    assert fallback.is_dir()
    monkeypatch.setenv("HOME", str(home))
    args = skill_script.build_parser().parse_args(
        [
            "--lao-root",
            str(tmp_path / "missing"),
            "--title",
            "Fail closed",
            "--description-file",
            str(tmp_path / "body.md"),
        ]
    )

    try:
        skill_script._resolve_lao_root(args)
    except skill_script.LauncherError as exc:
        assert "--lao-root does not point to a valid LAO checkout" in str(exc)
    else:
        raise AssertionError("invalid explicit root must not fall back")


def test_env_file_parent_is_never_promoted_to_code_root(tmp_path, monkeypatch):
    untrusted_root = _fake_lao_root(tmp_path / "untrusted")
    env_file = _secure_env(untrusted_root / ".env")
    empty_home = tmp_path / "empty-home"
    empty_home.mkdir()
    monkeypatch.setenv("HOME", str(empty_home))
    monkeypatch.setenv("LINEAR_CREATE_ISSUE_ENV_FILE", str(env_file))
    args = skill_script.build_parser().parse_args(
        [
            "--title",
            "Do not execute adjacent code",
            "--description-file",
            str(tmp_path / "body.md"),
        ]
    )

    try:
        skill_script._resolve_lao_root(args)
    except skill_script.LauncherError as exc:
        assert "Could not locate" in str(exc)
    else:
        raise AssertionError("an env file must not confer trust on adjacent code")


def test_relative_xdg_config_home_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative-config")
    monkeypatch.delenv("LINEAR_CREATE_ISSUE_ENV_FILE", raising=False)
    args = skill_script.build_parser().parse_args(
        [
            "--title",
            "Reject relative config",
            "--description-file",
            str(tmp_path / "body.md"),
        ]
    )

    try:
        skill_script._explicit_env_file(args)
    except skill_script.LauncherError as exc:
        assert "XDG_CONFIG_HOME must be an absolute path" in str(exc)
    else:
        raise AssertionError("relative XDG_CONFIG_HOME must fail closed")


def test_relative_environment_pointer_paths_are_rejected(tmp_path, monkeypatch):
    args = skill_script.build_parser().parse_args(
        [
            "--title",
            "Reject relative pointer",
            "--description-file",
            str(tmp_path / "body.md"),
        ]
    )
    checks = (
        ("LINEAR_CREATE_ISSUE_ENV_FILE", lambda: skill_script._explicit_env_file(args)),
        ("LINEAR_CREATE_ISSUE_LAO_ROOT", lambda: skill_script._resolve_lao_root(args)),
        ("LINEAR_CREATE_ISSUE_STATE_DB", lambda: skill_script._state_db_path(args)),
    )

    for name, check in checks:
        monkeypatch.setenv(name, "relative-path")
        try:
            check()
        except skill_script.LauncherError as exc:
            assert f"{name} must be an absolute path" in str(exc)
        else:
            raise AssertionError(f"{name} must reject relative paths")
        monkeypatch.delenv(name)


def test_live_creation_requires_preview_digest_before_ledger_or_subprocess(
    tmp_path,
    monkeypatch,
    capsys,
):
    root = _fake_lao_root(tmp_path)
    env_file = _secure_env(root / ".env")
    description = _description(tmp_path / "issue.md")

    def must_not_run(*args, **kwargs):
        raise AssertionError("subprocess must not run without a preview digest")

    monkeypatch.setattr(skill_script.subprocess, "run", must_not_run)
    result = skill_script.main(
        [
            "--lao-root",
            str(root),
            "--env-file",
            str(env_file),
            "--title",
            "No preview",
            "--description-file",
            str(description),
        ]
    )

    assert result == 2
    assert "--expected-request-sha256" in capsys.readouterr().err


def test_completed_request_is_replayed_without_second_mutation(tmp_path, monkeypatch, capsys):
    root = _fake_lao_root(tmp_path)
    env_file = _secure_env(root / ".env")
    description = _description(tmp_path / "issue.md")
    private_state = tmp_path / "private-state"
    private_state.mkdir(mode=0o700)
    state_db = private_state / "requests.sqlite3"
    created = {
        "identifier": "DEV-501",
        "issue_id": "issue-id",
        "title": "Create guarded issue",
        "url": "https://linear.example/DEV-501",
    }
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return _completed(stdout=json.dumps(created))

    monkeypatch.setattr(skill_script.subprocess, "run", fake_run)
    args = _live_args(
        root=root,
        env_file=env_file,
        description=description,
        state_db=state_db,
    )

    uppercase_args = _live_args(
        root=root,
        env_file=env_file,
        description=description,
        state_db=state_db,
        request_sha256="A" * 64,
    )

    assert skill_script.main(args) == 0
    assert skill_script.main(uppercase_args) == 0
    assert len(calls) == 1
    assert "--expected-request-sha256" in calls[0]
    assert capsys.readouterr().out.count('"identifier": "DEV-501"') == 2
    assert stat.S_IMODE(state_db.stat().st_mode) == 0o600
    with sqlite3.connect(state_db) as connection:
        assert connection.execute("SELECT status FROM requests").fetchone() == ("completed",)


def test_premutation_failure_releases_reservation_for_corrected_retry(
    tmp_path,
    monkeypatch,
):
    root = _fake_lao_root(tmp_path)
    env_file = _secure_env(root / ".env")
    description = _description(tmp_path / "issue.md")
    private_state = tmp_path / "private-state"
    private_state.mkdir(mode=0o700)
    state_db = private_state / "requests.sqlite3"
    created = {
        "identifier": "DEV-502",
        "issue_id": "issue-id-2",
        "title": "Create guarded issue",
        "url": "https://linear.example/DEV-502",
    }
    results = [
        _completed(returncode=2, stderr="request hash mismatch\n"),
        _completed(stdout=json.dumps(created)),
    ]

    monkeypatch.setattr(skill_script.subprocess, "run", lambda *args, **kwargs: results.pop(0))
    args = _live_args(
        root=root,
        env_file=env_file,
        description=description,
        state_db=state_db,
    )

    assert skill_script.main(args) == 2
    assert skill_script.main(args) == 0
    with sqlite3.connect(state_db) as connection:
        assert connection.execute("SELECT status FROM requests").fetchone() == ("completed",)


def test_uncertain_mutation_blocks_automatic_retry(tmp_path, monkeypatch, capsys):
    root = _fake_lao_root(tmp_path)
    env_file = _secure_env(root / ".env")
    description = _description(tmp_path / "issue.md")
    private_state = tmp_path / "private-state"
    private_state.mkdir(mode=0o700)
    state_db = private_state / "requests.sqlite3"
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return _completed(returncode=4, stderr="mutation outcome uncertain\n")

    monkeypatch.setattr(skill_script.subprocess, "run", fake_run)
    args = _live_args(
        root=root,
        env_file=env_file,
        description=description,
        state_db=state_db,
    )

    assert skill_script.main(args) == 4
    assert skill_script.main(args) == 4
    assert len(calls) == 1
    assert "check Linear before any manual retry" in capsys.readouterr().err
    with sqlite3.connect(state_db) as connection:
        assert connection.execute("SELECT status FROM requests").fetchone() == ("uncertain",)
