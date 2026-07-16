#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$ROOT_DIR"
# Use the host Docker daemon's checkout identity even inside a dev container.
# shellcheck source=scripts/host-path-lib.sh
source "$ROOT_DIR/scripts/host-path-lib.sh"
INSTANCE_ID="${LAO_INSTANCE_ID:-$(lao_checkout_instance_id "$ROOT_DIR")}"
if [[ ! "$INSTANCE_ID" =~ ^[a-z0-9][a-z0-9_-]{0,63}$ ]]; then
  echo "update-agent-tools: invalid LAO_INSTANCE_ID '$INSTANCE_ID'." >&2
  exit 64
fi
export LAO_INSTANCE_ID="$INSTANCE_ID"
export COMPOSE_PROJECT_NAME="lao-${INSTANCE_ID}"

# Exit contract consumed by run.sh.
readonly EXIT_RECOVERABLE=10
readonly EXIT_CONFIG=20
readonly EXIT_NO_FALLBACK=21
readonly STATE_VERSION_CURRENT=1

UPDATE_DIR="${LAO_UPDATE_DIR:-$ROOT_DIR/.runtime/update}"
STATE_FILE="$UPDATE_DIR/verified.env"
NPM_IMAGE="${LAO_UPDATE_NPM_IMAGE:-node:22-slim}"
CANDIDATE_IMAGE=""
VERIFIED_IMAGE="linear-agent-orchestrator:verified-${INSTANCE_ID}"
PREVIOUS_IMAGE="linear-agent-orchestrator:previous-${INSTANCE_ID}"
LEGACY_IMAGE="linear-agent-orchestrator:local"

backend=""
git_sha=""
fallback_only=false

usage() {
  cat >&2 <<'USAGE'
Usage: scripts/update-agent-tools.sh --backend codex|omx|claude --git-sha SHA [--fallback-only]

Builds and validates a candidate image, then promotes it to the verified tag.
Exit 10 means an existing verified image is usable as a fallback; exit 20 means
configuration/authentication is invalid; exit 21 means no fallback exists.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --backend)
      [[ $# -ge 2 ]] || { usage; exit 64; }
      backend="$2"
      shift 2
      ;;
    --git-sha)
      [[ $# -ge 2 ]] || { usage; exit 64; }
      git_sha="$2"
      shift 2
      ;;
    --fallback-only)
      fallback_only=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "update-agent-tools.sh: unknown option: $1" >&2
      usage
      exit 64
      ;;
  esac
done

case "$backend" in codex|omx|claude) ;; *) usage; exit 64 ;; esac
if [[ ! "$git_sha" =~ ^[0-9a-f]{40,64}$ && "$git_sha" != "unknown" ]]; then
  echo "update-agent-tools.sh: --git-sha must be a full hexadecimal Git object ID." >&2
  exit 64
fi

umask 077
mkdir -p -- "$UPDATE_DIR"
chmod 700 "$UPDATE_DIR"
# Candidate tags are process- and revision-specific; every managed image tag
# is additionally scoped to this physical checkout.
CANDIDATE_IMAGE="linear-agent-orchestrator:candidate-${INSTANCE_ID}-${git_sha:0:12}-$$"


log() { printf 'update-agent-tools: %s\n' "$*"; }
warn() { printf 'update-agent-tools: %s\n' "$*" >&2; }

is_stable_semver() {
  [[ "${1:-}" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]]
}

# Return 0 when $1 >= $2. Compare decimal component lengths before lexical
# order, avoiding shell-integer overflow even for an unexpected huge registry
# response that is still syntactically valid SemVer.
semver_at_least() {
  local -a lhs rhs
  local index
  IFS=. read -r -a lhs <<<"$1"
  IFS=. read -r -a rhs <<<"$2"
  for index in 0 1 2; do
    if (( ${#lhs[$index]} > ${#rhs[$index]} )); then return 0; fi
    if (( ${#lhs[$index]} < ${#rhs[$index]} )); then return 1; fi
    if [[ "${lhs[$index]}" > "${rhs[$index]}" ]]; then return 0; fi
    if [[ "${lhs[$index]}" < "${rhs[$index]}" ]]; then return 1; fi
  done
  return 0
}

# Candidate metadata is trustworthy only while HEAD and the complete nonignored
# worktree still match the revision selected by run.sh.
source_checkout_matches() {
  local current status
  if [[ "$git_sha" == "unknown" ]]; then
    warn "candidate builds require an identifiable Git revision."
    return 1
  fi
  current="$(git rev-parse --verify HEAD 2>/dev/null || true)"
  if [[ -z "$current" || "$current" != "$git_sha" ]]; then
    warn "checkout HEAD changed while preparing the candidate."
    return 1
  fi
  if ! status="$(git status --porcelain --untracked-files=normal 2>/dev/null)"; then
    warn "could not verify checkout cleanliness."
    return 1
  fi
  if [[ -n "$status" ]]; then
    warn "checkout contents changed while preparing the candidate."
    return 1
  fi
}
state_value() {
  local key="$1" file="$2" line
  [[ -r "$file" ]] || return 1
  line="$(grep -E "^${key}=" "$file" 2>/dev/null | tail -n 1)" || return 1
  [[ -n "$line" ]] || return 1
  printf '%s\n' "${line#*=}"
}

image_id() {
  docker image inspect --format '{{.Id}}' "$1" 2>/dev/null
}

image_exists() {
  docker image inspect "$1" >/dev/null 2>&1
}

# Populates V_* only when the persisted metadata is syntactically valid.
load_state_metadata() {
  V_GIT_SHA="$(state_value LAO_GIT_SHA "$STATE_FILE" 2>/dev/null || true)"
  V_SOURCE_REVISION="$(state_value LAO_SOURCE_REVISION "$STATE_FILE" 2>/dev/null || true)"
  V_IMAGE_ID="$(state_value LAO_IMAGE_ID "$STATE_FILE" 2>/dev/null || true)"
  V_IMAGE="$(state_value LAO_IMAGE "$STATE_FILE" 2>/dev/null || true)"
  V_CODEX_VERSION="$(state_value CODEX_VERSION "$STATE_FILE" 2>/dev/null || true)"
  V_OMX_VERSION="$(state_value OMX_VERSION "$STATE_FILE" 2>/dev/null || true)"
  V_CLAUDE_VERSION="$(state_value CLAUDE_CODE_VERSION "$STATE_FILE" 2>/dev/null || true)"
  V_BACKEND="$(state_value LAST_CHECKED_BACKEND "$STATE_FILE" 2>/dev/null || true)"
  local version
  version="$(state_value STATE_VERSION "$STATE_FILE" 2>/dev/null || true)"

  [[ "$version" == "$STATE_VERSION_CURRENT" ]] || return 1
  [[ "$V_IMAGE" == "$VERIFIED_IMAGE" ]] || return 1
  [[ "$V_GIT_SHA" =~ ^[0-9a-f]{40,64}$ || "$V_GIT_SHA" == "legacy-unknown" ]] || return 1
  [[ "$V_SOURCE_REVISION" == "$V_GIT_SHA" ]] || return 1
  [[ "$V_BACKEND" == "codex" || "$V_BACKEND" == "omx" || "$V_BACKEND" == "claude" ]] || return 1
  is_stable_semver "$V_CODEX_VERSION" || return 1
  is_stable_semver "$V_OMX_VERSION" || return 1
  is_stable_semver "$V_CLAUDE_VERSION" || return 1
}

# Require metadata and the immutable image behind the verified tag to agree.
load_verified_state() {
  local actual_id
  load_state_metadata || return 1
  actual_id="$(image_id "$VERIFIED_IMAGE" || true)"
  [[ -n "$actual_id" && "$actual_id" == "$V_IMAGE_ID" ]] || return 1
}

# Promotion retags the old verified image before atomically replacing state. If
# the host dies in that small window, restore the old tag recorded by state.
recover_interrupted_promotion() {
  local verified_id previous_id
  if ! load_state_metadata; then
    return 0
  fi
  verified_id="$(image_id "$VERIFIED_IMAGE" || true)"
  if [[ -n "$verified_id" && "$verified_id" == "$V_IMAGE_ID" ]]; then
    return 0
  fi
  previous_id="$(image_id "$PREVIOUS_IMAGE" || true)"
  if [[ -z "$previous_id" || "$previous_id" != "$V_IMAGE_ID" ]]; then
    return 0
  fi
  warn "recovering an interrupted promotion from the previous image."
  if ! docker image tag "$PREVIOUS_IMAGE" "$VERIFIED_IMAGE"; then
    warn "could not restore the verified tag from the previous image."
    return 1
  fi
  if ! load_verified_state; then
    warn "the restored verified tag does not match persisted state."
    return 1
  fi
  log "restored the last verified image after an interrupted promotion."
}

write_state() {
  local sha="$1" id="$2" codex_version="$3" omx_version="$4" claude_version="$5" checked_backend="$6"
  local tmp
  tmp="$(mktemp "$UPDATE_DIR/verified.env.tmp.XXXXXX")" || return 1
  if ! printf '%s\n' \
    "STATE_VERSION=$STATE_VERSION_CURRENT" \
    "LAO_GIT_SHA=$sha" \
    "LAO_SOURCE_REVISION=$sha" \
    "LAO_IMAGE_ID=$id" \
    "LAO_IMAGE=$VERIFIED_IMAGE" \
    "CODEX_VERSION=$codex_version" \
    "OMX_VERSION=$omx_version" \
    "CLAUDE_CODE_VERSION=$claude_version" \
    "LAST_CHECKED_BACKEND=$checked_backend" >"$tmp"; then
    rm -f -- "$tmp"
    return 1
  fi
  chmod 600 "$tmp"
  if ! mv -f -- "$tmp" "$STATE_FILE"; then
    rm -f -- "$tmp"
    return 1
  fi
}

dockerfile_default() {
  local key="$1" value
  value="$(sed -n "s/^ARG ${key}=//p" "$ROOT_DIR/Dockerfile" | head -n 1)"
  is_stable_semver "$value" || return 1
  printf '%s\n' "$value"
}

extract_cli_version() {
  local image="$1" executable="$2" output value
  output="$(docker run --rm --entrypoint "$executable" "$image" --version 2>&1)" || return 1
  value="$(printf '%s\n' "$output" | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' | head -n 1 || true)"
  is_stable_semver "$value" || return 1
  printf '%s\n' "$value"
}

check_exact_cli_versions() {
  local image="$1" expected_codex="$2" expected_omx="$3" expected_claude="$4"
  local actual_codex actual_omx actual_claude
  actual_codex="$(extract_cli_version "$image" codex)" || return 1
  actual_omx="$(extract_cli_version "$image" omx)" || return 1
  actual_claude="$(extract_cli_version "$image" claude)" || return 1
  if [[ "$actual_codex" != "$expected_codex" || "$actual_omx" != "$expected_omx" || "$actual_claude" != "$expected_claude" ]]; then
    warn "CLI version mismatch (codex=$actual_codex, omx=$actual_omx, claude=$actual_claude)."
    return 1
  fi
}

help_has() {
  local output="$1"
  shift
  local flag
  for flag in "$@"; do
    if ! grep -Fq -- "$flag" <<<"$output"; then
      warn "required backend option is missing from help: $flag"
      return 1
    fi
  done
}

check_docker_tooling() {
  local image="$1"
  case "${LAO_DOCKER_HOST:-false}" in
    1 | true | TRUE | yes | YES | on | ON)
      if ! docker run --rm --entrypoint docker "$image" --version >/dev/null 2>&1; then
        warn "Docker host mode requires a Docker CLI in the verified image."
        return 1
      fi
      if ! docker run --rm --entrypoint docker "$image" compose version >/dev/null 2>&1; then
        warn "Docker host mode requires the Docker Compose plugin."
        return 1
      fi
      if ! docker run --rm --entrypoint docker "$image" buildx version >/dev/null 2>&1; then
        warn "Docker host mode requires the Docker Buildx plugin."
        return 1
      fi
      ;;
    0 | false | FALSE | no | NO | off | OFF | "") ;;
    *) warn "invalid LAO_DOCKER_HOST value"; return 1 ;;
  esac
}

check_backend_help() {
  local image="$1" selected_backend="$2" root_help exec_help
  check_docker_tooling "$image" || return 1
  case "$selected_backend" in
    codex)
      root_help="$(docker run --rm --entrypoint codex "$image" --help 2>&1)" || return 1
      exec_help="$(docker run --rm --entrypoint codex "$image" exec --help 2>&1)" || return 1
      help_has "$root_help" --cd --model --sandbox --dangerously-bypass-approvals-and-sandbox || return 1
      help_has "$exec_help" --skip-git-repo-check || return 1
      ;;
    omx)
      # OMX's root command is a wrapper and intentionally does not repeat
      # Codex flags. Its `exec` subcommand delegates to Codex and must expose
      # every option used by lao-agent-runner.
      root_help="$(docker run --rm --entrypoint omx "$image" --help 2>&1)" || return 1
      exec_help="$(docker run --rm --entrypoint omx "$image" exec --help 2>&1)" || return 1
      help_has "$root_help" exec || return 1
      help_has "$exec_help" --cd --model --sandbox --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check || return 1
      ;;
    claude)
      root_help="$(docker run --rm --entrypoint claude "$image" --help 2>&1)" || return 1
      help_has "$root_help" --print --permission-mode --effort --model --dangerously-skip-permissions || return 1
      ;;
  esac
}

compose_with_image() {
  local image="$1" codex_version="$2" omx_version="$3" claude_version="$4" revision="$5"
  shift 5
  env \
    LAO_IMAGE="$image" \
    CODEX_VERSION="$codex_version" \
    OMX_VERSION="$omx_version" \
    CLAUDE_CODE_VERSION="$claude_version" \
    LAO_SOURCE_REVISION="$revision" \
    "$ROOT_DIR/scripts/compose.sh" "$@"
}

run_full_tests() {
  local image="$1" codex_version="$2" omx_version="$3" claude_version="$4" revision="$5"
  compose_with_image "$image" "$codex_version" "$omx_version" "$claude_version" "$revision" \
    run --rm --no-deps tests
}

startup_check() {
  local image="$1" codex_version="$2" omx_version="$3" claude_version="$4" revision="$5"
  compose_with_image "$image" "$codex_version" "$omx_version" "$claude_version" "$revision" \
    run --rm --no-deps -e AGENT_CONSOLE=true orchestrator startup-check
}

verified_startup_ok() {
  load_verified_state || return 1
  startup_check "$VERIFIED_IMAGE" "$V_CODEX_VERSION" "$V_OMX_VERSION" "$V_CLAUDE_VERSION" "$V_SOURCE_REVISION"
}

fallback_after_failure() {
  warn "$1"
  cleanup_candidate
  if ! load_verified_state; then
    warn "no valid verified image is available."
    return "$EXIT_NO_FALLBACK"
  fi
  if ! check_backend_help "$VERIFIED_IMAGE" "$backend"; then
    warn "the verified image is incompatible with the configured backend."
    return "$EXIT_NO_FALLBACK"
  fi
  if verified_startup_ok; then
    return "$EXIT_RECOVERABLE"
  fi
  warn "the verified image also failed startup-check; treating .env/authentication as invalid."
  return "$EXIT_CONFIG"
}

startup_failure() {
  warn "$1"
  cleanup_candidate
  if load_verified_state && check_backend_help "$VERIFIED_IMAGE" "$backend" && verified_startup_ok; then
    return "$EXIT_RECOVERABLE"
  fi
  warn "startup-check failed without a usable verified image; treating .env/authentication as invalid."
  return "$EXIT_CONFIG"
}

resolve_npm_latest() {
  local package="$1" output value
  output="$(docker run --rm --pull=missing "$NPM_IMAGE" npm view "$package" dist-tags.latest --json 2>/dev/null)" || return 1
  value="$(printf '%s' "$output" | tr -d '[:space:]\"')"
  is_stable_semver "$value" || return 1
  printf '%s\n' "$value"
}

# Best-effort migration for pre-updater installations. A legacy local image is
# adopted only after the same version/help/test/startup gates. Recording an
# unknown source revision deliberately causes a clean candidate rebuild next.
LEGACY_STARTUP_FAILED=false
CANDIDATE_OWNED=false
cleanup_candidate() {
  if [[ "$CANDIDATE_OWNED" == "true" ]]; then
    docker image rm --force "$CANDIDATE_IMAGE" >/dev/null 2>&1 || true
    CANDIDATE_OWNED=false
  fi
}
trap cleanup_candidate EXIT

adopt_legacy_image() {
  local codex_version omx_version claude_version id
  image_exists "$LEGACY_IMAGE" || return 1
  log "checking the existing local image as an initial fallback."
  codex_version="$(extract_cli_version "$LEGACY_IMAGE" codex)" || return 1
  omx_version="$(extract_cli_version "$LEGACY_IMAGE" omx)" || return 1
  claude_version="$(extract_cli_version "$LEGACY_IMAGE" claude)" || return 1
  check_backend_help "$LEGACY_IMAGE" "$backend" || return 1
  run_full_tests "$LEGACY_IMAGE" "$codex_version" "$omx_version" "$claude_version" legacy-unknown || return 1
  if ! startup_check "$LEGACY_IMAGE" "$codex_version" "$omx_version" "$claude_version" legacy-unknown; then
    LEGACY_STARTUP_FAILED=true
    return 1
  fi
  docker image tag "$LEGACY_IMAGE" "$VERIFIED_IMAGE" || return 1
  id="$(image_id "$VERIFIED_IMAGE")" || return 1
  if ! write_state legacy-unknown "$id" "$codex_version" "$omx_version" "$claude_version" "$backend"; then
    docker image rm --force "$VERIFIED_IMAGE" >/dev/null 2>&1 || true
    return 1
  fi
  log "adopted the validated local image as the initial fallback."
}

promote_candidate() {
  local codex_version="$1" omx_version="$2" claude_version="$3" id old_valid=false
  if load_verified_state; then
    old_valid=true
    docker image tag "$VERIFIED_IMAGE" "$PREVIOUS_IMAGE" || return 1
  fi
  if ! docker image tag "$CANDIDATE_IMAGE" "$VERIFIED_IMAGE"; then
    return 1
  fi
  if ! id="$(image_id "$VERIFIED_IMAGE")"; then
    if [[ "$old_valid" == "true" ]]; then
      docker image tag "$PREVIOUS_IMAGE" "$VERIFIED_IMAGE" >/dev/null 2>&1 || true
    else
      docker image rm --force "$VERIFIED_IMAGE" >/dev/null 2>&1 || true
    fi
    return 1
  fi
  if ! write_state "$git_sha" "$id" "$codex_version" "$omx_version" "$claude_version" "$backend"; then
    if [[ "$old_valid" == "true" ]]; then
      docker image tag "$PREVIOUS_IMAGE" "$VERIFIED_IMAGE" >/dev/null 2>&1 || true
    else
      docker image rm --force "$VERIFIED_IMAGE" >/dev/null 2>&1 || true
    fi
    return 1
  fi
  docker image rm --force "$CANDIDATE_IMAGE" >/dev/null 2>&1 || true
  CANDIDATE_OWNED=false
}

main() {
  local have_verified=false
  local codex_version omx_version claude_version latest needs_build=true

  if ! command -v docker >/dev/null 2>&1; then
    warn "Docker is required."
    return "$EXIT_NO_FALLBACK"
  fi

  if ! recover_interrupted_promotion; then
    warn "automatic interrupted-promotion recovery failed."
  fi

  if load_verified_state; then
    have_verified=true
  elif adopt_legacy_image; then
    have_verified=true
    load_verified_state
  fi

  if [[ "$fallback_only" == "true" ]]; then
    if [[ "$have_verified" == "true" ]]; then
      if ! check_backend_help "$VERIFIED_IMAGE" "$backend"; then
        warn "the verified image is incompatible with the configured backend."
        return "$EXIT_NO_FALLBACK"
      fi
      if verified_startup_ok; then
        return "$EXIT_RECOVERABLE"
      fi
      return "$EXIT_CONFIG"
    fi
    [[ "$LEGACY_STARTUP_FAILED" == "true" ]] && return "$EXIT_CONFIG"
    return "$EXIT_NO_FALLBACK"
  fi

  if ! source_checkout_matches; then
    fallback_after_failure "checkout no longer matches the selected source revision."
    return $?
  fi
  if [[ "$have_verified" == "true" ]]; then
    codex_version="$V_CODEX_VERSION"
    omx_version="$V_OMX_VERSION"
    claude_version="$V_CLAUDE_VERSION"
  else
    codex_version="$(dockerfile_default CODEX_VERSION)" || {
      fallback_after_failure "Dockerfile has an invalid Codex version default."; return $?;
    }
    omx_version="$(dockerfile_default OMX_VERSION)" || {
      fallback_after_failure "Dockerfile has an invalid OMX version default."; return $?;
    }
    claude_version="$(dockerfile_default CLAUDE_CODE_VERSION)" || {
      fallback_after_failure "Dockerfile has an invalid Claude Code version default."; return $?;
    }
  fi

  case "$backend" in
    codex)
      latest="$(resolve_npm_latest @openai/codex)" || {
        fallback_after_failure "could not resolve the stable Codex npm version."; return $?;
      }
      semver_at_least "$latest" "$codex_version" || {
        fallback_after_failure "the npm Codex latest tag would downgrade $codex_version to $latest."; return $?;
      }
      codex_version="$latest"
      ;;
    omx)
      latest="$(resolve_npm_latest @openai/codex)" || {
        fallback_after_failure "could not resolve the stable Codex npm version."; return $?;
      }
      semver_at_least "$latest" "$codex_version" || {
        fallback_after_failure "the npm Codex latest tag would downgrade $codex_version to $latest."; return $?;
      }
      codex_version="$latest"
      latest="$(resolve_npm_latest oh-my-codex)" || {
        fallback_after_failure "could not resolve the stable OMX npm version."; return $?;
      }
      semver_at_least "$latest" "$omx_version" || {
        fallback_after_failure "the npm OMX latest tag would downgrade $omx_version to $latest."; return $?;
      }
      omx_version="$latest"
      ;;
    claude)
      latest="$(resolve_npm_latest @anthropic-ai/claude-code)" || {
        fallback_after_failure "could not resolve the stable Claude Code npm version."; return $?;
      }
      semver_at_least "$latest" "$claude_version" || {
        fallback_after_failure "the npm Claude latest tag would downgrade $claude_version to $latest."; return $?;
      }
      claude_version="$latest"
      ;;
  esac

  if [[ "$have_verified" == "true" && "$V_GIT_SHA" == "$git_sha" && \
        "$V_CODEX_VERSION" == "$codex_version" && "$V_OMX_VERSION" == "$omx_version" && \
        "$V_CLAUDE_VERSION" == "$claude_version" ]]; then
    needs_build=false
  fi

  if [[ "$needs_build" == "false" ]]; then
    log "source and tool versions are already verified; skipping rebuild."
    if ! check_backend_help "$VERIFIED_IMAGE" "$backend"; then
      fallback_after_failure "the verified image does not support the configured backend options."
      return $?
    fi
    if ! verified_startup_ok; then
      return "$EXIT_CONFIG"
    fi
    if [[ "$V_BACKEND" != "$backend" ]]; then
      write_state "$V_GIT_SHA" "$V_IMAGE_ID" "$V_CODEX_VERSION" "$V_OMX_VERSION" "$V_CLAUDE_VERSION" "$backend" || \
        return "$EXIT_RECOVERABLE"
    fi
    return 0
  fi

  if ! source_checkout_matches; then
    fallback_after_failure "checkout changed before the candidate build."
    return $?
  fi
  log "building candidate (codex=$codex_version, omx=$omx_version, claude=$claude_version, source=${git_sha:0:12})."
  CANDIDATE_OWNED=true
  docker image rm --force "$CANDIDATE_IMAGE" >/dev/null 2>&1 || true
  if ! compose_with_image "$CANDIDATE_IMAGE" "$codex_version" "$omx_version" "$claude_version" "$git_sha" \
      build --pull orchestrator tests; then
    fallback_after_failure "candidate image build failed."
    return $?
  fi
  if ! check_exact_cli_versions "$CANDIDATE_IMAGE" "$codex_version" "$omx_version" "$claude_version"; then
    fallback_after_failure "candidate CLI version verification failed."
    return $?
  fi
  if ! check_backend_help "$CANDIDATE_IMAGE" "$backend"; then
    fallback_after_failure "candidate backend option smoke test failed."
    return $?
  fi
  if ! run_full_tests "$CANDIDATE_IMAGE" "$codex_version" "$omx_version" "$claude_version" "$git_sha"; then
    fallback_after_failure "candidate test suite failed."
    return $?
  fi
  if ! startup_check "$CANDIDATE_IMAGE" "$codex_version" "$omx_version" "$claude_version" "$git_sha"; then
    startup_failure "candidate startup-check failed."
    return $?
  fi
  if ! source_checkout_matches; then
    fallback_after_failure "checkout changed while validating the candidate."
    return $?
  fi
  if ! promote_candidate "$codex_version" "$omx_version" "$claude_version"; then
    fallback_after_failure "candidate promotion failed."
    return $?
  fi
  log "candidate promoted to $VERIFIED_IMAGE."
  return 0
}

set +e
main
status=$?
set -e
exit "$status"
