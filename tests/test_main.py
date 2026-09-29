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
