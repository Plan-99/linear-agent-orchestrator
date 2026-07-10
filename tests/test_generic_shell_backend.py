"""Model-neutral generic interactive shell backend coverage.

The tmux agent UI must be *backend-neutral*: it drives whatever command the
operator configures via ``RUNNER_COMMAND_TEMPLATE``, not just Codex/Claude. The
generic contract lives in ``ShellRunner``, which materializes prompt/context as
input, runs an arbitrary no-shell command, captures its live output, enforces a
timeout with process-group cleanup, and trusts completion only via the strict
``result.json`` gate.

These tests exercise that contract with a plain POSIX ``/bin/sh`` script standing
in for the backend -- deliberately nothing Codex- or Claude-specific -- covering
the four properties the epic depends on:

* live output/input   -- the backend reads the materialized prompt and its
                          stdout/stderr are captured verbatim;
* headless fallback    -- it runs to completion with no TTY/UI attached;
* timeout cleanup      -- a spawned descendant is killed with the process group;
* strict completion    -- exit 0 alone is not success; only a valid result.json is.
"""

from __future__ import annotations

from pathlib import Path
import shlex
import sys
import time

from linear_agent_orchestrator.config import RunnerConfig
from linear_agent_orchestrator.issue_schema import validate_issue_body
from linear_agent_orchestrator.linear_client import LinearIssue
from linear_agent_orchestrator.runner import ShellRunner


VALID_BODY = """## Task
Exercise a generic backend.

## Acceptance Criteria
- Output is captured

## Validation
- Inspect result
"""

# A JSON result string with no brace placeholders that could collide with the
# command-template tokenizer -- written by the fake backend on demand.
_RESULT_JSON = (
    '{"version":1,"status":"completed","summary":"ok",'
    '"validation":[{"description":"c","command":"true","exit_code":0,"evidence":"ev"}]}'
)


def make_issue(identifier: str = "GEN-1") -> LinearIssue:
    return LinearIssue(
        id=f"issue-{identifier}",
        identifier=identifier,
        title="Generic backend",
        description=VALID_BODY,
        url=None,
        updated_at="2026-07-10T00:00:00Z",
        state_id="state-todo",
        state_name="Todo",
        project_id=None,
        project_name=None,
        schema=validate_issue_body(VALID_BODY),
    )


def runner_config(tmp_path: Path, command: str, *, timeout: int = 5, allowed_env: tuple[str, ...] = ()) -> RunnerConfig:
    return RunnerConfig(
        command_template=command,
        timeout_seconds=timeout,
        work_dir=str(tmp_path / "work"),
        artifacts_dir=str(tmp_path / "artifacts"),
        allowed_env=allowed_env,
    )


def write_backend(tmp_path: Path, body: str) -> Path:
    backend = tmp_path / "generic-backend.sh"
    backend.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    backend.chmod(0o755)
    return backend


def test_generic_backend_captures_live_output_and_reads_prompt_input(tmp_path: Path):
    """A non-Codex/Claude ``sh`` backend reads the prompt and its output is captured."""

    backend = write_backend(
        tmp_path,
        'printf "STDOUT-BEGIN\\n"\n'
        'head -n 1 "$1"\n'            # $1 = prompt_path -> proves input delivery
        'printf "diagnostic\\n" 1>&2\n',
    )
    command = f"/bin/sh {shlex.quote(str(backend))} {{prompt_path}}"

    result = ShellRunner(runner_config(tmp_path, command)).run(make_issue())

    assert result.succeeded is True
    # Live stdout is captured verbatim, including the prompt line the backend read.
    assert result.stdout.splitlines()[0] == "STDOUT-BEGIN"
    assert "# Linear Issue GEN-1" in result.stdout
    # stderr is captured on its own channel.
    assert "diagnostic" in result.stderr


def test_generic_backend_headless_success_requires_valid_result_json(tmp_path: Path):
    """Strict completion: exit 0 without result.json is not semantic success."""

    # Backend exits 0 but writes nothing -> transport success, not completion.
    silent = write_backend(tmp_path, 'exit 0\n')
    silent_cmd = f"/bin/sh {shlex.quote(str(silent))}"
    silent_result = ShellRunner(runner_config(tmp_path, silent_cmd)).run(make_issue("GEN-SILENT"))
    assert silent_result.succeeded is True
    assert silent_result.completed_semantically is False

    # Backend writes a valid structured result -> semantic completion.
    writer = write_backend(tmp_path, f"printf '%s' '{_RESULT_JSON}' > \"$1\"\n")
    writer_cmd = f"/bin/sh {shlex.quote(str(writer))} {{result_path}}"
    writer_result = ShellRunner(runner_config(tmp_path, writer_cmd)).run(make_issue("GEN-OK"))
    assert writer_result.succeeded is True
    assert writer_result.completed_semantically is True
    assert writer_result.structured_result is not None
    assert writer_result.structured_result.status == "completed"


def test_generic_backend_nonzero_exit_is_not_completion_even_with_result(tmp_path: Path):
    """A backend that writes a valid result but exits nonzero must not reach Done."""

    backend = write_backend(
        tmp_path,
        f"printf '%s' '{_RESULT_JSON}' > \"$1\"\n"
        "exit 3\n",
    )
    command = f"/bin/sh {shlex.quote(str(backend))} {{result_path}}"

    result = ShellRunner(runner_config(tmp_path, command)).run(make_issue("GEN-EXIT3"))

    assert result.succeeded is False
    assert result.exit_code == 3
    assert result.completed_semantically is False


def test_generic_backend_timeout_kills_descendant_process_group(tmp_path: Path):
    """A generic backend that spawns a descendant is fully cleaned up on timeout."""

    marker = tmp_path / "descendant-survived.txt"
    backend = write_backend(
        tmp_path,
        # Spawn a background descendant that would write the marker after the
        # timeout, then block. Both must die when the process group is killed.
        f"( sleep 2; printf survived > {shlex.quote(str(marker))} ) &\n"
        "sleep 10\n",
    )
    command = f"/bin/sh {shlex.quote(str(backend))}"

    result = ShellRunner(runner_config(tmp_path, command, timeout=1)).run(make_issue("GEN-TIMEOUT"))
    # Give the (should-be-killed) descendant past its would-be write moment.
    time.sleep(1.5)

    assert result.timed_out is True
    assert result.exit_code is None
    assert result.completed_semantically is False
    assert not marker.exists()


def test_generic_backend_receives_only_allowlisted_env(tmp_path: Path):
    """The generic backend sees allowlisted env only -- secrets never leak in."""

    import os

    backend = write_backend(
        tmp_path,
        'printf "ALLOWED=%s\\n" "${GEN_ALLOWED:-<unset>}"\n'
        'printf "SECRET=%s\\n" "${GEN_SECRET_TOKEN:-<unset>}"\n',
    )
    command = f"/bin/sh {shlex.quote(str(backend))}"
    os.environ["GEN_ALLOWED"] = "visible"
    os.environ["GEN_SECRET_TOKEN"] = "must-not-leak"
    try:
        result = ShellRunner(
            runner_config(tmp_path, command, allowed_env=("GEN_ALLOWED",))
        ).run(make_issue("GEN-ENV"))
    finally:
        os.environ.pop("GEN_ALLOWED", None)
        os.environ.pop("GEN_SECRET_TOKEN", None)

    assert "ALLOWED=visible" in result.stdout
    assert "SECRET=<unset>" in result.stdout
    assert "must-not-leak" not in result.stdout
