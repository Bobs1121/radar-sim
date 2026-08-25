#!/usr/bin/env python3
"""Setup script for radar-sim."""

from setuptools import setup, find_packages

setup(
    name="radar-sim",
    version="4.0.0",
    description="雷达仿真辅助与数据分析工具 — 编译辅助 + MF4分析 + AI问答",
    author="radar-sim team",
    # The public SDK/MCP code uses Python 3.10 type syntax and the MCP
    # dependency is not released for the legacy 3.9 runtime. Keep package
    # metadata aligned with the Agent Tools bundle.
    python_requires=">=3.10",
    packages=find_packages(),
    include_package_data=True,
    package_data={"radar_sim_web": ["static/*.html", "static/*.css", "static/*.js"]},
    entry_points={
        "console_scripts": [
            "rsim=rsim:main",
            "radar-sim-mcp=radar_sim_mcp.server:main",
        ],
    },
    # Only PyYAML is needed by the core config layer; heavy deps (asammdf for
    # MF4 parsing, openai for AI Q&A) are optional so the control-plane
    # server/agent can install with just ``pip install .[control]`` on a Linux
    # box without pulling C-extension wheels.
    install_requires=[
        "PyYAML>=6.0",
    ],
    extras_require={
        # Control-plane server/agent: PyYAML only (already in install_requires).
        # Listed for clarity / future light deps.
        "control": [],
        # v5 SimulationSpec schema/model spike. Keep out of install_requires so
        # legacy control-plane installs remain PyYAML-only until WP1 is complete.
        "v5-spec": [
            "pydantic==2.13.4",
        ],
        # v5 /api/v1 server stack. Kept out of install_requires so the base
        # SDK install remains PyYAML-only; these packages require Python 3.10+.
        "v5-server": [
            "fastapi==0.139.0",
            "uvicorn==0.50.2",
            "pydantic==2.13.4",
            # UserRunConfig V2 derives RadarFL/FR/RL/RR from MF4 acquisition
            # metadata on the Linux control plane before Cluster submission.
            "asammdf>=6.0",
        ],
        # Official Python SDK transport stack.
        "sdk": [
            "httpx==0.28.1",
            "pydantic==2.13.4",
        ],
        # Agent-facing MCP adapter. The SDK remains usable without MCP.
        "mcp": [
            "httpx==0.28.1",
            "pydantic==2.13.4",
            "mcp>=1.28,<2",
        ],
        "v5": [
            "fastapi==0.139.0",
            "uvicorn==0.50.2",
            "httpx==0.28.1",
            "pydantic==2.13.4",
            "asammdf>=6.0",
        ],
        # Full local-execution stack: MF4 analysis + AI Q&A + config.
        "full": [
            "asammdf>=6.0",
            "openai>=1.0",
        ],
        "dev": [
            "pytest>=7.0",
            "pytest-cov",
        ],
    },
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
    ],
)
