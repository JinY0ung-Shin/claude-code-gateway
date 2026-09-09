"""Regression coverage for the migration-only legacy workspace key.

The compatibility switch must live at the workspace resolver boundary.  If an
HTTP route applies it locally while /v1/responses keeps using the whole identity,
the file browser and agent land in different directories for the same caller —
exactly the cross-surface half of issue #188.
"""

from src.workspace_manager import WorkspaceManager


def test_default_workspace_key_keeps_the_whole_identity(tmp_path, monkeypatch):
    monkeypatch.delenv("WORKSPACE_LEGACY_LOCALPART_KEY", raising=False)
    manager = WorkspaceManager(tmp_path)

    a = manager.resolve("alice@a.com", backend="claude")
    b = manager.resolve("alice@b.com", backend="claude")

    assert a == tmp_path / "alice@a.com" / "claude"
    assert b == tmp_path / "alice@b.com" / "claude"
    assert a != b


def test_legacy_workspace_key_is_applied_by_the_shared_resolver(tmp_path, monkeypatch):
    """Every consumer gets the same migration key, not just /files/*."""
    monkeypatch.setenv("WORKSPACE_LEGACY_LOCALPART_KEY", "true")
    manager = WorkspaceManager(tmp_path)

    # /v1/responses passes the whole body.user to WorkspaceManager.resolve().
    responses_path = manager.resolve("alice@corp.example.com", backend="claude")
    # Old file-route behavior pre-truncated the same identity before resolve().
    files_path = manager.resolve("alice", backend="claude")

    assert responses_path == files_path == tmp_path / "alice" / "claude"


def test_legacy_mode_remains_explicitly_collision_prone(tmp_path, monkeypatch):
    """The migration switch is compatibility, never an isolation guarantee."""
    monkeypatch.setenv("WORKSPACE_LEGACY_LOCALPART_KEY", "true")
    manager = WorkspaceManager(tmp_path)

    assert manager.resolve("alice@a.com", backend="claude") == manager.resolve(
        "alice@b.com", backend="claude"
    )
