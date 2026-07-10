"""Command-line entrypoint for the scaffolded orchestrator."""

from __future__ import annotations

import argparse
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
import sys

from . import __version__
from .config import AppConfig, RunnerConfig, load_config
from .daemon import run_daemon
from .issue_schema import clarification_comment, validate_issue_body
from .linear_client import LinearClient, LinearIssue
from .logging import configure_logging
from .runner import ShellRunner, process_next_issue_once
from .state import StateStore

LOGGER = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="linear-agent-orchestrator")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--env-file",
        action="append",
        type=Path,
        default=[],
        help="Optional dotenv file. May be passed multiple times; process environment wins.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check-config", help="Validate and print redacted startup configuration")
    check.add_argument(
        "--allow-missing-secrets",
        action="store_true",
        help="Validate non-secret wiring only; useful for local scaffold smoke tests.",
    )
    check.add_argument("--json", action="store_true", help="Print redacted config as JSON")

    startup = subparsers.add_parser("startup-check", help="Run startup wiring checks and exit")
    startup.add_argument(
        "--allow-missing-secrets",
        action="store_true",
        help="Skip secret presence checks for local scaffold smoke tests.",
    )

    daemon = subparsers.add_parser("daemon", help="Start the long-running polling daemon")
    daemon.add_argument(
        "--allow-missing-secrets",
        action="store_true",
        help="Skip secret presence checks for local scaffold smoke tests.",
    )
    daemon.add_argument(
        "--exit-after-startup",
        action="store_true",
        help="Exit after startup checks without polling.",
    )
    daemon.add_argument("--db-path", type=Path, help="Override STATE_DB_PATH for local claim state")
    daemon.add_argument("--owner-id", default="linear-agent-orchestrator-daemon", help="Lease owner identity")
    daemon.add_argument("--max-iterations", type=int, help="Bound daemon loop for tests/smoke checks")

    validate = subparsers.add_parser("validate-issue", help="Validate a local markdown issue body against the TEST schema")
    validate.add_argument("path", type=Path, help="Markdown file containing a candidate Linear issue body")
    validate.add_argument("--identifier", default="LOCAL-DRY-RUN", help="Identifier to include in clarification preview")
    validate.add_argument("--json", action="store_true", help="Print validation result as JSON")
    validate.add_argument("--show-clarification", action="store_true", help="Print clarification comment preview when invalid")

    state = subparsers.add_parser("state-init", help="Initialize or migrate the local SQLite state database")
    state.add_argument("--db-path", type=Path, help="Override STATE_DB_PATH for this command")
    state.add_argument("--json", action="store_true", help="Print migration result as JSON")

    dry_run = subparsers.add_parser("runner-dry-run", help="Run the generic shell runner against a local issue markdown file without Linear")
    dry_run.add_argument("path", type=Path, help="Markdown file containing a valid candidate issue body")
    dry_run.add_argument("--command-template", help="Override RUNNER_COMMAND_TEMPLATE for this dry-run")
    dry_run.add_argument("--work-dir", default="/tmp/lao-runner-work", help="Runner work root for this dry-run")
    dry_run.add_argument("--artifacts-dir", default="/tmp/lao-runner-artifacts", help="Runner artifacts root for this dry-run")
    dry_run.add_argument("--identifier", default="LOCAL-DRY-RUN", help="Synthetic issue identifier")
    dry_run.add_argument("--json", action="store_true", help="Print runner result as JSON")

    process_once = subparsers.add_parser("process-once", help="Poll and process one configured Linear Todo issue, then exit")
    process_once.add_argument("--db-path", type=Path, help="Override STATE_DB_PATH for local claim state")
    process_once.add_argument("--owner-id", default="linear-agent-orchestrator-process-once", help="Lease owner identity")
    process_once.add_argument("--first", type=int, default=10, help="Linear page size for one-shot polling")
    process_once.add_argument("--json", action="store_true", help="Print process result as JSON")

    create_issue = subparsers.add_parser("create-test-issue", help="Create a safe canonical Linear TEST issue for live e2e validation")
    create_issue.add_argument("--title", help="Issue title; defaults to timestamped G005 smoke title")
    create_issue.add_argument("--json", action="store_true", help="Print created issue as JSON")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config(env_files=args.env_file)
    configure_logging(config.log_level, config.log_format)

    require_secrets = not getattr(args, "allow_missing_secrets", False)
    if args.command == "check-config":
        return _check_config(config, require_secrets=require_secrets, as_json=args.json)
    if args.command == "startup-check":
        return _startup_check(config, require_secrets=require_secrets)
    if args.command == "daemon":
        return _daemon(
            config,
            require_secrets=require_secrets,
            exit_after_startup=args.exit_after_startup,
            db_path=args.db_path or Path(config.state_db_path),
            owner_id=args.owner_id,
            max_iterations=args.max_iterations,
        )
    if args.command == "validate-issue":
        return _validate_issue(args.path, identifier=args.identifier, as_json=args.json, show_clarification=args.show_clarification)
    if args.command == "state-init":
        return _state_init(args.db_path or Path(config.state_db_path), as_json=args.json)
    if args.command == "runner-dry-run":
        return _runner_dry_run(config, args.path, command_template=args.command_template, work_dir=args.work_dir, artifacts_dir=args.artifacts_dir, identifier=args.identifier, as_json=args.json)
    if args.command == "process-once":
        return _process_once(config, db_path=args.db_path or Path(config.state_db_path), owner_id=args.owner_id, first=args.first, as_json=args.json)
    if args.command == "create-test-issue":
        return _create_test_issue(config, title=args.title, as_json=args.json)
    raise AssertionError(f"Unhandled command: {args.command}")


def _check_config(config: AppConfig, require_secrets: bool, as_json: bool) -> int:
    errors = config.validate_startup(require_secrets=require_secrets)
    if as_json:
        print(json.dumps({"config": config.redacted_dict(), "errors": errors}, indent=2, sort_keys=True))
    else:
        print("configuration: ok" if not errors else "configuration: invalid")
        for error in errors:
            print(f"- {error}")
    return 1 if errors else 0


def _startup_check(config: AppConfig, require_secrets: bool) -> int:
    errors = config.validate_startup(require_secrets=require_secrets)
    if require_secrets and not errors:
        errors.extend(linear_assignee_preflight_errors(config))
    if errors:
        for error in errors:
            LOGGER.error("startup_config_error", extra={"error": error})
        return 1
    LOGGER.info(
        "startup_check_passed",
        extra={
            "environment": config.environment,
            "poll_interval_seconds": config.poll_interval_seconds,
            "lease_ttl_seconds": config.lease_ttl_seconds,
            "runner_timeout_seconds": config.runner.timeout_seconds,
        },
    )
    return 0


def linear_assignee_preflight_errors(config: AppConfig, client: LinearClient | None = None) -> list[str]:
    """Resolve the configured/default assignee through Linear before claiming work."""

    try:
        assignee_id = (client or LinearClient.from_config(config.linear)).resolve_assignee_id(config.linear)
    except Exception as exc:  # noqa: BLE001 - startup must fail closed for any client/transport error
        return [f"Linear assignee preflight failed: {type(exc).__name__}: {str(exc)[:300]}"]
    if not assignee_id:
        return ["Linear assignee preflight returned an empty assignee ID"]
    return []


def _validate_issue(path: Path, *, identifier: str, as_json: bool, show_clarification: bool) -> int:
    body = path.read_text(encoding="utf-8")
    result = validate_issue_body(body)
    payload = {
        "valid": result.valid,
        "missing_sections": list(result.missing_sections),
        "empty_sections": list(result.empty_sections),
        "diagnostics": list(result.diagnostics),
    }
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print("issue schema: valid" if result.valid else "issue schema: invalid")
        for diagnostic in result.diagnostics:
            print(f"- {diagnostic}")
    if show_clarification and not result.valid:
        print("\n--- clarification comment preview ---")
        print(clarification_comment(identifier, result))
    return 0 if result.valid else 1


def _runner_dry_run(
    config: AppConfig,
    path: Path,
    *,
    command_template: str | None,
    work_dir: str,
    artifacts_dir: str,
    identifier: str,
    as_json: bool,
) -> int:
    body = path.read_text(encoding="utf-8")
    schema = validate_issue_body(body)
    if not schema.valid:
        for diagnostic in schema.diagnostics:
            print(f"- {diagnostic}", file=sys.stderr)
        return 1
    runner_config = RunnerConfig(
        command_template=command_template or config.runner.command_template,
        timeout_seconds=config.runner.timeout_seconds,
        work_dir=work_dir,
        artifacts_dir=artifacts_dir,
        allowed_env=config.runner.allowed_env,
        project_dir=config.runner.project_dir,
        backend=config.runner.backend,
    )
    issue = LinearIssue(
        id="local-dry-run",
        identifier=identifier,
        title="Local dry-run issue",
        description=body,
        url=None,
        updated_at="local",
        state_id=None,
        state_name="Todo",
        project_id=None,
        project_name="LOCAL",
        schema=schema,
    )
    result = ShellRunner(runner_config).run(issue)
    payload = {
        "succeeded": result.completed_semantically,
        "transport_succeeded": result.succeeded,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "prompt_path": str(result.paths.prompt_path),
        "context_path": str(result.paths.context_path),
        "artifacts_dir": str(result.paths.artifacts_dir),
        "result_path": str(result.paths.result_path),
        "result_error": result.result_error,
        "semantic_status": result.structured_result.status if result.structured_result else None,
    }
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print("runner dry-run: success" if result.completed_semantically else "runner dry-run: failed")
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
    return 0 if result.completed_semantically else 1


def _create_test_issue(config: AppConfig, *, title: str | None, as_json: bool) -> int:
    missing = []
    if not config.linear.api_key:
        missing.append("LINEAR_API_KEY is required")
    if not config.linear.team_id:
        missing.append("LINEAR_TEAM_ID is required")
    if not (config.linear.project_name or config.linear.project_id):
        missing.append("LINEAR_PROJECT_NAME or LINEAR_PROJECT_ID is required")
    if missing:
        for error in missing:
            LOGGER.error("create_test_issue_config_error", extra={"error": error})
        return 1
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    issue_title = title or f"G005 Linear orchestrator smoke {stamp}"
    body = (
        "## Task\n"
        "Run the configured generic shell runner for the G005 live smoke validation.\n\n"
        "## Acceptance Criteria\n"
        "- The orchestrator claims this issue once.\n"
        "- The runner exits successfully.\n"
        "- Evidence comments are posted.\n"
        "- The issue is moved to Done.\n\n"
        "## Validation\n"
        "- Confirm this issue has start/result comments and status Done.\n"
    )
    issue = LinearClient.from_config(config.linear).create_issue(config.linear, title=issue_title, description=body)
    payload = {"issue_id": issue.id, "identifier": issue.identifier, "url": issue.url, "title": issue.title}
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"created {issue.identifier} {issue.url or ''}".rstrip())
    return 0


def _process_once(config: AppConfig, *, db_path: Path, owner_id: str, first: int, as_json: bool) -> int:
    errors = config.validate_startup(require_secrets=True)
    if not errors:
        errors.extend(linear_assignee_preflight_errors(config))
    if errors:
        for error in errors:
            LOGGER.error("process_once_config_error", extra={"error": error})
        return 1
    linear = LinearClient.from_config(config.linear)
    runner = ShellRunner(config.runner)
    with StateStore(db_path) as state:
        result = process_next_issue_once(
            config=config,
            state=state,
            linear=linear,
            runner=runner,
            owner_id=owner_id,
            first=first,
        )
    payload = {
        "status": result.status,
        "issue_id": result.issue_id,
        "issue_identifier": result.issue_identifier,
        "claimed": result.claimed,
        "executed": result.executed,
        "done_transitioned": result.done_transitioned,
        "comment_count": len(result.comment_ids),
        "message": result.message,
        "issue_url": result.issue_url,
    }
    if result.runner_result:
        payload.update(
            {
                "exit_code": result.runner_result.exit_code,
                "timed_out": result.runner_result.timed_out,
                "artifacts_dir": str(result.runner_result.paths.artifacts_dir),
            }
        )
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"process-once: {result.status} {result.issue_identifier}".rstrip())
    return 0 if result.status in {"completed", "schema_invalid", "no_executable_candidate", "no_candidate", "not_claimed"} else 1


def _state_init(db_path: Path, *, as_json: bool) -> int:
    with StateStore(db_path) as store:
        version = store.schema_version()
    payload = {"db_path": str(db_path), "schema_version": version}
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"state database ready: {db_path} (schema_version={version})")
    return 0


def _daemon(
    config: AppConfig,
    require_secrets: bool,
    exit_after_startup: bool,
    db_path: Path,
    owner_id: str,
    max_iterations: int | None,
) -> int:
    startup_code = _startup_check(config, require_secrets=require_secrets)
    if startup_code != 0:
        return startup_code
    # Test-friendly smoke: validates config and initializes/migrates state without Linear network access.
    if exit_after_startup or max_iterations == 0:
        with StateStore(db_path) as state:
            schema_version = state.schema_version()
        LOGGER.info("daemon_startup_ready", extra={"schema_version": schema_version, "db_path": str(db_path)})
        return 0

    if config.max_parallel_runs > 1:
        result = run_daemon(
            config=config,
            owner_id=owner_id,
            max_iterations=max_iterations,
            component_factory=lambda: (
                StateStore(db_path),
                LinearClient.from_config(config.linear),
                ShellRunner(config.runner),
            ),
        )
    else:
        linear = LinearClient.from_config(config.linear)
        runner = ShellRunner(config.runner)
        with StateStore(db_path) as state:
            result = run_daemon(
                config=config,
                state=state,
                linear=linear,
                runner=runner,
                owner_id=owner_id,
                max_iterations=max_iterations,
            )
    LOGGER.info(
        "daemon_stopped",
        extra={"iterations": result.iterations, "stop_requested": result.stop_requested, "last_status": result.last_status},
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
