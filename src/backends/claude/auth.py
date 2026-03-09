"""Claude backend authentication provider."""

import os
import logging
from typing import Dict, Any, List

from src.auth import BackendAuthProvider

logger = logging.getLogger(__name__)


class ClaudeAuthProvider(BackendAuthProvider):
    """Claude backend auth — delegates to ANTHROPIC_AUTH_TOKEN or CLI auth."""

    @property
    def name(self) -> str:
        return "claude"

    def __init__(self):
        self.auth_method = self._detect_method()

    def _detect_method(self) -> str:
        explicit = os.getenv("CLAUDE_AUTH_METHOD", "").lower()
        if explicit:
            method_map = {
                "cli": "claude_cli",
                "claude_cli": "claude_cli",
                "api_key": "anthropic",
                "anthropic": "anthropic",
            }
            if explicit in method_map:
                return method_map[explicit]
            raise ValueError(
                f"Unsupported CLAUDE_AUTH_METHOD '{explicit}'. "
                f"Supported values: {', '.join(method_map.keys())}"
            )
        if os.getenv("ANTHROPIC_AUTH_TOKEN"):
            return "anthropic"
        return "claude_cli"

    def validate(self) -> Dict[str, Any]:
        if self.auth_method == "anthropic":
            key = os.getenv("ANTHROPIC_AUTH_TOKEN")
            if not key:
                return {
                    "valid": False,
                    "errors": ["ANTHROPIC_AUTH_TOKEN environment variable not set"],
                    "config": {},
                }
            if len(key) < 10:
                logger.warning("ANTHROPIC_AUTH_TOKEN is shorter than 10 characters")
            return {
                "valid": True,
                "errors": [],
                "config": {"api_key_present": True, "api_key_length": len(key)},
            }
        # claude_cli — assume valid, actual check happens at SDK call time
        return {
            "valid": True,
            "errors": [],
            "config": {
                "method": "Claude Code CLI authentication",
                "note": "Using existing Claude Code CLI authentication",
            },
        }

    def build_env(self) -> Dict[str, str]:
        if self.auth_method == "anthropic":
            key = os.getenv("ANTHROPIC_AUTH_TOKEN")
            if key:
                return {"ANTHROPIC_AUTH_TOKEN": key}
        return {}

    def get_isolation_vars(self) -> List[str]:
        return ["OPENAI_API_KEY"]
