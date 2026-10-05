"""Regression tests for SAS-backed, bounded POD5 uploads."""
import xml.etree.ElementTree as ET
from types import SimpleNamespace
import pytest
from nanopore_gui.api import FoodPortClient, FoodPortError

class Session:
    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail
    def put(self, url, **kwargs):
        self.calls.append((url, kwargs))
        phase = kwargs["params"]["comp"]
        if self.fail == phase:
            return SimpleNamespace(ok=False, status_code=403, headers={},
                                   text="AuthorizationPermissionMismatch",
                                   reason="Forbidden", json=lambda: {})
        return SimpleNamespace(ok=True, status_code=201, headers={},
                               text="", reason="Created", json=lambda: {})

@pytest.mark.parametrize("length", [1, 8 * 1024 * 1024 + 1])
def test_stages_blocks_then_commits_using_same_sas(tmp_path, monkeypatch, length):
    path = tmp_path / "fixture.pod5"
    path.write_bytes(b"x" * length)
    client = FoodPortClient("https://foodport.invalid/api")
    session = Session()
    monkeypatch.setattr(client, "_transfer_session", lambda: session)
    progress = []
    sas = "https://storage.invalid/container/blob?sp=cw&sig=secret"
    client.upload_blob(sas, str(path), progress.append)
    assert sum(progress) == length
    assert len(session.calls) == (length + 8 * 1024 * 1024 - 1) // (8 * 1024 * 1024) + 1
    assert all(url == sas for url, _ in session.calls)
    blocks = [kwargs for _, kwargs in session.calls[:-1]]
    assert all(kwargs["params"]["comp"] == "block" for kwargs in blocks)
    assert all(len(kwargs["data"]) <= 8 * 1024 * 1024 for kwargs in blocks)
    assert len({kwargs["params"]["blockid"] for kwargs in blocks}) == len(blocks)
    commit = session.calls[-1][1]
    assert commit["params"] == {"comp": "blocklist"}
    assert [node.text for node in ET.fromstring(commit["data"])] == [
        kwargs["params"]["blockid"] for kwargs in blocks
    ]
    client.close()

@pytest.mark.parametrize("phase", ["block", "blocklist"])
def test_failed_stage_or_commit_raises_without_reporting_success(tmp_path, monkeypatch, phase):
    path = tmp_path / "fixture.pod5"
    path.write_bytes(b"pod5")
    client = FoodPortClient("https://foodport.invalid/api")
    session = Session(fail=phase)
    monkeypatch.setattr(client, "_transfer_session", lambda: session)
    with pytest.raises(FoodPortError) as exc:
        client.upload_blob("https://storage.invalid/blob?sig=secret", str(path))
    assert exc.value.status == 403
    assert "secret" not in str(exc.value)
    assert session.calls[-1][1]["params"]["comp"] == phase
    client.close()
