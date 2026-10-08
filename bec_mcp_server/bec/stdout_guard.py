"""Keep bec_lib's prints off stdout while a thread works with the BEC client.

Over stdio, stdout is the MCP wire: one stray ``print`` or rich table from bec_lib corrupts the
JSON-RPC stream. mcp 2.x already points fd 1 at stderr while its stdio transport serves, but it
falls back to sharing the stream when it cannot duplicate descriptors, and an embedder may pass
its own streams. This guard is the second line: inside ``guarded()``, text written to
``sys.stdout`` from the current thread goes to stderr instead.

``contextlib.redirect_stdout`` is not an option, because it swaps the process-wide
``sys.stdout`` and BEC calls run concurrently on worker threads. The guard installs one proxy
that routes per thread, and every other thread keeps writing where it did before.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, TextIO

_local = threading.local()
_install_lock = threading.Lock()


class _ThreadRoutedStdout:
    """Proxy for ``sys.stdout`` that diverts guarded threads' text to stderr."""

    def __init__(self, target: TextIO) -> None:
        self._target = target

    @staticmethod
    def _diverted() -> bool:
        return getattr(_local, "depth", 0) > 0

    @property
    def _stream(self) -> TextIO:
        # resolved per call: sys.stderr may itself be replaced (pytest capture, embedders)
        if self._diverted():
            return sys.stderr or sys.__stderr__  # type: ignore[return-value]
        return self._target

    def write(self, text: str) -> int:
        """Write to stdout, or to stderr from a guarded thread."""
        return self._stream.write(text)

    def writelines(self, lines: Any) -> None:
        """Write lines to stdout, or to stderr from a guarded thread."""
        self._stream.writelines(lines)

    def flush(self) -> None:
        """Flush the stream this thread writes to."""
        self._stream.flush()

    def __getattr__(self, name: str) -> Any:
        # encoding, isatty, fileno, buffer, ... come from the real stdout, so the stdio
        # transport and rich's terminal detection see the stream they expect
        return getattr(self._target, name)


def install() -> None:
    """Put the routing proxy in front of ``sys.stdout`` (idempotent)."""
    with _install_lock:
        if not isinstance(sys.stdout, _ThreadRoutedStdout) and sys.stdout is not None:
            sys.stdout = _ThreadRoutedStdout(sys.stdout)  # type: ignore[assignment]


def is_guarded() -> bool:
    """True while the current thread is inside ``guarded()``."""
    return getattr(_local, "depth", 0) > 0


@contextmanager
def guarded() -> Iterator[None]:
    """Divert this thread's stdout writes to stderr for the duration (re-entrant)."""
    install()
    _local.depth = getattr(_local, "depth", 0) + 1
    try:
        yield
    finally:
        _local.depth -= 1
