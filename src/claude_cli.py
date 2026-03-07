import os
import tempfile
import atexit
import shutil
from typing import AsyncGenerator, Dict, Any, Optional, List
from pathlib import Path
import logging

from claude_agent_sdk import query, ClaudeAgentOptions
from claude_agent_sdk.types import (
    StreamEvent,
    AssistantMessage,
    ResultMessage,
    UserMessage,
    SystemMessage,
)
from src.constants import (
    DEFAULT_TIMEOUT_MS,
    THINKING_MODE,
    THINKING_BUDGET_TOKENS,
    TOKEN_STREAMING,
    DISALLOWED_SUBAGENT_TYPES,
)
from src.message_adapter import MessageAdapter

logger = logging.getLogger(__name__)


class ClaudeCodeCLI:
    def __init__(self, timeout: int = None, cwd: Optional[str] = None):
        if timeout is None:
            timeout = DEFAULT_TIMEOUT_MS
        self.timeout = timeout / 1000  # Convert ms to seconds
        self.temp_dir = None

        # If cwd is provided (from CLAUDE_CWD env var), use it
        # Otherwise create an isolated temp directory
        if cwd:
            self.cwd = Path(cwd)
            # Check if the directory exists
            if not self.cwd.exists():
                logger.error(f"ERROR: Specified working directory does not exist: {self.cwd}")
                logger.error(
                    "Please create the directory first or unset CLAUDE_CWD to use a temporary directory"
                )
                raise ValueError(f"Working directory does not exist: {self.cwd}")
            else:
                logger.info(f"Using CLAUDE_CWD: {self.cwd}")
        else:
            # Create isolated temp directory (cross-platform)
            self.temp_dir = tempfile.mkdtemp(prefix="claude_code_workspace_")
            self.cwd = Path(self.temp_dir)
            logger.info(f"Using temporary isolated workspace: {self.cwd}")

            # Register cleanup function to remove temp dir on exit
            atexit.register(self._cleanup_temp_dir)

        # Import auth manager
        from src.auth import auth_manager, validate_claude_code_auth

        # Validate authentication
        is_valid, auth_info = validate_claude_code_auth()
        if not is_valid:
            logger.warning(f"Claude Code authentication issues detected: {auth_info['errors']}")
        else:
            logger.info(f"Claude Code authentication method: {auth_info.get('method', 'unknown')}")

        # Store auth environment variables for SDK
        self.claude_env_vars = auth_manager.get_claude_code_env_vars()

    def _build_sdk_options(
        self,
        model: Optional[str] = None,
        system_prompt: Optional[str] = None,
        max_turns: int = 10,
        allowed_tools: Optional[List[str]] = None,
        disallowed_tools: Optional[List[str]] = None,
        permission_mode: Optional[str] = None,
        output_format: Optional[Dict[str, Any]] = None,
        mcp_servers: Optional[Dict[str, Any]] = None,
        session_id: Optional[str] = None,
        resume: Optional[str] = None,
    ) -> ClaudeAgentOptions:
        """Build ClaudeAgentOptions with common parameters."""
        options = ClaudeAgentOptions(max_turns=max_turns, cwd=self.cwd, setting_sources=["project"])

        # Configure thinking mode
        if THINKING_MODE == "adaptive":
            options.thinking = {"type": "adaptive"}
        elif THINKING_MODE == "enabled":
            options.thinking = {"type": "enabled", "budget_tokens": THINKING_BUDGET_TOKENS}
        elif THINKING_MODE != "disabled":
            logger.warning(f"Unrecognized THINKING_MODE={THINKING_MODE!r}, thinking not configured")

        if model:
            options.model = model
        if system_prompt:
            options.system_prompt = {"type": "text", "text": system_prompt}
        else:
            options.system_prompt = {"type": "preset", "preset": "claude_code"}
        if allowed_tools:
            options.allowed_tools = allowed_tools
        base_disallowed = list(DISALLOWED_SUBAGENT_TYPES)
        if disallowed_tools:
            base_disallowed.extend(disallowed_tools)
        if base_disallowed:
            options.disallowed_tools = base_disallowed
        if permission_mode:
            options.permission_mode = permission_mode
        if output_format:
            options.output_format = output_format
        if mcp_servers:
            options.mcp_servers = mcp_servers
        if TOKEN_STREAMING:
            options.include_partial_messages = True

        # Session management: new session uses --session-id, follow-up uses --resume
        if resume:
            options.resume = resume
        elif session_id:
            options.extra_args["session-id"] = session_id

        return options

    _TYPE_MAP = {
        StreamEvent: "stream_event",
        AssistantMessage: "assistant",
        ResultMessage: "result",
        UserMessage: "user",
        SystemMessage: "system",
    }

    def _convert_message(self, message) -> Dict[str, Any]:
        """Convert SDK message object to dict if needed."""
        if isinstance(message, dict):
            return message
        if hasattr(message, "__dict__"):
            result = {
                k: v for k, v in vars(message).items() if not k.startswith("_") and not callable(v)
            }
            if "type" not in result:
                type_name = self._TYPE_MAP.get(type(message))
                if type_name:
                    result["type"] = type_name
            return result
        return message

    async def verify_cli(self) -> bool:
        """Verify Claude Agent SDK is working and authenticated."""
        try:
            # Test SDK with a simple query
            logger.info("Testing Claude Agent SDK...")

            messages = []
            async for message in query(
                prompt="Hello",
                options=ClaudeAgentOptions(
                    max_turns=1,
                    cwd=self.cwd,
                    system_prompt={"type": "preset", "preset": "claude_code"},
                ),
            ):
                messages.append(message)
                # Break early on first response to speed up verification
                # Handle both dict and object types
                msg_type = (
                    getattr(message, "type", None)
                    if hasattr(message, "type")
                    else message.get("type")
                    if isinstance(message, dict)
                    else None
                )
                if msg_type == "assistant":
                    break

            if messages:
                logger.info("✅ Claude Agent SDK verified successfully")
                return True
            else:
                logger.warning("⚠️ Claude Agent SDK test returned no messages")
                return False

        except Exception as e:
            logger.error(f"Claude Agent SDK verification failed: {e}")
            logger.warning("Please ensure Claude Code is installed and authenticated:")
            logger.warning("  1. Install: npm install -g @anthropic-ai/claude-code")
            logger.warning("  2. Set ANTHROPIC_AUTH_TOKEN environment variable")
            logger.warning("  3. Test: claude --print 'Hello'")
            return False

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
        """Run Claude Agent using the Python SDK and yield response chunks.

        For multi-turn conversations:
        - First turn: pass session_id (uses --session-id CLI flag)
        - Follow-up turns: pass resume=session_id (uses --resume CLI flag)
        """

        try:
            # Set authentication environment variables (if any)
            original_env = {}
            if self.claude_env_vars:  # Only set env vars if we have any
                for key, value in self.claude_env_vars.items():
                    original_env[key] = os.environ.get(key)
                    os.environ[key] = value

            try:
                options = self._build_sdk_options(
                    model=model,
                    system_prompt=system_prompt,
                    max_turns=max_turns,
                    allowed_tools=allowed_tools,
                    disallowed_tools=disallowed_tools,
                    permission_mode=permission_mode,
                    output_format=output_format,
                    mcp_servers=mcp_servers,
                    session_id=session_id,
                    resume=resume,
                )

                # Run the query and yield messages
                async for message in query(prompt=prompt, options=options):
                    # Debug logging
                    logger.debug(f"Raw SDK message type: {type(message)}")
                    logger.debug(f"Raw SDK message: {message}")

                    converted = self._convert_message(message)
                    logger.debug(f"Converted message: {converted}")
                    yield converted

            finally:
                # Restore original environment (if we changed anything)
                if original_env:
                    for key, original_value in original_env.items():
                        if original_value is None:
                            os.environ.pop(key, None)
                        else:
                            os.environ[key] = original_value

        except Exception as e:
            logger.error(f"Claude Agent SDK error: {e}")
            # Yield error message in the expected format
            yield {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "error_message": str(e),
            }

    def parse_claude_message(self, messages: List[Dict[str, Any]]) -> Optional[str]:
        """Extract the assistant message from Claude Agent SDK messages.

        Renders all content blocks (text, tool_use, tool_result, thinking)
        into a single text string. Prioritizes ResultMessage.result to avoid
        duplication with AssistantMessage content (SDK sends both with the
        same text).
        """
        # First pass: check if a ResultMessage with result exists
        result_text = None
        for message in messages:
            if message.get("subtype") == "success" and "result" in message:
                result = message["result"]
                if result and result.strip():
                    result_text = result

        # If ResultMessage found, use it directly (avoids duplication)
        if result_text is not None:
            return result_text

        # Fallback: extract from AssistantMessage content blocks
        all_parts = []
        for message in messages:
            # AssistantMessage (new SDK format): has content list
            if "content" in message and isinstance(message["content"], list):
                formatted = MessageAdapter.format_blocks(message["content"])
                if formatted:
                    all_parts.append(formatted)

            # AssistantMessage (old format)
            elif message.get("type") == "assistant" and "message" in message:
                sdk_message = message["message"]
                if isinstance(sdk_message, dict) and "content" in sdk_message:
                    content = sdk_message["content"]
                    if isinstance(content, list):
                        formatted = MessageAdapter.format_blocks(content)
                        if formatted:
                            all_parts.append(formatted)
                    elif isinstance(content, str) and content.strip():
                        all_parts.append(content)

        return "\n".join(all_parts) if all_parts else None

    def estimate_token_usage(
        self, prompt: str, completion: str, model: Optional[str] = None
    ) -> Dict[str, int]:
        """
        Estimate token usage based on character count.

        Uses rough approximation: ~4 characters per token for English text.
        This is approximate and may not match actual tokenization.
        """
        # Rough approximation: 1 token ≈ 4 characters
        prompt_tokens = max(1, len(prompt) // 4)
        completion_tokens = max(1, len(completion) // 4)

        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }

    def _cleanup_temp_dir(self):
        """Clean up temporary directory on exit."""
        if self.temp_dir and os.path.exists(self.temp_dir):
            try:
                shutil.rmtree(self.temp_dir)
                logger.info(f"Cleaned up temporary workspace: {self.temp_dir}")
            except Exception as e:
                logger.warning(f"Failed to clean up temp directory {self.temp_dir}: {e}")
