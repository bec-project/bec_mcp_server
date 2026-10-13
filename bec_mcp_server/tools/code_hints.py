"""Code mode's link to the other tools: findings that point at bec_code_traceback.

In code mode, every answer that shows a Python traceback gets a finding whose Next: call is
``bec_code_traceback`` with a short handle instead of the traceback itself, which a small model
would have to copy. A traceback in plain text (``bec_logs`` with full detail) gets a ref kept by
this server; a log record or incident from ``bec_log_range`` or ``bec_log_analysis``, whose
traceback was already parsed into a digest, gets the id of the cached record that holds it.
"""

from __future__ import annotations

import collections
import threading
from typing import Any, Callable, Iterator

from mcp.server.context import CallNext, HandlerResult, ServerRequestContext

from bec_mcp_server.bec.digest import iso_time
from bec_mcp_server.tools import ANSWER_HOOK, Finding
from bec_mcp_server.tools.tracebacks import Traceback, has_frames, parse_traceback, parse_tracebacks

#: tracebacks other tools showed that bec_code_traceback can be given by ref
TRACEBACK_REFS = 64
#: traceback findings added to one answer of another tool
MAX_HINTS = 3
#: characters of an exception quoted in a finding
_QUOTE_CHARS = 160


class TracebackRefs:
    """Tracebacks other tools have shown, by a short ref, so a Next: line need not repeat one."""

    def __init__(self, capacity: int = TRACEBACK_REFS) -> None:
        self._capacity = capacity
        self._parsed: collections.OrderedDict[str, Traceback] = collections.OrderedDict()
        self._lock = threading.Lock()

    def add(self, parsed: Traceback) -> str:
        """Keep ``parsed`` and return its ref; the same traceback always gets the same ref."""
        ref = f"tb-{parsed.key()}"
        with self._lock:
            self._parsed[ref] = parsed
            self._parsed.move_to_end(ref)
            while len(self._parsed) > self._capacity:
                self._parsed.popitem(last=False)
        return ref

    def get(self, ref: str) -> Traceback | None:
        """The traceback stored under ``ref``, if it is still kept."""
        with self._lock:
            return self._parsed.get(ref)


#: what code mode reads from the server's log cache: its records now, or None without a cache
LogRecords = Callable[[], "list[Any] | None"]


def cached_log_records(connection: Any) -> LogRecords:
    """The records of the connection's log cache, read when asked; None if it has none."""

    def records() -> list[Any] | None:
        cache = getattr(connection, "log_cache", None)
        return None if cache is None else cache.view().records

    return records


class TracebackHints:
    """Code mode's link to other tools: findings that point at bec_code_traceback.

    As middleware it lets tool_result() add those findings during each tool call. It is
    installed only in code mode, so no other mode pays for scanning answers.
    """

    def __init__(self, refs: TracebackRefs, log_records: LogRecords | None = None) -> None:
        self.refs = refs
        self.log_records = log_records

    def record_traceback(self, record_id: int) -> tuple[Traceback, dict[str, Any]]:
        """The traceback in one cached log record, and what the agent sees of that record.

        Raises:
            LookupError: if there is no log cache or it no longer holds the record.
            ValueError: if the record holds no traceback with frames.
        """
        records = self.log_records() if self.log_records is not None else None
        if records is None:
            raise LookupError("This server keeps no log cache, so pass the traceback text instead")
        record = next((item for item in records if item.id == record_id), None)
        if record is None:
            held = f"ids {records[0].id} to {records[-1].id}" if records else "no records"
            raise LookupError(
                f"Log record {record_id} is not in the log cache, which holds {held}; find the "
                "record with bec_log_range, or pass the traceback text instead"
            )
        found = [parsed for parsed in parse_tracebacks(record.message) if parsed.frames]
        if not found:
            raise ValueError(f"Log record {record_id} holds no Python traceback with frames")
        about = {"id": record.id, "service": record.service, "time": iso_time(record.timestamp)}
        return found[0], about

    def hints(self, data: dict[str, Any]) -> list[Finding]:
        """A finding per traceback in an answer's data, pointing at bec_code_traceback."""
        findings: list[Finding] = []
        seen: set[str] = set()
        for record_id, parsed in self._tracebacks(data):
            key = parsed.key()
            if key in seen:
                continue
            seen.add(key)
            findings.append(_hint(parsed, record_id, self.refs))
            if len(findings) >= MAX_HINTS:
                break
        return findings

    def _tracebacks(self, data: Any) -> Iterator[tuple[int | None, Traceback]]:
        """(log record id or None, traceback) for every traceback an answer shows."""
        by_id: dict[int, Any] | None = None
        for item in _walk(data):
            if isinstance(item, str):
                if has_frames(item):
                    for parsed in parse_tracebacks(item):
                        if parsed.frames:
                            yield None, parsed
                continue
            # a log record or incident from bec_log_range or bec_log_analysis: its traceback
            # was parsed into a digest, so it is found again in the cached record by id
            ids = _candidate_ids(item)
            if not ids or self.log_records is None:
                continue
            if by_id is None:
                by_id = {record.id: record for record in self.log_records() or ()}
            for record_id in ids:
                record = by_id.get(record_id)
                parsed = parse_traceback(record.message) if record is not None else None
                if parsed is not None and parsed.frames:
                    yield record_id, parsed
                    break

    async def __call__(
        self, ctx: ServerRequestContext[Any, Any], call_next: CallNext
    ) -> HandlerResult:
        if ctx.method != "tools/call":
            return await call_next(ctx)
        token = ANSWER_HOOK.set(self.hints)
        try:
            return await call_next(ctx)
        finally:
            ANSWER_HOOK.reset(token)


def _hint(parsed: Traceback, record_id: int | None, refs: TracebackRefs) -> Finding:
    shown = parsed.raised.text()
    if len(parsed.chain) > 1:
        # a chained traceback is logged as its last exception; the first one says what failed
        shown = f"{shown} (from {parsed.root.text()})"
    if len(shown) > _QUOTE_CHARS:
        shown = f"{shown[: _QUOTE_CHARS - 1]}…"
    message = f"Code mode can show the source of the {len(parsed.frames)} frames behind {shown}"
    if record_id is not None:
        return Finding(
            "info",
            "traceback_code",
            message,
            subject=shown,
            next_tool="bec_code_traceback",
            next_args={"record": record_id},
        )
    return Finding(
        "info",
        "traceback_code",
        message,
        subject=shown,
        next_tool="bec_code_traceback",
        next_args={"ref": refs.add(parsed)},
    )


def _candidate_ids(item: dict[str, Any]) -> list[int]:
    """Log record ids that may hold the traceback a digest shows, newest first."""
    if not isinstance(item.get("traceback"), dict):
        return []
    if isinstance(item.get("id"), int):
        return [item["id"]]
    # an incident: the newest of its records with a traceback supplied the one shown
    ids = [value for value in item.get("ids") or () if isinstance(value, int)]
    for key in ("last_id", "first_id"):
        if isinstance(item.get(key), int) and item[key] not in ids:
            ids.append(item[key])
    return sorted(ids, reverse=True)


def _walk(data: Any, max_items: int = 20_000) -> Iterator[Any]:
    """Every string and dict in ``data``; a dict that is a log digest is not entered."""
    stack = [data]
    visited = 0
    while stack and visited < max_items:
        item = stack.pop()
        visited += 1
        if isinstance(item, str):
            yield item
        elif isinstance(item, dict):
            yield item
            if not _candidate_ids(item):
                stack.extend(reversed(list(item.values())))
        elif isinstance(item, (list, tuple)):
            stack.extend(reversed(item))
