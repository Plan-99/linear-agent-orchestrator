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
- Added `AGENT_EFFORT` reasoning-effort passthrough (omx/codex: `-c model_reasoning_effort=...`, claude: `--effort ...`).
- Added DEV-21/DEV-22 self-hosted parallel-worker artifacts: module architecture doc and CONTRIBUTING draft.

### Changed
- Hardened the tmux agent console and PTY/bootstrap/supervisor scripts with updated runner integration, documentation, and test coverage.
- Changed daemon idle polling so Ctrl-C exits promptly while in-flight runs finish.
- Interactive runs now end when the operator exits the TUI instead of on structured-result detection; submitting a valid result moves the issue to the attention state while the session stays open, and the Done gate is evaluated after exit (two-phase success path, DEV-23/DEV-27).
- `RUNNER_TIMEOUT_SECONDS` now acts as an activity-based idle timeout for interactive runs (console output extends the deadline), bounded by the `RUNNER_MAX_LIFETIME_SECONDS` hard cap (DEV-25).
- Failed, timed-out, and fail-in-doubt runs are now routed to the configured attention state (`LINEAR_ATTENTION_STATE_NAME`, default `In Review`) after the failure comment, forming a single human review queue.

### Fixed
- Fixed isolated worktree creation failing on Git LFS target repositories: the runtime image now ships `git-lfs` with a system-level filter install (the repo's `post-checkout` hook aborted `git worktree add` when the binary was missing) and sets `lfs.skipdownloaderrors` so checkouts keep pointer files instead of failing when LFS objects are absent and no remote credentials exist (GitHub #1).
- Fixed fresh SQLite startup `database is locked` races with earlier busy timeouts, duplicate-column-tolerant migrations, and serialized factories.
- Fixed tmux new-window targeting from daemon pane IDs for per-run window console creation.
- Fixed interactive TUI backends receiving a pipe instead of the real pane PTY.
- Fixed interactive runs deadlocking on an undrained stdout pipe once output exceeded the pipe buffer (DEV-24 regression coverage).
- Fixed successful-run finalization deleting the local branch when the agent had committed inside its worktree (DEV-26).
