from __future__ import annotations

import os
from pathlib import Path
import shutil
import socket
import subprocess
import textwrap


REPO_ROOT = Path(__file__).parents[1]
COMPOSE_SCRIPT = REPO_ROOT / "scripts" / "compose.sh"
HOST_PATH_LIB = REPO_ROOT / "scripts" / "host-path-lib.sh"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    path.chmod(0o755)


def _fixture_repo(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "lao"
    scripts = root / "scripts"
    fake_bin = tmp_path / "bin"
    scripts.mkdir(parents=True)
    fake_bin.mkdir()
    (root / ".runtime" / "work").mkdir(parents=True)
    (root / ".runtime" / "artifacts").mkdir(parents=True)
    shutil.copy2(COMPOSE_SCRIPT, scripts / "compose.sh")
    shutil.copy2(HOST_PATH_LIB, scripts / "host-path-lib.sh")
    _write_executable(
        fake_bin / "docker",
        r"""
        #!/usr/bin/env bash
        set -eu
        printf 'gid=%s\n' "${LAO_DOCKER_GID:-}"
        printf 'socket=%s\n' "${LAO_DOCKER_SOCKET:-}"
        printf 'project=%s\n' "${LAO_PROJECT_DIR:-}"
        printf 'runtime=%s\n' "${LAO_RUNTIME_DIR:-}"
        printf 'xauthority=%s\n' "${XAUTHORITY:-}"
        printf 'client_host=%s\n' "${DOCKER_HOST:-}"
        for arg in "$@"; do
          printf 'arg=%s\n' "$arg"
        done
        """,
    )
    return root, fake_bin


def _run(root: Path, fake_bin: Path, **overrides: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(root),
        "LAO_INSTANCE_ID": "docker-host-test",
        "LAO_PROJECT_DIR": str(root),
        "LAO_DOCKER_HOST": "false",
    }
    env.pop("LAO_DOCKER_SOCKET", None)
    env.pop("XAUTHORITY", None)
    env.update(overrides)
    return subprocess.run(
        [str(root / "scripts" / "compose.sh"), "version"],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_default_mode_does_not_load_docker_host_override(tmp_path: Path) -> None:
    root, fake_bin = _fixture_repo(tmp_path)

    result = _run(root, fake_bin)

    assert result.returncode == 0
    args = [line for line in result.stdout.splitlines() if line.startswith("arg=")]
    assert args == ["arg=compose", "arg=version"]


def test_enabled_mode_loads_override_and_exports_host_identity(tmp_path: Path) -> None:
    root, fake_bin = _fixture_repo(tmp_path)
    (root / ".Xauthority").write_text("test-cookie\n", encoding="utf-8")
    socket_path = tmp_path / "docker.sock"
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(socket_path))
    socket_gid = socket_path.stat().st_gid
    try:
        result = _run(
            root,
            fake_bin,
            LAO_DOCKER_HOST="true",
            LAO_DOCKER_SOCKET=str(socket_path),
            DOCKER_HOST="tcp://untrusted.example:2375",
        )
    finally:
        server.close()

    assert result.returncode == 0
    lines = result.stdout.splitlines()
    assert f"gid={socket_gid}" in lines
    assert f"socket={socket_path}" in lines
    assert f"project={root}" in lines
    assert f"runtime={root / '.runtime'}" in lines
    assert f"xauthority={root / '.Xauthority'}" in lines
    assert f"client_host=unix://{socket_path}" in lines
    args = [line for line in lines if line.startswith("arg=")]
    assert args == [
        "arg=compose",
        "arg=-f",
        "arg=compose.yaml",
        "arg=-f",
        "arg=compose.docker-host.yaml",
        "arg=version",
    ]


def test_enabled_mode_rejects_missing_socket_before_docker(tmp_path: Path) -> None:
    root, fake_bin = _fixture_repo(tmp_path)

    result = _run(
        root,
        fake_bin,
        LAO_DOCKER_HOST="true",
        LAO_DOCKER_SOCKET=str(tmp_path / "missing.sock"),
    )

    assert result.returncode == 66
    assert "missing, not a socket, or not absolute" in result.stderr
    assert "arg=compose" not in result.stdout


def test_invalid_mode_fails_closed(tmp_path: Path) -> None:
    root, fake_bin = _fixture_repo(tmp_path)

    result = _run(root, fake_bin, LAO_DOCKER_HOST="sometimes")

    assert result.returncode == 64
    assert "must be true or false" in result.stderr
    assert "arg=compose" not in result.stdout
