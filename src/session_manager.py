import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from threading import Lock

from src.models import Message, SessionInfo
from src.constants import SESSION_CLEANUP_INTERVAL_MINUTES, SESSION_MAX_AGE_MINUTES

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


def _ensure_utc(dt: datetime) -> datetime:
    """Normalize datetimes to UTC while tolerating legacy naive inputs."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass
class Session:
    """Represents a conversation session with message history."""

    session_id: str
    ttl_minutes: int = 60
    messages: List[Message] = field(default_factory=list)
    created_at: datetime = field(default_factory=_utcnow)
    last_accessed: datetime = field(default_factory=_utcnow)
    expires_at: Optional[datetime] = field(default=None)
    turn_counter: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def __post_init__(self):
        self.created_at = _ensure_utc(self.created_at)
        self.last_accessed = _ensure_utc(self.last_accessed)
        if self.expires_at is None:
            self.expires_at = _utcnow() + timedelta(minutes=self.ttl_minutes)
        else:
            self.expires_at = _ensure_utc(self.expires_at)

    def touch(self):
        """Update last accessed time and extend expiration."""
        now = _utcnow()
        self.last_accessed = now
        self.expires_at = now + timedelta(minutes=self.ttl_minutes)

    def add_messages(self, messages: List[Message]):
        """Add new messages to the session."""
        self.messages.extend(messages)
        self.touch()

    def get_all_messages(self) -> List[Message]:
        """Get all messages in the session."""
        return self.messages

    def is_expired(self) -> bool:
        """Check if the session has expired."""
        return _utcnow() > self.expires_at

    def to_session_info(self) -> SessionInfo:
        """Convert to SessionInfo model."""
        return SessionInfo(
            session_id=self.session_id,
            created_at=self.created_at,
            last_accessed=self.last_accessed,
            message_count=len(self.messages),
            expires_at=self.expires_at,
        )


class SessionManager:
    """Manages conversation sessions with automatic cleanup."""

    def __init__(self, default_ttl_minutes: int = 60, cleanup_interval_minutes: int = 5):
        self.sessions: Dict[str, Session] = {}
        self.lock = Lock()
        self.default_ttl_minutes = default_ttl_minutes
        self.cleanup_interval_minutes = cleanup_interval_minutes
        self._cleanup_task = None

    def start_cleanup_task(self):
        """Start the automatic cleanup task - call this after the event loop is running."""
        if self._cleanup_task is not None:
            return  # Already started

        async def cleanup_loop():
            try:
                while True:
                    await asyncio.sleep(self.cleanup_interval_minutes * 60)
                    await self.cleanup_expired_sessions()
            except asyncio.CancelledError:
                logger.info("Session cleanup task cancelled")
                raise

        try:
            loop = asyncio.get_running_loop()
            self._cleanup_task = loop.create_task(cleanup_loop())
            logger.info(
                f"Started session cleanup task (interval: {self.cleanup_interval_minutes} minutes)"
            )
        except RuntimeError:
            logger.warning("No running event loop, automatic session cleanup disabled")

    async def cleanup_expired_sessions(self):
        """Remove expired sessions."""
        with self.lock:
            expired = [sid for sid, s in self.sessions.items() if s.is_expired()]
            for sid in expired:
                del self.sessions[sid]
                logger.info(f"Cleaned up expired session: {sid}")

    async def async_shutdown(self):
        """Async shutdown: clear all sessions."""
        if self._cleanup_task:
            self._cleanup_task.cancel()

        with self.lock:
            self.sessions.clear()
            logger.info("Session manager async shutdown complete")

    def get_or_create_session(self, session_id: str) -> Session:
        """Get existing session or create a new one."""
        with self.lock:
            if session_id in self.sessions:
                session = self.sessions[session_id]
                if session.is_expired():
                    logger.info(f"Session {session_id} expired, creating new session")
                    del self.sessions[session_id]
                    session = Session(session_id=session_id, ttl_minutes=self.default_ttl_minutes)
                    self.sessions[session_id] = session
                else:
                    session.touch()
            else:
                session = Session(session_id=session_id, ttl_minutes=self.default_ttl_minutes)
                self.sessions[session_id] = session
                logger.info(f"Created new session: {session_id}")

        return session

    def get_session(self, session_id: str) -> Optional[Session]:
        """Get existing session without creating new one."""
        with self.lock:
            session = self.sessions.get(session_id)
            if session and not session.is_expired():
                session.touch()
                return session
            elif session and session.is_expired():
                del self.sessions[session_id]
                logger.info(f"Removed expired session: {session_id}")

        return None

    def delete_session(self, session_id: str) -> bool:
        """Delete a session."""
        with self.lock:
            if session_id in self.sessions:
                del self.sessions[session_id]
                logger.info(f"Deleted session: {session_id}")
            else:
                return False

        return True

    def list_sessions(self) -> List[SessionInfo]:
        """List all active sessions."""
        with self.lock:
            expired_sessions = [
                session_id for session_id, session in self.sessions.items() if session.is_expired()
            ]
            for session_id in expired_sessions:
                del self.sessions[session_id]

            return [session.to_session_info() for session in self.sessions.values()]

    def process_messages(
        self, messages: List[Message], session_id: Optional[str] = None
    ) -> Tuple[List[Message], Optional[str]]:
        """
        Process messages for a request, handling both stateless and session modes.

        Returns:
            Tuple of (all_messages_for_claude, actual_session_id_used)
        """
        if session_id is None:
            # Stateless mode - just return the messages as-is
            return messages, None

        # Session mode - get or create session and merge messages
        session = self.get_or_create_session(session_id)

        # Add new messages to session
        session.add_messages(messages)

        # Return all messages in the session for Claude
        all_messages = session.get_all_messages()

        logger.info(
            f"Session {session_id}: processing {len(messages)} new messages, {len(all_messages)} total"
        )

        return all_messages, session_id

    def add_assistant_response(self, session_id: Optional[str], assistant_message: Message):
        """Add assistant response to session if session mode is active."""
        if session_id is None:
            return

        session = self.get_session(session_id)
        if session:
            session.add_messages([assistant_message])
            logger.info(f"Added assistant response to session {session_id}")

    def get_stats(self) -> Dict[str, int]:
        """Get session manager statistics."""
        with self.lock:
            active_sessions = sum(1 for s in self.sessions.values() if not s.is_expired())
            expired_sessions = sum(1 for s in self.sessions.values() if s.is_expired())
            total_messages = sum(len(s.messages) for s in self.sessions.values())

            return {
                "active_sessions": active_sessions,
                "expired_sessions": expired_sessions,
                "total_messages": total_messages,
            }

    def shutdown(self):
        """Shutdown the session manager and cleanup tasks."""
        if self._cleanup_task:
            self._cleanup_task.cancel()

        with self.lock:
            self.sessions.clear()
            logger.info("Session manager shutdown complete")


# Global session manager instance
session_manager = SessionManager(
    default_ttl_minutes=SESSION_MAX_AGE_MINUTES,
    cleanup_interval_minutes=SESSION_CLEANUP_INTERVAL_MINUTES,
)
