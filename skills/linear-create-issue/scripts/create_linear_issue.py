#!/usr/bin/env python3
"""Launch LAO's guarded generic Linear issue command from any directory."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import stat
import subprocess
import sys
from typing import Iterable


_LAO_MODULE = Path("src/linear_agent_orchestrator/cli.py")
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


def _explicit_env_file(args: argparse.Namespace) -> Path | None:
    if args.env_file is not None:
        return args.env_file.expanduser().resolve(strict=False)
    configured = os.environ.get("LINEAR_CREATE_ISSUE_ENV_FILE", "").strip()
    if configured:
        return Path(configured).expanduser().resolve(strict=False)
    user_config = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    candidate = user_config / "linear-create-issue" / ".env"
    return candidate.resolve(strict=False) if candidate.is_file() else None


def _root_candidates(args: argparse.Namespace, env_file: Path | None) -> list[Path]:
    candidates: list[Path] = []
    if args.lao_root is not None:
        candidates.append(args.lao_root)
    configured = os.environ.get("LINEAR_CREATE_ISSUE_LAO_ROOT", "").strip()
    if configured:
        candidates.append(Path(configured))
    if env_file is not None:
        candidates.append(env_file.parent)

    candidates.extend(
        (
            Path.home() / "Project" / "linear-agent-orchestrator",
            Path.home() / "Projects" / "linear-agent-orchestrator",
        )
    )
    return _dedupe(candidates)


def _resolve_lao_root(args: argparse.Namespace, env_file: Path | None) -> Path:
    for candidate in _root_candidates(args, env_file):
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


def _validate_inputs(title: str, description_file: Path) -> Path:
    if not title.strip():
        raise LauncherError("Issue title must not be empty.")
    path = description_file.expanduser().resolve(strict=False)
    if not path.is_file():
        raise LauncherError(f"Description file not found: {path}")
    try:
        path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise LauncherError(f"Description file must be valid UTF-8: {path}") from exc
    return path


def _child_environment(lao_root: Path) -> dict[str, str]:
    child = dict(os.environ)
    for key in _LINEAR_CONFIG_KEYS:
        child.pop(key, None)
    src = str(lao_root / "src")
    existing = child.get("PYTHONPATH", "")
    child["PYTHONPATH"] = src if not existing else os.pathsep.join((src, existing))
    return child


def _command(
    args: argparse.Namespace,
    *,
    env_file: Path,
    description_file: Path,
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "linear_agent_orchestrator.cli",
        "--env-file",
        str(env_file),
        "create-issue",
        "--title",
        args.title.strip(),
        "--description-file",
        str(description_file),
    ]
    if args.dry_run:
        command.append("--dry-run")
    if args.json:
        command.append("--json")
    return command


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        description_file = _validate_inputs(args.title, args.description_file)
        explicit_env = _explicit_env_file(args)
        lao_root = _resolve_lao_root(args, explicit_env)
        env_file = _resolve_env_file(explicit_env, lao_root)
    except (LauncherError, OSError) as exc:
        print(f"linear-create-issue: {exc}", file=sys.stderr)
        return 2

    completed = subprocess.run(
        _command(
            args,
            env_file=env_file,
            description_file=description_file,
        ),
        cwd=lao_root,
        env=_child_environment(lao_root),
        check=False,
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
