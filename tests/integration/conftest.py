"""Integration test fixtures for Codex backend.

These fixtures are intentionally separate from tests/conftest.py so that
existing unit tests remain completely unaffected.
"""

import os
import shutil
import sys
from pathlib import Path

import pytest

from tests.conftest import FakeCodexBackend  # noqa: F401 — re-exported for local use

# ---------------------------------------------------------------------------
# Skip markers
# ---------------------------------------------------------------------------

requires_codex_binary = pytest.mark.skipif(
    not (os.getenv("RUN_CODEX_BINARY_TESTS") and shutil.which("codex")),
    reason="Set RUN_CODEX_BINARY_TESTS=1 and install Codex CLI to run",
)

requires_openai_key = pytest.mark.skipif(
    not os.getenv("OPENAI_API_KEY"),
    reason="OPENAI_API_KEY not set",
)

# ---------------------------------------------------------------------------
# JSONL scenario constants (aligned to src/codex_cli.py normalizer)
# ---------------------------------------------------------------------------

CODEX_EVENTS_BASIC = [
    {"type": "thread.started", "thread_id": "t-test-001"},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"type": "agent_message", "text": "Hello from Codex"}},
    {"type": "turn.completed", "usage": {"input_tokens": 50, "output_tokens": 25}},
]

CODEX_EVENTS_MULTI_ITEM = [
    {"type": "thread.started", "thread_id": "t-test-002"},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"type": "reasoning", "text": "Let me think..."}},
    {
        "type": "item.started",
        "item": {"type": "command_execution", "command": "ls -la"},
    },
    {
        "type": "item.completed",
        "item": {
            "type": "command_execution",
            "command": "ls -la",
            "exit_code": 0,
            "output": "file1.py\nfile2.py",
        },
    },
    {
        "type": "item.completed",
        "item": {
            "type": "file_change",
            "changes": [{"kind": "update", "path": "main.py"}],
        },
    },
    {
        "type": "item.completed",
        "item": {"type": "agent_message", "text": "I found the files."},
    },
    {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 60}},
]

CODEX_EVENTS_ERROR = [
    {"type": "thread.started", "thread_id": "t-test-err"},
    {"type": "error", "message": "Rate limit exceeded"},
]

CODEX_EVENTS_TURN_FAILED = [
    {"type": "thread.started", "thread_id": "t-test-fail"},
    {"type": "turn.started"},
    {"type": "turn.failed", "message": "Internal error"},
]

# ---------------------------------------------------------------------------
# Mock binary fixtures
# ---------------------------------------------------------------------------

_MOCK_BINARY_PATH = Path(__file__).parent.parent / "fixtures" / "mock_codex_binary.py"


@pytest.fixture
def mock_codex_bin(tmp_path):
    """Create an executable shell wrapper that forwards args to the mock script."""
    wrapper = tmp_path / "codex"
    wrapper.write_text(f'#!/bin/sh\nexec {sys.executable} {_MOCK_BINARY_PATH} "$@"\n')
    wrapper.chmod(0o755)
    return str(wrapper)


@pytest.fixture
def integration_codex_cli(mock_codex_bin, tmp_path, monkeypatch):
    """Return a CodexCLI instance backed by the mock binary.

    Uses monkeypatch on the module-level constant so the constructor's
    ``_find_codex_binary()`` returns our mock wrapper.
    """
    monkeypatch.setattr("src.backends.codex.client.CODEX_CLI_PATH", mock_codex_bin)
    monkeypatch.setattr("src.backends.codex.client.CODEX_CONFIG_ISOLATION", True)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-integration")

    from src.backends.codex.client import CodexCLI

    return CodexCLI(timeout=5000, cwd=str(tmp_path))


# ---------------------------------------------------------------------------
# FakeCodexBackend (re-exported from tests/conftest.py for backward compat)
# ---------------------------------------------------------------------------

# FakeCodexBackend and fake_codex_backend fixture are imported from
# tests/conftest.py (see import at top of file). The fixture is available
# to all tests via conftest.py's autouse discovery.
