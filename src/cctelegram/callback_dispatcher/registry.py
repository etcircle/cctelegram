"""Declare the callback dispatcher command registry.

Core responsibilities:
  - Map callback-data prefixes to command classes, builders, executors, and scenarios.
  - Give tests one table to verify routing completeness.
  - Keep command-family ownership visible during callback extraction.

Key components:
  - CallbackRegistryEntry
  - CALLBACK_REGISTRY
  - lookup()
"""

from dataclasses import dataclass

from cctelegram.handlers.callback_data import (
    CB_ASK_DOWN,
    CB_ASK_ENTER,
    CB_ASK_ESC,
    CB_ASK_LEFT,
    CB_ASK_PICK,
    CB_ASK_REFRESH,
    CB_ASK_RIGHT,
    CB_ASK_SPACE,
    CB_ASK_TAB,
    CB_ASK_UP,
    CB_DIR_BIND_EXISTING,
    CB_DIR_CANCEL,
    CB_DIR_CONFIRM,
    CB_DIR_PAGE,
    CB_DIR_SELECT,
    CB_DIR_UP,
    CB_EFFORT,
    CB_HISTORY_NEXT,
    CB_HISTORY_PREV,
    CB_KEYS_PREFIX,
    CB_SCREENSHOT_REFRESH,
    CB_SESSION_CANCEL,
    CB_SESSION_NEW,
    CB_SESSION_SELECT,
    CB_WIN_BIND,
    CB_WIN_CANCEL,
    CB_WIN_NEW,
)


@dataclass(frozen=True)
class CallbackRegistryEntry:
    """One callback prefix and its documented owner paths."""

    prefix: str
    command_class: str
    builder_function_path: str
    executor_function_path: str
    scenario_test_path: str
    executor_name: str


CALLBACK_REGISTRY: tuple[CallbackRegistryEntry, ...] = (
    CallbackRegistryEntry(
        CB_HISTORY_PREV,
        "HistoryPageCommand",
        "cctelegram.handlers.history",
        "cctelegram.callback_dispatcher.history",
        "tests/scenarios",
        "execute_history_callback",
    ),
    CallbackRegistryEntry(
        CB_HISTORY_NEXT,
        "HistoryPageCommand",
        "cctelegram.handlers.history",
        "cctelegram.callback_dispatcher.history",
        "tests/scenarios",
        "execute_history_callback",
    ),
    CallbackRegistryEntry(
        CB_DIR_SELECT,
        "DirectorySelectCommand",
        "cctelegram.handlers.directory_browser",
        "cctelegram.callback_dispatcher.directory",
        "tests/scenarios/test_unbound_topic_first_message.py",
        "execute_directory_callback",
    ),
    CallbackRegistryEntry(
        CB_DIR_UP,
        "DirectoryUpCommand",
        "cctelegram.handlers.directory_browser",
        "cctelegram.callback_dispatcher.directory",
        "tests/scenarios/test_unbound_topic_first_message.py",
        "execute_directory_callback",
    ),
    CallbackRegistryEntry(
        CB_DIR_CONFIRM,
        "DirectoryConfirmCommand",
        "cctelegram.handlers.directory_browser",
        "cctelegram.callback_dispatcher.directory",
        "tests/scenarios/test_unbound_topic_first_message.py",
        "execute_directory_callback",
    ),
    CallbackRegistryEntry(
        CB_DIR_CANCEL,
        "DirectoryCancelCommand",
        "cctelegram.handlers.directory_browser",
        "cctelegram.callback_dispatcher.directory",
        "tests/scenarios/test_unbound_topic_first_message.py",
        "execute_directory_callback",
    ),
    CallbackRegistryEntry(
        CB_DIR_PAGE,
        "DirectoryPageCommand",
        "cctelegram.handlers.directory_browser",
        "cctelegram.callback_dispatcher.directory",
        "tests/scenarios/test_unbound_topic_first_message.py",
        "execute_directory_callback",
    ),
    CallbackRegistryEntry(
        CB_DIR_BIND_EXISTING,
        "DirectoryBindExistingCommand",
        "cctelegram.handlers.directory_browser",
        "cctelegram.callback_dispatcher.directory",
        "tests/scenarios/test_unbound_topic_first_message.py",
        "execute_directory_callback",
    ),
    CallbackRegistryEntry(
        CB_WIN_BIND,
        "WindowBindCommand",
        "cctelegram.handlers.directory_browser",
        "cctelegram.callback_dispatcher.directory",
        "tests/scenarios/test_unbound_topic_first_message.py",
        "execute_directory_callback",
    ),
    CallbackRegistryEntry(
        CB_WIN_NEW,
        "WindowNewCommand",
        "cctelegram.handlers.directory_browser",
        "cctelegram.callback_dispatcher.directory",
        "tests/scenarios/test_unbound_topic_first_message.py",
        "execute_directory_callback",
    ),
    CallbackRegistryEntry(
        CB_WIN_CANCEL,
        "WindowCancelCommand",
        "cctelegram.handlers.directory_browser",
        "cctelegram.callback_dispatcher.directory",
        "tests/scenarios/test_unbound_topic_first_message.py",
        "execute_directory_callback",
    ),
    CallbackRegistryEntry(
        CB_SCREENSHOT_REFRESH,
        "ScreenshotRefreshCommand",
        "cctelegram.callback_dispatcher.screenshot.build_screenshot_keyboard",
        "cctelegram.callback_dispatcher.screenshot",
        "tests/scenarios/test_screenshot_stale_window.py",
        "execute_screenshot_callback",
    ),
    CallbackRegistryEntry(
        CB_KEYS_PREFIX,
        "ScreenshotKeyCommand",
        "cctelegram.callback_dispatcher.screenshot.build_screenshot_keyboard",
        "cctelegram.callback_dispatcher.bash",
        "tests/scenarios/test_screenshot_stale_window.py",
        "execute_bash_callback",
    ),
    CallbackRegistryEntry(
        CB_ASK_UP,
        "InteractiveNavCommand",
        "cctelegram.handlers.interactive_ui",
        "cctelegram.callback_dispatcher.interactive",
        "tests/scenarios/test_interactive_prompt_safety.py",
        "execute_interactive_callback",
    ),
    CallbackRegistryEntry(
        CB_ASK_DOWN,
        "InteractiveNavCommand",
        "cctelegram.handlers.interactive_ui",
        "cctelegram.callback_dispatcher.interactive",
        "tests/scenarios/test_interactive_prompt_safety.py",
        "execute_interactive_callback",
    ),
    CallbackRegistryEntry(
        CB_ASK_LEFT,
        "InteractiveNavCommand",
        "cctelegram.handlers.interactive_ui",
        "cctelegram.callback_dispatcher.interactive",
        "tests/scenarios/test_interactive_prompt_safety.py",
        "execute_interactive_callback",
    ),
    CallbackRegistryEntry(
        CB_ASK_RIGHT,
        "InteractiveNavCommand",
        "cctelegram.handlers.interactive_ui",
        "cctelegram.callback_dispatcher.interactive",
        "tests/scenarios/test_interactive_prompt_safety.py",
        "execute_interactive_callback",
    ),
    CallbackRegistryEntry(
        CB_ASK_ESC,
        "InteractiveEscCommand",
        "cctelegram.handlers.interactive_ui",
        "cctelegram.callback_dispatcher.interactive",
        "tests/scenarios/test_interactive_prompt_safety.py",
        "execute_interactive_callback",
    ),
    CallbackRegistryEntry(
        CB_ASK_ENTER,
        "InteractiveNavCommand",
        "cctelegram.handlers.interactive_ui",
        "cctelegram.callback_dispatcher.interactive",
        "tests/scenarios/test_interactive_prompt_safety.py",
        "execute_interactive_callback",
    ),
    CallbackRegistryEntry(
        CB_ASK_SPACE,
        "InteractiveNavCommand",
        "cctelegram.handlers.interactive_ui",
        "cctelegram.callback_dispatcher.interactive",
        "tests/scenarios/test_interactive_prompt_safety.py",
        "execute_interactive_callback",
    ),
    CallbackRegistryEntry(
        CB_ASK_TAB,
        "InteractiveNavCommand",
        "cctelegram.handlers.interactive_ui",
        "cctelegram.callback_dispatcher.interactive",
        "tests/scenarios/test_interactive_prompt_safety.py",
        "execute_interactive_callback",
    ),
    CallbackRegistryEntry(
        CB_ASK_REFRESH,
        "InteractiveRefreshCommand",
        "cctelegram.handlers.interactive_ui",
        "cctelegram.callback_dispatcher.interactive",
        "tests/scenarios/test_interactive_prompt_safety.py",
        "execute_interactive_callback",
    ),
    CallbackRegistryEntry(
        CB_ASK_PICK,
        "InteractivePickCommand",
        "cctelegram.handlers.interactive_ui",
        "cctelegram.callback_dispatcher.interactive",
        "tests/scenarios/test_interactive_prompt_safety.py",
        "execute_interactive_callback",
    ),
    CallbackRegistryEntry(
        CB_SESSION_SELECT,
        "SessionSelectCommand",
        "cctelegram.handlers.directory_browser",
        "cctelegram.callback_dispatcher.directory",
        "tests/scenarios/test_unbound_topic_first_message.py",
        "execute_directory_callback",
    ),
    CallbackRegistryEntry(
        CB_SESSION_NEW,
        "SessionNewCommand",
        "cctelegram.handlers.directory_browser",
        "cctelegram.callback_dispatcher.directory",
        "tests/scenarios/test_unbound_topic_first_message.py",
        "execute_directory_callback",
    ),
    CallbackRegistryEntry(
        CB_SESSION_CANCEL,
        "SessionCancelCommand",
        "cctelegram.handlers.directory_browser",
        "cctelegram.callback_dispatcher.directory",
        "tests/scenarios/test_unbound_topic_first_message.py",
        "execute_directory_callback",
    ),
    CallbackRegistryEntry(
        CB_EFFORT,
        "EffortCommand",
        "cctelegram.callback_dispatcher.effort.build_effort_keyboard",
        "cctelegram.callback_dispatcher.effort",
        "tests/scenarios",
        "execute_effort_callback",
    ),
)


def lookup(data: str) -> CallbackRegistryEntry | None:
    """Return the longest-prefix registry entry matching callback data."""
    for entry in sorted(
        CALLBACK_REGISTRY, key=lambda item: len(item.prefix), reverse=True
    ):
        if data.startswith(entry.prefix):
            return entry
    return None
