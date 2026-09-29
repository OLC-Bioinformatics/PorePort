from nanopore_gui.api import FoodPortError
from concurrent.futures import ThreadPoolExecutor

from nanopore_gui.storage import QueueStore
from nanopore_gui.uploader import UploadCoordinator


class FakeClient:
    def __init__(self, prepare_errors=(), upload_errors=()):
        self.prepare_errors = list(prepare_errors)
        self.upload_errors = list(upload_errors)
        self.prepare_calls = 0
        self.completed = []

    def prepare_file(self, run_id, relative_path, size_bytes):
        self.prepare_calls += 1
        if self.prepare_errors:
            raise self.prepare_errors.pop(0)
        return {"file_id": 7, "upload_url": "https://blob.test/file"}

    def upload_blob(self, upload_url, local_path):
        if self.upload_errors:
            error = self.upload_errors.pop(0)
            if error:
                raise error
        return None

    def complete_file(self, run_id, file_id):
        self.completed.append(file_id)


def test_active_run_survives_store_reopen(tmp_path):
    database = tmp_path / "queue.sqlite3"
    store = QueueStore(database)
    store.save_run(12, "night run", str(tmp_path), "2026-08-25T10:00:00+00:00")
    store.connection.close()

    reopened = QueueStore(database)
    run = reopened.active_run()
    assert run["run_id"] == 12
    assert run["run_name"] == "night run"


def test_closed_run_is_not_restored(tmp_path):
    store = QueueStore(tmp_path / "queue.sqlite3")
    store.save_run(12, "night run", str(tmp_path), "2026-08-25T10:00:00+00:00")
    store.close_run(12)

    assert store.active_run() is None


def test_transient_upload_error_retries(monkeypatch, tmp_path):
    monkeypatch.setattr("nanopore_gui.uploader.time.sleep", lambda _: None)
    store = QueueStore(tmp_path / "queue.sqlite3")
    client = FakeClient([FoodPortError(503, "busy")])
    source = tmp_path / "sample.pod5"
    source.write_bytes(b"data")
    store.add_pending(1, "sample.pod5", str(source), 4)
    coordinator = UploadCoordinator(client, store, 1, max_attempts=2)

    coordinator._upload("sample.pod5", source, 4)

    assert client.prepare_calls == 2
    assert store.all(1)[0]["status"] == "uploaded"


def test_client_upload_error_is_not_retried(tmp_path):
    store = QueueStore(tmp_path / "queue.sqlite3")
    client = FakeClient([FoodPortError(400, "invalid file")])
    source = tmp_path / "sample.pod5"
    source.write_bytes(b"data")
    store.add_pending(1, "sample.pod5", str(source), 4)
    coordinator = UploadCoordinator(client, store, 1, max_attempts=3)

    coordinator._upload("sample.pod5", source, 4)

    assert client.prepare_calls == 1
    assert store.all(1)[0]["status"] == "error"


def test_stale_uploads_are_recovered(tmp_path):
    store = QueueStore(tmp_path / "queue.sqlite3")
    store.add_pending(1, "sample.pod5", "sample.pod5", 4)
    store.mark(1, "sample.pod5", "uploading")

    coordinator = UploadCoordinator(FakeClient(), store, 1)

    assert coordinator
    assert store.all(1)[0]["status"] == "pending"


def test_upload_oserror_retries(monkeypatch, tmp_path):
    monkeypatch.setattr("nanopore_gui.uploader.time.sleep", lambda _: None)
    store = QueueStore(tmp_path / "queue.sqlite3")
    client = FakeClient(upload_errors=(OSError("temporarily unavailable"), None))
    source = tmp_path / "sample.pod5"
    source.write_bytes(b"data")
    store.add_pending(1, "sample.pod5", str(source), 4)
    coordinator = UploadCoordinator(client, store, 1, max_attempts=2)

    coordinator._upload("sample.pod5", source, 4)

    assert client.prepare_calls == 2
    assert store.all(1)[0]["status"] == "uploaded"


def test_expired_sas_reacquires_upload_url(monkeypatch, tmp_path):
    monkeypatch.setattr("nanopore_gui.uploader.time.sleep", lambda _: None)
    store = QueueStore(tmp_path / "queue.sqlite3")
    client = FakeClient(upload_errors=(FoodPortError(403, "SAS expired"), None))
    source = tmp_path / "sample.pod5"
    source.write_bytes(b"data")
    store.add_pending(1, "sample.pod5", str(source), 4)
    coordinator = UploadCoordinator(client, store, 1, max_attempts=2)

    coordinator._upload("sample.pod5", source, 4)

    assert client.prepare_calls == 2
    assert store.all(1)[0]["status"] == "uploaded"


def test_queue_supports_concurrent_updates(tmp_path):
    store = QueueStore(tmp_path / "queue.sqlite3")

    def add_file(index):
        relative = f"sample-{index}.pod5"
        store.add_pending(1, relative, relative, index + 1)
        store.mark(1, relative, "uploaded")

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(add_file, range(32)))

    assert store.counts(1) == (32, sum(range(1, 33)), sum(range(1, 33)))