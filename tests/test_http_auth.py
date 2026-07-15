import json
import os
from pathlib import Path

import pytest

from core.http_auth import (
    HttpAuthError,
    HttpTokenAuthenticator,
    create_http_auth_config,
    generate_secure_token,
    load_http_auth,
)


def _document():
    return {
        "version": 1,
        "users": {
            "alice": "Alice_user_0123456789_abcdefghijklmnop",
            "bob": "Bob_user_0123456789_qrstuvwxyzABCDEFG",
        },
        "agents": {
            "win-a": {
                "owner": "alice",
                "token": "Agent_A_0123456789_abcdefghijklmnop",
            }
        },
    }


def test_user_and_agent_identity_are_derived_only_from_bearer_token():
    auth = HttpTokenAuthenticator.from_mapping(_document())

    user = auth.authenticate_user("Bearer Alice_user_0123456789_abcdefghijklmnop")
    agent = auth.authenticate_agent("bearer Agent_A_0123456789_abcdefghijklmnop")

    assert user.public_dict == {"role": "user", "owner": "alice"}
    assert agent.public_dict == {"role": "agent", "owner": "alice", "agent_id": "win-a"}
    assert "Alice_user_0123456789_abcdefghijklmnop" not in repr(auth)
    assert "Agent_A_0123456789_abcdefghijklmnop" not in repr(auth)
    assert "token" not in user.public_dict
    assert "token" not in agent.public_dict


@pytest.mark.parametrize(
    "authorization",
    [None, "", "Basic abc", "Bearer", "Bearer wrong_but_long_0123456789_ABCDEFGH", "Bearer a b"],
)
def test_invalid_bearer_headers_fail_without_echoing_secret(authorization):
    auth = HttpTokenAuthenticator.from_mapping(_document())
    with pytest.raises(HttpAuthError) as caught:
        auth.authenticate_user(authorization)
    assert "wrong_but_long" not in str(caught.value)


def test_role_tokens_cannot_cross_authenticate():
    auth = HttpTokenAuthenticator.from_mapping(_document())
    with pytest.raises(HttpAuthError, match="authentication failed"):
        auth.authenticate_user("Bearer Agent_A_0123456789_abcdefghijklmnop")
    with pytest.raises(HttpAuthError, match="authentication failed"):
        auth.authenticate_agent("Bearer Alice_user_0123456789_abcdefghijklmnop")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["users"].update({"empty": ""}),
        lambda d: d["users"].update({"short": "short"}),
        lambda d: d["users"].update({"weak": "a" * 64}),
        lambda d: d["agents"].update(
            {"duplicate": {"owner": "alice", "token": d["users"]["alice"]}}
        ),
        lambda d: d["agents"].update(
            {"orphan": {"owner": "nobody", "token": "Orphan_0123456789_abcdefghijklmnopqrst"}}
        ),
    ],
)
def test_rejects_empty_weak_duplicate_and_orphan_credentials(mutate):
    document = _document()
    mutate(document)
    with pytest.raises(HttpAuthError):
        HttpTokenAuthenticator.from_mapping(document)


def test_loads_json_file_and_rejects_malformed_or_symlink(tmp_path: Path):
    source = tmp_path / "auth.json"
    source.write_text(json.dumps(_document()), encoding="utf-8")
    assert load_http_auth(source).authenticate_user(
        "Bearer Bob_user_0123456789_qrstuvwxyzABCDEFG"
    ).owner == "bob"

    malformed = tmp_path / "malformed.json"
    malformed.write_text("{secret", encoding="utf-8")
    with pytest.raises(HttpAuthError, match="file is invalid") as caught:
        load_http_auth(malformed)
    assert "secret" not in str(caught.value)

    if hasattr(os, "symlink"):
        link = tmp_path / "link.json"
        try:
            link.symlink_to(source)
        except OSError:
            pass
        else:
            with pytest.raises(HttpAuthError, match="unavailable"):
                load_http_auth(link)

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        '{"version":1,"users":{"alice":"Alice_user_0123456789_abcdefghijklmnop",'
        '"alice":"Alice_user_9876543210_ponmlkjihgfedcba"},"agents":{}}',
        encoding="utf-8",
    )
    with pytest.raises(HttpAuthError, match="duplicate"):
        load_http_auth(duplicate)


def test_create_helper_generates_valid_private_file_without_returning_secrets(tmp_path: Path):
    destination = tmp_path / "secrets" / "http-auth.json"
    returned = create_http_auth_config(
        destination, users=["alice", "bob"], agents={"win-a": "alice"}
    )

    assert returned == destination.resolve()
    document = json.loads(destination.read_text(encoding="utf-8"))
    assert len(document["users"]["alice"]) >= 43
    assert document["users"]["alice"] != document["agents"]["win-a"]["token"]
    auth = load_http_auth(destination)
    assert auth.authenticate_agent(
        "Bearer " + document["agents"]["win-a"]["token"]
    ).public_dict == {"role": "agent", "owner": "alice", "agent_id": "win-a"}
    if os.name != "nt":
        assert destination.stat().st_mode & 0o077 == 0
    with pytest.raises(HttpAuthError, match="already exists"):
        create_http_auth_config(destination, users=["alice"])


def test_generated_tokens_are_secure_and_minimum_size_is_enforced():
    first = generate_secure_token()
    second = generate_secure_token()
    assert first != second
    assert len(first) >= 43
    with pytest.raises(HttpAuthError, match="at least 32 bytes"):
        generate_secure_token(nbytes=16)
