from pathlib import Path

from linear_agent_orchestrator.config import (
    DEFAULT_BACKOFF_INITIAL_SECONDS,
    DEFAULT_BACKOFF_MAX_ATTEMPTS,
    DEFAULT_BACKOFF_MAX_SECONDS,
    DEFAULT_LEASE_RENEWAL_INTERVAL_SECONDS,
    DEFAULT_LEASE_TTL_SECONDS,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_RUNNER_MAX_LIFETIME_SECONDS,
    DEFAULT_RUNNER_TIMEOUT_SECONDS,
    load_config,
    load_dotenv_file,
)


def test_config_defaults_include_first_story_constants():
    config = load_config(env={})

    assert config.poll_interval_seconds == DEFAULT_POLL_INTERVAL_SECONDS == 60
    assert config.lease_ttl_seconds == DEFAULT_LEASE_TTL_SECONDS == 900
    assert config.lease_renewal_interval_seconds == DEFAULT_LEASE_RENEWAL_INTERVAL_SECONDS == 120
    assert config.runner.timeout_seconds == DEFAULT_RUNNER_TIMEOUT_SECONDS == 3600
    assert config.runner.max_lifetime_seconds == DEFAULT_RUNNER_MAX_LIFETIME_SECONDS == 21600
    assert config.backoff.initial_seconds == DEFAULT_BACKOFF_INITIAL_SECONDS == 5
    assert config.backoff.max_seconds == DEFAULT_BACKOFF_MAX_SECONDS == 300
    assert config.backoff.max_attempts == DEFAULT_BACKOFF_MAX_ATTEMPTS == 5


def test_config_loads_required_linear_and_runner_values_from_env_without_leaking_secret():
    config = load_config(
        env={
            "LINEAR_API_KEY": "lin_api_secret",
            "LINEAR_TEAM_ID": "team-id",
            "LINEAR_PROJECT_NAME": "TEST",
            "RUNNER_COMMAND_TEMPLATE": "echo {prompt_path}",
            "RUNNER_ALLOWED_ENV": "PATH,HOME",
        }
    )

    assert config.validate_startup(require_secrets=True) == []
    assert config.linear.api_key == "lin_api_secret"
    assert config.linear.team_id == "team-id"
    assert config.linear.project_name == "TEST"
    assert config.runner.command_template == "echo {prompt_path}"
    assert config.runner.allowed_env == ("PATH", "HOME")
    assert config.redacted_dict()["linear"]["api_key"] == "***REDACTED***"


def test_runner_max_lifetime_loads_from_env_and_rejects_non_positive():
    custom = load_config(env={"RUNNER_MAX_LIFETIME_SECONDS": "7200"})
    assert custom.runner.max_lifetime_seconds == 7200
    assert not any("runner_max_lifetime_seconds" in error for error in custom.validate_startup(require_secrets=False))

    for invalid in ("0", "-1"):
        config = load_config(env={"RUNNER_MAX_LIFETIME_SECONDS": invalid})
        errors = config.validate_startup(require_secrets=False)
        assert "runner_max_lifetime_seconds must be positive" in errors


def test_environment_overrides_dotenv_file(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LINEAR_API_KEY=from_file\n"
        "LINEAR_TEAM_ID=file-team\n"
        "LINEAR_PROJECT_ID=file-project\n"
        "RUNNER_COMMAND_TEMPLATE='echo from file'\n",
        encoding="utf-8",
    )

    config = load_config(env={"LINEAR_API_KEY": "from_env"}, env_files=[env_file])

    assert config.linear.api_key == "from_env"
    assert config.linear.team_id == "file-team"
    assert config.linear.project_id == "file-project"
    assert config.runner.command_template == "echo from file"


def test_missing_secret_validation_lists_required_values():
    config = load_config(env={})

    errors = config.validate_startup(require_secrets=True)

    assert "LINEAR_API_KEY is required" in errors
    assert "LINEAR_TEAM_ID is required" in errors
    assert "LINEAR_PROJECT_NAME or LINEAR_PROJECT_ID is required" in errors
    assert "RUNNER_COMMAND_TEMPLATE is required" in errors
    assert config.validate_startup(require_secrets=False) == []


def test_dotenv_parser_supports_comments_export_and_quotes(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# local secrets stay out of source\n"
        "export LINEAR_API_KEY='quoted secret'\n"
        "RUNNER_COMMAND_TEMPLATE=\"echo hello\"\n",
        encoding="utf-8",
    )

    assert load_dotenv_file(env_file) == {
        "LINEAR_API_KEY": "quoted secret",
        "RUNNER_COMMAND_TEMPLATE": "echo hello",
    }


def test_startup_validation_rejects_invalid_runner_templates_before_processing():
    base = {
        "LINEAR_API_KEY": "lin_api_secret",
        "LINEAR_TEAM_ID": "team-id",
        "LINEAR_PROJECT_NAME": "TEST",
    }

    embedded = load_config(env={**base, "RUNNER_COMMAND_TEMPLATE": "echo prefix-{issue_id}"})
    unknown = load_config(env={**base, "RUNNER_COMMAND_TEMPLATE": "echo {unknown}"})
    bad_shlex = load_config(env={**base, "RUNNER_COMMAND_TEMPLATE": "echo 'unterminated"})

    assert any("Unsafe embedded runner placeholder" in error for error in embedded.validate_startup(require_secrets=True))
    assert any("Unknown runner command placeholder" in error for error in unknown.validate_startup(require_secrets=True))
    assert any("Invalid runner command template" in error for error in bad_shlex.validate_startup(require_secrets=True))


def test_startup_validation_rejects_missing_runner_executable():
    config = load_config(
        env={
            "LINEAR_API_KEY": "lin_api_secret",
            "LINEAR_TEAM_ID": "team-id",
            "LINEAR_PROJECT_NAME": "TEST",
            "RUNNER_COMMAND_TEMPLATE": "/definitely/missing/lao-agent {prompt_path}",
        }
    )

    assert config.validate_startup(require_secrets=True) == [
        "Runner executable not found on PATH: /definitely/missing/lao-agent"
    ]


def test_validate_project_workspace_rejects_relative_and_missing_paths(tmp_path):
    from linear_agent_orchestrator.config import validate_project_workspace

    relative_errors = validate_project_workspace("relative/project")
    assert any("must be an absolute path" in error for error in relative_errors)

    missing_errors = validate_project_workspace(tmp_path / "absent")
    assert missing_errors == [f"Agent project directory does not exist: {tmp_path / 'absent'}"]


def test_validate_project_workspace_rejects_non_git_directory(tmp_path):
    from linear_agent_orchestrator.config import validate_project_workspace

    errors = validate_project_workspace(tmp_path)

    assert errors == [f"Agent project directory is not a git worktree: {tmp_path}"]


def test_validate_project_workspace_requires_clean_git_worktree(tmp_path):
    import subprocess

    from linear_agent_orchestrator.config import validate_project_workspace

    subprocess.run(("git", "init", "--quiet", str(tmp_path)), check=True)
    assert validate_project_workspace(tmp_path) == []

    (tmp_path / "untracked.txt").write_text("dirty", encoding="utf-8")
    errors = validate_project_workspace(tmp_path)

    assert errors == [f"Agent project git worktree is dirty: {tmp_path}"]
