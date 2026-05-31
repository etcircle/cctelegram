"""Pinning tests for ``interactive_ui.reset_for_tests()``.

Guards two R3 decisions:

  1. The seam clears every per-test map (including ``_route_locks``) so no
     interactive state leaks into the next test.
  2. ``_clear_callbacks`` is a PROCESS-LIFETIME registry — ``status_polling``
     registers ``_on_interactive_clear`` exactly once at import. The seam must
     NOT clear it; doing so would silently disable clear-callback propagation
     for the rest of the suite (the registration never re-runs). This test
     fails loudly if a future change adds ``_clear_callbacks.clear()`` to the
     seam.
"""

from __future__ import annotations

# Importing status_polling ensures its module-level
# ``register_clear_callback(_on_interactive_clear)`` has run, so
# ``_clear_callbacks`` holds the process-lifetime registration we assert is
# preserved.
from cctelegram.handlers import interactive_ui as iu
from cctelegram.handlers import status_polling as _sp  # noqa: F401


# The pick-token store (``_pick_tokens`` / ``_pick_token_cache``) moved to
# ``pick_token`` (R4); its reset is pinned in ``test_pick_token.py``.
_PER_TEST_DICT_NAMES = (
    "_interactive_msgs",
    "_interactive_mode",
    "_interactive_msg_meta",
    "_last_completed_ask_tool_input",
    "_last_auq_tool_use_id",
    "_auq_context_posted",
    "_auq_context_post_pending",
    "_auq_context_msgs",
    "_route_locks",
)


def test_reset_clears_per_test_maps() -> None:
    """Seed every per-test map (incl. ``_route_locks``) and assert the seam
    empties them."""
    sentinel = object()
    for name in _PER_TEST_DICT_NAMES:
        getattr(iu, name)[("sentinel", name)] = sentinel
        assert getattr(iu, name), f"seed failed for {name}"

    iu.reset_for_tests()

    for name in _PER_TEST_DICT_NAMES:
        assert len(getattr(iu, name)) == 0, f"{name} not cleared by reset_for_tests()"


def test_reset_preserves_clear_callbacks_registry() -> None:
    """``_clear_callbacks`` is process-lifetime; the seam must NOT clear it.

    Pins the decision so a future ``_clear_callbacks.clear()`` in the seam —
    which would silently break clear-callback propagation for the rest of the
    suite — fails here loudly.
    """
    # status_polling's import-time registration is present.
    assert len(iu._clear_callbacks) >= 1, (
        "status_polling should have registered a clear callback at import"
    )
    before = list(iu._clear_callbacks)

    iu.reset_for_tests()

    assert iu._clear_callbacks == before, (
        "reset_for_tests() must NOT clear the process-lifetime _clear_callbacks"
    )
