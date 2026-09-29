from __future__ import annotations

import logging
import os
import re
import threading
from pathlib import Path
from time import monotonic
from urllib.parse import urlsplit

import requests
import urllib3
from urllib3.exceptions import InsecureRequestWarning


logger = logging.getLogger("nanopore_gui.api")


def _error_message(value):
    """Return the first useful error message found in a nested value."""
    if isinstance(value, str) and value.strip():
        return value.strip()

    if isinstance(value, list):
        for item in value:
            message = _error_message(item)
            if message:
                return message

    if isinstance(value, dict):
        for key in ("message", "detail", "error"):
            message = _error_message(value.get(key))
            if message:
                return message

    return None


def _field_error_message(data):
    """Return the first useful field-validation message in an API response."""
    ignored_keys = {"error", "detail", "request_id"}

    for key, value in data.items():
        if key in ignored_keys:
            continue

        message = _error_message(value)
        if message:
            return message

    return None


class FoodPortError(RuntimeError):
    """Error returned by the FoodPort API or an associated network request."""

    def __init__(
        self,
        status: int,
        detail: str,
        request: str | None = None,
        request_id: str | None = None,
    ):
        message = f"{status}: {detail}"

        if request_id:
            message = f"{message}\nRequest ID: {request_id}"

        if request:
            message = f"{message}\nRequest: {request}"

        super().__init__(message)
        self.status = status
        self.detail = detail
        self.request = request
        self.request_id = request_id


class FoodPortClient:
    """HTTP client for FoodPort Nanopore run and upload operations."""

    def __init__(
        self,
        base_url: str,
        timeout: tuple[float, float] = (10, 60),
        verify: bool = True,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._token: str | None = None
        self.verify = verify
        self.session = requests.Session()
        self.session.verify = verify

        if verify is False:
            urllib3.disable_warnings(InsecureRequestWarning)

        self._session_lock = threading.RLock()
        self._transfer_local = threading.local()
        self._transfer_sessions = set()
        self._transfer_sessions_lock = threading.RLock()

    @property
    def authenticated(self) -> bool:
        """Return whether an authentication token is configured."""
        return self._token is not None

    def set_token(self, token: str) -> None:
        """Configure an existing FoodPort token.

        Args:
            token: Nonempty token returned by FoodPort authentication.
        """
        token = str(token or "").strip()
        if not token:
            raise ValueError("FoodPort token must not be empty")
        self._token = token

    def login(self, username: str, password: str) -> None:
        """Authenticate using the legacy username/password endpoint."""
        logger.info("legacy_login_start")
        try:
            with self._session_lock:
                response = self.session.post(
                    f"{self.base_url}/auth/token/",
                    json={"username": username, "password": password},
                    timeout=self.timeout,
                )
        except requests.RequestException as exc:
            raise FoodPortError(
                0,
                f"Network unavailable: {exc}",
            ) from exc

        data = self._decode(response)
        token = data.get("token")
        if not token:
            raise FoodPortError(
                response.status_code,
                "Login response did not contain a token",
            )

        self.set_token(token)
        logger.info("legacy_login_success")

    def logout(self) -> None:
        """Forget the local authentication token."""
        self._token = None
        logger.info("logout_complete")

    def revoke_token(self) -> None:
        """Revoke the current token through FoodPort and forget it locally."""
        if not self._token:
            return

        logger.info("token_revoke_start")
        try:
            self._request("POST", "/auth/entra/logout/")
        finally:
            self._token = None
            logger.info("token_revoke_complete")

    def start_pairing(self) -> dict:
        """Start desktop browser pairing."""
        logger.info("pairing_start_request")
        result = self._public_request(
            "POST",
            "/auth/pairing/start/",
            json={},
        )
        approval_url = result.get("approval_url") or result.get("portal_url")

        if not result.get("pairing_id") or not approval_url:
            raise FoodPortError(502, "Pairing response was incomplete")

        result["approval_url"] = approval_url
        logger.info(
            "pairing_started expires_in=%s",
            result.get("expires_in", "unknown"),
        )
        return result

    def exchange_pairing(self, pairing_id: str, code: str) -> None:
        """Exchange an approved pairing code for an API token."""
        logger.info("pairing_exchange_request")
        result = self._public_request(
            "POST",
            "/auth/pairing/exchange/",
            json={"pairing_id": pairing_id, "code": code},
        )
        token = result.get("token")

        if not token:
            raise FoodPortError(
                502,
                "Pairing response did not contain a token",
            )

        self.set_token(token)
        logger.info(
            "pairing_exchange_success expires_in=%s",
            result.get("expires_in", "unknown"),
        )

    def _public_request(self, method: str, path: str, **kwargs):
        headers = kwargs.pop("headers", {})
        headers["Accept"] = "application/json"
        started = monotonic()
        logger.info(
            "request_start method=%s path=%s authenticated=false",
            method,
            path,
        )

        try:
            with self._session_lock:
                response = self.session.request(
                    method,
                    f"{self.base_url}{path}",
                    headers=headers,
                    timeout=self.timeout,
                    **kwargs,
                )
        except requests.RequestException as exc:
            logger.warning(
                "request_network_error method=%s path=%s error=%s",
                method,
                path,
                exc,
            )
            raise FoodPortError(
                0,
                f"Network unavailable: {exc}",
            ) from exc

        logger.info(
            "request_complete method=%s path=%s status=%s duration_ms=%d",
            method,
            path,
            response.status_code,
            (monotonic() - started) * 1000,
        )
        return self._decode(response, f"{method} {path}")

    def _request(self, method: str, path: str, **kwargs):
        if not self._token:
            raise FoodPortError(401, "Log in to FoodPort first")

        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Token {self._token}"
        headers["Accept"] = "application/json"
        started = monotonic()
        logger.info(
            "request_start method=%s path=%s authenticated=true",
            method,
            path,
        )

        try:
            with self._session_lock:
                response = self.session.request(
                    method,
                    f"{self.base_url}{path}",
                    headers=headers,
                    timeout=self.timeout,
                    **kwargs,
                )
        except requests.RequestException as exc:
            logger.warning(
                "request_network_error method=%s path=%s error=%s",
                method,
                path,
                exc,
            )
            raise FoodPortError(
                0,
                f"Network unavailable: {exc}",
            ) from exc

        logger.info(
            "request_complete method=%s path=%s status=%s duration_ms=%d",
            method,
            path,
            response.status_code,
            (monotonic() - started) * 1000,
        )
        return self._decode(response, f"{method} {path}")

    @staticmethod
    def _decode(response, request=None):
        try:
            data = response.json()
        except ValueError:
            data = {}

        if not response.ok:
            headers = getattr(response, "headers", {})
            request_id = data.get("request_id") or headers.get("X-Request-ID")
            body = getattr(response, "text", "").strip()

            if "<html" in body.lower() or "<!doctype" in body.lower():
                title = re.search(
                    r"<title[^>]*>(.*?)</title>",
                    body,
                    re.IGNORECASE | re.DOTALL,
                )
                heading = re.search(
                    r"<h[1-3][^>]*>(.*?)</h[1-3]>",
                    body,
                    re.IGNORECASE | re.DOTALL,
                )
                detail = " - ".join(
                    re.sub(r"\s+", " ", match.group(1)).strip()
                    for match in (title, heading)
                    if match
                ) or "FoodPort returned an HTML error page"
            else:
                detail = body

            detail = (
                _error_message(data.get("error"))
                or _field_error_message(data)
                or _error_message(data.get("detail"))
                or detail
                or response.reason
            )
            detail = str(detail)[:500]
            logger.warning(
                "request_failed status=%s request=%s detail=%s",
                response.status_code,
                request or "unknown",
                detail,
            )

            if request_id:
                logger.warning(
                    "request_failed_id request_id=%s",
                    request_id,
                )

            raise FoodPortError(
                response.status_code,
                str(detail),
                request,
                request_id,
            )

        return data

    def create_run(
        self,
        run_name: str,
        metadata: dict | None = None,
    ) -> dict:
        """Create a FoodPort Nanopore run."""
        payload = {"run_name": run_name}
        if metadata:
            payload.update(
                {
                    key: value
                    for key, value in metadata.items()
                    if value not in (None, "")
                }
            )

        samples = payload.get("sample_metadata", {}).get("samples", [])
        logger.info(
            "run_create_payload run_name=%s metadata_keys=%s "
            "barcode_values=%s sample_count=%s",
            run_name,
            sorted(key for key in payload if key != "run_name"),
            payload.get("barcode_values", []),
            len(samples) if isinstance(samples, list) else "unknown",
        )
        return self._request("POST", "/nanopore/runs/", json=payload)

    def status(self, run_id: int) -> dict:
        """Return current FoodPort Nanopore run status."""
        return self._request("GET", f"/nanopore/runs/{run_id}/")

    def processing_status(self, run_id: int) -> dict:
        """Return the normalized processing object for a run."""
        status = self.status(run_id)
        processing = status.get("processing") or {}
        return processing if isinstance(processing, dict) else {}

    def latest_result(self, run_id: int) -> dict:
        """Return and dereference the latest published result pointer."""
        result = self._request(
            "GET",
            f"/nanopore/runs/{run_id}/results/latest/",
        )
        return self._dereference_result(result)

    def iteration_result(self, run_id: int, iteration: int) -> dict:
        """Return and dereference one immutable iteration result."""
        result = self._request(
            "GET",
            f"/nanopore/runs/{run_id}/results/{iteration}/",
        )
        return self._dereference_result(result)

    def _dereference_result(self, result: dict) -> dict:
        result_url = (
            result.get("report_url")
            or result.get("result_manifest_url")
            or result.get("latest_result_url")
            or result.get("result_url")
            or result.get("download_url")
            or result.get("url")
        )

        if not result_url or isinstance(result.get("report"), dict):
            return result

        manifest = self._download_json(result_url)
        if isinstance(manifest, dict):
            result["result_manifest"] = manifest
        return result

    def _download_json(self, url: str) -> dict:
        parsed = urlsplit(url)
        logger.info(
            "result_manifest_download_start host=%s path=%s",
            parsed.hostname or "unknown",
            parsed.path or "/",
        )
        try:
            response = self._transfer_session().get(
                url,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            logger.warning(
                "result_manifest_download_network_error error=%s",
                exc,
            )
            raise FoodPortError(
                0,
                f"Network unavailable: {exc}",
            ) from exc

        logger.info(
            "result_manifest_download_complete status=%s",
            response.status_code,
        )
        return self._decode(response, "GET result manifest")

    def _transfer_session(self):
        """Return a transfer session owned by the current worker thread."""
        session = getattr(self._transfer_local, "session", None)
        if session is None:
            session = requests.Session()
            session.verify = self.verify
            self._transfer_local.session = session
            with self._transfer_sessions_lock:
                self._transfer_sessions.add(session)
        return session

    @staticmethod
    def _result_manifest(result: dict) -> dict:
        """Return the dereferenced result manifest from an API response."""
        manifest = result.get("result_manifest")
        if isinstance(manifest, dict):
            return manifest
        report = result.get("report")
        if isinstance(report, dict) and isinstance(report.get("outputs"), list):
            return report
        return result if isinstance(result, dict) else {}

    def result_outputs(self, result: dict) -> list[dict]:
        """Return normalized output entries from a result response."""
        outputs = self._result_manifest(result).get("outputs") or []
        return [entry for entry in outputs if isinstance(entry, dict)]

    def iteration_csv_outputs(self, result: dict) -> list[dict]:
        """Return scheduler result CSV entries from an iteration manifest."""
        csv_outputs = []
        for output in self.result_outputs(result):
            name = str(output.get("blob_name") or output.get("path") or output.get("name") or "")
            kind = str(output.get("kind") or "").lower()
            normalized = "/" + name.lower().lstrip("/")
            if name.lower().endswith(".csv") and (
                "/scheduler/results/" in normalized
                or "result" in kind
                or "report" in kind
            ):
                csv_outputs.append(output)
        return csv_outputs

    @staticmethod
    def _output_url(output: dict) -> str | None:
        for key in ("download_url", "sas_url", "url", "blob_url", "result_url"):
            value = output.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def download_result_output(self, output: dict, destination: str | Path) -> Path:
        """Download one manifest output atomically to a local path."""
        url = self._output_url(output)
        if not url:
            name = output.get("blob_name") or output.get("path") or output.get("name") or "result output"
            raise FoodPortError(502, "Result output does not include a download URL: {0}".format(name))
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".part")
        parsed = urlsplit(url)
        logger.info("result_output_download_start host=%s path=%s destination=%s", parsed.hostname or "unknown", parsed.path or "/", destination.name)
        try:
            with self._transfer_session().get(url, timeout=(10, 3600), stream=True) as response:
                if not response.ok:
                    self._decode(response, "GET result output")
                with temporary.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            handle.write(chunk)
            temporary.replace(destination)
        except requests.RequestException as exc:
            if temporary.exists():
                temporary.unlink()
            raise FoodPortError(0, f"Network unavailable: {exc}") from exc
        except Exception:
            if temporary.exists():
                temporary.unlink()
            raise
        logger.info("result_output_download_complete destination=%s size_bytes=%s", destination.name, destination.stat().st_size)
        return destination

    def download_iteration_csvs(self, result: dict, destination: str | Path) -> list[Path]:
        """Download all scheduler result CSVs described by one manifest."""
        destination = Path(destination)
        downloaded = []
        for output in self.iteration_csv_outputs(result):
            name = str(output.get("blob_name") or output.get("path") or output.get("name") or "iteration-result.csv")
            downloaded.append(self.download_result_output(output, destination / Path(name).name))
        return downloaded

    def prepare_file(
        self,
        run_id: int,
        relative_path: str,
        size_bytes: int,
    ) -> dict:
        """Register a POD5 file and receive a short-lived upload URL."""
        return self._request(
            "POST",
            f"/nanopore/runs/{run_id}/files/prepare/",
            json={
                "relative_path": relative_path,
                "size_bytes": size_bytes,
            },
        )

    def complete_file(self, run_id: int, file_id: int) -> dict:
        """Tell FoodPort that a previously prepared POD5 upload is complete."""
        return self._request(
            "POST",
            f"/nanopore/runs/{run_id}/files/{file_id}/complete/",
        )

    def finalize(self, run_id: int) -> dict:
        """Close run intake and ask FoodPort to drain pending iterations."""
        return self._request(
            "POST",
            f"/nanopore/runs/{run_id}/finalize/",
        )

    def upload_blob(
        self,
        upload_url: str,
        local_path: str,
        progress=None,
    ) -> None:
        """Upload one local blob using its SAS URL."""
        filename = os.path.basename(local_path)
        logger.info(
            "blob_upload_start filename=%s size_bytes=%s",
            filename,
            os.path.getsize(local_path),
        )

        with open(local_path, "rb") as handle:
            try:
                response = self._transfer_session().put(
                    upload_url,
                    data=_ProgressReader(handle, progress),
                    headers={
                        "x-ms-blob-type": "BlockBlob",
                        "Content-Type": "application/octet-stream",
                    },
                    timeout=(10, 3600),
                )
            except requests.RequestException as exc:
                logger.warning(
                    "blob_upload_network_error filename=%s error=%s",
                    filename,
                    exc,
                )
                raise FoodPortError(
                    0,
                    f"Network unavailable: {exc}",
                ) from exc

        logger.info(
            "blob_upload_complete filename=%s status=%s",
            filename,
            response.status_code,
        )
        self._decode(response)

    def close(self) -> None:
        """Close HTTP sessions and forget the local authentication token."""
        self.logout()
        self.session.close()
        with self._transfer_sessions_lock:
            sessions = list(self._transfer_sessions)
            self._transfer_sessions.clear()
        for session in sessions:
            session.close()



class _ProgressReader:
    """File wrapper that reports bytes read during a streaming upload."""

    def __init__(self, stream, progress):
        self.stream = stream
        self.progress = progress

    def read(self, size=-1):
        chunk = self.stream.read(size)
        if chunk and self.progress:
            self.progress(len(chunk))
        return chunk

    def __getattr__(self, name):
        return getattr(self.stream, name)
