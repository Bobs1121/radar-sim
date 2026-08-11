from pathlib import Path

import pytest

from core.mat_filter_resolver import MatFilterResolutionError, resolve_mat_filter


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    return root


def test_explicit_mat_filter_always_wins(tmp_path):
    root = _repo(tmp_path)
    explicit = root / "chosen.filter"
    explicit.write_text("chosen", encoding="utf-8")
    inferred = root / "tools" / "selena" / "matlab_transport_cfg" / "matlab_swx_plotreco.mdf.mat.filter"
    inferred.parent.mkdir(parents=True)
    inferred.write_text("inferred", encoding="utf-8")

    result = resolve_mat_filter(str(explicit), code_path=str(root))

    assert result.source == "user"
    assert result.path == explicit.resolve()


def test_repository_discovery_selects_unique_high_confidence_candidate(tmp_path):
    root = _repo(tmp_path)
    directory = root / "reco_fw" / "tools" / "selena" / "matlab_transport_cfg"
    directory.mkdir(parents=True)
    expected = directory / "matlab_swx_plotreco.mdf.mat.filter"
    expected.write_text("signals", encoding="utf-8")
    (directory / "matlab_swx_tgu.mdf.mat.filter").write_text("other", encoding="utf-8")
    existing = root / "ip_dc" / "build" / "selena" / "RelWithDebInfo"
    existing.mkdir(parents=True)

    result = resolve_mat_filter(existing_path=str(existing))

    assert result.source == "repository_inference"
    assert result.repository_root == root.resolve()
    assert result.path == expected.resolve()


def test_repository_discovery_never_guesses_between_equal_candidates(tmp_path):
    root = _repo(tmp_path)
    for branch in ("a", "b"):
        path = root / branch / "tools" / "selena" / "matlab_transport_cfg" / "matlab_swx_plotreco.mdf.mat.filter"
        path.parent.mkdir(parents=True)
        path.write_text(branch, encoding="utf-8")

    with pytest.raises(MatFilterResolutionError) as caught:
        resolve_mat_filter(code_path=str(root))

    assert caught.value.code == "mat_filter_ambiguous"
    assert len(caught.value.candidates) == 2
