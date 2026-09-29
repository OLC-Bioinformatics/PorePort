from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
import logging
from pathlib import Path
import threading
import time

from .api import FoodPortClient, FoodPortError
from .storage import QueueStore

logger = logging.getLogger("nanopore_gui.uploader")


class UploadCoordinator:
    """Coordinate bounded, durable POD5 uploads for one FoodPort run."""

    def __init__(
        self,
        client: FoodPortClient,
        store: QueueStore,
        run_id: int,
        workers: int = 3,
        max_attempts: int = 3,
    ):
        self.client = client
        self.store = store
        self.run_id = run_id
        self.max_attempts = max_attempts
        self.executor = ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="pod5-upload",
        )
        self._lock = threading.RLock()
        self._active = set()
        self._futures = set()
        self._closed = False
        self.store.recover_uploading(run_id)

    @property
    def is_idle(self) -> bool:
        """Return True when no upload worker is active."""
        with self._lock:
            return not self._active and not self._futures

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._active)

    def submit(
        self,
        relative_path: str,
        local_path: Path,
        size_bytes: int,
    ) -> bool:
        """Queue one file if it is not uploaded or already in flight."""
        local_path = Path(local_path)
        key = (self.run_id, relative_path)

        with self._lock:
            if self._closed or key in self._active:
                return False

            should_submit = self.store.add_pending(
                self.run_id,
                relative_path,
                str(local_path),
                size_bytes,
            )
            if not should_submit:
                return False

            self._active.add(key)
            logger.info(
                "file_upload_queued run_id=%s relative_path=%s "
                "size_bytes=%s",
                self.run_id,
                relative_path,
                size_bytes,
            )
            future = self.executor.submit(
                self._upload,
                relative_path,
                local_path,
                size_bytes,
            )
            self._futures.add(future)
            future.add_done_callback(
                lambda item, upload_key=key: self._future_done(
                    upload_key,
                    item,
                )
            )
            return True

    def _future_done(self, key, future: Future) -> None:
        with self._lock:
            self._futures.discard(future)
            self._active.discard(key)

        try:
            future.result()
        except Exception:
            # _upload handles expected failures. This protects the executor
            # from hiding an unexpected programming error.
            logger.exception(
                "file_upload_worker_crashed run_id=%s relative_path=%s",
                key[0],
                key[1],
            )

    def _upload(
        self,
        relative_path: str,
        local_path: Path,
        size_bytes: int,
    ) -> None:
        for attempt in range(1, self.max_attempts + 1):
            upload_phase = False
            try:
                if not local_path.is_file():
                    raise OSError(
                        "POD5 file no longer exists: {0}".format(local_path)
                    )

                current_size = local_path.stat().st_size
                if current_size != size_bytes:
                    raise OSError(
                        "POD5 file size changed before upload: expected "
                        "{0}, found {1}".format(size_bytes, current_size)
                    )

                logger.info(
                    "file_upload_attempt run_id=%s relative_path=%s "
                    "attempt=%s",
                    self.run_id,
                    relative_path,
                    attempt,
                )
                prepared = self.client.prepare_file(
                    self.run_id,
                    relative_path,
                    size_bytes,
                )
                self.store.mark(
                    self.run_id,
                    relative_path,
                    "uploading",
                    prepared["file_id"],
                    blob_name=(
                        prepared.get("blob_name") or prepared.get("name")
                    ),
                    container=(
                        prepared.get("container")
                        or prepared.get("container_name")
                    ),
                )
                upload_phase = True
                self.client.upload_blob(
                    prepared["upload_url"],
                    str(local_path),
                )
                upload_phase = False
                self.client.complete_file(
                    self.run_id,
                    prepared["file_id"],
                )
                self.store.mark(
                    self.run_id,
                    relative_path,
                    "uploaded",
                    prepared["file_id"],
                    error=None,
                )
                logger.info(
                    "file_upload_success run_id=%s relative_path=%s "
                    "file_id=%s",
                    self.run_id,
                    relative_path,
                    prepared["file_id"],
                )
                return
            except (OSError, FoodPortError) as exc:
                status = getattr(exc, "status", 0)
                retryable = (
                    isinstance(exc, OSError)
                    or status == 0
                    or status == 408
                    or status == 429
                    or status >= 500
                    or (upload_phase and status in (401, 403))
                )
                if not retryable or attempt == self.max_attempts:
                    self.store.mark(
                        self.run_id,
                        relative_path,
                        "error",
                        error=str(exc),
                    )
                    logger.error(
                        "file_upload_failed run_id=%s relative_path=%s "
                        "attempt=%s retryable=%s error=%s",
                        self.run_id,
                        relative_path,
                        attempt,
                        retryable,
                        exc,
                    )
                    return

                self.store.mark(
                    self.run_id,
                    relative_path,
                    "pending",
                    error=str(exc),
                )
                delay = min(30, 2 ** (attempt - 1))
                logger.warning(
                    "file_upload_retry run_id=%s relative_path=%s "
                    "attempt=%s delay=%s error=%s",
                    self.run_id,
                    relative_path,
                    attempt,
                    delay,
                    exc,
                )
                time.sleep(delay)

    def close(self, wait: bool = False) -> None:
        """Stop accepting submissions and optionally wait for workers."""
        with self._lock:
            if self._closed:
                return
            self._closed = True

        # cancel_futures is available on supported GUI Python versions. It
        # cancels queued work only; in-progress uploads finish normally.
        self.executor.shutdown(wait=wait, cancel_futures=not wait)
