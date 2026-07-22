from base64 import b64decode
from http.client import IncompleteRead
from io import BytesIO
import json
from threading import Barrier, Lock, Thread
from urllib.error import HTTPError
from urllib.parse import parse_qs

import pytest

import linear_agent_orchestrator.linear_client as linear_client_module
from linear_agent_orchestrator.config import LinearConfig
from linear_agent_orchestrator.linear_client import (
    ApiKeyAuth,
    COMMENT_CREATE_MUTATION,
    LINEAR_GRAPHQL_ENDPOINT,
    LINEAR_OAUTH_TOKEN_ENDPOINT,
    LinearClient,
    LinearAuthenticationError,
    LinearGraphQLError,
    LinearTransportError,
    OAuthClientCredentialsAuth,
    OAuthClientCredentialsTokenProvider,
    UrllibGraphQLTransport,
    WorkflowRole,
)


class MockTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def execute(self, query, variables=None):
        self.calls.append({"query": query, "variables": dict(variables or {})})
        if not self.responses:
            raise AssertionError("No mock response queued")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class UrlopenResponse:
    def __init__(self, payload):
        if isinstance(payload, bytes):
            self.body = payload
        else:
            self.body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return self.body


def oauth_token_response(token="oauth-token", *, expires_in=600):
    return {
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": expires_in,
        "scope": "read write",
    }


def http_error(url, status, body=b""):
    return HTTPError(url, status, "request failed", {}, BytesIO(body))


def test_oauth_client_credentials_request_uses_fixed_endpoint_basic_auth_and_fixed_scope(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout):
        calls.append((request, timeout))
        return UrlopenResponse(oauth_token_response())

    monkeypatch.setattr(linear_client_module, "urlopen", fake_urlopen)
    provider = OAuthClientCredentialsTokenProvider("client-id", "client-secret")

    assert provider.get_access_token() == "oauth-token"
    assert len(calls) == 1
    request, timeout = calls[0]
    headers = {name.casefold(): value for name, value in request.header_items()}
    assert request.full_url == LINEAR_OAUTH_TOKEN_ENDPOINT
    assert request.get_method() == "POST"
    assert timeout == 30
    assert headers["content-type"] == "application/x-www-form-urlencoded"
    assert headers["accept"] == "application/json"
    basic_value = headers["authorization"].removeprefix("Basic ")
    assert b64decode(basic_value).decode("utf-8") == "client-id:client-secret"
    assert parse_qs(request.data.decode("ascii")) == {
        "grant_type": ["client_credentials"],
        "scope": ["read,write"],
    }


def test_oauth_token_cache_refreshes_at_five_minute_skew_and_supports_conditional_invalidation(monkeypatch):
    now = [1_000.0]
    responses = [oauth_token_response("token-1"), oauth_token_response("token-2"), oauth_token_response("token-3")]
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(request)
        return UrlopenResponse(responses.pop(0))

    monkeypatch.setattr(linear_client_module, "urlopen", fake_urlopen)
    provider = OAuthClientCredentialsTokenProvider("client-id", "client-secret", clock=lambda: now[0])

    assert provider.get_access_token() == "token-1"
    now[0] = 1_299.0
    assert provider.get_access_token() == "token-1"
    now[0] = 1_300.0
    assert provider.get_access_token() == "token-2"
    provider.invalidate("token-1")
    assert provider.get_access_token() == "token-2"
    provider.invalidate("token-2")
    assert provider.get_access_token() == "token-3"
    assert len(calls) == 3


def test_oauth_token_cache_is_shared_safely_across_threads(monkeypatch):
    opener_lock = Lock()
    opener_calls = 0

    def fake_urlopen(request, timeout):
        nonlocal opener_calls
        with opener_lock:
            opener_calls += 1
        return UrlopenResponse(oauth_token_response("shared-token"))

    monkeypatch.setattr(linear_client_module, "urlopen", fake_urlopen)
    provider = OAuthClientCredentialsTokenProvider("client-id", "client-secret")
    barrier = Barrier(8)
    results = []

    def get_token():
        barrier.wait()
        results.append(provider.get_access_token())

    threads = [Thread(target=get_token) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results == ["shared-token"] * 8
    assert opener_calls == 1


def test_oauth_graphql_401_invalidates_exact_rejected_token_and_retries_once(monkeypatch):
    token_responses = [oauth_token_response("token-1"), oauth_token_response("token-2")]
    graphql_authorizations = []

    def fake_urlopen(request, timeout):
        if request.full_url == LINEAR_OAUTH_TOKEN_ENDPOINT:
            return UrlopenResponse(token_responses.pop(0))
        graphql_authorizations.append(request.get_header("Authorization"))
        if len(graphql_authorizations) == 1:
            raise http_error(request.full_url, 401)
        return UrlopenResponse({"data": {"viewer": {"id": "viewer-1"}}})

    monkeypatch.setattr(linear_client_module, "urlopen", fake_urlopen)
    provider = OAuthClientCredentialsTokenProvider("client-id", "client-secret")
    transport = UrllibGraphQLTransport(LINEAR_GRAPHQL_ENDPOINT, OAuthClientCredentialsAuth(provider))

    assert transport.execute("query { viewer { id } }") == {"data": {"viewer": {"id": "viewer-1"}}}
    assert graphql_authorizations == ["Bearer token-1", "Bearer token-2"]
    assert token_responses == []


def test_late_oauth_401_for_old_token_does_not_invalidate_newer_cached_token(monkeypatch):
    token_responses = [oauth_token_response("token-1"), oauth_token_response("token-2")]

    def fake_urlopen(request, timeout):
        return UrlopenResponse(token_responses.pop(0))

    monkeypatch.setattr(linear_client_module, "urlopen", fake_urlopen)
    provider = OAuthClientCredentialsTokenProvider("client-id", "client-secret")

    assert provider.get_access_token() == "token-1"
    provider.invalidate("token-1")
    assert provider.get_access_token() == "token-2"
    provider.invalidate("token-1")
    assert provider.get_access_token() == "token-2"
    assert token_responses == []


def test_oauth_graphql_second_401_fails_without_a_third_attempt(monkeypatch):
    token_responses = [
        oauth_token_response("token-1"),
        oauth_token_response("token-2"),
        oauth_token_response("token-3"),
    ]
    graphql_calls = []

    def fake_urlopen(request, timeout):
        if request.full_url == LINEAR_OAUTH_TOKEN_ENDPOINT:
            return UrlopenResponse(token_responses.pop(0))
        graphql_calls.append(request)
        raise http_error(request.full_url, 401)

    monkeypatch.setattr(linear_client_module, "urlopen", fake_urlopen)
    provider = OAuthClientCredentialsTokenProvider("client-id", "client-secret")
    transport = UrllibGraphQLTransport(LINEAR_GRAPHQL_ENDPOINT, OAuthClientCredentialsAuth(provider))

    with pytest.raises(LinearAuthenticationError, match="after one refresh") as exc_info:
        transport.execute("query { viewer { id } }")

    assert exc_info.value.status_code == 401
    assert exc_info.value.transient is False
    assert len(graphql_calls) == 2
    assert provider.get_access_token() == "token-3"
    assert token_responses == []


def test_api_key_graphql_401_is_not_refreshed_or_retried(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(request)
        raise http_error(request.full_url, 401)

    monkeypatch.setattr(linear_client_module, "urlopen", fake_urlopen)
    transport = UrllibGraphQLTransport(LINEAR_GRAPHQL_ENDPOINT, ApiKeyAuth("api-key"))

    with pytest.raises(LinearTransportError) as exc_info:
        transport.execute("query { viewer { id } }")

    assert exc_info.value.status_code == 401
    assert len(calls) == 1


@pytest.mark.parametrize("status", [408, 429, 500, 503])
def test_graphql_transient_http_errors_are_classified_for_backoff(monkeypatch, status):
    monkeypatch.setattr(
        linear_client_module,
        "urlopen",
        lambda request, timeout: (_ for _ in ()).throw(http_error(request.full_url, status)),
    )
    transport = UrllibGraphQLTransport(LINEAR_GRAPHQL_ENDPOINT, ApiKeyAuth("api-key"))

    with pytest.raises(LinearTransportError) as exc_info:
        transport.execute("query { viewer { id } }")

    assert exc_info.value.status_code == status
    assert exc_info.value.transient is True


@pytest.mark.parametrize(
    "read_error",
    [ConnectionResetError("connection reset"), IncompleteRead(b"partial")],
)
@pytest.mark.parametrize("http_error_response", [False, True])
def test_graphql_response_read_network_errors_are_transient(
    monkeypatch,
    read_error,
    http_error_response,
):
    class FailingReadResponse(UrlopenResponse):
        def read(self):
            raise read_error

    class FailingReadStream(BytesIO):
        def read(self, *args, **kwargs):
            raise read_error

    def fake_urlopen(request, timeout):
        if http_error_response:
            raise HTTPError(
                request.full_url,
                503,
                "request failed",
                {},
                FailingReadStream(),
            )
        return FailingReadResponse(b"")

    monkeypatch.setattr(linear_client_module, "urlopen", fake_urlopen)
    transport = UrllibGraphQLTransport(LINEAR_GRAPHQL_ENDPOINT, ApiKeyAuth("api-key"))

    with pytest.raises(LinearTransportError) as exc_info:
        transport.execute("query { viewer { id } }")

    assert exc_info.value.status_code == (503 if http_error_response else None)
    assert exc_info.value.transient is True


@pytest.mark.parametrize("status", [408, 429, 500, 503])
def test_oauth_token_transient_http_errors_are_classified_for_backoff(monkeypatch, status):
    monkeypatch.setattr(
        linear_client_module,
        "urlopen",
        lambda request, timeout: (_ for _ in ()).throw(http_error(request.full_url, status)),
    )
    provider = OAuthClientCredentialsTokenProvider("client-id", "client-secret")

    with pytest.raises(LinearTransportError) as exc_info:
        provider.get_access_token()

    assert exc_info.value.status_code == status
    assert exc_info.value.transient is True


@pytest.mark.parametrize(
    "read_error",
    [ConnectionResetError("connection reset"), IncompleteRead(b"partial")],
)
def test_oauth_token_response_read_network_errors_are_transient(monkeypatch, read_error):
    class FailingReadResponse(UrlopenResponse):
        def read(self):
            raise read_error

    monkeypatch.setattr(
        linear_client_module,
        "urlopen",
        lambda request, timeout: FailingReadResponse(b""),
    )
    provider = OAuthClientCredentialsTokenProvider("client-id", "client-secret")

    with pytest.raises(LinearTransportError) as exc_info:
        provider.get_access_token()

    assert exc_info.value.transient is True


def test_oauth_token_auth_errors_and_object_reprs_do_not_leak_credentials(monkeypatch):
    client_id = "sensitive-client-id"
    client_secret = "sensitive-client-secret"
    error_body = json.dumps({"error_description": client_secret}).encode("utf-8")

    def fake_urlopen(request, timeout):
        raise http_error(request.full_url, 400, error_body)

    monkeypatch.setattr(linear_client_module, "urlopen", fake_urlopen)
    provider = OAuthClientCredentialsTokenProvider(client_id, client_secret)
    auth = OAuthClientCredentialsAuth(provider)

    with pytest.raises(LinearAuthenticationError) as exc_info:
        provider.get_access_token()

    assert exc_info.value.status_code == 400
    assert exc_info.value.transient is False
    for rendered in (repr(provider), repr(auth), str(exc_info.value)):
        assert client_id not in rendered
        assert client_secret not in rendered


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        {"token_type": "Bearer", "expires_in": 600},
        {"access_token": "token", "token_type": "MAC", "expires_in": 600},
        {"access_token": "token", "token_type": "Bearer", "expires_in": 0},
        {"access_token": "token", "token_type": "Bearer", "expires_in": float("nan")},
    ],
)
def test_oauth_token_malformed_success_payloads_fail_closed(monkeypatch, payload):
    monkeypatch.setattr(linear_client_module, "urlopen", lambda request, timeout: UrlopenResponse(payload))
    provider = OAuthClientCredentialsTokenProvider("client-id", "client-secret")

    with pytest.raises(LinearAuthenticationError):
        provider.get_access_token()


def test_linear_client_from_config_preserves_api_key_and_accepts_shared_oauth_provider():
    api_client = LinearClient.from_config(LinearConfig(api_key="api-key"))
    assert isinstance(api_client.transport, UrllibGraphQLTransport)
    assert isinstance(api_client.transport.auth, ApiKeyAuth)

    oauth_config = LinearConfig(
        auth_mode="oauth_client_credentials",
        oauth_client_id="client-id",
        oauth_client_secret="client-secret",
        assignee_id="user-1",
    )
    shared_provider = OAuthClientCredentialsTokenProvider.from_config(oauth_config)
    oauth_client = LinearClient.from_config(oauth_config, oauth_token_provider=shared_provider)
    assert isinstance(oauth_client.transport, UrllibGraphQLTransport)
    assert isinstance(oauth_client.transport.auth, OAuthClientCredentialsAuth)
    assert oauth_client.transport.auth.token_provider is shared_provider


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        (
            LinearConfig(
                api_key="api-key",
                oauth_client_id="client-id",
                oauth_client_secret="client-secret",
            ),
            "cannot be combined",
        ),
        (
            LinearConfig(
                auth_mode="oauth_client_credentials",
                oauth_client_id="client-id",
                oauth_client_secret="client-secret",
            ),
            "LINEAR_ASSIGNEE_ID is required",
        ),
        (
            LinearConfig(
                auth_mode="oauth_client_credentials",
                oauth_client_id="client-id",
                assignee_id="user-1",
            ),
            "LINEAR_OAUTH_CLIENT_SECRET is required",
        ),
    ],
)
def test_linear_client_from_config_enforces_auth_invariants(config, expected):
    with pytest.raises(LinearAuthenticationError, match=expected):
        LinearClient.from_config(config)


def workflow_response():
    return {
        "data": {
            "team": {
                "states": {
                    "nodes": [
                        {"id": "state-backlog", "name": "Backlog", "type": "backlog"},
                        {"id": "state-todo", "name": "Todo", "type": "unstarted"},
                        {"id": "state-started", "name": "In Progress", "type": "started"},
                        {"id": "state-done", "name": "Done", "type": "completed"},
                        {"id": "state-review", "name": "In Review", "type": "started"},
                    ]
                }
            }
        }
    }


def issue_node(identifier="DEV-1", description=None):
    return {
        "id": f"issue-{identifier}",
        "identifier": identifier,
        "title": "Test issue",
        "description": description
        or "## Task\nDo it\n\n## Acceptance Criteria\n- Done\n\n## Validation\n- pytest -q",
        "url": "https://linear.app/test/issue/DEV-1",
        "updatedAt": "2026-07-09T13:00:00.000Z",
        "state": {"id": "state-todo", "name": "Todo", "type": "unstarted"},
        "project": {"id": "project-test", "name": "TEST"},
    }


def viewer_response(viewer_id="viewer-1", name="Viewer"):
    return {"data": {"viewer": {"id": viewer_id, "name": name}}}


def poll_response(nodes, has_next=False, end_cursor=None):
    return {
        "data": {
            "team": {
                "issues": {
                    "nodes": nodes,
                    "pageInfo": {"hasNextPage": has_next, "endCursor": end_cursor},
                }
            }
        }
    }


def test_api_key_auth_uses_authorization_header_without_bearer_prefix():
    assert ApiKeyAuth("secret").headers() == {"Authorization": "secret"}


def test_graphql_errors_array_raises_even_when_data_present_and_marks_rate_limited():
    transport = MockTransport(
        [
            {
                "data": {"viewer": None},
                "errors": [{"message": "slow down", "extensions": {"code": "RATELIMITED"}}],
            }
        ]
    )
    client = LinearClient(transport)

    with pytest.raises(LinearGraphQLError) as exc_info:
        client.graphql("query { viewer { id } }")

    assert exc_info.value.is_rate_limited is True
    assert exc_info.value.is_transient is True


def test_resolves_and_caches_team_workflow_state_ids():
    transport = MockTransport([workflow_response()])
    client = LinearClient(transport)
    config = LinearConfig(team_id="DEV", project_name="TEST")

    first = client.resolve_workflow_states(config)
    second = client.resolve_workflow_states(config)

    assert first[WorkflowRole.QUEUE].id == "state-todo"
    assert first[WorkflowRole.IN_PROGRESS].id == "state-started"
    assert first[WorkflowRole.DONE].id == "state-done"
    assert first[WorkflowRole.ATTENTION].id == "state-review"
    assert second[WorkflowRole.QUEUE].id == "state-todo"
    assert len(transport.calls) == 1
    assert "states" in transport.calls[0]["query"]


def test_missing_attention_workflow_state_fails_required_state_resolution():
    response = workflow_response()
    response["data"]["team"]["states"]["nodes"] = [
        state for state in response["data"]["team"]["states"]["nodes"] if state["name"] != "In Review"
    ]
    client = LinearClient(MockTransport([response]))

    with pytest.raises(LinearGraphQLError, match="attention/In Review workflow state named 'In Review'"):
        client.resolve_workflow_states(LinearConfig(team_id="DEV", project_name="TEST"))


def test_poll_todo_issues_filters_project_state_and_viewer_assignee_orders_by_updated_at():
    transport = MockTransport([workflow_response(), viewer_response(), poll_response([issue_node()])])
    client = LinearClient(transport)
    config = LinearConfig(team_id="DEV", project_name="TEST")

    page = client.poll_todo_issues(config, first=25)

    assert len(page.issues) == 1
    assert page.issues[0].schema.valid is True
    assert "viewer" in transport.calls[1]["query"]
    poll_call = transport.calls[2]
    assert "orderBy: updatedAt" in poll_call["query"]
    assert poll_call["variables"]["teamId"] == "DEV"
    assert poll_call["variables"]["first"] == 25
    assert poll_call["variables"]["filter"] == {
        "state": {"id": {"eq": "state-todo"}},
        "project": {"name": {"eq": "TEST"}},
        "assignee": {"id": {"eq": "viewer-1"}},
    }


def test_poll_todo_issues_uses_configured_assignee_without_viewer_query():
    transport = MockTransport([workflow_response(), poll_response([issue_node()])])
    client = LinearClient(transport)
    config = LinearConfig(team_id="DEV", project_name="TEST", assignee_id="user-42")

    client.poll_todo_issues(config)

    assert all("viewer" not in call["query"] for call in transport.calls)
    assert transport.calls[1]["variables"]["filter"]["assignee"] == {"id": {"eq": "user-42"}}


def test_viewer_fallback_is_cached_across_polls():
    transport = MockTransport(
        [
            workflow_response(),
            viewer_response(),
            poll_response([issue_node()]),
            poll_response([issue_node("DEV-2")]),
        ]
    )
    client = LinearClient(transport)
    config = LinearConfig(team_id="DEV", project_name="TEST")

    client.poll_todo_issues(config)
    client.poll_todo_issues(config)

    viewer_calls = [call for call in transport.calls if "viewer" in call["query"]]
    assert len(viewer_calls) == 1
    assert transport.calls[3]["variables"]["filter"]["assignee"] == {"id": {"eq": "viewer-1"}}


def test_viewer_resolution_failure_raises_before_polling():
    transport = MockTransport([workflow_response(), {"data": {"viewer": {}}}])
    client = LinearClient(transport)
    config = LinearConfig(team_id="DEV", project_name="TEST")

    with pytest.raises(LinearGraphQLError, match="viewer"):
        client.poll_todo_issues(config)

    assert len(transport.calls) == 2


def test_iter_todo_issues_uses_cursor_pagination():
    transport = MockTransport(
        [
            workflow_response(),
            viewer_response(),
            poll_response([issue_node("DEV-1")], has_next=True, end_cursor="cursor-1"),
            poll_response([issue_node("DEV-2")], has_next=False, end_cursor="cursor-2"),
        ]
    )
    client = LinearClient(transport)
    config = LinearConfig(team_id="DEV", project_id="project-test")

    issues = client.iter_todo_issues(config, first=1)

    assert [issue.identifier for issue in issues] == ["DEV-1", "DEV-2"]
    assert transport.calls[2]["variables"]["after"] is None
    assert transport.calls[3]["variables"]["after"] == "cursor-1"
    assert transport.calls[2]["variables"]["filter"]["project"] == {"id": {"eq": "project-test"}}


def test_schema_missing_issue_gets_clarification_comment_without_execution_path():
    invalid = issue_node("DEV-3", "## Task\nDo something")
    transport = MockTransport(
        [
            workflow_response(),
            viewer_response(),
            poll_response([invalid]),
            {"data": {"commentCreate": {"success": True, "comment": {"id": "comment-1"}}}},
        ]
    )
    client = LinearClient(transport)
    config = LinearConfig(team_id="DEV", project_name="TEST")

    issue = client.poll_todo_issues(config).issues[0]
    comment_id = client.comment_for_schema_missing(issue)

    assert issue.schema.valid is False
    assert comment_id == "comment-1"
    comment_call = transport.calls[3]
    assert COMMENT_CREATE_MUTATION.strip().splitlines()[0] in comment_call["query"]
    assert comment_call["variables"]["issueId"] == "issue-DEV-3"
    assert "Missing required section: Acceptance Criteria" in comment_call["variables"]["body"]
    assert "Missing required section: Validation" in comment_call["variables"]["body"]
    assert "will not execute a runner" in comment_call["variables"]["body"]


def test_valid_issue_does_not_get_clarification_comment():
    transport = MockTransport([])
    client = LinearClient(transport)
    issue = LinearClient(MockTransport([workflow_response(), viewer_response(), poll_response([issue_node()])])).poll_todo_issues(
        LinearConfig(team_id="DEV", project_name="TEST")
    ).issues[0]

    assert client.comment_for_schema_missing(issue) is None
    assert transport.calls == []


def test_update_issue_state_uses_issue_update_mutation():
    transport = MockTransport([
        {"data": {"issueUpdate": {"success": True, "issue": {"id": "issue-1", "state": {"id": "state-done"}}}}}
    ])
    client = LinearClient(transport)

    assert client.update_issue_state("issue-1", "state-done") is True
    assert "issueUpdate" in transport.calls[0]["query"]
    assert transport.calls[0]["variables"] == {"issueId": "issue-1", "stateId": "state-done"}


def test_create_issue_resolves_project_name_viewer_assignee_and_uses_issue_create_mutation():
    transport = MockTransport([
        {"data": {"team": {"projects": {"nodes": [{"id": "project-test", "name": "TEST"}]}}}},
        workflow_response(),
        {"data": {"team": {"id": "team-uuid", "key": "DEV", "name": "Development"}}},
        viewer_response(),
        {"data": {"issueCreate": {"success": True, "issue": issue_node("DEV-99")}}},
    ])
    client = LinearClient(transport)

    issue = client.create_issue(
        LinearConfig(team_id="DEV", project_name="TEST"),
        title="Smoke",
        description="Body",
        state_name="Backlog",
    )

    assert issue.identifier == "DEV-99"
    assert "projects" in transport.calls[0]["query"]
    assert "states" in transport.calls[1]["query"]
    assert "team" in transport.calls[2]["query"]
    assert "viewer" in transport.calls[3]["query"]
    assert transport.calls[4]["variables"]["input"] == {
        "teamId": "team-uuid",
        "title": "Smoke",
        "description": "Body",
        "stateId": "state-backlog",
        "assigneeId": "viewer-1",
        "projectId": "project-test",
    }
    assert "issueCreate" in transport.calls[4]["query"]


def test_create_issue_uses_configured_assignee_without_viewer_query():
    transport = MockTransport([
        {"data": {"team": {"projects": {"nodes": [{"id": "project-test", "name": "TEST"}]}}}},
        workflow_response(),
        {"data": {"team": {"id": "team-uuid", "key": "DEV", "name": "Development"}}},
        {"data": {"issueCreate": {"success": True, "issue": issue_node("DEV-100")}}},
    ])
    client = LinearClient(transport)
    config = LinearConfig(team_id="DEV", project_name="TEST", assignee_id="user-42")

    issue = client.create_issue(config, title="Smoke", description="Body")

    assert issue.identifier == "DEV-100"
    assert all("viewer" not in call["query"] for call in transport.calls)
    assert transport.calls[3]["variables"]["input"]["assigneeId"] == "user-42"


def comments_response(nodes, has_next=False, end_cursor=None):
    return {
        "data": {
            "issue": {
                "comments": {
                    "nodes": nodes,
                    "pageInfo": {"hasNextPage": has_next, "endCursor": end_cursor},
                }
            }
        }
    }


def test_list_issue_comments_uses_issue_comments_pagination():
    transport = MockTransport([
        comments_response([{"id": "comment-1", "body": "first"}], has_next=True, end_cursor="cursor-1"),
        comments_response([{"id": "comment-2", "body": "second"}], has_next=False),
    ])
    client = LinearClient(transport)

    comments = client.list_issue_comments("issue-1", first=1)

    assert [(comment.id, comment.body) for comment in comments] == [("comment-1", "first"), ("comment-2", "second")]
    assert "comments" in transport.calls[0]["query"]
    assert transport.calls[0]["variables"] == {"issueId": "issue-1", "first": 1, "after": None}
    assert transport.calls[1]["variables"]["after"] == "cursor-1"


def test_create_issue_includes_project_milestone_when_requested():
    transport = MockTransport([
        {"data": {"team": {"projects": {"nodes": [{"id": "project-test", "name": "TEST"}]}}}},
        workflow_response(),
        {"data": {"team": {"id": "team-uuid", "key": "DEV", "name": "Development"}}},
        {"data": {"issueCreate": {"success": True, "issue": issue_node("DEV-101")}}},
    ])
    client = LinearClient(transport)
    config = LinearConfig(team_id="DEV", project_name="TEST", assignee_id="user-42")

    issue = client.create_issue(
        config,
        title="Smoke",
        description="Body",
        state_name="Backlog",
        project_milestone_id="milestone-9",
    )

    assert issue.identifier == "DEV-101"
    assert transport.calls[3]["variables"]["input"]["projectMilestoneId"] == "milestone-9"


def _milestones_response():
    return {
        "data": {
            "project": {
                "id": "project-1",
                "name": "TEST",
                "projectMilestones": {
                    "nodes": [
                        {
                            "id": "m-1",
                            "name": "Stage 1",
                            "description": "perception",
                            "targetDate": "2026-08-01",
                            "sortOrder": 1.0,
                        },
                        {
                            "id": "m-2",
                            "name": "Stage 2",
                            "description": None,
                            "targetDate": None,
                            "sortOrder": 2.0,
                        },
                        {"name": "node-without-id-is-skipped"},
                    ]
                },
            }
        }
    }


def test_list_project_milestones_parses_and_skips_invalid_nodes():
    transport = MockTransport([_milestones_response()])
    client = LinearClient(transport)

    milestones = client.list_project_milestones("project-1")

    assert milestones == [
        {
            "id": "m-1",
            "name": "Stage 1",
            "description": "perception",
            "target_date": "2026-08-01",
            "sort_order": 1.0,
        },
        {
            "id": "m-2",
            "name": "Stage 2",
            "description": None,
            "target_date": None,
            "sort_order": 2.0,
        },
    ]
    assert "projectMilestones" in transport.calls[0]["query"]
    assert transport.calls[0]["variables"] == {"projectId": "project-1"}


def test_resolve_project_milestone_id_matches_name_case_insensitively():
    transport = MockTransport([_milestones_response()])
    client = LinearClient(transport)
    config = LinearConfig(team_id="DEV", project_id="project-1")

    assert client.resolve_project_milestone_id(config, "  stage 1 ") == "m-1"


def test_resolve_project_milestone_id_fails_closed_on_unknown_name():
    transport = MockTransport([_milestones_response()])
    client = LinearClient(transport)
    config = LinearConfig(team_id="DEV", project_id="project-1")

    with pytest.raises(LinearGraphQLError, match="available: Stage 1, Stage 2"):
        client.resolve_project_milestone_id(config, "Ghost")
