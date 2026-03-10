"""Backward compatibility — use src.backends.codex instead.

Provides lazy re-exports of CodexCLI, helper functions, and Codex-specific
constants so that existing ``from src.codex_cli import CodexCLI`` continues
to work.

NOTE: To monkeypatch constants for testing, patch the canonical location::

    monkeypatch.setattr("src.backends.codex.client.CODEX_CLI_PATH", "/new/path")

Patching ``src.codex_cli.CODEX_CLI_PATH`` sets a module-level attr on this
shim but does NOT change what ``CodexCLI`` reads at runtime.
"""


def __getattr__(name):
    # Client class
    if name == "CodexCLI":
        from src.backends.codex.client import CodexCLI

        return CodexCLI

    # Public helper functions
    _FUNC_NAMES = {
        "normalize_codex_event",
        "_extract_text_and_collab",
        "_strip_collab_json",
        "_collab_to_tool_blocks",
        "_build_content_blocks",
        "_normalize_usage",
    }
    if name in _FUNC_NAMES:
        from src.backends.codex import client as _client

        return getattr(_client, name)

    # Constants that used to live at module level
    _CONST_NAMES = {
        "CODEX_CLI_PATH",
        "CODEX_CONFIG_ISOLATION",
        "CODEX_DEFAULT_MODEL",
        "CODEX_MODELS",
        "CODEX_TIMEOUT_MS",
    }
    if name in _CONST_NAMES:
        from src.backends.codex import constants as _c

        return getattr(_c, name)

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
