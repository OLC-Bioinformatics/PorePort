from __future__ import annotations

import os
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from .api import FoodPortClient

DEFAULT_FILES = tuple(
    "FBF69780_2a40bb31_a027dece_{0}.pod5".format(index)
    for index in range(23)
)
DEFAULT_BARCODES = (3, 4)
DEFAULT_RELEASE_SIZES = (3, 7, 2, 5, 6)
DEFAULT_SAMPLE_METADATA = {
    3: {"seqid": "2026-MIN-0105", "olnid": "BDS-VTEC099"},
    4: {"seqid": "2026-MIN-0106", "olnid": "BDS-VTEC100"},
}
DEFAULT_SOURCE_CONTAINER = "poresippr-dataset2"
DEFAULT_SOURCE_PREFIX = "20260421_MIN"
DEFAULT_TARGET_PREFIX = "pass"


def test_metadata() -> dict:
    """Return the deterministic run metadata used by both GUI test modes."""
    return {
        "barcode_kit": "SQK-RBK114-24",
        "barcode_values": list(DEFAULT_BARCODES),
        "sample_metadata": {
            "samples": [
                {
                    "barcode": barcode,
                    "seqid": DEFAULT_SAMPLE_METADATA[barcode]["seqid"],
                    "olnid": DEFAULT_SAMPLE_METADATA[barcode]["olnid"],
                }
                for barcode in DEFAULT_BARCODES
            ]
        },
    }


def release_plan(files: tuple[Path | str, ...]):
    """Partition files using the seeder's 3,7,2,5,6 release pattern."""
    if not files:
        return ()
    sizes = []
    remaining = len(files)
    for configured in DEFAULT_RELEASE_SIZES:
        if remaining <= 0:
            break
        size = min(configured, remaining)
        sizes.append(size)
        remaining -= size
    if remaining:
        sizes.append(remaining)
    waves = []
    offset = 0
    for wave_number, size in enumerate(sizes, start=1):
        waves.append((wave_number, files[offset:offset + size]))
        offset += size
    return tuple(waves)


def _storage_client():
    try:
        from azure.storage.blob import BlobServiceClient
    except ImportError as exc:
        raise RuntimeError(
            "Cloud test mode requires azure-storage-blob. Install it with "
            "'python -m pip install azure-storage-blob'."
        ) from exc
    connection_string = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    if not connection_string:
        raise RuntimeError(
            "Cloud test mode requires AZURE_STORAGE_CONNECTION_STRING."
        )
    values = {
        part.split("=", 1)[0]: part.split("=", 1)[1]
        for part in connection_string.split(";")
        if "=" in part
    }
    account = values.get("AccountName")
    key = values.get("AccountKey")
    if not account or not key:
        raise RuntimeError(
            "AZURE_STORAGE_CONNECTION_STRING must contain AccountName and "
            "AccountKey."
        )
    return (
        BlobServiceClient.from_connection_string(connection_string),
        account,
        key,
    )


def _source_url(account, key, container, blob_name):
    from azure.storage.blob import BlobSasPermissions, generate_blob_sas
    sas = generate_blob_sas(
        account_name=account,
        container_name=container,
        blob_name=blob_name,
        account_key=key,
        permission=BlobSasPermissions(read=True),
        expiry=datetime.now(timezone.utc) + timedelta(hours=2),
    )
    return "https://{0}.blob.core.windows.net/{1}/{2}?{3}".format(
        account,
        container,
        blob_name,
        sas,
    )


def _copy_cloud_file(
    client: FoodPortClient,
    service,
    account: str,
    key: str,
    run_id: int,
    filename: str,
    wave_number: int,
    copy_timeout: float,
    copy_poll: float,
    cancel_event=None,
):
    from azure.core.exceptions import ResourceNotFoundError
    from azure.storage.blob import BlobClient

    source_name = str(PurePosixPath(DEFAULT_SOURCE_PREFIX) / filename)
    source = service.get_blob_client(DEFAULT_SOURCE_CONTAINER, source_name)
    size_bytes = source.get_blob_properties().size
    relative_path = str(
        PurePosixPath(DEFAULT_TARGET_PREFIX)
        / "release-wave-{0:06d}".format(wave_number)
        / filename
    )
    prepared = client.prepare_file(run_id, relative_path, size_bytes)
    destination_url = urlsplit(prepared["upload_url"])
    source_host = "{0}.blob.core.windows.net".format(account).lower()
    if (destination_url.hostname or "").lower() != source_host:
        raise RuntimeError(
            "The source fixture and FoodPort destination must use the same "
            "Azure Storage account for server-side test copies."
        )
    parts = [unquote(value) for value in destination_url.path.split("/") if value]
    if len(parts) < 2:
        raise RuntimeError("FoodPort returned an invalid destination upload URL.")
    destination = service.get_blob_client(parts[0], "/".join(parts[1:]))
    BlobClient.from_blob_url(prepared["upload_url"]).start_copy_from_url(
        _source_url(
            account,
            key,
            DEFAULT_SOURCE_CONTAINER,
            source_name,
        )
    )
    deadline = time.monotonic() + copy_timeout
    while True:
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("Cloud test run was cancelled.")
        try:
            properties = destination.get_blob_properties()
        except ResourceNotFoundError:
            if time.monotonic() >= deadline:
                raise TimeoutError("The destination blob did not become visible.")
            time.sleep(copy_poll)
            continue
        status = properties.copy.status if properties.copy else "success"
        if status == "success":
            return filename, prepared
        if status in ("failed", "aborted"):
            detail = (
                properties.copy.status_description
                if properties.copy
                else "unknown copy error"
            )
            raise RuntimeError("Azure Blob copy failed: {0}".format(detail))
        if time.monotonic() >= deadline:
            raise TimeoutError("Azure Blob copy did not finish before timeout.")
        time.sleep(copy_poll)


def seed_cloud_waves(
    client: FoodPortClient,
    run_id: int,
    wait_seconds: float = 150.0,
    auto_finalize: bool = True,
    progress=None,
    copy_timeout: float = 900.0,
    copy_poll: float = 5.0,
    cancel_event=None,
) -> dict:
    """Run the seed_blob_iterations cloud-copy workflow for an existing run."""
    notify = progress or (lambda _message: None)
    service, account, key = _storage_client()
    plan = release_plan(DEFAULT_FILES)
    for wave_index, (wave_number, wave_files) in enumerate(plan, start=1):
        if cancel_event is not None and cancel_event.is_set():
            return {"workflow_state": "cancelled"}
        notify(
            "Cloud test wave {0}/{1}: copying {2} POD5 files.".format(
                wave_number,
                len(plan),
                len(wave_files),
            )
        )
        prepared_wave = []
        for filename in wave_files:
            prepared_wave.append(
                _copy_cloud_file(
                    client,
                    service,
                    account,
                    key,
                    run_id,
                    str(filename),
                    wave_number,
                    copy_timeout,
                    copy_poll,
                    cancel_event=cancel_event,
                )
            )
        notify(
            "Cloud test wave {0}: completing {1} files.".format(
                wave_number,
                len(prepared_wave),
            )
        )
        for _filename, prepared in prepared_wave:
            if cancel_event is not None and cancel_event.is_set():
                return {"workflow_state": "cancelled"}
            client.complete_file(run_id, int(prepared["file_id"]))
        if wave_index < len(plan) and wait_seconds > 0:
            notify(
                "Cloud test wave {0} complete. Waiting {1:g} seconds.".format(
                    wave_number,
                    wait_seconds,
                )
            )
            if cancel_event is not None:
                if cancel_event.wait(wait_seconds):
                    return {"workflow_state": "cancelled"}
            else:
                time.sleep(wait_seconds)
    result = {"workflow_state": "processing"}
    if auto_finalize:
        notify("Cloud test waves complete. Finalizing the FoodPort run.")
        result = client.finalize(run_id)
    return result


class LocalWaveController:
    """Release local POD5 fixtures into a watched staging directory."""

    def __init__(
        self,
        source: Path,
        staging: Path,
        store,
        run_id: int,
        wait_seconds: float = 150.0,
        progress=None,
        finished=None,
        failed=None,
    ):
        self.source = Path(source)
        self.staging = Path(staging)
        self.store = store
        self.run_id = run_id
        self.wait_seconds = wait_seconds
        self.progress = progress or (lambda _message: None)
        self.finished = finished or (lambda: None)
        self.failed = failed or (lambda _error: None)
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run,
            name="local-test-waves",
            daemon=True,
        )
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        try:
            files = tuple(
                sorted(
                    path
                    for path in self.source.rglob("*")
                    if path.is_file() and path.suffix.lower() == ".pod5"
                )
            )
            if not files:
                raise RuntimeError(
                    "The --test-directory does not contain POD5 files."
                )
            if self.staging.exists():
                shutil.rmtree(self.staging)
            self.staging.mkdir(parents=True, exist_ok=True)
            plan = release_plan(files)
            for wave_index, (wave_number, wave_files) in enumerate(plan, start=1):
                if self._stop.is_set():
                    return
                wave_dir = self.staging / "pass" / (
                    "release-wave-{0:06d}".format(wave_number)
                )
                wave_dir.mkdir(parents=True, exist_ok=True)
                self.progress(
                    "Local test wave {0}/{1}: releasing {2} POD5 files.".format(
                        wave_number,
                        len(plan),
                        len(wave_files),
                    )
                )
                relative_paths = []
                for source in wave_files:
                    destination = wave_dir / source.name
                    temporary = destination.with_name(destination.name + ".part")
                    try:
                        os.link(source, temporary)
                    except OSError:
                        shutil.copy2(source, temporary)
                    temporary.replace(destination)
                    relative_paths.append(
                        destination.relative_to(self.staging).as_posix()
                    )
                while not self._stop.is_set():
                    rows = {
                        row["relative_path"]: row
                        for row in self.store.all(self.run_id)
                    }
                    states = [
                        rows[relative_path]["status"]
                        if relative_path in rows
                        else None
                        for relative_path in relative_paths
                    ]
                    if any(state == "error" for state in states):
                        raise RuntimeError(
                            "A local test-wave upload failed. Use Retry failed "
                            "uploads, then restart the test run."
                        )
                    if states and all(state == "uploaded" for state in states):
                        break
                    time.sleep(1)
                if wave_index < len(plan):
                    self.progress(
                        "Local test wave {0} uploaded. Waiting {1:g} seconds.".format(
                            wave_number,
                            self.wait_seconds,
                        )
                    )
                    if self._stop.wait(self.wait_seconds):
                        return
            self.finished()
        except Exception as exc:
            self.failed(exc)
