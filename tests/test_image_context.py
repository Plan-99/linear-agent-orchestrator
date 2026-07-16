from __future__ import annotations

from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
REQUIRED_IMAGE_FIXTURES = (
    "Dockerfile",
    "run.sh",
    "compose.yaml",
    "compose.docker-host.yaml",
    ".env.example",
    "skills/linear-create-issue/SKILL.md",
    "skills/linear-create-issue/agents/openai.yaml",
    "skills/linear-create-issue/scripts/create_linear_issue.py",
)


@pytest.mark.parametrize("relative_path", REQUIRED_IMAGE_FIXTURES)
def test_candidate_image_contains_test_fixture(relative_path: str) -> None:
    """Candidate pytest must exercise a self-contained image, not a host bind."""

    path = ROOT / relative_path
    assert path.exists(), f"candidate image is missing required fixture: {relative_path}"
