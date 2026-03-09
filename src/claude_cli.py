"""Backward compatibility — use src.backends.claude instead.

Provides lazy re-exports of ClaudeCodeCLI and Claude-specific constants
so that existing ``from src.claude_cli import ClaudeCodeCLI`` continues to work.

NOTE: To monkeypatch constants for testing, patch the canonical location::

    monkeypatch.setattr("src.backends.claude.client.THINKING_MODE", "disabled")

Patching ``src.claude_cli.THINKING_MODE`` sets a module-level attr on this
shim but does NOT change what ``ClaudeCodeCLI`` reads at runtime.
"""


def __getattr__(name):
    if name == "ClaudeCodeCLI":
        from src.backends.claude.client import ClaudeCodeCLI

        return ClaudeCodeCLI

    # Re-export constants that used to live at module level in the old claude_cli.py
    _CONST_NAMES = {
        "THINKING_MODE",
        "THINKING_BUDGET_TOKENS",
        "TOKEN_STREAMING",
        "DISALLOWED_SUBAGENT_TYPES",
    }
    if name in _CONST_NAMES:
        from src.backends.claude import constants as _c

        return getattr(_c, name)

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
