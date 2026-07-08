"""Tests for gen5_selena platform submodules (v4)."""

import os
import tempfile
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

# Use a minimal config for all tests
_MIN_CONFIG = {
    "project": {"name": "test", "platform": "gen5_selena"},
    "paths": {
        "source_root": "/src",
        "build_output": "/build",
        "r2d2_script": "/r2d2.py",
        "build_config": "/bc.config",
        "results_dir": tempfile.mkdtemp(),
    },
    "selena": {
        "executable_name": "selena.exe",
        "runtime_xml": "/rt.xml",
        "config_template": "/tmpl.txt",
        "exe_pattern": "dc_tools/selena/core/{build_mode}/selena.exe",
        "build_mode": "RelWithDebInfo",
        "simulation_timeout": 600,
    },
    "environment": {
        "python3_path": "/py/bin/python3.exe",
        "boost_root": "/boost",
        "path_prefix": ["/matlab/bin", "/qt/bin"],
    },
    "analysis": {},
}


# ---------------------------------------------------------------------------
# Log Parser tests
# ---------------------------------------------------------------------------


class TestLogParser:
    def setup_method(self):
        from platforms.gen5_selena.log_parser import Gen5LogParser
        self.parser = Gen5LogParser(_MIN_CONFIG)

    def test_parse_empty_log(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            f.write("")
            f.flush()
            result = self.parser.parse(f.name)
        os.unlink(f.name)
        assert result.runnables_loaded == 0
        assert len(result.errors) == 0

    def test_parse_error_entries(self):
        log_content = (
            "[15:32:20.727] (thread 12345) [error]: Something failed\n"
            "[15:32:21.000] (thread 12345) [warning]: Watch out\n"
            "[15:32:22.500] (thread 12345) [info]: All good\n"
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            f.write(log_content)
            f.flush()
            result = self.parser.parse(f.name)
        os.unlink(f.name)
        assert len(result.errors) == 1
        assert len(result.warnings) == 1
        assert result.errors[0].message == "Something failed"

    def test_parse_version(self):
        log_content = "Starting Selena 1.18.0 Roberta\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            f.write(log_content)
            f.flush()
            result = self.parser.parse(f.name)
        os.unlink(f.name)
        assert "1.18.0" in result.version

    def test_parse_runnables(self):
        log_content = (
            "Loading runnable: RunnableCfmFcta\n"
            "Loading runnable: RunnableFsm\n"
            "Loading runnable: TguOmiRunnable\n"
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            f.write(log_content)
            f.flush()
            result = self.parser.parse(f.name)
        os.unlink(f.name)
        assert result.runnables_loaded == 3

    def test_parse_connections(self):
        log_content = "316 connections established\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            f.write(log_content)
            f.flush()
            result = self.parser.parse(f.name)
        os.unlink(f.name)
        assert result.connections == 316

    def test_parse_duration(self):
        log_content = (
            "[00:00:00.000] (thread 1) [info]: start\n"
            "[00:00:10.500] (thread 1) [info]: end\n"
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            f.write(log_content)
            f.flush()
            result = self.parser.parse(f.name)
        os.unlink(f.name)
        assert abs(result.duration_sec - 10.5) < 0.01

    def test_parse_missing_file(self):
        result = self.parser.parse("/nonexistent/path.log")
        assert len(result.errors) == 1
        assert "not found" in result.errors[0].message.lower()


# ---------------------------------------------------------------------------
# Builder environment tests
# ---------------------------------------------------------------------------


class TestBuilderEnv:
    def setup_method(self):
        from platforms.gen5_selena.selena_builder import SelenaBuilder
        self.builder = SelenaBuilder(_MIN_CONFIG)

    def test_build_env_includes_boost_root(self):
        env = self.builder._build_env()
        assert env["BOOST_ROOT"] == "/boost"

    def test_build_env_path_assembly(self):
        env = self.builder._build_env()
        path_str = env["PATH"].replace("\\", "/")
        assert "/matlab/bin" in path_str
        assert "/qt/bin" in path_str
        assert "boost/lib64-msvc-14.0" in path_str
        assert "/py/bin" in path_str

    def test_check_environment_missing_r2d2(self):
        issues = self.builder.check_environment()
        # R2D2 script doesn't exist in test config
        assert any("R2D2" in i for i in issues)


# ---------------------------------------------------------------------------
# MF4 Reader tests
# ---------------------------------------------------------------------------


class TestMf4ReaderFuzzy:
    """Test fuzzy matching without actual MF4 files."""

    def test_fuzzy_exact_match(self):
        from platforms.gen5_selena.mf4_reader import Gen5Mf4Reader
        available = ["FCTA_State", "TGU_Distance", "BSD_Alarm"]
        assert Gen5Mf4Reader._fuzzy_match("FCTA_State", available) == "FCTA_State"

    def test_fuzzy_case_insensitive(self):
        from platforms.gen5_selena.mf4_reader import Gen5Mf4Reader
        available = ["FCTA_State", "TGU_Distance"]
        assert Gen5Mf4Reader._fuzzy_match("fcta_state", available) == "FCTA_State"

    def test_fuzzy_substring(self):
        from platforms.gen5_selena.mf4_reader import Gen5Mf4Reader
        available = ["PerUnitedRunnable_FCTA_State"]
        result = Gen5Mf4Reader._fuzzy_match("FCTA_State", available)
        assert result == "PerUnitedRunnable_FCTA_State"

    def test_fuzzy_no_match(self):
        from platforms.gen5_selena.mf4_reader import Gen5Mf4Reader
        available = ["Completely_Different_Signal"]
        assert Gen5Mf4Reader._fuzzy_match("FCTA_State", available) is None


class TestMf4ReaderExtract:
    """Test extract/list_available_signals with mocked MDF (no keys() attr)."""

    def _make_mock_sig(self, name: str):
        sig = MagicMock()
        sig.timestamps = np.array([0.0, 0.1, 0.2])
        sig.values = np.array([1.0, 2.0, 3.0])
        sig.unit = "m/s"
        return sig

    def _make_mock_mdf(self, channel_names: list[str], signals: dict[str, MagicMock]):
        """Create a mock MDF that mimics asammdf >= 8.x (no keys(), has channels_db)."""
        mdf = MagicMock()
        # asammdf 8.x: channels_db is a dict-like with .keys()
        channels_db = dict.fromkeys(channel_names)
        mdf.channels_db.keys.return_value = channel_names
        # mdf[name] no longer works; mdf.get(name) is the API
        mdf.get = lambda name: signals.get(name)
        return mdf

    def test_extract_exact_match(self):
        from platforms.gen5_selena.mf4_reader import Gen5Mf4Reader

        reader = Gen5Mf4Reader(_MIN_CONFIG)
        sig = self._make_mock_sig("FCTA_State")
        mock_mdf = self._make_mock_mdf(["FCTA_State", "TGU_Dist"], {"FCTA_State": sig})

        with patch("asammdf.MDF", return_value=mock_mdf):
            result = reader.extract("/fake/test.mf4", ["FCTA_State"])

        assert "FCTA_State" in result
        assert result["FCTA_State"].values == [1.0, 2.0, 3.0]
        assert result["FCTA_State"].unit == "m/s"

    def test_extract_fuzzy_match(self):
        from platforms.gen5_selena.mf4_reader import Gen5Mf4Reader

        reader = Gen5Mf4Reader(_MIN_CONFIG)
        sig = self._make_mock_sig("PerUnitedRunnable_FCTA_State")
        mock_mdf = self._make_mock_mdf(
            ["PerUnitedRunnable_FCTA_State"], {"PerUnitedRunnable_FCTA_State": sig}
        )

        with patch("asammdf.MDF", return_value=mock_mdf):
            result = reader.extract("/fake/test.mf4", ["FCTA_State"])

        assert "PerUnitedRunnable_FCTA_State" in result

    def test_extract_no_match_skipped(self):
        from platforms.gen5_selena.mf4_reader import Gen5Mf4Reader

        reader = Gen5Mf4Reader(_MIN_CONFIG)
        mock_mdf = self._make_mock_mdf(["Unrelated_Signal"], {})

        with patch("asammdf.MDF", return_value=mock_mdf):
            result = reader.extract("/fake/test.mf4", ["NonExistent"])

        assert len(result) == 0

    def test_list_available_signals(self):
        from platforms.gen5_selena.mf4_reader import Gen5Mf4Reader

        reader = Gen5Mf4Reader(_MIN_CONFIG)
        names = ["FCTA_State", "TGU_Distance", "BSD_Alarm"]
        mock_mdf = self._make_mock_mdf(names, {})

        with patch("asammdf.MDF", return_value=mock_mdf):
            signals = reader.list_available_signals("/fake/test.mf4")

        assert signals == names

    def test_extract_uses_channels_db_not_keys(self):
        """Regression: ensure we call channels_db.keys(), not mdf.keys()."""
        from platforms.gen5_selena.mf4_reader import Gen5Mf4Reader

        reader = Gen5Mf4Reader(_MIN_CONFIG)
        sig = self._make_mock_sig("SigA")
        mock_mdf = self._make_mock_mdf(["SigA"], {"SigA": sig})
        # Explicitly ensure .keys() does NOT exist (simulates asammdf 8.x)
        del mock_mdf.keys

        with patch("asammdf.MDF", return_value=mock_mdf):
            result = reader.extract("/fake/test.mf4", ["SigA"])


# ---------------------------------------------------------------------------
# Platform registration test
# ---------------------------------------------------------------------------


class TestPlatformRegistry:
    def test_gen5_registered(self):
        from platforms import get, list_all
        assert "gen5_selena" in list_all()

    def test_gen5_instance(self):
        from platforms import get
        platform = get("gen5_selena", _MIN_CONFIG)
        assert platform.platform_name == "gen5_selena"
        assert hasattr(platform, "build")
        # v4: run_simulation removed (user runs Selena manually in VS)
        assert hasattr(platform, "extract_signals")
        assert hasattr(platform, "parse_log")
