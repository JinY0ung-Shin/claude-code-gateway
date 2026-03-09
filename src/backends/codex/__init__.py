"""Codex backend subpackage.

Re-exports the Codex backend client, auth provider, and registration helpers.

NOTE: Heavy imports (CodexCLI, CodexAuthProvider) are lazy to avoid circular
imports.  See ``src.backends.claude.__init__`` for the rationale.
"""

import logging
from typing import Optional

from src.backends.codex.constants import CODEX_MODELS, CODEX_DEFAULT_MODEL  # noqa: F401
from src.backends.base import BackendDescriptor, BackendRegistry, ResolvedModel

logger = logging.getLogger(__name__)


def _codex_resolve(model: str) -> Optional[ResolvedModel]:
    """Resolve function for the Codex descriptor."""
    if "/" in model:
        prefix, sub_model = model.split("/", 1)
        if prefix in ("codex", *CODEX_MODELS):
            return ResolvedModel(
                public_model=model,
                backend="codex",
                provider_model=sub_model or None,
            )
        return None
    if model in CODEX_MODELS:
        return ResolvedModel(
            public_model=model,
            backend="codex",
            provider_model=CODEX_DEFAULT_MODEL,
        )
    return None


CODEX_DESCRIPTOR = BackendDescriptor(
    name="codex",
    owned_by="openai",
    models=list(CODEX_MODELS),
    resolve_fn=_codex_resolve,
)


# Lazy re-exports — deferred to avoid circular imports at module load time.
def __getattr__(name):
    if name == "CodexCLI":
        from src.backends.codex.client import CodexCLI

        return CodexCLI
    if name == "CodexAuthProvider":
        from src.backends.codex.auth import CodexAuthProvider

        return CodexAuthProvider
    if name == "normalize_codex_event":
        from src.backends.codex.client import normalize_codex_event

        return normalize_codex_event
    if name in (
        "_extract_text_and_collab",
        "_strip_collab_json",
        "_collab_to_tool_blocks",
        "_build_content_blocks",
        "_normalize_usage",
    ):
        from src.backends.codex import client as _client

        return getattr(_client, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def register(registry_cls=None, cwd: Optional[str] = None, timeout: Optional[int] = None) -> None:
    """Register Codex descriptor and client into the BackendRegistry.

    Always registers the descriptor (static metadata).
    Attempts to create a CodexCLI instance; fails gracefully if binary missing.
    """
    import os
    from src.backends.codex.client import CodexCLI

    if registry_cls is None:
        registry_cls = BackendRegistry

    # Always register descriptor
    registry_cls.register_descriptor(CODEX_DESCRIPTOR)

    # Create and register client (graceful failure)
    try:
        cli = CodexCLI(
            timeout=timeout,
            cwd=cwd or os.getenv("CLAUDE_CWD"),
        )
        registry_cls.register("codex", cli)
        logger.info("Registered backend: codex")
    except Exception as e:
        logger.warning("Codex backend registration failed: %s", e)
        logger.warning("Codex models will not be available. Check CODEX_CLI_PATH and installation.")
