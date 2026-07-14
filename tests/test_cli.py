import json
from types import SimpleNamespace

import pytest

import linear_agent_orchestrator.cli as cli
import linear_agent_orchestrator.linear_client as linear_client_module
from linear_agent_orchestrator.cli import linear_assignee_preflight_errors, main
from linear_agent_orchestrator.config import AppConfig, LinearConfig, RunnerConfig


class StubAssigneeClient:
    def __init__(
        self,
        assignee_id="viewer-1",
        error=None,
        *,
        team_id="team-uuid",
        project_id="project-uuid",
        include_project=True,
        include_user=True,
    ):
        self.assignee_id = assignee_id
        self.error = error
        self.team_id = team_id
        self.project_id = project_id
        self.include_project = include_project
        self.include_user = include_user
        self.calls = []

    def resolve_team_uuid(self, team_id):
        self.calls.append(("resolve_team_uuid", team_id))
        return self.team_id

    def resolve_project_id(self, config):
        self.calls.append(("resolve_project_id", config.project_name or config.project_id))
        return self.project_id

    def resolve_assignee_id(self, config):
        self.calls.append(("resolve_assignee_id", config.assignee_id))
        if self.error is not None:
            raise self.error
        return self.assignee_id

    def graphql(self, query, variables):
        assert "$projectId: ID!" in query
        self.calls.append(("graphql", variables))
        project_nodes = [{"id": self.project_id}] if self.include_project else []
        user = {"id": self.assignee_id} if self.include_user else None
        return {
            "team": {"id": self.team_id, "projects": {"nodes": project_nodes}},
            "user": user,
        }


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


def test_cli_startup_check_accepts_oauth_client_credentials(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("LINEAR_AUTH_MODE", raising=False)
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    monkeypatch.setattr(cli, "linear_assignee_preflight_errors", lambda config, client=None: [])
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LINEAR_AUTH_MODE=oauth_client_credentials\n"
        "LINEAR_OAUTH_CLIENT_ID=client-id\n"
        "LINEAR_OAUTH_CLIENT_SECRET=client-secret\n"
        "LINEAR_TEAM_ID=team\n"
        "LINEAR_PROJECT_ID=project-id\n"
        "LINEAR_ASSIGNEE_ID=human-user-id\n"
        "RUNNER_COMMAND_TEMPLATE='echo {prompt_path}'\n",
        encoding="utf-8",
    )

    exit_code = main(["--env-file", str(env_file), "startup-check"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "startup_check_passed" in captured.err
    assert "oauth_client_credentials" in captured.err
    assert "client-secret" not in captured.err


def test_oauth_linear_client_factory_shares_one_process_token_provider(monkeypatch):
    config = LinearConfig(
        auth_mode="oauth_client_credentials",
        oauth_client_id="client-id",
        oauth_client_secret="client-secret",
        team_id="DEV",
        project_id="project-id",
        assignee_id="human-user-id",
    )
    provider = object()
    provider_configs = []
    client_providers = []

    def build_provider(linear_config):
        provider_configs.append(linear_config)
        return provider

    def build_client(linear_config, *, oauth_token_provider=None):
        assert linear_config is config
        client_providers.append(oauth_token_provider)
        return object()

    monkeypatch.setattr(cli.OAuthClientCredentialsTokenProvider, "from_config", build_provider)
    monkeypatch.setattr(cli.LinearClient, "from_config", build_client)

    factory = cli._linear_client_factory(config)
    factory()
    factory()

    assert provider_configs == [config]
    assert client_providers == [provider, provider]


def test_oauth_startup_check_fetches_token_and_runs_read_only_access_preflight(monkeypatch):
    team_id = "team-uuid"
    project_id = "project-uuid"
    assignee_id = "human-user-id"
    calls = []

    class Response:
        def __init__(self, payload):
            self.body = json.dumps(payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self):
            return self.body

    def fake_urlopen(request, timeout):
        calls.append(request)
        if request.full_url == linear_client_module.LINEAR_OAUTH_TOKEN_ENDPOINT:
            return Response(
                {
                    "access_token": "oauth-token",
                    "token_type": "Bearer",
                    "expires_in": 2_591_999,
                }
            )

        assert request.get_header("Authorization") == "Bearer oauth-token"
        payload = json.loads(request.data)
        if "query TeamId" in payload["query"]:
            return Response({"data": {"team": {"id": team_id}}})
        assert "query LinearAccessPreflight" in payload["query"]
        assert "$projectId: ID!" in payload["query"]
        return Response(
            {
                "data": {
                    "team": {
                        "id": team_id,
                        "projects": {"nodes": [{"id": project_id}]},
                    },
                    "user": {"id": assignee_id},
                }
            }
        )

    monkeypatch.setattr(linear_client_module, "urlopen", fake_urlopen)
    linear_config = LinearConfig(
        auth_mode="oauth_client_credentials",
        oauth_client_id="client-id",
        oauth_client_secret="client-secret",
        team_id="DEV",
        project_id=project_id,
        assignee_id=assignee_id,
    )
    config = AppConfig(
        linear=linear_config,
        runner=RunnerConfig(command_template="echo {prompt_path}"),
    )

    assert cli._startup_check(
        config,
        require_secrets=True,
        client_factory=cli._linear_client_factory(linear_config),
    ) == 0
    assert [request.full_url for request in calls].count(
        linear_client_module.LINEAR_OAUTH_TOKEN_ENDPOINT
    ) == 1
    assert [request.full_url for request in calls].count(
        linear_client_module.LINEAR_GRAPHQL_ENDPOINT
    ) == 2


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
    assert [call[0] for call in client.calls] == [
        "resolve_team_uuid",
        "resolve_project_id",
        "resolve_assignee_id",
        "graphql",
    ]
    assert client.calls[-1][1] == {
        "teamId": "team-uuid",
        "projectId": "project-uuid",
        "assigneeId": "user-42",
    }


def test_linear_assignee_preflight_failure_fails_closed():
    config = AppConfig(linear=LinearConfig(api_key="key", team_id="DEV", project_name="TEST"))
    client = StubAssigneeClient(error=RuntimeError("boom"))

    errors = linear_assignee_preflight_errors(config, client=client)

    assert errors == ["Linear access preflight failed: RuntimeError: boom"]


def test_linear_assignee_preflight_empty_assignee_id_fails_closed():
    config = AppConfig(linear=LinearConfig(api_key="key", team_id="DEV", project_name="TEST"))

    errors = linear_assignee_preflight_errors(config, client=StubAssigneeClient(assignee_id=""))

    assert errors == ["Linear access preflight returned an empty assignee ID"]


def test_linear_access_preflight_fails_closed_for_inaccessible_project_or_assignee():
    config = AppConfig(
        linear=LinearConfig(
            auth_mode="oauth_client_credentials",
            oauth_client_id="client-id",
            oauth_client_secret="client-secret",
            team_id="DEV",
            project_id="project-uuid",
            assignee_id="user-42",
        )
    )

    assert linear_assignee_preflight_errors(
        config,
        client=StubAssigneeClient(assignee_id="user-42", include_project=False),
    ) == ["Linear access preflight could not access the configured project in the configured team"]
    assert linear_assignee_preflight_errors(
        config,
        client=StubAssigneeClient(assignee_id="user-42", include_user=False),
    ) == ["Linear access preflight could not access LINEAR_ASSIGNEE_ID"]


def test_create_test_issue_uses_central_oauth_validation(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("LINEAR_AUTH_MODE", raising=False)
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LINEAR_AUTH_MODE=oauth_client_credentials\n"
        "LINEAR_OAUTH_CLIENT_ID=client-id\n"
        "LINEAR_OAUTH_CLIENT_SECRET=client-secret\n"
        "LINEAR_TEAM_ID=DEV\n"
        "LINEAR_PROJECT_ID=project-id\n"
        "LINEAR_ASSIGNEE_ID=human-user-id\n",
        encoding="utf-8",
    )

    class StubCreateClient:
        def create_issue(self, config, *, title, description):
            assert config.auth_mode == "oauth_client_credentials"
            assert config.assignee_id == "human-user-id"
            return SimpleNamespace(
                id="issue-id",
                identifier="DEV-123",
                url="https://linear.example/DEV-123",
                title=title,
            )

    monkeypatch.setattr(cli.LinearClient, "from_config", lambda config: StubCreateClient())

    exit_code = main(["--env-file", str(env_file), "create-test-issue", "--json"])

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["identifier"] == "DEV-123"


def test_process_once_rejects_oauth_without_explicit_assignee_before_network(
    tmp_path,
    capsys,
    monkeypatch,
):
    monkeypatch.delenv("LINEAR_AUTH_MODE", raising=False)
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LINEAR_AUTH_MODE=oauth_client_credentials\n"
        "LINEAR_OAUTH_CLIENT_ID=client-id\n"
        "LINEAR_OAUTH_CLIENT_SECRET=client-secret\n"
        "LINEAR_TEAM_ID=DEV\n"
        "LINEAR_PROJECT_ID=project-id\n"
        "RUNNER_COMMAND_TEMPLATE='echo {prompt_path}'\n",
        encoding="utf-8",
    )

    def unexpected_call(*args, **kwargs):
        raise AssertionError("invalid OAuth config must fail before network or state access")

    monkeypatch.setattr(cli.LinearClient, "from_config", unexpected_call)
    monkeypatch.setattr(cli, "linear_assignee_preflight_errors", unexpected_call)
    monkeypatch.setattr(cli, "StateStore", unexpected_call)

    exit_code = main(
        [
            "--env-file",
            str(env_file),
            "process-once",
            "--db-path",
            str(tmp_path / "state.sqlite3"),
        ]
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "LINEAR_ASSIGNEE_ID is required" in captured.err
    assert "client-secret" not in captured.err


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
