from pathlib import Path

from core.build_script_policy import (
    adapt_build_script_for_incremental,
    has_existing_build_artifact,
    is_clean_command,
)


def test_r2d2_clean_command_is_disabled_without_project_specific_path(tmp_path: Path):
    script = tmp_path / "another_product" / "tools" / "compile.bat"
    script.parent.mkdir(parents=True)
    script.write_text(
        "@echo off\n"
        "echo [Cleaning]\n"
        "%python3% %root_path%/tools/R2D2.py -m %cfg% -clean\n"
        "%python3% %root_path%/tools/R2D2.py -bm RelWithDebInfo\n",
        encoding="utf-8",
    )

    result = adapt_build_script_for_incremental(script, existing_build=True)
    text = script.read_text(encoding="utf-8")

    assert result.changed is True
    assert result.existing_build_detected is True
    assert result.clean_command_lines == (3,)
    assert result.suppressed_lines == (3,)
    assert "-clean" in text
    assert text.splitlines()[2].lstrip().startswith("rem radar-sim:")
    assert "-bm RelWithDebInfo" in text
    assert adapt_build_script_for_incremental(script, existing_build=True).changed is False


def test_clean_detection_handles_continuations_and_known_build_tools(tmp_path: Path):
    script = tmp_path / "build.cmd"
    script.write_text(
        "cmake --build out ^\n"
        "  --clean-first\n"
        "ninja clean\n"
        "call clean.cmd\n"
        "echo clean is only a message\n"
        "set CLEAN=1\n",
        encoding="utf-8",
    )

    result = adapt_build_script_for_incremental(script)
    lines = script.read_text(encoding="utf-8").splitlines()

    assert result.clean_command_lines == (1, 2, 3, 4)
    assert result.suppressed_lines == (1, 2, 3, 4)
    assert all(line.lstrip().startswith("rem radar-sim:") for line in lines[:4])
    assert lines[4] == "echo clean is only a message"
    assert lines[5] == "set CLEAN=1"


def test_explicit_clean_request_keeps_detected_commands_enabled(tmp_path: Path):
    script = tmp_path / "build.bat"
    original = "python build.py --clean\n"
    script.write_text(original, encoding="utf-8")

    result = adapt_build_script_for_incremental(script, allow_clean=True)

    assert result.changed is False
    assert result.explicit_clean_requested is True
    assert result.clean_command_lines == (1,)
    assert script.read_text(encoding="utf-8") == original


def test_explicit_clean_restores_a_line_previously_suppressed(tmp_path: Path):
    script = tmp_path / "build.bat"
    original = "python build.py --clean\n"
    script.write_text(original, encoding="utf-8")

    adapt_build_script_for_incremental(script)
    restored = adapt_build_script_for_incremental(script, allow_clean=True)

    assert restored.changed is True
    assert restored.explicit_clean_requested is True
    assert script.read_text(encoding="utf-8") == original


def test_existing_artifact_detection_is_bounded_to_output_roots(tmp_path: Path):
    output = tmp_path / "build-output"
    nested = output / "RelWithDebInfo"
    nested.mkdir(parents=True)
    artifact = nested / "selena.exe"
    artifact.write_bytes(b"compiled")

    assert has_existing_build_artifact(tmp_path / "missing.exe", [output]) is True
    assert is_clean_command("%python3% R2D2.py -m config -clean") is True
    assert is_clean_command("echo Cleaning the environment") is False
    assert is_clean_command("python build.py C:\\workspace\\clean\\data") is False
    assert is_clean_command("ninja --clean-build") is False


def test_destructive_output_cleanup_and_generic_clean_scripts_are_disabled(tmp_path: Path):
    script = tmp_path / "build.bat"
    script.write_text(
        "git clean -fdx\n"
        "rmdir /s /q build-output\n"
        "powershell -File clean.ps1\n"
        "del /q C:\\workspace\\generated\\old.obj\n",
        encoding="utf-8",
    )

    result = adapt_build_script_for_incremental(script)

    assert result.clean_command_lines == (1, 2, 3, 4)
    assert result.suppressed_lines == (1, 2, 3, 4)


def test_non_batch_scripts_use_their_own_comment_syntax_and_restore(tmp_path: Path):
    for suffix, marker in ((".ps1", "# radar-sim:"), (".sh", "# radar-sim:")):
        script = tmp_path / f"build{suffix}"
        original = "python build.py --clean\n"
        script.write_text(original, encoding="utf-8")

        adapted = adapt_build_script_for_incremental(script)
        assert adapted.changed is True
        assert script.read_text(encoding="utf-8").startswith(marker)

        restored = adapt_build_script_for_incremental(script, allow_clean=True)
        assert restored.changed is True
        assert script.read_text(encoding="utf-8") == original
