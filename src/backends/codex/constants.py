"""Codex backend constants and configuration.

Single source of truth for Codex-specific models and configuration.
All configurable values can be overridden via environment variables.
"""

import os

# NOTE: This module must NOT import from src.constants to avoid circular imports.
# The default timeout fallback is duplicated here intentionally.
_DEFAULT_TIMEOUT_MS = int(os.getenv("MAX_TIMEOUT", "600000"))  # Same env var as src.constants

# Codex Models
# Codex sub-models (e.g. "codex/o3") are resolved via slash pattern in resolve_model()
CODEX_MODELS = [
    "codex",
]

# Codex Backend Configuration
CODEX_DEFAULT_MODEL = os.getenv("CODEX_DEFAULT_MODEL", "gpt-5.4")
CODEX_CLI_PATH = os.getenv("CODEX_CLI_PATH", "codex")
CODEX_TIMEOUT_MS = int(os.getenv("CODEX_TIMEOUT_MS", str(_DEFAULT_TIMEOUT_MS)))
CODEX_CONFIG_ISOLATION = os.getenv("CODEX_CONFIG_ISOLATION", "false").lower() in (
    "true",
    "1",
    "yes",
)
