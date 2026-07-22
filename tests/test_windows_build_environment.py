from pathlib import Path

import pytest

from core.windows_build_environment import (
    WindowsBuildDependencyError,
    prepare_windows_build_environment,
)


def _script_tree(tmp_path: Path):
    workspace = tmp_path / "workspace"
    scripts = workspace / "apl" / "base" / "bindings" / "demo" / "buildscripts"
    cmake = scripts.parent / "cmake"
    selena = scripts.parent / "selena"
    scripts.mkdir(parents=True)
    cmake.mkdir()
    selena.mkdir()
    package = scripts / "testbuild_Demo.bat"
    package.write_text("call patch.bat\n", encoding="utf-8")
    selena_script = selena / "jenkins_selena_build.bat"
    selena_script.write_text("python R2D2.py\n", encoding="utf-8")
    dependency = cmake / "generate_PAD_params.cmake"
    dependency.write_text(
        'set(perlFromEnv "perl")\nexecute_process(COMMAND ${perlFromEnv} pad_generator.pl)\n',
        encoding="utf-8",
    )
    return workspace, package, selena_script, dependency


def test_script_adjacent_perl_dependency_is_added_to_process_environment(tmp_path):
    workspace, package, selena_script, dependency = _script_tree(tmp_path)
    tcc_perl = tmp_path / "tcc" / "perl-tool"
    perl = tcc_perl / "perl" / "bin" / "perl.exe"
    strawberry = tcc_perl / "c" / "bin"
    perl.parent.mkdir(parents=True)
    strawberry.mkdir(parents=True)
    perl.write_bytes(b"perl")
    original = dependency.read_bytes()

    result = prepare_windows_build_environment(
        workspace_root=workspace,
        selena_build_script=selena_script,
        package_build_script=package,
        env={"TCCPATH_perl": str(tcc_perl), "PATH": "original"},
    )

    assert result.dependencies == ("perl",)
    assert result.perl_executable == str(perl.resolve())
    assert str(perl.parent) in result.environment["PATH"]
    assert str(strawberry) in result.environment["PATH"]
    assert result.environment["PATH"].endswith("original")
    assert result.evidence == (
        "apl/base/bindings/demo/cmake/generate_PAD_params.cmake",
    )
    assert dependency.read_bytes() == original


def test_missing_script_derived_perl_fails_before_build(tmp_path, monkeypatch):
    workspace, package, selena_script, _dependency = _script_tree(tmp_path)
    monkeypatch.setattr("core.windows_build_environment._candidate_perl_paths", lambda *_args: [])

    with pytest.raises(WindowsBuildDependencyError, match="require Perl"):
        prepare_windows_build_environment(
            workspace_root=workspace,
            selena_build_script=selena_script,
            package_build_script=package,
            env={"PATH": ""},
        )


def test_unrelated_build_scripts_do_not_require_perl(tmp_path):
    workspace = tmp_path / "workspace"
    script = workspace / "tools" / "build.bat"
    script.parent.mkdir(parents=True)
    script.write_text("cmake --build out\n", encoding="utf-8")

    result = prepare_windows_build_environment(
        workspace_root=workspace,
        selena_build_script=script,
        env={"PATH": "unchanged"},
    )

    assert result.dependencies == ()
    assert result.environment["PATH"] == "unchanged"
