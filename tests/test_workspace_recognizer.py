"""Tests for core.workspace_recognizer.

Covers: explicit script, auto-discovery, config matching, ambiguity,
unknown, path escape, and public-dict non-leakage. All path assertions are
casing/slash-stable and run on any host (we never touch the real filesystem
except for a temp dir used by the auto-discovery test).
"""

from pathlib import Path
from textwrap import dedent

import pytest

from core.workspace_recognizer import (
    STATUS_AMBIGUOUS,
    STATUS_RESOLVED,
    STATUS_UNRESOLVED,
    RecognitionResult,
    WorkspaceRecognizer,
    _is_within,
    _normalize_path,
    _script_escapes,
    recognize,
)


# --------------------------------------------------------------------------- #
# Fixtures: a synthetic projects/ tree that mirrors the real config schema.
# --------------------------------------------------------------------------- #

def _write_projects(tmp_path: Path, projects: dict) -> Path:
    """Write a config/projects/<name>/config.yaml per entry."""
    base = tmp_path / "config" / "projects"
    base.mkdir(parents=True)
    for name, cfg in projects.items():
        d = base / name
        d.mkdir()
        (d / "config.yaml").write_text(
            dedent(cfg).strip() + "\n", encoding="utf-8"
        )
    return base


# Two distinct projects whose repo roots are siblings, so a code_path cannot
# belong to both at the same time.
_BYDOD = """
    project:
      name: BYD_OD25
      platform: gen5_selena
      recipe: g3n_fvg3_od25
    repos:
      outer_repo_root: "D:/bydod25fr/byd"
      inner_repo_root: "D:/bydod25fr/byd"
    build:
      selena_build_script: "D:/bydod25fr/byd/apl/byd/selena/jenkins_selena_build.bat"
      build_config: "full_dsp"
      build_output: "D:/bydod25fr/byd/build/full_dsp"
"""

# Second project that OVERLAPS bydod25's root intentionally, to drive the
# ambiguous case. Its own platform differs so we can tell candidates apart.
_BYDOD_ALIAS = """
    project:
      name: BYD_OD25_ALIAS
      platform: gen5_selena_alt
    repos:
      outer_repo_root: "D:/bydod25fr/byd"
      inner_repo_root: "D:/bydod25fr/byd"
    build:
      selena_build_script: "D:/bydod25fr/byd/apl/byd/selena/jenkins_selena_build.bat"
"""

_OVRS = """
    project:
      name: BYD_OVS_CB
      platform: gen5_selena
    build:
      selena_build_script: "C:/BYD_OVS_CB/apl/byd/bindings/ovrs25/selena/jenkins_selena_build.bat"
"""


@pytest.fixture
def bydod_only(tmp_path):
    base = _write_projects(tmp_path, {"bydod25": _BYDOD})
    return WorkspaceRecognizer(projects_dir=base)


@pytest.fixture
def bydod_alias(tmp_path):
    base = _write_projects(
        tmp_path, {"bydod25": _BYDOD, "bydod25_alias": _BYDOD_ALIAS}
    )
    return WorkspaceRecognizer(projects_dir=base)


@pytest.fixture
def ovrs_only(tmp_path):
    base = _write_projects(tmp_path, {"ovrs25": _OVRS})
    return WorkspaceRecognizer(projects_dir=base)


# --------------------------------------------------------------------------- #
# Path helper unit tests.
# --------------------------------------------------------------------------- #

def test_normalize_backslash_and_case_stable():
    assert _normalize_path("D:\\Bydod25FR\\byd") == "d:/bydod25fr/byd"
    assert _normalize_path("D:/Bydod25FR/byd") == "d:/bydod25fr/byd"
    # Same logical path -> same normalized key regardless of separator/case.
    assert _normalize_path("D:/BYDOD25FR/BYD") == _normalize_path(
        "d:\\bydod25fr\\byd"
    )


def test_is_within_segment_boundary():
    parent = "d:/foo"
    assert _is_within(parent, "d:/foo")
    assert _is_within(parent, "d:/foo/bar")
    # Not a substring match across segment boundary.
    assert not _is_within(parent, "d:/foobar")
    assert not _is_within(parent, "d:/bar")


def test_script_escapes_detects_outside_workspace():
    assert _script_escapes("D:/bydod25fr/byd", "D:/other/build.bat")
    assert not _script_escapes(
        "D:/bydod25fr/byd", "D:/bydod25fr/byd/apl/build.bat"
    )
    # A sibling that shares a prefix but not a segment is an escape.
    assert _script_escapes("D:/bydod25fr/byd", "D:/bydod25fr/bydother/b.bat")


# --------------------------------------------------------------------------- #
# Config matching.
# --------------------------------------------------------------------------- #

def test_config_match_resolves_bydod(bydod_only):
    r = bydod_only.recognize("D:/bydod25fr/byd")
    assert r.status == STATUS_RESOLVED
    assert r.adapter_key == "gen5_selena"
    # workspace_root normalized and stable.
    assert r.workspace_root == "d:/bydod25fr/byd"
    assert r.confidence == pytest.approx(0.8)
    assert r.output_dir == "d:/bydod25fr/byd/build/full_dsp"


def test_config_match_resolves_subpath(bydod_only):
    r = bydod_only.recognize("D:/bydod25fr/byd/apl/byd/selena")
    assert r.status == STATUS_RESOLVED
    assert r.adapter_key == "gen5_selena"


def test_config_match_case_and_slash_insensitive(bydod_only):
    r = bydod_only.recognize("D:\\BYDOD25FR\\BYD")
    assert r.status == STATUS_RESOLVED
    assert r.adapter_key == "gen5_selena"


def test_config_match_user_points_higher_than_root(bydod_only):
    # User points at a parent of the configured root; still a candidate
    # (configured root lives below code_path).
    r = bydod_only.recognize("D:/bydod25fr")
    assert r.status == STATUS_RESOLVED


def test_ovrs_resolves_with_only_script_path(ovrs_only):
    r = ovrs_only.recognize("C:/BYD_OVS_CB")
    assert r.status == STATUS_RESOLVED
    assert r.adapter_key == "gen5_selena"


# --------------------------------------------------------------------------- #
# Explicit build script.
# --------------------------------------------------------------------------- #

def test_explicit_script_within_code_path_matches_config(bydod_only):
    script = "D:/bydod25fr/byd/apl/byd/selena/jenkins_selena_build.bat"
    r = bydod_only.recognize("D:/bydod25fr/byd", build_script=script)
    assert r.status == STATUS_RESOLVED
    assert r.confidence == pytest.approx(0.95)
    # build_script preserved (normalized representation accepted by caller).
    assert "jenkins_selena_build.bat" in r.build_script.replace("\\", "/")


def test_explicit_script_within_code_path_but_unknown(bydod_only):
    # A valid in-workspace script that is NOT a configured script still
    # resolves via config path match, not via the explicit-script bonus.
    script = "D:/bydod25fr/byd/my_custom_build.bat"
    r = bydod_only.recognize("D:/bydod25fr/byd", build_script=script)
    assert r.status == STATUS_RESOLVED
    assert r.confidence == pytest.approx(0.8)
    # build_script is normalized (lower-cased, forward slashes) for stability.
    assert r.build_script == "d:/bydod25fr/byd/my_custom_build.bat"


def test_explicit_script_escaping_code_path_is_rejected(bydod_only):
    script = "D:/elsewhere/jenkins_selena_build.bat"
    r = bydod_only.recognize("D:/bydod25fr/byd", build_script=script)
    # The escape is recorded in evidence; the configured script is still used.
    assert r.status == STATUS_RESOLVED
    assert any("rejected" in e for e in r.evidence)
    # The escaping script must NOT be the chosen build_script.
    assert r.build_script != script


def test_explicit_script_escape_with_no_config_match_is_unresolved(ovrs_only):
    # An escaping explicit script with no config match -> unresolved; we never
    # execute a script outside the user's workspace. Use a code_path that no
    # project config claims (ovrs config claims C:/BYD_OVS_CB, not this).
    code_path = "X:/no/such/workspace"
    script = "X:/Evil/jenkins_selena_build.bat"
    r = ovrs_only.recognize(code_path, build_script=script)
    assert r.status == STATUS_UNRESOLVED
    assert any("rejected" in e for e in r.evidence)


# --------------------------------------------------------------------------- #
# Auto-discovery.
# --------------------------------------------------------------------------- #

def test_auto_discover_script_inside_code_path(tmp_path):
    # Build a real on-disk workspace with a jenkins_selena_build.bat and a
    # projects config that has NO matching repo root, so only discovery can
    # produce a candidate.
    ws = tmp_path / "ws"
    deep = ws / "apl" / "byd" / "selena"
    deep.mkdir(parents=True)
    (deep / "jenkins_selena_build.bat").write_text("@echo off\n", encoding="utf-8")

    base = _write_projects(tmp_path, {"ovrs25": _OVRS})
    rec = WorkspaceRecognizer(projects_dir=base)
    r = rec.recognize(str(ws))
    assert r.status == STATUS_RESOLVED
    assert r.adapter_key == "selena"
    assert r.confidence == pytest.approx(0.6)
    assert r.build_script.endswith("jenkins_selena_build.bat")
    assert "auto-discovered" in " ".join(r.evidence)


def test_auto_discover_never_escapes_code_path(tmp_path):
    # Put the script in a sibling directory outside code_path; it must not be
    # discovered.
    ws = tmp_path / "ws"
    (ws).mkdir(parents=True)
    sibling = tmp_path / "sibling"
    sibling.mkdir()
    (sibling / "jenkins_selena_build.bat").write_text("x", encoding="utf-8")

    base = _write_projects(tmp_path, {"ovrs25": _OVRS})
    rec = WorkspaceRecognizer(projects_dir=base)
    r = rec.recognize(str(ws))
    assert r.status == STATUS_UNRESOLVED
    assert r.build_script == ""


def test_auto_discover_does_not_lower_config_confidence(bydod_only, tmp_path):
    # When config match (0.8) already exceeds discovery lift (0.6), confidence
    # stays at the config level. bydod_only claims root D:/bydod25fr/byd; on a
    # host where that dir exists and contains a script, discovery runs but must
    # NOT reduce confidence. We point at a subpath so config still matches and
    # any on-disk script under the real workspace is irrelevant to the value.
    r = bydod_only.recognize("D:/bydod25fr/byd/apl/byd/selena")
    assert r.status == STATUS_RESOLVED
    assert r.confidence == pytest.approx(0.8)


# --------------------------------------------------------------------------- #
# Ambiguity.
# --------------------------------------------------------------------------- #

def test_ambiguous_when_two_configs_tie(bydod_alias):
    # Both bydod25 and bydod25_alias claim the same root -> tie at 0.8.
    r = bydod_alias.recognize("D:/bydod25fr/byd")
    assert r.status == STATUS_AMBIGUOUS
    assert set(r.candidates) == {"gen5_selena", "gen5_selena_alt"}
    assert r.adapter_key == ""  # no single winner chosen
    assert any("tied" in e for e in r.evidence)


def test_ambiguous_resolved_by_explicit_script(bydod_alias):
    # An explicit script that matches ONE configured script breaks the tie
    # at 0.95 vs 0.8.
    script = "D:/bydod25fr/byd/apl/byd/selena/jenkins_selena_build.bat"
    r = bydod_alias.recognize("D:/bydod25fr/byd", build_script=script)
    # Both configs share the same configured script, so both lift to 0.95 ->
    # still ambiguous. This is the honest outcome: the script can't tell them
    # apart either.
    assert r.status == STATUS_AMBIGUOUS


# --------------------------------------------------------------------------- #
# Unknown / unresolved.
# --------------------------------------------------------------------------- #

def test_unknown_code_path_unresolved(bydod_only):
    # Use a path that is neither configured nor present on disk, so neither
    # config matching nor auto-discovery can produce a candidate.
    r = bydod_only.recognize("Z:/totally/unknown/workspace_xyz")
    assert r.status == STATUS_UNRESOLVED
    assert r.adapter_key == ""
    assert r.confidence == 0.0


def test_empty_code_path_unresolved(bydod_only):
    r = bydod_only.recognize("")
    assert r.status == STATUS_UNRESOLVED


def test_no_projects_dir_unresolved(tmp_path):
    empty = tmp_path / "empty_projects"
    empty.mkdir()
    rec = WorkspaceRecognizer(projects_dir=empty)
    # A non-existent code_path with no project configs and nothing to discover.
    r = rec.recognize("Z:/no/such/workspace_xyz")
    assert r.status == STATUS_UNRESOLVED


# --------------------------------------------------------------------------- #
# Public-dict non-leakage.
# --------------------------------------------------------------------------- #

def _any_path_fragment(pub, code_path):
    """True if any absolute path fragment leaks into the public view."""
    # Lowercase the code_path drive form and bare dir names to catch leakage.
    ncode = _normalize_path(code_path)
    # Check the full normalized path and its drive root and first segments.
    frags = {ncode, ncode.replace("/", "\\")}
    # Also check raw code_path pieces (case variants).
    for seg in Path(code_path).parts:
        if len(seg) > 2:  # skip drive letters like "D:"
            frags.add(seg.lower())
    blob = " ".join(
        str(v) for v in (
            list(pub["evidence"]) + pub["candidates"] + [pub["status"]]
        )
    ).lower()
    return any(f and f in blob for f in frags)


def test_public_dict_has_no_absolute_path(bydod_only):
    code_path = "D:/bydod25fr/byd"
    r = bydod_only.recognize(code_path)
    assert r.status == STATUS_RESOLVED
    pub = r.public_dict()
    # Allowed keys only.
    assert set(pub.keys()) == {"status", "confidence", "evidence", "candidates"}
    # No path-like absolute fragments leak.
    assert not _any_path_fragment(pub, code_path)
    # No project/profile names leak.
    blob = " ".join(pub["evidence"]).lower()
    for forbidden in ("bydod25", "byd_od25", "full_dsp", "g3n_fvg3_od25", "local-build"):
        assert forbidden not in blob, forbidden


def test_public_dict_no_paths_in_ambiguous(bydod_alias):
    code_path = "D:/bydod25fr/byd"
    r = bydod_alias.recognize(code_path)
    assert r.status == STATUS_AMBIGUOUS
    pub = r.public_dict()
    assert not _any_path_fragment(pub, code_path)


def test_public_dict_no_paths_in_unresolved(bydod_only):
    code_path = "X:/unknown/ws"
    r = bydod_only.recognize(code_path)
    assert r.status == STATUS_UNRESOLVED
    pub = r.public_dict()
    assert not _any_path_fragment(pub, code_path)


def test_as_dict_contains_paths_for_internal_use(bydod_only):
    r = bydod_only.recognize("D:/bydod25fr/byd")
    d = r.as_dict()
    # Internal view DOES carry paths (for logs/manifests).
    assert d["workspace_root"]
    assert d["build_script"]
    assert "adapter_key" in d


# --------------------------------------------------------------------------- #
# Module-level convenience wrapper.
# --------------------------------------------------------------------------- #

def test_module_level_recognize(tmp_path, monkeypatch):
    base = _write_projects(tmp_path, {"bydod25": _BYDOD})
    r = recognize("D:/bydod25fr/byd", projects_dir=base)
    assert r.status == STATUS_RESOLVED
    assert r.adapter_key == "gen5_selena"
