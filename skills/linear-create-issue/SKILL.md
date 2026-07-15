---
name: linear-create-issue
description: Create or draft Linear issues from concise requests in fresh Codex sessions by producing LAO-compatible Markdown and invoking a guarded local launcher. Use when the user asks to upload, create, register, file, post, or add an issue, task, feature, or bug to Linear, including Korean requests such as "리니어 이슈로 올려줘", "이슈 등록해", or "Linear에 생성해". Also use for Linear issue previews or drafts, but never mutate Linear unless the user explicitly requests creation.
---

# Linear Create Issue

Create a structured issue for the Linear team and project configured in Linear Agent Orchestrator (LAO). Keep credentials outside the skill and use the bundled launcher from any working directory.

## Decide whether creation is authorized

- Treat explicit verbs such as "create", "file", "post", "register", "upload", "올려줘", "등록해", and "생성해" as authorization for one Linear write.
- For "draft", "preview", "review", "정리해", or ambiguous wording, prepare the title and body but do not run the live command.
- Do not broaden a request for one issue into multiple issues.
- Do not retry a failed or interrupted live creation automatically. Report the uncertainty so the user can check Linear before trying again.

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

## Preview the target

Resolve `scripts/create_linear_issue.py` relative to this `SKILL.md`; never assume the user's current directory is the skill directory. Write the description to a temporary Markdown file without interpolating it into a shell command.

Always run the offline preview first:

```text
python3 <skill-dir>/scripts/create_linear_issue.py \
  --title <title> \
  --description-file <temporary-markdown-file> \
  --dry-run \
  --json
```

The preview validates configuration and body schema without opening a network connection. Verify that the reported team, project, state, and assignee source match the user's request. If the configured target conflicts with an explicitly requested target, stop and explain the mismatch rather than guessing or overriding it.

## Create exactly once

Only after an authorized request and a successful preview, run the same command once without `--dry-run`:

```text
python3 <skill-dir>/scripts/create_linear_issue.py \
  --title <title> \
  --description-file <temporary-markdown-file> \
  --json
```

Return the created issue identifier and URL. Remove the temporary description file after success or a definite validation failure. On a transport error or interruption, preserve enough local context to report the title and request fingerprint, but do not issue another mutation.

## Configuration discovery

The launcher uses only explicit, deterministic locations and never searches arbitrary `.env` files:

1. `--lao-root` and `--env-file`
2. `LINEAR_CREATE_ISSUE_LAO_ROOT` and `LINEAR_CREATE_ISSUE_ENV_FILE`
3. `~/.config/linear-create-issue/.env`
4. `~/Project/linear-agent-orchestrator/.env` with its matching checkout

The selected credential file must not be group- or world-accessible. Environment-file values are authoritative for the child LAO process so unrelated session variables cannot silently redirect the issue.
