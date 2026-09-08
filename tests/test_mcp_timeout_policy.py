"""Regression coverage for the gateway-owned MCP timeout policy (#182)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from src import config_check, constants, mcp_config


REPO_ROOT = Path(__file__).resolve().parents[1]


def _probe_constants(env: dict[str, str]) -> list[str]:
    code = (
        "import os; import src.constants as c; "
        "print(os.environ.get('MCP_TOOL_TIMEOUT')); "
        "print(c.cli_tool_watchdog_ms()); "
        "print(c._tool_stall_timeout_default())"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip().splitlines()


def test_unset_mcp_timeout_is_injected_as_gateway_default():
    env = os.environ.copy()
    env.pop("MCP_TOOL_TIMEOUT", None)
    env.pop("BASH_MAX_TIMEOUT_MS", None)
    env.pop("TOOL_STALL_TIMEOUT", None)

    injected, watchdog, tool_stall = _probe_constants(env)

    assert injected == "600000"
    assert watchdog == "600000"
    assert tool_stall == "660"


def test_explicit_mcp_timeout_drives_child_env_and_stall_budget():
    env = os.environ.copy()
    env["MCP_TOOL_TIMEOUT"] = "1200000"
    env.pop("BASH_MAX_TIMEOUT_MS", None)
    env.pop("TOOL_STALL_TIMEOUT", None)

    injected, watchdog, tool_stall = _probe_constants(env)

    assert injected == "1200000"
    assert watchdog == "1200000"
    assert tool_stall == "1260"


def test_server_timeout_above_ceiling_is_clamped_with_warning(monkeypatch, caplog):
    monkeypatch.setenv("MCP_TOOL_TIMEOUT", "600000")
    servers = {
        "slow": {"type": "stdio", "command": "slow-server", "timeout": 1200000},
        "fast": {"type": "stdio", "command": "fast-server", "timeout": 300000},
    }

    with caplog.at_level("WARNING"):
        resolved = mcp_config.resolve_mcp_servers(servers)

    assert resolved is not None
    assert resolved["slow"]["timeout"] == 600000
    assert resolved["fast"]["timeout"] == 300000
    assert servers["slow"]["timeout"] == 1200000  # input is never mutated
    assert "slow" in caplog.text
    assert "1200000ms" in caplog.text
    assert "600000ms" in caplog.text


def test_overlay_like_server_map_uses_the_same_ceiling(monkeypatch):
    """Plugin/manifest maps are merged before resolve_mcp_servers(), so the
    same final-map clamp must apply regardless of where a server came from."""
    monkeypatch.setenv("MCP_TOOL_TIMEOUT", "450000")
    merged = {
        "plugin-slow": {
            "type": "stdio",
            "command": "plugin-server",
            "timeout": 900000,
            "env": {"TOKEN": "{{env:PLUGIN_TOKEN}}"},
        }
    }

    resolved = mcp_config.resolve_mcp_servers(merged, environ={"PLUGIN_TOKEN": "secret"})

    assert resolved is not None
    assert resolved["plugin-slow"]["timeout"] == 450000
    assert resolved["plugin-slow"]["env"]["TOKEN"] == "secret"


def test_config_check_matches_runtime_effective_watchdog(monkeypatch):
    monkeypatch.setenv("MCP_TOOL_TIMEOUT", "1200000")
    monkeypatch.delenv("BASH_MAX_TIMEOUT_MS", raising=False)
    monkeypatch.delenv("TOOL_STALL_TIMEOUT", raising=False)
    monkeypatch.setenv("ACTIVE_TURN_MAX_AGE", "1800")

    assert constants.cli_tool_watchdog_ms() == 1200000
    assert constants._tool_stall_timeout_default() == 1260
    assert config_check._check_stall_hierarchy() == []


def test_config_check_rejects_when_effective_budget_cannot_fit_under_max_age(monkeypatch):
    monkeypatch.setenv("MCP_TOOL_TIMEOUT", "1800000")
    monkeypatch.delenv("BASH_MAX_TIMEOUT_MS", raising=False)
    monkeypatch.delenv("TOOL_STALL_TIMEOUT", raising=False)
    monkeypatch.setenv("ACTIVE_TURN_MAX_AGE", "1800")

    issues = config_check._check_stall_hierarchy()

    assert constants._tool_stall_timeout_default() == 1860
    matching = [i for i in issues if "ACTIVE_TURN_MAX_AGE=1800s" in i.message]
    assert matching and matching[0].severity == "error"
    with pytest.raises(RuntimeError, match="Refusing to start"):
        config_check.run_startup_config_check()


def test_config_check_rejects_tool_stall_not_above_watchdog(monkeypatch):
    monkeypatch.setenv("MCP_TOOL_TIMEOUT", "600000")
    monkeypatch.delenv("BASH_MAX_TIMEOUT_MS", raising=False)
    monkeypatch.setenv("TOOL_STALL_TIMEOUT", "600")
    monkeypatch.setenv("ACTIVE_TURN_MAX_AGE", "1800")

    issues = config_check._check_stall_hierarchy()

    matching = [i for i in issues if "not below the in-flight tool stall budget" in i.message]
    assert matching and matching[0].severity == "error"
    with pytest.raises(RuntimeError, match="Refusing to start"):
        config_check.run_startup_config_check()
