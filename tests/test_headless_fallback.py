"""Regression and adversarial coverage for the headless agent-runner fallback.

The model-neutral tmux agent UI epic layers a live-view/attach experience on top
of ``scripts/agent-runner.sh``. Whatever that UI adds, the *headless fallback* --
running the backend non-interactively, piping the prompt on stdin, and never
fabricating a structured result -- must stay byte-for-byte compatible so the
orchestrator keeps working in environments without tmux (CI, minimal containers).

These tests own the "flag-off compatibility" slice of the regression lane. They
assert the wrapper's headless contract directly against the real script so any
future edit that adds a UI layer cannot silently regress the fallback argv,
stdin delivery, optional-flag handling, or fail-closed exit codes.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "agent-runner.sh"


def make_fake_backend(tmp_path: Path, binary: str) -> dict[str, str]:
    """Install a fake backend CLI that records argv, the result-path env, and stdin."""

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / binary
    fake.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$@\" > \"$CAPTURE_ARGS\"\n"
        "printf '%s' \"${LAO_RESULT_PATH:-}\" > \"$CAPTURE_RESULT_ENV\"\n"
        "cat > \"$CAPTURE_STDIN\"\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "CAPTURE_ARGS": str(tmp_path / "args"),
        "CAPTURE_RESULT_ENV": str(tmp_path / "result-env"),
        "CAPTURE_STDIN": str(tmp_path / "stdin"),
    }


def run_script(prompt: Path, result_path: Path | str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPT), str(prompt), str(result_path)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def base_env(captures: dict[str, str], project_dir: Path, **overrides: str) -> dict[str, str]:
    env = {**os.environ, **captures, "AGENT_PROJECT_DIR": str(project_dir)}
    # Strip UI/permission knobs so each test declares exactly what it exercises;
    # the epic will add UI flags and the headless path must ignore their absence.
    for key in (
        "AGENT_BYPASS_PERMISSIONS",
        "AGENT_SANDBOX",
        "AGENT_PERMISSION_MODE",
        "AGENT_MODEL",
        "AGENT_BACKEND",
    ):
        env.pop(key, None)
    env.update(overrides)
    return env


def captured_args(captures: dict[str, str]) -> list[str]:
    return Path(captures["CAPTURE_ARGS"]).read_text(encoding="utf-8").splitlines()


def test_default_backend_is_headless_omx_exec(tmp_path: Path):
    """With AGENT_BACKEND unset the wrapper defaults to a headless ``omx exec``."""

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    captures = make_fake_backend(tmp_path, "omx")
    prompt = tmp_path / "prompt.md"
    prompt.write_text("do the work\n", encoding="utf-8")
    result_path = tmp_path / "artifacts" / "DEV-1" / "result.json"

    result = run_script(prompt, result_path, base_env(captures, project_dir))

    assert result.returncode == 0, result.stderr
    assert captured_args(captures) == [
        "exec",
        "--cd",
        str(project_dir),
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        "-",
    ]


@pytest.mark.parametrize(
    ("backend", "binary", "prefix", "suffix"),
    [
        ("omx", "omx", ["exec", "--cd", "{project}", "--skip-git-repo-check"], ["--dangerously-bypass-approvals-and-sandbox", "-"]),
        ("codex", "codex", ["exec", "--cd", "{project}", "--skip-git-repo-check"], ["--dangerously-bypass-approvals-and-sandbox", "-"]),
        ("claude", "claude", ["--print"], ["--dangerously-skip-permissions"]),
    ],
)
def test_model_flag_is_omitted_when_unset(tmp_path: Path, backend: str, binary: str, prefix: list[str], suffix: list[str]):
    """An unset/empty AGENT_MODEL must not inject a dangling ``--model`` token."""

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    captures = make_fake_backend(tmp_path, binary)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("analyze\n", encoding="utf-8")
    result_path = tmp_path / "artifacts" / "result.json"
    # Explicit empty string must behave like "unset" (the ``[[ -n ]]`` guard).
    env = base_env(captures, project_dir, AGENT_BACKEND=backend, AGENT_MODEL="")

    result = run_script(prompt, result_path, env)

    assert result.returncode == 0, result.stderr
    expected = [str(project_dir) if tok == "{project}" else tok for tok in (*prefix, *suffix)]
    args = captured_args(captures)
    assert args == expected
    assert "--model" not in args


@pytest.mark.parametrize("binary", ["omx", "codex", "claude"])
def test_prompt_is_delivered_on_stdin(tmp_path: Path, binary: str):
    """Every headless backend receives the prompt on stdin, unaltered."""

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    captures = make_fake_backend(tmp_path, binary)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("# Linear Issue DEV-7\nline two\n", encoding="utf-8")
    result_path = tmp_path / "artifacts" / "result.json"

    result = run_script(prompt, result_path, base_env(captures, project_dir, AGENT_BACKEND=binary))

    assert result.returncode == 0, result.stderr
    assert Path(captures["CAPTURE_STDIN"]).read_text(encoding="utf-8") == "# Linear Issue DEV-7\nline two\n"


def test_explicit_bypass_true_matches_default(tmp_path: Path):
    """AGENT_BYPASS_PERMISSIONS=true is identical to leaving it unset (default)."""

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("work\n", encoding="utf-8")

    (tmp_path / "a").mkdir()
    captures_default = make_fake_backend(tmp_path / "a", "omx")
    run_script(prompt, tmp_path / "a" / "result.json", base_env(captures_default, project_dir, AGENT_BACKEND="omx"))

    (tmp_path / "b").mkdir()
    captures_true = make_fake_backend(tmp_path / "b", "omx")
    run_script(
        prompt,
        tmp_path / "b" / "result.json",
        base_env(captures_true, project_dir, AGENT_BACKEND="omx", AGENT_BYPASS_PERMISSIONS="true"),
    )

    assert captured_args(captures_default) == captured_args(captures_true)


def test_headless_wrapper_never_fabricates_result(tmp_path: Path):
    """Transport exit 0 must not create the structured result; only the backend may."""

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    captures = make_fake_backend(tmp_path, "omx")
    prompt = tmp_path / "prompt.md"
    prompt.write_text("work\n", encoding="utf-8")
    result_path = tmp_path / "artifacts" / "DEV-9" / "result.json"
    assert not result_path.parent.exists()

    result = run_script(prompt, result_path, base_env(captures, project_dir, AGENT_BACKEND="omx"))

    assert result.returncode == 0, result.stderr
    # Parent dir is prepared and the result path is exported for the backend...
    assert result_path.parent.is_dir()
    assert Path(captures["CAPTURE_RESULT_ENV"]).read_text(encoding="utf-8") == str(result_path)
    # ...but the wrapper itself writes no result: the Done gate stays backend-owned.
    assert not result_path.exists()


def test_unreadable_prompt_fails_closed(tmp_path: Path):
    """A missing/unreadable prompt is a fail-closed transport error, not a run."""

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    captures = make_fake_backend(tmp_path, "omx")
    missing_prompt = tmp_path / "does-not-exist.md"
    result_path = tmp_path / "artifacts" / "result.json"

    result = run_script(missing_prompt, result_path, base_env(captures, project_dir, AGENT_BACKEND="omx"))

    assert result.returncode == 66
    assert "not readable" in result.stderr
    assert not Path(captures["CAPTURE_ARGS"]).exists()  # backend never invoked


def test_missing_project_dir_fails_closed(tmp_path: Path):
    """The wrapper refuses to run when the trusted workspace is absent."""

    captures = make_fake_backend(tmp_path, "omx")
    prompt = tmp_path / "prompt.md"
    prompt.write_text("work\n", encoding="utf-8")
    result_path = tmp_path / "artifacts" / "result.json"
    absent_project = tmp_path / "no-such-project"

    result = run_script(prompt, result_path, base_env(captures, absent_project, AGENT_BACKEND="omx"))

    assert result.returncode == 72
    assert "project directory does not exist" in result.stderr
    assert not Path(captures["CAPTURE_ARGS"]).exists()  # backend never invoked
