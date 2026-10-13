"""BEC's shared log stream: reading it through the client's connector and parsing its records.

Every BEC service and client publishes its log records to one Redis stream that bec_lib trims to
about 10,000 records. The log cache follows that stream; bec_logs, the log analysis tools and
bec_diagnose read either the cache or, when it is not running, the stream itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from bec_lib.endpoints import MessageEndpoints

#: BEC trims its log stream to about this many records (``bec_lib.logger``, ``max_size``)
LOG_STREAM_CAPACITY = 10_000

#: loguru level numbers as ``bec_lib.logger.LogLevel`` defines them
LOG_LEVELS = {
    "trace": 5,
    "debug": 10,
    "info": 20,
    "console_log": 21,
    "console_log_error": 22,
    "success": 25,
    "warning": 30,
    "error": 40,
    "critical": 50,
}

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


@dataclass(frozen=True)
class LogRecord:
    """One log record, parsed from a ``LogMessage``.

    ``id`` is the record's position in the server's log cache, counted from 1 since the server
    started; records read straight from the stream have none.
    """

    service: str | None
    level: str
    level_no: int
    timestamp: float | None
    message: str
    source: str | None
    id: int | None = None
    #: line of the logging call in ``source``
    line: int | None = None
    #: ``Type: value`` of an exception loguru attached (logger.exception); bec_lib publishes no
    #: traceback text for it, only this
    exception: str | None = None
    thread: str | None = None


def read_log_stream(client: Any, count: int = LOG_STREAM_CAPACITY) -> list[Any]:
    """Return the newest ``count`` log messages of BEC's shared log stream, oldest first.

    bec_lib has no public log reader, so this reads the stream through the client's own
    connector. It is the one place to change if bec_lib grows an accessor. Decoding a full
    10,000-record stream takes about half a second.
    """
    entries = client.connector.get_last(MessageEndpoints.log(), count=count)
    if entries is None:
        return []
    if isinstance(entries, dict):
        # get_last returns a bare dict, not a list, when count == 1
        entries = [entries]
    return [entry["data"] for entry in entries if isinstance(entry, dict) and "data" in entry]


def parse_log_message(message: Any) -> LogRecord | None:  # pylint: disable=too-many-locals
    """Turn one ``LogMessage`` from the stream into a ``LogRecord``; None if unusable.

    A ``LogRecord`` passes through unchanged, so readers accept cached records and stream
    messages alike.
    """
    if isinstance(message, LogRecord):
        return message
    log_msg = getattr(message, "log_msg", None)
    log_type = str(getattr(message, "log_type", "") or "").lower()
    if log_msg is None:
        return None
    line = exception = thread = None
    if isinstance(log_msg, dict):
        record = log_msg.get("record") or {}
        level = record.get("level") or {}
        level_name = str(level.get("name") or log_type.upper())
        level_no = level.get("no")
        text = record.get("message")
        if text is None:
            text = log_msg.get("text") or ""
        timestamp = (record.get("time") or {}).get("timestamp")
        service = log_msg.get("service_name") or None
        name, function = record.get("name"), record.get("function")
        source = f"{name}:{function}" if name and function else (name or None)
        line = record.get("line") if isinstance(record.get("line"), int) else None
        exception = _exception_text(record.get("exception"))
        thread = (record.get("thread") or {}).get("name") or None
    else:
        # log_msg is typed dict | str; the str form carries no structure at all
        level_name, level_no, text = log_type.upper(), None, log_msg
        timestamp = service = source = None
    if not isinstance(level_no, int):
        level_no = LOG_LEVELS.get(log_type, LOG_LEVELS.get(level_name.lower(), 0))
    if log_type == "console_log_error" or level_name.lower() == "console_log_error":
        # bec_lib numbers console_log_error at 22, below WARNING, yet it carries client-side
        # tracebacks; level="error" must not drop it
        level_no = max(level_no, LOG_LEVELS["error"])
    if not isinstance(timestamp, (int, float)):
        timestamp = None
    return LogRecord(
        service=str(service) if service else None,
        level=level_name,
        level_no=level_no,
        timestamp=float(timestamp) if timestamp is not None else None,
        message=_ANSI_RE.sub("", str(text)).strip(),
        source=source,
        line=line,
        exception=exception,
        thread=str(thread) if thread else None,
    )


def _exception_text(exception: Any) -> str | None:
    """loguru's serialized exception, ``{"type": ..., "value": ..., "traceback": bool}``."""
    if not isinstance(exception, dict) or not exception.get("type"):
        return None
    value = exception.get("value")
    return f"{exception['type']}: {value}" if value else str(exception["type"])
