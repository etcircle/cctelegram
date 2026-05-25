"""Tests for Claude Code session tracking + AUQ PreToolUse hook."""

import io
import json
import os
import stat
import sys
import time

import pytest

from cctelegram.hook import (
    _AUQ_MATCHER,
    _PRE_TOOL_USE_TIMEOUT_S,
    _SESSION_START_TIMEOUT_S,
    _UUID_RE,
    _install_hook,
    _is_pre_tool_use_installed,
    _is_session_start_installed,
    hook_main,
)


def _settings_with_session_start_commands(commands: list[str]) -> dict:
    return {
        "hooks": {
            "SessionStart": [
                {
                    "hooks": [
                        {"type": "command", "command": command, "timeout": 5}
                        for command in commands
                    ]
                }
            ]
        }
    }


def _session_start_commands(settings: dict) -> list[str]:
    return [
        hook["command"]
        for entry in settings.get("hooks", {}).get("SessionStart", [])
        if isinstance(entry, dict)
        for hook in entry.get("hooks", [])
        if isinstance(hook, dict) and isinstance(hook.get("command"), str)
    ]


def _pre_tool_use_entries(settings: dict) -> list[dict]:
    return [
        entry
        for entry in settings.get("hooks", {}).get("PreToolUse", [])
        if isinstance(entry, dict)
    ]


class TestUuidRegex:
    @pytest.mark.parametrize(
        "value",
        [
            "550e8400-e29b-41d4-a716-446655440000",
            "00000000-0000-0000-0000-000000000000",
            "abcdef01-2345-6789-abcd-ef0123456789",
        ],
        ids=["standard", "all-zeros", "all-hex"],
    )
    def test_valid_uuid_matches(self, value: str) -> None:
        assert _UUID_RE.match(value) is not None

    @pytest.mark.parametrize(
        "value",
        [
            "not-a-uuid",
            "550e8400-e29b-41d4-a716",
            "550e8400-e29b-41d4-a716-44665544000g",
            "",
        ],
        ids=["gibberish", "truncated", "invalid-hex-char", "empty"],
    )
    def test_invalid_uuid_no_match(self, value: str) -> None:
        assert _UUID_RE.match(value) is None


class TestIsSessionStartInstalled:
    def test_no_hooks_key(self) -> None:
        assert _is_session_start_installed({}) == "missing"

    def test_different_hook_command(self) -> None:
        settings = {
            "hooks": {
                "SessionStart": [
                    {"hooks": [{"type": "command", "command": "other-tool hook"}]}
                ]
            }
        }
        assert _is_session_start_installed(settings) == "missing"

    @pytest.mark.parametrize(
        "command",
        [
            "echo cc-telegram hook",
            "echo /usr/local/bin/cc-telegram hook",
            "# cc-telegram hook",
            "cc-telegram hook # comment",
            "cc-telegram hook && other-tool hook",
            "true&&/usr/local/bin/cc-telegram hook",
            "CCT=/usr/local/bin/cc-telegram hook",
            "https://example.test/cc-telegram hook",
            "sh -c 'cc-telegram hook'",
            "some-cc-telegram hook",
        ],
        ids=[
            "echo-exact",
            "echo-path-suffix",
            "comment",
            "trailing-comment",
            "shell-chain",
            "shell-chain-no-spaces",
            "assignment-prefix",
            "url-mention",
            "shell-wrapper",
            "substring-command",
        ],
    )
    def test_wrapper_mentions_are_not_classified_as_installed(
        self, command: str, tmp_path, monkeypatch
    ) -> None:
        """Wrapper / comment strings containing the hook command must not count
        as an installed hook, and ``_install_hook`` must add a fresh one
        alongside without rewriting the wrapper."""
        settings = _settings_with_session_start_commands([command])
        assert _is_session_start_installed(settings) == "missing"

        settings_file = tmp_path / "settings.json"
        settings_file.write_text(json.dumps(settings))
        monkeypatch.setattr(
            "cctelegram.hook._find_cc_telegram_path", lambda: "cc-telegram"
        )

        assert _install_hook(settings_file=settings_file) == 0
        data = json.loads(settings_file.read_text())
        # Wrapper preserved + fresh SessionStart appended.
        commands = _session_start_commands(data)
        assert commands == [command, "cc-telegram hook"]
        # PreToolUse also installed (idempotent dual-install).
        pre_entries = _pre_tool_use_entries(data)
        assert any(e.get("matcher") == _AUQ_MATCHER for e in pre_entries)

    def test_current_hook_matches(self) -> None:
        settings = {
            "hooks": {
                "SessionStart": [
                    {"hooks": [{"type": "command", "command": "cc-telegram hook"}]}
                ]
            }
        }
        assert _is_session_start_installed(settings) == "current"

    def test_path_qualified_current_hook_is_idempotent_for_session_start(
        self, tmp_path, monkeypatch
    ) -> None:
        # Pre-existing SessionStart hook + PreToolUse already current → no
        # change to either entry.
        settings = {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "/opt/bin/cc-telegram hook",
                                "timeout": 5,
                            }
                        ]
                    }
                ],
                "PreToolUse": [
                    {
                        "matcher": "AskUserQuestion",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "/opt/bin/cc-telegram hook",
                                "timeout": 2,
                            }
                        ],
                    }
                ],
            }
        }
        settings_file = tmp_path / "settings.json"
        settings_file.write_text(json.dumps(settings))
        monkeypatch.setattr(
            "cctelegram.hook._find_cc_telegram_path", lambda: "cc-telegram"
        )

        assert _install_hook(settings_file=settings_file) == 0
        data = json.loads(settings_file.read_text())
        assert _session_start_commands(data) == ["/opt/bin/cc-telegram hook"]
        pre_entries = _pre_tool_use_entries(data)
        assert len(pre_entries) == 1
        assert pre_entries[0]["matcher"] == "AskUserQuestion"


class TestIsPreToolUseInstalled:
    def test_no_pre_tool_use_key(self) -> None:
        assert _is_pre_tool_use_installed({}) == "missing"

    def test_pre_tool_use_with_wrong_matcher(self) -> None:
        settings = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [{"type": "command", "command": "cc-telegram hook"}],
                    }
                ]
            }
        }
        assert _is_pre_tool_use_installed(settings) == "missing"

    def test_pre_tool_use_with_unmanaged_command(self) -> None:
        settings = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "AskUserQuestion",
                        "hooks": [{"type": "command", "command": "other-tool hook"}],
                    }
                ]
            }
        }
        assert _is_pre_tool_use_installed(settings) == "missing"

    def test_pre_tool_use_managed_entry_is_current(self) -> None:
        settings = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "AskUserQuestion",
                        "hooks": [{"type": "command", "command": "cc-telegram hook"}],
                    }
                ]
            }
        }
        assert _is_pre_tool_use_installed(settings) == "current"


class TestInstallHookDual:
    def test_fresh_install_writes_both_events(self, tmp_path, monkeypatch) -> None:
        settings_file = tmp_path / "settings.json"
        monkeypatch.setattr(
            "cctelegram.hook._find_cc_telegram_path", lambda: "cc-telegram"
        )
        assert _install_hook(settings_file=settings_file) == 0
        data = json.loads(settings_file.read_text())
        assert _session_start_commands(data) == ["cc-telegram hook"]
        pre_entries = _pre_tool_use_entries(data)
        assert len(pre_entries) == 1
        assert pre_entries[0]["matcher"] == "AskUserQuestion"
        assert pre_entries[0]["hooks"][0]["timeout"] == _PRE_TOOL_USE_TIMEOUT_S
        # SessionStart timeout is the existing constant.
        ss_entry = data["hooks"]["SessionStart"][0]["hooks"][0]
        assert ss_entry["timeout"] == _SESSION_START_TIMEOUT_S

    def test_partial_install_only_adds_missing_event_session_current(
        self, tmp_path, monkeypatch
    ) -> None:
        # SessionStart current, PreToolUse missing → only PreToolUse added.
        settings = {
            "hooks": {
                "SessionStart": [
                    {"hooks": [{"type": "command", "command": "cc-telegram hook"}]}
                ]
            }
        }
        settings_file = tmp_path / "settings.json"
        settings_file.write_text(json.dumps(settings))
        monkeypatch.setattr(
            "cctelegram.hook._find_cc_telegram_path", lambda: "cc-telegram"
        )
        assert _install_hook(settings_file=settings_file) == 0
        data = json.loads(settings_file.read_text())
        # SessionStart NOT duplicated.
        assert _session_start_commands(data) == ["cc-telegram hook"]
        # PreToolUse newly added.
        pre_entries = _pre_tool_use_entries(data)
        assert len(pre_entries) == 1
        assert pre_entries[0]["matcher"] == "AskUserQuestion"

    def test_partial_install_only_adds_missing_event_pretool_current(
        self, tmp_path, monkeypatch
    ) -> None:
        # Inverse partial (codex P2 round 2): PreToolUse current,
        # SessionStart missing → only SessionStart added; PreToolUse
        # entry preserved untouched.
        settings = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "AskUserQuestion",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "cc-telegram hook",
                                "timeout": 2,
                            }
                        ],
                    }
                ]
            }
        }
        settings_file = tmp_path / "settings.json"
        settings_file.write_text(json.dumps(settings))
        monkeypatch.setattr(
            "cctelegram.hook._find_cc_telegram_path", lambda: "cc-telegram"
        )
        assert _install_hook(settings_file=settings_file) == 0
        data = json.loads(settings_file.read_text())
        # SessionStart freshly added with timeout 5.
        ss_entries = data["hooks"]["SessionStart"]
        assert len(ss_entries) == 1
        assert ss_entries[0]["hooks"][0]["timeout"] == _SESSION_START_TIMEOUT_S
        assert ss_entries[0]["hooks"][0]["command"] == "cc-telegram hook"
        # PreToolUse NOT duplicated, original entry preserved.
        pre_entries = _pre_tool_use_entries(data)
        assert len(pre_entries) == 1
        assert pre_entries[0]["matcher"] == "AskUserQuestion"

    def test_install_preserves_unrelated_hooks(self, tmp_path, monkeypatch) -> None:
        # An unrelated PostToolUse hook MUST survive installation.
        settings = {
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [{"type": "command", "command": "some-other-tool"}],
                    }
                ]
            }
        }
        settings_file = tmp_path / "settings.json"
        settings_file.write_text(json.dumps(settings))
        monkeypatch.setattr(
            "cctelegram.hook._find_cc_telegram_path", lambda: "cc-telegram"
        )
        assert _install_hook(settings_file=settings_file) == 0
        data = json.loads(settings_file.read_text())
        # Unrelated entry preserved.
        assert data["hooks"]["PostToolUse"][0]["matcher"] == "Bash"
        # Both managed entries present.
        assert _is_session_start_installed(data) == "current"
        assert _is_pre_tool_use_installed(data) == "current"


class TestHookMainValidation:
    def _run_hook_main(
        self, monkeypatch: pytest.MonkeyPatch, payload: dict, *, tmux_pane: str = ""
    ) -> None:
        monkeypatch.setattr(sys, "argv", ["cc-telegram", "hook"])
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        if tmux_pane:
            monkeypatch.setenv("TMUX_PANE", tmux_pane)
        else:
            monkeypatch.delenv("TMUX_PANE", raising=False)
        hook_main()

    def test_missing_session_id(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        self._run_hook_main(
            monkeypatch,
            {"cwd": "/tmp", "hook_event_name": "SessionStart"},
        )
        assert not (tmp_path / "session_map.json").exists()

    def test_invalid_uuid_format(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        self._run_hook_main(
            monkeypatch,
            {
                "session_id": "not-a-uuid",
                "cwd": "/tmp",
                "hook_event_name": "SessionStart",
            },
        )
        assert not (tmp_path / "session_map.json").exists()

    def test_relative_cwd(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        self._run_hook_main(
            monkeypatch,
            {
                "session_id": "550e8400-e29b-41d4-a716-446655440000",
                "cwd": "relative/path",
                "hook_event_name": "SessionStart",
            },
        )
        assert not (tmp_path / "session_map.json").exists()

    def test_unhandled_event_is_noop(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        self._run_hook_main(
            monkeypatch,
            {
                "session_id": "550e8400-e29b-41d4-a716-446655440000",
                "cwd": "/tmp",
                "hook_event_name": "Stop",
            },
        )
        assert not (tmp_path / "session_map.json").exists()
        assert not (tmp_path / "auq_pending").exists()

    def test_non_dict_payload_is_noop(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        # JSON array instead of object → reject.
        monkeypatch.setattr(sys, "argv", ["cc-telegram", "hook"])
        monkeypatch.setattr(sys, "stdin", io.StringIO("[1, 2, 3]"))
        monkeypatch.delenv("TMUX_PANE", raising=False)
        assert hook_main() == 0
        assert not (tmp_path / "session_map.json").exists()
        assert not (tmp_path / "auq_pending").exists()


# ── PreToolUse handler ────────────────────────────────────────────────────


_VALID_SESSION_ID = "550e8400-e29b-41d4-a716-446655440000"
_VALID_TOOL_USE_ID = "toolu_017abcdef01234567890ab"


def _auq_payload(
    *,
    questions: list[dict] | None = None,
    tool_use_id: str | None = _VALID_TOOL_USE_ID,
    session_id: str = _VALID_SESSION_ID,
) -> dict:
    if questions is None:
        questions = [
            {
                "question": "Pick a fruit",
                "header": "Fruit",
                "multiSelect": False,
                "options": [
                    {"label": "Apple", "description": "fresh & red"},
                    {"label": "Banana", "description": "yellow"},
                ],
            }
        ]
    payload = {
        "session_id": session_id,
        "cwd": "/Users/test/repo",
        "hook_event_name": "PreToolUse",
        "tool_name": "AskUserQuestion",
        "tool_input": {"questions": questions},
        "transcript_path": "/tmp/transcript.jsonl",
    }
    if tool_use_id is not None:
        payload["tool_use_id"] = tool_use_id
    return payload


def _run_hook_with_stdin(monkeypatch: pytest.MonkeyPatch, payload: dict | str) -> int:
    monkeypatch.setattr(sys, "argv", ["cc-telegram", "hook"])
    stdin_text = json.dumps(payload) if not isinstance(payload, str) else payload
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin_text))
    monkeypatch.delenv("TMUX_PANE", raising=False)
    return hook_main()


class TestPreToolUseHandler:
    def test_writes_side_file_with_schema_v1_and_fingerprint(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        assert _run_hook_with_stdin(monkeypatch, _auq_payload()) == 0

        side_file = tmp_path / "auq_pending" / f"{_VALID_SESSION_ID}.json"
        assert side_file.exists()
        record = json.loads(side_file.read_text())
        assert record["schema_version"] == 1
        assert record["session_id"] == _VALID_SESSION_ID
        assert record["tool_use_id"] == _VALID_TOOL_USE_ID
        assert record["tool_input"]["questions"][0]["question"] == "Pick a fruit"
        assert "input_fingerprint" in record
        assert len(record["input_fingerprint"]) == 12
        assert isinstance(record["written_at"], float)
        # Hook wrote it just now.
        assert abs(record["written_at"] - time.time()) < 5

    def test_writes_no_stdout(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
    ) -> None:
        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        assert _run_hook_with_stdin(monkeypatch, _auq_payload()) == 0
        captured = capsys.readouterr()
        # Logging goes to stderr. The hook must NOT print to stdout —
        # Claude Code's permission-decision parser would otherwise misread
        # output as a control message.
        assert captured.out == ""

    def test_dir_mode_0700_on_first_create(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        assert _run_hook_with_stdin(monkeypatch, _auq_payload()) == 0
        pending_dir = tmp_path / "auq_pending"
        mode = stat.S_IMODE(pending_dir.stat().st_mode)
        assert mode == 0o700

    def test_dir_mode_chmodded_when_preexisting_loose(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        # Pre-create the dir with loose mode 0755 — hook MUST tighten.
        pending_dir = tmp_path / "auq_pending"
        pending_dir.mkdir(mode=0o755)
        # mkdir's mode arg is masked by umask; force the loose mode.
        os.chmod(pending_dir, 0o755)
        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        assert _run_hook_with_stdin(monkeypatch, _auq_payload()) == 0
        mode = stat.S_IMODE(pending_dir.stat().st_mode)
        assert mode == 0o700

    def test_file_mode_0600(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        assert _run_hook_with_stdin(monkeypatch, _auq_payload()) == 0
        side_file = tmp_path / "auq_pending" / f"{_VALID_SESSION_ID}.json"
        mode = stat.S_IMODE(side_file.stat().st_mode)
        assert mode == 0o600

    def test_rejects_symlink_pending_dir(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        # Hostile-ish setup: auq_pending is a symlink to somewhere else.
        target = tmp_path / "evil_target"
        target.mkdir()
        (tmp_path / "auq_pending").symlink_to(target)
        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        assert _run_hook_with_stdin(monkeypatch, _auq_payload()) == 0
        # No file under the symlinked target.
        assert not list(target.iterdir())

    def test_non_auq_tool_skipped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        # PreToolUse for a different tool — matcher should have filtered,
        # but the handler is defensive.
        payload = _auq_payload()
        payload["tool_name"] = "Bash"
        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        assert _run_hook_with_stdin(monkeypatch, payload) == 0
        assert not (tmp_path / "auq_pending").exists()

    def test_invalid_tool_input_shape_logged_no_crash(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        payload = _auq_payload()
        payload["tool_input"] = "not a dict"
        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        assert _run_hook_with_stdin(monkeypatch, payload) == 0
        assert not (tmp_path / "auq_pending").exists()

    def test_empty_questions_array_skipped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        payload = _auq_payload(questions=[])
        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        assert _run_hook_with_stdin(monkeypatch, payload) == 0
        assert not (tmp_path / "auq_pending").exists()

    def test_malformed_question_label_skipped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        payload = _auq_payload(
            questions=[{"question": "Q", "options": [{"label": 42}]}]
        )
        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        assert _run_hook_with_stdin(monkeypatch, payload) == 0
        assert not (tmp_path / "auq_pending").exists()

    def test_sdk_entrypoint_skipped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "sdk-py")
        assert _run_hook_with_stdin(monkeypatch, _auq_payload()) == 0
        assert not (tmp_path / "auq_pending").exists()

    def test_handler_swallows_handler_exceptions(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        # Patch atomic_write_json to raise — hook MUST still exit 0
        # and NOT propagate to the caller (Claude Code would interpret
        # a non-zero exit as a permission deny / error).
        def _explode(*_a, **_kw):
            raise RuntimeError("simulated atomic-write failure")

        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        monkeypatch.setattr("cctelegram.utils.atomic_write_json", _explode)
        # We can't easily make `atomic_write_json` reach the patched ref
        # because the handler imports it locally. Instead simulate by
        # making the pending dir unwriteable.
        pending = tmp_path / "auq_pending"
        pending.mkdir(mode=0o500)
        # On macOS root-owned tests sometimes ignore mode; just confirm
        # exit code stays 0 regardless of actual write outcome.
        assert _run_hook_with_stdin(monkeypatch, _auq_payload()) == 0

    def test_missing_tool_use_id_still_writes_record(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        # The PreToolUse payload may omit tool_use_id under some Claude
        # versions; the side file should still be written with
        # tool_use_id="".
        payload = _auq_payload(tool_use_id=None)
        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        assert _run_hook_with_stdin(monkeypatch, payload) == 0
        side_file = tmp_path / "auq_pending" / f"{_VALID_SESSION_ID}.json"
        record = json.loads(side_file.read_text())
        assert record["tool_use_id"] == ""

    def test_atomic_no_partial_files_under_pending(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        # After a successful write, only the final <session_id>.json
        # exists under auq_pending/ — no stray temp files.
        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        assert _run_hook_with_stdin(monkeypatch, _auq_payload()) == 0
        entries = sorted(p.name for p in (tmp_path / "auq_pending").iterdir())
        assert entries == [f"{_VALID_SESSION_ID}.json"]
