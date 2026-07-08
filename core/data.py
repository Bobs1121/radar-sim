"""Data adaptivity layer: MF4 discovery, access checks, on-demand migration.

Shared by the local simulation path (`rsim run`) and the cluster backend
(`rsim cluster *`). Extracted from core/cluster.py so both backends use one
implementation of:

  - directory scanning for candidate input MF4s
  - bounded byte scanning for required signal names (without opening huge
    MF4s with asammdf)
  - local-drive vs UNC path classification
  - read/write access validation
  - on-demand copy of local data to a worker-visible shared location

The functions here intentionally avoid heavy MF4 parsing. Network-hosted BYD_SR
files can be hundreds of MB to GB, so scanning uses bounded head/tail segments.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Iterator, Optional

from core.simulation import OUTPUT_FILE_PATTERN


@dataclass
class DataFile:
    """One scanned input MF4 with optional required-signal match info."""

    path: str
    size: int
    signal_status: str  # present | missing | missing-in-prefix | not-scanned | error
    matched_signals: list[str]
    missing_signals: list[str]
    scanned_bytes: int
    detail: str


@dataclass
class AccessReport:
    """Result of probing a data path's reachability and writability."""

    path: str
    kind: str  # local | unc | missing | error
    readable: bool
    parent_writable: bool
    detail: str

    @property
    def ok(self) -> bool:
        return self.kind != "missing" and self.kind != "error" and self.readable


@dataclass
class ResolvedData:
    """Where input data should be read from after local resolution."""

    original_path: str
    resolved_path: str
    copied: bool
    access: "AccessReport"
    warnings: list[str]


# ---------------------------------------------------------------------------
# Path classification
# ---------------------------------------------------------------------------

def looks_local_windows_path(path: str) -> bool:
    """Return True for a drive-letter path (e.g. ``D:\\data\\...``), False for UNC."""
    text = str(path or "")
    if text.startswith("\\\\"):
        return False
    drive, _ = os.path.splitdrive(text)
    return bool(drive)


# ---------------------------------------------------------------------------
# MF4 discovery
# ---------------------------------------------------------------------------

def is_input_mf4(path: Path) -> bool:
    """True for a raw input MF4 (skips generated ``*out.MF4`` outputs)."""
    return path.suffix.upper() == ".MF4" and not OUTPUT_FILE_PATTERN.search(path.stem)


def iter_mf4_inputs(source: Path, *, limit: int = 0) -> Iterator[Path]:
    """Yield candidate input MF4 paths from a file or directory tree.

    When ``source`` is a single file, yield it (if it is an input MF4).
    When ``source`` is a directory, walk it deterministically (sorted dirs/files).
    ``limit`` <= 0 means unlimited.
    """
    if source.is_file():
        if is_input_mf4(source):
            yield source
        return
    if not source.exists():
        return
    yielded = 0
    for root, dirs, files in os.walk(source):
        dirs.sort()
        for name in sorted(files):
            path = Path(root) / name
            if not is_input_mf4(path):
                continue
            yield path
            yielded += 1
            if limit > 0 and yielded >= limit:
                return


def scan_data_files(
    source: Path,
    required_signals: list[str],
    *,
    limit: int = 20,
    max_read_mb: int = 8,
) -> list[DataFile]:
    """List candidate MF4 inputs and scan each for required signal names."""
    limit = max(1, int(limit or 20))
    max_bytes = max(0, int(max_read_mb or 0)) * 1024 * 1024
    files: list[DataFile] = []
    for path in iter_mf4_inputs(source, limit=limit):
        files.append(scan_data_file(path, required_signals, max_bytes=max_bytes))
        if len(files) >= limit:
            break
    return files


def scan_data_file(path: Path, required_signals: list[str], *, max_bytes: int) -> DataFile:
    """Scan one MF4's head/tail bytes for required signal names."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        return DataFile(str(path), 0, "error", [], required_signals, 0, str(exc))
    if not required_signals:
        return DataFile(str(path), size, "not-scanned", [], [], 0, "no required signals configured")
    if max_bytes <= 0:
        return DataFile(str(path), size, "not-scanned", [], required_signals, 0, "max_read_mb is 0")

    needles = {
        signal: [
            signal.encode("utf-8", errors="ignore"),
            signal.encode("utf-16le", errors="ignore"),
        ]
        for signal in required_signals
    }
    matched: set[str] = set()
    scanned = 0
    max_needle = max((len(needle) for variants in needles.values() for needle in variants), default=0)
    try:
        with path.open("rb") as handle:
            for offset, length in scan_segments(size, max_bytes):
                handle.seek(offset)
                tail = b""
                remaining = length
                while remaining > 0:
                    chunk = handle.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    scanned += len(chunk)
                    data = tail + chunk
                    for signal, variants in needles.items():
                        if signal in matched:
                            continue
                        if any(needle and needle in data for needle in variants):
                            matched.add(signal)
                    if len(matched) == len(needles):
                        break
                    tail = data[-max_needle:] if max_needle else b""
                if len(matched) == len(needles):
                    break
    except Exception as exc:
        return DataFile(str(path), size, "error", sorted(matched), [sig for sig in required_signals if sig not in matched], scanned, str(exc))

    missing = [signal for signal in required_signals if signal not in matched]
    if not missing:
        status = "present"
        detail = "all required signals found in scanned bytes"
    elif scanned >= size:
        status = "missing"
        detail = "required signals were not found in the complete file"
    else:
        status = "missing-in-prefix"
        detail = f"required signals were not found in {scanned} scanned bytes"
    return DataFile(str(path), size, status, sorted(matched), missing, scanned, detail)


def scan_segments(size: int, max_bytes: int) -> list[tuple[int, int]]:
    """Split a file into a head segment and a tail segment bounded by max_bytes."""
    if size <= 0 or max_bytes <= 0:
        return []
    if max_bytes >= size:
        return [(0, size)]
    head = max_bytes // 2
    tail = max_bytes - head
    if head <= 0:
        return [(max(0, size - tail), min(size, tail))]
    tail_offset = max(0, size - tail)
    if head >= tail_offset:
        return [(0, min(size, max_bytes))]
    return [(0, head), (tail_offset, tail)]


# ---------------------------------------------------------------------------
# Access validation
# ---------------------------------------------------------------------------

def check_data_access(path: str, *, output_dir: Optional[str] = None) -> AccessReport:
    """Probe whether a data path is readable and its area is writable.

    ``kind`` is ``local`` (drive-letter), ``unc`` (``\\\\share\\...``),
    ``missing`` (does not exist), or ``error`` (probe failed).
    ``parent_writable`` checks the parent directory of the data path, or
    ``output_dir`` when provided (useful for cluster workers that write
    outputs elsewhere).
    """
    text = str(path or "")
    target = Path(text)
    kind = "unc" if text.startswith("\\\\") else "local"
    if not text:
        return AccessReport("", "missing", False, False, "empty path")
    if not target.exists():
        return AccessReport(text, kind, False, False, f"{kind} path does not exist")

    readable = os.access(text, os.R_OK)
    write_target = Path(output_dir) if output_dir else target.parent
    parent_writable = False
    detail_parts: list[str] = []
    try:
        if write_target.exists():
            parent_writable = os.access(str(write_target), os.W_OK)
        else:
            # Probe by attempting to create the parent (common for fresh run dirs).
            write_target.mkdir(parents=True, exist_ok=True)
            parent_writable = os.access(str(write_target), os.W_OK)
        detail_parts.append(f"output area: {write_target}")
    except OSError as exc:
        detail_parts.append(f"output area not writable: {exc}")

    if not readable:
        detail_parts.insert(0, "not readable")
    return AccessReport(text, kind, readable, parent_writable, "; ".join(detail_parts))


# ---------------------------------------------------------------------------
# On-demand migration
# ---------------------------------------------------------------------------

def copy_input_data(source: Path, data_dir: Path) -> Path:
    """Copy a single MF4 or a dataset directory into ``data_dir``.

    Returns the destination path. Idempotent: existing targets are reused.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        target = data_dir / source.name
        if target.exists():
            return target
        shutil.copytree(source, target)
        return target
    target = data_dir / source.name
    if not target.exists():
        shutil.copy2(source, target)
    return target


def resolve_data_for_local(
    sim: dict[str, Any],
    *,
    input_path: str,
    profile_data: dict[str, Any] | None,
    runtime_data_dir: Path,
) -> ResolvedData:
    """Resolve where local execution should read input from.

    By default the input is referenced in place (no copy), even for UNC paths.
    Only when ``profile_data.copy`` is true is the data copied into the local
    runtime data directory — useful when repeated reads from a slow share
    would dominate runtime.
    """
    original = os.path.normpath(str(input_path or ""))
    profile_data = profile_data or {}
    copy = bool(profile_data.get("copy", False))
    warnings: list[str] = []

    access = check_data_access(original)
    if not access.ok:
        warnings.append(f"Input data not accessible: {original} ({access.detail})")
        return ResolvedData(original, original, False, access, warnings)

    if not copy:
        if access.kind == "unc":
            warnings.append(
                "Input data is on a UNC share and will be read in place; "
                "set profile data.copy=true to stage it locally first."
            )
        return ResolvedData(original, original, False, access, warnings)

    # copy == true: stage locally
    try:
        staged = copy_input_data(Path(original), runtime_data_dir)
        staged_access = check_data_access(str(staged))
        return ResolvedData(original, str(staged), True, staged_access, warnings)
    except OSError as exc:
        warnings.append(f"Failed to stage input data locally: {exc}; falling back to in-place reference")
        return ResolvedData(original, original, False, access, warnings)


def datafile_to_dict(item: DataFile) -> dict[str, Any]:
    return asdict(item)
