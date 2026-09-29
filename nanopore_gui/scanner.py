from __future__ import annotations

import time
from pathlib import Path


class StablePod5Scanner:
    """Discover POD5 files after their size and mtime remain stable."""

    def __init__(self, root: Path, stable_seconds: int = 180):
        self.root = Path(root)
        self.stable_seconds = stable_seconds
        self._observed: dict[Path, tuple[int, float, float]] = {}
        self._released: dict[Path, tuple[int, float]] = {}

    def scan(self) -> list[tuple[str, Path, int]]:
        """Return newly stable POD5 files relative to the configured root.

        A stable file is returned once for a given size/mtime signature. If a
        file changes later, its stability timer is restarted and the new
        signature may be returned after it becomes stable again.
        """
        ready = []
        now = time.time()
        seen = set()

        if not self.root.exists():
            self._observed.clear()
            self._released.clear()
            return ready

        for path in self.root.rglob("*"):
            if not path.is_file() or path.suffix.lower() != ".pod5":
                continue

            seen.add(path)
            try:
                stat = path.stat()
                with path.open("rb") as handle:
                    handle.read(1)
            except OSError:
                continue

            signature = (stat.st_size, stat.st_mtime)
            previous = self._observed.get(path)
            observed_at = (
                previous[2]
                if previous and previous[:2] == signature
                else now
            )
            self._observed[path] = signature + (observed_at,)

            if not previous or previous[:2] != signature:
                self._released.pop(path, None)
                continue

            stable_for = now - observed_at
            if stable_for < self.stable_seconds:
                continue

            if self._released.get(path) == signature:
                continue

            self._released[path] = signature
            ready.append(
                (
                    path.relative_to(self.root).as_posix(),
                    path,
                    stat.st_size,
                )
            )

        stale_paths = set(self._observed) - seen
        for path in stale_paths:
            self._observed.pop(path, None)
            self._released.pop(path, None)

        return ready
