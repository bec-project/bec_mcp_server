"""Cancellation, deadline and capacity guarantees of the worker offload, without Redis."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import anyio
import pytest

from bec_mcp_server.runtime import CallPool, offload


async def _wait_until(predicate):
    with anyio.fail_after(3):
        while not predicate():
            await anyio.sleep(0.001)


def test_cancelled_queued_call_never_dispatches():
    executed = threading.Event()

    async def run():
        gate = threading.Semaphore(1)
        limiter = anyio.CapacityLimiter(1)
        returned = anyio.Event()
        scope = anyio.CancelScope()

        async def submit():
            with scope:
                await offload(executed.set, timeout=5, gate=gate, limiter=limiter)
            returned.set()

        async with anyio.create_task_group() as tasks:
            async with limiter:
                tasks.start_soon(submit)
                await _wait_until(lambda: limiter.statistics().tasks_waiting == 1)
                scope.cancel()
                await returned.wait()
                assert scope.cancelled_caught
            # capacity is free again, but the cancelled call must not run later
            assert await offload(lambda: "next", timeout=1, gate=gate, limiter=limiter) == "next"
        assert not executed.is_set()

    anyio.run(run)


def test_expired_queued_call_never_dispatches():
    executed = threading.Event()

    async def run():
        gate = threading.Semaphore(1)
        limiter = anyio.CapacityLimiter(1)
        borrower = object()
        limiter.acquire_on_behalf_of_nowait(borrower)
        try:
            with pytest.raises(TimeoutError, match="no operation was dispatched"):
                await offload(executed.set, timeout=0.05, gate=gate, limiter=limiter)
        finally:
            limiter.release_on_behalf_of(borrower)
        await offload(lambda: None, timeout=1, gate=gate, limiter=limiter)
        assert not executed.is_set()

    anyio.run(run)


def test_cancellation_after_worker_admission_prevents_dispatch():
    class PausedGate:
        def __init__(self):
            self.entered = threading.Event()
            self.resume = threading.Event()
            self.released = threading.Event()

        def acquire(self, *, blocking):
            assert blocking is False
            self.entered.set()
            assert self.resume.wait(3)
            return True

        def release(self):
            self.released.set()

    gate = PausedGate()
    executed = threading.Event()

    async def run():
        returned = anyio.Event()
        scope = anyio.CancelScope()

        async def submit():
            with scope:
                await offload(executed.set, timeout=5, gate=gate)
            returned.set()

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(submit)
            try:
                await _wait_until(gate.entered.is_set)
                scope.cancel()
                await returned.wait()
                assert scope.cancelled_caught
            finally:
                gate.resume.set()
            await _wait_until(gate.released.is_set)
        assert not executed.is_set()

    anyio.run(run)


def test_worker_checks_absolute_deadline_before_dispatch(monkeypatch):
    times = iter([10.0, 12.0])
    monkeypatch.setattr(
        "bec_mcp_server.runtime.time", SimpleNamespace(monotonic=lambda: next(times))
    )
    gate = threading.Semaphore(1)
    executed = threading.Event()

    async def run():
        with pytest.raises(TimeoutError, match="expired before dispatch"):
            await offload(executed.set, timeout=1, gate=gate)

    anyio.run(run)
    assert not executed.is_set()
    assert gate.acquire(blocking=False)


def test_timed_out_call_keeps_capacity_until_worker_finishes():
    started = threading.Event()
    finish = threading.Event()
    replacement_executed = threading.Event()
    gate = threading.Semaphore(1)

    def stuck_call():
        started.set()
        assert finish.wait(3)

    async def run():
        limiter = anyio.CapacityLimiter(1)
        try:
            with pytest.raises(TimeoutError, match="did not finish within 0.1s"):
                await offload(stuck_call, timeout=0.1, gate=gate, limiter=limiter)
            assert started.is_set()
            # the abandoned call still holds the only slot: a new call must fail fast instead
            # of starting a second thread against the wedged service
            with pytest.raises(TimeoutError, match="all call slots are busy"):
                await offload(replacement_executed.set, timeout=1, gate=gate, limiter=limiter)
            assert not replacement_executed.is_set()
        finally:
            finish.set()
        await _wait_until(lambda: gate.acquire(blocking=False))
        gate.release()
        await offload(replacement_executed.set, timeout=1, gate=gate, limiter=limiter)
        assert replacement_executed.is_set()

    anyio.run(run)


def test_timeout_raised_by_the_call_itself_keeps_its_message():
    # bec_lib raises the builtin TimeoutError from several internal waits; those must reach
    # the agent with their own text, not be rewritten into the per-call deadline error
    def call():
        raise TimeoutError("bec_lib internal wait expired")

    async def run():
        await offload(call, timeout=5, gate=threading.Semaphore(1))

    with pytest.raises(TimeoutError, match="internal wait expired"):
        anyio.run(run)


def test_saturated_pool_reports_busy_slots_without_starting_more_work():
    gate = threading.Semaphore(1)
    release = threading.Event()

    async def run():
        results = {}

        async def slow():
            results["slow"] = await offload(release.wait, 3, timeout=5, gate=gate)

        async def fast():
            await anyio.sleep(0.2)  # let slow() take the only slot first
            try:
                await offload(time.sleep, 0.01, timeout=0.3, gate=gate)
            except TimeoutError as exc:
                results["fast_error"] = str(exc)
            release.set()

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(slow)
            tasks.start_soon(fast)
        return results

    results = anyio.run(run)

    assert "all call slots are busy" in results["fast_error"]
    assert results["slow"] is True


def test_call_pool_counts_calls_in_flight():
    entered = threading.Event()
    release = threading.Event()
    pool = CallPool("bec", capacity=2, timeout=5)

    def blocked():
        entered.set()
        assert release.wait(3)

    async def run():
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(pool.run, blocked)
            await _wait_until(entered.is_set)
            assert pool.describe() == {"capacity": 2, "in_flight": 1}
            release.set()
        assert pool.in_flight == 0

    anyio.run(run)
