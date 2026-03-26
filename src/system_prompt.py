"""Custom system prompt management.

Provides a thread-safe store for the global base system prompt.
Completely separate from ``RuntimeConfig`` to avoid logging prompt
content and to support large text values cleanly.

Priority: persisted override > file default > None (preset mode).

The admin override is persisted to a JSON file in the project data
directory so it survives server restarts.
"""

import json
import logging
import os
import platform
import re
from pathlib import Path
from threading import Lock
from typing import Optional

logger = logging.getLogger(__name__)

_lock = Lock()
_default_prompt: Optional[str] = None  # loaded from file at startup
_runtime_prompt: Optional[str] = None  # admin override
_preset_text: Optional[str] = None  # cached preset reference text

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_PERSIST_FILE = _DATA_DIR / "system_prompt.json"


def _load_persisted() -> Optional[str]:
    """Load the persisted admin override from disk."""
    if not _PERSIST_FILE.is_file():
        return None
    try:
        data = json.loads(_PERSIST_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            logger.warning("Persisted system prompt has invalid structure, ignoring")
            return None
        value = data.get("prompt")
        if not isinstance(value, str) or not value.strip():
            logger.warning("Persisted system prompt has invalid value, ignoring")
            return None
        return value
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load persisted system prompt: %s", e)
        return None


def _save_persisted(text: Optional[str]) -> None:
    """Save or delete the persisted admin override.

    Raises ``OSError`` on failure so callers can avoid in-memory/disk divergence.
    """
    if text is None:
        if _PERSIST_FILE.is_file():
            _PERSIST_FILE.unlink()
            logger.info("System prompt: persisted file removed")
    else:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        _PERSIST_FILE.write_text(
            json.dumps({"prompt": text}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("System prompt: persisted to %s", _PERSIST_FILE)


def _resolve_placeholders(text: str) -> str:
    """Replace ``{{PLACEHOLDER}}`` tokens with runtime values."""
    from src.constants import PROMPT_LANGUAGE, PROMPT_MEMORY_PATH

    replacements = {
        "LANGUAGE": PROMPT_LANGUAGE,
        "WORKING_DIRECTORY": os.getenv("CLAUDE_CWD", os.getcwd()),
        "PLATFORM": platform.system().lower(),
        "SHELL": os.environ.get("SHELL", ""),
        "OS_VERSION": platform.platform(),
        "MEMORY_PATH": PROMPT_MEMORY_PATH,
    }
    for key, value in replacements.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def load_default_prompt(file_path: str = "") -> None:
    """Load the default system prompt from *file_path*.

    Also restores any previously persisted admin override.
    Placeholders like ``{{LANGUAGE}}`` are resolved at load time.

    * If *file_path* is empty/blank, preset mode is used (no custom prompt).
    * If the file does not exist, ``FileNotFoundError`` is raised (fail-fast).
    """
    global _default_prompt, _runtime_prompt

    if not file_path or not file_path.strip():
        _default_prompt = None
        logger.info("System prompt: using claude_code preset (no file configured)")
    else:
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"SYSTEM_PROMPT_FILE not found: {file_path}")

        content = path.read_text(encoding="utf-8").strip()
        if not content:
            _default_prompt = None
            logger.warning("System prompt file is empty, falling back to preset mode")
        else:
            _default_prompt = _resolve_placeholders(content)
            logger.info("System prompt: loaded from file (%d chars)", len(_default_prompt))

    # Restore persisted admin override
    persisted = _load_persisted()
    if persisted:
        resolved = _resolve_placeholders(persisted)
        with _lock:
            _runtime_prompt = resolved
        logger.info("System prompt: restored persisted override (%d chars)", len(resolved))


def get_system_prompt() -> Optional[str]:
    """Return the active base system prompt.

    Returns ``None`` when in preset mode (no custom prompt active).
    """
    with _lock:
        if _runtime_prompt is not None:
            return _runtime_prompt
    return _default_prompt


def get_default_prompt() -> Optional[str]:
    """Return the file-loaded default prompt (ignoring runtime override)."""
    return _default_prompt


def set_system_prompt(text: str) -> None:
    """Set a runtime override for the system prompt and persist to disk.

    Raises ``ValueError`` if *text* is empty or whitespace-only.
    Raises ``OSError`` if the persist file cannot be written.
    """
    global _runtime_prompt
    stripped = text.strip()
    if not stripped:
        raise ValueError("System prompt cannot be empty. Use reset to revert to default.")
    _save_persisted(stripped)
    resolved = _resolve_placeholders(stripped)
    with _lock:
        _runtime_prompt = resolved
    logger.info("System prompt: runtime override set (%d chars)", len(stripped))


def reset_system_prompt() -> None:
    """Clear the runtime override, reverting to file default or preset.

    Raises ``OSError`` if the persist file cannot be removed.
    """
    global _runtime_prompt
    _save_persisted(None)
    with _lock:
        _runtime_prompt = None
    logger.info("System prompt: runtime override cleared")


def is_using_preset() -> bool:
    """Return ``True`` when no custom prompt is active (preset mode)."""
    return get_system_prompt() is None


def get_prompt_mode() -> str:
    """Return the current prompt mode as a string label."""
    with _lock:
        if _runtime_prompt is not None:
            return "custom"
    if _default_prompt is not None:
        return "file"
    return "preset"


def _load_preset_text() -> Optional[str]:
    """Load the claude_code preset reference from docs/, stripping the markdown header."""
    ref_path = Path(__file__).resolve().parent.parent / "docs" / "claude-code-system-prompt-reference.md"
    if not ref_path.is_file():
        return None
    raw = ref_path.read_text(encoding="utf-8")
    # Strip the markdown front-matter (title, blockquote, hr) — keep only the prompt body
    body = re.sub(r"\A#[^\n]*\n+(?:>[^\n]*\n)*\n*---\n*", "", raw).strip()
    return body or None


def get_preset_text() -> Optional[str]:
    """Return the cached claude_code preset reference text."""
    global _preset_text
    if _preset_text is None:
        _preset_text = _load_preset_text()
    return _preset_text
