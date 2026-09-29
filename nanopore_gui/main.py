from __future__ import annotations

import argparse
import html
import os
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication, QLabel
from PySide6.QtGui import QIcon
from PySide6.QtCore import QUrl

if __package__:
    from .app_logging import configure_logging
    from .api import FoodPortClient
    from .storage import QueueStore
    from .reports import repository_asset
    from .ui import MainWindow
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from nanopore_gui.app_logging import configure_logging
    from nanopore_gui.api import FoodPortClient
    from nanopore_gui.storage import QueueStore
    from nanopore_gui.reports import repository_asset
    from nanopore_gui.ui import MainWindow

DEFAULT_API = "https://foodport-dev.cloud-nuage.inspection.gc.ca/en-ca/api"


def _ssl_verify_setting():
    value = os.getenv("FOODPORT_VERIFY_SSL", "false").strip().lower()
    if value in {"0", "false", "no", "off"}:
        return False
    return os.getenv("FOODPORT_CA_BUNDLE") or True


def _arguments(argv=None):
    parser = argparse.ArgumentParser(description="PorePort GUI")
    parser.add_argument(
        "--test-run",
        action="store_true",
        help="prefill the GUI with the repository's small POD5 test fixture",
    )
    parser.add_argument(
        "--test-directory",
        type=Path,
        help=(
            "local POD5 source for real staged uploads; without this option "
            "--test-run uses the Azure blob fixture"
        ),
    )
    parser.add_argument(
        "--test-wave-delay",
        type=float,
        default=150.0,
        help="seconds between test release waves (default: 150)",
    )
    parser.add_argument(
        "--test-no-finalize",
        action="store_true",
        help="leave a test run open after the final release wave",
    )
    parser.add_argument(
        "--view-run", type=int, metavar="RUN_ID",
        help="open a locally saved run in read-only report-viewing mode",
    )
    return parser.parse_args(argv)


def run(argv=None):
    arguments = _arguments(argv)
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "CFIA.NanoporeCloudWorkflow"
            )
        except (AttributeError, OSError):
            pass
    app = QApplication(sys.argv)
    app.setApplicationName("PorePort")
    task_icon = repository_asset("cfia.jpg")
    if task_icon:
        app.setWindowIcon(QIcon(str(task_icon)))
    data_dir = Path(os.getenv("APPDATA", Path.home())) / "NanoporeCloudGUI"
    data_dir.mkdir(parents=True, exist_ok=True)
    log_path = configure_logging(data_dir / "logs")
    if arguments.test_directory and not arguments.test_run:
        raise SystemExit("--test-directory requires --test-run")
    if arguments.view_run is not None and arguments.view_run <= 0:
        raise SystemExit("--view-run requires a positive run ID")
    if arguments.view_run is not None and arguments.test_run:
        raise SystemExit("--view-run cannot be combined with --test-run")
    if arguments.test_wave_delay < 0:
        raise SystemExit("--test-wave-delay must be non-negative")
    test_directory = (
        arguments.test_directory.resolve()
        if arguments.test_directory
        else None
    )
    window = MainWindow(
        FoodPortClient(
            os.getenv("FOODPORT_API_BASE", DEFAULT_API),
            verify=_ssl_verify_setting(),
        ),
        QueueStore(data_dir / "queue.sqlite3"),
        test_mode=arguments.test_run,
        test_directory=test_directory,
        test_wave_delay=arguments.test_wave_delay,
        test_auto_finalize=not arguments.test_no_finalize,
        view_run_id=arguments.view_run,
    )
    log_link = QLabel()
    log_link.setText(
        '<a href="{0}">Diagnostic log: {1}</a>'.format(
            html.escape(QUrl.fromLocalFile(str(log_path.resolve())).toString(), quote=True),
            html.escape(str(log_path)),
        )
    )
    log_link.setOpenExternalLinks(True)
    log_link.setToolTip("Open the diagnostic log in your default application")
    window.statusBar().addWidget(log_link, 1)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    run()
