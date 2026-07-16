"""Regression-lane contracts for the model-neutral tmux agent UI epic.

worker-3 owns the regression / adversarial test lane. Several epic surfaces are
still landing across the team:

* private container tmux socket (no host tmux socket mount)
* lifecycle JSONL under ``.runtime``
* headless / host supervisor scripts
* operator direct-input (safe attach) capability

The tests below split into two kinds:

1. **Standing safety guards** that assert facts which must hold in every commit,
   epic or not (shell scripts parse; host tmux is forbidden and Docker is opt-in).
   These run and pass today and fail loudly if any worker regresses them.
2. **Forward contracts** for artifacts that do not exist yet. They locate the
   production artifact and ``skip`` with an explicit reason when it is absent, so
   the suite stays green now and the contract self-activates the moment the
   artifact lands -- without asserting speculative internals that would churn.
"""

from __future__ import annotations

from pathlib import Path
import re

import pytest


ROOT = Path(__file__).parents[1]
SCRIPTS_DIR = ROOT / "scripts"


def _shell_scripts() -> list[Path]:
    return sorted(SCRIPTS_DIR.glob("*.sh"))


def _compose_files() -> list[Path]:
    return sorted(p for p in ROOT.glob("compose*.y*ml") if p.is_file())


def _volume_mounts(compose_text: str) -> list[str]:
    """Return the host:container mount specs from a compose ``volumes:`` block.

    This is a deliberately small, dependency-free reader: it collects ``- a:b``
    list entries under a ``volumes:`` key and stops at the next same-or-shallower
    mapping key. It is used only for negative safety assertions.
    """

    mounts: list[str] = []
    in_volumes = False
    volumes_indent = 0
    for raw in compose_text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        key = raw.strip()
        if re.match(r"^volumes:\s*$", key):
            in_volumes = True
            volumes_indent = indent
            continue
        if in_volumes:
            if indent <= volumes_indent and not key.startswith("-"):
                in_volumes = False
                continue
            if key.startswith("- "):
                mounts.append(key[2:].strip())
    return mounts


# --------------------------------------------------------------------------- #
# Standing safety guards (run today, must always hold)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("script", _shell_scripts(), ids=lambda p: p.name)
def test_shell_scripts_parse(script: Path):
    """Every wrapper/supervisor script must be syntactically valid bash.

    As the epic adds headless/host supervisor and attach scripts under
    ``scripts/``, this parametrized guard automatically covers them.
    """

    import subprocess

    result = subprocess.run(["bash", "-n", str(script)], text=True, capture_output=True)
    assert result.returncode == 0, f"{script.name} failed bash -n: {result.stderr.strip()}"


@pytest.mark.parametrize("compose", _compose_files(), ids=lambda p: p.name)
def test_host_socket_mount_policy(compose: Path):
    """Host tmux is always forbidden; Docker is limited to its explicit override."""

    configuration = "\n".join(
        line for line in compose.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )
    tmux_socket = re.compile(r"/tmp/tmux|tmux-\d+|\$\{?TMUX", re.IGNORECASE)
    assert not tmux_socket.search(configuration), (
        f"{compose.name} mounts or references a forbidden host tmux socket"
    )
    if compose.name != "compose.docker-host.yaml":
        assert "docker.sock" not in configuration, (
            f"{compose.name} exposes Docker outside the explicit opt-in override"
        )


def test_docker_host_override_is_explicit_and_read_only() -> None:
    override = ROOT / "compose.docker-host.yaml"
    text = override.read_text(encoding="utf-8")

    assert "source: ${LAO_DOCKER_SOCKET}" in text
    assert "target: /var/run/docker.sock" in text
    socket_block = text.split("source: ${LAO_DOCKER_SOCKET}", maxsplit=1)[1]
    socket_block = socket_block.split("- type: bind", maxsplit=1)[0]
    assert "read_only: true" in socket_block
    assert (
        "source: ${AGENT_WORKTREE_ROOT}\n"
        "        target: ${AGENT_WORKTREE_ROOT}"
    ) in text


def test_default_compose_clears_inherited_docker_host() -> None:
    base = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    override = (ROOT / "compose.docker-host.yaml").read_text(encoding="utf-8")

    assert re.search(r'^\s+DOCKER_HOST:\s+""\s*$', base, re.MULTILINE)
    assert re.search(
        r"^\s+DOCKER_HOST:\s+unix:///var/run/docker\.sock\s*$", override, re.MULTILINE
    )


# --------------------------------------------------------------------------- #
# Forward contracts (skip until the epic artifact exists, then enforce)
# --------------------------------------------------------------------------- #

def _scripts_mentioning(pattern: str) -> list[Path]:
    rx = re.compile(pattern, re.IGNORECASE)
    return [p for p in _shell_scripts() if rx.search(p.read_text(encoding="utf-8"))]


def test_container_tmux_usage_requires_private_socket():
    """Container-side tmux calls pin ``-S``; host pane management does not.

    ``console-supervisor.sh`` intentionally talks to the host's current tmux
    server to create a sibling pane. Only calls made *inside* the container may
    use the per-run private socket. Conflating those boundaries would either
    reject the valid host UI or tempt an unsafe host-socket mount.
    """

    private_socket_scripts = [
        SCRIPTS_DIR / "lao-pty-host.sh",
        SCRIPTS_DIR / "console-attach.sh",
    ]
    for script in private_socket_scripts:
        if not script.exists():
            pytest.skip(f"{script.name} not implemented yet")
        text = script.read_text(encoding="utf-8")
        invocations = [
            line.strip()
            for line in text.splitlines()
            if not line.lstrip().startswith("#")
            and re.search(r"(?:^|[;&|!(])\s*!?\s*tmux\s", line)
        ]
        assert invocations, f"{script.name} contains no tmux invocation"
        assert all("-S " in line or '(-S ' in line for line in invocations), (
            f"{script.name} has container tmux calls without -S: {invocations}"
        )

    supervisor = SCRIPTS_DIR / "console-supervisor.sh"
    if supervisor.exists():
        text = supervisor.read_text(encoding="utf-8")
        assert "tmux split-window" in text
        host_tmux_lines = [
            line.strip()
            for line in text.splitlines()
            if re.search(r"\btmux (?:split-window|list-panes|select-pane)\b", line)
            and not line.lstrip().startswith("#")
        ]
        assert host_tmux_lines
        assert all(" -S " not in line for line in host_tmux_lines), (
            f"host pane management must use the current host tmux server: {host_tmux_lines}"
        )
        assert 'docker exec "$CONTAINER" tmux -S' in text, (
            "container session probes must cross the boundary via docker exec and the private socket"
        )


def test_lifecycle_jsonl_path_is_under_runtime():
    """Lifecycle JSONL, when implemented, must be emitted under ``.runtime`` (task 7)."""

    src = ROOT / "src"
    hits = [
        p
        for p in src.rglob("*.py")
        if re.search(r"jsonl|lifecycle", p.read_text(encoding="utf-8"), re.IGNORECASE)
    ]
    if not hits:
        pytest.skip("no lifecycle JSONL writer yet (path contract pending task 7)")
    joined = "\n".join(p.read_text(encoding="utf-8") for p in hits)
    # A JSONL lifecycle log must never be pinned to an absolute host path outside
    # the runtime tree; it should be relative or rooted at the runtime/logs dir.
    bad_absolute = re.findall(r"[\"'](/(?!workspace|home/agent)[^\"']*\.jsonl)[\"']", joined)
    assert not bad_absolute, f"lifecycle JSONL points outside the runtime tree: {bad_absolute}"


@pytest.mark.parametrize(
    ("label", "pattern"),
    [
        ("supervisor", r"supervis|headless.*supervisor|host.*supervisor"),
        ("direct-input", r"attach|send-keys|direct.?input|operator.*input"),
    ],
)
def test_new_capability_scripts_are_executable_and_parse(label: str, pattern: str):
    """Supervisor / direct-input scripts, once added, must be executable bash."""

    import os
    import subprocess

    candidates = _scripts_mentioning(pattern)
    if not candidates:
        pytest.skip(f"no {label} script yet (contract pending its owning task)")
    for script in candidates:
        assert os.access(script, os.X_OK), f"{script.name} must be executable"
        result = subprocess.run(["bash", "-n", str(script)], text=True, capture_output=True)
        assert result.returncode == 0, f"{script.name} failed bash -n: {result.stderr.strip()}"
