---
name: linear-create-issue
description: Create Linear issues in Backlog by default (or an explicitly requested state), assigning a fitting project milestone when one exists, or draft them, from concise requests in fresh sessions by producing LAO-compatible Markdown and invoking a guarded local launcher. Use when the user asks to upload, create, register, file, post, or add an issue, task, feature, or bug to Linear, including Korean requests such as "리니어 이슈로 올려줘", "이슈 등록해", "이슈 추가해", or "Linear에 생성해". Also use for Linear issue previews or drafts, but never mutate Linear unless the current user request explicitly requests creation.
---

# Linear Create Issue

Create a structured issue for the Linear team and project configured in Linear Agent Orchestrator (LAO). Keep credentials outside the skill and use the bundled launcher from any working directory. Backlog is the default creation state and the review gate; only the user promotes an approved issue to Todo so LAO can claim it, and only an explicit user request may target a different creation state. When the project tracks milestones, assign the issue to the best-fitting one at creation.

## Decide whether creation is authorized

- Treat explicit creation verbs in the current request, including "add", "create", "file", "post", "register", "upload", "추가해", "올려줘", "등록해", "업로드해", "발행해", and "생성해", as authorization for one Linear write.
- Let an explicit creation verb take precedence in phrases such as "정리해서 올려줘".
- For "draft", "preview", "review", "정리해", or ambiguous wording without a creation verb, prepare the title and body but do not run the live command.
- Never reuse write authorization from an earlier turn.
- Do not broaden a request for one issue into multiple issues.
- Do not retry a failed or interrupted live creation automatically.
- Create in Backlog by default. Pass a different `--state-name` only when the current user request explicitly names the target status (for example "Todo로 올려줘" or "In Progress 상태로 만들어"); never choose a non-Backlog state yourself, never promote the issue after creation, and note in the response that skipping Backlog bypasses the review gate.

## Prepare the issue

Inspect repository files only when the request depends on local project context. Never read `.env` contents to compose the issue and never place credentials, tokens, private keys, or unrelated personal data in the title or body.

Use a concise, actionable title and this exact non-empty Markdown schema:

```markdown
## Task
<the work to perform, including relevant scope and constraints>

## Acceptance Criteria
- <observable completion condition>

## Validation
- <commands, checks, or evidence that prove completion>
```

State uncertain requirements explicitly instead of inventing them. Keep validation proportional to the requested work.

The launcher supports title, description, an explicit creation state (default Backlog), and a project milestone; LAO configuration supplies team, project, Todo queue state, and assignee. If the request requires a priority, labels, cycle, parent, or due date, do not silently pretend those fields were set; explain that the user must adjust them manually after review.

## Assign a milestone when the project has one

Before the preview, list the configured project's milestones with the read-only command:

```text
python3 -I <skill-dir>/scripts/create_linear_issue.py --list-milestones --json
```

If the user names a milestone, use it verbatim. Otherwise pick the single milestone whose name and description clearly match the issue content and pass it with `--milestone-name` on both the preview and the live invocation. If the project has no milestones, or none is a clear fit, omit the flag and say so in the response instead of guessing. Never create, rename, or invent milestones; the launcher resolves the name to an existing milestone before the mutation and fails closed when it does not exist.

## Preview the exact request

Resolve `scripts/create_linear_issue.py` relative to this `SKILL.md`; never assume the user's current directory is the skill directory. Write the description to a private `0600` temporary Markdown file without interpolating its contents into a shell command.

Always use isolated Python and run the offline preview first:

```text
python3 -I <skill-dir>/scripts/create_linear_issue.py \
  --title <title> \
  --description-file <private-temporary-markdown-file> \
  [--state-name <explicitly-requested-state>] \
  [--milestone-name <chosen-milestone>] \
  --dry-run \
  --json
```

The preview validates configuration, secret exclusion, and body schema without constructing a Linear client. Verify that `creation_state_name` matches the intended state (`Backlog` unless the current request explicitly asked for another) and that `milestone_name` matches the chosen milestone (or is null when none was chosen); `queue_state_name` may remain `Todo` for the daemon. Also verify the reported team, project, and assignee source. Capture `request_sha256`; it binds the stripped title, exact body, creation state, milestone name, and configured nonsecret target fields. If the target conflicts with the request, stop rather than overriding or guessing.

## Create exactly once

Only after an authorized request and a successful preview, pass that preview digest to one live invocation:

```text
python3 -I <skill-dir>/scripts/create_linear_issue.py \
  --title <same-title> \
  --description-file <same-private-temporary-markdown-file> \
  [--state-name <same-state>] \
  [--milestone-name <same-milestone>] \
  --expected-request-sha256 <request_sha256-from-preview> \
  --json
```

The launcher verifies the digest immediately before network access. It records the request in a private SQLite ledger:

- Replay a completed request by returning its existing issue result without another mutation.
- Release a request that failed before mutation so it can be corrected.
- Mark an interrupted or mutation-time failure uncertain and block automatic retries.

Return the created issue identifier and URL, state the creation state and assigned milestone (or that none was assigned), and — when it was created in Backlog — remind the user to promote it to Todo only after review. Remove the temporary file after success or a logged `create_issue_description_error`, `create_issue_validation_error`, or `create_issue_preflight_error`. For `create_issue_failed`, interruption, or an uncertain-ledger message, report the title and preview `request_sha256`, keep the mutation blocked, and tell the user to check Linear before any manual recovery.

## Configuration discovery

Use only these deterministic locations; never search arbitrary `.env` files or infer executable code from a credential-file directory:

1. `--lao-root` and `--env-file`
2. `LINEAR_CREATE_ISSUE_LAO_ROOT` and `LINEAR_CREATE_ISSUE_ENV_FILE`
3. `~/.config/linear-create-issue/.env`
4. `~/Project/linear-agent-orchestrator/.env` with its matching fixed checkout

Require environment pointer paths to be absolute. Fail immediately when an explicit LAO root is invalid. Require the credential file and SQLite ledger to be private; store the default ledger at `~/.local/state/linear-create-issue/requests.sqlite3`. Treat the selected environment file as authoritative so unrelated session variables cannot redirect the issue.
