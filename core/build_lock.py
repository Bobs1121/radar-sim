"""Cross-process single-flight lock for one authorized build workspace."""

from __future__ import annotations

import hashlib
import os
import tempfile
import time
from pathlib import Path
from typing import Callable


class BuildLockError(RuntimeError):
    """Raised when a workspace build lock cannot be acquired."""

    def __init__(self, message: str, *, cancelled: bool = False, timed_out: bool = False) -> None:
        super().__init__(message)
        self.cancelled = bool(cancelled)
        self.timed_out = bool(timed_out)


class WorkspaceBuildLock:
    def __init__(self, workspace: str | Path) -> None:
        normalized = os.path.normcase(os.path.abspath(str(workspace)))
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        root = Path(tempfile.gettempdir()) / "radar-sim-build-locks"
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / f"{digest}.lock"
        self._handle = None

    def acquire(
        self,
        *,
        wait: bool = False,
        timeout: float | None = None,
        poll_interval: float = 0.5,
        cancel_callback: Callable[[], bool] | None = None,
        wait_callback: Callable[[], None] | None = None,
    ) -> "WorkspaceBuildLock":
        """Acquire the single-flight lock.

        The historical default remains fail-fast for low-level callers.  Job
        executors should use ``wait=True``: a second user compiling the same
        checkout is a resource-serialization case, not a simulation failure.
        OS-level locks are released automatically if the owning process dies,
        so waiting does not create a stale-lock recovery problem.
        """

        if timeout is not None and float(timeout) < 0:
            raise ValueError("build lock timeout must be non-negative")
        interval = max(0.05, float(poll_interval or 0.0))
        started = time.monotonic()
        handle = self.path.open("a+b")
        try:
            # msvcrt.locking requires at least one byte in the file.  Creating
            # it once also makes the lock file deterministic across retries.
            if self.path.stat().st_size == 0:
                handle.write(b"\0")
                handle.flush()
            while True:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except (OSError, BlockingIOError) as exc:
                    if not wait:
                        raise BuildLockError(
                            "another Selena build is already running for this code workspace"
                        ) from exc
                    if cancel_callback is not None and cancel_callback():
                        raise BuildLockError(
                            "build lock wait cancelled", cancelled=True
                        ) from exc
                    if timeout is not None and time.monotonic() - started >= float(timeout):
                        raise BuildLockError(
                            "build lock wait exceeded its safety timeout", timed_out=True
                        ) from exc
                    if wait_callback is not None:
                        wait_callback()
                    time.sleep(interval)
        except Exception:
            handle.close()
            raise
        self._handle = handle
        return self

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._handle = None

    def __enter__(self) -> "WorkspaceBuildLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()


def build_workspace_from_config(config: dict) -> str:
    repos = dict(config.get("repos") or {})
    paths = dict(config.get("paths") or {})
    build = dict(config.get("build") or {})
    return str(
        repos.get("inner_repo_root")
        or repos.get("outer_repo_root")
        or paths.get("project_root")
        or Path(str(build.get("selena_build_script") or ".")).parent
    )


__all__ = ["BuildLockError", "WorkspaceBuildLock", "build_workspace_from_config"]
