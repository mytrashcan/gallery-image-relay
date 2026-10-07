"""Per-post retry scheduling so one undeliverable post cannot block newer ones.

A failed post is never acknowledged: its successful destination receipts stay in
the archive and it is retried after an exponential backoff. While it waits, the
crawler selects the next eligible post instead of re-selecting the same one.
State is process-local; a restart retries pending posts immediately.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

DEFAULT_BASE_DELAY = 60.0
DEFAULT_MAX_DELAY = 30 * 60.0
# Consecutive failures after which the deferral is logged as an error for alerting.
ALERT_AFTER_ATTEMPTS = 5


@dataclass(frozen=True, slots=True)
class RetryState:
    attempts: int
    delay: float
    retry_at: float


class PostRetrySchedule:
    def __init__(
        self,
        *,
        base_delay: float = DEFAULT_BASE_DELAY,
        max_delay: float = DEFAULT_MAX_DELAY,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if base_delay <= 0 or max_delay < base_delay:
            raise ValueError("retry delays must satisfy 0 < base_delay <= max_delay")
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._clock = clock
        self._states: dict[str, RetryState] = {}

    def is_due(self, post_id: str) -> bool:
        state = self._states.get(post_id)
        return state is None or self._clock() >= state.retry_at

    def record_failure(self, post_id: str) -> RetryState:
        previous = self._states.get(post_id)
        attempts = (previous.attempts if previous else 0) + 1
        # Cap the exponent: the delay saturates long before, and 2 ** 1024 overflows a float.
        delay = min(self._max_delay, self._base_delay * 2 ** min(attempts - 1, 32))
        state = RetryState(attempts=attempts, delay=delay, retry_at=self._clock() + delay)
        self._states[post_id] = state
        return state

    def clear(self, post_id: str) -> None:
        self._states.pop(post_id, None)

    def retain(self, post_ids: Iterable[str]) -> None:
        """Forget posts no longer visible, keeping the state bounded by one list page."""
        visible = set(post_ids)
        for post_id in [key for key in self._states if key not in visible]:
            del self._states[post_id]

    def __len__(self) -> int:
        return len(self._states)


def log_deferred_post(source: str, gallery_name: str, post_id: str, state: RetryState) -> None:
    """Report a deferred post with identifiers only (no titles, URLs or filenames)."""
    log = logger.error if state.attempts >= ALERT_AFTER_ATTEMPTS else logger.warning
    log(
        "post delivery deferred source=%s gallery=%s post_id=%s attempts=%d retry_in=%ds",
        source, gallery_name, post_id, state.attempts, round(state.delay),
    )
