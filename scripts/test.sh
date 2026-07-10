#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

./scripts/bootstrap.sh >/dev/null
if [[ "$#" -eq 0 ]]; then
  exec ./scripts/compose.sh run --rm --build tests
fi
exec ./scripts/compose.sh run --rm --build tests pytest -q "$@"
