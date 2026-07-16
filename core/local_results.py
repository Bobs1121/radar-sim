"""Owner-scoped catalog for immutable Windows-local simulation results.

Physical result roots and archive locations are private catalog data.  Public
``ResultRef`` values contain only logical references, relative file evidence,
checksums, sizes, timestamps and retention metadata.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import tempfile
import threading
import time
import unicodedata
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable, Iterable

from core.user import normalize_user


class ResultCatalogError(ValueError):
    """Stable result validation, ownership, retention, or storage error."""


_RESULT_REF_RE = re.compile(r"^result:sha256:[0-9a-f]{64}$")
_RUN_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_CHECKSUM_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_WINDOWS_ILLEGAL_RE = re.compile(r'[<>:"|?*]')


@dataclass(frozen=True)
class ResultFileRef:
    relative_path: str
    size: int
    checksum: str

    def __post_init__(self) -> None:
        relative = _validate_relative_path(self.relative_path)
        size = int(self.size)
        checksum = str(self.checksum or "").strip().lower()
        if size < 0:
            raise ResultCatalogError("result file size is invalid")
        if not _CHECKSUM_RE.fullmatch(checksum):
            raise ResultCatalogError("result file checksum is invalid")
        object.__setattr__(self, "relative_path", relative)
        object.__setattr__(self, "size", size)
        object.__setattr__(self, "checksum", checksum)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ResultRef:
    ref: str
    run_ref: str
    files: tuple[ResultFileRef, ...]
    archive_checksum: str
    archive_size: int
    created_at: float
    retain_until: float

    def __post_init__(self) -> None:
        ref = str(self.ref or "").strip().lower()
        run_ref = str(self.run_ref or "").strip()
        files = tuple(self.files or ())
        archive_checksum = str(self.archive_checksum or "").strip().lower()
        archive_size = int(self.archive_size)
        created_at = float(self.created_at)
        retain_until = float(self.retain_until)
        if not _RESULT_REF_RE.fullmatch(ref):
            raise ResultCatalogError("result reference is invalid")
        if not _RUN_REF_RE.fullmatch(run_ref):
            raise ResultCatalogError("result run reference is invalid")
        if not files or any(not isinstance(item, ResultFileRef) for item in files):
            raise ResultCatalogError("result must contain at least one file")
        if len({item.relative_path.casefold() for item in files}) != len(files):
            raise ResultCatalogError("result file paths must be case-insensitively unique")
        if not _CHECKSUM_RE.fullmatch(archive_checksum) or archive_size <= 0:
            raise ResultCatalogError("result archive evidence is invalid")
        if any(not math.isfinite(value) or value < 0 for value in (created_at, retain_until)):
            raise ResultCatalogError("result timestamps are invalid")
        object.__setattr__(self, "ref", ref)
        object.__setattr__(self, "run_ref", run_ref)
        object.__setattr__(self, "files", files)
        object.__setattr__(self, "archive_checksum", archive_checksum)
        object.__setattr__(self, "archive_size", archive_size)
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "retain_until", retain_until)

    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def total_size(self) -> int:
        return sum(item.size for item in self.files)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "run_ref": self.run_ref,
            "files": [item.to_dict() for item in self.files],
            "file_count": self.file_count,
            "total_size": self.total_size,
            "archive_checksum": self.archive_checksum,
            "archive_size": self.archive_size,
            "created_at": self.created_at,
            "retain_until": self.retain_until,
        }

    @property
    def public_dict(self) -> dict[str, Any]:
        return self.to_dict()


class ResultCatalog:
    """Store deterministic archives while keeping every location server-side."""

    def __init__(
        self,
        storage_root: str | Path,
        db_path: str | Path,
        *,
        allowed_source_root: str | Path | Iterable[str | Path],
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        self._storage_root = _prepare_root(storage_root, "result storage root")
        raw_roots = (
            (allowed_source_root,)
            if isinstance(allowed_source_root, (str, Path))
            else tuple(allowed_source_root)
        )
        if not raw_roots:
            raise ResultCatalogError("at least one allowed result source root is required")
        self._allowed_source_roots = tuple(
            _prepare_root(root, "allowed result source root") for root in raw_roots
        )
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._now_fn = now_fn
        self._lock = threading.RLock()
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS local_results (
                    result_ref TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    run_ref TEXT NOT NULL,
                    files_json TEXT NOT NULL,
                    archive_checksum TEXT NOT NULL,
                    archive_size INTEGER NOT NULL,
                    archive_location TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    retain_until REAL NOT NULL,
                    UNIQUE(owner, run_ref)
                );
                CREATE INDEX IF NOT EXISTS idx_local_results_owner
                    ON local_results(owner, created_at DESC);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def publish(
        self,
        *,
        owner: str,
        run_ref: str,
        source_root: str | Path,
        files: Iterable[str],
        retain_until: float = 0,
    ) -> ResultRef:
        """Archive an exact relative file set from an authorized result root."""
        owner = normalize_user(owner)
        run_ref = str(run_ref or "").strip()
        if not _RUN_REF_RE.fullmatch(run_ref):
            raise ResultCatalogError("result run reference is invalid")
        retention = float(retain_until)
        if not math.isfinite(retention) or retention < 0:
            raise ResultCatalogError("result retention is invalid")
        source = _authorized_source_root(self._allowed_source_roots, source_root)
        relatives = _validate_file_set(files)
        evidence = tuple(_file_evidence(source, relative) for relative in relatives)

        owner_key = hashlib.sha256(owner.encode("utf-8")).hexdigest()[:24]
        owner_root = self._storage_root / "content" / owner_key
        owner_root.mkdir(parents=True, exist_ok=True)
        temporary = _temporary_archive(owner_root)
        try:
            _write_deterministic_archive(temporary, source, evidence)
            _verify_source_evidence(source, evidence)
            archive_checksum = _sha256_file(temporary)
            archive_size = temporary.stat().st_size
            archive = owner_root / (archive_checksum.removeprefix("sha256:") + ".zip")
            if archive.exists():
                _verify_archive_file(archive, archive_checksum, archive_size)
                temporary.unlink(missing_ok=True)
            else:
                os.replace(temporary, archive)

            digest_payload = "\0".join((owner, run_ref, archive_checksum))
            result_ref = "result:sha256:" + hashlib.sha256(digest_payload.encode("utf-8")).hexdigest()
            created_at = float(self._now_fn())
            candidate = ResultRef(
                ref=result_ref,
                run_ref=run_ref,
                files=evidence,
                archive_checksum=archive_checksum,
                archive_size=archive_size,
                created_at=created_at,
                retain_until=retention,
            )
            self._register(candidate, owner=owner, archive=archive)
            return self.get(result_ref, owner=owner, now=created_at)
        finally:
            temporary.unlink(missing_ok=True)

    def get(self, result_ref: str, *, owner: str, now: float | None = None) -> ResultRef:
        row = self._row(result_ref, owner=owner)
        result = self._public(row)
        current = float(self._now_fn() if now is None else now)
        if not math.isfinite(current) or current < 0:
            raise ResultCatalogError("result access time is invalid")
        if result.retain_until and result.retain_until < current:
            raise ResultCatalogError("result retention has expired")
        return result

    def list(self, *, owner: str, include_expired: bool = False, now: float | None = None) -> tuple[ResultRef, ...]:
        owner = normalize_user(owner)
        current = float(self._now_fn() if now is None else now)
        if not math.isfinite(current) or current < 0:
            raise ResultCatalogError("result access time is invalid")
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM local_results WHERE owner=? ORDER BY created_at DESC, result_ref",
                (owner,),
            ).fetchall()
        results = tuple(self._public(row) for row in rows)
        if include_expired:
            return results
        return tuple(item for item in results if not item.retain_until or item.retain_until >= current)

    def resolve_archive(self, result_ref: str, *, owner: str, now: float | None = None) -> Path:
        """Trusted-only resolution of the private archive location."""
        result = self.get(result_ref, owner=owner, now=now)
        row = self._row(result_ref, owner=owner)
        archive = Path(str(row["archive_location"]))
        _ensure_contained(self._storage_root, archive)
        _verify_archive_file(archive, result.archive_checksum, result.archive_size)
        return archive

    def _register(self, result: ResultRef, *, owner: str, archive: Path) -> None:
        files_json = _files_json(result.files)
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM local_results WHERE owner=? AND run_ref=?",
                (owner, result.run_ref),
            ).fetchone()
            if existing is not None:
                current = self._public(existing)
                if (
                    current.archive_checksum != result.archive_checksum
                    or current.files != result.files
                    or current.retain_until != result.retain_until
                ):
                    raise ResultCatalogError("result run already has different immutable content")
                return
            conn.execute(
                """
                INSERT INTO local_results(
                    result_ref,owner,run_ref,files_json,archive_checksum,archive_size,
                    archive_location,created_at,retain_until
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    result.ref, owner, result.run_ref, files_json, result.archive_checksum,
                    result.archive_size, str(archive), result.created_at, result.retain_until,
                ),
            )
            conn.commit()

    def _row(self, result_ref: str, *, owner: str) -> sqlite3.Row:
        result_ref = str(result_ref or "").strip().lower()
        if not _RESULT_REF_RE.fullmatch(result_ref):
            raise ResultCatalogError("result reference is invalid")
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM local_results WHERE result_ref=? AND owner=?",
                (result_ref, normalize_user(owner)),
            ).fetchone()
        if row is None:
            raise ResultCatalogError("result is unavailable")
        return row

    @staticmethod
    def _public(row: sqlite3.Row) -> ResultRef:
        return ResultRef(
            ref=str(row["result_ref"]),
            run_ref=str(row["run_ref"]),
            files=tuple(ResultFileRef(**item) for item in json.loads(row["files_json"])),
            archive_checksum=str(row["archive_checksum"]),
            archive_size=int(row["archive_size"]),
            created_at=float(row["created_at"]),
            retain_until=float(row["retain_until"]),
        )


def default_result_catalog(
    *, extra_allowed_source_roots: Iterable[str | Path] = ()
) -> ResultCatalog:
    """Return the shared local-full catalog under the configured RSIM_HOME."""
    home_text = str(os.environ.get("RSIM_HOME") or "").strip()
    home = Path(home_text).expanduser() if home_text else Path.home() / ".rsim"
    return ResultCatalog(
        home / "results" / "local-archives",
        home / "results" / "local-results.db",
        allowed_source_root=(home / "agent" / "runs", *tuple(extra_allowed_source_roots)),
    )


def _prepare_root(value: str | Path, label: str) -> Path:
    root = Path(value).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink():
        raise ResultCatalogError(f"{label} must not be a symlink")
    try:
        return root.resolve(strict=True)
    except OSError as exc:
        raise ResultCatalogError(f"{label} is unavailable") from exc


def _authorized_source_root(allowed: tuple[Path, ...], value: str | Path) -> Path:
    lexical = Path(value).expanduser()
    if lexical.is_symlink() or not lexical.is_dir():
        raise ResultCatalogError("result source root is unavailable")
    source = lexical.resolve(strict=True)
    for root in allowed:
        try:
            source.relative_to(root)
            return source
        except ValueError:
            continue
    raise ResultCatalogError("result path escapes its controlled root")


def _ensure_contained(root: Path, target: Path) -> None:
    try:
        target.resolve(strict=False).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ResultCatalogError("result path escapes its controlled root") from exc


def _validate_file_set(files: Iterable[str]) -> tuple[str, ...]:
    values = [_validate_relative_path(value) for value in (files or ())]
    if not values:
        raise ResultCatalogError("result must contain at least one file")
    if len({item.casefold() for item in values}) != len(values):
        raise ResultCatalogError("result file paths must be case-insensitively unique")
    return tuple(sorted(values, key=lambda item: (item.casefold(), item)))


def _validate_relative_path(value: str) -> str:
    text = str(value or "")
    if not text or text != text.strip() or "\\" in text or unicodedata.normalize("NFC", text) != text:
        raise ResultCatalogError("result path must be a normalized POSIX relative path")
    posix = PurePosixPath(text)
    windows = PureWindowsPath(text)
    if posix.is_absolute() or windows.is_absolute() or windows.drive:
        raise ResultCatalogError("result path must be relative")
    if any(part in {"", ".", ".."} for part in text.split("/")):
        raise ResultCatalogError("result path contains traversal or an empty segment")
    if len(text) > 1024:
        raise ResultCatalogError("result path is too long")
    for part in posix.parts:
        stem = part.split(".", 1)[0].upper()
        if (
            len(part) > 255
            or stem in _WINDOWS_RESERVED
            or _WINDOWS_ILLEGAL_RE.search(part)
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in part)
            or part.endswith((".", " "))
        ):
            raise ResultCatalogError("result path contains an unsafe segment")
    return posix.as_posix()


def _file_evidence(root: Path, relative: str) -> ResultFileRef:
    path = root.joinpath(*PurePosixPath(relative).parts)
    _ensure_contained(root, path)
    try:
        details = path.lstat()
    except OSError as exc:
        raise ResultCatalogError("result file is unavailable") from exc
    if path.is_symlink() or not stat.S_ISREG(details.st_mode):
        raise ResultCatalogError("result file must be a regular non-symlink file")
    return ResultFileRef(relative, details.st_size, _sha256_file(path))


def _verify_source_evidence(root: Path, files: tuple[ResultFileRef, ...]) -> None:
    for evidence in files:
        current = _file_evidence(root, evidence.relative_path)
        if current != evidence:
            raise ResultCatalogError("result file changed while it was archived")


def _temporary_archive(root: Path) -> Path:
    descriptor, name = tempfile.mkstemp(prefix="local-result-", suffix=".tmp", dir=str(root))
    os.close(descriptor)
    return Path(name)


def _zip_entry(relative: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    return info


def _write_deterministic_archive(path: Path, root: Path, files: tuple[ResultFileRef, ...]) -> None:
    try:
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9, allowZip64=True) as archive:
            for item in files:
                source = root.joinpath(*PurePosixPath(item.relative_path).parts)
                with source.open("rb") as reader, archive.open(_zip_entry(item.relative_path), "w", force_zip64=True) as writer:
                    for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                        writer.write(chunk)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ResultCatalogError("result archive creation failed") from exc


def _verify_archive_file(path: Path, checksum: str, size: int) -> None:
    if path.is_symlink() or not path.is_file() or path.stat().st_size != size or _sha256_file(path) != checksum:
        raise ResultCatalogError("result archive content is unavailable")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ResultCatalogError("result file is unavailable") from exc
    return "sha256:" + digest.hexdigest()


def _files_json(files: tuple[ResultFileRef, ...]) -> str:
    return json.dumps([item.to_dict() for item in files], ensure_ascii=False, sort_keys=True, separators=(",", ":"))


__all__ = [
    "ResultCatalog", "ResultCatalogError", "ResultFileRef", "ResultRef",
    "default_result_catalog",
]
