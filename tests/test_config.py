import json
from pathlib import Path

from linear_agent_orchestrator.config import (
    DEFAULT_BACKOFF_INITIAL_SECONDS,
    DEFAULT_BACKOFF_MAX_ATTEMPTS,
    DEFAULT_BACKOFF_MAX_SECONDS,
    DEFAULT_LEASE_RENEWAL_INTERVAL_SECONDS,
    DEFAULT_LEASE_TTL_SECONDS,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_RUNNER_MAX_LIFETIME_SECONDS,
    DEFAULT_RUNNER_TIMEOUT_ENABLED,
    DEFAULT_RUNNER_TIMEOUT_SECONDS,
    LINEAR_AUTH_MODE_API_KEY,
    LINEAR_AUTH_MODE_OAUTH_CLIENT_CREDENTIALS,
    RunnerConfig,
    validate_agent_runtime,
    load_config,
    load_dotenv_file,
)


def test_config_defaults_include_first_story_constants():
    config = load_config(env={})
    assert RunnerConfig().base_ref == "main"

    assert config.state_db_path is None
    assert config.poll_interval_seconds == DEFAULT_POLL_INTERVAL_SECONDS == 60
    assert config.lease_ttl_seconds == DEFAULT_LEASE_TTL_SECONDS == 900
    assert config.lease_renewal_interval_seconds == DEFAULT_LEASE_RENEWAL_INTERVAL_SECONDS == 120
    assert config.runner.timeout_enabled is DEFAULT_RUNNER_TIMEOUT_ENABLED is True
    assert config.runner.timeout_seconds == DEFAULT_RUNNER_TIMEOUT_SECONDS == 3600
    assert config.runner.max_lifetime_seconds == DEFAULT_RUNNER_MAX_LIFETIME_SECONDS == 21600
    assert config.runner.docker_host is None
    assert config.runner.worktree_root is None
    assert config.runner.base_ref == "main"
    assert config.backoff.initial_seconds == DEFAULT_BACKOFF_INITIAL_SECONDS == 5
    assert config.backoff.max_seconds == DEFAULT_BACKOFF_MAX_SECONDS == 300
    assert config.backoff.max_attempts == DEFAULT_BACKOFF_MAX_ATTEMPTS == 5
    assert config.linear.auth_mode == LINEAR_AUTH_MODE_API_KEY
    assert config.linear.attention_state_name == "In Review"


def test_config_loads_explicit_state_db_path():
    config = load_config(env={"STATE_DB_PATH": "/tmp/lao/state.sqlite3"})

    assert config.state_db_path == "/tmp/lao/state.sqlite3"


def test_config_loads_attention_state_name_from_env():
    config = load_config(env={"LINEAR_ATTENTION_STATE_NAME": "Needs Human"})

    assert config.linear.attention_state_name == "Needs Human"


def test_config_loads_required_linear_and_runner_values_from_env_without_leaking_secret():
    config = load_config(
        env={
            "LINEAR_API_KEY": "lin_api_secret",
            "LINEAR_TEAM_ID": "team-id",
            "LINEAR_PROJECT_NAME": "TEST",
            "RUNNER_COMMAND_TEMPLATE": "echo {prompt_path}",
            "RUNNER_ALLOWED_ENV": "PATH,HOME",
            "DOCKER_HOST": "unix:///var/run/docker.sock",
            "AGENT_WORKTREE_ROOT": "/tmp/spherevla-v2-worktrees",
            "AGENT_BASE_REF": "release",
        }
    )

    assert config.validate_startup(require_secrets=True) == []
    assert config.linear.api_key == "lin_api_secret"
    assert config.linear.team_id == "team-id"
    assert config.linear.project_name == "TEST"
    assert config.runner.command_template == "echo {prompt_path}"
    assert config.runner.allowed_env == ("PATH", "HOME")
    assert config.runner.docker_host == "unix:///var/run/docker.sock"
    assert config.runner.worktree_root == "/tmp/spherevla-v2-worktrees"
    assert config.runner.base_ref == "release"
    assert config.redacted_dict()["linear"]["api_key"] == "***REDACTED***"


def test_runner_allowed_env_rejects_linear_credentials_even_without_secret_checks():
    credential_names = (
        "LINEAR_API_KEY",
        "LINEAR_OAUTH_CLIENT_ID",
        "LINEAR_OAUTH_CLIENT_SECRET",
        "LINEAR_ACCESS_TOKEN",
        "LINEAR_API_TOKEN",
        "LINEAR_OAUTH_ACCESS_TOKEN",
        "LINEAR_OAUTH_TOKEN",
        "LINEAR_TOKEN",
    )

    for credential_name in credential_names:
        config = load_config(
            env={"RUNNER_ALLOWED_ENV": f"PATH,{credential_name.lower()},HOME"}
        )

        assert config.validate_startup(require_secrets=False) == [
            "RUNNER_ALLOWED_ENV must not include Linear credential variables: "
            + credential_name
        ]


def test_runner_allowed_env_reports_only_sorted_credential_names_not_values():
    secret = "never-print-this-linear-secret"
    config = load_config(
        env={
            "LINEAR_API_KEY": secret,
            "LINEAR_OAUTH_ACCESS_TOKEN": secret,
            "RUNNER_ALLOWED_ENV": "LINEAR_OAUTH_ACCESS_TOKEN,PATH,LINEAR_API_KEY",
        }
    )

    errors = config.validate_startup(require_secrets=False)

    assert errors == [
        "RUNNER_ALLOWED_ENV must not include Linear credential variables: "
        "LINEAR_API_KEY, LINEAR_OAUTH_ACCESS_TOKEN"
    ]
    assert secret not in "\n".join(errors)


def test_config_loads_oauth_client_credentials_and_redacts_only_the_secret():
    config = load_config(
        env={
            "LINEAR_AUTH_MODE": "OAUTH_CLIENT_CREDENTIALS",
            "LINEAR_OAUTH_CLIENT_ID": "oauth-client-id",
            "LINEAR_OAUTH_CLIENT_SECRET": "oauth-client-secret",
            "LINEAR_TEAM_ID": "team-id",
            "LINEAR_PROJECT_ID": "project-id",
            "LINEAR_ASSIGNEE_ID": "human-user-id",
            "RUNNER_COMMAND_TEMPLATE": "echo {prompt_path}",
        }
    )

    assert config.validate_startup(require_secrets=True) == []
    assert config.linear.auth_mode == LINEAR_AUTH_MODE_OAUTH_CLIENT_CREDENTIALS
    assert config.linear.oauth_client_id == "oauth-client-id"
    assert config.linear.oauth_client_secret == "oauth-client-secret"
    redacted = config.redacted_dict()["linear"]
    assert redacted["oauth_client_id"] == "oauth-client-id"
    assert redacted["oauth_client_secret"] == "***REDACTED***"
    assert "oauth-client-secret" not in json.dumps(config.redacted_dict())
    assert "oauth-client-secret" not in repr(config)


def test_oauth_validation_requires_client_credentials_and_explicit_human_assignee():
    complete = {
        "LINEAR_AUTH_MODE": "oauth_client_credentials",
        "LINEAR_OAUTH_CLIENT_ID": "client-id",
        "LINEAR_OAUTH_CLIENT_SECRET": "client-secret",
        "LINEAR_TEAM_ID": "team-id",
        "LINEAR_PROJECT_NAME": "TEST",
        "LINEAR_ASSIGNEE_ID": "human-user-id",
        "RUNNER_COMMAND_TEMPLATE": "echo {prompt_path}",
    }
    required = {
        "LINEAR_OAUTH_CLIENT_ID": "LINEAR_OAUTH_CLIENT_ID is required",
        "LINEAR_OAUTH_CLIENT_SECRET": "LINEAR_OAUTH_CLIENT_SECRET is required",
        "LINEAR_ASSIGNEE_ID": "LINEAR_ASSIGNEE_ID is required",
    }

    for key, expected in required.items():
        config = load_config(env={name: value for name, value in complete.items() if name != key})
        assert any(expected in error for error in config.validate_startup(require_secrets=True))

    missing_assignee = load_config(
        env={
            name: value
            for name, value in complete.items()
            if name != "LINEAR_ASSIGNEE_ID"
        }
    )
    assert missing_assignee.validate_startup(require_secrets=False) == [
        "LINEAR_ASSIGNEE_ID is required for "
        "LINEAR_AUTH_MODE=oauth_client_credentials"
    ]


def test_linear_auth_validation_rejects_unsupported_mode_and_mixed_credentials():
    unsupported = load_config(env={"LINEAR_AUTH_MODE": "magic"})
    assert unsupported.validate_startup(require_secrets=False) == [
        "LINEAR_AUTH_MODE must be one of: api_key, oauth_client_credentials"
    ]

    mixed = load_config(
        env={
            "LINEAR_AUTH_MODE": "oauth_client_credentials",
            "LINEAR_API_KEY": "api-secret",
            "LINEAR_OAUTH_CLIENT_ID": "client-id",
            "LINEAR_OAUTH_CLIENT_SECRET": "client-secret",
            "LINEAR_ASSIGNEE_ID": "human-user-id",
        }
    )
    assert mixed.validate_startup(require_secrets=False) == [
        "LINEAR_API_KEY cannot be combined with LINEAR_OAUTH_CLIENT_ID or "
        "LINEAR_OAUTH_CLIENT_SECRET"
    ]

    wrong_mode = load_config(
        env={
            "LINEAR_OAUTH_CLIENT_ID": "client-id",
            "LINEAR_OAUTH_CLIENT_SECRET": "client-secret",
        }
    )
    assert wrong_mode.validate_startup(require_secrets=False) == [
        "LINEAR_OAUTH_CLIENT_ID and LINEAR_OAUTH_CLIENT_SECRET require "
        "LINEAR_AUTH_MODE=oauth_client_credentials"
    ]


def test_runner_max_lifetime_loads_from_env_and_rejects_non_positive():
    custom = load_config(env={"RUNNER_MAX_LIFETIME_SECONDS": "7200"})
    assert custom.runner.max_lifetime_seconds == 7200
    assert not any("runner_max_lifetime_seconds" in error for error in custom.validate_startup(require_secrets=False))

    for invalid in ("0", "-1"):
        config = load_config(env={"RUNNER_MAX_LIFETIME_SECONDS": invalid})
        errors = config.validate_startup(require_secrets=False)
        assert "runner_max_lifetime_seconds must be positive" in errors


def test_runner_timeout_enabled_loads_from_env_and_defaults_on():
    assert load_config(env={}).runner.timeout_enabled is True
    assert load_config(env={"RUNNER_TIMEOUT_ENABLED": "false"}).runner.timeout_enabled is False
    assert load_config(env={"RUNNER_TIMEOUT_ENABLED": "true"}).runner.timeout_enabled is True
    assert load_config(env={"RUNNER_TIMEOUT_ENABLED": "treu"}).runner.timeout_enabled is True


def test_runner_timeout_switch_is_wired_to_compose_and_bootstrap():
    root = Path(__file__).parents[1]
    compose = (root / "compose.yaml").read_text(encoding="utf-8")
    bootstrap = (root / "scripts" / "bootstrap.sh").read_text(encoding="utf-8")

    assert "RUNNER_TIMEOUT_ENABLED: ${RUNNER_TIMEOUT_ENABLED:-true}" in compose
    assert "ensure_env_default RUNNER_TIMEOUT_ENABLED true" in bootstrap


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


def test_empty_process_auth_mode_allows_oauth_env_file_and_missing_mode_defaults_to_api_key(
    tmp_path: Path,
):
    env_file = tmp_path / ".env.oauth"
    env_file.write_text(
        "LINEAR_AUTH_MODE=oauth_client_credentials\n",
        encoding="utf-8",
    )

    oauth_config = load_config(env={"LINEAR_AUTH_MODE": ""}, env_files=[env_file])
    default_config = load_config(env={"LINEAR_AUTH_MODE": ""})

    assert oauth_config.linear.auth_mode == LINEAR_AUTH_MODE_OAUTH_CLIENT_CREDENTIALS
    assert default_config.linear.auth_mode == LINEAR_AUTH_MODE_API_KEY


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


def test_dotenv_parser_preserves_unquoted_multiword_values(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LINEAR_IN_PROGRESS_STATE_NAME=In Progress\n"
        "RUNNER_COMMAND_TEMPLATE=echo hello world\n",
        encoding="utf-8",
    )

    assert load_dotenv_file(env_file) == {
        "LINEAR_IN_PROGRESS_STATE_NAME": "In Progress",
        "RUNNER_COMMAND_TEMPLATE": "echo hello world",
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


def test_docker_host_runtime_requires_allowlist_and_cli(tmp_path, monkeypatch):
    import subprocess

    import linear_agent_orchestrator.config as config_module

    subprocess.run(("git", "init", "--quiet", str(tmp_path)), check=True)
    real_which = config_module.shutil.which
    monkeypatch.setattr(
        config_module.shutil,
        "which",
        lambda tool: None if tool == "docker" else real_which(tool),
    )
    runner = RunnerConfig(
        project_dir=str(tmp_path),
        backend="codex",
        docker_host="unix:///var/run/docker.sock",
        worktree_isolation=False,
    )

    errors = validate_agent_runtime(runner, require_auth=False)

    assert "Docker host mode requires DOCKER_HOST in RUNNER_ALLOWED_ENV" in errors
    assert "Docker host mode requires the Docker CLI on PATH" in errors


def test_docker_host_runtime_checks_daemon_compose_and_buildx(tmp_path, monkeypatch):
    import subprocess

    import linear_agent_orchestrator.config as config_module

    subprocess.run(("git", "init", "--quiet", str(tmp_path)), check=True)
    monkeypatch.setattr(config_module.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    calls = []

    def fake_preflight(command):
        if command[0] == "git":
            return 0, "true\n", ""
        calls.append(command)
        if command[:2] == ("docker", "version"):
            return 1, "", "permission denied"
        return 0, "", ""

    monkeypatch.setattr(config_module, "_run_preflight_command", fake_preflight)
    runner = RunnerConfig(
        project_dir=str(tmp_path),
        backend="codex",
        allowed_env=("DOCKER_HOST",),
        docker_host="unix:///var/run/docker.sock",
    )

    errors = validate_agent_runtime(runner, require_auth=False)

    assert errors == [
        "Docker host daemon check failed: permission denied",
    ]
    assert ("docker", "compose", "version") in calls
    assert ("docker", "buildx", "version") in calls
