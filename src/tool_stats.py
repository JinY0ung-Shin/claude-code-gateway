"""Per-turn tool-call aggregator used by the streaming path.

A single :class:`ToolStatsCollector` lives on the stack of each
/v1/responses request (both streaming and non-streaming).  Tool-use and
tool-result events emitted by the SDK are funnelled through it so the
gateway can write a concise summary (name / count / errors / total ms)
into the ``usage_tool`` table.

Only metadata is collected - never the tool's arguments or result body.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple


class ToolStatsCollector:
    """Aggregate tool-call counts and durations for a single turn."""

    __slots__ = ("_starts", "_stats")

    def __init__(self) -> None:
        # tool_use_id -> (tool_name, monotonic_start, parent_tool_use_id)
        self._starts: Dict[str, Tuple[str, float, Optional[str]]] = {}
        # tool_name -> {count, errors, total_ms}
        self._stats: Dict[str, Dict[str, int]] = {}

    def record_use(
        self,
        tool_use_id: Optional[str],
        name: str,
        *,
        parent_tool_use_id: Optional[str] = None,
    ) -> None:
        """Record the start of a tool invocation.

        ``tool_use_id`` may be ``None`` when the SDK elides IDs on streaming
        deltas - in that case we still bump the call counter but cannot
        pair the result for a latency measurement.

        ``parent_tool_use_id`` marks a subagent's call; it rides along so
        :meth:`in_flight` can apply the subagent stream gates.
        """
        if not name:
            return
        entry = self._stats.setdefault(name, {"count": 0, "errors": 0, "total_ms": 0})
        entry["count"] += 1
        if tool_use_id:
            self._starts[tool_use_id] = (name, time.monotonic(), parent_tool_use_id)

    def record_result(
        self,
        tool_use_id: Optional[str],
        is_error: bool,
        *,
        fallback_name: str = "",
    ) -> None:
        """Record completion of a tool invocation.

        When the matching ``record_use`` can be paired via ``tool_use_id``
        the elapsed milliseconds are added to the tool's ``total_ms``.
        Otherwise only the error counter is bumped (using
        ``fallback_name`` if provided, else ``"unknown"``).
        """
        start_info = self._starts.pop(tool_use_id, None) if tool_use_id else None
        if start_info is not None:
            name, started, _parent = start_info
            dur_ms = int((time.monotonic() - started) * 1000)
            entry = self._stats.setdefault(name, {"count": 0, "errors": 0, "total_ms": 0})
            entry["total_ms"] += dur_ms
            if is_error:
                entry["errors"] += 1
            return
        if is_error:
            name = fallback_name or "unknown"
            entry = self._stats.setdefault(name, {"count": 0, "errors": 0, "total_ms": 0})
            entry["errors"] += 1

    def in_flight(self) -> List[Dict[str, Any]]:
        """Tool calls started but not yet answered, oldest first.

        This is what the stall guard and the ``response.tool_progress``
        heartbeat read: a silent stream with an entry here is a tool running,
        not a wedged CLI. Each entry carries ``tool_use_id``, ``name``,
        ``parent_tool_use_id`` and ``elapsed_seconds`` (gateway-observed,
        since the completed tool_use block arrived).
        """
        now = time.monotonic()
        return [
            {
                "tool_use_id": tool_use_id,
                "name": name,
                "parent_tool_use_id": parent,
                "elapsed_seconds": int(now - started),
            }
            for tool_use_id, (name, started, parent) in sorted(
                self._starts.items(), key=lambda item: item[1][1]
            )
        ]

    def snapshot(self) -> Dict[str, Dict[str, int]]:
        """Return a shallow copy of the collected stats (tool_name -> fields)."""
        return {name: dict(stats) for name, stats in self._stats.items()}

    def __bool__(self) -> bool:  # pragma: no cover - trivial
        return bool(self._stats)
