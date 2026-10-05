import requests
import pytest

from nanopore_gui.api import FoodPortClient, FoodPortError


def test_login_network_error_is_normalized(monkeypatch):
    client = FoodPortClient("https://example.test")

    def fail(*args, **kwargs):
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(client.session, "post", fail)

    with pytest.raises(FoodPortError) as error:
        client.login("operator", "password")

    assert error.value.status == 0
    assert "Network unavailable" in error.value.detail


def test_foodport_lifecycle_uses_authenticated_api_requests(monkeypatch):
    class Response:
        ok = True
        status_code = 200
        reason = "OK"

        def __init__(self, data):
            self.data = data

        def json(self):
            return self.data

    client = FoodPortClient("https://example.test")
    requested = []
    responses = iter((
        Response({"token": "token"}),
        Response({"run_id": 42}),
        Response({"workflow_state": "uploading"}),
        Response({"workflow_state": "uploading"}),
        Response({"file_id": 9, "upload_url": "https://blob.test/file"}),
        Response({"workflow_state": "ready"}),
    ))

    def post(url, **kwargs):
        return next(responses)

    def request(method, url, **kwargs):
        requested.append((method, url, kwargs))
        return next(responses)

    monkeypatch.setattr(client.session, "post", post)
    monkeypatch.setattr(client.session, "request", request)

    client.login("operator", "password")
    assert client.create_run("run") == {"run_id": 42}
    assert client.status(42)["workflow_state"] == "uploading"
    assert client.latest_result(42)["workflow_state"] == "uploading"
    assert client.prepare_file(42, "pass/sample.pod5", 10)["file_id"] == 9
    assert client.complete_file(42, 9)["workflow_state"] == "ready"
    assert [item[0] for item in requested] == ["POST", "GET", "GET", "POST", "POST"]
    assert requested[2][1].endswith("/nanopore/runs/42/results/latest/")
    assert all(item[2]["headers"]["Authorization"] == "Token token" for item in requested)


def test_create_run_includes_optional_run_metadata(monkeypatch):
    class Response:
        ok = True
        status_code = 201
        reason = "Created"

        def json(self):
            return {"run_id": 42}

    client = FoodPortClient("https://example.test")
    client._token = "token"
    captured = {}

    def request(method, url, **kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(client.session, "request", request)
    client.create_run(
        "run", {"sample_metadata": {"sample_id": "S1"}},
    )

    assert captured["json"] == {
        "run_name": "run",
        "sample_metadata": {"sample_id": "S1"},
    }


def test_create_run_logs_safe_payload_shape(monkeypatch, caplog):
    class Response:
        ok = True
        status_code = 201
        reason = "Created"

        def json(self):
            return {"run_id": 42}

    client = FoodPortClient("https://example.test")
    client._token = "token"
    monkeypatch.setattr(client.session, "request", lambda *args, **kwargs: Response())

    with caplog.at_level("INFO", logger="nanopore_gui.api"):
        client.create_run(
            "260916-nanopore",
            {
                "barcode_kit": "SQK-RBK114-24",
                "barcode_values": [1],
                "sample_metadata": {"samples": [{"barcode": 1, "seqid": "2026-MIN-0001"}]},
            },
        )

    message = next(record.message for record in caplog.records if record.message.startswith("run_create_payload"))
    assert "metadata_keys=['barcode_kit', 'barcode_values', 'sample_metadata']" in message
    assert "barcode_values=[1]" in message
    assert "sample_count=1" in message
    assert "token" not in message


def test_http_error_preserves_non_json_response_body():
    class Response:
        ok = False
        status_code = 500
        reason = "Internal Server Error"
        text = "backend traceback reference"

        def json(self):
            raise ValueError

    with pytest.raises(FoodPortError) as error:
        FoodPortClient._decode(Response())

    assert error.value.status == 500
    assert error.value.detail == "backend traceback reference"
    assert error.value.request is None


def test_http_error_includes_safe_request_context():
    class Response:
        ok = False
        status_code = 500
        reason = "Internal Server Error"
        text = "Internal Server Error"

        def json(self):
            return {}

    with pytest.raises(FoodPortError) as error:
        FoodPortClient._decode(Response(), "POST /nanopore/runs/")

    assert "Request: POST /nanopore/runs/" in str(error.value)


@pytest.mark.parametrize(
    ("status", "payload", "expected"),
    [
        (400, {"error": "Run name is invalid.", "detail": "Bad request."}, "Run name is invalid."),
        (409, {"run_name": ["A run with this name already exists."]}, "A run with this name already exists."),
        (500, {"detail": "Unexpected backend failure."}, "Unexpected backend failure."),
    ],
)
def test_backend_error_formats_are_normalized(status, payload, expected):
    class Response:
        ok = False
        status_code = status
        reason = "Server Error"
        text = ""
        headers = {}

        def json(self):
            return payload

    with pytest.raises(FoodPortError) as raised:
        FoodPortClient._decode(Response(), "POST /nanopore/runs/")

    assert raised.value.status == status
    assert raised.value.detail == expected


def test_backend_error_includes_request_id():
    class Response:
        ok = False
        status_code = 409
        reason = "Conflict"
        text = ""
        headers = {}

        def json(self):
            return {
                "run_name": ["A run with this name already exists."],
                "request_id": "req-123",
            }

    with pytest.raises(FoodPortError) as raised:
        FoodPortClient._decode(Response(), "POST /nanopore/runs/")

    assert raised.value.request_id == "req-123"
    assert "Request ID: req-123" in str(raised.value)


def test_backend_error_uses_request_id_header_when_body_omits_it():
    class Response:
        ok = False
        status_code = 500
        reason = "Server Error"
        text = ""
        headers = {"X-Request-ID": "header-456"}

        def json(self):
            return {"detail": "Unexpected backend failure."}

    with pytest.raises(FoodPortError) as raised:
        FoodPortClient._decode(Response())

    assert raised.value.request_id == "header-456"


def test_pairing_exchange_is_public_and_stores_only_foodport_token(monkeypatch):
    class Response:
        ok = True
        status_code = 200
        reason = "OK"

        def json(self):
            return {"token": "foodport-token"}

    client = FoodPortClient("https://example.test")
    requested = []

    def request(method, url, **kwargs):
        requested.append((method, url, kwargs))
        return Response()

    monkeypatch.setattr(client.session, "request", request)
    client.exchange_pairing("pairing-1", "123456")

    assert client.authenticated
    assert requested[0][2]["json"] == {"pairing_id": "pairing-1", "code": "123456"}


def test_pairing_start_accepts_backend_approval_url(monkeypatch):
    class Response:
        ok = True
        status_code = 200
        reason = "OK"

        def json(self):
            return {"pairing_id": "pairing-1", "approval_url": "https://example.test/approve"}

    client = FoodPortClient("https://example.test")
    monkeypatch.setattr(client.session, "request", lambda *args, **kwargs: Response())

    assert client.start_pairing()["approval_url"] == "https://example.test/approve"


def test_iteration_result_downloads_manifest_without_foodport_auth(monkeypatch):
    class Response:
        ok = True
        status_code = 200
        reason = "OK"

        def __init__(self, data):
            self.data = data

        def json(self):
            return self.data

    client = FoodPortClient("https://example.test")
    client._token = "foodport-token"
    requests_seen = []

    def request(method, url, **kwargs):
        requests_seen.append((method, url, kwargs))
        return Response({"iteration": 2, "report_url": "https://blob.test/result.json"})

    def get(url, **kwargs):
        requests_seen.append(("GET", url, kwargs))
        return Response({"report": {"iterations": []}, "outputs": []})

    monkeypatch.setattr(client.session, "request", request)
    monkeypatch.setattr(client._transfer_session(), "get", get)

    result = client.iteration_result(42, 2)

    assert result["result_manifest"]["outputs"] == []
    assert requests_seen[0][1].endswith("/nanopore/runs/42/results/2/")
    assert requests_seen[0][2]["headers"]["Authorization"] == "Token foodport-token"
    assert requests_seen[1][1] == "https://blob.test/result.json"
    assert "Authorization" not in requests_seen[1][2]


def test_revoke_token_clears_token_when_backend_logout_fails(monkeypatch):
    client = FoodPortClient("https://example.test")
    client._token = "token"

    def fail(*args, **kwargs):
        raise FoodPortError(503, "unavailable")

    monkeypatch.setattr(client, "_request", fail)
    with pytest.raises(FoodPortError):
        client.revoke_token()
    assert not client.authenticated


def test_upload_reports_bytes_read(monkeypatch, tmp_path):
    class Response:
        ok = True
        status_code = 201
        reason = "Created"
        def json(self):
            return {}
    client = FoodPortClient("https://example.test")
    progress = []
    source = tmp_path / "sample.pod5"
    source.write_bytes(b"data")
    calls = []
    def put(url, **kwargs):
        calls.append(kwargs)
        if kwargs["params"]["comp"] == "block":
            assert kwargs["data"] == b"data"
        return Response()
    monkeypatch.setattr(client._transfer_session(), "put", put)
    client.upload_blob("https://blob.test/sas", str(source), progress.append)
    assert progress == [4]
    assert [call["params"]["comp"] for call in calls] == ["block", "blocklist"]
    client.close()

def test_api_error_uses_server_detail():
    class Response:
        ok = False
        status_code = 422
        reason = "Unprocessable Entity"

        def json(self):
            return {"detail": "invalid run"}

    error = None
    try:
        FoodPortClient._decode(Response())
    except FoodPortError as raised:
        error = raised

    assert error is not None
    assert error.status == 422
    assert error.detail == "invalid run"


def test_upload_request_does_not_add_foodport_auth(monkeypatch, tmp_path):
    class Response:
        ok = True
        status_code = 201
        reason = "Created"
        def json(self):
            return {}
    captured = []
    def put(url, **kwargs):
        captured.append((url, kwargs))
        return Response()
    client = FoodPortClient("https://example.test")
    client._token = "secret"
    monkeypatch.setattr(client._transfer_session(), "put", put)
    source = tmp_path / "sample.pod5"
    source.write_bytes(b"data")
    sas = "https://blob.test/sas?sig=upload-only"
    client.upload_blob(sas, str(source))
    assert len(captured) == 2
    assert [item[1]["params"]["comp"] for item in captured] == ["block", "blocklist"]
    assert all(url == sas for url, _ in captured)
    assert all("Authorization" not in kwargs["headers"] for _, kwargs in captured)
    assert all(kwargs["headers"]["x-ms-version"] == "2019-12-12" for _, kwargs in captured)
    assert captured[-1][1]["headers"]["x-ms-blob-content-type"] == "application/octet-stream"
    client.close()

def test_tls_verification_setting_applies_to_both_sessions():
    client = FoodPortClient("https://example.test", verify=False)

    assert client.session.verify is False
    assert client._transfer_session().verify is False


# Transfer sessions are isolated from the authenticated API session.
def test_transfer_session_is_thread_local_and_reused():
    import threading
    client = FoodPortClient("https://example.test")
    main = client._transfer_session()
    worker = []
    thread = threading.Thread(target=lambda: worker.append(client._transfer_session()))
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert main is client._transfer_session()
    assert worker[0] is not main and main is not client.session
    assert client._transfer_sessions == {main, worker[0]}
    client.close()
    assert not client._transfer_sessions


def test_processing_status_returns_only_dict(monkeypatch):
    client = FoodPortClient("https://example.test")
    for value, expected in (({"state": "running"}, {"state": "running"}),
                            (None, {}), (["bad"], {})):
        monkeypatch.setattr(client, "status", lambda run_id, v=value: {"processing": v})
        assert client.processing_status(42) == expected


def test_latest_result_uses_unauthenticated_transfer_session(monkeypatch):
    client = FoodPortClient("https://example.test")
    client.set_token("secret")
    seen = []

    class Response:
        ok = True
        status_code = 200
        def __init__(self, payload):
            self.payload = payload
        def json(self):
            return self.payload

    def api_request(method, url, **kwargs):
        seen.append((url, kwargs))
        return Response({"result_manifest_url": "https://blob.test/manifest.json"})

    def transfer_get(url, **kwargs):
        seen.append((url, kwargs))
        return Response({"outputs": []})

    monkeypatch.setattr(client.session, "request", api_request)
    monkeypatch.setattr(client._transfer_session(), "get", transfer_get)
    assert client.latest_result(42)["result_manifest"] == {"outputs": []}
    assert seen[0][1]["headers"]["Authorization"] == "Token secret"
    assert "headers" not in seen[1][1]


def test_iteration_csv_outputs_selects_scheduler_results_only():
    client = FoodPortClient("https://example.test")
    manifest = {"result_manifest": {"outputs": [
        {"blob_name": "scheduler/results/a.csv"},
        {"name": "b.csv", "kind": "result"},
        {"name": "c.csv", "kind": "other"},
        {"name": "figure.png", "kind": "result"},
        "invalid entry",
    ]}}
    assert [output.get("blob_name", output.get("name")) for output in
            client.iteration_csv_outputs(manifest)] == ["scheduler/results/a.csv", "b.csv"]


def test_download_result_output_is_atomic_and_has_no_api_token(monkeypatch, tmp_path):
    client = FoodPortClient("https://example.test")
    client.set_token("secret")
    captured = {}

    class Response:
        ok = True
        status_code = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def iter_content(self, chunk_size):
            assert chunk_size == 1024 * 1024
            yield b"first"
            yield b""
            yield b"second"

    def get(url, **kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(client._transfer_session(), "get", get)
    target = tmp_path / "nested" / "result.csv"
    assert client.download_result_output({"sas_url": "https://blob.test/file"}, target) == target
    assert target.read_bytes() == b"firstsecond"
    assert not (target.parent / "result.csv.part").exists()
    assert captured["stream"] is True and "headers" not in captured


def test_download_result_output_removes_partial_file_on_failure(monkeypatch, tmp_path):
    client = FoodPortClient("https://example.test")

    class Response:
        ok = True
        status_code = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def iter_content(self, chunk_size):
            yield b"partial"
            raise requests.ConnectionError("offline")

    monkeypatch.setattr(client._transfer_session(), "get", lambda *a, **k: Response())
    target = tmp_path / "result.csv"
    with pytest.raises(FoodPortError, match="Network unavailable"):
        client.download_result_output({"download_url": "https://blob.test/file"}, target)
    assert not target.exists() and not (tmp_path / "result.csv.part").exists()


def test_download_result_output_requires_url(tmp_path):
    client = FoodPortClient("https://example.test")
    with pytest.raises(FoodPortError, match="does not include a download URL"):
        client.download_result_output({"blob_name": "missing.csv"}, tmp_path / "missing.csv")


def test_download_iteration_csvs_uses_local_basename(monkeypatch, tmp_path):
    client = FoodPortClient("https://example.test")
    seen = []
    def download(output, destination):
        seen.append(destination)
        return destination
    monkeypatch.setattr(client, "download_result_output", download)
    result = {"result_manifest": {"outputs": [
        {"blob_name": "scheduler/results/a.csv"},
        {"blob_name": "scheduler/results/b.csv"},
        {"blob_name": "scheduler/results/chart.png"},
    ]}}
    assert client.download_iteration_csvs(result, tmp_path) == [tmp_path / "a.csv", tmp_path / "b.csv"]
    assert seen == [tmp_path / "a.csv", tmp_path / "b.csv"]


def test_html_error_extracts_title_and_heading():
    class Response:
        ok = False
        status_code = 502
        reason = "Bad Gateway"
        headers = {}
        text = "<html><title>Proxy error</title><h1>Upstream unavailable</h1></html>"
        def json(self):
            raise ValueError("not JSON")
    with pytest.raises(FoodPortError) as raised:
        FoodPortClient._decode(Response(), "GET /nanopore/runs/42/")
    assert raised.value.detail == "Proxy error - Upstream unavailable"
    assert raised.value.request == "GET /nanopore/runs/42/"
