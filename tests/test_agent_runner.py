from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "agent-runner.sh"


def make_fake_backend(tmp_path: Path, binary: str) -> dict[str, str]:
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


def run_script(tmp_path: Path, prompt: Path, result_path: Path | str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPT), str(prompt), str(result_path)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("backend", "binary", "expected_args"),
    [
        ("omx", "omx", ["exec", "--cd", "{project}", "--skip-git-repo-check", "--model", "test-model", "--dangerously-bypass-approvals-and-sandbox", "-"]),
        ("codex", "codex", ["exec", "--cd", "{project}", "--skip-git-repo-check", "--model", "test-model", "--dangerously-bypass-approvals-and-sandbox", "-"]),
        ("claude", "claude", ["--print", "--dangerously-skip-permissions", "--model", "test-model"]),
    ],
)
def test_agent_runner_bypasses_permissions_by_default(tmp_path: Path, backend: str, binary: str, expected_args: list[str]):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    captures = make_fake_backend(tmp_path, binary)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("analyze this project\n", encoding="utf-8")
    result_path = tmp_path / "artifacts" / "DEV-1" / "result.json"
    env = {
        **os.environ,
        **captures,
        "AGENT_BACKEND": backend,
        "AGENT_MODEL": "test-model",
        "AGENT_PROJECT_DIR": str(project_dir),
    }
    env.pop("AGENT_BYPASS_PERMISSIONS", None)
    env.pop("AGENT_EFFORT", None)

    result = run_script(tmp_path, prompt, result_path, env)

    assert result.returncode == 0, result.stderr
    expected = [str(project_dir) if value == "{project}" else value for value in expected_args]
    assert Path(captures["CAPTURE_ARGS"]).read_text(encoding="utf-8").splitlines() == expected
    assert Path(captures["CAPTURE_STDIN"]).read_text(encoding="utf-8") == "analyze this project\n"


@pytest.mark.parametrize(
    ("backend", "binary", "expected_effort_args"),
    [
        ("omx", "omx", ["-c", "model_reasoning_effort=xhigh"]),
        ("codex", "codex", ["-c", "model_reasoning_effort=xhigh"]),
        ("claude", "claude", ["--effort", "xhigh"]),
    ],
)
def test_agent_runner_passes_reasoning_effort(tmp_path: Path, backend: str, binary: str, expected_effort_args: list[str]):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    captures = make_fake_backend(tmp_path, binary)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("analyze this project\n", encoding="utf-8")
    env = {
        **os.environ,
        **captures,
        "AGENT_BACKEND": backend,
        "AGENT_MODEL": "test-model",
        "AGENT_EFFORT": "xhigh",
        "AGENT_PROJECT_DIR": str(project_dir),
    }

    result = run_script(tmp_path, prompt, tmp_path / "result.json", env)

    assert result.returncode == 0, result.stderr
    args = Path(captures["CAPTURE_ARGS"]).read_text(encoding="utf-8").splitlines()
    joined = " ".join(args)
    assert " ".join(expected_effort_args) in joined
    # Effort rides with the model selection, before the prompt/stdin marker.
    assert "--model test-model" in joined


@pytest.mark.parametrize(
    ("backend", "binary", "expected_args"),
    [
        ("omx", "omx", ["exec", "--cd", "{project}", "--skip-git-repo-check", "--model", "test-model", "--sandbox", "workspace-write", "-"]),
        ("codex", "codex", ["exec", "--cd", "{project}", "--skip-git-repo-check", "--model", "test-model", "--sandbox", "workspace-write", "-"]),
        ("claude", "claude", ["--print", "--permission-mode", "acceptEdits", "--model", "test-model"]),
    ],
)
def test_agent_runner_falls_back_to_backend_sandbox_when_bypass_disabled(tmp_path: Path, backend: str, binary: str, expected_args: list[str]):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    captures = make_fake_backend(tmp_path, binary)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("analyze this project\n", encoding="utf-8")
    result_path = tmp_path / "artifacts" / "result.json"
    env = {
        **os.environ,
        **captures,
        "AGENT_BACKEND": backend,
        "AGENT_MODEL": "test-model",
        "AGENT_PROJECT_DIR": str(project_dir),
        "AGENT_SANDBOX": "workspace-write",
        "AGENT_PERMISSION_MODE": "acceptEdits",
        "AGENT_BYPASS_PERMISSIONS": "false",
    }
    env.pop("AGENT_EFFORT", None)

    result = run_script(tmp_path, prompt, result_path, env)

    assert result.returncode == 0, result.stderr
    expected = [str(project_dir) if value == "{project}" else value for value in expected_args]
    assert Path(captures["CAPTURE_ARGS"]).read_text(encoding="utf-8").splitlines() == expected


def test_agent_runner_exports_result_path_and_creates_parent_dir(tmp_path: Path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    captures = make_fake_backend(tmp_path, "omx")
    prompt = tmp_path / "prompt.md"
    prompt.write_text("do the work\n", encoding="utf-8")
    result_path = tmp_path / "artifacts" / "DEV-9" / "result.json"
    assert not result_path.parent.exists()
    env = {
        **os.environ,
        **captures,
        "AGENT_BACKEND": "omx",
        "AGENT_PROJECT_DIR": str(project_dir),
    }

    result = run_script(tmp_path, prompt, result_path, env)

    assert result.returncode == 0, result.stderr
    assert Path(captures["CAPTURE_RESULT_ENV"]).read_text(encoding="utf-8") == str(result_path)
    assert result_path.parent.is_dir()
    # The wrapper must never synthesize a structured result itself.
    assert not result_path.exists()


def test_agent_runner_requires_result_path_argument(tmp_path: Path):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("test", encoding="utf-8")

    result = subprocess.run(
        [str(SCRIPT), str(prompt)],
        env={**os.environ, "AGENT_PROJECT_DIR": str(tmp_path)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 64
    assert "usage" in result.stderr


def test_agent_runner_rejects_relative_result_path(tmp_path: Path):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("test", encoding="utf-8")

    result = run_script(tmp_path, prompt, "artifacts/result.json", {**os.environ, "AGENT_PROJECT_DIR": str(tmp_path)})

    assert result.returncode == 64
    assert "must be absolute" in result.stderr


def test_agent_runner_rejects_unknown_backend(tmp_path: Path):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("test", encoding="utf-8")

    result = run_script(
        tmp_path,
        prompt,
        tmp_path / "artifacts" / "result.json",
        {**os.environ, "AGENT_BACKEND": "unknown", "AGENT_PROJECT_DIR": str(tmp_path)},
    )

    assert result.returncode == 64
    assert "unsupported AGENT_BACKEND" in result.stderr
