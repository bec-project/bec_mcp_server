"""The rules bec_log_analysis names a likely cause and a next step with.

Each rule recognises one known kind of failure in an incident's exception types and messages.
They are checked in order and the first match wins, so the specific ones come first: a Redis
timeout is about Redis, not a device. BEC wraps a device failure during a scan in a
DeviceInstructionError and that in a ScanAbortion, so a scan abort only counts as one when it is
the first exception of the chain, and a device failure no specific rule names still points at
the device. A rule only names the usual suspect; the records decide.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

Target = Literal["device", "services", "range"]


@dataclass(frozen=True)
class Rule:
    """A known kind of failure: how to recognise it, what usually causes it, what to try."""

    category: str
    pattern: re.Pattern
    cause: str
    suggestion: str
    #: where the finding points next: the device's diagnosis, the services, or the records
    target: Target = "range"
    #: match against the exception types only, not the messages
    types_only: bool = False
    #: match against the first exception of the chain only
    root_only: bool = False
    #: a finding severity overriding the records' level
    severity: str | None = None


def _rule(category: str, pattern: str, cause: str, suggestion: str, **kwargs: Any) -> Rule:
    return Rule(category, re.compile(pattern, re.IGNORECASE), cause, suggestion, **kwargs)


#: checked in order; the first rule that matches names the incident's category
RULES: tuple[Rule, ...] = (
    _rule(
        "limits",
        r"\bLimitError\b|not within (?:the )?limits|outside (?:of )?(?:its |the )?"
        r"(?:soft(?:ware)? )?limits",
        "a move was requested to a position outside the device's software limits",
        "move within the limits, or widen them if the target is intended "
        "(dev.<device>.limits = [low, high])",
        target="device",
    ),
    _rule(
        "redis",
        r"\bredis\b[^\n]{0,80}(?:connection|connect|refused|timeout|closed|lost|reset)|"
        r"connecting to [\w.\-]+:6379|\bBusyLoadingError\b",
        "a service lost, or could not open, its connection to BEC's Redis",
        "check that Redis is running and reachable from that service's host; restart the "
        "service if it does not reconnect",
        target="services",
    ),
    _rule(
        "epics",
        r"\bDisconnectedError\b|\bnot connected\b|failed to connect to PV|\bPV\b[^\n]{0,60}"
        r"(?:disconnect|not found|not connected)|\bChannelAccess|\bpyepics\b|\bcaproto\b",
        "an EPICS PV is disconnected: its IOC is down or not reachable from the device server",
        "check the IOC and the network (caget the PV from the device server's host), then "
        "reconnect or re-enable the device",
        target="device",
    ),
    _rule(
        "timeout",
        r"\b\w*Timeout(?:Error)?\b|timed out|did not (?:finish|complete|respond|reach)",
        "a device or service did not answer in time",
        "check that the device's controller or IOC responds and is not stuck; a stale readback "
        "points the same way",
        target="device",
    ),
    _rule(
        "unreachable",
        r"failed to connect to|could not connect to|connection refused|max retries exceeded|"
        r"\bNewConnectionError\b|name or service not known|nodename nor servname|\bSSLError\b",
        "a service cannot reach an endpoint it depends on, e.g. BEC Atlas, a detector or a "
        "file server",
        "check that the host named in the message is up and reachable from that service, or "
        "switch the integration off if this setup does not need it",
        target="services",
    ),
    _rule(
        "missing_module",
        r"\b(?:ModuleNotFoundError|ImportError)\b|No module named",
        "a Python module is missing from the environment the service runs in, often a plugin "
        "or an optional dependency",
        "install the missing package into that service's environment and restart the service",
        target="services",
    ),
    _rule(
        "permission",
        r"\bNOPERM\b|\bNoPermissionError\b|\bPermissionError\b|permission denied",
        "an account or file permission refused the operation",
        "check the Redis ACL of the account in the message, or the permissions of the path",
        target="services",
    ),
    _rule(
        "validation",
        r"\bValidationError\b|validation errors? for\b",
        "a message or configuration failed schema validation, often because services run "
        "different BEC versions or a config field has the wrong type",
        "compare the BEC versions of the services and check the field the error names",
        target="services",
    ),
    _rule(
        "unknown_device",
        r"\bdevice\b[^\n]{0,60}\b(?:not found|does not exist|unknown|is not (?:a valid|"
        r"available|loaded))|\b(?:no|unknown) device\b",
        "a device name that is not in the loaded session config was used",
        "check the spelling, and that the device is in the config that is loaded",
        target="device",
    ),
    _rule(
        "device_disabled",
        r"\bdevice\b[^\n]{0,60}\bdisabled\b|\bis disabled\b",
        "the device is disabled in this session",
        "enable it (dev.<device>.enabled = True) if it should take part",
        target="device",
    ),
    _rule(
        "file_io",
        r"\b(?:FileNotFoundError|IsADirectoryError|NotADirectoryError|FileExistsError)\b|"
        r"No space left on device|Read-only file system|\bh5py\b|\bHDF5\b|Unable to "
        r"(?:create|open) file",
        "reading or writing a file failed: the path, its permissions or the free disk space",
        "check that the data directory exists, is writable by the service and has space left",
        target="services",
    ),
    _rule(
        "device_init",
        r"failed to initiali[sz]e|initiali[sz]ation (?:of|failed)|\bDeviceConfigError\b|"
        r"\bwait_for_connection\b",
        "a device failed to initialize or connect when the config was loaded",
        "fix the device's config entry or its hardware connection, then reload the config",
        target="device",
    ),
    _rule(
        "scan_aborted",
        r"^(?:[\w.]+\.)?(?:ScanAbortion|KeyboardInterrupt)$",
        "the scan was stopped on request",
        "nothing to fix unless nobody meant to stop it; check who requested the abort",
        root_only=True,
        severity="info",
    ),
    _rule(
        "device_error",
        r"\bDeviceInstructionError\b|failed during the execution of the following instruction",
        "a device failed while carrying out a scan instruction; its own error follows in the "
        "message",
        "check the device named in the message, and the device server's records from that "
        "moment for its own traceback",
        target="device",
    ),
    _rule(
        "code_error",
        r"\b(?:AttributeError|TypeError|NameError|IndexError|KeyError|ValueError|"
        r"AssertionError|ZeroDivisionError|NotImplementedError|UnboundLocalError|"
        r"RecursionError|RuntimeError)\b",
        "an exception in code: a bug, or a value the code did not expect",
        "read the code at the origin frame and the records just before the first occurrence",
        types_only=True,
    ),
)

UNCLASSIFIED = Rule(
    "unclassified",
    re.compile(r"(?!)"),
    "no rule matches this failure",
    "read the full record and the records just before its first occurrence",
)

_DEVICE_PATTERNS = (
    re.compile(r"\bdevice[s]? ['\"]([A-Za-z_]\w*)['\"]"),
    re.compile(r"\bdevice ([A-Za-z_]\w*) (?:failed|is|was|did|not|has)\b"),
    re.compile(r"\"device\": \"([A-Za-z_]\w*)\""),
    re.compile(r"\bdev\.([A-Za-z_]\w*)\b"),
)
_NOT_DEVICES = frozenset({"server", "manager", "config", "the", "a", "an", "name", "names"})


def classify(types: str, text: str) -> Rule:
    """The first rule whose pattern matches.

    ``types`` holds the exception type names of the chain, separated by spaces, the first
    exception first; ``text`` the logged message and the exception messages.
    """
    root = types.split()[0] if types.strip() else ""
    for rule in RULES:
        if rule.root_only:
            haystack = root
        elif rule.types_only:
            haystack = types
        else:
            haystack = f"{types}\n{text}"
        if re.search(rule.pattern, haystack):
            return rule
    return UNCLASSIFIED


def devices_named(text: str) -> list[str]:
    """Device names a failure text mentions, in order of appearance."""
    found: list[str] = []
    for pattern in _DEVICE_PATTERNS:
        for match in pattern.finditer(text):
            name = match.group(1)
            if name.lower() not in _NOT_DEVICES and name not in found:
                found.append(name)
    return found
