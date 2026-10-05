from __future__ import annotations

from datetime import datetime, timezone
import json
import html
import logging
import csv
import hashlib
import platform
import shutil
import zipfile
import os
from pathlib import Path
import time
import webbrowser
import re

from PySide6.QtCore import QFileSystemWatcher, QObject, QRunnable, QThreadPool, QTimer, QSize, QRectF, QPoint, Qt, Signal, QEvent
from PySide6.QtGui import QColor, QKeySequence, QPixmap, QCursor, QImageReader, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QFileDialog, QComboBox, QFormLayout, QGridLayout,
    QDialog, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListView, QListWidget, QCheckBox,
    QMainWindow, QMessageBox, QPushButton, QPlainTextEdit, QProgressBar,
    QScrollArea, QSpinBox, QTabWidget, QTableWidget, QTableWidgetItem,
    QToolButton, QVBoxLayout, QWidget, QSizePolicy, QToolTip,
)

from .api import FoodPortClient, FoodPortError
from .reports import LAB_ADDRESSES, COVERAGE_SUFFICIENT_THRESHOLD, build_iteration_report, generate_target_report, load_report, repository_asset
from .scanner import StablePod5Scanner
from .storage import QueueStore
from .test_mode import (
    DEFAULT_BARCODES,
    DEFAULT_SAMPLE_METADATA,
    LocalWaveController,
    seed_cloud_waves,
)
from .uploader import UploadCoordinator
from .version import __version__


logger = logging.getLogger("nanopore_gui.ui")


class _ApiTask(QObject, QRunnable):
    succeeded = Signal(dict)
    failed = Signal(Exception)

    def __init__(self, operation):
        QObject.__init__(self)
        QRunnable.__init__(self)
        self.operation = operation

    def run(self):
        try:
            self.succeeded.emit(self.operation())
        except Exception as exc:
            if not (
                getattr(self, "suppress_not_published_log", False)
                and isinstance(exc, FoodPortError)
                and exc.status == 404
            ):
                logger.exception("background_operation_failed")
            self.failed.emit(exc)


class _StatusTask(_ApiTask):
    def __init__(self, client: FoodPortClient, run_id: int):
        super().__init__(lambda: client.status(run_id))


class _LatestResultTask(_ApiTask):
    def __init__(self, client: FoodPortClient, run_id: int):
        super().__init__(lambda: client.latest_result(run_id))


class _IterationResultTask(_ApiTask):
    def __init__(
        self,
        client: FoodPortClient,
        run_id: int,
        iteration: int,
        report_root: Path,
        report_context: dict,
    ):
        self.iteration = iteration
        self.suppress_not_published_log = True

        def operation():
            result = client.iteration_result(run_id, iteration)
            destination = (
                report_root
                / "run-{0}".format(run_id)
                / "iteration-{0:06d}".format(iteration)
            )
            csv_files = client.download_iteration_csvs(result, destination)
            report = build_iteration_report(
                iteration,
                csv_files,
                destination,
                context=report_context,
            )
            result = dict(result)
            result["report"] = report
            result["report_directory"] = str(destination)
            result["report_manifest_path"] = report.get("manifest_path")
            return result

        super().__init__(operation)


class _PairingStartTask(_ApiTask):
    def __init__(self, client: FoodPortClient):
        super().__init__(client.start_pairing)


class _PairingExchangeTask(_ApiTask):
    def __init__(self, client: FoodPortClient, pairing_id: str, code: str):
        super().__init__(lambda: client.exchange_pairing(pairing_id, code) or {})


class _CloudSeedTask(_ApiTask):
    progress = Signal(str)

    def __init__(
        self,
        client: FoodPortClient,
        run_id: int,
        wait_seconds: float,
        auto_finalize: bool,
    ):
        import threading
        self.cancel_event = threading.Event()
        super().__init__(
            lambda: seed_cloud_waves(
                client,
                run_id,
                wait_seconds=wait_seconds,
                auto_finalize=auto_finalize,
                progress=self.progress.emit,
                cancel_event=self.cancel_event,
            )
        )

    def cancel(self):
        self.cancel_event.set()


class _FastToolTipFilter(QObject):
    """Show native tooltips quickly while avoiding repeated flicker."""

    def __init__(self, parent=None, delay_ms=250):
        super().__init__(parent)
        self.delay_ms = delay_ms
        self._widget = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._show_pending)

    def watch(self, widget):
        if widget is not None:
            widget.installEventFilter(self)

    def eventFilter(self, watched, event):
        event_type = event.type()
        if event_type == QEvent.Type.Enter:
            text = watched.toolTip()
            if text:
                self._widget = watched
                self._timer.start(self.delay_ms)
        elif event_type in (QEvent.Type.Leave, QEvent.Type.Hide, QEvent.Type.Destroy):
            if watched is self._widget:
                self._timer.stop()
                self._widget = None
                QToolTip.hideText()
        elif event_type == QEvent.Type.ToolTip and watched.toolTip():
            # Suppress the slower platform tooltip; this filter shows it instead.
            return True
        return super().eventFilter(watched, event)

    def _show_pending(self):
        widget = self._widget
        if widget is not None and widget.isVisible() and widget.underMouse():
            QToolTip.showText(QCursor.pos(), widget.toolTip(), widget)


class _MetadataTable(QTableWidget):
    def keyPressEvent(self, event):
        if event.matches(QKeySequence.StandardKey.Copy):
            self._copy_selection()
            return
        if event.matches(QKeySequence.StandardKey.Paste):
            self._paste_clipboard()
            return
        super().keyPressEvent(event)

    def _copy_selection(self):
        indexes = self.selectedIndexes()
        if not indexes:
            return
        rows = range(min(index.row() for index in indexes), max(index.row() for index in indexes) + 1)
        columns = range(min(index.column() for index in indexes), max(index.column() for index in indexes) + 1)
        values = []
        for row in rows:
            values.append("\t".join(
                (self.item(row, column).text() if self.item(row, column) else "")
                for column in columns
            ))
        QApplication.clipboard().setText("\n".join(values))

    def _paste_clipboard(self):
        text = QApplication.clipboard().text()
        if not text:
            return
        start = self.currentIndex()
        if not start.isValid():
            start = self.model().index(0, 0)
        values = [line.split("\t") for line in text.rstrip("\r\n").splitlines()]
        for row_offset, row_values in enumerate(values):
            row = start.row() + row_offset
            if row >= self.rowCount():
                break
            for column_offset, value in enumerate(row_values):
                column = start.column() + column_offset
                if column >= self.columnCount():
                    break
                item = self.item(row, column)
                if item is None:
                    item = QTableWidgetItem()
                    self.setItem(row, column, item)
                item.setText(value)


class _CollapsibleSection(QWidget):
    """Compact section with a keyboard-accessible disclosure button."""

    def __init__(self, title, content, expanded=False, parent=None):
        super().__init__(parent)
        self.content = content
        self.toggle = QToolButton()
        self.toggle.setText(title)
        self.toggle.setCheckable(True)
        self.toggle.setChecked(bool(expanded))
        self.toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
        )
        self.toggle.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.toggle.setStyleSheet(
            "QToolButton { text-align: left; padding: 7px 9px; "
            "font-weight: 700; color: #176b5b; background: #eaf2ef; "
            "border: 1px solid #cbd8d3; border-radius: 5px; }"
        )
        self.toggle.toggled.connect(self._set_expanded)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self.toggle)
        layout.addWidget(content)
        content.setVisible(bool(expanded))

    def _set_expanded(self, expanded):
        self.content.setVisible(expanded)
        self.toggle.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
        )
        self.updateGeometry()

    def set_expanded(self, expanded):
        self.toggle.setChecked(bool(expanded))


class MainWindow(QMainWindow):
    test_progress = Signal(str)
    test_finished = Signal()
    test_failed = Signal(Exception)

    def __init__(
        self,
        client: FoodPortClient,
        store: QueueStore,
        test_mode: bool = False,
        test_directory: Path | None = None,
        test_wave_delay: float = 150.0,
        test_auto_finalize: bool = True,
        view_run_id: int | None = None,
        headless_test: bool = False,
    ):
        super().__init__()
        self.client = client
        self.store = store
        self.test_mode = test_mode
        self.headless_test = headless_test
        self.headless_error = None
        self.view_run_id = view_run_id
        self.test_directory = test_directory
        self.test_wave_delay = test_wave_delay
        self.test_auto_finalize = test_auto_finalize
        self.test_controller = None
        self._cloud_seed_task = None
        self._test_source_directory = test_directory
        self.test_progress.connect(self._test_progress_message)
        self.test_finished.connect(self._test_local_finished)
        self.test_failed.connect(self._test_failed)
        self.run_id: int | None = None
        self.run_started = None
        self.scanner = None
        self.uploader = None
        self._accepting_files = False
        self.watcher = QFileSystemWatcher(self)
        self.watcher.directoryChanged.connect(self._tick)
        self.api_pool = QThreadPool(self)
        self._status_in_flight = False
        self._result_in_flight = False
        self._iteration_result_tasks = {}
        self._target_report_task = None
        self._report_pages = []
        self._report_documents = {}
        self._report_artifacts = {}
        self._newest_iteration_seen = None
        self.report_root = (
            Path(os.getenv("APPDATA", Path.home()))
            / "NanoporeCloudGUI"
            / "reports"
        )
        self.report_root.mkdir(parents=True, exist_ok=True)
        self._login_task = None
        self._pairing_task = None
        self._pairing_id = None
        self._create_task = None
        self._finalize_task = None
        self._last_status = None
        self._last_state_marker = None
        self._last_uploaded = 0
        self._last_measurement = time.monotonic()
        self._bytes_per_second = 0.0
        self._last_new_file_at = None
        self._setup_path = Path(os.getenv("APPDATA", Path.home())) / "NanoporeCloudGUI" / "setup-draft.json"
        self._setup_path.parent.mkdir(parents=True, exist_ok=True)
        self._reference_info = {}
        self.setWindowTitle("PorePort")
        self.setMinimumSize(560, 420)
        self._live_pixmap = QPixmap()
        self._auto_size_live_result = True
        screen = QApplication.primaryScreen()
        available = screen.availableGeometry() if screen else None
        preferred_width = 1280
        if self.view_run_id is not None:
            cached = sorted(
                (self.report_root / "run-{0}".format(self.view_run_id)).glob(
                    "iteration-*/iteration-*-summary.png"
                )
            )
            if cached:
                dimensions = QImageReader(str(cached[-1])).size()
                if dimensions.isValid():
                    preferred_width = max(preferred_width, dimensions.width() + 100)
        if available:
            preferred_width = min(preferred_width, max(560, available.width() - 48))
            # Start at roughly three-quarters of the usable screen height,
            # leaving space for the taskbar/dock and window decorations.
            preferred_height = min(
                max(420, round(available.height() * 0.75)),
                max(420, available.height() - 48),
            )
        else:
            preferred_height = 720
        self.resize(preferred_width, preferred_height)
        self._build()
        if test_mode:
            self._apply_test_preset(test_directory)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(2000)

    def _build(self):
        root = QWidget()
        layout = QVBoxLayout(root)
        root.setMinimumSize(0, 0)
        root.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.setContentsMargins(14, 7, 14, 10)
        layout.setSpacing(7)

        heading = QLabel("PorePort")
        heading.setObjectName("heading")
        subheading = QLabel("Upload POD5 files. Track results as you sequence.")
        subheading.setObjectName("subheading")
        heading_column = QVBoxLayout()
        heading_column.setContentsMargins(0, 0, 0, 0)
        heading_column.setSpacing(0)
        heading_column.addWidget(heading)
        heading_column.addWidget(subheading)

        actions_column = QVBoxLayout()
        actions_column.setContentsMargins(0, 0, 0, 0)
        actions_column.setSpacing(3)
        wordmark = repository_asset("CFIA_logo.png")
        if wordmark:
            source = QPixmap(str(wordmark))
            if not source.isNull():
                scaled = source.scaled(
                    235, 36, Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                rounded = QPixmap(scaled.size())
                rounded.fill(Qt.GlobalColor.transparent)
                painter = QPainter(rounded)
                painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                clip = QPainterPath()
                clip.addRoundedRect(QRectF(rounded.rect()), 6, 6)
                painter.setClipPath(clip)
                painter.drawPixmap(0, 0, scaled)
                painter.end()
                wordmark_label = QLabel()
                wordmark_label.setPixmap(rounded)
                wordmark_label.setToolTip("Canadian Food Inspection Agency")
                wordmark_row = QHBoxLayout()
                wordmark_row.setContentsMargins(0, 0, 0, 0)
                wordmark_row.addStretch()
                wordmark_row.addWidget(wordmark_label)
                actions_column.addLayout(wordmark_row)
        top_actions = QHBoxLayout()
        top_actions.setContentsMargins(0, 0, 0, 0)
        top_actions.setSpacing(5)
        self.help_button = QPushButton("Workflow help")
        self.help_button.clicked.connect(self._show_help)
        self.about_button = QPushButton("About")
        self.about_button.clicked.connect(self._show_about)
        self.diagnostics_button = QPushButton("Export diagnostics")
        self.diagnostics_button.clicked.connect(self._export_diagnostics)
        top_actions.addWidget(self.help_button)
        top_actions.addWidget(self.about_button)
        top_actions.addWidget(self.diagnostics_button)
        actions_column.addLayout(top_actions)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(12)
        header.addLayout(heading_column)
        header.addStretch()
        header.addLayout(actions_column)
        layout.addLayout(header)

        self.login_box = QGroupBox("FoodPort access")
        login = QFormLayout(self.login_box)
        self.pair_button = QPushButton("Sign in with FoodPort in browser")
        self.pair_button.clicked.connect(self._start_pairing)
        self.pair_code = QLineEdit()
        self.pair_code.setPlaceholderText("Enter the one-time code shown by FoodPort")
        self.pair_code_button = QPushButton("Complete pairing")
        self.pair_code_button.clicked.connect(self._complete_pairing)
        login.addRow("Authentication", self.pair_button)
        login.addRow("Pairing code", self.pair_code)
        login.addRow("", self.pair_code_button)
        self.login_box.setTitle("")
        self.login_section = _CollapsibleSection(
            "FoodPort access", self.login_box, expanded=True
        )
        layout.addWidget(self.login_section)

        self.run_box = QGroupBox("Run setup")
        run_form = QFormLayout(self.run_box)
        self.run_form = run_form
        self.run_name = QLineEdit(datetime.now().strftime("%y%m%d-nanopore"))
        self.barcode_kit = QComboBox()
        self.barcode_kit.addItem("SQK-RBK114-24")
        self.lab_name = QComboBox()
        self.lab_name.addItems(["OLC", "FFFM", "GTA", "BUR", "DAR", "CAL", "STH"])
        self.barcode_values = QListWidget()
        self.barcode_values.setSelectionMode(QAbstractItemView.MultiSelection)
        self.barcode_values.setViewMode(QListView.ViewMode.IconMode)
        self.barcode_values.setFlow(QListView.Flow.LeftToRight)
        self.barcode_values.setWrapping(True)
        self.barcode_values.setResizeMode(QListView.ResizeMode.Adjust)
        self.barcode_values.setMovement(QListView.Movement.Static)
        self.barcode_values.setUniformItemSizes(True)
        self.barcode_values.setGridSize(QSize(58, 34))
        self.barcode_values.setFixedHeight(76)
        self.barcode_values.setStyleSheet(
            """
            QListWidget {
                background: #f4f7f5;
                border: 1px solid #cbd7d1;
                border-radius: 4px;
                padding: 4px;
            }
            QListWidget::item {
                color: #25433a;
                background: #ffffff;
                border: 1px solid #b8c9c1;
                border-radius: 4px;
                padding: 4px;
            }
            QListWidget::item:selected {
                color: #ffffff;
                background: #176b5b;
                border-color: #0e5347;
            }
            """
        )
        for number in range(1, 25):
            self.barcode_values.addItem(f"{number:02d}")
        self.barcode_values.itemSelectionChanged.connect(self._sync_sample_metadata_rows)
        barcode_actions = QHBoxLayout()
        for text, operation in (("Select all", self._select_all_barcodes), ("Clear all", self._clear_barcodes), ("Invert", self._invert_barcodes)):
            button = QPushButton(text); button.clicked.connect(operation); barcode_actions.addWidget(button)
        barcode_actions.addStretch()
        self.sample_metadata = _MetadataTable(0, 3)
        self.sample_metadata.setHorizontalHeaderLabels(["Barcode", "SEQID", "OLNID"])
        metadata_header = self.sample_metadata.horizontalHeader()
        metadata_header.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        metadata_header.resizeSection(0, 72)
        metadata_header.resizeSection(1, 180)
        metadata_header.resizeSection(2, 180)
        self.sample_metadata.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.sample_metadata.setSelectionBehavior(QAbstractItemView.SelectItems)
        self.sample_metadata.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.sample_metadata.horizontalHeader().setSectionsClickable(True)
        self.sample_metadata.verticalHeader().setSectionsClickable(True)
        self.sample_metadata.horizontalHeader().sectionClicked.connect(
            self.sample_metadata.selectColumn
        )
        self.sample_metadata.verticalHeader().sectionClicked.connect(
            self.sample_metadata.selectRow
        )
        metadata_actions = QHBoxLayout()
        self.import_metadata_button = QPushButton("Import CSV")
        self.import_metadata_button.clicked.connect(self._import_metadata_csv)
        self.export_metadata_button = QPushButton("Export CSV")
        self.export_metadata_button.clicked.connect(self._export_metadata_csv)
        metadata_actions.addWidget(self.import_metadata_button)
        metadata_actions.addWidget(self.export_metadata_button)
        metadata_actions.addStretch()
        self.folder = QLineEdit()
        self.folder_button = QPushButton("Choose folder")
        self.folder_button.clicked.connect(self._choose_folder)
        folder_row = QHBoxLayout()
        folder_row.addWidget(self.folder)
        folder_row.addWidget(self.folder_button)
        self.reference_path = QLineEdit()
        default_reference = repository_reference_path()
        self.reference_path.setText(str(default_reference) if default_reference else "")
        self.reference_button = QPushButton("Choose database")
        self.reference_button.clicked.connect(self._choose_reference)
        self.reference_info = QLabel("Reference database not validated.")
        self.reference_info.setWordWrap(True)
        reference_row = QHBoxLayout(); reference_row.addWidget(self.reference_path); reference_row.addWidget(self.reference_button)
        self.preflight_list = QListWidget(); self.preflight_list.setMaximumHeight(100)
        self.preflight_button = QPushButton("Run preflight checks")
        self.preflight_button.clicked.connect(lambda: self._run_preflight(show_success=True))
        setup_actions = QHBoxLayout()
        self.save_setup_button = QPushButton("Save setup")
        self.save_setup_button.clicked.connect(self._save_setup)
        self.load_setup_button = QPushButton("Load setup")
        self.load_setup_button.clicked.connect(self._load_setup)
        setup_actions.addWidget(self.save_setup_button)
        setup_actions.addWidget(self.load_setup_button)
        setup_actions.addStretch()
        self.create_button = QPushButton("Create run")
        self.create_button.clicked.connect(self._create_run)
        self.stability = QSpinBox()
        self.stability.setRange(0, 3600)
        self.stability.setValue(180)
        self.stability.setSuffix(" s")
        self.workers = QSpinBox()
        self.workers.setRange(1, 8)
        self.workers.setValue(3)
        run_form.addRow("Run name", self.run_name)
        run_form.addRow("Barcode kit", self.barcode_kit)
        run_form.addRow("Laboratory", self.lab_name)
        run_form.addRow("Barcode selection", barcode_actions)
        run_form.addRow("Barcodes", self.barcode_values)
        run_form.addRow("Sample metadata (SEQID/OLNID)", self.sample_metadata)
        run_form.addRow("Metadata actions", metadata_actions)
        run_form.addRow("POD5 directory", folder_row)
        run_form.addRow("Reference database", reference_row)
        run_form.addRow("Database information", self.reference_info)
        run_form.addRow("Setup", setup_actions)
        run_form.addRow("Preflight", self.preflight_button)
        run_form.addRow("Checks", self.preflight_list)
        run_form.addRow("File stability", self.stability)
        run_form.addRow("Concurrent uploads", self.workers)
        run_form.addRow("", self.create_button)
        self.run_box.setTitle("")
        self.run_section = _CollapsibleSection(
            "Run setup", self.run_box, expanded=False
        )
        layout.addWidget(self.run_section)

        status_content = QWidget()
        status_layout = QVBoxLayout(status_content)
        status_layout.setContentsMargins(0, 0, 0, 0)
        status_layout.setSpacing(8)
        self.status_label = QLabel("Log in to begin.")
        self.status_label.setObjectName("status")
        self._make_copyable(self.status_label)
        status_layout.addWidget(self.status_label)
        self.run_details = QLabel("No active run.")
        self.run_details.setObjectName("subheading")
        self._make_copyable(self.run_details)
        status_layout.addWidget(self.run_details)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        status_layout.addWidget(self.progress)

        grid = QGridLayout()
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(4)
        self.file_count = self._metric(grid, 0, 0, "Files")
        self.pending_count = self._metric(grid, 0, 1, "Pending")
        self.failed_count = self._metric(grid, 0, 2, "Failed")
        self.uploaded_size = self._metric(grid, 0, 3, "Uploaded")
        self.remaining_size = self._metric(grid, 1, 0, "Remaining")
        self.transfer_rate = self._metric(grid, 1, 1, "Transfer rate")
        self.run_age = self._metric(grid, 1, 2, "Run age")
        self.network = self._metric(grid, 1, 3, "Network")
        self.backlog_eta = self._metric(grid, 2, 0, "Backlog ETA")
        self.processing_status = self._metric(grid, 2, 1, "Processing")
        self.processing_progress = self._metric(grid, 2, 2, "Processed POD5")
        self.last_update = self._metric(grid, 2, 3, "Last update")
        status_layout.addLayout(grid)
        # Use the same disclosure treatment as the neighbouring Failed files
        # subsection, with a distinct inset panel for the advanced details.
        processing_content = QWidget()
        processing_content.setObjectName("processingDetailsPanel")
        processing_layout = QVBoxLayout(processing_content)
        processing_layout.setContentsMargins(14, 12, 14, 12)
        processing_layout.setSpacing(0)
        self.processing_details = QLabel("Processing details unavailable.")
        self.processing_details.setObjectName("processingDetailsText")
        self.processing_details.setWordWrap(True)
        self.processing_details.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self._make_copyable(self.processing_details)
        processing_layout.addWidget(self.processing_details)
        self.processing_details_section = _CollapsibleSection(
            "Processing details (advanced)", processing_content, expanded=False
        )
        self.processing_details_toggle = self.processing_details_section.toggle
        status_layout.addWidget(self.processing_details_section)
        failures = QWidget()
        failures_layout = QVBoxLayout(failures)
        failures_layout.setContentsMargins(4, 4, 4, 4)
        self.failed_files = QListWidget()
        self.failed_files.setMaximumHeight(110)
        self.retry_button = QPushButton("Retry failed uploads")
        self.retry_button.clicked.connect(self._retry_failed)
        failures_layout.addWidget(self.failed_files)
        failures_layout.addWidget(self.retry_button)
        self.failures_section = _CollapsibleSection(
            "Failed files", failures, expanded=False
        )
        status_layout.addWidget(self.failures_section)
        self.status_section = _CollapsibleSection(
            "Run information and status", status_content, expanded=False
        )
        layout.addWidget(self.status_section)

        results = QGroupBox("Live results")
        results_layout = QVBoxLayout(results)
        results_layout.setSpacing(6)
        live_controls = QHBoxLayout()
        live_controls.addWidget(QLabel("Iteration"))
        self.live_iteration = QComboBox()
        self.live_iteration.setMinimumWidth(115)
        self.live_iteration.currentIndexChanged.connect(self._show_live_iteration)
        self.live_previous_button = QPushButton("◀")
        self.live_previous_button.setToolTip("Previous iteration summary")
        self.live_previous_button.setAccessibleName("Previous iteration summary")
        self.live_previous_button.setFixedWidth(36)
        self.live_previous_button.setEnabled(False)
        self.live_previous_button.clicked.connect(
            lambda: self._move_live_iteration(-1)
        )
        self.live_next_button = QPushButton("▶")
        self.live_next_button.setToolTip("Next iteration summary")
        self.live_next_button.setAccessibleName("Next iteration summary")
        self.live_next_button.setFixedWidth(36)
        self.live_next_button.setEnabled(False)
        self.live_next_button.clicked.connect(
            lambda: self._move_live_iteration(1)
        )
        live_controls.addWidget(self.live_previous_button)
        live_controls.addWidget(self.live_iteration)
        live_controls.addWidget(self.live_next_button)
        live_controls.addStretch()
        results_layout.addLayout(live_controls)
        self.result_status = QLabel("Waiting for the first iteration summary.")
        self._make_copyable(self.result_status)
        results_layout.addWidget(self.result_status)
        self.live_summary_image = QLabel("No summary table image available yet.")
        self.live_summary_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.live_summary_image.setWordWrap(True)
        self.live_summary_image.setContentsMargins(12, 8, 12, 8)
        self.live_summary_image.setMinimumWidth(0)
        self.live_summary_image.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.live_summary_scroll = QScrollArea()
        # The empty-state label fills the viewport rather than sizing itself
        # to the text and being clipped by a narrow scroll area.
        self.live_summary_scroll.setWidgetResizable(True)
        self.live_summary_scroll.setMinimumHeight(135)
        self.live_summary_scroll.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.live_summary_scroll.setWidget(self.live_summary_image)
        self.live_tabs = QTabWidget()
        live_summary_page = QWidget()
        live_summary_layout = QVBoxLayout(live_summary_page)
        live_summary_layout.setContentsMargins(0, 0, 0, 0)
        live_summary_layout.addWidget(self.live_summary_scroll)
        self.live_tabs.addTab(live_summary_page, "Summary")
        results_layout.addWidget(self.live_tabs)
        files_content = QWidget()
        files_layout = QVBoxLayout(files_content)
        files_layout.setContentsMargins(4, 4, 4, 4)
        self.result_link = QLabel()
        self.result_link.setOpenExternalLinks(True)
        self._make_copyable(self.result_link)
        self.result_outputs = QListWidget()
        self.result_outputs.setMaximumHeight(110)
        files_layout.addWidget(self.result_link)
        files_layout.addWidget(self.result_outputs)
        self.live_files_section = _CollapsibleSection(
            "Published files", files_content, expanded=False
        )
        results_layout.addWidget(self.live_files_section)
        results.setTitle("")
        self.results_section = _CollapsibleSection(
            "Live results", results, expanded=False
        )
        layout.addWidget(self.results_section)

        self.report_box = QGroupBox("PorePort Report")
        report_layout = QVBoxLayout(self.report_box)
        report_metadata = QGroupBox("Report document fields")
        report_metadata_form = QFormLayout(report_metadata)
        self.report_metadata_form = report_metadata_form
        self.report_lab = QComboBox()
        for index in range(self.lab_name.count()):
            code = self.lab_name.itemText(index)
            self.report_lab.addItem(code, code)
            self.report_lab.setItemData(
                index, LAB_ADDRESSES.get(code, "Address not recorded"),
                Qt.ItemDataRole.ToolTipRole,
            )
        self.report_lab_address = QLabel()
        self.report_lab_address.setWordWrap(True)
        self.report_lab.currentIndexChanged.connect(self._report_lab_changed)
        self.lab_name.currentIndexChanged.connect(self._sync_report_lab_from_run)
        self._sync_report_lab_from_run()
        self.report_state = QComboBox()
        self.report_state.addItems(["Draft", "Under Review", "Approved", "Released", "Superseded"])
        self.report_rdims_document_id = QLineEdit()
        self.report_rdims_document_id.setPlaceholderText("Optional; leave blank until assigned")
        self.reviewer_name = QLineEdit()
        self.reviewer_name.setPlaceholderText("Optional reviewer name")
        self.approval_timestamp = QLineEdit()
        self.approval_timestamp.setPlaceholderText("Optional ISO approval timestamp")
        self.approval_now_button = QPushButton("Set current time")
        self.approval_now_button.clicked.connect(lambda: self.approval_timestamp.setText(datetime.now(timezone.utc).isoformat()))
        approval_row = QHBoxLayout()
        approval_row.addWidget(self.approval_timestamp)
        approval_row.addWidget(self.approval_now_button)
        self.digital_signature = QLineEdit()
        self.digital_signature.setPlaceholderText("Optional signer, certificate ID, or signature reference")
        self.report_fields_lock = QPushButton("Lock report fields")
        self.report_fields_lock.setCheckable(True)
        self.report_fields_lock.toggled.connect(self._set_report_fields_locked)
        report_metadata_form.addRow("Report laboratory", self.report_lab)
        report_metadata_form.addRow("Laboratory address", self.report_lab_address)
        report_metadata_form.addRow("Report state", self.report_state)
        report_metadata_form.addRow("RDIMS document ID", self.report_rdims_document_id)
        report_metadata_form.addRow("Reviewer", self.reviewer_name)
        report_metadata_form.addRow("Approval timestamp", approval_row)
        report_metadata_form.addRow("Digital signature reference", self.digital_signature)
        report_metadata_form.addRow("", self.report_fields_lock)
        self.generate_report_button = QPushButton("Generate report")
        self.generate_report_button.setEnabled(False)
        self.generate_report_button.clicked.connect(self._generate_target_report)
        report_metadata_form.addRow("", self.generate_report_button)
        for field in (self.report_rdims_document_id, self.reviewer_name,
                      self.approval_timestamp, self.digital_signature):
            field.textChanged.connect(lambda _value: self._refresh_target_report())
        self.report_state.currentIndexChanged.connect(
            lambda _index: self._refresh_target_report()
        )
        report_layout.addWidget(report_metadata)
        self.report_status = QLabel("No report generated. Latest iteration only.")
        self._make_copyable(self.report_status)
        report_layout.addWidget(self.report_status)
        self.report_tabs = QTabWidget()
        self.report_tabs.setMinimumHeight(180)

        report_filter_row = QHBoxLayout()
        self.report_filter = QLineEdit()
        self.report_filter.setPlaceholderText(
            'Query: SEQID:2026* AND NOT "-" | stx1 OR stx2 | OLNID:123?'
        )
        self.report_filter.textChanged.connect(self._filter_report_rows)
        self.report_status_filter = QComboBox(); self.report_status_filter.addItems(["All results", "Detected", "Not detected", "Insufficient data"])
        self.report_status_filter.currentIndexChanged.connect(self._filter_report_rows)
        self.export_filtered_button = QPushButton("Export filtered CSV")
        self.export_filtered_button.clicked.connect(self._export_filtered_results)
        report_filter_row.addWidget(self.report_filter); report_filter_row.addWidget(self.report_status_filter); report_filter_row.addWidget(self.export_filtered_button)
        summary_page = QWidget()
        summary_layout = QVBoxLayout(summary_page)
        summary_layout.setContentsMargins(0, 0, 0, 0)
        summary_layout.addLayout(report_filter_row)
        self.query_feedback = QLabel(
            'Search all columns, or use SEQID:, OLNID:, AND, OR, NOT, ( ), "phrases", ? and * wildcards.'
        )
        self.query_feedback.setWordWrap(True)
        summary_layout.addWidget(self.query_feedback)
        self.report_table = QTableWidget(0, 0)
        self.report_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.report_table.setSelectionBehavior(QAbstractItemView.SelectItems)
        self.report_table.setAlternatingRowColors(True)
        self.report_table.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        summary_layout.addWidget(self.report_table)
        self.summary_table_section = _CollapsibleSection(
            "Summary data table", summary_page, expanded=False
        )
        live_summary_layout.addWidget(self.summary_table_section)
        self._fit_report_table()

        self.coverage_image = QLabel("No coverage figure available.")
        self.coverage_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.coverage_image.setMinimumSize(0, 0)
        self.coverage_image.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        coverage_scroll = QScrollArea()
        coverage_scroll.setWidgetResizable(True)
        coverage_scroll.setWidget(self.coverage_image)
        self.live_tabs.addTab(coverage_scroll, "Coverage")

        self.targets_image = QLabel("No target figure available.")
        self.targets_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.targets_image.setMinimumSize(0, 0)
        self.targets_image.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        targets_scroll = QScrollArea()
        targets_scroll.setWidgetResizable(True)
        targets_scroll.setWidget(self.targets_image)
        self.live_tabs.addTab(targets_scroll, "Targets")
        self.trend_image = QLabel("Cross-iteration trends require multiple iterations.")
        self.trend_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.trend_image.setMinimumSize(0, 0)
        self.trend_image.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        trend_scroll = QScrollArea()
        trend_scroll.setWidgetResizable(True)
        trend_scroll.setWidget(self.trend_image)
        self.live_tabs.addTab(trend_scroll, "Trends")

        files_page = QWidget()
        files_layout = QVBoxLayout(files_page)
        report_actions = QHBoxLayout()
        self.open_pdf_button = QPushButton("Open summary PDF")
        self.open_pdf_button.setEnabled(False)
        self.open_pdf_button.clicked.connect(self._open_selected_pdf)
        self.open_report_button = QPushButton("Open HTML report")
        self.open_report_button.setEnabled(False)
        self.open_report_button.clicked.connect(self._open_selected_report)
        self.open_report_folder_button = QPushButton("Open report folder")
        self.open_report_folder_button.setEnabled(False)
        self.open_report_folder_button.clicked.connect(
            self._open_selected_report_folder
        )
        report_actions.addWidget(self.open_pdf_button)
        report_actions.addWidget(self.open_report_button)
        report_actions.addWidget(self.open_report_folder_button)
        report_actions.addStretch()
        files_layout.addLayout(report_actions)
        self.report_artifacts = QListWidget()
        self.report_artifacts.itemDoubleClicked.connect(
            lambda item: self._open_path(Path(item.data(Qt.ItemDataRole.UserRole)))
        )
        files_layout.addWidget(self.report_artifacts)
        self.report_tabs.addTab(files_page, "Files")
        report_layout.addWidget(self.report_tabs)
        self.report_box.setTitle("")
        self.report_section = _CollapsibleSection(
            "PorePort Report", self.report_box, expanded=False
        )
        layout.addWidget(self.report_section)

        actions = QHBoxLayout()
        self.pause_button = QPushButton("Pause file intake")
        self.pause_button.clicked.connect(self._toggle_file_intake)
        self.open_input_button = QPushButton("Open input folder")
        self.open_input_button.clicked.connect(lambda: self._open_path(Path(self.folder.text())) if self.folder.text() else None)
        self.finalize_button = QPushButton("Finalize run")
        self.finalize_button.clicked.connect(self._finalize)
        self.logout_button = QPushButton("Log out")
        self.logout_button.clicked.connect(self._logout)
        actions.addWidget(self.pause_button)
        actions.addWidget(self.open_input_button)
        actions.addWidget(self.finalize_button)
        actions.addStretch()
        actions.addWidget(self.logout_button)
        layout.addLayout(actions)
        page = QScrollArea()
        page.setObjectName("mainScrollArea")
        page.setWidgetResizable(True)
        page.setMinimumSize(0, 0)
        page.viewport().setMinimumSize(0, 0)
        page.setFrameShape(QScrollArea.Shape.NoFrame)
        page.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        page.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        page.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        page.setWidget(root)
        self.setCentralWidget(page)
        self._setup_floating_headers(page, root)
        self.setStyleSheet("""
            QWidget { background: #f5f7f6; color: #20302d; font-size: 14px; }
            QScrollArea#mainScrollArea { border: 0; background: #f5f7f6; }
            QGroupBox { border: 1px solid #cbd8d3; border-radius: 6px; margin-top: 12px; padding: 16px; background: #ffffff; }
            QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 5px; color: #176b5b; font-weight: 600; }
            QWidget#metricCard { background: #ffffff; border: 1px solid #cbd8d3; border-radius: 6px; }
            QWidget#processingDetailsPanel { background: #f7fbf9; border: 1px solid #cbd8d3; border-radius: 6px; }
            QLabel#processingDetailsText { background: transparent; border: 0; color: #344b45; font-size: 13px; }
            QLabel#metricTitle { background: transparent; border: 0; color: #63726d; font-size: 12px; }
            QLabel#metricValue { background: transparent; border: 0; color: #176b5b; font-size: 16px; font-weight: 700; }
            QLabel#heading { font-size: 21px; font-weight: 700; color: #12483f; }
            QLabel#subheading { color: #63726d; }
            QLabel#status { padding: 12px; background: #e1eee9; border-left: 4px solid #1d876e; }
            QLineEdit { background: #ffffff; border: 1px solid #bdccc6; border-radius: 4px; padding: 7px; }
            QPushButton { background: #176b5b; color: white; border: 0; border-radius: 4px; padding: 9px 16px; font-weight: 600; }
            QPushButton:hover { background: #0e5347; }
            QPushButton:disabled { background: #aab8b3; }
            QProgressBar { height: 12px; border: 0; border-radius: 6px; background: #d8e3df; }
            QProgressBar::chunk { border-radius: 6px; background: #e08c3c; }
        """)
        self._tooltip_filter = _FastToolTipFilter(self, delay_ms=250)
        self._apply_tooltips()
        self.pair_code_button.setEnabled(False)
        self._set_enabled(False)

    def _setup_floating_headers(self, scroll, root):
        """Keep collapsed, off-screen section headers reachable at either edge."""
        self._section_scroll = scroll
        self._floating_headers = {}
        self._floating_update_scheduled = False
        viewport = scroll.viewport()
        for section in (self.status_section, self.results_section,
                        self.report_section):
            button = QToolButton(viewport)
            button.setObjectName("floatingSectionHeader")
            button.setText(section.toggle.text())
            button.setToolButtonStyle(
                Qt.ToolButtonStyle.ToolButtonTextBesideIcon
            )
            button.setArrowType(Qt.ArrowType.RightArrow)
            button.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
            )
            button.setToolTip(
                "Open {0} and scroll to it".format(section.toggle.text())
            )
            button.setAccessibleName(
                "Open {0}".format(section.toggle.text())
            )
            button.setStyleSheet(
                "QToolButton#floatingSectionHeader { text-align: left; "
                "padding: 6px 9px; font-weight: 700; color: #176b5b; "
                "background: #eaf2ef; border: 1px solid #90b4a6; "
                "border-radius: 5px; }"
                "QToolButton#floatingSectionHeader:hover { "
                "background: #d7e9e1; }"
            )
            button.clicked.connect(
                lambda _checked=False, item=section:
                    self._open_floating_section(item)
            )
            section.toggle.toggled.connect(
                lambda _checked: self._schedule_floating_headers()
            )
            button.hide()
            self._floating_headers[section] = button
        scroll.verticalScrollBar().valueChanged.connect(
            self._schedule_floating_headers
        )
        scroll.verticalScrollBar().rangeChanged.connect(
            self._schedule_floating_headers
        )
        viewport.installEventFilter(self)
        root.installEventFilter(self)
        QTimer.singleShot(0, self._update_floating_headers)

    def _schedule_floating_headers(self, *args):
        if not self._floating_update_scheduled:
            self._floating_update_scheduled = True
            QTimer.singleShot(0, self._flush_floating_headers)

    def _flush_floating_headers(self):
        self._floating_update_scheduled = False
        self._update_floating_headers()

    def _open_floating_section(self, section):
        section.set_expanded(True)
        self._floating_headers[section].hide()
        # Opening changes the layout; scroll only after Qt has recalculated it.
        QTimer.singleShot(0, lambda: self._reveal_floating_section(section))

    def _reveal_floating_section(self, section):
        self._section_scroll.ensureWidgetVisible(section.toggle, 0, 12)
        self._update_floating_headers()

    def _update_floating_headers(self):
        if not hasattr(self, "_floating_headers"):
            return
        viewport = self._section_scroll.viewport()
        if not viewport.isVisible() or viewport.width() <= 0:
            for button in self._floating_headers.values():
                button.hide()
            return
        above, below = [], []
        for section, button in self._floating_headers.items():
            button.hide()
            if not section.isVisible() or section.toggle.isChecked():
                continue
            y = section.toggle.mapTo(viewport, QPoint(0, 0)).y()
            height = section.toggle.height()
            if y + height <= 0:
                above.append(button)
            elif y >= viewport.height():
                below.append(button)
        margin, gap, height = 7, 4, 32
        width = max(1, viewport.width() - 2 * margin)
        top = margin
        for button in above:
            if top + height > viewport.height() - margin:
                break
            button.setGeometry(margin, top, width, height)
            button.show()
            button.raise_()
            top += height + gap
        bottom = viewport.height() - margin
        for button in reversed(below):
            if bottom - height < top:
                break
            bottom -= height
            button.setGeometry(margin, bottom, width, height)
            button.show()
            button.raise_()
            bottom -= gap

    def eventFilter(self, watched, event):
        if (hasattr(self, "_section_scroll")
                and event.type() == QEvent.Type.Resize
                and watched is self._section_scroll.viewport()):
            self._schedule_floating_headers()
        return super().eventFilter(watched, event)

    @staticmethod
    def _metric(grid, row, column, title):
        # Metric cards must not inherit QGroupBox's large title margin and
        # padding: a short QGroupBox clips the value even while _tick updates it.
        card = QWidget()
        card.setObjectName("metricCard")
        card.setMinimumHeight(70)
        card.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(9, 6, 9, 6)
        card_layout.setSpacing(2)
        caption = QLabel(title)
        caption.setObjectName("metricTitle")
        caption.setWordWrap(True)
        value = QLabel("--")
        value.setObjectName("metricValue")
        value.setWordWrap(True)
        value.setMinimumHeight(24)
        card_layout.addWidget(caption)
        card_layout.addWidget(value)
        grid.addWidget(card, row, column)
        return value

    @staticmethod
    def _make_copyable(label):
        label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )

    def _set_enabled(self, active):
        self.run_box.setEnabled(active)
        self.logout_button.setEnabled(active)
        self.create_button.setEnabled(bool(active and not self.run_id))
        self.finalize_button.setEnabled(bool(self.run_id))
        self.retry_button.setEnabled(bool(self.run_id))

    def _start_pairing(self):
        logger.info("gui_pairing_start")
        self.pair_button.setEnabled(False)
        self.status_label.setText("Opening FoodPort sign-in in your browser...")
        self._pairing_task = _PairingStartTask(self.client)
        self._pairing_task.succeeded.connect(self._pairing_started)
        self._pairing_task.failed.connect(self._pairing_failed)
        self.api_pool.start(self._pairing_task)

    def _pairing_started(self, data):
        self._pairing_task = None
        self._pairing_id = data["pairing_id"]
        logger.info("gui_pairing_browser_opened")
        if not webbrowser.open(data["approval_url"]):
            self._pairing_failed(FoodPortError(0, "Could not open the FoodPort portal"))
            return
        self.pair_code_button.setEnabled(True)
        self.status_label.setText("Approve Nanopore GUI in the browser, then enter the one-time code.")

    def _complete_pairing(self):
        if not self._pairing_id or not self.pair_code.text().strip():
            self._show_error("Start browser sign-in and enter the one-time pairing code.")
            return
        self.pair_code_button.setEnabled(False)
        self._pairing_task = _PairingExchangeTask(
            self.client, self._pairing_id, self.pair_code.text().strip(),
        )
        self._pairing_task.succeeded.connect(self._pairing_succeeded)
        self._pairing_task.failed.connect(self._pairing_failed)
        self.api_pool.start(self._pairing_task)

    def _pairing_succeeded(self, _result):
        self._pairing_task = None
        self._pairing_id = None
        self.pair_code.clear()
        self.login_box.setEnabled(False)
        self.login_section.setVisible(False)
        self._set_enabled(True)
        self.status_label.setText("Connected to FoodPort. Create a run to start watching a folder.")
        self.network.setText("Connected")
        if not self.test_mode:
            self._restore_run()
        else:
            logger.info("gui_test_mode_skipping_saved_run")
        if not self.run_id:
            self.run_section.setVisible(True)
            self.run_section.set_expanded(True)
        logger.info("gui_pairing_complete")

    def _pairing_failed(self, error):
        self._pairing_task = None
        self.pair_button.setEnabled(True)
        self.pair_code_button.setEnabled(bool(self._pairing_id))
        self._show_error(str(error))
        logger.warning("gui_pairing_failed error=%s", error)

    def _choose_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Select POD5 directory")
        if path:
            self.folder.setText(path)

    def _apply_test_preset(self, test_directory: Path | None):
        self.run_name.setText(datetime.now().strftime("%y%m%d-nanopore-test"))
        if test_directory is None:
            self.folder.setText("Mock upload supplied by --test-run")
            self.folder.setReadOnly(True)
            self.folder_button.setEnabled(False)
        else:
            self.folder.setText(str(test_directory))
        self.stability.setValue(1)
        self.workers.setValue(2)
        selected = {str(value).zfill(2) for value in DEFAULT_BARCODES}
        self.barcode_values.blockSignals(True)
        for row in range(self.barcode_values.count()):
            item = self.barcode_values.item(row)
            item.setSelected(item.text() in selected)
        self.barcode_values.blockSignals(False)
        self._sync_sample_metadata_rows()
        for barcode, metadata in DEFAULT_SAMPLE_METADATA.items():
            matches = self.sample_metadata.findItems(
                str(barcode).zfill(2),
                Qt.MatchFlag.MatchExactly,
            )
            if not matches:
                continue
            row = matches[0].row()
            self.sample_metadata.item(row, 1).setText(metadata["seqid"])
            self.sample_metadata.item(row, 2).setText(metadata["olnid"])
        mode = "local" if test_directory is not None else "cloud-copy"
        self.status_label.setText(
            "Test preset loaded ({0}); wave plan 3,7,2,5,6.".format(mode)
        )
        logger.info(
            "gui_test_preset_applied mode=%s source=%s",
            mode,
            test_directory or "Azure fixture",
        )

    def _sync_sample_metadata_rows(self):
        existing = {}
        for row in range(self.sample_metadata.rowCount()):
            barcode_item = self.sample_metadata.item(row, 0)
            if barcode_item:
                existing[barcode_item.text()] = tuple(
                    self.sample_metadata.item(row, column).text().strip()
                    if self.sample_metadata.item(row, column) else ""
                    for column in (1, 2)
                )
        selected = [item.text() for item in self.barcode_values.selectedItems()]
        self.sample_metadata.setRowCount(0)
        for barcode in selected:
            row = self.sample_metadata.rowCount()
            self.sample_metadata.insertRow(row)
            barcode_item = QTableWidgetItem(barcode)
            barcode_item.setFlags(barcode_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.sample_metadata.setItem(row, 0, barcode_item)
            seqid, olnid = existing.get(barcode, ("", ""))
            self.sample_metadata.setItem(row, 1, QTableWidgetItem(seqid))
            self.sample_metadata.setItem(row, 2, QTableWidgetItem(olnid))
        self._resize_sample_metadata()

    def _resize_sample_metadata(self):
        header = self.sample_metadata.horizontalHeader().height()
        rows = sum(
            self.sample_metadata.rowHeight(row)
            for row in range(self.sample_metadata.rowCount())
        )
        desired = header + rows + self.sample_metadata.frameWidth() * 2 + 4
        # Keep setup compact. Larger selections scroll inside the metadata table.
        height = max(105, min(desired, 220))
        self.sample_metadata.setMinimumHeight(105)
        self.sample_metadata.setMaximumHeight(220)
        self.sample_metadata.resize(self.sample_metadata.width(), height)

    def _sample_metadata_payload(self):
        samples = []
        seqids = set()
        for row in range(self.sample_metadata.rowCount()):
            values = [
                self.sample_metadata.item(row, column).text().strip()
                if self.sample_metadata.item(row, column) else ""
                for column in range(3)
            ]
            if not any(values):
                continue
            barcode, seqid, olnid = values
            if not barcode or not seqid:
                raise ValueError("Each sample needs a barcode and SEQID.")
            if not re.fullmatch(r"\d{4}-MIN-\d{4}", seqid):
                raise ValueError("SEQID must use the format ####-MIN-####.")
            if seqid in seqids:
                raise ValueError("SEQIDs must be unique.")
            seqids.add(seqid)
            samples.append({"barcode": int(barcode), "seqid": seqid, "olnid": olnid})
        return {"samples": samples} if samples else None

    def _create_run(self):
        if not self._run_preflight(show_success=False):
            self._show_error("Preflight checks failed. Review the highlighted checks before creating the run.")
            return
        if not self.run_name.text().strip():
            self._show_error("Run name is required.")
            return
        if not self.test_mode and not self.folder.text().strip():
            self._show_error("A POD5 directory is required outside test mode.")
            return
        if not self.barcode_values.selectedItems():
            self._show_error("Select at least one barcode.")
            return
        run_name = self.run_name.text().strip()
        metadata = {
            "barcode_kit": self.barcode_kit.currentText(),
            "barcode_values": [int(item.text()) for item in self.barcode_values.selectedItems()],
            "lab_name": self.lab_name.currentText(),
            "reference_database": dict(self._reference_info),
        }
        try:
            sample_metadata = self._sample_metadata_payload()
        except ValueError as exc:
            self._show_error(str(exc))
            return
        if sample_metadata:
            metadata["sample_metadata"] = sample_metadata
        self.create_button.setEnabled(False)
        logger.info("gui_run_create_start run_name=%s", run_name)
        self._create_task = _ApiTask(lambda: self.client.create_run(run_name, metadata))
        self._create_task.succeeded.connect(self._create_succeeded)
        self._create_task.failed.connect(self._create_failed)
        self.api_pool.start(self._create_task)

    def _create_succeeded(self, data):
        self._create_task = None
        try:
            self.run_id = int(data["run_id"])
        except (KeyError, ValueError, TypeError) as exc:
            self.create_button.setEnabled(True)
            self._show_error(str(exc))
            return
        self.run_started = datetime.now(timezone.utc)
        lab_index = self.report_lab.findData(self.lab_name.currentText())
        if lab_index >= 0:
            self.report_lab.setCurrentIndex(lab_index)
        self._accepting_files = True
        input_path = self.folder.text()
        if self.test_mode:
            input_path = str(
                self.report_root
                / "test-input"
                / "run-{0}".format(self.run_id)
            )
            Path(input_path).mkdir(parents=True, exist_ok=True)
        self.store.save_run(
            self.run_id,
            self.run_name.text().strip(),
            input_path,
            self.run_started.isoformat(),
            self._run_metadata(),
        )
        self.folder.setText(input_path)
        self.run_details.setText(
            f"Run {self.run_id} | {self.run_name.text().strip()} | "
            f"Input: {input_path}"
        )
        self.run_box.setEnabled(False)
        self.run_section.setVisible(False)
        self.status_section.set_expanded(True)
        self.results_section.set_expanded(True)
        self.finalize_button.setEnabled(not self.test_mode)

        if self.test_mode and self._test_source_directory is None:
            self.scanner = None
            self.uploader = None
            self._start_cloud_test_seed()
        else:
            self.scanner = StablePod5Scanner(
                Path(input_path),
                stable_seconds=self.stability.value(),
            )
            self.uploader = UploadCoordinator(
                self.client,
                self.store,
                self.run_id,
                workers=self.workers.value(),
            )
            self._watch_folder(Path(input_path))
            if self.test_mode:
                self._start_local_test_waves(Path(input_path))
            else:
                self.status_label.setText(
                    f"Watching {input_path} for stable POD5 files."
                )
        logger.info(
            "gui_run_created run_id=%s folder=%s test_mode=%s",
            self.run_id,
            input_path,
            self.test_mode,
        )

    def _start_cloud_test_seed(self):
        self.status_label.setText(
            "Starting Azure fixture waves (3,7,2,5,6)."
        )
        self._cloud_seed_task = _CloudSeedTask(
            self.client,
            self.run_id,
            self.test_wave_delay,
            self.test_auto_finalize,
        )
        self._cloud_seed_task.progress.connect(self._test_progress_message)
        self._cloud_seed_task.succeeded.connect(self._cloud_test_succeeded)
        self._cloud_seed_task.failed.connect(self._test_failed)
        self.api_pool.start(self._cloud_seed_task)

    def _start_local_test_waves(self, staging: Path):
        self.status_label.setText(
            "Starting local fixture waves (3,7,2,5,6)."
        )
        self.test_controller = LocalWaveController(
            self._test_source_directory,
            staging,
            self.store,
            self.run_id,
            wait_seconds=self.test_wave_delay,
            progress=self.test_progress.emit,
            finished=self.test_finished.emit,
            failed=self.test_failed.emit,
        )
        self.test_controller.start()

    def _test_progress_message(self, message):
        self.status_label.setText(str(message))
        logger.info("gui_test_progress message=%s", message)

    def _cloud_test_succeeded(self, result):
        self._cloud_seed_task = None
        workflow_state = result.get("workflow_state", "processing")
        if self.run_id:
            self.store.update_run_status(self.run_id, workflow_state)
        self._accepting_files = False
        self.finalize_button.setEnabled(
            not self.test_auto_finalize and bool(self.run_id)
        )
        self.status_label.setText(
            "Cloud test waves complete; workflow={0}.".format(workflow_state)
        )

    def _test_local_finished(self):
        self.test_controller = None
        self.status_label.setText("Local test waves uploaded successfully.")
        if self.test_auto_finalize:
            self.finalize_button.setEnabled(True)
            self._finalize()
        else:
            self.finalize_button.setEnabled(True)

    def _test_failed(self, error):
        self._cloud_seed_task = None
        self.status_label.setText("Test run failed.")
        self.headless_error = str(error) if self.headless_test else None
        if not self.headless_test:
            self._show_error(str(error))
        logger.error("gui_test_run_failed error=%s", error)

    def _create_failed(self, error):
        if self.headless_test:
            self.headless_error = str(error)
        self._create_task = None
        self.create_button.setEnabled(True)
        self._show_error(str(error))
        logger.warning("gui_run_create_failed error=%s", error)

    def _tick(self):
        if not self.run_id:
            return
        if self._accepting_files and self.scanner and self.uploader:
            for relative, path, size in self.scanner.scan():
                self._last_new_file_at = datetime.now(timezone.utc)
                self.uploader.submit(relative, path, size)
        total, total_size, uploaded = self.store.counts(self.run_id)
        pending = total_size - uploaded
        rows = self.store.all(self.run_id)
        failed = sum(1 for row in rows if row["status"] == "error")
        containers = sorted({row["server_container"] for row in rows if row["server_container"]})
        blob_names = sorted({row["server_blob_name"] for row in rows if row["server_blob_name"]})
        details = f"Run {self.run_id} | {self.run_name.text().strip()} | Input: {self.folder.text()}"
        if containers:
            details += f" | Blob container: {', '.join(containers)}"
        elif blob_names:
            details += f" | Blob: {blob_names[0]}"
        self.run_details.setText(details)
        self.failed_files.clear()
        for row in rows:
            if row["status"] == "error":
                self.failed_files.addItem(f"{row['relative_path']}: {row['error'] or 'Unknown error'}")
        now = time.monotonic()
        interval = now - self._last_measurement
        if interval > 0:
            self._bytes_per_second = max(0, uploaded - self._last_uploaded) / interval
        self._last_uploaded = uploaded
        self._last_measurement = now
        self.file_count.setText(str(total))
        self.pending_count.setText(str(sum(1 for row in rows if row["status"] != "uploaded")))
        self.failed_count.setText(str(failed))
        self.failures_section.toggle.setText(
            "Failed files ({0})".format(failed) if failed else "Failed files"
        )
        self.uploaded_size.setText(_format_bytes(uploaded))
        self.remaining_size.setText(_format_bytes(pending))
        self.transfer_rate.setText(f"{_format_bytes(self._bytes_per_second)}/s" if self._bytes_per_second else "--")
        self.backlog_eta.setText(_format_duration(pending / self._bytes_per_second) if self._bytes_per_second else "--")
        self.progress.setValue(round(uploaded * 100 / total_size) if total_size else 0)
        if self.run_started:
            elapsed = datetime.now(timezone.utc) - self.run_started
            self.run_age.setText(str(elapsed).split(".")[0])
        if not self._status_in_flight:
            self._status_in_flight = True
            task = _StatusTask(self.client, self.run_id)
            task.succeeded.connect(self._status_succeeded)
            task.failed.connect(self._status_failed)
            self.api_pool.start(task)

    def _status_succeeded(self, status):
        self._status_in_flight = False
        if self.run_id:
            self._last_status = status
            self.network.setText("Connected")
            workflow_state = status.get("workflow_state", "unknown")
            self.store.update_run_status(self.run_id, workflow_state)
            if workflow_state in ("stopping", "complete", "error"):
                self._accepting_files = False
                self.finalize_button.setEnabled(False)
                self._stop_file_intake()
            processing = status.get("processing") or {}
            processing_state = processing.get("status") or status.get("processing_status") or "idle"
            self.processing_status.setText(str(processing_state))
            processed = processing.get("processed_pod5_count")
            total = processing.get("pod5_count") or processing.get("total_pod5_count")
            self.processing_progress.setText(
                f"{processed}/{total}" if processed is not None and total else str(processed or "--")
            )
            self.last_update.setText(datetime.now().strftime("%H:%M:%S"))
            self.processing_details.setText(_format_processing_details(processing))
            processing_message = processing.get("message") or processing.get("error") or ""
            processed_reports = processing.get("reports") or []
            state_marker = (
                workflow_state,
                processing_state,
                processing_message,
                processed,
                total,
                len(processed_reports) if isinstance(processed_reports, list) else "unknown",
            )
            if state_marker != getattr(self, "_last_state_marker", None):
                logger.info(
                    "gui_run_state_changed run_id=%s workflow_state=%s processing_state=%s "
                    "message=%s processed_pod5_count=%s pod5_count=%s reports=%s "
                    "retryable=%s retry_count=%s retry_limit=%s error=%s",
                    self.run_id,
                    workflow_state,
                    processing_state,
                    processing_message or "--",
                    processed if processed is not None else "--",
                    total if total is not None else "--",
                    len(processed_reports) if isinstance(processed_reports, list) else "unknown",
                    processing.get("retryable", "--"),
                    processing.get("retry_count", "--"),
                    processing.get("retry_limit", "--"),
                    processing.get("error") or "--",
                )
                self._last_state_marker = state_marker
            report = status.get("report") or processing.get("report")
            if report:
                self._remember_report(report)
            self._queue_iteration_results(processing.get("reports"))
            if workflow_state == "complete" or processing_state in {"complete", "completed"}:
                self._request_latest_result()
            self.status_label.setText(f"Run state: {workflow_state} | Processing: {processing_state}")

    def _status_failed(self, error):
        self._status_in_flight = False
        if self.run_id and isinstance(error, FoodPortError):
            self.network.setText("Offline")
            self.status_label.setText(f"FoodPort unavailable: {error.detail}")
            logger.warning("gui_status_poll_failed run_id=%s error=%s", self.run_id, error)

    def _request_latest_result(self):
        if self._result_in_flight or not self.run_id:
            return
        self._result_in_flight = True
        task = _LatestResultTask(self.client, self.run_id)
        task.succeeded.connect(self._latest_result_succeeded)
        task.failed.connect(self._latest_result_failed)
        self.api_pool.start(task)

    def _latest_result_succeeded(self, result):
        self._result_in_flight = False
        self._render_results(result, result.get("processing") or {})

    def _queue_iteration_results(self, reports):
        for entry in reports or []:
            if not isinstance(entry, dict):
                continue
            iteration = entry.get("iteration") or entry.get("generation")
            if iteration is None:
                continue
            try:
                iteration = int(iteration)
            except (TypeError, ValueError):
                continue
            report = entry.get("report")
            if isinstance(report, dict):
                self._remember_report(report)
            if iteration in self._report_documents or iteration in self._iteration_result_tasks:
                continue
            if not self.run_id:
                return
            task = _IterationResultTask(
                self.client,
                self.run_id,
                iteration,
                self.report_root,
                self._report_context(),
            )
            self._iteration_result_tasks[iteration] = task
            task.succeeded.connect(
                lambda result, item=iteration: self._iteration_result_succeeded(item, result)
            )
            task.failed.connect(
                lambda error, item=iteration: self._iteration_result_failed(item, error)
            )
            self.api_pool.start(task)

    def _iteration_result_succeeded(self, iteration, result):
        self._iteration_result_tasks.pop(iteration, None)
        report = result.get("report")
        if isinstance(report, dict):
            self._remember_report(report)
            if self.run_id:
                self.store.save_report(
                    self.run_id,
                    iteration,
                    result.get("report_directory") or "",
                    result.get("report_manifest_path"),
                )
        self._render_results(result, result.get("processing") or {})

    def _iteration_result_failed(self, iteration, error):
        self._iteration_result_tasks.pop(iteration, None)
        if isinstance(error, FoodPortError) and error.status == 404:
            logger.info("gui_iteration_pending iteration=%s", iteration)
            return
        if isinstance(error, FoodPortError) and error.status not in (404, 409):
            self.result_status.setText(f"Iteration {iteration} unavailable: {error.detail}")

    def _latest_result_failed(self, error):
        self._result_in_flight = False
        if isinstance(error, FoodPortError) and error.status not in (404, 409):
            self.result_status.setText(f"Result unavailable: {error.detail}")

    def _watch_folder(self, folder: Path):
        watched = [str(folder)] + [str(path) for path in folder.rglob("*") if path.is_dir()]
        current_paths = self.watcher.directories()
        if current_paths:
            self.watcher.removePaths(current_paths)
        self.watcher.addPaths(watched)

    def _finalize(self):
        if not self.run_id:
            return
        if self._finalize_task:
            return
        rows = self.store.all(self.run_id)
        pending_count = sum(1 for row in rows if row["status"] != "uploaded")
        failed_count = sum(1 for row in rows if row["status"] == "error")
        newest = self._last_new_file_at.isoformat() if self._last_new_file_at else "No files observed in this session"
        message = (
            "Finalize run {0}?\n\nFiles discovered: {1}\nPending: {2}\n"
            "Failed: {3}\nNewest file: {4}\n\nFinalization stops new file "
            "discovery and cannot be treated as a pause."
        ).format(self.run_id, len(rows), pending_count, failed_count, newest)
        if pending_count or failed_count:
            self._show_error(message + "\n\nResolve pending or failed uploads before finalizing.")
            return
        if not self.headless_test and QMessageBox.question(self, "Confirm finalization", message, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        self._accepting_files = False
        logger.info("gui_finalize_start run_id=%s", self.run_id)
        current_paths = self.watcher.directories()
        if current_paths:
            self.watcher.removePaths(current_paths)
        self.scanner = None
        if self.uploader and not self.uploader.is_idle:
            self.status_label.setText(
                "Waiting for {0} active upload(s) before finalizing.".format(
                    self.uploader.active_count
                )
            )
            QTimer.singleShot(500, self._finalize)
            return
        if self.uploader:
            self.uploader.close(wait=False)
            self.uploader = None
        if self.store.pending(self.run_id):
            self.uploader = UploadCoordinator(
                self.client, self.store, self.run_id, workers=self.workers.value()
            )
            self._show_error(
                "Finalization stopped new file discovery, but one or more uploads "
                "failed. Retry the failed files before finalizing again."
            )
            return
        run_id = self.run_id
        self.finalize_button.setEnabled(False)
        self._finalize_task = _ApiTask(lambda: self.client.finalize(run_id))
        self._finalize_task.succeeded.connect(self._finalize_succeeded)
        self._finalize_task.failed.connect(self._finalize_failed)
        self.api_pool.start(self._finalize_task)

    def _finalize_succeeded(self, result):
        self._finalize_task = None
        if self.run_id:
            self.store.update_run_status(
                self.run_id,
                result.get("workflow_state", "stopping"),
            )
        self.finalize_button.setEnabled(False)
        self.status_label.setText(
            f"Run termination requested: {result.get('workflow_state', 'ready')}"
        )
        logger.info("gui_finalize_accepted run_id=%s workflow_state=%s",
                    self.run_id, result.get("workflow_state", "unknown"))

    def _finalize_failed(self, error):
        if self.headless_test:
            self.headless_error = str(error)
        self._finalize_task = None
        self.finalize_button.setEnabled(True)
        self._show_error(str(error))
        logger.warning("gui_finalize_failed run_id=%s error=%s", self.run_id, error)

    def _retry_failed(self):
        if not self.run_id or not self.uploader:
            return
        failed = [row for row in self.store.all(self.run_id) if row["status"] == "error"]
        for row in failed:
            self.uploader.submit(row["relative_path"], Path(row["local_path"]), row["size_bytes"])
        self.status_label.setText(f"Retrying {len(failed)} failed upload(s).")
        logger.info("gui_retry_failed_uploads run_id=%s count=%s", self.run_id, len(failed))

    def _logout(self):
        logger.info("gui_logout_start run_id=%s", self.run_id)
        if self._cloud_seed_task:
            self._cloud_seed_task.cancel()
        if self.test_controller:
            self.test_controller.stop()
            self.test_controller = None
        if self.uploader:
            self.uploader.close()
        try:
            self.client.revoke_token()
        except FoodPortError:
            self.client.logout()
        if self.run_id and self.view_run_id is None:
            self.store.close_run(self.run_id)
        self.run_id = None
        self._accepting_files = False
        self.uploader = None
        self.scanner = None
        current_paths = self.watcher.directories()
        if current_paths:
            self.watcher.removePaths(current_paths)
        self.failed_files.clear()
        self.login_box.setEnabled(True)
        self.login_section.setVisible(True)
        self.login_section.set_expanded(True)
        self.run_section.setVisible(True)
        self.run_section.set_expanded(True)
        self._set_enabled(False)
        self.status_label.setText("Logged out.")
        self.run_details.setText("No active run.")
        self.network.setText("--")
        logger.info("gui_logout_complete")

    def closeEvent(self, event):
        if not self.headless_test and self.run_id and (self._accepting_files or (self.uploader and not self.uploader.is_idle)):
            answer = QMessageBox.question(self, "Active run", "A run is active. Closing the GUI will stop local file discovery until the run is restored. Close anyway?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore(); return
        self.timer.stop()
        current_paths = self.watcher.directories()
        if current_paths:
            self.watcher.removePaths(current_paths)
        if self._cloud_seed_task:
            self._cloud_seed_task.cancel()
        if self.test_controller:
            self.test_controller.stop()
            self.test_controller = None
        if self.uploader:
            self.uploader.close(wait=True)
        self.api_pool.waitForDone()
        self.client.close()
        self.store.close()
        event.accept()

    def _select_all_barcodes(self):
        for index in range(self.barcode_values.count()): self.barcode_values.item(index).setSelected(True)

    def _clear_barcodes(self):
        if self.sample_metadata.rowCount() and QMessageBox.question(self, "Clear barcodes", "Clear selected barcodes and their displayed metadata?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes: return
        self.barcode_values.clearSelection()

    def _invert_barcodes(self):
        for index in range(self.barcode_values.count()):
            item=self.barcode_values.item(index); item.setSelected(not item.isSelected())

    def _choose_reference(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select FASTA reference database", "", "FASTA or text (*.fasta *.fa *.fna *.txt);;All files (*)")
        if path: self.reference_path.setText(path); self._inspect_reference()

    def _inspect_reference(self):
        path=Path(self.reference_path.text().strip())
        info={}
        try:
            if not path.is_file(): raise ValueError("File does not exist")
            count=0; first=None
            digest=hashlib.sha256()
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(1024*1024), b""): digest.update(block)
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if line.strip() and first is None: first=line.strip()
                    if line.startswith("> ") or line.startswith(">") : count += 1
            if not first or not first.startswith(">") or not count: raise ValueError("Content is not recognizable FASTA")
            info={"name": path.name, "path": str(path.resolve()), "records": count, "sha256": digest.hexdigest(), "modified_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()}
            self.reference_info.setText("{name} | {records:,} records | SHA-256 {sha256}".format(**info))
        except Exception as exc:
            self.reference_info.setText("Reference database invalid: {0}".format(exc))
        self._reference_info=info
        return bool(info)

    def _run_preflight(self, show_success=False):
        self.preflight_list.clear(); checks=[]
        def add(ok, label): checks.append((ok,label)); self.preflight_list.addItem(("PASS: " if ok else "FAIL: ")+label)
        folder=Path(self.folder.text().strip()) if self.folder.text().strip() else None
        add(bool(self.run_name.text().strip()), "Run name supplied")
        add(bool(self.barcode_values.selectedItems()), "At least one barcode selected")
        try: payload=self._sample_metadata_payload(); add(bool(payload and payload.get("samples")), "Sample metadata complete and unique")
        except ValueError as exc: add(False, "Sample metadata: {0}".format(exc))
        mock_upload = self.test_mode and self._test_source_directory is None
        if mock_upload:
            add(True, "Mock POD5 upload enabled by --test-run")
            add(True, "Local POD5 directory not required in test mode")
        else:
            directory_ok = bool(folder and folder.is_dir() and os.access(str(folder), os.R_OK))
            add(directory_ok, "POD5 directory readable")
            if directory_ok:
                try:
                    add(shutil.disk_usage(str(folder)).free >= 5 * 1024**3, "At least 5 GB local free space")
                except OSError:
                    add(False, "Free-space check")
        add(self._inspect_reference(), "Reference database validated")
        good=all(ok for ok,_ in checks)
        self.create_button.setEnabled(good and not self.run_id)
        if show_success and good: QMessageBox.information(self, "Preflight", "All local preflight checks passed. FoodPort connectivity will be verified when the run is created.")
        return good

    def _metadata_rows(self):
        return [[self.sample_metadata.item(r,c).text() if self.sample_metadata.item(r,c) else "" for c in range(3)] for r in range(self.sample_metadata.rowCount())]

    def _import_metadata_csv(self):
        path,_=QFileDialog.getOpenFileName(self,"Import sample metadata","","CSV (*.csv);;All files (*)")
        if not path: return
        with open(path,"r",encoding="utf-8-sig",newline="") as handle: rows=list(csv.DictReader(handle))
        selected={str(row.get("Barcode") or row.get("barcode") or "").zfill(2) for row in rows}
        for i in range(self.barcode_values.count()): self.barcode_values.item(i).setSelected(self.barcode_values.item(i).text() in selected)
        self._sync_sample_metadata_rows()
        for row in rows:
            barcode=str(row.get("Barcode") or row.get("barcode") or "").zfill(2); matches=self.sample_metadata.findItems(barcode,Qt.MatchFlag.MatchExactly)
            if matches:
                r=matches[0].row(); self.sample_metadata.item(r,1).setText(str(row.get("SEQID") or row.get("seqid") or "")); self.sample_metadata.item(r,2).setText(str(row.get("OLNID") or row.get("OLN ID") or row.get("olnid") or ""))

    def _export_metadata_csv(self):
        path,_=QFileDialog.getSaveFileName(self,"Export sample metadata","sample-metadata.csv","CSV (*.csv)")
        if not path:return
        with open(path,"w",encoding="utf-8",newline="") as handle:
            writer=csv.writer(handle); writer.writerow(["Barcode","SEQID","OLNID"]); writer.writerows(self._metadata_rows())

    def _setup_payload(self):
        return {"run_name":self.run_name.text(),"barcode_kit":self.barcode_kit.currentText(),"lab_name":self.lab_name.currentText(),"folder":self.folder.text(),"reference_path":self.reference_path.text(),"stability":self.stability.value(),"workers":self.workers.value(),"barcodes":[i.text() for i in self.barcode_values.selectedItems()],"samples":self._metadata_rows()}

    def _save_setup(self):
        path,_=QFileDialog.getSaveFileName(self,"Save run setup",str(self._setup_path),"JSON (*.json)")
        if path: Path(path).write_text(json.dumps(self._setup_payload(),indent=2),encoding="utf-8")

    def _load_setup(self):
        path,_=QFileDialog.getOpenFileName(self,"Load run setup",str(self._setup_path.parent),"JSON (*.json)")
        if not path:return
        data=json.loads(Path(path).read_text(encoding="utf-8"))
        self.run_name.setText(data.get("run_name",self.run_name.text())); self.folder.setText(data.get("folder","")); self.reference_path.setText(data.get("reference_path","")); self.stability.setValue(int(data.get("stability",180))); self.workers.setValue(int(data.get("workers",3)))
        selected=set(data.get("barcodes",[]))
        for i in range(self.barcode_values.count()): self.barcode_values.item(i).setSelected(self.barcode_values.item(i).text() in selected)
        self._sync_sample_metadata_rows()
        for values in data.get("samples",[]):
            matches=self.sample_metadata.findItems(str(values[0]).zfill(2),Qt.MatchFlag.MatchExactly)
            if matches:
                r=matches[0].row()
                for c in (1,2): self.sample_metadata.item(r,c).setText(str(values[c] if len(values)>c else ""))
        self._inspect_reference()

    def _toggle_file_intake(self):
        if not self.run_id:return
        if self._accepting_files:
            self._accepting_files=False; self._stop_file_intake(); self.pause_button.setText("Resume file intake"); self.status_label.setText("File intake paused. The cloud run has not been finalized.")
        else:
            path=Path(self.folder.text())
            if not path.is_dir(): self._show_error("The input folder is unavailable."); return
            self.scanner=StablePod5Scanner(path,stable_seconds=self.stability.value()); self.uploader=UploadCoordinator(self.client,self.store,self.run_id,workers=self.workers.value()); self._watch_folder(path); self._accepting_files=True; self.pause_button.setText("Pause file intake")

    def _fit_report_table(self):
        """Fit visible rows, with a screen-height cap for large result sets."""
        table = self.report_table
        visible = sum(not table.isRowHidden(row) for row in range(table.rowCount()))
        rows_height = sum(table.rowHeight(row) for row in range(table.rowCount())
                          if not table.isRowHidden(row))
        header = table.horizontalHeader().height()
        scroll_height = table.horizontalScrollBar().sizeHint().height()
        desired = header + rows_height + 2 * table.frameWidth() + scroll_height + 8
        screen = self.screen() or QApplication.primaryScreen()
        limit = max(180, int(screen.availableGeometry().height() * 0.65)) if screen else 600
        table.setFixedHeight(min(max(desired, header + 48 if visible == 0 else 110), limit))

    def _filter_report_rows(self, *args):
        query = self.report_filter.text().strip()
        mode = self.report_status_filter.currentText()
        try:
            predicate = _compile_summary_query(query)
        except ValueError as error:
            self.query_feedback.setText("Query error: {0}".format(error))
            self.query_feedback.setStyleSheet("color: #a33131;")
            return  # Preserve the last valid result set on incomplete input.
        self.query_feedback.setStyleSheet("")
        shown = 0
        for row in range(self.report_table.rowCount()):
            data = {
                self.report_table.horizontalHeaderItem(col).text():
                    (self.report_table.item(row, col).text()
                     if self.report_table.item(row, col) else "")
                for col in range(self.report_table.columnCount())
            }
            visible = predicate(data)
            numeric = []
            for key, value in data.items():
                if _normalize_query_field(key) in ("seqid", "olnid"):
                    continue
                try:
                    numeric.append(float(value.replace("%", "").replace(",", "")))
                except ValueError:
                    pass
            if mode == "Detected":
                visible = visible and any(number > 0 for number in numeric)
            elif mode == "Not detected":
                visible = visible and bool(numeric) and all(number == 0 for number in numeric)
            elif mode == "Insufficient data":
                visible = visible and any(value.strip().lower() in ("-", "", "n/a")
                                          for value in data.values())
            self.report_table.setRowHidden(row, not visible)
            shown += bool(visible)
        self.query_feedback.setText("{0} of {1} rows shown. AND, OR, NOT, parentheses, column:term, ? and * supported.".format(
            shown, self.report_table.rowCount()))
        self._fit_report_table()

    def _export_filtered_results(self):
        path,_=QFileDialog.getSaveFileName(self,"Export filtered results","filtered-results.csv","CSV (*.csv)")
        if not path:return
        with open(path,"w",encoding="utf-8",newline="") as handle:
            writer=csv.writer(handle); writer.writerow([self.report_table.horizontalHeaderItem(c).text() for c in range(self.report_table.columnCount())])
            for r in range(self.report_table.rowCount()):
                if not self.report_table.isRowHidden(r): writer.writerow([self.report_table.item(r,c).text() if self.report_table.item(r,c) else "" for c in range(self.report_table.columnCount())])

    def _export_diagnostics(self):
        path,_=QFileDialog.getSaveFileName(self,"Export diagnostics","nanopore-diagnostics.zip","ZIP (*.zip)")
        if not path:return
        diagnostic={"generated_at":datetime.now(timezone.utc).isoformat(),"application_version":__version__,"python":platform.python_version(),"platform":platform.platform(),"run_id":self.run_id,"reference_database":self._reference_info,"status":self._last_status,"setup":self._setup_payload()}
        diagnostic["setup"].pop("folder",None)
        with zipfile.ZipFile(path,"w",zipfile.ZIP_DEFLATED) as archive: archive.writestr("diagnostics.json",json.dumps(diagnostic,indent=2,default=str))
        QMessageBox.information(self,"Diagnostics","Diagnostic bundle exported. Authentication tokens and authorization headers were not included.")

    def _show_help(self):
        QMessageBox.information(self,"Workflow help","1. Sign in to FoodPort.\n2. Enter run and sample metadata.\n3. Select the POD5 folder and validated reference database.\n4. Run preflight checks.\n5. Create the run and monitor uploads.\n6. Review preliminary report iterations.\n7. Finalize when known uploads are complete and you are ready to stop sending new files to this run.\n\nPause stops local discovery without finalizing the cloud run.")

    def _show_about(self):
        ref=self._reference_info.get("name","Not validated")
        QMessageBox.information(self,"About PorePort","PorePort\nGUI version: {0}\nReference database: {1}\n\nCanadian Food Inspection Agency / Agence canadienne d'inspection des aliments".format(__version__,ref))

    def _sync_report_lab_from_run(self, _index=None):
        """Default the report lab from Run setup until a run is created."""
        if self.run_id is not None:
            return
        index = self.report_lab.findData(self.lab_name.currentText())
        if index >= 0:
            self.report_lab.setCurrentIndex(index)
        self._report_lab_changed()

    def _report_lab_changed(self, _index=None):
        code = self.report_lab.currentData()
        self.report_lab_address.setText(
            LAB_ADDRESSES.get(code, "Address not recorded")
        )
        if hasattr(self, "report_artifacts"):
            self._refresh_target_report()

    def _set_report_fields_locked(self, locked):
        for widget in (self.report_lab, self.report_state, self.report_rdims_document_id,
                       self.reviewer_name, self.approval_timestamp,
                       self.approval_now_button, self.digital_signature):
            widget.setEnabled(not locked)
        self.report_fields_lock.setText("Unlock report fields" if locked else "Lock report fields")

    def _apply_tooltips(self):
        tips = {
            self.help_button: "Show the workflow instructions.",
            self.about_button: "Show the GUI version and reference database.",
            self.diagnostics_button: "Export a redacted troubleshooting bundle.",
            self.pair_button: "Open FoodPort in a browser to authorize the GUI.",
            self.pair_code: "Enter the one-time code displayed by FoodPort.",
            self.pair_code_button: "Complete FoodPort sign-in using the one-time code.",
            self.run_name: "Name used for the FoodPort run and generated reports.",
            self.barcode_kit: "Barcode kit used for this sequencing run.",
            self.lab_name: "Laboratory responsible for this run.",
            self.barcode_values: "Select all barcodes included in the run.",
            self.sample_metadata: "Enter a SEQID and optional OLNID for each barcode; spreadsheet copy and paste is supported.",
            self.import_metadata_button: "Import Barcode, SEQID, and OLNID columns from CSV.",
            self.export_metadata_button: "Export the displayed sample metadata to CSV.",
            self.save_setup_button: "Save the current run setup, barcode selection, sample metadata, POD5 source, reference database, and upload settings to a JSON file.",
            self.load_setup_button: "Load a previously saved run-setup JSON file and restore its run fields, barcode selection, sample metadata, source, reference database, and upload settings.",
            self.folder: "POD5 source directory. --test-run mock mode requires no real directory.",
            self.folder_button: "Choose a real POD5 directory.",
            self.reference_path: "FASTA reference database used by the analysis.",
            self.reference_button: "Choose a FASTA reference database.",
            self.reference_info: "Validated database identity, record count, and SHA-256 checksum.",
            self.preflight_button: "Validate run setup; --test-run skips the local POD5-directory requirement.",
            self.preflight_list: "Results of the most recent preflight validation.",
            self.stability: "Seconds a POD5 file must remain unchanged before upload.",
            self.workers: "Maximum concurrent uploads.",
            self.create_button: "Create the run. In --test-run mode, start the mock upload without a POD5 directory.",
            self.report_lab: "Select the laboratory printed on the target report; the address is shown below.",
            self.report_lab_address: "Address printed for the selected report laboratory.",
            self.report_state: "Workflow state written into the generated target report.",
            self.report_rdims_document_id: "Optional RDIMS ID; leave blank until assigned.",
            self.reviewer_name: "Optional reviewer written into regenerated reports.",
            self.approval_timestamp: "Optional approval timestamp written into regenerated reports.",
            self.approval_now_button: "Set the approval timestamp to the current UTC time.",
            self.digital_signature: "Optional signer, certificate ID, or signature reference.",
            self.report_fields_lock: "Lock or unlock the report fields to prevent accidental changes.",
            self.generate_report_button: "Create the PDF and HTML for the latest completed iteration. Live previews are unaffected.",
            self.live_iteration: "Select the live preview iteration for all four tabs.",
            self.live_previous_button: "Previous live preview iteration.",
            self.live_next_button: "Next live preview iteration.",
            self.report_filter: "Query the summary table: terms, SEQID: or OLNID:, AND/OR/NOT, parentheses, quoted phrases, ? and * wildcards.",
            self.report_status_filter: "Filter rows by detection or coverage status.",
            self.export_filtered_button: "Export visible summary rows to CSV.",
            self.open_pdf_button: "Open the report PDF.",
            self.open_report_button: "Open the HTML report.",
            self.open_report_folder_button: "Open the report artifact folder.",
            self.retry_button: "Retry failed uploads.",
            self.pause_button: "Pause or resume file discovery without finalizing.",
            self.open_input_button: "Open the real input directory when one is used.",
            self.finalize_button: "Finalize after all uploads complete.",
            self.logout_button: "Log out of FoodPort.",
            self.processing_details_toggle: "Show or hide detailed processing status.",
        }
        for widget, text in tips.items():
            widget.setToolTip(text)
        for section in (self.login_section, self.run_section, self.status_section,
                        self.results_section, self.live_files_section, self.report_section,
                        self.failures_section):
            section.toggle.setToolTip("Expand or collapse this section.")
        category_tips = (
            (self.run_form, self.run_name, "Run name used in FoodPort and report filenames."),
            (self.run_form, self.barcode_kit, "Barcode kit used to demultiplex the sequencing run."),
            (self.run_form, self.lab_name, "CFIA laboratory responsible for the run."),
            (self.run_form, self.barcode_values, "Barcodes selected for this run."),
            (self.run_form, self.sample_metadata, "Sample identifiers associated with the selected barcodes."),
            (self.run_form, self.folder, "Directory monitored for stable POD5 files; not required by --test-run."),
            (self.run_form, self.reference_path, "Reference FASTA used for analysis and report provenance."),
            (self.run_form, self.reference_info, "Validated reference-database identity and checksum."),
            (self.run_form, self.preflight_button, "Local checks that must pass before creating a normal run."),
            (self.run_form, self.preflight_list, "Detailed results from the latest preflight check."),
            (self.run_form, self.stability, "Minimum time a POD5 file must remain unchanged before upload."),
            (self.run_form, self.workers, "Maximum number of uploads performed concurrently."),
            (self.report_metadata_form, self.report_lab, "Laboratory and address printed on the target report."),
            (self.report_metadata_form, self.report_state, "Review and release state written into the target report."),
            (self.report_metadata_form, self.report_rdims_document_id, "Optional RDIMS interpretation-document identifier."),
            (self.report_metadata_form, self.reviewer_name, "Optional person recorded as the report reviewer."),
            (self.report_metadata_form, self.approval_timestamp, "Optional date and time when the report was approved."),
            (self.report_metadata_form, self.digital_signature, "Optional digital-signature or certificate reference."),
        )
        for form, field, text in category_tips:
            label = form.labelForField(field)
            if label is not None:
                label.setToolTip(text)
                tips[label] = text
        self.status_section.toggle.setToolTip(
            "Show or hide run details, transfer progress, Files, Pending, Failed, "
            "Uploaded, Remaining, transfer rate, run age, network, backlog, and "
            "processing-status boxes. Collapsing this section does not pause the run."
        )
        for index in range(self.barcode_values.count()):
            item = self.barcode_values.item(index)
            item.setToolTip("Include barcode {0}.".format(item.text()))
        for widget in tips:
            self._tooltip_filter.watch(widget)
        for section in (self.login_section, self.run_section, self.status_section,
                        self.results_section, self.live_files_section, self.report_section,
                        self.failures_section):
            self._tooltip_filter.watch(section.toggle)

    def _show_error(self, message):
        logger.error("gui_error message=%s", message)
        if self.headless_test:
            self.headless_error = str(message)
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("PorePort error")
        dialog.resize(760, 420)
        layout = QVBoxLayout(dialog)
        details = QPlainTextEdit()
        details.setReadOnly(True)
        details.setPlainText(message)
        details.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(details)
        actions = QHBoxLayout()
        copy_button = QPushButton("Copy error")
        copy_button.clicked.connect(lambda: QApplication.clipboard().setText(message))
        close_button = QPushButton("Close")
        close_button.clicked.connect(dialog.accept)
        actions.addStretch()
        actions.addWidget(copy_button)
        actions.addWidget(close_button)
        layout.addLayout(actions)
        dialog.exec()

    def _restore_run(self):
        saved = (
            self.store.run(self.view_run_id)
            if self.view_run_id is not None else self.store.active_run()
        )
        if saved is None:
            if self.view_run_id is not None:
                self.status_label.setText(
                    "Run {0} is not saved on this computer.".format(self.view_run_id)
                )
            return
        if self.view_run_id is None and not Path(saved["local_path"]).is_dir():
            return
        logger.info("gui_run_restore_start run_id=%s", saved["run_id"])
        self.run_id = saved["run_id"]
        saved_state = saved["workflow_state"] or "processing"
        self._accepting_files = self.view_run_id is None and saved_state not in (
            "stopping", "complete", "error"
        )
        self.run_name.setText(saved["run_name"])
        self.folder.setText(saved["local_path"])
        try:
            import json
            metadata = json.loads(saved["metadata_json"] or "{}")
        except (TypeError, ValueError):
            metadata = {}
        kit = metadata.get("barcode_kit", self.barcode_kit.currentText())
        index = self.barcode_kit.findText(kit)
        if index >= 0:
            self.barcode_kit.setCurrentIndex(index)
        lab_index = self.lab_name.findText(metadata.get("lab_name", "OLC"))
        if lab_index >= 0:
            self.lab_name.setCurrentIndex(lab_index)
        report_lab_index = self.report_lab.findData(self.lab_name.currentText())
        if report_lab_index >= 0:
            self.report_lab.setCurrentIndex(report_lab_index)
        selected_barcodes = {str(value).zfill(2) for value in metadata.get("barcode_values", [])}
        self.barcode_values.blockSignals(True)
        for row in range(self.barcode_values.count()):
            item = self.barcode_values.item(row)
            item.setSelected(item.text() in selected_barcodes)
        self.barcode_values.blockSignals(False)
        self._sync_sample_metadata_rows()
        self._set_sample_metadata(metadata.get("sample_metadata"))
        self.run_started = datetime.fromisoformat(saved["started_at"])
        if self._accepting_files:
            self.scanner = StablePod5Scanner(
                Path(saved["local_path"]),
                stable_seconds=self.stability.value(),
            )
            self.uploader = UploadCoordinator(
                self.client,
                self.store,
                self.run_id,
                workers=self.workers.value(),
            )
            self._watch_folder(Path(saved["local_path"]))
        else:
            self.scanner = None
            self.uploader = None
        self._load_cached_reports()
        self.status_section.set_expanded(True)
        self.results_section.set_expanded(True)
        self.run_box.setEnabled(False)
        self.run_section.setVisible(False)
        self.finalize_button.setEnabled(self._accepting_files)
        if self.view_run_id is not None:
            self.pause_button.setEnabled(False)
            self.retry_button.setEnabled(False)
            self.status_label.setText(
                "Viewing saved run {0} (read-only; no file intake).".format(self.run_id)
            )
        elif self._accepting_files:
            self.status_label.setText(
                f"Resumed run {self.run_id}; watching {saved['local_path']}."
            )
        elif self.view_run_id is None:
            self.status_label.setText(
                f"Resumed run {self.run_id} in monitor-only mode "
                f"({saved_state})."
            )
        self.run_details.setText(
            f"Run {self.run_id} | {saved['run_name']} | Input: {saved['local_path']}"
        )
        logger.info("gui_run_restored run_id=%s folder=%s", self.run_id, saved["local_path"])

    def _run_metadata(self):
        metadata = {
            "barcode_kit": self.barcode_kit.currentText(),
            "barcode_values": [int(item.text()) for item in self.barcode_values.selectedItems()],
            "lab_name": self.lab_name.currentText(),
            "reference_database": dict(self._reference_info),
        }
        sample_metadata = self._sample_metadata_payload()
        if sample_metadata:
            metadata["sample_metadata"] = sample_metadata
        return metadata

    def _report_context(self):
        metadata = self._run_metadata()
        return {
            "run_name": self.run_name.text().strip(),
            "lab_name": self.report_lab.currentData() or metadata.get("lab_name", "OLC"),
            "rdims_document_id": self.report_rdims_document_id.text().strip(),
            "sample_metadata": metadata.get("sample_metadata") or {},
            "report_state": self.report_state.currentText(),
            "reviewer_name": self.reviewer_name.text().strip(),
            "approval_timestamp": self.approval_timestamp.text().strip(),
            "digital_signature": self.digital_signature.text().strip(),
            "report_fields_locked": self.report_fields_lock.isChecked(),
            "version": "NanoporeCloudGUI {0}".format(__version__),
            "reference_database": metadata.get("reference_database") or dict(self._reference_info),
            "analysis_generated_at": datetime.now(timezone.utc).isoformat(),
        }

    def _set_sample_metadata(self, sample_metadata):
        self.sample_metadata.setRowCount(0)
        for sample in (sample_metadata or {}).get("samples", []):
            barcode = str(sample.get("barcode", "")).zfill(2)
            item = self.barcode_values.findItems(barcode, Qt.MatchFlag.MatchExactly)
            if not item:
                continue
            item[0].setSelected(True)
        self._sync_sample_metadata_rows()
        for sample in (sample_metadata or {}).get("samples", []):
            barcode = str(sample.get("barcode", "")).zfill(2)
            rows = self.sample_metadata.findItems(barcode, Qt.MatchFlag.MatchExactly)
            if not rows:
                continue
            row = rows[0].row()
            for column, key in enumerate(("barcode", "seqid", "olnid")):
                if column:
                    self.sample_metadata.item(row, column).setText(str(sample.get(key, "")))

    def _render_results(self, status, processing):
        """Show outputs from the enriched report nested in the API response."""
        report = status.get("report")
        manifest = status.get("result_manifest")
        if not isinstance(manifest, dict) or not isinstance(manifest.get("outputs"), list):
            manifest = report if isinstance(report, dict) and isinstance(report.get("outputs"), list) else None
        if manifest is None:
            candidate = processing.get("result_manifest")
            manifest = candidate if isinstance(candidate, dict) and isinstance(candidate.get("outputs"), list) else None
        if manifest is None and isinstance(status.get("outputs"), list):
            manifest = status
        outputs = manifest.get("outputs", []) if manifest is not None else []
        manifest_url = (
            status.get("result_manifest_url")
            or status.get("latest_result_url")
            or status.get("result_url")
            or status.get("url")
            or processing.get("result_manifest_url")
            or processing.get("latest_result_url")
        )
        self.result_outputs.clear()
        for output in outputs:
            if isinstance(output, dict):
                label = output.get("blob_name") or output.get("path") or output.get("name") or output.get("kind", "output")
                self.result_outputs.addItem("{0}: {1}".format(output.get("kind", "output"), label))
        self.live_files_section.toggle.setText(
            "Published files ({0})".format(len(outputs)) if outputs else "Published files"
        )
        if manifest_url and manifest is not None:
            self.result_link.setText('<a href="{0}">Open result manifest</a>'.format(
                html.escape(str(manifest_url), quote=True)
            ))
        else:
            self.result_link.clear()
        self._render_report(report or processing.get("report"))
        self._show_live_iteration(self.live_iteration.currentIndex())

    def _render_report(self, report):
        self._remember_report(report)

    def _remember_report(self, report):
        if not isinstance(report, dict):
            return
        artifacts = [
            str(path) for path in report.get("artifacts", []) if path
        ]
        for iteration, rows in _report_iterations(report):
            self._report_documents[iteration] = {
                "iterations": [{"iteration": iteration, "rows": rows}]
            }
            if artifacts:
                self._report_artifacts[iteration] = artifacts
        self._render_report_documents()
        self._refresh_live_iterations()

    def _refresh_live_iterations(self):
        selected = self.live_iteration.currentData()
        previous_latest = max(
            (self.live_iteration.itemData(i) for i in range(self.live_iteration.count())),
            default=None,
        )
        available = sorted(self._report_documents)
        self.live_iteration.blockSignals(True)
        self.live_iteration.clear()
        for iteration in available:
            self.live_iteration.addItem(str(iteration), iteration)
        choice = (
            available[-1] if available and (selected is None or selected == previous_latest)
            else selected if selected in available else (available[-1] if available else None)
        )
        if choice is not None:
            self.live_iteration.setCurrentIndex(self.live_iteration.findData(choice))
        self.live_iteration.blockSignals(False)
        self._show_live_iteration(self.live_iteration.currentIndex())

    def _move_live_iteration(self, offset):
        index = self.live_iteration.currentIndex() + offset
        if 0 <= index < self.live_iteration.count():
            self.live_iteration.setCurrentIndex(index)

    def _show_live_iteration(self, index):
        self.live_previous_button.setEnabled(index > 0)
        self.live_next_button.setEnabled(
            0 <= index < self.live_iteration.count() - 1
        )
        iteration = self.live_iteration.itemData(index) if index >= 0 else None
        if iteration is None or not self.run_id:
            self._show_live_summary_placeholder(
                "No summary table image available yet."
            )
            self.result_status.setText("Waiting for the first iteration summary.")
            self.report_table.clear()
            self.report_table.setRowCount(0)
            self.report_table.setColumnCount(0)
            self._filter_report_rows()
            return
        path = self._artifact_path(
            iteration, "iteration-{0:06d}-summary.png".format(iteration)
        )
        if path is None:
            fallback = (self.report_root / "run-{0}".format(self.run_id)
                        / "iteration-{0:06d}".format(iteration)
                        / "iteration-{0:06d}-summary.png".format(iteration))
            path = fallback if fallback.is_file() else None
        pixmap = QPixmap(str(path)) if path is not None else QPixmap()
        if pixmap.isNull():
            self._show_live_summary_placeholder(
                "Iteration {0} has no summary image yet.".format(iteration)
            )
        else:
            self._live_pixmap = pixmap
            if self._auto_size_live_result:
                self._auto_size_live_result = False
                screen = self.screen() or QApplication.primaryScreen()
                if screen:
                    available = screen.availableGeometry()
                    target_width = min(
                        max(1280, pixmap.width() + 100),
                        max(560, available.width() - 48),
                    )
                    if target_width > self.width():
                        self.resize(target_width, self.height())
            self._fit_live_summary_image()
        self.result_status.setText("Iteration {0} summary".format(iteration))
        pages = _report_iterations(self._report_documents.get(iteration, {}))
        rows = pages[-1][1] if pages else []
        # Keep identifiers visible even for cached/legacy rows without them.
        columns = ["SEQID", "OLN ID"]
        columns.extend(key for record in rows for key in record
                       if _normalize_query_field(key) not in ("seqid", "olnid")
                       and key not in columns)
        self.report_table.setColumnCount(len(columns))
        self.report_table.setHorizontalHeaderLabels(columns)
        self.report_table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            for column_index, column in enumerate(columns):
                value = row.get(column)
                if value is None and column == "OLN ID":
                    value = row.get("OLNID", row.get("olnid", row.get("oln_id")))
                if value is None and column == "SEQID":
                    value = row.get("seqid", row.get("sequence_id"))
                item = QTableWidgetItem(str(value if value is not None else "-"))
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                item.setBackground(_report_cell_color(column, item.text()))
                self.report_table.setItem(row_index, column_index, item)
        self.report_table.resizeColumnsToContents()
        self._filter_report_rows()
        self._show_live_figures(iteration)

    def _show_live_figures(self, iteration):
        self._set_report_image(self.coverage_image,
            self._artifact_path(iteration, "coverage.png"), "No coverage figure available.")
        self._set_report_image(self.targets_image,
            self._artifact_path(iteration, "detected-targets.png"), "No target figure available.")
        self._set_report_image(self.trend_image,
            self._run_artifact("trend-coverage.png"),
            "Cross-iteration trends require multiple iterations.")

    def _show_live_summary_placeholder(self, message):
        """Fill the Summary tab and wrap empty-state text at narrow widths."""
        self._live_pixmap = QPixmap()
        self.live_summary_image.setPixmap(QPixmap())
        self.live_summary_image.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.live_summary_scroll.setWidgetResizable(True)
        self.live_summary_image.setText(message)
        self.live_summary_scroll.setFixedHeight(135)

    def _fit_live_summary_image(self):
        """Scale a summary to the visible width and show its entire height."""
        if self._live_pixmap.isNull():
            return
        # Once there is a real image, use its scaled size and allow scrolling.
        self.live_summary_scroll.setWidgetResizable(False)
        self.live_summary_image.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )
        viewport_width = self.live_summary_scroll.viewport().width()
        width = max(160, viewport_width - 8) if viewport_width > 200 else max(160, self.width() - 96)
        image = self._live_pixmap
        if image.width() > width:
            image = image.scaledToWidth(
                width, Qt.TransformationMode.SmoothTransformation
            )
        self.live_summary_image.setText("")
        self.live_summary_image.setPixmap(image)
        self.live_summary_image.adjustSize()
        screen = self.screen() or QApplication.primaryScreen()
        max_height = max(135, int(screen.availableGeometry().height() * 0.45)) if screen else 500
        self.live_summary_scroll.setFixedHeight(min(image.height() + 14, max_height))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "live_summary_scroll") and not self._live_pixmap.isNull():
            QTimer.singleShot(0, self._fit_live_summary_image)

    def _render_report_documents(self):
        """Live previews can show every iteration; target report is latest only."""
        iterations = []
        for iteration in sorted(self._report_documents):
            iterations.extend(_report_iterations(self._report_documents[iteration]))
        self._report_pages = iterations
        if not iterations:
            self.report_table.clear()
            self.report_table.setRowCount(0)
            self.report_table.setColumnCount(0)
            self.generate_report_button.setEnabled(False)
            self.report_status.setText("Waiting for a completed iteration.")
            self._refresh_target_report()
            return
        latest = self._selected_report_iteration()
        self.generate_report_button.setEnabled(
            bool(self.run_id and self._target_report_task is None
                 and latest not in self._iteration_result_tasks)
        )
        self._refresh_target_report()

    def _target_report_marker(self):
        if not self.run_id or self._target_report_task is not None:
            return None
        path = self.report_root / "run-{0}".format(self.run_id) / "generated-target-report.json"
        if not path.is_file():
            return None
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            current_fields = {
                "lab_name": self.report_lab.currentData(),
                "report_state": self.report_state.currentText(),
                "rdims_document_id": self.report_rdims_document_id.text().strip(),
                "reviewer_name": self.reviewer_name.text().strip(),
                "approval_timestamp": self.approval_timestamp.text().strip(),
                "digital_signature": self.digital_signature.text().strip(),
            }
            saved = record.get("context") or {}
            if (record.get("iteration") == self._selected_report_iteration()
                    and all(saved.get(key, "") == value
                            for key, value in current_fields.items())
                    and Path(record["pdf"]).is_file()
                    and Path(record["html"]).is_file()):
                return record
        except (OSError, ValueError, KeyError, TypeError):
            logger.exception("target_report_marker_invalid path=%s", path)
        return None

    def _refresh_target_report(self):
        marker = self._target_report_marker()
        self.report_artifacts.clear()
        self.open_pdf_button.setEnabled(marker is not None)
        self.open_report_button.setEnabled(marker is not None)
        self.open_report_folder_button.setEnabled(marker is not None)
        self.report_tabs.setVisible(marker is not None)
        if marker:
            self.report_status.setText("Report generated for latest iteration {0}.".format(marker["iteration"]))
            for path in (marker["pdf"], marker["html"]):
                self.report_artifacts.addItem(Path(path).name)
                self.report_artifacts.item(self.report_artifacts.count() - 1).setData(
                    Qt.ItemDataRole.UserRole, path)
        elif self._target_report_task is not None:
            self.report_status.setText("Generating report for the latest iteration...")
        elif self._report_pages:
            self.report_status.setText(
                "Latest iteration {0}: press Generate report to create a report.".format(
                    self._selected_report_iteration()))
        else:
            self.report_status.setText("Waiting for a completed iteration.")

    def _generate_target_report(self):
        iteration = self._selected_report_iteration()
        if (iteration is None or not self.run_id or self._target_report_task is not None
                or self._iteration_result_tasks.get(iteration)):
            return
        record = self.store.report(self.run_id, iteration)
        if not record or not record["manifest_path"] or not Path(record["manifest_path"]).is_file():
            self._show_error("The latest iteration is not cached yet.")
            return
        cached = load_report(Path(record["manifest_path"]))
        csv_files = [Path(path) for path in cached.get("csv_files", [])]
        if not csv_files or not all(path.is_file() for path in csv_files):
            self._show_error("The latest iteration CSVs are missing.")
            return
        context = self._report_context()
        destination = self.report_root / "run-{0}".format(self.run_id)
        self.generate_report_button.setEnabled(False)
        task = _ApiTask(lambda: generate_target_report(iteration, csv_files, destination, context))
        self._target_report_task = task
        task.succeeded.connect(lambda result, item=iteration: self._target_report_generated(item, result))
        task.failed.connect(lambda error, item=iteration: self._target_report_failed(item, error))
        self._refresh_target_report()
        self.api_pool.start(task)

    def _target_report_generated(self, iteration, result):
        self._target_report_task = None
        self.generate_report_button.setEnabled(bool(self._report_pages))
        self._refresh_target_report()
        self.status_label.setText("Report generated for iteration {0}.".format(iteration))

    def _target_report_failed(self, iteration, error):
        self._target_report_task = None
        self.generate_report_button.setEnabled(bool(self._report_pages))
        self._refresh_target_report()
        self._show_error(str(error))

    def _run_artifact(self, filename):
        if not self.run_id:
            return None
        path = self.report_root / "run-{0}".format(self.run_id) / filename
        return path if path.is_file() else None

    def _stop_file_intake(self):
        current_paths = self.watcher.directories()
        if current_paths:
            self.watcher.removePaths(current_paths)
        self.scanner = None
        if self.uploader and self.uploader.is_idle:
            self.uploader.close(wait=False)
            self.uploader = None

    def _load_cached_reports(self):
        if not self.run_id:
            return
        for record in self.store.reports(self.run_id):
            manifest_path = record["manifest_path"]
            if not manifest_path or not Path(manifest_path).is_file():
                continue
            try:
                report = load_report(Path(manifest_path))
            except (OSError, ValueError):
                logger.exception(
                    "cached_report_load_failed run_id=%s iteration=%s",
                    self.run_id,
                    record["iteration"],
                )
                continue
            self._remember_report(report)

    def _artifact_path(self, iteration, filename):
        for artifact in self._report_artifacts.get(iteration, []):
            path = Path(artifact)
            if path.name.lower() == filename.lower() and path.is_file():
                return path
        return None

    @staticmethod
    def _set_report_image(label, path, missing_text):
        if not path:
            label.setPixmap(QPixmap())
            label.setText(missing_text)
            return
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            label.setPixmap(QPixmap())
            label.setText("Could not load {0}.".format(path.name))
            return
        label.setText("")
        label.setPixmap(
            pixmap.scaled(
                900,
                520,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def _pdf_report_path(self, iteration):
        marker = self._target_report_marker()
        return Path(marker["pdf"]) if marker and marker["iteration"] == iteration else None

    def _open_selected_pdf(self):
        path = self._pdf_report_path(self._selected_report_iteration())
        if path:
            self._open_path(path)

    def _html_report_path(self, iteration):
        marker = self._target_report_marker()
        return Path(marker["html"]) if marker and marker["iteration"] == iteration else None

    def _selected_report_iteration(self):
        return max(self._report_documents) if self._report_documents else None

    def _open_selected_report(self):
        iteration = self._selected_report_iteration()
        path = self._html_report_path(iteration)
        if path:
            self._open_path(path)

    def _open_selected_report_folder(self):
        iteration = self._selected_report_iteration()
        marker = self._target_report_marker()
        if marker:
            self._open_path(Path(marker["pdf"]).parent)

    @staticmethod
    def _open_path(path: Path):
        path = Path(path).resolve()
        webbrowser.open(path.as_uri())

def repository_reference_path():
    # Share the packaged-asset lookup used by the logo and report builder.
    return (repository_asset("PoreSippRDB_240509.fasta")
            or repository_asset("PoreSippRDB_240509.txt"))

def requests_error():
    # Avoid importing requests in the UI module just for its exception class.
    from requests import RequestException
    return RequestException


def _format_bytes(value):
    units = ("B", "KB", "MB", "GB", "TB")
    number = float(value)
    for unit in units:
        if number < 1024 or unit == units[-1]:
            return f"{number:.1f} {unit}"
        number /= 1024
    return f"{number:.1f} TB"


def _format_duration(seconds):
    seconds = max(0, int(seconds))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m {seconds}s"


def _format_processing_details(processing):
    details = [
        f"Status: {processing.get('status', '--')}",
        f"Message: {processing.get('message') or '--'}",
        f"Retryable: {processing.get('retryable', '--')}",
        f"Exit code: {processing.get('exit_code', '--')}",
        f"Error: {processing.get('error') or '--'}",
        f"Current iteration: {processing.get('current_generation') or processing.get('active_generation') or '--'}",
        f"Task owner iteration: {processing.get('task_owner_generation') or '--'}",
        f"Finished: {processing.get('finished_at') or '--'}",
    ]
    return " | ".join(details)



def _normalize_query_field(name):
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def _tokenize_summary_query(query):
    tokens = []
    pos = 0
    while pos < len(query):
        if query[pos].isspace():
            pos += 1
            continue
        if query[pos] in "()":
            tokens.append(query[pos])
            pos += 1
            continue
        start = pos
        quoted = False
        while pos < len(query):
            char = query[pos]
            if char == '"':
                quoted = not quoted
                pos += 1
            elif not quoted and (char.isspace() or char in "()"):
                break
            else:
                pos += 1
        if quoted:
            raise ValueError("Unclosed quotation mark")
        tokens.append(query[start:pos])
    return tokens


def _compile_summary_query(query):
    """Compile a small, safe case-insensitive boolean search over table rows.

    AND binds more tightly than OR; adjacent terms imply AND.  A question
    mark matches one character and an asterisk matches any number of them.
    """
    tokens = _tokenize_summary_query(query)
    if not tokens:
        return lambda row: True
    position = 0

    def peek():
        return tokens[position] if position < len(tokens) else None

    def consume():
        nonlocal position
        token = peek()
        position += 1
        return token

    def atom():
        token = consume()
        if token is None:
            raise ValueError("Expected a search term")
        if token == "(":
            expression = disjunction()
            if peek() != ")":
                raise ValueError("Missing closing parenthesis")
            consume()
            return expression
        if token == ")" or token.upper() in ("AND", "OR", "NOT"):
            raise ValueError("Expected a search term, got {0}".format(token))
        field = None
        if ":" in token:
            field, token = token.split(":", 1)
            field = _normalize_query_field(field)
            if not field:
                raise ValueError("A column name is required before ':'")
            if not token:
                token = consume()
                if token is None or token in "()" or token.upper() in ("AND", "OR", "NOT"):
                    raise ValueError("A search term is required after ':'")
        if token.startswith('"') or token.endswith('"'):
            if len(token) < 2 or not (token.startswith('"') and token.endswith('"')):
                raise ValueError("Quotes must surround the entire search term")
            token = token[1:-1]
        if not token:
            raise ValueError("Empty search term")
        pattern = re.compile("".join("." if char == "?" else ".*" if char == "*"
                                     else re.escape(char) for char in token), re.I | re.S)
        def matches(row):
            values = (value for key, value in row.items()
                      if field is None or _normalize_query_field(key) == field)
            return any(pattern.search(str(value)) for value in values)
        return matches

    def negation():
        if peek() is not None and peek().upper() == "NOT":
            consume()
            child = negation()
            return lambda row: not child(row)
        return atom()

    def conjunction():
        left = negation()
        while peek() is not None and peek() != ")" and peek().upper() != "OR":
            if peek().upper() == "AND":
                consume()
                if peek() is None or peek() == ")":
                    raise ValueError("AND needs a term after it")
            right = negation()
            previous = left
            left = lambda row, a=previous, b=right: a(row) and b(row)
        return left

    def disjunction():
        left = conjunction()
        while peek() is not None and peek().upper() == "OR":
            consume()
            if peek() is None or peek() == ")":
                raise ValueError("OR needs a term after it")
            right = conjunction()
            previous = left
            left = lambda row, a=previous, b=right: a(row) or b(row)
        return left

    result = disjunction()
    if position != len(tokens):
        raise ValueError("Unexpected token: {0}".format(peek()))
    return result


def _report_iterations(report):
    if not isinstance(report, dict):
        return []
    raw_iterations = report.get("iterations", [])
    if isinstance(raw_iterations, dict):
        raw_iterations = [
            {"iteration": iteration, "rows": rows}
            for iteration, rows in raw_iterations.items()
        ]
    pages = []
    for index, entry in enumerate(raw_iterations, 1):
        if not isinstance(entry, dict):
            continue
        iteration = entry.get("iteration", index)
        rows = entry.get("rows", [])
        if isinstance(rows, dict):
            rows = [rows]
        rows = [row for row in rows if isinstance(row, dict)]
        if rows:
            pages.append((iteration, rows))
    return pages


def _report_cell_color(column, value):
    if not value or value == "-":
        return QColor("white")
    column_key = str(column).strip().lower()
    try:
        number = float(str(value).replace("%", "").replace(",", ""))
    except ValueError:
        return QColor("white")

    if "coverage" in column_key:
        return QColor("#d3d3d3") if number < COVERAGE_SUFFICIENT_THRESHOLD else QColor("#b7d8ef")
    if "target" in column_key and "detected" in column_key:
        return QColor("#d3d3d3") if number == 0 else QColor("#b7d8ef")
    if column_key not in ("seqid", "olnid", "csv"):
        return QColor("#d3d3d3") if number == 0 else QColor("#b7d8ef")
    return QColor("white")
