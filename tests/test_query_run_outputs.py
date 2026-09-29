"""Offline regression tests for tools/query_run_outputs.py."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from nanopore_gui.api import FoodPortError


import importlib.util


def _load_tool(filename, module_name):
    # Load as a standalone script, matching `python tools/<filename>`.
    path = Path(__file__).resolve().parents[1] / "tools" / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


query = _load_tool("query_run_outputs.py", "query_run_outputs_tested")

def args(tmp_path):
    return SimpleNamespace(run_id=42, output_dir=tmp_path)


def test_not_yet_published_is_retried_without_marking_seen(tmp_path):
    client = Mock()
    client.iteration_result.side_effect = [
        FoodPortError(404, "Iteration results are not published yet."),
        {"report": {"outputs": [{"path": "result.csv", "size_bytes": 7,
                                   "download_url": "https://example/?sig=secret"}]}},
    ]
    seen = set()
    processing = {"reports": [{"generation": 3}]}
    assert query._download_new_iterations(client, args(tmp_path), processing, seen) == [3]
    assert seen == set()
    assert not (tmp_path / "iteration-000003.json").exists()
    query._download_new_iterations(client, args(tmp_path), processing, seen)
    assert seen == {3}
    saved = json.loads((tmp_path / "iteration-000003.json").read_text())
    assert saved["outputs"][0]["download_url"] == "<redacted>"
    assert client.iteration_result.call_count == 2


@pytest.mark.parametrize("status,detail", [
    (404, "Run not found."), (403, "Forbidden"), (502, "Bad gateway")
])
def test_unrelated_errors_are_not_swallowed(tmp_path, status, detail):
    client = Mock()
    client.iteration_result.side_effect = FoodPortError(status, detail)
    seen = set()
    with pytest.raises(FoodPortError):
        query._download_new_iterations(client, args(tmp_path),
                                       {"reports": [{"iteration": 2}]}, seen)
    assert seen == set()


def test_status_saved_without_signed_urls(tmp_path):
    client = Mock()
    client.status.return_value = {
        "run_id": 42, "workflow_state": "running",
        "processing": {"reports": []},
        "nested": {"download_url": "https://example/?sig=secret"},
    }
    status, processing = query._query_once(client, args(tmp_path), set())
    assert status["run_id"] == 42
    assert processing["reports"] == []
    assert json.loads((tmp_path / "status.json").read_text())["nested"]["download_url"] == "<redacted>"


def test_iteration_discovery_deduplicates_and_sorts():
    assert query._published_iterations({"reports": [
        {"generation": "3"}, {"iteration": 1}, {"generation": 3}, {}, "bad"
    ]}) == [1, 3]
