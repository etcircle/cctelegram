"""Callback dispatcher registry completeness tests.

Ensures every callback-data prefix is consciously owned by the dispatcher
registry or documented as a builder-only sentinel.
"""

import importlib

from cctelegram.callback_dispatcher.registry import CALLBACK_REGISTRY
from cctelegram.handlers import callback_data


# Non-action callback sentinels that builders may emit but the dispatcher should
# not route through a command executor.
BUILDER_ONLY_CALLBACKS: set[str] = set()


def test_every_callback_constant_is_registered_or_allowlisted() -> None:
    cb_constants = {
        name: value
        for name, value in vars(callback_data).items()
        if name.startswith("CB_") and isinstance(value, str)
    }
    registered_prefixes = {entry.prefix for entry in CALLBACK_REGISTRY}

    missing = {
        name: value
        for name, value in cb_constants.items()
        if name not in BUILDER_ONLY_CALLBACKS and value not in registered_prefixes
    }

    assert missing == {}


def test_registry_rule_would_catch_new_unmapped_constant() -> None:
    registered_prefixes = {entry.prefix for entry in CALLBACK_REGISTRY}
    fake_constants = {"CB_TEST": "test:"}

    missing = {
        name: value
        for name, value in fake_constants.items()
        if name not in BUILDER_ONLY_CALLBACKS and value not in registered_prefixes
    }

    assert missing == {"CB_TEST": "test:"}


def test_every_registry_entry_executor_is_importable_and_callable() -> None:
    """Each registry row must point at a real, callable executor."""
    for entry in CALLBACK_REGISTRY:
        module = importlib.import_module(entry.executor_function_path)
        executor = getattr(module, entry.executor_name, None)
        assert executor is not None, (
            f"Registry entry for {entry.prefix!r} points at "
            f"{entry.executor_function_path}.{entry.executor_name} "
            f"which is not defined."
        )
        assert callable(executor), (
            f"Registry entry for {entry.prefix!r} points at "
            f"{entry.executor_function_path}.{entry.executor_name} "
            f"which is not callable."
        )
