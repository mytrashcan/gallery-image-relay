from __future__ import annotations

import logging

import pytest

from Module.post_retry import ALERT_AFTER_ATTEMPTS, PostRetrySchedule, RetryState, log_deferred_post


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_schedule(clock: FakeClock) -> PostRetrySchedule:
    return PostRetrySchedule(base_delay=60, max_delay=300, clock=clock)


def test_unknown_post_is_due() -> None:
    assert make_schedule(FakeClock()).is_due("1") is True


def test_failure_defers_until_backoff_elapses() -> None:
    clock = FakeClock()
    schedule = make_schedule(clock)

    state = schedule.record_failure("1")

    assert state == RetryState(attempts=1, delay=60, retry_at=1060)
    assert schedule.is_due("1") is False
    clock.now = 1059.9
    assert schedule.is_due("1") is False
    clock.now = 1060
    assert schedule.is_due("1") is True


def test_backoff_doubles_and_is_capped() -> None:
    schedule = make_schedule(FakeClock())

    delays = [schedule.record_failure("1").delay for _ in range(5)]

    assert delays == [60, 120, 240, 300, 300]


def test_backoff_stays_capped_after_many_failures() -> None:
    schedule = make_schedule(FakeClock())

    for _ in range(2000):
        state = schedule.record_failure("1")

    assert state.attempts == 2000
    assert state.delay == 300


def test_empty_schedule_passed_to_crawlers_is_kept() -> None:
    from Module.arca_crawler import ArcaliveCrawler
    from Module.crawler import DCInsideCrawler

    schedule = PostRetrySchedule(base_delay=5, max_delay=10)

    assert DCInsideCrawler("https://gall.dcinside.com/mgallery/board/lists/?id=t", post_retry=schedule).post_retry is schedule
    assert ArcaliveCrawler("https://arca.live/b/t", session=object(), post_retry=schedule).post_retry is schedule


def test_clear_and_retain_forget_state() -> None:
    schedule = make_schedule(FakeClock())
    for post_id in ("1", "2", "3"):
        schedule.record_failure(post_id)

    schedule.clear("1")
    schedule.retain(["2", "4"])

    assert len(schedule) == 1
    assert schedule.is_due("1") is True
    assert schedule.is_due("2") is False
    assert schedule.is_due("3") is True


@pytest.mark.parametrize(("base_delay", "max_delay"), [(0, 10), (-1, 10), (20, 10)])
def test_rejects_invalid_delays(base_delay: float, max_delay: float) -> None:
    with pytest.raises(ValueError):
        PostRetrySchedule(base_delay=base_delay, max_delay=max_delay)


def test_repeated_failure_escalates_to_error_with_identifiers_only(caplog) -> None:
    caplog.set_level(logging.WARNING, logger="Module.post_retry")

    log_deferred_post("dcinside", "cats", "42", RetryState(attempts=1, delay=60, retry_at=0))
    log_deferred_post("dcinside", "cats", "42", RetryState(ALERT_AFTER_ATTEMPTS, delay=960, retry_at=0))

    assert [record.levelno for record in caplog.records] == [logging.WARNING, logging.ERROR]
    assert caplog.records[-1].getMessage() == (
        "post delivery deferred source=dcinside gallery=cats post_id=42 attempts=5 retry_in=960s"
    )
