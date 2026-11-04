"""Fake log streams for the log cache and log analysis tests, built from real bec_lib types."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import redis
from bec_lib import messages
from bec_lib.endpoints import MessageEndpoints
from louie import saferef

from bec_mcp_server.bec.connection import BecConnection, BecConnectionConfig
from bec_mcp_server.bec.log_cache import DEFAULT_LOG_CACHE_SIZE

NOW = 1_800_000_000.0
LEVEL_NO = {
    "DEBUG": 10,
    "INFO": 20,
    "CONSOLE_LOG_ERROR": 22,
    "SUCCESS": 25,
    "WARNING": 30,
    "ERROR": 40,
    "CRITICAL": 50,
}
#: a ScanServer record captured from a BEC 4 stack: a DeviceInstructionError (from a LimitError
#: on samx) re-raised as ScanAbortion; paths rewritten to /opt/bec
SCAN_ABORTION_TRACEBACK = (
    Path(__file__).parent / "data" / "scan_abortion_traceback.txt"
).read_text(encoding="utf-8")


def log_message(  # pylint: disable=too-many-arguments
    message: str,
    *,
    level: str = "INFO",
    service: str = "ScanServer",
    t: float = NOW - 1,
    name: str = "bec_server.scan_server.scan_worker",
    function: str = "run",
    line: int = 1,
    thread: str = "MainThread",
    exception: dict | None = None,
) -> messages.LogMessage:
    """A LogMessage shaped like the one bec_lib's logger publishes (loguru serialize=True)."""
    record = {
        "message": message,
        "level": {"name": level, "no": LEVEL_NO[level], "icon": ""},
        "time": {"repr": "", "timestamp": t},
        "name": name,
        "function": function,
        "module": name.rsplit(".", 1)[-1],
        "line": line,
        "exception": exception,
        "extra": {},
        "thread": {"id": 1, "name": thread},
        "process": {"id": 1, "name": "MainProcess"},
    }
    return messages.LogMessage(
        log_type=level.lower(),
        log_msg={"text": f"{service} | {message}", "record": record, "service_name": service},
    )


class StreamConnector:
    """The connector surface the log cache and log tools use, over an in-memory stream.

    Subscriptions are held through louie weak references, as bec_lib's connector holds them, so
    a callback whose owner is gone stops receiving records.
    """

    def __init__(self, stream: list[messages.LogMessage] | None = None) -> None:
        self.stream = list(stream or [])
        self.subscribers: list[Any] = []
        self.get_last_calls: list[int] = []
        self.register_error: BaseException | None = None
        self.up = True
        #: called inside get_last, to publish records while the seed is being read
        self.during_get_last: Any = None

    def ping(self) -> bool:
        if not self.up:
            raise redis.exceptions.ConnectionError("Redis went away")
        return True

    def set_retry_enabled(self, enabled: bool) -> None:
        pass

    def register(self, topics: Any, cb: Any = None, **kwargs: Any) -> None:
        assert getattr(topics, "endpoint", topics) == MessageEndpoints.log().endpoint
        if self.register_error is not None:
            raise self.register_error
        self.subscribers.append((saferef.safe_ref(cb), kwargs))

    def get_last(self, topic: Any, key: Any = None, count: int = 1) -> Any:
        assert getattr(topic, "endpoint", topic) == MessageEndpoints.log().endpoint
        self.get_last_calls.append(count)
        if not self.up:
            raise redis.exceptions.ConnectionError("Redis went away")
        newest = self.stream[-count:]
        if self.during_get_last is not None:
            hook, self.during_get_last = self.during_get_last, None
            hook()
        if not newest:
            return None
        entries = [{"data": msg} for msg in newest]
        return entries if count > 1 else entries[0]

    def publish(self, *msgs: messages.LogMessage) -> None:
        """Append records to the stream and deliver them to live subscribers."""
        for msg in msgs:
            self.stream.append(msg)
            for ref, kwargs in self.subscribers:
                callback = ref()
                if callback is not None:
                    callback({"data": msg}, **kwargs)

    def live_subscribers(self) -> int:
        return sum(1 for ref, _ in self.subscribers if ref() is not None)


def stream_client(stream=(), alarms=(), *, connector: StreamConnector | None = None):
    """A fake started BECClient whose connector serves ``stream``."""
    handler = SimpleNamespace(get_unhandled_alarms=lambda severity=None: list(alarms))
    return SimpleNamespace(
        connector=connector or StreamConnector(list(stream)),
        alarm_handler=handler,
        device_manager=object(),
        start=lambda: None,
        shutdown=lambda **kwargs: None,
    )


def connection_for(client, *, log_cache_size: int = DEFAULT_LOG_CACHE_SIZE) -> BecConnection:
    """A real BecConnection (lazy start, log cache, error mapping) whose client is the fake."""
    return BecConnection(
        BecConnectionConfig(log_cache_size=log_cache_size),
        client_factory=lambda config, cls: client,
    )
