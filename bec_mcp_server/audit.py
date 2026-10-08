"""Per-call audit log: one JSON line per tool call, written off the event loop."""

from __future__ import annotations

import dataclasses
import json
import logging
import queue
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anyio
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext

from bec_mcp_server.runtime import CallRecord, Runtime, end_call_scope, start_call_scope

logger = logging.getLogger(__name__)

#: serialized arguments longer than this are cut; v1 tools take names and small numbers
MAX_ARGUMENT_CHARS = 4096

#: error text kept per call, in the log and in mcp_status
MAX_ERROR_CHARS = 500

_STOP = object()


class AuditLog:
    """Append-only JSONL sink with size-based rotation.

    ``record()`` only enqueues: a slow or wedged file system (home directories on NFS) must not
    stall the event loop that serves every session. A daemon thread writes the lines. Any
    failure to write disables the log with one warning instead of failing tool calls.
    """

    def __init__(self, path: Path, *, max_bytes: int = 10_000_000, backups: int = 3) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self.backups = backups
        self._queue: queue.SimpleQueue[Any] = queue.SimpleQueue()
        self._broken = False
        self._thread = threading.Thread(target=self._run, name="bec-mcp-audit", daemon=True)
        self._thread.start()

    @property
    def enabled(self) -> bool:
        """False once a write failed; the log then stays off until the server restarts."""
        return not self._broken

    def record(self, entry: dict[str, Any]) -> None:
        """Queue one line; returns at once."""
        if not self._broken:
            self._queue.put(entry)

    def close(self, timeout: float = 2.0) -> None:
        """Write what is queued, then stop the writer thread."""
        self._queue.put(_STOP)
        self._thread.join(timeout)

    def flush(self, timeout: float = 2.0) -> None:
        """Wait until every line queued so far is written."""
        done = threading.Event()
        self._queue.put(done)
        done.wait(timeout)

    def _run(self) -> None:
        handle = None
        try:
            while True:
                item = self._queue.get()
                if item is _STOP:
                    return
                if isinstance(item, threading.Event):
                    if handle is not None:
                        handle.flush()
                    item.set()
                    continue
                if self._broken:
                    continue
                try:
                    handle = self._write(handle, item)
                except (OSError, TypeError, ValueError) as exc:
                    self._broken = True
                    logger.warning("audit log %s disabled: %s", self.path, exc)
        finally:
            if handle is not None:
                handle.close()

    def _write(self, handle: Any, entry: dict[str, Any]) -> Any:
        line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
        if handle is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.path.open("a", encoding="utf-8")
        elif handle.tell() + len(line) > self.max_bytes:
            handle.close()
            self._rotate()
            handle = self.path.open("a", encoding="utf-8")
        handle.write(line)
        handle.flush()
        return handle

    def _rotate(self) -> None:
        for index in range(self.backups - 1, 0, -1):
            older = self.path.with_name(f"{self.path.name}.{index}")
            if older.exists():
                older.replace(self.path.with_name(f"{self.path.name}.{index + 1}"))
        if self.backups > 0:
            self.path.replace(self.path.with_name(f"{self.path.name}.1"))
        else:
            self.path.unlink(missing_ok=True)


class AuditMiddleware:  # pylint: disable=too-few-public-methods
    """Observe every ``tools/call`` at the protocol boundary.

    Sitting in front of argument validation means calls a model got wrong (unknown tool,
    invented arguments) are recorded too; those are the most useful lines for evaluating
    smaller models. Each call updates the runtime's statistics (shown by mcp_status) and, when
    configured, appends one line to the audit log.
    """

    def __init__(self, runtime: Runtime, log: AuditLog | None, *, transport: str) -> None:
        self.runtime = runtime
        self.log = log
        self.transport = transport

    async def __call__(
        self, ctx: ServerRequestContext[Any, Any], call_next: CallNext
    ) -> HandlerResult:
        if ctx.method != "tools/call":
            return await call_next(ctx)
        params = dict(ctx.params or {})
        tool = str(params.get("name"))
        holder, token = start_call_scope()
        started = time.monotonic()
        outcome, error = "ok", None
        try:
            result = await call_next(ctx)
        except anyio.get_cancelled_exc_class():
            outcome = "cancelled"
            raise
        except BaseException as exc:
            # protocol-level refusals (MCPError) and malformed params reach us as exceptions
            outcome, error = "error", f"{type(exc).__name__}: {exc}"
            raise
        else:
            if _is_error(result):
                outcome, error = "error", _result_text(result)
            return result
        finally:
            end_call_scope(token)
            record = CallRecord(
                at=datetime.now(timezone.utc),
                tool=tool,
                duration_ms=round((time.monotonic() - started) * 1000, 1),
                outcome=outcome,
                error=_bounded(error),
            )
            self._record(ctx, params.get("arguments"), record, holder.get("cause"))

    def _record(
        self,
        ctx: ServerRequestContext[Any, Any],
        arguments: Any,
        record: CallRecord,
        cause: str | None,
    ) -> None:
        # mcp_status is read by the agent, so it gets what the agent already saw; the real
        # text of a crash stays with the operator (audit log and server log)
        visible = record
        if cause is not None:
            note = f"{cause.split(':', 1)[0]}; details in the server log"
            visible = dataclasses.replace(record, error=f"{record.error} ({note})")
        self.runtime.record_call(visible)
        if self.log is None:
            return
        entry: dict[str, Any] = {
            "ts": record.at.isoformat(timespec="milliseconds"),
            "tool": record.tool,
            "arguments": _bounded_arguments(arguments),
            "duration_ms": record.duration_ms,
            "outcome": record.outcome,
            "transport": self.transport,
            "client": _client_name(ctx),
        }
        if record.error is not None:
            entry["error"] = record.error
        if cause is not None:
            entry["cause"] = _bounded(cause)
        self.log.record(entry)


def _bounded(text: str | None) -> str | None:
    if text is not None and len(text) > MAX_ERROR_CHARS:
        return f"{text[:MAX_ERROR_CHARS]}... [truncated]"
    return text


def _is_error(result: HandlerResult) -> bool:
    if isinstance(result, dict):
        return bool(result.get("isError"))
    return bool(getattr(result, "is_error", False))


def _result_text(result: HandlerResult) -> str | None:
    content = (
        result.get("content") if isinstance(result, dict) else getattr(result, "content", None)
    )
    for item in content or ():
        text = item.get("text") if isinstance(item, dict) else getattr(item, "text", None)
        if text:
            return str(text)
    return None


def _bounded_arguments(arguments: Any) -> Any:
    try:
        text = json.dumps(arguments, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = repr(arguments)
    if len(text) <= MAX_ARGUMENT_CHARS:
        return arguments
    return {"_truncated": f"{text[:MAX_ARGUMENT_CHARS]}..."}


def _client_name(ctx: ServerRequestContext[Any, Any]) -> str | None:
    client_params = getattr(ctx.session, "client_params", None)
    info = getattr(client_params, "client_info", None)
    return getattr(info, "name", None)
