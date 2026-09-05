"""Single-host leader election for commands received by every bot process."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class ProcessLeaderLock:
    def __init__(self, path: str | None = None) -> None:
        lock_path = Path(path or os.getenv(
            "CACHE_COMMAND_LOCK_FILE", str(Path(tempfile.gettempdir()) / "gallery-image-relay-cache-command.lock")
        ))
        self._file = lock_path.open("a+")
        self.is_leader = False
        self.try_acquire()

    def try_acquire(self) -> bool:
        if self.is_leader:
            return True
        try:
            if os.name == "nt":
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.is_leader = True
        except OSError:
            return False
        return True

    def close(self) -> None:
        if self._file.closed:
            return
        if self.is_leader:
            if os.name == "nt":
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        self._file.close()
