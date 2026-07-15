"""Linear GraphQL client, polling queries, workflow-state cache, and comments.

G002 implements mocked-testable Linear API boundaries only. It does not claim
leases, execute runners, or run live end-to-end mutations.

Implementation notes from Linear docs:
- The GraphQL endpoint is https://api.linear.app/graphql.
- API keys are sent in the Authorization header.
- GraphQL responses can include an `errors` array even with HTTP 200.
- Rate limits surface as GraphQL errors with extension code RATELIMITED.
- List responses use Relay cursor pagination with `first` / `after`; `updatedAt`
  ordering is supported for recently updated resources.
"""

from __future__ import annotations

from base64 import b64encode
from dataclasses import dataclass, field as dataclass_field
from enum import Enum
from http.client import HTTPException
import json
from math import isfinite
from threading import Lock
import time
from typing import Any, Callable, Mapping, Protocol, runtime_checkable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import (
    LINEAR_AUTH_MODE_API_KEY,
    LINEAR_AUTH_MODE_OAUTH_CLIENT_CREDENTIALS,
    LinearConfig,
)
from .issue_schema import IssueSchemaResult, clarification_comment, validate_issue_body

LINEAR_GRAPHQL_ENDPOINT = "https://api.linear.app/graphql"
LINEAR_OAUTH_TOKEN_ENDPOINT = "https://api.linear.app/oauth/token"
LINEAR_OAUTH_SCOPES = "read,write"
OAUTH_TOKEN_REFRESH_SKEW_SECONDS = 300
DEFAULT_PAGE_SIZE = 50


class LinearError(Exception):
    """Base Linear client exception."""


class LinearAuthenticationError(LinearError):
    """Raised for missing, invalid, or rejected Linear credentials."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.transient = False


class LinearGraphQLError(LinearError):
    """Raised when Linear returns GraphQL errors or malformed payloads."""

    def __init__(self, message: str, errors: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.errors = errors or []
        self.codes = tuple(
            str(error.get("extensions", {}).get("code"))
            for error in self.errors
            if isinstance(error, dict) and error.get("extensions", {}).get("code")
        )

    @property
    def is_rate_limited(self) -> bool:
        return "RATELIMITED" in self.codes

    @property
    def is_transient(self) -> bool:
        transient_codes = {"RATELIMITED", "INTERNAL_SERVER_ERROR", "SERVICE_UNAVAILABLE", "TIMEOUT"}
        return any(code in transient_codes for code in self.codes)


class LinearTransportError(LinearError):
    """Raised for HTTP/network transport failures."""

    def __init__(self, message: str, *, status_code: int | None = None, transient: bool = False) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.transient = transient


class LinearAuth(Protocol):
    """Authentication strategy for Linear GraphQL requests."""

    def headers(self) -> dict[str, str]:
        """Return auth headers without exposing secrets to logs."""
        ...


@dataclass(frozen=True)
class ApiKeyAuth:
    """Personal API key or app key auth using Linear's Authorization header."""

    api_key: str = dataclass_field(repr=False)

    def headers(self) -> dict[str, str]:
        if not self.api_key:
            raise LinearAuthenticationError("LINEAR_API_KEY is required for API key auth")
        return {"Authorization": self.api_key}


@dataclass(frozen=True)
class OAuthTokenAuth:
    """Future-ready OAuth bearer token auth abstraction."""

    access_token: str = dataclass_field(repr=False)

    def headers(self) -> dict[str, str]:
        if not self.access_token:
            raise LinearAuthenticationError("OAuth access token is required")
        return {"Authorization": f"Bearer {self.access_token}"}


class OAuthClientCredentialsTokenProvider:
    """Thread-safe, in-memory Linear client-credentials token cache."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        timeout_seconds: int = 30,
        refresh_skew_seconds: int = OAUTH_TOKEN_REFRESH_SKEW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not client_id:
            raise LinearAuthenticationError("LINEAR_OAUTH_CLIENT_ID is required")
        if not client_secret:
            raise LinearAuthenticationError("LINEAR_OAUTH_CLIENT_SECRET is required")
        if timeout_seconds <= 0:
            raise ValueError("OAuth token timeout_seconds must be positive")
        if refresh_skew_seconds < 0:
            raise ValueError("OAuth token refresh_skew_seconds must not be negative")
        self._client_id = client_id
        self._client_secret = client_secret
        self._timeout_seconds = timeout_seconds
        self._refresh_skew_seconds = refresh_skew_seconds
        self._clock = clock
        self._lock = Lock()
        self._access_token: str | None = None
        self._expires_at = 0.0

    @classmethod
    def from_config(cls, config: LinearConfig) -> "OAuthClientCredentialsTokenProvider":
        """Build a provider from a validated OAuth Linear configuration."""

        if not config.oauth_client_id:
            raise LinearAuthenticationError("LINEAR_OAUTH_CLIENT_ID is required")
        if not config.oauth_client_secret:
            raise LinearAuthenticationError("LINEAR_OAUTH_CLIENT_SECRET is required")
        return cls(config.oauth_client_id, config.oauth_client_secret)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(client_id=<redacted>, client_secret=<redacted>, "
            f"timeout_seconds={self._timeout_seconds}, "
            f"refresh_skew_seconds={self._refresh_skew_seconds})"
        )

    def get_access_token(self) -> str:
        """Return a cached token, fetching one when absent or near expiry."""

        with self._lock:
            now = self._clock()
            if self._access_token and now < self._expires_at - self._refresh_skew_seconds:
                return self._access_token
            access_token, expires_in = self._request_access_token()
            self._access_token = access_token
            self._expires_at = self._clock() + expires_in
            return access_token

    def invalidate(self, access_token: str | None = None) -> None:
        """Discard the cache, conditionally when a rejected token is supplied."""

        with self._lock:
            if access_token is not None and access_token != self._access_token:
                return
            self._access_token = None
            self._expires_at = 0.0

    def _request_access_token(self) -> tuple[str, float]:
        credentials = b64encode(f"{self._client_id}:{self._client_secret}".encode("utf-8")).decode("ascii")
        body = urlencode(
            {
                "grant_type": "client_credentials",
                "scope": LINEAR_OAUTH_SCOPES,
            }
        ).encode("ascii")
        request = Request(
            LINEAR_OAUTH_TOKEN_ENDPOINT,
            data=body,
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:  # noqa: S310 - fixed Linear endpoint
                response_body = response.read().decode("utf-8")
        except HTTPError as exc:
            if exc.fp:
                exc.close()
            if exc.code in {408, 429} or 500 <= exc.code < 600:
                raise LinearTransportError(
                    f"Linear OAuth token HTTP error {exc.code}",
                    status_code=exc.code,
                    transient=True,
                ) from exc
            raise LinearAuthenticationError(
                f"Linear OAuth token request rejected with HTTP {exc.code}",
                status_code=exc.code,
            ) from exc
        except URLError as exc:
            raise LinearTransportError("Linear OAuth token transport error", transient=True) from exc
        except TimeoutError as exc:
            raise LinearTransportError("Linear OAuth token transport timeout", transient=True) from exc
        except (OSError, HTTPException) as exc:
            raise LinearTransportError("Linear OAuth token transport error", transient=True) from exc
        except UnicodeDecodeError as exc:
            raise LinearAuthenticationError("Linear OAuth token response was not valid UTF-8") from exc

        try:
            payload = json.loads(response_body)
        except json.JSONDecodeError as exc:
            raise LinearAuthenticationError("Linear OAuth token response was not valid JSON") from exc
        if not isinstance(payload, dict):
            raise LinearAuthenticationError("Linear OAuth token response JSON was not an object")

        access_token = payload.get("access_token")
        token_type = payload.get("token_type")
        expires_in = payload.get("expires_in")
        if not isinstance(access_token, str) or not access_token.strip():
            raise LinearAuthenticationError("Linear OAuth token response missing access_token")
        if not isinstance(token_type, str) or token_type.casefold() != "bearer":
            raise LinearAuthenticationError("Linear OAuth token response has unsupported token_type")
        if (
            isinstance(expires_in, bool)
            or not isinstance(expires_in, (int, float))
            or not isfinite(expires_in)
            or expires_in <= 0
        ):
            raise LinearAuthenticationError("Linear OAuth token response has invalid expires_in")
        return access_token.strip(), float(expires_in)


@dataclass(frozen=True)
class OAuthClientCredentialsAuth:
    """Bearer auth backed by a refreshable client-credentials token provider."""

    token_provider: OAuthClientCredentialsTokenProvider = dataclass_field(repr=False)

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token_provider.get_access_token()}"}

    def invalidate(self, access_token: str | None = None) -> None:
        self.token_provider.invalidate(access_token)


@runtime_checkable
class RefreshableLinearAuth(LinearAuth, Protocol):
    """Auth strategy whose rejected access token can be invalidated."""

    def invalidate(self, access_token: str | None = None) -> None:
        """Invalidate a rejected access token."""
        ...


class GraphQLTransport(Protocol):
    """Transport protocol used by tests and the production urllib transport."""

    def execute(self, query: str, variables: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Execute a GraphQL document and return decoded JSON payload."""
        ...


def _read_graphql_response_body(response: Any, *, status_code: int | None = None) -> str:
    """Read a GraphQL response, classifying interrupted reads as transient."""

    try:
        return response.read().decode("utf-8")
    except (OSError, HTTPException) as exc:
        raise LinearTransportError(
            "Linear GraphQL response read error",
            status_code=status_code,
            transient=True,
        ) from exc


class UrllibGraphQLTransport:
    """Small stdlib-only JSON GraphQL transport."""

    def __init__(self, endpoint: str, auth: LinearAuth, timeout_seconds: int = 30) -> None:
        self.endpoint = endpoint
        self.auth = auth
        self.timeout_seconds = timeout_seconds

    def execute(self, query: str, variables: Mapping[str, Any] | None = None) -> dict[str, Any]:
        body = json.dumps({"query": query, "variables": dict(variables or {})}).encode("utf-8")
        refreshable_auth = self.auth if isinstance(self.auth, RefreshableLinearAuth) else None
        for attempt in range(2):
            auth_headers = self.auth.headers()
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
                **auth_headers,
            }
            request = Request(self.endpoint, data=body, headers=headers, method="POST")
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310 - configured API endpoint
                    response_body = _read_graphql_response_body(response)
                    return _decode_json_payload(response_body)
            except HTTPError as exc:
                if exc.code == 401 and refreshable_auth is not None:
                    if attempt == 0:
                        refreshable_auth.invalidate(_bearer_access_token(auth_headers))
                        if exc.fp:
                            exc.close()
                        continue
                    refreshable_auth.invalidate(_bearer_access_token(auth_headers))
                    if exc.fp:
                        exc.close()
                    raise LinearAuthenticationError(
                        "Linear OAuth access token was rejected after one refresh",
                        status_code=401,
                    ) from exc

                response_body = (
                    _read_graphql_response_body(exc, status_code=exc.code)
                    if exc.fp
                    else ""
                )
                try:
                    payload = _decode_json_payload(response_body) if response_body else {}
                except LinearGraphQLError:
                    payload = {}
                if isinstance(payload.get("errors"), list):
                    raise _graphql_error(payload["errors"])
                raise LinearTransportError(
                    f"Linear HTTP error {exc.code}",
                    status_code=exc.code,
                    transient=exc.code in {408, 429} or 500 <= exc.code < 600,
                ) from exc
            except URLError as exc:
                raise LinearTransportError(f"Linear transport error: {exc.reason}", transient=True) from exc
            except TimeoutError as exc:
                raise LinearTransportError("Linear transport timeout", transient=True) from exc
            except (OSError, HTTPException) as exc:
                raise LinearTransportError("Linear transport error", transient=True) from exc
        raise AssertionError("unreachable OAuth retry state")


@dataclass(frozen=True)
class WorkflowState:
    id: str
    name: str
    type: str | None = None


@dataclass(frozen=True)
class LinearComment:
    id: str
    body: str


@dataclass(frozen=True)
class LinearIssue:
    id: str
    identifier: str
    title: str
    description: str
    url: str | None
    updated_at: str
    state_id: str | None
    state_name: str | None
    project_id: str | None
    project_name: str | None
    schema: IssueSchemaResult
    assignee_id: str | None = None
    assignee_name: str | None = None


@dataclass(frozen=True)
class PollPage:
    issues: tuple[LinearIssue, ...]
    end_cursor: str | None
    has_next_page: bool


class WorkflowRole(str, Enum):
    QUEUE = "queue"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    ATTENTION = "attention"


class WorkflowStateCache:
    """In-memory team-scoped workflow state resolver for G002."""

    def __init__(self, client: "LinearClient") -> None:
        self.client = client
        self._by_team: dict[str, dict[str, WorkflowState]] = {}

    def resolve_required(self, team_id: str, config: LinearConfig) -> dict[WorkflowRole, WorkflowState]:
        states = self._by_team.get(team_id)
        if states is None:
            states = {_state_key(state.name): state for state in self.client.list_workflow_states(team_id)}
            self._by_team[team_id] = states
        return {
            WorkflowRole.QUEUE: _lookup_state(states, config.queue_state_name, "queue/Todo"),
            WorkflowRole.IN_PROGRESS: _lookup_state(states, config.in_progress_state_name, "In Progress"),
            WorkflowRole.DONE: _lookup_state(states, config.done_state_name, "Done"),
            WorkflowRole.ATTENTION: _lookup_state(states, config.attention_state_name, "attention/In Review"),
        }

    def clear(self) -> None:
        self._by_team.clear()


class LinearClient:
    """GraphQL client methods used by the polling daemon."""

    def __init__(self, transport: GraphQLTransport) -> None:
        self.transport = transport
        self.workflow_states = WorkflowStateCache(self)
        self._viewer_id: str | None = None

    @classmethod
    def from_config(
        cls,
        config: LinearConfig,
        *,
        oauth_token_provider: OAuthClientCredentialsTokenProvider | None = None,
    ) -> "LinearClient":
        auth_errors = config.auth_validation_errors(require_secrets=True)
        if auth_errors:
            raise LinearAuthenticationError(auth_errors[0])

        if config.auth_mode == LINEAR_AUTH_MODE_API_KEY:
            if oauth_token_provider is not None:
                raise LinearAuthenticationError("OAuth token provider cannot be used with LINEAR_AUTH_MODE=api_key")
            if not config.api_key:
                raise LinearAuthenticationError("LINEAR_API_KEY is required")
            return cls(UrllibGraphQLTransport(LINEAR_GRAPHQL_ENDPOINT, ApiKeyAuth(config.api_key)))

        if config.auth_mode == LINEAR_AUTH_MODE_OAUTH_CLIENT_CREDENTIALS:
            if config.api_key:
                raise LinearAuthenticationError("LINEAR_API_KEY cannot be used with OAuth client credentials")
            provider = oauth_token_provider or OAuthClientCredentialsTokenProvider.from_config(config)
            return cls(UrllibGraphQLTransport(LINEAR_GRAPHQL_ENDPOINT, OAuthClientCredentialsAuth(provider)))

        raise LinearAuthenticationError(f"Unsupported LINEAR_AUTH_MODE: {config.auth_mode!r}")

    def graphql(self, query: str, variables: Mapping[str, Any] | None = None) -> dict[str, Any]:
        payload = self.transport.execute(query, variables)
        if not isinstance(payload, dict):
            raise LinearGraphQLError("Linear GraphQL response was not a JSON object")
        if isinstance(payload.get("errors"), list) and payload["errors"]:
            raise _graphql_error(payload["errors"])
        data = payload.get("data")
        if not isinstance(data, dict):
            raise LinearGraphQLError("Linear GraphQL response missing data object")
        return data

    def list_workflow_states(self, team_id: str) -> tuple[WorkflowState, ...]:
        data = self.graphql(WORKFLOW_STATES_QUERY, {"teamId": team_id})
        nodes = (((data.get("team") or {}).get("states") or {}).get("nodes") or [])
        return tuple(
            WorkflowState(id=str(node["id"]), name=str(node["name"]), type=node.get("type"))
            for node in nodes
            if isinstance(node, dict) and node.get("id") and node.get("name")
        )

    def resolve_workflow_states(self, config: LinearConfig) -> dict[WorkflowRole, WorkflowState]:
        if not config.team_id:
            raise ValueError("LINEAR_TEAM_ID is required to resolve workflow states")
        return self.workflow_states.resolve_required(config.team_id, config)

    def resolve_viewer_id(self) -> str:
        """Resolve and cache the authenticated Linear viewer ID."""

        if self._viewer_id is not None:
            return self._viewer_id
        data = self.graphql(VIEWER_QUERY)
        viewer = data.get("viewer") or {}
        viewer_id = viewer.get("id") if isinstance(viewer, dict) else None
        if not viewer_id:
            raise LinearGraphQLError("Could not resolve authenticated Linear viewer")
        self._viewer_id = str(viewer_id)
        return self._viewer_id

    def resolve_assignee_id(self, config: LinearConfig) -> str:
        """Return the explicitly configured assignee or the API viewer."""

        return config.assignee_id or self.resolve_viewer_id()

    def poll_todo_issues(
        self,
        config: LinearConfig,
        *,
        after: str | None = None,
        first: int = DEFAULT_PAGE_SIZE,
    ) -> PollPage:
        """Poll configured project Todo issues ordered by updatedAt using cursor pagination."""

        if not config.team_id:
            raise ValueError("LINEAR_TEAM_ID is required to poll issues")
        if not (config.project_id or config.project_name):
            raise ValueError("LINEAR_PROJECT_ID or LINEAR_PROJECT_NAME is required to poll issues")
        states = self.resolve_workflow_states(config)
        assignee_id = self.resolve_assignee_id(config)
        project_filter = {"id": {"eq": config.project_id}} if config.project_id else {"name": {"eq": config.project_name}}
        data = self.graphql(
            POLL_TODO_ISSUES_QUERY,
            {
                "teamId": config.team_id,
                "filter": {
                    "state": {"id": {"eq": states[WorkflowRole.QUEUE].id}},
                    "project": project_filter,
                    "assignee": {"id": {"eq": assignee_id}},
                },
                "first": first,
                "after": after,
            },
        )
        connection = ((data.get("team") or {}).get("issues") or {})
        page_info = connection.get("pageInfo") or {}
        nodes = connection.get("nodes") or []
        return PollPage(
            issues=tuple(_issue_from_node(node) for node in nodes if isinstance(node, dict)),
            end_cursor=page_info.get("endCursor"),
            has_next_page=bool(page_info.get("hasNextPage")),
        )

    def iter_todo_issues(self, config: LinearConfig, *, first: int = DEFAULT_PAGE_SIZE) -> tuple[LinearIssue, ...]:
        """Fetch all currently visible Todo pages for the configured project."""

        all_issues: list[LinearIssue] = []
        after: str | None = None
        while True:
            page = self.poll_todo_issues(config, after=after, first=first)
            all_issues.extend(page.issues)
            if not page.has_next_page:
                return tuple(all_issues)
            after = page.end_cursor


    def list_issue_comments(self, issue_id: str, *, first: int = DEFAULT_PAGE_SIZE) -> tuple[LinearComment, ...]:
        """List all comments for an issue via Linear Issue.comments pagination."""

        comments: list[LinearComment] = []
        after: str | None = None
        while True:
            data = self.graphql(ISSUE_COMMENTS_QUERY, {"issueId": issue_id, "first": first, "after": after})
            connection = ((data.get("issue") or {}).get("comments") or {})
            page_info = connection.get("pageInfo") or {}
            nodes = connection.get("nodes") or []
            comments.extend(
                LinearComment(id=str(node["id"]), body=str(node.get("body") or ""))
                for node in nodes
                if isinstance(node, dict) and node.get("id")
            )
            if not page_info.get("hasNextPage"):
                return tuple(comments)
            after = page_info.get("endCursor")

    def create_comment(self, issue_id: str, body: str) -> str | None:
        """Create a Linear comment and return its ID when present."""

        data = self.graphql(COMMENT_CREATE_MUTATION, {"issueId": issue_id, "body": body})
        payload = data.get("commentCreate") or {}
        if payload.get("success") is not True:
            raise LinearGraphQLError("Linear commentCreate returned success=false")
        comment = payload.get("comment") or {}
        return comment.get("id")

    def comment_for_schema_missing(self, issue: LinearIssue) -> str | None:
        """Add a clarification comment for invalid issue schema without executing work."""

        if issue.schema.valid:
            return None
        return self.create_comment(issue.id, clarification_comment(issue.identifier, issue.schema))

    def create_progress_comment(self, issue_id: str, body: str) -> str | None:
        """Create a progress comment. Kept separate for orchestration readability."""

        return self.create_comment(issue_id, body)

    def create_result_comment(self, issue_id: str, body: str) -> str | None:
        """Create a result/evidence comment. Kept separate for orchestration readability."""

        return self.create_comment(issue_id, body)

    def update_issue_state(self, issue_id: str, state_id: str) -> bool:
        """Update an issue state via Linear's issueUpdate mutation."""

        data = self.graphql(ISSUE_UPDATE_STATE_MUTATION, {"issueId": issue_id, "stateId": state_id})
        payload = data.get("issueUpdate") or {}
        if payload.get("success") is not True:
            raise LinearGraphQLError("Linear issueUpdate returned success=false")
        return True

    def resolve_team_uuid(self, team_id_or_key: str) -> str:
        """Resolve Linear team UUID from configured team ID/key."""

        data = self.graphql(TEAM_ID_QUERY, {"teamId": team_id_or_key})
        team = data.get("team") or {}
        if not team.get("id"):
            raise LinearGraphQLError(f"Could not resolve Linear team {team_id_or_key!r}")
        return str(team["id"])

    def resolve_project_id(self, config: LinearConfig) -> str | None:
        """Resolve configured project ID by exact project name when needed."""

        if config.project_id:
            return config.project_id
        if not config.project_name:
            return None
        if not config.team_id:
            raise ValueError("LINEAR_TEAM_ID is required to resolve project by name")
        data = self.graphql(PROJECT_BY_NAME_QUERY, {"teamId": config.team_id, "projectName": config.project_name})
        nodes = (((data.get("team") or {}).get("projects") or {}).get("nodes") or [])
        if not nodes:
            raise LinearGraphQLError(f"Could not resolve Linear project named {config.project_name!r}")
        return str(nodes[0]["id"])

    def create_issue(self, config: LinearConfig, *, title: str, description: str) -> LinearIssue:
        """Create a Linear issue in the configured team/project for live smoke tests."""

        if not config.team_id:
            raise ValueError("LINEAR_TEAM_ID is required to create issues")
        project_id = self.resolve_project_id(config)
        states = self.resolve_workflow_states(config)
        team_uuid = self.resolve_team_uuid(config.team_id)
        input_value = {
            "teamId": team_uuid,
            "title": title,
            "description": description,
            "stateId": states[WorkflowRole.QUEUE].id,
            "assigneeId": self.resolve_assignee_id(config),
        }
        if project_id:
            input_value["projectId"] = project_id
        data = self.graphql(ISSUE_CREATE_MUTATION, {"input": input_value})
        payload = data.get("issueCreate") or {}
        if payload.get("success") is not True:
            raise LinearGraphQLError("Linear issueCreate returned success=false")
        issue = payload.get("issue") or {}
        return _issue_from_node(issue)


def _issue_from_node(node: Mapping[str, Any]) -> LinearIssue:
    state = node.get("state") or {}
    project = node.get("project") or {}
    assignee = node.get("assignee") or {}
    description = node.get("description") or ""
    return LinearIssue(
        id=str(node["id"]),
        identifier=str(node.get("identifier") or node["id"]),
        title=str(node.get("title") or ""),
        description=str(description),
        url=node.get("url"),
        updated_at=str(node.get("updatedAt") or ""),
        state_id=state.get("id") if isinstance(state, dict) else None,
        state_name=state.get("name") if isinstance(state, dict) else None,
        project_id=project.get("id") if isinstance(project, dict) else None,
        project_name=project.get("name") if isinstance(project, dict) else None,
        schema=validate_issue_body(str(description)),
        assignee_id=assignee.get("id") if isinstance(assignee, dict) else None,
        assignee_name=assignee.get("name") if isinstance(assignee, dict) else None,
    )


def _lookup_state(states: Mapping[str, WorkflowState], name: str, role: str) -> WorkflowState:
    state = states.get(_state_key(name))
    if not state:
        available = ", ".join(sorted(state.name for state in states.values()))
        raise LinearGraphQLError(f"Could not resolve {role} workflow state named {name!r}; available states: {available}")
    return state


def _state_key(name: str) -> str:
    return name.strip().casefold()


def _decode_json_payload(response_body: str) -> dict[str, Any]:
    try:
        payload = json.loads(response_body)
    except json.JSONDecodeError as exc:
        raise LinearGraphQLError("Linear response was not valid JSON") from exc
    if not isinstance(payload, dict):
        raise LinearGraphQLError("Linear response JSON was not an object")
    return payload


def _bearer_access_token(headers: Mapping[str, str]) -> str | None:
    authorization = headers.get("Authorization")
    if not authorization or not authorization.startswith("Bearer "):
        return None
    access_token = authorization[len("Bearer ") :]
    return access_token or None


def _graphql_error(errors: list[dict[str, Any]]) -> LinearGraphQLError:
    messages = [str(error.get("message", "GraphQL error")) for error in errors if isinstance(error, dict)]
    prefix = "Linear GraphQL rate limited" if any((e.get("extensions") or {}).get("code") == "RATELIMITED" for e in errors if isinstance(e, dict)) else "Linear GraphQL error"
    return LinearGraphQLError(f"{prefix}: {'; '.join(messages) if messages else 'unknown error'}", errors)


WORKFLOW_STATES_QUERY = """
query WorkflowStates($teamId: String!) {
  team(id: $teamId) {
    id
    states {
      nodes {
        id
        name
        type
      }
    }
  }
}
"""

VIEWER_QUERY = """
query Viewer {
  viewer { id name }
}
"""

POLL_TODO_ISSUES_QUERY = """
query PollTodoIssues($teamId: String!, $filter: IssueFilter, $first: Int!, $after: String) {
  team(id: $teamId) {
    id
    issues(
      first: $first
      after: $after
      orderBy: updatedAt
      filter: $filter
    ) {
      nodes {
        id
        identifier
        title
        description
        url
        updatedAt
        state { id name type }
        project { id name }
        assignee { id name }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

ISSUE_COMMENTS_QUERY = """
query IssueComments($issueId: String!, $first: Int!, $after: String) {
  issue(id: $issueId) {
    id
    comments(first: $first, after: $after) {
      nodes { id body }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

COMMENT_CREATE_MUTATION = """
mutation CommentCreate($issueId: String!, $body: String!) {
  commentCreate(input: { issueId: $issueId, body: $body }) {
    success
    comment { id }
  }
}
"""

ISSUE_UPDATE_STATE_MUTATION = """
mutation IssueUpdateState($issueId: String!, $stateId: String!) {
  issueUpdate(id: $issueId, input: { stateId: $stateId }) {
    success
    issue { id state { id name type } }
  }
}
"""

PROJECT_BY_NAME_QUERY = """
query ProjectByName($teamId: String!, $projectName: String!) {
  team(id: $teamId) {
    id
    projects(filter: { name: { eq: $projectName } }) {
      nodes { id name }
    }
  }
}
"""

ISSUE_CREATE_MUTATION = """
mutation IssueCreate($input: IssueCreateInput!) {
  issueCreate(input: $input) {
    success
    issue {
      id
      identifier
      title
      description
      url
      updatedAt
      state { id name type }
      project { id name }
      assignee { id name }
    }
  }
}
"""

TEAM_ID_QUERY = """
query TeamId($teamId: String!) {
  team(id: $teamId) { id key name }
}
"""
