import threading

import pytest

from nanopore_gui import test_mode as mode
from nanopore_gui.storage import QueueStore


def test_metadata_returns_independent_expected_samples():
    metadata = mode.test_metadata()
    assert metadata["barcode_values"] == list(mode.DEFAULT_BARCODES)
    assert metadata["sample_metadata"]["samples"] == [
        {"barcode": barcode, **mode.DEFAULT_SAMPLE_METADATA[barcode]}
        for barcode in mode.DEFAULT_BARCODES
    ]
    metadata["barcode_values"].clear()
    assert mode.test_metadata()["barcode_values"] == list(mode.DEFAULT_BARCODES)


@pytest.mark.parametrize("count,expected", [
    (0, []), (1, [1]), (3, [3]), (4, [3, 1]),
    (23, [3, 7, 2, 5, 6]), (25, [3, 7, 2, 5, 6, 2]),
])
def test_release_plan_partitions_without_loss(count, expected):
    files = tuple(str(index) for index in range(count))
    plan = mode.release_plan(files)
    assert [number for number, _ in plan] == list(range(1, len(expected) + 1))
    assert [len(wave) for _, wave in plan] == expected
    assert tuple(item for _, wave in plan for item in wave) == files


def test_cloud_waves_complete_each_wave_before_finalizing(monkeypatch):
    monkeypatch.setattr(mode, "DEFAULT_FILES", ("a.pod5", "b.pod5", "c.pod5", "d.pod5"))
    monkeypatch.setattr(mode, "_storage_client", lambda: (object(), "account", "key"))
    events = []
    def copy(client, service, account, key, run_id, filename, wave_number,
             copy_timeout, copy_poll, cancel_event=None):
        events.append(("copy", wave_number, filename))
        return filename, {"file_id": len(events)}
    monkeypatch.setattr(mode, "_copy_cloud_file", copy)
    class Client:
        def complete_file(self, run_id, file_id):
            events.append(("complete", file_id))
        def finalize(self, run_id):
            events.append(("finalize", run_id))
            return {"workflow_state": "stopping"}
    result = mode.seed_cloud_waves(Client(), 42, wait_seconds=0)
    assert result == {"workflow_state": "stopping"}
    assert [event[0] for event in events] == [
        "copy", "copy", "copy", "complete", "complete", "complete",
        "copy", "complete", "finalize",
    ]


def test_cloud_waves_can_skip_finalize_or_cancel_before_copy(monkeypatch):
    monkeypatch.setattr(mode, "DEFAULT_FILES", ("a.pod5",))
    monkeypatch.setattr(mode, "_storage_client", lambda: (object(), "account", "key"))
    copied = []
    monkeypatch.setattr(mode, "_copy_cloud_file", lambda *args, **kwargs: (copied.append(1) or ("a.pod5", {"file_id": 1})))
    class Client:
        def complete_file(self, *args):
            pass
        def finalize(self, *args):
            raise AssertionError("finalize should not be called")
    assert mode.seed_cloud_waves(Client(), 42, wait_seconds=0, auto_finalize=False) == {"workflow_state": "processing"}
    cancelled = threading.Event()
    cancelled.set()
    assert mode.seed_cloud_waves(Client(), 42, cancel_event=cancelled) == {"workflow_state": "cancelled"}
    assert copied == [1]


def test_local_controller_releases_pod5_and_waits_for_upload(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.pod5").write_bytes(b"POD5")
    (source / "ignore.txt").write_text("ignored")
    staging = tmp_path / "staging"
    store = QueueStore(tmp_path / "queue.sqlite3")
    finished, errors = [], []
    controller = mode.LocalWaveController(source, staging, store, 42, wait_seconds=0,
                                          finished=lambda: finished.append(True), failed=errors.append)
    def mark_uploaded(_seconds):
        path = "pass/release-wave-000001/a.pod5"
        store.add_pending(42, path, str(staging / path), 4)
        store.mark(42, path, "uploaded")
    monkeypatch.setattr(mode.time, "sleep", mark_uploaded)
    try:
        controller._run()
        assert (staging / "pass/release-wave-000001/a.pod5").read_bytes() == b"POD5"
        assert finished == [True] and not errors
    finally:
        store.close()


def test_local_controller_reports_empty_source(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    errors = []
    controller = mode.LocalWaveController(source, tmp_path / "staging", object(), 42,
                                          failed=errors.append)
    controller._run()
    assert len(errors) == 1
    assert "does not contain POD5" in str(errors[0])


def test_local_controller_reports_failed_upload(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.pod5").write_bytes(b"POD5")
    store = QueueStore(tmp_path / "queue.sqlite3")
    errors = []
    controller = mode.LocalWaveController(source, tmp_path / "staging", store, 42,
                                          failed=errors.append)
    def mark_error(_seconds):
        path = "pass/release-wave-000001/a.pod5"
        store.add_pending(42, path, path, 4)
        store.mark(42, path, "error", error="failed")
    monkeypatch.setattr(mode.time, "sleep", mark_error)
    try:
        controller._run()
        assert len(errors) == 1
        assert "Retry failed uploads" in str(errors[0])
    finally:
        store.close()
