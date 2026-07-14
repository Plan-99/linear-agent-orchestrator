import json

import pytest

import linear_agent_orchestrator.cli as cli
from linear_agent_orchestrator.cli import linear_assignee_preflight_errors, main
from linear_agent_orchestrator.config import AppConfig, LinearConfig


class StubAssigneeClient:
    def __init__(self, assignee_id="viewer-1", error=None):
        self.assignee_id = assignee_id
        self.error = error
        self.calls = 0

    def resolve_assignee_id(self, config):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.assignee_id


def test_cli_check_config_allows_scaffold_smoke_without_secrets(capsys):
    exit_code = main(["check-config", "--allow-missing-secrets", "--json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["errors"] == []
    assert payload["config"]["poll_interval_seconds"] == 60


def test_cli_startup_check_with_complete_env_file(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli, "linear_assignee_preflight_errors", lambda config, client=None: [])
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LINEAR_API_KEY=secret\n"
        "LINEAR_TEAM_ID=team\n"
        "LINEAR_PROJECT_NAME=TEST\n"
        "RUNNER_COMMAND_TEMPLATE='echo {prompt_path}'\n",
        encoding="utf-8",
    )

    exit_code = main(["--env-file", str(env_file), "startup-check"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "startup_check_passed" in captured.err
    assert "secret" not in captured.err


def test_cli_startup_check_fails_when_assignee_preflight_fails(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(
        cli,
        "linear_assignee_preflight_errors",
        lambda config, client=None: ["Linear assignee preflight failed: RuntimeError: boom"],
    )
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LINEAR_API_KEY=secret\n"
        "LINEAR_TEAM_ID=team\n"
        "LINEAR_PROJECT_NAME=TEST\n"
        "RUNNER_COMMAND_TEMPLATE='echo {prompt_path}'\n",
        encoding="utf-8",
    )

    exit_code = main(["--env-file", str(env_file), "startup-check"])

    assert exit_code == 1
    assert "Linear assignee preflight failed" in capsys.readouterr().err


def test_cli_startup_check_allow_missing_secrets_skips_assignee_preflight_offline(capsys, monkeypatch):
    def _must_not_call(config, client=None):
        raise AssertionError("assignee preflight must not run with --allow-missing-secrets")

    monkeypatch.setattr(cli, "linear_assignee_preflight_errors", _must_not_call)

    exit_code = main(["startup-check", "--allow-missing-secrets"])

    assert exit_code == 0
    assert "startup_check_passed" in capsys.readouterr().err


def test_linear_assignee_preflight_success_with_injected_client():
    config = AppConfig(linear=LinearConfig(api_key="key", team_id="DEV", project_name="TEST"))
    client = StubAssigneeClient(assignee_id="user-42")

    assert linear_assignee_preflight_errors(config, client=client) == []
    assert client.calls == 1


def test_linear_assignee_preflight_failure_fails_closed():
    config = AppConfig(linear=LinearConfig(api_key="key", team_id="DEV", project_name="TEST"))
    client = StubAssigneeClient(error=RuntimeError("boom"))

    errors = linear_assignee_preflight_errors(config, client=client)

    assert errors == ["Linear assignee preflight failed: RuntimeError: boom"]


def test_linear_assignee_preflight_empty_assignee_id_fails_closed():
    config = AppConfig(linear=LinearConfig(api_key="key", team_id="DEV", project_name="TEST"))

    errors = linear_assignee_preflight_errors(config, client=StubAssigneeClient(assignee_id=""))

    assert errors == ["Linear assignee preflight returned an empty assignee ID"]


def test_cli_daemon_exit_after_startup_is_safe_placeholder(tmp_path):
    db_path = tmp_path / "state.sqlite3"

    exit_code = main(
        [
            "daemon",
            "--allow-missing-secrets",
            "--exit-after-startup",
            "--db-path",
            str(db_path),
        ]
    )

    assert exit_code == 0
    assert db_path.exists()


@pytest.mark.parametrize(
    "argv",
    [
        ["daemon", "--allow-missing-secrets", "--exit-after-startup"],
        ["state-init"],
        ["process-once"],
    ],
)
def test_stateful_commands_require_explicit_database_path(argv, monkeypatch, capsys):
    monkeypatch.delenv("STATE_DB_PATH", raising=False)

    def unexpected_call(*args, **kwargs):
        raise AssertionError("missing database path must fail before state or network access")

    monkeypatch.setattr(cli, "StateStore", unexpected_call)
    monkeypatch.setattr(cli.LinearClient, "from_config", unexpected_call)
    monkeypatch.setattr(cli, "linear_assignee_preflight_errors", unexpected_call)

    exit_code = main(argv)

    assert exit_code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        f"{argv[0]}: state database path is required; pass --db-path PATH, "
        "set STATE_DB_PATH, or run via ./scripts/compose.sh\n"
    )


def test_state_init_db_path_flag_overrides_environment(tmp_path, monkeypatch, capsys):
    environment_db = tmp_path / "from-environment.sqlite3"
    flag_db = tmp_path / "from-flag.sqlite3"
    monkeypatch.setenv("STATE_DB_PATH", str(environment_db))

    exit_code = main(["state-init", "--db-path", str(flag_db), "--json"])

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["db_path"] == str(flag_db)
    assert flag_db.exists()
    assert not environment_db.exists()


def test_state_init_uses_environment_database_path(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "from-environment.sqlite3"
    monkeypatch.setenv("STATE_DB_PATH", str(db_path))

    exit_code = main(["state-init", "--json"])

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["db_path"] == str(db_path)
    assert db_path.exists()


def test_state_init_uses_env_file_database_path(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "from-env-file.sqlite3"
    env_file = tmp_path / ".env"
    env_file.write_text(f"STATE_DB_PATH={db_path}\n", encoding="utf-8")
    monkeypatch.delenv("STATE_DB_PATH", raising=False)

    exit_code = main(["--env-file", str(env_file), "state-init", "--json"])

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["db_path"] == str(db_path)
    assert db_path.exists()
