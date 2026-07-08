"""Tests for core.data: MF4 discovery, access checks, on-demand migration."""

from pathlib import Path

from core.data import (
    DataFile,
    check_data_access,
    copy_input_data,
    is_input_mf4,
    iter_mf4_inputs,
    looks_local_windows_path,
    resolve_data_for_local,
    scan_data_file,
    scan_segments,
)


def _make_mf4(path: Path, content: bytes = b"mf4data") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_is_input_mf4_skips_generated_outputs(tmp_path):
    assert is_input_mf4(tmp_path / "recording.MF4")
    assert is_input_mf4(tmp_path / "recording.mf4")
    assert not is_input_mf4(tmp_path / "recordingout.MF4")
    assert not is_input_mf4(tmp_path / "recordingout (1).MF4")
    assert not is_input_mf4(tmp_path / "notes.txt")


def test_iter_mf4_inputs_from_directory_sorted(tmp_path):
    _make_mf4(tmp_path / "b.MF4")
    _make_mf4(tmp_path / "a.MF4")
    _make_mf4(tmp_path / "aout.MF4")  # generated output, skipped
    _make_mf4(tmp_path / "sub" / "c.MF4")
    found = [p.name for p in iter_mf4_inputs(tmp_path)]
    assert found == ["a.MF4", "b.MF4", "c.MF4"]


def test_iter_mf4_inputs_limit(tmp_path):
    for name in ("a.MF4", "b.MF4", "c.MF4"):
        _make_mf4(tmp_path / name)
    found = list(iter_mf4_inputs(tmp_path, limit=2))
    assert len(found) == 2


def test_iter_mf4_inputs_single_file(tmp_path):
    mf4 = _make_mf4(tmp_path / "single.MF4")
    assert list(iter_mf4_inputs(mf4)) == [mf4]
    assert list(iter_mf4_inputs(tmp_path / "missing.MF4")) == []


def test_looks_local_windows_path():
    assert looks_local_windows_path(r"D:\data\file.mf4")
    assert looks_local_windows_path("C:/tools/runtime.xml")
    assert not looks_local_windows_path(r"\\share\data\file.mf4")
    assert not looks_local_windows_path("")


def test_check_data_access_local_file(tmp_path):
    mf4 = _make_mf4(tmp_path / "data.MF4")
    report = check_data_access(str(mf4))
    assert report.kind == "local"
    assert report.ok
    assert report.readable


def test_check_data_access_missing():
    report = check_data_access(r"D:\nonexistent\missing.MF4")
    # A non-existent local path is classified by its shape (local), but not ok.
    assert report.kind == "local"
    assert not report.ok


def test_check_data_access_unc_classification(tmp_path):
    # A UNC-shaped path string that does not exist is still classified as unc/missing.
    report = check_data_access(r"\\fake_share\dir\file.MF4")
    assert report.kind == "unc"
    assert not report.ok


def test_scan_segments_head_and_tail():
    # File larger than max_bytes → head + tail segments.
    segs = scan_segments(100, 20)
    assert len(segs) == 2
    assert segs[0] == (0, 10)          # head = 20//2
    assert segs[1] == (90, 10)         # tail = 100-10
    # File smaller than max_bytes → single full segment.
    assert scan_segments(10, 100) == [(0, 10)]
    assert scan_segments(0, 10) == []


def test_scan_data_file_present_signal(tmp_path):
    mf4 = _make_mf4(tmp_path / "x.MF4", content=b"header..." + b"g_Golf_Signal_Name" + b"...tail")
    result = scan_data_file(mf4, ["g_Golf_Signal_Name"], max_bytes=1024)
    assert result.signal_status == "present"
    assert "g_Golf_Signal_Name" in result.matched_signals


def test_scan_data_file_missing_signal(tmp_path):
    mf4 = _make_mf4(tmp_path / "x.MF4", content=b"plain data without the signal")
    result = scan_data_file(mf4, ["g_Absent_Signal"], max_bytes=1024)
    assert result.signal_status == "missing"
    assert "g_Absent_Signal" in result.missing_signals


def test_scan_data_file_no_required_signals(tmp_path):
    mf4 = _make_mf4(tmp_path / "x.MF4")
    result = scan_data_file(mf4, [], max_bytes=1024)
    assert result.signal_status == "not-scanned"


def test_copy_input_data_file(tmp_path):
    src = _make_mf4(tmp_path / "src" / "a.MF4", content=b"payload")
    dest_dir = tmp_path / "staged"
    target = copy_input_data(src, dest_dir)
    assert target.exists()
    assert target.read_bytes() == b"payload"
    # Idempotent.
    again = copy_input_data(src, dest_dir)
    assert again == target


def test_copy_input_data_directory(tmp_path):
    src_dir = tmp_path / "dataset"
    _make_mf4(src_dir / "a.MF4")
    _make_mf4(src_dir / "b.MF4")
    dest_dir = tmp_path / "staged"
    target = copy_input_data(src_dir, dest_dir)
    assert target.is_dir()
    assert {p.name for p in target.iterdir()} == {"a.MF4", "b.MF4"}


def test_resolve_data_for_local_inplace_by_default(tmp_path):
    mf4 = _make_mf4(tmp_path / "data.MF4")
    result = resolve_data_for_local(
        {}, input_path=str(mf4), profile_data={"copy": False}, runtime_data_dir=tmp_path / "stage"
    )
    assert not result.copied
    assert result.resolved_path == str(mf4)


def test_resolve_data_for_local_copies_when_requested(tmp_path):
    mf4 = _make_mf4(tmp_path / "src" / "data.MF4", content=b"x")
    stage = tmp_path / "stage"
    result = resolve_data_for_local(
        {}, input_path=str(mf4), profile_data={"copy": True}, runtime_data_dir=stage
    )
    assert result.copied
    assert Path(result.resolved_path).exists()
    assert result.resolved_path != str(mf4)


def test_resolve_data_for_local_inaccessible(tmp_path):
    result = resolve_data_for_local(
        {}, input_path=str(tmp_path / "missing.MF4"), profile_data={"copy": False}, runtime_data_dir=tmp_path / "stage"
    )
    assert not result.access.ok
    assert result.warnings
