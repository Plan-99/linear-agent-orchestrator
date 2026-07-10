import pytest

from linear_agent_orchestrator.config import LinearConfig
from linear_agent_orchestrator.linear_client import (
    ApiKeyAuth,
    COMMENT_CREATE_MUTATION,
    LinearClient,
    LinearGraphQLError,
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


def workflow_response():
    return {
        "data": {
            "team": {
                "states": {
                    "nodes": [
                        {"id": "state-todo", "name": "Todo", "type": "unstarted"},
                        {"id": "state-started", "name": "In Progress", "type": "started"},
                        {"id": "state-done", "name": "Done", "type": "completed"},
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
    assert second[WorkflowRole.QUEUE].id == "state-todo"
    assert len(transport.calls) == 1
    assert "states" in transport.calls[0]["query"]


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

    issue = client.create_issue(LinearConfig(team_id="DEV", project_name="TEST"), title="Smoke", description="Body")

    assert issue.identifier == "DEV-99"
    assert "projects" in transport.calls[0]["query"]
    assert "states" in transport.calls[1]["query"]
    assert "team" in transport.calls[2]["query"]
    assert "viewer" in transport.calls[3]["query"]
    assert transport.calls[4]["variables"]["input"] == {
        "teamId": "team-uuid",
        "title": "Smoke",
        "description": "Body",
        "stateId": "state-todo",
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
