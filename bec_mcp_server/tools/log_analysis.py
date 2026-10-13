"""``bec_log_range`` and ``bec_log_analysis``: read the log closely, and explain its errors.

Both work on the server's log cache (``bec/log_cache.py``), whose records carry a stable ``id``.

- ``bec_log_range`` returns a contiguous slice of records in full: around one record, between two
  ids or two times, or the newest ones. A record with a Python traceback in its message gets it
  parsed: the exception chain, each frame's file, line, function and code, and the frame in BEC
  or plugin code where it started (``tools/tracebacks.py``).
- ``bec_log_analysis`` groups the errors, tracebacks and alarms in the cache into incidents, one
  per distinct failure, each with its count and time span, the records just before its first
  occurrence, the devices and scan it names, the incidents around it, and a likely cause with a
  suggested next step from a fixed table of rules.

The rules only name the usual suspects for a known kind of failure; the agent reads the records
and draws the conclusion. Everything here is deterministic and read-only.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from datetime import time as day_time
from datetime import timezone
from typing import Annotated, Any, Literal

from pydantic import BeforeValidator, Field

from bec_mcp_server.bec.connection import BecConnection
from bec_mcp_server.bec.digest import iso_time
from bec_mcp_server.bec.log_stream import LOG_LEVELS, LogRecord, parse_log_message
from bec_mcp_server.tools import READ_ONLY_TOOL, BecCall, Finding, tool_result
from bec_mcp_server.tools.log_rules import UNCLASSIFIED, Rule, classify, devices_named
from bec_mcp_server.tools.logs import (
    LogLevelName,
    LogSource,
    _age,
    _clip,
    _lowercase,
    message_shape,
    parse_alarm_line,
    read_logs,
)
from bec_mcp_server.tools.tracebacks import (
    Traceback,
    might_hold_traceback,
    parse_traceback,
    remove_traceback,
)

#: records on each side of ``around`` by default, and the most a caller may ask for
DEFAULT_CONTEXT = 10
MAX_CONTEXT = 100
#: records a range returns by default, and the most a caller may ask for
DEFAULT_RANGE_LIMIT = 40
MAX_RANGE_LIMIT = 200
#: characters kept of a message without a traceback
RECORD_MESSAGE_CHARS = 4000
#: total characters of a range answer, the hard bound on its size
RANGE_CHAR_BUDGET = 40_000

#: incidents returned by default, and the most a caller may ask for
DEFAULT_INCIDENTS = 10
MAX_INCIDENTS = 30
#: records shown before an incident's first occurrence and after it, and how far back and on
CONTEXT_BEFORE = 6
CONTEXT_AFTER = 3
CONTEXT_BEFORE_S = 60.0
CONTEXT_AFTER_S = 15.0
#: characters of a context record's message
CONTEXT_MESSAGE_CHARS = 200
#: characters of an incident's message and of the message quoted in its finding
INCIDENT_MESSAGE_CHARS = 600
FINDING_QUOTE_CHARS = 180
#: incidents starting within this many seconds of the previous one form a cluster
RELATED_WINDOW_S = 3.0
#: an incident repeating this often is called a loop
REPEAT_COUNT = 10
#: ids kept per incident
INCIDENT_IDS = 5
#: scan and device names searched in context records this far around the first occurrence
SCAN_WINDOW_S = 30.0

_SCAN_NUMBER_RE = re.compile(r"['\"]scan_number['\"]: (\d+)")
_SCAN_NAME_RE = re.compile(r"['\"]scan_name['\"]: ['\"](\w+)['\"]")
_SCAN_ID_RE = re.compile(
    r"\bscan[_ ]id['\"]?[:=]\s*['\"]?([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
)

Detail = Literal["short", "full"]


# ------------------------------------------------------------------------------------------------
# Shared: reading and describing the records
# ------------------------------------------------------------------------------------------------


def _records(source: LogSource) -> list[LogRecord]:
    records = (parse_log_message(message) for message in source.records)
    return [record for record in records if record is not None]


def _iso_ms(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(timespec="milliseconds")


def _cache_description(records: list[LogRecord], source: LogSource) -> dict[str, Any]:
    """What the source holds: its id and time span, and whether it is still following."""
    return {
        "source": "cache" if source.cached else "stream",
        "records": len(records),
        "capacity": source.capacity,
        "first_id": records[0].id if records else None,
        "last_id": records[-1].id if records else None,
        "oldest": iso_time(records[0].timestamp) if records else None,
        "newest": iso_time(records[-1].timestamp) if records else None,
    }


def _source(record: LogRecord) -> str | None:
    if record.source and record.line is not None:
        return f"{record.source}:{record.line}"
    return record.source


def record_digest(record: LogRecord, traceback: Traceback | None = None) -> dict[str, Any]:
    """One record in full, with its traceback parsed; the shape bec_log_range returns."""
    out: dict[str, Any] = {
        "id": record.id,
        "time": _iso_ms(record.timestamp),
        "service": record.service,
        "level": record.level,
        "source": _source(record),
    }
    if traceback is None and might_hold_traceback(record.message):
        traceback = parse_traceback(record.message)
    if traceback is not None:
        out["message"] = _clip(traceback.prefix or traceback.raised.text(), RECORD_MESSAGE_CHARS)
        out["traceback"] = traceback.digest()
    else:
        out["message"] = _clip(record.message, RECORD_MESSAGE_CHARS)
    if record.exception:
        # logger.exception: bec_lib publishes only the exception's type and value
        out["exception"] = record.exception
    if record.thread:
        out["thread"] = record.thread
    return out


def context_digest(record: LogRecord) -> dict[str, Any]:
    """A record as one short line, for the records around an incident."""
    text = remove_traceback(record.message) or record.message
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if might_hold_traceback(record.message):
        traceback = parse_traceback(record.message)
        if traceback is not None:
            first = f"{first} [{traceback.raised.text()}]" if first else traceback.raised.text()
    return {
        "id": record.id,
        "time": _iso_ms(record.timestamp),
        "service": record.service,
        "level": record.level,
        "message": _clip(first, CONTEXT_MESSAGE_CHARS),
    }


def _parse_time(value: str, name: str) -> float:
    """ISO 8601 date and time, or a time of day (today, UTC); UTC unless an offset is given."""
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        try:
            clock = day_time.fromisoformat(text)
        except ValueError:
            raise ValueError(
                f"{name} must be an ISO 8601 time such as 2026-10-13T16:33:44Z or 16:33:44, "
                f"got {value!r}."
            ) from None
        today = datetime.now(timezone.utc).date()
        moment = datetime.combine(today, clock, tzinfo=clock.tzinfo or timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


# ------------------------------------------------------------------------------------------------
# bec_log_range
# ------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RangeRequest:  # pylint: disable=too-many-instance-attributes
    """What bec_log_range was asked for; ``mode`` says which selection applies."""

    around: int | None = None
    before: int = DEFAULT_CONTEXT
    after: int = DEFAULT_CONTEXT
    start: int | None = None
    end: int | None = None
    since: float | None = None
    until: float | None = None
    service: str | None = None
    threshold: int = 0
    limit: int = DEFAULT_RANGE_LIMIT

    @property
    def mode(self) -> str:
        """around, ids, time or newest, in that order of precedence."""
        if self.around is not None:
            return "around"
        if self.start is not None or self.end is not None:
            return "ids"
        if self.since is not None or self.until is not None:
            return "time"
        return "newest"

    def keep(self, record: LogRecord) -> bool:
        """The service and level filters."""
        if self.service is not None and self.service not in (record.service or "").lower():
            return False
        return record.level_no >= self.threshold


def range_request(  # pylint: disable=too-many-arguments
    *,
    around: int | None = None,
    before: int = DEFAULT_CONTEXT,
    after: int = DEFAULT_CONTEXT,
    start: int | None = None,
    end: int | None = None,
    since: str | None = None,
    until: str | None = None,
    service: str | None = None,
    level: str | None = None,
    limit: int = DEFAULT_RANGE_LIMIT,
) -> RangeRequest:
    """Check and normalize bec_log_range's arguments before BEC is touched."""
    if level is not None and _lowercase(level) not in LOG_LEVELS:
        levels = ", ".join(n for n in LOG_LEVELS if not n.startswith("console"))
        raise ValueError(f"Unknown level {level!r}; use one of {levels}.")
    if start is not None and end is not None and end < start:
        raise ValueError(f"end ({end}) is before start ({start}); ids grow with time.")
    since_ts = _parse_time(since, "since") if since else None
    until_ts = _parse_time(until, "until") if until else None
    if since_ts is not None and until_ts is not None and until_ts < since_ts:
        raise ValueError("until is before since.")
    return RangeRequest(
        around=around,
        before=max(0, min(int(before), MAX_CONTEXT)),
        after=max(0, min(int(after), MAX_CONTEXT)),
        start=start,
        end=end,
        since=since_ts,
        until=until_ts,
        service=service.strip().split("/")[0].lower() or None if service else None,
        threshold=LOG_LEVELS[_lowercase(level)] if level else 0,
        limit=max(1, min(int(limit), MAX_RANGE_LIMIT)),
    )


_NO_IDS = (
    "Record ids need the server's log cache, which is not running; select by time with since "
    "and until instead, or call without arguments for the newest records."
)


def _index_of(records: list[LogRecord], record_id: int) -> int:
    """Position of the record with ``record_id``; ids grow by one, so this is arithmetic."""
    if not records:
        raise LookupError(f"Record {record_id} does not exist; the log is empty.")
    if records[0].id is None:
        raise ValueError(_NO_IDS)
    first, last = records[0].id, records[-1].id
    if record_id < first:
        raise LookupError(
            f"Record {record_id} has left the log cache, which now holds ids {first} to {last}."
        )
    if record_id > last:
        raise LookupError(f"Record {record_id} does not exist; the newest record is {last}.")
    index = record_id - first
    if records[index].id != record_id:  # pragma: no cover - ids are contiguous by construction
        index = next(i for i, r in enumerate(records) if r.id == record_id)
    return index


def _select(  # pylint: disable=too-many-locals
    records: list[LogRecord], request: RangeRequest
) -> tuple[list[LogRecord], int, int]:
    """The records in the range, plus how many matching records lie before and after it."""
    mode = request.mode
    if mode == "around":
        index = _index_of(records, request.around)
        low, high = max(0, index - request.before), index + request.after + 1
        window = [r for r in records[low:high] if r.id == request.around or request.keep(r)]
        before = sum(1 for r in records[:low] if request.keep(r))
        after = sum(1 for r in records[high:] if request.keep(r))
        return window, before, after
    if mode == "ids":
        if records and records[0].id is None:
            raise ValueError(_NO_IDS)
        lo = request.start if request.start is not None else -1
        hi = request.end if request.end is not None else float("inf")
        inside = [r for r in records if lo <= (r.id or 0) <= hi and request.keep(r)]
        outside_before = sum(1 for r in records if (r.id or 0) < lo and request.keep(r))
        outside_after = sum(1 for r in records if (r.id or 0) > hi and request.keep(r))
        from_start = request.start is not None
    elif mode == "time":
        lo_t = request.since if request.since is not None else float("-inf")
        hi_t = request.until if request.until is not None else float("inf")

        def when(record: LogRecord) -> float:
            return record.timestamp if record.timestamp is not None else float("-inf")

        inside = [r for r in records if lo_t <= when(r) <= hi_t and request.keep(r)]
        outside_before = sum(1 for r in records if when(r) < lo_t and request.keep(r))
        outside_after = sum(1 for r in records if when(r) > hi_t and request.keep(r))
        from_start = request.since is not None
    else:
        inside = [r for r in records if request.keep(r)]
        outside_before = outside_after = 0
        from_start = False
    if len(inside) <= request.limit:
        return inside, outside_before, outside_after
    if from_start:
        # an explicit start reads forward from it
        cut = len(inside) - request.limit
        return inside[: request.limit], outside_before, outside_after + cut
    cut = len(inside) - request.limit
    return inside[cut:], outside_before + cut, outside_after


def _fit(
    digests: list[dict[str, Any]], anchor: int | None
) -> tuple[list[dict[str, Any]], int, int]:
    """Drop records farthest from the anchor (or the oldest) until the answer fits the budget."""
    sizes = [len(str(d)) for d in digests]
    low, high = 0, len(digests)
    total = sum(sizes)
    while total > RANGE_CHAR_BUDGET and high - low > 1:
        if anchor is not None and anchor - low < high - 1 - anchor:
            high -= 1
            total -= sizes[high]
        else:
            total -= sizes[low]
            low += 1
    return digests[low:high], low, len(digests) - high


def log_range(records: list[LogRecord], request: RangeRequest, source: LogSource) -> dict[str, Any]:
    """bec_log_range's answer for the records of ``source``."""
    selected, before, after = _select(records, request)
    digests = [record_digest(record) for record in selected]
    anchor = next((i for i, record in enumerate(selected) if record.id == request.around), None)
    digests, cut_before, cut_after = _fit(digests, anchor)
    before += cut_before
    after += cut_after
    with_tb = [d for d in digests if "traceback" in d]
    data: dict[str, Any] = {
        "cache": _cache_description(records, source),
        "selection": {
            "mode": request.mode,
            "first_id": digests[0]["id"] if digests else None,
            "last_id": digests[-1]["id"] if digests else None,
            "shown": len(digests),
            "more_before": before,
            "more_after": after,
        },
        "records": digests,
    }
    return tool_result(
        True,
        _range_summary(records, request, digests, with_tb, source),
        data=data,
        truncated=bool(cut_before or cut_after),
    )


def _range_summary(
    records: list[LogRecord],
    request: RangeRequest,
    digests: list[dict[str, Any]],
    with_tb: list[dict[str, Any]],
    source: LogSource,
) -> str:
    where = "the server's log cache" if source.cached else "BEC's log stream (no cache)"
    parts: list[str] = []
    if source.note:
        parts.append(source.note)
    if not records:
        parts.append(f"{where.capitalize()} is empty.")
        return " ".join(parts)
    if not digests:
        parts.append(f"No records in {where} match this range and these filters.")
    elif digests[0]["id"] is not None:
        parts.append(
            f"{len(digests)} records from {where}, ids {digests[0]['id']} to {digests[-1]['id']} "
            f"({digests[0]['time']} to {digests[-1]['time']})."
        )
    else:
        parts.append(f"{len(digests)} records from {where}.")
    if with_tb:
        named = ", ".join(
            f"{d['traceback']['exception'].rsplit('.', 1)[-1]} (id {d['id']})" for d in with_tb[:5]
        )
        parts.append(f"{len(with_tb)} carry a traceback: {named}.")
    if records[0].id is not None:
        parts.append(
            f"The cache holds ids {records[0].id} to {records[-1].id}, from "
            f"{iso_time(records[0].timestamp)} to {iso_time(records[-1].timestamp)}."
        )
    if request.mode == "newest" and len(digests) < len(records):
        parts.append("Pass around, start/end or since/until for other records.")
    return " ".join(parts)


def log_range_tool(connection: BecConnection, request: RangeRequest) -> dict[str, Any]:
    """bec_log_range: read the cache (or the stream) and slice it."""
    source = read_logs(connection)
    return log_range(_records(source), request, source)


# ------------------------------------------------------------------------------------------------
# bec_log_analysis: incidents
# ------------------------------------------------------------------------------------------------


@dataclass
class Incident:  # pylint: disable=too-many-instance-attributes
    """One distinct failure and every record of it."""

    key: tuple[Any, ...]
    kind: str
    title: str
    level_no: int
    positions: list[int] = field(default_factory=list)
    records: list[LogRecord] = field(default_factory=list)
    traceback: Traceback | None = None
    alarm_severity: str | None = None
    alarm_device: str | None = None
    services: set[str] = field(default_factory=set)
    ref: str = ""
    rule: Rule = UNCLASSIFIED
    devices: list[str] = field(default_factory=list)

    def add(self, record: LogRecord, position: int, traceback: Traceback | None) -> None:
        """One more record of this failure; the newest supplies the traceback shown."""
        self.positions.append(position)
        self.records.append(record)
        self.level_no = max(self.level_no, record.level_no)
        if traceback is not None:
            self.traceback = traceback
        if record.service:
            self.services.add(record.service)

    @property
    def first(self) -> LogRecord:
        """The first record of the failure."""
        return self.records[0]

    @property
    def last(self) -> LogRecord:
        """The newest record of the failure."""
        return self.records[-1]

    @property
    def count(self) -> int:
        """How many records the failure has."""
        return len(self.records)

    def text(self) -> str:
        """The failure's message: the logged text and the exception chain."""
        record = self.last
        parts = [remove_traceback(record.message)]
        if self.traceback is not None:
            parts += [f"{e.type}: {e.message}" for e in self.traceback.chain]
        if record.exception:
            parts.append(record.exception)
        return "\n".join(part for part in parts if part)

    def types(self) -> str:
        """The exception type names involved, the first exception of the chain first."""
        names = []
        if self.traceback is not None:
            names += [e.type for e in self.traceback.chain]
        if self.last.exception:
            names.append(self.last.exception.split(":", 1)[0])
        return " ".join(names)

    def severity(self) -> str:
        """The finding severity: the rule's override, else from the records' level."""
        if self.rule.severity is not None:
            return self.rule.severity
        return "error" if self.level_no >= LOG_LEVELS["error"] else "warning"


def _signature(record: LogRecord, traceback: Traceback | None) -> tuple[tuple[Any, ...], str, str]:
    """What makes two records the same failure, the incident's kind, and its title."""
    service = record.service or "(unknown)"
    alarm = parse_alarm_line(record.message)
    if alarm is not None and not alarm.echo:
        first = alarm.message.splitlines()[0] if alarm.message else ""
        where = f" on {alarm.device}" if alarm.device else ""
        return (
            ("alarm", alarm.severity, alarm.device, message_shape(first)),
            "alarm",
            f"{alarm.severity} alarm{where}",
        )
    if traceback is not None:
        # keyed by the first exception and where it started: a failure that a caller logs
        # again wrapped in another exception (a scan abort around a device error) is one incident
        origin = traceback.origin()
        place = (origin.path or origin.file, origin.function) if origin is not None else None
        return (
            ("traceback", traceback.root.type, place),
            "traceback",
            f"{traceback.root.short_type} in {service}",
        )
    if record.exception:
        exception_type = record.exception.split(":", 1)[0]
        return (
            ("exception", exception_type, record.source),
            "exception",
            f"{exception_type} in {service}",
        )
    return (
        ("message", service, record.level, message_shape(record.message)),
        "message",
        f"{record.level.capitalize()} in {service}",
    )


@dataclass(frozen=True)
class AnalysisRequest:
    """What bec_log_analysis was asked for."""

    since: float | None = None
    service: str | None = None
    threshold: int = LOG_LEVELS["error"]
    limit: int = DEFAULT_INCIDENTS
    minutes: float | None = None


def analysis_request(
    *,
    minutes: float | None = None,
    service: str | None = None,
    level: str | None = None,
    limit: int = DEFAULT_INCIDENTS,
    now: float | None = None,
) -> AnalysisRequest:
    """Check and normalize bec_log_analysis' arguments before BEC is touched."""
    if level is not None and _lowercase(level) not in ("warning", "error", "critical"):
        raise ValueError("level must be warning, error or critical.")
    if minutes is not None and minutes <= 0:
        raise ValueError("minutes must be a positive number of minutes, or omitted for all.")
    now = time.time() if now is None else now
    return AnalysisRequest(
        since=now - minutes * 60 if minutes is not None else None,
        service=service.strip().split("/")[0].lower() or None if service else None,
        threshold=LOG_LEVELS[_lowercase(level)] if level else LOG_LEVELS["error"],
        limit=max(1, min(int(limit), MAX_INCIDENTS)),
        minutes=minutes,
    )


def find_incidents(  # pylint: disable=too-many-locals
    records: list[LogRecord], request: AnalysisRequest
) -> list[Incident]:
    """Group the failures in ``records`` (oldest first) into incidents, oldest first."""
    incidents: dict[tuple[Any, ...], Incident] = {}
    for position, record in enumerate(records):
        if request.since is not None and (record.timestamp or 0) < request.since:
            continue
        if request.service is not None and request.service not in (record.service or "").lower():
            continue
        traceback = (
            parse_traceback(record.message) if might_hold_traceback(record.message) else None
        )
        if record.level_no < request.threshold and (
            traceback is None or traceback.raised.type == "?"
        ):
            # below the level, only a record with a complete traceback counts
            continue
        alarm = parse_alarm_line(record.message)
        if alarm is not None and alarm.echo:
            # each client echoes the alarms it receives; the raiser's own line counts them
            continue
        key, kind, title = _signature(record, traceback)
        incident = incidents.get(key)
        if incident is None:
            incident = incidents[key] = Incident(key, kind, title, record.level_no)
            if alarm is not None:
                incident.alarm_severity, incident.alarm_device = alarm.severity, alarm.device
        incident.add(record, position, traceback)
    ordered = sorted(incidents.values(), key=lambda i: i.positions[0])
    for number, incident in enumerate(ordered, start=1):
        incident.ref = f"I{number}"
        text = incident.text()
        incident.rule = classify(incident.types(), text)
        names = devices_named(text)
        if incident.alarm_device:
            names = [incident.alarm_device] + [n for n in names if n != incident.alarm_device]
        incident.devices = names
    return ordered


def _context(
    records: list[LogRecord], incident: Incident
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Records just before the first occurrence and just after it, other failures included."""
    own = set(incident.positions)
    start = incident.positions[0]
    t0 = incident.first.timestamp
    before: list[dict[str, Any]] = []
    for position in range(start - 1, -1, -1):
        record = records[position]
        if len(before) >= CONTEXT_BEFORE:
            break
        if t0 is not None and record.timestamp is not None:
            if t0 - record.timestamp > CONTEXT_BEFORE_S:
                break
        if position in own or record.level_no < LOG_LEVELS["info"]:
            continue
        before.append(context_digest(record))
    after: list[dict[str, Any]] = []
    for position in range(start + 1, len(records)):
        record = records[position]
        if len(after) >= CONTEXT_AFTER:
            break
        if t0 is not None and record.timestamp is not None:
            if record.timestamp - t0 > CONTEXT_AFTER_S:
                break
        if position in own or record.level_no < LOG_LEVELS["info"]:
            continue
        after.append(context_digest(record))
    return before[::-1], after


def _scan_near(records: list[LogRecord], incident: Incident) -> dict[str, Any] | None:
    """The scan the records around the first occurrence talk about, if any."""
    t0 = incident.first.timestamp
    start = incident.positions[0]
    found: dict[str, Any] = {}
    # nearest records first: the scan that was running is mentioned closest to the failure
    order = sorted(
        range(max(0, start - 50), min(len(records), start + 20)), key=lambda p: abs(p - start)
    )
    for position in order:
        record = records[position]
        if t0 is not None and record.timestamp is not None:
            if abs(record.timestamp - t0) > SCAN_WINDOW_S:
                continue
        text = record.message
        for key, pattern in (
            ("name", _SCAN_NAME_RE),
            ("number", _SCAN_NUMBER_RE),
            ("id", _SCAN_ID_RE),
        ):
            if key not in found and (match := pattern.search(text)):
                found[key] = int(match.group(1)) if key == "number" else match.group(1)
        if len(found) == 3:
            break
    return found or None


def clusters(incidents: list[Incident]) -> list[list[Incident]]:
    """Incidents (oldest first) that started close together, each within the window of the last.

    A failure usually brings others with it within a second, such as a device error, the scan
    abort it causes and the alarm it raises; the first of a cluster is the one to explain.
    """
    groups: list[list[Incident]] = []
    for incident in incidents:
        t0 = incident.first.timestamp
        previous = groups[-1][-1].first.timestamp if groups else None
        if t0 is not None and previous is not None and t0 - previous <= RELATED_WINDOW_S:
            groups[-1].append(incident)
        else:
            groups.append([incident])
    return groups


def _related(groups: list[list[Incident]], incident: Incident) -> list[str]:
    for group in groups:
        if incident in group:
            return [other.ref for other in group if other is not incident]
    return []


def _origin_text(incident: Incident) -> str | None:
    """Where the failure started: the traceback's origin frame, else the logging call."""
    if incident.traceback is not None:
        origin = incident.traceback.origin()
        if origin is not None:
            return f"{origin.path or origin.file}:{origin.line} ({origin.function})"
    record = incident.last
    if record.exception and record.source:
        # logger.exception publishes no frames; the logging call sits in the except block
        line = f":{record.line}" if record.line is not None else ""
        return f"{record.source}{line}"
    return None


def _likely_cause(incident: Incident) -> str:
    rule = incident.rule
    cause = rule.cause
    if rule.category == "code_error" and (origin := _origin_text(incident)):
        cause = f"{cause}, surfacing at {origin}"
    if incident.count >= REPEAT_COUNT:
        span = (incident.last.timestamp or 0) - (incident.first.timestamp or 0)
        cause += (
            f"; it repeats ({incident.count} records in {_age(span)}), so the first occurrence "
            "is the one to explain"
        )
    return cause


def _suggestion(incident: Incident) -> str:
    suggestion = incident.rule.suggestion
    if incident.devices and "<device>" in suggestion:
        suggestion = suggestion.replace("<device>", incident.devices[0])
    return suggestion


def _message(incident: Incident) -> str:
    record = incident.last
    if incident.traceback is not None:
        traceback = incident.traceback
        text = f"{traceback.root.short_type}: {traceback.root.message}".rstrip(": ")
        if traceback.raised is not traceback.root:
            text = f"{text} (logged as {traceback.raised.text()})"
        if traceback.prefix:
            text = f"{traceback.prefix} | {text}"
        return _clip(text, INCIDENT_MESSAGE_CHARS)
    if incident.kind == "alarm":
        alarm = parse_alarm_line(record.message)
        if alarm is not None:
            return _clip(alarm.message, INCIDENT_MESSAGE_CHARS)
    return _clip(record.message, INCIDENT_MESSAGE_CHARS)


def incident_digest(
    records: list[LogRecord], groups: list[list[Incident]], incident: Incident
) -> dict[str, Any]:
    """One incident as the agent sees it."""
    before, after = _context(records, incident)
    out: dict[str, Any] = {
        "ref": incident.ref,
        "title": incident.title,
        "kind": incident.kind,
        "severity": incident.severity(),
        "count": incident.count,
        "first": _iso_ms(incident.first.timestamp),
        "last": _iso_ms(incident.last.timestamp),
        "first_id": incident.first.id,
        "last_id": incident.last.id,
        "ids": [r.id for r in incident.records[-INCIDENT_IDS:]] if incident.first.id else [],
        "services": sorted(incident.services),
        "message": _message(incident),
    }
    if incident.traceback is not None:
        digest = incident.traceback.digest()
        out["traceback"] = {
            key: digest[key] for key in ("exception", "message", "root_cause", "origin")
        }
    if incident.alarm_severity:
        out["alarm"] = {"severity": incident.alarm_severity, "device": incident.alarm_device}
    if incident.devices:
        out["devices"] = incident.devices[:5]
    scan = _scan_near(records, incident)
    if scan:
        out["scan"] = scan
    related = _related(groups, incident)
    if related:
        out["related"] = related
    out["category"] = incident.rule.category
    out["likely_cause"] = _likely_cause(incident)
    out["suggestion"] = _suggestion(incident)
    out["before"] = before
    out["after"] = after
    return out


def incident_finding(incident: Incident, now: float) -> Finding:
    """The incident as a finding: what, where, how often, the likely cause and what to try."""
    record = incident.last
    where = f" at {_origin_text(incident)}" if _origin_text(incident) else ""
    times = f"{incident.count} records" if incident.count > 1 else "once"
    age = f", last {_age(now - record.timestamp)} ago" if record.timestamp is not None else ""
    quote = _clip(_message(incident).splitlines()[0], FINDING_QUOTE_CHARS).rstrip(". ")
    message = (
        f"{incident.ref} {incident.title}{where} ({times}{age}): {quote}. Likely cause: "
        f"{_likely_cause(incident)}. Try: {_suggestion(incident)}"
    )
    severity = incident.severity()
    code = f"logs.{incident.rule.category}"
    subject = incident.devices[0] if incident.devices else incident.ref
    if incident.rule.target == "device" and incident.devices:
        return Finding(
            severity, code, message, subject, "bec_device_diagnose", {"device": incident.devices[0]}
        )
    if incident.rule.target == "services":
        return Finding(severity, code, message, subject, "bec_status", {})
    if incident.first.id is not None:
        return Finding(
            severity, code, message, subject, "bec_log_range", {"around": incident.first.id}
        )
    return Finding(severity, code, message, subject)


_SEVERITY_RANK = {"error": 0, "warning": 1, "info": 2}


def log_analysis(
    records: list[LogRecord],
    request: AnalysisRequest,
    source: LogSource,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """bec_log_analysis' answer for the records of ``source``."""
    now = time.time() if now is None else now
    incidents = find_incidents(records, request)
    # the most severe first, then the most recent: what is going wrong now
    shown = sorted(
        incidents, key=lambda i: (_SEVERITY_RANK[i.severity()], -(i.last.timestamp or 0))
    )[: request.limit]
    groups = clusters(incidents)
    digests = [incident_digest(records, groups, incident) for incident in shown]
    data: dict[str, Any] = {
        "cache": _cache_description(records, source),
        "window": {
            "since": iso_time(request.since),
            "service": request.service,
            "level": next(n for n, v in LOG_LEVELS.items() if v == request.threshold),
        },
        "incidents_found": len(incidents),
        "incidents": digests,
    }
    return tool_result(
        True,
        _analysis_summary(records, request, (incidents, groups, shown), source, now),
        findings=[incident_finding(incident, now) for incident in shown],
        data=data,
        truncated=len(shown) < len(incidents),
    )


def _analysis_summary(  # pylint: disable=too-many-locals
    records: list[LogRecord],
    request: AnalysisRequest,
    found: tuple[list[Incident], list[list[Incident]], list[Incident]],
    source: LogSource,
    now: float,
) -> str:
    incidents, groups, shown = found
    parts = [source.note] if source.note else []
    window = f" from the last {request.minutes:g} min" if request.minutes is not None else ""
    where = "cached" if source.cached else "streamed"
    if not records:
        parts.append("The log holds no records yet.")
        return " ".join(parts)
    if not incidents:
        level = "errors" if request.threshold >= LOG_LEVELS["error"] else "warnings or errors"
        parts.append(
            f"No {level}, tracebacks or alarms in the {len(records)} {where} records{window}."
        )
        return " ".join(parts)
    errors = sum(1 for i in incidents if i.severity() == "error")
    records_involved = sum(i.count for i in incidents)
    parts.append(
        f"{len(incidents)} distinct problems ({errors} errors) in {records_involved} of the "
        f"{len(records)} {where} records{window}; showing {len(shown)}, most severe and most "
        "recent first."
    )
    together = [group for group in groups if len(group) > 1]
    if together:
        # the newest cluster: what most likely went wrong last, and which incident leads it
        group = together[-1]
        lead = group[0]
        refs = ", ".join(i.ref for i in group[:-1]) + f" and {group[-1].ref}"
        when = f" {_age(now - lead.first.timestamp)} ago" if lead.first.timestamp else ""
        parts.append(
            f"{refs} started together{when}; the first, {lead.ref} {lead.title}, usually "
            "explains the others."
        )
    if len(shown) < len(incidents):
        parts.append("Narrow with minutes or service, or raise limit, to see the rest.")
    return " ".join(parts)


def log_analysis_tool(connection: BecConnection, request: AnalysisRequest) -> dict[str, Any]:
    """bec_log_analysis: read the cache (or the stream) and find its incidents."""
    source = read_logs(connection)
    return log_analysis(_records(source), request, source)


# ---------------------------------------------------------------------- MCP registration

OptionalId = Annotated[int | None, Field(ge=1)]


def register(server: Any, connection: BecConnection, call: BecCall) -> None:
    """Expose bec_log_range and bec_log_analysis; ``call`` offloads under the deadline."""

    @server.tool(
        name="bec_log_range",
        description="Consecutive BEC log records in full, around one record id, between two "
        "ids or times, or the newest, with Python tracebacks parsed into exception chain and "
        "frames.",
        annotations=READ_ONLY_TOOL,
    )
    async def bec_log_range(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        around: Annotated[
            OptionalId, Field(description="Record id to center on, e.g. a first_id or last_id.")
        ] = None,
        before: Annotated[
            int, Field(ge=0, le=MAX_CONTEXT, description="Records before around.")
        ] = DEFAULT_CONTEXT,
        after: Annotated[
            int, Field(ge=0, le=MAX_CONTEXT, description="Records after around.")
        ] = DEFAULT_CONTEXT,
        start: Annotated[OptionalId, Field(description="First record id of the range.")] = None,
        end: Annotated[OptionalId, Field(description="Last record id of the range.")] = None,
        since: Annotated[
            str | None,
            Field(description="Start time, ISO 8601 such as 2026-10-13T16:33:44Z; UTC by default."),
        ] = None,
        until: Annotated[str | None, Field(description="End time, ISO 8601.")] = None,
        service: Annotated[
            str | None, Field(description="Only services whose name contains this.")
        ] = None,
        level: Annotated[
            LogLevelName | None,
            BeforeValidator(_lowercase),
            Field(description="Lowest level to include, e.g. warning."),
        ] = None,
        limit: Annotated[
            int, Field(ge=1, le=MAX_RANGE_LIMIT, description="Most records to return.")
        ] = DEFAULT_RANGE_LIMIT,
    ) -> dict:
        request = range_request(
            around=around,
            before=before,
            after=after,
            start=start,
            end=end,
            since=since,
            until=until,
            service=service,
            level=level,
            limit=limit,
        )
        return await call(log_range_tool, connection, request)

    @server.tool(
        name="bec_log_analysis",
        description="Errors, tracebacks and alarms in the recent BEC log grouped into incidents, "
        "each with its traceback origin, the records before it, the devices and scan involved, "
        "a likely cause and a suggested next step.",
        annotations=READ_ONLY_TOOL,
    )
    async def bec_log_analysis(
        minutes: Annotated[
            float | None, Field(gt=0, description="Only records from the last N minutes.")
        ] = None,
        service: Annotated[
            str | None, Field(description="Only services whose name contains this.")
        ] = None,
        level: Annotated[
            Literal["warning", "error", "critical"] | None,
            BeforeValidator(_lowercase),
            Field(description="Lowest level that counts as a problem; default error."),
        ] = None,
        limit: Annotated[
            int, Field(ge=1, le=MAX_INCIDENTS, description="Most incidents to return.")
        ] = DEFAULT_INCIDENTS,
    ) -> dict:
        request = analysis_request(minutes=minutes, service=service, level=level, limit=limit)
        return await call(log_analysis_tool, connection, request)
