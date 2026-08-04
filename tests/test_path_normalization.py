from core.agent_asset_bindings import make_asset_binding_id
from core.agent_bindings import make_workspace_path_id
from core.agent_data_bindings import make_data_binding_id
from core.path_normalization import normalize_path_text
from core.user_config import UserRunConfig


def test_windows_path_spellings_normalize_for_user_config_and_matching_ids():
    first = r"D:\repo\..\repo\src"
    second = "d:/repo/src"

    assert normalize_path_text(first) == "D:/repo/src"
    assert make_workspace_path_id(first) == make_workspace_path_id(second)
    assert make_asset_binding_id(first) == make_asset_binding_id(second)
    assert make_data_binding_id("ovrs25", first) == make_data_binding_id("ovrs25", second)
    assert normalize_path_text("D://repo//src") == "D:/repo/src"


def test_unc_slash_spellings_normalize_for_matching_ids():
    first = r"\\server\share\data\..\data\Radar"
    second = "//server/share/data/Radar"

    assert normalize_path_text(first) == "//server/share/data/Radar"
    assert make_workspace_path_id(first) == make_workspace_path_id(second)
    assert make_asset_binding_id(first) == make_asset_binding_id(second)


def test_logical_uri_keeps_uri_semantics():
    assert normalize_path_text("shared://server//x/../y") == "shared://server/x/../y"


def test_user_run_config_exports_canonical_path_spelling():
    config = UserRunConfig.from_dict(
        {
            "schema_version": "2.0",
            "selena": {
                "source": "existing",
                "existing_path": r"D:\selena\..\selena\RelWithDebInfo",
                "runtime_xml": r"D:\selena\runtime.xml",
            },
            "data": {"path": r"D:\data\Radar\..\Radar"},
            "simulation": {"target": "cluster", "mat_filter": r"D:\cfg\mat.filter"},
        }
    )

    assert config.selena.existing_path == "D:/selena/RelWithDebInfo"
    assert config.data.path == "D:/data/Radar"
