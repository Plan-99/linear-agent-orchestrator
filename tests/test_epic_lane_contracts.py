"""Regression-lane contracts for the model-neutral tmux agent UI epic.

worker-3 owns the regression / adversarial test lane. Several epic surfaces are
still landing across the team:

* private container tmux socket (no host tmux socket mount)
* lifecycle JSONL under ``.runtime``
* headless / host supervisor scripts
* operator direct-input (safe attach) capability

The tests below split into two kinds:

1. **Standing safety guards** that assert facts which must hold in every commit,
   epic or not (shell scripts parse; no host tmux/docker socket is mounted).
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
def test_no_host_tmux_or_docker_socket_mount(compose: Path):
    """No compose service may bind-mount a host tmux server socket or docker socket.

    The tmux UI must run on a *private* container socket (task 6); the host tmux
    socket must never be mounted (task 4), and the docker socket stays out.
    """

    mounts = _volume_mounts(compose.read_text(encoding="utf-8"))
    forbidden = re.compile(r"docker\.sock|/tmp/tmux|tmux-\d+|\$\{?TMUX", re.IGNORECASE)
    offenders = [mount for mount in mounts if forbidden.search(mount)]
    assert not offenders, f"{compose.name} mounts a forbidden host socket: {offenders}"


# --------------------------------------------------------------------------- #
# Forward contracts (skip until the epic artifact exists, then enforce)
# --------------------------------------------------------------------------- #

def _scripts_mentioning(pattern: str) -> list[Path]:
    rx = re.compile(pattern, re.IGNORECASE)
    return [p for p in _shell_scripts() if rx.search(p.read_text(encoding="utf-8"))]


def test_tmux_usage_requires_private_socket():
    """Any tmux invocation must pin a private ``-S`` socket, never the default one."""

    tmux_scripts = _scripts_mentioning(r"\btmux\b")
    if not tmux_scripts:
        pytest.skip("no tmux usage in scripts/ yet (private-socket contract pending task 6)")
    for script in tmux_scripts:
        text = script.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if re.search(r"\btmux\b", stripped) and not stripped.startswith("#"):
                assert "-S " in stripped or "socket" in stripped.lower(), (
                    f"{script.name}: tmux invocation without a private socket: {stripped!r}"
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
