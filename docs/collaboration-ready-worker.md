# Collaboration-ready worker contract

This document defines the safety and completion contract that must hold before
the next live Linear test. It is a readiness contract, not evidence that every
item below is already implemented. Use the verification checklist at the end
before processing a real issue.

## Scope for the next live test

The daemon remains a polling-only, serial worker for one explicitly configured
Linear team, project, assignee, and repository. One fixed repository per daemon
is sufficient while the daemon executes at most one issue at a time.

The worker must:

- poll only Todo issues in the configured project that are assigned to the
  configured developer;
- default the assignee to the Linear API viewer when no explicit assignee ID is
  configured;
- treat unassigned issues and issues assigned to anyone else as ineligible;
- expose exactly one trusted repository to the agent at `/workspace/project`;
- move an issue to In Progress before agent execution; and
- move an issue to Done only after backend-neutral semantic completion is
  proven by structured validation evidence.

The worker must not add webhooks, Linear native agent sessions, Docker socket
access, issue-controlled host paths, automatic pushes, pull-request creation or
merge, issue deletion, or any automatic terminal transition other than Done.

## Isolation and trust boundary

Docker is the external security boundary. Codex, OMX, and Claude may run
non-interactively with their internal approval and sandbox restrictions bypassed
inside that boundary. This is safe only when the container configuration keeps
the boundary narrow:

- mount one explicitly configured repository at `/workspace/project`;
- never mount `/var/run/docker.sock`;
- run as the invoking Linux user's UID/GID rather than root;
- drop Linux capabilities and enable `no-new-privileges`;
- use dedicated, persistent agent credential homes instead of read-write mounts
  of the developer's live Codex or Claude homes;
- keep Linear secrets out of the agent subprocess environment;
- treat orchestrator runtime state — SQLite, artifacts, and the agent-console
  logs under `.runtime` — as host-owned, non-issue-controlled paths, and keep
  the tmux control socket container-private so the console never widens the
  host/container boundary (see [agent-console.md](agent-console.md)); and
- never derive a bind-mount source or other host path from issue content.

Permission bypass is a container-scoped operating mode, not a safe default for
running these backends directly on the host.

The optional agent console defaults to `auto` (enabled inside a host tmux,
headless otherwise) and, when enabled, gives read-only live observation for every
backend; direct keyboard input is explicit capability metadata
(`AGENT_CONSOLE_DIRECT_INPUT`, default false, false for the bundled one-shot
omx/codex/claude adapters) and is never inferred from the backend name. The
read-only default and this flag are an honest UI capability gate, not an
authorization control against a trusted host operator who can already
`docker exec`. Its lifecycle JSONL is secret-free by contract, but the raw
`output.log` inherits agent-output sensitivity.

## Structured result contract

Transport success is not task success. An agent CLI exiting with code zero only
means that the backend produced a response; it does not prove that the requested
work completed.

Every backend must write a JSON result file to the orchestrator-provided result
path before exiting. The contract is backend-neutral:

```json
{
  "version": 1,
  "status": "completed",
  "summary": "Implemented the requested change and verified it.",
  "validation": [
    {
      "description": "Unit tests",
      "command": "pytest -q",
      "exit_code": 0,
      "evidence": "74 passed"
    }
  ],
  "artifacts": ["relative/path/to/report.md"],
  "files_changed": ["relative/path/to/file.py"]
}
```

Required fields and semantics:

- `version` is `1`.
- `status` is one of `completed`, `failed`, `blocked`, or
  `needs_clarification`.
- `summary` is a non-empty human-readable outcome.
- `validation` is a non-empty array when `status` is `completed`.
- every completed validation entry has a non-empty description, a numeric
  `exit_code` equal to zero, and non-empty evidence;
- artifact and changed-file paths are relative to their declared roots and must
  not escape those roots.

The Done gate requires all of the following:

1. the backend process did not time out;
2. its transport exit code is zero;
3. the result file exists and parses as the supported schema;
4. `status` is `completed`; and
5. validation is non-empty and every recorded validation exit code is zero.

Any missing, malformed, unsupported, failed, blocked, or clarification result
fails closed. The issue remains non-Done and receives a failure comment that
explains the result-contract violation without exposing secrets.

## Workspace and execution rules

The generated prompt must state that `/workspace/project` is the only trusted
repository and that conflicting absolute repository paths in issue text refer
to that mount. The issue cannot select another work tree or host path.

Before claiming live work, preflight must verify that:

- the configured repository exists, is writable by the runtime UID/GID, and has
  the repository metadata required by the selected workflow;
- required baseline tools and the selected backend CLI are installed;
- backend authentication is usable without an interactive prompt;
- OMX setup and required skills are available through the installed CLI, not
  merely present as files;
- work and artifact directories are writable;
- the configured Linear assignee can be resolved; and
- the repository state is safe for the configured serial workflow.

If a clean-tree guard is enabled, an absent repository or a dirty work tree must
block execution rather than silently bypass the check. Because one repository is
reused across sequential issues, operators must integrate or discard the prior
issue's changes before another issue can safely run.

## Failure and retry behavior

Missing or invalid structured results, backend failures, preflight failures, and
validation failures must not move the issue to Done. They should record a
failure comment and retain enough local state for deterministic diagnosis and a
deliberate retry after the issue or runner configuration changes.

Local SQLite claims coordinate only processes that share the same database.
Assignee filtering prevents different developers' daemons from taking each
other's work, but it does not eliminate the race between two machines running
credentials for the same assignee. Do not run two such daemons for the next live
test.

## Deferred hardening

The following are intentionally deferred until after the next live test:

- issue-level worktrees and concurrent issue execution;
- a separate agent service that cannot observe the orchestrator's Linear
  environment through the container process namespace;
- restricted outbound network policy;
- cross-machine claim arbitration for the same Linear assignee;
- streaming long-running backend output; and
- automatic source-control or pull-request operations.

These deferrals do not weaken the invariants above. In particular, Docker socket
mounts and automatic push/PR/merge behavior remain out of scope.

## Verification checklist

Complete all checks without performing a destructive live Linear mutation:

1. Run the full Python test suite, including focused result-contract, assignee,
   preflight, prompt-path, and failure-to-Done regression tests.
2. Run Python compilation/type checking and shell syntax checks.
3. Inspect the resolved Compose model without publishing expanded secrets and
   prove non-root UID/GID, capability drop, `no-new-privileges`, one repository
   mount, dedicated credential sources, bypass defaults, and no Docker socket.
4. Smoke-test the built container for baseline tools, backend versions,
   non-interactive authentication, repository ownership, and repository writes.
5. Use a fake backend for end-to-end cases proving that only a completed result
   with non-empty passing validation reaches Done; all other transport/result
   combinations remain non-Done.
6. Confirm the polling request includes the resolved assignee and excludes
   unassigned or differently assigned issues.
7. Confirm the agent-console contract: emitted events validate against the v1
   schema in [agent-console.md](agent-console.md); the console is off by default;
   `supports_direct_input` is false for one-shot omx/codex/claude; the log root
   resolves under the host `.runtime/logs/console` mount while the tmux socket
   stays container-private; and no secret appears in `lifecycle.jsonl`.

Do not paste raw `docker compose config` output into logs or reports: Compose can
expand secrets from the local environment. Assert only the sanitized fields
needed by the verification gate.
