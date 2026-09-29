from pathlib import Path

from nanopore_gui.scanner import StablePod5Scanner


def test_scanner_uses_relative_posix_paths(tmp_path, monkeypatch):
    pod5 = tmp_path / "pass" / "sample.pod5"
    pod5.parent.mkdir()
    pod5.write_bytes(b"pod5")
    scanner = StablePod5Scanner(tmp_path, stable_seconds=0)
    scanner.scan()
    result = scanner.scan()
    assert result[0][0] == "pass/sample.pod5"
    assert result[0][2] == 4


def test_scanner_skips_unreadable_files(tmp_path, monkeypatch):
    pod5 = tmp_path / "locked.pod5"
    pod5.write_bytes(b"pod5")
    original_open = Path.open

    def reject_open(path, *args, **kwargs):
        if path == pod5:
            raise OSError("file is locked")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", reject_open)
    scanner = StablePod5Scanner(tmp_path, stable_seconds=0)

    scanner.scan()
    assert scanner.scan() == []


def test_scanner_waits_for_stability_interval(tmp_path, monkeypatch):
    pod5 = tmp_path / "sample.pod5"
    pod5.write_bytes(b"pod5")
    clock = iter((100.0, 105.0, 109.0, 110.0))
    monkeypatch.setattr("nanopore_gui.scanner.time.time", lambda: next(clock))
    scanner = StablePod5Scanner(tmp_path, stable_seconds=10)

    assert scanner.scan() == []
    assert scanner.scan() == []
    assert scanner.scan() == []
    assert scanner.scan()[0][0] == "sample.pod5"


def test_scanner_releases_each_unchanged_signature_only_once(tmp_path):
    pod5 = tmp_path / "sample.pod5"
    pod5.write_bytes(b"pod5")
    scanner = StablePod5Scanner(tmp_path, stable_seconds=0)

    assert scanner.scan() == []
    assert scanner.scan() == [("sample.pod5", pod5, 4)]
    assert scanner.scan() == []
    assert scanner.scan() == []


def test_scanner_ignores_other_extensions_and_directories(tmp_path):
    (tmp_path / "sample.fastq").write_bytes(b"data")
    (tmp_path / "folder.pod5").mkdir()
    pod5 = tmp_path / "nested" / "SAMPLE.POD5"
    pod5.parent.mkdir()
    pod5.write_bytes(b"123")
    scanner = StablePod5Scanner(tmp_path, stable_seconds=0)

    assert scanner.scan() == []
    assert scanner.scan() == [("nested/SAMPLE.POD5", pod5, 3)]


def test_scanner_resets_timer_when_file_size_changes(tmp_path, monkeypatch):
    pod5 = tmp_path / "sample.pod5"
    pod5.write_bytes(b"a")
    now = [100.0]
    monkeypatch.setattr("nanopore_gui.scanner.time.time", lambda: now[0])
    scanner = StablePod5Scanner(tmp_path, stable_seconds=10)

    assert scanner.scan() == []
    now[0] = 105.0
    pod5.write_bytes(b"abc")
    assert scanner.scan() == []
    now[0] = 114.0
    assert scanner.scan() == []
    now[0] = 115.0
    assert scanner.scan() == [("sample.pod5", pod5, 3)]
    assert scanner.scan() == []


def test_scanner_releases_changed_file_after_new_stability_interval(tmp_path, monkeypatch):
    pod5 = tmp_path / "sample.pod5"
    pod5.write_bytes(b"a")
    now = [100.0]
    monkeypatch.setattr("nanopore_gui.scanner.time.time", lambda: now[0])
    scanner = StablePod5Scanner(tmp_path, stable_seconds=10)

    assert scanner.scan() == []
    now[0] = 110.0
    assert scanner.scan() == [("sample.pod5", pod5, 1)]
    pod5.write_bytes(b"abc")
    assert scanner.scan() == []
    now[0] = 119.0
    assert scanner.scan() == []
    now[0] = 120.0
    assert scanner.scan() == [("sample.pod5", pod5, 3)]
    assert scanner.scan() == []


def test_scanner_detects_mtime_change_without_size_change(tmp_path, monkeypatch):
    import os

    pod5 = tmp_path / "sample.pod5"
    pod5.write_bytes(b"abc")
    os.utime(pod5, (1000, 1000))
    now = [100.0]
    monkeypatch.setattr("nanopore_gui.scanner.time.time", lambda: now[0])
    scanner = StablePod5Scanner(tmp_path, stable_seconds=10)

    assert scanner.scan() == []
    now[0] = 110.0
    assert scanner.scan() == [("sample.pod5", pod5, 3)]
    os.utime(pod5, (2000, 2000))
    assert scanner.scan() == []
    now[0] = 119.0
    assert scanner.scan() == []
    now[0] = 120.0
    assert scanner.scan() == [("sample.pod5", pod5, 3)]


def test_scanner_forgets_removed_file_before_same_path_reappears(tmp_path):
    pod5 = tmp_path / "sample.pod5"
    pod5.write_bytes(b"abc")
    scanner = StablePod5Scanner(tmp_path, stable_seconds=0)

    assert scanner.scan() == []
    assert scanner.scan() == [("sample.pod5", pod5, 3)]
    pod5.unlink()
    assert scanner.scan() == []
    assert pod5 not in scanner._observed
    assert pod5 not in scanner._released
    pod5.write_bytes(b"abc")
    assert scanner.scan() == []
    assert scanner.scan() == [("sample.pod5", pod5, 3)]


def test_scanner_clears_state_when_root_disappears(tmp_path):
    root = tmp_path / "watch"
    root.mkdir()
    pod5 = root / "sample.pod5"
    pod5.write_bytes(b"abc")
    scanner = StablePod5Scanner(root, stable_seconds=0)

    assert scanner.scan() == []
    assert scanner.scan() == [("sample.pod5", pod5, 3)]
    pod5.unlink()
    root.rmdir()
    assert scanner.scan() == []
    assert scanner._observed == {}
    assert scanner._released == {}
    root.mkdir()
    pod5.write_bytes(b"abc")
    assert scanner.scan() == []
    assert scanner.scan() == [("sample.pod5", pod5, 3)]


def test_scanner_retries_file_that_becomes_readable(tmp_path, monkeypatch):
    pod5 = tmp_path / "sample.pod5"
    pod5.write_bytes(b"abc")
    original_open = Path.open
    locked = [True]

    def open_when_unlocked(path, *args, **kwargs):
        if path == pod5 and locked[0]:
            raise OSError("locked")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_when_unlocked)
    scanner = StablePod5Scanner(tmp_path, stable_seconds=0)
    assert scanner.scan() == []
    assert scanner.scan() == []
    locked[0] = False
    assert scanner.scan() == []
    assert scanner.scan() == [("sample.pod5", pod5, 3)]


def test_scanner_handles_missing_root(tmp_path):
    scanner = StablePod5Scanner(tmp_path / "not-created", stable_seconds=0)
    assert scanner.scan() == []
    assert scanner._observed == {}
    assert scanner._released == {}
