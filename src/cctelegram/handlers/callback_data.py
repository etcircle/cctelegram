"""Callback data constants for Telegram inline keyboards.

Defines all CB_* prefixes used for routing callback queries in the bot.
Each prefix identifies a specific action or navigation target.

Constants:
  - CB_HISTORY_*: History pagination
  - CB_DIR_*: Directory browser navigation
  - CB_WIN_*: Window picker (bind existing unbound window)
  - CB_SCREENSHOT_*: Screenshot refresh
  - CB_ASK_*: Interactive UI navigation (arrows, enter, esc)
  - CB_KEYS_PREFIX: Screenshot control keys (kb:<key_id>:<window>)
"""

# History pagination
CB_HISTORY_PREV = "hp:"  # history page older
CB_HISTORY_NEXT = "hn:"  # history page newer

# Directory browser
CB_DIR_SELECT = "db:sel:"
CB_DIR_UP = "db:up"
CB_DIR_CONFIRM = "db:confirm"
CB_DIR_CANCEL = "db:cancel"
CB_DIR_PAGE = "db:page:"
CB_DIR_BIND_EXISTING = "db:bind"  # switch to window picker (opt-in)

# Window picker (bind existing unbound window)
CB_WIN_BIND = "wb:sel:"  # wb:sel:<index>
CB_WIN_NEW = "wb:new"  # proceed to directory browser
CB_WIN_CANCEL = "wb:cancel"

# Screenshot
CB_SCREENSHOT_REFRESH = "ss:ref:"

# Interactive UI (aq: prefix kept for backward compatibility)
CB_ASK_UP = "aq:up:"  # aq:up:<window>
CB_ASK_DOWN = "aq:down:"  # aq:down:<window>
CB_ASK_LEFT = "aq:left:"  # aq:left:<window>
CB_ASK_RIGHT = "aq:right:"  # aq:right:<window>
CB_ASK_ESC = "aq:esc:"  # aq:esc:<window>
CB_ASK_ENTER = "aq:enter:"  # aq:enter:<window>
CB_ASK_SPACE = "aq:spc:"  # aq:spc:<window>
CB_ASK_TAB = "aq:tab:"  # aq:tab:<window>
CB_ASK_REFRESH = "aq:ref:"  # aq:ref:<window>
# Structured option pick (PR 2b). The callback carries a short token that
# resolves server-side to the (window, fingerprint, option_number,
# option_label) bound when the keyboard was minted. Token-keyed instead of
# embedding state in the 64-byte callback_data — same shape as the
# attention-card flow in handlers/attention.py. Multi-select toggles use
# the same keyed token shape but dispatch only a bare digit and do not ledger.
CB_ASK_PICK = "aqp:"  # aqp:<route_hash>:<fp8>:<opt>:<token>
CB_ASK_TOGGLE = "aqt:"  # aqt:<route_hash>:<fp8>:<opt>:<token>
# Wave A late answer: after an AskUserQuestion ~60s AFK auto-resolve converts
# the picker into the "⏰ Claude proceeded" card, its option buttons deliver
# the choice as a NORMAL user text message (never a picker keystroke). One
# token per CARD, resolved via the in-memory handlers/late_answer registry.
CB_ASK_LATE = "aql:"  # aql:<window_id>:<opt>:<token>

# Session picker (resume existing session)
CB_SESSION_SELECT = "rs:sel:"  # rs:sel:<index>
CB_SESSION_NEW = "rs:new"  # start a new session
CB_SESSION_CANCEL = "rs:cancel"  # cancel

# Effort level picker (intercepts bare `/effort` in Telegram)
# window_id is embedded so a stale button after topic rebind is rejected.
CB_EFFORT = "eff:"  # eff:<level>:<window_id>  e.g. eff:xhigh:@28

# Per-user output verbosity panel (/settings). The owner's user_id is
# embedded so a second allowed user tapping someone else's panel is rejected
# without mutating anything (plan v4 §6). Fields/values are short enum
# tokens validated against output_prefs; namespaced "stg:" (not "set:") to
# stay collision-safe (hermes r2 P2-10).
CB_SETTINGS = (
    "stg:"  # stg:<field>:<value>:<owner_user_id>  e.g. stg:preset:compact:12345
)

# Screenshot control keys
CB_KEYS_PREFIX = "kb:"  # kb:<key_id>:<window>


def checked_callback_data(data: str) -> str:
    """Return callback data unchanged, or raise if it exceeds Telegram's limit.

    Lives in this dependency-free leaf module (not on the heavy
    ``callback_dispatcher`` package facade) so that ``interactive_ui`` and
    ``history`` can validate callback payloads without importing the
    dispatcher package — which would otherwise close the
    interactive_ui ↔ callback_dispatcher ↔ inbound_telegram import cycle.
    Mirrors the ``INTERACTIVE_TOOL_NAMES`` relocation onto ``route_runtime``.
    """
    if len(data.encode("utf-8")) > 64:
        raise RuntimeError(f"callback_data exceeds Telegram 64-byte limit: {data!r}")
    return data
