"""Generic shell runner and single-issue processing orchestration.

G004 implements safe local runner execution and mocked-testable Linear reporting.
It intentionally does not run live end-to-end Linear validation.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import hashlib
import json
import logging
import os
import re
import signal
import shlex
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Protocol

from .config import (
    AppConfig,
    LinearConfig,
    RunnerConfig,
    agent_worktree_root,
    validate_agent_worktree_settings,
    validate_project_workspace,
)
from .console_schema import CONSOLE_SCHEMA_VERSION, redact_text
from .issue_schema import clarification_comment, validate_issue_body
from .linear_client import LinearComment, LinearIssue, WorkflowRole, WorkflowState
from .state import StateStore

_PLACEHOLDERS = {
    "prompt_path",
    "context_path",
    "artifacts_dir",
    "result_path",
    "issue_id",
    "issue_identifier",
}
_SAFE_PATH_PART_RE = re.compile(r"[^A-Za-z0-9_.-]+")
# Strip ANSI/VT control sequences (CSI, OSC, and single-char escapes) before a
# pty log reaches redaction or a Linear comment. Escape-split secrets would
# otherwise survive redaction, and raw sequences render as garbage.
_ANSI_ESCAPE_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"  # CSI ... final byte
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC ... BEL or ST
    r"|\x1b[P_^X][^\x1b]*(?:\x07|\x1b\\)"  # DCS/APC/PM/SOS ... ST or BEL
    r"|\x1b[@-Z\\-_]"  # two-char escapes
)
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ConsoleContext:
    """Per-run interactive-console paths and the pty-host wrapper argv.

    The socket and run directory live under a container-private, non-mounted
    path keyed by a fresh run id, so ``materialize``'s per-issue cleanup can
    never race them and the host tmux socket is never involved. Only the JSONL
    lifecycle log and output log live under the bind-mounted console log root.
    """

    run_id: str
    run_dir: Path
    socket_path: Path
    session: str
    log_path: Path
    jsonl_path: Path
    backend: str
    supports_direct_input: bool
    wrapper_argv: tuple[str, ...]

    @property
    def backend_pid_path(self) -> Path:
        return self.run_dir / "backend_pid"

    @property
    def backend_starttime_path(self) -> Path:
        return self.run_dir / "backend_starttime"

    @property
    def terminated_path(self) -> Path:
        return self.run_dir / "terminated"


def _strip_ansi(value: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", value)


# A fresh checkout of a large repository legitimately exceeds the default git
# timeout; a killed `worktree add` leaves a locked registration behind.
_WORKTREE_ADD_TIMEOUT_SECONDS = 600


def _git_error_snippet(output: str, limit: int = 300) -> str:
    """Tail of git output with checkout-progress spam dropped.

    git leads stderr with hundreds of "Updating files: N%" lines, so a
    head-truncated snippet hides the actual fatal line at the end.
    """

    parts = [part.strip() for part in re.split(r"[\r\n]+", output) if part.strip()]
    parts = [part for part in parts if not part.startswith("Updating files:")]
    return " ".join(parts)[-limit:]


class RunnerError(ValueError):
    """Raised for unsafe or invalid runner configuration."""


@dataclass(frozen=True)
class RunnerPaths:
    issue_dir: Path
    inputs_dir: Path
    work_dir: Path
    artifacts_dir: Path
    prompt_path: Path
    context_path: Path
    result_path: Path


@dataclass(frozen=True)
class ValidationEvidence:
    description: str
    command: str
    exit_code: int
    evidence: str


@dataclass(frozen=True)
class StructuredResult:
    version: int
    status: str
    summary: str
    validation: tuple[ValidationEvidence, ...]
    artifacts: tuple[str, ...] = ()
    files_changed: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunnerResult:
    issue_id: str
    issue_identifier: str
    command: tuple[str, ...]
    paths: RunnerPaths
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    timeout_seconds: int
    structured_result: StructuredResult | None = None
    result_error: str | None = None
    # Local branch the run's changes were committed to (worktree isolation);
    # never pushed. None when isolation is off or the run changed nothing.
    worktree_branch: str | None = None
    # Operator-facing note about the isolated workspace (e.g. a kept dirty
    # worktree after a failed run, or a commit failure).
    worktree_note: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    @property
    def completed_semantically(self) -> bool:
        """Return true only for transport success plus explicit validated completion."""

        result = self.structured_result
        return bool(
            self.succeeded
            and result is not None
            and result.status == "completed"
            and result.validation
            and all(item.exit_code == 0 and item.evidence.strip() for item in result.validation)
        )


@dataclass(frozen=True)
class ProcessResult:
    issue_id: str
    issue_identifier: str
    status: str
    claimed: bool
    executed: bool
    done_transitioned: bool
    comment_ids: tuple[str | None, ...]
    runner_result: RunnerResult | None = None
    message: str | None = None
    issue_url: str | None = None


class LinearReporter(Protocol):
    def create_comment(self, issue_id: str, body: str) -> str | None: ...

    def list_issue_comments(self, issue_id: str, *, first: int = 50) -> tuple[LinearComment, ...]: ...

    def update_issue_state(self, issue_id: str, state_id: str) -> bool: ...

    def resolve_workflow_states(self, config: LinearConfig) -> dict[WorkflowRole, WorkflowState]: ...


@dataclass(frozen=True)
class _WorkspaceContext:
    """Effective workspace for one run: the base repo or an isolated worktree."""

    project_dir: str
    worktree_path: Path | None = None
    branch: str | None = None
    # Commit resolved from base_ref when the worktree was created; lets finish
    # detect agent-made commits even when the working tree itself is clean.
    base_commit: str | None = None


class ShellRunner:
    """Materialize prompt/context files and execute a no-shell command template."""

    def __init__(self, config: RunnerConfig) -> None:
        if not config.command_template:
            raise RunnerError("RUNNER_COMMAND_TEMPLATE is required")
        self.config = config
        self.command_template = config.command_template

    def _uses_standard_wrapper(self) -> bool:
        try:
            tokens = shlex.split(self.command_template, posix=True)
        except ValueError as exc:
            raise RunnerError(f"Invalid runner command template: {exc}") from exc
        return bool(tokens) and Path(tokens[0]).name == "lao-agent-runner"

    def preflight_workspace(self) -> list[str]:
        """Recheck the fixed repository immediately before an agent execution."""

        if not self._uses_standard_wrapper():
            return []
        # With worktree isolation each run branches from base_ref into its own
        # orchestrator-owned checkout, so the base repository may legitimately
        # be dirty (e.g. an operator reviewing a lao/<issue> branch).
        workspace_errors = validate_project_workspace(
            self.config.project_dir,
            require_clean=not self.config.worktree_isolation,
        )
        return [
            *workspace_errors,
            *validate_agent_worktree_settings(
                self.config, validate_ref=not workspace_errors
            ),
        ]

    # --- per-run git worktree isolation -----------------------------------

    def _isolation_enabled(self) -> bool:
        return self.config.worktree_isolation and self._uses_standard_wrapper()

    @staticmethod
    def _git(cwd: Path | str, *args: str, timeout: int = 120) -> tuple[int, str, str]:
        try:
            completed = subprocess.run(
                ["git", "-C", str(cwd), *args],
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return 1, "", str(exc)
        return completed.returncode, completed.stdout, completed.stderr

    def _unique_branch(self, base: Path, name: str) -> str:
        candidate = name
        suffix = 1
        while self._git(base, "rev-parse", "--verify", "--quiet", f"refs/heads/{candidate}")[0] == 0:
            suffix += 1
            candidate = f"{name}-{suffix}"
        return candidate

    def _remove_worktree(self, base: Path, worktree: Path, *, delete_branch: str | None = None) -> None:
        # Double --force also clears locked registrations: a run killed inside
        # `git worktree add` leaves the entry locked ("initializing"), which a
        # single --force and `worktree prune` both refuse to touch.
        self._git(base, "worktree", "remove", "--force", "--force", str(worktree))
        if worktree.exists():
            shutil.rmtree(worktree, ignore_errors=True)
        self._git(base, "worktree", "prune")
        if delete_branch:
            self._git(base, "branch", "-D", delete_branch)

    def _prepare_workspace(self, issue: LinearIssue) -> _WorkspaceContext:
        if not self._isolation_enabled():
            return _WorkspaceContext(project_dir=self.config.project_dir)
        base = Path(self.config.project_dir)
        safe_name = sanitize_path_part(issue.identifier or issue.id)
        worktree = agent_worktree_root(self.config) / safe_name
        # A previous crashed/kept run may have left a worktree directory, a
        # registration without a directory, or a locked entry from a killed
        # `worktree add`; git refuses the add below over any of them, and only
        # the directory is visible to an exists() check — so always clean.
        self._remove_worktree(base, worktree)
        branch = self._unique_branch(base, f"lao/{safe_name}-{execution_key(issue, self.config)[:8]}")
        worktree.parent.mkdir(parents=True, exist_ok=True)
        code, out, err = self._git(
            base,
            "worktree",
            "add",
            "-b",
            branch,
            str(worktree),
            self.config.base_ref,
            timeout=_WORKTREE_ADD_TIMEOUT_SECONDS,
        )
        if code != 0:
            # `add -b` creates the branch before the registration checks that
            # can fail it, so delete it or every failed retry leaks a branch.
            # A killed add can also leave a locked registration that still
            # references the branch, which makes a plain `branch -D` fail —
            # clear the registration first.
            self._remove_worktree(base, worktree, delete_branch=branch)
            raise RunnerError(
                f"Failed to create isolated worktree for {issue.identifier}: {_git_error_snippet(err or out)}"
            )
        base_commit = self._git(worktree, "rev-parse", "HEAD")[1].strip() or None
        return _WorkspaceContext(
            project_dir=str(worktree),
            worktree_path=worktree,
            branch=branch,
            base_commit=base_commit,
        )

    def _finish_workspace(
        self,
        workspace: _WorkspaceContext,
        issue: LinearIssue,
        *,
        completed: bool,
    ) -> tuple[str | None, str | None]:
        """Commit or preserve the isolated worktree; returns (branch, note)."""

        worktree = workspace.worktree_path
        if worktree is None:
            return None, None
        base = Path(self.config.project_dir)
        code, out, err = self._git(worktree, "status", "--porcelain")
        if code != 0:
            return None, f"Could not inspect isolated worktree {worktree}: {(err or out).strip()[:300]}"
        dirty = bool(out.strip())
        # Agents sometimes run `git commit` inside the worktree themselves, so
        # a clean tree does NOT mean "no work" — deleting the branch then would
        # destroy committed changes (observed live on DEV-26). Compare the
        # branch tip against the resolved base commit it was created from.
        tip = self._git(worktree, "rev-parse", "HEAD")[1].strip()
        agent_committed = bool(workspace.base_commit and tip and tip != workspace.base_commit)
        if not completed:
            if dirty:
                # Keep partial changes for post-mortem; the next run of this
                # issue recreates the worktree from the configured base ref.
                return None, (
                    f"Run did not complete semantically; isolated worktree kept for inspection at "
                    f"{worktree} (branch `{workspace.branch}`)"
                )
            if agent_committed:
                self._remove_worktree(base, worktree)
                return None, (
                    f"Run did not complete semantically but the agent committed work; "
                    f"branch `{workspace.branch}` kept for inspection."
                )
            self._remove_worktree(base, worktree, delete_branch=workspace.branch)
            return None, None
        if not dirty:
            if agent_committed:
                self._remove_worktree(base, worktree)
                return workspace.branch, "Agent committed directly inside the worktree; branch kept as the run result."
            self._remove_worktree(base, worktree, delete_branch=workspace.branch)
            return None, "Run completed without file changes; no branch was kept."
        self._git(worktree, "add", "-A")
        title = f"lao: {issue.identifier} {issue.title}".strip()
        code, out, err = self._git(
            worktree,
            "-c",
            "user.name=Linear Agent Orchestrator",
            "-c",
            "user.email=lao-agent@localhost",
            "commit",
            "--no-verify",
            "-m",
            title,
        )
        if code != 0:
            return None, (
                f"Local commit to `{workspace.branch}` failed; worktree kept at {worktree}: "
                f"{(err or out).strip()[:300]}"
            )
        self._remove_worktree(base, worktree)
        return workspace.branch, None

    def run(
        self,
        issue: LinearIssue,
        on_result_submitted: Callable[[], None] | None = None,
    ) -> RunnerResult:
        workspace = self._prepare_workspace(issue)
        paths = self.materialize(issue, project_dir=workspace.project_dir)
        command = render_command_template(
            self.command_template,
            {
                "prompt_path": str(paths.prompt_path),
                "context_path": str(paths.context_path),
                "artifacts_dir": str(paths.artifacts_dir),
                "result_path": str(paths.result_path),
                "issue_id": issue.id,
                "issue_identifier": issue.identifier,
            },
        )
        env = build_allowlisted_env(self.config.allowed_env)
        if workspace.worktree_path is not None:
            # The standard wrapper reads AGENT_PROJECT_DIR; point it (and the
            # backend) at the isolated worktree instead of the base checkout.
            env["AGENT_PROJECT_DIR"] = str(workspace.worktree_path)
        # Explicit (not allowlist-dependent) so the wrapper picks the one-shot
        # or interactive-TUI launch path deterministically.
        env["AGENT_INTERACTIVE"] = "true" if self.config.interactive else "false"
        # When the interactive console is enabled the backend is hosted inside a
        # per-run private tmux session so an operator can watch (and later steer)
        # it live. The wrapper runs the exact same rendered argv; only the
        # transport wrapping changes, and RunnerResult still records the original
        # backend command so audit/state stay truthful.
        console = self._prepare_console(issue) if self.config.interactive_console else None
        exec_command: list[str] = list(command)
        if console is not None:
            exec_command = [*console.wrapper_argv, *command]
        # Interactive runs can outlive result submission for as long as the
        # operator keeps the TUI open, and _wait_interactive only polls — with
        # PIPE nobody would drain stdout, so a wrapper writing more than the
        # pipe buffer (e.g. the pty host's final output tail) deadlocks and the
        # run never finishes. Back the output with files instead and read them
        # after exit.
        interactive_wait = self.config.interactive and console is not None
        stdout_path = stderr_path = None
        stdout_handle = stderr_handle = None
        if interactive_wait:
            stdout_path = paths.issue_dir / "runner-stdout.log"
            stderr_path = paths.issue_dir / "runner-stderr.log"
            stdout_handle = stdout_path.open("wb")
            stderr_handle = stderr_path.open("wb")
        process = subprocess.Popen(  # noqa: S603 - command is explicitly tokenized without shell=True
            exec_command,
            cwd=paths.work_dir,
            env=env,
            text=stdout_handle is None,
            stdout=stdout_handle if stdout_handle is not None else subprocess.PIPE,
            stderr=stderr_handle if stderr_handle is not None else subprocess.PIPE,
            shell=False,
            start_new_session=True,
        )

        def finish_output(timeout: float | None = None) -> tuple[str, str]:
            """communicate() for PIPE mode; wait + read back files otherwise."""

            if stdout_handle is None:
                return process.communicate(timeout=timeout)
            process.wait(timeout=timeout)
            stdout_handle.flush()
            stderr_handle.flush()
            return (
                stdout_path.read_text(encoding="utf-8", errors="replace"),
                stderr_path.read_text(encoding="utf-8", errors="replace"),
            )

        execution_timeout = (
            self.config.timeout_seconds if self.config.timeout_enabled else None
        )
        try:
            try:
                if self.config.interactive and console is not None:
                    outcome = self._wait_interactive(
                        process,
                        paths.result_path,
                        activity_path=console.log_path,
                        on_result_submitted=on_result_submitted,
                    )
                    if outcome == "timeout":
                        raise subprocess.TimeoutExpired(exec_command, self.config.timeout_seconds)
                    if outcome == "result":
                        # The agent submitted its structured result; the
                        # still-running TUI session is reaped deliberately, so
                        # the transport exit code is the orchestrator's (0),
                        # not the killed TUI's. Reached via autoclose or when
                        # the idle/lifetime deadline passes after submission
                        # (an unattended TUI must not fail a finished run).
                        # Otherwise the default interactive path waits for the
                        # operator/backend to exit the TUI, then loads and
                        # validates the same result file below.
                        self._teardown_console(console, reason="teardown")
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        stdout, stderr = finish_output()
                        stdout = self._console_stdout(console, stdout)
                        structured_result, result_error = load_structured_result(paths.result_path)
                        result = RunnerResult(
                            issue_id=issue.id,
                            issue_identifier=issue.identifier,
                            command=tuple(command),
                            paths=paths,
                            exit_code=0,
                            stdout=stdout,
                            stderr=stderr,
                            timed_out=False,
                            timeout_seconds=self.config.timeout_seconds,
                            structured_result=structured_result,
                            result_error=result_error,
                        )
                        branch, note = self._finish_workspace(workspace, issue, completed=result.completed_semantically)
                        if branch or note:
                            result = replace(result, worktree_branch=branch, worktree_note=note)
                        return result
                    # outcome == "exited": the operator or backend ended the
                    # TUI itself; fall through to the one-shot handling below.
                stdout, stderr = finish_output(timeout=execution_timeout)
                if console is not None:
                    stdout = self._console_stdout(console, stdout)
                structured_result, result_error = load_structured_result(paths.result_path)
                result = RunnerResult(
                    issue_id=issue.id,
                    issue_identifier=issue.identifier,
                    command=tuple(command),
                    paths=paths,
                    exit_code=process.returncode,
                    stdout=stdout,
                    stderr=stderr,
                    timed_out=False,
                    timeout_seconds=self.config.timeout_seconds,
                    structured_result=structured_result,
                    result_error=result_error,
                )
                branch, note = self._finish_workspace(workspace, issue, completed=result.completed_semantically)
                if branch or note:
                    result = replace(result, worktree_branch=branch, worktree_note=note)
                return result
            except subprocess.TimeoutExpired:
                # The backend may have spawned agents, test runners, or language
                # servers. Killing only the direct process would let descendants
                # continue mutating the trusted workspace after timeout. Each run
                # owns a fresh session/process group, so terminate the entire group.
                #
                # With the console enabled the tmux server has daemonized OUT of
                # this process group, so killpg alone would not reach the backend.
                # Tear the private server down (and SIGKILL the recorded backend
                # PID, verified via /proc) before the group kill.
                if console is not None:
                    self._teardown_console(console, reason="timeout")
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                stdout, stderr = finish_output()
                if console is not None:
                    stdout = self._console_stdout(console, stdout)
                _, note = self._finish_workspace(workspace, issue, completed=False)
                return RunnerResult(
                    issue_id=issue.id,
                    issue_identifier=issue.identifier,
                    command=tuple(command),
                    paths=paths,
                    exit_code=None,
                    stdout=stdout,
                    stderr=stderr,
                    timed_out=True,
                    timeout_seconds=self.config.timeout_seconds,
                    worktree_note=note,
                )
        finally:
            for handle in (stdout_handle, stderr_handle):
                if handle is not None:
                    try:
                        handle.close()
                    except OSError:
                        pass
            if console is not None:
                self._cleanup_console(console)

    def _wait_interactive(
        self,
        process: subprocess.Popen,
        result_path: Path,
        activity_path: Path | None = None,
        on_result_submitted: Callable[[], None] | None = None,
    ) -> str:
        """Wait for an interactive run to finish.

        Returns "exited" when the TUI process ends and "timeout" when it does
        not. timeout_seconds is an idle timeout here: any mtime/size change of
        ``activity_path`` (the run's console output log) pushes the deadline
        back, so the session survives as long as the backend keeps producing
        output or the operator keeps the conversation going. max_lifetime_seconds
        caps the run regardless of activity. When timeout_enabled is false,
        neither deadline is applied. A parseable structured result invokes
        ``on_result_submitted`` at most once without ending the default
        interactive session. If ``interactive_autoclose`` is true, also
        returns "result" after that notification, restoring the legacy
        result-driven teardown behavior. A deadline reached *after* the result
        was submitted also returns "result": the work is semantically done and
        only the idle TUI is being reaped, so it must not be reported as a
        timeout failure.
        """

        if self.config.timeout_enabled:
            now = time.monotonic()
            hard_deadline: float | None = now + self.config.max_lifetime_seconds
            idle_deadline: float | None = now + self.config.timeout_seconds
            last_activity = self._activity_signature(activity_path)
        else:
            hard_deadline = None
            idle_deadline = None
            last_activity = None
        result_submitted = False
        while True:
            if not result_submitted:
                structured, _error = load_structured_result(result_path)
                if structured is not None:
                    result_submitted = True
                    if on_result_submitted is not None:
                        on_result_submitted()
                    if self.config.interactive_autoclose:
                        return "result"
            if process.poll() is not None:
                return "exited"
            if hard_deadline is None or idle_deadline is None:
                time.sleep(2.0)
                continue
            now = time.monotonic()
            activity = self._activity_signature(activity_path)
            if activity != last_activity:
                last_activity = activity
                idle_deadline = now + self.config.timeout_seconds
            remaining = min(idle_deadline, hard_deadline) - now
            if remaining <= 0:
                return "result" if result_submitted else "timeout"
            time.sleep(min(2.0, max(0.05, remaining)))

    @staticmethod
    def _activity_signature(path: Path | None) -> tuple[float, int] | None:
        """Console-log identity (mtime, size) used to detect run activity."""

        if path is None:
            return None
        try:
            stat_result = path.stat()
        except OSError:
            return None
        return (stat_result.st_mtime, stat_result.st_size)

    def _prepare_console(self, issue: LinearIssue) -> _ConsoleContext:
        """Allocate per-run console paths keyed by a fresh, tmux-safe run id."""

        run_id = uuid.uuid4().hex
        run_dir = Path(self.config.console_socket_dir) / run_id
        socket_path = run_dir / "tmux.sock"
        log_dir = Path(self.config.console_log_dir) / run_id
        log_path = log_dir / "output.log"
        jsonl_path = log_dir / "lifecycle.jsonl"
        session = f"lao-{run_id}"
        run_dir.mkdir(parents=True, exist_ok=True)
        try:
            run_dir.chmod(0o700)
        except OSError:
            pass
        log_dir.mkdir(parents=True, exist_ok=True)
        backend = self.config.backend
        # Interactive TUI mode genuinely reads the PTY, so it always declares
        # direct input honestly regardless of the static metadata flag.
        supports_direct_input = self.config.console_direct_input or self.config.interactive
        wrapper_argv = (
            "lao-pty-host",
            "--schema-version",
            str(CONSOLE_SCHEMA_VERSION),
            "--run-id",
            run_id,
            "--run-dir",
            str(run_dir),
            "--socket",
            str(socket_path),
            "--session",
            session,
            "--log",
            str(log_path),
            "--jsonl",
            str(jsonl_path),
            "--backend",
            backend,
            "--supports-direct-input",
            "true" if supports_direct_input else "false",
            "--issue-id",
            issue.id,
            "--issue-identifier",
            issue.identifier,
            "--",
        )
        return _ConsoleContext(
            run_id=run_id,
            run_dir=run_dir,
            socket_path=socket_path,
            session=session,
            log_path=log_path,
            jsonl_path=jsonl_path,
            backend=backend,
            supports_direct_input=supports_direct_input,
            wrapper_argv=wrapper_argv,
        )

    def _console_stdout(self, console: _ConsoleContext, fallback: str) -> str:
        """Prefer the ANSI-stripped pty log for the failure-comment body."""

        text = self._read_console_log(console)
        return text if text.strip() else fallback

    def _read_console_log(self, console: _ConsoleContext) -> str:
        try:
            if console.log_path.is_symlink() or not console.log_path.is_file():
                return ""
            with console.log_path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - 1024 * 1024))
                raw = handle.read()
        except OSError:
            return ""
        return _strip_ansi(raw.decode("utf-8", errors="replace"))

    def _teardown_console(self, console: _ConsoleContext, *, reason: str = "timeout") -> None:
        """Kill the escaped private tmux server and the recorded backend subtree.

        killpg on this runner's own group does not reach a daemonized tmux
        server, so this is what actually stops the backend from mutating the
        workspace after a timeout. Steps, in order:

        1. Drop a ``terminated`` marker so the pty host suppresses its own
           terminal ``session_ended`` event — the runner owns the single
           terminal event (``session_killed``) on timeout/autoclose teardown.
        2. SIGKILL the backend's process group, but only after confirming the
           recorded PID still refers to the same process (matching /proc start
           time), so a recycled PID can never be killed by mistake. SIGKILL
           cannot be caught or ignored, so HUP-ignoring in-group descendants die
           too; only descendants that re-``setsid`` escape (the container remains
           the outer backstop, exactly as for the non-console path).
        3. Kill the private tmux server as a belt-and-suspenders reap.
        """

        try:
            console.terminated_path.write_text(reason, encoding="utf-8")
        except OSError:
            pass

        backend_pid = self._read_backend_pid(console)
        recorded_start = self._read_recorded_starttime(console)
        if backend_pid is not None and self._pid_identity_matches(backend_pid, recorded_start):
            self._kill_process_group(backend_pid)
            try:
                os.kill(backend_pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass

        self._kill_tmux_server(console)

        if backend_pid is not None:
            self._verify_pid_dead(backend_pid)
        self._append_console_event(console, "session_killed", {"reason": reason})

    def _kill_process_group(self, pid: int) -> None:
        try:
            pgid = os.getpgid(pid)
        except (ProcessLookupError, PermissionError, OSError):
            return
        # Never signal the runner's own group (would be self-inflicted).
        if pgid in (0, 1) or pgid == os.getpgrp():
            return
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    def _read_recorded_starttime(self, console: _ConsoleContext) -> str | None:
        try:
            value = console.backend_starttime_path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return value or None

    def _pid_identity_matches(self, pid: int, recorded_start: str | None) -> bool:
        """Guard against PID reuse: require the /proc start time to be unchanged.

        If no start time was recorded (best-effort miss) fall back to killing the
        recorded PID — the serial daemon means at most one backend exists, so the
        reuse window is negligible, but the check is applied whenever possible.
        """

        current_start = self._read_proc_starttime(pid)
        if current_start is None:
            # Process already gone (nothing to kill) — treat as non-matching.
            return recorded_start is None
        if recorded_start is None:
            return True
        return current_start == recorded_start

    @staticmethod
    def _read_proc_starttime(pid: int) -> str | None:
        """Field 22 (starttime) of /proc/<pid>/stat, robust to comm parentheses."""

        try:
            data = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            after_comm = data[data.rindex(")") + 1 :].split()
            # After comm the fields start at #3 (state); starttime is field #22.
            return after_comm[19]
        except (ValueError, IndexError):
            return None

    def _kill_tmux_server(self, console: _ConsoleContext) -> None:
        try:
            subprocess.run(
                ["tmux", "-S", str(console.socket_path), "kill-server"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass

    def _read_backend_pid(self, console: _ConsoleContext) -> int | None:
        try:
            raw = console.backend_pid_path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        try:
            pid = int(raw)
        except ValueError:
            return None
        return pid if pid > 1 else None

    @staticmethod
    def _verify_pid_dead(pid: int, attempts: int = 20, interval: float = 0.1) -> bool:
        # EPERM (PermissionError) means the PID EXISTS but is owned by another
        # user — the process is still alive, never treat that as dead.
        for _ in range(attempts):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            except PermissionError:
                pass  # still alive (exists, not signalable)
            except OSError:
                break
            time.sleep(interval)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        return False

    def _append_console_event(self, console: _ConsoleContext, event: str, extra: dict[str, Any]) -> None:
        payload: dict[str, Any] = {
            "event": event,
            "schema_version": CONSOLE_SCHEMA_VERSION,
            "run_id": console.run_id,
        }
        payload.update(extra)
        payload["ts"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        line = json.dumps(payload, separators=(",", ":"))
        try:
            console.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
            with console.jsonl_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            LOGGER.warning("console_event_write_failed", extra={"run_id": console.run_id, "event": event})

    def _cleanup_console(self, console: _ConsoleContext) -> None:
        """Best-effort removal of the private server and socket after a run.

        The bind-mounted log directory is intentionally left for host tooling.
        """

        self._kill_tmux_server(console)
        try:
            if console.socket_path.exists():
                console.socket_path.unlink()
        except OSError:
            pass
        for name in ("argv", "config", "backend_pid", "backend_starttime", "exit_code", "terminated"):
            try:
                (console.run_dir / name).unlink()
            except OSError:
                pass
        try:
            console.run_dir.rmdir()
        except OSError:
            pass

    def materialize(self, issue: LinearIssue, *, project_dir: str | None = None) -> RunnerPaths:
        effective_project_dir = project_dir or self.config.project_dir
        safe_name = sanitize_path_part(issue.identifier or issue.id)
        issue_dir = Path(self.config.work_dir) / safe_name
        inputs_dir = issue_dir / "inputs"
        work_dir = issue_dir / "work"
        artifacts_dir = Path(self.config.artifacts_dir) / safe_name
        if issue_dir.exists() or issue_dir.is_symlink():
            _remove_issue_dir(issue_dir)
        inputs_dir.mkdir(parents=True, exist_ok=False)
        work_dir.mkdir(parents=True, exist_ok=False)
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        prompt_path = inputs_dir / "prompt.md"
        context_path = inputs_dir / "context.json"
        result_path = artifacts_dir / "result.json"
        if result_path.exists() or result_path.is_symlink():
            result_path.unlink()
        prompt_path.write_text(
            render_prompt(issue, project_dir=effective_project_dir, result_path=result_path),
            encoding="utf-8",
        )
        context_path.write_text(
            json.dumps(
                render_context(issue, project_dir=effective_project_dir, result_path=result_path),
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        prompt_path.chmod(0o444)
        context_path.chmod(0o444)
        inputs_dir.chmod(0o555)
        return RunnerPaths(issue_dir, inputs_dir, work_dir, artifacts_dir, prompt_path, context_path, result_path)


def process_issue_once(
    *,
    issue: LinearIssue,
    config: AppConfig,
    state: StateStore,
    linear: LinearReporter,
    runner: ShellRunner,
    owner_id: str,
    move_in_progress: bool = True,
) -> ProcessResult:
    """Process one already-polled issue using local state, Linear reporting, and runner."""

    if not issue.schema.valid:
        return _process_schema_invalid_issue(
            issue=issue,
            config=config,
            state=state,
            linear=linear,
            owner_id=owner_id,
        )

    exec_key = execution_key(issue, config.runner)
    claim = state.acquire_claim(
        issue.id,
        owner_id,
        lease_ttl_seconds=config.lease_ttl_seconds,
        execution_key=exec_key,
        allow_failed_retry=True,
    )
    if not claim.acquired:
        return ProcessResult(
            issue.id,
            issue.identifier,
            "not_claimed",
            False,
            False,
            False,
            (),
            message=_claim_skip_message(claim.claim, exec_key),
            issue_url=issue.url,
        )
    try:
        states = linear.resolve_workflow_states(config.linear)
    except Exception as exc:  # noqa: BLE001 - retryable Linear state lookup failure must not lose local active work
        return _in_progress_transition_pending_result(issue=issue, exc=exc)
    comments: list[str | None] = []
    pending = state.get_finalization(issue.id, exec_key)
    if pending:
        result = _runner_result_from_state(pending["result"])
        return _finalize_success(
            issue=issue,
            result=result,
            state=state,
            linear=linear,
            owner_id=owner_id,
            states=states,
            comments=comments,
            execution_key=exec_key,
            pending=pending,
        )
    if state.has_execution(issue.id, exec_key):
        return _fail_in_doubt_execution(
            issue=issue,
            state=state,
            linear=linear,
            linear_config=config.linear,
            owner_id=owner_id,
            execution_key=exec_key,
            comments=comments,
            message=_in_doubt_execution_message(exec_key),
        )
    workspace_errors = runner.preflight_workspace()
    if workspace_errors:
        return _workspace_blocked_result(
            issue=issue,
            state=state,
            linear=linear,
            owner_id=owner_id,
            execution_key=exec_key,
            errors=workspace_errors,
        )
    state.upsert_active_work(issue.id, exec_key, issue=_issue_to_state(issue), owner_id=owner_id)
    if move_in_progress:
        try:
            in_progress_state = states[WorkflowRole.IN_PROGRESS]
            linear.update_issue_state(issue.id, in_progress_state.id)
            state.upsert_active_work(
                issue.id,
                exec_key,
                issue=_issue_to_state_with_workflow(issue, state_id=in_progress_state.id, state_name=in_progress_state.name),
                owner_id=owner_id,
            )
        except Exception as exc:  # noqa: BLE001 - retryable Linear transition failure must not start runner
            return _in_progress_transition_pending_result(issue=issue, exc=exc)
    if not state.has_comment(issue.id, "started"):
        try:
            comment_id = ensure_linear_comment(state, linear, issue.id, "started", start_comment(issue), owner_id=owner_id)
            comments.append(comment_id)
        except Exception as exc:  # noqa: BLE001 - non-critical comment failure must not block execution
            LOGGER.warning(
                "start_comment_failed",
                extra={
                    "issue_id": issue.id,
                    "issue_identifier": issue.identifier,
                    "error": _exception_summary(exc),
                },
            )

    if not state.record_execution_once(issue.id, exec_key, owner_id=owner_id):
        pending = state.get_finalization(issue.id, exec_key)
        if pending:
            result = _runner_result_from_state(pending["result"])
            return _finalize_success(
                issue=issue,
                result=result,
                state=state,
                linear=linear,
                owner_id=owner_id,
                states=states,
                comments=comments,
                execution_key=exec_key,
                pending=pending,
            )
        return _fail_in_doubt_execution(
            issue=issue,
            state=state,
            linear=linear,
            linear_config=config.linear,
            owner_id=owner_id,
            execution_key=exec_key,
            comments=comments,
            message=_in_doubt_execution_message(exec_key),
        )

    try:
        with LeaseRenewer(state.path, issue.id, owner_id, config.lease_ttl_seconds, config.lease_renewal_interval_seconds):
            if config.runner.interactive:

                def on_result_submitted() -> None:
                    _transition_to_attention_best_effort(issue=issue, linear=linear, states=states)

                result = runner.run(issue, on_result_submitted=on_result_submitted)
            else:
                result = runner.run(issue)
    except (OSError, RunnerError, subprocess.SubprocessError) as exc:
        summary = _exception_summary(exc)
        state.fail_claim(issue.id, owner_id, error=summary, execution_key=exec_key)
        if not state.has_comment(issue.id, f"failure:{exec_key}"):
            try:
                comment_id = ensure_linear_comment(
                    state,
                    linear,
                    issue.id,
                    f"failure:{exec_key}",
                    exception_failure_comment(issue, exc),
                    owner_id=owner_id,
                )
                comments.append(comment_id)
            except Exception:  # noqa: BLE001 - comment failure must not hide local failure
                comments.append(None)
        _transition_to_attention_best_effort(issue=issue, linear=linear, states=states)
        return ProcessResult(
            issue.id,
            issue.identifier,
            "failed",
            True,
            False,
            False,
            tuple(comments),
            message=summary,
            issue_url=issue.url,
        )

    if result.completed_semantically:
        state.upsert_finalization(
            issue.id,
            exec_key,
            result=_runner_result_to_state(result),
            issue=_issue_to_state(issue),
            owner_id=owner_id,
        )
        pending = state.get_finalization(issue.id, exec_key)
        return _finalize_success(
            issue=issue,
            result=result,
            state=state,
            linear=linear,
            owner_id=owner_id,
            states=states,
            comments=comments,
            execution_key=exec_key,
            pending=pending,
        )

    failure_message = _failure_summary(result)
    state.fail_claim(issue.id, owner_id, error=failure_message, execution_key=exec_key)
    if not state.has_comment(issue.id, f"failure:{exec_key}"):
        try:
            comment_id = ensure_linear_comment(
                state,
                linear,
                issue.id,
                f"failure:{exec_key}",
                failure_comment(result),
                owner_id=owner_id,
            )
            comments.append(comment_id)
        except Exception as exc:  # noqa: BLE001 - comment failure must not hide local failure
            comments.append(None)
            _transition_to_attention_best_effort(issue=issue, linear=linear, states=states)
            return ProcessResult(
                issue.id,
                issue.identifier,
                "failed",
                True,
                True,
                False,
                tuple(comments),
                result,
                message=_exception_summary(exc),
                issue_url=issue.url,
            )
    _transition_to_attention_best_effort(issue=issue, linear=linear, states=states)
    return ProcessResult(
        issue.id,
        issue.identifier,
        "failed",
        True,
        True,
        False,
        tuple(comments),
        result,
        message=failure_message,
        issue_url=issue.url,
    )


def _workspace_blocked_result(
    *,
    issue: LinearIssue,
    state: StateStore,
    linear: LinearReporter,
    owner_id: str,
    execution_key: str,
    errors: list[str],
) -> ProcessResult:
    message = "; ".join(errors)
    comments: list[str | None] = []
    comment_key = f"workspace-preflight:{execution_key}"
    if not state.has_comment(issue.id, comment_key):
        try:
            comments.append(
                ensure_linear_comment(
                    state,
                    linear,
                    issue.id,
                    comment_key,
                    workspace_preflight_comment(issue, errors),
                    owner_id=owner_id,
                )
            )
        except Exception:  # noqa: BLE001 - local release still prevents execution
            comments.append(None)
    state.release_claim(issue.id, owner_id)
    return ProcessResult(
        issue.id,
        issue.identifier,
        "workspace_blocked",
        True,
        False,
        False,
        tuple(comments),
        message=message,
        issue_url=issue.url,
    )


def _transition_to_attention_best_effort(
    *,
    issue: LinearIssue,
    linear: LinearReporter,
    states: dict[WorkflowRole, WorkflowState] | None = None,
    config: LinearConfig | None = None,
) -> None:
    """Move a failed run to the human-review queue without changing its result."""

    try:
        if states is None:
            if config is None:
                raise ValueError("Linear config is required when workflow states are not pre-resolved")
            states = linear.resolve_workflow_states(config)
        resolved = states
        linear.update_issue_state(issue.id, resolved[WorkflowRole.ATTENTION].id)
    except Exception as exc:  # noqa: BLE001 - local failure/comment state remains authoritative
        LOGGER.warning(
            "attention_transition_failed",
            extra={
                "issue_id": issue.id,
                "issue_identifier": issue.identifier,
                "error": _exception_summary(exc),
            },
        )


def _fail_in_doubt_execution(
    *,
    issue: LinearIssue,
    state: StateStore,
    linear: LinearReporter,
    linear_config: LinearConfig,
    owner_id: str,
    execution_key: str,
    comments: list[str | None],
    message: str,
) -> ProcessResult:
    state.fail_claim(issue.id, owner_id, error=message, execution_key=execution_key)
    comment_key = f"failure:{execution_key}"
    if not state.has_comment(issue.id, comment_key):
        try:
            comment_id = ensure_linear_comment(
                state,
                linear,
                issue.id,
                comment_key,
                in_doubt_failure_comment(issue, message),
                owner_id=owner_id,
            )
            comments.append(comment_id)
        except Exception as exc:  # noqa: BLE001 - local fail-closed state is authoritative
            comments.append(None)
            message = f"{message}; failure comment error: {_exception_summary(exc)}"
    _transition_to_attention_best_effort(issue=issue, linear=linear, config=linear_config)
    return ProcessResult(
        issue.id,
        issue.identifier,
        "failed_in_doubt",
        True,
        False,
        False,
        tuple(comments),
        message=message,
        issue_url=issue.url,
    )


def _in_progress_transition_pending_result(*, issue: LinearIssue, exc: BaseException) -> ProcessResult:
    summary = _exception_summary(exc)
    message = (
        f"In Progress transition pending for {issue.identifier}: {summary}. "
        "Runner was not started; execution intent was not recorded. "
        "The active claim and execution identity were kept so the daemon can retry safely."
    )
    LOGGER.warning(
        "in_progress_transition_pending",
        extra={
            "issue_id": issue.id,
            "issue_identifier": issue.identifier,
            "error": summary,
        },
    )
    return ProcessResult(
        issue.id,
        issue.identifier,
        "in_progress_transition_pending",
        True,
        False,
        False,
        (),
        message=message,
        issue_url=issue.url,
    )


def _in_doubt_execution_message(execution_key: str) -> str:
    return (
        "Fail-closed recovery: execution intent already exists for "
        f"execution_key={execution_key} but no successful finalization row exists. "
        "The runner was not invoked again to avoid duplicating external side effects; "
        "edit the Linear issue or change the runner command to create a new execution key and retry."
    )


def _config_drift_message(stored_key: str, current_key: str) -> str:
    return (
        "Fail-closed active-work recovery: stored execution_key="
        f"{stored_key} does not match current execution_key={current_key}. "
        "The runner was not invoked with a different current command/config under the stored active work record; "
        "a later fresh Todo poll with the current key may retry."
    )


def _process_schema_invalid_issue(
    *,
    issue: LinearIssue,
    config: AppConfig,
    state: StateStore,
    linear: LinearReporter,
    owner_id: str,
) -> ProcessResult:
    """Create/reconcile malformed-issue clarification under a short owned lease.

    Malformed issues are intentionally non-terminal: the same Linear issue may be
    edited into a valid schema later. The lease only serializes clarification
    comment creation across overlapping daemons and is released after the local
    attempt so valid future updates remain processable.
    """

    comment_key = "schema-missing"
    invalid_key = f"schema:{comment_key}"
    claim = state.acquire_claim(
        issue.id,
        owner_id,
        lease_ttl_seconds=config.lease_ttl_seconds,
        execution_key=invalid_key,
    )
    if not claim.acquired:
        return ProcessResult(
            issue.id,
            issue.identifier,
            "not_claimed",
            False,
            False,
            False,
            (),
            message=claim.claim.status,
            issue_url=issue.url,
        )

    comments: list[str | None] = []
    message: str | None = None
    try:
        if not state.has_comment(issue.id, comment_key):
            try:
                comment_id = ensure_linear_comment(
                    state,
                    linear,
                    issue.id,
                    comment_key,
                    clarification_comment(issue.identifier, issue.schema),
                    owner_id=owner_id,
                )
                comments.append(comment_id)
            except Exception as exc:  # noqa: BLE001 - malformed issue must fail open
                comments.append(None)
                message = _exception_summary(exc)
    finally:
        state.release_claim(issue.id, owner_id)

    return ProcessResult(
        issue.id,
        issue.identifier,
        "schema_invalid",
        True,
        False,
        False,
        tuple(comments),
        message=message,
        issue_url=issue.url,
    )


def process_next_issue_once(
    *,
    config: AppConfig,
    state: StateStore,
    linear,
    runner: ShellRunner,
    owner_id: str,
    first: int = 10,
) -> ProcessResult:
    """Recover pending finalizations, then poll Todo issues and process one candidate."""

    pending = process_pending_finalization_once(config=config, state=state, linear=linear, owner_id=owner_id)
    if pending is not None:
        return pending

    active = process_active_work_once(config=config, state=state, linear=linear, runner=runner, owner_id=owner_id)
    if active is not None:
        return active

    issues = linear.iter_todo_issues(config.linear, first=first)
    skipped_invalid = 0
    skipped_claimed = 0
    skipped_claimed_reasons: dict[str, int] = {}
    last_invalid: ProcessResult | None = None
    for issue in issues:
        result = process_issue_once(
            issue=issue,
            config=config,
            state=state,
            linear=linear,
            runner=runner,
            owner_id=owner_id,
        )
        if result.status == "schema_invalid":
            skipped_invalid += 1
            last_invalid = result
            continue
        if result.status == "not_claimed":
            skipped_claimed += 1
            reason = result.message or "claimed"
            skipped_claimed_reasons[reason] = skipped_claimed_reasons.get(reason, 0) + 1
            continue
        return result
    if skipped_invalid and not skipped_claimed:
        assert last_invalid is not None
        return ProcessResult(
            last_invalid.issue_id,
            last_invalid.issue_identifier,
            "no_executable_candidate",
            False,
            False,
            False,
            last_invalid.comment_ids,
            message=f"Skipped {skipped_invalid} schema-invalid issue(s); no valid executable candidate found",
            issue_url=last_invalid.issue_url,
        )
    claimed_detail = "; ".join(f"{reason}={count}" for reason, count in sorted(skipped_claimed_reasons.items()))
    claimed_summary = f"; skipped_claimed_reasons={claimed_detail}" if claimed_detail else ""
    return ProcessResult(
        "",
        "",
        "no_candidate",
        False,
        False,
        False,
        (),
        message=f"No processable Todo issue found; skipped_invalid={skipped_invalid}; skipped_claimed={skipped_claimed}{claimed_summary}",
    )


def process_active_work_once(
    *,
    config: AppConfig,
    state: StateStore,
    linear: LinearReporter,
    runner: ShellRunner,
    owner_id: str,
) -> ProcessResult | None:
    """Resume one locally persisted active work item that may no longer be Todo.

    Entries held by another owner's live lease (a parallel worker's in-flight
    run) are skipped, not returned: a worker must fall through to polling for
    new Todo issues instead of spinning on a sibling's active work.
    """

    for work in state.list_active_work():
        issue = _issue_from_work(work)
        stored_key = work["execution_key"]
        current_key = execution_key(issue, config.runner)
        if stored_key != current_key:
            claim = state.acquire_claim(
                issue.id,
                owner_id,
                lease_ttl_seconds=config.lease_ttl_seconds,
                execution_key=stored_key,
            )
            if not claim.acquired:
                continue
            return _fail_in_doubt_execution(
                issue=issue,
                state=state,
                linear=linear,
                linear_config=config.linear,
                owner_id=owner_id,
                execution_key=stored_key,
                comments=[],
                message=_config_drift_message(stored_key, current_key),
            )
        result = process_issue_once(
            issue=issue,
            config=config,
            state=state,
            linear=linear,
            runner=runner,
            owner_id=owner_id,
            move_in_progress=_active_work_needs_in_progress_transition(issue, config),
        )
        if result.status == "not_claimed":
            continue
        return result
    return None


def _active_work_needs_in_progress_transition(issue: LinearIssue, config: AppConfig) -> bool:
    """Retry the In Progress transition unless persisted issue metadata says it already happened."""

    return issue.state_name != config.linear.in_progress_state_name


def process_pending_finalization_once(
    *,
    config: AppConfig,
    state: StateStore,
    linear: LinearReporter,
    owner_id: str,
) -> ProcessResult | None:
    """Finalize one persisted successful runner result without relying on Todo polling."""

    for pending in state.list_pending_finalizations():
        issue = _issue_from_finalization(pending)
        claim = state.acquire_claim(
            issue.id,
            owner_id,
            lease_ttl_seconds=config.lease_ttl_seconds,
            execution_key=pending["execution_key"],
        )
        if not claim.acquired:
            continue
        result = _runner_result_from_state(pending["result"])
        states = linear.resolve_workflow_states(config.linear)
        return _finalize_success(
            issue=issue,
            result=result,
            state=state,
            linear=linear,
            owner_id=owner_id,
            states=states,
            comments=[],
            execution_key=pending["execution_key"],
            pending=state.get_finalization(issue.id, pending["execution_key"]) or pending,
        )
    return None


class LeaseRenewer:
    """Renew an active claim on a separate SQLite connection while a runner runs."""

    def __init__(
        self,
        db_path: Path,
        issue_id: str,
        owner_id: str,
        lease_ttl_seconds: int,
        interval_seconds: int,
    ) -> None:
        self.db_path = db_path
        self.issue_id = issue_id
        self.owner_id = owner_id
        self.lease_ttl_seconds = lease_ttl_seconds
        self.interval_seconds = max(0.01, float(interval_seconds))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "LeaseRenewer":
        self._thread = threading.Thread(target=self._run, name=f"lease-renewer-{self.issue_id}", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=max(1.0, self.interval_seconds + 0.5))

    def _run(self) -> None:
        with StateStore(self.db_path) as store:
            while not self._stop.wait(self.interval_seconds):
                if store.renew_claim(self.issue_id, self.owner_id, lease_ttl_seconds=self.lease_ttl_seconds) is None:
                    return


COMMENT_MARKER_NAMESPACE = "linear-agent-orchestrator/comment/v1"


def comment_marker(issue_id: str, comment_key: str) -> str:
    digest = hashlib.sha256()
    digest.update(COMMENT_MARKER_NAMESPACE.encode())
    digest.update(b"\0")
    digest.update(issue_id.encode())
    digest.update(b"\0")
    digest.update(comment_key.encode())
    return f"<!-- lao-comment:{digest.hexdigest()[:32]} -->"


def body_with_comment_marker(issue_id: str, comment_key: str, body: str) -> str:
    marker = comment_marker(issue_id, comment_key)
    return body if marker in body else f"{body.rstrip()}\n\n{marker}"


def ensure_linear_comment(
    state: StateStore,
    linear: LinearReporter,
    issue_id: str,
    comment_key: str,
    body: str,
    *,
    owner_id: str | None = None,
) -> str:
    """Durably ensure exactly one owned Linear comment exists for a stable key."""

    existing_id = state.ensure_comment_intent(issue_id, comment_key, owner_id=owner_id).comment_id
    if existing_id:
        return existing_id
    marker = comment_marker(issue_id, comment_key)
    found = _find_comment_by_marker(linear, issue_id, marker)
    if found is not None:
        state.mark_comment_sent(issue_id, comment_key, found.id, owner_id=owner_id)
        return found.id
    marked_body = body_with_comment_marker(issue_id, comment_key, body)
    created_id = linear.create_comment(issue_id, marked_body)
    if created_id:
        state.mark_comment_sent(issue_id, comment_key, created_id, owner_id=owner_id)
        return created_id
    # Defensive fallback for transports that return no ID despite success.
    found = _find_comment_by_marker(linear, issue_id, marker)
    if found is not None:
        state.mark_comment_sent(issue_id, comment_key, found.id, owner_id=owner_id)
        return found.id
    raise RuntimeError("Linear commentCreate returned no comment id and marker reconciliation found no comment")


def _find_comment_by_marker(linear: LinearReporter, issue_id: str, marker: str) -> LinearComment | None:
    for comment in linear.list_issue_comments(issue_id):
        if marker in comment.body:
            return comment
    return None


def _claim_skip_message(claim, execution_key: str) -> str:
    if claim.status == "failed" and claim.execution_key == execution_key:
        return "failed_same_execution_key"
    if claim.status == "completed":
        return "completed"
    if claim.status in {"claimed", "renewed"}:
        return f"active_lease:{claim.status}"
    return claim.status


def _finalize_success(
    *,
    issue: LinearIssue,
    result: RunnerResult,
    state: StateStore,
    linear: LinearReporter,
    owner_id: str,
    states: dict[WorkflowRole, WorkflowState],
    comments: list[str | None],
    execution_key: str,
    pending: dict[str, Any] | None,
) -> ProcessResult:
    if not result.completed_semantically:
        message = _failure_summary(result)
        state.fail_claim(issue.id, owner_id, error=message, execution_key=execution_key)
        comment_key = f"failure:{execution_key}"
        if not state.has_comment(issue.id, comment_key):
            try:
                comments.append(
                    ensure_linear_comment(
                        state,
                        linear,
                        issue.id,
                        comment_key,
                        failure_comment(result),
                        owner_id=owner_id,
                    )
                )
            except Exception:  # noqa: BLE001 - fail-closed local state is authoritative
                comments.append(None)
        _transition_to_attention_best_effort(issue=issue, linear=linear, states=states)
        return ProcessResult(
            issue.id,
            issue.identifier,
            "failed",
            True,
            False,
            False,
            tuple(comments),
            result,
            message=message,
            issue_url=issue.url,
        )
    pending = pending or state.get_finalization(issue.id, execution_key)
    comment_key = f"result:{execution_key}"
    if not pending or pending.get("result_comment_id") is None:
        try:
            comment_id = _ensure_result_comment(
                state=state,
                linear=linear,
                issue_id=issue.id,
                execution_key=execution_key,
                comment_key=comment_key,
                body=result_comment(result),
                owner_id=owner_id,
            )
        except Exception as exc:  # noqa: BLE001 - keep local pending state for replay
            return ProcessResult(
                issue.id,
                issue.identifier,
                "finalization_pending",
                True,
                True,
                False,
                tuple(comments),
                result,
                message=_exception_summary(exc),
                issue_url=issue.url,
            )
        comments.append(comment_id)
        pending = state.get_finalization(issue.id, execution_key) or pending

    refreshed = state.get_finalization(issue.id, execution_key) or pending
    if refreshed is None:
        raise RuntimeError(f"finalization state missing for issue {issue.id} and execution {execution_key}")
    if not refreshed.get("done_transitioned"):
        try:
            linear.update_issue_state(issue.id, states[WorkflowRole.DONE].id)
        except Exception as exc:  # noqa: BLE001 - replay should retry Done without rerunning
            return ProcessResult(
                issue.id,
                issue.identifier,
                "finalization_pending",
                True,
                True,
                False,
                tuple(comments),
                result,
                message=_exception_summary(exc),
                issue_url=issue.url,
            )
        state.mark_finalization_done(issue.id, execution_key, owner_id=owner_id)

    completed = state.complete_claim(issue.id, owner_id, execution_key=execution_key)
    if completed is None:
        return ProcessResult(
            issue.id,
            issue.identifier,
            "finalization_pending",
            True,
            True,
            True,
            tuple(comments),
            result,
            message="claim completion pending",
            issue_url=issue.url,
        )
    return ProcessResult(
        issue.id,
        issue.identifier,
        "completed",
        True,
        True,
        True,
        tuple(comments),
        result,
        issue_url=issue.url,
    )


def _ensure_result_comment(
    *,
    state: StateStore,
    linear: LinearReporter,
    issue_id: str,
    execution_key: str,
    comment_key: str,
    body: str,
    owner_id: str | None,
) -> str:
    existing_id = state.ensure_comment_intent(issue_id, comment_key, owner_id=owner_id).comment_id
    if existing_id:
        state.record_result_comment_finalization(issue_id, execution_key, comment_key, existing_id, owner_id=owner_id)
        return existing_id
    marker = comment_marker(issue_id, comment_key)
    found = _find_comment_by_marker(linear, issue_id, marker)
    if found is not None:
        state.record_result_comment_finalization(issue_id, execution_key, comment_key, found.id, owner_id=owner_id)
        return found.id
    created_id = linear.create_comment(issue_id, body_with_comment_marker(issue_id, comment_key, body))
    if created_id:
        state.record_result_comment_finalization(issue_id, execution_key, comment_key, created_id, owner_id=owner_id)
        return created_id
    found = _find_comment_by_marker(linear, issue_id, marker)
    if found is not None:
        state.record_result_comment_finalization(issue_id, execution_key, comment_key, found.id, owner_id=owner_id)
        return found.id
    raise RuntimeError("Linear commentCreate returned no comment id and marker reconciliation found no result comment")


def load_structured_result(result_path: Path) -> tuple[StructuredResult | None, str | None]:
    """Load and validate the backend-neutral result file without following symlinks."""

    try:
        if result_path.is_symlink():
            return None, "structured result path must not be a symlink"
        if not result_path.is_file():
            return None, "structured result file was not produced"
        if result_path.stat().st_size > 1024 * 1024:
            return None, "structured result file exceeds 1 MiB"
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, f"structured result could not be read: {_sanitize_error_message(str(exc))}"
    try:
        return _parse_structured_result_payload(payload), None
    except ValueError as exc:
        return None, str(exc)


def _parse_structured_result_payload(payload: object) -> StructuredResult:
    if not isinstance(payload, dict):
        raise ValueError("structured result must be a JSON object")
    version = payload.get("version")
    if type(version) is not int or version != 1:
        raise ValueError("structured result version must be integer 1")
    status = _required_result_string(payload, "status")
    allowed_statuses = {"completed", "failed", "blocked", "needs_clarification"}
    if status not in allowed_statuses:
        raise ValueError(f"structured result status must be one of {', '.join(sorted(allowed_statuses))}")
    summary = _required_result_string(payload, "summary")
    raw_validation = payload.get("validation")
    if not isinstance(raw_validation, list):
        raise ValueError("structured result validation must be a JSON array")
    validation: list[ValidationEvidence] = []
    for index, item in enumerate(raw_validation):
        if not isinstance(item, dict):
            raise ValueError(f"structured result validation[{index}] must be a JSON object")
        exit_code = item.get("exit_code")
        if type(exit_code) is not int:
            raise ValueError(f"structured result validation[{index}].exit_code must be an integer")
        validation.append(
            ValidationEvidence(
                description=_required_result_string(item, "description", prefix=f"validation[{index}]."),
                command=_required_result_string(item, "command", prefix=f"validation[{index}]."),
                exit_code=exit_code,
                evidence=_required_result_string(item, "evidence", prefix=f"validation[{index}]."),
            )
        )
    artifacts = _result_relative_paths(payload, "artifacts")
    files_changed = _result_relative_paths(payload, "files_changed")
    return StructuredResult(version, status, summary, tuple(validation), artifacts, files_changed)


def _required_result_string(payload: Mapping[str, Any], key: str, *, prefix: str = "") -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"structured result {prefix}{key} must be a nonempty string")
    return value.strip()


def _result_relative_paths(payload: Mapping[str, Any], key: str) -> tuple[str, ...]:
    values = payload.get(key, [])
    if not isinstance(values, list):
        raise ValueError(f"structured result {key} must be a JSON array")
    normalized: list[str] = []
    for index, value in enumerate(values):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"structured result {key}[{index}] must be a nonempty string")
        candidate = Path(value)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError(f"structured result {key}[{index}] must be a relative path without '..'")
        normalized.append(value)
    return tuple(normalized)


def _structured_result_to_state(result: StructuredResult | None) -> dict[str, Any] | None:
    if result is None:
        return None
    return {
        "version": result.version,
        "status": result.status,
        "summary": result.summary,
        "validation": [
            {
                "description": item.description,
                "command": item.command,
                "exit_code": item.exit_code,
                "evidence": item.evidence,
            }
            for item in result.validation
        ],
        "artifacts": list(result.artifacts),
        "files_changed": list(result.files_changed),
    }


def _structured_result_from_state(data: object) -> tuple[StructuredResult | None, str | None]:
    if data is None:
        return None, None
    try:
        return _parse_structured_result_payload(data), None
    except ValueError as exc:
        return None, f"persisted structured result is invalid: {exc}"


def _runner_result_to_state(result: RunnerResult) -> dict[str, Any]:
    return {
        "issue_id": result.issue_id,
        "issue_identifier": result.issue_identifier,
        "command": list(result.command),
        "paths": {
            "issue_dir": str(result.paths.issue_dir),
            "inputs_dir": str(result.paths.inputs_dir),
            "work_dir": str(result.paths.work_dir),
            "artifacts_dir": str(result.paths.artifacts_dir),
            "prompt_path": str(result.paths.prompt_path),
            "context_path": str(result.paths.context_path),
            "result_path": str(result.paths.result_path),
        },
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "timed_out": result.timed_out,
        "timeout_seconds": result.timeout_seconds,
        "structured_result": _structured_result_to_state(result.structured_result),
        "result_error": result.result_error,
        "worktree_branch": result.worktree_branch,
        "worktree_note": result.worktree_note,
    }


def _runner_result_from_state(data: dict[str, Any]) -> RunnerResult:
    paths = data["paths"]
    structured_result, persisted_result_error = _structured_result_from_state(data.get("structured_result"))
    return RunnerResult(
        issue_id=data["issue_id"],
        issue_identifier=data["issue_identifier"],
        command=tuple(data.get("command", ())),
        paths=RunnerPaths(
            issue_dir=Path(paths["issue_dir"]),
            inputs_dir=Path(paths["inputs_dir"]),
            work_dir=Path(paths["work_dir"]),
            artifacts_dir=Path(paths["artifacts_dir"]),
            prompt_path=Path(paths["prompt_path"]),
            context_path=Path(paths["context_path"]),
            result_path=Path(paths.get("result_path") or Path(paths["artifacts_dir"]) / "result.json"),
        ),
        exit_code=data["exit_code"],
        stdout=data.get("stdout", ""),
        stderr=data.get("stderr", ""),
        timed_out=bool(data["timed_out"]),
        timeout_seconds=int(data["timeout_seconds"]),
        structured_result=structured_result,
        result_error=data.get("result_error") or persisted_result_error,
        worktree_branch=data.get("worktree_branch"),
        worktree_note=data.get("worktree_note"),
    )


def _issue_to_state(issue: LinearIssue) -> dict[str, Any]:
    return {
        "id": issue.id,
        "identifier": issue.identifier,
        "title": issue.title,
        "description": issue.description,
        "url": issue.url,
        "updated_at": issue.updated_at,
        "state_id": issue.state_id,
        "state_name": issue.state_name,
        "project_id": issue.project_id,
        "project_name": issue.project_name,
        "assignee_id": issue.assignee_id,
        "assignee_name": issue.assignee_name,
    }


def _issue_to_state_with_workflow(issue: LinearIssue, *, state_id: str | None, state_name: str | None) -> dict[str, Any]:
    data = _issue_to_state(issue)
    data["state_id"] = state_id
    data["state_name"] = state_name
    return data


def _issue_from_finalization(pending: dict[str, Any]) -> LinearIssue:
    metadata = pending.get("issue") or {}
    result = pending["result"]
    description = metadata.get("description") or VALID_RECOVERY_ISSUE_BODY
    return LinearIssue(
        id=metadata.get("id") or pending["issue_id"],
        identifier=metadata.get("identifier") or result.get("issue_identifier") or pending["issue_id"],
        title=metadata.get("title") or "Pending runner finalization",
        description=description,
        url=metadata.get("url"),
        updated_at=metadata.get("updated_at") or "recovered",
        state_id=metadata.get("state_id"),
        state_name=metadata.get("state_name") or "In Progress",
        project_id=metadata.get("project_id"),
        project_name=metadata.get("project_name"),
        schema=validate_issue_body(description),
        assignee_id=metadata.get("assignee_id"),
        assignee_name=metadata.get("assignee_name"),
    )


def _issue_from_work(work: dict[str, Any]) -> LinearIssue:
    metadata = work.get("issue") or {}
    description = metadata.get("description") or VALID_RECOVERY_ISSUE_BODY
    return LinearIssue(
        id=metadata.get("id") or work["issue_id"],
        identifier=metadata.get("identifier") or work["issue_id"],
        title=metadata.get("title") or "Active runner work",
        description=description,
        url=metadata.get("url"),
        updated_at=metadata.get("updated_at") or "recovered",
        state_id=metadata.get("state_id"),
        state_name=metadata.get("state_name") or "In Progress",
        project_id=metadata.get("project_id"),
        project_name=metadata.get("project_name"),
        schema=validate_issue_body(description),
        assignee_id=metadata.get("assignee_id"),
        assignee_name=metadata.get("assignee_name"),
    )


VALID_RECOVERY_ISSUE_BODY = """## Task
Recover a previously successful runner finalization.

## Acceptance Criteria
- Evidence comment and Done transition are retried without rerunning.

## Validation
- Local finalization state completes.
"""


def render_command_template(template: str, values: dict[str, str]) -> list[str]:
    """Render a strict no-shell command template.

    The template is tokenized before placeholder replacement. A placeholder token
    must be exactly `{name}`; embedding placeholders in larger shell fragments is
    rejected to prevent issue metadata from altering tokenization or control flow.
    """

    try:
        tokens = shlex.split(template, posix=True)
    except ValueError as exc:
        raise RunnerError(f"Invalid runner command template: {exc}") from exc
    if not tokens:
        raise RunnerError("RUNNER_COMMAND_TEMPLATE must not be empty")
    rendered: list[str] = []
    for token in tokens:
        placeholders = re.findall(r"{([^{}]+)}", token)
        unknown = set(placeholders) - _PLACEHOLDERS
        if unknown:
            raise RunnerError(f"Unknown runner command placeholder(s): {', '.join(sorted(unknown))}")
        if placeholders:
            if len(placeholders) != 1 or token != "{" + placeholders[0] + "}":
                raise RunnerError(f"Unsafe embedded runner placeholder token: {token!r}")
            rendered.append(values[placeholders[0]])
        else:
            rendered.append(token)
    return rendered


def build_allowlisted_env(allowed_env: tuple[str, ...]) -> dict[str, str]:
    return {key: os.environ[key] for key in allowed_env if key in os.environ}


def _remove_issue_dir(issue_dir: Path) -> None:
    """Remove only the per-issue runner directory, even after read-only inputs.

    ``materialize`` intentionally leaves prompt/context files and their parent
    inputs directory read-only for the subprocess. A later run for the same
    issue identifier must still be able to replace that per-issue directory, so
    restore owner write permissions under that boundary before removal.
    """

    if issue_dir.is_symlink():
        issue_dir.unlink()
        return
    for root, dirs, files in os.walk(issue_dir, topdown=False, followlinks=False):
        root_path = Path(root)
        for name in files:
            _restore_owner_write(root_path / name, is_dir=False)
        for name in dirs:
            _restore_owner_write(root_path / name, is_dir=True)
        _restore_owner_write(root_path, is_dir=True)
    shutil.rmtree(issue_dir)


def _restore_owner_write(path: Path, *, is_dir: bool) -> None:
    try:
        if path.is_symlink():
            return
        mode = path.stat().st_mode
        path.chmod(mode | (0o700 if is_dir else 0o600))
    except FileNotFoundError:
        return


def render_prompt(
    issue: LinearIssue,
    *,
    project_dir: str = "/workspace/project",
    result_path: Path | str = "/workspace/artifacts/result.json",
) -> str:
    return "\n".join(
        [
            f"# Linear Issue {issue.identifier}",
            "",
            f"Title: {issue.title}",
            f"URL: {issue.url or ''}",
            "",
            "## Trusted Workspace",
            f"The only trusted repository is mounted at `{project_dir}`. Work only inside that repository.",
            "If the issue mentions another absolute repository path, treat it as referring to this configured workspace.",
            "Do not access unrelated host paths, push changes, or create or merge pull requests.",
            "Use the host Docker API only when `DOCKER_HOST` is explicitly present in your environment.",
            "In that trusted mode you may build and run sibling project containers; the current repository/worktree path is host-identical so relative Compose bind mounts are valid.",
            "Run sibling containers that write bind-mounted files as your current numeric UID:GID, not as root, so worktree cleanup remains possible.",
            "If a child needs Git metadata from an isolated worktree, also mount `${LAO_HOST_PROJECT_DIR}/.git` at that same absolute path; the worktree `.git` file points there.",
            "Stop and remove sibling containers before submitting the structured result so an active bind mount cannot outlive worktree cleanup.",
            "For a host-visible GUI, configure the sibling container with DISPLAY/XAUTHORITY and mount /tmp/.X11-unix; use GLFW rather than EGL for a real MuJoCo window.",
            "The operator can see that window, but you still need logs, screenshots, or automated checks as machine-verifiable debugging evidence.",
            "",
            "## Task",
            issue.schema.parsed.get("Task"),
            "",
            "## Acceptance Criteria",
            issue.schema.parsed.get("Acceptance Criteria"),
            "",
            "## Validation",
            issue.schema.parsed.get("Validation"),
            "",
            "## Required Structured Result",
            f"Before finishing, write a JSON result to `{result_path}` with this exact contract:",
            "```json",
            '{"version":1,"status":"completed|failed|blocked|needs_clarification","summary":"outcome",'
            '"validation":[{"description":"check","command":"command","exit_code":0,"evidence":"output"}],'
            '"artifacts":["relative/path"],"files_changed":["relative/path"]}',
            "```",
            "Linear moves to Done only for status `completed` with nonempty validation evidence and all exit codes equal to 0.",
            "Transport exit code 0 without this file is a failure.",
            "",
        ]
    )


def render_context(
    issue: LinearIssue,
    *,
    project_dir: str = "/workspace/project",
    result_path: Path | str = "/workspace/artifacts/result.json",
) -> dict[str, object]:
    return {
        "issue": {
            "id": issue.id,
            "identifier": issue.identifier,
            "title": issue.title,
            "url": issue.url,
            "updated_at": issue.updated_at,
            "state": {"id": issue.state_id, "name": issue.state_name},
            "project": {"id": issue.project_id, "name": issue.project_name},
            "assignee": {"id": issue.assignee_id, "name": issue.assignee_name},
        },
        "workspace": {"project_dir": project_dir, "trusted_repository_count": 1},
        "result_contract": {"version": 1, "path": str(result_path)},
        "sections": {
            "task": issue.schema.parsed.get("Task"),
            "acceptance_criteria": issue.schema.parsed.get("Acceptance Criteria"),
            "validation": issue.schema.parsed.get("Validation"),
        },
    }


def execution_key(issue: LinearIssue, runner_config: RunnerConfig) -> str:
    digest = hashlib.sha256()
    digest.update(issue.id.encode())
    digest.update(b"\0")
    digest.update(issue.updated_at.encode())
    digest.update(b"\0")
    digest.update(issue.description.encode())
    digest.update(b"\0")
    digest.update((runner_config.command_template or "").encode())
    digest.update(b"\0")
    digest.update(runner_config.project_dir.encode())
    digest.update(b"\0")
    digest.update(runner_config.backend.encode())
    digest.update(b"\0")
    digest.update(runner_config.base_ref.encode())
    return digest.hexdigest()


def start_comment(issue: LinearIssue) -> str:
    return f"Started processing `{issue.identifier}` with the generic shell runner."


def result_comment(result: RunnerResult) -> str:
    return _result_body("Runner completed successfully", result)


def workspace_preflight_comment(issue: LinearIssue, errors: list[str]) -> str:
    details = "\n".join(f"- {redact_sensitive_text(error)}" for error in errors)
    return "\n".join(
        [
            "Agent workspace preflight blocked execution",
            "",
            f"Issue: `{issue.identifier}`",
            details,
            "",
            "The runner was not invoked and the issue was not moved to Done.",
            "Clean or repair the configured trusted repository; the same execution can then be retried.",
        ]
    )


def failure_comment(result: RunnerResult) -> str:
    if result.timed_out:
        title = "Runner timed out"
    elif result.succeeded:
        title = "Runner did not report semantic completion"
    else:
        title = "Runner failed"
    return _result_body(title, result)


def in_doubt_failure_comment(issue: LinearIssue, message: str) -> str:
    return "\n".join(
        [
            "Runner recovery failed closed because prior execution state is in doubt",
            "",
            f"Issue: `{issue.identifier}`",
            f"Error: `{_sanitize_error_message(message)}`",
            "",
            "The runner was not invoked and the issue was not moved to Done.",
            "Edit the Linear issue or change the runner command to create a new execution key and retry.",
        ]
    )


def _result_body(title: str, result: RunnerResult) -> str:
    lines = [
        title,
        "",
        f"Issue: `{result.issue_identifier}`",
        f"Exit code: `{result.exit_code}`",
        f"Timed out: `{result.timed_out}`",
        f"Artifacts: `{result.paths.artifacts_dir}`",
        f"Structured result: `{result.paths.result_path}`",
    ]
    if result.worktree_branch:
        lines.append(f"Local branch (not pushed): `{result.worktree_branch}` — review and merge or discard manually.")
    if result.worktree_note:
        lines.append(f"Workspace note: {redact_sensitive_text(result.worktree_note)}")
    stdout = _truncate_tail(redact_sensitive_text(result.stdout.strip()))
    stderr = _truncate_tail(redact_sensitive_text(result.stderr.strip()))
    if stdout:
        lines.extend(["", "Stdout:", "```", stdout, "```"])
    if stderr:
        lines.extend(["", "Stderr:", "```", stderr, "```"])
    if result.result_error:
        lines.extend(["", f"Result contract error: `{_sanitize_error_message(result.result_error)}`"])
    if result.structured_result is not None:
        lines.extend(
            [
                "",
                f"Semantic status: `{result.structured_result.status}`",
                f"Summary: {redact_sensitive_text(result.structured_result.summary)}",
                "Validation:",
            ]
        )
        lines.extend(
            f"- `{item.command}` -> `{item.exit_code}`: {redact_sensitive_text(item.evidence)}"
            for item in result.structured_result.validation
        )
    return "\n".join(lines)


def exception_failure_comment(issue: LinearIssue, exc: BaseException) -> str:
    return "\n".join(
        [
            "Runner setup or execution failed before a successful result was produced",
            "",
            f"Issue: `{issue.identifier}`",
            f"Error type: `{type(exc).__name__}`",
            f"Error: `{_sanitize_error_message(str(exc))}`",
            "",
            "The issue was not moved to Done.",
        ]
    )


def _exception_summary(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {_sanitize_error_message(str(exc))}"


def _sanitize_error_message(message: str, limit: int = 500) -> str:
    sanitized = redact_sensitive_text(message).replace("`", "'").replace("\n", " ").replace("\r", " ")
    return _truncate(sanitized, limit)


def redact_sensitive_text(value: str) -> str:
    """Use the console contract's tested redactor for every Linear message."""

    return redact_text(value)


def _failure_summary(result: RunnerResult) -> str:
    if result.timed_out:
        return f"runner timed out after {result.timeout_seconds}s"
    if result.succeeded:
        return result.result_error or _semantic_failure_reason(result.structured_result)
    return f"runner exited with code {result.exit_code}"


def _semantic_failure_reason(result: StructuredResult | None) -> str:
    if result is None:
        return "runner did not report semantic completion"
    if result.status != "completed":
        return f"structured result status was {result.status!r}"
    if not result.validation:
        return "structured result completed without validation evidence"
    failed = [item for item in result.validation if item.exit_code != 0 or not item.evidence.strip()]
    if failed:
        return "structured result validation did not pass"
    return "runner did not report semantic completion"


def _truncate(value: str, limit: int = 4000) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "\n... <truncated>"


def _truncate_tail(value: str, limit: int = 4000) -> str:
    """Keep the failure-side tail, where command errors normally appear."""

    if len(value) <= limit:
        return value
    return "... <truncated; showing tail>\n" + value[-limit:]


def sanitize_path_part(value: str) -> str:
    sanitized = _SAFE_PATH_PART_RE.sub("-", value).strip(".-")
    return sanitized or "issue"
