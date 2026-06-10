"""Gate P3-1: topic teardown must clear status_polling's route-local caches.

``clear_topic_state`` (topic close / delete / stale-binding GC / ``/unbind``)
tears down message_queue state, side files, interactive state, and
route_runtime state — but historically left ``status_polling``'s route-keyed
poller caches untouched. A rebound topic reusing the same route key then
inherited stale entries: a leftover ``_last_published_ui_hash`` skips the
first-picker content-drain ordering barrier, a leftover ``_prev_run_state``
defeats the seed-without-edit repaint semantics, and a stale
``_last_pane_capture`` delays the rebound's first watchdog scrape.
"""

from __future__ import annotations

import pytest

from cctelegram.handlers import cleanup, status_polling

_USER = 1
_THREAD = 42
_OTHER_THREAD = 43
_WID = "@9"

_ALL_CACHES = (
    status_polling._last_pane_capture,
    status_polling._last_published_ui_hash,
    status_polling._absent_streak,
    status_polling._prev_run_state,
)

_SEED_VALUES = (123.0, "hash", 2, object())


@pytest.fixture(autouse=True)
def _clean_caches():
    for cache in _ALL_CACHES:
        cache.clear()
    yield
    for cache in _ALL_CACHES:
        cache.clear()


@pytest.mark.asyncio
async def test_clear_topic_state_pops_all_poller_route_caches() -> None:
    route = (_USER, _THREAD, _WID)
    other = (_USER, _OTHER_THREAD, _WID)
    for cache, value in zip(_ALL_CACHES, _SEED_VALUES):
        cache[route] = value
        cache[other] = value

    await cleanup.clear_topic_state(_USER, _THREAD, None, None)

    for cache in _ALL_CACHES:
        assert route not in cache, "stale poller cache entry survived teardown"
        # Sibling topic untouched.
        assert other in cache


def test_clear_route_caches_for_topic_scopes_by_user_and_thread() -> None:
    mine = (_USER, _THREAD, _WID)
    other_user = (2, _THREAD, _WID)
    other_thread = (_USER, _OTHER_THREAD, _WID)
    for cache, value in zip(_ALL_CACHES, _SEED_VALUES):
        cache[mine] = value
        cache[other_user] = value
        cache[other_thread] = value

    status_polling.clear_route_caches_for_topic(_USER, _THREAD)

    for cache in _ALL_CACHES:
        assert mine not in cache
        assert other_user in cache
        assert other_thread in cache
