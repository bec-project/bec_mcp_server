"""Turn bec_lib objects into small, JSON-safe digests for MCP clients.

The agent only ever sees what a tool returns, so every value goes through these helpers: large
sequences and arrays are summarized, non-finite floats become strings, and timestamps get an age.
"""

from __future__ import annotations

import enum
import math
import numbers
import time
from datetime import datetime, timezone
from typing import Any

#: sequences longer than this are summarized instead of inlined (token economy for LLM clients)
MAX_INLINE_SEQUENCE = 16


def jsonable(value: Any) -> Any:
    """Convert a bec_lib value (pydantic models, enums, numpy, containers) to plain JSON data."""
    if value is None or isinstance(value, (bool, str)):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, numbers.Integral):
        return int(value)
    if isinstance(value, numbers.Real):
        # NaN/Infinity are not valid JSON; pydantic would emit a literal NaN on the wire
        result = float(value)
        return result if math.isfinite(result) else str(result)
    if isinstance(value, dict):
        return {str(key): jsonable(val) for key, val in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted(str(item) for item in value)
    if isinstance(value, (list, tuple)):
        items = list(value)
        if len(items) > MAX_INLINE_SEQUENCE:
            return {
                "summary": f"sequence of {len(items)} items, first {MAX_INLINE_SEQUENCE} shown",
                "items": [jsonable(item) for item in items[:MAX_INLINE_SEQUENCE]],
            }
        return [jsonable(item) for item in items]
    # numpy arrays (duck-typed to avoid a hard numpy import)
    if hasattr(value, "shape") and hasattr(value, "dtype"):
        try:
            if getattr(value, "size", 0) <= MAX_INLINE_SEQUENCE:
                return jsonable(value.tolist())
            return {"shape": list(value.shape), "dtype": str(value.dtype), **array_stats(value)}
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def array_stats(value: Any) -> dict[str, Any]:
    """Min, max and mean over the finite elements only.

    A single NaN (masked pixels, divide-by-zero normalization) must not poison the stats or leak
    invalid JSON.
    """
    # x == x is False for NaN and abs(x) < inf excludes infinities; both stay duck-typed
    mask = (value == value) & (abs(value) < float("inf"))  # pylint: disable=comparison-with-itself
    finite = value[mask]
    stats: dict[str, Any] = {}
    if getattr(finite, "size", 0):
        stats["min"] = float(finite.min())
        stats["max"] = float(finite.max())
        stats["mean"] = float(finite.mean())
    non_finite = int(getattr(value, "size", 0) - getattr(finite, "size", 0))
    if non_finite:
        stats["non_finite_count"] = non_finite
    return stats


def inline_array(tree: Any) -> Any:
    """Inline a small array's tolist() tree without jsonable's sequence collapsing."""
    if isinstance(tree, list):
        return [inline_array(item) for item in tree]
    return jsonable(tree)


def signal_digest(value: Any, max_points: int = MAX_INLINE_SEQUENCE) -> Any:
    """Digest one signal value: small arrays inline, large ones as stats plus first/last points."""
    if hasattr(value, "shape") and hasattr(value, "dtype"):
        if getattr(value, "size", 0) <= max_points:
            # inline_array, not jsonable: the caller's max_points must win over jsonable's own
            # 16-element sequence collapsing
            return inline_array(value.tolist() if hasattr(value, "tolist") else value)
        digest: dict[str, Any] = {"shape": list(value.shape), "dtype": str(value.dtype)}
        try:
            digest.update(array_stats(value))
            flat = value.reshape(-1)
            digest["first"] = inline_array(flat[:3].tolist())
            digest["last"] = inline_array(flat[-3:].tolist())
        except (TypeError, ValueError):
            pass  # object/string dtypes: shape and dtype only
        return digest
    return jsonable(value)


def iso_time(timestamp: float | None) -> str | None:
    """Unix timestamp to an ISO 8601 UTC string with second precision."""
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(timespec="seconds")


def age_s(timestamp: float | None, now: float | None = None) -> float | None:
    """Seconds since a Unix timestamp, rounded to 0.1 s; None when the timestamp is unknown."""
    if timestamp is None:
        return None
    try:
        return round(max(0.0, (time.time() if now is None else now) - float(timestamp)), 1)
    except (TypeError, ValueError):
        return None


def first_line(text: str | None) -> str:
    """First line of a docstring or message, stripped; empty for missing text."""
    if not text:
        return ""
    lines = text.strip().splitlines()
    return lines[0] if lines else ""


def short_service_name(name: str) -> str:
    """Shorten an instance suffix: ``BECIPythonClient/4b9d1af8-44ed-...`` to ``.../4b9d1af8``.

    Unique services (DeviceServer, ScanServer, ...) publish under their bare class name, every
    other service and client under ``<class>/<uuid>``; the full uuid only costs tokens.
    """
    base, sep, instance = name.partition("/")
    if not sep:
        return name
    return f"{base}/{instance[:8]}"
