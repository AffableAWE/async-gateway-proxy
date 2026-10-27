import json
import logging

from app.logging_config import JsonFormatter


def make_record(**extra) -> logging.LogRecord:
    record = logging.LogRecord(
        name="gateway.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="proxy_forward",
        args=None,
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_output_is_one_json_line_with_extra_fields():
    line = JsonFormatter().format(make_record(upstream="http://x:1", status_code=200))
    assert "\n" not in line

    data = json.loads(line)
    assert data["message"] == "proxy_forward"
    assert data["level"] == "info"
    assert data["upstream"] == "http://x:1"
    assert data["status_code"] == 200
    # Internal LogRecord attributes must not leak into the output.
    assert "lineno" not in data and "pathname" not in data


def test_unserializable_values_do_not_crash():
    line = JsonFormatter().format(make_record(weird=object()))
    assert "weird" in json.loads(line)
