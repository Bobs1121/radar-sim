"""Tests for core.policy: 8-combination run-policy matrix."""

import pytest

from core.policy import RunPolicy, derive_run_policy, policy_from_config


LOCAL_DATA = r"D:\data\case.MF4"
UNC_DATA = r"\\share\data\case.MF4"
LOCAL_EXE = r"C:\build\selena.exe"
UNC_EXE = r"\\share\selena.exe"


# 8-combination matrix (path+cluster split into local-exe vs UNC-exe sub-cases)
@pytest.mark.parametrize("source,data,backend,exe,exp_copy_selena,exp_copy_data,exp_output_local", [
    ("build", LOCAL_DATA, "local",   "",        False, False, True),   # 1
    ("build", UNC_DATA,   "local",   "",        False, True,  True),   # 2
    ("build", LOCAL_DATA, "cluster", "",        True,  True,  False),  # 3
    ("build", UNC_DATA,   "cluster", "",        True,  False, False),  # 4
    ("path",  LOCAL_DATA, "local",   LOCAL_EXE, False, False, True),   # 5
    ("path",  UNC_DATA,   "local",   UNC_EXE,   False, True,  True),   # 6
    ("path",  LOCAL_DATA, "cluster", LOCAL_EXE, True,  True,  False),  # 7 (local exe → package)
    ("path",  UNC_DATA,   "cluster", UNC_EXE,   False, False, False),  # 8 (UNC exe → in place)
])
def test_derive_run_policy_matrix(source, data, backend, exe, exp_copy_selena, exp_copy_data, exp_output_local):
    p = derive_run_policy(source=source, data_path=data, backend=backend, selena_exe=exe)
    assert p.copy_selena is exp_copy_selena
    assert p.copy_data is exp_copy_data
    assert p.output_local is exp_output_local


def test_path_cluster_unc_exe_no_copy_selena():
    """source=path + cluster + UNC exe → copy_selena False (worker can see UNC)."""
    p = derive_run_policy(source="path", data_path=UNC_DATA, backend="cluster", selena_exe=UNC_EXE)
    assert p.copy_selena is False
    assert p.copy_data is False


def test_path_cluster_local_exe_copy_selena():
    """source=path + cluster + local exe → copy_selena True (worker can't see local)."""
    p = derive_run_policy(source="path", data_path=LOCAL_DATA, backend="cluster", selena_exe=LOCAL_EXE)
    assert p.copy_selena is True
    assert p.copy_data is True


def test_local_backend_never_copies_selena():
    """Local backend never packages selena — it runs where it is."""
    for source in ("build", "path"):
        p = derive_run_policy(source=source, data_path=LOCAL_DATA, backend="local", selena_exe=LOCAL_EXE)
        assert p.copy_selena is False


def test_data_is_unc_detected():
    p_unc = derive_run_policy(source="build", data_path=UNC_DATA, backend="cluster")
    assert p_unc.data_is_unc is True
    p_loc = derive_run_policy(source="build", data_path=LOCAL_DATA, backend="cluster")
    assert p_loc.data_is_unc is False


def test_rationale_non_empty():
    p = derive_run_policy(source="build", data_path=UNC_DATA, backend="local")
    assert p.rationale
    assert "本地" in p.rationale


def test_rationale_cluster():
    p = derive_run_policy(source="build", data_path=LOCAL_DATA, backend="cluster")
    assert "集群" in p.rationale
    assert "推送" in p.rationale


def test_policy_from_config_build_local():
    config = {
        "_profile_selena_source": "build",
        "active_backend": "local",
        "active_profile": "default",
        "profiles": [{"name": "default", "selena": {"source": "build"}, "backend": "local"}],
    }
    p = policy_from_config(config, LOCAL_DATA)
    assert p.backend == "local"
    assert p.copy_selena is False  # local backend
    assert p.copy_data is False    # local data


def test_policy_from_config_path_cluster_unc_exe():
    config = {
        "_profile_selena_source": "path",
        "active_backend": "cluster",
        "active_profile": "shared",
        "cluster": {"selena_exe": UNC_EXE},
        "profiles": [{"name": "shared", "selena": {"source": "path", "exe": UNC_EXE}, "backend": "cluster"}],
    }
    p = policy_from_config(config, UNC_DATA)
    assert p.copy_selena is False  # UNC exe, worker can see
    assert p.copy_data is False    # UNC data


def test_policy_from_config_build_cluster_local_data():
    config = {
        "_profile_selena_source": "build",
        "active_backend": "cluster",
        "active_profile": "cloud-build",
        "profiles": [{"name": "cloud-build", "selena": {"source": "build"}, "backend": "cluster"}],
    }
    p = policy_from_config(config, LOCAL_DATA)
    assert p.copy_selena is True   # build + cluster → package
    assert p.copy_data is True     # local data → migrate to share


def test_auto_copy_policy_wrapper_compat():
    """_auto_copy_policy still returns the dict shape tests depend on."""
    from core.api import _auto_copy_policy
    config = {"_profile_selena_source": "build", "active_backend": "cluster",
              "active_profile": "default", "profiles": [{"name": "default", "selena": {"source": "build"}, "backend": "cluster"}]}
    d = _auto_copy_policy(config, LOCAL_DATA)
    assert set(d.keys()) == {"copy_data", "copy_selena"}
    assert d["copy_selena"] is True
    assert d["copy_data"] is True
