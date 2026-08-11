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


def test_repository_discovery_uses_stable_first_candidate_when_scores_are_equal(tmp_path):
    root = _repo(tmp_path)
    for branch in ("a", "b"):
        path = root / branch / "tools" / "selena" / "matlab_transport_cfg" / "matlab_swx_plotreco.mdf.mat.filter"
        path.parent.mkdir(parents=True)
        path.write_text(branch, encoding="utf-8")

    result = resolve_mat_filter(code_path=str(root))

    assert result.path == (
        root / "a" / "tools" / "selena" / "matlab_transport_cfg" / "matlab_swx_plotreco.mdf.mat.filter"
    ).resolve()
    assert len(result.candidates) == 2


def test_repository_discovery_prefers_code_path_root_over_equal_stale_script_root(tmp_path):
    current = tmp_path / "current"
    stale = tmp_path / "stale"
    for root, content in ((current, "current"), (stale, "stale")):
        (root / ".git").mkdir(parents=True)
        path = root / "reco_fw" / "tools" / "selena" / "matlab_transport_cfg" / "matlab_swx_plotreco.mdf.mat.filter"
        path.parent.mkdir(parents=True)
        path.write_text(content, encoding="utf-8")
    stale_script = stale / "apl" / "product" / "selena" / "build.bat"
    stale_script.parent.mkdir(parents=True)
    stale_script.write_text("rem stale", encoding="utf-8")

    result = resolve_mat_filter(
        code_path=str(current),
        selena_build_script=str(stale_script),
    )

    assert result.repository_root == current.resolve()
    assert result.path.read_text(encoding="utf-8") == "current"


def test_repository_discovery_prefers_existing_selena_root_over_stale_script_root(tmp_path):
    selected = tmp_path / "selected"
    stale = tmp_path / "stale"
    for root, content in ((selected, "selected"), (stale, "stale")):
        (root / ".git").mkdir(parents=True)
        path = root / "reco_fw" / "tools" / "selena" / "matlab_transport_cfg" / "matlab_swx_plotreco.mdf.mat.filter"
        path.parent.mkdir(parents=True)
        path.write_text(content, encoding="utf-8")
    existing = selected / "ip_dc" / "build" / "selena" / "RelWithDebInfo"
    existing.mkdir(parents=True)
    stale_script = stale / "apl" / "product" / "selena" / "build.bat"
    stale_script.parent.mkdir(parents=True)
    stale_script.write_text("rem stale", encoding="utf-8")

    result = resolve_mat_filter(
        existing_path=str(existing),
        selena_build_script=str(stale_script),
    )

    assert result.repository_root == selected.resolve()
    assert result.path.read_text(encoding="utf-8") == "selected"


def test_repository_discovery_still_fails_when_no_high_confidence_candidate_exists(tmp_path):
    root = _repo(tmp_path)
    weak = root / "misc" / "generic.filter"
    weak.parent.mkdir(parents=True)
    weak.write_text("weak", encoding="utf-8")

    with pytest.raises(MatFilterResolutionError) as caught:
        resolve_mat_filter(code_path=str(root))

    assert caught.value.code == "mat_filter_not_found"
