#!/usr/bin/env python3
"""Setup script for radar-sim."""

from setuptools import setup, find_packages

setup(
    name="radar-sim",
    version="4.0.0",
    description="雷达仿真辅助与数据分析工具 — 编译辅助 + MF4分析 + AI问答",
    author="radar-sim team",
    python_requires=">=3.9",
    packages=find_packages(),
    include_package_data=True,
    package_data={"radar_sim_web": ["static/*.html", "static/*.css", "static/*.js"]},
    entry_points={
        "console_scripts": [
            "rsim=rsim:main",
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
        # v5 /api/v1 server stack. Kept out of install_requires so legacy
        # Python 3.9/control-plane imports remain PyYAML-only; these packages
        # require Python 3.10+ and pip will fail clearly on unsupported Python.
        "v5-server": [
            "fastapi==0.139.0",
            "uvicorn==0.50.2",
            "pydantic==2.13.4",
        ],
        # Official Python SDK transport stack.
        "sdk": [
            "httpx==0.28.1",
            "pydantic==2.13.4",
        ],
        "v5": [
            "fastapi==0.139.0",
            "uvicorn==0.50.2",
            "httpx==0.28.1",
            "pydantic==2.13.4",
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
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
    ],
)
