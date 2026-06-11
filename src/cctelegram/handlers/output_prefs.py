"""Per-user output verbosity resolution — the single authority for what renders.

Resolves a frozen ``OutputPrefs`` snapshot per recipient with the layering
"stored user override > explicit env default > preset" (plan v4 §4): the
preset table supplies every knob, legacy ``CC_TELEGRAM_SHOW_*`` env vars act
as knob-precise defaults only when EXPLICITLY set (never ceilings — a stored
override can re-enable what an env default suppressed), and per-user values
persisted in SessionManager's ``user_settings`` win over both. Consulted at
every Telegram emission point: the bot fan-out gates, the activity /
sub-agent / todo digest renderers, Agent prominence, the context footer, and
/history's user-echo filter.

Key components:
  - OutputPrefs (frozen dataclass — one resolved snapshot)
  - PRESETS / DEFAULT_PRESET (the §5 matrix; ``verbose`` ≡ pre-settings
    behavior)
  - resolve(user_id) — the only read path
  - KNOB_CHOICES — the /settings panel's tappable subset

Leaf rules: imports config + session only; stateless (no caches, no reset
seam — SessionManager owns the persisted state).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from ..config import config
from ..session import session_manager

# Policy enums (string-valued for state.json round-trip + callback tokens).
DIGEST_ON_DONE_KEEP = "keep"
DIGEST_ON_DONE_SUMMARY = "summary"
DIGEST_ON_DONE_DELETE = "delete"
SUBAGENT_CARDS_KEEP = "keep"
SUBAGENT_CARDS_SUMMARY = "summary"
SUBAGENT_CARDS_OFF = "off"


@dataclass(frozen=True)
class OutputPrefs:
    """Resolved per-recipient output preferences (one immutable snapshot)."""

    verbosity: str
    # Activity digest (the per-turn "🟡 Busy" card)
    digest_card: bool  # False ⇒ no digest card at all (quiet)
    digest_live_lines: int
    digest_line_chars: int
    result_snippet_chars: int
    digest_on_done: str  # keep | summary | delete (W1 — wired in PR-2)
    thinking_line: bool
    # Legacy CC_TELEGRAM_SHOW_TOOL_CALLS=false mapping: drop ALL
    # tool_use/tool_result surfaces for this recipient, including Agent/Task
    # (the faithful pre-settings behavior). No preset sets this False.
    tool_activity: bool
    # Sub-agent (sidechain) play-by-play cards
    subagent_cards: str  # keep | summary | off ("summary" collapse — PR-2)
    subagent_live_lines: int
    # Agent prominence (the 🤖 dispatched / 🤖✅ done surface)
    agent_dispatch_msg: bool
    agent_prompt_preview_chars: int
    # Other surfaces
    user_echo: bool
    todo_card: bool
    context_footer: bool


# The §5 preset matrix. ``verbose`` mirrors today's module constants
# (message_queue ACTIVITY_DIGEST_* / SUBAGENT_DIGEST_*) so the PR-1 default
# is behavior-neutral.
PRESETS: dict[str, OutputPrefs] = {
    "verbose": OutputPrefs(
        verbosity="verbose",
        digest_card=True,
        digest_live_lines=10,
        digest_line_chars=400,
        result_snippet_chars=240,
        digest_on_done=DIGEST_ON_DONE_KEEP,
        thinking_line=True,
        tool_activity=True,
        subagent_cards=SUBAGENT_CARDS_KEEP,
        subagent_live_lines=12,
        agent_dispatch_msg=True,
        agent_prompt_preview_chars=400,
        user_echo=True,
        todo_card=True,
        context_footer=True,
    ),
    "standard": OutputPrefs(
        verbosity="standard",
        digest_card=True,
        digest_live_lines=6,
        digest_line_chars=160,
        result_snippet_chars=96,
        digest_on_done=DIGEST_ON_DONE_SUMMARY,
        thinking_line=True,
        tool_activity=True,
        subagent_cards=SUBAGENT_CARDS_SUMMARY,
        subagent_live_lines=6,
        agent_dispatch_msg=True,
        agent_prompt_preview_chars=400,
        user_echo=False,
        todo_card=True,
        context_footer=True,
    ),
    "compact": OutputPrefs(
        verbosity="compact",
        digest_card=True,
        digest_live_lines=0,
        digest_line_chars=160,
        result_snippet_chars=96,
        digest_on_done=DIGEST_ON_DONE_SUMMARY,
        thinking_line=False,
        tool_activity=True,
        subagent_cards=SUBAGENT_CARDS_OFF,
        subagent_live_lines=0,
        agent_dispatch_msg=True,
        agent_prompt_preview_chars=120,
        user_echo=False,
        todo_card=True,
        context_footer=True,
    ),
    "quiet": OutputPrefs(
        verbosity="quiet",
        digest_card=False,
        digest_live_lines=0,
        digest_line_chars=160,
        result_snippet_chars=96,
        digest_on_done=DIGEST_ON_DONE_DELETE,
        thinking_line=False,
        tool_activity=True,
        subagent_cards=SUBAGENT_CARDS_OFF,
        subagent_live_lines=0,
        agent_dispatch_msg=False,
        agent_prompt_preview_chars=120,
        user_echo=False,
        todo_card=False,
        context_footer=True,
    ),
}

PRESET_NAMES: tuple[str, ...] = ("verbose", "standard", "compact", "quiet")
# The fallback preset for invalid stored/env values — and, via config's
# CC_TELEGRAM_VERBOSITY default, the preset a fresh user resolves to.
# Flipped to "standard" in PR-2 (plan v4 §8 decision 1).
DEFAULT_PRESET = "standard"

# Per-knob overrides the /settings panel exposes (validated on read AND at
# the callback seam). Values are the callback-token → stored-value mapping.
KNOB_CHOICES: dict[str, dict[str, Any]] = {
    "lines": {"64": 64, "160": 160, "400": 400},  # digest_line_chars
    "echo": {"on": True, "off": False},  # user_echo
    "footer": {"on": True, "off": False},  # context_footer
    # W1/W2 collapse policies (PR-2; deferred from PR-1 so the controls
    # never shipped before their mechanics — codex PR-1 review P2-1).
    "done": {
        "keep": DIGEST_ON_DONE_KEEP,
        "summary": DIGEST_ON_DONE_SUMMARY,
        "delete": DIGEST_ON_DONE_DELETE,
    },
    "subcards": {
        "keep": SUBAGENT_CARDS_KEEP,
        "summary": SUBAGENT_CARDS_SUMMARY,
        "off": SUBAGENT_CARDS_OFF,
    },
}
_KNOB_FIELDS: dict[str, str] = {
    "lines": "digest_line_chars",
    "echo": "user_echo",
    "footer": "context_footer",
    "done": "digest_on_done",
    "subcards": "subagent_cards",
}


def _env_layer(base: OutputPrefs) -> OutputPrefs:
    """Apply EXPLICITLY-set legacy env vars as knob defaults over the preset.

    Defaults, not ceilings: a stored per-user override (applied after this
    layer in ``resolve``) wins. ``CC_TELEGRAM_SHOW_TOOL_CALLS=false`` maps to
    the faithful full suppression (``tool_activity`` + sub-agent cards off,
    Agent surfaces included — plan v4 §4); ``CC_TELEGRAM_SHOW_USER_MESSAGES``
    and the footer/preview vars map one-to-one.
    """
    out = base
    if config.env_show_tool_calls_set and not config.show_tool_calls:
        out = replace(out, tool_activity=False, subagent_cards=SUBAGENT_CARDS_OFF)
    if config.env_show_user_messages_set:
        out = replace(out, user_echo=config.show_user_messages)
    if config.env_context_footer_set:
        out = replace(out, context_footer=config.context_in_message_footer)
    if config.env_agent_preview_set:
        out = replace(out, agent_prompt_preview_chars=config.agent_prompt_preview_chars)
    return out


def _override_layer(base: OutputPrefs, stored: dict[str, Any]) -> OutputPrefs:
    """Apply validated per-user knob overrides; junk values are ignored."""
    out = base
    for knob, field_name in _KNOB_FIELDS.items():
        if knob not in stored:
            continue
        choices = KNOB_CHOICES[knob]
        raw = stored[knob]
        for value in choices.values():
            # Type-strict (codex PR-1 review P2-2): Python's `1 == True` /
            # `0 == False` would let malformed stored JSON pass as a bool
            # knob — junk must stay inert, not coerce.
            if type(raw) is type(value) and raw == value:
                out = replace(out, **{field_name: value})
                break
    return out


def resolve(user_id: int) -> OutputPrefs:
    """Resolve the recipient's output preferences (the only read path).

    Stateless and cheap (two dict lookups + dataclass copies) — call at each
    emission point rather than caching, so a /settings tap applies from the
    next rendered surface onward.

    Layering: a STORED preset choice is a user override of the entire legacy
    env-default layer (hermes PR-1 review P1) — the env translation exists
    to map ``CC_TELEGRAM_SHOW_*`` for users who never touched ``/settings``;
    once a user picks a preset, their choice fully defines the baseline and
    an explicit ``SHOW_TOOL_CALLS=false`` can no longer act as a ceiling.
    Stored per-knob overrides (without a preset choice) still win over the
    env layer per-knob via ``_override_layer``.
    """
    stored = session_manager.get_user_settings(user_id)
    # isinstance guard before the membership test: stored values are only
    # shape-validated on load, so unhashable junk like {"verbosity": []}
    # must fall through to the default, not raise (dual r2 P2).
    stored_verbosity = stored.get("verbosity")
    if isinstance(stored_verbosity, str) and stored_verbosity in PRESETS:
        preset_name: str = stored_verbosity
        user_chose_preset = True
    else:
        preset_name = config.default_verbosity
        if preset_name not in PRESETS:
            preset_name = DEFAULT_PRESET
        user_chose_preset = False
    base = PRESETS[preset_name]
    layered = base if user_chose_preset else _env_layer(base)
    return _override_layer(layered, stored)
