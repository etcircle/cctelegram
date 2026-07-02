"""Tests for the working-directory browser listing (build_directory_browser).

Pins the per-entry error containment ported from etcircle/cctelegram PR #1
(Windows /mnt/c via WSL DrvFs): unreadable system files (pagefile.sys,
swapfile.sys, DumpStack.log.tmp) make ``Path.is_dir()`` raise
PermissionError/OSError, and a single raising entry must not collapse the
whole listing to "(No subdirectories)". Also pins the preserved semantics:
hidden-dir filtering, sorting, and the directory-level iterdir failure
falling back to an empty listing.
"""

from pathlib import Path

import pytest

from cctelegram.config import config
from cctelegram.handlers.directory_browser import build_directory_browser


class _FakeEntry:
    """Directory entry double: .name + .is_dir() that may raise."""

    def __init__(
        self, name: str, *, is_dir: bool = True, raises: Exception | None = None
    ):
        self.name = name
        self._is_dir = is_dir
        self._raises = raises

    def is_dir(self) -> bool:
        if self._raises is not None:
            raise self._raises
        return self._is_dir


@pytest.fixture
def _visible_dirs_only(monkeypatch):
    monkeypatch.setattr(config, "show_hidden_dirs", False)


@pytest.mark.usefixtures("_visible_dirs_only")
class TestBuildDirectoryBrowserListing:
    def _patch_iterdir(self, monkeypatch, entries):
        monkeypatch.setattr(Path, "iterdir", lambda self: iter(entries))

    def test_one_unreadable_entry_does_not_wipe_listing(self, monkeypatch, tmp_path):
        """The Windows-mount fix: pagefile.sys raising is skipped, dirs kept."""
        self._patch_iterdir(
            monkeypatch,
            [
                _FakeEntry("Users"),
                _FakeEntry("pagefile.sys", raises=PermissionError("denied")),
                _FakeEntry("Windows"),
                _FakeEntry("DumpStack.log.tmp", raises=OSError("bad")),
            ],
        )
        _text, _kb, subdirs = build_directory_browser(str(tmp_path), page=0)
        assert subdirs == ["Users", "Windows"]

    def test_listing_sorted_and_files_excluded(self, monkeypatch, tmp_path):
        self._patch_iterdir(
            monkeypatch,
            [
                _FakeEntry("zeta"),
                _FakeEntry("alpha"),
                _FakeEntry("notes.txt", is_dir=False),
            ],
        )
        _text, _kb, subdirs = build_directory_browser(str(tmp_path), page=0)
        assert subdirs == ["alpha", "zeta"]

    def test_hidden_dirs_filtered_when_disabled(self, monkeypatch, tmp_path):
        self._patch_iterdir(
            monkeypatch,
            [_FakeEntry(".git"), _FakeEntry("src")],
        )
        _text, _kb, subdirs = build_directory_browser(str(tmp_path), page=0)
        assert subdirs == ["src"]

    def test_hidden_dirs_included_when_enabled(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "show_hidden_dirs", True)
        self._patch_iterdir(
            monkeypatch,
            [_FakeEntry(".git"), _FakeEntry("src")],
        )
        _text, _kb, subdirs = build_directory_browser(str(tmp_path), page=0)
        assert subdirs == [".git", "src"]

    def test_directory_level_iterdir_failure_falls_back_empty(
        self, monkeypatch, tmp_path
    ):
        def _raise(self):
            raise PermissionError("denied")

        monkeypatch.setattr(Path, "iterdir", _raise)
        _text, _kb, subdirs = build_directory_browser(str(tmp_path), page=0)
        assert subdirs == []
