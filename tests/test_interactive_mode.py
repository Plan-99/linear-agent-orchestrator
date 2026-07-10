"""Interactive conversation mode: config gating, adapter argv, result-driven completion."""

from pathlib import Path
import os
import shutil
import stat
import subprocess
import tempfile
import time

import pytest

from linear_agent_orchestrator.config import RunnerConfig, load_config
from linear_agent_orchestrator.issue_schema import validate_issue_body
from linear_agent_orchestrator.linear_client import LinearIssue
from linear_agent_orchestrator.runner import ShellRunner

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_RUNNER = REPO_ROOT / "scripts" / "agent-runner.sh"
PTY_HOST = REPO_ROOT / "scripts" / "lao-pty-host.sh"
PTY_ENTRY = REPO_ROOT / "scripts" / "lao-pty-entry.sh"
HAVE_TMUX = shutil.which("tmux") is not None

VALID_BODY = """## Task
Talk it through.

## Acceptance Criteria
- Result submitted

## Validation
- Check result
"""

COMPLETED_RESULT = (
    '{"version":1,"status":"completed","summary":"done",'
    '"validation":[{"description":"t","command":"true","exit_code":0,"evidence":"ok"}]}'
)


def make_issue(identifier="DEV-CHAT"):
    return LinearIssue(
        id=f"issue-{identifier}",
        identifier=identifier,
        title="Interactive story",
        description=VALID_BODY,
        url=None,
        updated_at="2026-07-10T16:00:00Z",
        state_id="state-todo",
        state_name="Todo",
        project_id="project-test",
        project_name="TEST",
        schema=validate_issue_body(VALID_BODY),
    )


# --------------------------------------------------------------------------- #
# Config gating
# --------------------------------------------------------------------------- #

def test_agent_interactive_parses_and_requires_console():
    cfg = load_config({"AGENT_INTERACTIVE": "true", "AGENT_CONSOLE": "true"}, env_files=[])
    assert cfg.runner.interactive is True
    errors = cfg.validate_startup(require_secrets=False)
    assert not any("AGENT_INTERACTIVE" in error for error in errors)

    headless = load_config({"AGENT_INTERACTIVE": "true", "AGENT_CONSOLE": "false"}, env_files=[])
    errors = headless.validate_startup(require_secrets=False)
    assert any("AGENT_INTERACTIVE=true requires AGENT_CONSOLE=true" in error for error in errors)


def test_interactive_defaults_off():
    cfg = load_config({}, env_files=[])
    assert cfg.runner.interactive is False


def test_interactive_console_declares_direct_input(tmp_path):
    config = RunnerConfig(
        command_template="/bin/true {prompt_path} {result_path}",
        work_dir=str(tmp_path / "work"),
        artifacts_dir=str(tmp_path / "artifacts"),
        interactive_console=True,
        interactive=True,
        console_direct_input=False,
        console_log_dir=str(tmp_path / "logs"),
        console_socket_dir=str(tmp_path / "sock"),
    )
    console = ShellRunner(config)._prepare_console(make_issue())
    argv = list(console.wrapper_argv)
    flag_value = argv[argv.index("--supports-direct-input") + 1]
    assert flag_value == "true"
    assert console.supports_direct_input is True


# --------------------------------------------------------------------------- #
# Adapter argv (interactive TUI vs one-shot)
# --------------------------------------------------------------------------- #

def make_fake_backend(tmp_path, binary):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / binary
    fake.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$@\" > \"$CAPTURE_ARGS\"\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "CAPTURE_ARGS": str(tmp_path / "args"),
    }


@pytest.mark.parametrize(
    ("backend", "binary"),
    [("omx", "omx"), ("codex", "codex"), ("claude", "claude")],
)
def test_agent_runner_interactive_launches_tui_with_prompt_argument(tmp_path, backend, binary):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    captures = make_fake_backend(tmp_path, binary)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("please discuss this issue\n", encoding="utf-8")
    env = {
        **os.environ,
        **captures,
        "AGENT_BACKEND": backend,
        "AGENT_PROJECT_DIR": str(project_dir),
        "AGENT_INTERACTIVE": "true",
        "AGENT_MODEL": "test-model",
        "AGENT_BYPASS_PERMISSIONS": "true",
    }
    completed = subprocess.run(
        [str(AGENT_RUNNER), str(prompt), str(tmp_path / "result.json")],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    args = Path(captures["CAPTURE_ARGS"]).read_text(encoding="utf-8").splitlines()
    assert "exec" not in args
    assert "--print" not in args
    assert "-" not in args
    assert args[-1] == "please discuss this issue"
    assert "--model" in args and "test-model" in args
    if backend in ("omx", "codex"):
        assert "--dangerously-bypass-approvals-and-sandbox" in args
        assert "--skip-git-repo-check" not in args
    else:
        assert "--dangerously-skip-permissions" in args


# --------------------------------------------------------------------------- #
# Result-driven completion
# --------------------------------------------------------------------------- #

class FakeProcess:
    def __init__(self, exits_after_polls=None):
        self.polls = 0
        self.exits_after_polls = exits_after_polls

    def poll(self):
        self.polls += 1
        if self.exits_after_polls is not None and self.polls > self.exits_after_polls:
            return 0
        return None


def interactive_config(tmp_path, command, *, timeout=6):
    return RunnerConfig(
        command_template=command,
        timeout_seconds=timeout,
        work_dir=str(tmp_path / "work"),
        artifacts_dir=str(tmp_path / "artifacts"),
        allowed_env=("PATH", "HOME", "LAO_PTY_ENTRY"),
        interactive_console=True,
        interactive=True,
        console_log_dir=str(tmp_path / "logs"),
        console_socket_dir=tempfile.mkdtemp(prefix="li"),
    )


def test_wait_interactive_detects_result_then_exit_then_timeout(tmp_path):
    config = interactive_config(tmp_path, "/bin/true {prompt_path} {result_path}", timeout=1)
    runner = ShellRunner(config)
    result_path = tmp_path / "result.json"

    # Result file appears -> "result".
    result_path.write_text(COMPLETED_RESULT, encoding="utf-8")
    assert runner._wait_interactive(FakeProcess(), result_path) == "result"

    # Process exit wins when there is no result.
    result_path.unlink()
    assert runner._wait_interactive(FakeProcess(exits_after_polls=1), result_path) == "exited"

    # Neither -> timeout after timeout_seconds.
    started = time.monotonic()
    assert runner._wait_interactive(FakeProcess(), result_path) == "timeout"
    assert time.monotonic() - started < 5


@pytest.mark.skipif(not HAVE_TMUX, reason="tmux not available")
def test_interactive_run_completes_when_result_is_submitted(tmp_path, monkeypatch):
    bin_dir = tmp_path / "console-bin"
    bin_dir.mkdir()
    for name, src in (("lao-pty-host", PTY_HOST), ("lao-pty-entry", PTY_ENTRY)):
        (bin_dir / name).symlink_to(src)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("LAO_PTY_ENTRY", str(bin_dir / "lao-pty-entry"))

    # A "TUI" that, like real interactive backends, refuses to run unless its
    # stdout is a terminal (regression: tee-captured stdout broke codex with
    # "stdout is not a terminal"), writes its structured result, then keeps
    # running forever; only the orchestrator's result-detection ends this run.
    backend = tmp_path / "fake-tui"
    backend.write_text(
        "#!/usr/bin/env bash\n"
        "[ -t 1 ] || { echo 'fake-tui: stdout is not a terminal' >&2; exit 91; }\n"
        "echo 'fake-tui banner'\n"
        "sleep 1\n"
        f"printf '%s' '{COMPLETED_RESULT}' > \"$2\"\n"
        "sleep 120\n",
        encoding="utf-8",
    )
    backend.chmod(backend.stat().st_mode | stat.S_IEXEC)

    config = interactive_config(tmp_path, f"{backend} {{prompt_path}} {{result_path}}", timeout=30)
    started = time.monotonic()
    result = ShellRunner(config).run(make_issue())
    elapsed = time.monotonic() - started

    assert result.exit_code == 0
    assert not result.timed_out
    assert result.completed_semantically
    assert elapsed < 25, f"result-driven teardown should not wait for the TUI ({elapsed:.1f}s)"
