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
