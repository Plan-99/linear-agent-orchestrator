#!/usr/bin/env python3
"""Launch LAO's guarded generic Linear issue command from any directory."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys
import time
from typing import Any, Iterable


_LAO_MODULE = Path("src/linear_agent_orchestrator/cli.py")
_REQUEST_SHA_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_PREMUTATION_EXIT_CODES = frozenset({2, 3})
_LINEAR_CONFIG_KEYS = frozenset(
    {
        "LINEAR_API_KEY",
        "LINEAR_ASSIGNEE_ID",
        "LINEAR_ATTENTION_STATE_NAME",
        "LINEAR_AUTH_MODE",
        "LINEAR_DONE_STATE_NAME",
        "LINEAR_IN_PROGRESS_STATE_NAME",
        "LINEAR_OAUTH_CLIENT_ID",
        "LINEAR_OAUTH_CLIENT_SECRET",
        "LINEAR_PROJECT_ID",
        "LINEAR_PROJECT_NAME",
        "LINEAR_QUEUE_STATE_NAME",
        "LINEAR_TEAM_ID",
    }
)


class LauncherError(RuntimeError):
    """A deterministic local setup or input error."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create one LAO-compatible Linear issue using the configured OAuth/API actor."
    )
    parser.add_argument("--title", required=True, help="Linear issue title")
    parser.add_argument(
        "--description-file",
        required=True,
        type=Path,
        help="UTF-8 Markdown body containing Task, Acceptance Criteria, and Validation",
    )
    parser.add_argument("--lao-root", type=Path, help="Linear Agent Orchestrator checkout")
    parser.add_argument("--env-file", type=Path, help="LAO dotenv file containing Linear configuration")
    parser.add_argument(
        "--expected-request-sha256",
        help="Request digest returned by the immediately preceding dry-run; required for creation",
    )
    parser.add_argument(
        "--state-db",
        type=Path,
        help="Private idempotency ledger (default: XDG state directory)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and print a redacted plan without network access",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    return parser


def _dedupe(paths: Iterable[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        expanded = path.expanduser().resolve(strict=False)
        marker = str(expanded)
        if marker not in seen:
            seen.add(marker)
            result.append(expanded)
    return result


def _absolute_env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise LauncherError(f"{name} must be an absolute path")
    return path.resolve(strict=False)


def _explicit_env_file(args: argparse.Namespace) -> Path | None:
    if args.env_file is not None:
        return args.env_file.expanduser().resolve(strict=False)
    configured = os.environ.get("LINEAR_CREATE_ISSUE_ENV_FILE", "").strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            raise LauncherError("LINEAR_CREATE_ISSUE_ENV_FILE must be an absolute path")
        return path.resolve(strict=False)
    config_home = _absolute_env_path("XDG_CONFIG_HOME", Path.home() / ".config")
    candidate = config_home / "linear-create-issue" / ".env"
    return candidate.resolve(strict=False) if candidate.is_file() else None


def _explicit_lao_root(args: argparse.Namespace) -> tuple[Path, str] | None:
    if args.lao_root is not None:
        return args.lao_root.expanduser().resolve(strict=False), "--lao-root"
    configured = os.environ.get("LINEAR_CREATE_ISSUE_LAO_ROOT", "").strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            raise LauncherError("LINEAR_CREATE_ISSUE_LAO_ROOT must be an absolute path")
        return path.resolve(strict=False), "LINEAR_CREATE_ISSUE_LAO_ROOT"
    return None


def _resolve_lao_root(args: argparse.Namespace) -> Path:
    explicit = _explicit_lao_root(args)
    if explicit is not None:
        root, source = explicit
        if not (root / _LAO_MODULE).is_file():
            raise LauncherError(f"{source} does not point to a valid LAO checkout: {root}")
        return root

    candidates = _dedupe(
        (
            Path.home() / "Project" / "linear-agent-orchestrator",
            Path.home() / "Projects" / "linear-agent-orchestrator",
        )
    )
    for candidate in candidates:
        if (candidate / _LAO_MODULE).is_file():
            return candidate
    raise LauncherError(
        "Could not locate Linear Agent Orchestrator. Set "
        "LINEAR_CREATE_ISSUE_LAO_ROOT or pass --lao-root."
    )


def _resolve_env_file(explicit: Path | None, lao_root: Path) -> Path:
    env_file = explicit or (lao_root / ".env")
    env_file = env_file.expanduser().resolve(strict=False)
    if not env_file.is_file():
        raise LauncherError(
            f"Linear configuration file not found: {env_file}. Set "
            "LINEAR_CREATE_ISSUE_ENV_FILE or pass --env-file."
        )
    mode = stat.S_IMODE(env_file.stat().st_mode)
    if mode & 0o077:
        raise LauncherError(
            "Linear configuration file must not be accessible by group or others: "
            f"{env_file} (mode {mode:04o}; expected 0600 or stricter)."
        )
    return env_file


def _validate_inputs(title: str, description_file: Path) -> tuple[Path, str]:
    clean_title = title.strip()
    if not clean_title:
        raise LauncherError("Issue title must not be empty.")
    path = description_file.expanduser().resolve(strict=False)
    if not path.is_file():
        raise LauncherError(f"Description file not found: {path}")
    try:
        body = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise LauncherError(f"Description file must be valid UTF-8: {path}") from exc
    return path, clean_title


def _child_environment() -> dict[str, str]:
    child = dict(os.environ)
    for key in tuple(child):
        if key in _LINEAR_CONFIG_KEYS or key.startswith("PYTHON"):
            child.pop(key, None)
    return child


def _isolated_bootstrap(lao_root: Path) -> str:
    trusted_src = str((lao_root / "src").resolve(strict=False))
    return (
        "import runpy,sys;"
        f"sys.path.insert(0,{trusted_src!r});"
        "runpy.run_module('linear_agent_orchestrator.cli',run_name='__main__')"
    )


def _command(
    args: argparse.Namespace,
    *,
    lao_root: Path,
    env_file: Path,
    description_file: Path,
    force_json: bool = False,
) -> list[str]:
    command = [
        sys.executable,
        "-I",
        "-c",
        _isolated_bootstrap(lao_root),
        "--env-file",
        str(env_file),
        "create-issue",
        "--title",
        args.title.strip(),
        "--description-file",
        str(description_file),
    ]
    if args.expected_request_sha256:
        command.extend(("--expected-request-sha256", args.expected_request_sha256))
    if args.dry_run:
        command.append("--dry-run")
    if force_json or args.json:
        command.append("--json")
    return command


def _state_db_path(args: argparse.Namespace) -> Path:
    if args.state_db is not None:
        return args.state_db.expanduser().resolve(strict=False)
    configured = os.environ.get("LINEAR_CREATE_ISSUE_STATE_DB", "").strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            raise LauncherError("LINEAR_CREATE_ISSUE_STATE_DB must be an absolute path")
        return path.resolve(strict=False)
    state_home = _absolute_env_path(
        "XDG_STATE_HOME",
        Path.home() / ".local" / "state",
    )
    return (state_home / "linear-create-issue" / "requests.sqlite3").resolve(strict=False)


def _open_ledger(path: Path) -> sqlite3.Connection:
    parent = path.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent_mode = stat.S_IMODE(parent.stat().st_mode)
    if parent_mode & 0o077:
        raise LauncherError(
            f"Ledger directory must be private: {parent} "
            f"(mode {parent_mode:04o}; expected 0700 or stricter)."
        )
    if not path.exists():
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise LauncherError(
            f"Ledger must not be accessible by group or others: {path} "
            f"(mode {mode:04o}; expected 0600 or stricter)."
        )
    connection = sqlite3.connect(path, timeout=10)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS requests (
            request_sha256 TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            body_sha256 TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('pending', 'completed', 'uncertain')),
            response_json TEXT,
            updated_at REAL NOT NULL
        )
        """
    )
    connection.commit()
    return connection


def _reserve_request(
    connection: sqlite3.Connection,
    *,
    request_sha256: str,
    title: str,
    body_sha256: str,
) -> tuple[str, dict[str, Any] | None]:
    connection.execute("BEGIN IMMEDIATE")
    row = connection.execute(
        """
        SELECT title, body_sha256, status, response_json
        FROM requests
        WHERE request_sha256 = ?
        """,
        (request_sha256,),
    ).fetchone()
    if row is None:
        connection.execute(
            """
            INSERT INTO requests (
                request_sha256, title, body_sha256, status, response_json, updated_at
            ) VALUES (?, ?, ?, 'pending', NULL, ?)
            """,
            (request_sha256, title, body_sha256, time.time()),
        )
        connection.commit()
        return "reserved", None

    stored_title, stored_body_sha256, status, response_json = row
    connection.commit()
    if stored_title != title or stored_body_sha256 != body_sha256:
        raise LauncherError(
            "The request digest is already associated with different local content."
        )
    try:
        response = json.loads(response_json) if response_json else None
    except json.JSONDecodeError as exc:
        raise LauncherError("The idempotency ledger contains an invalid response") from exc
    return str(status), response


def _delete_reservation(connection: sqlite3.Connection, request_sha256: str) -> None:
    connection.execute(
        "DELETE FROM requests WHERE request_sha256 = ? AND status = 'pending'",
        (request_sha256,),
    )
    connection.commit()


def _mark_uncertain(connection: sqlite3.Connection, request_sha256: str) -> None:
    connection.execute(
        """
        UPDATE requests
        SET status = 'uncertain', updated_at = ?
        WHERE request_sha256 = ? AND status = 'pending'
        """,
        (time.time(), request_sha256),
    )
    connection.commit()


def _mark_completed(
    connection: sqlite3.Connection,
    request_sha256: str,
    response: dict[str, Any],
) -> None:
    connection.execute(
        """
        UPDATE requests
        SET status = 'completed', response_json = ?, updated_at = ?
        WHERE request_sha256 = ? AND status = 'pending'
        """,
        (json.dumps(response, sort_keys=True), time.time(), request_sha256),
    )
    connection.commit()


def _emit_created(response: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(response, indent=2, sort_keys=True))
    else:
        print(f"created {response.get('identifier', '')} {response.get('url') or ''}".rstrip())


def _emit_child(completed: subprocess.CompletedProcess[str]) -> None:
    if completed.stdout:
        sys.stdout.write(completed.stdout)
    if completed.stderr:
        sys.stderr.write(completed.stderr)


def _validate_created_response(stdout: str) -> dict[str, Any]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise LauncherError("LAO returned invalid JSON after issue creation") from exc
    if not isinstance(payload, dict):
        raise LauncherError("LAO returned a non-object result after issue creation")
    required = {"identifier", "issue_id", "title", "url"}
    if set(payload) != required or not payload.get("identifier") or not payload.get("issue_id"):
        raise LauncherError("LAO returned an incomplete result after issue creation")
    return payload


def _run_dry_run(
    args: argparse.Namespace,
    *,
    lao_root: Path,
    env_file: Path,
    description_file: Path,
) -> int:
    completed = subprocess.run(
        _command(
            args,
            lao_root=lao_root,
            env_file=env_file,
            description_file=description_file,
        ),
        cwd=lao_root,
        env=_child_environment(),
        check=False,
        text=True,
        capture_output=True,
    )
    _emit_child(completed)
    return completed.returncode


def _run_live(
    args: argparse.Namespace,
    *,
    lao_root: Path,
    env_file: Path,
    description_file: Path,
    clean_title: str,
) -> int:
    request_sha256 = (args.expected_request_sha256 or "").casefold()
    if not _REQUEST_SHA_RE.fullmatch(request_sha256):
        raise LauncherError(
            "Live creation requires --expected-request-sha256 from a successful dry-run."
        )

    body_sha256 = hashlib.sha256(description_file.read_bytes()).hexdigest()
    ledger = _open_ledger(_state_db_path(args))
    try:
        status, response = _reserve_request(
            ledger,
            request_sha256=request_sha256,
            title=clean_title,
            body_sha256=body_sha256,
        )
        if status == "completed":
            assert response is not None
            _emit_created(response, as_json=args.json)
            return 0
        if status in {"pending", "uncertain"}:
            print(
                "linear-create-issue: this request is already pending or uncertain; "
                "check Linear before any manual retry.",
                file=sys.stderr,
            )
            return 4

        try:
            completed = subprocess.run(
                _command(
                    args,
                    lao_root=lao_root,
                    env_file=env_file,
                    description_file=description_file,
                    force_json=True,
                ),
                cwd=lao_root,
                env=_child_environment(),
                check=False,
                text=True,
                capture_output=True,
            )
        except OSError:
            _delete_reservation(ledger, request_sha256)
            raise
        except BaseException:
            _mark_uncertain(ledger, request_sha256)
            raise

        if completed.returncode in _PREMUTATION_EXIT_CODES:
            _delete_reservation(ledger, request_sha256)
            _emit_child(completed)
            return completed.returncode
        if completed.returncode != 0:
            _mark_uncertain(ledger, request_sha256)
            _emit_child(completed)
            return 4

        try:
            created = _validate_created_response(completed.stdout)
        except LauncherError:
            _mark_uncertain(ledger, request_sha256)
            _emit_child(completed)
            raise
        _mark_completed(ledger, request_sha256, created)
        if completed.stderr:
            sys.stderr.write(completed.stderr)
        _emit_created(created, as_json=args.json)
        return 0
    finally:
        ledger.close()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        description_file, clean_title = _validate_inputs(args.title, args.description_file)
        explicit_env = _explicit_env_file(args)
        lao_root = _resolve_lao_root(args)
        env_file = _resolve_env_file(explicit_env, lao_root)
        if args.dry_run:
            return _run_dry_run(
                args,
                lao_root=lao_root,
                env_file=env_file,
                description_file=description_file,
            )
        return _run_live(
            args,
            lao_root=lao_root,
            env_file=env_file,
            description_file=description_file,
            clean_title=clean_title,
        )
    except (LauncherError, OSError, sqlite3.Error) as exc:
        print(f"linear-create-issue: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
