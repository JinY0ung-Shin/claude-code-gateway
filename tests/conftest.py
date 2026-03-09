"""Shared fixtures for tests that mutate module-level application state."""

import asyncio
import json
from typing import Any, AsyncGenerator, Dict, List, Optional

import pytest
from unittest.mock import AsyncMock, MagicMock

import src.main as main
import src.session_manager as session_manager_module
from src.backend_registry import BackendRegistry
from src.session_manager import SessionManager


def _cleanup_manager(manager):
    """Cancel cleanup task and clear sessions for a session manager instance."""
    cleanup_task = getattr(manager, "_cleanup_task", None)
    if cleanup_task is not None:
        cleanup_task.cancel()
        manager._cleanup_task = None

    with manager.lock:
        manager.sessions.clear()


def register_all_descriptors():
    """Register all backend descriptors (model metadata) so resolve_model() works.

    Called automatically by ``reset_main_state`` and available for tests that
    need descriptors before registering fake backends.
    """
    from src.backends.claude import CLAUDE_DESCRIPTOR
    from src.backends.codex import CODEX_DESCRIPTOR

    BackendRegistry.register_descriptor(CLAUDE_DESCRIPTOR)
    BackendRegistry.register_descriptor(CODEX_DESCRIPTOR)


@pytest.fixture(autouse=True)
def reset_main_state():
    """Restore mutable module state and clean shared session state between tests."""
    original_debug = main.DEBUG_MODE
    original_runtime_api_key = main.runtime_api_key
    original_max_request_size = main.MAX_REQUEST_SIZE

    # Register descriptors so resolve_model() works even after clear
    register_all_descriptors()

    yield

    main.DEBUG_MODE = original_debug
    main.runtime_api_key = original_runtime_api_key
    main.MAX_REQUEST_SIZE = original_max_request_size
    BackendRegistry.clear()

    seen_managers = set()
    for manager in (main.session_manager, session_manager_module.session_manager):
        if id(manager) in seen_managers:
            continue
        seen_managers.add(id(manager))
        _cleanup_manager(manager)


@pytest.fixture
def isolated_session_manager(monkeypatch):
    """Patch both modules to use a fresh SessionManager for a test."""
    manager = SessionManager(default_ttl_minutes=60, cleanup_interval_minutes=5)
    monkeypatch.setattr(main, "session_manager", manager)
    monkeypatch.setattr(session_manager_module, "session_manager", manager)

    try:
        yield manager
    finally:
        _cleanup_manager(manager)


# ============================================================================
# Codex subprocess mock fixture
# ============================================================================


def _make_codex_stdout_lines(events: list[dict] | None = None, preamble: str = ""):
    """Build a list of stdout byte-lines for a mocked Codex subprocess.

    Args:
        events: JSONL event dicts to emit. Defaults to a simple assistant response.
        preamble: Optional non-JSON text that codex-cli emits before JSONL.
    """
    if events is None:
        events = [
            {"type": "message.delta", "delta": {"content": "Hello from Codex"}},
            {"type": "message.completed"},
        ]

    lines: list[bytes] = []
    if preamble:
        lines.append((preamble + "\n").encode())
    for ev in events:
        lines.append((json.dumps(ev) + "\n").encode())
    return lines


@pytest.fixture
def mock_codex_subprocess(monkeypatch):
    """Mock asyncio.create_subprocess_exec for Codex CLI calls.

    Returns a factory that lets tests customise the stdout output.
    The mock process is pre-configured with a default assistant response.
    """

    class _Factory:
        def __init__(self):
            self.proc = MagicMock()
            self.proc.returncode = 0
            self.proc.pid = 12345
            self._stdout_lines = _make_codex_stdout_lines()
            self._apply_stdout()
            self.proc.stderr = AsyncMock()
            self.proc.stderr.readline = AsyncMock(return_value=b"")
            self.proc.wait = AsyncMock(return_value=0)
            self.captured_env = None

        def set_stdout(self, events: list[dict] | None = None, preamble: str = ""):
            """Override the stdout lines that the mock process will emit."""
            self._stdout_lines = _make_codex_stdout_lines(events, preamble)
            self._apply_stdout()

        def _apply_stdout(self):
            line_iter = iter(self._stdout_lines)

            async def readline():
                try:
                    return next(line_iter)
                except StopIteration:
                    return b""

            self.proc.stdout = MagicMock()
            self.proc.stdout.readline = readline

    factory = _Factory()

    async def fake_create_subprocess_exec(*args, **kwargs):
        factory.captured_env = kwargs.get("env")
        return factory.proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    return factory


# ============================================================================
# FakeCodexBackend — shared across unit and integration tests
# ============================================================================


class FakeCodexBackend:
    """Minimal BackendClient Protocol implementation for tests.

    Yields a deterministic sequence of normalized chunks so callers can assert
    on backend dispatch, session continuity, and token-usage mapping without
    a real Codex binary.
    """

    def __init__(self, thread_id: str = "fake-thread-123"):
        self.thread_id = thread_id
        self.calls: List[Dict[str, Any]] = []

    async def run_completion(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        stream: bool = True,
        max_turns: int = 10,
        allowed_tools: Optional[List[str]] = None,
        disallowed_tools: Optional[List[str]] = None,
        session_id: Optional[str] = None,
        resume: Optional[str] = None,
        permission_mode: Optional[str] = None,
        output_format: Optional[Dict[str, Any]] = None,
        mcp_servers: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        self.calls.append(
            {"prompt": prompt, "resume": resume, "model": model, "session_id": session_id}
        )
        yield {"type": "codex_session", "session_id": self.thread_id}
        yield {"type": "assistant", "content": [{"type": "text", "text": "codex reply"}]}
        yield {
            "type": "result",
            "subtype": "success",
            "result": "codex reply",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        }

    def build_options(self, request, resolved, overrides=None):
        """Minimal build_options for tests."""
        options = request.to_claude_options() if hasattr(request, "to_claude_options") else {}
        if overrides:
            options.update(overrides)
        if resolved.provider_model:
            options["model"] = resolved.provider_model
        options["permission_mode"] = "bypassPermissions"
        return options

    def parse_message(self, messages: List[Dict[str, Any]]) -> Optional[str]:
        return "codex reply"

    def estimate_token_usage(
        self, prompt: str, completion: str, model: Optional[str] = None
    ) -> Dict[str, int]:
        return {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

    async def verify(self) -> bool:
        return True


@pytest.fixture
def fake_codex_backend():
    """Register a FakeCodexBackend and clean up after the test."""
    from src.backends.codex import CODEX_DESCRIPTOR

    backend = FakeCodexBackend()
    BackendRegistry.register_descriptor(CODEX_DESCRIPTOR)
    BackendRegistry.register("codex", backend)
    yield backend
    BackendRegistry.unregister("codex")
