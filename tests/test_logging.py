import json
import logging

import pytest

from app.logging_config import JsonFormatter, install_request_id_factory
from app.request_context import request_id_var


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


# --- request id ------------------------------------------------------------


@pytest.fixture
def factory():
    """Install the request-id factory, and put the original back afterwards."""
    original = logging.getLogRecordFactory()
    install_request_id_factory()
    yield logging.getLogRecordFactory()
    logging.setLogRecordFactory(original)


def new_record(factory) -> logging.LogRecord:
    return factory("gateway.test", logging.INFO, __file__, 1, "event", None, None)


def test_request_id_is_added_inside_a_request(factory):
    token = request_id_var.set("req-123")
    try:
        data = json.loads(JsonFormatter().format(new_record(factory)))
    finally:
        request_id_var.reset(token)
    assert data["request_id"] == "req-123"


def test_request_id_is_left_out_outside_a_request(factory):
    data = json.loads(JsonFormatter().format(new_record(factory)))
    assert "request_id" not in data


def test_installing_twice_does_not_wrap_twice(factory):
    install_request_id_factory()
    assert logging.getLogRecordFactory() is factory
