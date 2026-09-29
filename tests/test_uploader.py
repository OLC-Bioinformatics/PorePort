import threading

import pytest

from nanopore_gui.api import FoodPortError
from nanopore_gui.storage import QueueStore
from nanopore_gui.uploader import UploadCoordinator


class FakeClient:
    def __init__(self, prepare_errors=(), upload_errors=(), complete_errors=()):
        self.calls = []
        self.prepare_errors = list(prepare_errors)
        self.upload_errors = list(upload_errors)
        self.complete_errors = list(complete_errors)

    def prepare_file(self, run_id, path, size):
        self.calls.append(("prepare", run_id, path, size))
        if self.prepare_errors:
            raise self.prepare_errors.pop(0)
        return {"file_id": 17, "upload_url": "https://example.invalid/blob",
                "blob_name": "blob", "container": "container"}

    def upload_blob(self, url, path):
        self.calls.append(("upload", url, path))
        if self.upload_errors:
            raise self.upload_errors.pop(0)

    def complete_file(self, run_id, file_id):
        self.calls.append(("complete", run_id, file_id))
        if self.complete_errors:
            raise self.complete_errors.pop(0)


@pytest.fixture
def store(tmp_path):
    instance = QueueStore(tmp_path / "queue.sqlite3")
    yield instance
    instance.close()


def submit_and_wait(store, client, file, *, attempts=3):
    coordinator = UploadCoordinator(client, store, 42, workers=1, max_attempts=attempts)
    try:
        assert coordinator.submit(file.name, file, file.stat().st_size)
        coordinator.close(wait=True)
        assert coordinator.is_idle
        return store.all(42)[0]
    finally:
        coordinator.close(wait=True)


def test_success_persists_uploaded_file_and_rejects_duplicate(tmp_path, store):
    file = tmp_path / "sample.pod5"
    file.write_bytes(b"POD5")
    client = FakeClient()
    row = submit_and_wait(store, client, file)
    assert row["status"] == "uploaded"
    assert row["attempt_count"] == 1
    assert (row["server_file_id"], row["server_blob_name"], row["server_container"]) == (17, "blob", "container")
    assert [call[0] for call in client.calls] == ["prepare", "upload", "complete"]
    coordinator = UploadCoordinator(client, store, 42)
    try:
        assert not coordinator.submit(file.name, file, 4)
    finally:
        coordinator.close(wait=True)


def test_transient_prepare_failure_retries_without_real_sleep(tmp_path, store, monkeypatch):
    monkeypatch.setattr("nanopore_gui.uploader.time.sleep", lambda _: None)
    file = tmp_path / "sample.pod5"
    file.write_bytes(b"POD5")
    client = FakeClient(prepare_errors=[FoodPortError(503, "busy")])
    row = submit_and_wait(store, client, file)
    assert row["status"] == "uploaded"
    assert row["attempt_count"] == 1  # Only successful prepare reaches uploading.
    assert [call[0] for call in client.calls] == ["prepare", "prepare", "upload", "complete"]


@pytest.mark.parametrize("status", [400, 401, 403, 409])
def test_nonretryable_prepare_error_stops_immediately(tmp_path, store, status):
    file = tmp_path / "sample.pod5"
    file.write_bytes(b"POD5")
    client = FakeClient(prepare_errors=[FoodPortError(status, "rejected")])
    row = submit_and_wait(store, client, file)
    assert row["status"] == "error"
    assert row["attempt_count"] == 0
    assert len(client.calls) == 1


@pytest.mark.parametrize("status", [401, 403])
def test_expired_upload_url_is_reprepared(tmp_path, store, monkeypatch, status):
    monkeypatch.setattr("nanopore_gui.uploader.time.sleep", lambda _: None)
    file = tmp_path / "sample.pod5"
    file.write_bytes(b"POD5")
    client = FakeClient(upload_errors=[FoodPortError(status, "expired")])
    row = submit_and_wait(store, client, file)
    assert row["status"] == "uploaded"
    assert row["attempt_count"] == 2
    assert [call[0] for call in client.calls] == ["prepare", "upload", "prepare", "upload", "complete"]


def test_missing_or_changed_file_never_calls_api(tmp_path, store, monkeypatch):
    monkeypatch.setattr("nanopore_gui.uploader.time.sleep", lambda _: None)
    client = FakeClient()
    missing = tmp_path / "missing.pod5"
    coordinator = UploadCoordinator(client, store, 42, max_attempts=2)
    try:
        assert coordinator.submit(missing.name, missing, 4)
        coordinator.close(wait=True)
        assert store.all(42)[0]["status"] == "error"
        assert not client.calls
    finally:
        coordinator.close(wait=True)
    changed = tmp_path / "changed.pod5"
    changed.write_bytes(b"POD5")
    coordinator = UploadCoordinator(client, store, 42, max_attempts=1)
    try:
        assert coordinator.submit(changed.name, changed, 3)
        coordinator.close(wait=True)
        assert {row["relative_path"]: row["status"] for row in store.all(42)}[changed.name] == "error"
        assert not client.calls
    finally:
        coordinator.close(wait=True)


def test_recover_uploading_on_startup(store):
    store.add_pending(42, "stale.pod5", "stale.pod5", 4)
    store.mark(42, "stale.pod5", "uploading")
    coordinator = UploadCoordinator(FakeClient(), store, 42)
    try:
        assert store.all(42)[0]["status"] == "pending"
        assert store.all(42)[0]["upload_started_at"] is None
    finally:
        coordinator.close(wait=True)


def test_inflight_duplicate_and_close_reject_new_work(tmp_path, store):
    started, release = threading.Event(), threading.Event()
    class BlockingClient(FakeClient):
        def upload_blob(self, url, path):
            started.set()
            assert release.wait(5), "Timed out waiting for test to release upload"
            super().upload_blob(url, path)
    file = tmp_path / "sample.pod5"
    file.write_bytes(b"POD5")
    coordinator = UploadCoordinator(BlockingClient(), store, 42, workers=1)
    try:
        assert coordinator.submit(file.name, file, 4)
        assert started.wait(5)
        assert coordinator.active_count == 1
        assert not coordinator.is_idle
        assert not coordinator.submit(file.name, file, 4)
        coordinator.close(wait=False)
        assert not coordinator.submit("other.pod5", file, 4)
    finally:
        release.set()
        coordinator.executor.shutdown(wait=True)
    assert coordinator.is_idle
    assert store.all(42)[0]["status"] == "uploaded"
