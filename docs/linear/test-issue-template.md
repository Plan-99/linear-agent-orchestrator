# Canonical Linear TEST Issue Template

Use this template for issues the polling daemon may execute in the configured TEST project.
G001 only documents the schema; later stories enforce it before execution.

```markdown
## Task
<what the runner should do>

## Acceptance Criteria
- <observable success criteria>

## Validation
- <commands or evidence expected>

<!-- Optional metadata: runner/repo hints only; v1 generic shell runner config primarily comes from daemon config. -->
<!--
metadata:
  repo: <optional repository/workspace hint>
  branch: <optional branch hint>
  runner: generic_shell_runner
-->
```

Required sections:
- `Task`
- `Acceptance Criteria`
- `Validation`

If a candidate issue is missing any required section, the daemon must not execute it and should add a clarification comment in a later story.
