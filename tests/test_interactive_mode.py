"""Interactive conversation mode: config gating, adapter argv, operator-exit completion."""

from pathlib import Path
import os
import shutil
import stat
import subprocess
import tempfile
import threading
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
    cfg = load_config({"APP_ENV": "test"}, env_files=[])
    assert cfg.runner.interactive is False
    assert cfg.runner.interactive_autoclose is False

    autoclose = load_config({"AGENT_INTERACTIVE_AUTOCLOSE": "true"}, env_files=[])
    assert autoclose.runner.interactive_autoclose is True


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


def interactive_config(tmp_path, command, *, timeout=6, autoclose=False, max_lifetime=21600):
    return RunnerConfig(
        command_template=command,
        timeout_seconds=timeout,
        max_lifetime_seconds=max_lifetime,
        work_dir=str(tmp_path / "work"),
        artifacts_dir=str(tmp_path / "artifacts"),
        allowed_env=("PATH", "HOME", "LAO_PTY_ENTRY"),
        interactive_console=True,
        interactive=True,
        interactive_autoclose=autoclose,
        console_log_dir=str(tmp_path / "logs"),
        console_socket_dir=tempfile.mkdtemp(prefix="li"),
    )


def test_wait_interactive_default_waits_for_exit_and_keeps_timeout(tmp_path):
    config = interactive_config(tmp_path, "/bin/true {prompt_path} {result_path}", timeout=1)
    runner = ShellRunner(config)
    result_path = tmp_path / "result.json"

    # A result file alone no longer ends an interactive run.
    result_path.write_text(COMPLETED_RESULT, encoding="utf-8")
    started = time.monotonic()
    assert runner._wait_interactive(FakeProcess(), result_path) == "timeout"
    assert time.monotonic() - started < 5

    # Process exit is the normal completion path even after a result exists.
    assert runner._wait_interactive(FakeProcess(exits_after_polls=1), result_path) == "exited"

    # Neither -> timeout after timeout_seconds.
    result_path.unlink()
    started = time.monotonic()
    assert runner._wait_interactive(FakeProcess(), result_path) == "timeout"
    assert time.monotonic() - started < 5


# --------------------------------------------------------------------------- #
# Activity-based idle timeout + hard lifetime cap
# --------------------------------------------------------------------------- #

class TimedFakeProcess:
    """poll() returns None until the wall-clock deadline, then 0."""

    def __init__(self, exits_after_seconds):
        self.deadline = time.monotonic() + exits_after_seconds

    def poll(self):
        return 0 if time.monotonic() >= self.deadline else None


def feed_activity(log_path, stop, interval=0.2):
    def writer():
        tick = 0
        while not stop.is_set():
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(f"tick {tick}\n")
            tick += 1
            stop.wait(interval)

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    return thread


def test_wait_interactive_continuing_activity_resets_idle_timeout(tmp_path):
    config = interactive_config(tmp_path, "/bin/true {prompt_path} {result_path}", timeout=1)
    runner = ShellRunner(config)
    log_path = tmp_path / "output.log"
    log_path.write_text("boot\n", encoding="utf-8")

    stop = threading.Event()
    thread = feed_activity(log_path, stop)
    started = time.monotonic()
    try:
        # The TUI exits well after timeout_seconds; ongoing console output must
        # keep pushing the idle deadline back until then.
        outcome = runner._wait_interactive(
            TimedFakeProcess(exits_after_seconds=2.5), tmp_path / "result.json", activity_path=log_path
        )
    finally:
        stop.set()
        thread.join(timeout=5)
    elapsed = time.monotonic() - started

    assert outcome == "exited"
    assert elapsed >= 2.0, f"activity should extend the run past timeout_seconds ({elapsed:.1f}s)"


def test_wait_interactive_idle_timeout_fires_when_activity_stops(tmp_path):
    config = interactive_config(tmp_path, "/bin/true {prompt_path} {result_path}", timeout=1)
    runner = ShellRunner(config)
    log_path = tmp_path / "output.log"
    log_path.write_text("boot\n", encoding="utf-8")

    stop = threading.Event()
    thread = feed_activity(log_path, stop)
    timer = threading.Timer(1.5, stop.set)
    timer.start()
    started = time.monotonic()
    try:
        outcome = runner._wait_interactive(FakeProcess(), tmp_path / "result.json", activity_path=log_path)
    finally:
        timer.cancel()
        stop.set()
        thread.join(timeout=5)
    elapsed = time.monotonic() - started

    assert outcome == "timeout"
    assert elapsed >= 2.0, f"idle timeout should count from the last activity ({elapsed:.1f}s)"
    assert elapsed < 8, f"idle timeout should fire ~timeout_seconds after activity stops ({elapsed:.1f}s)"


def test_wait_interactive_hard_cap_overrides_activity(tmp_path):
    config = interactive_config(
        tmp_path, "/bin/true {prompt_path} {result_path}", timeout=30, max_lifetime=1
    )
    runner = ShellRunner(config)
    log_path = tmp_path / "output.log"
    log_path.write_text("boot\n", encoding="utf-8")

    stop = threading.Event()
    thread = feed_activity(log_path, stop)
    started = time.monotonic()
    try:
        outcome = runner._wait_interactive(FakeProcess(), tmp_path / "result.json", activity_path=log_path)
    finally:
        stop.set()
        thread.join(timeout=5)
    elapsed = time.monotonic() - started

    assert outcome == "timeout"
    assert elapsed < 5, f"hard cap must end the run despite continuous activity ({elapsed:.1f}s)"


def test_wait_interactive_autoclose_detects_result(tmp_path):
    config = interactive_config(
        tmp_path,
        "/bin/true {prompt_path} {result_path}",
        timeout=1,
        autoclose=True,
    )
    runner = ShellRunner(config)
    result_path = tmp_path / "result.json"

    result_path.write_text(COMPLETED_RESULT, encoding="utf-8")
    assert runner._wait_interactive(FakeProcess(), result_path) == "result"


@pytest.mark.skipif(not HAVE_TMUX, reason="tmux not available")
def test_interactive_run_with_large_output_does_not_deadlock(tmp_path, monkeypatch):
    """Regression: the pty host's final output tail (up to 100KB) exceeded the
    OS pipe buffer while _wait_interactive only polled, deadlocking the run
    until timeout (observed live on DEV-24 with a real claude TUI)."""

    bin_dir = tmp_path / "console-bin"
    bin_dir.mkdir()
    for name, src in (("lao-pty-host", PTY_HOST), ("lao-pty-entry", PTY_ENTRY)):
        (bin_dir / name).symlink_to(src)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("LAO_PTY_ENTRY", str(bin_dir / "lao-pty-entry"))

    backend = tmp_path / "fake-tui"
    backend.write_text(
        "#!/usr/bin/env bash\n"
        "[ -t 1 ] || { echo 'not a terminal' >&2; exit 91; }\n"
        # Well past the pty host's 100KB stdout tail and the ~64KB pipe buffer.
        "for i in $(seq 1 4000); do echo \"line $i: 0123456789012345678901234567890123456789\"; done\n"
        f"printf '%s' '{COMPLETED_RESULT}' > \"$2\"\n"
        "exit 0\n",
        encoding="utf-8",
    )
    backend.chmod(backend.stat().st_mode | stat.S_IEXEC)

    config = interactive_config(tmp_path, f"{backend} {{prompt_path}} {{result_path}}", timeout=25)
    started = time.monotonic()
    result = ShellRunner(config).run(make_issue())
    elapsed = time.monotonic() - started

    assert not result.timed_out, "large output must not deadlock the interactive wait"
    assert result.exit_code == 0
    assert result.completed_semantically
    assert elapsed < 20, f"run should end promptly after TUI exit ({elapsed:.1f}s)"


@pytest.mark.skipif(not HAVE_TMUX, reason="tmux not available")
def test_interactive_run_waits_for_tui_exit_after_result_by_default(tmp_path, monkeypatch):
    bin_dir = tmp_path / "console-bin"
    bin_dir.mkdir()
    for name, src in (("lao-pty-host", PTY_HOST), ("lao-pty-entry", PTY_ENTRY)):
        (bin_dir / name).symlink_to(src)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("LAO_PTY_ENTRY", str(bin_dir / "lao-pty-entry"))

    # A "TUI" that, like real interactive backends, refuses to run unless its
    # stdout is a terminal (regression: tee-captured stdout broke codex with
    # "stdout is not a terminal"), writes its structured result, then keeps
    # talking before the operator/backend exits the TUI.
    backend = tmp_path / "fake-tui"
    backend.write_text(
        "#!/usr/bin/env bash\n"
        "[ -t 1 ] || { echo 'fake-tui: stdout is not a terminal' >&2; exit 91; }\n"
        "echo 'fake-tui banner'\n"
        "sleep 1\n"
        f"printf '%s' '{COMPLETED_RESULT}' > \"$2\"\n"
        "echo 'fake-tui result submitted'\n"
        "sleep 2\n"
        "echo 'fake-tui operator exit'\n",
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
    assert "fake-tui operator exit" in result.stdout
    assert elapsed >= 3, f"default interactive mode should wait for TUI exit ({elapsed:.1f}s)"


@pytest.mark.skipif(not HAVE_TMUX, reason="tmux not available")
def test_interactive_run_autoclose_tears_down_when_result_is_submitted(tmp_path, monkeypatch):
    bin_dir = tmp_path / "console-bin"
    bin_dir.mkdir()
    for name, src in (("lao-pty-host", PTY_HOST), ("lao-pty-entry", PTY_ENTRY)):
        (bin_dir / name).symlink_to(src)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("LAO_PTY_ENTRY", str(bin_dir / "lao-pty-entry"))

    backend = tmp_path / "fake-tui"
    backend.write_text(
        "#!/usr/bin/env bash\n"
        "[ -t 1 ] || { echo 'fake-tui: stdout is not a terminal' >&2; exit 91; }\n"
        "echo 'fake-tui banner'\n"
        "sleep 1\n"
        f"printf '%s' '{COMPLETED_RESULT}' > \"$2\"\n"
        "echo 'fake-tui result submitted'\n"
        "sleep 120\n",
        encoding="utf-8",
    )
    backend.chmod(backend.stat().st_mode | stat.S_IEXEC)

    config = interactive_config(
        tmp_path,
        f"{backend} {{prompt_path}} {{result_path}}",
        timeout=30,
        autoclose=True,
    )
    started = time.monotonic()
    result = ShellRunner(config).run(make_issue())
    elapsed = time.monotonic() - started

    assert result.exit_code == 0
    assert not result.timed_out
    assert result.completed_semantically
    assert elapsed < 25, f"autoclose should not wait for the TUI ({elapsed:.1f}s)"
