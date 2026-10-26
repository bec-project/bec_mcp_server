"""Device tools: list devices, describe one device's signals, and read its cached values.

Every function here takes the BECClient that ``BecConnection.use()`` lends and returns a digest
with the same top-level keys (ok, summary, findings, data, truncated). Values come only from what the device server
already published to Redis; nothing here asks a device for a fresh value, and
compat.forbid_device_rpc() makes any accidental RPC fail instead of reaching the scan queue.
"""

from __future__ import annotations

import difflib
import functools
import math
import numbers
import time
from collections import Counter
from collections.abc import Callable
from typing import Annotated, Any, Literal

import numpy as np
from bec_lib.device import AdjustableMixin, ComputedSignal, DeviceBase, Positioner, Signal
from pydantic import Field

from bec_mcp_server.bec import compat
from bec_mcp_server.bec.connection import BecConnection
from bec_mcp_server.bec.digest import MAX_INLINE_SEQUENCE, age_s, first_line, iso_time, jsonable
from bec_mcp_server.tools import READ_ONLY_TOOL, BecCall, tool_result

#: default and maximum page size of bec_devices; beamlines run hundreds of devices
DEFAULT_DEVICE_LIMIT = 100
MAX_DEVICE_LIMIT = 500

#: default and maximum number of signals bec_device_info lists
DEFAULT_SIGNAL_LIMIT = 50
MAX_SIGNAL_LIMIT = 200

#: readings per section (readback, configuration) returned by bec_device_read
MAX_READINGS = 100

#: user methods and attributes listed by bec_device_info
MAX_USER_ACCESS = 50

#: strings longer than this are cut; device values and docs are written by other people
MAX_TEXT_CHARS = 500

DeviceType = Literal["positioner", "signal", "computed_signal", "device"]
ReadoutPriority = Literal["monitored", "baseline", "async", "continuous", "on_request"]
SignalKind = Literal["hinted", "normal", "config", "omitted"]

_KIND_ORDER = {"hinted": 0, "normal": 1, "config": 2, "omitted": 3}


def _cached_only(func: Callable[..., dict]) -> Callable[..., dict]:
    """Run a tool body with device RPCs refused, in whatever thread executes it."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> dict:
        with compat.forbid_device_rpc():
            return func(*args, **kwargs)

    return wrapper


def _safe(getter: Callable[[], Any], default: Any = None) -> Any:
    """Read one public field; a malformed config or a refused RPC must not sink the whole call."""
    try:
        return getter()
    except Exception:  # pylint: disable=broad-except
        return default


def _call(obj: Any, method: str, default: Any = None) -> Any:
    """Call one public no-argument method the same forgiving way _safe reads a field."""
    try:
        return getattr(obj, method)()
    except Exception:  # pylint: disable=broad-except
        return default


def _text(value: Any, max_chars: int = MAX_TEXT_CHARS) -> str | None:
    if value is None:
        return None
    text = str(value)
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}… ({len(text)} chars)"


def _timestamp(value: Any) -> float | None:
    """A usable unix timestamp, or None for missing, boolean or non-finite values."""
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        return None
    return float(value) if math.isfinite(float(value)) else None


def _format_age(seconds: float | None) -> str:
    if seconds is None:
        return "unknown age"
    if seconds < 60:
        return f"{seconds:.0f} s old"
    if seconds < 3600:
        return f"{seconds / 60:.1f} min old"
    if seconds < 86400:
        return f"{seconds / 3600:.1f} h old"
    return f"{seconds / 86400:.1f} d old"


def _value_digest(value: Any) -> Any:
    """Small values inline; long arrays and numeric lists become shape plus statistics."""
    if isinstance(value, str):
        return _text(value)
    if (
        isinstance(value, (list, tuple))
        and len(value) > MAX_INLINE_SEQUENCE
        and all(isinstance(item, numbers.Real) and not isinstance(item, bool) for item in value)
    ):
        return jsonable(np.asarray(value, dtype=float))
    return jsonable(value)


def _device_manager(client: Any) -> Any:
    device_manager = getattr(client, "device_manager", None)
    if device_manager is None:
        raise ConnectionError("the BEC client has no device manager; BEC is not connected")
    return device_manager


def _snapshot(client: Any) -> dict[str, Any]:
    # the container is mutated by config-update callbacks on another thread
    return dict(_device_manager(client).devices)


def _device_type(device: Any) -> str:
    if isinstance(device, Positioner):
        return "positioner"
    if isinstance(device, ComputedSignal):
        return "computed_signal"
    if isinstance(device, Signal):
        return "signal"
    if isinstance(device, DeviceBase):
        return "device"
    return compat.device_info(device).get("device_base_class") or "device"


def _resolve(devices: dict[str, Any], name: str) -> tuple[str | None, str | None]:
    """Find a device by exact, case-insensitive or dotted name; return (device, signal path)."""
    name = (name or "").strip()
    folded = {key.casefold(): key for key in devices}
    if name in devices:
        return name, None
    if name.casefold() in folded:
        return folded[name.casefold()], None
    root, _, rest = name.partition(".")
    if rest:
        if root in devices:
            return root, rest
        if root.casefold() in folded:
            return folded[root.casefold()], rest
    return None, None


def _not_found(name: str, devices: dict[str, Any]) -> LookupError:
    """The error an unknown device name raises; the runtime hands its text to the agent."""
    close = difflib.get_close_matches(name, sorted(devices), n=5, cutoff=0.6)
    message = f"Device '{name}' not found"
    if close:
        message += f"; similar: {', '.join(close)}"
    return LookupError(f"{message}. Call bec_devices to list all {len(devices)} devices.")


def _no_device(devices: dict[str, Any]) -> dict:
    """The answer to a bare call: which argument is missing and where to find names."""
    example = min(devices) if devices else "samx"
    return tool_result(
        False,
        f"Name a device, e.g. device='{example}'; bec_devices lists all {len(devices)} devices.",
        data={"device_count": len(devices)},
    )


def _device_row(name: str, device: Any) -> dict[str, Any]:
    config = compat.device_config(device) or {}
    readout_priority = _safe(lambda: device.readout_priority)
    row: dict[str, Any] = {
        "name": name,
        "type": _device_type(device),
        "device_class": config.get("deviceClass"),
        "enabled": _safe(lambda: bool(device.enabled)),
        "readout_priority": getattr(readout_priority, "value", readout_priority),
        "tags": sorted(str(tag) for tag in (_call(device, "get_device_tags") or ())),
    }
    description = _safe(lambda: device.description)
    if description:
        row["description"] = _text(description, 200)
    return row


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _row_matches(row: dict[str, Any], filters: dict[str, Any]) -> bool:
    if filters["prefix"] and not row["name"].casefold().startswith(filters["prefix"].casefold()):
        return False
    if filters["enabled_only"] and not row["enabled"]:
        return False
    if filters["tag"] and filters["tag"].casefold() not in {t.casefold() for t in row["tags"]}:
        return False
    if filters["readout_priority"] and row["readout_priority"] != filters["readout_priority"]:
        return False
    return not filters["device_type"] or row["type"] == filters["device_type"]


def _list_summary(
    rows: list[dict], device_count: int, filters: dict[str, Any], offset: int, returned: int
) -> str:
    active = ", ".join(f"{key}={value}" for key, value in filters.items() if value)
    if not rows:
        return f"No devices match {active or 'the request'}; {device_count} devices in total."
    by_type = Counter(row["type"] for row in rows)
    kinds = ", ".join(_plural(count, kind) for kind, count in sorted(by_type.items()))
    if active:
        summary = f"{len(rows)} of {device_count} devices match {active}: {kinds}."
    else:
        summary = f"{_plural(len(rows), 'device')}: {kinds}."
    if not returned:
        return f"{summary} Offset {offset} is past the last match."
    summary += f" Showing {offset + 1}-{offset + returned}."
    if offset + returned < len(rows):
        summary += f" Call again with offset={offset + returned} for more."
    return summary


@_cached_only
def list_devices(  # pylint: disable=too-many-arguments
    client: Any,
    *,
    prefix: str | None = None,
    tag: str | None = None,
    readout_priority: str | None = None,
    device_type: str | None = None,
    enabled_only: bool = False,
    limit: int = DEFAULT_DEVICE_LIMIT,
    offset: int = 0,
) -> dict:
    """List devices with type, class, enabled flag, readout priority and tags, one page at a time.

    Args:
        client (Any): The BECClient lent by the connection.
        prefix (str | None): Keep devices whose name starts with this (case-insensitive).
        tag (str | None): Keep devices carrying this device tag (case-insensitive).
        readout_priority (str | None): Keep devices with this readout priority.
        device_type (str | None): Keep positioners, signals, computed_signals or devices.
        enabled_only (bool): Drop disabled devices.
        limit (int): Page size, capped at MAX_DEVICE_LIMIT.
        offset (int): Index of the first matching device to return.

    Returns:
        dict: The digest; data holds total, offset, next_offset, by_type and devices.
    """
    devices = _snapshot(client)
    filters = {
        "prefix": prefix,
        "tag": tag,
        "readout_priority": readout_priority,
        "device_type": device_type,
        "enabled_only": enabled_only or None,
    }
    rows = [
        row
        for row in (_device_row(name, device) for name, device in sorted(devices.items()))
        if _row_matches(row, filters)
    ]
    limit = max(1, min(int(limit), MAX_DEVICE_LIMIT))
    offset = max(0, int(offset))
    page = rows[offset : offset + limit]
    next_offset = offset + len(page) if offset + len(page) < len(rows) else None
    data = {
        "total": len(rows),
        "offset": offset,
        "returned": len(page),
        "next_offset": next_offset,
        "by_type": dict(sorted(Counter(row["type"] for row in rows).items())),
        "devices": page,
    }
    summary = _list_summary(rows, len(devices), filters, offset, len(page))
    return tool_result(True, summary, data=data, truncated=next_offset is not None)


def _signature(params: Any) -> str:
    if not isinstance(params, list):
        return "(...)"
    parts = []
    for param in params:
        if not isinstance(param, dict):
            continue
        kind = param.get("kind")
        text = {"VAR_POSITIONAL": "*", "VAR_KEYWORD": "**"}.get(kind, "") + str(param.get("name"))
        annotation = param.get("annotation")
        if annotation not in (None, "_empty"):
            text += f": {annotation if isinstance(annotation, str) else jsonable(annotation)}"
        default = param.get("default", "_empty")
        if default != "_empty" and kind not in ("VAR_POSITIONAL", "VAR_KEYWORD"):
            text += f"={default!r}"
        parts.append(text)
    return _text(f"({', '.join(parts)})", 300) or "()"


def _user_access(entries: Any, prefix: str = "") -> tuple[list[dict], list[str]]:
    """Flatten the device server's custom user-access tree into methods and attributes."""
    methods: list[dict] = []
    attributes: list[str] = []
    if not isinstance(entries, dict):
        return methods, attributes
    for name, entry in sorted(entries.items()):
        if not isinstance(entry, dict):
            continue
        full_name = f"{prefix}{name}"
        if entry.get("type") == "func":
            method = {"name": full_name, "signature": _signature(entry.get("signature"))}
            doc = first_line(entry.get("doc"))
            if doc:
                method["doc"] = _text(doc, 200)
            methods.append(method)
        elif "info" in entry:
            # a nested object such as a controller or the simulation namespace
            nested_methods, nested_attributes = _user_access(entry["info"], f"{full_name}.")
            methods.extend(nested_methods)
            attributes.extend(nested_attributes)
        else:
            attributes.append(full_name)
    return methods, attributes


def _access(read: Any, write: Any) -> str | None:
    if read is None and write is None:
        return None
    return {
        (True, True): "read-write",
        (True, False): "read-only",
        (False, True): "write-only",
        (False, False): "none",
    }[(bool(read) if read is not None else True, bool(write))]


def _compact(row: dict[str, Any]) -> dict[str, Any]:
    return {field: value for field, value in row.items() if value not in (None, "", [])}


def _signal_rows(device: Any, info: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per signal: the reading key, kind, dtype/shape, units and access."""
    rows: list[dict[str, Any]] = []
    for component, signal in (info.get("signals") or {}).items():
        if not isinstance(signal, dict):
            continue
        describe = signal.get("describe") or {}
        metadata = signal.get("metadata") or {}
        row = {
            "name": component,
            "key": signal.get("obj_name"),
            "kind": signal.get("kind_str"),
            "class": signal.get("signal_class"),
            "dtype": describe.get("dtype"),
            "shape": jsonable(describe.get("shape")),
            "units": describe.get("units") or metadata.get("units"),
            "precision": describe.get("precision"),
            "access": _access(metadata.get("read_access"), metadata.get("write_access")),
            "source": _text(describe.get("source"), 200),
            "doc": _text(first_line(signal.get("doc")), 200),
        }
        if metadata.get("connected") is False:
            row["connected"] = False
        rows.append(_compact(row))

    if not rows:
        # a plain signal or computed signal is its own (only) signal: describe() lists it
        hints = set((info.get("hints") or {}).get("fields") or [])
        describe = _call(device, "describe", {}) or {}
        for reading_key, entry in describe.items():
            entry = entry if isinstance(entry, dict) else {}
            row = {
                "name": reading_key,
                "key": reading_key,
                "kind": "hinted" if reading_key in hints else "normal",
                "dtype": entry.get("dtype"),
                "shape": jsonable(entry.get("shape")),
                "units": entry.get("units"),
                "precision": entry.get("precision"),
                "access": _access(info.get("read_access"), info.get("write_access")),
                "source": _text(entry.get("source"), 200),
            }
            rows.append(_compact(row))

    rows.sort(key=lambda row: (_KIND_ORDER.get(row.get("kind"), 9), row["name"]))
    return rows


def _info_summary(data: dict[str, Any], method_count: int, limit: int) -> str:
    state = "enabled" if data["enabled"] else "disabled"
    kinds = ", ".join(f"{count} {kind}" for kind, count in data["signal_kinds"].items())
    summary = (
        f"{data['name']} is a {data['type']} ({data['device_class'] or data['ophyd_class']}), "
        f"{state}, readout priority {data['readout_priority']}; "
        f"{_plural(data['signal_count'], 'signal')}" + (f" ({kinds})" if kinds else "")
    )
    if method_count:
        summary += f", {_plural(method_count, 'user method')}"
    summary += "."
    if data["signal_count"] > limit:
        summary += f" Showing the first {limit} signals; raise limit or filter by kind."
    return summary


@_cached_only
def device_info(
    client: Any,
    device: str | None = None,
    *,
    kind: str | None = None,
    limit: int = DEFAULT_SIGNAL_LIMIT,
) -> dict:
    """Describe one device: public config fields, signals with kinds and units, user methods.

    Args:
        client (Any): The BECClient lent by the connection.
        device (str): Device name; case-insensitive, a dotted signal path resolves to its device.
        kind (str | None): Only list signals of this kind (hinted, normal, config, omitted).
        limit (int): Maximum number of signals listed, capped at MAX_SIGNAL_LIMIT.

    Returns:
        dict: The digest; data holds the device fields, signals, user_methods and device_config.
    """
    devices = _snapshot(client)
    if not device:
        return _no_device(devices)
    name, _ = _resolve(devices, device)
    if name is None:
        raise _not_found(device, devices)
    obj = devices[name]
    info = compat.device_info(obj)
    config = compat.device_config(obj) or {}

    signals = [row for row in _signal_rows(obj, info) if not kind or row.get("kind") == kind]
    limit = max(1, min(int(limit), MAX_SIGNAL_LIMIT))
    methods, attributes = _user_access(info.get("custom_user_access"))
    data: dict[str, Any] = {
        **_device_row(name, obj),
        "ophyd_class": info.get("device_class"),
        "on_failure": getattr(_safe(lambda: obj.on_failure), "value", None),
        "read_only": _safe(lambda: bool(obj.read_only)),
        "software_trigger": _safe(lambda: bool(obj.software_trigger)),
        "egu": (_call(obj, "describe_configuration", {}) or {}).get("egu"),
        "hinted_signals": list((info.get("hints") or {}).get("fields") or []),
        "signal_count": len(signals),
        "signal_kinds": dict(Counter(row.get("kind") or "unknown" for row in signals)),
        "signals": signals[:limit],
        "sub_devices": [
            sub.get("device_attr_name")
            for sub in info.get("sub_devices") or []
            if isinstance(sub, dict) and sub.get("device_attr_name")
        ],
        "user_methods": methods[:MAX_USER_ACCESS],
        "user_attributes": attributes[:MAX_USER_ACCESS],
        "device_config": jsonable(config.get("deviceConfig")),
    }
    if config.get("userParameter"):
        data["user_parameter"] = jsonable(config["userParameter"])
    if _access(info.get("read_access"), info.get("write_access")):
        data["access"] = _access(info.get("read_access"), info.get("write_access"))
    truncated = len(signals) > limit or max(len(methods), len(attributes)) > MAX_USER_ACCESS
    return tool_result(
        True, _info_summary(data, len(methods), limit), data=data, truncated=truncated
    )


def _signal_aliases(name: str, obj: Any, info: dict[str, Any]) -> dict[str, str]:
    """Map every accepted spelling of a signal to the key it has in readings."""
    aliases: dict[str, str] = {}
    for component, signal in (info.get("signals") or {}).items():
        if not isinstance(signal, dict):
            continue
        key = signal.get("obj_name") or f"{name}_{component.replace('.', '_')}"
        for alias in (component, component.replace(".", "_"), f"{name}.{component}", key):
            aliases.setdefault(alias, key)
    for described in (
        _call(obj, "describe", {}) or {},
        _call(obj, "describe_configuration", {}) or {},
    ):
        for key in described:
            if key == "egu":
                continue
            aliases.setdefault(key, key)
            aliases.setdefault(key.removeprefix(f"{name}_"), key)
    return aliases


def _readings(raw: Any, now: float, wanted: set[str] | None) -> tuple[dict[str, dict], int]:
    readings: dict[str, dict] = {}
    total = 0
    if not isinstance(raw, dict):
        return readings, total
    for key, entry in sorted(raw.items()):
        if wanted is not None and key not in wanted:
            continue
        total += 1
        if len(readings) >= MAX_READINGS:
            continue
        if isinstance(entry, dict):
            timestamp = entry.get("timestamp")
            value = entry.get("value")
        else:
            timestamp, value = None, entry
        timestamp = _timestamp(timestamp)
        readings[key] = {
            "value": _value_digest(value),
            "timestamp": iso_time(timestamp) if timestamp is not None else None,
            "age_s": age_s(timestamp, now),
        }
    return readings, total


def _limits(obj: Any) -> dict[str, Any] | None:
    if not isinstance(obj, AdjustableMixin):
        return None
    limits = _safe(lambda: obj.limits)
    if not isinstance(limits, (list, tuple)) or len(limits) != 2:
        return None
    low, high = (jsonable(value) for value in limits)
    if not all(isinstance(value, (int, float)) for value in (low, high)):
        return None
    # ophyd disables limit checks when low >= high (the default [0, 0] means "no limits")
    return {"low": low, "high": high} if low < high else None


def _short(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.6g}"
    if isinstance(value, dict) and "shape" in value:
        return f"array {value['shape']}"
    text = str(value)
    return text if len(text) <= 60 else f"{text[:60]}…"


def _select_signals(
    name: str, obj: Any, info: dict[str, Any], requested: list[str]
) -> tuple[set[str] | None, list[str]]:
    """Reading keys for the requested signal names (None: all), and the names not found."""
    if not requested:
        return None, []
    aliases = _signal_aliases(name, obj, info)
    folded = {alias.casefold(): key for alias, key in aliases.items()}
    wanted: set[str] = set()
    unknown: list[str] = []
    for signal in requested:
        key = aliases.get(signal) or folded.get(signal.casefold())
        if key is None:
            unknown.append(signal)
        else:
            wanted.add(key)
    return wanted, unknown


def _read_summary(data: dict[str, Any], hinted: list[str], filtered: bool) -> str:
    name, readback = data["name"], data["readback"]
    configuration = data.get("configuration") or {}
    units = f" {data['egu']}" if data["egu"] else ""

    def reading_text(key: str, reading: dict) -> str:
        label = name if key == name else key
        unit_text = units if key in hinted else ""
        return f"{label} = {_short(reading['value'])}{unit_text} ({_format_age(reading['age_s'])})"

    parts = []
    if filtered:
        chosen = {**readback, **configuration}
        parts.extend(reading_text(key, chosen[key]) for key in list(chosen)[:3])
        if len(chosen) > 3:
            parts.append(f"{len(chosen) - 3} more in data")
        if not chosen:
            parts.append(f"No cached value for the requested signals of {name}")
    elif readback:
        primary = hinted[0] if hinted else next(iter(readback))
        parts.append(reading_text(primary, readback[primary]))
    elif configuration:
        count = _plural(len(configuration), "configuration value")
        parts.append(f"{name} has no cached readback; {count}")
    else:
        parts.append(f"{name} has no cached values")
    if data["limits"]:
        parts.append(f"limits {data['limits']['low']} to {data['limits']['high']}{units}")
    if data["enabled"] is False:
        parts.append("device is disabled, so values may be stale")
    summary = "; ".join(parts) + "."
    if data.get("unknown_signals"):
        summary += f" Unknown signals: {', '.join(data['unknown_signals'])}; see available_signals."
    if readback or configuration:
        summary += " Ages count from the last published update."
    return summary


def _add_readings(
    data: dict[str, Any], obj: Any, wanted: set[str] | None, with_config: bool
) -> bool:
    """Fill data["readback"] (and data["configuration"]); True when a section was capped."""
    now = time.time()
    # cached=True on the root device: the values the device server last published to Redis.
    # Errors propagate on purpose: an unreachable Redis must not read as "no cached values".
    data["readback"], total = _readings(obj.read(cached=True), now, wanted)
    truncated = total > len(data["readback"])
    if with_config:
        data["configuration"], total = _readings(obj.read_configuration(cached=True), now, wanted)
        truncated = truncated or total > len(data["configuration"])
    return truncated


@_cached_only
def device_read(
    client: Any,
    device: str | None = None,
    *,
    signals: list[str] | None = None,
    include_config: bool = True,
) -> dict:
    """Return one device's cached readback and configuration values with their age, and limits.

    Args:
        client (Any): The BECClient lent by the connection.
        device (str): Device name; "samx.velocity" reads that signal of samx.
        signals (list[str] | None): Only these signals (component or reading names).
        include_config (bool): Also return the cached configuration values.

    Returns:
        dict: The digest; data holds readback, configuration, limits and egu.
    """
    devices = _snapshot(client)
    if not device:
        return _no_device(devices)
    name, rest = _resolve(devices, device)
    if name is None:
        raise _not_found(device, devices)
    obj = devices[name]
    info = compat.device_info(obj)
    requested = list(signals or []) + ([rest] if rest else [])
    wanted, unknown = _select_signals(name, obj, info, requested)

    data: dict[str, Any] = {
        "name": name,
        "type": _device_type(obj),
        "enabled": _safe(lambda: bool(obj.enabled)),
        "egu": (_call(obj, "describe_configuration", {}) or {}).get("egu"),
        "limits": _limits(obj),
    }
    truncated = _add_readings(data, obj, wanted, with_config=include_config or bool(requested))
    if unknown:
        data["unknown_signals"] = unknown
        data["available_signals"] = sorted(set(_signal_aliases(name, obj, info).values()))[
            :MAX_READINGS
        ]
    hinted = [
        field
        for field in (info.get("hints") or {}).get("fields") or []
        if field in data["readback"]
    ]
    return tool_result(
        True, _read_summary(data, hinted, filtered=bool(requested)), data=data, truncated=truncated
    )


def register(server: Any, connection: BecConnection, call: BecCall) -> None:
    """Expose bec_devices, bec_device_info and bec_device_read on ``server``.

    Args:
        server (Any): The mcp MCPServer instance.
        connection (BecConnection): Lends the client; ``connection.run(func, **kwargs)`` calls
            ``func(client, **kwargs)``.
        call (BecCall): Offloads a blocking call under the server's deadline and pool limits.
    """

    @server.tool(
        description="List BEC devices (motors, signals, detectors) with type, class, "
        "enabled flag and tags; filter by name prefix, tag, type or readout priority, "
        "and page with offset.",
        annotations=READ_ONLY_TOOL,
    )
    async def bec_devices(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        prefix: Annotated[str | None, Field(description="Name prefix, e.g. 'sam'")] = None,
        tag: Annotated[str | None, Field(description="Device tag")] = None,
        readout_priority: ReadoutPriority | None = None,
        device_type: Annotated[
            DeviceType | None, Field(description="'positioner' lists the motors")
        ] = None,
        enabled_only: bool = False,
        limit: Annotated[int, Field(ge=1, le=MAX_DEVICE_LIMIT)] = DEFAULT_DEVICE_LIMIT,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> dict[str, Any]:
        return await call(
            connection.run,
            list_devices,
            prefix=prefix,
            tag=tag,
            readout_priority=readout_priority,
            device_type=device_type,
            enabled_only=enabled_only,
            limit=limit,
            offset=offset,
        )

    @server.tool(
        description="Describe one BEC device: class, enabled flag, readout priority, its "
        "signals with kind, units and read/write access, and its user methods.",
        annotations=READ_ONLY_TOOL,
    )
    async def bec_device_info(
        device: Annotated[str | None, Field(description="Device name, e.g. 'samx'")] = None,
        kind: Annotated[SignalKind | None, Field(description="Only signals of this kind")] = None,
        limit: Annotated[int, Field(ge=1, le=MAX_SIGNAL_LIMIT)] = DEFAULT_SIGNAL_LIMIT,
    ) -> dict[str, Any]:
        return await call(connection.run, device_info, device=device, kind=kind, limit=limit)

    @server.tool(
        description="Read one BEC device's last published values (readback and configuration) "
        "with their age in seconds, plus limits and units; never polls the hardware.",
        annotations=READ_ONLY_TOOL,
    )
    async def bec_device_read(
        device: Annotated[str | None, Field(description="Device name, e.g. 'samx'")] = None,
        signals: Annotated[
            list[str] | None, Field(description="Only these signals, e.g. ['velocity']")
        ] = None,
        include_config: bool = True,
    ) -> dict[str, Any]:
        return await call(
            connection.run,
            device_read,
            device=device,
            signals=signals,
            include_config=include_config,
        )
