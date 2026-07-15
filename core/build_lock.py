"""Cross-process single-flight lock for one authorized build workspace."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path


class BuildLockError(RuntimeError):
    """Raised when another process already builds the same workspace."""


class WorkspaceBuildLock:
    def __init__(self, workspace: str | Path) -> None:
        normalized = os.path.normcase(os.path.abspath(str(workspace)))
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        root = Path(tempfile.gettempdir()) / "radar-sim-build-locks"
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / f"{digest}.lock"
        self._handle = None

    def acquire(self) -> "WorkspaceBuildLock":
        handle = self.path.open("a+b")
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                handle.write(b"\0") if handle.tell() == 0 and self.path.stat().st_size == 0 else None
                handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            handle.close()
            raise BuildLockError("another Selena build is already running for this code workspace") from exc
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
