"""Code mode: read-only access to the source code below the roots the operator allows.

The agent normally sees no source code. Code mode is off unless the operator starts the server
with ``--mode code`` and at least one ``--code-root``; its tools then read Python files below
those roots and nowhere else:

- ``bec_code_traceback`` resolves each frame of a Python traceback to the code around its line,
  with the whole function that raised;
- ``bec_code_read`` reads more: a file's outline, one function or class, or a range of lines;
- ``bec_code_search`` finds where a text, such as an error message or a name, occurs.

Every other tool's answer that shows a traceback then carries a finding that points at
``bec_code_traceback`` with a short ref or a log record id (``code_hints.py``), so the agent never
has to copy the traceback itself.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Callable

from mcp.server.mcpserver import MCPServer

from bec_mcp_server.runtime import CallPool
from bec_mcp_server.source.roots import SourceFile, SourceRoot, SourceTree, Symbol
from bec_mcp_server.tools import ANSWER_HOOK, READ_ONLY_TOOL, Finding, ToolContext, tool_result
from bec_mcp_server.tools.code_hints import TRACEBACK_REFS, TracebackHints
from bec_mcp_server.tools.tracebacks import Frame, RaisedException, Traceback, parse_tracebacks

logger = logging.getLogger(__name__)

#: code calls in flight; a pool of their own, so a slow file system never blocks BEC calls
CODE_POOL_CAPACITY = 2

#: characters of code in one answer, across all frames or lines
MAX_ANSWER_CODE_CHARS = 24_000
#: characters kept of one source line
MAX_LINE_CHARS = 240
#: lines shown either side of a frame's line
FRAME_CONTEXT = 3
#: lines either side for the frame that raised an earlier exception of a chain
CAUSE_CONTEXT = 8
#: lines of the raising function shown whole; a longer one is cut around the frame's line
MAX_FUNCTION_LINES = 150
#: lines either side of bec_code_read(line=...)
READ_CONTEXT = 20
#: the most lines one bec_code_read returns
MAX_READ_LINES = 200
#: files up to this many lines are returned whole when no part is asked for
WHOLE_FILE_LINES = 150
#: entries in a file outline
MAX_OUTLINE = 300
#: how far from a frame's line the same source text is looked for in another version
MOVED_SEARCH_LINES = 80
#: bec_code_search: default and largest number of matches, shortest text, and time budget
SEARCH_LIMIT = 20
MAX_SEARCH_LIMIT = 100
MIN_SEARCH_CHARS = 3
SEARCH_TIME_BUDGET_S = 10.0

#: function names Python prints for code that is not a def, which never match a symbol
_ANONYMOUS = ("<module>", "<lambda>", "<listcomp>", "<dictcomp>", "<setcomp>", "<genexpr>")


def register(server: MCPServer, context: ToolContext, hints: TracebackHints) -> None:
    """Register bec_code_traceback, bec_code_read and bec_code_search."""
    tree = context.code
    if tree is None:
        raise RuntimeError("code mode needs source roots on the tool context")
    timeout = context.settings.tool_timeout
    context.runtime.pools.setdefault("code", CallPool("code", CODE_POOL_CAPACITY, timeout))
    search_budget = min(SEARCH_TIME_BUDGET_S, timeout * 0.6)

    @server.tool(
        description="Resolve each frame of a Python traceback, pasted as text, given by the ref "
        "a finding names or by a log record id, to the BEC source code around its line, with the "
        "whole function that raised.",
        annotations=READ_ONLY_TOOL,
    )
    async def bec_code_traceback(traceback: str = "", ref: str = "", record: int = 0) -> dict:
        return await context.runtime.call(
            "code", _without_hints, traceback_call, tree, hints, traceback, ref, record
        )

    @server.tool(
        description="Read BEC source code below the allowed roots: a file's outline, one function "
        "or class by name, or the lines around a line number.",
        annotations=READ_ONLY_TOOL,
    )
    async def bec_code_read(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        path: str = "", symbol: str = "", line: int = 0, start: int = 0, end: int = 0
    ) -> dict:
        return await context.runtime.call(
            "code", _without_hints, read_answer, tree, path, symbol, line, start, end
        )

    @server.tool(
        description="Find the lines of BEC source code below the allowed roots that contain a "
        "text, such as an error message or a function name.",
        annotations=READ_ONLY_TOOL,
    )
    async def bec_code_search(text: str = "", path: str = "", limit: int = SEARCH_LIMIT) -> dict:
        return await context.runtime.call(
            "code", _without_hints, search_answer, tree, text, path, limit, search_budget
        )


def _without_hints(func: Callable[..., Any], /, *args: Any) -> Any:
    # this runs in the worker thread's copy of the call context, so the reset stays there:
    # code answers quote source lines, never a traceback another tool showed
    ANSWER_HOOK.set(None)
    return func(*args)


# ------------------------------------------------------------------------------------------------
# bec_code_traceback
# ------------------------------------------------------------------------------------------------


def traceback_call(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    tree: SourceTree, hints: TracebackHints, text: str, ref: str = "", record: int = 0
) -> dict[str, Any]:
    """The bec_code_traceback answer for pasted text, a ref or a log record; else the roots.

    Raises:
        LookupError: for a ref or record this server does not hold.
        ValueError: for text or a record without a traceback.
    """
    if text.strip():
        found = [parsed for parsed in parse_tracebacks(text) if parsed.frames]
        if not found:
            raise ValueError(
                "No traceback frames found: pass a Python traceback including its lines like "
                "'File \"...\", line 12, in run'. For an error message alone, bec_code_search "
                "finds where it is raised."
            )
        return traceback_answer(tree, found[0], others=len(found) - 1)
    if ref.strip():
        parsed = hints.refs.get(ref.strip())
        if parsed is None:
            raise LookupError(
                f"No traceback is stored under ref {ref.strip()!r}: refs come from findings of "
                f"this server run, and only the newest {TRACEBACK_REFS} are kept. Pass the "
                "traceback text instead."
            )
        return traceback_answer(tree, parsed)
    if record:
        parsed, about = hints.record_traceback(record)
        return traceback_answer(tree, parsed, record=about)
    return _overview(tree, "No traceback given")


@dataclass(eq=False)
class _FrameView:
    """A traceback frame and what the code under the roots says about it."""

    frame: Frame
    source: SourceFile | None
    #: which exception of the chain the frame belongs to, first exception first
    segment: int = 0
    lines: list[str] | None = None
    #: where the frame's source line is in this version of the file
    line_here: int | None = None
    check: str = "not checked"
    symbol: Symbol | None = None
    code: str | None = None
    code_range: tuple[int, int] | None = None
    code_cut: bool = False
    #: another frame whose code already covers this frame's line
    shown_with: int | None = None
    error: str | None = None

    def inspect(self, tree: SourceTree) -> None:
        """Read the file, check it is the code that ran and find the enclosing function."""
        if self.source is None:
            return
        try:
            self.lines = tree.read_lines(self.source)
        except (LookupError, ValueError, OSError) as exc:
            self.error = str(exc)
            return
        self.check, self.line_here = _compare(self.lines, self.frame)
        if self.line_here is not None:
            self.symbol = tree.enclosing(tree.symbols(self.source, self.lines), self.line_here)

    @property
    def mismatch(self) -> bool:
        """Whether this file is evidently not the code that raised."""
        if self.check in ("moved", "differs"):
            return True
        function = self.frame.function
        return bool(
            self.symbol is not None
            and function
            and function not in _ANONYMOUS
            and self.symbol.short_name != function.rsplit(".", 1)[-1]
        )

    def attach_code(self, budget: _Budget, context: int, *, whole_function: bool = False) -> bool:
        """Add the code around the frame's line if the budget allows; False if it did not."""
        if self.lines is None or self.line_here is None:
            return True
        center = self.line_here
        first, last = center - context, center + context
        if whole_function and self.symbol is not None:
            first, last = self.symbol.start, self.symbol.end
            if last - first + 1 > MAX_FUNCTION_LINES:
                half = MAX_FUNCTION_LINES // 2
                first, last = max(first, center - half), min(last, center + half)
                self.code_cut = True
        first, last = max(1, first), min(len(self.lines), last)
        code = render(self.lines, first, last, mark=center)
        if not budget.take(code):
            return False
        self.code, self.code_range = code, (first, last)
        return True

    def covered_by(self, others: list[_FrameView]) -> int | None:
        """The position in ``others`` of a frame whose code already shows this frame's line."""
        for position, other in enumerate(others):
            if (
                other.code_range is not None
                and other.source == self.source
                and self.line_here is not None
                and other.code_range[0] <= self.line_here <= other.code_range[1]
            ):
                return position
        return None

    def digest(self, index: int, *, chained: bool) -> dict[str, Any]:
        """The frame as the agent sees it."""
        frame = self.frame
        out: dict[str, Any] = {
            "frame": index,
            "file": self.source.display if self.source else _short_path(frame.file),
            "line": frame.line,
            "function": frame.function,
            "under_roots": self.source is not None,
        }
        if chained:
            out["exception_index"] = self.segment
        if frame.repeated:
            out["repeated"] = frame.repeated
        if self.source is None or self.check != "matches":
            out["traceback_source"] = frame.code
        if self.source is not None:
            if self.source.match == "tail":
                out["matched_by"] = "path tail"
            out["source_check"] = self.check
            if self.check == "moved":
                out["line_here"] = self.line_here
            if self.symbol is not None:
                out["in"] = self.symbol.name
            if self.code is not None:
                out["code"] = self.code
            elif self.shown_with is not None:
                out["code_shown_with_frame"] = self.shown_with
            if self.error is not None:
                out["error"] = self.error
        return out


def traceback_answer(
    tree: SourceTree, parsed: Traceback, *, others: int = 0, record: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Resolve a parsed traceback's frames to the code under the roots.

    ``others`` counts further tracebacks in the same text; ``record`` describes the log record
    the traceback came from.
    """
    views = [
        _FrameView(frame, tree.find(frame.file), segment)
        for segment, exception in enumerate(parsed.chain)
        for frame in exception.frames
    ]
    for view in views:
        view.inspect(tree)
    chained = len(parsed.chain) > 1
    focus, without_code = _attach_code(parsed, views)
    findings = _traceback_findings(tree, parsed, views, focus, without_code)
    if others:
        findings.append(
            Finding(
                "info",
                "more_tracebacks",
                f"The text holds {others + 1} tracebacks and only the first is resolved; pass "
                "another one on its own to resolve it",
                subject="traceback",
            )
        )
    data: dict[str, Any] = {"record": record} if record else {}
    data |= {
        "exception": _exception_line(parsed.raised),
        "exception_detail": _exception_detail(parsed.raised) or None,
        "frames": [view.digest(index, chained=chained) for index, view in enumerate(views)],
        "roots": tree.labels,
    }
    if chained:
        data["exceptions"] = [
            {
                "exception": _exception_line(exception),
                "relation": parsed.links[index - 1] if index else None,
            }
            for index, exception in enumerate(parsed.chain)
        ]
    if parsed.frames_cut:
        data["frames_cut"] = parsed.frames_cut
    summary = _traceback_summary(tree, parsed, views, focus)
    if record:
        summary = f"Log record {record['id']} ({record['service'] or 'unknown service'}): {summary}"
    return tool_result(
        True,
        summary,
        findings=findings,
        data=data,
        truncated=bool(without_code) or any(view.code_cut for view in views),
    )


def _attach_code(
    parsed: Traceback, views: list[_FrameView]
) -> tuple[list[_FrameView], list[_FrameView]]:
    """Give frames their code within the budget, most telling first.

    Returns the innermost readable frame of each exception, the one raised last first, and the
    frames left without code.
    """
    focus: list[_FrameView] = []
    for index in reversed(range(len(parsed.chain))):
        inner = [v for v in views if v.segment == index and v.lines is not None]
        if inner:
            focus.append(inner[-1])
    budget = _Budget(MAX_ANSWER_CODE_CHARS)
    without_code: list[_FrameView] = []
    for rank, view in enumerate(focus):
        if not view.attach_code(budget, CAUSE_CONTEXT, whole_function=rank == 0):
            without_code.append(view)
    for view in reversed(views):
        if view.lines is None or any(view is other for other in focus):
            continue
        covering = view.covered_by(views)
        if covering is not None:
            view.shown_with = covering
        elif not view.attach_code(budget, FRAME_CONTEXT):
            without_code.append(view)
    return focus, without_code


def _traceback_summary(
    tree: SourceTree, parsed: Traceback, views: list[_FrameView], focus: list[_FrameView]
) -> str:
    exception = _clip(_exception_line(parsed.raised), 300)
    detail = _exception_detail(parsed.raised)
    if detail:
        exception = f"{exception} ({_clip(detail.splitlines()[0], 200)})"
    if not focus:
        frames = "its only frame is" if len(views) == 1 else f"none of its {len(views)} frames is"
        return (
            f"{exception}: {frames} in a readable file under the allowed source roots "
            f"({', '.join(tree.labels)})."
        )
    raiser = focus[0]
    assert raiser.source is not None
    where = raiser.symbol.name if raiser.symbol else raiser.frame.function or "?"
    under_roots = sum(1 for view in views if view.source is not None)
    return (
        f"{exception} raised in {where} ({raiser.source.display}:{raiser.line_here}); "
        f"{under_roots} of {len(views)} frames are under the allowed source roots."
    )


def _traceback_findings(
    tree: SourceTree,
    parsed: Traceback,
    views: list[_FrameView],
    focus: list[_FrameView],
    without_code: list[_FrameView],
) -> list[Finding]:
    findings = []
    if not focus:
        outside = sorted({_where_outside(view.frame.file) for view in views} - {""})
        findings.append(
            Finding(
                "warning",
                "no_frame_under_roots",
                "The code that raised is not below the allowed source roots "
                f"({', '.join(tree.labels)}); its frames are in "
                f"{', '.join(outside[:6]) or 'files that are not Python source'}",
                subject="code roots",
            )
        )
    mismatched = [view for view in views if view.mismatch]
    if mismatched:
        first = mismatched[0]
        labels = sorted({view.source.root.label for view in mismatched if view.source})
        findings.append(
            Finding(
                "warning",
                "source_version_mismatch",
                f"{len(mismatched)} frames do not match the code under {', '.join(labels)}, so "
                "that code is a different version than the one that raised and lines may be off; "
                f"e.g. {first.frame.function or '?'} at line {first.frame.line}",
                subject=first.source.display if first.source else None,
            )
        )
    if len(parsed.chain) > 1 and len(focus) > 1:
        first_raise = focus[-1]
        findings.append(
            Finding(
                "info",
                "chained_exceptions",
                f"This traceback chains {len(parsed.chain)} exceptions; the first was "
                f"{_clip(_exception_line(parsed.chain[first_raise.segment]), 160)} in "
                f"{first_raise.symbol.name if first_raise.symbol else first_raise.frame.function}",
                subject=first_raise.source.display if first_raise.source else None,
            )
        )
    for view in focus[:1]:
        if view.code_cut and view.symbol is not None and view.source is not None:
            findings.append(
                Finding(
                    "info",
                    "function_cut",
                    f"{view.symbol.name} is {view.symbol.end - view.symbol.start + 1} lines long; "
                    "only the part around the frame's line is shown",
                    subject=view.source.display,
                    next_tool="bec_code_read",
                    next_args={"path": view.source.display, "symbol": view.symbol.name},
                )
            )
    if without_code:
        skipped = without_code[0]
        assert skipped.source is not None
        findings.append(
            Finding(
                "info",
                "frames_without_code",
                f"{len(without_code)} frames under the roots are listed without code to keep the "
                "answer short",
                subject=skipped.source.display,
                next_tool="bec_code_read",
                next_args={"path": skipped.source.display, "line": skipped.frame.line},
            )
        )
    for view in views:
        if view.source is not None and view.source.alternatives:
            findings.append(
                Finding(
                    "info",
                    "ambiguous_file",
                    f"{view.frame.file} matches {view.source.alternatives + 1} files under the "
                    f"roots equally well; {view.source.display} is shown",
                    subject=view.source.display,
                )
            )
            break
    if parsed.frames_cut:
        findings.append(
            Finding(
                "info",
                "frames_cut",
                f"{parsed.frames_cut} frames from the middle of a deep traceback are left out",
                subject="traceback",
            )
        )
    return findings


def _compare(lines: list[str], frame: Frame) -> tuple[str, int | None]:
    """Whether the file has the source line the traceback printed, and where."""
    in_file = 1 <= frame.line <= len(lines)
    if frame.code is None:
        return "not checked", frame.line if in_file else None
    wanted = _squash(frame.code)
    if in_file and _squash(lines[frame.line - 1]) == wanted:
        return "matches", frame.line
    for offset in range(1, MOVED_SEARCH_LINES + 1):
        for number in (frame.line - offset, frame.line + offset):
            if 1 <= number <= len(lines) and _squash(lines[number - 1]) == wanted:
                return "moved", number
    return "differs", frame.line if in_file else None


def _exception_line(exception: RaisedException) -> str:
    """``module.Type: first line of the message``, as Python printed it."""
    if exception.type == "?":
        return "An exception whose line is missing from the text"
    first = exception.message.splitlines()[0].strip() if exception.message else ""
    return f"{exception.type}: {first}" if first else exception.type


def _exception_detail(exception: RaisedException) -> str:
    """The message lines after the first, e.g. the device error BEC appends."""
    lines = exception.message.splitlines()[1:]
    return "\n".join(line.strip() for line in lines if line.strip())


def _squash(text: str) -> str:
    return " ".join(text.split())


def _short_path(path: str) -> str:
    """A foreign path without its host prefix: from site-packages or the stdlib on."""
    parts = PurePosixPath(path).parts
    for marker in ("site-packages", "dist-packages"):
        if marker in parts:
            return "/".join(parts[parts.index(marker) + 1 :])
    for index, part in enumerate(parts):
        if part.startswith("python3"):
            return "/".join(parts[index:])
    return path


def _where_outside(path: str) -> str:
    """What a frame outside the roots is in: a package, the standard library or a file name."""
    if path.startswith("<"):
        return ""
    parts = PurePosixPath(path).parts
    for marker in ("site-packages", "dist-packages"):
        if marker in parts[:-1]:
            return parts[parts.index(marker) + 1]
    if any(part.startswith("python3") for part in parts[:-1]):
        return "the Python standard library"
    return parts[-1] if parts else ""


# ------------------------------------------------------------------------------------------------
# bec_code_read
# ------------------------------------------------------------------------------------------------


def read_answer(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    tree: SourceTree, path: str, symbol: str = "", line: int = 0, start: int = 0, end: int = 0
) -> dict[str, Any]:
    """The bec_code_read answer; without a path, the roots.

    Raises:
        LookupError: for a file or symbol that does not exist under the roots.
        ValueError: for lines outside the file.
    """
    if not path.strip():
        return _overview(tree, "No file given")
    source = tree.resolve(path)
    lines = tree.read_lines(source)
    total = len(lines)
    symbols = tree.symbols(source, lines)
    data: dict[str, Any] = {"file": source.display, "lines_total": total}
    span = _read_span(source, total, symbols, symbol.strip(), line, start, end)
    if span is None:
        return _outline_answer(source, total, symbols, data)
    first, last, mark, what = span
    if isinstance(what, Symbol):
        data["symbol"] = {"name": what.name, "kind": what.kind}
    elif mark is not None and (enclosing := tree.enclosing(symbols, mark)) is not None:
        data["in"] = enclosing.name
    first, last = max(1, first), min(total, last)
    cut = last - first + 1 > MAX_READ_LINES
    if cut:
        last = first + MAX_READ_LINES - 1
    code = render(lines, first, last, mark=mark)
    if len(code) > MAX_ANSWER_CODE_CHARS:
        code, last = _fit(lines, first, last, mark)
        cut = True
    data["range"] = [first, last]
    data["code"] = code
    findings = []
    if cut:
        findings.append(
            Finding(
                "info",
                "read_cut",
                f"Only lines {first} to {last} are shown",
                subject=source.display,
                next_tool="bec_code_read",
                next_args={"path": source.display, "start": last + 1, "end": last + MAX_READ_LINES},
            )
        )
    where = f"{source.display} (lines {first} to {last} of {total})"
    if isinstance(what, Symbol):
        summary = f"The {what.kind} {what.name} in {where}."
    else:
        summary = f"{what.capitalize()} of {where}."
    return tool_result(True, summary, findings=findings, data=data, truncated=cut)


def _read_span(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    source: SourceFile,
    total: int,
    symbols: list[Symbol],
    symbol: str,
    line: int,
    start: int,
    end: int,
) -> tuple[int, int, int | None, Symbol | str] | None:
    """(first, last, marked line, what) for a read request; None asks for the outline."""
    if symbol:
        found = _find_symbol(symbols, symbol, source)
        return found.start, found.end, None, found
    if line:
        if not 1 <= line <= total:
            raise ValueError(f"{source.display} has {total} lines; line {line} is outside it")
        return line - READ_CONTEXT, line + READ_CONTEXT, line, f"line {line}"
    if start or end:
        first = max(1, start or 1)
        last = end or first + 2 * READ_CONTEXT
        if last < first:
            raise ValueError(f"end ({end}) is before start ({first})")
        if first > total:
            raise ValueError(f"{source.display} has {total} lines; line {first} is outside it")
        return first, last, None, "lines"
    if total <= WHOLE_FILE_LINES:
        return 1, total, None, "the whole file"
    return None


def _outline_answer(
    source: SourceFile, total: int, symbols: list[Symbol], data: dict[str, Any]
) -> dict[str, Any]:
    data["outline"] = [
        {"name": symbol.name, "kind": symbol.kind, "lines": [symbol.start, symbol.end]}
        for symbol in symbols[:MAX_OUTLINE]
    ]
    if len(symbols) > MAX_OUTLINE:
        data["outline_cut"] = len(symbols) - MAX_OUTLINE
    findings = []
    if symbols:
        findings.append(
            Finding(
                "info",
                "outline_only",
                "The file is long, so only its outline is shown",
                subject=source.display,
                next_tool="bec_code_read",
                next_args={"path": source.display, "symbol": symbols[0].name},
            )
        )
    summary = (
        f"Outline of {source.display}: {len(symbols)} functions and classes in {total} lines; "
        "read one with symbol, or lines with line or start and end."
    )
    return tool_result(True, summary, findings=findings, data=data)


def _find_symbol(symbols: list[Symbol], name: str, source: SourceFile) -> Symbol:
    exact = [symbol for symbol in symbols if symbol.name == name]
    if exact:
        return exact[0]
    short = name.rsplit(".", 1)[-1]
    same_end = [symbol for symbol in symbols if symbol.short_name == short]
    if len(same_end) == 1:
        return same_end[0]
    candidates = same_end or [
        symbol for symbol in symbols if short.lower() in symbol.short_name.lower()
    ]
    shown = ", ".join(symbol.name for symbol in (candidates or symbols)[:15])
    raise LookupError(
        f"{source.display} has no function or class {name!r}"
        + (f"; {'candidates' if candidates else 'it defines'}: {shown}" if shown else "")
    )


def _fit(lines: list[str], first: int, last: int, mark: int | None) -> tuple[str, int]:
    """Shrink a range from the end until its code fits the answer budget."""
    while last > first:
        last = max(first, last - max(1, (last - first) // 4))
        code = render(lines, first, last, mark=mark)
        if len(code) <= MAX_ANSWER_CODE_CHARS:
            return code, last
    return render(lines, first, first, mark=mark), first


# ------------------------------------------------------------------------------------------------
# bec_code_search
# ------------------------------------------------------------------------------------------------


def search_answer(
    tree: SourceTree,
    text: str,
    path: str = "",
    limit: int = SEARCH_LIMIT,
    time_budget: float = SEARCH_TIME_BUDGET_S,
) -> dict[str, Any]:
    """The bec_code_search answer: lines containing ``text``, case-insensitively.

    Raises:
        ValueError: for a text shorter than MIN_SEARCH_CHARS.
        LookupError: for a path outside every root.
    """
    if not text.strip():
        return _overview(tree, "No text given")
    needle = text.strip()
    if len(needle) < MIN_SEARCH_CHARS:
        raise ValueError(f"search for at least {MIN_SEARCH_CHARS} characters, not {needle!r}")
    limit = max(1, min(limit or SEARCH_LIMIT, MAX_SEARCH_LIMIT))
    scopes = _scopes(tree, path)
    scan = _Scan(tree, needle.casefold(), limit, time.monotonic() + time_budget)
    for root, prefix in scopes:
        if not scan.run(root, prefix):
            break
    matches = scan.matches
    # definitions of a name come before its uses, and code before its tests
    matches.sort(key=_search_rank)
    where = ", ".join(f"{root.label}/{'/'.join(prefix)}".rstrip("/") for root, prefix in scopes)
    summary = f"{len(matches)} lines contain {needle!r} in {scan.files_matched} files under {where}"
    if scan.more:
        summary += f"; more exist beyond the first {limit}, narrow the search with path"
    if scan.timed_out:
        summary += f"; the search stopped after {time_budget:g} s with {scan.files_read} files read"
    findings = []
    if matches:
        first = matches[0]
        findings.append(
            Finding(
                "info",
                "search_match",
                f"The first match is in {first.get('in') or first['file']}; read the code around it",
                subject=first["file"],
                next_tool="bec_code_read",
                next_args={"path": first["file"], "line": first["line"]},
            )
        )
    elif not scan.timed_out:
        findings.append(
            Finding(
                "info",
                "no_match",
                f"No Python file under {where} contains {needle!r}; a message built at run time "
                "(an f-string) only matches a fixed part of it, so search for a shorter piece",
                subject=needle,
            )
        )
    return tool_result(
        True,
        summary + ".",
        findings=findings,
        data={"text": needle, "matches": matches, "files_read": scan.files_read},
        truncated=scan.more or scan.timed_out,
    )


class _Scan:  # pylint: disable=too-few-public-methods
    """One bec_code_search pass over files, stopping at the match limit or the deadline."""

    def __init__(self, tree: SourceTree, folded: str, limit: int, deadline: float) -> None:
        self.tree = tree
        self.folded = folded
        self.limit = limit
        self.deadline = deadline
        self.matches: list[dict[str, Any]] = []
        self.files_read = self.files_matched = 0
        self.more = self.timed_out = False

    def run(self, root: SourceRoot, prefix: tuple[str, ...]) -> bool:
        """Scan one root below ``prefix``; False once the search must stop."""
        for source in self.tree.walk(root, prefix):
            if time.monotonic() > self.deadline:
                self.timed_out = True
                return False
            try:
                lines = self.tree.read_lines(source)
            except (LookupError, ValueError, OSError):
                continue
            self.files_read += 1
            hits = [n for n, content in enumerate(lines, 1) if self.folded in content.casefold()]
            if hits:
                self.files_matched += 1
                if not self._add(source, lines, hits):
                    return False
        return True

    def _add(self, source: SourceFile, lines: list[str], hits: list[int]) -> bool:
        symbols = self.tree.symbols(source, lines)
        for number in hits:
            if len(self.matches) >= self.limit:
                self.more = True
                return False
            match: dict[str, Any] = {
                "file": source.display,
                "line": number,
                "text": _clip(lines[number - 1].strip(), MAX_LINE_CHARS),
            }
            enclosing = self.tree.enclosing(symbols, number)
            if enclosing is not None:
                match["in"] = enclosing.name
            self.matches.append(match)
        return True


def _search_rank(match: dict[str, Any]) -> tuple[bool, bool]:
    parts = PurePosixPath(match["file"]).parts
    in_tests = any(part in ("tests", "test") for part in parts) or parts[-1].startswith("test_")
    return not match["text"].startswith(("def ", "async def ", "class ")), in_tests


def _scopes(tree: SourceTree, path: str) -> list[tuple[SourceRoot, tuple[str, ...]]]:
    path = path.strip().strip("/")
    if not path:
        return [(root, ()) for root in tree.roots]
    label, *rest = PurePosixPath(path).parts
    root = tree.root(label)
    if root is None or ".." in rest:
        raise LookupError(
            f"{path!r} is not under an allowed source root; give a root ({', '.join(tree.labels)}) "
            "or a directory or file below one, e.g. "
            f"{tree.labels[0]}/<directory>"
        )
    return [(root, tuple(rest))]


# ------------------------------------------------------------------------------------------------
# shared
# ------------------------------------------------------------------------------------------------


def _overview(tree: SourceTree, reason: str) -> dict[str, Any]:
    labels = tree.labels
    summary = (
        f"{reason}. Code mode reads Python files below {len(labels)} source roots: "
        f"{', '.join(labels)}. Pass a traceback to bec_code_traceback, name a file as "
        f"<root>/<path> for bec_code_read (e.g. {labels[0]}/<path>.py), or search with "
        "bec_code_search."
    )
    return tool_result(True, summary, data=tree.describe())


def render(lines: list[str], first: int, last: int, *, mark: int | None = None) -> str:
    """Numbered source lines; ``>`` marks the line a frame or request points at."""
    out = []
    for number in range(first, last + 1):
        content = lines[number - 1]
        if len(content) > MAX_LINE_CHARS:
            content = f"{content[:MAX_LINE_CHARS]}… [cut]"
        out.append(f"{'>' if number == mark else ' '}{number:>5}| {content}")
    return "\n".join(out)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


class _Budget:  # pylint: disable=too-few-public-methods
    def __init__(self, chars: int) -> None:
        self.left = chars

    def take(self, text: str) -> bool:
        """Spend ``len(text)`` if that much is left."""
        if len(text) > self.left:
            return False
        self.left -= len(text)
        return True
