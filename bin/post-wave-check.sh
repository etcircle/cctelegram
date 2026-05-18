#!/usr/bin/env bash
# Wave-by-wave health diff for the architecture deepening campaign.
# Emits one page of grep-able metrics + tool status so the user can
# answer the reassessment-gate questions mechanically.
#
# Usage: bin/post-wave-check.sh
# Exit 0 on success regardless of green/red — this is a report tool,
# not a gate.

set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

hr() { printf '%s\n' "────────────────────────────────────────────────────────────"; }

echo "post-wave-check.sh — $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "branch: $(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo 'unknown')"
echo "head:   $(git rev-parse --short HEAD 2>/dev/null || echo 'unknown')"
hr

echo "── LoC metrics ──"
printf "bot.py LoC                : %d\n" "$(wc -l < src/cctelegram/bot.py)"
cb_start=$(grep -n '^async def callback_handler(' src/cctelegram/bot.py | cut -d: -f1)
if [ -n "$cb_start" ]; then
    cb_end=$(awk -v s="$cb_start" 'NR>s && /^(async )?def [a-zA-Z_]/ { print NR; exit }' src/cctelegram/bot.py)
    [ -z "$cb_end" ] && cb_end=$(wc -l < src/cctelegram/bot.py)
    printf "callback_handler LoC      : %d\n" "$((cb_end - cb_start))"
else
    printf "callback_handler LoC      : n/a\n"
fi
printf "message_queue.py LoC      : %d\n" "$(wc -l < src/cctelegram/handlers/message_queue.py)"
printf "busy_indicator.py LoC     : %d\n" "$(wc -l < src/cctelegram/handlers/busy_indicator.py)"
printf "status_polling.py LoC     : %d\n" "$(wc -l < src/cctelegram/handlers/status_polling.py)"
printf "interactive_ui.py LoC     : %d\n" "$(wc -l < src/cctelegram/handlers/interactive_ui.py)"
hr

echo "── Test brittleness signals ──"
mp_handler=$(grep -rE 'monkeypatch\.setattr\((handlers|cctelegram\.handlers)' tests/ 2>/dev/null \
            | grep -E '"_[a-z]|, *_[a-z]' | wc -l | tr -d ' ')
mp_internal=$(grep -rE 'monkeypatch\.setattr\([a-z_.]+, *"_[a-z]' tests/ 2>/dev/null | wc -l | tr -d ' ')
patch_internal=$(grep -rE 'patch(\.object)?\([^,]+, *"_[a-z]' tests/ 2>/dev/null | wc -l | tr -d ' ')
printf "monkeypatch handlers.*    : %s\n" "$mp_handler"
printf "monkeypatch _private      : %s\n" "$mp_internal"
printf "patch.object _private     : %s\n" "$patch_internal"
hr

echo "── Architecture signals ──"
state_cbs=$(grep -rcE 'register_state_callback\(' src/cctelegram/ 2>/dev/null \
            | awk -F: '{s+=$2} END {print s}')
activity_cbs=$(grep -rcE 'register_activity_callback\(' src/cctelegram/ 2>/dev/null \
              | awk -F: '{s+=$2} END {print s}')
async_locks=$(grep -rE 'asyncio\.Lock\(\)' src/cctelegram/ 2>/dev/null | wc -l | tr -d ' ')
reset_seams=$(grep -rE '^def reset_for_tests' src/cctelegram/ 2>/dev/null | wc -l | tr -d ' ')
printf "register_state_callback   : %s\n" "${state_cbs:-0}"
printf "register_activity_callback: %s\n" "${activity_cbs:-0}"
printf "asyncio.Lock() in src/    : %s\n" "$async_locks"
printf "reset_for_tests seams     : %s\n" "$reset_seams"
hr

echo "── Scenario tests ──"
if compgen -G "tests/scenarios/test_*.py" > /dev/null; then
    scen_files=$(ls tests/scenarios/test_*.py | wc -l | tr -d ' ')
    scen_cases=$(grep -hE '^(async )?def test_' tests/scenarios/test_*.py | wc -l | tr -d ' ')
    printf "scenario test files       : %s\n" "$scen_files"
    printf "scenario test cases       : %s\n" "$scen_cases"
else
    printf "scenario test files       : 0 (none yet)\n"
fi
hr

echo "── Tool status ──"
echo "[ruff check]"
if uv run ruff check src/ tests/ > /tmp/wave-check-ruff.log 2>&1; then
    echo "PASS"
else
    echo "FAIL ($(wc -l < /tmp/wave-check-ruff.log) lines of output, see /tmp/wave-check-ruff.log)"
fi

echo "[ruff format --check]"
if uv run ruff format --check src/ tests/ > /tmp/wave-check-fmt.log 2>&1; then
    echo "PASS"
else
    echo "FAIL (see /tmp/wave-check-fmt.log)"
fi

echo "[pyright]"
if uv run pyright src/cctelegram/ > /tmp/wave-check-pyright.log 2>&1; then
    echo "PASS"
else
    pyright_errors=$(grep -cE '^.* error: ' /tmp/wave-check-pyright.log 2>/dev/null || echo 0)
    echo "FAIL ($pyright_errors errors, see /tmp/wave-check-pyright.log)"
fi

echo "[pytest]"
if uv run pytest --tb=no -q > /tmp/wave-check-pytest.log 2>&1; then
    summary=$(tail -3 /tmp/wave-check-pytest.log | grep -E 'passed|failed|error' | head -1)
    echo "PASS — $summary"
else
    summary=$(tail -3 /tmp/wave-check-pytest.log | grep -E 'passed|failed|error' | head -1)
    echo "FAIL — $summary (see /tmp/wave-check-pytest.log)"
fi

echo "[pytest -m scenario]"
if uv run pytest -m scenario --tb=no -q > /tmp/wave-check-scenario.log 2>&1; then
    summary=$(tail -3 /tmp/wave-check-scenario.log | grep -E 'passed|failed|error' | head -1)
    echo "PASS — $summary"
else
    summary=$(tail -3 /tmp/wave-check-scenario.log | grep -E 'passed|failed|error' | head -1)
    echo "FAIL — $summary (see /tmp/wave-check-scenario.log)"
fi
hr

echo "done."
