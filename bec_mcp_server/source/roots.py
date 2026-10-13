"""The source roots an operator allows in code mode, and every file access under them.

Nothing outside these roots is ever read. A path is accepted only when its real location (after
resolving symlinks) is a regular Python file inside a root's real location, so ``..``, absolute
paths and symlinks pointing out of a root are all refused the same way. Reads are sized: files
above :data:`MAX_FILE_BYTES` are refused, and callers cap how many lines they return.

Tracebacks name files by the path on the host that raised them, which is rarely the path under
a root here: a service may run from a deployed venv while the root is a checkout. Such paths are
matched by their longest common tail with a file under a root (``bec_server/scan_server/
scan_worker.py``), using an index of each root's Python files.
"""

from __future__ import annotations

import ast
import collections
import os
import re
import stat
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator, Sequence

#: the only files code mode reads
SOURCE_SUFFIXES = (".py",)

#: larger files are refused; generated code of this size explains nothing
MAX_FILE_BYTES = 2_000_000

#: Python files indexed per root; a whole site-packages is well below this
MAX_INDEXED_FILES = 200_000

#: seconds before a root is walked again, so edits in a checkout are picked up
INDEX_TTL_S = 300.0

#: directories never walked: caches and dependency trees that hold no BEC code
_SKIPPED_DIRS = frozenset({"__pycache__", "node_modules"})

#: parsed files kept for symbol lookups
_SYMBOL_CACHE_SIZE = 64

_MODULE_RE = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+$")


@dataclass(frozen=True)
class SourceRoot:
    """A directory the agent may read below, shown to it only by its label."""

    label: str
    path: Path


@dataclass(frozen=True)
class SourceFile:
    """A Python file under a root."""

    root: SourceRoot
    rel: PurePosixPath
    #: how the request was matched: "path" (as given) or "tail" (by the end of a foreign path)
    match: str = "path"
    #: other files that matched equally well
    alternatives: int = 0

    @property
    def display(self) -> str:
        """How the agent names this file: ``<root label>/<path in the root>``."""
        return f"{self.root.label}/{self.rel.as_posix()}"

    @property
    def location(self) -> Path:
        """Where the file is on this host; never shown to the agent."""
        return self.root.path.joinpath(*self.rel.parts)


@dataclass(frozen=True)
class Symbol:
    """A function or class in a file, with the lines it spans."""

    name: str
    kind: str
    #: first line, including decorators
    start: int
    end: int

    @property
    def short_name(self) -> str:
        """The name without its class, as a traceback prints it."""
        return self.name.rsplit(".", 1)[-1]


@dataclass
class _Index:
    """The Python files of one root, by file name."""

    by_name: dict[str, list[tuple[str, ...]]] = field(default_factory=dict)
    files: int = 0
    complete: bool = True
    built_at: float = 0.0


class SourceTree:
    """The allowed roots, with path resolution, sized reads and symbol lookup."""

    def __init__(
        self,
        roots: Sequence[SourceRoot],
        *,
        index_ttl: float = INDEX_TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not roots:
            raise ValueError("code mode needs at least one source root")
        self.roots = tuple(
            SourceRoot(root.label, Path(os.path.realpath(root.path))) for root in roots
        )
        self._by_label = {root.label: root for root in self.roots}
        self._index_ttl = index_ttl
        self._clock = clock
        self._indexes: dict[str, _Index] = {}
        self._index_locks = {root.label: threading.Lock() for root in self.roots}
        self._symbols: collections.OrderedDict[Path, tuple[tuple[int, int], list[Symbol]]] = (
            collections.OrderedDict()
        )
        self._symbols_lock = threading.Lock()

    # --------------------------------------------------------------------------------------
    # what the agent may see about the roots
    # --------------------------------------------------------------------------------------

    @property
    def labels(self) -> list[str]:
        """The roots' labels, in the operator's order."""
        return [root.label for root in self.roots]

    def describe(self) -> dict[str, Any]:
        """Labels and index state, without the host paths; never walks a root."""
        roots = []
        for root in self.roots:
            index = self._indexes.get(root.label)
            entry: dict[str, Any] = {"label": root.label}
            if index is not None:
                entry["python_files"] = index.files
                if not index.complete:
                    entry["index_complete"] = False
            roots.append(entry)
        return {"roots": roots}

    # --------------------------------------------------------------------------------------
    # resolving what the agent or a traceback names
    # --------------------------------------------------------------------------------------

    def find(self, name: str) -> SourceFile | None:
        """The file ``name`` refers to, or None if it is not a file under a root.

        ``name`` may be a display path (``bec/bec_lib/bec_lib/client.py``), a path relative to
        a root, an absolute path on this host or another one, or a dotted module name.
        """
        name = (name or "").strip().strip("'\"")
        if not name or name.startswith("<"):
            # <string>, <frozen importlib._bootstrap>, <ipython-input-...>: not files
            return None
        if _MODULE_RE.match(name) and not name.endswith(SOURCE_SUFFIXES):
            parts = tuple(name.split("."))
            return self._tail_match(parts[:-1] + (f"{parts[-1]}.py",)) or self._tail_match(
                parts + ("__init__.py",)
            )
        pure = PurePosixPath(name)
        if pure.suffix not in SOURCE_SUFFIXES:
            return None
        found = self._absolute(Path(name)) if pure.is_absolute() else self._relative(pure)
        if found is not None:
            return found
        return self._tail_match(tuple(part for part in pure.parts if part not in ("/", "..", ".")))

    def resolve(self, name: str) -> SourceFile:
        """Like :meth:`find`, but explain a miss.

        Raises:
            LookupError: naming the roots and files with the same name, if any.
        """
        found = self.find(name)
        if found is not None:
            return found
        basename = PurePosixPath((name or "").strip()).name
        similar = self.files_named(basename)[:5] if basename.endswith(SOURCE_SUFFIXES) else []
        hint = f" Files with that name: {', '.join(similar)}." if similar else ""
        raise LookupError(
            f"{name!r} is not a Python file under the allowed source roots "
            f"({', '.join(self.labels)}).{hint} Name files as <root>/<path>, e.g. "
            f"{self.labels[0]}/<path>.py, or give a dotted module name."
        )

    def files_named(self, basename: str) -> list[str]:
        """Display paths of every indexed file called ``basename``."""
        found = []
        for root in self.roots:
            for parts in self._index(root).by_name.get(basename, ()):
                found.append(f"{root.label}/{'/'.join(parts)}")
        return found

    def _relative(self, rel: PurePosixPath) -> SourceFile | None:
        """A display path (``<label>/...``), else a path relative to any root."""
        label, *rest = rel.parts
        root = self._by_label.get(label)
        if root is not None and rest:
            found = self._accept(root, PurePosixPath(*rest))
            if found is not None:
                return found
        for root in self.roots:
            found = self._accept(root, rel)
            if found is not None:
                return found
        return None

    def _absolute(self, path: Path) -> SourceFile | None:
        real = Path(os.path.realpath(path))
        for root in self.roots:
            if real.is_relative_to(root.path):
                found = self._accept(root, PurePosixPath(real.relative_to(root.path).as_posix()))
                if found is not None:
                    return found
        return None

    def _accept(self, root: SourceRoot, rel: PurePosixPath, **kwargs: Any) -> SourceFile | None:
        """``rel`` under ``root`` if it is a readable Python file that stays inside the root."""
        if rel.is_absolute() or ".." in rel.parts or rel.suffix not in SOURCE_SUFFIXES:
            return None
        real = Path(os.path.realpath(root.path.joinpath(*rel.parts)))
        if not real.is_relative_to(root.path):
            return None
        try:
            if not stat.S_ISREG(real.stat().st_mode):
                return None
        except OSError:
            return None
        return SourceFile(root, rel, **kwargs)

    def _tail_match(self, parts: tuple[str, ...]) -> SourceFile | None:
        """The indexed file sharing the longest tail with ``parts``.

        At least the file name and its directory must match, unless ``parts`` is a bare file
        name; equal matches go to the earlier root, then to the shorter path.
        """
        if not parts:
            return None
        needed = min(2, len(parts))
        best: list[tuple[int, int, int, SourceRoot, tuple[str, ...]]] = []
        for order, root in enumerate(self.roots):
            for candidate in self._index(root).by_name.get(parts[-1], ()):
                score = _common_tail(parts, candidate)
                if score >= needed:
                    best.append((-score, order, len(candidate), root, candidate))
        if not best:
            return None
        best.sort(key=lambda item: item[:3])
        top_score = best[0][0]
        ties = sum(1 for item in best if item[0] == top_score) - 1
        for _, _, _, root, candidate in best:
            found = self._accept(root, PurePosixPath(*candidate), match="tail", alternatives=ties)
            if found is not None:
                return found
        return None

    # --------------------------------------------------------------------------------------
    # reading
    # --------------------------------------------------------------------------------------

    def read_lines(self, source: SourceFile) -> list[str]:
        """The file's lines, without line ends.

        Raises:
            ValueError: if the file is larger than MAX_FILE_BYTES.
            LookupError: if it vanished or left the root since it was resolved.
        """
        checked = self._accept(source.root, source.rel)
        if checked is None:
            raise LookupError(f"{source.display} is no longer a file under its source root")
        real = Path(os.path.realpath(checked.location))
        size = real.stat().st_size
        if size > MAX_FILE_BYTES:
            raise ValueError(
                f"{source.display} is {size // 1000} kB, more than code mode reads "
                f"({MAX_FILE_BYTES // 1000} kB)"
            )
        with real.open("r", encoding="utf-8", errors="replace") as handle:
            return handle.read().splitlines()

    def symbols(self, source: SourceFile, lines: list[str] | None = None) -> list[Symbol]:
        """Every function and class in the file, outermost first; [] if it does not parse."""
        real = Path(os.path.realpath(source.location))
        try:
            info = real.stat()
        except OSError:
            return []
        version = (info.st_mtime_ns, info.st_size)
        with self._symbols_lock:
            cached = self._symbols.get(real)
            if cached is not None and cached[0] == version:
                self._symbols.move_to_end(real)
                return cached[1]
        if lines is None:
            lines = self.read_lines(source)
        found = parse_symbols("\n".join(lines))
        with self._symbols_lock:
            self._symbols[real] = (version, found)
            while len(self._symbols) > _SYMBOL_CACHE_SIZE:
                self._symbols.popitem(last=False)
        return found

    def enclosing(self, symbols: list[Symbol], line: int) -> Symbol | None:
        """The innermost function or class spanning ``line``."""
        inside = [symbol for symbol in symbols if symbol.start <= line <= symbol.end]
        return max(inside, key=lambda symbol: symbol.start) if inside else None

    # --------------------------------------------------------------------------------------
    # walking
    # --------------------------------------------------------------------------------------

    def walk(self, root: SourceRoot, prefix: tuple[str, ...] = ()) -> Iterator[SourceFile]:
        """Every indexed Python file of ``root`` below ``prefix``, in path order."""
        index = self._index(root)
        paths = sorted(
            parts
            for candidates in index.by_name.values()
            for parts in candidates
            if parts[: len(prefix)] == prefix
        )
        for parts in paths:
            yield SourceFile(root, PurePosixPath(*parts))

    def root(self, label: str) -> SourceRoot | None:
        """The root with this label, if any."""
        return self._by_label.get(label)

    def _index(self, root: SourceRoot) -> _Index:
        index = self._indexes.get(root.label)
        if index is not None and self._clock() - index.built_at < self._index_ttl:
            return index
        # one walk per root at a time; a walk abandoned by a timed-out call still fills the
        # cache for the next one
        with self._index_locks[root.label]:
            index = self._indexes.get(root.label)
            if index is None or self._clock() - index.built_at >= self._index_ttl:
                index = _build_index(root.path)
                index.built_at = self._clock()
                self._indexes[root.label] = index
        return index


def _build_index(top: Path) -> _Index:
    index = _Index()
    stack = [(top, ())]
    while stack:
        directory, parts = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError:
            continue
        for entry in entries:
            name = entry.name
            try:
                if entry.is_dir(follow_symlinks=False):
                    if not name.startswith(".") and name not in _SKIPPED_DIRS:
                        stack.append((Path(entry.path), parts + (name,)))
                    continue
                if not name.endswith(SOURCE_SUFFIXES):
                    continue
                if entry.is_symlink():
                    real = Path(os.path.realpath(entry.path))
                    if not real.is_relative_to(top) or not real.is_file():
                        continue
                elif not entry.is_file(follow_symlinks=False):
                    continue
            except OSError:
                continue
            if index.files >= MAX_INDEXED_FILES:
                index.complete = False
                return index
            index.by_name.setdefault(name, []).append(parts + (name,))
            index.files += 1
    return index


def _common_tail(a: Sequence[str], b: Sequence[str]) -> int:
    count = 0
    for left, right in zip(reversed(a), reversed(b)):
        if left != right:
            break
        count += 1
    return count


def parse_symbols(text: str) -> list[Symbol]:
    """Functions and classes with qualified names (``Class.method``); [] on a syntax error."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    found: list[Symbol] = []

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}{child.name}"
                start = min([child.lineno] + [item.lineno for item in child.decorator_list])
                kind = "class" if isinstance(child, ast.ClassDef) else "function"
                found.append(Symbol(name, kind, start, child.end_lineno or child.lineno))
                visit(child, f"{name}.")
            elif not isinstance(child, (ast.expr, ast.Lambda)):
                # functions defined inside if/try/with blocks still count
                visit(child, prefix)

    visit(tree, "")
    found.sort(key=lambda symbol: (symbol.start, -symbol.end))
    return found
