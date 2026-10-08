"""Deterministic diagnostic rules over a snapshot of cached BEC state, and the merged report.

``tools/diagnose.py`` collects a ``diagnose_state.Snapshot`` through the BEC client. Every rule
here is a pure function of that snapshot: it reads nothing from BEC itself and returns
:class:`Finding` objects in plain words, each naming the tool to call next. The rules cover what
no other tool reports: one device's problems, the devices across the beamline, and recent log
errors. bec_diagnose merges them with the findings of bec_status, bec_queue,
bec_beamline_states, bec_scan_history and bec_logs (see :func:`tool_entries`), so each rule lives
in exactly one tool.
"""

from __future__ import annotations

import difflib
import math
import numbers
import re
from dataclasses import dataclass
from typing import Any, Sequence

from bec_mcp_server.bec.digest import short_service_name
from bec_mcp_server.tools import Finding
from bec_mcp_server.tools.diagnose_state import (
    ConfigReset,
    DeviceState,
    LogRecord,
    QueueState,
    ServiceState,
    Snapshot,
)

TOOL_STATUS = "bec_status"
TOOL_DEVICES = "bec_devices"
TOOL_DEVICE_READ = "bec_device_read"
TOOL_DEVICE_DIAGNOSE = "bec_device_diagnose"
TOOL_QUEUE = "bec_queue"
TOOL_LOGS = "bec_logs"

#: loguru's ERROR level number, as bec_lib's LogLevel defines it
ERROR_LEVEL = 40

_SEVERITY_RANK = {"error": 0, "warning": 1, "info": 2}
#: the areas bec_diagnose reports on, in the order a reader expects them; a finding's area is
#: the tool that reported it, or for this module's own findings the prefix of its code
AREAS = ("bec", "services", "devices", "queue", "states", "scans", "logs", "diagnose")
_AREA_OF_PREFIX = {"device": "devices"}

#: services that are user sessions or GUIs rather than part of BEC, as bec_status counts them
_CLIENT_MARKERS = ("client", "becwidgets", "pylsp")

#: names listed in one finding before switching to a count
_MAX_NAMES = 10

#: characters quoted from a log or alarm text; those texts come from other users and hardware
_QUOTE_CHARS = 160

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_WHITESPACE_RE = re.compile(r"\s+")

#: the device server logs this (as a warning or an error) for every device it disables
_INIT_FAILED_RE = re.compile(r"Failed to initiali[sz]e device:?\s+([A-Za-z_][A-Za-z0-9_]*)")


# ---------------------------------------------------------------------------
# formatting helpers
# ---------------------------------------------------------------------------


def _plural(count: int, word: str, plural: str | None = None) -> str:
    return f"{count} {word if count == 1 else plural or word + 's'}"


#: (from how many seconds, seconds per unit, unit) for ages, largest first
_AGE_UNITS = ((48 * 3600, 86400, "days"), (90 * 60, 3600, "h"), (90, 60, "min"))


def _ago(seconds: float) -> str:
    seconds = max(0.0, seconds)
    for threshold, unit, label in _AGE_UNITS:
        if seconds >= threshold:
            return f"{seconds / unit:.0f} {label}"
    return f"{seconds:.0f} s"


def _fmt(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def _names(names: Sequence[str]) -> str:
    shown = ", ".join(names[:_MAX_NAMES])
    if len(names) > _MAX_NAMES:
        shown += f" and {len(names) - _MAX_NAMES} more"
    return shown


def _quote(text: str) -> str:
    """The telling line of an untrusted text, cleaned, shortened and in quotes.

    That is the first non-empty line, or the exception at the end when the text is a traceback.
    """
    lines = [line.strip() for line in _ANSI_RE.sub("", text or "").splitlines() if line.strip()]
    if len(lines) > 1 and lines[0].startswith("Traceback"):
        lines = lines[-1:]
    line = _WHITESPACE_RE.sub(" ", lines[0]) if lines else ""
    if len(line) > _QUOTE_CHARS:
        line = line[: _QUOTE_CHARS - 1] + "…"
    return f'"{line}"'


def _normalize_service(name: str) -> str:
    # non-unique services publish as <class>/<uuid>; match on the class
    return (name or "").split("/", 1)[0].lower().replace("_", "").replace(" ", "")


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, numbers.Real)
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _mentions(text: str, name: str) -> bool:
    """True if ``text`` names the device ``name`` (signals such as ``samx_setpoint`` count)."""
    pattern = rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9])"
    return re.search(pattern, _ANSI_RE.sub("", text or "")) is not None


def _recent(timestamp: float | None, snapshot: Snapshot) -> bool:
    # records without a time are kept: they come from the tail of the stream
    return timestamp is None or snapshot.now - timestamp <= snapshot.window_s


def _failure_reason(text: str) -> str:
    """The most telling line of an init-failure log: the exception at the end of a traceback."""
    lines = [line.strip() for line in _ANSI_RE.sub("", text or "").splitlines() if line.strip()]
    if len(lines) > 1:
        return lines[-1]
    match = _INIT_FAILED_RE.search(text or "")
    rest = (text or "")[match.end() :] if match else text or ""
    return rest.strip(" :") or text


# ---------------------------------------------------------------------------
# shared derivations
# ---------------------------------------------------------------------------


def device_server_problem(services: Sequence[ServiceState] | None) -> str | None:
    """Why the device server's cached data cannot be trusted, or None if it is up or unknown."""
    if services is None:
        return None
    for service in services:
        if "deviceserver" in _normalize_service(service.name):
            if service.status == "ERROR":
                return "the device server reports ERROR"
            return None
    return "the device server is not running"


def queue_idle(queues: Sequence[QueueState] | None) -> bool | None:
    """True if no queue runs or holds a scan, None if the queues were not read."""
    if queues is None:
        return None
    return all(not queue.running and queue.pending == 0 for queue in queues)


def init_failures(snapshot: Snapshot) -> dict[str, str | None]:
    """Devices whose initialization failed, mapped to the reason if a log record gives one.

    A failure only counts while the device is not enabled: the device server disables every
    device it fails to initialize, so an enabled device was reloaded since.
    """
    failures: dict[str, str | None] = {}
    progress = snapshot.init_progress
    if progress is not None and progress.finished and not progress.success:
        failures[progress.device] = None
    for record in snapshot.logs or []:
        if not _recent(record.timestamp, snapshot):
            continue
        match = _INIT_FAILED_RE.search(record.message or "")
        if match:
            failures[match.group(1)] = _failure_reason(record.message)
    return {
        name: reason
        for name, reason in failures.items()
        if (device := snapshot.devices.get(name)) is None or device.enabled is not True
    }


def _recent_resets(snapshot: Snapshot) -> list[ConfigReset]:
    return [reset for reset in snapshot.config_resets if _recent(reset.timestamp, snapshot)]


def _reset_text(reset: ConfigReset, snapshot: Snapshot) -> str:
    when = (
        f"{_ago(snapshot.now - reset.timestamp)} ago" if reset.timestamp is not None else "recently"
    )
    return (
        f"The device config was reset {when}, which BEC does when loading a config fails or "
        "is cancelled."
    )


def _outside_limits(device: DeviceState) -> tuple[float, float] | None:
    if device.limits is None or not _is_number(device.readback):
        return None
    low, high = device.limits
    # equal limits (BEC reports [0, 0] when none are set) mean the motor has no soft limits
    if not (_is_number(low) and _is_number(high)) or not low < high:
        return None
    if device.readback < low or device.readback > high:
        return low, high
    return None


# ---------------------------------------------------------------------------
# rules
# ---------------------------------------------------------------------------


def device_findings(snapshot: Snapshot, name: str) -> list[Finding]:
    """Everything the cached state says is wrong with one device."""
    device = snapshot.devices.get(name)
    resets = [reset for reset in _recent_resets(snapshot) if name in reset.devices]
    if device is None:
        if resets:
            return [
                Finding(
                    "error",
                    "device.config_reset",
                    f"{name} is not loaded. {_reset_text(resets[-1], snapshot)}",
                    name,
                    TOOL_LOGS,
                    {"level": "error"},
                )
            ]
        return [_not_found(name, snapshot.device_names)]

    findings: list[Finding] = []
    failures = init_failures(snapshot)
    progress = snapshot.init_progress
    if name in failures:
        reason = failures[name]
        findings.append(
            Finding(
                "error",
                "device.init_failed",
                f"{name} failed to initialize and the device server disabled it."
                + (f" Reason: {_quote(reason)}." if reason else ""),
                name,
                TOOL_LOGS,
                {"text": name, "level": "warning"},
            )
        )
    elif device.enabled is False:
        findings.append(
            Finding(
                "warning",
                "device.disabled",
                f"{name} is disabled, so BEC does not use it until it is enabled again.",
                name,
                TOOL_LOGS,
                {"text": name},
            )
        )
    if progress is not None and progress.device == name and not progress.finished:
        findings.append(
            Finding(
                "info",
                "device.initializing",
                f"The device server is still initializing {name} "
                f"(device {progress.index + 1} of {progress.total}).",
                name,
                TOOL_STATUS,
            )
        )
    if resets:
        findings.append(
            Finding(
                "info",
                "device.config_reset",
                f"{_reset_text(resets[-1], snapshot)} {name} has been loaded again since.",
                name,
                TOOL_LOGS,
                {"level": "error"},
            )
        )

    if device.enabled is not False:
        findings.extend(_live_device_findings(snapshot, device))
    findings.extend(_device_alarm_findings(snapshot, name))
    findings.extend(_device_log_findings(snapshot, name, skip_init_failures=name in failures))
    return findings


def _not_found(name: str, device_names: Sequence[str] | None) -> Finding:
    similar = difflib.get_close_matches(name, list(device_names or []), n=3, cutoff=0.6)
    hint = f" Similar names: {', '.join(similar)}." if similar else ""
    return Finding(
        "error", "device.not_found", f"{name} is not a configured device.{hint}", name, TOOL_DEVICES
    )


def _live_device_findings(snapshot: Snapshot, device: DeviceState) -> list[Finding]:
    """Rules over cached readbacks, limits and moving status; only meaningful when enabled."""
    name = device.name
    findings: list[Finding] = []
    problem = device_server_problem(snapshot.services)
    if problem is not None:
        findings.append(
            Finding(
                "error",
                "device.data_unverified",
                f"{problem[0].upper()}{problem[1:]}, so {name}'s cached values may be out of date.",
                name,
                TOOL_STATUS,
            )
        )

    if device.readback_time is None and device.readback is None:
        findings.append(
            Finding(
                "warning" if device.is_positioner else "info",
                "device.no_readback",
                f"BEC has no cached readback for {name}; the device server has not published "
                "one since the device was loaded.",
                name,
                TOOL_LOGS,
                {"text": name},
            )
        )
    elif (
        device.readback_time is not None
        and snapshot.now - device.readback_time > snapshot.stale_after_s
    ):
        findings.append(
            Finding(
                "info",
                "device.readback_old",
                f"{name}'s readback last changed {_ago(snapshot.now - device.readback_time)} ago "
                f"(value {_fmt(device.readback)}). BEC publishes readbacks only when they change, "
                "which is normal for a device at rest; if it should be changing, check its "
                "hardware.",
                name,
                TOOL_DEVICE_READ,
                {"device": name},
            )
        )

    limits = _outside_limits(device)
    if limits is not None:
        findings.append(
            Finding(
                "warning",
                "device.outside_limits",
                f"{name} reads {_fmt(device.readback)}, outside its soft limits "
                f"[{_fmt(limits[0])}, {_fmt(limits[1])}]. Moves to targets outside the limits "
                "are refused; check whether the limits or the position are wrong.",
                name,
                TOOL_DEVICE_READ,
                {"device": name},
            )
        )

    if device.moving:
        if queue_idle(snapshot.queues):
            findings.append(
                Finding(
                    "warning",
                    "device.moving_without_scan",
                    f"{name} reports moving although no scan is running; it may be moved "
                    "outside BEC, or its moving flag is stuck.",
                    name,
                    TOOL_DEVICE_READ,
                    {"device": name},
                )
            )
        else:
            findings.append(
                Finding("info", "device.moving", f"{name} is moving.", name, TOOL_QUEUE)
            )
    return findings


def _device_alarm_findings(snapshot: Snapshot, name: str) -> list[Finding]:
    alarms = [
        alarm
        for alarm in snapshot.alarms or []
        if _recent(alarm.timestamp, snapshot)
        and (name in alarm.devices or _mentions(alarm.message, name))
    ]
    if not alarms:
        return []
    latest = alarms[-1]
    kind = " ".join(part for part in (latest.severity, latest.exception_type) if part)
    return [
        Finding(
            "error" if any(alarm.severity == "MAJOR" for alarm in alarms) else "warning",
            "device.alarms",
            f"{_plural(len(alarms), 'alarm')} about {name} in the last {_ago(snapshot.window_s)}; "
            f"latest: {kind} {_quote(latest.message)}.",
            name,
            TOOL_LOGS,
            {"text": name},
        )
    ]


def _device_log_findings(
    snapshot: Snapshot, name: str, *, skip_init_failures: bool
) -> list[Finding]:
    records = [
        record
        for record in snapshot.logs or []
        if record.level_no >= ERROR_LEVEL
        and _recent(record.timestamp, snapshot)
        and _mentions(record.message, name)
        and not (skip_init_failures and _is_init_failure_of(record, name))
    ]
    if not records:
        return []
    latest = records[-1]
    return [
        Finding(
            "warning",
            "device.log_errors",
            f"{_plural(len(records), 'error log record')} mention {name} in the last "
            f"{_ago(snapshot.window_s)}; latest{_from(latest, snapshot)}: "
            f"{_quote(latest.message)}.",
            name,
            TOOL_LOGS,
            {"text": name, "level": "error"},
        )
    ]


def _is_init_failure_of(record: LogRecord, name: str) -> bool:
    match = _INIT_FAILED_RE.search(record.message or "")
    return match is not None and match.group(1) == name


def _from(record: LogRecord, snapshot: Snapshot) -> str:
    parts = []
    if record.service:
        parts.append(f" from {record.service}")
    if record.timestamp is not None:
        parts.append(f" {_ago(snapshot.now - record.timestamp)} ago")
    return "".join(parts)


def device_overview_findings(snapshot: Snapshot) -> list[Finding]:
    """Beamline-wide device rules: config loaded, init failures, disabled, limits, moving."""
    findings: list[Finding] = []
    resets = _recent_resets(snapshot)
    if snapshot.device_names is not None and not snapshot.device_names:
        findings.append(
            Finding(
                "error",
                "devices.none_loaded",
                "No devices are loaded."
                + (f" {_reset_text(resets[-1], snapshot)}" if resets else ""),
                "devices",
                TOOL_LOGS,
                {"level": "error"},
            )
        )
    elif resets:
        findings.append(
            Finding(
                "info",
                "devices.config_reset",
                f"{_reset_text(resets[-1], snapshot)} A config has been loaded since.",
                "devices",
                TOOL_LOGS,
                {"level": "error"},
            )
        )

    progress = snapshot.init_progress
    if progress is not None and not (progress.finished and progress.index >= progress.total):
        findings.append(
            Finding(
                "info",
                "devices.loading",
                f"The device server is loading the device config: {progress.index} of "
                f"{progress.total} devices done.",
                "devices",
                TOOL_STATUS,
            )
        )

    failures = sorted(init_failures(snapshot))
    if failures:
        findings.append(
            Finding(
                "error",
                "devices.init_failed",
                f"{_plural(len(failures), 'device')} failed to initialize and "
                f"{'was' if len(failures) == 1 else 'were'} disabled: {_names(failures)}.",
                "devices",
                TOOL_DEVICE_DIAGNOSE,
                {"device": failures[0]},
            )
        )

    devices = [snapshot.devices[name] for name in sorted(snapshot.devices)]
    disabled = [d.name for d in devices if d.enabled is False and d.name not in failures]
    if disabled:
        findings.append(
            Finding(
                "info",
                "devices.disabled",
                f"{_plural(len(disabled), 'device')} {'is' if len(disabled) == 1 else 'are'} "
                f"disabled: {_names(disabled)}.",
                "devices",
                TOOL_DEVICE_DIAGNOSE,
                {"device": disabled[0]},
            )
        )

    enabled = [d for d in devices if d.enabled is not False]
    outside = [(d, limits) for d in enabled if (limits := _outside_limits(d)) is not None]
    if outside:
        details = [
            f"{d.name} ({_fmt(d.readback)} not in [{_fmt(low)}, {_fmt(high)}])"
            for d, (low, high) in outside
        ]
        findings.append(
            Finding(
                "warning",
                "devices.outside_limits",
                f"{_plural(len(outside), 'device')} read outside {'its' if len(outside) == 1 else 'their'} "
                f"soft limits: {_names(details)}.",
                "devices",
                TOOL_DEVICE_DIAGNOSE,
                {"device": outside[0][0].name},
            )
        )

    if queue_idle(snapshot.queues):
        moving = [d.name for d in enabled if d.moving]
        if moving:
            findings.append(
                Finding(
                    "warning",
                    "devices.moving_without_scan",
                    f"{_plural(len(moving), 'device')} report moving although no scan is "
                    f"running: {_names(moving)}. They may be moved outside BEC, or their moving "
                    "flag is stuck.",
                    "devices",
                    TOOL_DEVICE_DIAGNOSE,
                    {"device": moving[0]},
                )
            )
    return findings


def log_findings(snapshot: Snapshot) -> list[Finding]:
    """Recent error records; init failures already reported as devices are left out."""
    failures = init_failures(snapshot)
    records = [
        record
        for record in snapshot.logs or []
        if record.level_no >= ERROR_LEVEL
        and _recent(record.timestamp, snapshot)
        and not any(_is_init_failure_of(record, name) for name in failures)
    ]
    if not records:
        return []
    services = sorted({record.service for record in records if record.service})
    source = f" from {', '.join(services)}" if services else ""
    latest = records[-1]
    # one noisy service is the common case; point the next call straight at it
    args = {"service": services[0], "level": "error"} if len(services) == 1 else {"level": "error"}
    return [
        Finding(
            "warning",
            "logs.errors",
            f"{_plural(len(records), 'error log record')} in the last {_ago(snapshot.window_s)}"
            f"{source}; latest{_from(latest, snapshot)}: {_quote(latest.message)}.",
            "logs",
            TOOL_LOGS,
            args,
        )
    ]


def unavailable_findings(snapshot: Snapshot) -> list[Finding]:
    """Sources the tool could not read, so the diagnosis is incomplete."""
    return [
        Finding(
            "warning",
            "diagnose.incomplete",
            f"Could not check {source}: {_quote(error)}.",
            source,
            TOOL_STATUS,
        )
        for source, error in snapshot.unavailable.items()
    ]


# ---------------------------------------------------------------------------
# reports: this module's findings and other tools' findings in one ordered list
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    """One finding as a tool returns it: the sentence, its structured twin and its area."""

    severity: str
    area: str
    text: str
    twin: dict[str, Any] | None

    @property
    def message(self) -> str:
        """The sentence without its severity prefix and next call."""
        text = self.text.split(" Next: ", 1)[0]
        prefix, sep, rest = text.partition(": ")
        return rest if sep and prefix.lower() in _SEVERITY_RANK else text


def finding_entries(findings: Sequence[Finding], area: str | None = None) -> list[Entry]:
    """This module's findings as entries; the area defaults to the prefix of each code."""
    return [
        Entry(
            finding.severity,
            area or _AREA_OF_PREFIX.get(prefix := finding.code.split(".", 1)[0], prefix),
            finding.text(),
            finding.as_dict(),
        )
        for finding in findings
    ]


def tool_entries(area: str, result: dict[str, Any]) -> list[Entry]:
    """The findings another tool returned, paired with their twins in ``data["findings"]``.

    Text and twin pair up by position; if a tool returned plain sentences without twins, the
    severity is read from the sentence's ``<Severity>:`` prefix instead.
    """
    texts = [str(text) for text in result.get("findings") or []]
    twins = (result.get("data") or {}).get("findings") or []
    aligned = len(twins) == len(texts)
    entries = []
    for index, text in enumerate(texts):
        twin = twins[index] if aligned and isinstance(twins[index], dict) else None
        severity = (twin or {}).get("severity") or text.partition(":")[0].lower()
        entries.append(
            Entry(severity if severity in _SEVERITY_RANK else "warning", area, text, twin)
        )
    return entries


def order_entries(entries: Sequence[Entry]) -> list[Entry]:
    """Errors first, then warnings, then notes; within each, services before devices and so on."""
    ranked = sorted(
        enumerate(entries),
        key=lambda item: (
            _SEVERITY_RANK[item[1].severity],
            AREAS.index(item[1].area) if item[1].area in AREAS else len(AREAS),
            item[0],
        ),
    )
    return [entry for _, entry in ranked]


def summarize(entries: Sequence[Entry], subject: str) -> str:
    """One deterministic sentence or two: counts and the most important finding."""
    errors = sum(entry.severity == "error" for entry in entries)
    warnings = sum(entry.severity == "warning" for entry in entries)
    if not entries:
        return f"No problems found for {subject}."
    if not errors and not warnings:
        return (
            f"No problems found for {subject}; {_plural(len(entries), 'note')}. "
            f"First: {entries[0].message}"
        )
    counts = ", ".join(
        text
        for count, text in (
            (errors, _plural(errors, "error")),
            (warnings, _plural(warnings, "warning")),
        )
        if count
    )
    return f"{counts} for {subject}. Most important: {entries[0].message}"


def report(
    entries: Sequence[Entry], *, subject: str, data: dict[str, Any], max_findings: int
) -> dict[str, Any]:
    """The fixed tool shape (as tool_result builds it), entries ordered and capped."""
    ordered = order_entries(entries)
    shown = ordered[: max(1, max_findings)]
    data = dict(data)
    if any(entry.twin is not None for entry in shown):
        data["findings"] = [entry.twin for entry in shown]
    return {
        "ok": not any(entry.severity != "info" for entry in ordered),
        "summary": summarize(ordered, subject),
        "findings": [entry.text for entry in shown],
        "data": data,
        "truncated": len(shown) < len(ordered),
    }


def device_facts(snapshot: Snapshot, name: str) -> dict[str, Any]:
    """The cached facts the device rules looked at, for the report's ``data``."""
    device = snapshot.devices.get(name)
    if device is None:
        return {"device": name, "configured": False}
    facts: dict[str, Any] = {
        "device": name,
        "configured": True,
        "enabled": device.enabled,
        "readback": device.readback,
        "readback_age_s": (
            round(snapshot.now - device.readback_time, 1)
            if device.readback_time is not None
            else None
        ),
    }
    if device.limits is not None:
        facts["limits"] = list(device.limits)
    if device.moving is not None:
        facts["moving"] = device.moving
    return facts


def diagnose_device(snapshot: Snapshot, name: str, *, max_findings: int = 20) -> dict[str, Any]:
    """Report for one device: its findings and the facts they rest on."""
    findings = device_findings(snapshot, name) + unavailable_findings(snapshot)
    data = {**device_facts(snapshot, name), "window_s": snapshot.window_s}
    return report(finding_entries(findings), subject=name, data=data, max_findings=max_findings)


def _device_counts(snapshot: Snapshot) -> dict[str, int]:
    return {
        "configured": len(snapshot.device_names or []),
        "disabled": sum(device.enabled is False for device in snapshot.devices.values()),
    }


def diagnose_devices(snapshot: Snapshot, *, max_findings: int = 20) -> dict[str, Any]:
    """Report for all devices: the beamline-wide device rules, without services or queue."""
    findings = device_overview_findings(snapshot) + unavailable_findings(snapshot)
    data: dict[str, Any] = {"window_s": snapshot.window_s}
    if snapshot.device_names is not None:
        data["devices"] = _device_counts(snapshot)
    result = report(
        finding_entries(findings), subject="the devices", data=data, max_findings=max_findings
    )
    if not findings:
        result["summary"] += " Name a device to check its readback, limits, errors and alarms."
    return result


def overview(snapshot: Snapshot) -> dict[str, Any]:
    """A compact picture of services, devices and queues for bec_diagnose's ``data``."""
    data: dict[str, Any] = {"window_s": snapshot.window_s}
    if snapshot.services is not None:
        # clients (user sessions, GUIs, other MCP servers) are not part of BEC's health
        data["services"] = {
            short_service_name(service.name): service.status
            for service in sorted(snapshot.services, key=lambda service: service.name)
            if not any(marker in _normalize_service(service.name) for marker in _CLIENT_MARKERS)
        }
    if snapshot.device_names is not None:
        data["devices"] = _device_counts(snapshot)
    if snapshot.queues is not None:
        data["queues"] = {
            queue.name: {"status": queue.status, "pending": queue.pending}
            for queue in snapshot.queues
        }
    return data
