"""Configuration loading for the Linear Agent Orchestrator.

This module intentionally avoids runtime third-party dependencies. Values are
loaded from process environment with optional .env files for local development;
secret values are never required to be committed to source.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Mapping, MutableMapping
import json
import os
import re
import shlex
import shutil
import subprocess


DEFAULT_POLL_INTERVAL_SECONDS = 60
DEFAULT_LEASE_TTL_SECONDS = 900
DEFAULT_LEASE_RENEWAL_INTERVAL_SECONDS = 120
DEFAULT_RUNNER_TIMEOUT_ENABLED = True
DEFAULT_RUNNER_TIMEOUT_SECONDS = 3600
DEFAULT_RUNNER_MAX_LIFETIME_SECONDS = 21600
DEFAULT_BACKOFF_INITIAL_SECONDS = 5
DEFAULT_BACKOFF_MAX_SECONDS = 300
DEFAULT_BACKOFF_MAX_ATTEMPTS = 5
LINEAR_AUTH_MODE_API_KEY = "api_key"
LINEAR_AUTH_MODE_OAUTH_CLIENT_CREDENTIALS = "oauth_client_credentials"
SUPPORTED_LINEAR_AUTH_MODES = frozenset(
    {LINEAR_AUTH_MODE_API_KEY, LINEAR_AUTH_MODE_OAUTH_CLIENT_CREDENTIALS}
)
_FORBIDDEN_RUNNER_LINEAR_CREDENTIAL_ENV_NAMES = frozenset(
    {
        "LINEAR_ACCESS_TOKEN",
        "LINEAR_API_KEY",
        "LINEAR_API_TOKEN",
        "LINEAR_OAUTH_ACCESS_TOKEN",
        "LINEAR_OAUTH_CLIENT_ID",
        "LINEAR_OAUTH_CLIENT_SECRET",
        "LINEAR_OAUTH_TOKEN",
        "LINEAR_TOKEN",
    }
)
_RUNNER_PLACEHOLDERS = {
    "prompt_path",
    "context_path",
    "artifacts_dir",
    "result_path",
    "issue_id",
    "issue_identifier",
}


@dataclass(frozen=True)
class BackoffConfig:
    """Retry/backoff defaults for transient Linear or runner infrastructure errors."""

    initial_seconds: int = DEFAULT_BACKOFF_INITIAL_SECONDS
    max_seconds: int = DEFAULT_BACKOFF_MAX_SECONDS
    max_attempts: int = DEFAULT_BACKOFF_MAX_ATTEMPTS


@dataclass(frozen=True)
class LinearConfig:
    """Linear API and queue targeting configuration."""

    auth_mode: str = LINEAR_AUTH_MODE_API_KEY
    api_key: str | None = field(default=None, repr=False)
    oauth_client_id: str | None = None
    oauth_client_secret: str | None = field(default=None, repr=False)
    team_id: str | None = None
    project_name: str | None = None
    project_id: str | None = None
    queue_state_name: str = "Todo"
    in_progress_state_name: str = "In Progress"
    done_state_name: str = "Done"
    attention_state_name: str = "In Review"
    assignee_id: str | None = None

    def auth_validation_errors(self, *, require_secrets: bool = True) -> list[str]:
        """Validate one unambiguous Linear authentication configuration."""

        errors: list[str] = []
        if self.auth_mode not in SUPPORTED_LINEAR_AUTH_MODES:
            supported = ", ".join(sorted(SUPPORTED_LINEAR_AUTH_MODES))
            errors.append(f"LINEAR_AUTH_MODE must be one of: {supported}")
            return errors

        has_oauth_credential = bool(self.oauth_client_id or self.oauth_client_secret)
        if self.api_key and has_oauth_credential:
            errors.append(
                "LINEAR_API_KEY cannot be combined with LINEAR_OAUTH_CLIENT_ID or "
                "LINEAR_OAUTH_CLIENT_SECRET"
            )
        elif self.auth_mode == LINEAR_AUTH_MODE_API_KEY and has_oauth_credential:
            errors.append(
                "LINEAR_OAUTH_CLIENT_ID and LINEAR_OAUTH_CLIENT_SECRET require "
                "LINEAR_AUTH_MODE=oauth_client_credentials"
            )
        elif self.auth_mode == LINEAR_AUTH_MODE_OAUTH_CLIENT_CREDENTIALS and self.api_key:
            errors.append(
                "LINEAR_API_KEY requires LINEAR_AUTH_MODE=api_key and cannot be used in OAuth mode"
            )

        if (
            self.auth_mode == LINEAR_AUTH_MODE_OAUTH_CLIENT_CREDENTIALS
            and not self.assignee_id
        ):
            errors.append(
                "LINEAR_ASSIGNEE_ID is required for "
                "LINEAR_AUTH_MODE=oauth_client_credentials"
            )

        if require_secrets:
            if self.auth_mode == LINEAR_AUTH_MODE_API_KEY:
                if not self.api_key:
                    errors.append("LINEAR_API_KEY is required")
            else:
                if not self.oauth_client_id:
                    errors.append(
                        "LINEAR_OAUTH_CLIENT_ID is required for "
                        "LINEAR_AUTH_MODE=oauth_client_credentials"
                    )
                if not self.oauth_client_secret:
                    errors.append(
                        "LINEAR_OAUTH_CLIENT_SECRET is required for "
                        "LINEAR_AUTH_MODE=oauth_client_credentials"
                    )
        return errors

    def validation_errors(self, *, require_secrets: bool = True) -> list[str]:
        """Validate Linear authentication and its queue target."""

        errors = self.auth_validation_errors(require_secrets=require_secrets)
        if self.auth_mode not in SUPPORTED_LINEAR_AUTH_MODES:
            return errors
        if require_secrets:
            if not self.team_id:
                errors.append("LINEAR_TEAM_ID is required")
            if not (self.project_name or self.project_id):
                errors.append("LINEAR_PROJECT_NAME or LINEAR_PROJECT_ID is required")
        return errors


@dataclass(frozen=True)
class RunnerConfig:
    """Generic shell runner configuration placeholders for later stories."""

    command_template: str | None = None
    # Disable every backend execution deadline (one-shot fixed timeout plus
    # interactive idle timeout and hard lifetime cap) when false. Bounded
    # setup/cleanup operations keep their own defensive timeouts.
    timeout_enabled: bool = DEFAULT_RUNNER_TIMEOUT_ENABLED
    timeout_seconds: int = DEFAULT_RUNNER_TIMEOUT_SECONDS
    # Interactive runs treat timeout_seconds as an idle timeout: console output
    # activity keeps pushing the deadline back. This hard cap ends the run
    # regardless of activity so a chatty backend can never extend forever.
    # One-shot runs ignore it and keep the fixed timeout_seconds deadline.
    max_lifetime_seconds: int = DEFAULT_RUNNER_MAX_LIFETIME_SECONDS
    work_dir: str = "/workspace/work"
    artifacts_dir: str = "/workspace/artifacts"
    allowed_env: tuple[str, ...] = field(default_factory=tuple)
    docker_host: str | None = None
    project_dir: str = "/workspace/project"
    backend: str = "omx"
    # Git worktrees are source checkouts, not runner scratch data. None keeps
    # the legacy native default (<work_dir>/worktrees); Compose sets an
    # explicit path and Docker-host mode replaces it with a host-identical one.
    worktree_root: str | None = None
    # Stable commit-ish from which every issue branch is created. Pinning this
    # avoids accidentally stacking new work on the operator's checked-out HEAD.
    base_ref: str = "main"
    # Per-run git worktree isolation (standard lao-agent-runner wrapper only).
    # Each run works in an orchestrator-owned worktree branched from base_ref;
    # a semantically successful run is committed to a local lao/<issue> branch
    # (never pushed) and the worktree is removed.
    worktree_isolation: bool = True
    # Interactive conversation mode: host the backend's interactive TUI in the
    # per-run console instead of the one-shot exec path. By default the run ends
    # only when the TUI exits (or timeout fires); the structured result is loaded
    # afterward and the Done gate is unchanged. Requires the interactive console.
    interactive: bool = False
    # Legacy interactive behavior: close the TUI as soon as a valid structured
    # result appears. Default false leaves the operator in control of when the
    # TUI exits after reviewing final output or wrapping up the conversation.
    interactive_autoclose: bool = False
    # Interactive console (model-neutral tmux/PTY host). Default off keeps the
    # execution path byte-identical. The tmux socket lives under an ephemeral,
    # container-private path (never a bind mount, never the host tmux socket);
    # only lifecycle JSONL and the output log live under the bind-mounted log
    # root so a host supervisor can observe and attach via `docker exec`.
    interactive_console: bool = False
    console_log_dir: str = "/workspace/logs/console"
    console_socket_dir: str = "/tmp/lao-console"
    # Backend/model-neutral capability metadata: whether the hosted command
    # actually reads the PTY, so a host supervisor may honestly offer read-write
    # (operator-typing) attach. Default false — the bundled one-shot omx/codex/
    # claude adapters ignore tty input. An interactive/custom runner opts in via
    # AGENT_CONSOLE_DIRECT_INPUT=true. This never relaxes the result.json gate.
    console_direct_input: bool = False


@dataclass(frozen=True)
class AppConfig:
    """Complete application configuration for startup checks."""

    environment: str = "local"
    log_level: str = "INFO"
    log_format: str = "json"
    state_db_path: str | None = None
    # Number of concurrent issue workers inside this one daemon. Each worker
    # polls with its own Linear client/state connection and a distinct claim
    # owner id; SQLite claims prevent two workers from taking the same issue.
    # Values > 1 require per-run worktree isolation.
    max_parallel_runs: int = 1
    poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS
    lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS
    lease_renewal_interval_seconds: int = DEFAULT_LEASE_RENEWAL_INTERVAL_SECONDS
    backoff: BackoffConfig = field(default_factory=BackoffConfig)
    linear: LinearConfig = field(default_factory=LinearConfig)
    runner: RunnerConfig = field(default_factory=RunnerConfig)

    def validate_startup(self, require_secrets: bool = True) -> list[str]:
        """Return human-readable startup validation errors.

        G001 validates wiring only. Later stories will validate Linear connectivity,
        project existence, and runner execution semantics.
        """

        errors: list[str] = []
        if self.poll_interval_seconds <= 0:
            errors.append("poll_interval_seconds must be positive")
        if self.lease_ttl_seconds <= 0:
            errors.append("lease_ttl_seconds must be positive")
        if self.lease_renewal_interval_seconds <= 0:
            errors.append("lease_renewal_interval_seconds must be positive")
        if self.lease_renewal_interval_seconds >= self.lease_ttl_seconds:
            errors.append("lease_renewal_interval_seconds must be less than lease_ttl_seconds")
        if self.runner.timeout_seconds <= 0:
            errors.append("runner_timeout_seconds must be positive")
        if self.runner.max_lifetime_seconds <= 0:
            errors.append("runner_max_lifetime_seconds must be positive")
        forbidden_runner_env = sorted(
            {
                name.upper()
                for name in self.runner.allowed_env
                if name.upper() in _FORBIDDEN_RUNNER_LINEAR_CREDENTIAL_ENV_NAMES
            }
        )
        if forbidden_runner_env:
            errors.append(
                "RUNNER_ALLOWED_ENV must not include Linear credential variables: "
                + ", ".join(forbidden_runner_env)
            )
        if self.backoff.initial_seconds <= 0:
            errors.append("backoff.initial_seconds must be positive")
        if self.backoff.max_seconds < self.backoff.initial_seconds:
            errors.append("backoff.max_seconds must be >= backoff.initial_seconds")
        if self.backoff.max_attempts <= 0:
            errors.append("backoff.max_attempts must be positive")
        if self.runner.interactive and not self.runner.interactive_console:
            errors.append("AGENT_INTERACTIVE=true requires AGENT_CONSOLE=true (the TUI needs the per-run tmux console)")
        if self.max_parallel_runs <= 0:
            errors.append("MAX_PARALLEL_RUNS must be positive")
        if self.max_parallel_runs > 1 and not self.runner.worktree_isolation:
            errors.append("MAX_PARALLEL_RUNS > 1 requires AGENT_WORKTREE_ISOLATION=true (parallel runs must not share one checkout)")
        errors.extend(self.linear.validation_errors(require_secrets=require_secrets))
        if require_secrets:
            if not self.runner.command_template:
                errors.append("RUNNER_COMMAND_TEMPLATE is required")
        if self.runner.command_template:
            try:
                rendered = validate_runner_command_template(self.runner.command_template)
                if shutil.which(rendered[0]) is None:
                    errors.append(f"Runner executable not found on PATH: {rendered[0]}")
                elif Path(rendered[0]).name == "lao-agent-runner":
                    errors.extend(validate_agent_runtime(self.runner, require_auth=require_secrets))
            except ValueError as exc:
                errors.append(str(exc))
        return errors

    def redacted_dict(self) -> dict[str, object]:
        """Return a serialization-safe dict with secrets redacted."""

        data = asdict(self)
        if self.linear.api_key:
            data["linear"]["api_key"] = "***REDACTED***"  # type: ignore[index]
        if self.linear.oauth_client_secret:
            data["linear"]["oauth_client_secret"] = "***REDACTED***"  # type: ignore[index]
        return data


def validate_runner_command_template(template: str) -> list[str]:
    """Validate runner template tokenization/placeholders without executing it."""

    dummy_values = {
        "prompt_path": "/tmp/lao-startup/prompt.md",
        "context_path": "/tmp/lao-startup/context.json",
        "artifacts_dir": "/tmp/lao-startup/artifacts",
        "result_path": "/tmp/lao-startup/artifacts/result.json",
        "issue_id": "issue-startup-check",
        "issue_identifier": "DEV-STARTUP",
    }
    try:
        tokens = shlex.split(template, posix=True)
    except ValueError as exc:
        raise ValueError(f"Invalid runner command template: {exc}") from exc
    if not tokens:
        raise ValueError("RUNNER_COMMAND_TEMPLATE must not be empty")
    rendered: list[str] = []
    for token in tokens:
        placeholders = re.findall(r"{([^{}]+)}", token)
        unknown = set(placeholders) - _RUNNER_PLACEHOLDERS
        if unknown:
            raise ValueError(f"Unknown runner command placeholder(s): {', '.join(sorted(unknown))}")
        if placeholders:
            if len(placeholders) != 1 or token != "{" + placeholders[0] + "}":
                raise ValueError(f"Unsafe embedded runner placeholder token: {token!r}")
            rendered.append(dummy_values[placeholders[0]])
        else:
            rendered.append(token)
    return rendered


def load_dotenv_file(path: Path) -> dict[str, str]:
    """Parse a small dotenv file without expanding variables or shelling out.

    Supports `KEY=value`, optional `export KEY=value`, comments, blank lines,
    and shell-like quoting via `shlex.split`. Missing files are ignored.
    """

    values: dict[str, str] = {}
    if not path.exists():
        return values

    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            raise ValueError(f"Invalid dotenv line {line_number} in {path}: missing '='")
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"Invalid dotenv line {line_number} in {path}: empty key")
        try:
            parsed = shlex.split(raw_value, comments=False, posix=True)
        except ValueError as exc:
            raise ValueError(f"Invalid dotenv line {line_number} in {path}: {exc}") from exc
        # Preserve unquoted multi-word dotenv values such as
        # ``LINEAR_IN_PROGRESS_STATE_NAME=In Progress``. ``shlex`` still
        # handles quoted/escaped values, while joining prevents silently
        # truncating everything after the first token.
        values[key] = " ".join(parsed) if parsed else ""
    return values


def merged_environment(
    env: Mapping[str, str] | None = None,
    env_files: list[Path] | None = None,
) -> dict[str, str]:
    """Merge dotenv files then non-empty environment values with env precedence."""

    merged: dict[str, str] = {}
    for env_file in env_files or []:
        merged.update(load_dotenv_file(env_file))
    for key, value in dict(os.environ if env is None else env).items():
        if value != "":
            merged[key] = value
    return merged


def load_config(
    env: Mapping[str, str] | None = None,
    env_files: list[Path] | None = None,
) -> AppConfig:
    """Load application configuration from environment and optional dotenv files."""

    source = merged_environment(env=env, env_files=env_files)

    return AppConfig(
        environment=_get_str(source, "APP_ENV", "local"),
        log_level=_get_str(source, "LOG_LEVEL", "INFO").upper(),
        log_format=_get_str(source, "LOG_FORMAT", "json"),
        state_db_path=_get_optional_str(source, "STATE_DB_PATH"),
        max_parallel_runs=_get_int(source, "MAX_PARALLEL_RUNS", 1),
        poll_interval_seconds=_get_int(source, "POLL_INTERVAL_SECONDS", DEFAULT_POLL_INTERVAL_SECONDS),
        lease_ttl_seconds=_get_int(source, "LEASE_TTL_SECONDS", DEFAULT_LEASE_TTL_SECONDS),
        lease_renewal_interval_seconds=_get_int(
            source,
            "LEASE_RENEWAL_INTERVAL_SECONDS",
            DEFAULT_LEASE_RENEWAL_INTERVAL_SECONDS,
        ),
        backoff=BackoffConfig(
            initial_seconds=_get_int(source, "BACKOFF_INITIAL_SECONDS", DEFAULT_BACKOFF_INITIAL_SECONDS),
            max_seconds=_get_int(source, "BACKOFF_MAX_SECONDS", DEFAULT_BACKOFF_MAX_SECONDS),
            max_attempts=_get_int(source, "BACKOFF_MAX_ATTEMPTS", DEFAULT_BACKOFF_MAX_ATTEMPTS),
        ),
        linear=LinearConfig(
            auth_mode=_get_str(source, "LINEAR_AUTH_MODE", LINEAR_AUTH_MODE_API_KEY).strip().casefold(),
            api_key=_get_optional_str(source, "LINEAR_API_KEY"),
            oauth_client_id=_get_optional_str(source, "LINEAR_OAUTH_CLIENT_ID"),
            oauth_client_secret=_get_optional_str(source, "LINEAR_OAUTH_CLIENT_SECRET"),
            team_id=_get_optional_str(source, "LINEAR_TEAM_ID"),
            project_name=_get_optional_str(source, "LINEAR_PROJECT_NAME"),
            project_id=_get_optional_str(source, "LINEAR_PROJECT_ID"),
            queue_state_name=_get_str(source, "LINEAR_QUEUE_STATE_NAME", "Todo"),
            in_progress_state_name=_get_str(source, "LINEAR_IN_PROGRESS_STATE_NAME", "In Progress"),
            done_state_name=_get_str(source, "LINEAR_DONE_STATE_NAME", "Done"),
            attention_state_name=_get_str(source, "LINEAR_ATTENTION_STATE_NAME", "In Review"),
            assignee_id=_get_optional_str(source, "LINEAR_ASSIGNEE_ID"),
        ),
        runner=RunnerConfig(
            command_template=_get_optional_str(source, "RUNNER_COMMAND_TEMPLATE"),
            timeout_enabled=_get_bool(
                source,
                "RUNNER_TIMEOUT_ENABLED",
                DEFAULT_RUNNER_TIMEOUT_ENABLED,
            ),
            timeout_seconds=_get_int(source, "RUNNER_TIMEOUT_SECONDS", DEFAULT_RUNNER_TIMEOUT_SECONDS),
            max_lifetime_seconds=_get_int(
                source,
                "RUNNER_MAX_LIFETIME_SECONDS",
                DEFAULT_RUNNER_MAX_LIFETIME_SECONDS,
            ),
            work_dir=_get_str(source, "RUNNER_WORK_DIR", "/workspace/work"),
            artifacts_dir=_get_str(source, "RUNNER_ARTIFACTS_DIR", "/workspace/artifacts"),
            allowed_env=_get_csv_tuple(source, "RUNNER_ALLOWED_ENV"),
            docker_host=_get_optional_str(source, "DOCKER_HOST"),
            project_dir=_get_str(source, "AGENT_PROJECT_DIR", "/workspace/project"),
            backend=_get_str(source, "AGENT_BACKEND", "omx").casefold(),
            worktree_root=_get_optional_str(source, "AGENT_WORKTREE_ROOT"),
            base_ref=_get_str(source, "AGENT_BASE_REF", "main"),
            worktree_isolation=_get_bool(source, "AGENT_WORKTREE_ISOLATION", True),
            interactive=_get_bool(source, "AGENT_INTERACTIVE", False),
            interactive_autoclose=_get_bool(source, "AGENT_INTERACTIVE_AUTOCLOSE", False),
            interactive_console=_get_bool(source, "AGENT_CONSOLE", False),
            console_log_dir=_get_str(source, "AGENT_CONSOLE_LOG_DIR", "/workspace/logs/console"),
            console_socket_dir=_get_str(source, "AGENT_CONSOLE_SOCKET_DIR", "/tmp/lao-console"),
            console_direct_input=_get_bool(source, "AGENT_CONSOLE_DIRECT_INPUT", False),
        ),
    )


def apply_env_file_to_process(path: Path, environ: MutableMapping[str, str] | None = None) -> None:
    """Load env file values into a process-like mapping without overriding keys."""

    target = environ if environ is not None else os.environ
    for key, value in load_dotenv_file(path).items():
        target.setdefault(key, value)


# Recovery guidance for stale agent-home credential copies (GitHub #3): a
# host-side token rotation invalidates the dedicated copy until it is reseeded.
_CREDENTIAL_RECOVERY_HINT = "re-login on the host, then run ./scripts/bootstrap.sh --refresh-credentials"


def validate_agent_runtime(runner: RunnerConfig, *, require_auth: bool) -> list[str]:
    """Validate the fixed-workspace agent backend before any issue is claimed."""

    errors: list[str] = []
    if runner.backend not in {"omx", "codex", "claude"}:
        return [f"Unsupported AGENT_BACKEND: {runner.backend!r}"]

    project_dir = Path(runner.project_dir)
    # With per-run worktree isolation the base repository never hosts agent
    # edits directly, so a dirty base worktree (e.g. an unreviewed lao/<issue>
    # branch checkout) must not block runs; each run branches from base_ref.
    workspace_errors = validate_project_workspace(
        project_dir, require_clean=not runner.worktree_isolation
    )
    errors.extend(workspace_errors)
    errors.extend(validate_agent_worktree_settings(runner, validate_ref=not workspace_errors))

    missing_tools = [tool for tool in ("rg", "ps") if shutil.which(tool) is None]
    if missing_tools:
        errors.append(f"Missing required worker tooling: {', '.join(missing_tools)}")

    if runner.docker_host:
        if "DOCKER_HOST" not in runner.allowed_env:
            errors.append(
                "Docker host mode requires DOCKER_HOST in RUNNER_ALLOWED_ENV"
            )
        if shutil.which("docker") is None:
            errors.append("Docker host mode requires the Docker CLI on PATH")
        else:
            docker_checks = (
                ("daemon", ("docker", "version", "--format", "{{.Server.Version}}")),
                ("Compose plugin", ("docker", "compose", "version")),
                ("Buildx plugin", ("docker", "buildx", "version")),
            )
            for label, command in docker_checks:
                check = _run_preflight_command(command)
                if check[0] != 0:
                    errors.append(
                        f"Docker host {label} check failed: {_preflight_output(check)}"
                    )
    backend_tools = ("omx", "codex") if runner.backend == "omx" else (runner.backend,)
    for tool in backend_tools:
        if shutil.which(tool) is None:
            errors.append(f"Agent backend CLI not found on PATH: {tool}")

    if runner.backend == "omx" and shutil.which("omx") is not None:
        omx_list = _run_preflight_command(("omx", "list", "--json"))
        if omx_list[0] != 0:
            errors.append(f"OMX setup check failed: {_preflight_output(omx_list)}")
        else:
            try:
                catalog = json.loads(omx_list[1])
                skills = catalog.get("skills") if isinstance(catalog, dict) else None
                analyze_active = any(
                    isinstance(skill, dict) and skill.get("name") == "analyze" and skill.get("status") == "active"
                    for skill in (skills or [])
                )
                if not analyze_active:
                    errors.append("OMX setup is missing the active analyze skill")
            except json.JSONDecodeError:
                errors.append("OMX setup check returned invalid JSON")

    if require_auth and runner.backend in {"omx", "codex"} and shutil.which("codex") is not None:
        auth_check = _run_preflight_command(("codex", "login", "status"))
        if auth_check[0] != 0:
            errors.append(
                f"Codex authentication check failed: {_preflight_output(auth_check)}"
                f" (recover: {_CREDENTIAL_RECOVERY_HINT})"
            )
    if require_auth and runner.backend == "claude" and shutil.which("claude") is not None:
        auth_check = _run_preflight_command(("claude", "auth", "status", "--json"))
        if auth_check[0] != 0:
            errors.append(
                f"Claude authentication check failed: {_preflight_output(auth_check)}"
                f" (recover: {_CREDENTIAL_RECOVERY_HINT})"
            )
        else:
            try:
                auth_status = json.loads(auth_check[1])
                if not isinstance(auth_status, dict) or auth_status.get("loggedIn") is not True:
                    errors.append(
                        f"Claude authentication check reported loggedIn=false (recover: {_CREDENTIAL_RECOVERY_HINT})"
                    )
            except json.JSONDecodeError:
                errors.append("Claude authentication check returned invalid JSON")
    return errors


def agent_worktree_root(runner: RunnerConfig) -> Path:
    """Return the explicit source-worktree root or the legacy native default."""

    if runner.worktree_root:
        return Path(runner.worktree_root)
    return Path(runner.work_dir) / "worktrees"


def validate_agent_worktree_settings(
    runner: RunnerConfig,
    *,
    validate_ref: bool = True,
) -> list[str]:
    """Validate isolated-worktree placement and its explicit Git base ref."""

    if not runner.worktree_isolation:
        return []

    errors: list[str] = []
    root = agent_worktree_root(runner)
    if not root.is_absolute():
        errors.append("AGENT_WORKTREE_ROOT must be an absolute path")
    elif root.exists() and not root.is_dir():
        errors.append(f"Agent worktree root is not a directory: {root}")

    base_ref = runner.base_ref.strip()
    if not base_ref:
        errors.append("AGENT_BASE_REF must not be empty")
    elif base_ref.startswith("-"):
        errors.append("AGENT_BASE_REF must not start with '-'")
    elif validate_ref:
        ref_check = _run_preflight_command(
            (
                "git",
                "-C",
                str(runner.project_dir),
                "rev-parse",
                "--verify",
                "--quiet",
                f"{base_ref}^{{commit}}",
            )
        )
        if ref_check[0] != 0:
            errors.append(
                f"AGENT_BASE_REF does not resolve to a commit in the target repository: {base_ref}"
            )
    return errors


def validate_project_workspace(project_dir: str | Path, *, require_clean: bool = True) -> list[str]:
    """Validate the trusted repository path; optionally require a clean git worktree."""

    path = Path(project_dir)
    errors: list[str] = []
    if not path.is_absolute():
        errors.append("AGENT_PROJECT_DIR must be an absolute path")
    if not path.is_dir():
        errors.append(f"Agent project directory does not exist: {path}")
        return errors
    if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
        errors.append(f"Agent project directory is not readable and writable: {path}")
    if shutil.which("git") is None:
        errors.append("Missing required worker tooling: git")
        return errors
    git_check = _run_preflight_command(("git", "-C", str(path), "rev-parse", "--is-inside-work-tree"))
    if git_check[0] != 0 or git_check[1].strip() != "true":
        errors.append(f"Agent project directory is not a git worktree: {path}")
        return errors
    if require_clean:
        git_status = _run_preflight_command(("git", "-C", str(path), "status", "--porcelain"))
        if git_status[0] != 0:
            errors.append(f"Could not inspect agent project git status: {_preflight_output(git_status)}")
        elif git_status[1].strip():
            errors.append(f"Agent project git worktree is dirty: {path}")
    return errors


def _run_preflight_command(command: tuple[str, ...]) -> tuple[int, str, str]:
    try:
        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)
    return completed.returncode, completed.stdout, completed.stderr


def _preflight_output(result: tuple[int, str, str]) -> str:
    value = (result[2] or result[1]).strip().replace("\n", " ")
    return value[:300] or f"exit code {result[0]}"


def _get_optional_str(source: Mapping[str, str], key: str) -> str | None:
    value = source.get(key)
    return value if value else None


def _get_str(source: Mapping[str, str], key: str, default: str) -> str:
    return source.get(key) or default


def _get_int(source: Mapping[str, str], key: str, default: int) -> int:
    raw = source.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer, got {raw!r}") from exc


def _get_csv_tuple(source: Mapping[str, str], key: str) -> tuple[str, ...]:
    raw = source.get(key, "")
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _get_bool(source: Mapping[str, str], key: str, default: bool) -> bool:
    raw = source.get(key)
    if raw is None or raw == "":
        return default
    normalized = raw.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    # Preserve the declared default for unknown spellings. In particular, a
    # typo must never silently turn off a default-on safety boundary.
    return default
