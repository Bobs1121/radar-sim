import pytest

from core.shared_namespace import (
    SharedNamespace,
    SharedNamespaceError,
    SharedNamespaceRegistry,
)


def _registry() -> SharedNamespaceRegistry:
    return SharedNamespaceRegistry(
        [
            SharedNamespace("general", r"\\server\share", "/mnt/share", r"\\worker\share"),
            SharedNamespace("project", r"\\server\share\Project", "/mnt/project", r"\\worker\project"),
        ]
    )


def test_longest_prefix_wins_and_preserves_suffix_case():
    result = _registry().resolve(r"\\SERVER\SHARE\project\SceneA\Input.MF4")
    assert result.namespace_id == "project"
    assert result.relative_path == "SceneA/Input.MF4"
    assert result.central_probe_path == "/mnt/project/SceneA/Input.MF4"
    assert result.worker_path == r"\\worker\project\SceneA\Input.MF4"


def test_prefix_match_requires_path_boundary():
    with pytest.raises(SharedNamespaceError, match="authorized"):
        _registry().resolve(r"\\server\share-other\a.MF4")


@pytest.mark.parametrize(
    "path",
    [
        r"\\server\share\..\secret.MF4",
        "//server/share//a.MF4",
        "//server",
        "D:/data/a.MF4",
        "/mnt/share/a.MF4",
    ],
)
def test_shared_path_rejects_traversal_empty_or_non_unc(path: str):
    with pytest.raises(SharedNamespaceError):
        _registry().resolve(path)


def test_arbitrary_unc_is_not_trusted():
    with pytest.raises(SharedNamespaceError, match="authorized"):
        _registry().resolve(r"\\other\share\a.MF4")


def test_existing_linux_mount_map_is_migrated():
    registry = SharedNamespaceRegistry.from_config(
        {"cluster": {"linux_mount_map": {r"\\server\share": "/mnt/share"}}}
    )
    result = registry.resolve(r"\\server\share\case")
    assert result.central_probe_path == "/mnt/share/case"
    assert result.worker_path == r"\\server\share\case"


def test_public_summary_does_not_expose_paths():
    public = _registry().public_summary()
    assert public == [{"id": "project"}, {"id": "general"}]
    assert "server" not in str(public)
    assert "/mnt" not in str(public)


def test_namespace_config_requires_absolute_central_mount():
    with pytest.raises(SharedNamespaceError, match="absolute mount"):
        SharedNamespace("bad", r"\\server\share", "relative/path", r"\\worker\share")
