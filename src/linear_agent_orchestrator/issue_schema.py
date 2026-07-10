"""Linear issue body parsing and schema validation for executable work items."""

from __future__ import annotations

from dataclasses import dataclass
import re

REQUIRED_SECTIONS: tuple[str, ...] = ("Task", "Acceptance Criteria", "Validation")
_SECTION_RE = re.compile(r"^#{1,6}\s+(?P<title>.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class ParsedIssueBody:
    """Parsed markdown sections from a Linear issue body."""

    raw: str
    sections: dict[str, str]

    def get(self, section: str) -> str:
        return self.sections.get(_normalize_heading(section), "")


@dataclass(frozen=True)
class IssueSchemaResult:
    """Validation result for the canonical Linear TEST issue schema."""

    valid: bool
    missing_sections: tuple[str, ...]
    empty_sections: tuple[str, ...]
    parsed: ParsedIssueBody

    @property
    def diagnostics(self) -> tuple[str, ...]:
        messages: list[str] = []
        for section in self.missing_sections:
            messages.append(f"Missing required section: {section}")
        for section in self.empty_sections:
            messages.append(f"Required section is empty: {section}")
        return tuple(messages)


def parse_issue_body(body: str | None) -> ParsedIssueBody:
    """Parse markdown headings into normalized section names and trimmed bodies."""

    raw = body or ""
    matches = list(_SECTION_RE.finditer(raw))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        title = _normalize_heading(match.group("title"))
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
        sections[title] = raw[start:end].strip()
    return ParsedIssueBody(raw=raw, sections=sections)


def validate_issue_body(body: str | None) -> IssueSchemaResult:
    """Validate required Task / Acceptance Criteria / Validation sections."""

    parsed = parse_issue_body(body)
    missing: list[str] = []
    empty: list[str] = []
    for section in REQUIRED_SECTIONS:
        key = _normalize_heading(section)
        if key not in parsed.sections:
            missing.append(section)
        elif not parsed.sections[key].strip():
            empty.append(section)
    return IssueSchemaResult(
        valid=not missing and not empty,
        missing_sections=tuple(missing),
        empty_sections=tuple(empty),
        parsed=parsed,
    )


def clarification_comment(identifier: str, result: IssueSchemaResult) -> str:
    """Build a safe clarification comment for a schema-missing issue.

    The body contains only canonical diagnostics and the public issue identifier;
    it never includes config secrets or runner command content.
    """

    lines = [
        "I cannot execute this issue yet because it is missing the required TEST issue schema.",
        "",
        f"Issue: `{identifier}`",
        "",
        "Required sections:",
        "- `## Task`",
        "- `## Acceptance Criteria`",
        "- `## Validation`",
        "",
        "Diagnostics:",
    ]
    for diagnostic in result.diagnostics:
        lines.append(f"- {diagnostic}")
    lines.extend(
        [
            "",
            "Please update the issue body with the missing content. I will leave it in `Todo` and will not execute a runner for this issue.",
        ]
    )
    return "\n".join(lines)


def _normalize_heading(title: str) -> str:
    return re.sub(r"\s+", " ", title.strip()).casefold()
