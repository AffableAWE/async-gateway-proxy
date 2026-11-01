import logging

import pytest

from app.resilience.breaker import CircuitBreaker, State


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def breaker(clock) -> CircuitBreaker:
    return CircuitBreaker(
        "http://up", failure_threshold=3, recovery_timeout_seconds=10, clock=clock
    )


def fail(breaker, times):
    for _ in range(times):
        assert breaker.allow_request()
        breaker.record_failure()


def test_starts_closed_and_allows(breaker):
    assert breaker.state is State.CLOSED
    assert breaker.allow_request()


def test_opens_after_threshold_failures_in_a_row(breaker):
    fail(breaker, 2)
    assert breaker.state is State.CLOSED
    fail(breaker, 1)
    assert breaker.state is State.OPEN
    assert not breaker.allow_request()


def test_a_success_resets_the_count(breaker):
    fail(breaker, 2)
    breaker.record_success()
    fail(breaker, 2)
    assert breaker.state is State.CLOSED  # 2 + 2, but never 3 in a row


def test_retry_after_counts_down(breaker, clock):
    fail(breaker, 3)
    assert breaker.retry_after_seconds() == 10
    clock.now += 7.5
    assert breaker.retry_after_seconds() == 3  # rounded up


def test_half_open_after_the_timeout(breaker, clock):
    fail(breaker, 3)
    clock.now += 9.9
    assert breaker.state is State.OPEN
    clock.now += 0.1
    assert breaker.state is State.HALF_OPEN


def test_half_open_lets_exactly_one_probe_through(breaker, clock):
    fail(breaker, 3)
    clock.now += 10
    assert breaker.allow_request()  # the probe
    assert not breaker.allow_request()  # everyone else waits for its result
    assert not breaker.allow_request()


def test_successful_probe_closes(breaker, clock):
    fail(breaker, 3)
    clock.now += 10
    assert breaker.allow_request()
    breaker.record_success()
    assert breaker.state is State.CLOSED
    assert breaker.allow_request()


def test_failed_probe_reopens_for_a_full_timeout(breaker, clock):
    fail(breaker, 3)
    clock.now += 10
    assert breaker.allow_request()
    breaker.record_failure()
    assert breaker.state is State.OPEN
    clock.now += 9
    assert breaker.state is State.OPEN
    clock.now += 1
    assert breaker.state is State.HALF_OPEN


def test_after_closing_it_takes_threshold_failures_again(breaker, clock):
    fail(breaker, 3)
    clock.now += 10
    breaker.allow_request()
    breaker.record_success()
    fail(breaker, 2)
    assert breaker.state is State.CLOSED


def test_state_changes_are_logged(breaker, clock, caplog):
    caplog.set_level(logging.INFO)
    fail(breaker, 3)
    clock.now += 10
    breaker.allow_request()
    breaker.record_success()

    changes = [
        (r.from_state, r.to_state)
        for r in caplog.records
        if r.getMessage() == "circuit_state_change"
    ]
    assert changes == [("closed", "open"), ("open", "half_open"), ("half_open", "closed")]
    assert all(
        r.upstream == "http://up"
        for r in caplog.records
        if r.getMessage() == "circuit_state_change"
    )


def test_a_lost_probe_does_not_block_forever(breaker, clock):
    fail(breaker, 3)
    clock.now += 10
    assert breaker.allow_request()  # probe goes out, its result never comes back
    clock.now += 5
    assert not breaker.allow_request()
    clock.now += 5  # one recovery timeout later
    assert breaker.allow_request()  # a new probe is allowed
