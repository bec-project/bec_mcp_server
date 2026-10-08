"""Worker offload with deadlines and bounded pools, plus the server's own call statistics."""

from __future__ import annotations

import collections
import contextvars
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import partial
from typing import Any, Callable

import anyio
from mcp.server.mcpserver.exceptions import ToolError

logger = logging.getLogger(__name__)

#: failures from these exception types are expected (a BEC service is down, a name does not
#: exist, a deadline passed) and their message is written for the agent; anything else is a
#: crash whose text stays in the server log
ANTICIPATED_ERRORS: tuple[type[Exception], ...] = (
    TimeoutError,
    ConnectionError,
    LookupError,
    ValueError,
)

#: how many failed calls mcp_status keeps
RECENT_FAILURES = 20

#: the real cause of a crash, handed from the tool layer to the audit middleware; a mutable
#: holder so the value survives the context copies anyio makes for tasks and threads
_CALL_CAUSE: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "bec_mcp_call_cause", default=None
)


async def offload(
    func: Callable[..., Any],
    /,
    *args: Any,
    timeout: float,
    gate: threading.Semaphore,
    limiter: anyio.CapacityLimiter | None = None,
    **kwargs: Any,
) -> Any:
    """Run blocking (Redis) work on a worker thread, bounded in time and concurrency.

    The mcp SDK runs sync tools on AnyIO's shared worker pool with no deadline, so a hung Redis
    call would pin a thread and the caller forever. Each pool's limiter queues callers
    asynchronously. A worker never waits for the semaphore: it protects capacity still occupied
    by work abandoned after cancellation, because AnyIO may release its limiter token when the
    awaiting task is cancelled. Cancellation and the absolute deadline are checked before any
    queued work is dispatched, so a call that was reported as not started never starts later.
    """
    name = getattr(func, "__name__", "bec call")
    call = partial(func, *args, **kwargs)
    deadline = time.monotonic() + timeout
    dispatch_lock = threading.Lock()
    dispatch_closed = False
    dispatched = False

    def _gated() -> Any:
        nonlocal dispatched
        anyio.from_thread.check_cancelled()
        if not gate.acquire(blocking=False):
            raise TimeoutError(
                f"{name} could not start: all call slots are busy. No operation was dispatched; "
                "call mcp_status to see how many calls are still running."
            )
        try:
            # Commit dispatch atomically with closing admission on cancellation or timeout.
            # Otherwise a worker paused after its checks could execute after the caller was
            # told that nothing had been dispatched.
            with dispatch_lock:
                if dispatch_closed:
                    return None
                anyio.from_thread.check_cancelled()
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"{name} expired before dispatch; no operation was dispatched"
                    )
                dispatched = True
            return call()
        finally:
            gate.release()

    result: Any = None
    try:
        with anyio.move_on_after(timeout) as scope:
            result = await anyio.to_thread.run_sync(_gated, abandon_on_cancel=True, limiter=limiter)
    finally:
        with dispatch_lock:
            dispatch_closed = True
    if scope.cancelled_caught:
        # only the deadline lands here; a TimeoutError raised by the call itself (bec_lib has
        # several internal waits that raise it) propagates with its real message
        if not dispatched:
            raise TimeoutError(
                f"{name} could not start within {timeout:g}s; no operation was dispatched"
            )
        raise TimeoutError(
            f"{name} did not finish within {timeout:g}s; Redis or a BEC service may be "
            "unresponsive"
        )
    return result


class CallPool:
    """A bounded set of worker slots sharing one deadline, e.g. every call that reaches BEC.

    Pools are separate so stuck work in one (a wedged Redis) cannot starve another (docs search,
    which never touches Redis). Both the AnyIO limiter and the thread-side semaphore are sized
    to ``capacity``: the limiter queues callers, the semaphore bounds threads that are still
    running after their caller gave up.
    """

    def __init__(self, name: str, capacity: int, timeout: float) -> None:
        self.name = name
        self.capacity = capacity
        self.timeout = timeout
        self._gate = _CountingSemaphore(capacity)
        self._limiter = anyio.CapacityLimiter(capacity)

    @property
    def in_flight(self) -> int:
        """Worker threads busy right now, including calls whose caller gave up."""
        return self._gate.held

    async def run(self, func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        """Run ``func`` on a worker thread of this pool, within the pool's deadline."""
        return await offload(
            func, *args, timeout=self.timeout, gate=self._gate, limiter=self._limiter, **kwargs
        )

    def describe(self) -> dict[str, Any]:
        """JSON-safe form for mcp_status."""
        return {"capacity": self.capacity, "in_flight": self.in_flight}


class _CountingSemaphore:
    """threading.Semaphore that also reports how many slots are held (for mcp_status)."""

    def __init__(self, value: int) -> None:
        self._semaphore = threading.Semaphore(value)
        self._lock = threading.Lock()
        self.held = 0

    def acquire(self, *, blocking: bool = True) -> bool:
        """Take a slot, like threading.Semaphore.acquire."""
        acquired = self._semaphore.acquire(blocking=blocking)  # pylint: disable=consider-using-with
        if acquired:
            with self._lock:
                self.held += 1
        return acquired

    def release(self) -> None:
        """Give a slot back."""
        with self._lock:
            self.held -= 1
        self._semaphore.release()


@dataclass(frozen=True)
class CallRecord:
    """One tool call as the audit middleware saw it."""

    at: datetime
    tool: str
    duration_ms: float
    outcome: str
    error: str | None = None

    def describe(self) -> dict[str, Any]:
        """JSON-safe form for mcp_status."""
        return {
            "at": self.at.isoformat(timespec="seconds"),
            "tool": self.tool,
            "outcome": self.outcome,
            "error": self.error,
        }


@dataclass
class Runtime:
    """Process-wide state shared by every session: pools, start time and call statistics."""

    tool_timeout: float
    max_bec_calls: int
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    _started_monotonic: float = field(default_factory=time.monotonic, init=False, repr=False)
    pools: dict[str, CallPool] = field(init=False)
    calls_total: int = field(default=0, init=False)
    calls_failed: int = field(default=0, init=False)
    recent_failures: collections.deque[CallRecord] = field(
        default_factory=lambda: collections.deque(maxlen=RECENT_FAILURES), init=False
    )
    _stats_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        self.pools = {"bec": CallPool("bec", self.max_bec_calls, self.tool_timeout)}

    @property
    def uptime_s(self) -> float:
        """Seconds since the server started."""
        return time.monotonic() - self._started_monotonic

    def record_call(self, record: CallRecord) -> None:
        """Count a finished tool call and keep it if it failed."""
        with self._stats_lock:
            self.calls_total += 1
            if record.outcome != "ok":
                self.calls_failed += 1
                self.recent_failures.append(record)

    async def call(self, pool: str, func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        """Run blocking work in a pool and turn failures into answers the agent can act on."""
        try:
            return await self.pools[pool].run(func, *args, **kwargs)
        except ToolError:
            raise
        except ANTICIPATED_ERRORS as exc:
            raise ToolError(str(exc) or type(exc).__name__) from exc
        except Exception as exc:
            note_crash(exc)
            raise


def note_crash(exc: BaseException) -> None:
    """Hand an unexpected exception's real text to the audit middleware of this call."""
    holder = _CALL_CAUSE.get()
    if holder is not None:
        holder["cause"] = f"{type(exc).__name__}: {exc}"


def start_call_scope() -> tuple[dict[str, str], contextvars.Token]:
    """Open the holder that note_crash() fills for the tool call about to run."""
    holder: dict[str, str] = {}
    return holder, _CALL_CAUSE.set(holder)


def end_call_scope(token: contextvars.Token) -> None:
    """Close the holder opened by start_call_scope()."""
    _CALL_CAUSE.reset(token)
