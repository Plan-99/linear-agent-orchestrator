from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import subprocess
import textwrap

import pytest


REPO_ROOT = Path(__file__).parents[1]
RUN_SCRIPT = REPO_ROOT / "run.sh"
UPDATER_SCRIPT = REPO_ROOT / "scripts" / "update-agent-tools.sh"
CONSOLE_LIB = REPO_ROOT / "scripts" / "console-lib.sh"
HOST_PATH_LIB = REPO_ROOT / "scripts" / "host-path-lib.sh"
DOCKERFILE = REPO_ROOT / "Dockerfile"
TEST_INSTANCE_ID = "test-instance"
TEST_VERIFIED_IMAGE = f"linear-agent-orchestrator:verified-{TEST_INSTANCE_ID}"
TEST_PREVIOUS_IMAGE = f"linear-agent-orchestrator:previous-{TEST_INSTANCE_ID}"


def write_executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    path.chmod(0o755)


def make_launcher_repo(tmp_path: Path, env_text: str = "AGENT_BACKEND=omx\n") -> tuple[Path, dict[str, str], Path]:
    repo = tmp_path / "launcher"
    scripts = repo / "scripts"
    fake_bin = tmp_path / "launcher-bin"
    scripts.mkdir(parents=True)
    fake_bin.mkdir()

    shutil.copy2(RUN_SCRIPT, repo / "run.sh")
    (repo / "run.sh").chmod(0o755)
    shutil.copy2(CONSOLE_LIB, scripts / "console-lib.sh")
    shutil.copy2(HOST_PATH_LIB, scripts / "host-path-lib.sh")
    (repo / ".env").write_text(env_text, encoding="utf-8")

    write_executable(
        scripts / "bootstrap.sh",
        r'''
        #!/usr/bin/env bash
        set -eu
        { printf 'bootstrap'; printf '|%s' "$@"; printf '\n'; } >> "$CALL_LOG"
        exit "${BOOTSTRAP_STATUS:-0}"
        ''',
    )
    write_executable(
        scripts / "update-agent-tools.sh",
        r'''
        #!/usr/bin/env bash
        set -eu
        { printf 'updater'; printf '|%s' "$@"; printf '\n'; } >> "$CALL_LOG"
        for arg in "$@"; do
          if [[ "$arg" == --fallback-only ]]; then
            exit "${UPDATER_FALLBACK_STATUS:-${UPDATER_STATUS:-0}}"
          fi
        done
        exit "${UPDATER_STATUS:-0}"
        ''',
    )
    write_executable(
        scripts / "daemon.sh",
        r'''
        #!/usr/bin/env bash
        set -eu
        { printf 'daemon'; printf '|%s' "$@"; printf '\n'; } >> "$CALL_LOG"
        printf 'daemon-image|%s\n' "${LAO_IMAGE:-}" >> "$CALL_LOG"
        exit "${DAEMON_STATUS:-0}"
        ''',
    )
    write_executable(
        fake_bin / "docker",
        r'''
        #!/usr/bin/env bash
        set -eu
        { printf 'docker'; printf '|%s' "$@"; printf '\n'; } >> "$CALL_LOG"
        case "${1:-}" in
          ps)
            if [[ "$*" == *'com.docker.compose.project='* ]]; then
              if [[ "${FAKE_DAEMON_RUNNING:-0}" == 1 ]]; then
                printf 'daemon-container-id\n'
              fi
            elif [[ "${FAKE_UNSCOPED_DAEMON:-none}" != none ]]; then
              printf 'legacy-daemon-container-id\n'
            fi
            ;;
          inspect)
            id="${@: -1}"
            if [[ "$id" == daemon-container-id ]]; then
              case "$*" in
                *'{{json .Config.Cmd}}'*) printf '["daemon"]\n' ;;
                *'{{.Name}}'*) printf '/lao-daemon\n' ;;
              esac
              exit 0
            fi
            if [[ "$id" != legacy-daemon-container-id ]]; then
              exit 1
            fi
            case "$*" in
              *'com.docker.compose.project.working_dir'*)
                case "${FAKE_UNSCOPED_DAEMON:-none}" in
                  working-dir) printf '%s\n' "$FAKE_LAUNCHER_ROOT" ;;
                  workspace-collision) printf '/workspace/repo\n' ;;
                  *) printf '/unrelated/checkout\n' ;;
                esac
                ;;
              *'index .Config.Labels "com.docker.compose.project"'*)
                printf 'linear-agent-orchestrator\n'
                ;;
              *'Destination "/workspace/data"'*)
                case "${FAKE_UNSCOPED_DAEMON:-none}" in
                  working-dir) : ;;
                  data-mount) printf '%s/.runtime/data\n' "$FAKE_LAUNCHER_ROOT" ;;
                  *) printf '/unrelated/checkout/.runtime/data\n' ;;
                esac
                ;;
              *'{{json .Config.Cmd}}'*) printf '["daemon"]\n' ;;
              *'{{.Name}}'*) printf '/legacy-lao-daemon\n' ;;
            esac
            ;;
        esac
        ''',
    )
    write_executable(
        fake_bin / "git",
        r'''
        #!/usr/bin/env bash
        set -eu
        { printf 'git'; printf '|%s' "$@"; printf '\n'; } >> "$CALL_LOG"
        mode="${GIT_MODE:-clean}"
        case "${1:-}" in
          rev-parse)
            case "${2:-}" in
              --is-inside-work-tree) printf 'true\n' ;;
              --abbrev-ref) printf 'origin/main\n' ;;
              HEAD)
                if [[ -f "$GIT_UPDATE_MARKER" ]]; then
                  printf '%040d\n' 0 | tr 0 b
                else
                  printf '%040d\n' 0 | tr 0 a
                fi
                ;;
              *) exit 1 ;;
            esac
            ;;
          status)
            [[ "$mode" == dirty ]] && printf ' M tracked-file\n'
            ;;
          symbolic-ref) printf 'main\n' ;;
          config) printf 'origin\n' ;;
          fetch)
            if [[ "$mode" == fetch-fail ]]; then
              exit 1
            fi
            exit 0
            ;;
          merge-base)
            exit 0
            ;;
          merge)
            touch "$GIT_UPDATE_MARKER"
            ;;
          *) exit 1 ;;
        esac
        ''',
    )

    call_log = tmp_path / "launcher-calls.log"
    env = {**os.environ}
    for key in (
        "LAO_IMAGE",
        "LAO_INSTANCE_ID",
        "LAO_VERIFIED_IMAGE",
        "LAO_PREVIOUS_IMAGE",
        "COMPOSE_PROJECT_NAME",
        "LAO_RUN_LOCK_HELD",
        "LAO_RUN_REEXECED",
        "UPDATER_STATUS",
        "BOOTSTRAP_STATUS",
        "DAEMON_STATUS",
        "FAKE_DAEMON_RUNNING",
        "FAKE_UNSCOPED_DAEMON",
        "GIT_MODE",
    ):
        env.pop(key, None)
    env.update(
        {
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "CALL_LOG": str(call_log),
            "FAKE_LAUNCHER_ROOT": str(repo.resolve()),
            "GIT_UPDATE_MARKER": str(tmp_path / "git-updated"),
            "LAO_UPDATE_DIR": str(tmp_path / "launcher-update"),
            "LAO_INSTANCE_ID": TEST_INSTANCE_ID,
        }
    )
    return repo, env, call_log


def run_launcher(repo: Path, env: dict[str, str], *args: str, skip_git: bool = True) -> subprocess.CompletedProcess[str]:
    run_env = {**env}
    if skip_git:
        run_env["LAO_RUN_REEXECED"] = "1"
    return subprocess.run(
        [str(repo / "run.sh"), *args],
        cwd=repo,
        env=run_env,
        text=True,
        capture_output=True,
        check=False,
    )


def read_calls(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def candidate_image_from_calls(calls: list[str]) -> str:
    prefix = "docker|image|rm|--force|linear-agent-orchestrator:candidate-"
    first_removal = next(line for line in calls if line.startswith(prefix))
    return first_removal.rsplit("|", 1)[1]


def test_public_scripts_are_executable_and_have_valid_bash_syntax():
    for script in (RUN_SCRIPT, UPDATER_SCRIPT):
        assert script.stat().st_mode & stat.S_IXUSR, f"{script} must be executable"
        result = subprocess.run(["bash", "-n", str(script)], text=True, capture_output=True, check=False)
        assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("backend", ["codex", "omx", "claude"])
def test_launcher_reads_backend_and_starts_only_verified_image(tmp_path: Path, backend: str):
    repo, env, call_log = make_launcher_repo(tmp_path, f"AGENT_BACKEND={backend}\n")

    result = run_launcher(repo, env)

    assert result.returncode == 0, result.stderr
    calls = read_calls(call_log)
    updater = next(line for line in calls if line.startswith("updater|"))
    assert f"|--backend|{backend}|" in updater
    assert "|--git-sha|" in updater
    assert f"daemon-image|{TEST_VERIFIED_IMAGE}" in calls


def test_launcher_scopes_daemon_query_and_verified_image_to_checkout(tmp_path: Path):
    repo, env, call_log = make_launcher_repo(tmp_path, "AGENT_BACKEND=codex\n")

    result = run_launcher(repo, env)

    assert result.returncode == 0, result.stderr
    calls = read_calls(call_log)
    docker_ps = next(
        line
        for line in calls
        if line.startswith("docker|ps|")
        and f"|label=com.docker.compose.project=lao-{TEST_INSTANCE_ID}|" in line
    )
    assert f"|--filter|label=com.docker.compose.project=lao-{TEST_INSTANCE_ID}|" in docker_ps
    assert "|--filter|label=com.docker.compose.service=orchestrator" in docker_ps
    assert f"daemon-image|{TEST_VERIFIED_IMAGE}" in calls


def test_launcher_never_executes_dotenv_contents(tmp_path: Path):
    marker = tmp_path / "dotenv-was-executed"
    env_text = f"AGENT_BACKEND=codex\nUNRELATED=$(touch {marker})\n"
    repo, env, call_log = make_launcher_repo(tmp_path, env_text)

    result = run_launcher(repo, env)

    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    assert any("|--backend|codex|" in line for line in read_calls(call_log))


def test_launcher_rejects_executable_backend_value_without_running_it(tmp_path: Path):
    marker = tmp_path / "backend-was-executed"
    repo, env, call_log = make_launcher_repo(tmp_path, f"AGENT_BACKEND=$(touch {marker})\n")

    result = run_launcher(repo, env)

    assert result.returncode == 20
    assert not marker.exists()
    calls = read_calls(call_log)
    assert not any(line.startswith("updater|") for line in calls)
    assert not any(line.startswith("daemon|") for line in calls)


def test_refresh_and_daemon_arguments_are_forwarded(tmp_path: Path):
    repo, env, call_log = make_launcher_repo(tmp_path)

    result = run_launcher(repo, env, "--refresh-credentials", "--", "--max-iterations", "1")

    assert result.returncode == 0, result.stderr
    calls = read_calls(call_log)
    assert "bootstrap|--refresh-credentials" in calls
    assert "daemon|--max-iterations|1" in calls


@pytest.mark.parametrize("mode", ["dirty", "fetch-fail"])
def test_git_update_problem_uses_verified_fallback(tmp_path: Path, mode: str):
    repo, env, call_log = make_launcher_repo(tmp_path)
    env["GIT_MODE"] = mode
    env["UPDATER_STATUS"] = "10"

    result = run_launcher(repo, env, skip_git=False)

    assert result.returncode == 0, result.stderr
    calls = read_calls(call_log)
    assert any(line.startswith("updater|") and "|--fallback-only" in line for line in calls)
    assert any(line.startswith("daemon|") for line in calls)
    assert "last verified image" in result.stderr


def test_fast_forward_reexecs_updated_launcher_once_and_preserves_args(tmp_path: Path):
    repo, env, call_log = make_launcher_repo(tmp_path)
    env["GIT_MODE"] = "update"

    result = run_launcher(
        repo,
        env,
        "--refresh-credentials",
        "--",
        "--max-iterations",
        "2",
        skip_git=False,
    )

    assert result.returncode == 0, result.stderr
    calls = read_calls(call_log)
    assert sum(line.startswith("git|merge|--ff-only") for line in calls) == 1
    assert sum(line.startswith("updater|") for line in calls) == 1
    assert sum(line.startswith("daemon|") for line in calls) == 1
    assert "bootstrap|--refresh-credentials" in calls
    assert "daemon|--max-iterations|2" in calls
    assert "restarting with the updated launcher" in result.stdout


def test_existing_daemon_is_left_running_without_update(tmp_path: Path):
    repo, env, call_log = make_launcher_repo(tmp_path)
    env["FAKE_DAEMON_RUNNING"] = "1"

    result = run_launcher(repo, env, skip_git=False)

    assert result.returncode == 0, result.stderr
    calls = read_calls(call_log)
    assert not any(line.startswith(("bootstrap|", "updater|", "daemon|")) for line in calls)
    assert "already running" in result.stdout
    assert "no update or restart" in result.stdout


@pytest.mark.parametrize("identity_match", ["working-dir", "data-mount"])
def test_matching_unscoped_legacy_daemon_is_left_running_without_update(
    tmp_path: Path, identity_match: str
):
    repo, env, call_log = make_launcher_repo(tmp_path)
    env["FAKE_UNSCOPED_DAEMON"] = identity_match

    result = run_launcher(repo, env, skip_git=False)

    assert result.returncode == 0, result.stderr
    calls = read_calls(call_log)
    assert (
        "docker|ps|--quiet|--filter|label=com.docker.compose.service=orchestrator"
        in calls
    )
    assert any(
        line.startswith("docker|inspect|--format|")
        and "com.docker.compose.project.working_dir" in line
        for line in calls
    )
    assert any(
        line.startswith("docker|inspect|--format|")
        and 'Destination "/workspace/data"' in line
        for line in calls
    )
    assert not any(
        line.startswith(("bootstrap|", "updater|", "daemon|"))
        for line in calls
    )
    assert "already running (legacy-lao-daemon)" in result.stdout
    assert "no update or restart" in result.stdout


def test_unrelated_unscoped_orchestrator_is_ignored(tmp_path: Path):
    repo, env, call_log = make_launcher_repo(tmp_path, "AGENT_BACKEND=codex\n")
    env["FAKE_UNSCOPED_DAEMON"] = "unrelated"

    result = run_launcher(repo, env)

    assert result.returncode == 0, result.stderr
    calls = read_calls(call_log)
    assert any(line.startswith("updater|") for line in calls)
    assert any(line.startswith("daemon|") for line in calls)
    assert f"daemon-image|{TEST_VERIFIED_IMAGE}" in calls
    assert "already running" not in result.stdout


def test_workspace_working_dir_collision_uses_authoritative_data_mount(tmp_path: Path):
    repo, env, call_log = make_launcher_repo(tmp_path, "AGENT_BACKEND=codex\n")
    # A separate host checkout may have created the same container-side label.
    # Its bind mount source is the authoritative identity and must win.
    env["FAKE_UNSCOPED_DAEMON"] = "workspace-collision"

    result = run_launcher(repo, env)

    assert result.returncode == 0, result.stderr
    calls = read_calls(call_log)
    assert any(
        line.startswith("docker|inspect|--format|")
        and "com.docker.compose.project.working_dir" in line
        for line in calls
    )
    assert any(
        line.startswith("docker|inspect|--format|")
        and 'Destination "/workspace/data"' in line
        for line in calls
    )
    assert (
        "docker|inspect|--format|{{json .Config.Cmd}}|legacy-daemon-container-id"
        not in calls
    )
    assert any(line.startswith("updater|") for line in calls)
    assert any(line.startswith("daemon|") for line in calls)
    assert "already running" not in result.stdout


def test_first_run_without_verified_fallback_stops(tmp_path: Path):
    repo, env, call_log = make_launcher_repo(tmp_path)
    env["UPDATER_STATUS"] = "21"

    result = run_launcher(repo, env)

    assert result.returncode == 21
    assert "no verified fallback image" in result.stderr
    assert not any(line.startswith("daemon|") for line in read_calls(call_log))


def test_configuration_or_auth_failure_never_starts_daemon(tmp_path: Path):
    repo, env, call_log = make_launcher_repo(tmp_path)
    env["UPDATER_STATUS"] = "20"

    result = run_launcher(repo, env)

    assert result.returncode == 20
    assert "configuration or authentication validation failed" in result.stderr
    assert not any(line.startswith("daemon|") for line in read_calls(call_log))


@pytest.mark.parametrize(
    ("fallback_status", "expected_status", "starts_daemon"),
    [("10", 0, True), ("21", 21, False)],
)
def test_unexpected_updater_exit_checks_fallback_exactly_once(
    tmp_path: Path, fallback_status: str, expected_status: int, starts_daemon: bool
):
    repo, env, call_log = make_launcher_repo(tmp_path)
    env.update({"UPDATER_STATUS": "99", "UPDATER_FALLBACK_STATUS": fallback_status})

    result = run_launcher(repo, env)

    assert result.returncode == expected_status
    updater_calls = [line for line in read_calls(call_log) if line.startswith("updater|")]
    assert len(updater_calls) == 2
    assert "|--fallback-only" not in updater_calls[0]
    assert updater_calls[1].endswith("|--fallback-only")
    daemon_started = any(line.startswith("daemon|") for line in read_calls(call_log))
    assert daemon_started is starts_daemon


def make_updater_repo(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    repo = tmp_path / "updater"
    scripts = repo / "scripts"
    fake_bin = tmp_path / "updater-bin"
    scripts.mkdir(parents=True)
    fake_bin.mkdir()
    shutil.copy2(UPDATER_SCRIPT, scripts / "update-agent-tools.sh")
    (scripts / "update-agent-tools.sh").chmod(0o755)
    shutil.copy2(HOST_PATH_LIB, scripts / "host-path-lib.sh")
    shutil.copy2(DOCKERFILE, repo / "Dockerfile")

    write_executable(
        scripts / "compose.sh",
        r'''
        #!/usr/bin/env bash
        set -eu
        { printf 'compose'; printf '|%s' "$@"; printf '\n'; } >> "$CALL_LOG"
        case "$*" in
          *'build --pull orchestrator tests'*) exit "${FAKE_BUILD_STATUS:-0}" ;;
          *'orchestrator startup-check'*)
            if [[ "${FAKE_DIRTY_DURING_VALIDATION:-0}" == 1 &&
                  "${LAO_IMAGE:-}" == linear-agent-orchestrator:candidate-* ]]; then
              touch "$FAKE_GIT_DIRTY_MARKER"
            fi
            exit "${FAKE_STARTUP_STATUS:-0}"
            ;;
          *'tests'*) exit "${FAKE_TEST_STATUS:-0}" ;;
        esac
        ''',
    )
    write_executable(
        fake_bin / "git",
        r'''
        #!/usr/bin/env bash
        set -eu
        { printf 'git'; printf '|%s' "$@"; printf '\n'; } >> "$CALL_LOG"
        case "${1:-}" in
          rev-parse)
            if [[ "${2:-}" == --verify && "${3:-}" == HEAD ]]; then
              printf '%s\n' "$FAKE_GIT_SHA"
              exit 0
            fi
            exit 1
            ;;
          status)
            if [[ -f "$FAKE_GIT_DIRTY_MARKER" || "${FAKE_GIT_MODE:-clean}" == dirty ]]; then
              printf '?? changed-during-validation\n'
            fi
            ;;
          *) exit 1 ;;
        esac
        ''',
    )
    write_executable(
        fake_bin / "docker",
        r'''
        #!/usr/bin/env bash
        set -eu
        { printf 'docker'; printf '|%s' "$@"; printf '\n'; } >> "$CALL_LOG"
        mkdir -p "$FAKE_DOCKER_STATE_DIR"
        verified="${LAO_VERIFIED_IMAGE:-linear-agent-orchestrator:verified-${LAO_INSTANCE_ID}}"
        previous="${LAO_PREVIOUS_IMAGE:-linear-agent-orchestrator:previous-${LAO_INSTANCE_ID}}"

        state_file_for() {
          case "$1" in
            "$verified") printf '%s/verified.id\n' "$FAKE_DOCKER_STATE_DIR" ;;
            "$previous") printf '%s/previous.id\n' "$FAKE_DOCKER_STATE_DIR" ;;
            linear-agent-orchestrator:candidate-*)
              printf '%s/candidate.id\n' "$FAKE_DOCKER_STATE_DIR"
              ;;
            *) return 1 ;;
          esac
        }

        read_image_id() {
          local image="$1" file
          file="$(state_file_for "$image" 2>/dev/null || true)"
          if [[ -n "$file" && -f "$file" ]]; then
            cat "$file"
            return 0
          fi
          if [[ "$image" == "$verified" && "${FAKE_HAVE_VERIFIED:-0}" == 1 ]]; then
            printf '%s\n' "${FAKE_VERIFIED_ID:-sha256:verified-old}"
            return 0
          fi
          if [[ "$image" == linear-agent-orchestrator:candidate-* ]]; then
            printf '%s\n' "${FAKE_CANDIDATE_ID:-sha256:candidate-new}"
            return 0
          fi
          if [[ "$image" == linear-agent-orchestrator:local &&
                "${FAKE_HAVE_LEGACY:-0}" == 1 ]]; then
            printf '%s\n' "${FAKE_LEGACY_ID:-sha256:legacy}"
            return 0
          fi
          return 1
        }

        write_image_id() {
          local image="$1" id="$2" file
          file="$(state_file_for "$image")" || return 1
          printf '%s\n' "$id" > "$file"
        }

        if [[ "${1:-}" == image && "${2:-}" == inspect ]]; then
          image="${@: -1}"
          id="$(read_image_id "$image")" || exit 1
          [[ "$*" == *'--format'* ]] && printf '%s\n' "$id"
          exit 0
        fi
        if [[ "${1:-}" == image && "${2:-}" == tag ]]; then
          source="${3:-}"
          destination="${4:-}"
          id="$(read_image_id "$source")" || exit 1
          write_image_id "$destination" "$id"
          exit 0
        fi
        if [[ "${1:-}" == image && "${2:-}" == rm ]]; then
          image="${@: -1}"
          file="$(state_file_for "$image" 2>/dev/null || true)"
          [[ -n "$file" ]] && rm -f -- "$file"
          exit 0
        fi
        if [[ "${1:-}" == image ]]; then
          exit 0
        fi
        if [[ "${1:-}" == run && "$*" == *' npm view '* ]]; then
          case "$*" in
            *' @openai/codex '*) printf '"%s"\n' "${NPM_CODEX:-1.0.0}" ;;
            *' oh-my-codex '*) printf '"%s"\n' "${NPM_OMX:-1.0.0}" ;;
            *' @anthropic-ai/claude-code '*) printf '"%s"\n' "${NPM_CLAUDE:-3.0.0}" ;;
            *) exit 1 ;;
          esac
          exit 0
        fi
        if [[ "${1:-}" == run && "$*" == *'--entrypoint codex '* ]]; then
          [[ "$*" == *' --version' ]] && { printf 'codex %s\n' "${CLI_CODEX:-1.0.0}"; exit 0; }
          if [[ "${5:-}" == "$verified" && "${FAKE_VERIFIED_HELP_STATUS:-0}" != 0 ]]; then
            exit "$FAKE_VERIFIED_HELP_STATUS"
          fi
          printf '%s\n' '--cd --model --sandbox --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check'
          exit 0
        fi
        if [[ "${1:-}" == run && "$*" == *'--entrypoint omx '* ]]; then
          [[ "$*" == *' --version' ]] && { printf 'omx %s\n' "${CLI_OMX:-0.20.0}"; exit 0; }
          if [[ "${5:-}" == "$verified" && "${FAKE_VERIFIED_HELP_STATUS:-0}" != 0 ]]; then
            exit "$FAKE_VERIFIED_HELP_STATUS"
          fi
          printf '%s\n' '--cd --model --sandbox --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check'
          exit 0
        fi
        if [[ "${1:-}" == run && "$*" == *'--entrypoint claude '* ]]; then
          [[ "$*" == *' --version' ]] && { printf 'claude %s\n' "${CLI_CLAUDE:-2.1.195}"; exit 0; }
          if [[ "${5:-}" == "$verified" && "${FAKE_VERIFIED_HELP_STATUS:-0}" != 0 ]]; then
            exit "$FAKE_VERIFIED_HELP_STATUS"
          fi
          printf '%s\n' '--print --permission-mode --effort --model --dangerously-skip-permissions'
          exit 0
        fi
        exit 1
        ''',
    )

    call_log = tmp_path / "updater-calls.log"
    env = {**os.environ}
    for key in (
        "LAO_INSTANCE_ID",
        "LAO_VERIFIED_IMAGE",
        "LAO_PREVIOUS_IMAGE",
        "COMPOSE_PROJECT_NAME",
        "FAKE_GIT_MODE",
        "FAKE_DIRTY_DURING_VALIDATION",
        "FAKE_VERIFIED_HELP_STATUS",
    ):
        env.pop(key, None)
    env.update(
        {
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "CALL_LOG": str(call_log),
            "LAO_UPDATE_DIR": str(tmp_path / "updater-state"),
            "LAO_INSTANCE_ID": TEST_INSTANCE_ID,
            "FAKE_DOCKER_STATE_DIR": str(tmp_path / "fake-docker-state"),
            "FAKE_GIT_DIRTY_MARKER": str(tmp_path / "git-dirty"),
            "FAKE_BUILD_STATUS": "1",
        }
    )
    return repo, env, call_log


def run_updater(
    repo: Path,
    env: dict[str, str],
    backend: str,
    git_sha: str = "a" * 40,
    *,
    fallback_only: bool = False,
) -> subprocess.CompletedProcess[str]:
    args = [
        str(repo / "scripts" / "update-agent-tools.sh"),
        "--backend",
        backend,
        "--git-sha",
        git_sha,
    ]
    if fallback_only:
        args.append("--fallback-only")
    run_env = {**env, "FAKE_GIT_SHA": git_sha}
    return subprocess.run(
        args,
        cwd=repo,
        env=run_env,
        text=True,
        capture_output=True,
        check=False,
    )


def write_verified_state(
    env: dict[str, str],
    *,
    git_sha: str = "1" * 40,
    image_id: str = "sha256:verified-old",
    backend: str = "codex",
) -> tuple[Path, str]:
    update_dir = Path(env["LAO_UPDATE_DIR"])
    update_dir.mkdir(parents=True, exist_ok=True)
    state = "\n".join(
        (
            "STATE_VERSION=1",
            f"LAO_GIT_SHA={git_sha}",
            f"LAO_SOURCE_REVISION={git_sha}",
            f"LAO_IMAGE_ID={image_id}",
            f"LAO_IMAGE={TEST_VERIFIED_IMAGE}",
            "CODEX_VERSION=1.0.0",
            "OMX_VERSION=1.0.0",
            "CLAUDE_CODE_VERSION=3.0.0",
            f"LAST_CHECKED_BACKEND={backend}",
            "",
        )
    )
    state_file = update_dir / "verified.env"
    state_file.write_text(state, encoding="utf-8")
    docker_state = Path(env["FAKE_DOCKER_STATE_DIR"])
    docker_state.mkdir(parents=True, exist_ok=True)
    (docker_state / "verified.id").write_text(f"{image_id}\n", encoding="utf-8")
    return state_file, state


@pytest.mark.parametrize(
    ("backend", "expected_packages"),
    [
        ("codex", {"@openai/codex"}),
        ("omx", {"@openai/codex", "oh-my-codex"}),
        ("claude", {"@anthropic-ai/claude-code"}),
    ],
)
def test_updater_queries_only_packages_needed_by_backend(
    tmp_path: Path, backend: str, expected_packages: set[str]
):
    repo, env, call_log = make_updater_repo(tmp_path)

    result = run_updater(repo, env, backend)

    assert result.returncode == 21
    npm_calls = [line for line in read_calls(call_log) if "|npm|view|" in line]
    actual_packages = {line.split("|npm|view|", 1)[1].split("|", 1)[0] for line in npm_calls}
    assert actual_packages == expected_packages
    assert "no valid verified image" in result.stderr


def test_candidate_build_failure_preserves_verified_state(tmp_path: Path):
    repo, env, call_log = make_updater_repo(tmp_path)
    previous_sha = "1" * 40
    state_file, previous_state = write_verified_state(
        env,
        git_sha=previous_sha,
    )
    env.update({"NPM_CODEX": "2.0.0", "FAKE_BUILD_STATUS": "1"})

    result = run_updater(repo, env, "codex", git_sha="2" * 40)

    assert result.returncode == 10, result.stderr
    assert state_file.read_text(encoding="utf-8") == previous_state
    calls = read_calls(call_log)
    assert any(line.startswith("compose|") and "|build|--pull|orchestrator|tests" in line for line in calls)
    assert any(line.startswith("compose|") and "|orchestrator|startup-check" in line for line in calls)
    candidate_image = candidate_image_from_calls(calls)
    assert not any(
        line == f"docker|image|tag|{candidate_image}|{TEST_VERIFIED_IMAGE}"
        for line in calls
    )
    candidate_removals = [
        line for line in calls
        if line == f"docker|image|rm|--force|{candidate_image}"
    ]
    assert len(candidate_removals) >= 2, "candidate must also be cleaned after the failed build"


def test_fallback_only_rejects_verified_image_with_incompatible_backend_help(tmp_path: Path):
    repo, env, call_log = make_updater_repo(tmp_path)
    write_verified_state(env)
    env["FAKE_VERIFIED_HELP_STATUS"] = "1"

    result = run_updater(repo, env, "codex", fallback_only=True)

    assert result.returncode == 21, result.stderr
    calls = read_calls(call_log)
    assert any(
        line.startswith("docker|run|") and
        f"|--entrypoint|codex|{TEST_VERIFIED_IMAGE}|--help" in line
        for line in calls
    )
    assert not any(
        line.startswith("compose|") and "|orchestrator|startup-check" in line
        for line in calls
    )
    assert "incompatible with the configured backend" in result.stderr


def test_interrupted_promotion_restores_previous_image_before_fallback(tmp_path: Path):
    repo, env, call_log = make_updater_repo(tmp_path)
    state_file, previous_state = write_verified_state(env)
    docker_state = Path(env["FAKE_DOCKER_STATE_DIR"])
    (docker_state / "verified.id").write_text("sha256:candidate-new\n", encoding="utf-8")
    (docker_state / "previous.id").write_text("sha256:verified-old\n", encoding="utf-8")

    result = run_updater(repo, env, "codex", fallback_only=True)

    assert result.returncode == 10, result.stderr
    assert state_file.read_text(encoding="utf-8") == previous_state
    assert (docker_state / "verified.id").read_text(encoding="utf-8") == "sha256:verified-old\n"
    calls = read_calls(call_log)
    assert f"docker|image|tag|{TEST_PREVIOUS_IMAGE}|{TEST_VERIFIED_IMAGE}" in calls
    assert any(
        line.startswith("compose|") and "|orchestrator|startup-check" in line
        for line in calls
    )
    assert "restored the last verified image" in result.stdout


def test_checkout_change_during_candidate_validation_prevents_promotion(tmp_path: Path):
    repo, env, call_log = make_updater_repo(tmp_path)
    env.update(
        {
            "FAKE_BUILD_STATUS": "0",
            "FAKE_DIRTY_DURING_VALIDATION": "1",
        }
    )

    result = run_updater(repo, env, "codex")

    assert result.returncode == 21, result.stderr
    assert Path(env["FAKE_GIT_DIRTY_MARKER"]).exists()
    assert not (Path(env["LAO_UPDATE_DIR"]) / "verified.env").exists()
    calls = read_calls(call_log)
    candidate_image = candidate_image_from_calls(calls)
    assert not any(
        line == f"docker|image|tag|{candidate_image}|{TEST_VERIFIED_IMAGE}"
        for line in calls
    )
    assert any(
        line.startswith("compose|") and "|orchestrator|startup-check" in line
        for line in calls
    )
    assert sum(line == "git|status|--porcelain|--untracked-files=normal" for line in calls) >= 3
    assert "checkout changed while validating the candidate" in result.stderr


@pytest.mark.parametrize("latest", ["2.0.0-beta.1", "not-a-version", "\"\""])
def test_invalid_npm_latest_is_never_built(tmp_path: Path, latest: str):
    repo, env, call_log = make_updater_repo(tmp_path)
    env["NPM_CODEX"] = latest

    result = run_updater(repo, env, "codex")

    assert result.returncode == 21
    assert not any(line.startswith("compose|") and "|build|" in line for line in read_calls(call_log))


def test_candidate_startup_failure_without_fallback_is_configuration_error(tmp_path: Path):
    repo, env, call_log = make_updater_repo(tmp_path)
    env.update({"FAKE_BUILD_STATUS": "0", "FAKE_STARTUP_STATUS": "1"})

    result = run_updater(repo, env, "codex")

    assert result.returncode == 20, result.stderr
    calls = read_calls(call_log)
    assert any(line.startswith("compose|") and "|tests" in line for line in calls)
    assert any(line.startswith("compose|") and "|orchestrator|startup-check" in line for line in calls)
    candidate_image = candidate_image_from_calls(calls)
    candidate_removals = [
        line for line in calls
        if line == f"docker|image|rm|--force|{candidate_image}"
    ]
    assert len(candidate_removals) >= 2
    assert not (Path(env["LAO_UPDATE_DIR"]) / "verified.env").exists()


def test_successful_candidate_is_promoted_with_atomic_verified_metadata(tmp_path: Path):
    repo, env, call_log = make_updater_repo(tmp_path)
    env["FAKE_BUILD_STATUS"] = "0"
    git_sha = "3" * 40

    result = run_updater(repo, env, "codex", git_sha=git_sha)

    assert result.returncode == 0, result.stderr
    state_file = Path(env["LAO_UPDATE_DIR"]) / "verified.env"
    assert state_file.exists()
    state = dict(
        line.split("=", 1)
        for line in state_file.read_text(encoding="utf-8").splitlines()
    )
    assert state == {
        "STATE_VERSION": "1",
        "LAO_GIT_SHA": git_sha,
        "LAO_SOURCE_REVISION": git_sha,
        "LAO_IMAGE_ID": "sha256:candidate-new",
        "LAO_IMAGE": TEST_VERIFIED_IMAGE,
        "CODEX_VERSION": "1.0.0",
        "OMX_VERSION": "0.20.0",
        "CLAUDE_CODE_VERSION": "2.1.195",
        "LAST_CHECKED_BACKEND": "codex",
    }
    assert stat.S_IMODE(state_file.stat().st_mode) == 0o600
    calls = read_calls(call_log)
    candidate_image = candidate_image_from_calls(calls)
    assert f"docker|image|tag|{candidate_image}|{TEST_VERIFIED_IMAGE}" in calls
    assert any(line.startswith("compose|") and "|build|--pull|orchestrator|tests" in line for line in calls)
    assert any(line.startswith("compose|") and "|tests" in line for line in calls)
    assert any(line.startswith("compose|") and "|orchestrator|startup-check" in line for line in calls)
