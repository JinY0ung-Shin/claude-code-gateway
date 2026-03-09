"""Codex backend authentication provider."""

import os
from typing import Dict, Any, List

from src.auth import BackendAuthProvider


class CodexAuthProvider(BackendAuthProvider):
    """Codex backend auth — OPENAI_API_KEY is optional (Codex CLI handles its own auth)."""

    @property
    def name(self) -> str:
        return "codex"

    def validate(self) -> Dict[str, Any]:
        key = os.getenv("OPENAI_API_KEY")
        return {
            "valid": True,
            "errors": [],
            "config": {"api_key_present": bool(key)},
        }

    def build_env(self) -> Dict[str, str]:
        key = os.getenv("OPENAI_API_KEY")
        return {"OPENAI_API_KEY": key} if key else {}

    def get_isolation_vars(self) -> List[str]:
        return ["ANTHROPIC_AUTH_TOKEN"]
