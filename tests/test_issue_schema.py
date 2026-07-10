from linear_agent_orchestrator.issue_schema import clarification_comment, validate_issue_body


def test_schema_parser_accepts_required_sections():
    body = """
# Context
Ignore me.

## Task
Do the work.

## Acceptance Criteria
- observable result

## Validation
- pytest -q
"""

    result = validate_issue_body(body)

    assert result.valid is True
    assert result.missing_sections == ()
    assert result.empty_sections == ()
    assert result.parsed.get("Task") == "Do the work."


def test_schema_parser_rejects_missing_and_empty_sections_with_canonical_diagnostics():
    result = validate_issue_body("## Task\n\n## Validation\npytest -q")

    assert result.valid is False
    assert result.missing_sections == ("Acceptance Criteria",)
    assert result.empty_sections == ("Task",)
    assert "Missing required section: Acceptance Criteria" in result.diagnostics
    assert "Required section is empty: Task" in result.diagnostics


def test_clarification_comment_is_safe_and_actionable():
    result = validate_issue_body("## Task\nDo it")

    comment = clarification_comment("DEV-123", result)

    assert "DEV-123" in comment
    assert "## Acceptance Criteria" in comment
    assert "## Validation" in comment
    assert "will not execute a runner" in comment
    assert "LINEAR_API_KEY" not in comment
