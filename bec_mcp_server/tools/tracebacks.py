"""Python tracebacks in BEC log records or pasted text: frames, exception chain, and origin.

BEC services log most failures with ``traceback.format_exc()`` in the message, so the full
traceback is plain text in the record; loguru's own format marks the failing frame with ``>``
and draws variable notes under source lines. Text an agent passes on is often mangled further:
newlines escaped by JSON, colour codes, a log prefix before the header, the ``|`` gutters of an
exception group, or every line stripped of its indentation. This parser takes all of these,
line by line, and imports or reads nothing.

For each traceback it returns the chain of exceptions as Python printed it (``raise ... from``,
or an exception raised while handling another), each with its frames: file, line, function and
the source line under it. Each frame also names the package it belongs to and its path inside
that package (``bec_server/scan_server/scan_worker.py``), so a reader with its own checkout of
that package can open the file whatever directory the beamline installed it in. The *origin* is
the frame in BEC or plugin code closest to where the first exception was raised.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

TRACEBACK_HEADER = "Traceback (most recent call last):"

#: characters of input looked at; a traceback is a few kB, a pasted log can be much more
MAX_TRACEBACK_CHARS = 200_000
#: frames kept per traceback; deep recursion is cut in the middle, keeping both ends
MAX_FRAMES = 60
#: lines of an exception message kept after its first line
MAX_DETAIL_LINES = 20
#: frames shown per exception in a digest: the innermost matter most, the outermost give the
#: entry point
INNER_FRAMES = 8
OUTER_FRAMES = 2
#: characters kept of an exception message in a digest
MESSAGE_CHARS = 1500

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_HEADER_RE = re.compile(r"(?:Exception Group )?Traceback \(most recent call last\):")
#: a frame line: plain, with loguru's ``>`` marker, or inside an exception group's ``|`` gutter
_FRAME_RE = re.compile(
    r'^(?P<lead>[\s>]*)File "(?P<file>[^"]+)", line (?P<line>\d+)(?:, in (?P<function>.+?))?\s*$'
)
_CHAIN_MARKERS = {
    "During handling of the above exception, another exception occurred:": "context",
    "The above exception was the direct cause of the following exception:": "cause",
}
#: Python 3.11+ position markers under a source line, e.g. ``~~~~^^^^``
_CARET_RE = re.compile(r"^\s*[~^]+[~^\s]*$")
#: loguru's variable notes under a source line, drawn with box characters
_LOGURU_NOTE_RE = re.compile(r"^\s*[│└├┬┼─]")
_REPEAT_RE = re.compile(r"^\s*\[Previous line repeated (\d+) more times?\]")
#: an exception group's separators, e.g. ``+-+---------------- 1 ----------------``
_GROUP_SEPARATOR_RE = re.compile(r"^\s*\+[-+]*(?:\s*\d+\s*-+|-+)\s*$")
_GUTTER_RE = re.compile(r"^\s*[|+] ?")
#: the first line of an exception: ``KeyError: 'samx'``, ``bec_lib.x.DeviceError``, ...
_EXCEPTION_RE = re.compile(
    r"^[A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Warning|Iteration|Timeout|Group)"
    r"(?:\[\w+\])?(?::.*)?$|^[A-Za-z_][\w.]*: \S"
)
#: a bare class name, e.g. ``bec_server.scan_server.errors.ScanAbortion`` raised without a message
_BARE_NAME_RE = re.compile(r"^[A-Za-z_][\w.]*$")
_TYPE_RE = re.compile(r"^(?P<type>[A-Za-z_][\w.]*(?:\[\w+\])?)(?::(?P<message>.*))?$")

#: packages of the BEC ecosystem; a plugin's package name ends in ``_bec``
BEC_PACKAGES = frozenset(
    {
        "bec_lib",
        "bec_server",
        "bec_ipython_client",
        "bec_widgets",
        "bec_qthemes",
        "bec_launcher",
        "ophyd_devices",
        "bec_mcp_server",
    }
)
_PLUGIN_RE = re.compile(r"^[A-Za-z_]\w*_bec$")
_STDLIB_RE = re.compile(r"/lib/python3\.\d+/(?!site-packages/|dist-packages/)")


# ------------------------------------------------------------------------------------------------
# The parsed form
# ------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Frame:
    """One ``File "...", line N, in func`` entry and the source line Python printed under it."""

    file: str
    line: int
    function: str | None
    code: str | None = None
    #: ``[Previous line repeated N more times]`` after this frame
    repeated: int = 0

    @property
    def package(self) -> str | None:
        """``bec_server``, a plugin such as ``debye_bec``, ``stdlib``, a third-party package, or None."""
        return locate(self.file)[0]

    @property
    def path(self) -> str | None:
        """The file's path inside its package, e.g. ``bec_server/scan_server/scan_worker.py``."""
        return locate(self.file)[1]

    @property
    def in_bec(self) -> bool:
        """Whether the frame is in BEC or beamline plugin code rather than stdlib or a library."""
        package = self.package
        return package is not None and (package in BEC_PACKAGES or bool(_PLUGIN_RE.match(package)))

    def digest(self) -> dict[str, Any]:
        """The frame as the agent sees it."""
        out: dict[str, Any] = {"file": self.file, "line": self.line, "function": self.function}
        if self.code:
            out["code"] = self.code
        if self.repeated:
            out["repeated"] = self.repeated
        package, path = locate(self.file)
        if package:
            out["package"] = package
        if path:
            out["path"] = path
        return out


@dataclass(frozen=True)
class RaisedException:
    """One exception of a chain with the frames Python printed for it."""

    type: str
    message: str
    frames: tuple[Frame, ...]

    @property
    def short_type(self) -> str:
        """The class name without its module: ``DeviceInstructionError``."""
        return self.type.rsplit(".", 1)[-1]

    def text(self) -> str:
        """``Type: first line of the message``."""
        first = self.message.splitlines()[0] if self.message else ""
        return f"{self.short_type}: {first}" if first else self.short_type


@dataclass(frozen=True)
class Traceback:
    """A printed exception chain; ``chain[0]`` is the first exception, ``chain[-1]`` the last.

    ``links[i]`` says how ``chain[i + 1]`` relates to ``chain[i]``: ``"cause"`` for
    ``raise ... from``, ``"context"`` for an exception raised while handling the previous one.
    An exception whose type line is missing (a cut-off paste) has the type ``"?"``.
    """

    chain: tuple[RaisedException, ...]
    links: tuple[str | None, ...]
    #: text before the traceback, usually what the code logged about the failure
    prefix: str = ""
    #: frames dropped from the middle of a deep traceback
    frames_cut: int = 0

    @property
    def raised(self) -> RaisedException:
        """The exception that ended up being logged (the last one Python printed)."""
        return self.chain[-1]

    @property
    def root(self) -> RaisedException:
        """The first exception of the chain, usually closest to the real cause."""
        return self.chain[0]

    @property
    def frames(self) -> list[Frame]:
        """Every frame, first exception first, outermost call first within each."""
        return [frame for exception in self.chain for frame in exception.frames]

    def origin(self) -> Frame | None:
        """The innermost frame in BEC or plugin code, searching from the first exception on.

        That is where BEC code called into what failed, or raised it itself; when no frame is
        BEC code, the innermost frame at all.
        """
        for exception in self.chain:
            for frame in reversed(exception.frames):
                if frame.in_bec:
                    return frame
        for exception in self.chain:
            if exception.frames:
                return exception.frames[-1]
        return None

    def key(self) -> str:
        """A short stable id for this traceback's exception and frames."""
        parts = [self.raised.type]
        parts.extend(f"{frame.file}:{frame.line}" for frame in self.frames)
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:10]

    def digest(self) -> dict[str, Any]:
        """The traceback as the agent sees it: the chain with bounded frames, and the origin."""
        chain = []
        for index, exception in enumerate(self.chain):
            frames = exception.frames
            entry: dict[str, Any] = {
                "type": exception.type,
                "message": _clip(exception.message, MESSAGE_CHARS),
            }
            if index and self.links[index - 1]:
                entry["link"] = self.links[index - 1]
            if len(frames) > INNER_FRAMES + OUTER_FRAMES:
                entry["frames"] = [f.digest() for f in frames[:OUTER_FRAMES]] + [
                    f.digest() for f in frames[-INNER_FRAMES:]
                ]
                entry["frames_skipped"] = len(frames) - INNER_FRAMES - OUTER_FRAMES
            else:
                entry["frames"] = [f.digest() for f in frames]
            chain.append(entry)
        origin = self.origin()
        out = {
            "exception": self.raised.type,
            "message": _clip(self.raised.message, MESSAGE_CHARS),
            "root_cause": self.root.text() if len(self.chain) > 1 else None,
            "origin": origin.digest() if origin is not None else None,
            "chain": chain,
        }
        if self.frames_cut:
            out["frames_cut"] = self.frames_cut
        return out


# ------------------------------------------------------------------------------------------------
# Parsing
# ------------------------------------------------------------------------------------------------


def normalize(text: str) -> str:
    """Undo what transport did to a traceback: escaped newlines, colour codes, CRLF."""
    text = (text or "")[:MAX_TRACEBACK_CHARS]
    if "\n" not in text and "\\n" in text:
        # copied out of a JSON string or a repr()
        text = text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t")
        text = text.replace('\\"', '"').replace("\\'", "'")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return _ANSI_RE.sub("", text)


def has_frames(text: str) -> bool:
    """Cheap test whether ``text`` may hold a traceback frame, before parsing it."""
    return 'File "' in text and ", line " in text


def might_hold_traceback(text: str) -> bool:
    """Cheap test whether ``text`` may hold a traceback, a header or frames."""
    return "Traceback (most recent call last)" in text or has_frames(text)


def parse_tracebacks(text: str) -> list[Traceback]:
    """Every traceback in ``text``, in order; text around them is ignored."""
    normalized = normalize(text)
    parser = _Parser()
    for raw in normalized.split("\n"):
        parser.feed(raw)
    tracebacks = [tb for tb in (_build(group) for group in parser.groups) if tb is not None]
    if tracebacks:
        first_header = _HEADER_RE.search(normalized)
        prefix = normalized[: first_header.start()].strip() if first_header else ""
        tracebacks[0] = _replace_prefix(tracebacks[0], prefix)
    return tracebacks


def parse_traceback(text: str) -> Traceback | None:
    """The first traceback in ``text``; None when it holds none."""
    if not might_hold_traceback(text):
        return None
    tracebacks = parse_tracebacks(text)
    return tracebacks[0] if tracebacks else None


def remove_traceback(text: str) -> str:
    """What was logged before the traceback; the whole text when there is none."""
    match = _HEADER_RE.search(text)
    if match is None:
        return text
    return text[: match.start()].strip()


@dataclass
class _Segment:
    """One exception of a chain while it is being read."""

    frames: list[dict[str, Any]] = field(default_factory=list)
    exception: str | None = None
    detail: list[str] = field(default_factory=list)
    relation: str | None = None


class _Parser:  # pylint: disable=too-few-public-methods
    """A line-by-line reader; each group of segments is one traceback."""

    def __init__(self) -> None:
        self.groups: list[list[_Segment]] = []
        self.current: _Segment | None = None
        self.frame: dict[str, Any] | None = None
        self.frame_column = 0
        self.pending_relation: str | None = None

    def feed(self, raw: str) -> None:
        """Take one line of the text."""
        if _GROUP_SEPARATOR_RE.match(raw):
            self._close()
            return
        line = _GUTTER_RE.sub("", raw, count=1) if _GUTTER_RE.match(raw) else raw
        stripped = line.strip()
        if not stripped:
            return
        if _HEADER_RE.search(line):
            self._open()
            return
        if stripped in _CHAIN_MARKERS:
            self._close()
            self.pending_relation = _CHAIN_MARKERS[stripped]
            return
        match = _FRAME_RE.match(line)
        if match:
            self._frame(match)
            return
        if self.frame is not None and self._under_frame(line, stripped):
            return
        current = self.current
        if current is None or not current.frames:
            return
        column = len(line) - len(line.lstrip())
        if current.exception is None:
            if _is_exception_line(stripped, column, self.frame_column):
                current.exception = stripped
                self.frame = None
        elif len(current.detail) < MAX_DETAIL_LINES:
            current.detail.append(stripped)

    def _frame(self, match: re.Match[str]) -> None:
        if self.current is None or self.current.exception is not None:
            # frames without a header: a partial paste
            self._open()
        assert self.current is not None
        self.frame = {
            "file": match["file"],
            "line": int(match["line"]),
            "function": (match["function"] or "").strip() or None,
            "code": None,
            "repeated": 0,
        }
        self.current.frames.append(self.frame)
        self.frame_column = len(match["lead"].replace(">", " "))

    def _under_frame(self, line: str, stripped: str) -> bool:
        """Handle a line printed under a frame: source, position markers, notes, repeats."""
        assert self.frame is not None
        repeat = _REPEAT_RE.match(line)
        if repeat:
            self.frame["repeated"] = int(repeat[1])
            return True
        if _CARET_RE.match(line) or _LOGURU_NOTE_RE.match(line):
            return True
        indented = len(line) - len(line.lstrip()) > self.frame_column
        if self.frame["code"] is None:
            if indented:
                self.frame["code"] = stripped
                return True
            if self.frame_column == 0 and not _EXCEPTION_RE.match(stripped):
                # a paste stripped of its indentation: the line after a frame is its source
                self.frame["code"] = stripped
                return True
        return indented

    def _open(self) -> None:
        segment = _Segment(relation=self.pending_relation)
        if self.pending_relation is None or not self.groups:
            # a header without a chain marker before it starts a new traceback
            self.groups.append([])
            segment.relation = None
        self.groups[-1].append(segment)
        self.current = segment
        self.frame = None
        self.pending_relation = None

    def _close(self) -> None:
        self.current = None
        self.frame = None


def _is_exception_line(stripped: str, column: int, frame_column: int) -> bool:
    if _EXCEPTION_RE.match(stripped):
        return True
    # a class raised without a message, e.g. ``raise ScanAbortion``, prints as its bare name
    return (
        column == 0
        and bool(_BARE_NAME_RE.match(stripped))
        and ("." in stripped or frame_column > 0)
    )


def _build(segments: list[_Segment]) -> Traceback | None:
    """A Traceback from the segments read for it; None when nothing usable was read."""
    segments = [s for s in segments if s.frames or s.exception]
    if not segments:
        return None
    total = sum(len(s.frames) for s in segments)
    keep = _kept_frames(total)
    chain: list[RaisedException] = []
    links: list[str | None] = []
    index = 0
    for number, segment in enumerate(segments):
        frames = []
        for frame in segment.frames:
            if keep is None or index in keep:
                frames.append(Frame(**frame))
            index += 1
        type_name, message = _split_exception(segment.exception)
        if segment.detail:
            detail = "\n".join(segment.detail)
            message = f"{message}\n{detail}" if message else detail
        chain.append(RaisedException(type=type_name, message=message, frames=tuple(frames)))
        if number:
            links.append(segment.relation)
    return Traceback(chain=tuple(chain), links=tuple(links), frames_cut=max(0, total - MAX_FRAMES))


def _kept_frames(total: int) -> set[int] | None:
    """Indices of the frames kept from a deep traceback; None keeps them all."""
    if total <= MAX_FRAMES:
        return None
    head = MAX_FRAMES // 4
    return set(range(head)) | set(range(total - (MAX_FRAMES - head), total))


def _split_exception(line: str | None) -> tuple[str, str]:
    if line is None:
        return "?", ""
    match = _TYPE_RE.match(line)
    if match is None:
        return "?", line
    return match["type"], (match["message"] or "").strip()


def _replace_prefix(traceback: Traceback, prefix: str) -> Traceback:
    return Traceback(
        chain=traceback.chain, links=traceback.links, prefix=prefix, frames_cut=traceback.frames_cut
    )


# ------------------------------------------------------------------------------------------------
# Where a file belongs
# ------------------------------------------------------------------------------------------------


def locate(file: str) -> tuple[str | None, str | None]:
    """The package a source file belongs to and its path inside that package.

    ``.../site-packages/bec_lib/client.py`` and a source checkout
    ``.../bec/bec_lib/bec_lib/client.py`` both give ``("bec_lib", "bec_lib/client.py")``.
    """
    posix = file.replace("\\", "/")
    for marker in ("/site-packages/", "/dist-packages/"):
        if marker in posix:
            relative = posix.rsplit(marker, 1)[1]
            return relative.split("/")[0].removesuffix(".py"), relative
    if _STDLIB_RE.search(posix):
        return "stdlib", _STDLIB_RE.split(posix, maxsplit=1)[1]
    parts = PurePosixPath(posix).parts
    # a source checkout: <repo>/<package>/<package>/module.py, so take the last match
    for position in range(len(parts) - 2, -1, -1):
        part = parts[position]
        if part in BEC_PACKAGES or _PLUGIN_RE.match(part):
            return part, "/".join(parts[position:])
    return None, None


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[:limit]}… [cut]"
