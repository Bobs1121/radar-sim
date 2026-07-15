"""Small, dependency-free HTTP bearer-token authentication core.

The HTTP adapters are deliberately kept outside this module.  They should pass
the complete ``Authorization`` header to :meth:`authenticate_user` or
:meth:`authenticate_agent` and use the returned principal as the sole source of
identity.  In particular, caller-controlled identity headers must not override
the principal.

Raw tokens are validated while loading and then discarded.  The live
authenticator retains only SHA-256 digests, and bearer matching uses
``hmac.compare_digest`` for every credential in the requested role.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


class HttpAuthError(ValueError):
    """Stable authentication or credential-configuration failure."""


_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,512}$")
_BEARER_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{1,512}$")
_MAX_CONFIG_BYTES = 1024 * 1024
_VERSION = 1


@dataclass(frozen=True)
class AuthPrincipal:
    """Authenticated identity safe to pass to services and public responses."""

    role: str
    owner: str
    agent_id: str | None = None

    def __post_init__(self) -> None:
        if self.role not in {"user", "agent"}:
            raise HttpAuthError("authentication principal role is invalid")
        _validate_identity(self.owner, "owner")
        if self.role == "user" and self.agent_id is not None:
            raise HttpAuthError("user principal cannot contain an agent id")
        if self.role == "agent":
            _validate_identity(self.agent_id, "agent id")

    @property
    def public_dict(self) -> dict[str, str]:
        result = {"role": self.role, "owner": self.owner}
        if self.agent_id is not None:
            result["agent_id"] = self.agent_id
        return result


@dataclass(frozen=True, repr=False)
class _Credential:
    principal: AuthPrincipal
    token_digest: bytes


class HttpTokenAuthenticator:
    """Immutable token verifier loaded from a versioned JSON mapping."""

    def __init__(self, credentials: Iterable[_Credential]) -> None:
        self._credentials = tuple(credentials)
        if not self._credentials:
            raise HttpAuthError("authentication configuration has no credentials")

    def __repr__(self) -> str:
        user_count = sum(item.principal.role == "user" for item in self._credentials)
        agent_count = len(self._credentials) - user_count
        return f"HttpTokenAuthenticator(users={user_count}, agents={agent_count})"

    @classmethod
    def from_mapping(cls, document: Mapping[str, Any]) -> "HttpTokenAuthenticator":
        if not isinstance(document, Mapping):
            raise HttpAuthError("authentication configuration must be an object")
        if set(document) != {"version", "users", "agents"}:
            raise HttpAuthError("authentication configuration fields are invalid")
        if document.get("version") != _VERSION:
            raise HttpAuthError("authentication configuration version is unsupported")
        users = document.get("users")
        agents = document.get("agents")
        if not isinstance(users, Mapping) or not isinstance(agents, Mapping):
            raise HttpAuthError("authentication users and agents must be objects")

        credentials: list[_Credential] = []
        token_digests: set[bytes] = set()
        owner_names: set[str] = set()
        for raw_owner, raw_token in users.items():
            owner = _validate_identity(raw_owner, "user")
            token = _validate_token(raw_token)
            digest = _token_digest(token)
            _claim_unique_digest(digest, token_digests)
            owner_names.add(owner)
            credentials.append(_Credential(AuthPrincipal("user", owner), digest))

        for raw_agent_id, raw_entry in agents.items():
            agent_id = _validate_identity(raw_agent_id, "agent id")
            if not isinstance(raw_entry, Mapping) or set(raw_entry) != {"owner", "token"}:
                raise HttpAuthError("agent credential fields are invalid")
            owner = _validate_identity(raw_entry.get("owner"), "agent owner")
            if owner not in owner_names:
                raise HttpAuthError("agent owner is not a configured user")
            token = _validate_token(raw_entry.get("token"))
            digest = _token_digest(token)
            _claim_unique_digest(digest, token_digests)
            credentials.append(_Credential(AuthPrincipal("agent", owner, agent_id), digest))

        return cls(credentials)

    @classmethod
    def from_file(cls, path: str | Path) -> "HttpTokenAuthenticator":
        source = Path(path).expanduser()
        try:
            if source.is_symlink() or not source.is_file():
                raise HttpAuthError("authentication configuration file is unavailable")
            if source.stat().st_size > _MAX_CONFIG_BYTES:
                raise HttpAuthError("authentication configuration file is too large")
            raw = source.read_text(encoding="utf-8")
            document = json.loads(raw, object_pairs_hook=_reject_duplicate_json_keys)
        except HttpAuthError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise HttpAuthError("authentication configuration file is invalid") from exc
        return cls.from_mapping(document)

    def authenticate_user(self, authorization: str | None) -> AuthPrincipal:
        """Authenticate a user Bearer header and return its configured owner."""

        return self._authenticate(authorization, role="user")

    def authenticate_agent(self, authorization: str | None) -> AuthPrincipal:
        """Authenticate an agent Bearer header and return owner plus agent id."""

        return self._authenticate(authorization, role="agent")

    def _authenticate(self, authorization: str | None, *, role: str) -> AuthPrincipal:
        token = _parse_bearer(authorization)
        candidate = _token_digest(token)
        matched: AuthPrincipal | None = None
        # Do not stop at the first match.  Duplicate credentials are rejected at
        # load time, and every credential for the role receives a constant-time
        # digest comparison.
        for credential in self._credentials:
            if credential.principal.role != role:
                continue
            if hmac.compare_digest(candidate, credential.token_digest):
                matched = credential.principal
        if matched is None:
            raise HttpAuthError("authentication failed")
        return matched


def load_http_auth(path: str | Path) -> HttpTokenAuthenticator:
    """Load and validate an authentication configuration file."""

    return HttpTokenAuthenticator.from_file(path)


def generate_secure_token(*, nbytes: int = 32) -> str:
    """Generate a URL-safe bearer token with at least 256 bits of randomness."""

    if not isinstance(nbytes, int) or isinstance(nbytes, bool) or nbytes < 32:
        raise HttpAuthError("secure token size must be at least 32 bytes")
    return secrets.token_urlsafe(nbytes)


def create_http_auth_config(
    path: str | Path,
    *,
    users: Iterable[str],
    agents: Mapping[str, str] | None = None,
    overwrite: bool = False,
) -> Path:
    """Create a validated JSON credential file and restrict its permissions.

    ``users`` contains owner names.  ``agents`` maps agent ids to one of those
    owners.  All tokens are generated internally.  The helper returns only the
    file path so credentials are not accidentally included in logs or ordinary
    result objects.
    """

    if isinstance(users, (str, bytes)):
        raise HttpAuthError("users must be a collection of identities")
    user_names = [_validate_identity(value, "user") for value in users]
    if not user_names or len(set(user_names)) != len(user_names):
        raise HttpAuthError("users must be non-empty and unique")
    if agents is not None and not isinstance(agents, Mapping):
        raise HttpAuthError("agents must map agent ids to owners")
    agent_owners = dict(agents or {})
    document: dict[str, Any] = {
        "version": _VERSION,
        "users": {owner: generate_secure_token() for owner in user_names},
        "agents": {},
    }
    for raw_agent_id, raw_owner in agent_owners.items():
        agent_id = _validate_identity(raw_agent_id, "agent id")
        owner = _validate_identity(raw_owner, "agent owner")
        document["agents"][agent_id] = {"owner": owner, "token": generate_secure_token()}
    # Validate before touching the destination.  This also catches unknown
    # owners and the astronomically unlikely duplicate generated token.
    HttpTokenAuthenticator.from_mapping(document)

    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not overwrite:
        raise HttpAuthError("authentication configuration file already exists")
    temporary_path: Path | None = None
    try:
        fd, raw_temporary = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=str(destination.parent)
        )
        temporary_path = Path(raw_temporary)
        try:
            os.chmod(temporary_path, 0o600)
            handle = os.fdopen(fd, "w", encoding="utf-8", newline="\n")
            fd = -1
            with handle:
                json.dump(document, handle, ensure_ascii=True, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            if fd >= 0:
                os.close(fd)
        if overwrite:
            os.replace(temporary_path, destination)
        else:
            try:
                # A same-directory hard link publishes the complete temporary
                # file atomically and, unlike replace(), cannot overwrite a
                # destination created after the earlier existence check.
                os.link(temporary_path, destination)
            except FileExistsError as exc:
                raise HttpAuthError("authentication configuration file already exists") from exc
            temporary_path.unlink()
        temporary_path = None
        _restrict_file_permissions(destination)
    except HttpAuthError:
        raise
    except OSError as exc:
        raise HttpAuthError("authentication configuration file could not be created") from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return destination.resolve()


def _parse_bearer(authorization: str | None) -> str:
    if not isinstance(authorization, str):
        raise HttpAuthError("authentication failed")
    parts = authorization.strip().split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HttpAuthError("authentication failed")
    # A configured credential must meet the stronger policy in _validate_token.
    # Request input only needs to be safely hashable: even a short, incorrect
    # candidate proceeds through the same digest comparison loop.
    if not _BEARER_TOKEN_RE.fullmatch(parts[1]):
        raise HttpAuthError("authentication failed")
    return parts[1]


def _validate_identity(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _IDENTITY_RE.fullmatch(value):
        raise HttpAuthError(f"{label} is invalid")
    return value


def _validate_token(value: Any) -> str:
    if not isinstance(value, str) or not _TOKEN_RE.fullmatch(value):
        raise HttpAuthError("credential token is empty or weak")
    # Length alone catches most accidental placeholders; this additionally
    # rejects repeated-character and tiny-alphabet values such as ``abcd`` x 8.
    if len(set(value)) < 8:
        raise HttpAuthError("credential token is empty or weak")
    return value


def _token_digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("ascii")).digest()


def _claim_unique_digest(digest: bytes, claimed: set[bytes]) -> None:
    if digest in claimed:
        raise HttpAuthError("credential tokens must be unique")
    claimed.add(digest)


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise HttpAuthError("authentication configuration contains duplicate fields")
        result[key] = value
    return result


def _restrict_file_permissions(path: Path) -> None:
    """Best-effort cross-platform private-file mode (strict on POSIX)."""

    try:
        os.chmod(path, 0o600)
    except OSError as exc:
        if os.name != "nt":
            raise HttpAuthError("authentication configuration permissions could not be restricted") from exc
