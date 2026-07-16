#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_DIR="$ROOT_DIR/skills/linear-create-issue"
CODEX_SKILLS_HOME="${CODEX_HOME:-${HOME:?HOME or CODEX_HOME is required}/.codex}/skills"
TARGET_DIR="$CODEX_SKILLS_HOME/linear-create-issue"

if [[ "$CODEX_SKILLS_HOME" != /* ]]; then
  printf 'Codex skills path must be absolute: %s\n' "$CODEX_SKILLS_HOME" >&2
  exit 2
fi

install -d -m 0755 "$TARGET_DIR/agents" "$TARGET_DIR/scripts"
install -m 0644 "$SOURCE_DIR/SKILL.md" "$TARGET_DIR/SKILL.md"
install -m 0644 "$SOURCE_DIR/agents/openai.yaml" "$TARGET_DIR/agents/openai.yaml"
install -m 0755 \
  "$SOURCE_DIR/scripts/create_linear_issue.py" \
  "$TARGET_DIR/scripts/create_linear_issue.py"

printf 'Installed linear-create-issue at %s\n' "$TARGET_DIR"
