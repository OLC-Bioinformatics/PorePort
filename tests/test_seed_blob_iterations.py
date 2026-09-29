"""Offline regression tests for tools/seed_blob_iterations.py."""
from pathlib import Path
from types import SimpleNamespace, ModuleType
import sys
from unittest.mock import Mock

import pytest



import importlib.util


def _load_tool(filename, module_name):
    # Load as a standalone script, matching `python tools/<filename>`.
    path = Path(__file__).resolve().parents[1] / "tools" / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


seed = _load_tool("seed_blob_iterations.py", "seed_blob_iterations_tested")

def test_default_release_plan_and_waits():
    args = seed.parse_args(["--run-name", "test-run"])
    files, barcodes, sizes, waits = seed.validate_arguments(args)
    assert len(files) == 23
    assert barcodes == (3, 4)
    assert sizes == (3, 7, 2, 5, 6)
    assert waits == (150.0,) * 4


def test_rejects_invalid_plan_before_creating_run():
    args = seed.parse_args(["--run-name", "test-run", "--release-size", "22"])
    with pytest.raises(ValueError):
        seed.validate_arguments(args)
    with pytest.raises(ValueError):
        seed.normalize_wait_plan([1, 2], 5)
    with pytest.raises(ValueError):
        seed.normalize_wait_plan([-1], 2)


@pytest.fixture
def fake_azure(monkeypatch):
    azure = ModuleType("azure")
    core = ModuleType("azure.core")
    exceptions = ModuleType("azure.core.exceptions")
    storage = ModuleType("azure.storage")
    blob = ModuleType("azure.storage.blob")
    exceptions.ResourceNotFoundError = type("ResourceNotFoundError", (Exception,), {})
    blob.BlobClient = type("BlobClient", (), {"from_blob_url": staticmethod(lambda url: Mock())})
    for name, module in [("azure", azure), ("azure.core", core),
                         ("azure.core.exceptions", exceptions),
                         ("azure.storage", storage), ("azure.storage.blob", blob)]:
        monkeypatch.setitem(sys.modules, name, module)
    return blob.BlobClient


def test_copy_failure_never_marks_file_complete(monkeypatch, fake_azure):
    client = Mock()
    client.prepare_file.return_value = {
        "upload_url": "https://example.blob.core.windows.net/raw/one.pod5?sig=fake",
        "file_id": 12,
    }
    source = Mock()
    source.get_blob_properties.return_value = SimpleNamespace(size=5)
    destination = Mock()
    destination.get_blob_properties.return_value = SimpleNamespace(
        copy=SimpleNamespace(status="failed", status_description="copy rejected")
    )
    service = Mock()
    service.get_blob_client.side_effect = [source, destination]
    monkeypatch.setattr(seed, "source_url", lambda *a: "https://source.example/file")
    # Azure imports are inside copy_file; use installed SDK in the GUI environment.
    from azure.storage.blob import BlobClient
    monkeypatch.setattr(BlobClient, "from_blob_url", lambda url: Mock())
    args = seed.parse_args(["--run-name", "test-run"])
    with pytest.raises(RuntimeError, match="copy failed"):
        seed.copy_file(client, service, "example", "key", args, 42,
                       "one.pod5", 1)
    client.complete_file.assert_not_called()
    client.finalize.assert_not_called()


def test_successful_copy_returns_prepared_record(monkeypatch, fake_azure):
    client = Mock()
    client.prepare_file.return_value = {
        "upload_url": "https://example.blob.core.windows.net/raw/one.pod5?sig=fake",
        "file_id": 12,
    }
    source = Mock()
    source.get_blob_properties.return_value = SimpleNamespace(size=5)
    destination = Mock()
    destination.get_blob_properties.return_value = SimpleNamespace(
        copy=SimpleNamespace(status="success")
    )
    service = Mock()
    service.get_blob_client.side_effect = [source, destination]
    monkeypatch.setattr(seed, "source_url", lambda *a: "https://source.example/file")
    from azure.storage.blob import BlobClient
    upload = Mock()
    monkeypatch.setattr(BlobClient, "from_blob_url", lambda url: upload)
    args = seed.parse_args(["--run-name", "test-run"])
    prepared = seed.copy_file(client, service, "example", "key", args, 42,
                              "one.pod5", 1)
    assert prepared["file_id"] == 12
    upload.start_copy_from_url.assert_called_once()
    client.complete_file.assert_not_called()  # main completes only after whole wave copies.
