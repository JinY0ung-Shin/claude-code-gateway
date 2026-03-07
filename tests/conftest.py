"""Shared fixtures for tests that mutate module-level application state."""

import pytest

import src.main as main
import src.session_manager as session_manager_module
from src.session_manager import SessionManager


def _cleanup_manager(manager):
    """Cancel cleanup task and clear sessions for a session manager instance."""
    cleanup_task = getattr(manager, "_cleanup_task", None)
    if cleanup_task is not None:
        cleanup_task.cancel()
        manager._cleanup_task = None

    with manager.lock:
        manager.sessions.clear()


@pytest.fixture(autouse=True)
def reset_main_state():
    """Restore mutable module state and clean shared session state between tests."""
    original_debug = main.DEBUG_MODE
    original_runtime_api_key = main.runtime_api_key
    original_max_request_size = main.MAX_REQUEST_SIZE

    yield

    main.DEBUG_MODE = original_debug
    main.runtime_api_key = original_runtime_api_key
    main.MAX_REQUEST_SIZE = original_max_request_size

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
