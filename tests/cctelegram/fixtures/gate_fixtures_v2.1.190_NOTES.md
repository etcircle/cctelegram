# Interactive approval-gate fixtures — Wave 0 capture notes

**Captured:** 2026-06-24, Claude Code **v2.1.190**, isolated rig (`tmux gatecap`,
80×24, non-bypass `claude`, `CC_TELEGRAM_DIR=/tmp/gatecap-cfg`). PII trimmed
(`em.tanev@gmail.com` → `user@example.com`).

These are the REAL TUI captures that gate the interactive-approval-gate feature
(plan `temp/2026-06-24-interactive-approval-gate-plan-v4.md`). Wave 0 falsified
several of plan v4 §1's assumed regexes — read the **Plan corrections** below
BEFORE writing the parsers.

## Fixtures

| File | Gate | Shape highlights |
|---|---|---|
| `permission_webfetch_v2.1.190.txt` | A / WebFetch | top `Claude wants to fetch content from <host>` + `Do you want to allow Claude to fetch this content?`; opt 3 = `No, and tell Claude what to do differently (esc)` — **inline `(esc)`, NO separate footer** |
| `permission_webfetch_advance_v2.1.190.txt` | A / advance | post-Enter resolved frame (picker GONE, `Fetch(...) ⎿ Fetching…`) — PR-2 `_classify_advance` |
| `permission_bash_v2.1.190.txt` | A / Bash | top `Bash command` + `<cmd>` + `<desc>` + `Do you want to proceed?`; opts `1. Yes / 2. Yes, and always allow access to <dir>/ from this project / 3. No`; **footer `Esc to cancel · Tab to amend · ctrl+e to explain`** |
| `permission_write_long_v2.1.190.txt` | A / Write (long preview) | top `Do you want to create <file>?`; opts `1. Yes / 2. Yes, allow all edits during this session (shift+tab) / 3. No`; **footer `Esc to cancel · Tab to amend`**; 45-line preview ABOVE the question (see below) |
| `permission_write_long_visible_v2.1.190.txt` | A / Write visible | the visible 24-line slice — question+options+footer are at the bottom; proves the detection anchors stay on-screen |
| `workflow_dynamic_launch_v2.1.190.txt` | B / Workflow | top `Run a dynamic workflow?` + `<desc>` + `This dynamic workflow will spin up multiple subagents across the following phases:` + phase list + `Dynamic workflows can use a lot of tokens quickly…`; opts `1. Yes, run it / 2. View raw script / 3. No`; **footer `Esc to cancel · Tab to amend` + `ctrl+g to edit script in $EDITOR`** |
| `workflow_dynamic_launch_visible_v2.1.190.txt` | B / Workflow visible | visible slice |
| `epm_v2170_ctrl_plus_g.txt` (pre-existing) | EPM collision | reuse for the EPM↔Workflow near-miss RED test (shared `Esc`/`ctrl+g` footer family) |

## Plan corrections (Wave 0 falsified plan v4 §1)

1. **Top verb varies — `allow|proceed|make` is INCOMPLETE.** Real verbs seen:
   `allow` (WebFetch), `proceed` (Bash), `create` (Write). Plan v4 §142's
   `Do you want to (allow|proceed|make)\b` would MISS the Write gate. Broaden the
   verb set (`allow|proceed|make|create|run|read|edit|write|fetch|search|…`) OR
   match `^\s*Do you want to \w+` and lean on the co-occurring option block +
   footer for specificity. Keep the `Claude wants to ` alternative top.

2. **Bottom anchor varies — `(esc)`-tailed option is NOT universal.** WebFetch
   carries the `(esc)` INLINE on option 3 and has NO separate footer line.
   Bash/Write carry a bare `3. No` and a SEPARATE footer
   `Esc to cancel · Tab to amend [· ctrl+e to explain]`. Plan v4 §143's
   `(esc)`-only bottom anchor would DETECT WebFetch but MISS Bash/Write. The
   Permission bottom anchor must accept **either** the inline-`(esc)` option line
   **or** the `Esc to cancel · Tab to amend` footer.

3. **Footer family collision (real).** Bash/Write footer `Esc to cancel · Tab to
   amend` overlaps the Workflow footer `Esc to cancel · Tab to amend` /
   `ctrl+g to edit script` AND EPM's `Esc to (cancel|exit)` / `ctrl+g`.
   Disambiguate on the TOP anchor only: Bash=`Bash command`/`Do you want to
   proceed?`; Write=`Do you want to create`; Workflow=`Run a dynamic workflow?`/
   `This dynamic workflow will`; EPM=`Claude has written up a plan`/`Would you
   like to proceed?`. Also `ctrl+e to explain` (Bash) vs `ctrl+g to edit script`
   (Workflow) is a secondary discriminator. **RED-test all directions.**

4. **§1.1 visible-pane liveness anchor is likely UNNECESSARY for these shapes.**
   Claude Code redraws gates IN PLACE — `tmux capture-pane -S -500` returns only
   the visible 24 lines (no scrollback), and the `Do you want to…?` question is
   ALWAYS adjacent to the options at the visible bottom (only the file/content
   PREVIEW scrolls off, above the question). So the "top anchor scrolled off,
   only options remain" scenario plan v4 §1.1/P1-2 worried about did NOT
   reproduce. The existing detection (question+options+footer all visible)
   suffices; Bash/Write `Esc to cancel` is already in `_PICKER_ANCHOR_MARKERS`.
   **Recommendation:** drop the new `_PICKER_ANCHOR_MARKERS` permission anchor
   from PR-1 unless a real long-preview capture later shows the question itself
   scrolling off. (Verify against the bot's actual `capture_pane` scrollback
   setting before finalizing.)

5. **Gate B cleaner top anchor:** `Run a dynamic workflow?` (plan v4 only listed
   `This dynamic workflow will` / `Dynamic workflows can use`). All three are
   present; `Run a dynamic workflow?` is the tightest.

## Mechanical questions (plan v4 Open-Q2/Q3)

- **Commit keystroke = Enter** (Open-Q3). On WebFetch, Enter on the ❯-selected
  `1. Yes` RESOLVED the picker and the tool proceeded. The v2.1.168
  navigate-verify-**Enter** model holds for the Permission widget at v2.1.190.
  *Still to confirm per-widget in PR-2 from the Bash/Write/Workflow advance
  frames (capture pending).*
- **Decline = Esc → structured, picker gone, returns to prompt** (Open-Q2),
  confirmed for **Bash** (probe.txt NOT created → tool denied), **Write**
  (poem.txt NOT created), **Workflow** (`⎿ Dynamic workflow cancelled`). NOT
  free-text, NOT whole-turn cancel. **EXCEPTION to verify:** WebFetch option 3 is
  `No, and tell Claude what to do differently (esc)` — the "tell Claude what to
  do differently" wording suggests Esc there MAY open a free-text follow-up. Must
  capture the WebFetch decline frame before PR-2 commits the WebFetch decline
  path (S-4 per-variant).

## Still to capture (PR-2 gates / nice-to-have — do NOT block PR-1 detection)

- post-select **advance frame** for Bash / Write / Workflow (Enter-commit proof
  per widget) — PR-2.
- **View raw script** (Gate B opt 2) post-tap frame — prove non-resolving
  (picker stays) → never a button — PR-2.
- **WebFetch decline** frame (free-text vs structured) — PR-2.
- **WebSearch** + a **2-option** permission variant — extra Gate-A detection
  coverage.
- **Workflow Bash-approval** gate — PR-3 family member.

Rig left running (`tmux gatecap`) for these. Read-only Bash commands (`ls`,
`cat`) AUTO-ALLOW even without bypass; only side-effecting commands gate.
