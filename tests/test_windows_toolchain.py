from __future__ import annotations

from pathlib import Path

import pytest

from core.windows_toolchain import (
    VisualStudioInstallation,
    WindowsToolchainError,
    adapt_selena_script_visual_studio,
    detect_visual_studio_installations,
)


def test_detects_visual_studio_2015_x64_compiler(tmp_path):
    pf86 = tmp_path / "Program Files x86"
    compiler = pf86 / "Microsoft Visual Studio 14.0" / "VC" / "bin" / "amd64" / "cl.exe"
    compiler.parent.mkdir(parents=True)
    compiler.write_bytes(b"")

    found = detect_visual_studio_installations(program_files_x86=pf86, program_files=tmp_path / "pf")

    assert [(item.tag, item.year, item.toolset) for item in found] == [("vs14", "2015", "v140")]


def test_requested_version_is_read_from_vs_postfix_assignment():
    from core.windows_toolchain import requested_visual_studio_tag

    text = 'SET "VS_POSTFIX=-vs vs16"\npython3 R2D2.py %VS_POSTFIX%\n'

    assert requested_visual_studio_tag(text) == "vs16"


def test_adapts_hardcoded_vs2019_to_installed_vs2015(tmp_path):
    script = tmp_path / "jenkins.bat"
    script.write_text(
        'SET "VS_POSTFIX="\n'
        'if exist "C:\\Program Files (x86)\\Microsoft Visual Studio\\2019" SET "VS_POSTFIX=-vs vs16"\n'
        'python3 R2D2.py --clean %VS_POSTFIX%\n'
        'python3 R2D2.py -bm RelWithDebInfo -vs vs16\n',
        encoding="utf-8",
    )
    vs2015 = VisualStudioInstallation("vs14", "2015", "v140")

    result = adapt_selena_script_visual_studio(script, installations=[vs2015])
    text = script.read_text(encoding="utf-8")

    assert result.changed is True
    assert result.requested_tag == "vs16"
    assert result.installation.tag == "vs14"
    assert "-vs vs16" not in text
    assert 'SET "VS_POSTFIX="' in text
    assert "Windows Agent" in text
    assert adapt_selena_script_visual_studio(script, installations=[vs2015]).changed is False


def test_adapts_default_script_to_installed_vs2019(tmp_path):
    script = tmp_path / "jenkins.bat"
    script.write_text('SET "VS_POSTFIX="\npython3 R2D2.py --clean %VS_POSTFIX%\npython3 R2D2.py -bm Release\n')
    vs2019 = VisualStudioInstallation("vs16", "2019", "v142")

    adapt_selena_script_visual_studio(script, installations=[vs2019])
    text = script.read_text()

    assert 'SET "VS_POSTFIX=-vs vs16"' in text
    assert text.count("-vs vs16") == 2


def test_missing_visual_studio_is_actionable_and_does_not_modify_script(tmp_path):
    script = tmp_path / "jenkins.bat"
    script.write_text("python3 R2D2.py -vs vs16\n")

    with pytest.raises(WindowsToolchainError, match="Install Visual Studio"):
        adapt_selena_script_visual_studio(script, installations=[])

    assert "-vs vs16" in script.read_text()
