import os
import re

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from nanopore_gui import ui
from nanopore_gui.storage import QueueStore


class FakeClient:
    def __init__(self):
        self.create_run_calls = []

    def create_run(self, run_name, metadata):
        self.create_run_calls.append((run_name, metadata))
        return {"run_id": 42}


class CapturePool:
    def __init__(self):
        self.tasks = []

    def start(self, task):
        self.tasks.append(task)


def make_window(tmp_path):
    application = QApplication.instance() or QApplication([])
    window = ui.MainWindow(FakeClient(), QueueStore(tmp_path / "queue.sqlite3"))
    window.api_pool = CapturePool()
    return application, window


def test_test_preset_prefills_contract_run_and_fixture_values(tmp_path):
    application = QApplication.instance() or QApplication([])
    window = ui.MainWindow(
        FakeClient(), QueueStore(tmp_path / "queue.sqlite3"), test_mode=True, test_directory=tmp_path
    )

    assert window.folder.text() == str(tmp_path)
    assert re.fullmatch(r"\d{6}-nanopore-test", window.run_name.text())
    assert window.stability.value() == 1
    assert window.workers.value() == 2
    selected = {item.text() for item in window.barcode_values.selectedItems()}
    assert selected == {str(barcode).zfill(2) for barcode in ui.DEFAULT_BARCODES}
    rows = {
        window.sample_metadata.item(row, 0).text(): (
            window.sample_metadata.item(row, 1).text(),
            window.sample_metadata.item(row, 2).text(),
        )
        for row in range(window.sample_metadata.rowCount())
    }
    assert set(rows) == selected
    for barcode, metadata in ui.DEFAULT_SAMPLE_METADATA.items():
        assert rows[str(barcode).zfill(2)] == (
            metadata["seqid"], metadata["olnid"]
        )



def test_pairing_code_entry_queues_exchange_task(monkeypatch, tmp_path):
    _application, window = make_window(tmp_path)
    window._pairing_id = "pairing-123"
    window.pair_code_button.setEnabled(True)
    window.pair_code.setText("")
    QTest.keyClicks(window.pair_code, "332845")

    captured = {}

    class FakePairingTask(QObject):
        succeeded = Signal(dict)
        failed = Signal(Exception)
        finished = Signal(object)

        def __init__(self, client, pairing_id, code):
            super().__init__()
            self.signals = self
            captured.update(client=client, pairing_id=pairing_id, code=code)

    monkeypatch.setattr(ui, "_PairingExchangeTask", FakePairingTask)
    QTest.mouseClick(window.pair_code_button, Qt.MouseButton.LeftButton)

    assert captured["client"] is window.client
    assert captured["pairing_id"] == "pairing-123"
    assert captured["code"] == "332845"
    assert len(window.api_pool.tasks) == 1


def test_run_form_entry_queues_create_task_with_metadata(tmp_path, monkeypatch):
    _application, window = make_window(tmp_path)
    window._set_enabled(True)
    window.run_name.setText("")
    QTest.keyClicks(window.run_name, "20260914-gui-test")
    window.barcode_values.item(0).setSelected(True)
    window.sample_metadata.item(0, 1).setText("2026-MIN-0001")
    window.sample_metadata.item(0, 2).setText("OLN-1")
    window.folder.setText(str(tmp_path))

    monkeypatch.setattr(window, "_run_preflight", lambda show_success=False: True)
    QTest.mouseClick(window.create_button, Qt.MouseButton.LeftButton)

    assert len(window.api_pool.tasks) == 1
    window.api_pool.tasks[0].operation()
    assert window.client.create_run_calls == [(
        "20260914-gui-test",
        {
            "barcode_kit": "SQK-RBK114-24",
            "barcode_values": [1],
            "lab_name": "OLC",
            "reference_database": {},
            "sample_metadata": {
                "samples": [{
                    "barcode": 1,
                    "seqid": "2026-MIN-0001",
                    "olnid": "OLN-1",
                }],
            },
        },
    )]


def test_barcode_selector_is_a_compact_tiled_multi_select(tmp_path):
    _application, window = make_window(tmp_path)

    assert window.barcode_values.viewMode().name == "IconMode"
    assert window.barcode_values.gridSize().width() == 58
    assert window.barcode_values.gridSize().height() == 34
    assert window.barcode_values.height() == 76


def test_metadata_table_uses_equal_wide_seqid_and_olnid_columns(tmp_path):
    _application, window = make_window(tmp_path)

    header = window.sample_metadata.horizontalHeader()
    assert header.sectionSize(1) == header.sectionSize(2)
    assert header.sectionSize(0) < header.sectionSize(1)


def test_metadata_table_copies_and_pastes_tsv(tmp_path):
    _application, window = make_window(tmp_path)
    window._set_enabled(True)
    window.barcode_values.item(0).setSelected(True)
    window.barcode_values.item(1).setSelected(True)
    table = window.sample_metadata
    table.item(0, 1).setText("2026-MIN-0001")
    table.item(0, 2).setText("OLN-1")
    table.setCurrentCell(0, 1)
    table.item(0, 1).setSelected(True)
    table.item(0, 2).setSelected(True)
    QTest.keyClick(table, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert QApplication.clipboard().text() == "2026-MIN-0001\tOLN-1"

    QApplication.clipboard().setText("2026-MIN-0001\tOLN-1\n2026-MIN-0002\tOLN-2")
    table.setCurrentCell(0, 1)
    QTest.keyClick(table, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
    assert table.item(0, 1).text() == "2026-MIN-0001"
    assert table.item(0, 2).text() == "OLN-1"
    assert table.item(1, 1).text() == "2026-MIN-0002"
    assert table.item(1, 2).text() == "OLN-2"


def test_processing_details_are_collapsed_by_default(tmp_path):
    _application, window = make_window(tmp_path)

    assert window.processing_details_section.content.isHidden()
    assert window.processing_details_toggle.arrowType() == Qt.ArrowType.RightArrow

    window.processing_details_toggle.click()
    assert not window.processing_details_section.content.isHidden()
    assert window.processing_details_toggle.arrowType() == Qt.ArrowType.DownArrow


def test_diagnostic_messages_are_copyable(tmp_path):
    _application, window = make_window(tmp_path)

    for label in (
        window.status_label,
        window.run_details,
        window.processing_details,
        window.result_status,
        window.result_link,
        window.report_status,
    ):
        assert label.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse
        assert label.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByKeyboard


def test_status_report_is_rendered_before_run_completion(tmp_path):
    _application, window = make_window(tmp_path)
    window.run_id = 42

    window._status_succeeded({
        "workflow_state": "processing",
        "processing": {
            "status": "processing",
            "report": {
                "iterations": [
                    {"iteration": 1, "rows": [{"SEQID": "2026-MIN-0001", "stx1": 2}]},
                ],
            },
        },
    })

    assert window.live_iteration.count() == 1
    assert window.report_table.item(0, 0).text() == "2026-MIN-0001"
    assert window.status_label.text() == "Run state: processing | Processing: processing"


def test_report_generations_are_retained(tmp_path):
    _application, window = make_window(tmp_path)

    window.run_id = 42
    window._remember_report({
        "iterations": [{"iteration": 1, "rows": [{"SEQID": "2026-MIN-0001"}]}],
    })
    window._remember_report({
        "iterations": [{"iteration": 2, "rows": [{"SEQID": "2026-MIN-0002"}]}],
    })

    assert [window.live_iteration.itemData(index) for index in range(window.live_iteration.count())] == [1, 2]
    window.live_iteration.setCurrentIndex(0)
    assert window.report_table.item(0, 0).text() == "2026-MIN-0001"
    window.live_iteration.setCurrentIndex(1)
    assert window.report_table.item(0, 0).text() == "2026-MIN-0002"


def test_finalize_stops_file_intake_before_requesting_termination(tmp_path, monkeypatch):
    _application, window = make_window(tmp_path)
    window.run_id = 42
    window._accepting_files = True
    window.scanner = object()

    class IdleUploader:
        is_idle = True

        def __init__(self):
            self.closed = False

        def close(self, wait=False):
            self.closed = True

    uploader = IdleUploader()
    window.uploader = uploader
    monkeypatch.setattr(ui.QMessageBox, "question", lambda *args: ui.QMessageBox.StandardButton.Yes)
    window._finalize()

    assert not window._accepting_files
    assert window.scanner is None
    assert uploader.closed
    assert window.uploader is None
    assert len(window.api_pool.tasks) == 1


def test_report_renders_iterations_and_threshold_colours(tmp_path):
    _application, window = make_window(tmp_path)

    window.run_id = 42
    window._render_report({
        "iterations": [
            {
                "iteration": 1,
                "rows": [{"SEQID": "2026-MIN-0001", "stx1": 1, "Coverage": 8.0}],
            },
            {
                "iteration": 2,
                "rows": [{"SEQID": "2026-MIN-0001", "stx1": 4, "Coverage": 3.0}],
            },
        ],
    })

    assert window.live_iteration.count() == 2
    assert window.report_table.item(0, 0).text() == "2026-MIN-0001"
    assert window.report_table.item(0, 2).background().color().name() == "#b7d8ef"
    window.live_iteration.setCurrentIndex(1)
    assert window.report_table.item(0, 3).text() == "3.0"


def test_byte_format_uses_smallest_readable_unit():
    assert ui._format_bytes(24_120) == "23.6 KB"
    assert ui._format_bytes(1_048_576) == "1.0 MB"


def test_summary_query_supports_boolean_fields_and_wildcards():
    matching = {"SEQID": "2026-MIN-0001", "OLN ID": "OLN-1", "stx1": "2"}
    other = {"SEQID": "2026-MIN-0002", "OLN ID": "OLN-2", "stx1": "0"}
    predicate = ui._compile_summary_query("SEQID:2026-MIN-000? AND (stx1:2 OR OLNID:OLN-3) AND NOT OLNID:OLN-2")
    assert predicate(matching)
    assert not predicate(other)
    assert ui._compile_summary_query('"MIN-0001"')(matching)
    assert ui._compile_summary_query("")(other)


def test_summary_query_rejects_incomplete_expressions():
    import pytest
    for query in ('SEQID:', 'stx1 AND', 'stx1 OR', '(stx1', '"unfinished', 'AND stx1'):
        with pytest.raises(ValueError):
            ui._compile_summary_query(query)


def test_report_iterations_accepts_mapping_and_discards_empty_rows():
    assert ui._report_iterations({"iterations": {1: [{"SEQID": "A"}], 2: [None, "bad"]}}) == [(1, [{"SEQID": "A"}])]
    assert ui._report_iterations(None) == []


def test_duration_and_processing_details_formatting():
    assert ui._format_duration(0) == "0m 0s"
    assert ui._format_duration(3661) == "1h 1m"
    assert "Status: processing" in ui._format_processing_details({"status": "processing"})


def test_sample_metadata_requires_unique_well_formed_seqids(tmp_path):
    import pytest
    _application, window = make_window(tmp_path)
    for index in (0, 1):
        window.barcode_values.item(index).setSelected(True)
    window.sample_metadata.item(0, 1).setText("invalid")
    with pytest.raises(ValueError, match="SEQID must use"):
        window._sample_metadata_payload()
    window.sample_metadata.item(0, 1).setText("2026-MIN-0001")
    window.sample_metadata.item(1, 1).setText("2026-MIN-0001")
    with pytest.raises(ValueError, match="unique"):
        window._sample_metadata_payload()
    window.sample_metadata.item(1, 1).setText("2026-MIN-0002")
    assert [sample["barcode"] for sample in window._sample_metadata_payload()["samples"]] == [1, 2]


def test_barcode_selection_preserves_existing_metadata(tmp_path):
    _application, window = make_window(tmp_path)
    window.barcode_values.item(0).setSelected(True)
    window.sample_metadata.item(0, 1).setText("2026-MIN-0001")
    window.barcode_values.item(1).setSelected(True)
    assert window.sample_metadata.item(0, 1).text() == "2026-MIN-0001"
    window.barcode_values.item(1).setSelected(False)
    assert window.sample_metadata.rowCount() == 1
    assert window.sample_metadata.item(0, 1).text() == "2026-MIN-0001"


def test_invalid_report_query_keeps_last_visible_rows(tmp_path):
    _application, window = make_window(tmp_path)
    window.run_id = 42
    window._remember_report({"iterations": [{"iteration": 1, "rows": [
        {"SEQID": "2026-MIN-0001", "stx1": 2},
        {"SEQID": "2026-MIN-0002", "stx1": 0},
    ]}]})
    window.report_filter.setText("SEQID:2026-MIN-0001")
    assert [window.report_table.isRowHidden(i) for i in range(2)] == [False, True]
    window.report_filter.setText("SEQID:2026-MIN-0001 AND")
    assert [window.report_table.isRowHidden(i) for i in range(2)] == [False, True]
    assert window.query_feedback.text().startswith("Query error:")


def test_report_status_filter_detected_and_not_detected(tmp_path):
    _application, window = make_window(tmp_path)
    window.run_id = 42
    window._remember_report({"iterations": [{"iteration": 1, "rows": [
        {"SEQID": "A", "stx1": 2}, {"SEQID": "B", "stx1": 0},
    ]}]})
    window.report_status_filter.setCurrentText("Detected")
    assert [window.report_table.isRowHidden(i) for i in range(2)] == [False, True]
    window.report_status_filter.setCurrentText("Not detected")
    assert [window.report_table.isRowHidden(i) for i in range(2)] == [True, False]


def test_live_iteration_navigation_tracks_latest_and_previous(tmp_path):
    _application, window = make_window(tmp_path)
    window.run_id = 42
    for number in (1, 2):
        window._remember_report({"iterations": [{"iteration": number, "rows": [{"SEQID": str(number)}]}]})
    assert window.live_iteration.currentData() == 2
    assert window.live_previous_button.isEnabled()
    window._move_live_iteration(-1)
    assert window.live_iteration.currentData() == 1
    assert window.live_next_button.isEnabled()
    window._remember_report({"iterations": [{"iteration": 3, "rows": [{"SEQID": "3"}]}]})
    assert window.live_iteration.currentData() == 1


def test_finalize_refuses_pending_files(tmp_path, monkeypatch):
    _application, window = make_window(tmp_path)
    window.run_id = 42
    window._accepting_files = True
    window.store.add_pending(42, "sample.pod5", "sample.pod5", 12)
    errors = []
    monkeypatch.setattr(window, "_show_error", errors.append)
    window._finalize()
    assert len(errors) == 1
    assert "Resolve pending or failed uploads" in errors[0]
    assert window._accepting_files
    assert window.api_pool.tasks == []


def test_target_report_requires_latest_cached_iteration(tmp_path, monkeypatch):
    _application, window = make_window(tmp_path)
    window.run_id = 42
    window._remember_report({"iterations": [{"iteration": 1, "rows": [{"SEQID": "A"}]}]})
    errors = []
    monkeypatch.setattr(window, "_show_error", errors.append)
    window._generate_target_report()
    assert errors == ["The latest iteration is not cached yet."]
    assert window.api_pool.tasks == []


def test_headless_finalize_bypasses_dialog_but_preserves_pending_guard(tmp_path, monkeypatch):
    _application = QApplication.instance() or QApplication([])
    window = ui.MainWindow(FakeClient(), QueueStore(tmp_path / "queue.sqlite3"),
                           test_mode=True, test_directory=tmp_path, headless_test=True)
    window.api_pool = CapturePool()
    window.run_id = 42
    window._accepting_files = True
    window.store.add_pending(42, "sample.pod5", "sample.pod5", 12)
    def forbidden(*args, **kwargs):
        raise AssertionError("headless mode must not open a confirmation dialog")
    monkeypatch.setattr(ui.QMessageBox, "question", forbidden)
    window._finalize()
    assert window.api_pool.tasks == []
    assert window._accepting_files
    assert "Resolve pending or failed uploads" in window.headless_error


def test_headless_error_is_recorded_without_modal_dialog(tmp_path, monkeypatch):
    _application = QApplication.instance() or QApplication([])
    window = ui.MainWindow(FakeClient(), QueueStore(tmp_path / "queue.sqlite3"),
                           test_mode=True, test_directory=tmp_path, headless_test=True)
    monkeypatch.setattr(ui.QDialog, "exec", lambda *a: (_ for _ in ()).throw(
        AssertionError("headless error must not open a dialog")))
    window._show_error("upload failed")
    assert window.headless_error == "upload failed"


def test_floating_header_resize_event_does_not_recurse(tmp_path):
    _application, window = make_window(tmp_path)
    window.show()
    for _ in range(4):
        window._section_scroll.viewport().resize(650, 450)
        _application.processEvents()
    assert window._section_scroll.viewport().width() == 650


def test_task_lifetime_and_queued_cleanup(tmp_path):
    application, window = make_window(tmp_path)
    task = ui._ApiTask(lambda: {"ok": True})
    assert not task.autoDelete()
    results = []
    task.succeeded.connect(results.append)
    window._start_api_task(task)
    assert id(task) in window._pool_tasks
    task.run()
    for _ in range(10):
        application.processEvents()
        if id(task) not in window._pool_tasks:
            break
    assert results == [{"ok": True}]
    assert id(task) not in window._pool_tasks


def test_status_callback_ignores_previous_run_generation(tmp_path):
    _application, window = make_window(tmp_path)
    window.run_id = 42
    window._tick()
    task = window.api_pool.tasks[-1]
    window._run_generation += 1
    window.run_id = 43
    task.succeeded.emit({"workflow_state": "complete"})
    _application.processEvents()
    assert window._last_status is None


def test_closing_blocks_new_api_submissions(tmp_path):
    _application, window = make_window(tmp_path)
    window._closing = True
    import pytest
    with pytest.raises(RuntimeError, match="closing"):
        window._start_api_task(ui._ApiTask(lambda: {}))
