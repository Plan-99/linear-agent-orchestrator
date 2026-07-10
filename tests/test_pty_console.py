"""Focused smoke tests for the model-neutral interactive console (worker-1 scope).

Covers the container/core execution boundary: config gating, flag-off argv
invariance, flag-on wrapping with real tmux, Done-gate parity, lifecycle JSONL
contract v1, and hardened timeout teardown (single terminal event + descendant
containment). Real-tmux cases skip cleanly where tmux is unavailable.
"""

from pathlib import Path
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
import uuid

import pytest

from linear_agent_orchestrator.config import RunnerConfig, load_config
from linear_agent_orchestrator.issue_schema import validate_issue_body
from linear_agent_orchestrator.linear_client import LinearIssue
from linear_agent_orchestrator import runner as runner_mod
from linear_agent_orchestrator.runner import ShellRunner

REPO_ROOT = Path(__file__).resolve().parents[1]
PTY_HOST = REPO_ROOT / "scripts" / "lao-pty-host.sh"
PTY_ENTRY = REPO_ROOT / "scripts" / "lao-pty-entry.sh"
HAVE_TMUX = shutil.which("tmux") is not None

VALID_BODY = """## Task
Write an artifact.

## Acceptance Criteria
- Artifact exists

## Validation
- Inspect output
"""


def make_issue(identifier="DEV-CONSOLE"):
    return LinearIssue(
        id=f"issue-{identifier}",
        identifier=identifier,
        title="Console story",
        description=VALID_BODY,
        url="https://linear.app/test/issue/DEV-CONSOLE",
        updated_at="2026-07-10T00:00:00Z",
        state_id="state-todo",
        state_name="Todo",
        project_id="project-test",
        project_name="TEST",
        schema=validate_issue_body(VALID_BODY),
    )


def _console_bin(tmp_path):
    """A bin dir exposing lao-pty-host / lao-pty-entry by their installed names."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, src in (("lao-pty-host", PTY_HOST), ("lao-pty-entry", PTY_ENTRY)):
        link = bin_dir / name
        link.symlink_to(src)
    return bin_dir


def _console_config(tmp_path, command, *, timeout=5, direct_input=False):
    return RunnerConfig(
        command_template=command,
        timeout_seconds=timeout,
        work_dir=str(tmp_path / "work"),
        artifacts_dir=str(tmp_path / "artifacts"),
        allowed_env=("PATH", "HOME", "LAO_PTY_ENTRY"),
        interactive_console=True,
        console_log_dir=str(tmp_path / "logs"),
        # Short /tmp path keeps the unix socket path under the ~108 char limit.
        console_socket_dir=tempfile.mkdtemp(prefix="lc"),
        console_direct_input=direct_input,
    )


def _fake_backend(tmp_path, name, body):
    script = tmp_path / name
    script.write_text("#!/usr/bin/env bash\n" + body)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def _read_events(config, run_id_dir_parent):
    for run_dir in Path(config.console_log_dir).iterdir():
        jsonl = run_dir / "lifecycle.jsonl"
        if jsonl.exists():
            return [json.loads(line) for line in jsonl.read_text().splitlines() if line.strip()]
    return []


# --------------------------------------------------------------------------- #
# Config gating
# --------------------------------------------------------------------------- #

def test_agent_console_auto_resolves_false():
    cfg = load_config({"AGENT_CONSOLE": "auto"})
    assert cfg.runner.interactive_console is False


def test_agent_console_true_enables_and_direct_input():
    cfg = load_config({"AGENT_CONSOLE": "true", "AGENT_CONSOLE_DIRECT_INPUT": "true"})
    assert cfg.runner.interactive_console is True
    assert cfg.runner.console_direct_input is True


def test_agent_console_default_is_off():
    cfg = load_config({})
    assert cfg.runner.interactive_console is False
    assert cfg.runner.console_direct_input is False


# --------------------------------------------------------------------------- #
# Control-sequence stripping (secrets must not survive via escape-splitting)
# --------------------------------------------------------------------------- #

def test_strip_ansi_covers_csi_osc_dcs_apc_pm():
    from linear_agent_orchestrator.runner import _strip_ansi

    payload = (
        "a"
        "\x1b[31mR\x1b[0m"  # CSI colour
        "\x1b]0;window-title\x07"  # OSC
        "\x1bPq;data\x1b\\"  # DCS
        "\x1b_application\x07"  # APC
        "\x1b^privacy\x1b\\"  # PM
        "b"
    )
    assert _strip_ansi(payload) == "aRb"


def test_verify_pid_dead_treats_eperm_as_alive(monkeypatch):
    from linear_agent_orchestrator.runner import ShellRunner

    def _raise_eperm(pid, sig):
        raise PermissionError("EPERM")

    monkeypatch.setattr(runner_mod.os, "kill", _raise_eperm)
    # A process we cannot signal (EPERM) still exists — must not be "dead".
    assert ShellRunner._verify_pid_dead(999999, attempts=2, interval=0.001) is False


# --------------------------------------------------------------------------- #
# Flag-off invariance: byte-identical Popen argv, no console side effects
# --------------------------------------------------------------------------- #

def test_flag_off_argv_is_unwrapped(tmp_path, monkeypatch):
    captured = {}

    real_popen = runner_mod.subprocess.Popen

    class _Recorder:
        def __init__(self, argv, **kwargs):
            captured["argv"] = list(argv)
            self._inner = real_popen(["true"], **{k: v for k, v in kwargs.items()})

        def communicate(self, timeout=None):
            return "", ""

        @property
        def returncode(self):
            return 0

    monkeypatch.setattr(runner_mod.subprocess, "Popen", _Recorder)

    fake = _fake_backend(tmp_path, "b.sh", 'printf ok > "$1"\n')
    config = RunnerConfig(
        command_template=f"{fake} {{result_path}}",
        timeout_seconds=5,
        work_dir=str(tmp_path / "work"),
        artifacts_dir=str(tmp_path / "artifacts"),
    )
    assert config.interactive_console is False
    ShellRunner(config).run(make_issue())
    assert captured["argv"][0] == str(fake)
    assert "lao-pty-host" not in captured["argv"][0]


# --------------------------------------------------------------------------- #
# Flag-on with real tmux
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(not HAVE_TMUX, reason="tmux not installed")
def test_console_completed_result_moves_semantically(tmp_path, monkeypatch):
    monkeypatch.setenv("LAO_PTY_ENTRY", str(PTY_ENTRY))
    monkeypatch.setenv("PATH", f"{_console_bin(tmp_path)}:{os.environ['PATH']}")

    result_payload = (
        '{"version":1,"status":"completed","summary":"ok",'
        '"validation":[{"description":"c","command":"true","exit_code":0,"evidence":"done"}]}'
    )
    fake = _fake_backend(
        tmp_path, "backend.sh",
        f'cat > "$1" <<\'JSON\'\n{result_payload}\nJSON\necho "backend stdout line"\n',
    )
    config = _console_config(tmp_path, f"{fake} {{result_path}}", direct_input=False)
    result = ShellRunner(config).run(make_issue())

    # Done-gate parity: transport exit 0 + validated completed result.
    assert result.exit_code == 0
    assert result.timed_out is False
    assert result.completed_semantically is True
    # RunnerResult records the ORIGINAL backend argv, not the wrapper.
    assert result.command[0] == str(fake)
    assert "lao-pty-host" not in " ".join(result.command)
    # Backend output was captured (for failure comments) via the pty log.
    assert "backend stdout line" in result.stdout

    events = _read_events(config, tmp_path)
    kinds = [e["event"] for e in events]
    assert kinds == ["session_started", "session_ended"]
    started = events[0]
    assert started["schema_version"] == 1
    assert started["backend"] == config.backend
    assert started["supports_direct_input"] is False
    assert events[1]["exit_code"] == 0


@pytest.mark.skipif(not HAVE_TMUX, reason="tmux not installed")
def test_console_direct_input_capability_is_emitted(tmp_path, monkeypatch):
    monkeypatch.setenv("LAO_PTY_ENTRY", str(PTY_ENTRY))
    monkeypatch.setenv("PATH", f"{_console_bin(tmp_path)}:{os.environ['PATH']}")
    fake = _fake_backend(tmp_path, "backend.sh", 'printf hi > "$1"\n')
    config = _console_config(tmp_path, f"{fake} {{result_path}}", direct_input=True)
    ShellRunner(config).run(make_issue())
    events = _read_events(config, tmp_path)
    assert events[0]["supports_direct_input"] is True


@pytest.mark.skipif(not HAVE_TMUX, reason="tmux not installed")
def test_private_tmux_session_delivers_direct_input_to_backend(tmp_path):
    """A capability-enabled backend can read bytes typed into its private PTY."""

    run_id = uuid.uuid4().hex
    # Unix-domain socket paths are typically capped at ~108 bytes; pytest's
    # descriptive tmp path can exceed that before the run id is appended.
    private_root = Path(tempfile.mkdtemp(prefix="lc"))
    run_dir = private_root / run_id
    socket = run_dir / "tmux.sock"
    session = f"lao-{run_id}"
    log_path = tmp_path / "logs" / run_id / "output.log"
    jsonl_path = log_path.parent / "lifecycle.jsonl"
    backend = _fake_backend(
        tmp_path,
        "interactive.sh",
        'echo READY\nIFS= read -r reply\nprintf "REPLY:%s\\n" "$reply"\n',
    )
    env = dict(os.environ, LAO_PTY_ENTRY=str(PTY_ENTRY))
    proc = subprocess.Popen(
        [
            str(PTY_HOST),
            "--run-id", run_id,
            "--run-dir", str(run_dir),
            "--socket", str(socket),
            "--session", session,
            "--log", str(log_path),
            "--jsonl", str(jsonl_path),
            "--backend", "custom",
            "--supports-direct-input", "true",
            "--issue-id", "issue-direct",
            "--issue-identifier", "DEV-DIRECT",
            "--", str(backend),
        ],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if jsonl_path.exists() and "session_started" in jsonl_path.read_text(encoding="utf-8"):
                break
            time.sleep(0.1)
        else:
            pytest.fail("private tmux session did not publish session_started")

        sent = subprocess.run(
            ["tmux", "-S", str(socket), "send-keys", "-t", session, "--", "hello from operator", "Enter"],
            text=True,
            capture_output=True,
            timeout=5,
        )
        assert sent.returncode == 0, sent.stderr
        stdout, stderr = proc.communicate(timeout=10)
        assert proc.returncode == 0, stderr
        assert "REPLY:hello from operator" in log_path.read_text(encoding="utf-8")
        assert "REPLY:hello from operator" in stdout
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        subprocess.run(
            ["tmux", "-S", str(socket), "kill-server"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        shutil.rmtree(private_root, ignore_errors=True)


@pytest.mark.skipif(not HAVE_TMUX, reason="tmux not installed")
def test_private_tmux_without_capability_provides_stdin_eof(tmp_path):
    """Observe-only runs cannot hang by accidentally reading the pane PTY."""

    run_id = uuid.uuid4().hex
    private_root = Path(tempfile.mkdtemp(prefix="lc"))
    run_dir = private_root / run_id
    socket = run_dir / "tmux.sock"
    log_path = tmp_path / "logs" / run_id / "output.log"
    jsonl_path = log_path.parent / "lifecycle.jsonl"
    backend = _fake_backend(
        tmp_path,
        "observe-only.sh",
        'if IFS= read -r reply; then echo "UNEXPECTED:$reply"; else echo EOF; fi\n',
    )
    proc = subprocess.Popen(
        [
            str(PTY_HOST),
            "--run-id", run_id,
            "--run-dir", str(run_dir),
            "--socket", str(socket),
            "--session", f"lao-{run_id}",
            "--log", str(log_path),
            "--jsonl", str(jsonl_path),
            "--backend", "custom",
            "--supports-direct-input", "false",
            "--issue-id", "issue-observe",
            "--issue-identifier", "DEV-OBSERVE",
            "--", str(backend),
        ],
        env=dict(os.environ, LAO_PTY_ENTRY=str(PTY_ENTRY)),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        stdout, stderr = proc.communicate(timeout=10)
        assert proc.returncode == 0, stderr
        assert "EOF" in stdout
        assert "UNEXPECTED:" not in stdout
        events = [json.loads(line) for line in jsonl_path.read_text(encoding="utf-8").splitlines()]
        assert events[0]["supports_direct_input"] is False
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        subprocess.run(
            ["tmux", "-S", str(socket), "kill-server"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        shutil.rmtree(private_root, ignore_errors=True)


@pytest.mark.skipif(not HAVE_TMUX, reason="tmux not installed")
def test_console_timeout_single_terminal_event_and_kills_descendants(tmp_path, monkeypatch):
    monkeypatch.setenv("LAO_PTY_ENTRY", str(PTY_ENTRY))
    monkeypatch.setenv("PATH", f"{_console_bin(tmp_path)}:{os.environ['PATH']}")

    marker = tmp_path / "descendant.marker"
    hup_marker = tmp_path / "hup.marker"
    # A normal child and a HUP-ignoring child; both must die on teardown.
    fake = _fake_backend(
        tmp_path, "slow.sh",
        f'bash -c \'sleep 4; echo alive > "{marker}"\' &\n'
        f'bash -c \'trap "" HUP; sleep 4; echo hup > "{hup_marker}"\' &\n'
        'echo started\nsleep 30\n',
    )
    config = _console_config(tmp_path, f"{fake}", timeout=1)
    result = ShellRunner(config).run(make_issue())

    assert result.timed_out is True
    assert result.exit_code is None

    import time as _t
    _t.sleep(5)
    assert not marker.exists(), "normal descendant survived timeout teardown"
    assert not hup_marker.exists(), "HUP-ignoring descendant survived SIGKILL teardown"

    events = _read_events(config, tmp_path)
    kinds = [e["event"] for e in events]
    # Exactly one terminal event, and it is the runner-owned session_killed.
    assert kinds.count("session_ended") == 0
    assert kinds.count("session_killed") == 1
    killed = [e for e in events if e["event"] == "session_killed"][0]
    assert killed["reason"] == "timeout"
    assert killed["schema_version"] == 1
