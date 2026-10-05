from __future__ import annotations

import argparse
import html
import getpass
import webbrowser
import time
import os
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication, QLabel
from PySide6.QtGui import QIcon
from PySide6.QtCore import QUrl, QTimer

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
    parser.add_argument(
        "--headless-test-run", action="store_true",
        help="terminal pairing then unattended cloud fixture (or local with "
        "--test-directory)"
    )
    parser.add_argument(
        "--headless-run-name",
        help="unique name required for --headless-test-run"
    )
    parser.add_argument(
        "--headless-timeout", type=int, default=14400,
        help="maximum seconds for unattended test (default: 14400)"
    )
    return parser.parse_args(argv)


def run(argv=None):
    """
    Run the PorePort GUI application with the specified command-line arguments.
    """
    # Parse command-line arguments and perform initial validation
    arguments = _arguments(argv)
    if arguments.headless_test_run:
        if not arguments.headless_run_name:
            raise SystemExit(
                "--headless-test-run requires --headless-run-name"
            )
        if arguments.test_run or arguments.view_run is not None or \
            arguments.test_no_finalize:
            raise SystemExit(
                "headless test cannot combine with --test-run, --view-run "
                "or --test-no-finalize"
            )
        if arguments.test_directory is not None and not \
            arguments.test_directory.is_dir():
            raise SystemExit("--test-directory must be a readable directory")
        if arguments.headless_timeout <= 0:
            raise SystemExit("--headless-timeout must be positive")
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
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
    if arguments.test_directory and not (arguments.test_run or arguments.headless_test_run):
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
    client = FoodPortClient(
            os.getenv("FOODPORT_API_BASE", DEFAULT_API),
            verify=_ssl_verify_setting(),
        )
    if arguments.headless_test_run:
        try:
            pairing = client.start_pairing()
            print("Approve this pairing in a browser on another computer:",
                  pairing["approval_url"], flush=True)
            try:
                webbrowser.open(pairing["approval_url"])
            except Exception as error:
                print("Browser launch unavailable; use the printed URL:", error, file=sys.stderr)
            code = getpass.getpass("One-time pairing code: ")
            client.exchange_pairing(pairing["pairing_id"], code)
        except Exception:
            client.close()
            raise
    window = MainWindow(
        client,
        QueueStore(data_dir / "queue.sqlite3"),
        test_mode=arguments.test_run or arguments.headless_test_run,
        headless_test=arguments.headless_test_run,
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
    if arguments.headless_test_run:
        window.run_name.setText(arguments.headless_run_name)
        window.lab_name.setCurrentText("OLC")
        window.report_lab.setCurrentIndex(window.report_lab.findData("OLC"))
        window.report_state.setCurrentText("Draft")
        window._pairing_succeeded({})
        if not window._run_preflight():
            window.client.close()
            window.store.close()
            raise SystemExit("Headless preflight failed; no run was created")
        started = time.monotonic()
        outcome = {"code": 1}
        def check():
            if outcome.get("finished"):
                return
            if window.headless_error:
                print("Headless test failed:", window.headless_error, file=sys.stderr)
                outcome["finished"] = True
                window.close()
                app.quit()
                return
            if time.monotonic() - started > arguments.headless_timeout:
                print("Headless test timed out; check the run before retrying.", file=sys.stderr)
                outcome["finished"] = True
                window.close()
                app.quit()
                return
            status = window._last_status or {}
            if not window.run_id:
                return
            workflow = status.get("workflow_state")
            if workflow == "error":
                print("Headless test run ended in error:", window.run_id, file=sys.stderr)
                outcome["finished"] = True
                window.close()
                app.quit()
                return
            if workflow != "complete" or window._cloud_seed_task is not None or window.test_controller is not None:
                return
            processing = status.get("processing") or {}
            advertised = []
            for entry in processing.get("reports") or []:
                if isinstance(entry, dict):
                    try:
                        advertised.append(int(entry.get("iteration") or entry.get("generation")))
                    except (TypeError, ValueError):
                        continue
            if not advertised:
                return
            latest = max(advertised)
            if latest in window._iteration_result_tasks or latest not in window._report_documents:
                return
            marker = window._target_report_marker()
            if marker is None or marker.get("iteration") != latest:
                if window._target_report_task is None:
                    window._generate_target_report()
                return
            print("Headless test complete: run", window.run_id, "iteration", latest, flush=True)
            outcome["code"] = 0
            outcome["finished"] = True
            window.close()
            app.quit()
        monitor = QTimer(window)
        monitor.timeout.connect(check)
        monitor.start(2000)
        QTimer.singleShot(0, window._create_run)
        app.exec()
        return outcome["code"]
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    sys.exit(run())
