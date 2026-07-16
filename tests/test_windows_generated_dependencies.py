from pathlib import Path
from types import SimpleNamespace

from core.windows_generated_dependencies import prepare_package_generated_dependencies


def _workspace(tmp_path: Path):
    root = tmp_path / "workspace"
    builder = root / "apl" / "byd" / "tools" / "builder"
    builder.mkdir(parents=True)
    package = builder / "cmake_build.bat"
    package.write_text("call %~dp0\\cmake_gen.bat\n", encoding="utf-8")
    generator = builder / "GEN_PAD_PARAMS.bat"
    generator.write_text(
        "set cp=%~dp0\n"
        'set "PAD_COMMOND=C:/Perl/bin/perl.exe -I %cp%../../../../ip_if/tools/pad_gen/lib '
        '%cp%../../../../ip_if/tools/pad_gen/bin/pad_generator.pl -p "\n'
        "%PAD_COMMOND% %cp%../../components/rpm/padrpm/ -b %cp%../../components/rpm/padrpm/padrpm.xml\n",
        encoding="utf-8",
    )
    lib = root / "ip_if" / "tools" / "pad_gen" / "lib"
    lib.mkdir(parents=True)
    perl_script = root / "ip_if" / "tools" / "pad_gen" / "bin" / "pad_generator.pl"
    perl_script.parent.mkdir(parents=True)
    perl_script.write_text("generator", encoding="utf-8")
    target = root / "apl" / "byd" / "components" / "rpm" / "padrpm"
    target.mkdir(parents=True)
    (target / "padrpm.xml").write_text("<xml/>", encoding="utf-8")
    return root, package, target


def test_package_generator_uses_tcc_perl_and_process_local_path(tmp_path, monkeypatch):
    root, package, target = _workspace(tmp_path)
    perl_root = tmp_path / "tcc-perl"
    perl = perl_root / "perl" / "bin" / "perl.exe"
    perl.parent.mkdir(parents=True)
    perl.write_bytes(b"perl")
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        (target / "padrpm_pub_gen.h").write_text("generated", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    result = prepare_package_generated_dependencies(
        package,
        root,
        env={"TCCPATH_perl": str(perl_root), "PATH": "original"},
        runner=run,
    )

    assert result.changed is True
    assert result.generated_targets == ("apl/byd/components/rpm/padrpm",)
    assert calls[0][0][0] == str(perl)
    assert str(perl_root / "c" / "bin") in calls[0][1]["env"]["PATH"]


def test_package_generator_is_skipped_when_generated_headers_exist(tmp_path):
    root, package, target = _workspace(tmp_path)
    (target / "padrpm_pub_gen.h").write_text("generated", encoding="utf-8")

    result = prepare_package_generated_dependencies(package, root, runner=lambda *_args, **_kwargs: None)

    assert result.changed is False
