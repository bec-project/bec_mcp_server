"""Scan tools: which scans exist, how to call one, and what ran recently.

All three read cached state through the client's own connector: the ``available_scans``
payload the scan server publishes, and the ``scan_history`` stream. ``client.scans`` is
built from that same payload, but it only exposes each scan's run function (with a
signature stripped of the units, bounds and descriptions BEC 4 publishes), so reading the
payload keeps everything public without touching ``client.scans._available_scans``.
"""

from __future__ import annotations

import difflib
import math
import re
import time
from collections import Counter
from collections.abc import Iterator
from typing import TYPE_CHECKING, Annotated, Any

from bec_lib.device import Positioner
from bec_lib.endpoints import MessageEndpoints
from pydantic import Field

from bec_mcp_server.bec.connection import BecConnection
from bec_mcp_server.bec.digest import first_line, iso_time, jsonable
from bec_mcp_server.tools import READ_ONLY_TOOL, BecCall, Finding, tool_result

if TYPE_CHECKING:  # pragma: no cover
    from bec_lib.client import BECClient

#: exit statuses of scans that finished as intended; anything else is reported as a finding
CLEAN_EXIT_STATUSES = frozenset({"closed", "user_completed"})

#: bec_scan_history returns at most this many scans
MAX_HISTORY = 50

#: docstring text kept in bec_scan_help when the docstring has no Args section
MAX_DOC_CHARS = 1500

#: motor names preferred for example calls when the session has them
PREFERRED_MOTORS = ("samx", "samy", "samz")

_MISSING = "_empty"
_ARGS_LINE_RE = re.compile(r"^\s+\*{0,2}(\w+)\s*\(([^)]*)\):\s*(.*)$")
_DOC_SECTIONS = ("Args:", "Returns:", "Examples:", "Example:", "Raises:", "Notes:", "Note:")
_EXAMPLE_NOTE = (
    "Values are placeholders that satisfy the argument types and bounds; set real positions, "
    "steps and times before running."
)


# --------------------------------------------------------------------------------------
# Reading BEC
# --------------------------------------------------------------------------------------


def read_available_scans(client: BECClient) -> dict[str, dict[str, Any]] | None:
    """The scan server's published scans, internal ones dropped as the client drops them.

    None means nothing is published: the scan server has not started or Redis was flushed.
    """
    msg = client.connector.get(MessageEndpoints.available_scans())
    resource = getattr(msg, "resource", None)
    if not isinstance(resource, dict):
        return None
    return {
        name: info
        for name, info in resource.items()
        if isinstance(info, dict) and not info.get("is_internal")
    }


def read_scan_history(client: BECClient, count: int) -> list[Any]:
    """The newest ``count`` scan-history messages, newest first.

    Reads the stream rather than ``client.history``, which keeps only the newest 50 scans
    and silently drops scans whose file this host cannot read.
    """
    entries = client.connector.get_last(MessageEndpoints.scan_history(), count=count)
    if entries is None:
        return []
    if isinstance(entries, dict):  # get_last returns a bare entry when count == 1
        entries = [entries]
    messages = [entry.get("data") for entry in entries if isinstance(entry, dict)]
    return [msg for msg in reversed(messages) if msg is not None]


def example_motors(client: BECClient) -> list[str]:
    """Enabled positioners of the session, preferred names first. Cached config only."""
    try:
        devices = client.device_manager.devices.enabled_devices
    except AttributeError:
        return []
    names = sorted(dev.name for dev in devices if isinstance(dev, Positioner))
    preferred = [name for name in PREFERRED_MOTORS if name in names]
    return preferred + [name for name in names if name not in preferred]


# --------------------------------------------------------------------------------------
# Serialized annotations (bec_lib.signature_serializer)
# --------------------------------------------------------------------------------------


def _scan_argument(annotation: Any) -> dict[str, Any]:
    """The ScanArgument metadata BEC 4 attaches to an annotation, or an empty dict."""
    if isinstance(annotation, dict) and "Annotated" in annotation:
        metadata = annotation["Annotated"].get("metadata") or {}
        return metadata.get("ScanArgument") or {}
    if isinstance(annotation, list):
        for member in annotation:
            if meta := _scan_argument(member):
                return meta
    return {}


def _base_type(annotation: Any) -> Any:
    """The annotation without Annotated metadata and without None in a union."""
    if isinstance(annotation, dict) and "Annotated" in annotation:
        return _base_type(annotation["Annotated"].get("type"))
    if isinstance(annotation, list):
        members = [member for member in annotation if member != "NoneType"]
        return _base_type(members[0]) if members else "NoneType"
    return annotation


def render_type(annotation: Any) -> str:
    """A serialized annotation as a short Python-like type string."""
    if annotation in (None, _MISSING):
        return "any"
    if isinstance(annotation, str):
        return {"DeviceBase": "device", "NoneType": "None"}.get(annotation, annotation)
    if isinstance(annotation, list):
        return " | ".join(render_type(member) for member in annotation)
    if isinstance(annotation, dict):
        return _render_dict_type(annotation)
    return str(annotation)


def _render_dict_type(annotation: dict[str, Any]) -> str:
    if "Annotated" in annotation:
        return render_type(annotation["Annotated"].get("type"))
    if "Literal" in annotation:
        return f"Literal[{', '.join(repr(value) for value in annotation['Literal'])}]"
    if "Generic" in annotation:
        generic = annotation["Generic"]
        args = ", ".join(render_type(arg) for arg in generic.get("args") or [])
        return f"{render_type(generic.get('origin'))}[{args}]"
    return str(annotation)


def _constraint(meta: dict[str, Any]) -> str | None:
    parts = [
        f"{symbol} {_number(meta[key])}"
        for key, symbol in (("gt", ">"), ("ge", ">="), ("lt", "<"), ("le", "<="))
        if meta.get(key) is not None
    ]
    return ", ".join(parts) or None


def _number(value: Any) -> Any:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


# --------------------------------------------------------------------------------------
# Example calls
# --------------------------------------------------------------------------------------


def _clamp(value: float, meta: dict[str, Any], integer: bool) -> float:
    step = 1 if integer else 0.1
    if meta.get("ge") is not None:
        value = max(value, meta["ge"])
    if meta.get("gt") is not None and value <= meta["gt"]:
        value = meta["gt"] + step
    if meta.get("le") is not None:
        value = min(value, meta["le"])
    if meta.get("lt") is not None and value >= meta["lt"]:
        value = meta["lt"] - step
    return value


#: example floats by words in the argument name, first match wins; times in seconds get 0.1 s
_FLOAT_BY_NAME = (
    (("start",), -1.0),
    (("stop", "end", "target"), 1.0),
    (("inner",), 0.5),
    (("outer",), 2.0),
)
_FLOAT_STEP_WORDS = ("step", "spacing", "width")


def _float_guess(name: str, meta: dict[str, Any]) -> float:
    lowered = name.lower()
    for words, value in _FLOAT_BY_NAME:
        if any(word in lowered for word in words):
            return value
    if meta.get("units") == "s":
        return 0.1
    return 0.5 if any(word in lowered for word in _FLOAT_STEP_WORDS) else 1.0


def _float_value(name: str, meta: dict[str, Any]) -> float:
    guess = _float_guess(name, meta)
    low = meta.get("ge") if meta.get("ge") is not None else meta.get("gt")
    stop_like = any(word in name.lower() for word in ("stop", "end"))
    if low is not None and guess <= low and stop_like:
        # a range with a positive floor (an energy in eV): stop a little above the floor so
        # it does not land on the same value as start
        guess = low * 1.1 if low > 0 else low + 1.0
    return _clamp(guess, meta, integer=False)


def _int_guess(name: str) -> int:
    lowered = name.lower()
    if "start" in lowered:
        return 0
    if any(word in lowered for word in ("stop", "end", "step", "point", "num")):
        return 10
    return 3


def example_literal(  # pylint: disable=too-many-return-statements
    name: str, annotation: Any, motors: Iterator[str]
) -> str:
    """Python source for one example argument value, built from its serialized annotation."""
    meta = _scan_argument(annotation)
    if meta.get("example") is not None:
        return repr(meta["example"])
    base = _base_type(annotation)
    if base == "DeviceBase":
        return f"dev.{next(motors)}"
    if isinstance(base, dict) and "Literal" in base:
        options = [value for value in base["Literal"] if value is not None]
        return repr(options[0]) if options else "None"
    if isinstance(base, dict) and "Generic" in base:
        return _generic_literal(name, base["Generic"], motors)
    if base == "bool":
        return "True" if name == "relative" else "False"
    if base == "int":
        return repr(int(_clamp(_int_guess(name), meta, integer=True)))
    if base == "float":
        return repr(float(_float_value(name, meta)))
    if base == "str":
        return repr(name)
    return "..."


def _generic_literal(name: str, generic: dict[str, Any], motors: Iterator[str]) -> str:
    origin = generic.get("origin")
    args = generic.get("args") or []
    if origin == "list":
        element = args[0] if args else "float"
        if _base_type(element) in ("float", "int"):
            return "[-1.0, 0.0, 1.0]" if _base_type(element) == "float" else "[1, 2, 3]"
        return f"[{example_literal(name, element, motors)}]"
    if origin == "tuple":
        # positional tuples such as (start, stop, steps): floats run -1.0, 1.0, 2.0, ...
        floats = iter((-1.0, 1.0, 2.0, 3.0, 4.0))
        values = []
        for element in args:
            if _base_type(element) == "float":
                values.append(repr(next(floats)))
            else:
                values.append(example_literal(name, element, motors))
        return f"({', '.join(values)}{',' if len(values) == 1 else ''})"
    if origin == "dict":
        return "{}"
    return "..."


class _MotorSupply:
    """Motor names for an example: the session's own first, then placeholders it records."""

    def __init__(self, motors: list[str]) -> None:
        self._motors = motors
        self._fallback = [] if motors else list(PREFERRED_MOTORS)
        self._index = 0
        self.placeholders: list[str] = []

    def __iter__(self) -> _MotorSupply:
        return self

    def __next__(self) -> str:
        index, self._index = self._index, self._index + 1
        if index < len(self._motors):
            return self._motors[index]
        name = self._fallback[index] if index < len(self._fallback) else f"motor{index + 1}"
        self.placeholders.append(name)
        return name


def _required(param: dict[str, Any]) -> bool:
    return param.get("default", _MISSING) == _MISSING


def example_call(name: str, info: dict[str, Any], motors: list[str]) -> tuple[str, list[str]]:
    """The shortest valid call, using the session's motors where it needs devices.

    Returns the call and the placeholder motor names it had to invent, if any.
    """
    supply = _MotorSupply(motors)
    parts: list[str] = []
    bundle = info.get("arg_input") or {}
    bundle_size = info.get("arg_bundle_size") or {}
    if bundle and bundle_size.get("bundle"):
        for _ in range(bundle_size.get("min") or 1):
            parts.extend(
                example_literal(arg_name, arg_type, supply) for arg_name, arg_type in bundle.items()
            )
    for param in info.get("signature") or []:
        kind = param.get("kind")
        if kind in ("VAR_POSITIONAL", "VAR_KEYWORD") or not _required(param):
            continue
        value = example_literal(param["name"], param.get("annotation"), supply)
        parts.append(f"{param['name']}={value}" if kind == "KEYWORD_ONLY" else value)
    return f"scans.{name}({', '.join(parts)})", supply.placeholders


# --------------------------------------------------------------------------------------
# Digests
# --------------------------------------------------------------------------------------


def _doc_parts(doc: str | None) -> tuple[str, dict[str, str]]:
    """The docstring's prose before its sections, and the descriptions in its Args section."""
    text = (doc or "").strip()
    lines = text.splitlines()
    prose: list[str] = []
    arg_docs: dict[str, str] = {}
    section = None
    for line in lines:
        stripped = line.strip()
        if stripped in _DOC_SECTIONS:
            section = stripped
            continue
        if section is None:
            prose.append(line)
        elif section == "Args:" and (match := _ARGS_LINE_RE.match(line)):
            arg_docs[match.group(1)] = match.group(3).strip()
    if section is None:
        return text[:MAX_DOC_CHARS], arg_docs
    return "\n".join(prose).strip(), arg_docs


def _argument(param: dict[str, Any], kind: str, doc: str | None) -> dict[str, Any]:
    """One argument from a serialized signature parameter (name, annotation, default)."""
    annotation = param.get("annotation")
    meta = _scan_argument(annotation)
    required = _required(param)
    entry: dict[str, Any] = {
        "name": param["name"],
        "type": render_type(annotation),
        "kind": kind,
        "required": required,
    }
    if not required:
        entry["default"] = jsonable(param.get("default"))
    optional = {
        "units": meta.get("units"),
        "constraint": _constraint(meta),
        "description": meta.get("description") or doc,
        "expert": meta.get("expert") or None,
    }
    entry.update({key: value for key, value in optional.items() if value})
    return entry


def scan_arguments(info: dict[str, Any]) -> list[dict[str, Any]]:
    """Every argument a user can pass, in call order, with types, defaults and units."""
    _, arg_docs = _doc_parts(info.get("doc"))
    arguments: list[dict[str, Any]] = []
    bundle = info.get("arg_input") or {}
    if bundle and (info.get("arg_bundle_size") or {}).get("bundle"):
        for arg_name, arg_type in bundle.items():
            param = {"name": arg_name, "annotation": arg_type}
            arguments.append(_argument(param, "repeated", arg_docs.get(arg_name)))
    for param in info.get("signature") or []:
        kind = param.get("kind")
        if kind in ("VAR_POSITIONAL", "VAR_KEYWORD"):
            continue
        if _scan_argument(param.get("annotation")).get("hidden") and not _required(param):
            continue
        kind = "keyword" if kind == "KEYWORD_ONLY" else "positional"
        arguments.append(_argument(param, kind, arg_docs.get(param["name"])))
    return arguments


def _repeat_rule(info: dict[str, Any]) -> str | None:
    bundle = info.get("arg_input") or {}
    size = info.get("arg_bundle_size") or {}
    if not bundle or not size.get("bundle"):
        return None
    low, high = size.get("min") or 1, size.get("max")
    group = ", ".join(bundle)
    if high is None:
        return f"pass ({group}) as a group, at least {low} time{'s' if low != 1 else ''}"
    if high == low:
        return f"pass ({group}) as a group, exactly {low} time{'s' if low != 1 else ''}"
    return f"pass ({group}) as a group, {low} to {high} times"


def _history_entry(msg: Any) -> dict[str, Any]:
    start = getattr(msg, "start_time", None)
    end = getattr(msg, "end_time", None)
    duration = None
    if isinstance(start, (int, float)) and isinstance(end, (int, float)):
        duration = round(end - start, 1) if math.isfinite(end - start) else None
    entry = {
        "scan_number": getattr(msg, "scan_number", None),
        "scan_name": getattr(msg, "scan_name", None),
        "status": getattr(msg, "exit_status", None),
        "reason": getattr(msg, "reason", None),
        "start_time": iso_time(start),
        "duration_s": duration,
        "num_points": getattr(msg, "num_points", None),
        "file_path": getattr(msg, "file_path", None),
    }
    return {key: value for key, value in entry.items() if value is not None}


# --------------------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------------------


def _not_published() -> dict[str, Any]:
    finding = Finding(
        "error",
        "scans_not_published",
        "The scan server has not published any scans; it may not be running.",
        subject="ScanServer",
        next_tool="bec_status",
    )
    return tool_result(
        False,
        "The scan server has not published any scans. It may not be running; "
        "call bec_status to check.",
        findings=[finding],
        data={"scans": []},
    )


def _history_finding(msg: Any, now: float) -> Finding | None:
    """A finding for a scan that did not finish as intended, or None."""
    status = getattr(msg, "exit_status", None)
    if status in CLEAN_EXIT_STATUSES:
        return None
    number, name = getattr(msg, "scan_number", None), getattr(msg, "scan_name", None)
    subject = f"scan {number}"
    if getattr(msg, "reason", None) == "user":
        return Finding(
            "info",
            "scan_stopped_by_user",
            f"Scan {number} ({name}) was {status} by a user.",
            subject=subject,
        )
    cause = " after an alarm" if getattr(msg, "reason", None) == "alarm" else ""
    start = getattr(msg, "start_time", None)
    minutes = 60
    if isinstance(start, (int, float)) and math.isfinite(start):
        # a log window reaching back to the scan's start, so its errors are included
        minutes = max(5, math.ceil((now - start) / 60) + 1)
    return Finding(
        "warning",
        "scan_not_closed",
        f"Scan {number} ({name}) ended {status}{cause}.",
        subject=subject,
        next_tool="bec_logs",
        next_args={"min_level": "warning", "minutes": minutes},
    )


def scan_list_digest(client: BECClient, name_contains: str = "") -> dict[str, Any]:
    """Scan names with one-line descriptions, sorted by name."""
    scans = read_available_scans(client)
    if scans is None:
        return _not_published()
    needle = name_contains.strip().lower()
    rows = [
        {
            "name": name,
            "description": first_line(info.get("doc")),
            "is_scan": bool(info.get("is_scan", True)),
        }
        for name, info in sorted(scans.items())
        if needle in name.lower()
    ]
    moves = [row["name"] for row in rows if not row["is_scan"]]
    if not rows:
        summary = f"No scan name contains {name_contains!r}; {len(scans)} scans are available."
    else:
        summary = f"{len(rows)} scans available"
        if needle:
            summary += f" matching {name_contains!r}"
        if moves:
            summary += f", of which {', '.join(moves)} only move devices and record no data"
        summary += ". Call bec_scan_help with a name for its arguments and an example."
    return tool_result(True, summary, data={"scans": rows})


def _normalize_scan_name(scan_name: str) -> str:
    name = scan_name.strip()
    if name.startswith("scans."):
        name = name[len("scans.") :]
    return name.split("(", 1)[0].strip()


def _scan_not_found(scan_name: str, name: str, scans: dict[str, Any]) -> LookupError:
    close = difflib.get_close_matches(name, list(scans), n=3, cutoff=0.5)
    hint = f" Similar: {', '.join(close)}." if close else ""
    return LookupError(f"Scan {scan_name!r} not found.{hint} Call bec_scans to list all scans.")


def _placeholder_finding(scan: str, motors: list[str], placeholders: list[str]) -> Finding:
    names = ", ".join(f"dev.{placeholder}" for placeholder in placeholders)
    return Finding(
        "info",
        "example_placeholder_motors",
        f"This session has {len(motors)} enabled motor{'s' if len(motors) != 1 else ''}, so "
        f"the example uses placeholders ({names}); replace them with real motors.",
        subject=scan,
        next_tool="bec_devices",
        next_args={"device_type": "positioner", "enabled_only": True},
    )


def scan_help_digest(client: BECClient, scan_name: str) -> dict[str, Any]:
    """One scan's description, arguments and an example call for the BEC client."""
    scans = read_available_scans(client)
    if scans is None:
        return _not_published()
    name = _normalize_scan_name(scan_name)
    info = scans.get(name)
    if info is None:
        raise _scan_not_found(scan_name, name, scans)
    motors = example_motors(client)
    example, placeholders = example_call(name, info, motors)
    data: dict[str, Any] = {
        "name": name,
        "description": _doc_parts(info.get("doc"))[0],
        "is_scan": bool(info.get("is_scan", True)),
        "arguments": scan_arguments(info),
        "example": example,
        "example_note": _EXAMPLE_NOTE,
    }
    if repeat := _repeat_rule(info):
        data["repeat"] = repeat
    findings = [_placeholder_finding(name, motors, placeholders)] if placeholders else []
    summary = f"{name}: {first_line(info.get('doc'))} Example: {example}"
    return tool_result(True, summary, findings=findings, data=data)


def scan_history_digest(
    client: BECClient, limit: int = 10, now: float | None = None
) -> dict[str, Any]:
    """The newest finished scans with number, status and duration."""
    limit = max(1, min(int(limit), MAX_HISTORY))
    now = time.time() if now is None else now
    messages = read_scan_history(client, limit + 1)
    truncated = len(messages) > limit
    messages = messages[:limit]
    rows = [_history_entry(msg) for msg in messages]
    if not rows:
        return tool_result(True, "No finished scans in the scan history.", data={"scans": []})
    statuses = Counter(row.get("status", "unknown") for row in rows)
    counts = ", ".join(f"{count} {status}" for status, count in statuses.most_common())
    first, last = rows[-1].get("scan_number"), rows[0].get("scan_number")
    findings = [finding for msg in messages if (finding := _history_finding(msg, now))]
    summary = f"Last {len(rows)} scans (numbers {first} to {last}): {counts}."
    return tool_result(True, summary, findings=findings, data={"scans": rows}, truncated=truncated)


# --------------------------------------------------------------------------------------
# Tools: plain functions taking the connection, and their MCP registration
# --------------------------------------------------------------------------------------


def list_scans(connection: BecConnection, name_contains: str = "") -> dict[str, Any]:
    """bec_scans: scan names with one-line descriptions."""
    return connection.run(scan_list_digest, name_contains)


def scan_help(connection: BecConnection, scan_name: str = "line_scan") -> dict[str, Any]:
    """bec_scan_help: one scan's arguments and an example call; unknown names raise LookupError."""
    return connection.run(scan_help_digest, scan_name)


def scan_history(connection: BecConnection, limit: int = 10) -> dict[str, Any]:
    """bec_scan_history: the newest finished scans."""
    return connection.run(scan_history_digest, limit)


def register(server: Any, connection: BecConnection, call: BecCall) -> None:
    """Expose bec_scans, bec_scan_help and bec_scan_history; ``call`` offloads under the deadline."""

    @server.tool(
        name="bec_scans",
        description="List the scans this BEC instance offers, with a one-line description each.",
        annotations=READ_ONLY_TOOL,
    )
    async def bec_scans(
        name_contains: Annotated[str, Field(description="Part of a scan name, e.g. 'line'")] = "",
    ) -> dict[str, Any]:
        return await call(list_scans, connection, name_contains)

    @server.tool(
        name="bec_scan_help",
        description="Explain how to run one scan: its arguments with units and defaults, and an "
        "example call to paste into the BEC client.",
        annotations=READ_ONLY_TOOL,
    )
    async def bec_scan_help(
        scan_name: Annotated[
            str, Field(description="Scan name from bec_scans, e.g. 'grid_scan'")
        ] = "line_scan",
    ) -> dict[str, Any]:
        return await call(scan_help, connection, scan_name)

    @server.tool(
        name="bec_scan_history",
        description="List the most recent finished scans, newest first, with number, status and "
        "duration; file paths are for the user and cannot be opened here.",
        annotations=READ_ONLY_TOOL,
    )
    async def bec_scan_history(
        limit: Annotated[int, Field(ge=1, le=MAX_HISTORY, description="Scans to list")] = 10,
    ) -> dict[str, Any]:
        return await call(scan_history, connection, limit)
