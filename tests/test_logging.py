import json
import logging

from linear_agent_orchestrator.logging import JsonFormatter, configure_logging


def test_json_formatter_emits_structured_fields():
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="linear_agent_orchestrator.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello",
        args=(),
        exc_info=None,
    )
    record.issue_id = "LIN-1"

    payload = json.loads(formatter.format(record))

    assert payload["level"] == "INFO"
    assert payload["logger"] == "linear_agent_orchestrator.test"
    assert payload["message"] == "hello"
    assert payload["issue_id"] == "LIN-1"
    assert "timestamp" in payload


def test_configure_logging_replaces_root_handlers():
    configure_logging("INFO", "json")

    root = logging.getLogger()

    assert root.level == logging.INFO
    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0].formatter, JsonFormatter)
