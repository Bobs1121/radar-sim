"""Tests for core/user.py per-user isolation."""

import os
from pathlib import Path

import pytest

import core.user as user_mod
from core.user import control_db_path_for_user, current_user, normalize_user


def test_current_user_respects_rsim_user(monkeypatch):
    monkeypatch.setenv("RSIM_USER", "alice")
    assert current_user() == "alice"


def test_current_user_sanitizes_unsafe_chars(monkeypatch):
    monkeypatch.setenv("RSIM_USER", r"user/with\bad:chars")
    u = current_user()
    assert "/" not in u and "\\" not in u and ":" not in u
    assert u  # non-empty
    assert u == normalize_user(r"user/with\bad:chars")


def test_current_user_falls_back_to_default(monkeypatch):
    monkeypatch.delenv("RSIM_USER", raising=False)
    monkeypatch.setattr("core.user.getpass.getuser", lambda: "")
    assert current_user() == "default"


def test_control_db_path_per_user(monkeypatch, tmp_path):
    monkeypatch.setenv("RSIM_HOME", str(tmp_path))
    p_alice = control_db_path_for_user("alice")
    p_bob = control_db_path_for_user("bob")
    assert p_alice.name == "_control_alice.db"
    assert p_bob.name == "_control_bob.db"
    assert p_alice != p_bob
    assert str(p_alice).startswith(str(tmp_path))


def test_control_db_path_default_user(monkeypatch, tmp_path):
    monkeypatch.setenv("RSIM_HOME", str(tmp_path))
    p = control_db_path_for_user("default")
    assert p.name == "_control.db"  # no suffix for default


def test_control_db_path_uses_current_user(monkeypatch, tmp_path):
    monkeypatch.setenv("RSIM_HOME", str(tmp_path))
    monkeypatch.setenv("RSIM_USER", "carol")
    p = control_db_path_for_user(None)
    assert p.name == "_control_carol.db"


@pytest.mark.parametrize("raw", ["../../../escape", r"..\..\escape", "a/b", ".", "..", "", "safe.user-1"])
def test_control_db_path_never_escapes_results(monkeypatch, tmp_path, raw):
    monkeypatch.setenv("RSIM_HOME", str(tmp_path))
    path = control_db_path_for_user(raw).resolve()
    results = (tmp_path / "results").resolve()
    assert path.parent == results
    assert path.name.endswith(".db")
    assert ".." not in path.name


def test_normalize_user_dangerous_values_fallback_to_default():
    assert normalize_user("") == "default"
    assert normalize_user(".") == "default"
    assert normalize_user("..") == "default"
