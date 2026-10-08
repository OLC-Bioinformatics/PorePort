"""CLI configuration and startup wiring for the PorePort GUI."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from nanopore_gui import main


@pytest.mark.parametrize("options, expected", [
    ([], {"test_run": False, "test_directory": None,
          "test_wave_delay": 150.0, "test_no_finalize": False, "view_run": None}),
    (["--test-run", "--test-directory", "fixtures", "--test-wave-delay", "2.5"],
     {"test_run": True, "test_directory": Path("fixtures"), "test_wave_delay": 2.5}),
    (["--test-run", "--test-no-finalize"],
     {"test_run": True, "test_no_finalize": True}),
    (["--view-run", "1835"], {"view_run": 1835, "test_run": False}),
])
def test_arguments(options, expected):
    arguments = main._arguments(options)
    for key, value in expected.items():
        assert getattr(arguments, key) == value


@pytest.mark.parametrize("value", ["0", "false", "NO", "Off"])
def test_ssl_verification_disabled_values(monkeypatch, value):
    monkeypatch.setenv("FOODPORT_VERIFY_SSL", value)
    monkeypatch.setenv("FOODPORT_CA_BUNDLE", "/tmp/test-ca.pem")
    assert main._ssl_verify_setting() is False


def test_ssl_verification_defaults_to_disabled(monkeypatch):
    monkeypatch.delenv("FOODPORT_VERIFY_SSL", raising=False)
    monkeypatch.delenv("FOODPORT_CA_BUNDLE", raising=False)
    assert main._ssl_verify_setting() is False


def test_ssl_verification_can_be_enabled(monkeypatch):
    monkeypatch.setenv("FOODPORT_VERIFY_SSL", "true")
    monkeypatch.delenv("FOODPORT_CA_BUNDLE", raising=False)
    assert main._ssl_verify_setting() is True


def test_ssl_verification_uses_ca_bundle(monkeypatch):
    monkeypatch.setenv("FOODPORT_VERIFY_SSL", "true")
    monkeypatch.setenv("FOODPORT_CA_BUNDLE", "/tmp/test-ca.pem")
    assert main._ssl_verify_setting() == "/tmp/test-ca.pem"


@pytest.fixture
def startup(monkeypatch, tmp_path):
    """Mock Qt and I/O without opening a GUI or connecting to FoodPort."""
    created = SimpleNamespace()

    class App:
        def __init__(self, argv):
            created.app = self
            self.name = None
            self.icon = None

        def setApplicationName(self, name):
            self.name = name

        def setWindowIcon(self, icon):
            self.icon = icon

        def exec(self):
            return 0

    class Label:
        def __init__(self):
            created.label = self

        def setText(self, text):
            self.text = text

        def setOpenExternalLinks(self, value):
            self.external_links = value

        def setToolTip(self, text):
            self.tooltip = text

    class StatusBar:
        def addWidget(self, widget, stretch):
            created.status_widget = widget
            created.stretch = stretch

    class Window:
        def __init__(self, client, store, **kwargs):
            created.window = self
            created.client = client
            created.store = store
            created.kwargs = kwargs
            self.bar = StatusBar()

        def statusBar(self):
            return self.bar

        def show(self):
            self.shown = True

    class URL:
        @staticmethod
        def fromLocalFile(path):
            return SimpleNamespace(toString=lambda: Path(path).as_uri())

    monkeypatch.setattr(main, "QApplication", App)
    monkeypatch.setattr(main, "QLabel", Label)
    monkeypatch.setattr(main, "QIcon", lambda path: ("icon", path))
    monkeypatch.setattr(main, "QUrl", URL)
    monkeypatch.setattr(main, "MainWindow", Window)
    monkeypatch.setattr(main, "FoodPortClient", lambda url, **kw: (url, kw))
    monkeypatch.setattr(main, "QueueStore", lambda path: path)
    monkeypatch.setattr(main, "configure_logging", lambda path: path / "nanopore-gui.log")
    monkeypatch.setattr(main, "configure_crash_diagnostics", lambda path: path / "nanopore-gui-fatal-test.log")
    monkeypatch.setattr(main, "repository_asset", lambda name: tmp_path / name)
    monkeypatch.setattr(main.sys, "exit", lambda code: (_ for _ in ()).throw(SystemExit(code)))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("FOODPORT_API_BASE", "https://example.test/api")
    monkeypatch.setenv("FOODPORT_VERIFY_SSL", "true")
    monkeypatch.delenv("FOODPORT_CA_BUNDLE", raising=False)
    return created


def test_view_run_startup_sets_read_only_run_and_clickable_diagnostic_log(startup, tmp_path):
    with pytest.raises(SystemExit) as exit_info:
        main.run(["--view-run", "1835"])
    assert exit_info.value.code == 0
    assert startup.app.name == "PorePort"
    assert startup.app.icon == ("icon", str(tmp_path / "cfia.jpg"))
    assert startup.client == ("https://example.test/api", {"verify": True})
    assert startup.store == tmp_path / "NanoporeCloudGUI" / "queue.sqlite3"
    assert startup.kwargs["view_run_id"] == 1835
    assert startup.kwargs["test_mode"] is False
    assert startup.window.shown is True
    assert startup.label.external_links is True
    assert "Diagnostic log:" in startup.label.text
    assert "nanopore-gui.log" in startup.label.text
    assert "file://" in startup.label.text
    assert startup.status_widget is startup.label


def test_test_directory_is_resolved_and_auto_finalize_flag_is_passed(startup, tmp_path):
    directory = tmp_path / "fixture"
    directory.mkdir()
    with pytest.raises(SystemExit) as exit_info:
        main.run(["--test-run", "--test-directory", str(directory),
                  "--test-wave-delay", "0", "--test-no-finalize"])
    assert exit_info.value.code == 0
    assert startup.kwargs["test_mode"] is True
    assert startup.kwargs["test_directory"] == directory.resolve()
    assert startup.kwargs["test_wave_delay"] == 0
    assert startup.kwargs["test_auto_finalize"] is False
    assert startup.kwargs["view_run_id"] is None


@pytest.mark.parametrize("options, message", [
    (["--test-directory", "fixtures"], "--test-directory requires --test-run"),
    (["--view-run", "0"], "--view-run requires a positive run ID"),
    (["--view-run", "-2"], "--view-run requires a positive run ID"),
    (["--view-run", "1835", "--test-run"], "--view-run cannot be combined"),
    (["--test-wave-delay", "-1"], "--test-wave-delay must be non-negative"),
])
def test_invalid_startup_combinations_are_rejected(startup, options, message):
    with pytest.raises(SystemExit, match=message):
        main.run(options)
    assert not hasattr(startup, "window")


# Headless startup tests never contact FoodPort or create a real run.
@pytest.mark.parametrize("options, message", [
    (["--headless-test-run"], "requires --headless-run-name"),
    (["--headless-test-run", "--test-directory", "."], "requires --headless-run-name"),
    (["--headless-test-run", "--test-directory", "missing", "--headless-run-name", "trial"], "readable directory"),
    (["--headless-test-run", "--test-directory", ".", "--headless-run-name", "trial", "--test-run"], "cannot combine"),
    (["--headless-test-run", "--test-directory", ".", "--headless-run-name", "trial", "--test-no-finalize"], "cannot combine"),
    (["--headless-test-run", "--test-directory", ".", "--headless-run-name", "trial", "--headless-timeout", "0"], "must be positive"),
])
def test_headless_invalid_options_rejected_before_pairing(startup, options, message):
    with pytest.raises(SystemExit, match=message):
        main.run(options)
    assert not hasattr(startup, "window")


def test_headless_arguments():
    parsed = main._arguments(["--headless-test-run", "--headless-run-name", "trial",
                              "--test-directory", "fixtures", "--headless-timeout", "90"])
    assert parsed.headless_test_run is True
    assert parsed.headless_run_name == "trial"
    assert parsed.test_directory == Path("fixtures")
    assert parsed.headless_timeout == 90


def test_headless_pairing_failure_never_creates_run(startup, monkeypatch, tmp_path):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    class Client:
        closed = False
        def start_pairing(self):
            return {"pairing_id": "pair-1", "approval_url": "https://example.test/approve"}
        def exchange_pairing(self, pairing_id, code):
            raise RuntimeError("pairing denied")
        def close(self):
            self.closed = True
    client = Client()
    monkeypatch.setattr(main, "FoodPortClient", lambda *a, **k: client)
    monkeypatch.setattr(main.webbrowser, "open", lambda url: False)
    monkeypatch.setattr(main.getpass, "getpass", lambda *a: "000000")
    with pytest.raises(RuntimeError, match="pairing denied"):
        main.run(["--headless-test-run", "--test-directory", str(fixture),
                  "--headless-run-name", "trial"])
    assert client.closed
    assert not hasattr(startup, "window")


def test_headless_pairing_and_report_gated_success(startup, monkeypatch, tmp_path, capsys):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    calls = []
    class Client:
        def start_pairing(self):
            calls.append("start")
            return {"pairing_id": "pair-1", "approval_url": "https://example.test/approve"}
        def exchange_pairing(self, pairing_id, code):
            calls.append((pairing_id, code))
        def close(self):
            calls.append("close")
    class Store:
        def close(self):
            calls.append("store-close")
    class Window:
        def __init__(self, client, store, **kwargs):
            startup.window = self
            startup.kwargs = kwargs
            self.client, self.store = client, store
            self.run_name = SimpleNamespace(setText=lambda name: calls.append(("name", name)))
            self.headless_error = None
            self.run_id = None
            self._last_status = {}
            self._report_documents = {}
            self._cloud_seed_task = None
            self.test_controller = None
            self._iteration_result_tasks = {}
            self._target_report_task = None
            self.lab_name = SimpleNamespace(setCurrentText=lambda value: calls.append(("lab", value)))
            self.report_lab = SimpleNamespace(findData=lambda value: 0, setCurrentIndex=lambda value: None)
            self.report_state = SimpleNamespace(setCurrentText=lambda value: None)
            self._target_report_marker = lambda: None
            self._generate_target_report = lambda: calls.append("generate-report")
            self.bar = SimpleNamespace(addWidget=lambda *a: None)
        def statusBar(self):
            return self.bar
        def _pairing_succeeded(self, result):
            calls.append("paired")
        def _run_preflight(self):
            calls.append("preflight")
            return True
        def _create_run(self):
            calls.append("create")
            self.run_id = 42
            self._last_status = {"workflow_state": "complete", "processing": {"reports": [{"iteration": 1}]}}
            self._report_documents = {1: {"iterations": [{"iteration": 1, "rows": [{}]}]}}
        def close(self):
            calls.append("window-close")
    class Timer:
        @staticmethod
        def singleShot(milliseconds, callback):
            assert milliseconds == 0
            callback()

        def __init__(self, parent):
            self.timeout = SimpleNamespace(connect=lambda fn: setattr(startup, "check", fn))

        def start(self, interval):
            assert interval == 2000
    def event_loop():
        startup.check()  # completed workflow alone must not count as success
        assert "Headless test complete" not in capsys.readouterr().out
        assert "window-close" not in calls
        return 0
    monkeypatch.setattr(main, "FoodPortClient", lambda *a, **k: Client())
    monkeypatch.setattr(main, "QueueStore", lambda *a: Store())
    monkeypatch.setattr(main, "MainWindow", Window)
    monkeypatch.setattr(main, "QTimer", Timer)
    monkeypatch.setattr(main.webbrowser, "open", lambda url: False)
    monkeypatch.setattr(main.getpass, "getpass", lambda *a: "123456")
    monkeypatch.setattr(main.QApplication, "exec", lambda self: event_loop())
    monkeypatch.setattr(main.QApplication, "quit", lambda self: calls.append("app-quit"), raising=False)
    result = main.run(["--headless-test-run", "--test-directory", str(fixture),
                       "--headless-run-name", "trial"])
    assert result == 1
    assert calls.index(("pair-1", "123456")) < calls.index("create")
    assert startup.kwargs["headless_test"] is True
    assert startup.kwargs["test_mode"] is True
    assert ("name", "trial") in calls
    assert "generate-report" in calls
    assert "window-close" not in calls


def test_headless_cloud_fixture_without_directory(startup, monkeypatch):
    calls = []
    class Client:
        def start_pairing(self):
            return {"pairing_id": "p", "approval_url": "https://example.test/approve"}
        def exchange_pairing(self, pairing_id, code):
            calls.append("paired")
        def close(self):
            pass
    class Field:
        def setText(self, value):
            pass
        def setCurrentText(self, value):
            pass
        def findData(self, value):
            return 0
        def setCurrentIndex(self, value):
            pass
    class Window:
        def __init__(self, client, store, **kwargs):
            startup.kwargs = kwargs
            self.run_name = self.lab_name = self.report_lab = self.report_state = Field()
        def statusBar(self):
            return SimpleNamespace(addWidget=lambda *args: None)
        def _pairing_succeeded(self, data):
            pass
        def _run_preflight(self):
            return True
        def _create_run(self):
            calls.append("created")
    class Timer:
        @staticmethod
        def singleShot(delay, callback):
            callback()
        def __init__(self, parent):
            self.timeout = SimpleNamespace(connect=lambda callback: None)
        def start(self, delay):
            pass
    monkeypatch.setattr(main, "FoodPortClient", lambda *args, **kwargs: Client())
    monkeypatch.setattr(main, "MainWindow", Window)
    monkeypatch.setattr(main, "QTimer", Timer)
    monkeypatch.setattr(main.webbrowser, "open", lambda url: False)
    monkeypatch.setattr(main.getpass, "getpass", lambda *args: "123456")
    assert main.run(["--headless-test-run", "--headless-run-name", "trial"]) == 1
    assert startup.kwargs["test_directory"] is None
    assert startup.kwargs["test_mode"] is True
    assert startup.kwargs["test_auto_finalize"] is True
    assert calls == ["paired", "created"]
