import pytest

from app.resilience.retry import Failure, RetryBudget, backoff_delay, is_retryable


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# --- what is retryable ---------------------------------------------------


@pytest.mark.parametrize("method", ["GET", "POST", "PATCH", "PUT", "DELETE"])
def test_connect_failures_are_always_retryable(method):
    # The request never left the gateway, so even a POST is safe.
    assert is_retryable(method, Failure.CONNECT)


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_timeouts_are_never_retried(method):
    assert not is_retryable(method, Failure.TIMEOUT)


@pytest.mark.parametrize(
    ("method", "expected"),
    [
        ("GET", True),
        ("HEAD", True),
        ("OPTIONS", True),
        ("PUT", True),
        ("DELETE", True),
        ("POST", False),
        ("PATCH", False),
        ("get", True),
    ],
)
def test_after_sending_only_idempotent_methods_are_retried(method, expected):
    assert is_retryable(method, Failure.STATUS) is expected
    assert is_retryable(method, Failure.TRANSPORT) is expected


# --- backoff -------------------------------------------------------------


def upper_bound(lo, hi):
    return hi  # an rng that always picks the top of the range


def test_backoff_doubles_up_to_the_cap():
    delays = [backoff_delay(n, 0.1, 1.0, rng=upper_bound) for n in range(1, 7)]
    assert delays == pytest.approx([0.1, 0.2, 0.4, 0.8, 1.0, 1.0])


def test_backoff_is_random_from_zero_to_the_cap():
    delays = [backoff_delay(3, 0.1, 1.0) for _ in range(500)]
    assert all(0 <= d <= 0.4 for d in delays)
    assert min(delays) < 0.1 and max(delays) > 0.3  # spread over the range
    assert len(set(delays)) > 400  # not one fixed value


# --- retry budget ----------------------------------------------------------


def test_budget_floor_allows_a_few_retries_with_no_traffic():
    budget = RetryBudget(ratio=0.2, min_per_second=0.3, window_seconds=10, clock=FakeClock())
    # floor = 0.3 * 10 = 3 retries
    assert [budget.try_spend() for _ in range(4)] == [True, True, True, False]


def test_budget_grows_with_traffic():
    budget = RetryBudget(ratio=0.2, min_per_second=0, window_seconds=10, clock=FakeClock())
    for _ in range(50):
        budget.record_request()
    # 20% of 50 = 10 retries
    results = [budget.try_spend() for _ in range(11)]
    assert results == [True] * 10 + [False]


def test_budget_forgets_old_traffic():
    clock = FakeClock()
    budget = RetryBudget(ratio=1.0, min_per_second=0, window_seconds=10, clock=clock)
    budget.record_request()
    assert budget.try_spend()
    assert not budget.try_spend()

    clock.now += 11  # both the request and the retry left the window
    assert not budget.try_spend()  # no recent traffic, no floor
    budget.record_request()
    assert budget.try_spend()


def test_zero_budget_means_no_retries():
    budget = RetryBudget(ratio=0, min_per_second=0, clock=FakeClock())
    for _ in range(100):
        budget.record_request()
    assert not budget.try_spend()
