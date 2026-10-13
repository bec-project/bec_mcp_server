"""The server's own copy of BEC's log stream, kept up to date in the background.

When the BEC client starts, the cache subscribes to the log stream through that client's connector
and seeds itself with the newest records already in Redis. From then on bec_lib's own stream
thread delivers every new record, so following the stream costs no tool call, no worker slot and
no time on the event loop. Tools read a snapshot of the cache instead of decoding the stream on
every call, and refer to records by a stable ``id``, counted from 1 since the server started.

The cache is bounded: once ``capacity`` records are held, the oldest one goes for each new one.
It survives client restarts: the new client's seed is merged, so records that arrived while no
client was running are picked up and records already held are not added twice. While BEC is
unreachable, tools can still read what the cache held before.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from bec_lib.endpoints import MessageEndpoints

from bec_mcp_server.bec.digest import iso_time
from bec_mcp_server.bec.log_stream import (
    LOG_STREAM_CAPACITY,
    LogRecord,
    parse_log_message,
    read_log_stream,
)

logger = logging.getLogger(__name__)

#: records the cache holds by default: as many as bec_lib keeps in the stream, so the cache
#: reaches back at least as far as Redis
DEFAULT_LOG_CACHE_SIZE = LOG_STREAM_CAPACITY

#: characters kept per cached message; a record longer than this is a dump, not a log line
MAX_STORED_MESSAGE_CHARS = 20_000


def record_key(record: LogRecord) -> tuple[Any, ...]:
    """What makes a record unique: who logged what, from where, at which microsecond."""
    return (
        record.service,
        record.timestamp,
        record.level_no,
        record.thread,
        record.source,
        record.line,
        record.message,
    )


@dataclass(frozen=True)
class CacheView:
    """A consistent snapshot of the cache for one tool call."""

    records: list[LogRecord]
    capacity: int
    #: the cache is subscribed to the stream right now
    following: bool
    #: records dropped because the cache was full
    evicted: int
    #: why the cache is not following, if it is not
    problem: str | None
    #: when following stopped (epoch seconds), if it did
    stopped_at: float | None


class _Follower:  # pylint: disable=too-few-public-methods
    """Hands one client's stream records to the cache.

    bec_lib keeps only a weak reference to a callback, so the cache holds the follower and drops
    it to stop following; records from an earlier client's follower are ignored.
    """

    def __init__(self, cache: LogCache, generation: int) -> None:
        self._cache = cache
        self._generation = generation

    def on_message(self, msg: Any, **_kwargs: Any) -> None:
        """Stream callback, run on the client's callback thread; must never raise."""
        # pylint: disable=protected-access
        self._cache._receive(self._generation, msg)


class LogCache:
    """A bounded, thread-safe copy of BEC's log stream with stable record ids."""

    def __init__(
        self, capacity: int = DEFAULT_LOG_CACHE_SIZE, *, clock: Callable[[], float] = time.time
    ):
        if capacity < 1:
            raise ValueError("the log cache needs room for at least one record")
        self.capacity = capacity
        self._clock = clock
        self._lock = threading.Lock()
        self._records: deque[LogRecord] = deque()
        self._keys: set[tuple[Any, ...]] = set()
        self._next_id = 0
        self._generation = 0
        self._follower: _Follower | None = None
        self._seeding = False
        self._pending: deque[LogRecord] = deque(maxlen=capacity)
        self._state = "not_started"
        self._problem: str | None = None
        self._following_since: float | None = None
        self._stopped_at: float | None = None
        self._received = 0
        self._duplicates = 0
        self._unreadable = 0
        self._evicted = 0
        self._attaches = 0

    # ------------------------------------------------------------------ following a client

    def attach(self, client: Any) -> None:
        """Follow ``client``'s log stream from now on, seeded with the records already in Redis.

        Called when a client has started. Never raises: a cache that cannot follow leaves the
        tools reading the stream themselves.
        """
        with self._lock:
            self._generation += 1
            generation = self._generation
            follower = self._follower = _Follower(self, generation)
            self._seeding = True
            self._pending.clear()
            self._state = "starting"
        try:
            # subscribe first: records logged while the seed is read arrive through the
            # subscription, and those in both are merged once
            client.connector.register(MessageEndpoints.log(), cb=follower.on_message)
            seed = [
                parse_log_message(message) for message in read_log_stream(client, self.capacity)
            ]
        except Exception as exc:  # pylint: disable=broad-except
            problem = f"could not follow the log stream: {type(exc).__name__}: {exc}"
            logger.warning("log cache: %s", problem)
            with self._lock:
                if generation == self._generation:
                    self._stop_locked("failed", problem)
            return
        with self._lock:
            if generation != self._generation:
                return
            for record in seed:
                if record is not None:
                    self._add_locked(record)
            for record in self._pending:
                self._add_locked(record)
            self._pending.clear()
            self._seeding = False
            self._state = "following"
            self._problem = None
            self._stopped_at = None
            self._following_since = self._clock()
            self._attaches += 1
        logger.info("log cache: following BEC's log stream (%d records held)", len(self._records))

    def detach(self, reason: str) -> None:
        """Stop following the current client, which is about to shut down."""
        with self._lock:
            if self._follower is not None:
                self._stop_locked("stopped", reason)

    def _stop_locked(self, state: str, problem: str) -> None:
        # dropping the follower ends bec_lib's weak subscription to it
        self._follower = None
        self._generation += 1
        self._seeding = False
        self._pending.clear()
        self._state = state
        self._problem = problem
        self._stopped_at = self._clock()
        self._following_since = None

    def _receive(self, generation: int, msg: Any) -> None:
        try:
            message = msg.get("data") if isinstance(msg, dict) else getattr(msg, "value", msg)
            record = parse_log_message(message)
        except Exception:  # pylint: disable=broad-except
            record = None
        with self._lock:
            if generation != self._generation:
                return
            self._received += 1
            if record is None:
                self._unreadable += 1
            elif self._seeding:
                self._pending.append(record)
            else:
                self._add_locked(record)

    def _add_locked(self, record: LogRecord) -> None:
        if len(record.message) > MAX_STORED_MESSAGE_CHARS:
            record = dataclasses.replace(
                record, message=f"{record.message[:MAX_STORED_MESSAGE_CHARS]}… [cut]"
            )
        key = record_key(record)
        if key in self._keys:
            self._duplicates += 1
            return
        if len(self._records) >= self.capacity:
            self._keys.discard(record_key(self._records.popleft()))
            self._evicted += 1
        self._next_id += 1
        self._records.append(dataclasses.replace(record, id=self._next_id))
        self._keys.add(key)

    # ------------------------------------------------------------------ reading

    @property
    def following(self) -> bool:
        """Whether the cache is subscribed to the stream right now."""
        return self._state == "following"

    def __len__(self) -> int:
        return len(self._records)

    def view(self) -> CacheView:
        """A snapshot for one tool call; copying 10,000 references takes well under a millisecond."""
        with self._lock:
            return CacheView(
                records=list(self._records),
                capacity=self.capacity,
                following=self._state == "following",
                evicted=self._evicted,
                problem=self._problem,
                stopped_at=self._stopped_at,
            )

    def state(self) -> dict[str, Any]:
        """Cache state for mcp_status; touches no I/O."""
        with self._lock:
            oldest = self._records[0] if self._records else None
            newest = self._records[-1] if self._records else None
            return {
                "state": self._state,
                "records": len(self._records),
                "capacity": self.capacity,
                "first_id": oldest.id if oldest else None,
                "last_id": newest.id if newest else None,
                "oldest": iso_time(oldest.timestamp) if oldest else None,
                "newest": iso_time(newest.timestamp) if newest else None,
                "received": self._received,
                "duplicates": self._duplicates,
                "unreadable": self._unreadable,
                "evicted": self._evicted,
                "subscriptions": self._attaches,
                "problem": self._problem,
            }
