"""Node-local, extracted result-directory delivery.

The catalog ZIP remains the Web/SDK result boundary.  This helper is called by
the execution device only; it resolves ``result.path``, copies selected files
atomically and returns a path-free status summary.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import uuid
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable, Mapping


class ResultDeliveryError(ValueError):
    def __init__(self, message: str, *, code: str = "result_delivery_failed") -> None:
        super().__init__(str(message))
        self.code = str(code or "result_delivery_failed")


_MANIFEST = "manifest.json"
_SCHEMA = "radar-sim.result-directory/1.0"
_DEVICES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def resolve_result_destination(requested_path: str | os.PathLike[str] | None, job_id: str, *, home: str | os.PathLike[str] | None = None) -> Path:
    """Resolve a local result root plus ``job_id`` on the receiving device."""
    job = _job(job_id)
    raw = str(requested_path or "").strip()
    if raw:
        _path_text(raw)
        root_lex = Path(raw).expanduser()
        _reject_link(root_lex)
        _check_ancestors(root_lex.parent)
        root = _resolve(root_lex, "result destination is unavailable")
        _check_dir(root, root_error=True)
        lex = root_lex / job
    else:
        base = Path(home).expanduser() if home is not None else Path.home()
        lex = base / "RadarSim" / "results" / job
    _reject_link(lex)
    _check_ancestors(lex.parent)
    target = _resolve(lex, "result destination is unavailable")
    _check_dir(target)
    return target


def materialize_result_directory(source_root: str | os.PathLike[str], destination: str | os.PathLike[str], *, files: Iterable[str | Mapping[str, Any]] | None = None, input_results: Iterable[Mapping[str, Any]] | None = None, manifest: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Copy result files into an atomic, idempotent directory and write a manifest."""
    source = _source(source_root)
    target = _destination(destination)
    if _inside(target, source):
        raise ResultDeliveryError("result destination overlaps its source", code="result_destination_invalid")
    original = dict(manifest or {})
    chosen = files if files is not None else original.get("files")
    evidence = _evidence(source, chosen if isinstance(chosen, (list, tuple)) else None)
    inputs = _inputs(input_results if input_results is not None else original.get("input_results"))
    public = _manifest(original, evidence, inputs)
    manifest_bytes = _json(public)
    checksum = _digest(evidence, manifest_bytes)
    existing = _existing(target, evidence, checksum)
    if existing:
        return existing
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        _check_ancestors(target.parent)
    except OSError as exc:
        raise ResultDeliveryError("result destination is unavailable", code="result_destination_invalid") from exc
    temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}.radar-sim-{uuid.uuid4().hex}.", dir=str(target.parent)))
    try:
        for item in evidence:
            source_file = _file(source, item["relative_path"])
            target_file = _child(temporary, item["relative_path"])
            target_file.parent.mkdir(parents=True, exist_ok=True)
            try:
                with source_file.open("rb") as reader, target_file.open("xb") as writer:
                    shutil.copyfileobj(reader, writer, 1024 * 1024)
            except OSError as exc:
                raise ResultDeliveryError("result file copy failed") from exc
        (temporary / _MANIFEST).write_bytes(manifest_bytes)
        _verify(temporary, evidence, checksum)
        try:
            os.replace(str(temporary), str(target))
        except FileExistsError:
            raced = _existing(target, evidence, checksum)
            if raced:
                return raced
            raise ResultDeliveryError("result destination already contains different content", code="result_destination_conflict")
    except ResultDeliveryError:
        raise
    except OSError as exc:
        raise ResultDeliveryError("result directory materialization failed") from exc
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
    return {"status": "delivered", "file_count": len(evidence), "checksum": checksum}


def _job(value: object) -> str:
    text = str(value or "").strip()
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-"
    if not text or len(text) > 256 or any(ch not in allowed for ch in text) or "/" in text or "\\" in text or ".." in text:
        raise ResultDeliveryError("job id is invalid", code="result_destination_invalid")
    return text


def _path_text(value: str) -> None:
    normalized = value.replace("\\", "/")
    if "\x00" in value or any(part == ".." for part in normalized.split("/")):
        raise ResultDeliveryError("result destination contains traversal", code="result_destination_invalid")
    win, posix = PureWindowsPath(value), PurePosixPath(normalized)
    if not str(posix) or str(posix) == "/" or str(win) == "\\" or (os.name != "nt" and win.drive):
        raise ResultDeliveryError("result destination is invalid", code="result_destination_invalid")
    if any(part.split(".", 1)[0].upper() in _DEVICES for part in normalized.split("/") if part):
        raise ResultDeliveryError("result destination uses a device name", code="result_destination_invalid")


def _resolve(path: Path, message: str) -> Path:
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ResultDeliveryError(message, code="result_destination_invalid") from exc


def _reject_link(path: Path) -> None:
    try:
        if path.exists() and path.is_symlink():
            raise ResultDeliveryError("result destination uses a symlink", code="result_destination_invalid")
    except OSError as exc:
        raise ResultDeliveryError("result destination is unavailable", code="result_destination_invalid") from exc


def _check_ancestors(path: Path) -> None:
    try:
        if any(item.exists() and item.is_symlink() for item in list(path.parents)[::-1] + [path]):
            raise ResultDeliveryError("result destination uses a symlink", code="result_destination_invalid")
    except OSError as exc:
        raise ResultDeliveryError("result destination is unavailable", code="result_destination_invalid") from exc


def _check_dir(path: Path, *, root_error: bool = False) -> None:
    if path == Path(path.anchor):
        raise ResultDeliveryError("result destination must not be a filesystem root", code="result_destination_invalid")
    if path.exists() and (path.is_symlink() or not path.is_dir()):
        raise ResultDeliveryError("result destination is not a directory", code="result_destination_invalid")


def _source(value: str | os.PathLike[str]) -> Path:
    lexical = Path(value).expanduser()
    if lexical.exists() and lexical.is_symlink():
        raise ResultDeliveryError("result source is unavailable", code="result_source_unavailable")
    try:
        root = lexical.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ResultDeliveryError("result source is unavailable", code="result_source_unavailable") from exc
    if not root.is_dir():
        raise ResultDeliveryError("result source is unavailable", code="result_source_unavailable")
    return root


def _destination(value: str | os.PathLike[str]) -> Path:
    lexical = Path(value).expanduser()
    _reject_link(lexical)
    _check_ancestors(lexical.parent)
    target = _resolve(lexical, "result destination is invalid")
    _check_dir(target)
    return target


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _relative(value: object) -> str:
    text = str(value or "").strip().replace("\\", "/")
    posix, windows = PurePosixPath(text), PureWindowsPath(text)
    if not text or posix.is_absolute() or windows.is_absolute() or windows.drive or any(part in {"", ".", ".."} for part in text.split("/")):
        raise ResultDeliveryError("result file path is invalid", code="result_source_unavailable")
    return posix.as_posix()


def _file(root: Path, relative: str) -> Path:
    path = root.joinpath(*PurePosixPath(relative).parts)
    if not _inside(path.resolve(strict=False), root):
        raise ResultDeliveryError("result file escapes source", code="result_source_unavailable")
    try:
        details = path.lstat()
    except OSError as exc:
        raise ResultDeliveryError("result file is unavailable", code="result_source_unavailable") from exc
    if path.is_symlink() or not stat.S_ISREG(details.st_mode):
        raise ResultDeliveryError("result file is not a regular file", code="result_source_unavailable")
    return path


def _child(root: Path, relative: str) -> Path:
    path = root.joinpath(*PurePosixPath(relative).parts)
    if not _inside(path.resolve(strict=False), root):
        raise ResultDeliveryError("result file escapes destination", code="result_destination_invalid")
    return path


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ResultDeliveryError("result file is unavailable", code="result_source_unavailable") from exc
    return "sha256:" + digest.hexdigest()


def _evidence(root: Path, values: Iterable[str | Mapping[str, Any]] | None) -> tuple[dict[str, Any], ...]:
    if values is None:
        values = [p.relative_to(root).as_posix() for p in sorted(root.rglob("*"), key=lambda p: p.as_posix().casefold()) if p.is_file() and not p.is_symlink()]
    result, seen = [], set()
    for raw in values:
        record = dict(raw) if isinstance(raw, Mapping) else {"relative_path": raw}
        relative = _relative(record.get("relative_path") or record.get("path"))
        if relative == _MANIFEST or relative.casefold() in seen:
            raise ResultDeliveryError("result file list is invalid", code="result_source_unavailable")
        seen.add(relative.casefold())
        path = _file(root, relative)
        size, checksum = path.stat().st_size, _sha(path)
        if record.get("size") is not None and int(record["size"]) != size:
            raise ResultDeliveryError("result file size evidence changed", code="result_source_unavailable")
        expected = str(record.get("checksum") or record.get("sha256") or "").lower().removeprefix("sha256:")
        if expected and expected != checksum.removeprefix("sha256:"):
            raise ResultDeliveryError("result file checksum evidence changed", code="result_source_unavailable")
        result.append({"relative_path": relative, "size": int(size), "checksum": checksum})
    if not result:
        raise ResultDeliveryError("result contains no files", code="result_source_unavailable")
    return tuple(sorted(result, key=lambda x: (x["relative_path"].casefold(), x["relative_path"])))


def _inputs(values: Iterable[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    result = []
    for raw in values or ():
        if not isinstance(raw, Mapping):
            continue
        item = {}
        for key in ("index", "status", "returncode", "error_code", "input_relative_path", "output_relative_path"):
            value = raw.get(key)
            if value in (None, ""):
                continue
            if key.endswith("path"):
                value = _relative(value)
            elif key in {"index", "returncode"}:
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    continue
            else:
                value = str(value)[:256]
            item[key] = value
        if item:
            result.append(item)
    return result


def _manifest(original: Mapping[str, Any], files: tuple[dict[str, Any], ...], inputs: list[dict[str, Any]]) -> dict[str, Any]:
    result = {"schema_version": _SCHEMA, "status": str(original.get("status") or "succeeded"), "files": list(files)}
    for key in ("job_id", "result_ref", "config_fingerprint", "runtime_bundle_id", "dataset_id"):
        if original.get(key) not in (None, ""):
            result[key] = str(original[key])[:512]
    if inputs:
        result["input_results"] = inputs
    summary = original.get("summary")
    if isinstance(summary, Mapping):
        result["summary"] = {str(k): v for k, v in summary.items() if not any(word in str(k).lower() for word in ("path", "location")) and isinstance(v, (str, int, float, bool, type(None)))}
    return result


def _json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(files: tuple[dict[str, Any], ...], manifest: bytes) -> str:
    digest = hashlib.sha256()
    for item in files:
        digest.update(f"{item['relative_path']}\0{item['size']}\0{item['checksum']}\0".encode("utf-8"))
    digest.update(manifest)
    return "sha256:" + digest.hexdigest()


def _verify(root: Path, files: tuple[dict[str, Any], ...], checksum: str) -> None:
    manifest = root / _MANIFEST
    if not manifest.is_file() or manifest.is_symlink():
        raise ResultDeliveryError("result manifest is unavailable")
    for item in files:
        path = _child(root, item["relative_path"])
        if not path.is_file() or path.is_symlink() or path.stat().st_size != item["size"] or _sha(path) != item["checksum"]:
            raise ResultDeliveryError("result directory verification failed")
    if _digest(files, manifest.read_bytes()) != checksum:
        raise ResultDeliveryError("result directory checksum verification failed")


def _existing(target: Path, files: tuple[dict[str, Any], ...], checksum: str) -> dict[str, Any] | None:
    if not target.exists():
        return None
    _check_dir(target)
    manifest = target / _MANIFEST
    if not manifest.is_file() or manifest.is_symlink():
        raise ResultDeliveryError("result destination already contains different content", code="result_destination_conflict")
    if _digest(files, manifest.read_bytes()) != checksum:
        raise ResultDeliveryError("result destination already contains different content", code="result_destination_conflict")
    _verify(target, files, checksum)
    return {"status": "already_present", "file_count": len(files), "checksum": checksum}


__all__ = ["ResultDeliveryError", "materialize_result_directory", "resolve_result_destination"]
