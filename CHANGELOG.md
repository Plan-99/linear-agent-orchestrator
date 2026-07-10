# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Added
- Introduced the Linear agent orchestrator CLI/daemon, configuration, Linear client, issue schema, state handling, runner, Docker/Compose runtime, bootstrap/test scripts, docs, and tests.
- Added collaboration-ready worker and Linear test issue documentation.
- Added model-neutral tmux/PTY agent-console support with host/container entry scripts, lifecycle JSONL schema, config/env surface, console docs, and live backend attach tooling.
- Added regression/adversarial coverage for done-gate invariants, epic lane contracts, headless fallback, generic shell backend, PTY console, console schema, and shell console scripts.
- Added per-run tmux window consoles (`lao:<run_id>`), with split-pane attach retained as an optional console target.
- Added per-run git worktree isolation that starts from `HEAD`, commits successful runs to local `lao/<issue>-<key>` branches, and preserves failed worktrees.
- Added `MAX_PARALLEL_RUNS` parallel worker pool support with distinct claim owners, staggered polling, and parallel-run coverage.
- Added `AGENT_INTERACTIVE` conversation mode for backend TUIs hosted in run consoles with structured-result completion.
- Added DEV-18/DEV-19 artifacts: git-history CHANGELOG drafting and package metadata smoke tests.

### Changed
- Hardened the tmux agent console and PTY/bootstrap/supervisor scripts with updated runner integration, documentation, and test coverage.
- Changed daemon idle polling so Ctrl-C exits promptly while in-flight runs finish.

### Fixed
- Fixed fresh SQLite startup `database is locked` races with earlier busy timeouts, duplicate-column-tolerant migrations, and serialized factories.
- Fixed tmux new-window targeting from daemon pane IDs for per-run window console creation.
