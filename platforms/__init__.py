"""
Platform registry.

Platforms register themselves during import.
Usage:
    from platforms import get, list_all
    p = get("gen5_selena")
    print(list_all())
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.platform import PlatformBackend


_registry: dict[str, type] = {}


def register(cls: type) -> None:
    """Register a platform backend class.

    The class is stored without instantiation — config is passed
    when the user calls get(name, config).
    """
    # Create a temporary instance just to read platform_name,
    # using an empty dict as config (safe for name-only access).
    temp = cls({})
    _registry[temp.platform_name] = cls


def get(name: str, config: dict | None = None) -> PlatformBackend:
    """Get a platform instance by name, passing config."""
    if name not in _registry:
        available = ", ".join(sorted(_registry.keys()))
        raise ValueError(f"Platform '{name}' not registered. Available: {available}")
    return _registry[name](config or {})


def list_all() -> list[str]:
    """Return sorted list of registered platform names."""
    return sorted(_registry.keys())


# Auto-import known platforms to trigger @register decorators
try:
    from . import gen5_selena  # noqa: F401
except ImportError:
    pass
