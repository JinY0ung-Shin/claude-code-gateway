"""Claude backend constants and configuration.

Single source of truth for Claude-specific tool names, models, and configuration.
All configurable values can be overridden via environment variables.
"""

import os

# Claude Agent SDK Tool Names
# These are the built-in tools available in the Claude Agent SDK
# See: https://docs.anthropic.com/en/docs/claude-code/sdk
CLAUDE_TOOLS = [
    "Task",  # Launch agents for complex tasks
    "Bash",  # Execute bash commands
    "Glob",  # File pattern matching
    "Grep",  # Search file contents
    "Read",  # Read files
    "Edit",  # Edit files
    "Write",  # Write files
    "NotebookEdit",  # Edit Jupyter notebooks
    "WebFetch",  # Fetch web content
    "TodoWrite",  # Manage todo lists
    "WebSearch",  # Search the web
    "BashOutput",  # Get bash output
    "KillShell",  # Kill bash shells
    "Skill",  # Execute skills
    "SlashCommand",  # Execute slash commands
]

# Default tools to allow when tools are enabled
# Subset of CLAUDE_TOOLS that are safe and commonly used
DEFAULT_ALLOWED_TOOLS = [
    "Read",
    "Glob",
    "Grep",
    "Bash",
    "Write",
    "Edit",
]

# Claude Models
# Models supported by Claude Code SDK
# See: https://docs.anthropic.com/en/docs/about-claude/models/overview
# See: https://docs.anthropic.com/en/docs/claude-code/model-config
CLAUDE_MODELS = [
    "opus",
    "sonnet",
    "haiku",
]

# Thinking Mode Configuration
# Options: "adaptive" (recommended for Opus 4.6/Sonnet 4.6), "enabled", "disabled"
THINKING_MODE = os.getenv("THINKING_MODE", "adaptive")
THINKING_BUDGET_TOKENS = int(os.getenv("THINKING_BUDGET_TOKENS", "10000"))

# Token-Level Streaming
# When enabled, uses SDK's include_partial_messages to stream individual tokens
# instead of waiting for complete messages
TOKEN_STREAMING = os.getenv("TOKEN_STREAMING", "true").lower() in ("true", "1", "yes")

# Disallowed Subagent Types
# Comma-separated list of subagent types to block via Agent(type) syntax
# Example: "statusline-setup,Plan"
_raw_disallowed = os.getenv("DISALLOWED_SUBAGENT_TYPES", "statusline-setup")
DISALLOWED_SUBAGENT_TYPES = [f"Agent({t.strip()})" for t in _raw_disallowed.split(",") if t.strip()]
