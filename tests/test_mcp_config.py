#!/usr/bin/env python3
"""
Unit tests for src/mcp_config.py
"""

import json
import pytest
from unittest.mock import patch
from src.mcp_config import load_mcp_config

class TestLoadMcpConfig:
    """Test load_mcp_config() with various inputs."""

    def test_empty_config_env_returns_empty(self):
        with patch("src.mcp_config.MCP_CONFIG", ""):
            assert load_mcp_config() == {}

    def test_malformed_json_returns_empty(self):
        with patch("src.mcp_config.MCP_CONFIG", "{ malformed json }"):
            assert load_mcp_config() == {}

    def test_non_existent_file_as_json_fails_and_returns_empty(self):
        # When not a file, it's parsed as JSON string
        with patch("src.mcp_config.MCP_CONFIG", "/nonexistent/path/config.json"):
            assert load_mcp_config() == {}

    def test_valid_inline_json(self):
        config = {"mcpServers": {"test": {"type": "stdio", "command": "echo"}}}
        with patch("src.mcp_config.MCP_CONFIG", json.dumps(config)):
            result = load_mcp_config()
            assert "test" in result
            assert result["test"]["command"] == "echo"

    def test_valid_json_file(self, tmp_path):
        config = {"mcpServers": {"file-server": {"type": "stdio", "command": "ls"}}}
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps(config))

        with patch("src.mcp_config.MCP_CONFIG", str(config_file)):
            result = load_mcp_config()
            assert "file-server" in result
            assert result["file-server"]["command"] == "ls"

    def test_malformed_json_file_returns_empty(self, tmp_path):
        config_file = tmp_path / "bad.json"
        config_file.write_text("{ invalid file content }")

        with patch("src.mcp_config.MCP_CONFIG", str(config_file)):
            assert load_mcp_config() == {}

    def test_unsupported_server_type_is_skipped(self):
        config = {
            "mcpServers": {
                "valid": {"type": "stdio", "command": "ls"},
                "invalid": {"type": "grpc", "command": "foo"}
            }
        }
        with patch("src.mcp_config.MCP_CONFIG", json.dumps(config)):
            result = load_mcp_config()
            assert "valid" in result
            assert "invalid" not in result

    def test_not_a_dict_config_returns_empty(self):
        with patch("src.mcp_config.MCP_CONFIG", "[1, 2, 3]"):
            assert load_mcp_config() == {}
