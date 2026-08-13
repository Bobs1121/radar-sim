import os
from pathlib import Path

from core.selena_runtime_environment import infer_selena_runtime_environment
from cli.agent import _apply_project_independent_runtime_environment
from core.config import load_local_execution_config


def test_infers_runtime_path_from_nearest_generic_cmake_cache(tmp_path: Path) -> None:
    build = tmp_path / "any-workspace" / "build" / "variant"
    selena = build / "dc_tools" / "selena" / "core" / "RelWithDebInfo"
    selena.mkdir(parents=True)
    qt = tmp_path / "deps" / "qt" / "msvc"
    matlab = tmp_path / "deps" / "matlab"
    boost = tmp_path / "deps" / "boost-lib"
    env_bin = tmp_path / "deps" / "selena-env" / "bin"
    for path in (qt / "bin", qt / "lib", matlab / "bin" / "win64", boost, env_bin):
        path.mkdir(parents=True)
    (build / "CMakeCache.txt").write_text(
        "\n".join(
            (
                f"Qt5Core_DIR:PATH={qt.as_posix()}/lib/cmake/Qt5Core",
                f"Qt5Xml_DIR:PATH={qt.as_posix()}/lib/cmake/Qt5Xml",
                f"Matlab_ROOT_DIR:PATH={matlab.as_posix()}",
                f"Boost_LIBRARY_DIR_RELEASE:PATH={boost.as_posix()}",
                f"PYTHON_EXECUTABLE:FILEPATH={env_bin.as_posix()}/python.exe",
                "Qt5Gui_DIR:PATH=C:/missing/qt/lib/cmake/Qt5Gui",
            )
        ),
        encoding="utf-8",
    )

    result = infer_selena_runtime_environment((selena,))

    assert result.cache_path == build / "CMakeCache.txt"
    assert result.path_prefix == (
        matlab / "bin" / "win64",
        qt / "bin",
        qt / "lib",
        boost,
        env_bin,
    )


def test_missing_cache_preserves_empty_fallback(tmp_path: Path) -> None:
    folder = tmp_path / "standalone" / "Selena"
    folder.mkdir(parents=True)

    result = infer_selena_runtime_environment((folder, "", tmp_path / "missing"))

    assert result.cache_path is None
    assert result.path_prefix == ()


def test_workspace_fallback_selects_latest_bounded_build_cache(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    older = workspace / "ip_dc" / "build" / "old"
    newer = workspace / "ip_dc" / "build" / "new"
    dep = tmp_path / "dep"
    for path in (older, newer, dep / "bin" / "win64"):
        path.mkdir(parents=True)
    (older / "CMakeCache.txt").write_text("Matlab_ROOT_DIR:PATH=C:/missing\n", encoding="utf-8")
    os.utime(older / "CMakeCache.txt", (1, 1))
    cache = newer / "CMakeCache.txt"
    cache.write_text(f"Matlab_ROOT_DIR:PATH={dep.as_posix()}\n", encoding="utf-8")
    os.utime(cache, (2, 2))

    result = infer_selena_runtime_environment((workspace,))

    assert result.cache_path == cache
    assert result.path_prefix == (dep / "bin" / "win64",)


def test_local_preflight_injects_private_environment_without_project_adapter(
    tmp_path: Path,
) -> None:
    build = tmp_path / "workspace" / "build" / "variant"
    selena = build / "selena" / "RelWithDebInfo"
    matlab = tmp_path / "matlab"
    selena.mkdir(parents=True)
    (matlab / "bin" / "win64").mkdir(parents=True)
    (build / "CMakeCache.txt").write_text(
        f"Matlab_ROOT_DIR:PATH={matlab.as_posix()}\n",
        encoding="utf-8",
    )
    config = {"environment": {"path_prefix": ["already-present"]}}

    _apply_project_independent_runtime_environment(
        config,
        {"existing_path": str(selena)},
        {},
    )

    assert config["environment"]["path_prefix"] == [
        str(matlab / "bin" / "win64"),
        "already-present",
    ]


def test_local_runtime_environment_never_calls_legacy_project_derivation(
    tmp_path: Path, monkeypatch,
) -> None:
    """V2 local PATH reconstruction stays independent of product heuristics."""
    workspace = tmp_path / "workspace"
    build = workspace / "build" / "variant"
    selena = build / "dc_tools" / "selena" / "core" / "RelWithDebInfo"
    dependency = tmp_path / "dependency" / "bin" / "win64"
    selena.mkdir(parents=True)
    dependency.mkdir(parents=True)
    (build / "CMakeCache.txt").write_text(
        f"Matlab_ROOT_DIR:PATH={dependency.parent.parent.as_posix()}\n",
        encoding="utf-8",
    )

    def legacy_derivation_must_not_run(*_args, **_kwargs):
        raise AssertionError("legacy project derivation must be unreachable for V2")

    monkeypatch.setattr(
        "core.config.derive_project_context_from_selena_script",
        legacy_derivation_must_not_run,
    )
    config = {"environment": {"path_prefix": []}}
    _apply_project_independent_runtime_environment(
        config,
        {
            "existing_path": str(selena),
            "selena_build_script": str(workspace / "legacy-product-script.bat"),
        },
        {"code_path": str(workspace)},
    )

    assert config["environment"]["path_prefix"] == [str(dependency)]


def test_project_independent_selena_contract_is_tolerant_by_default() -> None:
    config = load_local_execution_config("workspace-0123456789abcdef01234567")

    assert config["simulation"]["tolerant"] is True
