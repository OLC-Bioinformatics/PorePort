from nanopore_gui.storage import QueueStore


def test_queue_is_idempotent(tmp_path):
    store = QueueStore(tmp_path / "queue.sqlite3")
    store.add_pending(1, "sample.pod5", "sample.pod5", 12)
    store.add_pending(1, "sample.pod5", "sample.pod5", 12)
    assert len(store.all(1)) == 1
    assert store.counts(1) == (1, 12, 0)


def test_run_metadata_survives_store_reopen(tmp_path):
    database = tmp_path / "queue.sqlite3"
    store = QueueStore(database)
    store.save_run(1, "run", str(tmp_path), "2026-09-14T10:00:00+00:00", {
        "instrument_name": "MinION",
        "sample_metadata": {"sample_id": "S1"},
    })
    store.close()

    reopened = QueueStore(database)
    run = reopened.active_run()
    assert run["metadata_json"] == (
        '{"instrument_name": "MinION", "sample_metadata": {"sample_id": "S1"}}'
    )


def test_queue_refreshes_changed_pending_file(tmp_path):
    store = QueueStore(tmp_path / "queue.sqlite3")
    assert store.add_pending(1, "sample.pod5", "old.pod5", 12) is True
    assert store.add_pending(1, "sample.pod5", "new.pod5", 20) is True
    row, = store.all(1)
    assert (row["local_path"], row["size_bytes"], row["status"]) == ("new.pod5", 20, "pending")
    assert store.counts(1) == (1, 20, 0)
    store.close()


def test_uploading_and_uploaded_items_are_not_resubmitted(tmp_path):
    store = QueueStore(tmp_path / "queue.sqlite3")
    store.add_pending(1, "sample.pod5", "original", 12)
    store.mark(1, "sample.pod5", "uploading")
    assert store.add_pending(1, "sample.pod5", "changed", 20) is False
    assert store.all(1)[0]["attempt_count"] == 1
    store.mark(1, "sample.pod5", "uploaded", file_id=9,
               blob_name="blob", container="container")
    assert store.add_pending(1, "sample.pod5", "changed", 20) is False
    row, = store.all(1)
    assert (row["status"], row["size_bytes"], row["server_file_id"]) == ("uploaded", 12, 9)
    assert (row["server_blob_name"], row["server_container"]) == ("blob", "container")
    assert row["uploaded_at"] is not None
    assert store.counts(1) == (1, 12, 12)
    assert store.pending(1) == []
    store.close()


def test_error_can_be_requeued_without_losing_attempt_count(tmp_path):
    store = QueueStore(tmp_path / "queue.sqlite3")
    store.add_pending(1, "sample.pod5", "old", 12)
    store.mark(1, "sample.pod5", "uploading")
    store.mark(1, "sample.pod5", "error", error="temporary failure")
    assert store.pending(1)[0]["error"] == "temporary failure"
    assert store.add_pending(1, "sample.pod5", "new", 20) is True
    row, = store.all(1)
    assert (row["status"], row["error"], row["attempt_count"]) == ("pending", None, 1)
    assert (row["local_path"], row["size_bytes"]) == ("new", 20)
    store.close()


def test_recover_uploading_only_affects_requested_run(tmp_path):
    store = QueueStore(tmp_path / "queue.sqlite3")
    for run_id in (1, 2):
        store.add_pending(run_id, "sample.pod5", "sample.pod5", 12)
        store.mark(run_id, "sample.pod5", "uploading")
    assert store.recover_uploading(1) == 1
    assert store.recover_uploading(1) == 0
    assert store.all(1)[0]["status"] == "pending"
    assert store.all(1)[0]["upload_started_at"] is None
    assert store.all(2)[0]["status"] == "uploading"
    store.close()


def test_invalid_status_does_not_change_queue(tmp_path):
    import pytest
    store = QueueStore(tmp_path / "queue.sqlite3")
    store.add_pending(1, "sample.pod5", "sample.pod5", 12)
    with pytest.raises(ValueError, match="Unsupported queue status"):
        store.mark(1, "sample.pod5", "invalid")
    assert store.all(1)[0]["status"] == "pending"
    store.close()


def test_pending_and_all_are_sorted_and_isolated_by_run(tmp_path):
    store = QueueStore(tmp_path / "queue.sqlite3")
    for name in ("z.pod5", "a.pod5", "m.pod5"):
        store.add_pending(1, name, name, 2)
    store.add_pending(2, "other.pod5", "other.pod5", 7)
    store.mark(1, "m.pod5", "uploaded")
    assert [row["relative_path"] for row in store.pending(1)] == ["a.pod5", "z.pod5"]
    assert [row["relative_path"] for row in store.all(1)] == ["a.pod5", "m.pod5", "z.pod5"]
    assert store.counts(1) == (3, 6, 2)
    assert store.counts(2) == (1, 7, 0)
    assert store.counts(3) == (0, 0, 0)
    store.close()


def test_run_status_and_close_survive_reopen(tmp_path):
    database = tmp_path / "queue.sqlite3"
    store = QueueStore(database)
    store.save_run(1, "first", "first-path", "2026-09-14T10:00:00+00:00")
    store.save_run(2, "second", "second-path", "2026-09-15T10:00:00+00:00")
    assert store.active_run()["run_id"] == 2
    store.update_run_status(2, "processing")
    assert store.active_run()["run_id"] == 2
    assert store.run(2)["last_status_at"] is not None
    store.update_run_status(2, "complete")
    assert store.run(2)["active"] == 0
    assert store.active_run()["run_id"] == 1
    store.close_run(1)
    assert store.active_run() is None
    store.close()
    reopened = QueueStore(database)
    assert reopened.run(2)["workflow_state"] == "complete"
    assert reopened.active_run() is None
    reopened.close()


def test_error_run_is_terminal(tmp_path):
    store = QueueStore(tmp_path / "queue.sqlite3")
    store.save_run(1, "run", "path", "2026-09-14T10:00:00+00:00")
    store.update_run_status(1, "error")
    assert store.run(1)["active"] == 0
    assert store.active_run() is None
    store.close()


def test_reports_are_upserted_sorted_and_durable(tmp_path):
    database = tmp_path / "queue.sqlite3"
    store = QueueStore(database)
    store.save_report(1, 3, "third", "third.json")
    store.save_report(1, 1, "first")
    store.save_report(2, 1, "other")
    store.save_report(1, 3, "updated", "updated.json")
    assert [row["iteration"] for row in store.reports(1)] == [1, 3]
    assert store.report(1, 3)["report_directory"] == "updated"
    assert store.report(1, 3)["manifest_path"] == "updated.json"
    assert store.report(1, 2) is None
    assert len(store.reports(2)) == 1
    store.close()
    reopened = QueueStore(database)
    assert reopened.report(1, 3)["report_directory"] == "updated"
    reopened.close()
