"""``bec_logs``: a filtered, condensed tail of BEC's shared log stream, with alarms merged in.

The tool only filters and condenses. Reading the records and deciding what they mean stays with
the agent, so a dedicated log agent can later use this as its only tool.

Every BEC service and client publishes its log records to one Redis stream that BEC trims to
about 10,000 records. On a busy beamline that is well under an hour, so this shows what just
happened, not an archive. The records come from the server's log cache (``bec/log_cache.py``),
which follows the stream in the background and numbers every record with an ``id`` that
bec_log_range and bec_log_analysis accept; without the cache the stream is read directly.
Alarms reach the stream as well: ``raise_alarm`` logs every alarm it
raises (WARNING as a warning, MINOR and MAJOR as an error), and every client's alarm handler
echoes WARNING and MINOR alarms it receives as a warning of its own. Both forms are folded into
one alarm entry here, together with the MAJOR alarms the server's own client has retained.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Annotated, Any, Literal

from bec_lib.alarm_handler import Alarms
from pydantic import BeforeValidator, Field

from bec_mcp_server.bec.connection import BecConnection, BecUnavailableError
from bec_mcp_server.bec.digest import iso_time
from bec_mcp_server.bec.log_stream import (
    LOG_LEVELS,
    LOG_STREAM_CAPACITY,
    LogRecord,
    parse_log_message,
    read_log_stream,
)
from bec_mcp_server.tools import READ_ONLY_TOOL, BecCall, Finding, tool_result

LogLevelName = Literal["trace", "debug", "info", "success", "warning", "error", "critical"]
Detail = Literal["short", "full"]


def _lowercase(value: Any) -> Any:
    """Small models send ERROR or Full; the enum values are lowercase."""
    return value.strip().lower() if isinstance(value, str) else value


#: collapsed message groups returned by default, and the most a caller may ask for
DEFAULT_LIMIT = 30
MAX_LIMIT = 200
#: alarm groups returned; alarms are rare, so this only bounds a pathological stream
MAX_ALARMS = 20
#: characters kept per message in short and full detail
SHORT_MESSAGE_CHARS = 300
FULL_MESSAGE_CHARS = 4000
#: total message characters across all returned groups; the hard bound on output size
OUTPUT_CHAR_BUDGET = 40_000
#: characters of an alarm text quoted in its finding line
FINDING_QUOTE_CHARS = 160
#: distinct exact texts tracked per group before ``variants`` stops counting
MAX_TRACKED_VARIANTS = 1000
#: an alarm handler's echo lands within this many seconds of the raising service's log line
ALARM_ECHO_WINDOW_S = 30.0
#: shortest message that may match an alarm by containment rather than equality
_MIN_CONTAINED_CHARS = 12

_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE
)
_HEX_RE = re.compile(r"\b0x[0-9a-f]+\b", re.IGNORECASE)
_NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?\b")
#: ``RedisConnector.raise_alarm``: "Alarm (MAJOR) raised for device 'samx': <error_message>"
_ALARM_RAISED_RE = re.compile(
    r"^Alarm \((WARNING|MINOR|MAJOR)\) raised(?: for device '(.*?)')?: (.*)$", re.DOTALL
)
#: ``AlarmBase.__str__``, logged by every client's alarm handler for WARNING and MINOR alarms
_ALARM_ECHO_RE = re.compile(
    r"^An alarm has occurred\. Severity: (WARNING|MINOR|MAJOR)\.\n(.*?)\.\n\t ?(.*)$", re.DOTALL
)


# ------------------------------------------------------------------------------------------------
# Access to BEC: the log cache, or the stream and the client's alarms
# ------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LogSource:
    """The records a log tool works on, oldest first, and where they came from."""

    #: cached ``LogRecord``s, or ``LogMessage``s read from the stream
    records: list[Any]
    #: how many records the source holds at most; a full source may have lost older records
    capacity: int
    #: the records come from the log cache and carry ids
    cached: bool
    #: something the agent must know about these records, e.g. that BEC is unreachable
    note: str | None = None
    client_alarms: list[Any] = field(default_factory=list)


def cached_or_streamed(connection: Any, client: Any) -> LogSource:
    """The log cache while it follows the stream, else the stream itself (blocking)."""
    cache = getattr(connection, "log_cache", None)
    if cache is not None and cache.following:
        view = cache.view()
        return LogSource(view.records, view.capacity, cached=True)
    return LogSource(read_log_stream(client), LOG_STREAM_CAPACITY, cached=False)


def read_logs(connection: Any, *, with_alarms: bool = False) -> LogSource:
    """Records for one tool call; starting the client here also starts the cache.

    When BEC cannot be reached, the records the cache holds are still served, with a note,
    because what happened just before BEC went away is often the answer. Without cached
    records the BecUnavailableError propagates.
    """
    try:
        with connection.use() as client:
            source = cached_or_streamed(connection, client)
            if with_alarms:
                source = replace(source, client_alarms=read_client_alarms(client))
            return source
    except BecUnavailableError as exc:
        cache = getattr(connection, "log_cache", None)
        if cache is None or len(cache) == 0:
            raise
        view = cache.view()
        return LogSource(
            view.records,
            view.capacity,
            cached=True,
            note=f"BEC is unreachable ({exc}), so these are the records the server cached "
            "before; anything logged since is missing.",
        )


def read_client_alarms(client: Any) -> list[Any]:
    """Unhandled MAJOR alarms the server's own client has received since it started.

    bec_lib keeps only alarms above MINOR on the client-side stack; the rest go to the logs.
    """
    handler = getattr(client, "alarm_handler", None)
    if handler is None:
        return []
    return list(handler.get_unhandled_alarms(severity=Alarms.MAJOR))


# ------------------------------------------------------------------------------------------------
# Parsing
# ------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class AlarmLine:
    severity: str
    device: str | None
    exception_type: str | None
    message: str
    echo: bool


def parse_alarm_line(text: str) -> AlarmLine | None:
    """Recognise an alarm in a log message, in either the raiser's or a client's form."""
    match = _ALARM_RAISED_RE.match(text)
    if match:
        severity, device, message = match.groups()
        return AlarmLine(severity, device or None, None, message.strip(), echo=False)
    match = _ALARM_ECHO_RE.match(text)
    if match:
        severity, exception_type, message = match.groups()
        return AlarmLine(severity, None, exception_type.strip() or None, message.strip(), True)
    return None


def message_shape(text: str) -> str:
    """Collapse key for a message: first and last line, with ids and numbers masked.

    Masking only whole numbers keeps names like ``samx1`` or ``roi2`` apart, while
    "Number of pixels 1024 ..." and "Number of pixels 2048 ..." collapse into one group.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    head = lines[0] if len(lines) == 1 else f"{lines[0]} … {lines[-1]}"
    head = _UUID_RE.sub("<id>", head)
    head = _HEX_RE.sub("0x#", head)
    return _NUMBER_RE.sub("#", head)[:500]


def condense(text: str, detail: Detail) -> str:
    """Shorten a message: full keeps lines up to a cap, short keeps the first and last line."""
    if detail == "full":
        return _clip(text, FULL_MESSAGE_CHARS)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    if len(lines) == 1:
        return _clip(lines[0], SHORT_MESSAGE_CHARS)
    half = SHORT_MESSAGE_CHARS // 2
    skipped = f" … [{len(lines) - 2} more lines] … " if len(lines) > 2 else " … "
    return f"{_clip(lines[0], half)}{skipped}{_clip(lines[-1], half)}"


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[:limit]}… [cut]"


# ------------------------------------------------------------------------------------------------
# Grouping
# ------------------------------------------------------------------------------------------------


@dataclass
class _Group:
    service: str | None
    level: str
    level_no: int
    source: str | None
    message: str
    count: int = 0
    first: float | None = None
    last: float | None = None
    order: int = 0
    variants: set[int] = field(default_factory=set)
    last_id: int | None = None

    def add(self, record: LogRecord, order: int) -> None:
        """Count one more record; the newest one supplies the message shown."""
        self.count += 1
        self.order = order
        self.last_id = record.id if record.id is not None else self.last_id
        self.message = record.message
        self.source = record.source or self.source
        if record.timestamp is not None:
            self.first = record.timestamp if self.first is None else self.first
            self.last = record.timestamp
        if len(self.variants) < MAX_TRACKED_VARIANTS:
            self.variants.add(hash(record.message))

    def digest(self, detail: Detail) -> dict[str, Any]:
        """The group as the agent sees it."""
        out: dict[str, Any] = {"service": self.service, "level": self.level, "count": self.count}
        if self.count > 1:
            out["first"] = iso_time(self.first)
        out["last"] = iso_time(self.last)
        if len(self.variants) > 1:
            # the message differs between repeats (numbers, ids); shown is the newest
            out["variants"] = len(self.variants)
        out["message"] = condense(self.message, detail)
        if detail == "full" and self.source:
            out["source"] = self.source
        if self.last_id is not None:
            # the newest record's cache id, for bec_log_range(around=...)
            out["last_id"] = self.last_id
        return out


@dataclass
class _AlarmGroup:
    severity: str
    device: str | None
    exception_type: str | None
    message: str
    key: str
    raised: int = 0
    echoes: dict[str, int] = field(default_factory=dict)
    first: float | None = None
    last: float | None = None
    order: int = 0
    services: set[str] = field(default_factory=set)
    retained_by_client: bool = False
    last_id: int | None = None

    @property
    def count(self) -> int:
        """How often the alarm was raised.

        Each raise logs one line in the raising service, and each client echoes each alarm
        once, so without a raiser line the busiest echoing client gives the count.
        """
        if self.raised:
            return self.raised
        if self.echoes:
            return max(self.echoes.values())
        return 1 if self.retained_by_client else 0

    def add(self, line: AlarmLine, record: LogRecord | None, order: int) -> None:
        """Count a raiser line, or note an echo from a client."""
        service = record.service if record is not None else None
        if line.echo:
            self.echoes[service or "(unknown)"] = self.echoes.get(service or "(unknown)", 0) + 1
        else:
            self.raised += 1
            self.message = line.message or self.message
            if service:
                self.services.add(service)
        if not self.raised:
            # only echoes so far: they are the best text and origin there is
            self.message = line.message or self.message
            if service:
                self.services.add(service)
        self.order = max(self.order, order)
        self.exception_type = self.exception_type or line.exception_type
        self.device = self.device or line.device
        if record is not None and record.id is not None:
            self.last_id = max(self.last_id or 0, record.id)
        if record is not None and record.timestamp is not None:
            if self.first is None or record.timestamp < self.first:
                self.first = record.timestamp
            if self.last is None or record.timestamp > self.last:
                self.last = record.timestamp

    def near(self, timestamp: float | None) -> bool:
        """Whether ``timestamp`` falls close enough to this alarm to be its echo."""
        if timestamp is None or self.first is None or self.last is None:
            return False
        return self.first - ALARM_ECHO_WINDOW_S <= timestamp <= self.last + ALARM_ECHO_WINDOW_S

    def digest(self, detail: Detail) -> dict[str, Any]:
        """The alarm as the agent sees it under data["alarms"]."""
        out: dict[str, Any] = {"severity": self.severity}
        if self.device:
            out["device"] = self.device
        if self.exception_type:
            out["exception_type"] = self.exception_type
        out["count"] = self.count
        if self.count > 1 and self.first is not None:
            out["first"] = iso_time(self.first)
        out["last"] = iso_time(self.last)
        if self.services:
            out["services"] = sorted(self.services)
        if self.retained_by_client:
            out["unhandled"] = True
        if self.last_id is not None:
            out["last_id"] = self.last_id
        out["message"] = condense(self.message, detail)
        return out

    def finding(self, now: float) -> Finding:
        """The alarm as a finding; one about a device points at that device's diagnosis."""
        where = f" on {self.device}" if self.device else ""
        times = f"raised {self.count} times" if self.count > 1 else "raised once"
        when = f", last {_age(now - self.last)} ago" if self.last is not None else ""
        if self.retained_by_client and self.last is None:
            when = ", still unhandled (time unknown)"
        quote = _clip(condense(self.message, "short"), FINDING_QUOTE_CHARS)
        severity = _FINDING_SEVERITY.get(self.severity, "warning")
        code = f"alarm.{self.severity.lower()}"
        message = f"{self.severity} alarm{where}, {times}{when}: {quote}"
        if self.device:
            return Finding(
                severity, code, message, self.device, "bec_device_diagnose", {"device": self.device}
            )
        if self.last_id is not None:
            # without a device, the records around the alarm are the next thing to read
            return Finding(
                severity, code, message, "alarms", "bec_log_range", {"around": self.last_id}
            )
        return Finding(severity, code, message, "alarms")


#: alarm severity to finding severity
_FINDING_SEVERITY = {"MAJOR": "error", "MINOR": "warning", "WARNING": "info"}
_SEVERITY_ORDER = {"MAJOR": 2, "MINOR": 1, "WARNING": 0}


class _AlarmMerger:
    """Fold raiser lines, client echoes and the client's own alarm stack into alarm groups."""

    def __init__(self) -> None:
        self.groups: dict[tuple[str, str | None, str], _AlarmGroup] = {}
        self._echoes: list[tuple[AlarmLine, LogRecord, int]] = []

    def add_line(self, line: AlarmLine, record: LogRecord, order: int) -> None:
        """Group a raiser line now; hold an echo until every raiser line is in."""
        if line.echo:
            # merged after all raiser lines are in: an echo can land before the raiser's line
            self._echoes.append((line, record, order))
            return
        self._group(line.severity, line.device, line.message).add(line, record, order)

    def finish_lines(self) -> None:
        """Fold the held echoes into the alarm they repeat, or group them on their own."""
        for line, record, order in self._echoes:
            match = self._match(line.severity, line.message, record.timestamp)
            if match is None:
                match = self._group(line.severity, None, line.message)
            match.add(line, record, order)
        self._echoes.clear()

    def add_client_alarm(self, alarm: Any, keep_unmatched: bool) -> bool:
        """Mark a log group as still unhandled, or add the alarm itself; False if dropped."""
        info = getattr(getattr(alarm, "alarm", None), "info", None)
        if info is None:
            return False
        severity = getattr(getattr(alarm, "severity", None), "name", None) or "MAJOR"
        texts = [t for t in (info.error_message, info.compact_error_message) if t]
        device = info.device if isinstance(info.device, str) else _device_text(info.device)
        for text in texts:
            group = self.groups.get((severity, device, message_shape(text))) or self._match(
                severity, text, None
            )
            if group is not None:
                group.retained_by_client = True
                group.exception_type = group.exception_type or info.exception_type
                return True
        if not keep_unmatched or not texts:
            return False
        group = self._group(severity, device, texts[0])
        group.exception_type = group.exception_type or info.exception_type
        group.order = -1
        group.retained_by_client = True
        return True

    def _group(self, severity: str, device: str | None, message: str) -> _AlarmGroup:
        key = (severity, device, message_shape(message))
        group = self.groups.get(key)
        if group is None:
            group = _AlarmGroup(severity, device, None, message, key=key[2])
            self.groups[key] = group
        return group

    def _match(self, severity: str, message: str, timestamp: float | None) -> _AlarmGroup | None:
        shape = message_shape(message)
        same = [g for g in self.groups.values() if g.severity == severity and g.count]
        for group in same:
            if group.key == shape or _contains(group.key, shape):
                return group
        near = [g for g in same if g.near(timestamp)]
        if timestamp is not None and near:
            return min(near, key=lambda g: abs((g.last or timestamp) - timestamp))
        return None


def _contains(a: str, b: str) -> bool:
    """One message inside the other: an echo carries the compact text, the raiser the full one."""
    shorter, longer = sorted((a, b), key=len)
    return len(shorter) >= _MIN_CONTAINED_CHARS and shorter in longer


def _device_text(device: Any) -> str | None:
    if device is None:
        return None
    return str(device)


# ------------------------------------------------------------------------------------------------
# The digest
# ------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Filters:
    service: str | None
    threshold: int
    text: str | None
    since: float | None

    def in_window(self, record: LogRecord) -> bool:
        """Whether the record falls inside the requested time window."""
        if self.since is None:
            return True
        return record.timestamp is not None and record.timestamp >= self.since

    def keep(self, record: LogRecord) -> bool:
        """Whether the record passes the service, level and text filters."""
        if self.service is not None and self.service not in (record.service or "").lower():
            return False
        if record.level_no < self.threshold:
            return False
        if self.text is None:
            return True
        return self.text in f"{record.message}\n{record.source or ''}".lower()

    def keep_client_alarm(self, alarm: Any) -> bool:
        """Whether an alarm from the client's stack passes the filters."""
        # stack entries carry no service, and raise_alarm logs MINOR and above as errors
        if self.service is not None or self.threshold > LOG_LEVELS["error"]:
            return False
        if self.text is None:
            return True
        info = getattr(getattr(alarm, "alarm", None), "info", None)
        fields = (
            getattr(info, "error_message", None),
            getattr(info, "compact_error_message", None),
            getattr(info, "exception_type", None),
            _device_text(getattr(info, "device", None)),
        )
        return any(self.text in str(value).lower() for value in fields if value)


@dataclass
class _Scan:
    """One pass over the stream: window bounds, counts, message groups and alarms."""

    filters: _Filters
    fetched: int = 0
    scanned: int = 0
    oldest: float | None = None
    newest: float | None = None
    matched: int = 0
    services_seen: dict[str, int] = field(default_factory=dict)
    level_counts: dict[str, int] = field(default_factory=dict)
    service_counts: dict[str, int] = field(default_factory=dict)
    groups: dict[tuple[str | None, str, str], _Group] = field(default_factory=dict)
    alarms: _AlarmMerger = field(default_factory=_AlarmMerger)
    dropped_client_alarms: int = 0

    def feed(self, order: int, message: Any) -> None:
        """Count one stream message and file it under a message group or an alarm."""
        self.fetched += 1
        record = parse_log_message(message)
        if record is None:
            return
        self.scanned += 1
        if record.timestamp is not None:
            self.oldest = min(self.oldest or record.timestamp, record.timestamp)
            self.newest = max(self.newest or record.timestamp, record.timestamp)
        if not self.filters.in_window(record):
            return
        name = record.service or "(unknown)"
        self.services_seen[name] = self.services_seen.get(name, 0) + 1
        if not self.filters.keep(record):
            return
        self.matched += 1
        self.level_counts[record.level] = self.level_counts.get(record.level, 0) + 1
        self.service_counts[name] = self.service_counts.get(name, 0) + 1
        alarm = parse_alarm_line(record.message)
        if alarm is not None:
            self.alarms.add_line(alarm, record, order)
            return
        key = (record.service, record.level, message_shape(record.message))
        group = self.groups.get(key)
        if group is None:
            group = self.groups[key] = _Group(
                record.service, record.level, record.level_no, record.source, record.message
            )
        group.add(record, order)

    def add_client_alarms(self, client_alarms: Iterable[Any]) -> None:
        """Merge the held echoes, then the alarms the server's client still holds."""
        self.alarms.finish_lines()
        for client_alarm in client_alarms:
            if not self.filters.keep_client_alarm(client_alarm):
                continue
            # an alarm without a log line has no time, so it can only be shown without a window
            if not self.alarms.add_client_alarm(client_alarm, self.filters.since is None):
                self.dropped_client_alarms += 1

    def complete(self, capacity: int) -> bool:
        """Whether the stream reaches back to the start of the requested window."""
        since = self.filters.since
        return since is None or self.fetched < capacity or (self.oldest or since + 1) <= since

    def levels(self) -> dict[str, int]:
        """Matched records per level, most severe first."""
        return dict(
            sorted(self.level_counts.items(), key=lambda kv: -LOG_LEVELS.get(kv[0].lower(), 0))
        )

    def newest_match(self, alarm_groups: list[_AlarmGroup]) -> float | None:
        """Time of the newest matching record or alarm."""
        times = [g.last for g in self.groups.values() if g.last is not None]
        times += [g.last for g in alarm_groups if g.last is not None]
        return max(times) if times else None


def logs_digest(  # pylint: disable=too-many-arguments,too-many-locals
    messages: Iterable[Any],
    client_alarms: Iterable[Any] = (),
    *,
    service: str | None = None,
    level: str | None = None,
    text: str | None = None,
    minutes: float | None = None,
    limit: int = DEFAULT_LIMIT,
    detail: Detail = "short",
    now: float | None = None,
    stream_capacity: int = LOG_STREAM_CAPACITY,
) -> dict[str, Any]:
    """Filter and condense log messages (oldest first) into a bounded digest."""
    detail = _lowercase(detail)
    check_arguments(level, minutes, detail)
    now = time.time() if now is None else now
    scan = _Scan(
        _Filters(
            # "BECIPythonClient/4b9d1af8" (a service status name) logs as "BECIPythonClient"
            service=service.strip().split("/")[0].lower() or None if service else None,
            threshold=LOG_LEVELS[_lowercase(level)] if level else 0,
            text=text.strip().lower() if text and text.strip() else None,
            since=now - minutes * 60 if minutes is not None else None,
        )
    )
    for order, message in enumerate(messages):
        scan.feed(order, message)
    scan.add_client_alarms(client_alarms)

    limit = max(1, min(int(limit), MAX_LIMIT))
    entries, entries_cut = _select(list(scan.groups.values()), limit, detail, OUTPUT_CHAR_BUDGET)
    alarm_groups = sorted(
        (g for g in scan.alarms.groups.values() if g.count),
        key=lambda g: (g.order, _SEVERITY_ORDER.get(g.severity, 0)),
    )
    alarm_cut = max(0, len(alarm_groups) - MAX_ALARMS)
    alarm_groups = alarm_groups[alarm_cut:]
    complete = scan.complete(stream_capacity)
    data: dict[str, Any] = {
        "window": {
            "scanned": scan.scanned,
            "oldest": iso_time(scan.oldest),
            "newest": iso_time(scan.newest),
            "since": iso_time(scan.filters.since),
            "complete": complete,
        },
        "matched": scan.matched,
        "distinct_messages": len(scan.groups),
        "levels": scan.levels(),
        "services": dict(sorted(scan.service_counts.items(), key=lambda kv: -kv[1])[:20]),
        "alarms": [g.digest(detail) for g in alarm_groups],
        "entries": entries,
    }
    summary = [_match_sentence(scan, minutes, len(entries), entries_cut)]
    if alarm_groups:
        major = sum(1 for g in alarm_groups if g.severity == "MAJOR")
        more = f" (and {alarm_cut} older)" if alarm_cut else ""
        summary.append(f"{len(alarm_groups)} alarm(s){more}, {major} MAJOR; see findings.")
    newest_match = scan.newest_match(alarm_groups)
    if newest_match is not None:
        summary.append(f"Newest match {_age(now - newest_match)} ago.")
    if not complete and scan.oldest is not None:
        summary.append(
            f"The stream only reaches back {_age(now - scan.oldest)}; older records were trimmed."
        )
    if scan.dropped_client_alarms:
        summary.append(
            f"{scan.dropped_client_alarms} unhandled MAJOR alarm(s) held by the server's client "
            "have no log line in this view; call without minutes or filters to see them."
        )
    if entries_cut:
        summary.append("Narrow with level, service, text or minutes, or raise limit.")
    return tool_result(
        True,
        " ".join(summary) if scan.scanned else "The BEC log stream is empty.",
        findings=[group.finding(now) for group in reversed(alarm_groups)],
        data=data,
        truncated=entries_cut > 0 or alarm_cut > 0,
    )


def _match_sentence(scan: _Scan, minutes: float | None, shown: int, cut: int) -> str:
    window = f" from the last {minutes:g} min" if minutes is not None else ""
    if scan.matched == 0:
        sentence = f"No log records{window} match the filters ({scan.scanned} scanned)."
        if scan.filters.service is not None:
            present = ", ".join(sorted(scan.services_seen)[:15]) or "none"
            sentence += f" Services with records{window}: {present}."
        return sentence
    levels = ", ".join(f"{n} {level}" for level, n in scan.levels().items())
    shown_part = f"showing the newest {shown}" if cut else f"showing all {shown}"
    return (
        f"{scan.matched} records{window} match ({levels}) out of {scan.scanned} scanned, "
        f"collapsed into {len(scan.groups)} distinct messages; {shown_part}."
    )


def _select(
    groups: list[_Group], limit: int, detail: Detail, budget: int
) -> tuple[list[dict[str, Any]], int]:
    """Newest groups first until the limit or the character budget runs out; oldest first out."""
    chosen: list[dict[str, Any]] = []
    used = 0
    for group in sorted(groups, key=lambda g: g.order, reverse=True):
        if len(chosen) >= limit:
            break
        digest = group.digest(detail)
        cost = len(digest["message"]) + 80
        if chosen and used + cost > budget:
            break
        chosen.append(digest)
        used += cost
    chosen.reverse()
    return chosen, len(groups) - len(chosen)


def _age(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 90:
        return f"{seconds:.0f} s"
    if seconds < 90 * 60:
        return f"{seconds / 60:.0f} min"
    if seconds < 48 * 3600:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.1f} days"


def check_arguments(level: str | None, minutes: float | None, detail: str) -> None:
    """Refuse unusable arguments with a ValueError the agent can act on, before BEC is touched."""
    if level is not None and _lowercase(level) not in LOG_LEVELS:
        levels = ", ".join(n for n in LOG_LEVELS if not n.startswith("console"))
        raise ValueError(f"Unknown level {level!r}; use one of {levels}.")
    if minutes is not None and minutes <= 0:
        raise ValueError("minutes must be a positive number of minutes, or omitted for no window.")
    if _lowercase(detail) not in ("short", "full"):
        raise ValueError("detail must be 'short' or 'full'.")


# ------------------------------------------------------------------------------------------------
# The tool
# ------------------------------------------------------------------------------------------------


def logs(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    connection: BecConnection,
    service: str | None = None,
    level: str | None = None,
    text: str | None = None,
    minutes: float | None = None,
    limit: int = DEFAULT_LIMIT,
    detail: Detail = "short",
) -> dict[str, Any]:
    """bec_logs: read the cached records (or the stream) and the client's alarms, then digest.

    BEC being unreachable raises BecUnavailableError, a ConnectionError the runtime hands to
    the agent with its message, unless the cache still holds records.
    """
    check_arguments(level, minutes, detail)
    source = read_logs(connection, with_alarms=True)
    result = logs_digest(
        source.records,
        source.client_alarms,
        service=service,
        level=level,
        text=text,
        minutes=minutes,
        limit=limit,
        detail=detail,
        stream_capacity=source.capacity,
    )
    if source.note:
        result["summary"] = f"{source.note} {result['summary']}"
    return result


# ---------------------------------------------------------------------- MCP registration


def register(server: Any, connection: BecConnection, call: BecCall) -> None:
    """Expose bec_logs on ``server``; ``call`` offloads under the deadline."""

    @server.tool(
        name="bec_logs",
        description="Recent BEC log records and alarms, filtered by service, level, text and "
        "time window, with repeated messages collapsed into counts.",
        annotations=READ_ONLY_TOOL,
    )
    async def bec_logs(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        service: Annotated[
            str | None,
            Field(description="Only services whose name contains this, e.g. ScanServer."),
        ] = None,
        level: Annotated[
            LogLevelName | None,
            BeforeValidator(_lowercase),
            Field(description="Lowest level to include, e.g. error."),
        ] = None,
        text: Annotated[
            str | None, Field(description="Only records whose message contains this text.")
        ] = None,
        minutes: Annotated[
            float | None, Field(gt=0, description="Only records from the last N minutes.")
        ] = None,
        limit: Annotated[
            int, Field(ge=1, le=MAX_LIMIT, description="Most distinct messages to return.")
        ] = DEFAULT_LIMIT,
        detail: Annotated[
            Detail,
            BeforeValidator(_lowercase),
            Field(description="short: first and last line; full: whole message."),
        ] = "short",
    ) -> dict:
        return await call(logs, connection, service, level, text, minutes, limit, detail)
