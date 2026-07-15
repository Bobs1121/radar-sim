"""Tests for core/agent_policy.py.

Covers:
- normalization of node kinds and capabilities
- duplicate removal in capability lists
- wildcard rejection
- forbidden alias rejection for light agent
- corrupt-record claim defense
- light allowed build/artifact/data capabilities
- full local simulation positive claim
- legacy permissive compatibility
"""

import pytest

from core.agent_policy import (
    AgentPolicyError,
    DEFAULT_FULL_CAPABILITIES,
    DEFAULT_LIGHT_CAPABILITIES,
    FORBIDDEN_FOR_LIGHT_CAPABILITIES,
    FORBIDDEN_FOR_LIGHT_TASK_TYPES,
    LIGHT_AGENT_CAPABILITIES,
    LINUX_EXECUTOR_CAPABILITIES,
    MODE_FULL,
    MODE_LIGHT,
    MODE_TO_NODE_KIND,
    NODE_KIND_LINUX_EXECUTOR,
    NODE_KIND_LEGACY,
    NODE_KIND_PLATFORM_GATEWAY,
    NODE_KIND_WINDOWS_AGENT,
    NODE_KIND_WINDOWS_FULL,
    PLATFORM_GATEWAY_CAPABILITIES,
    default_capabilities_for_mode,
    forbidden_light_capabilities,
    filter_capabilities_for_node,
    is_forbidden_light_task_type,
    is_light_node_kind,
    is_windows_node_kind,
    may_claim_task,
    node_kind_for_mode,
    normalize_capabilities,
    normalize_node_kind,
    normalize_windows_mode,
    required_capabilities_for_task,
    validate_light_capabilities,
)


class TestNormalizeNodeKind:
    def test_known_kinds(self):
        assert normalize_node_kind("windows_agent") == "windows_agent"
        assert normalize_node_kind("windows_full") == "windows_full"
        assert normalize_node_kind("linux_executor") == "linux_executor"
        assert normalize_node_kind("platform_gateway") == "platform_gateway"
        assert normalize_node_kind("legacy") == NODE_KIND_LEGACY

    def test_case_and_whitespace(self):
        assert normalize_node_kind("  Windows_Agent  ") == "windows_agent"
        assert normalize_node_kind("WINDOWS_FULL") == "windows_full"
        assert normalize_node_kind("Linux_Executor") == "linux_executor"

    def test_none_returns_empty(self):
        assert normalize_node_kind(None) == ""

    def test_unknown_returns_lower(self):
        assert normalize_node_kind("UNKNOWN_KIND") == "unknown_kind"


class TestNormalizeWindowsMode:
    def test_defaults(self):
        assert normalize_windows_mode(None) == MODE_LIGHT
        assert normalize_windows_mode("") == MODE_LIGHT

    def test_full(self):
        assert normalize_windows_mode("full") == MODE_FULL
        assert normalize_windows_mode("FULL") == MODE_FULL
        assert normalize_windows_mode("  Full  ") == MODE_FULL

    def test_light(self):
        assert normalize_windows_mode("light") == MODE_LIGHT
        assert normalize_windows_mode("LIGHT") == MODE_LIGHT


class TestNodeKindForMode:
    def test_light(self):
        assert node_kind_for_mode("light") == NODE_KIND_WINDOWS_AGENT

    def test_full(self):
        assert node_kind_for_mode("full") == NODE_KIND_WINDOWS_FULL

    def test_invalid_raises(self):
        with pytest.raises(AgentPolicyError):
            node_kind_for_mode("invalid")


class TestIsLightNodeKind:
    def test_true(self):
        assert is_light_node_kind("windows_agent") is True
        assert is_light_node_kind("  windows_agent  ") is True

    def test_false(self):
        assert is_light_node_kind("windows_full") is False
        assert is_light_node_kind("linux_executor") is False
        assert is_light_node_kind(None) is False


class TestIsWindowsNodeKind:
    def test_true(self):
        assert is_windows_node_kind("windows_agent") is True
        assert is_windows_node_kind("windows_full") is True

    def test_false(self):
        assert is_windows_node_kind("linux_executor") is False
        assert is_windows_node_kind("platform_gateway") is False
        assert is_windows_node_kind(None) is False


class TestNormalizeCapabilities:
    def test_basic(self):
        assert normalize_capabilities(["build.selena", "artifact.upload"]) == [
            "build.selena",
            "artifact.upload",
        ]

    def test_deduplication(self):
        assert normalize_capabilities(
            ["build.selena", "build.selena", "artifact.upload"]
        ) == ["build.selena", "artifact.upload"]

    def test_whitespace_and_case(self):
        assert normalize_capabilities(
            ["  Build.Selena  ", "ARTIFACT.UPLOAD"]
        ) == ["build.selena", "artifact.upload"]

    def test_deduplicates_case_insensitively(self):
        assert normalize_capabilities(
            ["Build.Selena", " build.selena ", "BUILD.SELENA"]
        ) == ["build.selena"]

    @pytest.mark.parametrize("value", ["build.selena", ["build.selena", 7]])
    def test_rejects_non_list_or_non_string_items(self, value):
        with pytest.raises(AgentPolicyError, match="list of strings"):
            normalize_capabilities(value)

    def test_empty_and_none(self):
        assert normalize_capabilities([]) == []
        assert normalize_capabilities(None) == []

    def test_preserves_order(self):
        caps = ["a", "b", "c", "a", "d", "b"]
        assert normalize_capabilities(caps) == ["a", "b", "c", "d"]


class TestForbiddenLightCapabilities:
    def test_empty(self):
        assert forbidden_light_capabilities([]) == []
        assert forbidden_light_capabilities(None) == []

    def test_forbidden_detected(self):
        caps = ["simulation.local", "build.selena"]
        assert forbidden_light_capabilities(caps) == ["simulation.local"]

    def test_wildcards(self):
        assert forbidden_light_capabilities(["*"]) == ["*"]
        assert forbidden_light_capabilities(["local.*"]) == ["local.*"]
        assert forbidden_light_capabilities(["cluster.*"]) == ["cluster.*"]

    def test_allowed_not_reported(self):
        caps = list(LIGHT_AGENT_CAPABILITIES)
        assert forbidden_light_capabilities(caps) == []

    def test_case_insensitive(self):
        assert forbidden_light_capabilities(["SIMULATION.LOCAL"]) == ["simulation.local"]


class TestValidateLightCapabilities:
    def test_allowed_pass(self):
        caps = list(LIGHT_AGENT_CAPABILITIES)
        assert validate_light_capabilities(caps) == caps

    def test_forbidden_raises(self):
        with pytest.raises(AgentPolicyError) as exc:
            validate_light_capabilities(["simulation.local"])
        assert "simulation.local" in str(exc.value)

    def test_wildcard_raises(self):
        with pytest.raises(AgentPolicyError) as exc:
            validate_light_capabilities(["*"])
        assert "*" in str(exc.value)

    def test_mixed_allowed_and_forbidden(self):
        caps = ["build.selena", "simulation.local", "cluster.*"]
        with pytest.raises(AgentPolicyError) as exc:
            validate_light_capabilities(caps)
        msg = str(exc.value)
        assert "simulation.local" in msg
        assert "cluster.*" in msg

    def test_unknown_capability_is_rejected_by_allowlist(self):
        with pytest.raises(AgentPolicyError, match="unsupported capability"):
            validate_light_capabilities(["build.selena", "future.admin"])


class TestDefaultCapabilitiesForMode:
    def test_light(self):
        assert default_capabilities_for_mode("light") == DEFAULT_LIGHT_CAPABILITIES

    def test_full(self):
        assert default_capabilities_for_mode("full") == DEFAULT_FULL_CAPABILITIES

    def test_default_is_light(self):
        assert default_capabilities_for_mode(None) == DEFAULT_LIGHT_CAPABILITIES

    def test_invalid_mode_raises(self):
        with pytest.raises(AgentPolicyError, match="unsupported windows-mode"):
            default_capabilities_for_mode("gateway")

    def test_full_default_has_no_cluster_runtime(self):
        caps = set(default_capabilities_for_mode("full"))
        assert "local.run_sim" in caps
        assert "simulation.local" in caps
        assert "cluster.run" not in caps
        assert "cluster.gateway" not in caps
        assert "simulation.cluster" not in caps


class TestIsForbiddenLightTaskType:
    def test_forbidden_task_types(self):
        for alias in FORBIDDEN_FOR_LIGHT_TASK_TYPES:
            assert is_forbidden_light_task_type(alias) is True

    def test_forbidden_with_stage(self):
        assert is_forbidden_light_task_type(None, "run_simulation") is True
        assert is_forbidden_light_task_type(None, "preflight") is True

    def test_allowed_task_types(self):
        assert is_forbidden_light_task_type("build.selena") is False
        assert is_forbidden_light_task_type("artifact.upload") is False
        assert is_forbidden_light_task_type(None, None) is False

    def test_case_insensitive(self):
        assert is_forbidden_light_task_type("Run_Simulation") is True
        assert is_forbidden_light_task_type("Cluster.RUN") is True

    def test_corrupt_record_defense(self):
        # A corrupt record might store a forbidden value in either field.
        assert is_forbidden_light_task_type("run_simulation", None) is True
        assert is_forbidden_light_task_type(None, "run_simulation") is True
        assert is_forbidden_light_task_type("", "collect_results") is True
        assert is_forbidden_light_task_type("collect_results", "") is True


class TestConstants:
    def test_light_caps_subset_of_full(self):
        assert LIGHT_AGENT_CAPABILITIES.issubset(set(DEFAULT_FULL_CAPABILITIES))

    def test_forbidden_and_allowed_disjoint(self):
        assert LIGHT_AGENT_CAPABILITIES.isdisjoint(FORBIDDEN_FOR_LIGHT_CAPABILITIES)

    def test_mode_mapping(self):
        assert MODE_TO_NODE_KIND[MODE_LIGHT] == NODE_KIND_WINDOWS_AGENT
        assert MODE_TO_NODE_KIND[MODE_FULL] == NODE_KIND_WINDOWS_FULL


class TestMayClaimTask:
    @pytest.mark.parametrize(
        "task_type,stage_type",
        [
            ("local.check", None),
            ("local.build_selena", None),
            ("build_selena", "build_selena"),
            ("artifact.upload", None),
            ("register_artifact", "register_artifact"),
            ("prepare_data", "prepare_data"),
        ],
    )
    def test_light_allowed_work(self, task_type, stage_type):
        assert may_claim_task("windows_agent", task_type, stage_type)

    @pytest.mark.parametrize(
        "task_type,stage_type",
        [
            ("local.run_sim", None),
            ("cluster.run", None),
            ("run_simulation", "run_simulation"),
            ("collect_results", "collect_results"),
            ("finalize_manifest", "finalize_manifest"),
            ("future.admin", None),
        ],
    )
    def test_light_corrupt_record_is_denied(self, task_type, stage_type):
        assert not may_claim_task("windows_agent", task_type, stage_type)

    def test_full_local_simulation_is_allowed_but_cluster_is_not_default(self):
        assert may_claim_task("windows_full", "local.run_sim")
        assert may_claim_task("windows_full", "run_simulation", "run_simulation")
        assert not may_claim_task("windows_full", "cluster.run")
        assert not may_claim_task("windows_full", "cluster.gateway")

    def test_legacy_is_permissive(self):
        assert may_claim_task("legacy", "cluster.run")
        assert may_claim_task("", "future.legacy.task")

    def test_unknown_node_kind_is_denied(self):
        assert not may_claim_task("platform_gateway_typo", "cluster.run")

    def test_cluster_roles_have_disjoint_stage_allowlists(self):
        assert may_claim_task("linux_executor", "preflight", "preflight")
        assert may_claim_task("linux_executor", "collect_results", "collect_results")
        assert not may_claim_task("linux_executor", "run_simulation", "run_simulation")
        assert may_claim_task("platform_gateway", "run_simulation", "run_simulation")
        assert may_claim_task("platform_gateway", "cluster.run")
        assert not may_claim_task("platform_gateway", "preflight", "preflight")


class TestServerCapabilityFilter:
    def test_light_filters_wildcard_runtime_and_unknown(self):
        effective, rejected = filter_capabilities_for_node(
            "windows_agent",
            ["BUILD.SELENA", "*", "cluster.run", "future.admin"],
        )
        assert effective == ["build.selena"]
        assert rejected == ["*", "cluster.run", "future.admin"]

    def test_full_filters_cluster_runtime_but_keeps_local_sim(self):
        effective, rejected = filter_capabilities_for_node(
            "windows_full",
            ["local.run_sim", "simulation.local", "cluster.run", "cluster.gateway"],
        )
        assert effective == ["local.run_sim", "simulation.local"]
        assert rejected == ["cluster.run", "cluster.gateway"]

    def test_legacy_preserves_canonical_capabilities(self):
        assert filter_capabilities_for_node("legacy", ["LOCAL.*", "cluster.run"]) == (
            ["local.*", "cluster.run"],
            [],
        )

    def test_linux_executor_uses_strict_allowlist(self):
        effective, rejected = filter_capabilities_for_node(
            "linux_executor",
            ["cluster.prepare", "result.collect", "simulation.cluster", "*"],
        )
        assert effective == ["cluster.prepare", "result.collect"]
        assert rejected == ["simulation.cluster", "*"]

    def test_platform_gateway_uses_strict_allowlist(self):
        effective, rejected = filter_capabilities_for_node(
            "platform_gateway",
            ["simulation.cluster", "cluster.gateway", "cluster.run", "result.collect"],
        )
        assert effective == ["simulation.cluster", "cluster.gateway", "cluster.run"]
        assert rejected == ["result.collect"]

    def test_cluster_role_capabilities_are_disjoint(self):
        assert LINUX_EXECUTOR_CAPABILITIES.isdisjoint(PLATFORM_GATEWAY_CAPABILITIES)


def test_v5_build_stage_alias_requires_formal_build_capability():
    assert required_capabilities_for_task("build_selena", "build_selena") == ("build.selena",)
    assert required_capabilities_for_task("local.build_selena") == ()


def test_node_specific_stage_capability_requirements():
    assert required_capabilities_for_task("preflight", "preflight", "linux_executor") == (
        "cluster.prepare",
    )
    assert required_capabilities_for_task(
        "run_simulation", "run_simulation", "platform_gateway"
    ) == ("simulation.cluster",)
    assert required_capabilities_for_task(
        "run_simulation", "run_simulation", "windows_full"
    ) == ("simulation.local",)
