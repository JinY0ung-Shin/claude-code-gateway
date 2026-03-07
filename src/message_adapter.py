from typing import List, Optional
from src.models import Message
import re
import json


class MessageAdapter:
    """Converts between OpenAI message format and Claude Code prompts."""

    TOOL_RESULT_MAX_LENGTH = 2000

    @staticmethod
    def _truncate_tool_content(content):
        """Truncate tool result content if too long."""
        if isinstance(content, str) and len(content) > MessageAdapter.TOOL_RESULT_MAX_LENGTH:
            return content[: MessageAdapter.TOOL_RESULT_MAX_LENGTH] + "\n... (truncated)"
        return content

    @staticmethod
    def _block_to_dict(block) -> Optional[dict]:
        """Convert a ContentBlock object to a plain dict."""
        # TextBlock (object)
        if hasattr(block, "text") and not hasattr(block, "thinking"):
            return {"type": "text", "text": block.text}

        # TextBlock (dict)
        if isinstance(block, dict) and block.get("type") == "text":
            return block

        # ThinkingBlock (object)
        if hasattr(block, "thinking"):
            return {"type": "thinking", "thinking": block.thinking}

        # ThinkingBlock (dict)
        if isinstance(block, dict) and block.get("type") == "thinking":
            return block

        # ToolUseBlock (object)
        if hasattr(block, "name") and hasattr(block, "input"):
            return {
                "type": "tool_use",
                "id": getattr(block, "id", ""),
                "name": block.name,
                "input": block.input if isinstance(block.input, dict) else str(block.input),
            }

        # ToolUseBlock (dict)
        if isinstance(block, dict) and block.get("type") == "tool_use":
            return block

        # ToolResultBlock (object)
        if hasattr(block, "tool_use_id") and hasattr(block, "content"):
            return {
                "type": "tool_result",
                "tool_use_id": block.tool_use_id,
                "content": MessageAdapter._truncate_tool_content(block.content),
                "is_error": bool(getattr(block, "is_error", False)),
            }

        # ToolResultBlock (dict)
        if isinstance(block, dict) and block.get("type") == "tool_result":
            result = dict(block)
            result["content"] = MessageAdapter._truncate_tool_content(result.get("content", ""))
            return result

        # Plain string
        if isinstance(block, str):
            return {"type": "text", "text": block}

        return None

    @staticmethod
    def format_block(block) -> Optional[str]:
        """Convert a ContentBlock to a formatted string.

        Handles: TextBlock, ToolUseBlock, ToolResultBlock, ThinkingBlock.
        TextBlock returns plain text. ThinkingBlock returns <think> tags.
        Other blocks return fenced JSON.
        Returns None for unrecognized blocks.
        """
        # Fast path: TextBlock object — skip dict allocation
        if hasattr(block, "text") and not hasattr(block, "thinking"):
            return block.text
        if isinstance(block, dict) and block.get("type") == "text":
            return block.get("text", "")
        if isinstance(block, str):
            return block

        # ThinkingBlock — render as <think> tags (OpenAI-compatible)
        if hasattr(block, "thinking"):
            return "<think>" + (block.thinking or "") + "</think>"
        if isinstance(block, dict) and block.get("type") == "thinking":
            return "<think>" + block.get("thinking", "") + "</think>"

        block_dict = MessageAdapter._block_to_dict(block)
        if block_dict is None:
            return None

        return "\n```json\n" + json.dumps(block_dict, ensure_ascii=False) + "\n```\n"

    @staticmethod
    def format_blocks(content_blocks: list) -> Optional[str]:
        """Convert a list of ContentBlocks to a single text string."""
        parts = []
        for block in content_blocks:
            formatted = MessageAdapter.format_block(block)
            if formatted:
                parts.append(formatted)
        return "".join(parts) if parts else None

    @staticmethod
    def messages_to_prompt(messages: List[Message]) -> tuple[str, Optional[str]]:
        """
        Convert OpenAI messages to Claude Code prompt format.
        Returns (prompt, system_prompt)
        """
        system_prompt = None
        conversation_parts = []

        for message in messages:
            if message.role == "system":
                # Use the last system message as the system prompt
                system_prompt = message.content
            elif message.role == "user":
                conversation_parts.append(message.content)
            elif message.role == "assistant":
                conversation_parts.append(message.content)

        # Join conversation parts
        prompt = "\n\n".join(conversation_parts)

        return prompt, system_prompt

    @staticmethod
    def filter_content(content: str) -> str:
        """
        Filter content for unsupported features and tool usage.
        Remove thinking blocks, tool calls, and image references.
        """
        if not content:
            return content

        # Remove thinking blocks (common when tools are disabled but Claude tries to think)
        thinking_pattern = r"<thinking>.*?</thinking>"
        content = re.sub(thinking_pattern, "", content, flags=re.DOTALL)

        # Extract content from attempt_completion blocks (these contain the actual user response)
        attempt_completion_pattern = r"<attempt_completion>(.*?)</attempt_completion>"
        attempt_matches = re.findall(attempt_completion_pattern, content, flags=re.DOTALL)
        if attempt_matches:
            # Use the content from the attempt_completion block
            extracted_content = attempt_matches[0].strip()

            # If there's a <result> tag inside, extract from that
            result_pattern = r"<result>(.*?)</result>"
            result_matches = re.findall(result_pattern, extracted_content, flags=re.DOTALL)
            if result_matches:
                extracted_content = result_matches[0].strip()

            if extracted_content:
                content = extracted_content
        else:
            # Remove other tool usage blocks (when tools are disabled but Claude tries to use them)
            tool_patterns = [
                r"<read_file>.*?</read_file>",
                r"<write_file>.*?</write_file>",
                r"<bash>.*?</bash>",
                r"<search_files>.*?</search_files>",
                r"<str_replace_editor>.*?</str_replace_editor>",
                r"<args>.*?</args>",
                r"<ask_followup_question>.*?</ask_followup_question>",
                r"<attempt_completion>.*?</attempt_completion>",
                r"<question>.*?</question>",
                r"<follow_up>.*?</follow_up>",
                r"<suggest>.*?</suggest>",
            ]

            for pattern in tool_patterns:
                content = re.sub(pattern, "", content, flags=re.DOTALL)

        # Pattern to match image references or base64 data
        image_pattern = r"\[Image:.*?\]|data:image/.*?;base64,.*?(?=\s|$)"

        def replace_image(match):
            return "[Image: Content not supported by Claude Code]"

        content = re.sub(image_pattern, replace_image, content)

        # Clean up extra whitespace and newlines
        content = re.sub(r"\n\s*\n\s*\n", "\n\n", content)  # Multiple newlines to double
        content = content.strip()

        # If content is now empty or only whitespace, provide a fallback
        if not content or content.isspace():
            return "I understand you're testing the system. How can I help you today?"

        return content

    @staticmethod
    def response_input_to_prompt(input_data) -> str:
        """Convert Responses API input to a Claude prompt string.

        Accepts either a plain string or an array of input items
        (OpenAI Responses API format).
        """
        if isinstance(input_data, str):
            return input_data

        # Array format: extract text from message items
        parts = []
        for item in input_data:
            content = item.content

            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                # content is list of content parts, e.g. [{"type": "input_text", "text": "..."}]
                text_parts = []
                for part in content:
                    if isinstance(part, dict) and part.get("text"):
                        text_parts.append(part["text"])
                text = "\n".join(text_parts)
            else:
                continue

            if not text:
                continue

            parts.append(text)

        return "\n\n".join(parts)

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """
        Rough estimation of token count.
        OpenAI's rule of thumb: ~4 characters per token for English text.
        """
        return len(text) // 4
