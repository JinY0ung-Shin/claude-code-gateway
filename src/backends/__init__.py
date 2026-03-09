"""Backend subpackage — multi-backend discovery, registration, and model resolution.

Re-exports core types and provides ``discover_backends()`` and ``resolve_model()``
as the primary entry points for ``main.py``.
"""

from __future__ import annotations

import logging

from src.backends.base import (  # noqa: F401
    BackendClient,
    BackendConfigError,
    BackendDescriptor,
    BackendRegistry,
    ResolvedModel,
)

logger = logging.getLogger(__name__)


def discover_backends(registry_cls=None) -> None:
    """Discover and register all known backends.

    Always registers Claude.  Registers Codex with try/except for graceful
    failure (binary may not be installed).
    """
    if registry_cls is None:
        registry_cls = BackendRegistry

    # Claude — always registered
    from src.backends.claude import register as register_claude

    register_claude(registry_cls=registry_cls)

    # Codex — graceful failure
    try:
        from src.backends.codex import register as register_codex

        register_codex(registry_cls=registry_cls)
    except Exception as e:
        # Descriptor may already be registered by register_codex before the
        # client creation failed — that's intentional so model resolution
        # can report "known but not available".
        logger.warning("Codex backend not available: %s", e)


def resolve_model(model: str) -> ResolvedModel:
    """Parse a model string into backend + provider model.

    Queries registered descriptors first (so resolution works even if a
    backend failed to start), then falls back to Claude as the default backend.

    Resolution rules:
    - ``codex``         -> backend="codex", provider_model=<CODEX_DEFAULT_MODEL>
    - ``codex/o3``      -> backend="codex", provider_model="o3"
    - ``codex/o4-mini`` -> backend="codex", provider_model="o4-mini"
    - ``sonnet``        -> backend="claude", provider_model="sonnet"
    - ``opus``          -> backend="claude", provider_model="opus"
    - ``claude/opus``   -> backend="claude", provider_model="opus"
    """
    # Try each registered descriptor's resolve function
    for desc in BackendRegistry.all_descriptors().values():
        resolved = desc.resolve_fn(model)
        if resolved is not None:
            return resolved

    # Default: Claude backend (handles unknown models gracefully)
    return ResolvedModel(public_model=model, backend="claude", provider_model=model)
